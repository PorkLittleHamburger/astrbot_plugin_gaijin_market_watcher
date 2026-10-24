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
from .trade_models import (
    CENT_RAW,
    PHASE_COMPLETED,
    PHASE_ERROR,
    PHASE_HOLDING,
    PHASE_LISTING,
    PHASE_PAUSED,
    PHASE_WAITING_BUY,
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


class TradeError(RuntimeError):
    """可由用户修正的交易业务错误。"""


class AutoTrader:
    def __init__(self, *, client, auth, storage, config, accounts=None, notifier, logger, on_watch_change=None) -> None:
        self.client = client
        self.auth = auth
        self.config = config
        self.accounts = accounts or config
        self.notifier = notifier
        self.logger = logger
        self.store = TradeStore(storage, logger)
        self.on_watch_change = on_watch_change
        self._task: asyncio.Task | None = None
        self._running = False
        self._queue: asyncio.Queue[dict[str, Quote]] = asyncio.Queue(maxsize=2)
        self._action_lock = asyncio.Lock()
        self._pending_arms: dict[str, float] = {}
        self._next_listing_reconcile: dict[str, float] = {}

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
        rule = self._require_rule(rule_id)
        if rule.live:
            raise TradeError("实盘规则不能直接修改，请先退出实盘")
        runtime = self.store.get_runtime(rule.rule_id)
        if runtime.held_quantity or runtime.orders or runtime.phase in (PHASE_HOLDING, PHASE_LISTING):
            raise TradeError("持仓或挂单期间不能修改规则，请先撤单并清仓")
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
        current = self.store.settings
        settings = TradeSettings(
            enabled=bool(payload.get("enabled", current.enabled)),
            total_budget_raw=self._optional_price(payload, "total_budget", current.total_budget_raw),
            daily_limit_raw=self._optional_price(payload, "daily_limit", current.daily_limit_raw),
            max_order_raw=self._optional_price(payload, "max_order", current.max_order_raw),
            max_quantity=max(0, int(payload.get("max_quantity", current.max_quantity) or 0)),
            poll_interval=max(1, min(3600, int(payload.get("poll_interval", current.poll_interval) or 5))),
            private_mode=bool(payload.get("private_mode", current.private_mode)),
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
            f"检查间隔：{settings.poll_interval} 秒",
        )
        return settings

    async def prepare_arm(self, rule_id: str) -> str:
        rule = self._require_rule(rule_id)
        runtime = self.store.get_runtime(rule.rule_id)
        if rule.live:
            raise TradeError("规则已经在实盘模式")
        if runtime.held_quantity or runtime.orders or runtime.phase in (PHASE_HOLDING, PHASE_LISTING):
            raise TradeError("模拟持仓或订单尚未清空，不能切换实盘")
        settings = self.store.settings
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
        action = str(action or "").lower()
        if action == "prepare_arm":
            await self.prepare_arm(rule_id)
            return self._require_rule(rule_id)
        if action == "arm":
            return await self.confirm_arm(rule_id, confirmation_id)
        if action == "disarm":
            return await self.disarm(rule_id)
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
            if runtime.held_quantity or runtime.orders or runtime.phase in (PHASE_HOLDING, PHASE_LISTING):
                raise TradeError("仍有持仓或自动挂单，不能删除规则")
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
        cancelled: list[str] = []
        errors: list[str] = []
        for rule in list(self.store.rules):
            runtime = self.store.get_runtime(rule.rule_id)
            if runtime.orders:
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

    def _is_rule_tradable(self, rule: TradeRule) -> bool:
        runtime = self.store.get_runtime(rule.rule_id)
        if not rule.enabled or runtime.phase in (PHASE_COMPLETED, PHASE_ERROR, PHASE_PAUSED):
            return False
        if rule.live and not self.store.settings.enabled:
            return False
        return runtime.phase in (PHASE_WAITING_BUY, PHASE_HOLDING, PHASE_LISTING)

    async def _evaluate_rule(self, rule: TradeRule, quote: Quote) -> None:
        runtime = self.store.get_runtime(rule.rule_id)
        if runtime.phase == PHASE_WAITING_BUY:
            sell_raw = self._quote_raw(quote.sell_min)
            if sell_raw and sell_raw <= rule.buy_price_raw:
                if rule.live:
                    await self._live_buy(rule, quote)
                else:
                    await self._paper_buy(rule, quote)
            return
        if runtime.phase == PHASE_HOLDING:
            if rule.live:
                await self._live_sell(rule, quote)
            else:
                await self._paper_sell(rule, quote)
            return
        if runtime.phase == PHASE_LISTING:
            if rule.live:
                await self._reconcile_live_listing(rule)
            else:
                await self._paper_reconcile_listing(rule, quote)

    async def drill_paper_cycle(self, rule_id: str) -> dict[str, Any]:
        """用一次真实盘口快照做隔离的完整模拟，不修改规则运行状态。"""
        rule = self._require_rule(rule_id)
        if rule.live:
            raise TradeError("实盘规则不能执行模拟演练，请先切换到模拟盘")

        token = await self.auth.get_token()
        book = await self.client.fetch_book(rule.market_name, token, appid=self.config.appid)
        if str(book.kind or "").upper() not in ("", "COMMODITY") or book.auctionable:
            raise TradeError("该物品不是可模拟的普通商品")

        initial_best_sell = book.best_sell
        initial_best_buy = book.best_buy
        initial_sell_depth = sum(level.amount for level in book.sell_levels)
        initial_buy_depth = sum(level.amount for level in book.buy_levels)

        live_buy_quantity, live_buy_cost = self._fillable_buy(book, rule.buy_price_raw, rule.quantity)
        synthetic_buy_quantity = max(0, rule.quantity - live_buy_quantity)
        total_buy_cost = live_buy_cost + synthetic_buy_quantity * rule.buy_price_raw
        average_buy_price = self._weighted_price(total_buy_cost, rule.quantity)

        sim_runtime = TradeRuntime.from_dict(self.store.get_runtime(rule.rule_id).to_dict())
        sim_runtime.reset_for_cycle()
        sim_runtime.held_quantity = rule.quantity
        sim_runtime.cost_basis_raw = total_buy_cost
        sim_runtime.last_buy_price_raw = average_buy_price
        sim_runtime.phase = PHASE_HOLDING

        live_sell_quantity, live_sell_proceeds, _ = self._fillable_sell(
            book,
            rule.sell_net_price_raw,
            rule.quantity,
        )
        synthetic_sell_quantity = max(0, rule.quantity - live_sell_quantity)
        total_sell_proceeds = live_sell_proceeds + synthetic_sell_quantity * rule.sell_net_price_raw
        average_net_price = (
            int(Decimal(total_sell_proceeds) / Decimal(rule.quantity))
            if total_sell_proceeds > 0 and rule.quantity > 0
            else 0
        )

        sim_runtime.held_quantity = 0
        sim_runtime.listed_quantity = 0
        sim_runtime.proceeds_raw = total_sell_proceeds
        sim_runtime.cost_basis_raw = 0
        sim_runtime.last_sell_price_raw = rule.sell_gross_price_raw
        sim_runtime.phase = PHASE_COMPLETED
        sim_runtime.cycle_count = 1

        profit_raw = total_sell_proceeds - total_buy_cost
        drill_id = "D" + secrets.token_hex(4).upper()
        steps = [
            {
                "kind": "market",
                "title": "读取初始盘口",
                "detail": (
                    f"最低卖价 {fmt_gjn(initial_best_sell) if initial_best_sell else '无'} GJN，"
                    f"最高求购 {fmt_gjn(initial_best_buy) if initial_best_buy else '无'} GJN；"
                    f"卖单深度 {initial_sell_depth}，求购深度 {initial_buy_depth}"
                ),
            },
            {
                "kind": "buy",
                "title": "模拟买入",
                "detail": (
                    f"盘口成交 {live_buy_quantity} 个，虚拟补足 {synthetic_buy_quantity} 个；"
                    f"均价 {fmt_gjn(average_buy_price)} GJN，成本 {fmt_gjn(total_buy_cost)} GJN"
                ),
            },
            {
                "kind": "sell",
                "title": "模拟卖出",
                "detail": (
                    f"盘口求购成交 {live_sell_quantity} 个，虚拟补足 {synthetic_sell_quantity} 个；"
                    f"税后均价 {fmt_gjn(average_net_price)} GJN，到账 {fmt_gjn(total_sell_proceeds)} GJN"
                ),
            },
            {
                "kind": "complete",
                "title": "模拟周期完成",
                "detail": (
                    f"共 {rule.quantity} 个，模拟盈亏 {raw_to_decimal(profit_raw):+.2f} GJN；规则实际状态未改变"
                ),
            },
        ]
        message = f"完整模拟演练 {drill_id}：{rule.title}，模拟盈亏 {raw_to_decimal(profit_raw):+.2f} GJN"
        details = self._event_context(
            rule,
            sim_runtime,
            "paper_drill",
            message,
            "info",
            {
                "drill_id": drill_id,
                "drill_initial_best_sell": fmt_gjn(initial_best_sell) if initial_best_sell else "",
                "drill_initial_best_buy": fmt_gjn(initial_best_buy) if initial_best_buy else "",
                "drill_buy_quantity": rule.quantity,
                "drill_live_buy_quantity": live_buy_quantity,
                "drill_synthetic_buy_quantity": synthetic_buy_quantity,
                "drill_buy_cost": fmt_gjn(total_buy_cost),
                "drill_sell_quantity": rule.quantity,
                "drill_live_sell_quantity": live_sell_quantity,
                "drill_synthetic_sell_quantity": synthetic_sell_quantity,
                "drill_sell_proceeds": fmt_gjn(total_sell_proceeds),
                "drill_profit": f"{raw_to_decimal(profit_raw):+.2f}",
                "trade_cost_basis": fmt_gjn(total_buy_cost),
                "trade_profit": f"{raw_to_decimal(profit_raw):+.2f}",
            },
        )
        event = TradeEvent(
            timestamp=time.time(),
            rule_id=rule.rule_id,
            kind="paper_drill",
            message=message,
            details=details,
        )
        self.store.append_event(event)
        return {
            "drill_id": drill_id,
            "rule_id": rule.rule_id,
            "rule_title": rule.title,
            "initial": {
                "best_sell": fmt_gjn(initial_best_sell) if initial_best_sell else "",
                "best_buy": fmt_gjn(initial_best_buy) if initial_best_buy else "",
                "sell_depth": initial_sell_depth,
                "buy_depth": initial_buy_depth,
            },
            "buy": {
                "quantity": rule.quantity,
                "live_quantity": live_buy_quantity,
                "synthetic_quantity": synthetic_buy_quantity,
                "average_price": fmt_gjn(average_buy_price),
                "cost": fmt_gjn(total_buy_cost),
            },
            "sell": {
                "quantity": rule.quantity,
                "live_quantity": live_sell_quantity,
                "synthetic_quantity": synthetic_sell_quantity,
                "average_net_price": fmt_gjn(average_net_price),
                "proceeds": fmt_gjn(total_sell_proceeds),
            },
            "summary": {
                "profit": f"{raw_to_decimal(profit_raw):+.2f}",
                "profit_per_unit": f"{raw_to_decimal(profit_raw) / Decimal(rule.quantity):+.2f}",
                "logged": bool(event.event_id),
            },
            "steps": steps,
        }

    async def _paper_buy(self, rule: TradeRule, quote: Quote) -> None:
        token = await self.auth.get_token()
        book = await self.client.fetch_book(rule.market_name, token, appid=self.config.appid)
        fill, cost = self._fillable_buy(book, rule.buy_price_raw, rule.quantity)
        if fill <= 0:
            return
        runtime = self.store.get_runtime(rule.rule_id)
        runtime.held_quantity = fill
        runtime.listed_quantity = 0
        runtime.cost_basis_raw = cost
        runtime.last_buy_price_raw = self._weighted_price(cost, fill)
        runtime.phase = PHASE_HOLDING
        runtime.updated_at = time.time()
        self.store.save_runtime()
        await self._record(
            rule,
            "paper_buy",
            f"[模拟] {rule.title} 买入 {fill}/{rule.quantity} 个，成本 {fmt_gjn(cost)} GJN",
            details={
                "trade_side": "BUY",
                "trade_price": fmt_gjn(runtime.last_buy_price_raw),
                "trade_quantity": fill,
                "trade_amount": fmt_gjn(cost),
            },
        )
        await self._paper_sell(rule, quote)

    async def _paper_sell(self, rule: TradeRule, quote: Quote) -> None:
        token = await self.auth.get_token()
        book = await self.client.fetch_book(rule.market_name, token, appid=self.config.appid)
        runtime = self.store.get_runtime(rule.rule_id)
        available = runtime.available_quantity
        if available <= 0:
            await self._complete_cycle(rule)
            return
        sold, proceeds, immediate = self._fillable_sell(book, rule.sell_net_price_raw, available)
        runtime.proceeds_raw += proceeds
        self._reduce_cost(runtime, sold)
        runtime.held_quantity = max(0, runtime.held_quantity - sold)
        runtime.last_sell_price_raw = rule.sell_gross_price_raw
        if runtime.held_quantity > 0:
            runtime.listed_quantity = runtime.held_quantity
            runtime.phase = PHASE_LISTING
        else:
            runtime.listed_quantity = 0
            runtime.phase = PHASE_COMPLETED
        runtime.updated_at = time.time()
        self.store.save_runtime()
        if sold:
            await self._record(
                rule,
                "paper_sell",
                f"[模拟] {rule.title} 立即卖出 {sold} 个（求购覆盖 {immediate}），税后 {fmt_gjn(proceeds)} GJN",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(rule.sell_gross_price_raw),
                    "trade_quantity": sold,
                    "trade_amount": fmt_gjn(sold * rule.sell_gross_price_raw),
                    "trade_net_price": fmt_gjn(rule.sell_net_price_raw),
                },
            )
        if runtime.held_quantity > 0:
            await self._record(
                rule,
                "paper_listing",
                f"[模拟] 剩余 {runtime.held_quantity} 个按 {fmt_gjn(rule.sell_gross_price_raw)} GJN 挂单",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(rule.sell_gross_price_raw),
                    "trade_quantity": runtime.held_quantity,
                    "trade_amount": fmt_gjn(runtime.held_quantity * rule.sell_gross_price_raw),
                    "trade_net_price": fmt_gjn(rule.sell_net_price_raw),
                },
            )
        else:
            await self._complete_cycle(rule)

    async def _paper_reconcile_listing(self, rule: TradeRule, quote: Quote) -> None:
        token = await self.auth.get_token()
        book = await self.client.fetch_book(rule.market_name, token, appid=self.config.appid)
        runtime = self.store.get_runtime(rule.rule_id)
        quantity = min(runtime.listed_quantity, runtime.held_quantity)
        sold, proceeds, _ = self._fillable_sell(book, rule.sell_net_price_raw, quantity)
        if sold <= 0:
            return
        runtime.proceeds_raw += proceeds
        runtime.held_quantity = max(0, runtime.held_quantity - sold)
        runtime.listed_quantity = max(0, runtime.listed_quantity - sold)
        self._reduce_cost(runtime, sold)
        runtime.updated_at = time.time()
        self.store.save_runtime()
        await self._record(
            rule,
            "paper_listing_fill",
            f"[模拟] {rule.title} 挂单成交 {sold} 个，税后 {fmt_gjn(proceeds)} GJN",
            details={
                "trade_side": "SELL",
                "trade_price": fmt_gjn(rule.sell_gross_price_raw),
                "trade_quantity": sold,
                "trade_amount": fmt_gjn(sold * rule.sell_gross_price_raw),
                "trade_net_price": fmt_gjn(rule.sell_net_price_raw),
            },
        )
        if runtime.held_quantity <= 0:
            await self._complete_cycle(rule)

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
        try:
            await self.client.place_buy_order(
                rule.market_name,
                fill,
                rule.buy_price_raw,
                token,
                transact_id=self.client.new_transaction_id(),
                appid=self.config.appid,
                private_mode=self.store.settings.private_mode,
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
        )
        runtime = self.store.get_runtime(rule.rule_id)
        if remaining:
            runtime.orders = remaining
            runtime.last_error = "买单存在未撤销余量"
            runtime.phase = PHASE_ERROR
            self.store.save_runtime()
            raise TradeError("买单余量撤销失败，已暂停规则，请到官方市场检查订单")
        if acquired <= 0:
            raise TradeError("买单回执成功，但库存未检测到新增物品，已暂停并等待人工核对")

        runtime.phase = PHASE_HOLDING
        runtime.held_quantity = acquired
        runtime.listed_quantity = 0
        runtime.cost_basis_raw = acquired * rule.buy_price_raw
        runtime.assets = allocations
        runtime.last_buy_price_raw = rule.buy_price_raw
        runtime.last_error = ""
        runtime.updated_at = time.time()
        self.store.ledger.daily_spent_raw += acquired * rule.buy_price_raw
        self.store.save_runtime()
        self.store.save_ledger()
        partial = "（部分成交，余量已撤销）" if acquired < rule.quantity else ""
        await self._record(
            rule,
            "buy",
            f"{rule.title} 实际买入 {acquired}/{fill} 个，限价 {fmt_gjn(rule.buy_price_raw)} GJN{partial}",
            details={
                "trade_side": "BUY",
                "trade_price": fmt_gjn(rule.buy_price_raw),
                "trade_quantity": acquired,
                "trade_amount": fmt_gjn(acquired * rule.buy_price_raw),
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
        before_ids = {self._order_id(item) for item in before_orders}
        request_sent = False
        request_failed = False
        for context_id, asset_id, amount, price_raw in chunks:
            try:
                await self.client.place_sell_order(
                    context_id,
                    asset_id,
                    amount,
                    price_raw,
                    seller_receives_raw(price_raw),
                    token,
                    transact_id=self.client.new_transaction_id(),
                    appid=self.config.appid,
                    private_mode=self.store.settings.private_mode,
                )
                request_sent = True
            except TransportError as exc:
                self.logger.warning(f"[GJM Trade] 卖单结果不确定，开始对账: {exc}")
                request_failed = True
                break

        await asyncio.sleep(0.5)
        after_orders = await self.client.get_open_orders(token)
        tracked = [
            self._order_from_market(item)
            for item in after_orders
            if self._order_id(item) not in before_ids
            and self._order_market(item) == rule.market_name
            and self._order_side(item) == "SELL"
        ]
        listed = min(runtime.held_quantity, sum(order.amount for order in tracked))
        sold = max(0, runtime.held_quantity - listed)
        runtime.orders = tracked
        runtime.listed_quantity = listed
        runtime.proceeds_raw += sold * rule.sell_net_price_raw
        self._reduce_cost(runtime, sold)
        if listed > 0:
            runtime.held_quantity = listed
            runtime.phase = PHASE_LISTING
            self._next_listing_reconcile[rule.rule_id] = time.time() + MAX_LISTING_RECONCILE_SECONDS
        else:
            runtime.held_quantity = 0
            runtime.phase = PHASE_COMPLETED if runtime.held_quantity == 0 else PHASE_HOLDING
        if request_failed:
            runtime.phase = PHASE_ERROR
            runtime.last_error = "卖单提交结果不确定，请到官方市场核对订单和库存"
        runtime.updated_at = time.time()
        self.store.save_runtime()

        if sold > 0:
            await self._record(
                rule,
                "sell",
                f"{rule.title} 卖出 {sold} 个，预计税后 {fmt_gjn(sold * rule.sell_net_price_raw)} GJN",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(rule.sell_gross_price_raw),
                    "trade_quantity": sold,
                    "trade_amount": fmt_gjn(sold * rule.sell_gross_price_raw),
                    "trade_net_price": fmt_gjn(rule.sell_net_price_raw),
                },
            )
        if listed > 0:
            await self._record(
                rule,
                "listing",
                f"{rule.title} 剩余 {listed} 个已挂单，目标税后 {fmt_gjn(rule.sell_net_price_raw)} GJN",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(rule.sell_gross_price_raw),
                    "trade_quantity": listed,
                    "trade_amount": fmt_gjn(listed * rule.sell_gross_price_raw),
                    "trade_net_price": fmt_gjn(rule.sell_net_price_raw),
                },
            )
        if request_failed:
            raise TradeError(runtime.last_error)
        if sold <= 0 and listed <= 0 and request_sent:
            raise TradeError("卖单已提交但未检测到库存变化或挂单，已暂停并等待人工核对")
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
        by_id = {self._order_id(item): item for item in current_orders}
        missing: list[TradeOrder] = []
        current_tracked: list[TradeOrder] = []
        partial_sold = 0
        for order in runtime.orders:
            item = by_id.get(order.order_id)
            if item is None:
                missing.append(order)
                continue
            amount = max(0, self._order_amount(item))
            if order.amount > amount:
                partial_sold += order.amount - amount
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

        sold = partial_sold + max(0, missing_amount - returned)
        runtime.orders = current_tracked
        runtime.listed_quantity = sum(order.amount for order in current_tracked)
        if sold > 0:
            runtime.held_quantity = max(0, runtime.held_quantity - sold)
            runtime.proceeds_raw += sold * rule.sell_net_price_raw
            self._reduce_cost(runtime, sold)
        runtime.updated_at = time.time()
        self.store.save_runtime()
        self._next_listing_reconcile[rule.rule_id] = now + MAX_LISTING_RECONCILE_SECONDS
        if sold:
            await self._record(
                rule,
                "listing_fill",
                f"{rule.title} 挂单成交 {sold} 个，预计税后 {fmt_gjn(sold * rule.sell_net_price_raw)} GJN",
                details={
                    "trade_side": "SELL",
                    "trade_price": fmt_gjn(rule.sell_gross_price_raw),
                    "trade_quantity": sold,
                    "trade_amount": fmt_gjn(sold * rule.sell_gross_price_raw),
                    "trade_net_price": fmt_gjn(rule.sell_net_price_raw),
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
    ) -> tuple[int, list[TradeAssetAllocation], list[TradeOrder]]:
        before_ids = {self._order_id(item) for item in before_orders}
        await asyncio.sleep(0.6)
        after_orders = await self.client.get_open_orders(token)
        new_buys = [
            item
            for item in after_orders
            if self._order_id(item) not in before_ids
            and self._order_market(item) == rule.market_name
            and self._order_side(item) == "BUY"
        ]
        remaining_orders: list[TradeOrder] = []
        for item in new_buys:
            order = self._order_from_market(item)
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
        if not runtime.orders:
            if pause:
                rule.enabled = False
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
            pair_id=str(item.get("pairId") or item.get("pair_id") or ""),
            side=cls._order_side(item),
            amount=cls._order_amount(item),
            price_raw=int(item.get("price") or item.get("normalPrice") or item.get("localPrice") or 0),
            market_name=cls._order_market(item),
        )

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
        "paper_listing": "模拟挂单",
        "listing_fill": "挂单成交",
        "paper_listing_fill": "模拟挂单成交",
        "paper_drill": "完整模拟演练",
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
            message = "市场登录已失效，自动交易暂停，请发送 /gjma login"
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
