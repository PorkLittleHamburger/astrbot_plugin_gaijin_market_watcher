"""AstrBot 插件：Gaijin 市场行情监控（astrbot_plugin_gaijin_market_watcher）。

核心能力：
  * 按配置的间隔轮询 trade.gaijin.net 上一件或多件物品的
    「实时最低售价 / 实时最高求购价 / 挂单量」；
  * **登录通过聊天指令完成**：发送 /login 发起登录，再用
    /login <验证码> 提交两步验证码；
  * 订阅按 **UID**（人）区分：群聊里 UMO 相同但每个人 UID 不同，
    推送时会 @ 到订阅者本人；
  * 订阅物品只接受**物品页链接**。

本文件只负责「装配 + 指令层」，业务逻辑全部在 core/ 下按职责拆分：
    core/constants.py      接口契约（端点 / action / 价格刻度 / 错误码）
    core/models.py         纯数据模型（Quote / MonitorSpec / Subscription ...）
    core/config.py         配置解析与校验
    core/config_watch.py   配置热监听（让配置界面改动即时生效）
    core/api_client.py     网络层（唯一的 HTTP 出口）
    core/auth.py           鉴权层（JWT 生命周期 / 无密码续期 / 两阶段 SSO 登录）
    core/subscriptions.py  订阅层（每用户每物品一条，只接受链接）
    core/resolver.py       物品定位层（链接 -> market_name）
    core/storage.py        持久化层（data/plugin_data/）
    core/notifier.py       推送层（判定 / 渲染 / @ 订阅者）
    core/watcher.py        调度层（定时轮询 -> 分发）
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:  # 常规：AstrBot 以包的形式加载插件
    from .core import platforms
    from .core.api_client import GaijinMarketClient
    from .core.auth import AuthManager, LoginError, LoginRequiredError, describe_jwt, extract_refresh_token
    from .core.config import PluginConfig
    from .core.config_watch import ConfigFileWatcher
    from .core.constants import MIN_POLL_INTERVAL
    from .core.models import Subscription
    from .core.notifier import Notifier
    from .core.resolver import extract_reference, prettify_market_name
    from .core.snapshot import SnapshotDispatcher
    from .core.storage import PluginStorage
    from .core.subscriptions import REJECT_HINT, SubscriptionStore
    from .core.watcher import MarketWatcher
except ImportError:  # 兜底：以文件路径方式加载时
    import sys

    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.append(str(Path(__file__).resolve().parent))

    from core import platforms
    from core.api_client import GaijinMarketClient
    from core.auth import AuthManager, LoginError, LoginRequiredError, describe_jwt, extract_refresh_token
    from core.config import PluginConfig
    from core.config_watch import ConfigFileWatcher
    from core.constants import MIN_POLL_INTERVAL
    from core.models import Subscription
    from core.notifier import Notifier
    from core.resolver import extract_reference, prettify_market_name
    from core.snapshot import SnapshotDispatcher
    from core.storage import PluginStorage
    from core.subscriptions import REJECT_HINT, SubscriptionStore
    from core.watcher import MarketWatcher

PLUGIN_NAME = "astrbot_plugin_gaijin_market_watcher"


def _args_after(event: AstrMessageEvent, *command_path: str) -> str:
    """取出指令之后的自由文本参数。

    物品链接、别名等参数交给框架解析容易失真（含 :// 与空格），
    因此这里直接从原始消息里按「指令路径」裁剪。
    """
    text = (event.get_message_str() or "").replace("\u200b", "").strip()
    pattern = r"^[/!#／]?\s*" + r"\s+".join(re.escape(p) for p in command_path) + r"\s*"
    matched = re.match(pattern, text, re.IGNORECASE)
    if matched:
        return text[matched.end() :].strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@register(
    PLUGIN_NAME,
    "porklittlehamburger",
    (
        "监控 Gaijin Market(trade.gaijin.net) 物品的实时最低售价与最高求购价；"
        "发送 /login 登录；按 UID 区分订阅者并在群聊里 @ 到本人。"
    ),
    "2.9.0",
)
class GaijinMarketWatcher(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.raw_config = config if config is not None else {}
        self.cfg = PluginConfig(dict(self.raw_config))

        # ---------- 数据目录（AstrBot 约定：持久化数据放 data/plugin_data） ----------
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # 极端情况下回退
            self.data_dir = Path(__file__).resolve().parent / "data"
        self.storage = PluginStorage(self.data_dir, self.logger)

        # ---------- 各层装配 ----------
        self.client = GaijinMarketClient(
            language=self.cfg.language,
            timeout=self.cfg.request_timeout,
            retries=self.cfg.request_retries,
            min_interval=min(self.cfg.item_interval or 0.4, 1.0),
            logger=self.logger,
        )
        self.auth = AuthManager(self.client, self.storage, self.cfg, self.logger)
        self.auth.bootstrap()

        self.subscriptions = SubscriptionStore(self.cfg, self._persist_subscriptions, self.logger)
        self.notifier = Notifier(self.context, self.storage, self.cfg, self.logger)
        #: 网页快照：价格变化后异步补一张商品页截图（失败只丢图，不影响推送）
        self.snapshots = SnapshotDispatcher(
            context=self.context,
            config=lambda: self.cfg,
            storage=self.storage,
            jwt_provider=self.auth.get_token,
            appid_provider=lambda: self.cfg.appid,
            data_dir=self.data_dir,
            logger=self.logger,
        )
        self.notifier.set_snapshot_hook(self.snapshots.schedule)
        self.notifier.set_snapshot_provider(self.snapshots)
        self.watcher = MarketWatcher(
            client=self.client,
            auth=self.auth,
            storage=self.storage,
            config=self.cfg,
            notifier=self.notifier,
            logger=self.logger,
            on_auth_failure=self._notify_auth_failure,
        )

        # ---------- 配置热监听 ----------
        self._config_path = self._resolve_config_path()
        self._config_watcher = ConfigFileWatcher(self._config_path, self._on_config_changed, self.logger)
        #: 已消费过的验证码，避免同一串被反复提交
        self._consumed_code = self.cfg.twofa_code

        self._started = False
        self._auth_notice_at = 0.0
        #: UID -> 最近一次会话 的内存缓存，避免重复写盘
        self._seen_sessions: dict[str, str] = {}

        # 尽量立刻启动；若当前不在事件循环中，则交给 on_astrbot_loaded 钩子
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._ensure_started())

    # ================================================================= 生命周期

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self) -> None:
        await self._ensure_started()

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True

        for hint in self.cfg.validate():
            self.logger.info(f"[GJM] {hint}")

        self._config_watcher.start()
        self.watcher.start()
        # 启动时补一次：可能用户已经先填好账号/验证码再重载插件
        await self._consume_config_code()

    async def terminate(self) -> None:
        """插件停用 / 卸载时调用：务必释放后台任务与连接。"""
        with contextlib.suppress(Exception):
            await self.snapshots.shutdown()
        with contextlib.suppress(Exception):
            await self.notifier.shutdown()
        with contextlib.suppress(Exception):
            await self.watcher.stop()
        with contextlib.suppress(Exception):
            await self._config_watcher.stop()
        with contextlib.suppress(Exception):
            await self.client.close()

    # ================================================================= 配置

    def _resolve_config_path(self) -> Path:
        """定位 AstrBot 为本插件生成的配置文件。

        不同安装布局下 data 目录的位置不同，因此这里探测多个候选：
          1. <数据根>/plugin_data/<插件名> -> <数据根>/config/<插件名>_config.json
          2. 插件装在 <数据根>/plugins/<插件名> -> <数据根>/config/<插件名>_config.json
          3. astrbot 自身暴露的数据根目录
        优先返回**已存在**的文件；都还没生成时选父目录存在的那个，
        保证监听器之后能发现它。
        """
        name = f"{PLUGIN_NAME}_config.json"
        candidates = [
            self.data_dir.parent.parent / "config" / name,
            Path(__file__).resolve().parent.parent.parent / "config" / name,
        ]
        try:
            from astrbot.api.star import get_astrbot_data_path

            candidates.append(Path(get_astrbot_data_path()) / "config" / name)
        except Exception:
            pass

        for candidate in candidates:
            if candidate.exists():
                return candidate
        for candidate in candidates:
            if candidate.parent.is_dir():
                return candidate
        return candidates[0]

    async def _on_config_changed(self, data: dict) -> None:
        """配置文件发生变化：热更新到各层，并处理"配置里填的验证码"。"""
        self._apply_config(data)
        self.logger.info("[GJM] 检测到配置变更，已热更新。")
        await self._consume_config_code()

    def _apply_config(self, data: dict) -> None:
        """把新配置应用到内存中的各层。"""
        self.cfg.reload(data)
        # 订阅层持有自己的权威列表，配置变了要同步过去
        self.subscriptions.reload(self.cfg.subscriptions)
        # 让网络层跟随配置变化（语言 / 超时 / 重试）
        self.client.language = self.cfg.language
        self.client.timeout = self.cfg.request_timeout
        self.client.retries = self.cfg.request_retries
        # 各层持有的是同一个 cfg / storage 对象引用，无需重新注入
        self.watcher.wake()

    def _save_config(self, mutate=None) -> None:
        """把改动写回 AstrBot 配置，并热重载本插件的强类型配置。"""
        if mutate is not None:
            mutate(self.raw_config)
        try:
            save = getattr(self.raw_config, "save_config", None)
            if callable(save):
                save()
        except Exception as exc:
            self.logger.warning(f"[GJM] 保存配置失败: {exc}")

        data = dict(self.raw_config)
        self._apply_config(data)
        # 告诉监听器"这份内容是我们自己写的"，避免下一轮自我触发
        self._config_watcher.mark_applied(data)

    def _persist_subscriptions(self, subs: list[Subscription]) -> None:
        """订阅列表落盘：写成 template_list 结构，WebUI 配置页里可直接查看/修改。"""
        # 同一个 UID 折叠成一条：物品写成链接列表，刷新间隔挂在用户上
        grouped: dict[str, dict] = {}
        for sub in subs:
            entry = grouped.setdefault(
                sub.uid,
                {
                    "__template_key": "user_entry",
                    "enabled": sub.enabled,
                    "uid": sub.uid,
                    "note": self.storage.get_user_name(sub.uid) or sub.display_name,
                    "interval": sub.interval,
                    "items": [],
                },
            )
            # 同组里以“任一启用即为启用”“间隔取最小值”为准
            entry["enabled"] = entry["enabled"] or sub.enabled
            entry["interval"] = min(int(entry["interval"] or sub.interval), int(sub.interval))
            url = f"https://trade.gaijin.net/market/{self.cfg.appid}/{sub.market_name}"
            if url not in entry["items"]:
                entry["items"].append(url)
        self._save_config(lambda raw: raw.__setitem__("user_subscriptions", list(grouped.values())))

    def save_refresh_token(self, token: str) -> None:
        self._save_config(lambda raw: raw.__setitem__("refresh_token", token))

    def save_twofa_code(self, code: str) -> None:
        """写回验证码字段（提交后清空，避免验证码长期留痕）。"""
        self._save_config(lambda raw: raw.__setitem__("twofa_code", code))

    def on_login_success(self) -> None:
        """登录成功后的统一收尾。"""
        self.logger.info("[GJM] 登录成功，令牌已更新。")
        self.watcher.wake()

    async def _consume_config_code(self) -> None:
        """如果配置界面里填了验证码且确实有等待中的登录，就自动提交。

        这是"在配置界面里填验证码"这条路径的落点：
        用户在插件配置表单里填好验证码 -> 保存 -> 配置热监听发现变化 -> 自动提交。
        """
        code = str(self.cfg.twofa_code or "").strip()
        if not code or code == self._consumed_code:
            return
        if not self.auth.has_pending_login:
            self.logger.info("[GJM] 配置里填了验证码，但当前没有等待中的登录流程，已忽略。")
            return

        self._consumed_code = code
        try:
            step = await self.auth.submit_code(code)
        except Exception as exc:
            self.logger.warning(f"[GJM] 自动提交配置界面里的验证码失败: {exc}")
        else:
            self.logger.info(f"[GJM] 配置界面验证码提交成功：{step.message}")
            self.on_login_success()
        finally:
            # 用完即清，避免验证码长期留在配置文件里
            self.save_twofa_code("")

    # ================================================================= 身份

    def _remember(self, event: AstrMessageEvent) -> None:
        """记录 UID -> 会话(UMO) 映射与用户昵称。

        这是"用 UID 配置推送对象 / @ 订阅者"的基础：
        AstrBot 发送消息只能按会话(UMO)寻址，必须先落到具体会话上。
        """
        uid = event.get_sender_id()
        umo = event.unified_msg_origin
        if not uid or not umo:
            return
        name = event.get_sender_name() or ""
        if self._seen_sessions.get(uid) == umo and not name:
            return
        self._seen_sessions[uid] = umo
        self.storage.remember_session(uid, umo, self._platform_of(event), name)

    @staticmethod
    def _platform_of(event: AstrMessageEvent) -> str:
        """从 UMO（平台:消息类型:会话ID）中取出平台标识。"""
        umo = event.unified_msg_origin or ""
        return umo.split(":", 1)[0] if ":" in umo else ""

    @staticmethod
    def _uid_candidates(event: AstrMessageEvent) -> set[str]:
        """列出该事件中能代表"人"的 UID 候选。

        1. 发送者 UID —— 首选，语义最明确；
        2. 会话 ID 的首段 —— 部分适配器把会话 ID 编成 <UID>_<群ID>；
           仅当它不等于群 ID 时才纳入，避免"配置了群 ID 等于全群都是管理员"。
        """
        candidates: set[str] = set()
        sender = event.get_sender_id()
        if sender:
            candidates.add(str(sender))
        parts = str(event.get_session_id() or "").split("_")
        group_id = str(event.get_group_id() or "")
        if parts and parts[0] and parts[0] != group_id:
            candidates.add(parts[0])
        return candidates

    def _owner_notify_umos(self) -> list[str]:
        """令牌失效等异常要主动通知谁：拥有者的会话。"""
        return self.notifier.owner_sessions() or self.storage.list_subscriptions()

    async def _platform_report(self) -> list[str]:
        """各平台的 @ 与主动推送能力（结论来自 core.platforms，供 /gjm debug 使用）。"""
        lines = ["【平台能力】"]
        cfg: dict = {}
        with contextlib.suppress(Exception):
            cfg = self.context.get_config() or {}
        items = (cfg.get("platform") or []) if isinstance(cfg, dict) else []
        for item in items:
            pid, ptype = str(item.get("id") or ""), str(item.get("type") or "")
            if not pid:
                continue
            declared: bool | None = None
            with contextlib.suppress(Exception):
                inst = self.context.get_platform_inst(pid)
                meta = inst.meta() if inst is not None else None
                if meta is not None:
                    declared = getattr(meta, "support_proactive_message", None)
            lines.append(platforms.describe(pid, ptype, declared))
        return lines

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """是否为插件拥有者。

        按 **UID** 判定（配置项 owner_uids）：同一真人在不同平台 / 不同会话下
        UID 稳定，比 UMO 更适合做身份标识。未配置时回退 AstrBot 管理员判定。
        """
        if self.cfg.owner_uids:
            return bool(self._uid_candidates(event) & set(self.cfg.owner_uids))
        return bool(event.is_admin())

    async def _notify_auth_failure(self, reason: str) -> None:
        """令牌失效时通知拥有者（限频 30 分钟）。"""
        if time.time() - self._auth_notice_at < 1800:
            return
        self._auth_notice_at = time.time()
        await self.notifier.push(
            f"Gaijin 令牌失效，监控已暂停。\n原因：{reason}\n请发送 /login 重新登录。",
            self._owner_notify_umos(),
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def _track_session(self, event: AstrMessageEvent) -> None:
        """被动学习 UID -> 会话 映射。

        优先级 -10：让其它 handler 先执行；本 handler 只维护映射表，
        既不产出消息、也不终止事件传播，因此对既有流程完全透明。
        """
        with contextlib.suppress(Exception):
            self._remember(event)

    # ================================================================= 指令

    @filter.command_group("gjm", alias={"/gjm"})
    def gjm(self):
        """Gaijin 市场行情监控指令组。"""

    @gjm.command("help")
    @gjm.command("help")
    async def gjm_help(self, event: AstrMessageEvent):
        """查看指令列表。"""
        self._remember(event)
        yield event.plain_result(
            "Gaijin 行情监控\n"
            "/login [验证码]        登录 / 提交两步验证码\n"
            "/gjm sub <链接>        订阅物品\n"
            "/gjm unsub <链接>      取消订阅\n"
            "/gjm my                我的订阅\n"
            "/gjm list              被监控的物品与最新行情\n"
            "/gjm price <链接>      查询单个物品行情\n"
            "/gjm shot [链接]       拍摄商品页快照\n"
            "/gjm id                查看本会话 UID / UMO\n"
            "/gjm status            运行与令牌状态\n"
            "/gjm now               立即刷新一轮\n"
            "/gjm interval <秒>     设置我的刷新间隔\n"
            "/gjm token <回跳链接>  导入 refresh_token\n"
            "/gjm debug             排障信息"
        )

    @gjm.command("id")
    @gjm.command("id")
    async def gjm_id(self, event: AstrMessageEvent):
        """查看本会话的 UID / UMO。"""
        self._remember(event)
        umo = event.unified_msg_origin or ""
        parts = umo.split(":", 2)
        lines = [
            "【本会话信息】",
            f"平台：{parts[0] if parts else ''}",
            f"UMO：{umo}",
            f"UID：{event.get_sender_id()}",
            f"昵称：{event.get_sender_name() or '未知'}",
            f"群 ID：{event.get_group_id() or '（私聊）'}",
            f"是否拥有者：{'是' if self._is_owner(event) else '否'}",
            f"我的订阅：{len(self.cfg.subscriptions_of(str(event.get_sender_id())))} 条",
            "",
            "配置「拥有者 UID」时填上面的 UID。",
        ]
        yield event.plain_result("\n".join(lines))

    @gjm.command("sub")
    async def gjm_sub(self, event: AstrMessageEvent):
        """订阅物品（只接受物品页链接）。"""
        self._remember(event)
        url = _args_after(event, "gjm", "sub")
        if not url:
            yield event.plain_result(f"用法：/gjm sub <物品链接>\n{REJECT_HINT}")
            return

        uid = str(event.get_sender_id() or "")
        ok, message = self.subscriptions.subscribe(uid, url)
        if ok:
            market_name = self.subscriptions.parse_url(url)
            self.storage.set_item_name(
                market_name, self.cfg.display_name_of(market_name) or prettify_market_name(market_name)
            )
            self.watcher.wake()
        yield event.plain_result(message)

    @gjm.command("unsub")
    async def gjm_unsub(self, event: AstrMessageEvent):
        """取消订阅物品（只接受物品页链接）。"""
        self._remember(event)
        url = _args_after(event, "gjm", "unsub")
        if not url:
            yield event.plain_result(f"用法：/gjm unsub <物品链接>\n{REJECT_HINT}")
            return
        ok, message = self.subscriptions.unsubscribe(str(event.get_sender_id() or ""), url)
        yield event.plain_result(message)

    @gjm.command("my")
    async def gjm_my(self, event: AstrMessageEvent):
        """查看我订阅的物品。"""
        self._remember(event)
        uid = str(event.get_sender_id() or "")
        subs = self.cfg.subscriptions_of(uid)
        if not subs:
            yield event.plain_result("你还没有订阅。用 /gjm sub <物品链接> 订阅。")
            return
        lines = [f"【我的订阅 · {len(subs)} 件】"]
        for index, sub in enumerate(subs, start=1):
            quote = self.storage.get_last_quote(sub.market_name)
            lines.append(f"{index}. {sub.display_name or sub.market_name}")
            lines.append(f"   {self.item_url(sub.market_name)}")
            if quote and quote.ok:
                lines.append(f"   售出 {quote.sell_min} / 求购 {quote.buy_max} GJN")
        yield event.plain_result("\n".join(lines))

    def item_url(self, market_name: str) -> str:
        return f"https://trade.gaijin.net/market/{self.cfg.appid}/{market_name}"

    @gjm.command("price")
    async def gjm_price(self, event: AstrMessageEvent):
        """查询单个物品的实时行情（只接受物品页链接）。"""
        self._remember(event)
        url = _args_after(event, "gjm", "price")
        if not url:
            yield event.plain_result(f"用法：/gjm price <物品链接>\n{REJECT_HINT}")
            return
        _, market_name = extract_reference(url)
        if not market_name:
            yield event.plain_result(REJECT_HINT)
            return

        try:
            token = await self.auth.get_token()
        except LoginRequiredError as exc:
            yield event.plain_result(f"令牌不可用：{exc}\n请发送 /login 登录。")
            return

        await event.send(event.plain_result("正在查询…"))
        try:
            quote = await self.client.fetch_quote(
                market_name,
                token,
                appid=self.cfg.appid,
                display_name=self.cfg.display_name_of(market_name) or self.storage.get_item_name(market_name),
            )
        except Exception as exc:
            yield event.plain_result(f"抓取行情失败：{exc}")
            return

        yield event.plain_result(
            f"【{quote.title}】\n"
            f"最低售价：{quote.sell_min} GJN\n"
            f"最高求购：{quote.buy_max} GJN\n"
            f"挂单量：售 {quote.sell_depth} / 求 {quote.buy_depth}\n"
            f"{self.item_url(market_name)}"
        )

    @gjm.command("shot")
    async def gjm_shot(self, event: AstrMessageEvent):
        """拍摄物品页快照（用于验证截图功能）。"""
        self._remember(event)
        arg = _args_after(event, "gjm", "shot")
        market_name = ""
        if arg:
            _, market_name = extract_reference(arg)
        if not market_name:
            subs = self.cfg.subscriptions_of(str(event.get_sender_id()))
            if subs:
                _, market_name = extract_reference(subs[0].item_url)
        if not market_name:
            yield event.plain_result(f"用法：/gjm shot <物品链接>\n{REJECT_HINT}")
            return
        await event.send(event.plain_result("正在打开商品页截图…（约 15 秒）"))
        try:
            path = await self.snapshots.capture(market_name)
        except Exception as exc:
            yield event.plain_result(f"截图失败：{exc}")
            return
        yield event.image_result(str(path))

    @gjm.command("status")
    @gjm.command("status")
    async def gjm_status(self, event: AstrMessageEvent):
        """查看运行与令牌状态。"""
        self._remember(event)
        stats = self.watcher.stats()
        auth = self.auth.status()
        lines = ["【运行状态】"]
        values = sorted(set((stats.get("intervals") or {}).values()))
        span = "—" if not values else (f"{values[0]} 秒" if len(values) == 1 else f"{values[0]}~{values[-1]} 秒")
        lines.append(f"监控：{'运行中' if stats['running'] else '已停止'}，间隔 {span}（按订阅独立）")
        lines.append(f"物品：{stats['watched']} 个（来自 {stats['subscriptions']} 条订阅）")
        lines.append(f"轮询：已完成 {stats['poll_count']} 轮")
        if stats["last_poll_at"]:
            lines.append("上次：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stats["last_poll_at"])))
        if stats["last_error"]:
            lines.append(f"错误：{stats['last_error']}")
        lines.append("")
        lines.append("【令牌】")
        if auth["has_jwt"] and auth["seconds_left"] > 0:
            nick = auth.get("nick") or "未知"
            lines.append(f"JWT：有效（{nick}，剩 {auth['seconds_left'] / 86400:.1f} 天）")
        elif auth["has_jwt"]:
            lines.append("JWT：已过期")
        else:
            lines.append("JWT：未配置")
        lines.append(f"refresh_token：{'有' if auth['has_refresh_token'] else '无'}")
        lines.append(f"账号密码：{'有' if auth['has_account'] else '无'}")
        if auth["pending_login"]:
            lines.append("登录：等待验证码（发 /login <验证码>）")
        if auth["last_error"]:
            lines.append(f"续期错误：{auth['last_error']}")
        subs = self.cfg.enabled_subscriptions
        uids = list(dict.fromkeys(s.uid for s in subs))
        lines.append("")
        lines.append(f"推送：{self.cfg.push_mode}，{len(subs)} 条订阅 / {len(uids)} 个 UID")
        for uid in uids:
            if not self.notifier.session_of(uid):
                lines.append(f"  ! {uid} 未解析到会话")
        yield event.plain_result("\n".join(lines))

    @gjm.command("list")
    async def gjm_list(self, event: AstrMessageEvent):
        """列出被订阅的物品与最近一次行情。"""
        self._remember(event)
        names = self.cfg.watched_market_names
        if not names:
            yield event.plain_result("没有被订阅的物品。用 /gjm sub <物品链接> 订阅。")
            return

        lines = ["【被监控的物品】"]
        for index, market_name in enumerate(names, start=1):
            quote = self.storage.get_last_quote(market_name)
            title = (
                self.storage.get_item_name(market_name)
                or self.cfg.display_name_of(market_name)
                or prettify_market_name(market_name)
            )
            subs = self.cfg.subscribers_of(market_name)
            lines.append(f"{index}. {title}（{len(subs)} 人订阅）")
            lines.append(f"   {self.item_url(market_name)}")
            if quote and quote.ok:
                lines.append(f"   售出 {quote.sell_min} / 求购 {quote.buy_max} GJN")
            else:
                lines.append("   暂无行情数据")

        lines.append("")
        lines.append("提示：物品由用户订阅产生，用 /gjm sub <物品链接> 添加。")
        yield event.plain_result("\n".join(lines))

    @gjm.command("now")
    async def gjm_now(self, event: AstrMessageEvent):
        """立即手动刷新一轮行情。"""
        self._remember(event)
        if not self._is_owner(event):
            yield event.plain_result("仅插件拥有者可以触发手动刷新。")
            return
        if not self.cfg.watched_market_names:
            yield event.plain_result("没有被监控的物品。")
            return
        yield event.plain_result("正在刷新行情，请稍候…")
        try:
            await self.watcher.poll_once()
        except Exception as exc:
            yield event.plain_result(f"刷新失败：{exc}")

    @gjm.command("interval")
    @gjm.command("interval")
    async def gjm_interval(self, event: AstrMessageEvent):
        """修改自己订阅的刷新间隔（秒）。"""
        self._remember(event)
        parts = _args_after(event, "gjm", "interval").split()
        if not parts:
            yield event.plain_result(
                f"用法：/gjm interval <秒> [物品链接]\n"
                f"（最小 {MIN_POLL_INTERVAL} 秒，建议 ≥ 10；不填链接则作用于你的全部订阅）"
            )
            return
        try:
            value = int(float(parts[0]))
        except ValueError:
            yield event.plain_result("秒数没看懂，请给整数，例如 /gjm interval 30")
            return
        market_name = ""
        if len(parts) > 1:
            _, market_name = extract_reference(" ".join(parts[1:]))
            if not market_name:
                yield event.plain_result(REJECT_HINT)
                return
        ok, message = self.subscriptions.set_interval(str(event.get_sender_id() or ""), value, market_name)
        yield event.plain_result(message)
        if ok:
            self.watcher.wake()

    @filter.command("login", alias={"/login", "登录"})
    async def cmd_login(self, event: AstrMessageEvent):
        """登录 Gaijin 账号：/login 发起，/login <验证码> 提交验证码。"""
        self._remember(event)
        # 验证码属敏感内容：终止事件传播，避免其进入 LLM 上下文或其它插件
        event.stop_event()

        if not self._is_owner(event):
            yield event.plain_result("仅拥有者可登录。请把你的 UID 填入配置的「拥有者 UID」。\n发送 /gjm id 可查看。")
            return

        code = _args_after(event, "login")
        if code:
            yield event.plain_result(await self._submit_login_code(code))
            return
        yield event.plain_result(await self._begin_login())

    async def _begin_login(self) -> str:
        """第一步：发起登录。优先用 refresh_token（免密码），其次账号密码。"""
        if self.auth.has_pending_login:
            return "已有登录在等待验证码。请发送 /login <验证码>"

        if self.auth.refresh_token and await self.auth.try_refresh():
            self.watcher.wake()
            return "refresh_token 仍有效，已换新 JWT，无需登录。"

        if not (self.cfg.account_email and self.cfg.account_password):
            return "未配置账号邮箱 / 密码。请先在插件配置里填写后重试。"

        try:
            step = await self.auth.begin_login(self.cfg.account_email, self.cfg.account_password)
        except LoginError as exc:
            return f"登录失败：{exc}"
        except Exception as exc:
            self.logger.error(f"[GJM] 登录第一阶段异常: {exc}")
            return f"登录失败：{exc}"

        if not step.need_code:
            self.on_login_success()
            return "登录成功。"

        return "站点要求验证码。请发送 /login <验证码>（15 分钟内有效）"

    async def _submit_login_code(self, code: str) -> str:
        """第二步：提交验证码。"""
        if not self.auth.has_pending_login:
            return "没有等待验证码的登录流程（可能已超时）。请先发送 /login。"
        try:
            step = await self.auth.submit_code(code)
        except LoginError as exc:
            return f"验证码提交失败：{exc}"
        except Exception as exc:
            self.logger.error(f"[GJM] 登录第二阶段异常: {exc}")
            return f"验证码提交失败：{exc}"
        self.on_login_success()
        return f"验证码校验通过，{step.message}"

    # ================================================================= 令牌应急

    @gjm.command("token")
    async def gjm_token(self, event: AstrMessageEvent):
        """手动导入 refresh_token（正常用 /login 登录）。"""
        self._remember(event)
        if not self._is_owner(event):
            yield event.plain_result("仅插件拥有者可以设置令牌。")
            return
        arg = _args_after(event, "gjm", "token")
        if not arg:
            yield event.plain_result("用法：/gjm token <含 refresh_token 的回跳链接>\n正常流程请到插件配置里完成登录。")
            return

        token = extract_refresh_token(arg)
        if token:
            self.auth.absorb(refresh_token=token)
            self.save_refresh_token(token)
        elif arg.startswith("eyJ") and arg.count(".") == 2:
            self.auth.absorb(jwt=arg)
            self._save_config(lambda raw: raw.__setitem__("jwt_token", arg))
        else:
            yield event.plain_result("无法识别输入，请提供含 refresh_token 的回跳链接或 JWT。")
            return

        await event.send(event.plain_result("已保存，正在校验…"))
        try:
            await self.auth.try_refresh()
            account = await self.auth.verify()
            self.watcher.wake()
            yield event.plain_result(
                f"令牌有效，账号 {account.get('nick', '未知')}（uid {account.get('userId', '?')}）。"
            )
        except Exception as exc:
            yield event.plain_result(f"令牌校验失败：{exc}")

    @gjm.command("debug")
    async def gjm_debug(self, event: AstrMessageEvent):
        """排障：显示登录流程、配置监听与 @ 适配状态。"""
        self._remember(event)
        if not self._is_owner(event):
            yield event.plain_result("仅插件拥有者可用。")
            return
        auth = self.auth.status()
        pending = self.auth.get_pending_login()
        umo = event.unified_msg_origin or ""
        platform_type = await self.notifier._platform_type(umo)
        plan = platforms.mention_plan(platform_type, str(event.get_sender_id()), event.get_sender_name() or "")
        mention_note = {
            platforms.STYLE_MARKDOWN_TEXT: f"文本内嵌标记（markdown 通道）：{plan.text}",
            platforms.STYLE_PLAIN_TEXT: f"纯文本 @（该平台不认 At 组件）：{plan.text}",
            platforms.STYLE_AT_COMPONENT: "标准 At 组件",
        }[plan.style]
        yield event.plain_result(
            "【排障信息】\n"
            f"配置文件：{self._config_path}\n"
            f"配置文件存在：{self._config_path.exists()}\n"
            f"配置热监听运行：{self._config_watcher.running}\n"
            f"等待验证码：{'是' if pending else '否'}"
            + (f"（字段 {pending.code_field}，已等待 {int((auth['pending_age'] or 0) / 60)} 分钟）" if pending else "")
            + f"\n配置里的验证码：{'已填' if self.cfg.twofa_code else '空'}\n"
            f"账号邮箱：{self.cfg.account_email or '未填'}\n"
            f"账号密码：{'已填' if self.cfg.account_password else '未填'}\n"
            f"JWT 账号：{describe_jwt(self.auth.jwt).get('nick') or '无'}\n"
            "\n【@ 适配】\n"
            f"本会话平台类型：{platform_type or '（未匹配到）'}\n"
            f"mention_users：{self.cfg.mention_users}\n"
            f"将采用：{mention_note}\n"
            f"拥有者 UID / 已识别会话：{self.cfg.owner_uids or '（未配置）'} / "
            f"{len(self.notifier.owner_sessions())}" + "\n\n" + "\n".join(await self._platform_report())
        )
