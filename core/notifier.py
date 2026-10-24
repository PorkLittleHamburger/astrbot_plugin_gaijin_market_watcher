"""推送层：负责「判断要不要推」「渲染成人看的文本」「@ 到正确的人」。

刷新模式：
  always       按订阅者设置的间隔轮询并推送，即使价格没有变化
  only_change  固定每 5 秒轮询，只有售价/求购价变化时推送

阈值采用「边沿触发」而非「电平触发」：
  只有当某个阈值从「不满足」变为「满足」的那一刻才告警；
  若价格持续低于阈值，不会每轮重复刷屏（刻意设计，避免打扰）。
  判定无需额外状态：把上一轮行情重新算一遍阈值，比对差集即可。

投递对象：
  * 私聊按订阅者发送；
  * 群聊按物品聚合并 @ 群内全部订阅者；
  * 抓取失败等异常仍通知拥有者。

@ 的实现（2026-09-24 真机验证，已固化为唯一策略，不再暴露配置项）：
  * **QQ 官方机器人（qq_official）不支持 At 组件** —— AstrBot 该适配器出站只取
    get_plain_text()，At 会被静默丢弃（不报错、也不生效）。
  * 官方做法：把 @ 以**文本内嵌标记**写进消息，而且**必须走 markdown 通道**
    （纯文本 content 会被客户端原样显示成尖括号字面量，已实测失败）。
    标记：<qqbot-at-user id="{uid}" />    ← 群聊可用；旧协议 <@{uid}> 已标注即将弃用
  * 其它平台（Telegram / QQ 个人号等）用标准 At 组件。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace

from . import platforms
from .charts import render_price_chart
from .constants import PUSH_ALWAYS, PUSH_ON_TRIGGER
from .models import MonitorSpec, Quote, SnapshotItem, Subscription, TriggerEvent, fmt_delta, fmt_price
from .payload import TRADE_KNOWN_FIELDS, render_message

#: 分隔行：真正的空行会被 QQ 的 markdown 渲染折叠掉，
#: 所以放一个零宽空格（U+200B）—— 它不是空白字符，str.strip()/客户端 trim 都删不掉，
#: 渲染出来就是一行空白。
_BLANK_LINE = "\u200b"
BLOCK_SEPARATOR = f"\n{_BLANK_LINE}\n"


@dataclass(slots=True)
class DeliveryBlock:
    """一条聚合消息中的单个物品文本块。"""

    text: str
    title: str = ""
    mentions: list[tuple[str, str]] = field(default_factory=list)
    images: list[str | bytes] = field(default_factory=list)
    used_mention: bool = False


def threshold_hits(spec: MonitorSpec, quote: Quote | None) -> dict[str, str]:
    """返回当前满足的阈值，形如 {"sell": "最低售价 39.00 <= 阈值 100.00"}。

    用固定 key（sell / buy）标识"哪条阈值"，文案里带价格，
    这样即使价格变动也能正确识别出"是不是同一条阈值被触发"。
    """
    hits: dict[str, str] = {}
    if quote is None or not quote.ok:
        return hits
    if spec.sell_below > 0 and quote.sell_min is not None and quote.sell_min <= spec.sell_below:
        hits["sell"] = f"最低售价 {fmt_price(quote.sell_min)} <= 阈值 {fmt_price(spec.sell_below)}"
    if spec.buy_above > 0 and quote.buy_max is not None and quote.buy_max >= spec.buy_above:
        hits["buy"] = f"最高求购 {fmt_price(quote.buy_max)} >= 阈值 {fmt_price(spec.buy_above)}"
    return hits


def _price_changed(new: float | None, old: float | None) -> bool:
    if old is None:
        return True  # 首次观测视为变化，用于建立基线
    if new is None:
        return False
    return abs(new - old) > 1e-9


_RARITY_NAMES = {
    "common": "普通",
    "uncommon": "罕见",
    "rare": "稀有",
    "veryrare": "非常稀有",
    "ultrarare": "超稀有",
    "legendary": "传说",
}


def _rarity_of(tags: list[str]) -> str:
    """从标签里解析稀有度（quality:ultraRare -> 超稀有）。"""
    for tag in tags:
        if tag.lower().startswith("quality:"):
            value = tag.split(":", 1)[1]
            return _RARITY_NAMES.get(value.lower(), value)
    return ""


def _signed_change(old: float | None, new: float | None, *, decimals: int = 2, unit: str = "") -> str:
    """带符号的变化量文本；无法比较时返回空串。"""
    if old is None or new is None:
        return ""
    diff = new - old
    if abs(diff) < 10 ** (-(decimals + 1)):
        return "0" + unit
    return f"{diff:+.{decimals}f}{unit}"


def _change_rate(old: float | None, new: float | None, *, decimals: int = 1) -> str:
    """带符号的变化率文本（如 +2.3%）；无法比较时返回空串。"""
    if old in (None, 0) or new is None:
        return ""
    return f"{(new - old) / old * 100:+.{decimals}f}%"


class Notifier:
    def __init__(self, context, storage, config, logger, *, subscriptions=None, output=None, accounts=None) -> None:
        self.context = context
        self.storage = storage
        self.config = config
        self.logger = logger
        self.subscriptions = subscriptions
        self.output = output
        self._payload_warned = False
        self.accounts = accounts
        #: 出站失败通知限频（umo -> 上次通知时间）
        self._send_failure_at: dict[str, float] = {}
        #: 推送合并缓冲：(umo, uid) -> {uid, name, events, failures, deadline, on_delivered}
        #: 群聊中多个订阅者共享 UMO，必须继续按 UID 分开，否则会丢失 @。
        self._pending: dict[tuple[str, str], dict] = {}
        self._flush_task: asyncio.Task | None = None
        self._chart_cache: dict[tuple[str, int, float, int, str], bytes] = {}

    # ------------------------------------------------------------ 会话解析

    def _guess_private_session(self, uid: str) -> str:
        """该 UID 尚未在本机器人上出现时的兜底：按已知平台拼一个私聊会话。

        只有在平台标识唯一确定时才这么做，否则宁可"未解析"也不乱发。
        """
        known = self.storage.known_platforms()
        if len(known) == 1:
            return f"{next(iter(known))}:FriendMessage:{uid}"
        return ""

    def session_of(self, uid: str) -> str:
        """把 UID 解析成会话（推送 / @ 的前提）。"""
        return self.storage.resolve_uid(uid) or self._guess_private_session(uid)

    def owner_sessions(self, owner_uids: list[str] | None = None) -> list[str]:
        """拥有者 UID -> 会话列表（全局监控项的通知目标）。"""
        uids = (
            owner_uids
            if owner_uids is not None
            else (self.accounts.owner_uids if self.accounts else getattr(self.config, "owner_uids", []))
        )
        umos: list[str] = []
        for uid in uids:
            resolved = self.session_of(uid)
            if resolved and resolved not in umos:
                umos.append(resolved)
        return umos

    async def _platform_type(self, umo: str) -> str:
        """查出该会话所属平台的适配器类型（如 qq_official / telegram）。

        @ 的实现方式与平台强相关，所以必须先知道是哪个平台。
        """
        platform_id = (umo or "").split(":", 1)[0]
        if not platform_id:
            return ""
        try:
            cfg = self.context.get_config()
            if inspect.isawaitable(cfg):
                cfg = await cfg
            for item in (cfg or {}).get("platform", []) or []:
                if str(item.get("id")) == platform_id:
                    return str(item.get("type") or "")
        except Exception as exc:
            self.logger.debug(f"读取平台类型失败: {exc}")
        return ""

    # ------------------------------------------------------------ 推送判定

    def evaluate(
        self, spec: MonitorSpec, quote: Quote, previous: Quote | None, *, mode: str = ""
    ) -> TriggerEvent | None:
        """判断这条监控是否应该推送，返回事件对象或 None。"""
        if not quote.ok:
            return None

        hits_now = threshold_hits(spec, quote)
        hits_before = threshold_hits(spec, previous)
        # 新触发的阈值 = 本轮满足 - 上轮已满足
        new_hits = {k: v for k, v in hits_now.items() if k not in hits_before}

        changed = _price_changed(quote.sell_min, previous.sell_min if previous else None) or _price_changed(
            quote.buy_max, previous.buy_max if previous else None
        )

        effective_mode = mode or self.config.push_mode
        if effective_mode == PUSH_ALWAYS:
            should_push = True
        elif effective_mode == PUSH_ON_TRIGGER:
            should_push = bool(new_hits)
        else:  # PUSH_ON_CHANGE / REFRESH_ONLY_CHANGE
            should_push = changed or bool(new_hits)

        if not should_push:
            return None

        return TriggerEvent(
            spec=spec,
            quote=quote,
            previous=previous,
            reasons=list(new_hits.values()),
            active_hits=hits_now,
            mode=effective_mode,
        )

    def evaluate_subscription(self, sub: Subscription, quote: Quote, previous: Quote | None) -> TriggerEvent | None:
        """用户订阅的判定：复用全局规则，但阈值来自订阅记录本身。"""
        return self.evaluate(sub.to_spec(), quote, previous, mode=sub.mode)

    # ------------------------------------------------------------ 文本渲染

    def render_digest(
        self,
        events: list[TriggerEvent],
        failures: list[Quote] | None = None,
        *,
        title: str = "Gaijin 行情",
    ) -> str:
        """把多个事件渲染成一条消息。"""
        blocks: list[str] = [f"{title} · {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        for event in events:
            blocks.append(self._render_item(event))
        for quote in failures or []:
            blocks.append(f"{quote.title}\n  抓取失败：{quote.error}")
        # 各物品之间空一行（同上）
        return BLOCK_SEPARATOR.join(blocks)

    # ------------------------------------------------------- payload 上下文

    @staticmethod
    def _group_of(umo: str) -> str:
        """从 UMO 里取群 ID（私聊返回空串）。"""
        parts = (umo or "").split(":", 2)
        if len(parts) < 3 or "group" not in parts[1].lower():
            return ""
        return parts[2].rsplit("_", 1)[-1]

    def _item_context(self, event: TriggerEvent) -> dict:
        """单件物品在 payload 模板里的字段（含变化量与变化率）。"""
        quote, previous = event.quote, event.previous
        old_sell = previous.sell_min if previous else None
        old_buy = previous.buy_max if previous else None
        old_sell_depth = previous.sell_depth if previous else None
        old_buy_depth = previous.buy_depth if previous else None
        meta = self.storage.get_item_meta(quote.market_name) or {}
        tags = [str(tag) for tag in (meta.get("tags") or [])]
        icon = str(meta.get("icon") or "")
        return {
            "name": quote.title,
            "market_name": quote.market_name,
            "content": self._render_item(event),
            "sell": quote.sell_min,
            "buy": quote.buy_max,
            "sell_text": fmt_price(quote.sell_min),
            "buy_text": fmt_price(quote.buy_max),
            "sell_change_amount": _signed_change(old_sell, quote.sell_min),
            "buy_change_amount": _signed_change(old_buy, quote.buy_max),
            "sell_change_rate": _change_rate(old_sell, quote.sell_min),
            "buy_change_rate": _change_rate(old_buy, quote.buy_max),
            "sell_delta": fmt_delta(quote.sell_min, old_sell),
            "buy_delta": fmt_delta(quote.buy_max, old_buy),
            "sell_depth": quote.sell_depth,
            "buy_depth": quote.buy_depth,
            "sell_count_change_amount": _signed_change(old_sell_depth, quote.sell_depth, decimals=0),
            "buy_count_change_amount": _signed_change(old_buy_depth, quote.buy_depth, decimals=0),
            "sell_count_change_rate": _change_rate(old_sell_depth, quote.sell_depth),
            "buy_count_change_rate": _change_rate(old_buy_depth, quote.buy_depth),
            "url": f"https://trade.gaijin.net/market/{quote.app_id or self.config.appid}/{quote.market_name}",
            "icon": icon,
            "tags": "、".join(tags),
            "tags_json": tags,
            "rarity": _rarity_of(tags),
            "color": str(meta.get("color") or ""),
            "reasons": "、".join(event.reasons),
            "reasons_json": list(event.reasons),
            "reason": event.reasons[0] if event.reasons else "",
            "kind": quote.kind,
            "error": quote.error,
            "item_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(quote.timestamp or time.time())),
            "item_timestamp": int(quote.timestamp or time.time()),
        }

    def build_payload_context(
        self,
        events: list[TriggerEvent],
        failures: list[Quote] | None,
        *,
        umo: str = "",
        uid: str = "",
        user_name: str = "",
        mention: str = "",
        platform: str = "",
        total_items: int | None = None,
    ) -> dict:
        """组装 payload 模板可用到的全部字段（整轮 + 本轮主物品的快捷字段）。"""
        now = time.time()
        items = [self._item_context(event) for event in events]
        context: dict = {
            "title": "Gaijin 行情",
            #: 按你示例：2026-09-24-17-32-33（全横线）
            "datetime": time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(now)),
            #: 常规写法：2026-09-24 17:32:33
            "datetime_plain": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "timestamp": int(now),
            "digest": self.render_digest(events, failures, title="Gaijin 行情"),
            "content": self._render_item(events[0]) if len(events) == 1 else "",
            "count": len(items) if total_items is None else total_items,
            "uid": uid,
            "user": user_name,
            "group": self._group_of(umo),
            "platform": platform,
            "mention": mention,
            "mode": events[0].mode if events else self.config.push_mode,
            "failures": "、".join(quote.title for quote in (failures or [])),
            "failures_json": [{"name": q.title, "error": q.error} for q in (failures or [])],
            "appid": self.config.appid,
        }
        primary = next((event for event in events if event.reasons), events[0] if events else None)
        if primary is not None:
            context.update(self._item_context(primary))
        return context

    def render_trade_event(self, event) -> str:
        """使用独立的交易模板渲染 TradeEvent。"""
        template = (getattr(self.output, "trade_payload", "") or "").strip()
        fallback = f"【GJM 自动交易】{event.message}"
        if not template:
            return fallback
        context = dict(event.details or {})
        context.setdefault("trade_kind", event.kind)
        context.setdefault("trade_level", event.level)
        context.setdefault("trade_message", event.message)
        model = render_message(template, context, known_fields=TRADE_KNOWN_FIELDS)
        if model.ok:
            return model.text
        if not self._payload_warned:
            self._payload_warned = True
            self.logger.warning(f"[GJM] 交易输出模板渲染失败，已回退文本：{model.error}")
        return fallback

    def _render_item(self, event: TriggerEvent) -> str:
        quote, previous = event.quote, event.previous
        old_sell = previous.sell_min if previous else None
        old_buy = previous.buy_max if previous else None

        lines = [quote.title]
        lines.append(f"  售出 {fmt_price(quote.sell_min)} GJN  {fmt_delta(quote.sell_min, old_sell)}")
        lines.append(f"  求购 {fmt_price(quote.buy_max)} GJN  {fmt_delta(quote.buy_max, old_buy)}")
        lines.append(f"  挂单量 售 {quote.sell_depth} / 求 {quote.buy_depth}")

        # 本轮新触发的阈值
        for reason in event.reasons:
            lines.append(f"  >> 阈值：{reason}")

        return "\n".join(lines)

    # ------------------------------------------------------------ 发送

    async def _chart_for(self, market_name: str, display_name: str) -> bytes | None:
        limit = max(1, int(getattr(self.config, "chart_history_points", 5) or 5))
        try:
            points = self.storage.get_price_history(market_name, limit=limit)
        except Exception as exc:
            self.logger.debug(f"[GJM] 读取图表历史失败（{market_name}）：{exc}")
            return None
        if not points:
            return None
        cache_key = (
            market_name,
            int(points[-1].get("t") or 0),
            float(points[-1].get("sell") or 0),
            limit,
            display_name,
        )
        cached = self._chart_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            image = await asyncio.to_thread(render_price_chart, display_name or market_name, points)
        except Exception as exc:
            self.logger.warning(f"[GJM] 价格图生成失败（{market_name}）：{exc}")
            return None
        if len(self._chart_cache) > 256:
            self._chart_cache.clear()
        self._chart_cache[cache_key] = image
        return image

    def _mention_plans(
        self,
        platform_type: str,
        mentions: list[tuple[str, str]],
        umo: str,
    ):
        if not self.config.mention_users:
            return []
        seen: set[str] = set()
        result = []
        for uid, name in mentions:
            if not uid or uid in seen:
                continue
            seen.add(uid)
            result.append((uid, name, platforms.mention_plan_for(platform_type, uid, name, umo=umo)))
        return result

    def _template_mention_text(self, plans) -> str:
        tokens = [
            plan.text
            for _, _, plan in plans
            if plan.style in (platforms.STYLE_MARKDOWN_TEXT, platforms.STYLE_PLAIN_TEXT)
        ]
        return "\n".join(token for token in tokens if token)

    async def _render_event_blocks(
        self,
        events: list[TriggerEvent],
        failures: list[Quote] | None,
        *,
        umo: str,
        uid: str,
        user_name: str,
        platform_type: str,
        mentions_by_event: dict[int, list[tuple[str, str]]] | None = None,
    ) -> list[DeliveryBlock]:
        mention_map = mentions_by_event or {}
        template = (getattr(self.output, "quote_payload", "") or getattr(self.config, "payload", "") or "").strip()
        base = self.build_payload_context(
            events,
            failures,
            umo=umo,
            uid=uid,
            user_name=user_name,
            mention="",
            platform=platform_type,
            total_items=len(events),
        )
        rendered: list[DeliveryBlock] = []
        item_blocks: list[DeliveryBlock] = []
        problem = ""
        unknown: list[str] = []

        if template:
            for index, event in enumerate(events):
                mentions = mention_map.get(index, [])
                plans = self._mention_plans(platform_type, mentions, umo)
                context = dict(base)
                context.update(self._item_context(event))
                context["mention"] = self._template_mention_text(plans)
                piece = render_message(template, context)
                if not piece.ok:
                    problem = piece.error
                    break
                unknown = piece.unknown_fields or unknown
                text = piece.text or self._render_item(event)
                block = DeliveryBlock(
                    text=text,
                    title=event.quote.title,
                    mentions=mentions,
                    images=list(piece.images),
                    used_mention=piece.used_mention,
                )
                item_blocks.append(block)
                rendered.append(block)

        if not template or problem or len(rendered) != len(events):
            rendered = [
                DeliveryBlock(
                    text=f"Gaijin 行情 · {time.strftime('%Y-%m-%d %H:%M:%S')}",
                    title="Gaijin 行情",
                )
            ]
            item_blocks = [
                DeliveryBlock(
                    text=self._render_item(event),
                    title=event.quote.title,
                    mentions=mention_map.get(index, []),
                )
                for index, event in enumerate(events)
            ]
            rendered.extend(item_blocks)
            if problem and not self._payload_warned:
                self._payload_warned = True
                self.logger.warning(f"[GJM] payload 渲染失败，已回退为可读文本：{problem}")
        elif unknown and not self._payload_warned:
            self._payload_warned = True
            self.logger.warning("[GJM] payload 含未知字段（已按空值处理）：" + "、".join(unknown))

        for block in rendered:
            if block.mentions and not block.text:
                block.text = "Gaijin 行情"

        for event, block in zip(events, item_blocks, strict=False):
            chart = await self._chart_for(event.quote.market_name, event.quote.title)
            if chart is not None:
                block.images.append(chart)

        for quote in failures or []:
            rendered.append(DeliveryBlock(text=f"{quote.title}\n  抓取失败：{quote.error}", title=quote.title))
        return rendered

    def _append_image(self, chain, source: str | bytes) -> None:
        from astrbot.api.message_components import Image

        if isinstance(source, bytes):
            maker = getattr(Image, "fromBytes", None)
            if callable(maker):
                chain.chain.append(maker(source))
            else:
                chain.chain.append(Image.fromBase64(base64.b64encode(source).decode()))
            return
        if source.startswith(("http://", "https://")):
            chain.chain.append(Image.fromURL(source))
        else:
            chain.chain.append(Image.fromFileSystem(source))

    async def _send_blocks(self, umo: str, blocks: list[DeliveryBlock]) -> bool:
        """按物品顺序发送文本、@ 与图片。"""
        if not blocks:
            return False
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import At, Plain

        platform_type = await self._platform_type(umo) if umo else ""
        total_images = sum(len(block.images) for block in blocks)
        prepared = [(block, self._mention_plans(platform_type, block.mentions, umo)) for block in blocks]
        has_native_group_mentions = any(
            plan.style == platforms.STYLE_MARKDOWN_TEXT for _block, plans in prepared for _uid, _name, plan in plans
        )
        defer_images = platform_type in platforms.ONE_IMAGE_PLATFORMS and (
            total_images > 1 or (total_images > 0 and has_native_group_mentions)
        )
        chain = MessageChain()
        use_markdown = False
        for index, (block, plans) in enumerate(prepared):
            if index > 0:
                chain.chain.append(Plain(BLOCK_SEPARATOR))
            if any(plan.style == platforms.STYLE_MARKDOWN_TEXT for _, _, plan in plans):
                use_markdown = True
            if plans and all(plan.style == platforms.STYLE_AT_COMPONENT for _, _, plan in plans):
                for uid, name, _plan in plans:
                    chain.chain.append(At(qq=uid, name=name or None))
                    chain.chain.append(Plain(" "))
            elif plans and not block.used_mention:
                mention_text = self._template_mention_text(plans)
                if mention_text:
                    block.text = f"{mention_text}\n{block.text}"
            if block.text:
                chain.chain.append(Plain(block.text))
            if not defer_images:
                for source in block.images:
                    try:
                        self._append_image(chain, source)
                    except Exception as exc:
                        self.logger.debug(f"[GJM] 附加图片失败（{source!r}）：{exc}")
        if use_markdown:
            setter = getattr(chain, "use_markdown", None)
            if callable(setter):
                setter(True)
            else:
                chain.use_markdown_ = True
        try:
            sent = bool(await self.context.send_message(umo, chain))
        except Exception as exc:
            self.logger.warning(f"推送到 {umo} 失败: {exc}")
            return False
        delivery_ok = sent
        if sent and defer_images:
            for block in blocks:
                for source in block.images:
                    if not await self._send_block_image(umo, block.title, source):
                        self.logger.warning(f"[GJM] 图表补发失败（{block.title or umo}）")
                        delivery_ok = False
        return delivery_ok

    async def _send_block_image(self, umo: str, title: str, source: str | bytes) -> bool:
        """QQ 官方单消息只支持一张图时，为物品单独补发图片。"""
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain

        chain = MessageChain()
        if title:
            chain.chain.append(Plain(title))
        try:
            self._append_image(chain, source)
        except Exception as exc:
            self.logger.warning(f"构造图片补发消息失败（{title or umo}）：{exc}")
            return False
        try:
            return bool(await self.context.send_message(umo, chain))
        except Exception as exc:
            self.logger.warning(f"图片补发到 {umo} 失败: {exc}")
            return False

    def _build_chain(self, text: str, mention_uid: str = "", mention_name: str = ""):
        """构造单段纯文本消息链，供 owner 通知和兼容路径使用。"""
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import At, Plain

        chain = MessageChain()
        if mention_uid and self.config.mention_users:
            chain.chain.append(At(qq=mention_uid, name=mention_name or None))
            chain.chain.append(Plain(" "))
        chain.chain.append(Plain(text))
        return chain

    async def _send_plain(
        self,
        umo: str,
        text: str,
        *,
        markdown: bool | None = None,
        images: list[str] | None = None,
    ) -> bool:
        """发送纯文本（不含任何富媒体组件）。

        markdown=True 会强制走 markdown 消息 —— QQ 官方的文本内嵌 @ 标记只在
        markdown 通道里被解析，所以这里必须显式指定，不能依赖平台默认值。
        """
        chain = self._build_chain(text)
        for source in images or []:
            try:
                from astrbot.api.message_components import Image

                url = str(source)
                if url.startswith(("http://", "https://")):
                    chain.chain.append(Image.fromURL(url))
                else:
                    chain.chain.append(Image.fromFileSystem(url))
            except Exception as exc:
                self.logger.debug(f"附加 payload 图片失败（{source}）：{exc}")
        if markdown is not None:
            setter = getattr(chain, "use_markdown", None)
            if callable(setter):
                setter(markdown)
            else:  # 兜底：直接写字段
                chain.use_markdown_ = markdown
        try:
            return bool(await self.context.send_message(umo, chain))
        except Exception as exc:
            self.logger.warning(f"推送到 {umo} 失败: {exc}")
            return False

    async def _send(
        self,
        umo: str,
        text: str,
        mention_uid: str = "",
        mention_name: str = "",
        *,
        events: list[TriggerEvent] | None = None,
        failures: list[Quote] | None = None,
        uid: str = "",
    ) -> bool:
        """向单个会话发送，并按平台选择正确的 @ 方式（策略已固化，无配置项）。

        配置了 payload 时正文改由模板渲染：JSON 骨架按书写顺序逐行取值，
        多件物品逐件渲染后拼接；任一份渲染失败都整体回退到 text。
        """
        if events:
            platform_type = await self._platform_type(umo) if umo else ""
            mentions_by_event = {0: [(mention_uid, mention_name)]} if mention_uid else {}
            blocks = await self._render_event_blocks(
                events,
                failures,
                umo=umo,
                uid=uid or mention_uid,
                user_name=mention_name,
                platform_type=platform_type,
                mentions_by_event=mentions_by_event,
            )
            return await self._send_blocks(umo, blocks)
        return await self._send_plain(umo, text)

    async def _notify_send_failure(self, umo: str) -> None:
        """出站失败时告知拥有者（同一会话限频 30 分钟），避免推送悄悄丢失。"""
        now = time.time()
        if now - self._send_failure_at.get(umo, 0.0) < 1800:
            return
        self._send_failure_at[umo] = now
        owners = self.owner_sessions()
        if not owners:
            return
        platform_type = await self._platform_type(umo)
        note = platforms.PROACTIVE_NOTES.get(platform_type, "")
        text = (
            f"推送到 {umo} 失败。\n"
            + (f"该平台限制：{note}\n" if note else "")
            + "常见原因：平台不支持主动推送 / 上下文令牌失效（微信需用户先给机器人发一条消息）/ 会话已失效。"
        )
        for owner in owners:
            with suppress(Exception):
                await self._send_plain(owner, text)

    async def push(self, text: str, umos: list[str] | None = None) -> int:
        """向指定会话列表推送，返回成功条数。

        目标必须显式给出（订阅者由 push_to_subscribers 负责，这里服务拥有者通知）。
        """
        targets = [u for u in (umos or []) if u]
        if not targets:
            self.logger.info("本次推送没有目标会话，已跳过。")
            return 0
        sent = 0
        for umo in targets:
            sent += 1 if await self._send(umo, text) else 0
        return sent

    async def _deliver(
        self,
        uid: str,
        umo: str,
        events: list[TriggerEvent],
        failures: list[Quote] | None,
        user_name: str,
    ) -> bool:
        """投递给单个订阅者。"""
        text = self.render_digest(events, failures, title="Gaijin 行情")
        sent = await self._send(
            umo,
            text,
            mention_uid=uid,
            mention_name=user_name,
            events=events,
            failures=failures,
            uid=uid,
        )
        if not sent:
            await self._notify_send_failure(umo)
        return sent

    async def push_group_snapshot(self, umo: str, items: list[SnapshotItem]) -> bool:
        """向一个群发送完整行情快照；每件物品单独 @、正文和图表。"""
        if not items:
            return False
        platform_type = await self._platform_type(umo) if umo else ""
        template = (getattr(self.output, "quote_payload", "") or getattr(self.config, "payload", "") or "").strip()
        events = [
            TriggerEvent(
                spec=MonitorSpec(
                    market_name=item.market_name,
                    display_name=item.display_name,
                    enabled=True,
                ),
                quote=item.quote,
                previous=item.previous,
                mode=PUSH_ALWAYS,
            )
            for item in items
            if item.quote is not None
        ]
        base = self.build_payload_context(
            events,
            None,
            umo=umo,
            platform=platform_type,
            total_items=len(items),
        )
        blocks: list[DeliveryBlock] = []
        problem = ""
        unknown: list[str] = []
        for item in items:
            plans = self._mention_plans(platform_type, item.mentions, umo)
            mention_text = self._template_mention_text(plans)
            text = ""
            images: list[str] = []
            used_mention = False
            if item.quote is not None:
                event = TriggerEvent(
                    spec=MonitorSpec(item.market_name, item.display_name, enabled=True),
                    quote=item.quote,
                    previous=item.previous,
                    mode=PUSH_ALWAYS,
                )
                if template:
                    context = dict(base)
                    context.update(self._item_context(event))
                    context["mention"] = mention_text
                    piece = render_message(template, context)
                    if piece.ok:
                        text = piece.text or self._render_item(event)
                        images = list(piece.images)
                        used_mention = piece.used_mention
                        unknown = piece.unknown_fields or unknown
                    else:
                        problem = piece.error
                        text = self._render_item(event)
                else:
                    text = self._render_item(event)
                if item.error:
                    text += f"\n  >> 本次刷新失败：{item.error}"
            else:
                text = f"{item.display_name or item.market_name}\n  暂无行情"
                if item.error:
                    text += f"（本次刷新失败：{item.error}）"
            chart = await self._chart_for(item.market_name, item.display_name)
            if chart is not None:
                images.append(chart)
            blocks.append(
                DeliveryBlock(
                    text=text,
                    title=item.display_name or item.market_name,
                    mentions=item.mentions,
                    images=images,
                    used_mention=used_mention,
                )
            )
        if problem:
            fallback_blocks: list[DeliveryBlock] = []
            for item, old_block in zip(items, blocks, strict=True):
                if item.quote is not None:
                    event = TriggerEvent(
                        spec=MonitorSpec(item.market_name, item.display_name, enabled=True),
                        quote=item.quote,
                        previous=item.previous,
                        mode=PUSH_ALWAYS,
                    )
                    text = self._render_item(event)
                    if item.error:
                        text += f"\n  >> 本次刷新失败：{item.error}"
                else:
                    text = f"{item.display_name or item.market_name}\n  暂无行情"
                    if item.error:
                        text += f"（本次刷新失败：{item.error}）"
                fallback_blocks.append(
                    DeliveryBlock(
                        text=text,
                        title=item.display_name or item.market_name,
                        mentions=item.mentions,
                        images=[image for image in old_block.images if isinstance(image, bytes)],
                    )
                )
            blocks = fallback_blocks
        if problem and not self._payload_warned:
            self._payload_warned = True
            self.logger.warning(f"[GJM] payload 渲染失败，已回退为可读文本：{problem}")
        elif unknown and not self._payload_warned:
            self._payload_warned = True
            self.logger.warning("[GJM] payload 含未知字段（已按空值处理）：" + "、".join(unknown))
        sent = await self._send_blocks(umo, blocks)
        if not sent:
            await self._notify_send_failure(umo)
        return sent

    # ------------------------------------------------------------ 推送合并

    @staticmethod
    def _merge_events(events: list[TriggerEvent]) -> list[TriggerEvent]:
        """同一物品在窗口内出现多次时只保留一条：价格取最新、基准取最早。"""
        merged: dict[str, TriggerEvent] = {}
        for event in events:
            key = event.quote.market_name
            previous = merged.get(key)
            if previous is None:
                merged[key] = event
                continue
            reasons = list(dict.fromkeys([*previous.reasons, *event.reasons]))
            merged[key] = replace(previous, quote=event.quote, reasons=reasons, active_hits=event.active_hits)
        return list(merged.values())

    def _buffer_push(
        self,
        uid: str,
        umo: str,
        events: list[TriggerEvent],
        failures: list[Quote] | None,
        user_name: str,
        window: int,
        on_delivered: Callable[[str, str, list[TriggerEvent]], None] | None = None,
    ) -> None:
        """把本次事件并入缓冲，等窗口到点后合成一条消息发出。"""
        key = (umo, uid)
        entry = self._pending.get(key)
        now = time.time()
        if entry is None:
            entry = {
                "uid": uid,
                "name": user_name,
                "events": [],
                "failures": [],
                "deadline": now + window,
                "on_delivered": on_delivered,
            }
            self._pending[key] = entry
        entry["uid"] = uid
        entry["name"] = user_name or entry.get("name", "")
        entry["events"].extend(events)
        entry["failures"].extend(failures or [])
        if on_delivered is not None:
            entry["on_delivered"] = on_delivered

    def _report_delivered(
        self,
        callback: Callable[[str, str, list[TriggerEvent]], None] | None,
        target_umo: str,
        uid: str,
        events: list[TriggerEvent],
    ) -> None:
        """送达后通知调用方推进推送基线。"""
        if callback is None:
            return
        try:
            callback(target_umo, uid, list(events))
        except Exception as exc:
            self.logger.warning(f"[GJM] 更新推送送达状态失败（{uid}）：{exc}")

    def _ensure_flush_task(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.create_task(self._flush_loop(), name="gjm-push-flush")
        except RuntimeError as exc:
            self.logger.debug(f"[GJM] 无法调度合并冲发任务：{exc}")

    async def _flush_loop(self) -> None:
        """到点就把缓冲里的内容合成一条消息发出去。"""
        try:
            while self._pending:
                now = time.time()
                earliest = min(entry["deadline"] for entry in self._pending.values())
                if earliest > now:
                    await asyncio.sleep(min(earliest - now, 1.0))
                    continue
                for key in [key for key, entry in self._pending.items() if entry["deadline"] <= now]:
                    umo, uid = key
                    entry = self._pending.pop(key, None)
                    if not entry or not entry["events"]:
                        continue
                    events = self._merge_events(entry["events"])
                    failures = entry["failures"] or None
                    try:
                        delivered = await self._deliver(uid, umo, events, failures, entry["name"])
                        if delivered:
                            self._report_delivered(entry.get("on_delivered"), umo, uid, events)
                    except Exception as exc:
                        self.logger.warning(f"[GJM] 合并推送失败（{umo}）：{exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(f"[GJM] 推送合并器异常：{exc}")

    async def shutdown(self) -> None:
        """停止插件时调用：先冲发缓冲，再取消任务。"""
        if self._pending:
            for key in list(self._pending):
                umo, uid = key
                entry = self._pending.pop(key)
                if entry and entry["events"]:
                    events = self._merge_events(entry["events"])
                    with suppress(Exception):
                        delivered = await self._deliver(
                            uid,
                            umo,
                            events,
                            entry["failures"] or None,
                            entry["name"],
                        )
                        if delivered:
                            self._report_delivered(entry.get("on_delivered"), umo, uid, events)
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    async def push_to_subscribers(
        self,
        per_user: dict[tuple[str, str], list[TriggerEvent]],
        user_names: dict[tuple[str, str], str] | None = None,
        failures: list[Quote] | None = None,
        *,
        on_delivered: Callable[[str, str, list[TriggerEvent]], None] | None = None,
    ) -> int:
        """把各自的事件推送给对应订阅者，群聊里会 @ 到本人。

        `push_batch_seconds > 0` 时先入缓冲：同一时间窗内多件物品的变化会被合成
        **同一条消息**再发；窗口为 0 则维持“每件立即发”。
        """
        names = user_names or {}
        window = max(0, int(getattr(self.config, "push_batch_seconds", 0) or 0))
        sent = 0
        for (target_umo, uid), events in per_user.items():
            if not events:
                continue
            if platforms.is_group_session(platforms.session_type_of(target_umo)):
                # 群聊由 watcher 的周期快照统一发送，不能在私聊事件路径重复投递。
                continue
            user_markets = {
                sub.market_name
                for sub in getattr(self.subscriptions or self.config, "enabled_subscriptions", ())
                if sub.uid == uid and sub.target_umo == target_umo
            }
            user_failures = [quote for quote in (failures or []) if quote.market_name in user_markets]
            umo = target_umo or self.session_of(uid)
            if not umo:
                self.logger.warning(f"订阅者 {uid} 尚未解析到会话，本轮无法推送。")
                continue
            user_name = names.get((target_umo, uid), "")
            if window > 0:
                self._buffer_push(uid, umo, events, user_failures, user_name, window, on_delivered)
                sent += 1
                continue
            if await self._deliver(uid, umo, events, user_failures or None, user_name):
                self._report_delivered(on_delivered, target_umo or umo, uid, events)
                sent += 1
        if window > 0 and self._pending:
            self._ensure_flush_task()
        return sent
