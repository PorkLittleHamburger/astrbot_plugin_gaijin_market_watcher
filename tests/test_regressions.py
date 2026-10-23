from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from core.api_client import (
    AuthExpiredError,
    GaijinMarketClient,
    MarketApiError,
    RefreshTokenInvalidError,
    TransportError,
)
from core.auth import AuthManager, LoginRequiredError
from core.charts import render_price_chart
from core.config import PluginConfig
from core.models import MonitorSpec, Quote, SnapshotItem, Subscription, TriggerEvent
from core.notifier import DeliveryBlock, Notifier
from core.payload import render_message, substitute
from core.resolver import extract_link_reference
from core.storage import PluginStorage
from core.subscriptions import SubscriptionStore
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


class _Storage:
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.quotes: dict[str, Quote] = {}
        self.history: dict[str, list[dict]] = {}

    def resolve_uid(self, uid: str) -> str:
        return self.sessions.get(uid, "")

    def known_platforms(self) -> set[str]:
        return {"test"}

    def get_user_name(self, uid: str) -> str:
        return uid.title()

    def get_item_meta(self, _market_name: str) -> dict:
        return {}

    def get_item_name(self, _market_name: str) -> str:
        return ""

    def get_last_quote(self, _market_name: str):
        return self.quotes.get(_market_name)

    def set_last_quote(self, quote) -> None:
        self.quotes[quote.market_name] = quote
        self.history.setdefault(quote.market_name, []).append(
            {"t": quote.timestamp, "sell": quote.sell_min, "buy": quote.buy_max}
        )

    def get_price_history(self, market_name: str, limit: int = 50) -> list[dict]:
        return self.history.get(market_name, [])[-limit:]


class _NotifierConfig:
    push_mode = "on_change"
    payload = ""
    push_batch_seconds = 10
    mention_users = True
    appid = "1067"
    owner_uids: list[str] = []
    chart_history_points = 5


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FlakyHttpClient:
    def __init__(self) -> None:
        self.calls = 0

    async def post(self, *_args, **_kwargs) -> _Response:
        self.calls += 1
        if self.calls == 1:
            return _Response(503, {})
        return _Response(200, {"response": {"success": True, "ok": 1}})


class _RetryClient(GaijinMarketClient):
    def __init__(self) -> None:
        super().__init__(min_interval=0, retries=1)
        self.fake_http = _FlakyHttpClient()

    async def _get_client(self):
        return self.fake_http


class _SubscriptionConfig:
    def __init__(self, interval: int = 120) -> None:
        self.subscriptions = [Subscription("user", "id50381_f_16xl_usa", interval=interval)]
        self._default_interval = 30
        self.default_refresh_mode = "only_change"

    def display_name_of(self, market_name: str) -> str:
        return market_name


class _WatcherConfig:
    def __init__(self, interval: int) -> None:
        self.subscription = Subscription(
            "user",
            "item",
            interval=interval,
            target_umo="test:FriendMessage:user",
        )
        self.enabled_subscriptions = [self.subscription]
        self.watched_market_names = ["item"]
        self.item_interval = 0
        self.appid = "1067"
        self.group_push_interval = 30
        self.chart_history_points = 5

    def interval_of(self, _market_name: str) -> int:
        return self.subscription.interval

    def display_name_of(self, _market_name: str) -> str:
        return "item"


