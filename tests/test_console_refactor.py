from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.account_store import AccountStore
from core.config import PluginConfig
from core.models import Subscription
from core.notifier import Notifier
from core.output_store import DEFAULT_QUOTE_PAYLOAD, DEFAULT_TRADE_PAYLOAD, OutputStore
from core.storage import PluginStorage
from core.subscriptions import SubscriptionStore
from core.trade_models import TradeEvent, TradeRule, TradeRuntime
from core.trader import AutoTrader


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def debug(self, *_args, **_kwargs) -> None:
        pass


class ConsoleRefactorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = PluginStorage(Path(self.tmp.name), _Logger())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_account_migration_is_redacted_and_password_can_be_preserved(self) -> None:
        store = AccountStore(self.storage, _Logger())
        self.assertTrue(
            store.migrate_legacy(
                {
                    "account_email": "user@example.com",
                    "account_password": "secret",
                    "owner_uids": ["owner-a", "owner-b"],
                }
            )
        )
        sanitized = store.sanitized()
        self.assertNotIn("password", sanitized)
        self.assertTrue(sanitized["password_configured"])
        self.assertEqual(sanitized["owner_uids"], ["owner-a", "owner-b"])

        self.assertTrue(store.save(email="new@example.com", password="", owner_uids=["owner-c"]))
        self.assertEqual(store.password, "secret")
        restored = AccountStore(self.storage, _Logger())
        self.assertEqual(restored.email, "new@example.com")
        self.assertEqual(restored.password, "secret")
        raw = json.loads((Path(self.tmp.name) / "account.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["password"], "secret")

    def test_quote_default_matches_legacy_schema_template(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["payload"]["default"], DEFAULT_QUOTE_PAYLOAD)
        self.assertTrue(schema["payload"]["invisible"])

    def test_account_and_subscription_input_limits(self) -> None:
        store = AccountStore(self.storage, _Logger())
        with self.assertRaises(ValueError):
            store.save(email="owner@example.com", password="x", owner_uids=["x" * 129])

        subscription_store = SubscriptionStore(lambda _subs: True)
        ok, message = subscription_store.replace_group(
            uid="user",
            target_umo="test:FriendMessage:user",
            enabled=True,
            interval=30,
            mode="always",
            items=[{"url": f"https://trade.gaijin.net/market/1067/item-{index}"} for index in range(101)],
        )
        self.assertFalse(ok)
        self.assertIn("100", message)

    def test_output_migration_defaults_and_preview(self) -> None:
        store = OutputStore(self.storage, _Logger())
        self.assertTrue(store.migrate_legacy({"payload": "old-quote-template"}))
        self.assertEqual(store.quote_payload, "old-quote-template")
        self.assertEqual(store.trade_payload, DEFAULT_TRADE_PAYLOAD)

        self.assertTrue(
            store.save(
                quote_payload=DEFAULT_QUOTE_PAYLOAD,
                trade_payload='{"title":"交易","content":["$trade_message"]}',
            )
        )
        trade_preview = store.preview(store.trade_payload, "trade")
        self.assertTrue(trade_preview["ok"])
        self.assertIn("交易", trade_preview["text"])
        self.assertEqual(trade_preview["unknown_fields"], [])
        quote_preview = store.preview(store.quote_payload, "quote")
        self.assertTrue(quote_preview["ok"])
        self.assertIn("F-16XL", quote_preview["text"])
        field_preview = store.preview('{"trade":"$trade_side $trade_quantity $trade_fee"}', "trade")
        self.assertEqual(field_preview["text"], "SELL 1 15%")
        self.assertEqual(field_preview["unknown_fields"], [])
        unknown_preview = store.preview('{"unknown":"$does_not_exist"}', "trade")
        self.assertIn("does_not_exist", unknown_preview["unknown_fields"])
        with self.assertRaises(ValueError):
            store.save(quote_payload="{broken", trade_payload=DEFAULT_TRADE_PAYLOAD)

    def test_subscription_groups_support_create_move_item_delete_and_remove_group(self) -> None:
        saved: list[Subscription] = []
        store = SubscriptionStore(lambda subs: saved.extend(subs) or True)
        link_a = "https://trade.gaijin.net/market/1067/item-a"
        link_b = "https://trade.gaijin.net/market/1067/item-b"
        ok, _ = store.replace_group(
            uid="user",
            target_umo="test:FriendMessage:user",
            enabled=True,
            interval=12,
            mode="always",
            items=[{"url": link_a}, {"url": link_b}],
        )
        self.assertTrue(ok)
        self.assertEqual(len(store.all()), 2)

        ok, _ = store.replace_group(
            original_uid="user",
            original_target="test:FriendMessage:user",
            uid="user",
            target_umo="test:GroupMessage:group",
            enabled=False,
            interval=20,
            mode="only_change",
            items=[{"url": link_a}],
        )
        self.assertTrue(ok)
        self.assertEqual(len(store.all()), 1)
        moved = store.all()[0]
        self.assertEqual(moved.target_umo, "test:GroupMessage:group")
        self.assertEqual((moved.interval, moved.mode, moved.enabled), (20, "only_change", False))

        self.assertTrue(store.delete_group("user", "test:GroupMessage:group")[0])
        self.assertEqual(store.all(), [])

    def test_notifier_owner_sessions_come_from_account_store(self) -> None:
        storage = SimpleNamespace(resolve_uid=lambda uid: f"test:FriendMessage:{uid}")
        notifier = Notifier(
            None,
            storage,
            SimpleNamespace(),
            _Logger(),
            accounts=SimpleNamespace(owner_uids=["owner-a"]),
        )
        self.assertEqual(notifier.owner_sessions(), ["test:FriendMessage:owner-a"])

    def test_group_activity_does_not_replace_known_private_session(self) -> None:
        self.storage.remember_session("owner", "test:FriendMessage:owner", "test", "Owner")
        self.storage.remember_session("owner", "test:GroupMessage:group", "test", "Owner")
        self.assertEqual(self.storage.resolve_private_uid("owner"), "test:FriendMessage:owner")
        notifier = Notifier(None, self.storage, SimpleNamespace(appid="1067"), _Logger())
        self.assertEqual(notifier.session_of("owner"), "test:FriendMessage:owner")

    def test_config_validation_uses_runtime_auth_state(self) -> None:
        hints = PluginConfig({}).validate(auth=SimpleNamespace(jwt="runtime-jwt", refresh_token=""))
        self.assertFalse(any("尚未配置登录令牌" in hint for hint in hints))

    async def test_trade_template_renders_all_event_context_and_falls_back(self) -> None:
        output = SimpleNamespace(trade_payload=DEFAULT_TRADE_PAYLOAD)
        notifier = Notifier(None, self.storage, SimpleNamespace(), _Logger(), output=output)
        event = TradeEvent(
            timestamp=0,
            rule_id="T1",
            kind="buy",
            message="已买入 1 个",
            details={"trade_message": "已买入 1 个", "trade_side": "BUY"},
        )
        self.assertEqual(notifier.render_trade_event(event), "【GJM 自动交易】已买入 1 个")

        output.trade_payload = "{broken"
        self.assertEqual(notifier.render_trade_event(event), "【GJM 自动交易】已买入 1 个")

    def test_trade_event_context_exposes_rule_position_and_execution_fields(self) -> None:
        fake = SimpleNamespace(config=SimpleNamespace(appid="1067"))
        rule = TradeRule(
            rule_id="TABC",
            market_name="item",
            buy_price_raw=200000,
            quantity=2,
            sell_net_price_raw=272000,
            mode="loop",
            live=True,
        )
        runtime = TradeRuntime(
            rule_id="TABC", held_quantity=1, listed_quantity=0, cost_basis_raw=200000, proceeds_raw=272000
        )
        context = AutoTrader._event_context(
            fake,
            rule,
            runtime,
            "sell",
            "卖出 1 个",
            "info",
            {"trade_side": "SELL", "trade_quantity": 1},
        )
        self.assertEqual(context["trade_rule_id"], "TABC")
        self.assertEqual(context["trade_held_qty"], 1)
        self.assertEqual(context["trade_side"], "SELL")
        self.assertEqual(context["trade_fee"], "15%")
        self.assertEqual(context["trade_profit"], "+7.20")

    def test_console_assets_cover_tabs_and_new_apis(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "views/trade/index.html").read_text(encoding="utf-8")
        app = (root / "views/trade/app.js").read_text(encoding="utf-8")
        for tab in ("monitor", "trade", "account", "output"):
            self.assertIn(f'data-tab="{tab}"', html)
        tab_positions = [html.index(f'data-tab="{tab}"') for tab in ("monitor", "trade", "output", "account")]
        self.assertEqual(tab_positions, sorted(tab_positions))
        for endpoint in (
            "account/overview",
            "account/save",
            "account/login/start",
            "account/login/code",
            "monitor/overview",
            "monitor/subscriptions/save",
            "monitor/subscriptions/delete",
            "output/overview",
            "output/save",
            "output/preview",
            "trade/rules/drill",
            "trade/logs",
        ):
            self.assertIn(endpoint, app)
        self.assertIn("完整模拟演练", app)
        self.assertIn("切换到模拟盘", app)
        self.assertIn("function authIsValid", app)
        self.assertNotIn('auth.has_jwt ? "有效"', app)
        self.assertIn("insertAdjacentHTML", app)
        self.assertNotIn("renderEvents(data.events", app)
        subscriptions_source = (root / "core/subscriptions.py").read_text(encoding="utf-8")
        self.assertNotIn("/gjma", subscriptions_source)
