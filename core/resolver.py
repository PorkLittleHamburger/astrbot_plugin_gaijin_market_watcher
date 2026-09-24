"""物品定位层：把用户输入（URL / market_name / 名称）解析为候选物品。

Gaijin Market 中物品的唯一标识是 market_name，也就是物品页 URL 的最后一段，
以及搜索接口返回的 hash_name。例如：

    https://trade.gaijin.net/market/1067/id50381_f_16xl_usa
    -> appid = "1067", market_name = "id50381_f_16xl_usa"

【重要实测结论】搜索接口 cln_market_search 是模糊匹配，搜 "F-16XL" 会返回
F-14A / 吕贝克 / 喷火 等无关物品。因此本模块采取双路径策略：

  1. URL / market_name 直查：置信度 100%，且会用盘口接口做有效性校验；
  2. 名称搜索：必须配合本地相似度打分，并把 TopN 交给用户确认。

本模块只做"解析与打分"，不做任何推送、持久化。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

#: 物品页 URL，兼容带/不带 appid、带/不带协议
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?trade\.gaijin\.net/market/(?:(\d+)/)?([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)
#: market_name 允许的字符集
MARKET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
#: 站点会在中文名里插入零宽字符，必须清掉再做比较
ZW_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
#: 分词用的分隔符（含各类中英文括号）
SPLIT_RE = re.compile(r"[\s\-_/|,（）()\[\]【】]+")


def clean_text(text: str | None) -> str:
    """清除零宽字符与首尾空白。"""
    if not text:
        return ""
    return ZW_RE.sub("", str(text)).strip()


def normalize(text: str | None) -> str:
    """归一化：清零宽 + 压空白 + 转小写。"""
    return re.sub(r"\s+", " ", clean_text(text)).lower()


def looks_like_market_name(token: str) -> bool:
    """判断一个裸词是否像 market_name。

    market_name 的特征：不含空格、以 空格/连字符 之外的字符组成，且用下划线分词，
    例如 ugcitem_1002267、id50381_f_16xl_usa；
    而人类可读名称通常带空格、连字符或中日韩文字。
    """
    token = clean_text(token)
    if not token or len(token) < 4 or " " in token:
        return False
    if not MARKET_NAME_RE.match(token):
        return False
    if re.search(r"[\u4e00-\u9fff]", token):
        return False
    return "_" in token


def extract_reference(text: str) -> tuple[str | None, str | None]:
    """从任意输入中提取 (appid, market_name)。识别失败返回 (None, None)。"""
    text = clean_text(text)
    if not text:
        return None, None
    matched = URL_RE.search(text)
    if matched:
        return (matched.group(1) or None), matched.group(2)
    if looks_like_market_name(text):
        return None, text
    return None, None


def prettify_market_name(market_name: str) -> str:
    """把 ID 变成可读文本，作为显示名兜底。

    id50381_f_16xl_usa -> "F 16xl Usa"
    """
    text = clean_text(market_name).replace("_", " ")
    text = re.sub(r"^id\d+\s*", "", text, flags=re.IGNORECASE)
    return text.strip().title() or clean_text(market_name)


def score_name(query: str, name: str) -> float:
    """给"搜索关键词"与"候选物品名"打分，范围 0~1。

    组成：0.5 * 编辑相似度 + 0.25 * 是否包含 + 0.25 * 关键词覆盖率。
    """
    query_n, name_n = normalize(query), normalize(name)
    if not query_n or not name_n:
        return 0.0
    if query_n == name_n:
        return 1.0

    ratio = SequenceMatcher(None, query_n, name_n).ratio()
    containment = 1.0 if query_n in name_n else 0.0
    tokens = [t for t in SPLIT_RE.split(query_n) if len(t) >= 2]
    coverage = sum(1 for t in tokens if t in name_n) / len(tokens) if tokens else 0.0

    score = 0.5 * ratio + 0.25 * containment + 0.25 * coverage
    if name_n.startswith(query_n):
        score += 0.05
    return round(min(1.0, score), 4)
