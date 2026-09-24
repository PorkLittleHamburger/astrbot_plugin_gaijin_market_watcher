"""平台差异的**唯一权威处**。

为什么需要这个模块：AstrBot 各平台适配器的出站行为并不一致，而且差异都藏在
适配器实现里（不是文档里）。目前实测确认的有：

* ``qq_official`` / ``qq_official_webhook``
  出站只取纯文本或 markdown，**At 组件会被静默丢弃**（不报错也不生效）。
  正确做法是把 @ 写成**文本内嵌标记**并走 markdown 通道。
  但该标记**只有群聊 / 频道能发**：私聊（C2C / 频道私信）接口会直接拒绝整条消息
  （``ServerError: C2C消息不支持qqbot-at-user.``），因此私聊一律不 @。

* ``weixin_oc``（个人微信）
  ``send_by_session`` 只处理 ``Plain`` / ``Image`` / ``Video`` / ``File``，
  ``At`` 会落到 "unsupported outbound segment type" 分支被忽略；
  **没有文字的消息会被整条丢弃**（"message without plain text is ignored"）；
  主动推送还需要 ``context_token`` —— 用户先给机器人发过消息才有，
  没有令牌时会直接跳过发送并只打一条 warning。

* ``wecom``（企业微信客服模式）
  ``send_by_session`` 直接抛「企业微信客服模式不支持 send_by_session 主动发送」。

* 其它平台（telegram / discord / aiocqhttp / lark …）标准 ``At`` 组件可用。

本模块只做两件事：把这些事实写成数据，并提供几个纯函数给推送层与诊断输出复用。
不依赖 AstrBot、不发网络请求，便于单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 文本内嵌 @ 标记（{uid} 为占位符）。必须走 markdown 通道才会被客户端解析，
#: 而且只有群聊 / 频道能发 —— 私聊发出去平台会拒绝整条消息。
TEXT_MENTION_TEMPLATES: dict[str, str] = {
    "qq_official": '<qqbot-at-user id="{uid}" />',
    "qq_official_webhook": '<qqbot-at-user id="{uid}" />',
}

#: 群聊专用的 @ 标记本身（私聊正文里出现它会被平台整条拒绝，需剔除）。
GROUP_MENTION_TAG_PATTERN = re.compile(r"<qqbot-at-user\b[^>]*?/?>")

#: AstrBot 的 UMO 形如 ``platform_id:message_type:session_id``，中间段来自
#: ``astrbot.core.platform.message_type.MessageType``。
SESSION_TYPE_GROUP = "GroupMessage"
SESSION_TYPE_FRIEND = "FriendMessage"
SESSION_TYPE_OTHER = "OtherMessage"

#: 出站不处理 At 组件的平台 —— 对这些平台要降级为**纯文本 @昵称**，否则 @ 会凭空消失。
NO_AT_PLATFORMS: frozenset[str] = frozenset(
    {
        "weixin_oc",
        "weixin_official_account",
        "wecom",
        "qqofficial_webhook",
        "webchat",
    }
)

#: 明确不支持主动推送（非用户触发）的平台。
NO_PROACTIVE_PLATFORMS: frozenset[str] = frozenset({"wecom"})

#: 主动推送的额外前提（人话说明，用于配置体检与 /gjm debug）。
PROACTIVE_NOTES: dict[str, str] = {
    "weixin_oc": "需要用户先给机器人发过消息（context_token 过期后要再发一次，否则推送会被跳过）",
    "weixin_official_account": "受微信客服消息时限约束（用户互动后的一段时间内可推送）",
    "wecom": "企业微信客服模式不支持主动推送",
}

#: 出站必须带文字的平台（只发图片会被整条丢弃）。
TEXT_REQUIRED_PLATFORMS: frozenset[str] = frozenset({"weixin_oc", "weixin_official_account", "wecom"})

STYLE_MARKDOWN_TEXT = "markdown_text"
STYLE_PLAIN_TEXT = "plain_text"
STYLE_AT_COMPONENT = "at_component"
#: 该会话下不该 @（平台会拒绝整条消息）—— 调用方直接不加任何 @。
STYLE_NONE = "none"


@dataclass(frozen=True, slots=True)
class MentionPlan:
    """某个平台上「怎么 @ 到人」的结论。"""

    style: str
    text: str = ""
    #: 是否必须让整条消息走 markdown 通道
    needs_markdown: bool = False

    @property
    def is_component(self) -> bool:
        """True 表示调用方应该改用 At 组件（而不是往正文里塞文本）。"""
        return self.style == STYLE_AT_COMPONENT


def session_type_of(umo: str) -> str:
    """从 UMO（``platform_id:message_type:session_id``）里取出会话类型段。

    取不到时返回空串，调用方按「未知」处理（沿用历史行为）。
    """
    parts = (umo or "").split(":", 2)
    return parts[1].strip() if len(parts) > 1 else ""


def is_group_session(session_type: str) -> bool:
    """是否群聊 / 频道会话（QQ 官方的群聊 @ 标记只在这些会话里被接受）。"""
    return session_type == SESSION_TYPE_GROUP


def strip_group_mention_tags(text: str) -> str:
    """剔除文本里的 QQ 群聊专用 @ 标记（私聊带着它，平台会拒绝整条消息）。"""
    return GROUP_MENTION_TAG_PATTERN.sub("", text)


def mention_plan(platform_type: str, uid: str, name: str = "", *, session_type: str = "") -> MentionPlan:
    """给出该平台在这个会话里 @ 某人的正确方式。

    ``session_type`` 传 UMO 的中间段（见 :func:`session_type_of`）；已知是私聊 / 单聊时
    返回 :data:`STYLE_NONE`，调用方直接不 @。留空表示「不知道怎么@」，按历史行为处理。
    """
    template = TEXT_MENTION_TEMPLATES.get(platform_type)
    if template:
        if session_type and not is_group_session(session_type):
            # 私聊（C2C / 频道私信）：平台不认群聊 @ 标记，发了整条消息会被拒
            return MentionPlan(style=STYLE_NONE)
        return MentionPlan(style=STYLE_MARKDOWN_TEXT, text=template.format(uid=uid), needs_markdown=True)

    plain = f"@{name}" if name else f"@{uid}"
    if platform_type in NO_AT_PLATFORMS:
        return MentionPlan(style=STYLE_PLAIN_TEXT, text=plain)
    return MentionPlan(style=STYLE_AT_COMPONENT, text=plain)


def mention_plan_for(platform_type: str, uid: str, name: str = "", *, umo: str = "") -> MentionPlan:
    """按会话（UMO）给出 @ 方式：从 UMO 里解析会话类型再交给 :func:`mention_plan`。"""
    return mention_plan(platform_type, uid, name, session_type=session_type_of(umo))


def requires_text(platform_type: str) -> bool:
    """该平台是否要求消息必须带文字（只发图会被丢掉）。"""
    return platform_type in TEXT_REQUIRED_PLATFORMS


def supports_at_component(platform_type: str) -> bool:
    return platform_type not in NO_AT_PLATFORMS and platform_type not in TEXT_MENTION_TEMPLATES


def proactive_hint(platform_type: str, declared_flag: bool | None = None) -> str:
    """主动推送能力的一句话结论，供诊断输出使用。"""
    if platform_type in NO_PROACTIVE_PLATFORMS or declared_flag is False:
        return "主动推送：不支持"
    note = PROACTIVE_NOTES.get(platform_type, "")
    return "主动推送：支持" + (f"（{note}）" if note else "")


def mention_style_text(plan: MentionPlan) -> str:
    """把人话版的 @ 结论写出来（供 /gjm debug 与文档复用）。"""
    if plan.style == STYLE_MARKDOWN_TEXT:
        return f"文本内嵌标记（markdown 通道）：{plan.text}"
    if plan.style == STYLE_PLAIN_TEXT:
        return f"纯文本 @（该平台不认 At 组件）：{plan.text}"
    if plan.style == STYLE_AT_COMPONENT:
        return "标准 At 组件"
    return "不 @（该会话类型下平台不支持 @ 标记，已自动跳过）"


def describe(platform_id: str, platform_type: str, declared_flag: bool | None = None) -> str:
    """一行平台能力说明，供 /gjm debug 使用。"""
    plan = mention_plan(platform_type, "UID", "某人")
    at_text = mention_style_text(plan)
    if platform_type in TEXT_MENTION_TEMPLATES:
        # 这种 @ 只在群聊 / 频道有效，私聊会被平台拒绝，插件会自动跳过
        at_text += "｜仅群聊 / 频道；私聊自动跳过 @"
    return f"  {platform_id}（{platform_type}）：@ = {at_text}；{proactive_hint(platform_type, declared_flag)}"


def capability_rows() -> list[tuple[str, str, str, str]]:
    """平台能力矩阵（供文档/README 生成）：(平台, @ 方式, 图片, 主动推送)。"""
    rows = [
        (
            "qq_official",
            "文本内嵌标记（markdown 通道；私聊不支持，自动跳过）",
            "支持",
            "支持（群聊需适配器补丁，本插件已处理）",
        ),
        ("qq_official_webhook", "文本内嵌标记（markdown 通道；私聊不支持，自动跳过）", "支持", "支持"),
        ("aiocqhttp / OneBot", "At 组件", "支持", "支持"),
        ("telegram / discord / slack …", "At 组件", "支持", "支持"),
        (
            "weixin_oc（个人微信）",
            "纯文本 @昵称（At 会被忽略）",
            "支持（但消息必须带文字）",
            "需 context_token：用户先发过消息才行",
        ),
        ("weixin_official_account", "纯文本 @昵称", "支持", "受微信客服消息时限约束"),
        ("wecom（客服模式）", "纯文本 @昵称", "支持", "不支持"),
    ]
    return rows
