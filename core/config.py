"""配置层：把 AstrBot 注入的原始配置 dict 归一化为强类型对象。

AstrBot 会依据 _conf_schema.json 生成配置文件并注入 __init__，
原始值可能因为 WebUI 编辑器而出现类型漂移（例如 int 存成 str），
因此这里做一次统一的宽松解析 + 校验，业务代码只消费强类型结果。

【重要】账号密码字段说明：
  account_email / account_password 用于在 WebUI 里完成登录（含两步验证），
  它们会被写入插件配置文件（data/config/<插件名>_config.json）。
  这是"把登录全部搬到配置界面"这一需求的必然结果 —— 配置项即持久化。
  若你希望密码不落盘，把这两项留空并只填 refresh_token 即可（功能不受影响，
  只是 refresh_token 失效后需要重新填写）。
"""

from __future__ import annotations

from typing import Any

from .constants import (
    DEFAULT_APPID,
    DEFAULT_LANGUAGE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    PUSH_BATCH_SECONDS,
    PUSH_MODES,
    PUSH_ON_CHANGE,
    SNAPSHOT_MODES,
)
from .models import Subscription
from .payload import validate_payload
from .resolver import clean_text, extract_reference, prettify_market_name


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if isinstance(value, (int, float)):
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


def _as_str_list(value: Any) -> list[str]:
    """把 list / 逗号分隔字符串 / 单个字符串统一成去重的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        return []
    result: list[str] = []
    for part in parts:
        text = clean_text(str(part))
        if text and text not in result:
            result.append(text)
    return result


def _clamp_interval(value: Any, default: int = DEFAULT_POLL_INTERVAL) -> int:
    """把单条订阅的刷新间隔夹到合法区间（秒）。"""
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, _as_int(value, default)))


class PluginConfig:
    """强类型插件配置。"""

    def __init__(self, raw: dict | None) -> None:
        self.raw: dict = dict(raw or {})

        # ---------- 账号与登录（全部在 WebUI 完成） ----------
        self.account_email: str = _as_str(self.raw.get("account_email"))
        self.account_password: str = str(self.raw.get("account_password") or "")
        #: 用户在配置界面粘贴的验证码；插件检测到变化后会尝试提交（用完即清空）
        self.twofa_code: str = clean_text(str(self.raw.get("twofa_code") or ""))

        # ---------- 令牌 ----------
        self.jwt_token: str = _as_str(self.raw.get("jwt_token"))
        self.refresh_token: str = _as_str(self.raw.get("refresh_token"))

        # ---------- 接口 ----------
        self.appid: str = _as_str(self.raw.get("appid"), DEFAULT_APPID)
        self.language: str = _as_str(self.raw.get("language"), DEFAULT_LANGUAGE)
        self.request_timeout: float = max(3.0, _as_float(self.raw.get("request_timeout"), DEFAULT_TIMEOUT))
        self.request_retries: int = max(0, _as_int(self.raw.get("request_retries"), DEFAULT_RETRIES))
        self.item_interval: float = max(0.0, _as_float(self.raw.get("item_interval"), 0.4))

        # ---------- 监控行为 ----------
        # 刷新间隔不再全局配置，而是跟着每条订阅走（见 interval_of）。
        push_mode = _as_str(self.raw.get("push_mode"), PUSH_ON_CHANGE)
        self.push_mode: str = push_mode if push_mode in PUSH_MODES else PUSH_ON_CHANGE

        # ---------- 身份与路由 ----------
        # 拥有者（管理员）：按 **UID** 判定，/login 与管理员指令都用它。
        # 同一真人在不同平台 / 会话下 UID 稳定，所以只需要填 UID。
        self.owner_uids: list[str] = _as_str_list(self.raw.get("owner_uids"))
        #: 推送时是否 @ 订阅者（群聊场景必需；QQ 官方会自动走文本内嵌标记）
        self.mention_users: bool = _as_bool(self.raw.get("mention_users"), True)

        # ---------- 网页快照（价格变化时补一张商品页截图） ----------
        #: off=不截图；on_change=价格变化后截图；on_trigger=仅阈值触发时截图
        snapshot_mode = _as_str(self.raw.get("snapshot_mode"), "on_change").lower()
        self.snapshot_mode: str = snapshot_mode if snapshot_mode in SNAPSHOT_MODES else "on_change"
        #: 同一会话两次截图的最小间隔（秒），防止高频变化把截图队列打爆
        self.snapshot_cooldown: int = max(0, _as_int(self.raw.get("snapshot_cooldown"), 600))

        # ---------- 输出模板（payload） ----------
        #: 用 $字段 占位，渲染成 JSON 当作推送正文；留空 = 保持默认可读文本
        self.payload: str = str(self.raw.get("payload") or "")

        # ---------- 推送合并 ----------
        #: 同一时间窗内多件物品的变化合并成一条消息（0 = 每件单独发）
        self.push_batch_seconds: int = max(0, min(120, _as_int(self.raw.get("push_batch_seconds"), PUSH_BATCH_SECONDS)))

        # ---------- 用户订阅（每用户每物品一条） ----------
        #: 解析订阅时若没写 interval，沿用旧的全局 poll_interval（兼容），再退回默认值
        self._default_interval: int = _clamp_interval(self.raw.get("poll_interval"), DEFAULT_POLL_INTERVAL)
        self.subscriptions: list[Subscription] = self._parse_subscriptions(self.raw.get("user_subscriptions"))

    # ------------------------------------------------------------------ 解析

    def _parse_subscriptions(self, value: Any) -> list[Subscription]:
        """解析用户订阅。

        支持两种结构：
        ① **每用户一组**（当前结构）：`{uid, interval, enabled, items: [链接, ...]}`
           —— 同一个用户只出现一次，物品是链接列表，刷新间隔挂在用户上；
        ② 旧结构：一条一个物品（`{uid, item_url, interval, ...}`），仍然兼容。
        """
        subs: dict[tuple[str, str], Subscription] = {}
        for entry in self._entries(value):
            for sub in self._parse_entry(entry):
                subs[sub.key] = sub
        return list(subs.values())

    @staticmethod
    def _entries(value: Any) -> list[Any]:
        if isinstance(value, dict):
            value = list(value.values())
        return list(value) if isinstance(value, (list, tuple)) else []

    def _parse_entry(self, entry: Any) -> list[Subscription]:
        """把一条配置（用户组，或旧的单物品条）解析成若干 Subscription。"""
        if not isinstance(entry, dict):
            return []
        uid = _as_str(entry.get("uid"))
        if not uid:
            return []

        # 门限配置项已移除：这里一律 0（不启用阈值），旧配置里的残留值不再生效。
        # 注意：`note` 是用户备注，**不能**当作物品显示名（否则 /gjm my 会把物品名显示成备注）。
        shared = {
            "display_name": _as_str(entry.get("name")),
            "sell_below": 0.0,
            "buy_above": 0.0,
            "enabled": _as_bool(entry.get("enabled"), True),
            "interval": _clamp_interval(entry.get("interval"), self._default_interval),
        }

        out: list[Subscription] = []
        items = entry.get("items")
        if isinstance(items, dict):
            items = list(items.values())
        for item in items if isinstance(items, (list, tuple)) else []:
            if isinstance(item, dict):  # 也容忍 [{item_url, sell_below, buy_above}]
                _, market_name = extract_reference(
                    str(item.get("item_url") or item.get("url") or item.get("item") or "")
                )
                if not market_name:
                    continue
                out.append(
                    Subscription(
                        uid=uid,
                        market_name=market_name,
                        display_name=_as_str(item.get("name")) or shared["display_name"],
                        enabled=_as_bool(item.get("enabled"), shared["enabled"]),
                        interval=shared["interval"],
                    )
                )
                continue
            _, market_name = extract_reference(str(item))
            if market_name:
                out.append(Subscription(uid=uid, market_name=market_name, **shared))

        # 兼容旧结构：物品链接直接写在这一条上
        raw_ref = entry.get("item_url") or entry.get("url") or entry.get("item") or ""
        if not out and raw_ref:
            _, market_name = extract_reference(str(raw_ref))
            if market_name:
                out.append(Subscription(uid=uid, market_name=market_name, **shared))
        return out

    # ------------------------------------------------------------------ 校验

    def validate(self) -> list[str]:
        """返回面向用户的配置提示（不抛异常，尽力运行）。"""
        hints: list[str] = []
        short = sorted({s.interval for s in self.enabled_subscriptions if s.interval < 10})
        if short:
            hints.append(
                "以下订阅的刷新间隔较短：" + "、".join(f"{v} 秒" for v in short) + "，可能触发站点风控，建议 ≥ 10 秒。"
            )

        has_token = bool(self.jwt_token or self.refresh_token)
        has_account = bool(self.account_email and self.account_password)
        if not has_token and not has_account:
            hints.append("尚未配置任何凭据：请在 WebUI 插件配置完成登录，或填写 refresh_token / jwt_token。")
        if self.account_email and not self.account_password:
            hints.append("已填账号邮箱但未填密码，无法自动登录。")

        watched = self.watched_market_names
        if not watched:
            hints.append(
                "当前没有被监控/订阅的物品：可在聊天里发送 /gjm sub <物品链接>，或在配置里填写 user_subscriptions。"
            )
        if not self.owner_uids:
            hints.append("未配置拥有者 UID，/login 等管理指令将回退为 AstrBot 管理员判定。")
        if not self.subscriptions:
            hints.append("当前没有任何用户订阅，推送将无处可去（可用 /gjm sub <物品链接> 添加）。")
        if self.snapshot_mode != "off" and self.snapshot_cooldown < 60:
            hints.append(f"网页快照冷却仅 {self.snapshot_cooldown} 秒：每张图约需 15 秒渲染，过短会造成截图排队。")
        if self.payload.strip():
            ok, note = validate_payload(self.payload)
            if not ok:
                hints.append(f"payload 模板不可用（{note}），推送会退回可读文本。")
            elif note.startswith("可用，但含未知字段"):
                hints.append(f"payload 模板：{note}")
        return hints

    # ------------------------------------------------------------------ 间隔

    def interval_of(self, market_name: str) -> int:
        """某件物品的实际抓取间隔 = 订阅它的那些用户里最小的 interval。"""
        values = [s.interval for s in self.enabled_subscriptions if s.market_name == market_name]
        return min(values) if values else DEFAULT_POLL_INTERVAL

    @property
    def intervals(self) -> dict[str, int]:
        """物品 -> 实际间隔。"""
        return {name: self.interval_of(name) for name in self.watched_market_names}

    @property
    def min_interval(self) -> int:
        values = list(self.intervals.values())
        return min(values) if values else DEFAULT_POLL_INTERVAL

    # ------------------------------------------------------------------ 便捷

    @property
    def enabled_subscriptions(self) -> list[Subscription]:
        return [s for s in self.subscriptions if s.enabled]

    @property
    def watched_market_names(self) -> list[str]:
        """全局监控清单与用户订阅的并集（去重）—— 轮询只按这份清单走。"""
        names: list[str] = []
        for sub in self.enabled_subscriptions:
            if sub.market_name not in names:
                names.append(sub.market_name)
        return names

    def subscriptions_of(self, uid: str, market_name: str = "") -> list[Subscription]:
        """查某个 UID 的订阅（可按物品过滤）。"""
        result = [s for s in self.subscriptions if s.uid == uid]
        if market_name:
            result = [s for s in result if s.market_name == market_name]
        return result

    def subscribers_of(self, market_name: str) -> list[Subscription]:
        """查订阅了某物品的所有用户。"""
        return [s for s in self.enabled_subscriptions if s.market_name == market_name]

    def display_name_of(self, market_name: str) -> str:
        """物品显示名。

        ① 先用订阅里的别名（旧配置的 `name`）；
        ② 没有就用 slug 推导出的可读名（`id50381_f_16xl_usa` -> `F 16Xl Usa`）——
           以前这个由配置里的 name 提供，改成"每用户一条"后就没有逐物品名字了。
        """
        for sub in self.subscriptions:
            if sub.market_name == market_name and sub.display_name:
                return sub.display_name
        return prettify_market_name(market_name)

    def reload(self, raw: dict | None) -> None:
        """热重载：用新配置覆盖当前实例的全部字段。"""
        self.__init__(raw)  # type: ignore[misc]
