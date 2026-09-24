"""推送层：负责「判断要不要推」「渲染成人看的文本」「@ 到正确的人」。

推送模式（push_mode）：
  always      每次轮询都推送
  on_change   仅当售价/求购价相对上一次发生变化时推送（首次观测会推一次作为基线）
  on_trigger  仅当阈值**被触发**时推送

阈值采用「边沿触发」而非「电平触发」：
  只有当某个阈值从「不满足」变为「满足」的那一刻才告警；
  若价格持续低于阈值，不会每轮重复刷屏（刻意设计，避免打扰）。
  判定无需额外状态：把上一轮快照重新算一遍阈值，比对差集即可。

投递对象：**只有订阅者本人**（user_subscriptions）。
  抓取失败等异常则通知拥有者（owner_uids 解析出的会话）。

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
import inspect
import time
from contextlib import suppress
from dataclasses import replace

from . import platforms
from .constants import PUSH_ALWAYS, PUSH_ON_TRIGGER
from .models import MonitorSpec, Quote, Subscription, TriggerEvent, fmt_delta, fmt_price
from .payload import render_message

#: 分隔行：真正的空行会被 QQ 的 markdown 渲染折叠掉，
#: 所以放一个零宽空格（U+200B）—— 它不是空白字符，str.strip()/客户端 trim 都删不掉，
#: 渲染出来就是一行空白。
_BLANK_LINE = "\u200b"
BLOCK_SEPARATOR = f"\n{_BLANK_LINE}\n"


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
    def __init__(self, context, storage, config, logger) -> None:
        self.context = context
        self.storage = storage
        self.config = config
        self.logger = logger
        #: 推送成功后可选的“补一张网页快照”回调（由主插件注入，非阻塞）
        self.snapshot_hook = None
        #: payload 模板的问题只提醒一次，避免刷屏
        self._payload_warned = False
        #: 出站失败通知限频（umo -> 上次通知时间）
        self._send_failure_at: dict[str, float] = {}
        #: 现拍快照并等待完成的提供者（由主插件注入）
        self.snapshot_provider = None
        #: 后台投递任务（等图的那些），避免拖住轮询循环
        self._tasks: set[asyncio.Task] = set()
        #: 推送合并缓冲：umo -> {uid, name, events, failures, deadline}
        self._pending: dict[str, dict] = {}
        self._flush_task: asyncio.Task | None = None

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
        uids = owner_uids if owner_uids is not None else self.config.owner_uids
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

    def evaluate(self, spec: MonitorSpec, quote: Quote, previous: Quote | None) -> TriggerEvent | None:
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

        mode = self.config.push_mode
        if mode == PUSH_ALWAYS:
            should_push = True
        elif mode == PUSH_ON_TRIGGER:
            should_push = bool(new_hits)
        else:  # PUSH_ON_CHANGE
            should_push = changed or bool(new_hits)

        if not should_push:
            return None

        return TriggerEvent(
            spec=spec,
            quote=quote,
            previous=previous,
            reasons=list(new_hits.values()),
            active_hits=hits_now,
        )

    def evaluate_subscription(self, sub: Subscription, quote: Quote, previous: Quote | None) -> TriggerEvent | None:
        """用户订阅的判定：复用全局规则，但阈值来自订阅记录本身。"""
        return self.evaluate(sub.to_spec(), quote, previous)

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

    def _item_context(self, event: TriggerEvent, snapshot_path: str = "") -> dict:
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
            "image": snapshot_path,
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
        snapshot_path: str = "",
    ) -> dict:
        """组装 payload 模板可用到的全部字段（整轮 + 本轮主物品的快捷字段）。"""
        now = time.time()
        items = [self._item_context(event, snapshot_path) for event in events]
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
            "mode": self.config.push_mode,
            "failures": "、".join(quote.title for quote in (failures or [])),
            "failures_json": [{"name": q.title, "error": q.error} for q in (failures or [])],
            "appid": self.config.appid,
            "image": snapshot_path,
        }
        primary = next((event for event in events if event.reasons), events[0] if events else None)
        if primary is not None:
            context.update(self._item_context(primary, snapshot_path))
        return context

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

    def _build_chain(self, text: str, mention_uid: str = "", mention_name: str = ""):
        """构造消息链；需要 @ 时把 At 放在最前面（适用于支持 At 的平台）。"""
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
        snapshot_path: str = "",
    ) -> bool:
        """向单个会话发送，并按平台选择正确的 @ 方式（策略已固化，无配置项）。

        配置了 payload 时正文改由模板渲染：JSON 骨架按书写顺序逐行取值，
        多件物品逐件渲染后拼接；任一份渲染失败都整体回退到 text。
        """
        platform_type = await self._platform_type(umo) if umo else ""
        # @ 的方式由 core.platforms 统一裁决（各平台适配器差异都写在那里）
        plan = platforms.mention_plan(platform_type, mention_uid, mention_name) if mention_uid else None
        token = plan.text if plan else ""
        at_unsupported = bool(plan and plan.style == platforms.STYLE_PLAIN_TEXT)

        body, used_mention = text, False
        template = (self.config.payload or "").strip()
        images: list[str] = []
        if events and template:
            blocks: list[str] = []
            problem = ""
            unknown: list[str] = []
            base = self.build_payload_context(
                events,
                failures,
                umo=umo,
                uid=uid or mention_uid,
                user_name=mention_name,
                mention=token,
                platform=platform_type,
                total_items=len(events),
                snapshot_path=snapshot_path,
            )
            for index, event in enumerate(events):
                context = dict(base)
                context.update(self._item_context(event, snapshot_path))
                if index > 0:
                    # @ 只放在第一份里，多件时不会重复 @
                    context["mention"] = ""
                piece = render_message(template, context)
                if not piece.ok:
                    problem = piece.error
                    break
                if piece.used_mention:
                    used_mention = True
                unknown = piece.unknown_fields or unknown
                if piece.text:
                    blocks.append(piece.text)
                images.extend(piece.images)
            if blocks and not problem:
                # 多件之间空一行（用零宽空格行，避免被客户端渲染折叠）
                body = BLOCK_SEPARATOR.join(blocks)
                # 多件合并时同一张图只附一次
                images = list(dict.fromkeys(images))
            if not self._payload_warned:
                if problem:
                    self._payload_warned = True
                    self.logger.warning(f"[GJM] payload 渲染失败，已回退为可读文本：{problem}")
                elif unknown:
                    self._payload_warned = True
                    self.logger.warning("[GJM] payload 含未知字段（已按空值处理）：" + "、".join(unknown))

        if not body.strip() and platforms.requires_text(platform_type):
            # 这些平台会丢弃“没有文字”的消息，补一句兜底文字
            body = (events[0].quote.title if events else "") or "Gaijin 行情"

        # 模板自己放了 @ 标记就不要再叠加
        if used_mention:
            needs_md = bool(plan and plan.style == platforms.STYLE_MARKDOWN_TEXT)
            return await self._send_plain(umo, body, markdown=True if needs_md else None, images=images)
        if mention_uid and self.config.mention_users:
            if plan and plan.style == platforms.STYLE_MARKDOWN_TEXT:
                # 实测结论：必须走 markdown 通道，纯文本会把标记原样显示出来
                return await self._send_plain(umo, f"{token}\n{body}", markdown=True, images=images)
            if at_unsupported:
                # 该平台的适配器会静默忽略 At 组件，改用纯文本 @
                return await self._send_plain(umo, f"{token}\n{body}", images=images)
            try:
                chain = self._build_chain(body, mention_uid, mention_name)
                for source in images:
                    with suppress(Exception):
                        from astrbot.api.message_components import Image

                        url = str(source)
                        chain.chain.append(
                            Image.fromURL(url) if url.startswith(("http://", "https://")) else Image.fromFileSystem(url)
                        )
                if await self.context.send_message(umo, chain):
                    return True
            except Exception as exc:
                self.logger.debug(f"@ 推送失败（{umo}），降级为纯文本: {exc}")
        return await self._send_plain(umo, body, images=images)

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

    def set_snapshot_hook(self, hook) -> None:
        """注入网页快照回调；签名 (umo, market_names, *, triggered) -> bool。"""
        self.snapshot_hook = hook

    def _fire_snapshot(self, umo: str, events: list[TriggerEvent]) -> None:
        """本轮推送成功后，按配置决定是否补一张商品页快照。"""
        if self.snapshot_hook is None or not events:
            return
        target = next((ev for ev in events if ev.reasons), events[0])
        try:
            self.snapshot_hook(umo, [target.quote.market_name], triggered=bool(target.reasons))
        except Exception as exc:
            self.logger.debug(f"网页快照回调异常：{exc}")

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

    def set_snapshot_provider(self, provider) -> None:
        """注入“现拍快照并等待完成”的提供者。

        签名：async def (umo, market_name, *, triggered) -> str（返回图片路径，失败返回空串）
        """
        self.snapshot_provider = provider

    async def _deliver(
        self,
        uid: str,
        umo: str,
        events: list[TriggerEvent],
        failures: list[Quote] | None,
        user_name: str,
    ) -> bool:
        """投递给单个订阅者。

        配置了 payload 且模板里带图片时：**先拍快照、等它渲染完**，再把正文与图片
        放在同一条消息里发出（不再另外补发一张图片）。
        """
        template = (self.config.payload or "").strip()
        want_snapshot = False
        if template:
            probe = render_message(
                template,
                self.build_payload_context(events, failures, umo=umo, uid=uid, user_name=user_name),
            )
            want_snapshot = probe.needs_snapshot

        snapshot_path = ""
        if want_snapshot and self.snapshot_provider is not None and events:
            target = next((event for event in events if event.reasons), events[0])
            try:
                snapshot_path = await self.snapshot_provider(
                    umo, target.quote.market_name, triggered=bool(target.reasons)
                )
            except Exception as exc:
                self.logger.warning(f"[GJM] 快照拍摄失败（{target.quote.market_name}）：{exc}")
                snapshot_path = ""

        text = self.render_digest(events, failures, title="Gaijin 行情")
        sent = await self._send(
            umo,
            text,
            mention_uid=uid,
            mention_name=user_name,
            events=events,
            failures=failures,
            uid=uid,
            snapshot_path=snapshot_path,
        )
        if not sent:
            await self._notify_send_failure(umo)
        if sent and not (template and want_snapshot):
            # 没有 payload（或模板不含图片）时沿用旧行为：推送后单独补一张快照
            self._fire_snapshot(umo, events)
        return sent

    def _probe_needs_snapshot(self, events: list[TriggerEvent], umo: str, uid: str, user_name: str) -> bool:
        """这次投递是否需要现拍一张图（模板里有图片占位且还没拍到）。"""
        template = (self.config.payload or "").strip()
        if not template or not events:
            return False
        probe = render_message(
            template,
            self.build_payload_context(events, None, umo=umo, uid=uid, user_name=user_name),
        )
        return probe.needs_snapshot

    def _spawn_delivery(
        self,
        uid: str,
        umo: str,
        events: list[TriggerEvent],
        failures: list[Quote] | None,
        user_name: str,
    ) -> None:
        """把一次投递丢到后台（等图要十几秒，不能卡住轮询循环）。"""
        try:
            task = asyncio.create_task(self._deliver(uid, umo, events, failures, user_name))
        except RuntimeError as exc:
            self.logger.debug(f"[GJM] 当前上下文无法调度投递任务：{exc}")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
        self, uid: str, umo: str, events: list[TriggerEvent], failures: list[Quote] | None, user_name: str, window: int
    ) -> None:
        """把本次事件并入缓冲，等窗口到点后合成一条消息发出。"""
        entry = self._pending.get(umo)
        now = time.time()
        if entry is None:
            entry = {"uid": uid, "name": user_name, "events": [], "failures": [], "deadline": now + window}
            self._pending[umo] = entry
        entry["uid"] = uid
        entry["name"] = user_name or entry.get("name", "")
        entry["events"].extend(events)
        entry["failures"].extend(failures or [])

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
                for umo in [u for u, e in self._pending.items() if e["deadline"] <= now]:
                    entry = self._pending.pop(umo, None)
                    if not entry or not entry["events"]:
                        continue
                    events = self._merge_events(entry["events"])
                    failures = entry["failures"] or None
                    try:
                        if self._probe_needs_snapshot(events, umo, entry["uid"], entry["name"]):
                            self._spawn_delivery(entry["uid"], umo, events, failures, entry["name"])
                        else:
                            await self._deliver(entry["uid"], umo, events, failures, entry["name"])
                    except Exception as exc:
                        self.logger.warning(f"[GJM] 合并推送失败（{umo}）：{exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(f"[GJM] 推送合并器异常：{exc}")

    async def shutdown(self) -> None:
        """停止插件时调用：先冲发缓冲，再取消任务。"""
        if self._pending:
            for umo in list(self._pending):
                entry = self._pending.pop(umo)
                if entry and entry["events"]:
                    with suppress(Exception):
                        await self._deliver(
                            entry["uid"],
                            umo,
                            self._merge_events(entry["events"]),
                            entry["failures"] or None,
                            entry["name"],
                        )
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    async def push_to_subscribers(
        self,
        per_user: dict[str, list[TriggerEvent]],
        user_names: dict[str, str] | None = None,
        failures: list[Quote] | None = None,
    ) -> int:
        """把各自的事件推送给对应订阅者，群聊里会 @ 到本人。

        `push_batch_seconds > 0` 时先入缓冲：同一时间窗内多件物品的变化会被合成
        **同一条消息**再发；窗口为 0 则维持“每件立即发”。
        """
        names = user_names or {}
        window = max(0, int(getattr(self.config, "push_batch_seconds", 0) or 0))
        sent = 0
        for uid, events in per_user.items():
            if not events:
                continue
            umo = self.session_of(uid)
            if not umo:
                self.logger.warning(f"订阅者 {uid} 尚未解析到会话，本轮无法推送。")
                continue
            user_name = names.get(uid, "")
            if window > 0:
                self._buffer_push(uid, umo, events, failures, user_name, window)
                sent += 1
                continue
            if self._probe_needs_snapshot(events, umo, uid, user_name):
                self._spawn_delivery(uid, umo, events, failures, user_name)
                sent += 1
                continue
            if await self._deliver(uid, umo, events, failures, user_name):
                sent += 1
        if window > 0 and self._pending:
            self._ensure_flush_task()
        return sent
