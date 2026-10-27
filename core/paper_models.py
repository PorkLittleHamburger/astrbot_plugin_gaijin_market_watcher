"""模拟盘（Paper Trading）的数据模型。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .trade_models import PRICE_SCALE, BookLevel, BookSnapshot, gross_for_net_raw, seller_receives_raw

PAPER_ORDER_OPEN = "OPEN"
PAPER_ORDER_PARTIALLY_FILLED = "PARTIALLY_FILLED"
PAPER_ORDER_FILLED = "FILLED"
PAPER_ORDER_CANCELLED = "CANCELLED"
PAPER_ORDER_REJECTED = "REJECTED"
PAPER_OPEN_STATUSES = (PAPER_ORDER_OPEN, PAPER_ORDER_PARTIALLY_FILLED)

PAPER_SOURCE_MANUAL = "MANUAL"
PAPER_SOURCE_RULE = "RULE"
PAPER_SOURCE_SCENARIO = "SCENARIO"

PAPER_SIDE_BUY = "BUY"
PAPER_SIDE_SELL = "SELL"
PAPER_SIDES = (PAPER_SIDE_BUY, PAPER_SIDE_SELL)

PAPER_SCENARIO_PENDING = "PENDING"
PAPER_SCENARIO_ACTIVE = "ACTIVE"
PAPER_SCENARIO_ENDED = "ENDED"
PAPER_SCENARIO_TARGET_BUY = "BUY"
PAPER_SCENARIO_TARGET_SELL = "SELL"
PAPER_SCENARIO_TARGETS = (PAPER_SCENARIO_TARGET_BUY, PAPER_SCENARIO_TARGET_SELL)

DEFAULT_PAPER_CASH_RAW = 1000 * PRICE_SCALE
DEFAULT_PAPER_MIN_PRICE_RAW = 1000
DEFAULT_PAPER_MAX_PRICE_RAW = 20_000_000
PAPER_PRICE_TICK_RAW = 100
MAX_PAPER_ORDER_QUANTITY = 1000


def _book_version(
    buy_levels: list[BookLevel], sell_levels: list[BookLevel], depth_buy: int, depth_sell: int, synthetic: bool
) -> str:
    payload = {
        "buy": [[int(level.price_raw), int(level.amount)] for level in buy_levels],
        "sell": [[int(level.price_raw), int(level.amount)] for level in sell_levels],
        "depth_buy": int(depth_buy),
        "depth_sell": int(depth_sell),
        "synthetic": bool(synthetic),
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]


@dataclass(slots=True)
class PaperOrder:
    order_id: str
    client_order_id: str
    market_name: str
    side: str
    price_raw: int
    quantity: int
    filled_quantity: int = 0
    status: str = PAPER_ORDER_OPEN
    source: str = PAPER_SOURCE_MANUAL
    rule_id: str = ""
    cycle_id: str = ""
    scenario_id: str = ""
    reserved_cash_raw: int = 0
    filled_amount_raw: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancelled_at: float = 0.0
    last_error: str = ""

    @property
    def remaining_quantity(self) -> int:
        return max(0, int(self.quantity) - int(self.filled_quantity))

    @property
    def is_open(self) -> bool:
        return self.status in PAPER_OPEN_STATUSES and self.remaining_quantity > 0

    @property
    def average_fill_price_raw(self) -> int:
        if self.filled_quantity <= 0:
            return 0
        return int(self.filled_amount_raw // self.filled_quantity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "market_name": self.market_name,
            "side": self.side,
            "price_raw": int(self.price_raw),
            "quantity": int(self.quantity),
            "filled_quantity": int(self.filled_quantity),
            "status": self.status,
            "source": self.source,
            "rule_id": self.rule_id,
            "cycle_id": self.cycle_id,
            "scenario_id": self.scenario_id,
            "reserved_cash_raw": int(self.reserved_cash_raw),
            "filled_amount_raw": int(self.filled_amount_raw),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "cancelled_at": float(self.cancelled_at),
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperOrder:
        return cls(
            order_id=str(data.get("order_id") or ""),
            client_order_id=str(data.get("client_order_id") or ""),
            market_name=str(data.get("market_name") or ""),
            side=str(data.get("side") or "").upper(),
            price_raw=max(0, int(data.get("price_raw") or 0)),
            quantity=max(0, int(data.get("quantity") or 0)),
            filled_quantity=max(0, int(data.get("filled_quantity") or 0)),
            status=str(data.get("status") or PAPER_ORDER_OPEN),
            source=str(data.get("source") or PAPER_SOURCE_MANUAL),
            rule_id=str(data.get("rule_id") or ""),
            cycle_id=str(data.get("cycle_id") or ""),
            scenario_id=str(data.get("scenario_id") or ""),
            reserved_cash_raw=max(0, int(data.get("reserved_cash_raw") or 0)),
            filled_amount_raw=max(0, int(data.get("filled_amount_raw") or 0)),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            cancelled_at=float(data.get("cancelled_at") or 0.0),
            last_error=str(data.get("last_error") or ""),
        )


@dataclass(slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    market_name: str
    side: str
    price_raw: int
    quantity: int
    gross_raw: int
    fee_raw: int
    net_raw: int
    cost_basis_raw: int = 0
    realized_pnl_raw: int = 0
    source: str = PAPER_SOURCE_MANUAL
    rule_id: str = ""
    cycle_id: str = ""
    scenario_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "market_name": self.market_name,
            "side": self.side,
            "price_raw": int(self.price_raw),
            "quantity": int(self.quantity),
            "gross_raw": int(self.gross_raw),
            "fee_raw": int(self.fee_raw),
            "net_raw": int(self.net_raw),
            "cost_basis_raw": int(self.cost_basis_raw),
            "realized_pnl_raw": int(self.realized_pnl_raw),
            "source": self.source,
            "rule_id": self.rule_id,
            "cycle_id": self.cycle_id,
            "scenario_id": self.scenario_id,
            "created_at": float(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperFill:
        return cls(
            fill_id=str(data.get("fill_id") or ""),
            order_id=str(data.get("order_id") or ""),
            market_name=str(data.get("market_name") or ""),
            side=str(data.get("side") or "").upper(),
            price_raw=max(0, int(data.get("price_raw") or 0)),
            quantity=max(0, int(data.get("quantity") or 0)),
            gross_raw=max(0, int(data.get("gross_raw") or 0)),
            fee_raw=max(0, int(data.get("fee_raw") or 0)),
            net_raw=max(0, int(data.get("net_raw") or 0)),
            cost_basis_raw=max(0, int(data.get("cost_basis_raw") or 0)),
            realized_pnl_raw=int(data.get("realized_pnl_raw") or 0),
            source=str(data.get("source") or PAPER_SOURCE_MANUAL),
            rule_id=str(data.get("rule_id") or ""),
            cycle_id=str(data.get("cycle_id") or ""),
            scenario_id=str(data.get("scenario_id") or ""),
            created_at=float(data.get("created_at") or time.time()),
        )


@dataclass(slots=True)
class PaperLot:
    lot_id: str
    market_name: str
    quantity: int
    remaining_quantity: int
    unit_cost_raw: int
    acquired_at: float = field(default_factory=time.time)
    source_order_id: str = ""
    rule_id: str = ""
    scenario_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "market_name": self.market_name,
            "quantity": int(self.quantity),
            "remaining_quantity": int(self.remaining_quantity),
            "unit_cost_raw": int(self.unit_cost_raw),
            "acquired_at": float(self.acquired_at),
            "source_order_id": self.source_order_id,
            "rule_id": self.rule_id,
            "scenario_id": self.scenario_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperLot:
        return cls(
            lot_id=str(data.get("lot_id") or ""),
            market_name=str(data.get("market_name") or ""),
            quantity=max(0, int(data.get("quantity") or 0)),
            remaining_quantity=max(
                0,
                int(data["remaining_quantity"] if "remaining_quantity" in data else data.get("quantity") or 0),
            ),
            unit_cost_raw=max(0, int(data.get("unit_cost_raw") or 0)),
            acquired_at=float(data.get("acquired_at") or time.time()),
            source_order_id=str(data.get("source_order_id") or ""),
            rule_id=str(data.get("rule_id") or ""),
            scenario_id=str(data.get("scenario_id") or ""),
        )


@dataclass(slots=True)
class PaperAccount:
    starting_cash_raw: int = DEFAULT_PAPER_CASH_RAW
    cash_available_raw: int = DEFAULT_PAPER_CASH_RAW
    cash_reserved_raw: int = 0
    realized_pnl_raw: int = 0
    daily_buy_raw: int = 0
    daily_day: str = ""
    version: int = 1
    lots: list[PaperLot] = field(default_factory=list)

    @property
    def total_cash_raw(self) -> int:
        return max(0, int(self.cash_available_raw)) + max(0, int(self.cash_reserved_raw))

    def inventory_quantity(self, market_name: str = "") -> int:
        return sum(
            lot.remaining_quantity
            for lot in self.lots
            if lot.remaining_quantity > 0 and (not market_name or lot.market_name == market_name)
        )

    def inventory_cost_raw(self, market_name: str = "") -> int:
        return sum(
            lot.remaining_quantity * lot.unit_cost_raw
            for lot in self.lots
            if lot.remaining_quantity > 0 and (not market_name or lot.market_name == market_name)
        )

    def clone(self) -> PaperAccount:
        return PaperAccount(
            starting_cash_raw=int(self.starting_cash_raw),
            cash_available_raw=int(self.cash_available_raw),
            cash_reserved_raw=int(self.cash_reserved_raw),
            realized_pnl_raw=int(self.realized_pnl_raw),
            daily_buy_raw=int(self.daily_buy_raw),
            daily_day=self.daily_day,
            version=int(self.version),
            lots=[PaperLot.from_dict(lot.to_dict()) for lot in self.lots],
        )

    def touch(self) -> None:
        self.version += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_cash_raw": int(self.starting_cash_raw),
            "cash_available_raw": int(self.cash_available_raw),
            "cash_reserved_raw": int(self.cash_reserved_raw),
            "realized_pnl_raw": int(self.realized_pnl_raw),
            "daily_buy_raw": int(self.daily_buy_raw),
            "daily_day": self.daily_day,
            "version": int(self.version),
            "lots": [lot.to_dict() for lot in self.lots],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperAccount:
        return cls(
            starting_cash_raw=max(
                0,
                int(data.get("starting_cash_raw", DEFAULT_PAPER_CASH_RAW)),
            ),
            cash_available_raw=max(0, int(data.get("cash_available_raw") or 0)),
            cash_reserved_raw=max(0, int(data.get("cash_reserved_raw") or 0)),
            realized_pnl_raw=int(data.get("realized_pnl_raw") or 0),
            daily_buy_raw=max(0, int(data.get("daily_buy_raw") or 0)),
            daily_day=str(data.get("daily_day") or ""),
            version=max(1, int(data.get("version") or 1)),
            lots=[PaperLot.from_dict(item) for item in (data.get("lots") or []) if isinstance(item, dict)],
        )


@dataclass(slots=True)
class PaperMarketBook:
    market_name: str
    buy_levels: list[BookLevel] = field(default_factory=list)
    sell_levels: list[BookLevel] = field(default_factory=list)
    depth_buy: int = 0
    depth_sell: int = 0
    observed_at: float = field(default_factory=time.time)
    version: str = ""
    synthetic: bool = False

    @classmethod
    def from_snapshot(
        cls,
        book: BookSnapshot,
        *,
        observed_at: float,
        synthetic: bool = False,
        extra_buy: list[BookLevel] | None = None,
        extra_sell: list[BookLevel] | None = None,
    ) -> PaperMarketBook:
        buy = [BookLevel(level.price_raw, level.amount) for level in book.buy_levels]
        sell = [BookLevel(level.price_raw, level.amount) for level in book.sell_levels]
        buy.extend(BookLevel(level.price_raw, level.amount) for level in (extra_buy or []))
        sell.extend(BookLevel(level.price_raw, level.amount) for level in (extra_sell or []))
        buy.sort(key=lambda level: level.price_raw, reverse=True)
        sell.sort(key=lambda level: level.price_raw)
        depth_buy = sum(level.amount for level in buy)
        depth_sell = sum(level.amount for level in sell)
        return cls(
            market_name=book.market_name,
            buy_levels=buy,
            sell_levels=sell,
            depth_buy=depth_buy,
            depth_sell=depth_sell,
            observed_at=float(observed_at),
            version=_book_version(buy, sell, depth_buy, depth_sell, synthetic),
            synthetic=synthetic,
        )

    @property
    def best_buy(self) -> int | None:
        return self.buy_levels[0].price_raw if self.buy_levels else None

    @property
    def best_sell(self) -> int | None:
        return self.sell_levels[0].price_raw if self.sell_levels else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_name": self.market_name,
            "buy_levels": [
                {"price_raw": int(level.price_raw), "amount": int(level.amount)} for level in self.buy_levels
            ],
            "sell_levels": [
                {"price_raw": int(level.price_raw), "amount": int(level.amount)} for level in self.sell_levels
            ],
            "depth_buy": int(self.depth_buy),
            "depth_sell": int(self.depth_sell),
            "observed_at": float(self.observed_at),
            "version": self.version,
            "synthetic": bool(self.synthetic),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperMarketBook:
        def levels(items: Any) -> list[BookLevel]:
            result = []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                price = max(0, int(item.get("price_raw") or 0))
                amount = max(0, int(item.get("amount") or 0))
                if price > 0 and amount > 0:
                    result.append(BookLevel(price_raw=price, amount=amount))
            return result

        return cls(
            market_name=str(data.get("market_name") or ""),
            buy_levels=levels(data.get("buy_levels")),
            sell_levels=levels(data.get("sell_levels")),
            depth_buy=max(0, int(data.get("depth_buy") or 0)),
            depth_sell=max(0, int(data.get("depth_sell") or 0)),
            observed_at=float(data.get("observed_at") or time.time()),
            version=str(data.get("version") or ""),
            synthetic=bool(data.get("synthetic")),
        )


@dataclass(slots=True)
class PaperScenario:
    scenario_id: str
    rule_id: str
    market_name: str
    buy_price_raw: int
    sell_net_price_raw: int
    quantity: int
    delay_seconds: float
    duration_seconds: float
    created_at: float
    activate_at: float
    expires_at: float
    status: str = PAPER_SCENARIO_PENDING
    target_side: str = PAPER_SCENARIO_TARGET_BUY
    target_quantity: int = 1
    cycle_id: str = ""
    account: PaperAccount = field(default_factory=PaperAccount)
    orders: list[PaperOrder] = field(default_factory=list)
    fills: list[PaperFill] = field(default_factory=list)
    book: PaperMarketBook | None = None
    consumed_buy: dict[int, int] = field(default_factory=dict)
    consumed_sell: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "rule_id": self.rule_id,
            "market_name": self.market_name,
            "buy_price_raw": int(self.buy_price_raw),
            "sell_net_price_raw": int(self.sell_net_price_raw),
            "quantity": int(self.quantity),
            "target_side": self.target_side,
            "target_quantity": int(self.target_quantity),
            "cycle_id": self.cycle_id,
            "delay_seconds": float(self.delay_seconds),
            "duration_seconds": float(self.duration_seconds),
            "created_at": float(self.created_at),
            "activate_at": float(self.activate_at),
            "expires_at": float(self.expires_at),
            "status": self.status,
            "account": self.account.to_dict(),
            "orders": [order.to_dict() for order in self.orders],
            "fills": [fill.to_dict() for fill in self.fills],
            "book": self.book.to_dict() if self.book is not None else None,
            "consumed_buy": {str(key): int(value) for key, value in self.consumed_buy.items()},
            "consumed_sell": {str(key): int(value) for key, value in self.consumed_sell.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperScenario:
        book = data.get("book")
        return cls(
            scenario_id=str(data.get("scenario_id") or ""),
            rule_id=str(data.get("rule_id") or ""),
            market_name=str(data.get("market_name") or ""),
            buy_price_raw=max(0, int(data.get("buy_price_raw") or 0)),
            sell_net_price_raw=max(0, int(data.get("sell_net_price_raw") or 0)),
            quantity=max(0, int(data.get("quantity") or 0)),
            target_side=(
                str(data.get("target_side") or PAPER_SCENARIO_TARGET_BUY).upper()
                if str(data.get("target_side") or PAPER_SCENARIO_TARGET_BUY).upper() in PAPER_SCENARIO_TARGETS
                else PAPER_SCENARIO_TARGET_BUY
            ),
            target_quantity=max(1, int(data.get("target_quantity") or data.get("quantity") or 1)),
            cycle_id=str(data.get("cycle_id") or ""),
            delay_seconds=max(0.0, float(data.get("delay_seconds") or 0)),
            duration_seconds=max(0.0, float(data.get("duration_seconds") or 0)),
            created_at=float(data.get("created_at") or time.time()),
            activate_at=float(data.get("activate_at") or time.time()),
            expires_at=float(data.get("expires_at") or time.time()),
            status=str(data.get("status") or PAPER_SCENARIO_PENDING),
            account=PaperAccount.from_dict(data.get("account") or {}),
            orders=[PaperOrder.from_dict(item) for item in (data.get("orders") or []) if isinstance(item, dict)],
            fills=[PaperFill.from_dict(item) for item in (data.get("fills") or []) if isinstance(item, dict)],
            book=PaperMarketBook.from_dict(book) if isinstance(book, dict) else None,
            consumed_buy={int(key): int(value) for key, value in (data.get("consumed_buy") or {}).items()},
            consumed_sell={int(key): int(value) for key, value in (data.get("consumed_sell") or {}).items()},
        )


def scenario_book(base: PaperMarketBook, scenario: PaperScenario, now: float) -> PaperMarketBook:
    """只向当前目标一侧渐进注入合成流动性，并保证买一始终低于卖一。"""
    start = max(0.0, float(scenario.created_at))
    activate = max(start + 0.001, float(scenario.activate_at))
    progress = max(0.0, min(1.0, (float(now) - start) / (activate - start)))
    tick = PAPER_PRICE_TICK_RAW
    quantity = max(1, int(scenario.target_quantity))
    buy_levels = [BookLevel(level.price_raw, level.amount) for level in base.buy_levels]
    sell_levels = [BookLevel(level.price_raw, level.amount) for level in base.sell_levels]

    def allocations(prices: list[int]) -> list[tuple[int, int]]:
        count = len(prices)
        base_quantity, remainder = divmod(quantity, count)
        quantities = list(reversed([base_quantity + (1 if index < remainder else 0) for index in range(count)]))
        return [(price, amount) for price, amount in zip(prices, quantities, strict=True) if amount > 0]

    if scenario.target_side == PAPER_SCENARIO_TARGET_BUY and scenario.buy_price_raw > 0:
        prices = [scenario.buy_price_raw + tick * 2]
        if progress >= 0.5:
            prices.append(scenario.buy_price_raw + tick)
        if progress >= 0.999:
            prices.append(scenario.buy_price_raw)
        sell_levels.extend(BookLevel(price_raw=price, amount=amount) for price, amount in allocations(prices))
        min_ask = min(level.price_raw for level in sell_levels if level.amount > 0)
        buy_levels = [level for level in buy_levels if level.price_raw < min_ask]
        if not buy_levels:
            buy_levels = [BookLevel(price_raw=max(tick, min_ask - tick), amount=quantity)]
    else:
        target = gross_for_net_raw(scenario.sell_net_price_raw)
        prices = [max(tick, target - tick * 2)]
        if progress >= 0.5:
            prices.append(max(tick, target - tick))
        if progress >= 0.999:
            prices.append(target)
        buy_levels.extend(BookLevel(price_raw=price, amount=amount) for price, amount in allocations(prices))
        max_bid = max(level.price_raw for level in buy_levels if level.amount > 0)
        sell_levels = [level for level in sell_levels if level.price_raw > max_bid]
        if not sell_levels:
            sell_levels = [BookLevel(price_raw=max_bid + tick, amount=quantity)]

    def merged(levels: list[BookLevel], *, reverse: bool) -> list[BookLevel]:
        totals: dict[int, int] = {}
        for level in levels:
            if level.price_raw > 0 and level.amount > 0:
                totals[level.price_raw] = totals.get(level.price_raw, 0) + int(level.amount)
        return [BookLevel(price_raw=price, amount=totals[price]) for price in sorted(totals, reverse=reverse)]

    return PaperMarketBook.from_snapshot(
        BookSnapshot(
            market_name=base.market_name,
            buy_levels=merged(buy_levels, reverse=True),
            sell_levels=merged(sell_levels, reverse=False),
            kind="COMMODITY",
        ),
        observed_at=now,
        synthetic=True,
    )


def net_for_gross_raw(price_raw: int) -> tuple[int, int]:
    """返回（单件税后净额，单件手续费）。"""
    net = seller_receives_raw(price_raw)
    return net, max(0, int(price_raw) - int(net))
