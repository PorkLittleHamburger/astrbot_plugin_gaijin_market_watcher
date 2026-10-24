from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.api_client import GaijinMarketClient, MarketApiError
from core.auth import LoginRequiredError
from core.models import Quote
from core.storage import PluginStorage
from core.trade_models import (
    PHASE_COMPLETED,
    PHASE_LISTING,
    BookLevel,
    BookSnapshot,
    TradeAssetAllocation,
    TradeEvent,
    TradeOrder,
    TradeSettings,
    fmt_gjn,
    gross_for_net_raw,
    parse_gjn_raw,
    seller_receives_raw,
)
from core.trade_store import TradeStore
from core.trader import AutoTrader, TradeError
from core.watcher import MarketWatcher


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def debug(self, *_args, **_kwargs) -> None:
        pass


class _Auth:
    async def get_token(self, *_args, **_kwargs) -> str:
        return "token"


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    def owner_sessions(self) -> list[str]:
        return []

    async def push(self, text, targets, *_args, **_kwargs) -> None:
        self.sent.append((text, list(targets)))

    def render_trade_event(self, event) -> str:
        return event.message


class _Config:
    appid = "1067"
    owner_uids = ["owner"]


class _WatchConfig:
    enabled_subscriptions: list = []
    group_push_interval = 30

    @staticmethod
    def interval_of(_market_name: str) -> int:
        return 30


class _Client:
    def __init__(self) -> None:
        self.quote = Quote("item", sell_min=7.0, buy_max=10.0, kind="COMMODITY", display_name="Item")
        self.books: list[BookSnapshot] = []
        self.orders: list[list[dict]] = []
        self.inventories: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []
        self.balance = 10_000_000
        self.buy_calls: list[dict] = []
        self.sell_calls: list[dict] = []
        self._transaction_counter = 0

    async def fetch_quote(self, *_args, **_kwargs) -> Quote:
        return self.quote

    async def get_asset_class(self, *_args, **_kwargs):
        return [{"name": "asset_class", "value": "coupon"}]

    async def fetch_book(self, *_args, **_kwargs) -> BookSnapshot:
        return self.books.pop(0)

    async def get_open_orders(self, *_args, **_kwargs) -> list[dict]:
        return self.orders.pop(0) if self.orders else []

    async def cancel_order(self, pair_id: str, order_id: str, *_args, **_kwargs) -> dict:
        self.cancelled.append((pair_id, order_id))
        return {"success": True}

    async def get_contexts(self, *_args, **_kwargs):
        return [{"id": "ctx"}]

    async def get_context_assets(self, *_args, **_kwargs):
        if len(self.inventories) > 1:
            return self.inventories.pop(0)
        return self.inventories[0] if self.inventories else []

    async def get_balance(self, *_args, **_kwargs) -> int:
        return self.balance

    async def place_buy_order(self, market_name, amount, price_raw, token, **kwargs):
        self.buy_calls.append(
            {"market": market_name, "amount": amount, "price_raw": price_raw, "token": token, **kwargs}
        )
        return {
            "success": True,
            "id": f"buy-{len(self.buy_calls)}",
            "pairId": "pair",
            "transactid": kwargs.get("transact_id"),
        }

    async def place_sell_order(self, context_id, asset_id, amount, price_raw, wished, token, **kwargs):
        self.sell_calls.append(
            {
                "context_id": context_id,
                "asset_id": asset_id,
                "amount": amount,
                "price_raw": price_raw,
                "wished": wished,
                "token": token,
                **kwargs,
            }
        )
        return {
            "success": True,
            "id": f"sell-{len(self.sell_calls)}",
            "pairId": "pair",
            "transactid": kwargs.get("transact_id"),
        }

    def new_transaction_id(self) -> int:
        self._transaction_counter += 1
        return 1234566 + self._transaction_counter


class TradeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = PluginStorage(Path(self.tmp.name), _Logger())
        self.client = _Client()
        self.trader = AutoTrader(
            client=self.client,
            auth=_Auth(),
            storage=self.storage,
            config=_Config(),
            notifier=_Notifier(),
            logger=_Logger(),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _book(self, sells=(), buys=()) -> BookSnapshot:
        return BookSnapshot(
            market_name="item",
            sell_levels=[BookLevel(price_raw=p, amount=a) for p, a in sells],
            buy_levels=[BookLevel(price_raw=p, amount=a) for p, a in buys],
            kind="COMMODITY",
        )

    def test_fee_is_fixed_15_percent_and_rounds_to_cent(self) -> None:
        self.assertEqual(parse_gjn_raw("10.00"), 100_000)
        self.assertEqual(seller_receives_raw(100_000), 85_000)
        self.assertEqual(fmt_gjn(85_000), "8.50")
        self.assertEqual(gross_for_net_raw(85_000), 100_000)
        self.assertEqual(gross_for_net_raw(85_001), 100_200)

    async def test_create_rule_rejects_non_profitable_rule(self) -> None:
        with self.assertRaises(TradeError):
            await self.trader.create_rule(
                url="https://trade.gaijin.net/market/1067/item",
                buy_price="10.00",
                quantity=1,
                sell_net_price="10.00",
                mode="once",
            )

    async def test_paper_buy_partial_then_list_and_complete(self) -> None:
        self.client.books.extend(
            [
                self._book(sells=[(70_000, 2), (80_000, 1)], buys=[]),
                self._book(sells=[], buys=[(120_000, 2)]),
                self._book(sells=[], buys=[(120_000, 3)]),
            ]
        )
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=3,
            sell_net_price="9.00",
            mode="once",
        )
        runtime = self.trader.store.get_runtime(rule.rule_id)
        await self.trader._paper_buy(rule, self.client.quote)
        self.assertEqual(runtime.phase, PHASE_LISTING)
        self.assertEqual(runtime.held_quantity, 1)
        self.assertEqual(runtime.listed_quantity, 1)
        await self.trader._paper_reconcile_listing(rule, self.client.quote)
        self.assertEqual(runtime.phase, PHASE_COMPLETED)
        self.assertFalse(rule.enabled)
        self.assertEqual(runtime.cycle_count, 1)

    async def test_paper_drill_uses_initial_book_and_is_isolated(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=3,
            sell_net_price="9.00",
            mode="loop",
        )
        runtime = self.trader.store.get_runtime(rule.rule_id)
        runtime.phase = PHASE_LISTING
        runtime.held_quantity = 2
        runtime.listed_quantity = 2
        runtime.cost_basis_raw = 170_000
        runtime.proceeds_raw = 123_000
        before = runtime.to_dict()
        self.client.books = [
            self._book(
                sells=[(80_000, 1)],
                buys=[(120_000, 1)],
            )
        ]

        result = await self.trader.drill_paper_cycle(rule.rule_id)

        self.assertEqual(self.client.books, [])
        self.assertEqual(self.client.buy_calls, [])
        self.assertEqual(self.client.sell_calls, [])
        self.assertEqual(runtime.to_dict(), before)
        self.assertEqual(result["initial"]["best_sell"], "8.00")
        self.assertEqual(result["initial"]["best_buy"], "12.00")
        self.assertEqual(result["buy"]["live_quantity"], 1)
        self.assertEqual(result["buy"]["synthetic_quantity"], 2)
        self.assertEqual(result["buy"]["cost"], "24.00")
        self.assertEqual(result["sell"]["live_quantity"], 1)
        self.assertEqual(result["sell"]["synthetic_quantity"], 2)
        self.assertEqual(result["sell"]["proceeds"], "28.20")
        self.assertEqual(result["summary"]["profit"], "+4.20")

        log_files = list((Path(self.tmp.name) / "logs").glob("trade-*.jsonl"))
        self.assertEqual(len(log_files), 1)
        events = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
        drill_events = [event for event in events if event["kind"] == "paper_drill"]
        self.assertEqual(len(drill_events), 1)
        self.assertGreater(drill_events[0]["event_id"], 0)
        self.assertEqual(drill_events[0]["details"]["drill_profit"], "+4.20")
        self.assertEqual(drill_events[0]["details"]["trade_profit"], "+4.20")
        reloaded = TradeStore(self.storage, _Logger())
        self.assertTrue(any(event.kind == "paper_drill" for event in reloaded.events))

    async def test_paper_drill_rejects_live_rule(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        with self.assertRaises(TradeError):
            await self.trader.drill_paper_cycle(rule.rule_id)

    async def test_paper_drill_requires_market_login(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )

        class _NoAuth:
            async def get_token(self, **_kwargs):
                raise LoginRequiredError("未登录")

        self.trader.auth = _NoAuth()
        with self.assertRaises(LoginRequiredError):
            await self.trader.drill_paper_cycle(rule.rule_id)

    async def test_reconcile_buy_cancels_remainder_and_tracks_inventory_delta(self) -> None:
        self.client.orders = [[{"id": "new-buy", "pairId": "pair", "type": "BUY", "market": "item", "amount": 3}]]
        self.client.inventories = [
            [{"id": "asset-1", "class": [{"name": "asset_class", "value": "coupon"}], "amount": 2}]
        ]
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=4,
            sell_net_price="9.00",
            mode="once",
        )
        before = {("ctx", "asset-1"): {"quantity": 1, "class_id": "x"}}
        with patch("core.trader.asyncio.sleep", new=AsyncMock()):
            acquired, allocations, remaining = await self.trader._reconcile_buy(
                rule,
                "token",
                [],
                before,
                4,
                request_ok=True,
                submitted=TradeOrder(
                    order_id="new-buy",
                    pair_id="pair",
                    side="BUY",
                    amount=4,
                    price_raw=80_000,
                    market_name="item",
                    transaction_id=1,
                ),
            )
        self.assertEqual(acquired, 1)
        self.assertEqual(allocations[0].quantity, 1)
        self.assertFalse(remaining)
        self.assertEqual(self.client.cancelled, [("pair", "new-buy")])

    async def test_reconcile_buy_does_not_cancel_unknown_manual_order(self) -> None:
        self.client.orders = [
            [
                {
                    "id": "manual-buy",
                    "pairId": "manual-pair",
                    "type": "BUY",
                    "market": "item",
                    "amount": 1,
                }
            ]
        ]
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        with patch("core.trader.asyncio.sleep", new=AsyncMock()):
            acquired, allocations, remaining = await self.trader._reconcile_buy(
                rule,
                "token",
                [],
                {},
                1,
                request_ok=True,
                submitted=TradeOrder(
                    order_id="plugin-buy",
                    pair_id="plugin-pair",
                    side="BUY",
                    amount=1,
                    price_raw=80_000,
                    market_name="item",
                    transaction_id=99,
                ),
            )
        self.assertEqual(acquired, 0)
        self.assertEqual(allocations, [])
        self.assertEqual([order.order_id for order in remaining], ["manual-buy"])
        self.assertEqual(self.client.cancelled, [])

    async def test_reconcile_buy_waits_until_cancelled_order_disappears(self) -> None:
        repeat_order = {
            "id": "plugin-buy",
            "pairId": "plugin-pair",
            "type": "BUY",
            "market": "item",
            "amount": 1,
            "transactid": 99,
        }
        self.client.orders = [[repeat_order], [repeat_order], [repeat_order], [repeat_order]]
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        with patch("core.trader.asyncio.sleep", new=AsyncMock()):
            acquired, allocations, remaining = await self.trader._reconcile_buy(
                rule,
                "token",
                [],
                {},
                1,
                request_ok=True,
                submitted=TradeOrder(
                    order_id="plugin-buy",
                    pair_id="plugin-pair",
                    side="BUY",
                    amount=1,
                    price_raw=80_000,
                    market_name="item",
                    transaction_id=99,
                ),
            )
        self.assertEqual(acquired, 0)
        self.assertEqual(allocations, [])
        self.assertEqual([order.order_id for order in remaining], ["plugin-buy"])
        self.assertEqual(self.client.cancelled, [("plugin-pair", "plugin-buy")])

    async def test_live_sell_partially_fills_and_tracks_remaining_listing(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=3,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        runtime = self.trader.store.get_runtime(rule.rule_id)
        runtime.held_quantity = 3
        runtime.cost_basis_raw = 240_000
        runtime.assets = [TradeAssetAllocation(context_id="ctx", asset_id="asset", quantity=3)]
        self.client.books = [
            self._book(buys=[(120_000, 2)]),
        ]
        self.client.orders = [
            [],
            [{"id": "sell-1", "pairId": "pair", "type": "SELL", "market": "item", "amount": 1, "price": 106_000}],
        ]
        with patch("core.trader.asyncio.sleep", new=AsyncMock()):
            await self.trader._live_sell(rule, self.client.quote)
        self.assertEqual(runtime.phase, PHASE_LISTING)
        self.assertEqual(runtime.held_quantity, 1)
        self.assertEqual(runtime.listed_quantity, 1)
        self.assertEqual(len(self.client.sell_calls), 2)

    async def test_live_sell_waits_for_delayed_order_visibility_without_phantom_fill(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=3,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        runtime = self.trader.store.get_runtime(rule.rule_id)
        runtime.held_quantity = 3
        runtime.cost_basis_raw = 240_000
        runtime.assets = [TradeAssetAllocation(context_id="ctx", asset_id="asset", quantity=3)]
        self.client.books = [self._book(buys=[(120_000, 3)])]
        self.client.orders = [
            [],
            [],
            [
                {
                    "id": "sell-1",
                    "pairId": "pair",
                    "type": "SELL",
                    "market": "item",
                    "amount": 3,
                    "price": 120_000,
                }
            ],
        ]

        with patch("core.trader.asyncio.sleep", new=AsyncMock()):
            await self.trader._live_sell(rule, self.client.quote)

        self.assertEqual(runtime.held_quantity, 3)
        self.assertEqual(runtime.listed_quantity, 3)
        self.assertEqual(runtime.proceeds_raw, 0)
        self.assertEqual(runtime.phase, PHASE_LISTING)

    async def test_live_sell_detects_returned_inventory_instead_of_phantom_sale(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        runtime = self.trader.store.get_runtime(rule.rule_id)
        runtime.held_quantity = 1
        runtime.cost_basis_raw = 80_000
        runtime.assets = [
            TradeAssetAllocation(
                context_id="ctx",
                asset_id="asset",
                quantity=1,
                baseline_quantity=0,
            )
        ]
        self.client.books = [self._book(buys=[(120_000, 1)])]
        self.client.inventories = [
            [{"id": "asset", "class": [{"name": "asset_class", "value": "coupon"}], "amount": 1}]
        ]

        with patch("core.trader.asyncio.sleep", new=AsyncMock()):
            await self.trader._live_sell(rule, self.client.quote)

        self.assertEqual(runtime.held_quantity, 1)
        self.assertEqual(runtime.listed_quantity, 0)
        self.assertEqual(runtime.proceeds_raw, 0)
        self.assertEqual(runtime.phase, "holding")

    async def test_live_buy_uses_observed_fill_prices_for_cost_basis(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=3,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        self.trader.store.settings = TradeSettings(
            enabled=True,
            total_budget_raw=1_000_000,
            daily_limit_raw=1_000_000,
            max_order_raw=1_000_000,
            max_quantity=10,
        )
        self.client.books = [self._book(sells=[(70_000, 2), (80_000, 1)])]
        self.client.orders = [
            [],
            [
                {
                    "id": "buy-1",
                    "pairId": "pair",
                    "type": "BUY",
                    "market": "item",
                    "amount": 1,
                    "price": 80_000,
                }
            ],
        ]
        self.client.inventories = [
            [],
            [{"id": "asset", "class": [{"name": "asset_class", "value": "coupon"}], "amount": 2}],
        ]

        with (
            patch("core.trader.asyncio.sleep", new=AsyncMock()),
            patch.object(self.trader, "_live_sell", new=AsyncMock()),
        ):
            await self.trader._live_buy(rule, self.client.quote)

        runtime = self.trader.store.get_runtime(rule.rule_id)
        self.assertEqual(runtime.cost_basis_raw, 140_000)
        self.assertEqual(runtime.last_buy_price_raw, 70_000)
        self.assertEqual(self.trader.store.ledger.daily_spent_raw, 140_000)

    async def test_manual_listing_cancel_is_detected_by_returned_inventory(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        runtime = self.trader.store.get_runtime(rule.rule_id)
        runtime.phase = PHASE_LISTING
        runtime.held_quantity = 1
        runtime.listed_quantity = 1
        runtime.assets = [TradeAssetAllocation(context_id="ctx", asset_id="asset", quantity=1, baseline_quantity=0)]
        runtime.orders = [
            TradeOrder(
                order_id="sell-1",
                pair_id="pair",
                side="SELL",
                amount=1,
                price_raw=106_000,
                market_name="item",
            )
        ]
        self.client.orders = [[]]
        self.client.inventories = [
            [{"id": "asset", "class": [{"name": "asset_class", "value": "coupon"}], "amount": 1}]
        ]
        await self.trader._reconcile_live_listing(rule)
        self.assertEqual(runtime.held_quantity, 1)
        self.assertEqual(runtime.listed_quantity, 0)
        self.assertEqual(runtime.phase, "holding")

    def test_trade_store_round_trip(self) -> None:
        store = TradeStore(self.storage, _Logger())
        store.settings = TradeSettings(enabled=True, total_budget_raw=100_000)
        store.save_settings()
        loaded = TradeStore(self.storage, _Logger())
        self.assertTrue(loaded.settings.enabled)
        self.assertEqual(loaded.settings.total_budget_raw, 100_000)

    def test_trade_order_round_trip_preserves_transaction_id(self) -> None:
        order = TradeOrder(
            order_id="order-1",
            pair_id="pair-1",
            side="SELL",
            amount=2,
            price_raw=90_100,
            market_name="item",
            transaction_id=987654321,
        )
        self.assertEqual(TradeOrder.from_dict(order.to_dict()).transaction_id, 987654321)

    def test_trade_view_assets_and_i18n_exist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "views" / "trade" / "index.html").is_file())
        app = (root / "views" / "trade" / "app.js").read_text(encoding="utf-8")
        self.assertIn('apiGet("trade/overview")', app)
        self.assertIn('"trade/stop-all"', app)
        self.assertTrue((root / ".astrbot-plugin" / "i18n" / "zh-CN.json").is_file())

    def test_trade_view_javascript_dom_ids_exist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "trade" / "index.html").read_text(encoding="utf-8")
        app = (root / "views" / "trade" / "app.js").read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', html))
        refs = set(re.findall(r'\$\("([^"]+)"\)', app))
        self.assertEqual(refs - ids, set())

    async def test_update_settings_allows_zero_to_disable_limits(self) -> None:
        settings = await self.trader.update_settings(
            {"enabled": False, "total_budget": 0, "daily_limit": "0", "max_order": "0", "max_quantity": 0}
        )
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.total_budget_raw, 0)
        self.assertEqual(settings.daily_limit_raw, 0)
        self.assertEqual(settings.max_order_raw, 0)

    async def test_budget_rejects_daily_limit_overrun(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        self.trader.store.settings = TradeSettings(
            enabled=True,
            total_budget_raw=1_000_000,
            daily_limit_raw=50_000,
            max_order_raw=1_000_000,
            max_quantity=10,
        )
        with self.assertRaises(TradeError):
            await self.trader._check_budget(100_000, rule)

    async def test_notifications_only_go_to_owner_private_sessions(self) -> None:
        notifier = _Notifier()
        notifier.owner_sessions = lambda: ["p:GroupMessage:g", "p:FriendMessage:owner"]
        self.trader.notifier = notifier
        event = TradeEvent(timestamp=0, rule_id="", kind="test", message="test", details={"trade_message": "test"})
        await self.trader._notify_event(event)
        self.assertEqual(notifier.sent, [("test", ["p:FriendMessage:owner"])])

    async def test_api_trade_calls_use_stable_transaction_and_no_internal_retry(self) -> None:
        client = GaijinMarketClient(min_interval=0)
        with patch.object(client, "_post", new=AsyncMock(return_value={"success": True})) as post:
            await client.place_buy_order("item", 2, 100_000, "token", transact_id=123456)
            args, kwargs = post.await_args
            self.assertEqual(args[1], "cln_market_buy")
            self.assertEqual(args[2]["transactid"], 123456)
            self.assertEqual(args[2]["price"], 100_000)
            self.assertEqual(args[2]["amount"], 2)
            self.assertEqual(kwargs["retries"], 0)

        with patch.object(client, "_post", new=AsyncMock(return_value={"success": True})) as post:
            await client.place_sell_order("ctx", "asset", 1, 106_000, 90_100, "token", transact_id=654321)
            args, kwargs = post.await_args
            self.assertEqual(args[1], "cln_market_sell")
            self.assertEqual(args[2]["seller_should_get"], 90_100)
            self.assertEqual(args[2]["transactid"], 654321)
            self.assertEqual(kwargs["retries"], 0)

    async def test_fetch_book_falls_back_to_brief_for_commodity(self) -> None:
        client = GaijinMarketClient(min_interval=0)
        brief = {
            "success": True,
            "type": "COMMODITY",
            "SELL": [[451_000, 2]],
            "BUY": [[430_000, 1]],
        }
        with patch.object(
            client,
            "_post",
            new=AsyncMock(
                side_effect=[
                    MarketApiError("cln_books", "REQUIRE_NONCOMMODITY_PAIR"),
                    brief,
                ]
            ),
        ) as post:
            book = await client.fetch_book("item", "token")

        self.assertEqual(post.await_count, 2)
        self.assertEqual(post.await_args_list[1].args[1], "cln_books_brief")
        self.assertEqual(book.best_sell, 451_000)
        self.assertEqual(book.best_buy, 430_000)

    async def test_arm_requires_prepare_confirmation(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        self.trader.store.settings = TradeSettings(
            enabled=True,
            total_budget_raw=1_000_000,
            daily_limit_raw=1_000_000,
            max_order_raw=1_000_000,
            max_quantity=10,
        )
        with self.assertRaises(TradeError):
            await self.trader.confirm_arm(rule.rule_id, rule.rule_id)
        await self.trader.prepare_arm(rule.rule_id)
        armed = await self.trader.confirm_arm(rule.rule_id, rule.rule_id)
        self.assertTrue(armed.live)

    async def test_switch_to_paper_requires_clear_position(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        runtime = self.trader.store.get_runtime(rule.rule_id)
        runtime.held_quantity = 1
        runtime.phase = PHASE_LISTING
        with self.assertRaises(TradeError):
            await self.trader.action(rule.rule_id, "disarm")

    async def test_switch_to_paper_resets_and_persists_rule(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        rule.live = True
        runtime = self.trader.store.get_runtime(rule.rule_id)
        runtime.cycle_count = 2
        runtime.last_error = ""
        switched = await self.trader.action(rule.rule_id, "disarm")
        self.assertFalse(switched.live)
        self.assertFalse(switched.enabled)
        reloaded = TradeStore(self.storage, _Logger())
        persisted = reloaded.get_rule(rule.rule_id)
        self.assertIsNotNone(persisted)
        self.assertFalse(persisted.live)

    async def test_public_trade_mutations_wait_for_action_lock(self) -> None:
        rule = await self.trader.create_rule(
            url="https://trade.gaijin.net/market/1067/item",
            buy_price="8.00",
            quantity=1,
            sell_net_price="9.00",
            mode="once",
        )
        await self.trader._action_lock.acquire()
        task = asyncio.create_task(self.trader.action(rule.rule_id, "pause"))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.trader._action_lock.release()
        await task
        self.assertFalse(rule.enabled)

    def test_watcher_uses_trader_market_names_and_five_second_interval(self) -> None:
        watcher = MarketWatcher(
            client=self.client,
            auth=_Auth(),
            storage=self.storage,
            config=_WatchConfig(),
            notifier=_Notifier(),
            logger=_Logger(),
            extra_market_names=lambda: ["trade-item"],
            extra_interval=lambda: 5,
        )
        self.assertEqual(watcher._private_market_names(), ["trade-item"])
        self.assertEqual(watcher._interval_for("trade-item"), 5)
