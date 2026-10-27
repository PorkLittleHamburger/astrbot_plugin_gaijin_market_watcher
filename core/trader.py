"""自动交易引擎。

交易与行情推送刻意解耦：watch 只负责抓取 Quote，本模块通过独立队列串行
执行盘口确认、预算检查、下单、库存对账和状态持久化。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from . import platforms
from .api_client import AuthExpiredError, MarketApiError, TransportError
from .models import Quote
from .paper_engine import PaperError, PaperTradingEngine
from .paper_models import (
    PAPER_SIDE_BUY,
    PAPER_SIDE_SELL,
    PAPER_SOURCE_RULE,
    PAPER_SOURCE_SCENARIO,
)
from .trade_models import (
    CENT_RAW,
    PHASE_BUYING,
    PHASE_COMPLETED,
    PHASE_ERROR,
    PHASE_HOLDING,
    PHASE_LISTING,
    PHASE_PAUSED,
    PHASE_SELLING,
    PHASE_WAITING_BUY,
    PHASE_WORKING,
    TRADE_ITEM_COMMODITY,
    TRADE_MODE_ONCE,
    TRADE_MODES,
    BookSnapshot,
    TradeAssetAllocation,
    TradeEvent,
    TradeOrder,
    TradeRule,
    TradeRuntime,
    TradeSettings,
    fmt_gjn,
    parse_gjn_raw,
    raw_to_decimal,
    seller_receives_raw,
)
from .trade_store import TradeStorageError, TradeStore

SHANGHAI = ZoneInfo("Asia/Shanghai")
ARM_CONFIRM_TTL = 60
MAX_RULE_QUANTITY = 1_000
MAX_LISTING_RECONCILE_SECONDS = 30
SELL_RECONCILE_ATTEMPTS = 5
SELL_RECONCILE_DELAY = 0.8
BUY_CANCEL_VERIFY_ATTEMPTS = 3


class TradeError(RuntimeError):
    """可由用户修正的交易业务错误。"""


def _paper_integer(value: Any, message: str) -> int:
    """严格解析盘口接口中的整数字段，拒绝会丢失精度的小数值。"""
    if isinstance(value, bool):
        raise TradeError(message)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise TradeError(message)
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text[0] in "+-" and text[1:].isdigit())):
            return int(text)
    raise TradeError(message)


class AutoTrader:
    def __init__(self, *, client, auth, storage, config, accounts=None, notifier, logger, on_watch_change=None) -> None:
        self.client = client
        self.auth = auth
        self.config = config
        self.accounts = accounts or config
        self.notifier = notifier
        self.logger = logger
        self.store = TradeStore(storage, logger)
        self.paper = PaperTradingEngine(storage, logger)
        self._paper_limits_loaded_at = 0.0
        self._paper_fetch_locks: dict[str, asyncio.Lock] = {}
        self.on_watch_change = on_watch_change
        self._task: asyncio.Task | None = None
        self._running = False
        self._queue: asyncio.Queue[dict[str, Quote]] = asyncio.Queue(maxsize=2)
        self._action_lock = asyncio.Lock()
        self._pending_arms: dict[str, float] = {}
        self._next_listing_reconcile: dict[str, float] = {}
        self._last_mode_result: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if self.running:
            return False
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="gjm-auto-trader")
        return True

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def on_quotes(self, quotes: dict[str, Quote]) -> None:
        """由行情 watcher 调用，只做非阻塞入队。"""
        if not self.watch_market_names():
            return
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(dict(quotes))

    async def _loop(self) -> None:
        while self._running:
            try:
                quotes = await self._queue.get()
                async with self._action_lock:
                    for rule in list(self.store.rules):
                        if not self._is_rule_tradable(rule):
                            continue
                        quote = quotes.get(rule.market_name)
                        if quote is not None and quote.ok:
                            try:
                                await self._evaluate_rule(rule, quote)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                await self._fail_rule(rule, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error(f"[GJM Trade] 自动交易循环异常: {exc}")
                await asyncio.sleep(1)

    def watch_market_names(self) -> list[str]:
        names: list[str] = []
        for rule in self.store.rules:
            runtime = self.store.get_runtime(rule.rule_id)
            if rule.enabled and runtime.phase != PHASE_COMPLETED and rule.market_name not in names:
                names.append(rule.market_name)
        return names

    def poll_interval(self) -> int:
        return max(1, min(3600, int(self.store.settings.poll_interval or 5)))

    def rule_view(self, rule: TradeRule) -> dict[str, Any]:
        runtime = self.store.get_runtime(rule.rule_id)
        return {
            **rule.to_dict(),
            "title": rule.title,
            "buy_price": fmt_gjn(rule.buy_price_raw),
            "sell_net_price": fmt_gjn(rule.sell_net_price_raw),
            "sell_gross_price": fmt_gjn(rule.sell_gross_price_raw),
            "estimated_net": fmt_gjn(seller_receives_raw(rule.sell_gross_price_raw)),
            "runtime": runtime.to_dict(),
            "available_quantity": runtime.available_quantity,
            "profit_per_unit": fmt_gjn(rule.sell_net_price_raw - rule.buy_price_raw),
            "paper_orders": [
                self.paper._order_view(order) for order in self.paper.rule_orders(rule.rule_id) if order.is_open
            ],
        }

    def overview(self) -> dict[str, Any]:
        self._rollover_ledger()
        exposure = sum(self.store.get_runtime(rule.rule_id).cost_basis_raw for rule in self.store.rules if rule.live)
        return {
            "settings": {
                **self.store.settings.to_dict(),
                "total_budget": fmt_gjn(self.store.settings.total_budget_raw),
                "daily_limit": fmt_gjn(self.store.settings.daily_limit_raw),
                "max_order": fmt_gjn(self.store.settings.max_order_raw),
            },
            "ledger": {
                **self.store.ledger.to_dict(),
                "daily_spent": fmt_gjn(self.store.ledger.daily_spent_raw),
                "daily_remaining": fmt_gjn(
                    max(0, self.store.settings.daily_limit_raw - self.store.ledger.daily_spent_raw)
                ),
            },
            "exposure_raw": exposure,
            "exposure": fmt_gjn(exposure),
            "budget_remaining": fmt_gjn(max(0, self.store.settings.total_budget_raw - exposure)),
            "rules": [self.rule_view(rule) for rule in self.store.rules],
            "orders": [order for rule in self.store.rules for order in self.store.get_runtime(rule.rule_id).orders],
            "running": self.running,
            "mode": "live" if self.store.settings.live_mode_enabled else "paper",
            "live_rules": sum(1 for rule in self.store.rules if rule.live),
            "paper_rules": sum(1 for rule in self.store.rules if not rule.live),
            "last_mode_result": dict(self._last_mode_result),
            "paper": self.paper.overview(order_limit=20, fill_limit=20),
        }

    async def create_rule(
        self,
        *,
        url: str,
        buy_price: Any,
        quantity: Any,
        sell_net_price: Any,
        mode: str,
        display_name: str = "",
    ) -> TradeRule:
        async with self._action_lock:
            return await self._create_rule_unlocked(
                url=url,
                buy_price=buy_price,
                quantity=quantity,
                sell_net_price=sell_net_price,
                mode=mode,
                display_name=display_name,
            )

    async def _create_rule_unlocked(
        self,
        *,
        url: str,
        buy_price: Any,
        quantity: Any,
        sell_net_price: Any,
        mode: str,
        display_name: str = "",
    ) -> TradeRule:
        from .resolver import extract_reference

        _, market_name = extract_reference(str(url or ""))
        if not market_name:
            raise TradeError("请提供物品页链接")
        if market_name.startswith("__auction__"):
            raise TradeError("首版自动交易仅支持普通商品，不支持拍卖物品")
        try:
            buy_raw = parse_gjn_raw(buy_price)
            sell_net_raw = parse_gjn_raw(sell_net_price)
            qty = int(quantity)
        except (TypeError, ValueError) as exc:
            raise TradeError(str(exc)) from exc
        if not 1 <= qty <= MAX_RULE_QUANTITY:
            raise TradeError(f"数量必须在 1 到 {MAX_RULE_QUANTITY} 之间")
        mode = str(mode or "").lower()
        if mode not in TRADE_MODES:
            raise TradeError("交易周期只能是 once 或 loop")
        if sell_net_raw <= buy_raw:
            raise TradeError("税后卖出价必须高于买入价")
        if any(rule.market_name == market_name for rule in self.store.rules):
            raise TradeError("该物品已经存在自动交易规则")

        token = await self.auth.get_token()
        quote = await self.client.fetch_quote(market_name, token, appid=self.config.appid)
        if str(quote.kind or "").upper() not in ("", "COMMODITY"):
            raise TradeError("该物品不是普通商品，不能使用自动交易")
        asset_class = await self.client.get_asset_class(market_name, token, appid=self.config.appid)
        class_text = json.dumps(asset_class, ensure_ascii=False, sort_keys=True).lower()
        if '"auctionable": true' in class_text or "auction" in market_name:
            raise TradeError("该物品属于拍卖类型，首版不支持")

        now = time.time()
        rule = TradeRule(
            rule_id=self._new_rule_id(),
            market_name=market_name,
            display_name=str(display_name or quote.title or market_name),
            item_type=TRADE_ITEM_COMMODITY,
            buy_price_raw=buy_raw,
            quantity=qty,
            sell_net_price_raw=sell_net_raw,
            mode=mode,
            enabled=True,
            live=False,
            created_at=now,
            updated_at=now,
        )
        self.store.rules.append(rule)
        self.store.runtimes[rule.rule_id] = TradeRuntime(rule_id=rule.rule_id)
        try:
            self.store.save_rules()
            self.store.save_runtime()
        except TradeStorageError:
            self.store.reload()
            raise
        await self._record(
            rule,
            "created",
            f"已创建模拟交易规则 {rule.rule_id}：{rule.title}，买入 {fmt_gjn(buy_raw)}，"
            f"税后卖出 {fmt_gjn(sell_net_raw)}，数量 {qty}，模式 {mode}",
        )
        self._changed()
        return rule

    async def update_rule(self, rule_id: str, **changes: Any) -> TradeRule:
        async with self._action_lock:
            return await self._update_rule_unlocked(rule_id, **changes)

    async def _update_rule_unlocked(self, rule_id: str, **changes: Any) -> TradeRule:
        rule = self._require_rule(rule_id)
        if rule.live:
            raise TradeError("实盘规则不能直接修改，请先退出实盘")
        runtime = self.store.get_runtime(rule.rule_id)
        if (
            runtime.held_quantity
            or runtime.orders
            or runtime.phase in (PHASE_HOLDING, PHASE_LISTING)
            or self._paper_open_orders(rule.rule_id)
            or self.paper.active_scenario_for_rule(rule.rule_id) is not None
        ):
            raise TradeError("持仓、挂单或行情场景期间不能修改规则，请先撤单并清仓")
        buy_raw = (
            parse_gjn_raw(changes.get("buy_price", rule.buy_price)) if "buy_price" in changes else rule.buy_price_raw
        )
        sell_raw = (
            parse_gjn_raw(changes.get("sell_net_price", rule.sell_net_price))
            if "sell_net_price" in changes
            else rule.sell_net_price_raw
        )
        qty = int(changes.get("quantity", rule.quantity))
        mode = str(changes.get("mode", rule.mode)).lower()
        if not 1 <= qty <= MAX_RULE_QUANTITY:
            raise TradeError(f"数量必须在 1 到 {MAX_RULE_QUANTITY} 之间")
        if mode not in TRADE_MODES:
            raise TradeError("交易周期只能是 once 或 loop")
        if sell_raw <= buy_raw:
            raise TradeError("税后卖出价必须高于买入价")
        previous = rule.to_dict()
        rule.buy_price_raw = buy_raw
        rule.sell_net_price_raw = sell_raw
        rule.quantity = qty
        rule.mode = mode
        if "display_name" in changes:
            rule.display_name = str(changes.get("display_name") or rule.display_name)
        rule.updated_at = time.time()
        try:
            self.store.save_rules()
        except TradeStorageError:
            restored = TradeRule.from_dict(previous)
            self.store.rules = [restored if item.rule_id == rule.rule_id else item for item in self.store.rules]
            raise
        self._changed()
        await self._record(rule, "updated", f"已更新交易规则 {rule.rule_id}")
        return rule

    async def update_settings(self, payload: dict[str, Any]) -> TradeSettings:
        async with self._action_lock:
            return await self._update_settings_unlocked(payload)

    async def _update_settings_unlocked(self, payload: dict[str, Any]) -> TradeSettings:
        current = self.store.settings
        settings = TradeSettings(
            enabled=bool(payload.get("enabled", current.enabled)),
            total_budget_raw=self._optional_price(payload, "total_budget", current.total_budget_raw),
            daily_limit_raw=self._optional_price(payload, "daily_limit", current.daily_limit_raw),
            max_order_raw=self._optional_price(payload, "max_order", current.max_order_raw),
            max_quantity=max(0, int(payload.get("max_quantity", current.max_quantity) or 0)),
            poll_interval=max(1, min(3600, int(payload.get("poll_interval", current.poll_interval) or 5))),
            private_mode=bool(payload.get("private_mode", current.private_mode)),
            live_mode_enabled=bool(current.live_mode_enabled),
        )
        previous = self.store.settings
        self.store.settings = settings
        try:
            self.store.save_settings()
        except TradeStorageError:
            self.store.settings = previous
            raise
        self._changed()
        await self._record_system(
            "settings",
            "安全设置已更新\n"
            f"总开关：{'开' if settings.enabled else '关'}\n"
            f"总投入预算：{fmt_gjn(settings.total_budget_raw)} GJN\n"
            f"每日限额：{fmt_gjn(settings.daily_limit_raw)} GJN\n"
            f"单笔上限：{fmt_gjn(settings.max_order_raw)} GJN\n"
            f"最大数量：{settings.max_quantity}\n"
            f"检查间隔：{settings.poll_interval} 秒\n"
            f"交易环境：{'实盘许可已开启' if settings.live_mode_enabled else '模拟盘'}",
        )
        return settings

    async def set_global_mode(self, mode: str, confirmation: str = "") -> dict[str, Any]:
        """Batch-switch the global paper/live permission and eligible rules."""
        async with self._action_lock:
            target = str(mode or "").strip().lower()
            if target not in ("paper", "live"):
                raise TradeError("交易环境只能是 paper 或 live")
            if target == "live" and str(confirmation or "").strip().upper() != "LIVE":
                raise TradeError("切换实盘必须输入 LIVE 确认")

            previous = self.store.settings
            updated = TradeSettings.from_dict(previous.to_dict())
            updated.live_mode_enabled = target == "live"
            self.store.settings = updated
            try:
                self.store.save_settings()
            except TradeStorageError:
                self.store.settings = previous
                raise

            switched: list[dict[str, str]] = []
            skipped: list[dict[str, str]] = []
            already: list[dict[str, str]] = []
            for rule in list(self.store.rules):
                item = {"rule_id": rule.rule_id, "title": rule.title}
                if (target == "live" and rule.live) or (target == "paper" and not rule.live):
                    already.append(item)
                    continue
                try:
                    if target == "live":
                        runtime = self.store.get_runtime(rule.rule_id)
                        if runtime.phase == PHASE_ERROR:
                            raise TradeError("错误规则不能切换实盘")
                        if runtime.phase in (PHASE_COMPLETED, PHASE_PAUSED) or not rule.enabled:
                            skipped.append({**item, "reason": "规则已暂停或已完成，保留原状态"})
                            continue
                        await self._prepare_arm_unlocked(rule.rule_id)
                        try:
                            await self._confirm_arm_unlocked(rule.rule_id, rule.rule_id)
                        except Exception:
                            self._pending_arms.pop(rule.rule_id, None)
                            raise
                    else:
                        await self._disarm_unlocked(rule.rule_id)
                    switched.append(item)
                except TradeError as exc:
                    skipped.append({**item, "reason": str(exc)})
                except Exception as exc:  # noqa: BLE001 - report each rule without aborting the batch
                    self._pending_arms.pop(rule.rule_id, None)
                    self.logger.warning(f"[GJM Trade] 批量切换 {rule.rule_id} 失败: {exc}")
                    skipped.append({**item, "reason": str(exc) or "切换失败"})

            result = {
                "mode": target,
                "switched": switched,
                "skipped": skipped,
                "already": already,
                "at": time.time(),
            }
            self._last_mode_result = dict(result)
            mode_text = "实盘" if target == "live" else "模拟盘"
            lines = [f"全局交易环境已切换为{mode_text}"]
            if switched:
                lines.append("已切换：" + "、".join(item["rule_id"] for item in switched))
            if skipped:
                lines.append("已跳过：")
                lines.extend(f"- {item['rule_id']}：{item['reason']}" for item in skipped)
            if already:
                lines.append("无需切换：" + "、".join(item["rule_id"] for item in already))
            await self._record_system("mode_changed", "\n".join(lines))
            self._changed()
            return result

    async def prepare_arm(self, rule_id: str) -> str:
        async with self._action_lock:
            return await self._prepare_arm_unlocked(rule_id)

    async def _prepare_arm_unlocked(self, rule_id: str) -> str:
        rule = self._require_rule(rule_id)
        runtime = self.store.get_runtime(rule.rule_id)
        if rule.live:
            raise TradeError("规则已经在实盘模式")
        if (
            runtime.held_quantity
            or runtime.orders
            or runtime.phase in (PHASE_HOLDING, PHASE_LISTING)
            or self._paper_open_orders(rule.rule_id)
            or self.paper.active_scenario_for_rule(rule.rule_id) is not None
        ):
            raise TradeError("模拟持仓、订单或行情场景尚未清空，不能切换实盘")
        settings = self.store.settings
        if not settings.live_mode_enabled:
            raise TradeError("实盘总开关未开启，请先在交易设置中切换为实盘模式")
        if not settings.enabled:
            raise TradeError("自动交易总开关未开启")
        if not (settings.total_budget_raw > 0 and settings.daily_limit_raw > 0 and settings.max_order_raw > 0):
            raise TradeError("请先设置总预算、每日限额和单笔上限，且均需大于 0")
        if settings.max_quantity <= 0:
            raise TradeError("请先设置最大交易数量")
        if not self.accounts.owner_uids:
            raise TradeError("必须配置拥有者 UID，交易不会回退到 AstrBot 管理员判定")
        cost = rule.buy_price_raw * rule.quantity
        if cost > settings.max_order_raw:
            raise TradeError(f"规则最大买入额 {fmt_gjn(cost)} 超过单笔上限 {fmt_gjn(settings.max_order_raw)}")
        if rule.quantity > settings.max_quantity:
            raise TradeError(f"规则数量超过全局最大数量 {settings.max_quantity}")
        exposure = sum(self.store.get_runtime(item.rule_id).cost_basis_raw for item in self.store.rules if item.live)
        if exposure + cost > settings.total_budget_raw:
            raise TradeError("规则买入额将超过全局总投入预算")
        self._rollover_ledger()
        if self.store.ledger.daily_spent_raw + cost > settings.daily_limit_raw:
            raise TradeError("规则买入额将超过今日剩余额度")
        self._pending_arms[rule.rule_id] = time.time() + ARM_CONFIRM_TTL
        return (
            f"即将把规则 {rule.rule_id} 切换为实盘：\n"
            f"物品：{rule.title}\n"
            f"买入限价：{fmt_gjn(rule.buy_price_raw)} GJN × {rule.quantity} = {fmt_gjn(cost)} GJN\n"
            f"税后目标：{fmt_gjn(rule.sell_net_price_raw)} GJN\n"
            f"对应税前挂单价：{fmt_gjn(rule.sell_gross_price_raw)} GJN（固定 15% 手续费）\n"
            f"模式：{rule.mode}\n"
            f"请在 {ARM_CONFIRM_TTL} 秒内再次确认规则 ID {rule.rule_id}。"
        )

    async def confirm_arm(self, rule_id: str, confirmation_id: str) -> TradeRule:
        async with self._action_lock:
            return await self._confirm_arm_unlocked(rule_id, confirmation_id)

    async def _confirm_arm_unlocked(self, rule_id: str, confirmation_id: str) -> TradeRule:
        rule = self._require_rule(rule_id)
        expires = self._pending_arms.get(rule.rule_id, 0)
        if expires < time.time():
            self._pending_arms.pop(rule.rule_id, None)
            raise TradeError("实盘确认已过期，请重新发起")
        if str(confirmation_id).strip().upper() != rule.rule_id.upper():
            raise TradeError("确认 ID 不匹配")
        self._check_live_limits(rule)
        token = await self.auth.get_token()
        orders = await self.client.get_open_orders(token)
        if any(self._order_market(item) == rule.market_name for item in orders):
            raise TradeError("该物品存在开放订单，请先在官方市场处理后再切换实盘")
        runtime = self.store.get_runtime(rule.rule_id)
        runtime.reset_for_cycle()
        rule.live = True
        rule.enabled = True
        rule.updated_at = time.time()
        self._pending_arms.pop(rule.rule_id, None)
        self.store.save_rules()
        self.store.save_runtime()
        self._changed()
        await self._record(rule, "armed", f"规则 {rule.rule_id} 已切换为实盘，等待买入条件")
        return rule

    async def disarm(self, rule_id: str) -> TradeRule:
        async with self._action_lock:
            return await self._disarm_unlocked(rule_id)

    async def _disarm_unlocked(self, rule_id: str) -> TradeRule:
        rule = self._require_rule(rule_id)
        runtime = self.store.get_runtime(rule.rule_id)
        if runtime.held_quantity or runtime.orders:
            raise TradeError("仍有持仓或自动挂单，请先执行撤单/清仓")
        rule.live = False
        rule.enabled = False
        rule.updated_at = time.time()
        runtime.reset_for_cycle()
        self.store.save_rules()
        self.store.save_runtime()
        self._changed()
        await self._record(rule, "disarmed", f"规则 {rule.rule_id} 已退出实盘")
        return rule

    async def action(self, rule_id: str, action: str, confirmation_id: str = "") -> TradeRule:
        async with self._action_lock:
            return await self._action_unlocked(rule_id, action, confirmation_id)

    async def _action_unlocked(self, rule_id: str, action: str, confirmation_id: str = "") -> TradeRule:
        action = str(action or "").lower()
        if action == "prepare_arm":
            await self._prepare_arm_unlocked(rule_id)
            return self._require_rule(rule_id)
        if action == "arm":
            return await self._confirm_arm_unlocked(rule_id, confirmation_id)
        if action == "disarm":
            return await self._disarm_unlocked(rule_id)
        rule = self._require_rule(rule_id)
        runtime = self.store.get_runtime(rule.rule_id)
        if action == "pause":
            rule.enabled = False
            await self._record(rule, "paused", f"规则 {rule.rule_id} 已暂停")
        elif action == "resume":
            if runtime.phase == PHASE_ERROR:
                raise TradeError("错误规则请先处理错误或删除重建")
            rule.enabled = True
            await self._record(rule, "resumed", f"规则 {rule.rule_id} 已恢复")
        elif action == "cancel_orders":
            await self._cancel_rule_orders(rule, pause=True)
        elif action == "delete":
            if (
                runtime.held_quantity
                or runtime.orders
                or runtime.phase in (PHASE_HOLDING, PHASE_LISTING)
                or self._paper_open_orders(rule.rule_id)
                or self.paper.active_scenario_for_rule(rule.rule_id) is not None
            ):
                raise TradeError("仍有持仓、自动挂单或行情场景，不能删除规则")
            self.store.rules = [item for item in self.store.rules if item.rule_id != rule.rule_id]
            self.store.runtimes.pop(rule.rule_id, None)
            self.store.save_rules()
            self.store.save_runtime()
            self._changed()
            await self._record(rule, "deleted", f"规则 {rule.rule_id} 已删除")
            return rule
        else:
            raise TradeError("不支持的操作")
        rule.updated_at = time.time()
        self.store.save_rules()
        self.store.save_runtime()
        self._changed()
        return rule

    async def stop_all(self) -> dict[str, Any]:
        async with self._action_lock:
            return await self._stop_all_unlocked()

    async def _stop_all_unlocked(self) -> dict[str, Any]:
        cancelled: list[str] = []
        errors: list[str] = []
        for rule in list(self.store.rules):
            runtime = self.store.get_runtime(rule.rule_id)
            scenario = self.paper.active_scenario_for_rule(rule.rule_id)
            paper_open = any(order.is_open for order in self.paper.rule_orders(rule.rule_id))
            if scenario is not None:
                paper_open = paper_open or any(
                    order.is_open for order in self.paper.rule_orders(rule.rule_id, scenario_id=scenario.scenario_id)
                )
            if runtime.orders or paper_open:
                try:
                    await self._cancel_rule_orders(rule, pause=False)
                    cancelled.append(rule.rule_id)
                except Exception as exc:
                    errors.append(f"{rule.rule_id}: {exc}")
            rule.enabled = False
            rule.updated_at = time.time()
        self.store.save_rules()
        self.store.save_runtime()
        self._changed()
        text = "自动交易急停：\n所有规则已暂停。"
        if cancelled:
            text += f"\n已撤销订单：{', '.join(cancelled)}"
        if errors:
            text += "\n撤单异常：\n" + "\n".join(errors)
        await self._record_system("stop_all", text)
        return {"cancelled": cancelled, "errors": errors}

    def _paper_open_orders(self, rule_id: str) -> list:
        orders = [order for order in self.paper.rule_orders(rule_id) if order.is_open]
        scenario = self.paper.active_scenario_for_rule(rule_id)
        if scenario is not None:
            orders.extend(
                order for order in self.paper.rule_orders(rule_id, scenario_id=scenario.scenario_id) if order.is_open
            )
        return orders

    def _is_rule_tradable(self, rule: TradeRule) -> bool:
        runtime = self.store.get_runtime(rule.rule_id)
        if not rule.enabled or runtime.phase in (PHASE_COMPLETED, PHASE_ERROR, PHASE_PAUSED):
            return False
        if not self.store.settings.enabled:
            return False
        if rule.live and runtime.phase == PHASE_WAITING_BUY and not self.store.settings.live_mode_enabled:
            return False
        return runtime.phase in (
            PHASE_WAITING_BUY,
            PHASE_BUYING,
            PHASE_HOLDING,
            PHASE_SELLING,
            PHASE_WORKING,
            PHASE_LISTING,
        )

    async def _evaluate_rule(self, rule: TradeRule, quote: Quote) -> None:
        if not rule.live:
            await self._evaluate_paper_rule(rule)
            return
        runtime = self.store.get_runtime(rule.rule_id)
        if runtime.phase == PHASE_WAITING_BUY:
            sell_raw = self._quote_raw(quote.sell_min)
            if sell_raw and sell_raw <= rule.buy_price_raw:
                await self._live_buy(rule, quote)
            return
        if runtime.phase == PHASE_HOLDING:
            await self._live_sell(rule, quote)
            return
        if runtime.phase == PHASE_LISTING:
            await self._reconcile_live_listing(rule)

    async def _ensure_paper_limits(self, token: str) -> None:
        if time.time() - self._paper_limits_loaded_at < 3600:
            return
        try:
            info = await self.client.get_market_info(token)
            min_price = int(info.get("minPrice") or info.get("min_price") or self.paper.price_min_raw)
            max_price = int(info.get("maxPrice") or info.get("max_price") or self.paper.price_max_raw)
            self.paper.set_price_limits(min_price, max_price)
        except Exception as exc:
            self.logger.warning(f"[GJM Paper] 读取市场限价失败，保留默认值: {exc}")
        self._paper_limits_loaded_at = time.time()

    async def _fetch_paper_book(self, market_name: str, token: str) -> tuple[float, BookSnapshot]:
        lock = self._paper_fetch_locks.setdefault(market_name, asyncio.Lock())
        async with lock:
            book = await self.client.fetch_book(market_name, token, appid=self.config.appid)
            return time.time(), book

    @staticmethod
    def _require_commodity_book(book: BookSnapshot) -> None:
        if str(book.kind or "").upper() not in ("", "COMMODITY") or book.auctionable:
            raise TradeError("该物品不是可模拟的普通商品")

    async def _evaluate_paper_rule(self, rule: TradeRule) -> None:
        token = await self.auth.get_token()
        await self._ensure_paper_limits(token)
        observed_at, book = await self._fetch_paper_book(rule.market_name, token)
        self._require_commodity_book(book)
        await self._record_any_paper_fills(self.paper.advance_scenarios())
        await self._record_any_paper_fills(self.paper.observe_market(book, observed_at=observed_at))
        scenario = self.paper.active_scenario_for_rule(rule.rule_id)
        if scenario is not None:
            await self._execute_paper_rule(rule, scenario.scenario_id, update_runtime=False)
        else:
            await self._execute_paper_rule(rule, "", update_runtime=True)

    async def _record_paper_fills(self, rule: TradeRule, fills: list) -> None:
        for fill in fills:
            if fill.side == PAPER_SIDE_BUY:
                kind = "paper_buy"
                text = f"[模拟] {rule.title} 买入 {fill.quantity} 个，成交价 {fmt_gjn(fill.price_raw)} GJN"
            else:
                kind = "paper_sell"
                text = (
                    f"[模拟] {rule.title} 卖出 {fill.quantity} 个，成交价 {fmt_gjn(fill.price_raw)} GJN，"
                    f"税后 {fmt_gjn(fill.net_raw)} GJN"
                )
            if fill.scenario_id:
                text = "[场景] " + text
            await self._record(
                rule,
                kind,
                text,
                details={
                    "trade_side": fill.side,
                    "trade_price": fmt_gjn(fill.price_raw),
                    "trade_quantity": fill.quantity,
                    "trade_amount": fmt_gjn(fill.gross_raw),
                    "trade_net_price": fmt_gjn(int(fill.net_raw / fill.quantity) if fill.quantity else 0),
                },
            )

    async def _execute_paper_rule(self, rule: TradeRule, scenario_id: str, *, update_runtime: bool) -> None:
        runtime = self.store.get_runtime(rule.rule_id)
        cycle_id = runtime.paper_cycle_id or self.paper.new_cycle_id(rule.rule_id, runtime.cycle_count)
        if update_runtime and not runtime.paper_cycle_id:
            runtime.paper_cycle_id = cycle_id
            self.store.save_runtime()
        orders = self.paper.rule_orders(rule.rule_id, cycle_id, scenario_id=scenario_id)
        fills = self.paper.rule_fills(rule.rule_id, cycle_id, scenario_id=scenario_id)
        book = self.paper.current_book(rule.market_name, scenario_id=scenario_id)
        if book is None:
            return
        buy_filled = sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_BUY)
        sell_filled = sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_SELL)
        open_buy = [order for order in orders if order.is_open and order.side == PAPER_SIDE_BUY]
        open_sell = [order for order in orders if order.is_open and order.side == PAPER_SIDE_SELL]
        remaining_buy = max(0, rule.quantity - buy_filled)
        if not open_buy and remaining_buy > 0 and book.best_sell is not None and book.best_sell <= rule.buy_price_raw:
            try:
                self._check_paper_risk(rule, remaining_buy * rule.buy_price_raw)
                _, new_fills = self.paper.place_order(
                    market_name=rule.market_name,
                    side=PAPER_SIDE_BUY,
                    price_raw=rule.buy_price_raw,
                    quantity=remaining_buy,
                    source=PAPER_SOURCE_SCENARIO if scenario_id else PAPER_SOURCE_RULE,
                    rule_id=rule.rule_id,
                    cycle_id=cycle_id,
                    client_order_id=(
                        f"{cycle_id}:BUY:{buy_filled}:{sum(1 for item in orders if item.side == PAPER_SIDE_BUY)}"
                    ),
                    scenario_id=scenario_id,
                )
                await self._record_any_paper_fills(new_fills)
            except (PaperError, TradeError) as exc:
                self.logger.warning(f"[GJM Trade] 模拟买入 {rule.rule_id} 跳过: {exc}")
        orders = self.paper.rule_orders(rule.rule_id, cycle_id, scenario_id=scenario_id)
        fills = self.paper.rule_fills(rule.rule_id, cycle_id, scenario_id=scenario_id)
        buy_filled = sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_BUY)
        sell_filled = sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_SELL)
        open_sell = [order for order in orders if order.is_open and order.side == PAPER_SIDE_SELL]
        open_sell_quantity = sum(order.remaining_quantity for order in open_sell)
        desired_sell = max(0, buy_filled - sell_filled - open_sell_quantity)
        available_inventory = self.paper.available_inventory(rule.market_name, scenario_id=scenario_id)
        if desired_sell > 0 and available_inventory >= desired_sell:
            try:
                _, new_fills = self.paper.place_order(
                    market_name=rule.market_name,
                    side=PAPER_SIDE_SELL,
                    price_raw=rule.sell_gross_price_raw,
                    quantity=desired_sell,
                    source=PAPER_SOURCE_SCENARIO if scenario_id else PAPER_SOURCE_RULE,
                    rule_id=rule.rule_id,
                    cycle_id=cycle_id,
                    client_order_id=(
                        f"{cycle_id}:SELL:{sell_filled}:{sum(1 for item in orders if item.side == PAPER_SIDE_SELL)}"
                    ),
                    scenario_id=scenario_id,
                )
                await self._record_any_paper_fills(new_fills)
            except PaperError as exc:
                self.logger.warning(f"[GJM Trade] 模拟卖出 {rule.rule_id} 跳过: {exc}")
        if not update_runtime:
            return
        orders = self.paper.rule_orders(rule.rule_id, cycle_id)
        fills = self.paper.rule_fills(rule.rule_id, cycle_id)
        buy_filled = sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_BUY)
        sell_filled = sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_SELL)
        open_buy = [order for order in orders if order.is_open and order.side == PAPER_SIDE_BUY]
        open_sell = [order for order in orders if order.is_open and order.side == PAPER_SIDE_SELL]
        runtime.held_quantity = max(0, buy_filled - sell_filled)
        runtime.listed_quantity = sum(order.remaining_quantity for order in open_sell)
        runtime.cost_basis_raw = sum(fill.gross_raw for fill in fills if fill.side == PAPER_SIDE_BUY)
        runtime.proceeds_raw = sum(fill.net_raw for fill in fills if fill.side == PAPER_SIDE_SELL)
        buy_fills = [fill for fill in fills if fill.side == PAPER_SIDE_BUY]
        sell_fills = [fill for fill in fills if fill.side == PAPER_SIDE_SELL]
        if buy_fills:
            runtime.last_buy_price_raw = self._weighted_price(sum(fill.gross_raw for fill in buy_fills), buy_filled)
        if sell_fills:
            runtime.last_sell_price_raw = self._weighted_price(sum(fill.gross_raw for fill in sell_fills), sell_filled)
        runtime.updated_at = time.time()
        if open_buy and open_sell:
            runtime.phase = PHASE_WORKING
        elif open_buy:
            runtime.phase = PHASE_BUYING
        elif open_sell or sell_filled < buy_filled:
            runtime.phase = PHASE_SELLING
        else:
            runtime.phase = PHASE_WAITING_BUY if buy_filled <= 0 else PHASE_HOLDING
        self.store.save_runtime()
        if buy_filled >= rule.quantity and sell_filled >= rule.quantity and not open_buy and not open_sell:
            await self._complete_paper_cycle(rule, runtime)

    def _check_paper_risk(self, rule: TradeRule, order_cost_raw: int) -> None:
        settings = self.store.settings
        if not settings.enabled:
            raise TradeError("自动交易总开关未开启")
        if settings.max_quantity <= 0 or rule.quantity > settings.max_quantity:
            raise TradeError("交易数量超过全局上限")
        if settings.max_order_raw <= 0 or order_cost_raw > settings.max_order_raw:
            raise TradeError("模拟买入额超过单笔上限")
        if settings.daily_limit_raw <= 0:
            raise TradeError("每日限额未配置")
        if settings.total_budget_raw <= 0:
            raise TradeError("总投入预算未配置")
        self.paper.rollover_day()
        daily_spent = self.paper.account.daily_buy_raw
        if daily_spent + order_cost_raw > settings.daily_limit_raw:
            raise TradeError("本次模拟买入将超过每日累计限额")
        open_buy_reserved = sum(
            order.remaining_quantity * order.price_raw
            for order in self.paper.orders
            if order.is_open and order.side == PAPER_SIDE_BUY
        )
        exposure = self.paper.account.inventory_cost_raw() + open_buy_reserved
        if exposure + order_cost_raw > settings.total_budget_raw:
            raise TradeError("本次模拟买入将超过全局总投入预算")

    async def _complete_paper_cycle(self, rule: TradeRule, runtime) -> None:
        runtime.cycle_count += 1
        if rule.mode == TRADE_MODE_ONCE:
            runtime.phase = PHASE_COMPLETED
            rule.enabled = False
            runtime.held_quantity = 0
            runtime.listed_quantity = 0
        else:
            runtime.reset_for_cycle()
        rule.updated_at = time.time()
        self.store.save_rules()
        self.store.save_runtime()
        self._changed()
        await self._record(rule, "cycle_complete", f"{rule.title} 模拟周期完成，轮次 {runtime.cycle_count}")

    async def paper_overview(self, scenario_id: str = "") -> dict[str, Any]:
        async with self._action_lock:
            await self._record_any_paper_fills(self.paper.advance_scenarios())
            return self.paper.overview(scenario_id=scenario_id)

    async def paper_market(self, market_name: str, *, scenario_id: str = "") -> dict[str, Any]:
        token = await self.auth.get_token()
        await self._ensure_paper_limits(token)
        observed_at, book = await self._fetch_paper_book(market_name, token)
        self._require_commodity_book(book)
        async with self._action_lock:
            await self._record_any_paper_fills(self.paper.advance_scenarios())
            await self._record_any_paper_fills(self.paper.observe_market(book, observed_at=observed_at))
            return self.paper.market_view(market_name, scenario_id=scenario_id)

    async def paper_place_order(
        self,
        *,
        market_name: str,
        side: str,
        price_raw: int,
        quantity: int,
        client_order_id: str = "",
        scenario_id: str = "",
    ) -> dict[str, Any]:
        if client_order_id:
            async with self._action_lock:
                existing = self.paper.lookup_client_order(
                    client_order_id,
                    market_name=market_name,
                    side=side,
                    price_raw=price_raw,
                    quantity=quantity,
                    scenario_id=scenario_id,
                )
                if existing is not None:
                    return {
                        "order": self.paper._order_view(existing),
                        "fills": [],
                        "overview": self.paper.overview(scenario_id=scenario_id),
                    }
        token = await self.auth.get_token()
        await self._ensure_paper_limits(token)
        observed_at, book = await self._fetch_paper_book(market_name, token)
        self._require_commodity_book(book)
        async with self._action_lock:
            await self._record_any_paper_fills(self.paper.advance_scenarios())
            await self._record_any_paper_fills(self.paper.observe_market(book, observed_at=observed_at))
            order, fills = self.paper.place_order(
                market_name=market_name,
                side=side,
                price_raw=price_raw,
                quantity=quantity,
                client_order_id=client_order_id,
                scenario_id=scenario_id,
            )
            await self._record_any_paper_fills(fills)
            return {
                "order": self.paper._order_view(order),
                "fills": [self.paper._fill_view(fill) for fill in fills],
                "overview": self.paper.overview(scenario_id=scenario_id),
            }

    async def paper_cancel_order(self, order_id: str, *, scenario_id: str = "") -> dict[str, Any]:
        async with self._action_lock:
            order = self.paper.cancel_order(order_id, scenario_id=scenario_id)
            return {
                "order": self.paper._order_view(order),
                "overview": self.paper.overview(scenario_id=scenario_id),
            }

    async def paper_create_scenario(
        self,
        *,
        rule_id: str,
        delay_seconds: float = 5.0,
        duration_seconds: float = 30.0,
    ) -> dict[str, Any]:
        async with self._action_lock:
            rule = self._require_rule(rule_id)
            if rule.live:
                raise TradeError("实盘规则不能生成模拟场景")
            market_name = rule.market_name
            buy_price_raw = rule.buy_price_raw
            sell_net_price_raw = rule.sell_net_price_raw
            quantity = rule.quantity
            runtime = self.store.get_runtime(rule.rule_id)
            cycle_id = runtime.paper_cycle_id or self.paper.new_cycle_id(rule.rule_id, runtime.cycle_count)
        token = await self.auth.get_token()
        await self._ensure_paper_limits(token)
        observed_at, book = await self._fetch_paper_book(market_name, token)
        self._require_commodity_book(book)
        async with self._action_lock:
            await self._record_any_paper_fills(self.paper.advance_scenarios())
            await self._record_any_paper_fills(self.paper.observe_market(book, observed_at=observed_at))
            scenario = self.paper.create_scenario(
                rule_id=rule_id,
                market_name=market_name,
                buy_price_raw=buy_price_raw,
                sell_net_price_raw=sell_net_price_raw,
                quantity=quantity,
                cycle_id=cycle_id,
                delay_seconds=delay_seconds,
                duration_seconds=duration_seconds,
            )
            return scenario

    async def paper_close_scenario(self, scenario_id: str) -> dict[str, Any]:
        async with self._action_lock:
            return self.paper.close_scenario(scenario_id)

    async def paper_reset(self, starting_cash_raw: int) -> dict[str, Any]:
        async with self._action_lock:
            account = self.paper.reset_account(int(starting_cash_raw))
            for rule in self.store.rules:
                if rule.live:
                    continue
                runtime = self.store.get_runtime(rule.rule_id)
                runtime.reset_for_cycle()
                runtime.proceeds_raw = 0
                runtime.cycle_count = 0
                runtime.last_buy_price_raw = 0
                runtime.last_sell_price_raw = 0
                runtime.last_error = ""
            self.store.save_runtime()
            self._changed()
            return {"account": account, "overview": self.paper.overview()}

    async def _record_any_paper_fills(self, fills: list) -> None:
        for fill in fills:
            rule = self.store.get_rule(fill.rule_id) if fill.rule_id else None
            if rule is not None:
                await self._record_paper_fills(rule, [fill])
            else:
                action = "买入" if fill.side == PAPER_SIDE_BUY else "卖出"
                await self._record_system(
                    "paper_manual_fill",
                    f"[模拟手工{action}] {fill.market_name} {fill.quantity} 个，成交价 {fmt_gjn(fill.price_raw)} GJN",
                    {
                        "trade_side": fill.side,
                        "trade_price": fmt_gjn(fill.price_raw),
                        "trade_quantity": fill.quantity,
                        "trade_amount": fmt_gjn(fill.gross_raw),
                        "trade_net_price": fmt_gjn(int(fill.net_raw / fill.quantity) if fill.quantity else 0),
                    },
                )

    async def _record_manual_paper_fills(self, fills: list) -> None:
        await self._record_any_paper_fills(fills)

    async def _live_buy(self, rule: TradeRule, quote: Quote) -> None:
        price_raw = self._quote_raw(quote.sell_min)
        if not price_raw or price_raw > rule.buy_price_raw:
            return
        self._check_live_limits(rule)
        token = await self.auth.get_token()
        book = await self.client.fetch_book(rule.market_name, token, appid=self.config.appid)
        if str(book.kind or "").upper() not in ("", "COMMODITY") or book.auctionable:
            raise TradeError("市场标记该物品为拍卖/特殊物品，已停止自动交易")
        fill, _ = self._fillable_buy(book, rule.buy_price_raw, rule.quantity)
        if fill <= 0:
            return
        await self._check_budget(fill * rule.buy_price_raw, rule)
        before_orders = await self.client.get_open_orders(token)
        if any(self._order_market(item) == rule.market_name for item in before_orders):
            raise TradeError("该物品存在开放订单，自动买入已暂停")
        before_inventory = await self._inventory_snapshot(rule, token)
        request_ok = True
        transact_id = self.client.new_transaction_id()
        submitted = TradeOrder(
            order_id="",
            pair_id="",
            side="BUY",
            amount=fill,
            price_raw=rule.buy_price_raw,
            market_name=rule.market_name,
            transaction_id=transact_id,
        )
        try:
            response = await self.client.place_buy_order(
                rule.market_name,
                fill,
                rule.buy_price_raw,
                token,
                transact_id=transact_id,
                appid=self.config.appid,
                private_mode=self.store.settings.private_mode,
            )
            submitted = self._order_from_response(
                response,
                side="BUY",
                amount=fill,
                price_raw=rule.buy_price_raw,
                market_name=rule.market_name,
                transaction_id=transact_id,
            )
        except TransportError as exc:
            request_ok = False
            self.logger.warning(f"[GJM Trade] 买单结果不确定，开始对账: {exc}")

        acquired, allocations, remaining = await self._reconcile_buy(
            rule,
            token,
            before_orders,
            before_inventory,
            fill,
            request_ok=request_ok,
            submitted=submitted,
        )
        runtime = self.store.get_runtime(rule.rule_id)
        if remaining:
            runtime.last_error = "检测到无法确认归属或未撤销的开放买单"
            runtime.phase = PHASE_ERROR
            self.store.save_runtime()
            raise TradeError("检测到无法确认归属或未撤销的开放买单，已暂停规则，请到官方市场检查订单")
        if acquired <= 0:
            message = (
                "买单回执成功，但库存未检测到新增物品，已暂停并等待人工核对"
                if request_ok
                else "买单结果不确定，且库存未确认新增物品，已暂停并等待人工核对"
            )
            raise TradeError(message)

        actual_cost = self._buy_cost_for_quantity(book, rule.buy_price_raw, acquired)
        average_price = self._weighted_price(actual_cost, acquired)
        runtime.phase = PHASE_HOLDING
        runtime.held_quantity = acquired
        runtime.listed_quantity = 0
        runtime.cost_basis_raw = actual_cost
        runtime.assets = allocations
        runtime.last_buy_price_raw = average_price
        runtime.last_error = ""
        runtime.updated_at = time.time()
        self.store.ledger.daily_spent_raw += actual_cost
        self.store.save_runtime()
        self.store.save_ledger()
        partial = "（部分成交）" if acquired < fill else ""
        await self._record(
            rule,
            "buy",
            f"{rule.title} 实际买入 {acquired}/{fill} 个，均价 {fmt_gjn(average_price)} GJN{partial}",
            details={
                "trade_side": "BUY",
                "trade_price": fmt_gjn(average_price),
                "trade_quantity": acquired,
                "trade_amount": fmt_gjn(actual_cost),
            },
        )
        await self._live_sell(rule, quote)

    async def _live_sell(self, rule: TradeRule, quote: Quote) -> None:
        token = await self.auth.get_token()
        book = await self.client.fetch_book(rule.market_name, token, appid=self.config.appid)
        runtime = self.store.get_runtime(rule.rule_id)
        asset_quantities = [
            (asset.context_id, asset.asset_id, asset.quantity) for asset in runtime.assets if asset.quantity > 0
        ]
        available = min(runtime.available_quantity, sum(item[2] for item in asset_quantities))
        if available <= 0:
            await self._complete_cycle(rule)
            return

        chunks: list[tuple[str, str, int, int]] = []
        cursor = self._asset_cursor(asset_quantities)
        remaining = available
        for level in book.buy_levels:
            if remaining <= 0:
                break
            if seller_receives_raw(level.price_raw) < rule.sell_net_price_raw:
                continue
            take = min(remaining, level.amount)
            chunks.extend(self._take_asset_chunks(cursor, take, level.price_raw))
            remaining -= take
        if remaining > 0:
            chunks.extend(self._take_asset_chunks(cursor, remaining, rule.sell_gross_price_raw))

        before_orders = await self.client.get_open_orders(token)
        unowned_before = self._unowned_market_orders(rule, before_orders, runtime.orders)
        if unowned_before:
            raise TradeError("该物品存在无法确认归属的开放订单，自动卖出已暂停")
        before_ids = {self._order_id(item) for item in before_orders if self._order_id(item)}
        original_held = runtime.held_quantity
        submitted: list[TradeOrder] = []
        submission_success: dict[int, bool] = {}
        request_error = ""
        for context_id, asset_id, amount, price_raw in chunks:
            transact_id = self.client.new_transaction_id()
            submitted_order = TradeOrder(
                order_id="",
                pair_id="",
                side="SELL",
                amount=amount,
                price_raw=price_raw,
                market_name=rule.market_name,
                transaction_id=transact_id,
            )
            try:
                response = await self.client.place_sell_order(
                    context_id,
                    asset_id,
                    amount,
                    price_raw,
                    seller_receives_raw(price_raw),
                    token,
                    transact_id=transact_id,
                    appid=self.config.appid,
                    private_mode=self.store.settings.private_mode,
                )
                submitted_order = self._order_from_response(
                    response,
                    side="SELL",
                    amount=amount,
                    price_raw=price_raw,
                    market_name=rule.market_name,
                    transaction_id=transact_id,
                )
                submission_success[transact_id] = True
            except TransportError as exc:
                self.logger.warning(f"[GJM Trade] 卖单结果不确定，开始对账: {exc}")
                submission_success[transact_id] = False
                request_error = "卖单提交结果不确定，请到官方市场核对订单和库存"
                break
            except Exception as exc:
                self.logger.warning(f"[GJM Trade] 卖单提交失败，开始对账: {exc}")
                submission_success[transact_id] = False
                request_error = f"部分卖单提交失败：{exc}"
                break
            submitted.append(submitted_order)

        ever_tracked_by_transaction: dict[int, TradeOrder] = {}
        unknown_orders: list[TradeOrder] = []
        for attempt in range(SELL_RECONCILE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(SELL_RECONCILE_DELAY)
            after_orders = await self.client.get_open_orders(token)
            new_market_orders = [
                item
                for item in after_orders
                if (not self._order_id(item) or self._order_id(item) not in before_ids)
                and self._order_market(item) == rule.market_name
            ]
            unknown_orders = []
            for item in new_market_orders:
                match = next(
                    (
                        order
                        for order in submitted
                        if self._order_side(item) == "SELL" and self._order_matches_submission(order, item)
                    ),
                    None,
                )
                if match is None:
                    unknown_orders.append(self._order_from_market(item))
                    continue
                tracked = self._order_from_market(item)
                tracked.transaction_id = match.transaction_id
                ever_tracked_by_transaction[match.transaction_id] = tracked
            if unknown_orders:
                break
            if len(ever_tracked_by_transaction) == len(submitted):
                break

        tracked = [
            ever_tracked_by_transaction[order.transaction_id]
            for order in submitted
            if order.transaction_id in ever_tracked_by_transaction
        ]
        listed = min(original_held, sum(order.amount for order in tracked))
        missing_successes = [
            order
            for order in submitted
            if order.transaction_id not in ever_tracked_by_transaction
            and submission_success.get(order.transaction_id, False)
        ]
        returned_by_transaction: dict[int, int] = {}
        returned_total = 0
        unresolved = bool(unknown_orders or request_error)
        if missing_successes:
            try:
                inventory = await self._inventory_snapshot(rule, token)
                actual_available = sum(
                    max(
                        0,
                        inventory.get((asset.context_id, asset.asset_id), {}).get("quantity", 0)
                        - asset.baseline_quantity,
                    )
                    for asset in runtime.assets
                )
            except Exception as exc:
                unresolved = True
                request_error = f"卖单库存对账失败：{exc}"
            else:
                returned_total = min(sum(order.amount for order in missing_successes), max(0, actual_available))
                returned_left = returned_total
                for order in reversed(missing_successes):
                    returned_quantity = min(order.amount, returned_left)
                    returned_by_transaction[order.transaction_id] = returned_quantity
                    returned_left -= returned_quantity

        sold = 0
        gross_amount = 0
        net_proceeds = 0
        for order in submitted:
            tracked_order = ever_tracked_by_transaction.get(order.transaction_id)
            if tracked_order is not None:
                sold_quantity = max(0, order.amount - tracked_order.amount)
            elif submission_success.get(order.transaction_id, False):
                # 经过多次查询仍未出现挂单，视为已即时成交；未知订单出现时不会走这里。
                sold_quantity = max(
                    0,
                    order.amount - returned_by_transaction.get(order.transaction_id, 0),
                )
            else:
                sold_quantity = 0
                unresolved = True
            sold += sold_quantity
            gross_amount += sold_quantity * order.price_raw
            net_proceeds += sold_quantity * seller_receives_raw(order.price_raw)

        sold = min(original_held, sold)
        average_gross = self._weighted_price(gross_amount, sold)
        average_net = self._weighted_price(net_proceeds, sold)
        runtime.orders = tracked
        runtime.listed_quantity = listed
        runtime.proceeds_raw += net_proceeds
        self._reduce_cost(runtime, sold)
        runtime.held_quantity = max(0, original_held - sold)
        runtime.last_sell_price_raw = average_gross or rule.sell_gross_price_raw
        if unresolved:
            runtime.phase = PHASE_ERROR
            runtime.last_error = (
                "检测到无法确认归属的开放卖单，请到官方市场核对"
                if unknown_orders
                else (request_error or "卖单提交结果不确定，请到官方市场核对订单和库存")
            )
        elif runtime.held_quantity <= 0:
            runtime.phase = PHASE_COMPLETED
        elif listed > 0:
            runtime.phase = PHASE_LISTING
            self._next_listing_reconcile[rule.rule_id] = time.time() + MAX_LISTING_RECONCILE_SECONDS
        else:
            runtime.phase = PHASE_HOLDING
        runtime.updated_at = time.time()
        self.store.save_runtime()

        if sold > 0:
            await self._record(
                rule,
                "sell",
                f"{rule.title} 卖出 {sold} 个，实际均价 {fmt_gjn(average_gross)} GJN，"
                f"税后到账 {fmt_gjn(net_proceeds)} GJN",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(average_gross),
                    "trade_quantity": sold,
                    "trade_amount": fmt_gjn(gross_amount),
                    "trade_net_price": fmt_gjn(average_net),
                },
            )
        if listed > 0:
            listed_gross = sum(order.amount * order.price_raw for order in tracked)
            listed_net = sum(order.amount * seller_receives_raw(order.price_raw) for order in tracked)
            await self._record(
                rule,
                "listing",
                f"{rule.title} 剩余 {listed} 个已挂单，预计税后 {fmt_gjn(listed_net)} GJN",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(self._weighted_price(listed_gross, listed)),
                    "trade_quantity": listed,
                    "trade_amount": fmt_gjn(listed_gross),
                    "trade_net_price": fmt_gjn(self._weighted_price(listed_net, listed)),
                },
            )
        if returned_total > 0:
            await self._record(
                rule,
                "listing_returned",
                f"{rule.title} 有 {returned_total} 个卖单未生效或已退回库存",
                level="warning",
            )
        if unresolved:
            raise TradeError(runtime.last_error)
        if runtime.phase == PHASE_COMPLETED:
            await self._complete_cycle(rule)

    async def _reconcile_live_listing(self, rule: TradeRule) -> None:
        now = time.time()
        if now < self._next_listing_reconcile.get(rule.rule_id, 0):
            return
        token = await self.auth.get_token()
        runtime = self.store.get_runtime(rule.rule_id)
        previous_available = runtime.available_quantity
        current_orders = await self.client.get_open_orders(token)
        unowned = self._unowned_market_orders(rule, current_orders, runtime.orders)
        if unowned:
            raise TradeError("检测到无法确认归属的开放订单，挂单对账已暂停")
        by_id = {self._order_id(item): item for item in current_orders}
        by_transaction = {
            self._order_transaction_id(item): item for item in current_orders if self._order_transaction_id(item)
        }
        missing: list[TradeOrder] = []
        current_tracked: list[TradeOrder] = []
        partial_sold = 0
        partial_gross = 0
        partial_net = 0
        for order in runtime.orders:
            item = by_id.get(order.order_id)
            if item is None and order.transaction_id:
                item = by_transaction.get(order.transaction_id)
            if item is None:
                missing.append(order)
                continue
            amount = max(0, self._order_amount(item))
            if order.amount > amount:
                sold_quantity = order.amount - amount
                price_raw = order.price_raw or rule.sell_gross_price_raw
                partial_sold += sold_quantity
                partial_gross += sold_quantity * price_raw
                partial_net += sold_quantity * seller_receives_raw(price_raw)
                order.amount = amount
            if amount > 0:
                current_tracked.append(order)

        missing_amount = sum(order.amount for order in missing)
        returned = 0
        if missing_amount:
            inventory = await self._inventory_snapshot(rule, token)
            actual_tracked = sum(
                max(
                    0,
                    inventory.get((asset.context_id, asset.asset_id), {}).get("quantity", 0) - asset.baseline_quantity,
                )
                for asset in runtime.assets
            )
            returned = min(missing_amount, max(0, actual_tracked - previous_available))

        missing_sold = 0
        missing_gross = 0
        missing_net = 0
        returned_left = returned
        for order in missing:
            returned_quantity = min(order.amount, returned_left)
            returned_left -= returned_quantity
            sold_quantity = max(0, order.amount - returned_quantity)
            price_raw = order.price_raw or rule.sell_gross_price_raw
            missing_sold += sold_quantity
            missing_gross += sold_quantity * price_raw
            missing_net += sold_quantity * seller_receives_raw(price_raw)

        sold = partial_sold + missing_sold
        gross_amount = partial_gross + missing_gross
        net_proceeds = partial_net + missing_net
        average_gross = self._weighted_price(gross_amount, sold)
        average_net = self._weighted_price(net_proceeds, sold)
        runtime.orders = current_tracked
        runtime.listed_quantity = sum(order.amount for order in current_tracked)
        if sold > 0:
            runtime.held_quantity = max(0, runtime.held_quantity - sold)
            runtime.proceeds_raw += net_proceeds
            runtime.last_sell_price_raw = average_gross
            self._reduce_cost(runtime, sold)
        runtime.updated_at = time.time()
        self.store.save_runtime()
        self._next_listing_reconcile[rule.rule_id] = now + MAX_LISTING_RECONCILE_SECONDS
        if sold:
            await self._record(
                rule,
                "listing_fill",
                f"{rule.title} 挂单成交 {sold} 个，实际均价 {fmt_gjn(average_gross)} GJN，"
                f"税后到账 {fmt_gjn(net_proceeds)} GJN",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(average_gross),
                    "trade_quantity": sold,
                    "trade_amount": fmt_gjn(gross_amount),
                    "trade_net_price": fmt_gjn(average_net),
                },
            )
        if runtime.held_quantity <= 0:
            await self._complete_cycle(rule)
        elif returned and not current_tracked:
            runtime.phase = PHASE_HOLDING
            self.store.save_runtime()
            await self._record(rule, "listing_returned", f"{rule.title} 挂单被撤销，库存已返回", level="warning")

    async def _complete_cycle(self, rule: TradeRule) -> None:
        runtime = self.store.get_runtime(rule.rule_id)
        runtime.cycle_count += 1
        proceeds = runtime.proceeds_raw
        cycle = runtime.cycle_count
        if rule.mode == TRADE_MODE_ONCE:
            runtime.phase = PHASE_COMPLETED
            runtime.held_quantity = 0
            runtime.listed_quantity = 0
            runtime.orders = []
            rule.enabled = False
            rule.live = False
        else:
            runtime.reset_for_cycle()
            runtime.proceeds_raw = proceeds
            runtime.cycle_count = cycle
        rule.updated_at = time.time()
        self.store.save_rules()
        self.store.save_runtime()
        self._changed()
        mode_text = "单次任务完成，已停用" if rule.mode == TRADE_MODE_ONCE else "循环任务完成一轮，继续等待买点"
        await self._record(rule, "cycle_complete", f"{rule.title} {mode_text}")

    async def _reconcile_buy(
        self,
        rule: TradeRule,
        token: str,
        before_orders: list[dict],
        before_inventory: dict[tuple[str, str], dict],
        requested: int,
        *,
        request_ok: bool,
        submitted: TradeOrder,
    ) -> tuple[int, list[TradeAssetAllocation], list[TradeOrder]]:
        before_ids = {self._order_id(item) for item in before_orders}
        await asyncio.sleep(0.6)
        after_orders = await self.client.get_open_orders(token)
        new_market_orders = [
            item
            for item in after_orders
            if (not self._order_id(item) or self._order_id(item) not in before_ids)
            and self._order_market(item) == rule.market_name
        ]
        matched_orders = [
            item
            for item in new_market_orders
            if self._order_side(item) == "BUY" and self._order_matches_submission(submitted, item)
        ]
        matched_object_ids = {id(item) for item in matched_orders}
        unowned_orders = [
            self._order_from_market(item) for item in new_market_orders if id(item) not in matched_object_ids
        ]
        if unowned_orders:
            # 无法确认这些订单属于本次请求。宁可停止，也不能撤销用户手工创建的订单。
            return 0, [], unowned_orders

        remaining_orders: list[TradeOrder] = []
        for item in matched_orders:
            order = self._order_from_market(item)
            if not order.pair_id or not order.order_id:
                remaining_orders.append(order)
                continue
            try:
                await self.client.cancel_order(
                    order.pair_id,
                    order.order_id,
                    token,
                    transact_id=self.client.new_transaction_id(),
                )
            except Exception as exc:
                self.logger.warning(f"[GJM Trade] 撤销买单余量失败: {exc}")
                remaining_orders.append(order)
        if remaining_orders:
            return 0, [], remaining_orders
        if matched_orders:
            still_open: list[TradeOrder] = []
            for attempt in range(BUY_CANCEL_VERIFY_ATTEMPTS):
                await asyncio.sleep(0.5 + attempt * 0.25)
                current_orders = await self.client.get_open_orders(token)
                current_unknown = [
                    item
                    for item in current_orders
                    if (not self._order_id(item) or self._order_id(item) not in before_ids)
                    and self._order_market(item) == rule.market_name
                    and not self._order_matches_submission(submitted, item)
                ]
                if current_unknown:
                    return 0, [], [self._order_from_market(item) for item in current_unknown]
                still_open = [
                    self._order_from_market(item)
                    for item in current_orders
                    if self._order_matches_submission(submitted, item)
                ]
                if not still_open:
                    break
            if still_open:
                return 0, [], still_open

        acquired = 0
        allocations: list[TradeAssetAllocation] = []
        attempts = 5 if request_ok else 3
        for attempt in range(attempts):
            after_inventory = await self._inventory_snapshot(rule, token)
            allocations = []
            for key, entry in after_inventory.items():
                before_qty = int(before_inventory.get(key, {}).get("quantity", 0))
                delta = max(0, int(entry.get("quantity", 0)) - before_qty)
                if delta:
                    allocations.append(
                        TradeAssetAllocation(
                            context_id=key[0],
                            asset_id=key[1],
                            quantity=delta,
                            class_id=str(entry.get("class_id") or ""),
                            baseline_quantity=before_qty,
                        )
                    )
            acquired = sum(item.quantity for item in allocations)
            if acquired > 0:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(1.0 + attempt * 0.5)
        if acquired > requested:
            left = requested
            for item in allocations:
                item.quantity = min(item.quantity, left)
                left -= item.quantity
            allocations = [item for item in allocations if item.quantity > 0]
            acquired = requested
        return acquired, allocations, []

    async def _inventory_snapshot(self, rule: TradeRule, token: str) -> dict[tuple[str, str], dict]:
        asset_class = await self.client.get_asset_class(rule.market_name, token, appid=self.config.appid)
        target = self._class_signature(asset_class)
        if not target:
            raise TradeError("无法解析物品资产分类，不能安全对账库存")
        contexts = await self.client.get_contexts(token, appid=self.config.appid)
        snapshot: dict[tuple[str, str], dict] = {}
        for context in contexts:
            context_id = str(context.get("id") or context.get("contextid") or "")
            if not context_id:
                continue
            assets = await self.client.get_context_assets(context_id, token, appid=self.config.appid)
            for asset in assets:
                if self._class_signature(asset.get("class")) != target:
                    continue
                asset_id = str(asset.get("id") or asset.get("assetid") or "")
                if not asset_id:
                    continue
                quantity = int(asset.get("amount") or asset.get("quantity") or 1)
                snapshot[(context_id, asset_id)] = {"quantity": quantity, "class_id": target}
        return snapshot

    async def _cancel_rule_orders(self, rule: TradeRule, *, pause: bool) -> None:
        runtime = self.store.get_runtime(rule.rule_id)
        scenario = self.paper.active_scenario_for_rule(rule.rule_id)
        paper_cancelled = self.paper.cancel_orders_for_rule(rule.rule_id)
        scenario_cancelled: list = []
        if scenario is not None:
            scenario_cancelled = self.paper.cancel_orders_for_rule(rule.rule_id, scenario_id=scenario.scenario_id)
            self.paper.close_scenario(scenario.scenario_id)
        if not runtime.orders:
            runtime.listed_quantity = 0
            runtime.phase = PHASE_HOLDING if runtime.held_quantity > 0 else PHASE_WAITING_BUY
            rule.enabled = False if pause else rule.enabled
            runtime.updated_at = time.time()
            self.store.save_rules()
            self.store.save_runtime()
            if paper_cancelled or scenario_cancelled:
                await self._record(rule, "orders_cancelled", f"规则 {rule.rule_id} 的模拟挂单已撤销")
            return
        token = await self.auth.get_token()
        errors: list[str] = []
        for order in list(runtime.orders):
            try:
                await self.client.cancel_order(
                    order.pair_id,
                    order.order_id,
                    token,
                    transact_id=self.client.new_transaction_id(),
                )
            except Exception as exc:
                errors.append(f"{order.order_id}: {exc}")
        if errors:
            runtime.last_error = "；".join(errors)
            runtime.phase = PHASE_ERROR
            rule.enabled = False
            self.store.save_rules()
            self.store.save_runtime()
            raise TradeError("部分自动订单撤销失败：" + "；".join(errors))
        runtime.orders = []
        runtime.listed_quantity = 0
        runtime.phase = PHASE_HOLDING if runtime.held_quantity > 0 else PHASE_WAITING_BUY
        rule.enabled = False if pause else rule.enabled
        runtime.last_error = ""
        runtime.updated_at = time.time()
        self.store.save_runtime()
        await self._record(rule, "orders_cancelled", f"规则 {rule.rule_id} 的自动挂单已撤销")

    def _check_live_limits(self, rule: TradeRule) -> None:
        if not rule.live and not self._pending_arms.get(rule.rule_id):
            raise TradeError("规则尚未切换为实盘")
        settings = self.store.settings
        if not settings.live_mode_enabled:
            raise TradeError("实盘总开关未开启")
        if not settings.enabled:
            raise TradeError("自动交易总开关未开启")
        if settings.max_quantity <= 0 or rule.quantity > settings.max_quantity:
            raise TradeError("交易数量超过全局上限")
        cost = rule.buy_price_raw * rule.quantity
        if settings.max_order_raw <= 0 or cost > settings.max_order_raw:
            raise TradeError("本规则最大买入额超过单笔上限")
        if rule.sell_net_price_raw <= rule.buy_price_raw:
            raise TradeError("税后卖出价不高于买入价")

    async def _check_budget(self, order_cost: int, rule: TradeRule) -> None:
        self._rollover_ledger()
        settings = self.store.settings
        if order_cost > settings.max_order_raw:
            raise TradeError(f"单笔金额 {fmt_gjn(order_cost)} 超过上限 {fmt_gjn(settings.max_order_raw)}")
        if self.store.ledger.daily_spent_raw + order_cost > settings.daily_limit_raw:
            raise TradeError("本次买入将超过每日累计限额")
        exposure = sum(self.store.get_runtime(item.rule_id).cost_basis_raw for item in self.store.rules if item.live)
        if exposure + order_cost > settings.total_budget_raw:
            raise TradeError("本次买入将超过全局总投入预算")
        token = await self.auth.get_token()
        balance = await self.client.get_balance(token)
        if balance < order_cost:
            raise TradeError(f"GJN 余额不足：需要 {fmt_gjn(order_cost)}，当前 {fmt_gjn(balance)}")

    def _rollover_ledger(self) -> None:
        today = datetime.now(SHANGHAI).date().isoformat()
        if self.store.ledger.day != today:
            self.store.ledger.day = today
            self.store.ledger.daily_spent_raw = 0
            with contextlib.suppress(TradeStorageError):
                self.store.save_ledger()

    @staticmethod
    def _fillable_buy(book: BookSnapshot, limit_raw: int, quantity: int) -> tuple[int, int]:
        remaining = max(0, int(quantity))
        fill = 0
        cost = 0
        for level in book.sell_levels:
            if level.price_raw > limit_raw or remaining <= 0:
                break
            take = min(remaining, level.amount)
            fill += take
            cost += take * level.price_raw
            remaining -= take
        return fill, cost

    @staticmethod
    def _fillable_sell(book: BookSnapshot, net_target_raw: int, quantity: int) -> tuple[int, int, int]:
        remaining = max(0, int(quantity))
        sold = 0
        proceeds = 0
        immediate = 0
        for level in book.buy_levels:
            if remaining <= 0:
                break
            net = seller_receives_raw(level.price_raw)
            if net < net_target_raw:
                continue
            take = min(remaining, level.amount)
            sold += take
            proceeds += net * take
            immediate += take
            remaining -= take
        return sold, proceeds, immediate

    @staticmethod
    def _reduce_cost(runtime: TradeRuntime, sold: int) -> None:
        if sold <= 0 or runtime.held_quantity <= 0 or runtime.cost_basis_raw <= 0:
            return
        sold = min(sold, runtime.held_quantity)
        removed = int(
            (Decimal(runtime.cost_basis_raw) * Decimal(sold) / Decimal(runtime.held_quantity)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        runtime.cost_basis_raw = max(0, runtime.cost_basis_raw - removed)

    @staticmethod
    def _asset_cursor(items: list[tuple[str, str, int]]) -> list[list[Any]]:
        return [[context_id, asset_id, amount] for context_id, asset_id, amount in items]

    @staticmethod
    def _take_asset_chunks(cursor: list[list[Any]], quantity: int, price_raw: int) -> list[tuple[str, str, int, int]]:
        chunks: list[tuple[str, str, int, int]] = []
        remaining = max(0, int(quantity))
        for item in cursor:
            if remaining <= 0:
                break
            available = int(item[2])
            if available <= 0:
                continue
            take = min(available, remaining)
            item[2] = available - take
            remaining -= take
            chunks.append((str(item[0]), str(item[1]), take, int(price_raw)))
        return chunks

    @staticmethod
    def _weighted_price(cost: int, quantity: int) -> int:
        return int(Decimal(cost) / Decimal(quantity)) if quantity else 0

    @staticmethod
    def _class_signature(value: Any) -> str:
        pairs: list[tuple[str, str]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("name") is not None:
                    pairs.append((str(item.get("name")), str(item.get("value"))))
        elif isinstance(value, dict):
            if value.get("name") is not None and value.get("value") is not None:
                pairs.append((str(value.get("name")), str(value.get("value"))))
            else:
                pairs.extend((str(key), str(item)) for key, item in value.items())
        pairs.sort()
        return "|".join(f"{name}={item}" for name, item in pairs)

    @staticmethod
    def _order_id(item: dict) -> str:
        return str(item.get("id") or item.get("orderId") or item.get("order_id") or "")

    @staticmethod
    def _order_pair_id(item: dict) -> str:
        return str(item.get("pairId") or item.get("pair_id") or "")

    @staticmethod
    def _order_transaction_id(item: dict) -> int:
        value = (
            item.get("transactid")
            or item.get("transactId")
            or item.get("transact_id")
            or item.get("transactionId")
            or item.get("transaction_id")
            or 0
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _order_market(item: dict) -> str:
        return str(item.get("market") or item.get("market_name") or item.get("hash_name") or "")

    @staticmethod
    def _order_side(item: dict) -> str:
        return str(item.get("type") or item.get("side") or "").upper()

    @classmethod
    def _order_amount(cls, item: dict) -> int:
        return int(item.get("amount") or item.get("quantity") or item.get("count") or 0)

    @classmethod
    def _order_from_market(cls, item: dict) -> TradeOrder:
        return TradeOrder(
            order_id=cls._order_id(item),
            pair_id=cls._order_pair_id(item),
            side=cls._order_side(item),
            amount=cls._order_amount(item),
            price_raw=int(item.get("price") or item.get("normalPrice") or item.get("localPrice") or 0),
            market_name=cls._order_market(item),
            transaction_id=cls._order_transaction_id(item),
        )

    @classmethod
    def _order_from_response(
        cls,
        response: Any,
        *,
        side: str,
        amount: int,
        price_raw: int,
        market_name: str,
        transaction_id: int,
    ) -> TradeOrder:
        item = cls._find_order_payload(response)
        return TradeOrder(
            order_id=cls._order_id(item) if item else "",
            pair_id=cls._order_pair_id(item) if item else "",
            side=side,
            amount=max(0, int(amount)),
            price_raw=max(0, int(price_raw)),
            market_name=market_name,
            transaction_id=max(0, int(transaction_id)),
        )

    @classmethod
    def _find_order_payload(cls, value: Any) -> dict:
        if isinstance(value, dict):
            if cls._order_id(value) or cls._order_pair_id(value) or cls._order_transaction_id(value):
                return value
            for key in ("order", "data", "result", "response", "orders"):
                if key in value:
                    found = cls._find_order_payload(value[key])
                    if found:
                        return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_order_payload(item)
                if found:
                    return found
        return {}

    @classmethod
    def _order_matches_submission(cls, submitted: TradeOrder, item: dict) -> bool:
        order_id = cls._order_id(item)
        if submitted.order_id and order_id and submitted.order_id == order_id:
            return True
        transaction_id = cls._order_transaction_id(item)
        if submitted.transaction_id and transaction_id:
            return submitted.transaction_id == transaction_id
        return False

    @classmethod
    def _unowned_market_orders(
        cls,
        rule: TradeRule,
        orders: list[dict],
        owned: list[TradeOrder] | None = None,
    ) -> list[TradeOrder]:
        owned_ids = {order.order_id for order in (owned or []) if order.order_id}
        owned_transactions = {order.transaction_id for order in (owned or []) if order.transaction_id}
        return [
            cls._order_from_market(item)
            for item in orders
            if cls._order_market(item) == rule.market_name
            and cls._order_id(item) not in owned_ids
            and cls._order_transaction_id(item) not in owned_transactions
        ]

    @staticmethod
    def _buy_cost_for_quantity(book: BookSnapshot, limit_raw: int, quantity: int) -> int:
        remaining = max(0, int(quantity))
        cost = 0
        for level in book.sell_levels:
            if level.price_raw > limit_raw or remaining <= 0:
                break
            take = min(remaining, level.amount)
            cost += take * level.price_raw
            remaining -= take
        return cost + remaining * max(0, int(limit_raw))

    @staticmethod
    def _quote_raw(value: float | None) -> int:
        if value is None:
            return 0
        raw = Decimal(str(value)) * Decimal(CENT_RAW) * Decimal(CENT_RAW)
        return int(raw.to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _optional_price(payload: dict[str, Any], key: str, default: int) -> int:
        if key not in payload:
            return int(default)
        value = payload.get(key)
        if value in (None, "", 0, "0", "0.0", "0.00"):
            return 0
        return parse_gjn_raw(value)

    _KIND_LABELS = {
        "created": "规则创建",
        "updated": "规则更新",
        "settings": "安全设置",
        "mode_changed": "交易环境切换",
        "armed": "切换实盘",
        "disarmed": "退出实盘",
        "paused": "规则暂停",
        "resumed": "规则恢复",
        "deleted": "规则删除",
        "orders_cancelled": "撤销挂单",
        "buy": "买入成交",
        "paper_buy": "模拟买入",
        "sell": "卖出成交",
        "paper_sell": "模拟卖出",
        "listing": "挂卖出单",
        "listing_fill": "挂单成交",
        "paper_manual_fill": "模拟手工成交",
        "stop_all": "急停",
        "partial_bought": "部分买入",
        "sold": "卖出成交",
        "partial_sold": "部分卖出",
        "listed": "挂出卖单",
        "listing_returned": "卖单退回",
        "cycle_complete": "周期完成",
        "error": "交易异常",
        "reconciled": "订单对账",
    }

    def _event_context(
        self,
        rule: TradeRule | None,
        runtime: TradeRuntime | None,
        kind: str,
        message: str,
        level: str,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = time.time()
        context: dict[str, Any] = {
            "trade_kind": str(kind),
            "trade_kind_text": AutoTrader._KIND_LABELS.get(str(kind), str(kind)),
            "trade_level": str(level),
            "trade_message": str(message),
            "trade_datetime": time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(now)),
            "trade_datetime_plain": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "trade_timestamp": int(now),
            "trade_fee": "15%",
        }
        if rule is not None:
            context.update(
                {
                    "trade_rule_id": rule.rule_id,
                    "trade_name": rule.title,
                    "trade_market_name": rule.market_name,
                    "trade_url": f"https://trade.gaijin.net/market/{self.config.appid}/{rule.market_name}",
                    "trade_item_type": rule.item_type,
                    "trade_mode": rule.mode,
                    "trade_live": "是" if rule.live else "否",
                    "trade_enabled": "是" if rule.enabled else "否",
                    "trade_buy_price": fmt_gjn(rule.buy_price_raw),
                    "trade_sell_net_price": fmt_gjn(rule.sell_net_price_raw),
                    "trade_sell_gross_price": fmt_gjn(rule.sell_gross_price_raw),
                }
            )
        if runtime is not None:
            context.update(
                {
                    "trade_phase": runtime.phase,
                    "trade_last_error": runtime.last_error,
                    "trade_held_qty": int(runtime.held_quantity),
                    "trade_listed_qty": int(runtime.listed_quantity),
                    "trade_available_qty": int(runtime.available_quantity),
                    "trade_cost_basis": fmt_gjn(runtime.cost_basis_raw),
                    "trade_proceeds": fmt_gjn(runtime.proceeds_raw),
                    "trade_cycle_count": int(runtime.cycle_count),
                    "trade_last_buy_price": fmt_gjn(runtime.last_buy_price_raw),
                    "trade_last_sell_price": fmt_gjn(runtime.last_sell_price_raw),
                }
            )
            profit_raw = runtime.proceeds_raw - runtime.cost_basis_raw
            context["trade_profit"] = f"{raw_to_decimal(profit_raw):+.2f}"
        if extra:
            context.update({str(key): value for key, value in extra.items() if value is not None})
        return context

    async def _record(
        self,
        rule: TradeRule,
        kind: str,
        message: str,
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> None:
        runtime = self.store.get_runtime(rule.rule_id)
        context = self._event_context(rule, runtime, kind, message, level, details)
        event = TradeEvent(
            timestamp=time.time(),
            rule_id=rule.rule_id,
            kind=kind,
            message=message,
            level=level,
            details=context,
        )
        self.store.append_event(event)
        await self._notify_event(event)

    async def _record_system(self, kind: str, message: str, details: dict[str, Any] | None = None) -> None:
        context = self._event_context(None, None, kind, message, "info", details)
        event = TradeEvent(timestamp=time.time(), rule_id="", kind=kind, message=message, details=context)
        self.store.append_event(event)
        await self._notify_event(event)

    async def _notify_event(self, event: TradeEvent) -> None:
        umos = []
        for umo in self.notifier.owner_sessions():
            if platforms.session_type_of(umo) == platforms.SESSION_TYPE_FRIEND and umo not in umos:
                umos.append(umo)
        if not umos:
            self.logger.info(f"[GJM Trade] 没有拥有者私聊会话：{event.message}")
            return
        try:
            text = self.notifier.render_trade_event(event)
            await self.notifier.push(text, umos)
        except Exception as exc:
            self.logger.warning(f"[GJM Trade] 私聊通知发送失败: {exc}")

    async def _fail_rule(self, rule: TradeRule, exc: Exception) -> None:
        runtime = self.store.get_runtime(rule.rule_id)
        if isinstance(exc, AuthExpiredError):
            message = "市场登录已失效，自动交易暂停，请发送 /gjm login"
            runtime.phase = PHASE_ERROR
            rule.enabled = False
        elif isinstance(exc, TradeError):
            message = str(exc)
            runtime.phase = PHASE_ERROR
            rule.enabled = False
        elif isinstance(exc, MarketApiError):
            message = f"市场接口拒绝交易：{exc}"
            runtime.phase = PHASE_ERROR
            rule.enabled = False
        else:
            message = f"自动交易异常：{exc}"
        runtime.last_error = message
        runtime.updated_at = time.time()
        self.store.save_runtime()
        self.store.save_rules()
        self.logger.error(f"[GJM Trade] {rule.rule_id}: {message}")
        await self._record(rule, "error", f"{rule.title}：{message}", level="error")

    def _require_rule(self, rule_id: str) -> TradeRule:
        text = str(rule_id).strip()
        rule = self.store.get_rule(text.upper()) or self.store.get_rule(text)
        if rule is None:
            raise TradeError("找不到该交易规则")
        return rule

    def _new_rule_id(self) -> str:
        while True:
            rule_id = "T" + secrets.token_hex(4).upper()
            if not any(rule.rule_id == rule_id for rule in self.store.rules):
                return rule_id

    def _changed(self) -> None:
        callback = self.on_watch_change
        if callable(callback):
            with contextlib.suppress(Exception):
                callback()
