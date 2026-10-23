"""自动交易的数据模型与价格计算。

价格在业务层统一使用 Gaijin 的原始刻度整数：1 GJN = 10000。
市场可交易价格的最小步进为 100（即 0.01 GJN），但资产/订单接口可能返回
更细的原始值，因此所有落盘与比较都使用整数，避免浮点误差。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

PRICE_SCALE = 10_000
CENT_RAW = 100
MARKET_FEE_RATE = Decimal("0.15")
USER_FEE_RATIO = Decimal("1") - MARKET_FEE_RATE

TRADE_ITEM_COMMODITY = "commodity"
TRADE_ITEM_AUCTION = "auction"

TRADE_MODE_ONCE = "once"
TRADE_MODE_LOOP = "loop"
TRADE_MODES = (TRADE_MODE_ONCE, TRADE_MODE_LOOP)

PHASE_WAITING_BUY = "waiting_buy"
PHASE_HOLDING = "holding"
PHASE_LISTING = "listing"
PHASE_COMPLETED = "completed"
PHASE_PAUSED = "paused"
PHASE_ERROR = "error"


def parse_gjn_raw(value: Any) -> int:
    """把用户输入的 GJN 金额转成两位小数的原始刻度。"""
    try:
        amount = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 - 统一转换成业务异常
        raise ValueError("价格格式不正确") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("价格必须大于 0")
    cents = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents) * CENT_RAW


def raw_to_decimal(raw: int | float | str) -> Decimal:
    return Decimal(int(raw or 0)) / Decimal(PRICE_SCALE)


def fmt_gjn(raw: int | float | str) -> str:
    return f"{raw_to_decimal(raw):.2f}"


def seller_receives_raw(gross_raw: int) -> int:
    """按固定 15% 手续费计算卖家到手价，向下取整到 0.01 GJN。"""
    if gross_raw <= 0:
        return 0
    after_fee_raw = (Decimal(int(gross_raw)) * USER_FEE_RATIO).to_integral_value(rounding=ROUND_CEILING)
    cents = (after_fee_raw / Decimal(CENT_RAW)).to_integral_value(rounding=ROUND_FLOOR)
    return int(cents) * CENT_RAW


def gross_for_net_raw(net_raw: int) -> int:
    """反推达到指定到手价所需的最低挂单价。"""
    if net_raw <= 0:
        return 0
    cents = (Decimal(int(net_raw)) / USER_FEE_RATIO / Decimal(CENT_RAW)).to_integral_value(rounding=ROUND_CEILING)
    gross = int(cents) * CENT_RAW
    while gross < 2**63 and seller_receives_raw(gross) < net_raw:
        gross += CENT_RAW
    return gross


def price_ticks_to_gjn(values: list[tuple[int, int]]) -> list[tuple[float, int]]:
    return [(int(price) / PRICE_SCALE, int(amount)) for price, amount in values]


@dataclass(slots=True)
class BookLevel:
    price_raw: int
    amount: int
    order_id: str = ""
    pair_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_raw": self.price_raw,
            "amount": self.amount,
            "order_id": self.order_id,
            "pair_id": self.pair_id,
        }


@dataclass(slots=True)
class BookSnapshot:
    market_name: str
    sell_levels: list[BookLevel] = field(default_factory=list)
    buy_levels: list[BookLevel] = field(default_factory=list)
    kind: str = ""
    auctionable: bool = False

    @property
    def best_sell(self) -> int | None:
        return self.sell_levels[0].price_raw if self.sell_levels else None

    @property
    def best_buy(self) -> int | None:
        return self.buy_levels[0].price_raw if self.buy_levels else None


@dataclass(slots=True)
class TradeRule:
    rule_id: str
    market_name: str
    display_name: str = ""
    item_type: str = TRADE_ITEM_COMMODITY
    buy_price_raw: int = 0
    quantity: int = 1
    sell_net_price_raw: int = 0
    mode: str = TRADE_MODE_ONCE
    enabled: bool = True
    live: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def buy_price(self) -> float:
        return float(raw_to_decimal(self.buy_price_raw))

    @property
    def sell_net_price(self) -> float:
        return float(raw_to_decimal(self.sell_net_price_raw))

    @property
    def title(self) -> str:
        return self.display_name or self.market_name

    @property
    def sell_gross_price_raw(self) -> int:
        return gross_for_net_raw(self.sell_net_price_raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "market_name": self.market_name,
            "display_name": self.display_name,
            "item_type": self.item_type,
            "buy_price_raw": int(self.buy_price_raw),
            "quantity": int(self.quantity),
            "sell_net_price_raw": int(self.sell_net_price_raw),
            "mode": self.mode,
            "enabled": bool(self.enabled),
            "live": bool(self.live),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeRule:
        allowed = set(cls.__slots__)  # type: ignore[attr-defined]
        values = {key: data[key] for key in allowed if key in data}
        return cls(**values)


@dataclass(slots=True)
class TradeSettings:
    enabled: bool = False
    total_budget_raw: int = 0
    daily_limit_raw: int = 0
    max_order_raw: int = 0
    max_quantity: int = 0
    poll_interval: int = 5
    private_mode: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "total_budget_raw": int(self.total_budget_raw),
            "daily_limit_raw": int(self.daily_limit_raw),
            "max_order_raw": int(self.max_order_raw),
            "max_quantity": int(self.max_quantity),
            "poll_interval": int(self.poll_interval),
            "private_mode": bool(self.private_mode),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeSettings:
        allowed = set(cls.__slots__)  # type: ignore[attr-defined]
        values = {key: data[key] for key in allowed if key in data}
        return cls(**values)


@dataclass(slots=True)
class TradeAssetAllocation:
    context_id: str
    asset_id: str
    quantity: int
    class_id: str = ""
    baseline_quantity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "asset_id": self.asset_id,
            "quantity": int(self.quantity),
            "class_id": self.class_id,
            "baseline_quantity": int(self.baseline_quantity),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeAssetAllocation:
        return cls(
            context_id=str(data.get("context_id") or ""),
            asset_id=str(data.get("asset_id") or ""),
            quantity=max(0, int(data.get("quantity") or 0)),
            class_id=str(data.get("class_id") or ""),
            baseline_quantity=max(0, int(data.get("baseline_quantity") or 0)),
        )


@dataclass(slots=True)
class TradeOrder:
    order_id: str
    pair_id: str
    side: str
    amount: int
    price_raw: int
    market_name: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "pair_id": self.pair_id,
            "side": self.side,
            "amount": int(self.amount),
            "price_raw": int(self.price_raw),
            "market_name": self.market_name,
            "created_at": float(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeOrder:
        return cls(
            order_id=str(data.get("order_id") or ""),
            pair_id=str(data.get("pair_id") or ""),
            side=str(data.get("side") or "").upper(),
            amount=max(0, int(data.get("amount") or 0)),
            price_raw=max(0, int(data.get("price_raw") or 0)),
            market_name=str(data.get("market_name") or ""),
            created_at=float(data.get("created_at") or time.time()),
        )


@dataclass(slots=True)
class TradeRuntime:
    rule_id: str
    phase: str = PHASE_WAITING_BUY
    held_quantity: int = 0
    listed_quantity: int = 0
    cost_basis_raw: int = 0
    proceeds_raw: int = 0
    cycle_count: int = 0
    assets: list[TradeAssetAllocation] = field(default_factory=list)
    orders: list[TradeOrder] = field(default_factory=list)
    last_error: str = ""
    last_buy_price_raw: int = 0
    last_sell_price_raw: int = 0
    updated_at: float = field(default_factory=time.time)

    @property
    def available_quantity(self) -> int:
        return max(0, int(self.held_quantity) - int(self.listed_quantity))

    def reset_for_cycle(self) -> None:
        self.phase = PHASE_WAITING_BUY
        self.held_quantity = 0
        self.listed_quantity = 0
        self.cost_basis_raw = 0
        self.assets = []
        self.orders = []
        self.last_error = ""
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "phase": self.phase,
            "held_quantity": int(self.held_quantity),
            "listed_quantity": int(self.listed_quantity),
            "cost_basis_raw": int(self.cost_basis_raw),
            "proceeds_raw": int(self.proceeds_raw),
            "cycle_count": int(self.cycle_count),
            "assets": [asset.to_dict() for asset in self.assets],
            "orders": [order.to_dict() for order in self.orders],
            "last_error": self.last_error,
            "last_buy_price_raw": int(self.last_buy_price_raw),
            "last_sell_price_raw": int(self.last_sell_price_raw),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeRuntime:
        return cls(
            rule_id=str(data.get("rule_id") or ""),
            phase=str(data.get("phase") or PHASE_WAITING_BUY),
            held_quantity=max(0, int(data.get("held_quantity") or 0)),
            listed_quantity=max(0, int(data.get("listed_quantity") or 0)),
            cost_basis_raw=max(0, int(data.get("cost_basis_raw") or 0)),
            proceeds_raw=max(0, int(data.get("proceeds_raw") or 0)),
            cycle_count=max(0, int(data.get("cycle_count") or 0)),
            assets=[
                TradeAssetAllocation.from_dict(item) for item in (data.get("assets") or []) if isinstance(item, dict)
            ],
            orders=[TradeOrder.from_dict(item) for item in (data.get("orders") or []) if isinstance(item, dict)],
            last_error=str(data.get("last_error") or ""),
            last_buy_price_raw=max(0, int(data.get("last_buy_price_raw") or 0)),
            last_sell_price_raw=max(0, int(data.get("last_sell_price_raw") or 0)),
            updated_at=float(data.get("updated_at") or time.time()),
        )


@dataclass(slots=True)
class TradeLedger:
    day: str = ""
    daily_spent_raw: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "daily_spent_raw": int(self.daily_spent_raw)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeLedger:
        return cls(day=str(data.get("day") or ""), daily_spent_raw=max(0, int(data.get("daily_spent_raw") or 0)))


@dataclass(slots=True)
class TradeEvent:
    timestamp: float
    rule_id: str
    kind: str
    message: str
    level: str = "info"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": float(self.timestamp),
            "rule_id": self.rule_id,
            "kind": self.kind,
            "message": self.message,
            "level": self.level,
            "details": self.details,
        }
