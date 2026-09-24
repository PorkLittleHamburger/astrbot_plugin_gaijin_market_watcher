"""平台差异的**唯一权威处**。

为什么需要这个模块：AstrBot 各平台适配器的出站行为并不一致，而且差异都藏在
适配器实现里（不是文档里）。目前实测确认的有：

* ``qq_official`` / ``qq_official_webhook``
  出站只取纯文本或 markdown，**At 组件会被静默丢弃**（不报错也不生效）。
  正确做法是把 @ 写成**文本内嵌标记**并走 markdown 通道。

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

from dataclasses import dataclass

#: 文本内嵌 @ 标记（{uid} 为占位符）。必须走 markdown 通道才会被客户端解析。
TEXT_MENTION_TEMPLATES: dict[str, str] = {
    "qq_official": '<qqbot-at-user id="{uid}" />',
    "qq_official_webhook": '<qqbot-at-user id="{uid}" />',
}

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


def mention_plan(platform_type: str, uid: str, name: str = "") -> MentionPlan:
    """给出该平台 @ 某人的正确方式（不需要 @ 时调用方自行跳过）。"""
    template = TEXT_MENTION_TEMPLATES.get(platform_type)
    if template:
        return MentionPlan(style=STYLE_MARKDOWN_TEXT, text=template.format(uid=uid), needs_markdown=True)

    plain = f"@{name}" if name else f"@{uid}"
    if platform_type in NO_AT_PLATFORMS:
        return MentionPlan(style=STYLE_PLAIN_TEXT, text=plain)
    return MentionPlan(style=STYLE_AT_COMPONENT, text=plain)


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


def describe(platform_id: str, platform_type: str, declared_flag: bool | None = None) -> str:
    """一行平台能力说明，供 /gjm debug 使用。"""
    plan = mention_plan(platform_type, "UID", "某人")
    at_text = {
        STYLE_MARKDOWN_TEXT: "文本内嵌标记（走 markdown）",
        STYLE_PLAIN_TEXT: "纯文本 @昵称（该平台不认 At 组件）",
        STYLE_AT_COMPONENT: "At 组件",
    }[plan.style]
    return f"  {platform_id}（{platform_type}）：@ = {at_text}；{proactive_hint(platform_type, declared_flag)}"


def capability_rows() -> list[tuple[str, str, str, str]]:
    """平台能力矩阵（供文档/README 生成）：(平台, @ 方式, 图片, 主动推送)。"""
    rows = [
        ("qq_official", "文本内嵌标记（markdown 通道）", "支持", "支持（群聊需适配器补丁，本插件已处理）"),
        ("qq_official_webhook", "文本内嵌标记（markdown 通道）", "支持", "支持"),
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
