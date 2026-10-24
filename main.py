"""AstrBot 插件：Gaijin 市场行情监控与控制台。

核心能力：
  * 按订阅间隔轮询 trade.gaijin.net 上一件或多件物品的
    「实时最低售价 / 实时最高求购价 / 挂单量」；
  * 插件视图统一管理账户登录、拥有者 UID、行情订阅和双输出模板；
  * 聊天登录与 /gjm 订阅命令保留为备用入口；
  * 订阅按 UID + 会话区分，群聊推送时会 @ 到订阅者本人；
  * 订阅物品只接受物品页链接。

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
import copy
import re
import shutil
import time
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # 兼容尚未提供插件 Web API 的旧版 AstrBot；聊天功能仍可使用
    error_response = json_response = request = None  # type: ignore[assignment]

try:  # 常规：AstrBot 以包的形式加载插件
    from .core import platforms
    from .core.account_store import AccountStore
    from .core.api_client import AuthExpiredError, GaijinMarketClient
    from .core.auth import AuthManager, LoginError, LoginRequiredError, describe_jwt
    from .core.config import PluginConfig
    from .core.config_watch import ConfigFileWatcher
    from .core.constants import MIN_POLL_INTERVAL, ONLY_CHANGE_INTERVAL, REFRESH_ONLY_CHANGE
    from .core.models import Subscription
    from .core.notifier import Notifier
    from .core.output_store import DEFAULT_QUOTE_PAYLOAD, DEFAULT_TRADE_PAYLOAD, OutputStore
    from .core.resolver import extract_link_reference, prettify_market_name
    from .core.storage import PluginStorage
    from .core.subscriptions import REJECT_HINT, SubscriptionStore, parse_legacy_subscriptions
    from .core.trader import AutoTrader, TradeError
    from .core.watcher import MarketWatcher
except ImportError:  # 兜底：以文件路径方式加载时
    import sys

    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.append(str(Path(__file__).resolve().parent))

    from core import platforms
    from core.account_store import AccountStore
    from core.api_client import AuthExpiredError, GaijinMarketClient
    from core.auth import AuthManager, LoginError, LoginRequiredError, describe_jwt
    from core.config import PluginConfig
    from core.config_watch import ConfigFileWatcher
    from core.constants import MIN_POLL_INTERVAL, ONLY_CHANGE_INTERVAL, REFRESH_ONLY_CHANGE
    from core.models import Subscription
    from core.notifier import Notifier
    from core.output_store import DEFAULT_QUOTE_PAYLOAD, DEFAULT_TRADE_PAYLOAD, OutputStore
    from core.resolver import extract_link_reference, prettify_market_name
    from core.storage import PluginStorage
    from core.subscriptions import REJECT_HINT, SubscriptionStore, parse_legacy_subscriptions
    from core.trader import AutoTrader, TradeError
    from core.watcher import MarketWatcher

PLUGIN_NAME = "astrbot_plugin_gaijin_market_watcher"
FORMER_AUTOTRADER_PLUGIN_NAME = "astrbot_plugin_gaijin_market_autotrader"
FORMER_MIGRATION_MARKER = "former_autotrader_migration.json"
FORMER_TRADE_FILES = (
    "trade_rules.json",
    "trade_runtime.json",
    "trade_settings.json",
    "trade_ledger.json",
    "trade_events.json",
    "trade_logs_meta.json",
)
LEGACY_UI_KEYS = (
    "account_email",
    "account_password",
    "twofa_code",
    "owner_uids",
    "user_subscriptions",
    "payload",
)


def _can_cleanup_legacy_config(
    *,
    legacy_values_present: bool,
    migration_ok: bool,
    data_files_ready: bool,
) -> bool:
    """只有所有权威数据已落盘时，才允许删除兼容配置。"""
    return bool(legacy_values_present and migration_ok and data_files_ready)


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
        "Gaijin 市场行情监控插件升级版，使用 /gjm 指令命名空间；"
        "支持行情订阅、账户登录、双输出模板，以及默认模拟的自动交易。"
    ),
    "2.20.2",
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
        self._migrate_former_autotrader_data()

        # ---------- 账户 / 输出 / 订阅：插件视图的权威数据源 ----------
        migrating = self.storage.get_ui_schema_version() < 1
        self.accounts = AccountStore(self.storage, self.logger)
        self.output = OutputStore(self.storage, self.logger)
        if migrating:
            account_ok = self.accounts.migrate_legacy(dict(self.raw_config))
            output_ok = self.output.migrate_legacy(dict(self.raw_config))
        else:
            account_ok = output_ok = True

        subscription_raw = self.storage.load_aux_json("subscriptions.json", None)
        subscription_migrated = isinstance(subscription_raw, list)
        if subscription_migrated:
            subscription_items = [Subscription.from_dict(item) for item in subscription_raw if isinstance(item, dict)]
        else:
            subscription_items = parse_legacy_subscriptions(
                self.raw_config.get("user_subscriptions"),
                default_interval=self.cfg.default_interval,
                default_mode=self.cfg.default_refresh_mode,
            )
            subscription_migrated = self.storage.save_aux_json(
                "subscriptions.json", [item.to_dict() for item in subscription_items]
            )

        legacy_values_present = any(bool(self.raw_config.get(key)) for key in LEGACY_UI_KEYS)
        migration_ok = account_ok and output_ok and subscription_migrated
        data_files_ready = bool(self.accounts.persisted and self.output.persisted and subscription_migrated)
        self._legacy_config_pending = _can_cleanup_legacy_config(
            legacy_values_present=legacy_values_present,
            migration_ok=migration_ok,
            data_files_ready=data_files_ready,
        )
        if migrating and account_ok and output_ok and subscription_migrated:
            self.storage.set_ui_schema_version(1)
        elif legacy_values_present:
            self.logger.warning("[GJM] 旧配置迁移未完成，保留原配置并将在下次启动重试。")

        target_migration_pending = any(not sub.target_umo for sub in subscription_items)
        self.subscriptions = SubscriptionStore(
            self._persist_subscriptions_data,
            self.logger,
            resolve_target=self.storage.resolve_uid,
            items=subscription_items,
            default_interval=self.cfg.default_interval,
            default_mode=self.cfg.default_refresh_mode,
        )
        self._target_migration_pending = target_migration_pending
        if target_migration_pending:
            self._persist_subscriptions_data(self.subscriptions.all())

        # ---------- 各层装配 ----------
        self.client = GaijinMarketClient(
            language=self.cfg.language,
            timeout=self.cfg.request_timeout,
            retries=self.cfg.request_retries,
            min_interval=min(self.cfg.item_interval or 0.4, 1.0),
            logger=self.logger,
        )
        self.auth = AuthManager(self.client, self.storage, self.cfg, self.logger, accounts=self.accounts)
        self.auth.bootstrap()
        self.notifier = Notifier(
            self.context,
            self.storage,
            self.cfg,
            self.logger,
            subscriptions=self.subscriptions,
            output=self.output,
            accounts=self.accounts,
        )
        self.trader = AutoTrader(
            client=self.client,
            auth=self.auth,
            storage=self.storage,
            config=self.cfg,
            accounts=self.accounts,
            notifier=self.notifier,
            logger=self.logger,
            on_watch_change=self._trade_watch_changed,
        )
        self.watcher = MarketWatcher(
            client=self.client,
            auth=self.auth,
            storage=self.storage,
            config=self.cfg,
            subscriptions=self.subscriptions,
            notifier=self.notifier,
            logger=self.logger,
            on_auth_failure=self._notify_auth_failure,
            extra_market_names=self.trader.watch_market_names,
            extra_interval=self.trader.poll_interval,
            on_quotes=self.trader.on_quotes,
        )
        self._register_web_apis()

        # ---------- 配置热监听 ----------
        self._config_path = self._resolve_config_path()
        self._config_watcher = ConfigFileWatcher(self._config_path, self._on_config_changed, self.logger)

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

        for hint in self.cfg.validate(
            subscriptions=self.subscriptions,
            owner_uids=self.accounts.owner_uids,
            output=self.output,
            auth=self.auth,
        ):
            self.logger.info(f"[GJM] {hint}")

        if self._target_migration_pending:
            self.logger.info("[GJM] 已把缺少会话目标的订阅补齐到各自最近会话。")

        self._cleanup_legacy_config()
        self._config_watcher.start()
        self.trader.start()
        self.watcher.start()

    async def terminate(self) -> None:
        """插件停用 / 卸载时调用：务必释放后台任务与连接。"""
        with contextlib.suppress(Exception):
            await self.notifier.shutdown()
        with contextlib.suppress(Exception):
            await self.watcher.stop()
        with contextlib.suppress(Exception):
            await self.trader.stop()
        with contextlib.suppress(Exception):
            await self._config_watcher.stop()
        with contextlib.suppress(Exception):
            await self.client.close()

    # ================================================================= 配置

    def _migrate_former_autotrader_data(self) -> None:
        """把旧独立自动交易插件的交易数据合并到当前老插件数据目录。"""
        marker = self.storage.load_aux_json(FORMER_MIGRATION_MARKER, None)
        if isinstance(marker, dict):
            return
        source = self.data_dir.parent / FORMER_AUTOTRADER_PLUGIN_NAME
        if not source.is_dir():
            return
        copied: list[str] = []
        failed = False
        for name in FORMER_TRADE_FILES:
            source_file = source / name
            target_file = self.data_dir / name
            if not source_file.is_file() or target_file.exists():
                continue
            try:
                shutil.copy2(source_file, target_file)
                copied.append(name)
            except OSError as exc:
                failed = True
                self.logger.warning(f"[GJM] 迁移旧自动交易文件 {name} 失败: {exc}")

        source_logs = source / "logs"
        target_logs = self.data_dir / "logs"
        if source_logs.is_dir():
            try:
                target_logs.mkdir(parents=True, exist_ok=True)
                for source_file in source_logs.glob("trade-*.jsonl"):
                    target_file = target_logs / source_file.name
                    if target_file.exists():
                        continue
                    shutil.copy2(source_file, target_file)
                    copied.append(f"logs/{source_file.name}")
            except OSError as exc:
                failed = True
                self.logger.warning(f"[GJM] 迁移旧自动交易日志失败: {exc}")

        if failed:
            self.logger.warning("[GJM] 旧自动交易插件数据迁移未完成，下次启动会重试。")
            return
        saved = self.storage.save_aux_json(
            FORMER_MIGRATION_MARKER,
            {
                "source_plugin": FORMER_AUTOTRADER_PLUGIN_NAME,
                "copied": copied,
                "migrated_at": time.time(),
            },
        )
        if saved and copied:
            self.logger.info(f"[GJM] 已合并旧自动交易插件数据：{', '.join(copied)}")

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
        """配置文件发生变化：热更新技术参数。"""
        self._apply_config(data)
        self.logger.info("[GJM] 检测到配置变更，已热更新。")

    def _apply_config(self, data: dict) -> None:
        """把新配置应用到内存中的技术层。"""
        self.cfg.reload(data)
        self.client.language = self.cfg.language
        self.client.timeout = self.cfg.request_timeout
        self.client.retries = self.cfg.request_retries
        if self.auth.sync_config_tokens():
            self.watcher.reset_auth_backoff()
        self.watcher.reconfigure()

    def _save_config(self, mutate=None) -> bool:
        """把改动写回 AstrBot 配置，成功后热重载技术配置。"""
        save = getattr(self.raw_config, "save_config", None)
        if not callable(save):
            self.logger.error("[GJM] 当前配置对象不支持 save_config，改动未保存。")
            raise RuntimeError("当前配置对象不支持保存")

        snapshot = copy.deepcopy(dict(self.raw_config))
        if mutate is not None:
            mutate(self.raw_config)
        try:
            save()
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.raw_config.clear()
                self.raw_config.update(snapshot)
            self.logger.warning(f"[GJM] 保存配置失败: {exc}")
            raise

        data = dict(self.raw_config)
        self._apply_config(data)
        self._config_watcher.mark_applied(data)
        return True

    def _cleanup_legacy_config(self) -> None:
        """迁移完成后从原生配置中移除敏感项和已迁出的界面字段。"""
        if not getattr(self, "_legacy_config_pending", False):
            return
        legacy_keys = LEGACY_UI_KEYS
        if not any(key in self.raw_config for key in legacy_keys):
            self._legacy_config_pending = False
            return
        try:
            self._save_config(lambda raw: [raw.pop(key, None) for key in legacy_keys])
        except Exception as exc:
            self.logger.warning(f"[GJM] 清理旧配置字段失败，下次启动将继续尝试: {exc}")
            return
        self._legacy_config_pending = False

    def _persist_subscriptions_data(self, subs: list[Subscription]) -> bool:
        """订阅数据原子写入插件数据目录。"""
        return self.storage.save_aux_json("subscriptions.json", [sub.to_dict() for sub in subs])

    def on_login_success(self) -> None:
        """登录成功后的统一收尾。"""
        self.logger.info("[GJM] 登录成功，令牌已更新。")
        self.watcher.reset_auth_backoff()
        self.watcher.wake()

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
        if self.storage.remember_session(uid, umo, self._platform_of(event), name) is False:
            self.logger.warning(f"[GJM] 会话映射写入失败：{uid}")
        if self.subscriptions.bind_legacy_target(uid, umo):
            self.watcher.reconfigure()

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
        owners = self.notifier.owner_sessions()
        if owners:
            return owners
        # 未配置拥有者 UID 时，保留向已知订阅会话兜底通知的行为。
        fallback: list[str] = []
        for sub in self.subscriptions.all():
            umo = self.notifier.session_of(sub.uid)
            if umo and umo not in fallback:
                fallback.append(umo)
        return fallback

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

    # ================================================================= 交易 WebUI

    def _register_web_apis(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            self.logger.warning("[GJM] 当前 AstrBot 不支持插件 Web API，控制台不可用。")
            return
        routes = [
            ("account/overview", self._account_web_overview, ["GET"]),
            ("account/save", self._account_web_save, ["POST"]),
            ("account/login/start", self._account_web_login_start, ["POST"]),
            ("account/login/code", self._account_web_login_code, ["POST"]),
            ("monitor/overview", self._monitor_web_overview, ["GET"]),
            ("monitor/subscriptions/save", self._monitor_web_save, ["POST"]),
            ("monitor/subscriptions/delete", self._monitor_web_delete, ["POST"]),
            ("output/overview", self._output_web_overview, ["GET"]),
            ("output/save", self._output_web_save, ["POST"]),
            ("output/preview", self._output_web_preview, ["POST"]),
            ("trade/overview", self._trade_web_overview, ["GET"]),
            ("trade/settings", self._trade_web_settings, ["POST"]),
            ("trade/rules/save", self._trade_web_save_rule, ["POST"]),
            ("trade/rules/action", self._trade_web_action, ["POST"]),
            ("trade/rules/drill", self._trade_web_drill, ["POST"]),
            ("trade/stop-all", self._trade_web_stop_all, ["POST"]),
            ("trade/logs", self._trade_web_logs, ["GET"]),
        ]
        for path, handler, methods in routes:
            register_api(
                f"/{PLUGIN_NAME}/{path}",
                handler,
                methods,
                f"Gaijin market console: {path}",
            )

    def _account_overview_data(self) -> dict:
        auth = self.auth.status()
        return {
            "account": self.accounts.sanitized(),
            "auth": {
                "has_jwt": bool(auth.get("has_jwt")),
                "has_refresh_token": bool(auth.get("has_refresh_token")),
                "has_account": bool(auth.get("has_account")),
                "nick": auth.get("nick") or "",
                "uid": auth.get("uid") or "",
                "seconds_left": int(auth.get("seconds_left") or 0),
                "last_error": auth.get("last_error") or "",
                "pending_login": bool(auth.get("pending_login")),
                "pending_age": int(auth.get("pending_age") or 0),
            },
        }

    async def _account_web_overview(self):
        return json_response(self._account_overview_data())

    async def _account_web_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        email = payload.get("email", self.accounts.email)
        password = payload.get("password")
        owner_uids = payload.get("owner_uids", self.accounts.owner_uids)
        if not isinstance(email, str):
            return error_response("账号邮箱格式不正确", status_code=400)
        if password is not None and not isinstance(password, str):
            return error_response("账号密码格式不正确", status_code=400)
        if not isinstance(owner_uids, list | tuple):
            return error_response("owner_uids 必须是数组", status_code=400)
        try:
            saved = self.accounts.save(email=email, password=password, owner_uids=owner_uids)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        if not saved:
            return error_response("保存账号设置失败，请检查插件数据目录权限", status_code=500)
        self.logger.info(
            f"[GJM] WebUI 账号设置修改者: {self._trade_web_actor()}；拥有者 UID 数量: {len(self.accounts.owner_uids)}"
        )
        return json_response(self._account_overview_data())

    async def _account_web_login_start(self):
        try:
            message = await self._begin_login()
        except Exception as exc:
            self.logger.warning(f"[GJM] WebUI 发起登录失败: {exc}")
            return error_response(str(exc), status_code=400)
        self.logger.info(f"[GJM] WebUI 发起登录: {self._trade_web_actor()}")
        data = self._account_overview_data()
        data.update({"message": message, "need_code": self.auth.has_pending_login})
        return json_response(data)

    async def _account_web_login_code(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        code = str(payload.get("code") or "").strip()
        if not code:
            return error_response("验证码不能为空", status_code=400)
        try:
            message = await self._submit_login_code(code)
        except Exception as exc:
            self.logger.warning("[GJM] WebUI 提交验证码失败")
            return error_response(str(exc), status_code=400)
        self.logger.info(f"[GJM] WebUI 提交登录验证码: {self._trade_web_actor()}")
        data = self._account_overview_data()
        data.update({"message": message, "need_code": self.auth.has_pending_login})
        return json_response(data)

    def _monitor_overview_data(self) -> dict:
        grouped: dict[tuple[str, str], dict] = {}
        for sub in self.subscriptions.all():
            key = (sub.target_umo, sub.uid)
            group = grouped.setdefault(
                key,
                {
                    "uid": sub.uid,
                    "target_umo": sub.target_umo,
                    "note": self.storage.get_user_name(sub.uid) or "",
                    "enabled": False,
                    "interval": sub.interval,
                    "mode": sub.mode,
                    "items": [],
                },
            )
            group["enabled"] = bool(group["enabled"] or sub.enabled)
            group["interval"] = min(int(group["interval"]), int(sub.interval))
            if sub.mode == "only_change":
                group["mode"] = "only_change"
            group["items"].append(
                {
                    "market_name": sub.market_name,
                    "display_name": sub.display_name,
                    "enabled": bool(sub.enabled),
                    "interval": int(sub.interval),
                    "mode": sub.mode,
                    "url": f"https://trade.gaijin.net/market/{self.cfg.appid}/{sub.market_name}",
                }
            )
        sessions_raw = self.storage.load_aux_json("sessions.json", {})
        sessions: list[dict] = []
        if isinstance(sessions_raw, dict):
            for uid, item in sessions_raw.items():
                if not isinstance(item, dict) or not item.get("umo"):
                    continue
                sessions.append(
                    {
                        "uid": str(uid),
                        "umo": str(item.get("umo") or ""),
                        "platform": str(item.get("platform") or ""),
                        "name": str(item.get("name") or ""),
                        "updated_at": float(item.get("updated_at") or 0),
                    }
                )
        sessions.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
        return {
            "groups": list(grouped.values()),
            "sessions": sessions,
            "watched_market_names": self.subscriptions.watched_market_names,
            "defaults": {
                "interval": self.cfg.default_interval,
                "mode": self.cfg.default_refresh_mode,
                "url_example": "https://trade.gaijin.net/market/1067/id50381_f_16xl_usa",
            },
        }

    async def _monitor_web_overview(self):
        return json_response(self._monitor_overview_data())

    async def _monitor_web_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            ok, message = self.subscriptions.replace_group(
                original_uid=str(payload.get("original_uid") or ""),
                original_target=str(payload.get("original_target") or ""),
                uid=str(payload.get("uid") or ""),
                target_umo=str(payload.get("target_umo") or ""),
                enabled=bool(payload.get("enabled", True)),
                interval=int(payload.get("interval") or self.cfg.default_interval),
                mode=str(payload.get("mode") or self.cfg.default_refresh_mode),
                items=list(payload.get("items") or []),
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        if not ok:
            return error_response(message, status_code=400)
        self.watcher.reconfigure()
        self.logger.info(f"[GJM] WebUI 保存订阅: {self._trade_web_actor()}")
        data = self._monitor_overview_data()
        data["message"] = message
        return json_response(data)

    async def _monitor_web_delete(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ok, message = self.subscriptions.delete_group(
            str(payload.get("uid") or ""),
            str(payload.get("target_umo") or ""),
            str(payload.get("market_name") or ""),
        )
        if not ok:
            return error_response(message, status_code=400)
        self.watcher.reconfigure()
        self.logger.info(f"[GJM] WebUI 删除订阅: {self._trade_web_actor()}")
        data = self._monitor_overview_data()
        data["message"] = message
        return json_response(data)

    def _output_overview_data(self) -> dict:
        return {
            "quote_payload": self.output.quote_payload,
            "trade_payload": self.output.trade_payload,
            "defaults": {
                "quote_payload": DEFAULT_QUOTE_PAYLOAD,
                "trade_payload": DEFAULT_TRADE_PAYLOAD,
            },
            "fields": self.output.fields(),
        }

    async def _output_web_overview(self):
        return json_response(self._output_overview_data())

    async def _output_web_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        quote_template = payload.get("quote_payload", self.output.quote_payload)
        trade_template = payload.get("trade_payload", self.output.trade_payload)
        if not isinstance(quote_template, str) or not isinstance(trade_template, str):
            return error_response("输出模板必须是字符串", status_code=400)
        try:
            if not self.output.save(quote_payload=quote_template, trade_payload=trade_template):
                return error_response("保存输出模板失败，请检查插件数据目录权限", status_code=500)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        self.logger.info(f"[GJM] WebUI 保存输出模板: {self._trade_web_actor()}")
        data = self._output_overview_data()
        data["message"] = "输出模板已保存"
        return json_response(data)

    async def _output_web_preview(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        kind = str(payload.get("kind") or "quote")
        template = payload.get("template")
        if not isinstance(template, str):
            return error_response("template 必须是字符串", status_code=400)
        try:
            return json_response(self.output.preview(template, kind))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    @staticmethod
    def _trade_web_actor() -> str:
        return str(getattr(request, "username", "") or "dashboard")

    async def _trade_web_overview(self):
        data = self.trader.overview()
        auth = self.auth.status()
        data["auth"] = {
            "has_jwt": bool(auth.get("has_jwt")),
            "nick": auth.get("nick") or "",
            "seconds_left": int(auth.get("seconds_left") or 0),
        }
        return json_response(data)

    async def _trade_web_logs(self):
        try:
            limit = max(1, min(500, int(request.query.get("limit", 100))))
            after = max(0, int(request.query.get("after", 0)))
        except (TypeError, ValueError):
            return error_response("limit 和 after 必须是整数", status_code=400)
        events = self.trader.store.events_after(after, limit)
        next_cursor = int(events[-1].event_id) if events else after
        return json_response(
            {
                "events": [event.to_dict() for event in events],
                "next_cursor": next_cursor,
            }
        )

    async def _trade_web_settings(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            await self.trader.update_settings(payload)
        except (TradeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            self.logger.error(f"[GJM Trade] WebUI 保存设置失败: {exc}")
            return error_response("保存交易设置失败", status_code=500)
        self.logger.info(f"[GJM Trade] WebUI 设置修改者: {self._trade_web_actor()}")
        return json_response(self.trader.overview())

    async def _trade_web_save_rule(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            rule_id = str(payload.get("rule_id") or "").strip()
            if rule_id:
                changes = {
                    key: payload[key]
                    for key in ("buy_price", "quantity", "sell_net_price", "mode", "display_name")
                    if key in payload
                }
                rule = await self.trader.update_rule(rule_id, **changes)
            else:
                rule = await self.trader.create_rule(
                    url=str(payload.get("url") or ""),
                    buy_price=payload.get("buy_price"),
                    quantity=payload.get("quantity"),
                    sell_net_price=payload.get("sell_net_price"),
                    mode=str(payload.get("mode") or ""),
                    display_name=str(payload.get("display_name") or ""),
                )
        except (TradeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except (AuthExpiredError, LoginRequiredError) as exc:
            return error_response(f"登录已失效：{exc}", status_code=401)
        except Exception as exc:
            self.logger.error(f"[GJM Trade] WebUI 保存规则失败: {exc}")
            return error_response("保存交易规则失败", status_code=500)
        self.logger.info(f"[GJM Trade] WebUI 规则修改者: {self._trade_web_actor()}")
        return json_response({"rule": self.trader.rule_view(rule)})

    async def _trade_web_action(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        rule_id = str(payload.get("rule_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        try:
            if action == "prepare_arm":
                message = await self.trader.prepare_arm(rule_id)
                return json_response({"prepared": True, "rule_id": rule_id, "message": message})
            rule = await self.trader.action(rule_id, action, str(payload.get("confirmation_id") or ""))
        except (TradeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except (AuthExpiredError, LoginRequiredError) as exc:
            return error_response(f"登录已失效：{exc}", status_code=401)
        except Exception as exc:
            self.logger.error(f"[GJM Trade] WebUI 操作失败: {exc}")
            return error_response("执行交易操作失败", status_code=500)
        self.logger.info(f"[GJM Trade] WebUI 操作 {action} / {rule_id} / {self._trade_web_actor()}")
        return json_response({"rule": self.trader.rule_view(rule)})

    async def _trade_web_drill(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        rule_id = str(payload.get("rule_id") or "").strip()
        try:
            result = await self.trader.drill_paper_cycle(rule_id)
        except (TradeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except (AuthExpiredError, LoginRequiredError) as exc:
            return error_response(f"登录已失效：{exc}", status_code=401)
        except Exception as exc:
            self.logger.error(f"[GJM Trade] 模拟演练失败: {exc}")
            return error_response("模拟演练失败", status_code=500)
        self.logger.info(f"[GJM Trade] WebUI 模拟演练 {rule_id} / {self._trade_web_actor()}")
        return json_response(result)

    async def _trade_web_stop_all(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or str(payload.get("confirmation") or "").strip().lower() != "stop-all":
            return error_response("请输入 stop-all 确认急停", status_code=400)
        try:
            result = await self.trader.stop_all()
        except Exception as exc:
            self.logger.error(f"[GJM Trade] WebUI 急停失败: {exc}")
            return error_response("执行急停失败", status_code=500)
        self.logger.warning(f"[GJM Trade] WebUI 急停操作者: {self._trade_web_actor()}")
        return json_response(result)

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """是否为插件拥有者。

        按 **UID** 判定（配置项 owner_uids）：同一真人在不同平台 / 不同会话下
        UID 稳定，比 UMO 更适合做身份标识。未配置时回退 AstrBot 管理员判定。
        """
        owner_uids = self.accounts.owner_uids
        if owner_uids:
            return bool(self._uid_candidates(event) & set(owner_uids))
        return bool(event.is_admin())

    def _trade_access_error(self, event: AstrMessageEvent) -> str:
        """交易指令严格要求：显式拥有者 UID + 私聊会话。"""
        if not self.accounts.owner_uids:
            return "自动交易已禁用：请先在插件控制台的账户页填写拥有者 UID，不能回退到 AstrBot 管理员判定。"
        if platforms.session_type_of(event.unified_msg_origin or "") != platforms.SESSION_TYPE_FRIEND:
            return "自动交易指令仅允许在拥有者私聊中使用。"
        if not self._is_owner(event):
            return "仅插件拥有者可以操作自动交易。"
        return ""

    def _trade_watch_changed(self) -> None:
        watcher = getattr(self, "watcher", None)
        if watcher is not None:
            watcher.reconfigure()

    async def _notify_auth_failure(self, reason: str) -> None:
        """令牌失效时通知拥有者（限频 30 分钟）。"""
        if time.time() - self._auth_notice_at < 1800:
            return
        self._auth_notice_at = time.time()
        await self.notifier.push(
            f"Gaijin 令牌失效，监控已暂停。\n原因：{reason}\n请发送 /gjm login 或在插件控制台账户页重新登录。",
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

    @filter.command_group("gjm", alias={"/gjm", "gjma", "/gjma"})
    def gjm(self):
        """Gaijin 市场行情监控与自动交易指令组。"""

    @gjm.command("help")
    async def gjm_help(self, event: AstrMessageEvent):
        """查看指令列表。"""
        self._remember(event)
        yield event.plain_result(
            "Gaijin 行情监控\n"
            "/gjm login [验证码]        登录 / 提交两步验证码\n"
            "/gjm sub <链接>        订阅物品\n"
            "/gjm unsub <链接|编号> 取消订阅\n"
            "/gjm mysub             先刷新行情，再显示我的订阅 + 全局列表\n"
            "/gjm price <链接>      查询单个物品行情\n"
            "/gjm status            运行与令牌状态\n"
            "/gjm refresh mode <模式>  always / only_change\n"
            "/gjm refresh interval <秒> [链接|编号]\n"
            "群聊会自动按固定间隔发送全部订阅物品和价格图\n"
            "/gjm trade             自动交易（仅拥有者私聊）\n"
            "/gjm debug             排障信息"
        )

    @gjm.command("sub")
    async def gjm_sub(self, event: AstrMessageEvent):
        """订阅物品（只接受物品页链接）。"""
        self._remember(event)
        url = _args_after(event, "gjm", "sub")
        if not url:
            yield event.plain_result(f"用法：/gjm sub <物品链接>\n{REJECT_HINT}")
            return

        uid = str(event.get_sender_id() or "")
        target_umo = event.unified_msg_origin or ""
        ok, message = self.subscriptions.subscribe(uid, url, target_umo)
        if ok:
            market_name = self.subscriptions.parse_url(url)
            display_name = self.subscriptions.display_name_of(market_name) or prettify_market_name(market_name)
            if self.storage.set_item_name(market_name, display_name) is False:
                self.logger.warning(f"[GJM] 物品名称缓存写入失败：{market_name}")
            self.watcher.reconfigure()
        yield event.plain_result(message)

    @gjm.command("unsub")
    async def gjm_unsub(self, event: AstrMessageEvent):
        """按 /gjm mysub 的编号或物品页链接取消订阅。"""
        self._remember(event)
        arg = _args_after(event, "gjm", "unsub").strip()
        if not arg:
            yield event.plain_result("用法：/gjm unsub <编号>\n或：/gjm unsub <物品链接>\n先发送 /gjm mysub 查看编号。")
            return
        uid = str(event.get_sender_id() or "")
        target_umo = event.unified_msg_origin or ""
        serial = arg[1:] if arg.startswith("#") else arg
        if serial.isdigit():
            ok, message = self.subscriptions.remove_by_index(uid, int(serial), target_umo)
        else:
            ok, message = self.subscriptions.unsubscribe(uid, arg, target_umo)
        yield event.plain_result(message)
        if ok:
            self.watcher.reconfigure()

    @gjm.command("mysub")
    async def gjm_mysub(self, event: AstrMessageEvent):
        """先刷新行情，再查看我的订阅与全局列表。"""
        self._remember(event)
        action = _args_after(event, "gjm", "mysub").strip().lower()
        if action:
            yield event.plain_result("用法：/gjm mysub")
            return

        if self.subscriptions.watched_market_names:
            await event.send(event.plain_result("正在刷新行情，请稍候…"))
            try:
                await self.watcher.poll_once()
            except Exception as exc:
                yield event.plain_result(f"刷新失败，以下为最近缓存：{exc}")

        uid = str(event.get_sender_id() or "")
        target_umo = event.unified_msg_origin or ""
        subs = self.subscriptions.subscriptions_of(uid, target_umo)
        sections: list[str] = []
        if not subs:
            sections.append("【我的订阅】\n你还没有订阅。用 /gjm sub <物品链接> 订阅。")
        else:
            lines = [f"【当前会话中我的订阅 · {len(subs)} 件】"]
            for index, sub in enumerate(subs, start=1):
                quote = self.storage.get_last_quote(sub.market_name)
                if platforms.is_group_session(platforms.session_type_of(target_umo)):
                    mode_text = f"群聊全量，每 {self.cfg.group_push_interval} 秒"
                elif sub.mode == REFRESH_ONLY_CHANGE:
                    mode_text = f"only_change，每 {ONLY_CHANGE_INTERVAL} 秒检查"
                else:
                    mode_text = f"always，每 {sub.interval} 秒"
                lines.append(f"{index}. {sub.display_name or sub.market_name}（{mode_text}）")
                lines.append(f"   {self.item_url(sub.market_name)}")
                if quote and quote.ok:
                    lines.append(f"   售出 {quote.sell_min} / 求购 {quote.buy_max} GJN")
            lines.append("")
            lines.append("删除订阅：/gjm unsub <编号>，例如 /gjm unsub 1")
            sections.append("\n".join(lines))
        sections.append(self._global_list_text())
        yield event.plain_result("\n\n".join(sections))

    def _global_list_text(self) -> str:
        """渲染全局被监控物品列表，供 ``/gjm mysub`` 使用。"""
        names = self.subscriptions.watched_market_names
        if not names:
            return "没有被订阅的物品。用 /gjm sub <物品链接> 订阅。"

        lines = ["【被监控的物品】"]
        for index, market_name in enumerate(names, start=1):
            quote = self.storage.get_last_quote(market_name)
            title = (
                self.storage.get_item_name(market_name)
                or self.subscriptions.display_name_of(market_name)
                or prettify_market_name(market_name)
            )
            subs = self.subscriptions.subscribers_of(market_name)
            lines.append(f"{index}. {title}（{len(subs)} 人订阅）")
            lines.append(f"   {self.item_url(market_name)}")
            if quote and quote.ok:
                lines.append(f"   售出 {quote.sell_min} / 求购 {quote.buy_max} GJN")
            else:
                lines.append("   暂无行情数据")

        lines.append("")
        lines.append("提示：物品由用户订阅产生，用 /gjm sub <物品链接> 添加。")
        return "\n".join(lines)

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
        _, market_name = extract_link_reference(url)
        if not market_name:
            yield event.plain_result(REJECT_HINT)
            return

        try:
            token = await self.auth.get_token()
        except LoginRequiredError as exc:
            yield event.plain_result(f"令牌不可用：{exc}\n请发送 /gjm login 登录。")
            return

        await event.send(event.plain_result("正在查询…"))
        try:
            quote = await self.client.fetch_quote(
                market_name,
                token,
                appid=self.cfg.appid,
                display_name=self.subscriptions.display_name_of(market_name) or self.storage.get_item_name(market_name),
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
            lines.append("登录：等待验证码（发 /gjm login <验证码>）")
        if auth["last_error"]:
            lines.append(f"续期错误：{auth['last_error']}")
        subs = self.subscriptions.enabled_subscriptions
        uids = list(dict.fromkeys(s.uid for s in subs))
        lines.append("")
        lines.append(
            f"推送：{self.cfg.push_mode}，{len(subs)} 条订阅 / {len(uids)} 个 UID；"
            f"群聊每 {self.cfg.group_push_interval} 秒全量刷新，图表 {self.cfg.chart_history_points} 点"
        )
        for uid in uids:
            if not self.notifier.session_of(uid):
                lines.append(f"  ! {uid} 未解析到会话")
        yield event.plain_result("\n".join(lines))

    @gjm.group("refresh")
    def gjm_refresh(self):
        """刷新策略指令组。"""

    @gjm_refresh.command("mode")
    async def gjm_refresh_mode(self, event: AstrMessageEvent):
        """设置当前用户全部订阅的刷新模式。"""
        self._remember(event)
        if platforms.is_group_session(platforms.session_type_of(event.unified_msg_origin or "")):
            yield event.plain_result(
                f"群聊统一每 {self.cfg.group_push_interval} 秒刷新全部物品并推送，不使用个人刷新模式。"
            )
            return
        mode = _args_after(event, "gjm", "refresh", "mode").strip().lower()
        if mode == "on_change":
            mode = "only_change"
        if mode not in {"always", "only_change"}:
            yield event.plain_result("用法：/gjm refresh mode <always|only_change>")
            return
        ok, message = self.subscriptions.set_refresh_mode(
            str(event.get_sender_id() or ""),
            mode,
            event.unified_msg_origin or "",
        )
        yield event.plain_result(message)
        if ok:
            self.watcher.reconfigure()

    @gjm_refresh.command("interval")
    async def gjm_refresh_interval(self, event: AstrMessageEvent):
        """设置当前用户的全局或单物品刷新间隔（秒）。"""
        self._remember(event)
        if platforms.is_group_session(platforms.session_type_of(event.unified_msg_origin or "")):
            yield event.plain_result(
                f"群聊统一每 {self.cfg.group_push_interval} 秒刷新全部物品并推送，不支持个人刷新间隔。"
            )
            return
        parts = _args_after(event, "gjm", "refresh", "interval").split()
        if not parts:
            yield event.plain_result(
                f"用法：/gjm refresh interval <秒> [物品链接|编号]\n"
                f"（最小 {MIN_POLL_INTERVAL} 秒；不填目标则作用于你的全部订阅）"
            )
            return
        try:
            value = int(float(parts[0]))
        except (ValueError, OverflowError):
            yield event.plain_result("秒数没看懂，请给整数，例如 /gjm refresh interval 30")
            return
        uid = str(event.get_sender_id() or "")
        target = " ".join(parts[1:]).strip()
        target_umo = event.unified_msg_origin or ""
        serial = target[1:] if target.startswith("#") else target
        if serial.isdigit():
            ok, message = self.subscriptions.set_interval_by_index(uid, value, int(serial), target_umo)
        elif target:
            _, market_name = extract_link_reference(target)
            if not market_name:
                yield event.plain_result(REJECT_HINT)
                return
            ok, message = self.subscriptions.set_interval(uid, value, market_name, target_umo)
        else:
            ok, message = self.subscriptions.set_interval(uid, value, "", target_umo)
        yield event.plain_result(message)
        if ok:
            self.watcher.reconfigure()

    @gjm.group("trade")
    def gjm_trade(self):
        """自动交易指令组（仅拥有者私聊）。"""

    @gjm_trade.command("add")
    async def gjm_trade_add(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        raw = _args_after(event, "gjm", "trade", "add")
        parts = raw.split()
        if len(parts) != 5:
            yield event.plain_result(
                "用法：/gjm trade add <物品链接> <买入价> <数量> <税后卖出价> <once|loop>\n新规则默认模拟运行。"
            )
            return
        try:
            rule = await self.trader.create_rule(
                url=parts[0],
                buy_price=parts[1],
                quantity=parts[2],
                sell_net_price=parts[3],
                mode=parts[4],
            )
        except Exception as exc:
            yield event.plain_result(f"创建失败：{exc}")
            return
        yield event.plain_result(
            f"已创建模拟规则 {rule.rule_id}\n"
            f"物品：{rule.title}\n"
            f"买入：{rule.buy_price_raw / 10000:.2f} GJN × {rule.quantity}\n"
            f"税后卖出：{rule.sell_net_price_raw / 10000:.2f} GJN\n"
            f"模式：{rule.mode}\n"
            f"实盘切换：/gjm trade arm {rule.rule_id}"
        )

    @gjm_trade.command("list")
    async def gjm_trade_list(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        rules = self.trader.store.rules
        if not rules:
            yield event.plain_result("当前没有自动交易规则。")
            return
        lines = ["【自动交易规则】"]
        for rule in rules:
            runtime = self.trader.store.get_runtime(rule.rule_id)
            lines.append(
                f"{rule.rule_id}  {'实盘' if rule.live else '模拟'}  "
                f"{'启用' if rule.enabled else '停用'}  {runtime.phase}\n"
                f"  {rule.title}｜买 {rule.buy_price_raw / 10000:.2f}｜数量 {rule.quantity}｜"
                f"税后卖 {rule.sell_net_price_raw / 10000:.2f}｜{rule.mode}"
            )
            if runtime.last_error:
                lines.append(f"  错误：{runtime.last_error}")
        yield event.plain_result("\n".join(lines))

    @gjm_trade.command("show")
    async def gjm_trade_show(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        rule_id = _args_after(event, "gjm", "trade", "show").strip()
        if not rule_id:
            yield event.plain_result("用法：/gjm trade show <规则ID>")
            return
        try:
            rule = self.trader._require_rule(rule_id)
            view = self.trader.rule_view(rule)
        except TradeError as exc:
            yield event.plain_result(str(exc))
            return
        runtime = view["runtime"]
        yield event.plain_result(
            f"【规则 {view['rule_id']}】\n"
            f"物品：{view['title']}\n"
            f"市场：{self.item_url(view['market_name'])}\n"
            f"状态：{runtime['phase']}｜{'实盘' if view['live'] else '模拟'}｜"
            f"{'启用' if view['enabled'] else '停用'}\n"
            f"买入限价：{view['buy_price']} GJN × {view['quantity']}\n"
            f"税后目标：{view['sell_net_price']} GJN\n"
            f"税前挂单价：{view['sell_gross_price']} GJN（预计到手 {view['estimated_net']}）\n"
            f"持仓：{runtime['held_quantity']}，挂单：{runtime['listed_quantity']}，"
            f"累计税后收益：{runtime['proceeds_raw'] / 10000:.2f} GJN\n"
            f"模式：{view['mode']}｜完成轮数：{runtime['cycle_count']}\n"
        )

    @gjm_trade.command("budget")
    async def gjm_trade_budget(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        data = self.trader.overview()
        settings = data["settings"]
        ledger = data["ledger"]
        yield event.plain_result(
            "【自动交易预算】\n"
            f"总开关：{'开' if settings['enabled'] else '关'}\n"
            f"总投入预算：{settings['total_budget']} GJN｜当前占用 {data['exposure']}｜"
            f"剩余 {data['budget_remaining']}\n"
            f"每日限额：{settings['daily_limit']} GJN｜今日已用 {ledger['daily_spent']}｜"
            f"剩余 {ledger['daily_remaining']}\n"
            f"单笔上限：{settings['max_order']} GJN｜最大数量：{settings['max_quantity']}"
        )

    @gjm_trade.command("arm")
    async def gjm_trade_arm(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        parts = _args_after(event, "gjm", "trade", "arm").split()
        if not parts:
            yield event.plain_result("用法：/gjm trade arm <规则ID>，然后发送 /gjm trade arm <规则ID> confirm")
            return
        rule_id = parts[0]
        try:
            if len(parts) > 1 and parts[1].lower() == "confirm":
                await self.trader.confirm_arm(rule_id, rule_id)
                yield event.plain_result(f"规则 {rule_id.upper()} 已切换实盘。")
            else:
                yield event.plain_result(await self.trader.prepare_arm(rule_id))
        except Exception as exc:
            yield event.plain_result(f"切换实盘失败：{exc}")

    @gjm_trade.command("disarm")
    async def gjm_trade_disarm(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        rule_id = _args_after(event, "gjm", "trade", "disarm").strip()
        try:
            await self.trader.action(rule_id, "disarm")
        except Exception as exc:
            yield event.plain_result(f"退出实盘失败：{exc}")
            return
        yield event.plain_result(f"规则 {rule_id.upper()} 已退出实盘。")

    @gjm_trade.command("pause")
    async def gjm_trade_pause(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        rule_id = _args_after(event, "gjm", "trade", "pause").strip()
        try:
            await self.trader.action(rule_id, "pause")
        except Exception as exc:
            yield event.plain_result(f"暂停失败：{exc}")
            return
        yield event.plain_result(f"规则 {rule_id.upper()} 已暂停。")

    @gjm_trade.command("resume")
    async def gjm_trade_resume(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        rule_id = _args_after(event, "gjm", "trade", "resume").strip()
        try:
            await self.trader.action(rule_id, "resume")
        except Exception as exc:
            yield event.plain_result(f"恢复失败：{exc}")
            return
        yield event.plain_result(f"规则 {rule_id.upper()} 已恢复。")

    @gjm_trade.command("cancel")
    async def gjm_trade_cancel(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        rule_id = _args_after(event, "gjm", "trade", "cancel").strip()
        try:
            await self.trader.action(rule_id, "cancel_orders")
        except Exception as exc:
            yield event.plain_result(f"撤单失败：{exc}")
            return
        yield event.plain_result(f"规则 {rule_id.upper()} 的自动订单已撤销，规则已暂停。")

    @gjm_trade.command("delete")
    async def gjm_trade_delete(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        rule_id = _args_after(event, "gjm", "trade", "delete").strip()
        try:
            await self.trader.action(rule_id, "delete")
        except Exception as exc:
            yield event.plain_result(f"删除失败：{exc}")
            return
        yield event.plain_result(f"规则 {rule_id.upper()} 已删除。")

    @gjm_trade.command("orders")
    async def gjm_trade_orders(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        lines: list[str] = []
        for rule in self.trader.store.rules:
            runtime = self.trader.store.get_runtime(rule.rule_id)
            for order in runtime.orders:
                lines.append(
                    f"{rule.rule_id}  {order.side}  {rule.title}\n"
                    f"  订单 {order.order_id}｜数量 {order.amount}｜价格 {order.price_raw / 10000:.2f} GJN"
                )
        yield event.plain_result("\n".join(lines) if lines else "当前没有本插件跟踪的自动挂单。")

    @gjm_trade.command("log")
    async def gjm_trade_log(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        raw = _args_after(event, "gjm", "trade", "log").strip()
        try:
            limit = max(1, min(50, int(raw or 10)))
        except ValueError:
            yield event.plain_result("用法：/gjm trade log [1-50]")
            return
        events = self.trader.store.events[-limit:]
        if not events:
            yield event.plain_result("暂无自动交易日志。")
            return
        lines = ["【自动交易日志】"]
        for event_item in events:
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(event_item.timestamp))
            lines.append(f"{stamp} [{event_item.rule_id or '-'}] {event_item.message}")
        yield event.plain_result("\n".join(lines))

    @gjm_trade.command("stop-all")
    async def gjm_trade_stop_all(self, event: AstrMessageEvent):
        self._remember(event)
        event.stop_event()
        error = self._trade_access_error(event)
        if error:
            yield event.plain_result(error)
            return
        if _args_after(event, "gjm", "trade", "stop-all").strip().lower() != "confirm":
            yield event.plain_result(
                "急停会撤销本插件跟踪的全部订单并暂停规则。\n确认请发送：/gjm trade stop-all confirm"
            )
            return
        try:
            result = await self.trader.stop_all()
        except Exception as exc:
            yield event.plain_result(f"急停失败：{exc}")
            return
        text = "自动交易已急停，所有规则已暂停。"
        if result["cancelled"]:
            text += "\n已撤单规则：" + "、".join(result["cancelled"])
        if result["errors"]:
            text += "\n撤单异常：\n" + "\n".join(result["errors"])
        yield event.plain_result(text)

    @gjm.command("login")
    async def cmd_login(self, event: AstrMessageEvent):
        """登录 Gaijin 账号：/gjm login 发起，/gjm login <验证码> 提交验证码。"""
        self._remember(event)
        # 验证码属敏感内容：终止事件传播，避免其进入 LLM 上下文或其它插件
        event.stop_event()

        if not self._is_owner(event):
            yield event.plain_result(
                "仅拥有者可登录。请把你的 UID 填入插件控制台账户页的「拥有者 UID」。"
                "\n可发送 AstrBot 内置命令 /sid 查看。"
            )
            return

        code = _args_after(event, "gjm", "login")
        if code:
            yield event.plain_result(await self._submit_login_code(code))
            return
        yield event.plain_result(await self._begin_login())

    async def _begin_login(self) -> str:
        """第一步：发起登录。优先用 refresh_token（免密码），其次账号密码。"""
        if self.auth.has_pending_login:
            return "已有登录在等待验证码。请发送 /gjm login <验证码>"

        if self.auth.refresh_token and await self.auth.try_refresh():
            self.watcher.wake()
            return "refresh_token 仍有效，已换新 JWT，无需登录。"

        if not (self.accounts.email and self.accounts.password):
            return "未配置账号邮箱 / 密码。请先在插件控制台的账户页填写后重试。"

        try:
            step = await self.auth.begin_login(self.accounts.email, self.accounts.password)
        except LoginError as exc:
            return f"登录失败：{exc}"
        except Exception as exc:
            self.logger.error(f"[GJM] 登录第一阶段异常: {exc}")
            return f"登录失败：{exc}"

        if not step.need_code:
            self.on_login_success()
            return "登录成功。"

        return "站点要求验证码。请发送 /gjm login <验证码>（15 分钟内有效）"

    async def _submit_login_code(self, code: str) -> str:
        """第二步：提交验证码。"""
        if not self.auth.has_pending_login:
            return "没有等待验证码的登录流程（可能已超时）。请先发送 /gjm login。"
        try:
            step = await self.auth.submit_code(code)
        except LoginError as exc:
            return f"验证码提交失败：{exc}"
        except Exception as exc:
            self.logger.error(f"[GJM] 登录第二阶段异常: {exc}")
            return f"验证码提交失败：{exc}"
        self.on_login_success()
        return f"验证码校验通过，{step.message}"

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
        session_type = platforms.session_type_of(umo)
        plan = platforms.mention_plan_for(
            platform_type, str(event.get_sender_id()), event.get_sender_name() or "", umo=umo
        )
        mention_note = platforms.mention_style_text(plan)
        yield event.plain_result(
            "【排障信息】\n"
            f"配置文件：{self._config_path}\n"
            f"配置文件存在：{self._config_path.exists()}\n"
            f"配置热监听运行：{self._config_watcher.running}\n"
            f"等待验证码：{'是' if pending else '否'}"
            + (f"（字段 {pending.code_field}，已等待 {int((auth['pending_age'] or 0) / 60)} 分钟）" if pending else "")
            + f"\n账号邮箱：{self.accounts.email or '未填'}\n"
            f"账号密码：{'已填' if self.accounts.password else '未填'}\n"
            f"JWT 账号：{describe_jwt(self.auth.jwt).get('nick') or '无'}\n"
            "\n【@ 适配】\n"
            f"本会话平台类型：{platform_type or '（未匹配到）'}（会话类型 {session_type or '未知'}）\n"
            f"mention_users：{self.cfg.mention_users}\n"
            f"将采用：{mention_note}\n"
            f"拥有者 UID / 已识别会话：{self.accounts.owner_uids or '（未配置）'} / "
            f"{len(self.notifier.owner_sessions())}" + "\n\n" + "\n".join(await self._platform_report())
        )
