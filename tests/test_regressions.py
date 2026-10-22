from __future__ import annotations

import time
import unittest
from types import ModuleType
from unittest.mock import patch

from core.api_client import GaijinMarketClient
from core.auth import AuthManager
from core.config import PluginConfig
from core.models import MonitorSpec, Quote, Subscription, TriggerEvent
from core.notifier import Notifier
from core.payload import substitute
from core.resolver import extract_link_reference
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

    def resolve_uid(self, uid: str) -> str:
        return self.sessions.get(uid, "")

    def known_platforms(self) -> set[str]:
        return {"test"}

    def get_user_name(self, uid: str) -> str:
        return uid.title()

    def get_item_meta(self, _market_name: str) -> dict:
        return {}


class _NotifierConfig:
    push_mode = "on_change"
    payload = ""
    push_batch_seconds = 10
    mention_users = True
    appid = "1067"
    owner_uids: list[str] = []


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

    def display_name_of(self, market_name: str) -> str:
        return market_name


class _WatcherConfig:
    def __init__(self, interval: int) -> None:
        self.subscription = Subscription("user", "item", interval=interval)
        self.enabled_subscriptions = [self.subscription]
        self.watched_market_names = ["item"]

    def interval_of(self, _market_name: str) -> int:
        return self.subscription.interval


class _WatcherNotifier:
    pass


class RegressionTests(unittest.IsolatedAsyncioTestCase):
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

    def test_empty_mention_is_not_treated_as_rendered(self) -> None:
        text, used_mention, _unknown = substitute("$mention", {"mention": ""})

        self.assertEqual(text, "")
        self.assertFalse(used_mention)

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
                self.sent = None

            def get_config(self) -> dict:
                return {"platform": [{"id": "test", "type": "telegram"}]}

            async def send_message(self, _umo: str, chain) -> bool:
                self.sent = chain
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
        self.assertIsInstance(context.sent.chain[0], _At)
        self.assertEqual(context.sent.chain[0].qq, "alice")
        self.assertTrue(any(isinstance(item, _Plain) and item.text == "Item" for item in context.sent.chain))

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

        auth.sync_config_tokens()

        self.assertEqual(auth.refresh_token, "new-refresh-token")
        self.assertEqual(auth.jwt, "")


if __name__ == "__main__":
    unittest.main()
