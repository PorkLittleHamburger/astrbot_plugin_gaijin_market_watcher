"""共享模拟账户、纸面订单与撮合引擎。"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .paper_models import (
    DEFAULT_PAPER_CASH_RAW,
    DEFAULT_PAPER_MAX_PRICE_RAW,
    DEFAULT_PAPER_MIN_PRICE_RAW,
    MAX_PAPER_ORDER_QUANTITY,
    PAPER_ORDER_CANCELLED,
    PAPER_ORDER_FILLED,
    PAPER_ORDER_PARTIALLY_FILLED,
    PAPER_PRICE_TICK_RAW,
    PAPER_SCENARIO_ACTIVE,
    PAPER_SCENARIO_ENDED,
    PAPER_SCENARIO_PENDING,
    PAPER_SCENARIO_TARGET_BUY,
    PAPER_SCENARIO_TARGET_SELL,
    PAPER_SIDE_BUY,
    PAPER_SIDE_SELL,
    PAPER_SOURCE_MANUAL,
    PAPER_SOURCE_RULE,
    PaperAccount,
    PaperFill,
    PaperLot,
    PaperMarketBook,
    PaperOrder,
    PaperScenario,
    scenario_book,
)
from .trade_models import BookLevel, BookSnapshot, fmt_gjn, raw_to_decimal, seller_receives_raw

PAPER_STATE_FILE = "paper_state.json"
MAX_PAPER_FILL_HISTORY = 3000
SHANGHAI = ZoneInfo("Asia/Shanghai")


class PaperError(RuntimeError):
    """可由用户修正的模拟交易业务错误。"""


class PaperStorageError(RuntimeError):
    """模拟交易状态无法安全落盘。"""


def _integer_value(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise PaperError(f"{label}必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise PaperError(f"{label}必须是整数")
    text = str(value or "").strip()
    if not text or any(char not in "+-0123456789" for char in text):
        raise PaperError(f"{label}必须是整数")
    try:
        return int(text)
    except ValueError as exc:
        raise PaperError(f"{label}必须是整数") from exc


@dataclass(slots=True)
class _Liquidity:
    version: str
    buy_levels: list[BookLevel]
    sell_levels: list[BookLevel]

    @classmethod
    def from_book(cls, book: PaperMarketBook) -> _Liquidity:
        return cls(
            version=book.version,
            buy_levels=[BookLevel(level.price_raw, level.amount) for level in book.buy_levels],
            sell_levels=[BookLevel(level.price_raw, level.amount) for level in book.sell_levels],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "buy_levels": [
                {"price_raw": int(level.price_raw), "amount": int(level.amount)} for level in self.buy_levels
            ],
            "sell_levels": [
                {"price_raw": int(level.price_raw), "amount": int(level.amount)} for level in self.sell_levels
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _Liquidity:
        def levels(items: Any) -> list[BookLevel]:
            return [
                BookLevel(price_raw=int(item["price_raw"]), amount=int(item["amount"]))
                for item in (items or [])
                if isinstance(item, dict) and int(item.get("price_raw") or 0) > 0 and int(item.get("amount") or 0) >= 0
            ]

        return cls(
            version=str(data.get("version") or ""),
            buy_levels=levels(data.get("buy_levels")),
            sell_levels=levels(data.get("sell_levels")),
        )


@dataclass(slots=True)
class _Scope:
    account: PaperAccount
    orders: list[PaperOrder]
    fills: list[PaperFill]
    book: PaperMarketBook | None
    liquidity: _Liquidity | None
    scenario_id: str = ""


class PaperMatchingEngine:
    """价格优先、同价时间优先的纸面订单撮合器。"""

    def __init__(self, engine: PaperTradingEngine) -> None:
        self._engine = engine

    def match_market_orders(self, market_name: str, *, scenario_id: str = "") -> list[PaperFill]:
        engine = self._engine
        scope = engine._scope(scenario_id)
        liquidity = engine._scope_liquidity(scope, market_name)
        if liquidity is None:
            return []
        produced: list[PaperFill] = []
        candidates = sorted(
            [
                order
                for order in scope.orders
                if order.is_open
                and order.market_name == market_name
                and (scenario_id or not order.rule_id or engine.active_scenario_for_rule(order.rule_id) is None)
            ],
            key=lambda order: (
                0 if order.side == PAPER_SIDE_BUY else 1,
                -order.price_raw if order.side == PAPER_SIDE_BUY else order.price_raw,
                order.created_at,
                order.order_id,
            ),
        )
        for order in candidates:
            produced.extend(self.match_order(order, scope, time.time()))
        if produced:
            scope.account.touch()
            if scenario_id:
                scenario = engine.scenarios[scenario_id]
                scenario.orders = scope.orders
                scenario.fills = scope.fills
                scenario.account = scope.account
        return produced

    def match_order(self, order: PaperOrder, scope: _Scope, now_ts: float) -> list[PaperFill]:
        engine = self._engine
        liquidity = engine._scope_liquidity(scope, order.market_name)
        if liquidity is None:
            return []
        levels = liquidity.sell_levels if order.side == PAPER_SIDE_BUY else liquidity.buy_levels
        fills: list[PaperFill] = []
        remaining = order.remaining_quantity
        for level in levels:
            if remaining <= 0:
                break
            if level.amount <= 0:
                continue
            if order.side == PAPER_SIDE_BUY and level.price_raw > order.price_raw:
                continue
            if order.side == PAPER_SIDE_SELL and level.price_raw < order.price_raw:
                continue
            take = min(remaining, level.amount)
            if take <= 0:
                continue
            fill = engine._apply_fill(order, scope, level.price_raw, take, now_ts)
            level.amount -= take
            if scope.scenario_id:
                scenario = engine.scenarios[scope.scenario_id]
                consumed = scenario.consumed_sell if order.side == PAPER_SIDE_BUY else scenario.consumed_buy
                consumed[level.price_raw] = consumed.get(level.price_raw, 0) + take
            remaining -= take
            fills.append(fill)
        if fills:
            order.updated_at = now_ts
            order.status = PAPER_ORDER_FILLED if order.remaining_quantity == 0 else PAPER_ORDER_PARTIALLY_FILLED
            if scope.scenario_id:
                scope.fills.extend(fills)
            else:
                engine.fills.extend(fills)
                engine.fills = engine.fills[-MAX_PAPER_FILL_HISTORY:]
            scope.account.touch()
        return fills


class PaperTradingEngine:
    def __init__(self, storage, logger, *, starting_cash_raw: int = DEFAULT_PAPER_CASH_RAW, load: bool = True) -> None:
        self.storage = storage
        self.logger = logger
        self.starting_cash_raw = max(0, int(starting_cash_raw))
        self.account = PaperAccount(
            starting_cash_raw=self.starting_cash_raw,
            cash_available_raw=self.starting_cash_raw,
        )
        self.price_min_raw = DEFAULT_PAPER_MIN_PRICE_RAW
        self.price_max_raw = DEFAULT_PAPER_MAX_PRICE_RAW
        self.orders: list[PaperOrder] = []
        self.fills: list[PaperFill] = []
        self.scenarios: dict[str, PaperScenario] = {}
        self._market_books: dict[str, PaperMarketBook] = {}
        self._market_observed_at: dict[str, float] = {}
        self._liquidity: dict[str, _Liquidity] = {}
        self._scenario_liquidity: dict[str, _Liquidity] = {}
        self._order_seq = 1
        self._fill_seq = 1
        self._scenario_seq = 1
        self.matching = PaperMatchingEngine(self)
        self._last_saved_data = self._serialize_state()
        if load:
            self.reload()

    # ------------------------------------------------------------------ 持久化
    def reload(self) -> None:
        raw = self.storage.load_aux_json(PAPER_STATE_FILE, {})
        if not isinstance(raw, dict) or not raw:
            self._last_saved_data = copy.deepcopy(self._serialize_state())
            self._save()
            return
        try:
            self._apply_state(raw)
            self._last_saved_data = copy.deepcopy(self._serialize_state())
        except Exception as exc:
            self.logger.warning(f"[GJM Paper] 模拟账户状态损坏，已重建: {exc}")
            self.reset_account(self.starting_cash_raw, persist=False)
            self._last_saved_data = copy.deepcopy(self._serialize_state())
            self._save()

    def _serialize_state(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "starting_cash_raw": int(self.starting_cash_raw),
            "price_min_raw": int(self.price_min_raw),
            "price_max_raw": int(self.price_max_raw),
            "account": self.account.to_dict(),
            "orders": [order.to_dict() for order in self.orders],
            "fills": [fill.to_dict() for fill in self.fills[-MAX_PAPER_FILL_HISTORY:]],
            "scenarios": [scenario.to_dict() for scenario in self.scenarios.values()],
            "market_books": {name: book.to_dict() for name, book in self._market_books.items()},
            "market_observed_at": {name: float(observed) for name, observed in self._market_observed_at.items()},
            "liquidity": {name: liquidity.to_dict() for name, liquidity in self._liquidity.items()},
            "order_seq": int(self._order_seq),
            "fill_seq": int(self._fill_seq),
            "scenario_seq": int(self._scenario_seq),
        }

    def _apply_state(self, raw: dict[str, Any]) -> None:
        self.starting_cash_raw = max(
            0,
            int(raw.get("starting_cash_raw", DEFAULT_PAPER_CASH_RAW)),
        )
        self.price_min_raw = max(1, int(raw.get("price_min_raw") or DEFAULT_PAPER_MIN_PRICE_RAW))
        self.price_max_raw = max(self.price_min_raw, int(raw.get("price_max_raw") or DEFAULT_PAPER_MAX_PRICE_RAW))
        self.account = PaperAccount.from_dict(raw.get("account") or {})
        self.orders = [PaperOrder.from_dict(item) for item in (raw.get("orders") or []) if isinstance(item, dict)]
        self.fills = [PaperFill.from_dict(item) for item in (raw.get("fills") or []) if isinstance(item, dict)]
        self.scenarios = {
            item["scenario_id"]: PaperScenario.from_dict(item)
            for item in (raw.get("scenarios") or [])
            if isinstance(item, dict) and item.get("scenario_id")
        }
        self._market_books = {
            name: PaperMarketBook.from_dict(item)
            for name, item in (raw.get("market_books") or {}).items()
            if isinstance(item, dict)
        }
        self._market_observed_at = {name: float(value) for name, value in (raw.get("market_observed_at") or {}).items()}
        self._liquidity = {
            name: _Liquidity.from_dict(item)
            for name, item in (raw.get("liquidity") or {}).items()
            if isinstance(item, dict)
        }
        for name, book in self._market_books.items():
            self._liquidity.setdefault(name, _Liquidity.from_book(book))
        self._scenario_liquidity = {}
        for scenario_id, scenario in self.scenarios.items():
            if scenario.book is None or scenario.status == PAPER_SCENARIO_ENDED:
                continue
            self._scenario_liquidity[scenario_id] = _Liquidity.from_book(scenario.book)
        self._order_seq = max(1, int(raw.get("order_seq") or 1))
        self._fill_seq = max(1, int(raw.get("fill_seq") or 1))
        self._scenario_seq = max(1, int(raw.get("scenario_seq") or 1))

    def _save(self) -> None:
        data = self._serialize_state()
        if not self.storage.save_aux_json(PAPER_STATE_FILE, data):
            if self._last_saved_data is not None:
                self._apply_state(copy.deepcopy(self._last_saved_data))
            raise PaperStorageError("模拟交易状态写入失败")
        self._last_saved_data = copy.deepcopy(data)

    def set_price_limits(self, min_price_raw: int, max_price_raw: int) -> None:
        self.price_min_raw = max(1, int(min_price_raw))
        self.price_max_raw = max(self.price_min_raw, int(max_price_raw))
        self._save()

    def reset_account(self, starting_cash_raw: int | None = None, *, persist: bool = True) -> dict[str, Any]:
        cash = self.starting_cash_raw if starting_cash_raw is None else max(0, int(starting_cash_raw))
        self.starting_cash_raw = cash
        self.account = PaperAccount(starting_cash_raw=cash, cash_available_raw=cash)
        self.orders = []
        self.fills = []
        self.scenarios = {}
        self._scenario_liquidity = {}
        self._order_seq = 1
        self._fill_seq = 1
        self._scenario_seq = 1
        if persist:
            self._save()
        return self.account_view()

    # ------------------------------------------------------------------ 视图
    def _market_book_view(self, book: PaperMarketBook | None) -> dict[str, Any] | None:
        if book is None:
            return None
        return book.to_dict()

    def _available_sell_quantity(self, market_name: str, *, scenario_id: str = "") -> int:
        scope = self._scope(scenario_id)
        reserved = sum(
            order.remaining_quantity
            for order in scope.orders
            if order.is_open and order.side == PAPER_SIDE_SELL and order.market_name == market_name
        )
        if scenario_id:
            scenario = self.scenarios.get(scenario_id)
            if scenario is not None:
                reserved += sum(
                    order.remaining_quantity
                    for order in self.orders
                    if (
                        order.is_open
                        and order.side == PAPER_SIDE_SELL
                        and order.market_name == market_name
                        and order.rule_id != scenario.rule_id
                    )
                )
        return max(0, scope.account.inventory_quantity(market_name) - reserved)

    def _lot_view(self, lot: PaperLot) -> dict[str, Any]:
        return {
            **lot.to_dict(),
            "unit_cost": fmt_gjn(lot.unit_cost_raw),
            "cost_basis": fmt_gjn(lot.unit_cost_raw * lot.remaining_quantity),
        }

    def _order_view(self, order: PaperOrder) -> dict[str, Any]:
        return {
            **order.to_dict(),
            "price": fmt_gjn(order.price_raw),
            "remaining_quantity": order.remaining_quantity,
            "average_fill_price": fmt_gjn(order.average_fill_price_raw),
        }

    def _fill_view(self, fill: PaperFill) -> dict[str, Any]:
        return {
            **fill.to_dict(),
            "price": fmt_gjn(fill.price_raw),
            "gross": fmt_gjn(fill.gross_raw),
            "fee": fmt_gjn(fill.fee_raw),
            "net": fmt_gjn(fill.net_raw),
            "cost_basis": fmt_gjn(fill.cost_basis_raw),
            "realized_pnl": f"{raw_to_decimal(fill.realized_pnl_raw):+.2f}",
        }

    def rollover_day(self, now: float | None = None) -> None:
        now_ts = time.time() if now is None else float(now)
        today = datetime.fromtimestamp(now_ts, SHANGHAI).date().isoformat()
        if self.account.daily_day != today:
            self.account.daily_day = today
            self.account.daily_buy_raw = 0
            self.account.touch()
            self._save()

    def account_view(self, *, scenario_id: str = "") -> dict[str, Any]:
        scope = self._scope(scenario_id)
        inventory: list[dict[str, Any]] = []
        unrealized = 0
        market_value = 0
        for market in sorted({lot.market_name for lot in scope.account.lots if lot.remaining_quantity > 0}):
            quantity = scope.account.inventory_quantity(market)
            cost = scope.account.inventory_cost_raw(market)
            book = (
                scope.book
                if scope.book is not None and scope.book.market_name == market
                else self._market_books.get(market)
            )
            mark_price = seller_receives_raw(book.best_buy) if book and book.best_buy else 0
            inventory_value = mark_price * quantity if mark_price else cost
            unrealized += inventory_value - cost
            market_value += inventory_value
            inventory.append(
                {
                    "market_name": market,
                    "quantity": quantity,
                    "available_quantity": self._available_sell_quantity(market, scenario_id=scenario_id),
                    "cost_basis_raw": cost,
                    "cost_basis": fmt_gjn(cost),
                    "average_cost": fmt_gjn(int(cost / quantity) if quantity else 0),
                    "market_value_raw": inventory_value,
                    "market_value": fmt_gjn(inventory_value),
                    "lots": [
                        self._lot_view(lot)
                        for lot in scope.account.lots
                        if lot.market_name == market and lot.remaining_quantity > 0
                    ],
                }
            )
        cash_total = scope.account.total_cash_raw
        return {
            "starting_cash_raw": scope.account.starting_cash_raw,
            "starting_cash": fmt_gjn(scope.account.starting_cash_raw),
            "cash_available_raw": scope.account.cash_available_raw,
            "cash_available": fmt_gjn(scope.account.cash_available_raw),
            "cash_reserved_raw": scope.account.cash_reserved_raw,
            "cash_reserved": fmt_gjn(scope.account.cash_reserved_raw),
            "cash_total_raw": cash_total,
            "cash_total": fmt_gjn(cash_total),
            "realized_pnl_raw": scope.account.realized_pnl_raw,
            "realized_pnl": f"{raw_to_decimal(scope.account.realized_pnl_raw):+.2f}",
            "daily_buy_raw": scope.account.daily_buy_raw,
            "daily_buy": fmt_gjn(scope.account.daily_buy_raw),
            "unrealized_pnl_raw": unrealized,
            "unrealized_pnl": f"{raw_to_decimal(unrealized):+.2f}",
            "inventory_value_raw": market_value,
            "inventory_value": fmt_gjn(market_value),
            "equity_raw": cash_total + market_value,
            "equity": fmt_gjn(cash_total + market_value),
            "inventory": inventory,
            "version": int(scope.account.version),
        }

    def _scope(self, scenario_id: str = "") -> _Scope:
        if not scenario_id:
            return _Scope(
                account=self.account,
                orders=self.orders,
                fills=self.fills,
                book=None,
                liquidity=None,
                scenario_id="",
            )
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            raise PaperError("场景不存在")
        return _Scope(
            account=scenario.account,
            orders=scenario.orders,
            fills=scenario.fills,
            book=scenario.book,
            liquidity=self._scenario_liquidity.get(scenario_id),
            scenario_id=scenario_id,
        )

    def _ensure_scope_book(self, scope: _Scope, market_name: str) -> PaperMarketBook | None:
        if scope.scenario_id:
            scenario = self.scenarios.get(scope.scenario_id)
            if scenario is None:
                return None
            if scenario.book is None or scenario.book.market_name != market_name:
                scenario.book = self._market_books.get(market_name)
            return scenario.book
        return self._market_books.get(market_name)

    def _scope_liquidity(self, scope: _Scope, market_name: str) -> _Liquidity | None:
        if scope.scenario_id:
            return self._scenario_liquidity.get(scope.scenario_id)
        return self._liquidity.get(market_name)

    def observe_market(self, book: BookSnapshot, *, observed_at: float | None = None) -> list[PaperFill]:
        now = time.time() if observed_at is None else float(observed_at)
        candidate = PaperMarketBook.from_snapshot(book, observed_at=now)
        last_observed = self._market_observed_at.get(book.market_name, 0.0)
        if now < last_observed:
            return []
        self._market_observed_at[book.market_name] = now
        old = self._market_books.get(book.market_name)
        if old is not None and old.version == candidate.version:
            return []
        self._market_books[book.market_name] = candidate
        self._liquidity[book.market_name] = _Liquidity.from_book(candidate)
        fills = self.matching.match_market_orders(book.market_name, scenario_id="")
        if fills or any(order.is_open for order in self.orders):
            self._save()
        return fills

    def current_book(self, market_name: str, *, scenario_id: str = "") -> PaperMarketBook | None:
        if scenario_id:
            scenario = self.scenarios.get(scenario_id)
            return scenario.book if scenario is not None else None
        return self._market_books.get(market_name)

    def available_inventory(self, market_name: str, *, scenario_id: str = "") -> int:
        return self._available_sell_quantity(market_name, scenario_id=scenario_id)

    def _view_scope_id(self, scenario_id: str) -> str:
        """把已结束场景视为真实盘口视图，避免轮询停留在旧场景快照。"""
        if not scenario_id:
            return ""
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            raise PaperError("场景已结束或不存在")
        return "" if scenario.status == PAPER_SCENARIO_ENDED else scenario_id

    def market_view(self, market_name: str, *, scenario_id: str = "") -> dict[str, Any]:
        scenario_id = self._view_scope_id(scenario_id)
        if scenario_id and self.scenarios[scenario_id].market_name != market_name:
            scenario_id = ""
        if scenario_id:
            scenario = self.scenarios[scenario_id]
            book = scenario.book
            orders = [order for order in scenario.orders if order.market_name == market_name]
            account = self._account_for_scenario(scenario)
        else:
            book = self._market_books.get(market_name)
            orders = [order for order in self.orders if order.market_name == market_name]
            account = self.account_view()
        return {
            "market_name": market_name,
            "book": self._market_book_view(book),
            "orders": [self._order_view(order) for order in orders],
            "account": account,
            "book_version": book.version if book else "",
            "observed_at": book.observed_at if book else 0,
        }

    def _account_for_scenario(self, scenario: PaperScenario) -> dict[str, Any]:
        return self.account_view(scenario_id=scenario.scenario_id)

    def lookup_client_order(
        self,
        client_order_id: str,
        *,
        market_name: str,
        side: str,
        price_raw: int,
        quantity: int,
        scenario_id: str = "",
    ) -> PaperOrder | None:
        if not client_order_id:
            return None
        orders = self._scope(scenario_id).orders
        existing = next((order for order in orders if order.client_order_id == client_order_id), None)
        if existing is not None and (
            existing.market_name != str(market_name or "").strip()
            or existing.side != str(side or "").upper()
            or existing.price_raw != _integer_value(price_raw, "价格")
            or existing.quantity != _integer_value(quantity, "数量")
        ):
            raise PaperError("client_order_id 已用于参数不同的订单")
        return existing

    def place_order(
        self,
        *,
        market_name: str,
        side: str,
        price_raw: int,
        quantity: int,
        source: str = PAPER_SOURCE_MANUAL,
        rule_id: str = "",
        cycle_id: str = "",
        client_order_id: str = "",
        scenario_id: str = "",
        now: float | None = None,
    ) -> tuple[PaperOrder, list[PaperFill]]:
        now_ts = time.time() if now is None else float(now)
        side = str(side or "").upper()
        if side not in (PAPER_SIDE_BUY, PAPER_SIDE_SELL):
            raise PaperError("订单方向必须是 BUY 或 SELL")
        market_name = str(market_name or "").strip()
        if not market_name:
            raise PaperError("缺少商品 market_name")
        price_raw = _integer_value(price_raw, "价格")
        quantity = _integer_value(quantity, "数量")
        if price_raw <= 0 or price_raw % PAPER_PRICE_TICK_RAW:
            raise PaperError("价格必须是 0.01 GJN 的整数倍")
        if not (self.price_min_raw <= price_raw <= self.price_max_raw):
            raise PaperError("价格超出市场允许范围")
        if not 1 <= quantity <= MAX_PAPER_ORDER_QUANTITY:
            raise PaperError(f"数量必须在 1 到 {MAX_PAPER_ORDER_QUANTITY} 之间")
        scope = self._scope(scenario_id)
        existing = self.lookup_client_order(
            client_order_id,
            market_name=market_name,
            side=side,
            price_raw=price_raw,
            quantity=quantity,
            scenario_id=scenario_id,
        )
        if existing is not None:
            return existing, []
        scenario = self.scenarios.get(scenario_id) if scenario_id else None
        if scenario is not None and scenario.status == PAPER_SCENARIO_ENDED:
            raise PaperError("场景已结束")
        if scenario is not None and scenario.market_name != market_name:
            raise PaperError("场景仅支持当前规则绑定的商品")
        if scope.book is None:
            scope.book = self._ensure_scope_book(scope, market_name)
        if side == PAPER_SIDE_BUY:
            required = price_raw * quantity
            if scope.account.cash_available_raw < required:
                raise PaperError(f"模拟账户可用 GJN 不足：需要 {fmt_gjn(required)}")
        else:
            available = self._available_sell_quantity(market_name, scenario_id=scenario_id)
            if available < quantity:
                raise PaperError(f"可用库存不足：需要 {quantity}，当前 {available}")

        order = PaperOrder(
            order_id=f"PO{self._order_seq:08X}",
            client_order_id=str(client_order_id or ""),
            market_name=market_name,
            side=side,
            price_raw=price_raw,
            quantity=quantity,
            source=source,
            rule_id=str(rule_id or ""),
            cycle_id=str(cycle_id or ""),
            scenario_id=str(scenario_id or ""),
            reserved_cash_raw=price_raw * quantity if side == PAPER_SIDE_BUY else 0,
            created_at=now_ts,
            updated_at=now_ts,
        )
        self._order_seq += 1
        if side == PAPER_SIDE_BUY:
            scope.account.cash_available_raw -= order.reserved_cash_raw
            scope.account.cash_reserved_raw += order.reserved_cash_raw
        scope.orders.append(order)
        fills = self.matching.match_order(order, scope, now_ts)
        scope.account.touch()
        if scenario_id:
            scenario = self.scenarios[scenario_id]
            scenario.orders = scope.orders
            scenario.fills = scope.fills
            scenario.account = scope.account
            if fills:
                self._sync_scenario_book(scenario, now_ts)
        self._save()
        return order, fills

    def cancel_orders_for_rule(self, rule_id: str, *, scenario_id: str = "") -> list[PaperOrder]:
        scope = self._scope(scenario_id)
        cancelled: list[PaperOrder] = []
        for order in list(scope.orders):
            if order.rule_id == rule_id and order.is_open:
                self.cancel_order(order.order_id, scenario_id=scenario_id)
                cancelled.append(order)
        return cancelled

    def cancel_order(self, order_id: str, *, scenario_id: str = "", now: float | None = None) -> PaperOrder:
        now_ts = time.time() if now is None else float(now)
        scenario = self.scenarios.get(scenario_id) if scenario_id else None
        if scenario is not None and scenario.status == PAPER_SCENARIO_ENDED:
            raise PaperError("场景已结束")
        scope = self._scope(scenario_id)
        order = next((item for item in scope.orders if item.order_id == order_id), None)
        if order is None:
            raise PaperError("找不到模拟订单")
        if not order.is_open:
            return order
        if order.side == PAPER_SIDE_BUY and order.remaining_quantity > 0:
            release = order.remaining_quantity * order.price_raw
            scope.account.cash_reserved_raw = max(0, scope.account.cash_reserved_raw - release)
            scope.account.cash_available_raw += release
            order.reserved_cash_raw = max(0, order.reserved_cash_raw - release)
        order.status = PAPER_ORDER_CANCELLED
        order.cancelled_at = now_ts
        order.updated_at = now_ts
        scope.account.touch()
        if scenario_id:
            scenario = self.scenarios[scenario_id]
            scenario.orders = scope.orders
            scenario.account = scope.account
        self._save()
        return order

    def _apply_fill(self, order: PaperOrder, scope: _Scope, price_raw: int, quantity: int, now_ts: float) -> PaperFill:
        gross = int(price_raw) * int(quantity)
        fill_id = f"PF{self._fill_seq:08X}"
        self._fill_seq += 1
        if order.side == PAPER_SIDE_BUY:
            reserved_release = int(order.price_raw) * int(quantity)
            scope.account.cash_reserved_raw = max(0, scope.account.cash_reserved_raw - reserved_release)
            scope.account.cash_available_raw += max(0, reserved_release - gross)
            order.reserved_cash_raw = max(0, order.reserved_cash_raw - reserved_release)
            order.filled_quantity += quantity
            order.filled_amount_raw += gross
            lot = PaperLot(
                lot_id=f"PL{self._fill_seq:08X}",
                market_name=order.market_name,
                quantity=quantity,
                remaining_quantity=quantity,
                unit_cost_raw=price_raw,
                acquired_at=now_ts,
                source_order_id=order.order_id,
                rule_id=order.rule_id,
                scenario_id=order.scenario_id,
            )
            self._fill_seq += 1
            scope.account.lots.append(lot)
            if order.source == PAPER_SOURCE_RULE:
                today = datetime.fromtimestamp(now_ts, SHANGHAI).date().isoformat()
                if scope.account.daily_day != today:
                    scope.account.daily_day = today
                    scope.account.daily_buy_raw = 0
                scope.account.daily_buy_raw += gross
            return PaperFill(
                fill_id=fill_id,
                order_id=order.order_id,
                market_name=order.market_name,
                side=PAPER_SIDE_BUY,
                price_raw=price_raw,
                quantity=quantity,
                gross_raw=gross,
                fee_raw=0,
                net_raw=0,
                cost_basis_raw=gross,
                source=order.source,
                rule_id=order.rule_id,
                cycle_id=order.cycle_id,
                scenario_id=order.scenario_id,
                created_at=now_ts,
            )

        # SELL：FIFO 消耗账户中最旧的批次。
        remaining = quantity
        cost = 0
        for lot in scope.account.lots:
            if remaining <= 0:
                break
            if lot.market_name != order.market_name or lot.remaining_quantity <= 0:
                continue
            take = min(remaining, lot.remaining_quantity)
            lot.remaining_quantity -= take
            remaining -= take
            cost += take * lot.unit_cost_raw
        if remaining:
            raise PaperError("模拟库存与卖单冻结数量不一致")
        net_per_unit = seller_receives_raw(price_raw)
        net = net_per_unit * quantity
        fee = max(0, gross - net)
        scope.account.cash_available_raw += net
        scope.account.realized_pnl_raw += net - cost
        order.filled_quantity += quantity
        order.filled_amount_raw += gross
        return PaperFill(
            fill_id=fill_id,
            order_id=order.order_id,
            market_name=order.market_name,
            side=PAPER_SIDE_SELL,
            price_raw=price_raw,
            quantity=quantity,
            gross_raw=gross,
            fee_raw=fee,
            net_raw=net,
            cost_basis_raw=cost,
            realized_pnl_raw=net - cost,
            source=order.source,
            rule_id=order.rule_id,
            cycle_id=order.cycle_id,
            scenario_id=order.scenario_id,
            created_at=now_ts,
        )

    # ------------------------------------------------------------------ 场景
    @staticmethod
    def _refresh_scenario_objective(scenario: PaperScenario) -> bool:
        rule_fills = [
            fill
            for fill in scenario.fills
            if fill.rule_id == scenario.rule_id and (not scenario.cycle_id or fill.cycle_id == scenario.cycle_id)
        ]
        buy_filled = sum(fill.quantity for fill in rule_fills if fill.side == PAPER_SIDE_BUY)
        sell_filled = sum(fill.quantity for fill in rule_fills if fill.side == PAPER_SIDE_SELL)
        buy_gap = max(0, int(scenario.quantity) - buy_filled)
        sell_gap = max(0, buy_filled - sell_filled)
        if sell_gap > 0:
            target_side, target_quantity = PAPER_SCENARIO_TARGET_SELL, sell_gap
        elif buy_gap > 0:
            target_side, target_quantity = PAPER_SCENARIO_TARGET_BUY, buy_gap
        else:
            return False
        scenario.target_side = target_side
        scenario.target_quantity = max(1, int(target_quantity))
        return True

    def create_scenario(
        self,
        *,
        rule_id: str,
        market_name: str,
        buy_price_raw: int,
        sell_net_price_raw: int,
        quantity: int,
        cycle_id: str = "",
        delay_seconds: float = 5.0,
        duration_seconds: float = 30.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        if not rule_id:
            raise PaperError("缺少规则 ID")
        if buy_price_raw <= 0 or sell_net_price_raw <= 0 or quantity <= 0:
            raise PaperError("场景必须包含有效的买卖价格和数量")
        if not (1.0 <= float(delay_seconds) <= 60.0):
            raise PaperError("场景延迟必须在 1 到 60 秒之间")
        if not (5.0 <= float(duration_seconds) <= 600.0):
            raise PaperError("场景时长必须在 5 到 600 秒之间")
        active = next(
            (
                item
                for item in self.scenarios.values()
                if item.rule_id == rule_id and item.status != PAPER_SCENARIO_ENDED
            ),
            None,
        )
        if active is not None:
            raise PaperError("该规则已有进行中的场景")
        base = self._market_books.get(market_name)
        if base is None:
            raise PaperError("请先加载真实盘口")
        scenario_id = f"SC{self._scenario_seq:08X}"
        self._scenario_seq += 1
        activate_at = now_ts + float(delay_seconds)
        scenario_orders = [
            PaperOrder.from_dict({**order.to_dict(), "scenario_id": scenario_id})
            for order in self.orders
            if order.rule_id == rule_id and order.is_open and (not cycle_id or order.cycle_id == cycle_id)
        ]
        scenario_fills = [
            PaperFill.from_dict(fill.to_dict())
            for fill in self.fills
            if fill.rule_id == rule_id and (not cycle_id or fill.cycle_id == cycle_id)
        ]
        scenario = PaperScenario(
            scenario_id=scenario_id,
            rule_id=rule_id,
            market_name=market_name,
            buy_price_raw=max(0, int(buy_price_raw)),
            sell_net_price_raw=max(0, int(sell_net_price_raw)),
            quantity=max(1, int(quantity)),
            target_quantity=1,
            cycle_id=str(cycle_id or ""),
            delay_seconds=float(delay_seconds),
            duration_seconds=float(duration_seconds),
            created_at=now_ts,
            activate_at=activate_at,
            expires_at=activate_at + float(duration_seconds),
            status=PAPER_SCENARIO_PENDING,
            account=self.account.clone(),
            orders=scenario_orders,
            fills=scenario_fills,
            book=base,
        )
        if not self._refresh_scenario_objective(scenario):
            raise PaperError("规则当前没有可模拟的买卖缺口")
        scenario.book = scenario_book(base, scenario, now_ts)
        self.scenarios[scenario_id] = scenario
        self._scenario_liquidity[scenario_id] = _Liquidity.from_book(scenario.book)
        self._save()
        return self.scenario_view(scenario_id)

    def _sync_scenario_book(self, scenario: PaperScenario, now_ts: float) -> None:
        base = self._market_books.get(scenario.market_name)
        if base is None:
            return
        consumed_book = self._apply_scenario_consumption(scenario_book(base, scenario, now_ts), scenario)
        scenario.book = consumed_book
        self._scenario_liquidity[scenario.scenario_id] = _Liquidity.from_book(consumed_book)

    @staticmethod
    def _apply_scenario_consumption(book: PaperMarketBook, scenario: PaperScenario) -> PaperMarketBook:
        def subtract(levels: list[BookLevel], consumed: dict[int, int]) -> None:
            for price_raw, amount in consumed.items():
                remaining = int(amount)
                for level in levels:
                    if remaining <= 0:
                        break
                    if level.price_raw != price_raw or level.amount <= 0:
                        continue
                    take = min(level.amount, remaining)
                    level.amount -= take
                    remaining -= take

        subtract(book.buy_levels, scenario.consumed_buy)
        subtract(book.sell_levels, scenario.consumed_sell)
        return PaperMarketBook.from_snapshot(
            BookSnapshot(
                market_name=book.market_name,
                buy_levels=[BookLevel(level.price_raw, level.amount) for level in book.buy_levels if level.amount > 0],
                sell_levels=[
                    BookLevel(level.price_raw, level.amount) for level in book.sell_levels if level.amount > 0
                ],
                kind="COMMODITY",
            ),
            observed_at=book.observed_at,
            synthetic=True,
        )

    def advance_scenarios(self, now: float | None = None) -> list[PaperFill]:
        now_ts = time.time() if now is None else float(now)
        produced: list[PaperFill] = []
        changed = False
        for scenario in list(self.scenarios.values()):
            if scenario.status == PAPER_SCENARIO_ENDED:
                continue
            if now_ts >= scenario.expires_at:
                scenario.status = PAPER_SCENARIO_ENDED
                self._scenario_liquidity.pop(scenario.scenario_id, None)
                changed = True
                continue
            base = self._market_books.get(scenario.market_name)
            if base is None:
                continue
            before = (scenario.target_side, scenario.target_quantity)
            self._refresh_scenario_objective(scenario)
            if before != (scenario.target_side, scenario.target_quantity):
                changed = True
            next_book = self._apply_scenario_consumption(scenario_book(base, scenario, now_ts), scenario)
            if next_book.version == (scenario.book.version if scenario.book else ""):
                if scenario.status == PAPER_SCENARIO_PENDING and now_ts >= scenario.activate_at:
                    scenario.status = PAPER_SCENARIO_ACTIVE
                    changed = True
                continue
            scenario.book = next_book
            scenario.status = PAPER_SCENARIO_ACTIVE if now_ts >= scenario.activate_at else PAPER_SCENARIO_PENDING
            self._scenario_liquidity[scenario.scenario_id] = _Liquidity.from_book(next_book)
            produced.extend(self.matching.match_market_orders(scenario.market_name, scenario_id=scenario.scenario_id))
            if produced:
                self._sync_scenario_book(scenario, now_ts)
            changed = True
        if changed or produced:
            self._save()
        return produced

    def scenario_view(self, scenario_id: str) -> dict[str, Any]:
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            raise PaperError("场景不存在")
        now = time.time()
        return {
            **scenario.to_dict(),
            "book": scenario.book.to_dict() if scenario.book else None,
            "account": self.account_view(scenario_id=scenario_id),
            "orders": [self._order_view(order) for order in scenario.orders],
            "fills": [self._fill_view(fill) for fill in scenario.fills[-100:]],
            "remaining_seconds": max(0.0, scenario.expires_at - now)
            if scenario.status != PAPER_SCENARIO_ENDED
            else 0.0,
            "activate_in_seconds": max(0.0, scenario.activate_at - now)
            if scenario.status == PAPER_SCENARIO_PENDING
            else 0.0,
        }

    def close_scenario(self, scenario_id: str, *, now: float | None = None) -> dict[str, Any]:
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            raise PaperError("场景不存在")
        scenario.status = PAPER_SCENARIO_ENDED
        self._scenario_liquidity.pop(scenario_id, None)
        self._save()
        return self.scenario_view(scenario_id)

    def active_scenario_for_rule(self, rule_id: str) -> PaperScenario | None:
        return next(
            (
                item
                for item in self.scenarios.values()
                if item.rule_id == rule_id and item.status in (PAPER_SCENARIO_PENDING, PAPER_SCENARIO_ACTIVE)
            ),
            None,
        )

    def scenario_scope(self, rule_id: str) -> _Scope:
        scenario = self.active_scenario_for_rule(rule_id)
        if scenario is None:
            return _Scope(self.account, self.orders, self.fills, None, None, "")
        return _Scope(
            scenario.account,
            scenario.orders,
            scenario.fills,
            scenario.book,
            self._scenario_liquidity.get(scenario.scenario_id),
            scenario.scenario_id,
        )

    # ------------------------------------------------------------------ 查询
    def overview(self, *, scenario_id: str = "", order_limit: int = 100, fill_limit: int = 100) -> dict[str, Any]:
        scenario_id = self._view_scope_id(scenario_id)
        scope = self._scope(scenario_id)
        orders = sorted(scope.orders, key=lambda order: order.created_at, reverse=True)[:order_limit]
        fills = sorted(scope.fills, key=lambda fill: fill.created_at, reverse=True)[:fill_limit]
        scenarios = [
            self.scenario_view(item.scenario_id)
            for item in self.scenarios.values()
            if item.status != PAPER_SCENARIO_ENDED
        ]
        return {
            "account": self.account_view(scenario_id=scenario_id),
            "orders": [self._order_view(order) for order in orders],
            "fills": [self._fill_view(fill) for fill in fills],
            "scenarios": scenarios,
            "active_scenario_id": scope.scenario_id,
            "account_version": int(scope.account.version),
        }

    def rule_orders(self, rule_id: str, cycle_id: str = "", *, scenario_id: str = "") -> list[PaperOrder]:
        scope = self._scope(scenario_id)
        return [
            order for order in scope.orders if order.rule_id == rule_id and (not cycle_id or order.cycle_id == cycle_id)
        ]

    def rule_fills(self, rule_id: str, cycle_id: str = "", *, scenario_id: str = "") -> list[PaperFill]:
        scope = self._scope(scenario_id)
        return [fill for fill in scope.fills if fill.rule_id == rule_id and (not cycle_id or fill.cycle_id == cycle_id)]

    def new_cycle_id(self, rule_id: str, cycle_count: int) -> str:
        return f"{rule_id}:C{int(cycle_count) + 1}"


# ------------------------------------------------------------------ 便捷状态转换
def fill_summary(fills: list[PaperFill]) -> dict[str, int]:
    return {
        "buy_quantity": sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_BUY),
        "sell_quantity": sum(fill.quantity for fill in fills if fill.side == PAPER_SIDE_SELL),
        "gross_raw": sum(fill.gross_raw for fill in fills),
        "fee_raw": sum(fill.fee_raw for fill in fills),
        "net_raw": sum(fill.net_raw for fill in fills if fill.side == PAPER_SIDE_SELL),
        "realized_pnl_raw": sum(fill.realized_pnl_raw for fill in fills if fill.side == PAPER_SIDE_SELL),
    }
