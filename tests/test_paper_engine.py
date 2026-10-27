from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.paper_engine import PaperError, PaperStorageError, PaperTradingEngine
from core.paper_models import (
    PAPER_ORDER_CANCELLED,
    PAPER_ORDER_FILLED,
    PAPER_ORDER_PARTIALLY_FILLED,
    PAPER_SIDE_BUY,
    PAPER_SIDE_SELL,
)
from core.storage import PluginStorage
from core.trade_models import BookLevel, BookSnapshot, gross_for_net_raw


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def debug(self, *_args, **_kwargs) -> None:
        pass


def _book(buy=(), sell=(), depth_buy=0, depth_sell=0) -> BookSnapshot:
    return BookSnapshot(
        market_name="item",
        buy_levels=[BookLevel(price_raw=p, amount=a) for p, a in buy],
        sell_levels=[BookLevel(price_raw=p, amount=a) for p, a in sell],
        kind="COMMODITY",
        auctionable=False,
    )


class PaperEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = PluginStorage(Path(self.tmp.name), _Logger())
        self.engine = PaperTradingEngine(self.storage, _Logger(), starting_cash_raw=1_000_000)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_buy_fills_at_external_ask_and_adds_fifo_lot(self) -> None:
        self.engine.observe_market(_book(sell=[(80_000, 2)], depth_sell=2), observed_at=1)
        order, fills = self.engine.place_order(
            market_name="item", side=PAPER_SIDE_BUY, price_raw=81_000, quantity=2, client_order_id="c1", now=2
        )
        self.assertEqual(order.status, PAPER_ORDER_FILLED)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price_raw, 80_000)
        account = self.engine.account_view()
        self.assertEqual(account["cash_total_raw"], 1_000_000 - 160_000)
        self.assertEqual(account["inventory"][0]["quantity"], 2)
        self.assertEqual(account["inventory"][0]["cost_basis_raw"], 160_000)

    def test_same_book_version_does_not_fill_twice(self) -> None:
        book = _book(sell=[(80_000, 1)], depth_sell=1)
        self.engine.observe_market(book, observed_at=1)
        order, fills = self.engine.place_order(
            market_name="item", side=PAPER_SIDE_BUY, price_raw=81_000, quantity=1, now=2
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(self.engine.observe_market(book, observed_at=3), [])
        self.assertEqual(order.status, PAPER_ORDER_FILLED)

    def test_partial_buy_remains_open_then_fills_on_new_book(self) -> None:
        self.engine.observe_market(_book(sell=[(80_000, 1)], depth_sell=1), observed_at=1)
        order, fills = self.engine.place_order(
            market_name="item", side=PAPER_SIDE_BUY, price_raw=81_000, quantity=2, now=2
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(order.status, PAPER_ORDER_PARTIALLY_FILLED)
        self.assertEqual(order.remaining_quantity, 1)
        self.engine.observe_market(_book(sell=[(80_000, 2)], depth_sell=2), observed_at=3)
        self.assertEqual(order.status, PAPER_ORDER_FILLED)
        self.assertEqual(self.engine.account_view()["inventory"][0]["quantity"], 2)

    def test_cancel_buy_releases_reserved_cash(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        order, fills = self.engine.place_order(
            market_name="item", side=PAPER_SIDE_BUY, price_raw=80_000, quantity=2, now=2
        )
        self.assertEqual(fills, [])
        before_cancel = self.engine.account_view()
        self.assertEqual(before_cancel["cash_reserved_raw"], 160_000)
        cancelled = self.engine.cancel_order(order.order_id, now=3)
        self.assertEqual(cancelled.status, PAPER_ORDER_CANCELLED)
        after_cancel = self.engine.account_view()
        self.assertEqual(after_cancel["cash_available_raw"], before_cancel["cash_available_raw"] + 160_000)
        self.assertEqual(after_cancel["cash_reserved_raw"], 0)

    def test_sell_consumes_fifo_and_charges_fifteen_percent(self) -> None:
        self.engine.observe_market(
            _book(buy=[(92_000, 2)], sell=[(80_000, 2)], depth_buy=2, depth_sell=2), observed_at=1
        )
        self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=81_000, quantity=2, now=2)
        self.engine.observe_market(_book(buy=[(92_000, 2)], sell=[], depth_buy=2), observed_at=3)
        order, fills = self.engine.place_order(
            market_name="item", side=PAPER_SIDE_SELL, price_raw=90_000, quantity=2, now=4
        )
        self.assertEqual(order.status, PAPER_ORDER_FILLED)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].gross_raw, 184_000)
        self.assertEqual(fills[0].net_raw, 156_400)
        self.assertEqual(fills[0].cost_basis_raw, 160_000)
        self.assertEqual(fills[0].realized_pnl_raw, -3_600)
        self.assertEqual(self.engine.account_view()["inventory"], [])

    def test_scenario_is_isolated_from_base_account(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        scenario = self.engine.create_scenario(
            rule_id="T1",
            market_name="item",
            buy_price_raw=80_000,
            sell_net_price_raw=90_000,
            quantity=1,
            delay_seconds=5,
            duration_seconds=30,
            now=1,
        )
        sid = scenario["scenario_id"]
        self.engine.advance_scenarios(now=7)
        base = self.engine.account_view()
        self.assertEqual(base["cash_total_raw"], 1_000_000)
        self.engine.place_order(
            market_name="item", side=PAPER_SIDE_BUY, price_raw=80_000, quantity=1, scenario_id=sid, now=7
        )
        scenario_account = self.engine.account_view(scenario_id=sid)
        self.assertNotEqual(scenario_account["cash_total_raw"], base["cash_total_raw"])
        self.engine.close_scenario(sid, now=8)
        self.assertEqual(self.engine.account_view()["cash_total_raw"], 1_000_000)

    def test_scenario_switches_sides_without_crossing(self) -> None:
        self.engine.observe_market(
            _book(buy=[(70_000, 5)], sell=[(90_000, 5)], depth_buy=5, depth_sell=5), observed_at=1
        )
        scenario = self.engine.create_scenario(
            rule_id="T1",
            market_name="item",
            buy_price_raw=80_000,
            sell_net_price_raw=85_000,
            quantity=5,
            cycle_id="C1",
            delay_seconds=5,
            duration_seconds=30,
            now=1,
        )
        sid = scenario["scenario_id"]
        self.engine.advance_scenarios(now=7)
        buy_book = self.engine.scenarios[sid].book
        self.assertLess(buy_book.best_buy, buy_book.best_sell)
        synthetic_asks = [level for level in buy_book.sell_levels if level.price_raw <= 80_200]
        self.assertEqual(sum(level.amount for level in synthetic_asks), 5)
        _, first = self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_BUY,
            price_raw=80_200,
            quantity=5,
            source="RULE",
            rule_id="T1",
            cycle_id="C1",
            scenario_id=sid,
            now=7,
        )
        self.assertEqual(first[0].price_raw, 80_000)

        self.engine.advance_scenarios(now=8)
        active = self.engine.scenarios[sid]
        self.assertEqual(active.target_side, "SELL")
        self.assertLess(active.book.best_buy, active.book.best_sell)
        synthetic_bids = [level for level in active.book.buy_levels if level.price_raw >= 98_000]
        self.assertEqual(sum(level.amount for level in synthetic_bids), 5)
        _, second = self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_SELL,
            price_raw=gross_for_net_raw(85_000),
            quantity=5,
            source="RULE",
            rule_id="T1",
            cycle_id="C1",
            scenario_id=sid,
            now=8,
        )
        self.assertEqual(second[0].price_raw, gross_for_net_raw(85_000))

    def test_client_order_id_stays_idempotent_after_scenario_ends(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        scenario = self.engine.create_scenario(
            rule_id="T1",
            market_name="item",
            buy_price_raw=80_000,
            sell_net_price_raw=90_000,
            quantity=1,
            delay_seconds=5,
            duration_seconds=30,
            now=2,
        )
        sid = scenario["scenario_id"]
        order, _ = self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_BUY,
            price_raw=79_000,
            quantity=1,
            client_order_id="retry-key",
            scenario_id=sid,
            now=3,
        )
        self.engine.close_scenario(sid, now=4)
        retried, fills = self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_BUY,
            price_raw=79_000,
            quantity=1,
            client_order_id="retry-key",
            scenario_id=sid,
            now=5,
        )
        self.assertEqual(retried.order_id, order.order_id)
        self.assertEqual(fills, [])
        with self.assertRaisesRegex(PaperError, "参数不同"):
            self.engine.place_order(
                market_name="item",
                side=PAPER_SIDE_BUY,
                price_raw=79_000,
                quantity=2,
                client_order_id="retry-key",
                scenario_id=sid,
                now=6,
            )

    def test_rule_base_orders_are_dormant_while_scenario_is_active(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        order, _ = self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_BUY,
            price_raw=80_000,
            quantity=1,
            source="RULE",
            rule_id="T1",
            cycle_id="C1",
            client_order_id="base-order",
            now=2,
        )
        scenario = self.engine.create_scenario(
            rule_id="T1",
            market_name="item",
            buy_price_raw=80_000,
            sell_net_price_raw=90_000,
            quantity=1,
            cycle_id="C1",
            delay_seconds=5,
            duration_seconds=30,
            now=3,
        )
        self.engine.advance_scenarios(now=9)
        self.engine.observe_market(_book(sell=[(75_000, 1)], depth_sell=1), observed_at=10)
        self.assertTrue(order.is_open)
        scenario_order = self.engine.scenarios[scenario["scenario_id"]].orders[0]
        self.assertEqual(scenario_order.status, "FILLED")
        self.engine.close_scenario(scenario["scenario_id"], now=11)
        self.engine.observe_market(_book(sell=[(74_000, 1)], depth_sell=1), observed_at=12)
        self.assertEqual(order.status, "FILLED")

    def test_consumed_visible_depth_does_not_reset_after_restart(self) -> None:
        book = _book(sell=[(80_000, 1)], depth_sell=1)
        self.engine.observe_market(book, observed_at=1)
        order, _ = self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=81_000, quantity=2, now=2)
        self.assertEqual(order.remaining_quantity, 1)
        reloaded = PaperTradingEngine(self.storage, _Logger(), starting_cash_raw=1_000_000)
        self.assertEqual(reloaded.observe_market(book, observed_at=3), [])
        restored = next(item for item in reloaded.orders if item.order_id == order.order_id)
        self.assertEqual(restored.remaining_quantity, 1)
        self.assertEqual(reloaded.account_view()["inventory"][0]["quantity"], 1)

    def test_scenario_consumption_survives_restart(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        scenario = self.engine.create_scenario(
            rule_id="T1",
            market_name="item",
            buy_price_raw=80_000,
            sell_net_price_raw=90_000,
            quantity=3,
            cycle_id="C1",
            delay_seconds=5,
            duration_seconds=30,
            now=1,
        )
        sid = scenario["scenario_id"]
        self.engine.advance_scenarios(now=7)
        self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_BUY,
            price_raw=80_200,
            quantity=1,
            scenario_id=sid,
            now=7,
        )
        reloaded = PaperTradingEngine(self.storage, _Logger(), starting_cash_raw=1_000_000)
        _, fills = reloaded.place_order(
            market_name="item",
            side=PAPER_SIDE_BUY,
            price_raw=80_200,
            quantity=2,
            scenario_id=sid,
            now=8,
        )
        self.assertEqual([fill.price_raw for fill in fills], [80_100, 80_200])
        self.assertEqual([fill.quantity for fill in fills], [1, 1])

    def test_scenario_book_is_not_double_consumed_after_advance_and_restart(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        scenario = self.engine.create_scenario(
            rule_id="T1",
            market_name="item",
            buy_price_raw=80_000,
            sell_net_price_raw=90_000,
            quantity=3,
            cycle_id="C1",
            delay_seconds=5,
            duration_seconds=30,
            now=1,
        )
        sid = scenario["scenario_id"]
        self.engine.advance_scenarios(now=7)
        self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_BUY,
            price_raw=80_200,
            quantity=1,
            source="RULE",
            rule_id="T1",
            cycle_id="C1",
            scenario_id=sid,
            now=7,
        )
        self.engine.advance_scenarios(now=8)
        self.engine.place_order(
            market_name="item",
            side=PAPER_SIDE_SELL,
            price_raw=gross_for_net_raw(90_000),
            quantity=1,
            source="RULE",
            rule_id="T1",
            cycle_id="C1",
            scenario_id=sid,
            now=8,
        )
        self.engine.advance_scenarios(now=9)
        before = self.engine.scenarios[sid].book.sell_levels[0]
        self.assertEqual((before.price_raw, before.amount), (80_100, 1))
        reloaded = PaperTradingEngine(self.storage, _Logger(), starting_cash_raw=1_000_000)
        restored = reloaded.scenarios[sid].book.sell_levels[0]
        liquidity = reloaded._scenario_liquidity[sid].sell_levels[0]
        self.assertEqual((restored.price_raw, restored.amount), (80_100, 1))
        self.assertEqual((liquidity.price_raw, liquidity.amount), (80_100, 1))

    def test_fill_produced_by_book_update_is_persisted(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        order, _ = self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=80_000, quantity=1, now=2)
        self.assertEqual(order.status, "OPEN")
        fills = self.engine.observe_market(_book(sell=[(75_000, 1)], depth_sell=1), observed_at=3)
        self.assertEqual(len(fills), 1)
        self.assertFalse(order.is_open)
        reloaded = PaperTradingEngine(self.storage, _Logger(), starting_cash_raw=1_000_000)
        restored = next(item for item in reloaded.orders if item.order_id == order.order_id)
        self.assertEqual(restored.status, "FILLED")
        self.assertEqual(reloaded.account_view()["inventory"][0]["quantity"], 1)

    def test_failed_persistence_rolls_back_in_memory_state(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=1)
        before = self.engine.account_view()
        stored_orders = list(self.engine.orders)
        with (
            patch.object(self.storage, "save_aux_json", return_value=False),
            self.assertRaises(PaperStorageError),
        ):
            self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=80_000, quantity=1, now=2)
        self.assertEqual(self.engine.account_view(), before)
        self.assertEqual(self.engine.orders, stored_orders)

    def test_zero_starting_cash_survives_restart(self) -> None:
        self.engine.reset_account(0)
        reloaded = PaperTradingEngine(self.storage, _Logger(), starting_cash_raw=1_000_000)
        account = reloaded.account_view()
        self.assertEqual(account["starting_cash_raw"], 0)
        self.assertEqual(account["cash_available_raw"], 0)

    def test_restart_restores_account_and_orders(self) -> None:
        self.engine.observe_market(_book(sell=[(80_000, 1)], depth_sell=1), observed_at=1)
        order, _ = self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=81_000, quantity=1, now=2)
        reloaded = PaperTradingEngine(self.storage, _Logger(), starting_cash_raw=1_000_000)
        self.assertEqual(reloaded.account_view()["inventory"][0]["quantity"], 1)
        self.assertTrue(
            any(item.order_id == order.order_id and item.status == PAPER_ORDER_FILLED for item in reloaded.orders)
        )

    def test_older_book_snapshot_cannot_override_newer_market_state(self) -> None:
        self.engine.observe_market(_book(sell=[(90_000, 1)], depth_sell=1), observed_at=20)
        order, _ = self.engine.place_order(
            market_name="item", side=PAPER_SIDE_BUY, price_raw=85_000, quantity=1, now=21
        )
        self.engine.observe_market(_book(sell=[(80_000, 1)], depth_sell=1), observed_at=10)
        self.assertEqual(order.status, "OPEN")
        self.assertEqual(self.engine.account_view()["inventory"], [])

    def test_quantity_and_price_limits_are_rejected(self) -> None:
        self.engine.observe_market(_book(sell=[(80_000, 1)], depth_sell=1), observed_at=1)
        with self.assertRaisesRegex(PaperError, "0.01 GJN"):
            self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=80_050, quantity=1)
        with self.assertRaisesRegex(PaperError, "1 到 1000"):
            self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=80_000, quantity=1001)
        with self.assertRaisesRegex(PaperError, "必须是整数"):
            self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=80_000.9, quantity=1)
        with self.assertRaisesRegex(PaperError, "必须是整数"):
            self.engine.place_order(market_name="item", side=PAPER_SIDE_BUY, price_raw=80_000, quantity=1.9)


if __name__ == "__main__":
    unittest.main()
