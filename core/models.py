"""数据模型层：全部为纯数据结构，不含任何 IO 逻辑。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import DEFAULT_APPID, DEFAULT_POLL_INTERVAL, PRICE_DECIMALS


def fmt_price(value: float | None) -> str:
    """把 GJN 价格格式化为 2 位小数字符串；None 显示为 --。"""
    if value is None:
        return "--"
    return f"{value:.{PRICE_DECIMALS}f}"


def fmt_delta(new: float | None, old: float | None) -> str:
    """生成价格变化描述，如 "▲ +1.20 (+3.2%)" / "持平" / "(首次)"。"""
    if old is None:
        return "(首次)"
    if new is None:
        return "(无数据)"
    diff = new - old
    if abs(diff) < 1e-9:
        return "持平"
    arrow = "▲" if diff > 0 else "▼"
    pct = (diff / old * 100) if old else 0.0
    return f"{arrow} {diff:+.{PRICE_DECIMALS}f} ({pct:+.1f}%)"


@dataclass(slots=True)
class Quote:
    """某个物品在某一时刻的行情快照。"""

    market_name: str
    sell_min: float | None = None  # 实时最低售价(GJN)  <- SELL[0]
    buy_max: float | None = None  # 实时最高求购价(GJN) <- BUY[0]
    sell_depth: int = 0  # 出售挂单总量
    buy_depth: int = 0  # 求购挂单总量
    timestamp: float = field(default_factory=time.time)
    display_name: str = ""
    app_id: str = DEFAULT_APPID
    kind: str = ""  # COMMODITY / 其它
    error: str = ""  # 非空表示本次抓取失败

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def title(self) -> str:
        return self.display_name or self.market_name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Quote:
        allowed = set(cls.__slots__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass(slots=True)
class MonitorSpec:
    """一条监控规则（对应配置里 template_list 的一项）。"""

    market_name: str
    display_name: str = ""
    sell_below: float = 0.0  # 最低售价 <= 该值 -> 触发；0 表示不启用
    buy_above: float = 0.0  # 最高求购价 >= 该值 -> 触发；0 表示不启用
    enabled: bool = True

    @property
    def key(self) -> str:
        return self.market_name

    @property
    def title(self) -> str:
        return self.display_name or self.market_name


@dataclass(slots=True)
class Subscription:
    """一条用户订阅：某个 UID 订阅了某件物品。

    设计为"每用户每物品一条"，好处：
      * 聊天指令只需一个链接（满足"只接受链接"）；
      * 阈值可以在这条记录上单独设置，管理界面里逐条可改；
      * 推送时天然知道该 @ 谁。
    """

    uid: str
    market_name: str
    display_name: str = ""
    sell_below: float = 0.0
    buy_above: float = 0.0
    enabled: bool = True
    #: 该订阅者希望的刷新间隔（秒）。同一物品被多人订阅时取最小值。
    interval: int = DEFAULT_POLL_INTERVAL

    @property
    def key(self) -> tuple[str, str]:
        return (self.uid, self.market_name)

    def to_spec(self) -> MonitorSpec:
        """复用全局监控的阈值判定逻辑（推送目标不同，判定规则一致）。"""
        return MonitorSpec(
            market_name=self.market_name,
            display_name=self.display_name,
            sell_below=self.sell_below,
            buy_above=self.buy_above,
            enabled=self.enabled,
        )


@dataclass(slots=True)
class SearchCandidate:
    """搜索候选项，带置信度。"""

    market_name: str
    name: str
    score: float
    app_id: str = DEFAULT_APPID
    icon: str = ""
    sell_min: float | None = None
    buy_max: float | None = None


@dataclass(slots=True)
class TriggerEvent:
    """一次需要推送的事件。"""

    spec: MonitorSpec
    quote: Quote
    previous: Quote | None = None
    #: 本轮"新触发"的阈值文案（边沿触发，不会重复刷屏）
    reasons: list[str] = field(default_factory=list)
    #: 当前仍处于满足状态的阈值 key -> 文案
    active_hits: dict[str, str] = field(default_factory=dict)
