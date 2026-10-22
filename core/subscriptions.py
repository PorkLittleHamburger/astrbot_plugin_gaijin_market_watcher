"""订阅层：管理「谁订阅了哪件物品」。

数据落点：插件配置文件的 `user_subscriptions`（template_list）。
这样做同时满足两条需求：
  * **WebUI 插件配置页里可直接查看 / 手动修改**；
  * 自定义 Page 里可以用按钮增删，底层始终是同一份数据，不存在双份真相。

物品标识：**只接受物品页链接**（如
https://trade.gaijin.net/market/1067/id50381_f_16xl_usa），
由 resolver 统一解析成 market_name，非链接输入一律拒绝。
"""

from __future__ import annotations

from collections.abc import Callable

from .constants import MAX_POLL_INTERVAL, MIN_POLL_INTERVAL, REFRESH_MODES
from .models import Subscription
from .resolver import extract_link_reference, prettify_market_name

#: 给用户看的输入示例
URL_EXAMPLE = "https://trade.gaijin.net/market/1067/id50381_f_16xl_usa"
REJECT_HINT = f"只接受物品页链接，例如：{URL_EXAMPLE}"


class SubscriptionStore:
    """用户订阅的读写入口。所有改动都会写回插件配置。"""

    def __init__(
        self,
        config,
        persist: Callable[[list[Subscription]], None],
        logger=None,
    ) -> None:
        #: 强类型配置对象（用于展示名等只读查询）
        self.config = config
        #: 持久化回调：把订阅列表写回配置
        self.persist = persist
        self.logger = logger
        #: 权威列表：初始化时从配置载入，之后由本对象维护。
        #: 这样即使持久化回调没能同步刷新配置对象，也不会出现"重复订阅"这类逻辑漏判。
        self._items: list[Subscription] = list(config.subscriptions)

    def reload(self, items: list[Subscription] | None = None) -> None:
        """从配置（或外部传入的列表）重新载入，用于配置热更新后同步。"""
        self._items = list(items if items is not None else self.config.subscriptions)

    # ------------------------------------------------------------------ 查询

    def all(self) -> list[Subscription]:
        return list(self._items)

    def subscribers_of(self, market_name: str) -> list[Subscription]:
        return [s for s in self._items if s.market_name == market_name and s.enabled]

    def subscriptions_of(self, uid: str) -> list[Subscription]:
        """按配置展示顺序返回某个用户的全部订阅。"""
        return [s for s in self._items if s.uid == uid]

    def find(self, uid: str, market_name: str) -> Subscription | None:
        return next((s for s in self._items if s.uid == uid and s.market_name == market_name), None)

    # ------------------------------------------------------------------ 变更

    @staticmethod
    def parse_url(url: str) -> str:
        """从用户输入中解析 market_name；非链接返回空串。"""
        _, market_name = extract_link_reference(str(url or ""))
        return market_name

    def subscribe(
        self,
        uid: str,
        url: str,
        *,
        display_name: str = "",
        sell_below: float = 0.0,
        buy_above: float = 0.0,
    ) -> tuple[bool, str]:
        """新增一条订阅。

        间隔是**用户级**的：该用户已有订阅时，新物品自动继承同样的间隔与门限，
        保证“同一用户刷新间隔一致”。
        """
        market_name = self.parse_url(url)
        if not market_name:
            return False, "只接受物品页链接。"

        peers = [s for s in self._items if s.uid == uid]
        if peers:
            interval = peers[0].interval
            mode = peers[0].mode
            sell_below = sell_below or peers[0].sell_below
            buy_above = buy_above or peers[0].buy_above
        else:
            interval = self.config._default_interval  # noqa: SLF001
            mode = self.config.default_refresh_mode

        existing = self.find(uid, market_name)
        name = display_name or self.config.display_name_of(market_name) or prettify_market_name(market_name)
        sub = Subscription(
            uid=uid,
            market_name=market_name,
            display_name=name,
            sell_below=max(0.0, sell_below),
            buy_above=max(0.0, buy_above),
            enabled=True,
            interval=interval,
            mode=mode,
        )
        if existing is not None:
            subs = [sub if s.key == sub.key else s for s in self._items]
            self._commit(subs)
            return True, f"已更新订阅：{name}"
        self._commit([*self._items, sub])
        return True, f"订阅成功：{name}\n（{market_name}，刷新间隔 {interval} 秒）"

    def unsubscribe(self, uid: str, url: str) -> tuple[bool, str]:
        """取消订阅。"""
        market_name = self.parse_url(url)
        if not market_name:
            return False, REJECT_HINT
        return self.remove(uid, market_name)

    def remove(self, uid: str, market_name: str) -> tuple[bool, str]:
        subs = list(self._items)
        remaining = [s for s in subs if not (s.uid == uid and s.market_name == market_name)]
        if len(remaining) == len(subs):
            return False, "你没有订阅这件物品。"
        self._commit(remaining)
        return True, f"已取消订阅：{market_name}"

    def remove_by_index(self, uid: str, index: int) -> tuple[bool, str]:
        """按 ``/gjm my`` 显示的编号删除当前用户的一条订阅。"""
        own = self.subscriptions_of(uid)
        if not own:
            return False, "你还没有订阅。用 /gjm sub <物品链接> 订阅。"
        if index < 1 or index > len(own):
            return False, f"编号无效，请输入 1~{len(own)} 之间的编号。"

        target = own[index - 1]
        remaining = [s for s in self._items if s.key != target.key]
        self._commit(remaining)
        name = target.display_name or target.market_name
        return True, f"已取消订阅：{name}（剩余 {len(own) - 1} 件）"

    def update(
        self,
        uid: str,
        market_name: str,
        *,
        display_name: str | None = None,
        sell_below: float | None = None,
        buy_above: float | None = None,
        enabled: bool | None = None,
    ) -> tuple[bool, str]:
        """修改某条订阅（供 WebUI 页面使用）。"""
        subs = list(self._items)
        for index, sub in enumerate(subs):
            if sub.uid == uid and sub.market_name == market_name:
                subs[index] = Subscription(
                    uid=uid,
                    market_name=market_name,
                    display_name=sub.display_name if display_name is None else display_name,
                    sell_below=sub.sell_below if sell_below is None else max(0.0, sell_below),
                    buy_above=sub.buy_above if buy_above is None else max(0.0, buy_above),
                    enabled=sub.enabled if enabled is None else enabled,
                    interval=sub.interval,
                    mode=sub.mode,
                )
                self._commit(subs)
                return True, "已保存。"
        return False, "未找到该订阅。"

    def set_interval(self, uid: str, seconds: int, market_name: str = "") -> tuple[bool, str]:
        """设置某用户订阅的刷新间隔；market_name 为空则作用于其全部订阅。"""
        value = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, int(seconds)))
        targets = [s for s in self._items if s.uid == uid and (not market_name or s.market_name == market_name)]
        if not targets:
            if market_name:
                return False, "你没有订阅这件物品。"
            return False, "你还没有订阅任何物品，先用 /gjm sub <物品链接> 订阅。"

        target_keys = {s.key for s in targets}
        updated: list[Subscription] = []
        for sub in self._items:
            if sub.key in target_keys:
                updated.append(
                    Subscription(
                        uid=sub.uid,
                        market_name=sub.market_name,
                        display_name=sub.display_name,
                        sell_below=sub.sell_below,
                        buy_above=sub.buy_above,
                        enabled=sub.enabled,
                        interval=value,
                        mode=sub.mode,
                    )
                )
            else:
                updated.append(sub)
        self._commit(updated)
        if market_name:
            return True, f"已把该物品刷新间隔设为 {value} 秒。"
        return True, f"已把你的刷新间隔设为 {value} 秒（对你名下 {len(targets)} 件物品同时生效）。"

    def set_interval_by_index(self, uid: str, seconds: int, index: int) -> tuple[bool, str]:
        own = self.subscriptions_of(uid)
        if not own:
            return False, "你还没有订阅。用 /gjm sub <物品链接> 订阅。"
        if index < 1 or index > len(own):
            return False, f"编号无效，请输入 1~{len(own)} 之间的编号。"
        return self.set_interval(uid, seconds, own[index - 1].market_name)

    def set_refresh_mode(self, uid: str, mode: str) -> tuple[bool, str]:
        """设置当前用户全部订阅的刷新模式。"""
        if mode not in REFRESH_MODES:
            return False, "刷新模式只能是 always 或 only_change。"
        targets = [s for s in self._items if s.uid == uid]
        if not targets:
            return False, "你还没有订阅。用 /gjm sub <物品链接> 订阅。"

        updated = [
            Subscription(
                uid=sub.uid,
                market_name=sub.market_name,
                display_name=sub.display_name,
                sell_below=sub.sell_below,
                buy_above=sub.buy_above,
                enabled=sub.enabled,
                interval=sub.interval,
                mode=mode,
            )
            if sub.uid == uid
            else sub
            for sub in self._items
        ]
        self._commit(updated)
        return True, f"已把刷新模式设为 {mode}（对你名下 {len(targets)} 件物品生效）。"

    def _commit(self, subs: list[Subscription]) -> None:
        """先更新本地权威列表，再落盘；保证后续读取立刻看到最新状态。"""
        self._items = list(subs)
        try:
            self.persist(self._items)
        except Exception as exc:  # 落盘失败不应让内存状态与用户预期错位
            if self.logger:
                self.logger.error(f"保存订阅失败: {exc}")
