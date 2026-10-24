"""订阅层：管理「谁订阅了哪件物品」。

订阅数据保存在插件数据目录的 ``subscriptions.json``。聊天命令和插件视图共用
同一个 SubscriptionStore，避免出现配置与页面双份真相。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .constants import (
    DEFAULT_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    ONLY_CHANGE_INTERVAL,
    REFRESH_MODES,
    REFRESH_ONLY_CHANGE,
)
from .models import Subscription
from .resolver import extract_link_reference, extract_reference, prettify_market_name

URL_EXAMPLE = "https://trade.gaijin.net/market/1067/id50381_f_16xl_usa"
REJECT_HINT = f"只接受物品页链接，例如：{URL_EXAMPLE}"
SUBSCRIPTIONS_FILE = "subscriptions.json"


def _as_int(value: Any, default: int) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是"}
    if isinstance(value, int | float):
        return bool(value)
    return default


def _clamp_interval(value: Any) -> int:
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, _as_int(value, DEFAULT_POLL_INTERVAL)))


def _refresh_mode(value: Any, default: str) -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in REFRESH_MODES else default


def parse_legacy_subscriptions(
    value: Any,
    *,
    default_interval: int = DEFAULT_POLL_INTERVAL,
    default_mode: str = REFRESH_ONLY_CHANGE,
) -> list[Subscription]:
    """解析 2.18.x 插件配置里的 template_list 结构（仅迁移使用）。"""
    entries = list(value.values()) if isinstance(value, dict) else value
    if not isinstance(entries, list | tuple):
        return []
    result: dict[tuple[str, str, str], Subscription] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("uid") or "").strip()
        if not uid:
            continue
        shared_target = str(entry.get("target") or entry.get("target_umo") or "").strip()
        shared_enabled = _as_bool(entry.get("enabled"), True)
        shared_interval = _clamp_interval(entry.get("interval", default_interval))
        shared_mode = _refresh_mode(entry.get("mode"), default_mode)
        shared_name = str(entry.get("name") or "").strip()
        items = entry.get("items")
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list | tuple):
            items = []
        for item in items:
            if isinstance(item, dict):
                _, market_name = extract_reference(
                    str(item.get("item_url") or item.get("url") or item.get("item") or "")
                )
                if not market_name:
                    continue
                sub = Subscription(
                    uid=uid,
                    market_name=market_name,
                    display_name=str(item.get("name") or shared_name or prettify_market_name(market_name)),
                    sell_below=0.0,
                    buy_above=0.0,
                    enabled=_as_bool(item.get("enabled"), shared_enabled),
                    interval=_clamp_interval(item.get("interval", shared_interval)),
                    mode=_refresh_mode(item.get("mode"), shared_mode),
                    target_umo=shared_target,
                )
                result[sub.key] = sub
                continue
            _, market_name = extract_reference(str(item))
            if not market_name:
                continue
            sub = Subscription(
                uid=uid,
                market_name=market_name,
                display_name=shared_name or prettify_market_name(market_name),
                sell_below=0.0,
                buy_above=0.0,
                enabled=shared_enabled,
                interval=shared_interval,
                mode=shared_mode,
                target_umo=shared_target,
            )
            result[sub.key] = sub

        # 兼容更早的“一条一个物品”结构。
        raw_ref = entry.get("item_url") or entry.get("url") or entry.get("item") or ""
        if not items and raw_ref:
            _, market_name = extract_reference(str(raw_ref))
            if market_name:
                sub = Subscription(
                    uid=uid,
                    market_name=market_name,
                    display_name=shared_name or prettify_market_name(market_name),
                    sell_below=0.0,
                    buy_above=0.0,
                    enabled=shared_enabled,
                    interval=shared_interval,
                    mode=shared_mode,
                    target_umo=shared_target,
                )
                result[sub.key] = sub
    return list(result.values())


class SubscriptionStore:
    """用户订阅的读写入口。所有改动都会原子写入插件数据目录。"""

    def __init__(
        self,
        persist: Callable[[list[Subscription]], bool | None],
        logger=None,
        resolve_target: Callable[[str], str] | None = None,
        *,
        items: list[Subscription] | None = None,
        default_interval: int = DEFAULT_POLL_INTERVAL,
        default_mode: str = REFRESH_ONLY_CHANGE,
    ) -> None:
        # 兼容 2.18.x 的构造函数 SubscriptionStore(config, persist, logger=None)。
        if hasattr(persist, "subscriptions") and callable(logger):
            legacy_config = persist
            legacy_persist = logger
            legacy_logger = resolve_target if hasattr(resolve_target, "error") else None
            persist = legacy_persist
            logger = legacy_logger
            items = list(getattr(legacy_config, "subscriptions", []) or [])
            default_interval = int(getattr(legacy_config, "_default_interval", default_interval) or default_interval)
            default_mode = str(getattr(legacy_config, "default_refresh_mode", default_mode) or default_mode)
            resolve_target = getattr(legacy_config, "resolve_target", None)
        self.persist = persist
        self.logger = logger
        self.resolve_target = resolve_target
        self.default_interval = _clamp_interval(default_interval)
        self.default_mode = _refresh_mode(default_mode, REFRESH_ONLY_CHANGE)
        self._items: list[Subscription] = []
        self.reload(items or [])

    def reload(self, items: list[Subscription] | None = None) -> None:
        self._items = self._normalize(list(self._items if items is None else items))

    def _normalize(self, items: list[Subscription]) -> list[Subscription]:
        normalized: list[Subscription] = []
        for sub in items:
            target = sub.target_umo or (self.resolve_target(sub.uid) if self.resolve_target else "")
            normalized.append(self._copy(sub, target_umo=target) if target != sub.target_umo else sub)
        return normalized

    @staticmethod
    def _copy(sub: Subscription, **changes) -> Subscription:
        values = sub.to_dict()
        values.update(changes)
        return Subscription(**values)

    # ------------------------------------------------------------------ 查询

    def all(self) -> list[Subscription]:
        return list(self._items)

    @property
    def enabled_subscriptions(self) -> list[Subscription]:
        return [sub for sub in self._items if sub.enabled]

    @property
    def watched_market_names(self) -> list[str]:
        names: list[str] = []
        for sub in self.enabled_subscriptions:
            if sub.market_name not in names:
                names.append(sub.market_name)
        return names

    def interval_of(self, market_name: str) -> int:
        values = [
            ONLY_CHANGE_INTERVAL if sub.mode == REFRESH_ONLY_CHANGE else max(MIN_POLL_INTERVAL, sub.interval)
            for sub in self.enabled_subscriptions
            if sub.market_name == market_name
        ]
        return min(values) if values else DEFAULT_POLL_INTERVAL

    @property
    def intervals(self) -> dict[str, int]:
        return {name: self.interval_of(name) for name in self.watched_market_names}

    @property
    def min_interval(self) -> int:
        values = list(self.intervals.values())
        return min(values) if values else DEFAULT_POLL_INTERVAL

    def subscribers_of(self, market_name: str, target_umo: str | None = None) -> list[Subscription]:
        return [
            sub
            for sub in self._items
            if sub.market_name == market_name and sub.enabled and (target_umo is None or sub.target_umo == target_umo)
        ]

    def subscriptions_of(self, uid: str, target_umo: str | None = None) -> list[Subscription]:
        return [sub for sub in self._items if sub.uid == uid and (target_umo is None or sub.target_umo == target_umo)]

    def find(self, uid: str, market_name: str, target_umo: str | None = None) -> Subscription | None:
        return next(
            (
                sub
                for sub in self._items
                if sub.uid == uid
                and sub.market_name == market_name
                and (target_umo is None or sub.target_umo == target_umo)
            ),
            None,
        )

    def display_name_of(self, market_name: str) -> str:
        for sub in self._items:
            if sub.market_name == market_name and sub.display_name:
                return sub.display_name
        return prettify_market_name(market_name)

    def bind_legacy_target(self, uid: str, target_umo: str) -> bool:
        if not uid or not target_umo:
            return False
        changed = False
        updated: list[Subscription] = []
        for sub in self._items:
            if sub.uid == uid and not sub.target_umo:
                sub = self._copy(sub, target_umo=target_umo)
                changed = True
            updated.append(sub)
        return self._commit(updated) if changed else False

    def hydrate_targets(self) -> bool:
        if self.resolve_target is None:
            return False
        changed = False
        for sub in self._items:
            if not sub.target_umo and self.resolve_target(sub.uid):
                changed = True
                break
        if not changed:
            return False
        self._items = self._normalize(self._items)
        return self._commit(self._items)

    # ------------------------------------------------------------------ 变更

    @staticmethod
    def parse_url(url: str) -> str:
        _, market_name = extract_link_reference(str(url or ""))
        return market_name

    def subscribe(
        self,
        uid: str,
        url: str,
        target_umo: str = "",
        *,
        display_name: str = "",
        sell_below: float = 0.0,
        buy_above: float = 0.0,
    ) -> tuple[bool, str]:
        market_name = self.parse_url(url)
        if not market_name:
            return False, "只接受物品页链接。"
        existing = self.find(uid, market_name, target_umo)
        peers = [sub for sub in self._items if sub.uid == uid and sub.target_umo == target_umo]
        if existing is not None:
            interval = existing.interval
            mode = existing.mode
            sell_below = sell_below or existing.sell_below
            buy_above = buy_above or existing.buy_above
        elif peers:
            interval = peers[0].interval
            mode = peers[0].mode
            sell_below = sell_below or peers[0].sell_below
            buy_above = buy_above or peers[0].buy_above
        else:
            interval = self.default_interval
            mode = self.default_mode
        name = display_name or (existing.display_name if existing else "") or self.display_name_of(market_name)
        sub = Subscription(
            uid=uid,
            market_name=market_name,
            display_name=name,
            sell_below=max(0.0, sell_below),
            buy_above=max(0.0, buy_above),
            enabled=True,
            interval=interval,
            mode=mode,
            target_umo=target_umo,
        )
        if existing is not None:
            subs = [sub if item.key == sub.key else item for item in self._items]
            if not self._commit(subs):
                return False, "保存订阅失败，请检查插件数据目录写入权限或日志。"
            return True, f"已更新订阅：{name}"
        if not self._commit([*self._items, sub]):
            return False, "保存订阅失败，请检查插件数据目录写入权限或日志。"
        return True, f"订阅成功：{name}\n（{market_name}，刷新间隔 {interval} 秒）"

    def unsubscribe(self, uid: str, url: str, target_umo: str = "") -> tuple[bool, str]:
        market_name = self.parse_url(url)
        if not market_name:
            return False, REJECT_HINT
        return self.remove(uid, market_name, target_umo)

    def remove(self, uid: str, market_name: str, target_umo: str | None = None) -> tuple[bool, str]:
        remaining = [
            sub
            for sub in self._items
            if not (
                sub.uid == uid
                and sub.market_name == market_name
                and (target_umo is None or sub.target_umo == target_umo)
            )
        ]
        if len(remaining) == len(self._items):
            return False, "你没有订阅这件物品。"
        if not self._commit(remaining):
            return False, "取消订阅失败，请检查插件数据目录写入权限或日志。"
        return True, f"已取消订阅：{market_name}"

    def remove_by_index(self, uid: str, index: int, target_umo: str | None = None) -> tuple[bool, str]:
        own = self.subscriptions_of(uid, target_umo)
        if not own:
            return False, "你还没有订阅。用 /gjm sub <物品链接> 订阅。"
        if index < 1 or index > len(own):
            return False, f"编号无效，请输入 1~{len(own)} 之间的编号。"
        target = own[index - 1]
        remaining = [sub for sub in self._items if sub.key != target.key]
        if not self._commit(remaining):
            return False, "取消订阅失败，请检查插件数据目录写入权限或日志。"
        return True, f"已取消订阅：{target.display_name or target.market_name}（剩余 {len(own) - 1} 件）"

    def update(
        self,
        uid: str,
        market_name: str,
        target_umo: str | None = None,
        *,
        display_name: str | None = None,
        sell_below: float | None = None,
        buy_above: float | None = None,
        enabled: bool | None = None,
    ) -> tuple[bool, str]:
        subs = list(self._items)
        for index, sub in enumerate(subs):
            if (
                sub.uid == uid
                and sub.market_name == market_name
                and (target_umo is None or sub.target_umo == target_umo)
            ):
                subs[index] = self._copy(
                    sub,
                    display_name=sub.display_name if display_name is None else display_name,
                    sell_below=sub.sell_below if sell_below is None else max(0.0, sell_below),
                    buy_above=sub.buy_above if buy_above is None else max(0.0, buy_above),
                    enabled=sub.enabled if enabled is None else enabled,
                )
                if not self._commit(subs):
                    return False, "保存订阅失败，请检查插件数据目录写入权限或日志。"
                return True, "已保存。"
        return False, "未找到该订阅。"

    def replace_group(
        self,
        *,
        original_uid: str = "",
        original_target: str = "",
        uid: str,
        target_umo: str,
        enabled: bool,
        interval: int,
        mode: str,
        items: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """新增/编辑一整组订阅；同一会话内的物品一次性替换。"""
        uid = str(uid or "").strip()
        target_umo = str(target_umo or "").strip()
        mode = _refresh_mode(mode, self.default_mode)
        interval = _clamp_interval(interval)
        if not uid:
            return False, "订阅者 UID 不能为空。"
        if len(uid) > 128:
            return False, "订阅者 UID 过长。"
        if not target_umo:
            return False, "订阅所属会话 UMO 不能为空。"
        if len(target_umo) > 512:
            return False, "订阅会话 UMO 过长。"
        if target_umo.count(":") < 2:
            return False, "订阅会话 UMO 格式不正确。"
        if not items:
            return False, "订阅组至少需要一件物品。"
        if len(items) > 100:
            return False, "单个订阅组最多包含 100 件物品。"

        records: list[Subscription] = []
        seen: set[str] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            market_name = self.parse_url(str(raw.get("url") or ""))
            if not market_name:
                return False, REJECT_HINT
            if market_name in seen:
                return False, f"同一会话不能重复订阅：{market_name}"
            seen.add(market_name)
            name = str(raw.get("display_name") or "").strip() or self.display_name_of(market_name)
            records.append(
                Subscription(
                    uid=uid,
                    market_name=market_name,
                    display_name=name,
                    enabled=bool(enabled),
                    interval=interval,
                    mode=mode,
                    target_umo=target_umo,
                )
            )
        if not records:
            return False, "订阅组至少需要一件有效物品。"

        original_uid = str(original_uid or uid).strip()
        original_target = str(original_target or target_umo).strip()
        remaining = [sub for sub in self._items if not (sub.uid == original_uid and sub.target_umo == original_target)]
        conflicts = {(sub.uid, sub.target_umo, sub.market_name) for sub in remaining}
        for record in records:
            if record.key in conflicts:
                return False, "目标会话中已存在同一物品，请直接编辑原订阅组。"
        if not self._commit([*remaining, *records]):
            return False, "保存订阅失败，请检查插件数据目录写入权限或日志。"
        return True, "订阅组已保存。"

    def delete_group(self, uid: str, target_umo: str, market_name: str = "") -> tuple[bool, str]:
        uid = str(uid or "").strip()
        target_umo = str(target_umo or "").strip()
        market_name = str(market_name or "").strip()
        if market_name:
            return self.remove(uid, market_name, target_umo)
        remaining = [sub for sub in self._items if not (sub.uid == uid and sub.target_umo == target_umo)]
        if len(remaining) == len(self._items):
            return False, "未找到该订阅组。"
        if not self._commit(remaining):
            return False, "删除订阅组失败，请检查插件数据目录写入权限或日志。"
        return True, "订阅组已删除。"

    def set_interval(self, uid: str, seconds: int, market_name: str = "", target_umo: str | None = None):
        value = _clamp_interval(seconds)
        targets = [
            sub
            for sub in self._items
            if sub.uid == uid
            and (target_umo is None or sub.target_umo == target_umo)
            and (not market_name or sub.market_name == market_name)
        ]
        if not targets:
            if market_name:
                return False, "你没有订阅这件物品。"
            return False, "你还没有订阅任何物品，先用 /gjm sub <物品链接> 订阅。"
        updated = [self._copy(sub, interval=value) if sub in targets else sub for sub in self._items]
        if not self._commit(updated):
            return False, "保存刷新间隔失败，请检查插件数据目录写入权限或日志。"
        if market_name:
            return True, f"已把该物品刷新间隔设为 {value} 秒。"
        return True, f"已把你的刷新间隔设为 {value} 秒（对你名下 {len(targets)} 件物品同时生效）。"

    def set_interval_by_index(self, uid: str, seconds: int, index: int, target_umo: str | None = None):
        own = self.subscriptions_of(uid, target_umo)
        if not own:
            return False, "你还没有订阅。用 /gjm sub <物品链接> 订阅。"
        if index < 1 or index > len(own):
            return False, f"编号无效，请输入 1~{len(own)} 之间的编号。"
        return self.set_interval(uid, seconds, own[index - 1].market_name, target_umo)

    def set_refresh_mode(self, uid: str, mode: str, target_umo: str | None = None):
        if mode not in REFRESH_MODES:
            return False, "刷新模式只能是 always 或 only_change。"
        targets = [
            sub for sub in self._items if sub.uid == uid and (target_umo is None or sub.target_umo == target_umo)
        ]
        if not targets:
            return False, "你还没有订阅。用 /gjm sub <物品链接> 订阅。"
        updated = [self._copy(sub, mode=mode) if sub in targets else sub for sub in self._items]
        if not self._commit(updated):
            return False, "保存刷新模式失败，请检查插件数据目录写入权限或日志。"
        return True, f"已把刷新模式设为 {mode}（对你名下 {len(targets)} 件物品生效）。"

    def _commit(self, subs: list[Subscription]) -> bool:
        previous = self._items
        self._items = list(subs)
        try:
            persisted = self.persist(self._items)
            if persisted is False:
                raise RuntimeError("持久化回调返回失败")
        except Exception as exc:
            self._items = previous
            if self.logger:
                self.logger.error(f"保存订阅失败: {exc}")
            return False
        return True
