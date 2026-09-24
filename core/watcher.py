"""调度层：按配置的间隔轮询行情，并把结果分发给「全局目标」与「订阅者」。

轮询与投递是两件事，这里刻意分开处理：

  轮询：`config.watched_market_names` 是「全局监控清单」与「用户订阅」的并集，
        每件物品**只抓一次**，避免多人订阅同一物品时重复打接口。

  投递：只投递两类 ——
        * 用户订阅（user_subscriptions）-> 订阅者本人，群聊里会 @ 到他。

其它设计要点：
  * 单个 asyncio.Task 串行轮询，物品之间插入小间隔，避免并发打爆接口 / 触发风控；
  * 所有异常都在循环内部消化，绝不让插件因一次网络抖动而崩溃；
  * 令牌失效时进入"退避 + 通知拥有者"状态，而不是无限重试刷屏。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress

from .api_client import AuthExpiredError, MarketApiError
from .auth import LoginRequiredError
from .constants import MIN_POLL_INTERVAL
from .models import Quote, TriggerEvent

#: 令牌失效后的重试间隔（秒），避免反复打扰用户
AUTH_BACKOFF_SECONDS = 600


#: 单次 sleep 上限：保证配置变更 / 唤醒能被及时感知
MAX_SLEEP_SECONDS = 60.0


class MarketWatcher:
    def __init__(
        self,
        *,
        client,
        auth,
        storage,
        config,
        notifier,
        logger,
        on_auth_failure=None,
    ) -> None:
        self.client = client
        self.auth = auth
        self.storage = storage
        self.config = config
        self.notifier = notifier
        self.logger = logger
        #: 须要用户介入时（如令牌全部失效）的回调：async def (reason: str) -> None
        self.on_auth_failure = on_auth_failure

        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._running = False
        self._auth_backoff_until = 0.0
        self.last_poll_at: float = 0.0
        self.last_poll_error: str = ""
        self.poll_count: int = 0
        #: 物品 -> 下次到点时间（抓取频率 = 该物品所有订阅里的最小值）
        self._next_due: dict[str, float] = {}
        #: (用户, 物品) -> 他上次收到推送时的行情，作为下次对比基准
        self._last_pushed: dict[tuple[str, str], Quote] = {}
        #: (用户, 物品) -> 他下一次允许收到推送的时间（推送频率 = 他自己设的 interval）
        self._next_notify: dict[tuple[str, str], float] = {}
        #: 物品元数据（图标/标签）后台补齐，失败不影响主流程
        self._meta_pending: set[str] = set()
        self._meta_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """启动轮询。重复调用是安全的（幂等）。"""
        if self.running:
            return False
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="gaijin-market-watcher")
        self.logger.info(
            f"行情监控已启动：{len(self.config.watched_market_names)} 个物品、"
            f"{len(self.config.enabled_subscriptions)} 条用户订阅，"
            f"间隔 {self.config.min_interval}~{max(list(self.config.intervals.values()) or [0])} 秒（各自独立）。"
        )
        return True

    async def stop(self) -> None:
        """停止轮询（插件停用 / 卸载时调用）。"""
        self._running = False
        self._wake.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.logger.info("行情监控已停止。")

    def wake(self) -> None:
        """立刻触发一轮轮询。"""
        self._wake.set()

    # ------------------------------------------------------------ 主循环

    async def _loop(self) -> None:
        # 启动后稍作等待，避免与 AstrBot 初始化抢资源
        await asyncio.sleep(3)
        while self._running:
            try:
                await self.poll_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 兜底：任何异常都不允许终结循环
                self.last_poll_error = str(exc)
                self.logger.error(f"轮询出现未预期异常: {exc}")

            wait = self._next_wait_seconds()
            self._wake.clear()
            # 等待"到点"或"被唤醒"，二者先到先算
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=wait)

    def _next_wait_seconds(self) -> float:
        """距离「下一件物品到点」还有多久。

        下限 1 秒；上限 MAX_SLEEP_SECONDS，保证配置改动能被及时感知。
        """
        now = time.time()
        waits = [self._next_due[mn] - now for mn in self.config.watched_market_names if mn in self._next_due]
        wait = min(waits) if waits else 1.0
        if now < self._auth_backoff_until:
            wait = max(wait, self._auth_backoff_until - now)
        return max(1.0, min(wait, MAX_SLEEP_SECONDS))

    # ------------------------------------------------------------ 单次轮询

    async def poll_once(self) -> list[TriggerEvent]:
        """立即轮询全部物品（/gjm now、唤醒时使用）。"""
        return await self.poll_items(self.config.watched_market_names)

    async def poll_due(self) -> list[TriggerEvent]:
        """只轮询已到点的物品（间隔由各自订阅者决定）。"""
        now = time.time()
        due = [mn for mn in self.config.watched_market_names if now >= self._next_due.get(mn, 0.0)]
        if not due:
            return []
        return await self.poll_items(due)

    async def poll_items(self, names: list[str]) -> list[TriggerEvent]:
        """抓取指定物品 -> 判定 -> 推送给订阅者。

        返回值：本轮真正产生的事件（按用户聚合后展平），供调用方展示。
        """
        self.poll_count += 1
        self.last_poll_at = time.time()

        watched = [mn for mn in names if mn]
        if not watched:
            self.logger.debug("没有任何被订阅的物品，本轮跳过。")
            return []

        try:
            token = await self.auth.get_token()
        except LoginRequiredError as exc:
            await self._handle_auth_failure(str(exc))
            return []

        # ---------- 0) 先快照"上一轮"行情 ----------
        # 必须在写入新快照之前读取，否则判定阶段拿到的"旧值"就是新值，
        # 会导致永远检测不到价格变化。
        previous_map: dict[str, Quote | None] = {
            market_name: self.storage.get_last_quote(market_name) for market_name in watched
        }

        # ---------- 1) 抓取：每件物品只请求一次 ----------
        quotes: dict[str, Quote] = {}
        failures: list[Quote] = []
        auth_error = ""

        for index, market_name in enumerate(watched):
            if index > 0 and self.config.item_interval > 0:
                await asyncio.sleep(self.config.item_interval)
            # 先排下一次：无论本次成功失败都不会陷入紧凑重试
            self._next_due[market_name] = time.time() + self.config.interval_of(market_name)

            display = self.config.display_name_of(market_name) or self.storage.get_item_name(market_name)
            try:
                quote = await self.client.fetch_quote(market_name, token, appid=self.config.appid, display_name=display)
            except AuthExpiredError as exc:
                auth_error = str(exc)
                try:
                    token = await self.auth.get_token(force_refresh=True)
                    quote = await self.client.fetch_quote(
                        market_name, token, appid=self.config.appid, display_name=display
                    )
                    auth_error = ""
                except Exception as inner:
                    failures.append(Quote(market_name=market_name, display_name=display, error=str(inner)))
                    break
            except MarketApiError as exc:
                failures.append(Quote(market_name=market_name, display_name=display, error=str(exc)))
                continue
            except Exception as exc:
                failures.append(
                    Quote(
                        market_name=market_name,
                        display_name=display,
                        error=f"未知错误: {exc}",
                    )
                )
                continue

            quotes[market_name] = quote
            self.storage.set_last_quote(quote)
            self._ensure_item_meta(market_name, quote.title)

        if auth_error:
            await self._handle_auth_failure(auth_error)

        # ---------- 2) 判定与分发：只投递给订阅者 ----------
        per_user, user_names = self._evaluate_subscribers(quotes, previous_map)

        if per_user:
            await self.notifier.push_to_subscribers(per_user, user_names, failures)

        if failures:
            # 抓取失败也通知拥有者，便于排障（订阅者正文里也会带失败原因）
            owner_sessions = self.notifier.owner_sessions()
            if owner_sessions:
                await self.notifier.push(self.notifier.render_digest([], failures), owner_sessions)

        if per_user or failures:
            self.logger.info(f"本轮：订阅推送 {len(per_user)} 人、失败 {len(failures)} 条。")
        self.last_poll_error = ""
        return [ev for evs in per_user.values() for ev in evs]

    # ------------------------------------------------------------ 物品元数据

    def _ensure_item_meta(self, market_name: str, display_name: str) -> None:
        """后台补齐物品元数据（图标 / 标签 / 稀有度），供 payload 模板使用。"""
        if not market_name or market_name in self._meta_pending:
            return
        if self.storage.get_item_meta(market_name):
            return
        self._meta_pending.add(market_name)
        task = asyncio.create_task(self._fetch_item_meta(market_name, display_name), name="gjm-meta")
        self._meta_tasks.add(task)
        task.add_done_callback(self._meta_tasks.discard)

    async def _fetch_item_meta(self, market_name: str, display_name: str) -> None:
        try:
            token = await self.auth.get_token()
            candidates = [display_name, market_name, market_name.split("_", 1)[-1]]
            for query in [c for c in candidates if c]:
                try:
                    assets = await self.client.search_items(query, token, appid=self.config.appid, limit=20)
                except Exception:
                    continue
                hit = next((a for a in assets if str(a.get("hash_name")) == market_name), None)
                if not hit:
                    continue
                self.storage.set_item_meta(
                    market_name,
                    {
                        "icon": str(hit.get("icon") or ""),
                        "tags": [str(t) for t in (hit.get("tags") or [])],
                        "color": str(hit.get("color") or ""),
                        "name": str(hit.get("name") or ""),
                    },
                )
                self.logger.info(f"[GJM] 已缓存物品元数据：{display_name or market_name}")
                return
        except Exception as exc:
            self.logger.debug(f"[GJM] 物品元数据获取失败（{market_name}）：{exc}")
        finally:
            self._meta_pending.discard(market_name)

    # ------------------------------------------------------------ 判定

    def _evaluate_subscribers(
        self, quotes: dict[str, Quote], previous_map: dict[str, Quote | None]
    ) -> tuple[dict[str, list[TriggerEvent]], dict[str, str]]:
        """按用户聚合判定结果：每个人只拿到自己订阅物品的事件。

        **两层节流**（两者不可混为一谈）：
          * 抓取频率由物品决定 = 该物品所有订阅里最小的 interval（最快的订阅者不该被拖慢）；
          * 推送频率由**订阅者自己**决定 = 每个 (用户, 物品) 有独立窗口，窗口内静默，
            窗口一开就把"相比他上次收到的变化"一次性汇报，因此慢订阅者不会被
            快订阅者的节奏带着刷屏。
        """
        per_user: dict[str, list[TriggerEvent]] = {}
        user_names: dict[str, str] = {}
        now = time.time()
        alive: set[tuple[str, str]] = set()

        for sub in self.config.enabled_subscriptions:
            quote = quotes.get(sub.market_name)
            if quote is None:
                continue
            key = (sub.uid, sub.market_name)
            alive.add(key)
            if now < self._next_notify.get(key, 0.0):
                # 还在他自己的安静窗口内：不打扰，也不推进窗口
                continue
            baseline = self._last_pushed.get(key) or previous_map.get(sub.market_name)
            event = self.notifier.evaluate_subscription(sub, quote, baseline)
            if event is None:
                continue
            per_user.setdefault(sub.uid, []).append(event)
            user_names.setdefault(sub.uid, self.storage.get_user_name(sub.uid))
            # 只有真的推送了才消耗窗口，并以这条行情作为他下次的对比基准
            self._last_pushed[key] = quote
            self._next_notify[key] = now + max(MIN_POLL_INTERVAL, sub.interval)

        # 订阅被删掉后不留垃圾
        for stale in [k for k in self._last_pushed if k not in alive]:
            self._last_pushed.pop(stale, None)
            self._next_notify.pop(stale, None)
        return per_user, user_names

    # ------------------------------------------------------------ 错误处理

    async def _handle_auth_failure(self, reason: str) -> None:
        """令牌失效：进入退避，并通知拥有者需要人工处理。"""
        self._auth_backoff_until = time.time() + AUTH_BACKOFF_SECONDS
        self.last_poll_error = reason
        self.logger.error(f"市场令牌不可用：{reason}")

        if self.on_auth_failure is not None:
            try:
                await self.on_auth_failure(reason)
            except Exception as exc:  # 通知失败不影响主循环
                self.logger.warning(f"发送鉴权异常通知失败: {exc}")

    # ------------------------------------------------------------ 状态

    def stats(self) -> dict:
        return {
            "running": self.running,
            "poll_count": self.poll_count,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_poll_error,
            "poll_interval": self.config.min_interval,
            "intervals": self.config.intervals,
            "subscriptions": len(self.config.enabled_subscriptions),
            "watched": len(self.config.watched_market_names),
        }