class _WatcherNotifier:
    def owner_sessions(self) -> list[str]:
        return []

    def session_of(self, uid: str) -> str:
        return f"test:FriendMessage:{uid}"


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_webui_schema_does_not_expose_token_fields(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertNotIn("jwt_token", schema)
        self.assertNotIn("refresh_token", schema)
        self.assertEqual(schema["group_push_interval"]["default"], 30)
        self.assertEqual(schema["chart_history_points"]["default"], 5)
        self.assertIn("target", schema["user_subscriptions"]["templates"]["user_entry"]["items"])

    def test_batching_is_keyed_by_umo_and_uid(self) -> None:
        notifier = Notifier(None, _Storage(), _NotifierConfig(), _Logger())
        umo = "test:GroupMessage:1"
        first = object()
        second = object()

        notifier._buffer_push("alice", umo, [first], None, "Alice", 10)
        notifier._buffer_push("bob", umo, [second], None, "Bob", 10)

        self.assertEqual(set(notifier._pending), {(umo, "alice"), (umo, "bob")})
        self.assertEqual(notifier._pending[(umo, "alice")]["events"], [first])
        self.assertEqual(notifier._pending[(umo, "bob")]["events"], [second])

    async def test_http_5xx_is_retried(self) -> None:
        client = _RetryClient()
        body = await client._post("https://example.invalid", "test", {}, with_token="token")

        self.assertEqual(body["ok"], 1)
        self.assertEqual(client.fake_http.calls, 2)

    async def test_http_401_is_auth_error_and_400_is_not_success(self) -> None:
        class _StatusHttp:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

            async def post(self, *_args, **_kwargs) -> _Response:
                return _Response(self.status_code, {"message": "error"})

        class _StatusClient(GaijinMarketClient):
            def __init__(self, status_code: int) -> None:
                super().__init__(min_interval=0, retries=0)
                self.status_code = status_code

            async def _get_client(self):
                return _StatusHttp(self.status_code)

        with self.assertRaises(AuthExpiredError):
            await _StatusClient(401)._post("https://example.invalid", "test", {}, with_token="bad")
        with self.assertRaises(MarketApiError):
            await _StatusClient(400)._post("https://example.invalid", "test", {}, with_token="bad")

    def test_command_link_parser_rejects_bare_market_name(self) -> None:
        self.assertEqual(extract_link_reference("id50381_f_16xl_usa"), (None, None))
        self.assertEqual(
            extract_link_reference("https://trade.gaijin.net/market/1067/id50381_f_16xl_usa"),
            ("1067", "id50381_f_16xl_usa"),
        )

    def test_subscription_update_preserves_interval(self) -> None:
        saved: list[Subscription] = []
        store = SubscriptionStore(_SubscriptionConfig(interval=120), saved.extend)

        ok, _ = store.update("user", "id50381_f_16xl_usa", enabled=False)

        self.assertTrue(ok)
        self.assertFalse(store.all()[0].enabled)
        self.assertEqual(store.all()[0].interval, 120)

    def test_resubscribe_preserves_per_item_refresh_settings(self) -> None:
        config = _SubscriptionConfig()
        config.subscriptions = [
            Subscription("user", "first", display_name="First", interval=30, mode="only_change"),
            Subscription("user", "second", display_name="Second", interval=5, mode="always"),
        ]
        store = SubscriptionStore(config, lambda _subs: None)

        ok, _ = store.subscribe("user", "https://trade.gaijin.net/market/1067/second")

        self.assertTrue(ok)
        updated = store.find("user", "second")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.interval, 5)
        self.assertEqual(updated.mode, "always")
        self.assertEqual(updated.display_name, "Second")

    def test_subscriptions_are_scoped_to_origin_session(self) -> None:
        config = _SubscriptionConfig()
        config.subscriptions = []
        saved: list[Subscription] = []
        store = SubscriptionStore(config, saved.extend)
        link = "https://trade.gaijin.net/market/1067/id50381_f_16xl_usa"
        group_a = "test:GroupMessage:a"
        group_b = "test:GroupMessage:b"

        self.assertTrue(store.subscribe("user", link, group_a)[0])
        self.assertTrue(store.subscribe("user", link, group_b)[0])

        self.assertEqual(len(store.subscriptions_of("user", group_a)), 1)
        self.assertEqual(len(store.subscriptions_of("user", group_b)), 1)
        self.assertTrue(store.remove("user", "id50381_f_16xl_usa", group_a)[0])
        self.assertEqual(store.subscriptions_of("user", group_a), [])
        self.assertEqual(len(store.subscriptions_of("user", group_b)), 1)

    def test_remove_by_index_only_counts_current_users_subscriptions(self) -> None:
        config = _SubscriptionConfig()
        config.subscriptions = [
            Subscription("user", "first", display_name="First"),
            Subscription("other", "other-item", display_name="Other"),
            Subscription("user", "second", display_name="Second"),
        ]
        saved: list[Subscription] = []
        store = SubscriptionStore(config, saved.extend)

        ok, message = store.remove_by_index("user", 2)

        self.assertTrue(ok)
        self.assertIn("Second", message)
        self.assertEqual([sub.market_name for sub in store.all()], ["first", "other-item"])
        self.assertFalse(store.remove_by_index("user", 9)[0])

    def test_failed_subscription_save_rolls_back_memory(self) -> None:
        config = _SubscriptionConfig()

        def fail_persist(_subs) -> None:
            raise OSError("read-only")

        store = SubscriptionStore(config, fail_persist)

        ok, message = store.remove_by_index("user", 1)

        self.assertFalse(ok)
        self.assertIn("失败", message)
        self.assertEqual([sub.market_name for sub in store.all()], ["id50381_f_16xl_usa"])

    def test_storage_keeps_bounded_embedded_price_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = PluginStorage(Path(temp_dir), _Logger())
            for index in range(7):
                storage.set_last_quote(
                    Quote(
                        "item",
                        sell_min=10.0 + index,
                        buy_max=9.0 + index,
                        timestamp=1000.0 + index,
                    )
                )

            points = storage.get_price_history("item", limit=5)
            raw = json.loads((Path(temp_dir) / "quotes.json").read_text(encoding="utf-8"))

        self.assertEqual(len(points), 5)
        self.assertEqual(points[-1]["sell"], 16.0)
        self.assertEqual(len(raw["item"]["history"]), 7)

    def test_storage_write_failure_is_returned_to_callers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = PluginStorage(Path(temp_dir), _Logger())
            with patch("core.storage.os.replace", side_effect=OSError("disk full")):
                quote_saved = storage.set_last_quote(Quote("item", sell_min=10.0, buy_max=9.0, timestamp=1000.0))
                auth_saved = storage.set_auth_state("jwt", "refresh")

        self.assertFalse(quote_saved)
        self.assertFalse(auth_saved)

    def test_price_chart_is_valid_png(self) -> None:
        points = [{"t": 1000 + index, "sell": 10 + index, "buy": 8 + index} for index in range(5)]

        image_bytes = render_price_chart("测试物品", points)

        self.assertTrue(image_bytes.startswith(b"\x89PNG"))
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as image:
            self.assertEqual(image.size, (1200, 680))

    def test_refresh_commands_support_global_and_per_item_targets(self) -> None:
        config = _SubscriptionConfig()
        config.subscriptions = [
            Subscription("user", "first", interval=30),
            Subscription("other", "other-item", interval=40),
            Subscription("user", "second", interval=30),
        ]
        saved: list[Subscription] = []
        store = SubscriptionStore(config, saved.extend)

        ok, _ = store.set_interval("user", 12, "first")
        self.assertTrue(ok)
        self.assertEqual(store.subscriptions_of("user")[0].interval, 12)
        self.assertEqual(store.subscriptions_of("user")[1].interval, 30)
        self.assertEqual(store.subscriptions_of("other")[0].interval, 40)

        ok, _ = store.set_interval_by_index("user", 18, 2)
        self.assertTrue(ok)
        self.assertEqual(store.subscriptions_of("user")[1].interval, 18)

        ok, _ = store.set_refresh_mode("user", "always")
        self.assertTrue(ok)
        self.assertTrue(all(sub.mode == "always" for sub in store.subscriptions_of("user")))
        self.assertEqual(store.subscriptions_of("other")[0].mode, "only_change")

    def test_config_parses_per_item_interval_and_refresh_mode(self) -> None:
        link = "https://trade.gaijin.net/market/1067/id50381_f_16xl_usa"
        config = PluginConfig(
            {
                "user_subscriptions": [
                    {
                        "uid": "fast",
                        "interval": 30,
                        "mode": "always",
                        "items": [{"url": link, "interval": 9}],
                    },
                    {
                        "uid": "slow",
                        "interval": 30,
                        "mode": "only_change",
                        "items": [link],
                    },
                ]
            }
        )

        fast = config.subscriptions_of("fast")[0]
        slow = config.subscriptions_of("slow")[0]
        self.assertEqual((fast.interval, fast.mode), (9, "always"))
        self.assertEqual((slow.interval, slow.mode), (30, "only_change"))
        self.assertEqual(config.interval_of("id50381_f_16xl_usa"), 5)

    def test_config_parses_subscription_target_and_hydrates_legacy(self) -> None:
        link = "https://trade.gaijin.net/market/1067/id50381_f_16xl_usa"
        config = PluginConfig(
            {
                "user_subscriptions": [
                    {"uid": "known", "target": "test:GroupMessage:known", "items": [link]},
                    {"uid": "legacy", "items": [link]},
                ]
            }
        )

        self.assertEqual(
            config.subscriptions_of("known", target_umo="test:GroupMessage:known")[0].target_umo,
            "test:GroupMessage:known",
        )
        changed = config.hydrate_subscription_targets(lambda uid: "test:GroupMessage:legacy" if uid == "legacy" else "")

        self.assertEqual(changed, 1)
        self.assertEqual(config.subscriptions_of("legacy")[0].target_umo, "test:GroupMessage:legacy")

    def test_refresh_modes_differ_on_unchanged_quote(self) -> None:
        notifier = Notifier(None, _Storage(), _NotifierConfig(), _Logger())
        spec = MonitorSpec("item")
        quote = Quote("item", sell_min=10.0, buy_max=9.0)

        only_change = notifier.evaluate(spec, quote, quote, mode="only_change")
        always = notifier.evaluate(spec, quote, quote, mode="always")

        self.assertIsNone(only_change)
        self.assertIsNotNone(always)
        self.assertEqual(always.mode, "always")

    def test_reconfigure_shortens_existing_windows(self) -> None:
        config = _WatcherConfig(interval=10)
        watcher = MarketWatcher(
            client=object(),
            auth=object(),
            storage=object(),
            config=config,
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )
        now = time.time()
        watcher._next_due["item"] = now + 3600
        watcher._last_pushed[config.subscription.key] = object()
        watcher._next_notify[config.subscription.key] = now + 3600

        watcher.reconfigure()

        self.assertLessEqual(watcher._next_due["item"], now + 11)
        self.assertLessEqual(watcher._next_notify[config.subscription.key], now + 11)

    def test_fetch_failure_keeps_user_throttle_state(self) -> None:
        config = _WatcherConfig(interval=60)
        watcher = MarketWatcher(
            client=object(),
            auth=object(),
            storage=object(),
            config=config,
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )
        key = config.subscription.key
        watcher._last_pushed[key] = object()
        watcher._next_notify[key] = time.time() + 60

        watcher._evaluate_subscribers({}, {})

        self.assertIn(key, watcher._last_pushed)
        self.assertIn(key, watcher._next_notify)

    def test_unconfirmed_delivery_does_not_advance_push_state(self) -> None:
        config = _WatcherConfig(interval=5)
        config.subscription.mode = "only_change"
        notifier = Notifier(None, _Storage(), _NotifierConfig(), _Logger())
        watcher = MarketWatcher(
            client=object(),
            auth=object(),
            storage=_Storage(),
            config=config,
            notifier=notifier,
            logger=_Logger(),
        )
        key = config.subscription.key
        previous = Quote("item", sell_min=11.0, buy_max=8.0)
        quote = Quote("item", sell_min=10.0, buy_max=9.0)

        per_user, _ = watcher._evaluate_subscribers({"item": quote}, {"item": previous})

        delivery_key = ("test:FriendMessage:user", "user")
        self.assertIn(delivery_key, per_user)
        self.assertNotIn(key, watcher._last_pushed)
        self.assertNotIn(key, watcher._next_notify)

        # 第一次发送未送达后，即使最新缓存已经等于当前行情，也必须继续重试。
        retry, _ = watcher._evaluate_subscribers({"item": quote}, {"item": quote})
        self.assertIn(delivery_key, retry)

        watcher._mark_delivered("test:FriendMessage:user", "user", per_user[delivery_key])

        target_key = ("test:FriendMessage:user", "user", "item")
        self.assertIs(watcher._last_pushed[target_key], quote)
        self.assertGreater(watcher._next_notify[target_key], time.time())

    async def test_buffered_delivery_calls_ack_after_success(self) -> None:
        class _DeliveringNotifier(Notifier):
            async def _deliver(self, *_args, **_kwargs) -> bool:
                return True

        notifier = _DeliveringNotifier(None, _Storage(), _NotifierConfig(), _Logger())
        event = TriggerEvent(
            spec=MonitorSpec("item"),
            quote=Quote("item", sell_min=10.0, buy_max=9.0),
        )
        delivered: list[tuple[str, str, list[TriggerEvent]]] = []
        umo = "test:GroupMessage:1"

        notifier._buffer_push(
            "alice",
            umo,
            [event],
            None,
            "Alice",
            10,
            lambda target_umo, uid, events: delivered.append((target_umo, uid, events)),
        )
        notifier._pending[(umo, "alice")]["deadline"] = time.time() - 1
        await notifier._flush_loop()

        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], umo)
        self.assertEqual(delivered[0][1], "alice")
        self.assertEqual(delivered[0][2][0].quote.market_name, "item")

    async def test_auth_backoff_blocks_polling_for_full_duration(self) -> None:
        class _UnexpectedAuth:
            async def get_token(self, **_kwargs):
                raise AssertionError("退避期内不应请求鉴权")

        config = _WatcherConfig(interval=5)
        watcher = MarketWatcher(
            client=object(),
            auth=_UnexpectedAuth(),
            storage=_Storage(),
            config=config,
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )
        watcher._next_due["item"] = time.time()
        watcher._auth_backoff_until = time.time() + 600

        self.assertEqual(watcher._next_wait_seconds(), 60.0)
        self.assertGreater(watcher._auth_backoff_remaining(), 500)
        self.assertEqual(await watcher.poll_items(["item"]), [])
        self.assertEqual(watcher.poll_count, 0)

    async def test_auth_backoff_skips_group_snapshots(self) -> None:
        group_umo = "test:GroupMessage:group"

        class _Config:
            enabled_subscriptions = [Subscription("alice", "item", target_umo=group_umo, interval=5)]
            watched_market_names = ["item"]
            item_interval = 0
            appid = "1067"
            group_push_interval = 5
            chart_history_points = 5

            @staticmethod
            def interval_of(_market_name: str) -> int:
                return 5

            @staticmethod
            def display_name_of(_market_name: str) -> str:
                return "Item"

        class _Notifier(_WatcherNotifier):
            def __init__(self) -> None:
                self.calls = 0

            def session_of(self, _uid: str) -> str:
                return group_umo

            async def push_group_snapshot(self, _umo: str, _items: list[SnapshotItem]) -> bool:
                self.calls += 1
                return True

        notifier = _Notifier()
        watcher = MarketWatcher(
            client=object(),
            auth=object(),
            storage=_Storage(),
            config=_Config(),
            notifier=notifier,
            logger=_Logger(),
        )
        watcher._auth_backoff_until = time.time() + 600
        watcher._next_group_push = {group_umo: time.time() - 1}

        result = await watcher.poll_due_groups()

        self.assertEqual(result, {})
        self.assertEqual(notifier.calls, 0)
        self.assertEqual(watcher._next_wait_seconds(), 60.0)
        self.assertGreater(watcher._auth_backoff_remaining(), 500)

    async def test_transport_error_during_token_refresh_does_not_return_expired_token(self) -> None:
        class _Config:
            jwt_token = "expired.invalid.jwt"
            refresh_token = "refresh-token"
            account_email = ""
            account_password = ""

        class _Storage:
            def get_auth_state(self) -> dict:
                return {}

            def get_pending_login(self) -> dict:
                return {}

            def set_auth_state(self, *_args, **_kwargs) -> bool:
                return True

        class _Client:
            async def authorize_by_refresh_token(self, _refresh_token: str) -> str:
                raise TransportError("refresh", "HTTP 503")

        auth = AuthManager(_Client(), _Storage(), _Config(), _Logger())
        auth.bootstrap()

        with self.assertRaises(TransportError):
            await auth.get_token(force_refresh=True)
        self.assertIn("503", auth.status()["last_error"])

    async def test_invalid_refresh_token_requires_login(self) -> None:
        class _Config:
            jwt_token = ""
            refresh_token = "bad-refresh-token"
            account_email = ""
            account_password = ""

        class _Storage:
            def get_auth_state(self) -> dict:
                return {}

            def get_pending_login(self) -> dict:
                return {}

            def set_auth_state(self, *_args, **_kwargs) -> bool:
                return True

        class _Client:
            async def authorize_by_refresh_token(self, _refresh_token: str) -> str:
                raise RefreshTokenInvalidError("refresh", "BAD_REFRESH_TOKEN")

        auth = AuthManager(_Client(), _Storage(), _Config(), _Logger())
        auth.bootstrap()

        with self.assertRaises(LoginRequiredError):
            await auth.get_token(force_refresh=True)

    async def test_force_refresh_does_not_reuse_rejected_unexpired_token(self) -> None:
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode()

        class _Config:
            jwt_token = f"e30.{payload}.signature"
            refresh_token = "refresh-token"
            account_email = ""
            account_password = ""

        class _Storage:
            def get_auth_state(self) -> dict:
                return {}

            def get_pending_login(self) -> dict:
                return {}

            def set_auth_state(self, *_args, **_kwargs) -> bool:
                return True

        class _Client:
            async def authorize_by_refresh_token(self, _refresh_token: str) -> str:
                raise TransportError("refresh", "HTTP 503")

        auth = AuthManager(_Client(), _Storage(), _Config(), _Logger())
        auth.bootstrap()

        with self.assertRaises(TransportError):
            await auth.get_token(force_refresh=True)

    async def test_non_auth_error_after_token_refresh_does_not_trigger_backoff(self) -> None:
        class _Auth:
            async def get_token(self, **_kwargs) -> str:
                return "token"

        class _Client:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch_quote(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise AuthExpiredError("fetch", "INVALID_TOKEN")
                raise TransportError("fetch", "HTTP 503")

        watcher = MarketWatcher(
            client=_Client(),
            auth=_Auth(),
            storage=_Storage(),
            config=_WatcherConfig(interval=5),
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )

        await watcher.poll_items(["item"])

        self.assertEqual(watcher._auth_backoff_remaining(), 0)
        self.assertIn("503", watcher.last_poll_error)

    async def test_transient_token_error_retries_on_normal_interval(self) -> None:
        class _Auth:
            async def get_token(self, **_kwargs) -> str:
                raise TransportError("refresh", "HTTP 503")

        class _Client:
            async def fetch_quote(self, *_args, **_kwargs):
                raise AssertionError("不应在无令牌时抓取行情")

        watcher = MarketWatcher(
            client=_Client(),
            auth=_Auth(),
            storage=_Storage(),
            config=_WatcherConfig(interval=5),
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )
        before = time.time()

        await watcher.poll_items(["item"])

        self.assertEqual(watcher._auth_backoff_remaining(), 0)
        self.assertIn("503", watcher.last_poll_error)
        self.assertGreaterEqual(watcher._next_due["item"], before + 4.9)

    async def test_transient_error_during_forced_refresh_does_not_trigger_backoff(self) -> None:
        class _Auth:
            def __init__(self) -> None:
                self.calls = 0

            async def get_token(self, *, force_refresh: bool = False) -> str:
                self.calls += 1
                if force_refresh:
                    raise TransportError("refresh", "HTTP 503")
                return "expired-token"

        class _Client:
            async def fetch_quote(self, *_args, **_kwargs):
                raise AuthExpiredError("fetch", "INVALID_TOKEN")

        auth = _Auth()
        watcher = MarketWatcher(
            client=_Client(),
            auth=auth,
            storage=_Storage(),
            config=_WatcherConfig(interval=5),
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )

        await watcher.poll_items(["item"])

        self.assertEqual(auth.calls, 2)
        self.assertEqual(watcher._auth_backoff_remaining(), 0)
        self.assertIn("503", watcher.last_poll_error)

    async def test_group_tick_fetches_unique_items_and_builds_full_snapshots(self) -> None:
        group_a = "test:GroupMessage:a"
        group_b = "test:GroupMessage:b"

        class _Config:
            enabled_subscriptions = [
                Subscription("alice", "item-a", target_umo=group_a, interval=60),
                Subscription("bob", "item-a", target_umo=group_a, interval=60),
                Subscription("carol", "item-b", target_umo=group_b, interval=60),
            ]
            watched_market_names = ["item-a", "item-b"]
            item_interval = 0
            appid = "1067"
            group_push_interval = 30
            chart_history_points = 5

            @staticmethod
            def interval_of(_market_name: str) -> int:
                return 60

            @staticmethod
            def display_name_of(market_name: str) -> str:
                return market_name

        class _Auth:
            async def get_token(self, **_kwargs) -> str:
                return "token"

        class _Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def fetch_quote(self, market_name: str, *_args, **_kwargs) -> Quote:
                self.calls.append(market_name)
                return Quote(market_name, sell_min=10.0, buy_max=9.0, display_name=market_name)

        class _Notifier(_WatcherNotifier):
            def __init__(self) -> None:
                self.snapshots: dict[str, list[SnapshotItem]] = {}

            def session_of(self, uid: str) -> str:
                return group_a if uid in {"alice", "bob"} else group_b

            async def push_group_snapshot(self, umo: str, items: list[SnapshotItem]) -> bool:
                self.snapshots[umo] = items
                return True

        class _GroupStorage(_Storage):
            def get_item_meta(self, _market_name: str) -> dict:
                return {"cached": True}

        client = _Client()
        notifier = _Notifier()
        watcher = MarketWatcher(
            client=client,
            auth=_Auth(),
            storage=_GroupStorage(),
            config=_Config(),
            notifier=notifier,
            logger=_Logger(),
        )
        watcher._sync_group_schedule(1000.0)
        self.assertEqual(watcher._next_group_push, {group_a: 1030.0, group_b: 1030.0})
        self.assertEqual(await watcher.poll_due(), [])
        watcher._next_group_push = {group_a: time.time() - 1, group_b: time.time() - 1}

        results = await watcher.poll_due_groups()

        self.assertEqual(client.calls, ["item-a", "item-b"])
        self.assertEqual(results, {group_a: True, group_b: True})
        self.assertEqual([item.market_name for item in notifier.snapshots[group_a]], ["item-a"])
        self.assertEqual(
            [uid for uid, _name in notifier.snapshots[group_a][0].mentions],
            ["alice", "bob"],
        )
        self.assertEqual([item.market_name for item in notifier.snapshots[group_b]], ["item-b"])

    async def test_concurrent_poll_calls_are_serialized(self) -> None:
        class _Config:
            enabled_subscriptions: list[Subscription] = []
            watched_market_names = ["item"]
            item_interval = 0
            appid = "1067"
            group_push_interval = 30
            chart_history_points = 5

            @staticmethod
            def interval_of(_market_name: str) -> int:
                return 5

            @staticmethod
            def display_name_of(_market_name: str) -> str:
                return "Item"

        class _Auth:
            async def get_token(self, **_kwargs) -> str:
                return "token"

        class _SlowClient:
            def __init__(self) -> None:
                self.calls = 0
                self.active = 0
                self.max_active = 0
                self.first_started = asyncio.Event()
                self.release = asyncio.Event()

            async def fetch_quote(self, market_name: str, *_args, **_kwargs) -> Quote:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.first_started.set()
                await self.release.wait()
                self.active -= 1
                return Quote(market_name, sell_min=10.0, buy_max=9.0, display_name="Item")

        class _CachedStorage(_Storage):
            def get_item_meta(self, _market_name: str) -> dict:
                return {"cached": True}

        client = _SlowClient()
        watcher = MarketWatcher(
            client=client,
            auth=_Auth(),
            storage=_CachedStorage(),
            config=_Config(),
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )

        first = asyncio.create_task(watcher.poll_items(["item"]))
        await asyncio.wait_for(client.first_started.wait(), timeout=1)
        second = asyncio.create_task(watcher.poll_items(["item"]))
        await asyncio.sleep(0.02)

        self.assertEqual(client.max_active, 1)
        client.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.max_active, 1)

    def test_idle_watcher_uses_long_sleep(self) -> None:
        class _Config:
            enabled_subscriptions: list[Subscription] = []
            watched_market_names: list[str] = []
            item_interval = 0
            appid = "1067"
            group_push_interval = 30
            chart_history_points = 5

            @staticmethod
            def interval_of(_market_name: str) -> int:
                return 30

            @staticmethod
            def display_name_of(_market_name: str) -> str:
                return ""

        watcher = MarketWatcher(
            client=object(),
            auth=object(),
            storage=object(),
            config=_Config(),
            notifier=_WatcherNotifier(),
            logger=_Logger(),
        )

        self.assertEqual(watcher._next_wait_seconds(), 60.0)

    def test_empty_mention_is_not_treated_as_rendered(self) -> None:
        text, used_mention, _unknown = substitute("$mention", {"mention": ""})

        self.assertEqual(text, "")
        self.assertFalse(used_mention)

    def test_payload_image_alias_uses_icon_without_unknown_field(self) -> None:
        text, _used_mention, unknown = substitute("$image", {"icon": "https://example.invalid/item.png"})
        rendered = render_message('{"image":"$image"}', {"icon": "https://example.invalid/item.png"})

        self.assertEqual(text, "https://example.invalid/item.png")
        self.assertEqual(unknown, [])
        self.assertEqual(rendered.images, ["https://example.invalid/item.png"])
        self.assertEqual(rendered.unknown_fields, [])

    async def test_component_platform_renders_real_at_from_template_mention(self) -> None:
        class _MessageChain:
            def __init__(self) -> None:
                self.chain: list[object] = []

        class _At:
            def __init__(self, *, qq: str, name: str | None = None) -> None:
                self.qq = qq
                self.name = name

        class _Plain:
            def __init__(self, text: str) -> None:
                self.text = text

        class _Image:
            pass

        class _Context:
            def __init__(self) -> None:
                self.sent_messages: list[object] = []

            def get_config(self) -> dict:
                return {"platform": [{"id": "test", "type": "telegram"}]}

            async def send_message(self, _umo: str, chain) -> bool:
                self.sent_messages.append(chain)
                return True

        class _Config(_NotifierConfig):
            payload = '{"mention":"$mention","title":"$name"}'

        event_module = ModuleType("astrbot.api.event")
        event_module.MessageChain = _MessageChain
        components_module = ModuleType("astrbot.api.message_components")
        components_module.At = _At
        components_module.Image = _Image
        components_module.Plain = _Plain
        context = _Context()
        notifier = Notifier(context, _Storage(), _Config(), _Logger())
        event = TriggerEvent(
            spec=MonitorSpec("item"),
            quote=Quote("item", sell_min=1.0, buy_max=0.5, display_name="Item"),
        )

        with patch.dict(
            "sys.modules",
            {
                "astrbot.api.event": event_module,
                "astrbot.api.message_components": components_module,
            },
        ):
            sent = await notifier._send(
                "test:FriendMessage:user",
                "fallback",
                "alice",
                "Alice",
                events=[event],
                uid="alice",
            )

        self.assertTrue(sent)
        primary = context.sent_messages[0]
        self.assertIsInstance(primary.chain[0], _At)
        self.assertEqual(primary.chain[0].qq, "alice")
        self.assertTrue(any(isinstance(item, _Plain) and item.text == "Item" for item in primary.chain))

    async def test_group_snapshot_orders_mentions_blocks_and_charts(self) -> None:
        class _MessageChain:
            def __init__(self) -> None:
                self.chain: list[object] = []
                self.markdown = None

            def use_markdown(self, value: bool) -> None:
                self.markdown = value

        class _Plain:
            def __init__(self, text: str) -> None:
                self.text = text

        class _At:
            pass

        class _Image:
            def __init__(self, data: bytes) -> None:
                self.data = data

            @classmethod
            def fromBytes(cls, data: bytes):
                return cls(data)

        class _Context:
            def __init__(self) -> None:
                self.sent_messages: list[object] = []

            def get_config(self) -> dict:
                return {"platform": [{"id": "test", "type": "qq_official"}]}

            async def send_message(self, _umo: str, chain) -> bool:
                self.sent_messages.append(chain)
                return True

        event_module = ModuleType("astrbot.api.event")
        event_module.MessageChain = _MessageChain
        components_module = ModuleType("astrbot.api.message_components")
        components_module.At = _At
        components_module.Image = _Image
        components_module.Plain = _Plain
        storage = _Storage()
        for index in range(5):
            timestamp = 1000 + index
            storage.history.setdefault("item-a", []).append({"t": timestamp, "sell": 10 + index, "buy": 8 + index})
            storage.history.setdefault("item-b", []).append({"t": timestamp, "sell": 20 + index, "buy": 18 + index})
        context = _Context()
        notifier = Notifier(context, storage, _NotifierConfig(), _Logger())
        items = [
            SnapshotItem(
                market_name="item-a",
                display_name="Item A",
                quote=Quote("item-a", sell_min=14.0, buy_max=12.0, display_name="Item A"),
                previous=None,
                mentions=[("alice", "Alice"), ("bob", "Bob")],
            ),
            SnapshotItem(
                market_name="item-b",
                display_name="Item B",
                quote=Quote("item-b", sell_min=24.0, buy_max=22.0, display_name="Item B"),
                previous=None,
                mentions=[("carol", "Carol")],
            ),
            SnapshotItem(
                market_name="item-c",
                display_name="Item C",
                quote=None,
                previous=None,
                mentions=[("dave", "Dave")],
                error="HTTP 503",
            ),
        ]

        with patch.dict(
            "sys.modules",
            {
                "astrbot.api.event": event_module,
                "astrbot.api.message_components": components_module,
            },
        ):
            sent = await notifier.push_group_snapshot("test:GroupMessage:group", items)

        self.assertTrue(sent)
        primary = context.sent_messages[0]
        texts = [part.text for part in primary.chain if isinstance(part, _Plain)]
        images = [part for message in context.sent_messages for part in message.chain if isinstance(part, _Image)]
        joined = "\n".join(texts)
        self.assertLess(joined.index('id="alice"'), joined.index('id="bob"'))
        self.assertLess(joined.index('id="bob"'), joined.index("Item A"))
        self.assertLess(joined.index("Item A"), joined.index('id="carol"'))
        self.assertLess(joined.index('id="carol"'), joined.index("Item B"))
        self.assertLess(joined.index("Item B"), joined.index('id="dave"'))
        self.assertIn("暂无行情", joined)
        self.assertIn("HTTP 503", joined)
        self.assertEqual(len(images), 2)
        self.assertTrue(all(image.data.startswith(b"\x89PNG") for image in images))
        self.assertEqual(len(context.sent_messages), 3)
        self.assertTrue(primary.markdown)

    async def test_qq_group_single_chart_defers_image_to_preserve_native_mention(self) -> None:
        class _MessageChain:
            def __init__(self) -> None:
                self.chain: list[object] = []
                self.markdown = None

            def use_markdown(self, value: bool) -> None:
                self.markdown = value

        class _Plain:
            def __init__(self, text: str) -> None:
                self.text = text

        class _At:
            pass

        class _Image:
            def __init__(self, data: bytes) -> None:
                self.data = data

            @classmethod
            def fromBytes(cls, data: bytes):
                return cls(data)

        class _Context:
            def __init__(self) -> None:
                self.sent_messages: list[object] = []

            def get_config(self) -> dict:
                return {"platform": [{"id": "test", "type": "qq_official"}]}

            async def send_message(self, _umo: str, chain) -> bool:
                self.sent_messages.append(chain)
                return True

        event_module = ModuleType("astrbot.api.event")
        event_module.MessageChain = _MessageChain
        components_module = ModuleType("astrbot.api.message_components")
        components_module.At = _At
        components_module.Image = _Image
        components_module.Plain = _Plain
        context = _Context()
        notifier = Notifier(context, _Storage(), _NotifierConfig(), _Logger())
        block = DeliveryBlock(
            text="Item A",
            title="Item A",
            mentions=[("alice", "Alice")],
            images=[b"\x89PNG\r\n\x1a\n"],
        )

        with patch.dict(
            "sys.modules",
            {
                "astrbot.api.event": event_module,
                "astrbot.api.message_components": components_module,
            },
        ):
            sent = await notifier._send_blocks("test:GroupMessage:group", [block])

        self.assertTrue(sent)
        self.assertEqual(len(context.sent_messages), 2)
        primary_text = "".join(part.text for part in context.sent_messages[0].chain if isinstance(part, _Plain))
        self.assertIn('<qqbot-at-user id="alice" />', primary_text)
        self.assertFalse(any(isinstance(part, _Image) for part in context.sent_messages[0].chain))
        self.assertTrue(any(isinstance(part, _Image) for part in context.sent_messages[1].chain))

        class _FailingContext(_Context):
            def __init__(self) -> None:
                super().__init__()
                self.outcomes = [True, False]

            async def send_message(self, _umo: str, chain) -> bool:
                self.sent_messages.append(chain)
                return self.outcomes.pop(0)

        failed_context = _FailingContext()
        failed_notifier = Notifier(failed_context, _Storage(), _NotifierConfig(), _Logger())
        with patch.dict(
            "sys.modules",
            {
                "astrbot.api.event": event_module,
                "astrbot.api.message_components": components_module,
            },
        ):
            failed_sent = await failed_notifier._send_blocks("test:GroupMessage:group", [block])

        self.assertFalse(failed_sent)
        self.assertEqual(len(failed_context.sent_messages), 2)

    async def test_qq_single_image_stays_inline_without_native_group_mention(self) -> None:
        class _MessageChain:
            def __init__(self) -> None:
                self.chain: list[object] = []
                self.markdown = None

            def use_markdown(self, value: bool) -> None:
                self.markdown = value

        class _Plain:
            def __init__(self, text: str) -> None:
                self.text = text

        class _At:
            pass

        class _Image:
            def __init__(self, data: bytes) -> None:
                self.data = data

            @classmethod
            def fromBytes(cls, data: bytes):
                return cls(data)

        class _Context:
            def __init__(self) -> None:
                self.sent_messages: list[object] = []

            def get_config(self) -> dict:
                return {"platform": [{"id": "test", "type": "qq_official"}]}

            async def send_message(self, _umo: str, chain) -> bool:
                self.sent_messages.append(chain)
                return True

        event_module = ModuleType("astrbot.api.event")
        event_module.MessageChain = _MessageChain
        components_module = ModuleType("astrbot.api.message_components")
        components_module.At = _At
        components_module.Image = _Image
        components_module.Plain = _Plain
        block = DeliveryBlock(
            text="Item A",
            title="Item A",
            mentions=[("alice", "Alice")],
            images=[b"\x89PNG\r\n\x1a\n"],
        )

        async def _send(umo: str, mention_users: bool):
            context = _Context()
            config = _NotifierConfig()
            config.mention_users = mention_users
            notifier = Notifier(context, _Storage(), config, _Logger())
            with patch.dict(
                "sys.modules",
                {
                    "astrbot.api.event": event_module,
                    "astrbot.api.message_components": components_module,
                },
            ):
                sent = await notifier._send_blocks(umo, [block])
            return sent, context.sent_messages

        private_sent, private_messages = await _send("test:FriendMessage:alice", True)
        disabled_sent, disabled_messages = await _send("test:GroupMessage:group", False)

        self.assertTrue(private_sent)
        self.assertEqual(len(private_messages), 1)
        self.assertTrue(any(isinstance(part, _Image) for part in private_messages[0].chain))
        self.assertTrue(disabled_sent)
        self.assertEqual(len(disabled_messages), 1)
        self.assertTrue(any(isinstance(part, _Image) for part in disabled_messages[0].chain))

    async def test_subscriber_only_receives_own_failure_details(self) -> None:
        class _FilterConfig(_NotifierConfig):
            enabled_subscriptions = [Subscription("bob", "item-b", target_umo="test:FriendMessage:bob")]
            push_batch_seconds = 0

        class _CaptureNotifier(Notifier):
            def __init__(self, *args) -> None:
                super().__init__(*args)
                self.seen_failures: list[str] = []

            async def _deliver(self, _uid, _umo, _events, failures, _name) -> bool:
                self.seen_failures = [quote.market_name for quote in (failures or [])]
                return True

        notifier = _CaptureNotifier(None, _Storage(), _FilterConfig(), _Logger())
        event = TriggerEvent(spec=MonitorSpec("item-b"), quote=Quote("item-b", sell_min=10.0))
        failures = [
            Quote("item-a", error="private failure"),
            Quote("item-b", error="own failure"),
        ]

        target = "test:FriendMessage:bob"
        await notifier.push_to_subscribers({(target, "bob"): [event]}, {(target, "bob"): "Bob"}, failures)

        self.assertEqual(notifier.seen_failures, ["item-b"])

    def test_config_token_sync_updates_cached_refresh_token(self) -> None:
        class Config:
            jwt_token = ""
            refresh_token = ""
            account_email = ""
            account_password = ""

        class Storage:
            def get_auth_state(self) -> dict:
                return {}

            def set_auth_state(self, *_args, **_kwargs) -> None:
                pass

        class Client:
            async def authorize_by_refresh_token(self, refresh_token: str) -> str:
                return "jwt-for-" + refresh_token

        config = Config()
        auth = AuthManager(Client(), Storage(), config, _Logger())
        auth.bootstrap()
        config.refresh_token = "new-refresh-token"

        updated = auth.sync_config_tokens()

        self.assertTrue(updated)
        self.assertEqual(auth.refresh_token, "new-refresh-token")
        self.assertEqual(auth.jwt, "")
        self.assertFalse(auth.sync_config_tokens())


if __name__ == "__main__":
    unittest.main()
