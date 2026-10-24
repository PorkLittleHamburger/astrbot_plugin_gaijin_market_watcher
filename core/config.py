"""配置层：把 AstrBot 注入的技术配置归一化为强类型对象。

登录凭据、拥有者 UID、订阅和输出模板已经迁出插件配置，分别由
AccountStore / SubscriptionStore / OutputStore 管理。本模块只保留连接、
调度和推送相关的技术参数。
"""

from __future__ import annotations

from typing import Any

from .constants import (
    DEFAULT_APPID,
    DEFAULT_CHART_HISTORY_POINTS,
    DEFAULT_GROUP_PUSH_INTERVAL,
    DEFAULT_LANGUAGE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    MAX_CHART_HISTORY_POINTS,
    MAX_GROUP_PUSH_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_CHART_HISTORY_POINTS,
    MIN_GROUP_PUSH_INTERVAL,
    MIN_POLL_INTERVAL,
    PUSH_BATCH_SECONDS,
    PUSH_MODES,
    PUSH_ON_CHANGE,
    REFRESH_ALWAYS,
    REFRESH_ONLY_CHANGE,
)
from .resolver import clean_text


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if isinstance(value, int | float):
        return bool(value)
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = clean_text(str(value))
    return text or default


class PluginConfig:
    """强类型插件技术配置。"""

    def __init__(self, raw: dict | None) -> None:
        self.raw: dict = dict(raw or {})

        # 令牌字段仅用于兼容 2.18.x 以前遗留在配置里的旧安装。
        self.jwt_token: str = _as_str(self.raw.get("jwt_token"))
        self.refresh_token: str = _as_str(self.raw.get("refresh_token"))

        self.appid: str = _as_str(self.raw.get("appid"), DEFAULT_APPID)
        self.language: str = _as_str(self.raw.get("language"), DEFAULT_LANGUAGE)
        self.request_timeout: float = max(3.0, _as_float(self.raw.get("request_timeout"), DEFAULT_TIMEOUT))
        self.request_retries: int = max(0, _as_int(self.raw.get("request_retries"), DEFAULT_RETRIES))
        self.item_interval: float = max(0.0, _as_float(self.raw.get("item_interval"), 0.4))

        push_mode = _as_str(self.raw.get("push_mode"), PUSH_ON_CHANGE)
        self.push_mode: str = push_mode if push_mode in PUSH_MODES else PUSH_ON_CHANGE
        self.default_refresh_mode: str = REFRESH_ALWAYS if self.push_mode == REFRESH_ALWAYS else REFRESH_ONLY_CHANGE
        self.mention_users: bool = _as_bool(self.raw.get("mention_users"), True)

        self.push_batch_seconds: int = max(0, min(120, _as_int(self.raw.get("push_batch_seconds"), PUSH_BATCH_SECONDS)))
        self.group_push_interval: int = max(
            MIN_GROUP_PUSH_INTERVAL,
            min(MAX_GROUP_PUSH_INTERVAL, _as_int(self.raw.get("group_push_interval"), DEFAULT_GROUP_PUSH_INTERVAL)),
        )
        self.chart_history_points: int = max(
            MIN_CHART_HISTORY_POINTS,
            min(MAX_CHART_HISTORY_POINTS, _as_int(self.raw.get("chart_history_points"), DEFAULT_CHART_HISTORY_POINTS)),
        )
        self.default_interval: int = max(
            MIN_POLL_INTERVAL,
            min(MAX_POLL_INTERVAL, _as_int(self.raw.get("poll_interval"), DEFAULT_POLL_INTERVAL)),
        )

    def validate(self, *, subscriptions=None, owner_uids=None, output=None, auth=None) -> list[str]:
        """返回面向用户的启动提示（不抛异常）。"""
        hints: list[str] = []
        if subscriptions is not None:
            short = sorted({sub.interval for sub in subscriptions.enabled_subscriptions if sub.interval < 10})
            if short:
                hints.append(
                    "以下订阅的刷新间隔较短："
                    + "、".join(f"{value} 秒" for value in short)
                    + "，可能触发站点风控，建议 ≥ 10 秒。"
                )
            if not subscriptions.watched_market_names:
                hints.append("当前没有行情订阅：可在插件控制台或聊天中订阅物品。")
        has_credentials = bool(
            (getattr(auth, "jwt", "") or getattr(auth, "refresh_token", ""))
            if auth is not None
            else (self.jwt_token or self.refresh_token)
        )
        if not has_credentials:
            hints.append("尚未配置登录令牌：请在插件控制台完成账号登录。")
        if owner_uids is not None and not owner_uids:
            hints.append("未配置拥有者 UID：聊天登录可回退管理员判定，但自动交易会持续禁用。")
        if output is not None:
            for label, template, kind in (
                ("行情", output.quote_payload, "quote"),
                ("交易", output.trade_payload, "trade"),
            ):
                ok, note = output.validate(template, kind)
                if not ok:
                    hints.append(f"{label}输出模板不可用（{note}），将退回可读文本。")
        return hints

    def reload(self, raw: dict | None) -> None:
        self.__init__(raw)
