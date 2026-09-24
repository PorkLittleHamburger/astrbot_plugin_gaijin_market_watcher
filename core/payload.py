"""输出模板（payload）：决定一条推送**长什么样**。

写法：JSON 骨架。**键名只是位置标记**，消息里只出现值（不会出现键名与花括号）。

    {
        "title": "$item_name行情报告",
        "datetime": "时间：$datetime",
        "content": [
            "卖单最低$sell_lowest_price，较上次变化$sell_change_amount($sell_change_rate)",
            "买单最高$buy_highest_price，较上次变化$buy_change_amount($buy_change_rate)",
            "卖单数$sell_count,较上次变化$sell_count_change_amount($sell_count_change_rate)",
            "买单数$buy_count,较上次变化$buy_count_change_amount($buy_count_change_rate)"
        ],
        "image": "$image"
    }

拼装规则（自上而下、逐行拼接成**一条**消息）：

  * 键按你写 JSON 的顺序处理；
  * 字符串值 → 替换 `$变量` 后成为一行（值里带换行时保持多行）；
  * **数组值 → 每个元素一行**；元素内部**禁止换行**（换行会被压成空格），
    所以把 `$content` 这种多行文本塞进数组元素也不会破坏“一行一项”；
  * 值为空的段（`$group` 在私聊、`$icon` 未缓存…）→ **自动省略**；
  * 键名是 `image` / `icon` / `img` / `picture` / `photo` / `pic` / `cover` / `thumbnail` → 作为**图片附件**；
  * 键名是 `mention` / `at` → 输出 @ 标记（用了它插件就不再自动加 @）；
  * 多件物品：按“单件”逐件渲染后拼接。

`$image` 特指**商品页快照**：需要现拍时插件会**等它渲染完**（约十几秒），
然后把正文与图片**放在同一条消息**里发出；拍不了（没开快照 / 正在拍 / 失败）就只发正文。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("astrbot")

#: 这些键会被当作**图片附件**而不是文字
IMAGE_KEYS = frozenset({"image", "icon", "img", "picture", "photo", "pic", "cover", "thumbnail"})

#: 这些键会被当作 @ 标记
MENTION_KEYS = frozenset({"mention", "at"})

#: 别名 -> 正式字段名（方便你沿用自己习惯的命名）
ALIASES: dict[str, str] = {
    "item_name": "name",
    "goods_name": "name",
    "sell_lowest_price": "sell_text",
    "lowest_price": "sell_text",
    "sell_price": "sell_text",
    "buy_highest_price": "buy_text",
    "highest_price": "buy_text",
    "buy_price": "buy_text",
    "sell_count": "sell_depth",
    "sell_orders": "sell_depth",
    "buy_count": "buy_depth",
    "buy_orders": "buy_depth",
    "sell_change_pct": "sell_change_rate",
    "buy_change_pct": "buy_change_rate",
    "sell_count_change_pct": "sell_count_change_rate",
    "buy_count_change_pct": "buy_count_change_rate",
    "time": "datetime",
    "round_time": "datetime",
    "now": "datetime",
}

#: 整轮（一次推送）可用字段
ROUND_FIELDS: dict[str, str] = {
    "title": "推送标题",
    "datetime": "本轮时间（YYYY-MM-DD-HH-MM-SS，全横线）",
    "datetime_plain": "本轮时间（YYYY-MM-DD HH:MM:SS，带空格）",
    "timestamp": "本轮时间（Unix 秒，整数）",
    "digest": "整轮可读正文（含表头与抓取失败项）",
    "count": "本轮物品数量",
    "uid": "收件人 UID",
    "user": "收件人昵称",
    "group": "群 ID（私聊为空串）",
    "platform": "平台类型，如 qq_official",
    "mention": "该平台的 @ 标记；用了它插件就不再自动加 @",
    "mode": "推送模式 on_change / always / on_trigger",
    "failures": "本轮抓取失败的物品名（顿号分隔）",
    "failures_json": "失败明细 [{name, error}]",
    "appid": "游戏 appid",
}

#: 单件物品可用字段
ITEM_FIELDS: dict[str, str] = {
    "name": "物品显示名（别名 $item_name）",
    "market_name": "市场 slug（如 id50381_f_16xl_usa）",
    "content": "该件物品的可读文本块（多行）",
    "sell": "最低售价（数字，GJN）",
    "buy": "最高求购（数字，GJN）",
    "sell_text": "最低售价（两位小数文本，别名 $sell_lowest_price）",
    "buy_text": "最高求购（两位小数文本，别名 $buy_highest_price）",
    "sell_change_amount": "最低售价较上次的变化量（带符号，两位小数）",
    "sell_change_rate": "最低售价变化率（带符号，如 -25% / +2.3%）",
    "buy_change_amount": "最高求购较上次的变化量",
    "buy_change_rate": "最高求购变化率",
    "sell_delta": "最低售价变化（含箭头与百分比的描述文本）",
    "buy_delta": "最高求购变化（同上）",
    "sell_depth": "出售挂单量（别名 $sell_count）",
    "buy_depth": "求购挂单量（别名 $buy_count）",
    "sell_count_change_amount": "出售挂单量变化量",
    "sell_count_change_rate": "出售挂单量变化率",
    "buy_count_change_amount": "求购挂单量变化量",
    "buy_count_change_rate": "求购挂单量变化率",
    "url": "商品页链接",
    "image": "**商品页快照**（图片附件；需要现拍时会等它渲染完再一起发）",
    "icon": "物品图标 URL",
    "tags": "标签（顿号分隔，如 type:aircraft、quality:ultraRare）",
    "tags_json": "标签数组",
    "rarity": "稀有度（由标签推导，如 超稀有）",
    "color": "稀有度颜色（如 C816C1）",
    "reasons": "本轮新触发的阈值文案（顿号分隔）",
    "reasons_json": "阈值文案数组",
    "reason": "第一条阈值文案",
    "kind": "物品类型（如 COMMODITY）",
    "error": "该物品的抓取错误（正常为空串）",
    "item_time": "该物品行情时间（YYYY-MM-DD HH:MM:SS）",
    "item_timestamp": "该物品行情时间（Unix 秒）",
}

KNOWN_FIELDS = frozenset(ROUND_FIELDS) | frozenset(ITEM_FIELDS)

_PLACEHOLDER = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
#: 常见粘贴事故：中文/智能引号
_SMART_QUOTES = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\uff02": '"', "\uff07": "'"}
#: 常见粘贴事故：数组/对象末尾多一个逗号
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def normalize_json_text(raw: str) -> str:
    """对常见粘贴错误做宽容处理（智能引号、末尾多余逗号）。"""
    text = raw.strip().lstrip("\ufeff").replace("\u200b", "")
    for bad, good in _SMART_QUOTES.items():
        text = text.replace(bad, good)
    return _TRAILING_COMMA.sub(r"\1", text)


def load_skeleton(raw: str) -> tuple[dict | None, str]:
    """解析 JSON 骨架；返回 (对象, 错误说明)。宽容常见的粘贴错误。"""
    original = raw.strip().lstrip("\ufeff").replace("\u200b", "")
    candidates = [original]
    normalized = normalize_json_text(raw)
    if normalized != original:
        candidates.append(normalized)
    last_error = ""
    for text in candidates:
        try:
            parsed = json.loads(text)
        except Exception as exc:
            last_error = str(exc)
            continue
        if isinstance(parsed, dict):
            return parsed, ""
        last_error = "顶层不是 JSON 对象（应以 { 开头）"
    return None, last_error


@dataclass
class RenderedMessage:
    """一次 payload 渲染的产物（正文 + 需要一起发出的图片）。"""

    lines: list[str] = field(default_factory=list)
    #: 直接可用的图片来源（本地路径或 URL）
    images: list[str] = field(default_factory=list)
    #: 需要现拍一张商品页快照
    needs_snapshot: bool = False
    used_mention: bool = False
    unknown_fields: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def text(self) -> str:
        return "\n".join([line for line in self.lines if line])

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.lines or self.images or self.needs_snapshot)

    @property
    def empty(self) -> bool:
        return not self.lines and not self.images and not self.needs_snapshot


def single_line(text: str) -> str:
    """把文本压成一行（数组元素内部禁止换行）。"""
    return _MULTI_SPACE.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()


def _resolve(name: str) -> str:
    return ALIASES.get(name, name)


def substitute(template: str, context: dict[str, Any]) -> tuple[str, bool, list[str]]:
    """替换 `$变量`，返回 (文本, 是否用了 @, 未知字段列表)。"""
    unknown: list[str] = []
    used_mention = False

    def _do(match: re.Match) -> str:
        nonlocal used_mention
        if match.group(0) == "$$":
            return "$"
        raw_name = match.group(1)
        name = _resolve(raw_name)
        if name not in KNOWN_FIELDS:
            unknown.append(raw_name)
        if name == "mention":
            used_mention = True
        value = context.get(name, "")
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    try:
        text = _PLACEHOLDER.sub(_do, template)
    except Exception:  # 模板再离谱也不该炸掉推送
        return "", False, []
    return text, used_mention, sorted(set(unknown))


def render_message(template: str, context: dict[str, Any]) -> RenderedMessage:
    """渲染一条消息。

    以 `{` 开头 = JSON 骨架（解析失败**不会**原样吐出模板，而是明确报错并回退）；
    其它形态 = 纯文本模板，按行替换。
    """
    raw = (template or "").strip()
    if not raw:
        return RenderedMessage()

    if raw.startswith("{"):
        skeleton, parse_error = load_skeleton(raw)
        if skeleton is None:
            return RenderedMessage(error=f"JSON 解析失败：{parse_error}（常见原因：多了一个逗号、用了中文引号）")
        return _render_skeleton(skeleton, context)

    text, used_mention, unknown = substitute(raw, context)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return RenderedMessage(error="模板渲染结果为空", used_mention=used_mention)
    return RenderedMessage(lines=lines, used_mention=used_mention, unknown_fields=unknown)


def _render_skeleton(skeleton: dict[str, Any], context: dict[str, Any]) -> RenderedMessage:
    """按 JSON 的书写顺序逐段取值并拼行。"""
    result = RenderedMessage()
    unknown: list[str] = []

    def _consume(line: str, used_mention: bool, names: list[str]) -> str:
        nonlocal unknown
        unknown.extend(names)
        if used_mention:
            result.used_mention = True
        return line

    for key, raw_value in skeleton.items():
        lowered = str(key).lower()

        # ---- 图片类键：附件（$image 特指商品页快照，需现拍）----
        if lowered in IMAGE_KEYS:
            if isinstance(raw_value, str):
                value, used_mention, names = substitute(raw_value, context)
                _consume("", used_mention, names)
                value = value.strip()
                if value:
                    result.images.append(value)
                elif re.search(r"\$image(?![A-Za-z0-9_])", raw_value):
                    # 还没拍到：告诉调用方“这一条需要现拍一张，拍完再一起发”
                    result.needs_snapshot = True
            continue

        # ---- @ 类键 ----
        if lowered in MENTION_KEYS:
            if isinstance(raw_value, str):
                value, used_mention, names = substitute(raw_value, context)
                _consume("", used_mention, names)
                if value.strip():
                    result.used_mention = True
                    result.lines.append(value.strip())
            continue

        # ---- 数组：每个元素一行，元素内不换行 ----
        if isinstance(raw_value, list):
            for item in raw_value:
                if not isinstance(item, str):
                    item = json.dumps(item, ensure_ascii=False)
                value, used_mention, names = substitute(item, context)
                _consume("", used_mention, names)
                value = single_line(value)
                if value:
                    result.lines.append(value)
            continue

        if isinstance(raw_value, str):
            value, used_mention, names = substitute(raw_value, context)
            _consume("", used_mention, names)
            for line in value.splitlines() or [""]:
                if line.strip():
                    result.lines.append(line.rstrip())
            continue

        # ---- 其它类型：原样转成文本 ----
        if raw_value is not None:
            result.lines.append(json.dumps(raw_value, ensure_ascii=False))

    result.unknown_fields = sorted(set(unknown))
    if result.empty:
        result.error = "模板渲染结果为空"
    return result


def validate_payload(template: str) -> tuple[bool, str]:
    """配置体检：返回 (是否可用, 说明)。"""
    if not template or not template.strip():
        return True, "未配置（使用默认可读文本）"
    if template.strip().startswith("{"):
        skeleton, parse_error = load_skeleton(template)
        if skeleton is None:
            return False, f"JSON 解析失败：{parse_error}（常见原因：多了一个逗号、用了中文引号）"
    sample: dict[str, Any] = {name: name for name in KNOWN_FIELDS}
    sample.update(
        {
            "timestamp": 1790220000,
            "count": 2,
            "sell": 45.0,
            "buy": 42.0,
            "sell_text": "45.00",
            "buy_text": "42.00",
            "sell_depth": 570,
            "buy_depth": 2168,
            "sell_change_amount": "+1.00",
            "sell_change_rate": "+2.3%",
            "buy_change_amount": "0",
            "buy_change_rate": "0%",
            "sell_count_change_amount": "+10",
            "sell_count_change_rate": "+1.8%",
            "buy_count_change_amount": "-5",
            "buy_count_change_rate": "-0.2%",
            "image": "",
        }
    )
    for alias, target in ALIASES.items():
        sample[alias] = sample.get(target, "")
    model = render_message(template, sample)
    notes = []
    if model.unknown_fields:
        notes.append("含未知字段：" + "、".join(model.unknown_fields))
    if model.error:
        notes.append(model.error)
    kind = "JSON 骨架" if not model.error else "模板"
    if notes:
        return not model.error, f"{kind}；" + "；".join(notes)
    return True, f"{kind}，可用（{len(model.lines)} 行" + (
        "、含图片" if model.images or model.needs_snapshot else ""
    ) + "）"
