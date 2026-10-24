"""调度层：按配置的间隔轮询行情，并把结果分发给「全局目标」与「订阅者」。

轮询与投递是两件事，这里刻意分开处理：

  轮询：`config.watched_market_names` 是「全局监控清单」与「用户订阅」的并集，
        每件物品**只抓一次**，避免多人订阅同一物品时重复打接口。

  投递：
        * 私聊订阅 -> 按个人模式和间隔推送；
        * 群聊 -> 固定间隔强制刷新全群订阅，并合并成一条完整快照。

其它设计要点：
  * 单个 asyncio.Task 串行轮询，物品之间插入小间隔，避免并发打爆接口 / 触发风控；
  * 所有异常都在循环内部消化，绝不让插件因一次网络抖动而崩溃；
  * 令牌失效时进入"退避 + 通知拥有者"状态，而不是无限重试刷屏。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress

from . import platforms
from .api_client import AuthExpiredError, MarketApiError
from .auth import LoginRequiredError
from .constants import MIN_POLL_INTERVAL, ONLY_CHANGE_INTERVAL, REFRESH_ONLY_CHANGE
from .models import Quote, SnapshotItem, TriggerEvent

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
        extra_market_names=None,
        extra_interval=None,
        on_quotes=None,
    ) -> None:
        self.client = client
        self.auth = auth
        self.storage = storage
        self.config = config
        self.notifier = notifier
        self.logger = logger
        #: 须要用户介入时（如令牌全部失效）的回调：async def (reason: str) -> None
        self.on_auth_failure = on_auth_failure
        self.extra_market_names = extra_market_names
        self.extra_interval = extra_interval
        self.on_quotes = on_quotes

        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        #: 手动刷新、普通轮询和群快照可能从不同协程同时触发，
        #: 串行化抓取与分发，避免重复请求和重复推送。
        self._poll_lock = asyncio.Lock()
        self._running = False
        self._auth_backoff_until = 0.0
        self.last_poll_at: float = 0.0
        self.last_poll_error: str = ""
        self.poll_count: int = 0
        #: 物品 -> 下次到点时间（抓取频率 = 该物品所有订阅里的最小值）
        self._next_due: dict[str, float] = {}
        #: (用户, 物品) -> 他上次成功收到推送时的行情，作为下次对比基准
        self._last_pushed: dict[tuple[str, str, str], Quote] = {}
        #: (用户, 物品) -> 当前用于比较的基线；发送失败时保持旧值以支持下一轮重试
        self._comparison_base: dict[tuple[str, str, str], Quote | None] = {}
        #: (用户, 物品) -> 他下一次允许收到推送的时间（推送频率 = 他自己设的 interval）
        self._next_notify: dict[tuple[str, str, str], float] = {}
        #: 物品元数据（图标/标签）后台补齐，失败不影响主流程
        self._meta_pending: set[str] = set()
        self._meta_tasks: set[asyncio.Task] = set()
        #: 群聊会话 -> 下一次全量快照时间
        self._next_group_push: dict[str, float] = {}
        #: 物品 -> 最近一次抓取错误；成功后立即清除
        self._latest_errors: dict[str, str] = {}
        #: 物品 -> 最近一次实际发起抓取的时间，避免普通轮询与群快照同轮重复请求
        self._last_attempt_at: dict[str, float] = {}

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """启动轮询。重复调用是安全的（幂等）。"""
        if self.running:
            return False
        self._running = True
        self._sync_group_schedule(time.time())
        self._task = asyncio.create_task(self._loop(), name="gaijin-market-watcher")
        self.logger.info(
            f"行情监控已启动：{len(self._private_market_names())} 个物品、"
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

    def reconfigure(self) -> None:
        """让新的订阅间隔即时约束现有调度窗口。"""
        now = time.time()
        watched = set(self._private_market_names())
        for market_name in watched:
            proposed = now + max(MIN_POLL_INTERVAL, self._interval_for(market_name))
            current = self._next_due.get(market_name)
            self._next_due[market_name] = proposed if current is None else min(current, proposed)
        for market_name in [name for name in self._next_due if name not in watched]:
            self._next_due.pop(market_name, None)

        active = {sub.key for sub in self.config.enabled_subscriptions}
        for sub in self.config.enabled_subscriptions:
            key = sub.key
            if key not in self._last_pushed:
                continue
            interval = ONLY_CHANGE_INTERVAL if sub.mode == REFRESH_ONLY_CHANGE else max(MIN_POLL_INTERVAL, sub.interval)
            proposed = now + interval
            current = self._next_notify.get(key)
            self._next_notify[key] = proposed if current is None else min(current, proposed)
        for key in [key for key in self._next_notify if key not in active]:
            self._next_notify.pop(key, None)
        self._sync_group_schedule(now)
        self._wake.set()

    # ------------------------------------------------------------ 主循环

    async def _loop(self) -> None:
        # 启动后稍作等待，避免与 AstrBot 初始化抢资源
        await asyncio.sleep(3)
        while self._running:
            try:
                await self.poll_due()
                await self.poll_due_groups()
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
        backoff_remaining = self._auth_backoff_remaining(now)
        if backoff_remaining > 0:
            return max(1.0, min(backoff_remaining, MAX_SLEEP_SECONDS))
        waits = [self._next_due[mn] - now for mn in self._private_market_names() if mn in self._next_due]
        waits.extend(deadline - now for deadline in self._next_group_push.values())
        wait = min(waits) if waits else MAX_SLEEP_SECONDS
        return max(1.0, min(wait, MAX_SLEEP_SECONDS))

    def _group_targets(self) -> list[str]:
        groups: list[str] = []
        for sub in self.config.enabled_subscriptions:
            target = self._target_of(sub)
            if target and platforms.is_group_session(platforms.session_type_of(target)) and target not in groups:
                groups.append(target)
        return groups

    def _private_market_names(self) -> list[str]:
        names: list[str] = []
        for sub in self.config.enabled_subscriptions:
            target = self._target_of(sub)
            if not platforms.is_group_session(platforms.session_type_of(target)) and sub.market_name not in names:
                names.append(sub.market_name)
        if callable(self.extra_market_names):
            with suppress(Exception):
                for market_name in self.extra_market_names() or []:
                    if market_name and market_name not in names:
                        names.append(str(market_name))
        return names

    def _interval_for(self, market_name: str) -> int:
        base = int(self.config.interval_of(market_name))
        names = []
        if callable(self.extra_market_names):
            with suppress(Exception):
                names = list(self.extra_market_names() or [])
        if market_name in names and callable(self.extra_interval):
            with suppress(Exception):
                return min(base, max(1, int(self.extra_interval())))
        return base

    def _target_of(self, sub) -> str:
        if sub.target_umo:
            return sub.target_umo
        resolver = getattr(self.notifier, "session_of", None)
        return str(resolver(sub.uid) or "") if callable(resolver) else ""

    def _sync_group_schedule(self, now: float) -> None:
        groups = set(self._group_targets())
        interval = max(1, int(self.config.group_push_interval))
        for target in groups:
            self._next_group_push.setdefault(target, now + interval)
        for target in [target for target in self._next_group_push if target not in groups]:
            self._next_group_push.pop(target, None)

    # ------------------------------------------------------------ 单次轮询

    async def poll_once(self) -> list[TriggerEvent]:
        """立即轮询全部物品（/gjma mysub、唤醒时使用）。"""
        return await self.poll_items(self._private_market_names())

    async def poll_due(self) -> list[TriggerEvent]:
        """只轮询已到点的物品（间隔由各自订阅者决定）。"""
        now = time.time()
        due = [mn for mn in self._private_market_names() if now >= self._next_due.get(mn, 0.0)]
        if not due:
            return []
        return await self.poll_items(due)

    async def poll_due_groups(self) -> dict[str, bool]:
        """到期群统一刷新物品并发送完整快照。"""
        if self._auth_backoff_remaining() > 0:
            self.logger.debug("市场鉴权仍在退避期内，本轮群快照跳过。")
            return {}
        now = time.time()
        self._sync_group_schedule(now)
        due = [target for target, deadline in self._next_group_push.items() if now >= deadline]
        if not due:
            return {}
        interval = max(1, int(self.config.group_push_interval))
        for target in due:
            self._next_group_push[target] = now + interval

        due_set = set(due)
        markets: list[str] = []
        for sub in self.config.enabled_subscriptions:
            target = self._target_of(sub)
            if target in due_set and sub.market_name not in markets:
                markets.append(sub.market_name)
        fresh_after = now - 1.0
        markets_to_fetch = [name for name in markets if self._last_attempt_at.get(name, 0.0) < fresh_after]
        if markets_to_fetch:
            await self.poll_items(markets_to_fetch)

        results: dict[str, bool] = {}
        for target in due:
            snapshot = self._build_group_snapshot(target)
            if not snapshot:
                results[target] = False
                continue
            results[target] = await self.notifier.push_group_snapshot(target, snapshot)
        success = sum(1 for delivered in results.values() if delivered)
        self.logger.info(f"群聊全量推送：到期 {len(due)} 个群，成功 {success} 个。")
        return results

    async def poll_items(self, names: list[str]) -> list[TriggerEvent]:
        """串行抓取指定物品并分发事件。"""
        async with self._poll_lock:
            return await self._poll_items_unlocked(names)

    async def _poll_items_unlocked(self, names: list[str]) -> list[TriggerEvent]:
        """抓取指定物品 -> 判定 -> 推送给订阅者。

        返回值：本轮真正产生的事件（按用户聚合后展平），供调用方展示。
        """
        if self._auth_backoff_remaining() > 0:
            self.logger.debug("市场鉴权仍在退避期内，本轮跳过。")
            return []

        self.poll_count += 1
        self.last_poll_at = time.time()

        watched = [mn for mn in names if mn]
        if not watched:
            self.logger.debug("没有任何被订阅的物品，本轮跳过。")
            return []

        try:
            token = await self.auth.get_token()
        except LoginRequiredError as exc:
            for market_name in watched:
                self._latest_errors[market_name] = str(exc)
            await self._handle_auth_failure(str(exc))
            return []
        except MarketApiError as exc:
            error = f"获取市场令牌失败: {exc}"
            retry_at = time.time()
            for market_name in watched:
                self._latest_errors[market_name] = error
            self._next_due[market_name] = retry_at + self._interval_for(market_name)
            self.last_poll_error = error
            self.logger.warning(error)
            return []
        except Exception as exc:
            error = f"获取市场令牌时发生未知错误: {exc}"
            retry_at = time.time()
            for market_name in watched:
                self._latest_errors[market_name] = error
                self._next_due[market_name] = retry_at + self._interval_for(market_name)
            self.last_poll_error = error
            self.logger.error(error)
            return []
        self._auth_backoff_until = 0.0

        # ---------- 0) 先读取上一轮行情 ----------
        # 必须在写入新行情之前读取，否则判定阶段拿到的"旧值"就是新值，
        # 会导致永远检测不到价格变化。
        previous_map: dict[str, Quote | None] = {
            market_name: self.storage.get_last_quote(market_name) for market_name in watched
        }

        # ---------- 1) 抓取：每件物品只请求一次 ----------
        quotes: dict[str, Quote] = {}
        failures: list[Quote] = []
        auth_error = ""
        cache_write_error = ""

        for index, market_name in enumerate(watched):
            if index > 0 and self.config.item_interval > 0:
                await asyncio.sleep(self.config.item_interval)
            # 先排下一次：无论本次成功失败都不会陷入紧凑重试
            self._next_due[market_name] = time.time() + self._interval_for(market_name)
            self._last_attempt_at[market_name] = time.time()

            display = self.config.display_name_of(market_name) or self.storage.get_item_name(market_name)
            try:
                quote = await self.client.fetch_quote(market_name, token, appid=self.config.appid, display_name=display)
            except AuthExpiredError as exc:
                try:
                    token = await self.auth.get_token(force_refresh=True)
                except LoginRequiredError as refresh_exc:
                    auth_error = str(refresh_exc)
                    self._latest_errors[market_name] = auth_error
                    failures.append(Quote(market_name=market_name, display_name=display, error=str(refresh_exc)))
                    break
                except MarketApiError as refresh_exc:
                    error = f"{exc}; 刷新令牌异常: {refresh_exc}"
                    self._latest_errors[market_name] = error
                    failures.append(Quote(market_name=market_name, display_name=display, error=error))
                    break
                except Exception as refresh_exc:
                    error = f"{exc}; 刷新令牌异常: {refresh_exc}"
                    self._latest_errors[market_name] = error
                    failures.append(Quote(market_name=market_name, display_name=display, error=error))
                    break

                try:
                    quote = await self.client.fetch_quote(
                        market_name, token, appid=self.config.appid, display_name=display
                    )
                except AuthExpiredError as retry_exc:
                    auth_error = f"{exc}; 刷新令牌后仍被拒绝: {retry_exc}"
                    self._latest_errors[market_name] = auth_error
                    failures.append(Quote(market_name=market_name, display_name=display, error=auth_error))
                    break
                except MarketApiError as retry_exc:
                    self._latest_errors[market_name] = str(retry_exc)
                    failures.append(Quote(market_name=market_name, display_name=display, error=str(retry_exc)))
                    continue
                except Exception as retry_exc:
                    error = f"刷新令牌后抓取失败: {retry_exc}"
                    self._latest_errors[market_name] = error
                    failures.append(
                        Quote(
                            market_name=market_name,
                            display_name=display,
                            error=error,
                        )
                    )
                    continue
            except MarketApiError as exc:
                self._latest_errors[market_name] = str(exc)
                failures.append(Quote(market_name=market_name, display_name=display, error=str(exc)))
                continue
            except Exception as exc:
                error = f"未知错误: {exc}"
                self._latest_errors[market_name] = error
                failures.append(
                    Quote(
                        market_name=market_name,
                        display_name=display,
                        error=error,
                    )
                )
                continue

            quotes[market_name] = quote
            self._latest_errors.pop(market_name, None)
            if self.storage.set_last_quote(quote) is False:
                cache_write_error = f"行情缓存写入失败：{market_name}"
                self.logger.warning(cache_write_error)
            self._ensure_item_meta(market_name, quote.title)

        if quotes and self.on_quotes is not None:
            try:
                callback_result = self.on_quotes(quotes)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            except Exception as exc:
                self.logger.warning(f"自动交易行情分发失败: {exc}")

        if auth_error:
            await self._handle_auth_failure(auth_error)

        # ---------- 2) 判定与分发：只投递给订阅者 ----------
        per_user, user_names = self._evaluate_subscribers(quotes, previous_map)

        if per_user:
            await self.notifier.push_to_subscribers(
                per_user,
                user_names,
                failures,
                on_delivered=self._mark_delivered,
            )

        if failures:
            # 抓取失败也通知拥有者，便于排障（订阅者正文里也会带失败原因）
            owner_sessions = self.notifier.owner_sessions()
            if owner_sessions:
                await self.notifier.push(self.notifier.render_digest([], failures), owner_sessions)

        if per_user or failures:
            self.logger.info(f"本轮：订阅推送 {len(per_user)} 人、失败 {len(failures)} 条。")
        if auth_error:
            # _handle_auth_failure 已记录具体原因，不能在本次轮询结束前清空。
            pass
        elif failures:
            self.last_poll_error = failures[0].error
        else:
            self.last_poll_error = cache_write_error
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
                persisted = self.storage.set_item_meta(
                    market_name,
                    {
                        "icon": str(hit.get("icon") or ""),
                        "tags": [str(t) for t in (hit.get("tags") or [])],
                        "color": str(hit.get("color") or ""),
                        "name": str(hit.get("name") or ""),
                    },
                )
                if persisted is False:
                    self.logger.warning(f"[GJM] 物品元数据写入失败：{display_name or market_name}")
                    return
                self.logger.info(f"[GJM] 已缓存物品元数据：{display_name or market_name}")
                return
        except Exception as exc:
            self.logger.debug(f"[GJM] 物品元数据获取失败（{market_name}）：{exc}")
        finally:
            self._meta_pending.discard(market_name)

    # ------------------------------------------------------------ 判定

    def _build_group_snapshot(self, target_umo: str) -> list[SnapshotItem]:
        """构造某个群的完整订阅快照，同物多人只保留一个物品块。"""
        grouped: dict[str, list] = {}
        for sub in self.config.enabled_subscriptions:
            target = self._target_of(sub)
            if target == target_umo:
                grouped.setdefault(sub.market_name, []).append(sub)

        items: list[SnapshotItem] = []
        for market_name, subscribers in grouped.items():
            first = subscribers[0]
            display = self.config.display_name_of(market_name) or self.storage.get_item_name(market_name)
            quote = self.storage.get_last_quote(market_name)
            if quote is not None and (not quote.ok or (quote.sell_min is None and quote.buy_max is None)):
                quote = None
            history = self.storage.get_price_history(
                market_name,
                limit=max(2, self.config.chart_history_points + 1),
            )
            previous = None
            if history:
                prior = [
                    point
                    for point in history
                    if quote is None or float(point.get("t") or 0) < float(quote.timestamp) - 1e-6
                ]
                point = prior[-1] if prior else (history[-2] if len(history) > 1 else None)
                if point is not None:
                    previous = Quote(
                        market_name=market_name,
                        sell_min=point.get("sell"),
                        buy_max=point.get("buy"),
                        timestamp=float(point.get("t") or 0),
                        display_name=display,
                    )
            mentions: list[tuple[str, str]] = []
            seen: set[str] = set()
            for sub in subscribers:
                if sub.uid in seen:
                    continue
                seen.add(sub.uid)
                mentions.append((sub.uid, self.storage.get_user_name(sub.uid)))
            items.append(
                SnapshotItem(
                    market_name=market_name,
                    display_name=display or first.display_name or market_name,
                    quote=quote,
                    previous=previous,
                    mentions=mentions,
                    error=self._latest_errors.get(market_name, ""),
                )
            )
        return items

    def _evaluate_subscribers(
        self, quotes: dict[str, Quote], previous_map: dict[str, Quote | None]
    ) -> tuple[dict[tuple[str, str], list[TriggerEvent]], dict[tuple[str, str], str]]:
        """按用户聚合判定结果：每个人只拿到自己订阅物品的事件。

        **两层节流**（两者不可混为一谈）：
          * 抓取频率由物品决定 = 该物品所有订阅里最小的 interval（最快的订阅者不该被拖慢）；
          * 推送频率由**订阅者自己**决定 = 每个 (用户, 物品) 有独立窗口，窗口内静默，
            窗口一开就把"相比他上次收到的变化"一次性汇报，因此慢订阅者不会被
            快订阅者的节奏带着刷屏。

        这里只生成事件，不推进推送状态；基线必须由推送层确认送达后更新。
        """
        per_user: dict[tuple[str, str], list[TriggerEvent]] = {}
        user_names: dict[tuple[str, str], str] = {}
        now = time.time()
        alive: set[tuple[str, str, str]] = set()

        for sub in self.config.enabled_subscriptions:
            target_umo = self._target_of(sub)
            if platforms.is_group_session(platforms.session_type_of(target_umo)):
                continue
            key = (target_umo, sub.uid, sub.market_name)
            alive.add(key)
            quote = quotes.get(sub.market_name)
            if quote is None:
                continue
            if now < self._next_notify.get(key, 0.0):
                # 还在他自己的安静窗口内：不打扰，也不推进窗口
                continue
            if key in self._comparison_base:
                baseline = self._comparison_base[key]
            else:
                baseline = self._last_pushed.get(key) or previous_map.get(sub.market_name)
                self._comparison_base[key] = baseline
            event = self.notifier.evaluate_subscription(sub, quote, baseline)
            if event is None:
                continue
            delivery_key = (target_umo, sub.uid)
            per_user.setdefault(delivery_key, []).append(event)
            user_names.setdefault(delivery_key, self.storage.get_user_name(sub.uid))

        # 订阅被删掉后不留垃圾
        for stale in [k for k in self._last_pushed if k not in alive]:
            self._last_pushed.pop(stale, None)
            self._comparison_base.pop(stale, None)
            self._next_notify.pop(stale, None)
        for stale in [k for k in self._comparison_base if k not in alive]:
            self._comparison_base.pop(stale, None)
        return per_user, user_names

    def _mark_delivered(self, target_umo: str, uid: str, events: list[TriggerEvent]) -> None:
        """推送确认送达后，推进该用户的对比基准与安静窗口。"""
        now = time.time()
        latest: dict[str, Quote] = {}
        for event in events:
            latest[event.quote.market_name] = event.quote

        subscriptions = {
            (self._target_of(sub), sub.uid, sub.market_name): sub for sub in self.config.enabled_subscriptions
        }
        for market_name, quote in latest.items():
            key = (target_umo, uid, market_name)
            sub = subscriptions.get(key)
            if sub is None:
                continue
            self._last_pushed[key] = quote
            self._comparison_base[key] = quote
            notify_interval = (
                ONLY_CHANGE_INTERVAL if sub.mode == REFRESH_ONLY_CHANGE else max(MIN_POLL_INTERVAL, sub.interval)
            )
            self._next_notify[key] = now + notify_interval

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

    def _auth_backoff_remaining(self, now: float | None = None) -> float:
        """当前鉴权退避还剩多少秒；0 表示可以正常请求。"""
        return max(0.0, self._auth_backoff_until - (time.time() if now is None else now))

    def reset_auth_backoff(self) -> None:
        """登录成功后立即解除退避，让下一次轮询可以马上继续。"""
        self._auth_backoff_until = 0.0

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
            "watched": len(self._private_market_names()),
            "groups": len(self._group_targets()),
            "group_push_interval": self.config.group_push_interval,
        }
