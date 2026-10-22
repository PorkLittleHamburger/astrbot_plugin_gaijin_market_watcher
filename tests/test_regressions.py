from __future__ import annotations

import time
import unittest
from types import ModuleType
from unittest.mock import patch

from core.api_client import GaijinMarketClient
from core.auth import AuthManager
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

    async def test_candidate_refresh_token_validation_does_not_mutate_auth(self) -> None:
        class Config:
            jwt_token = ""
            refresh_token = "existing"
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

        auth = AuthManager(Client(), Storage(), Config(), _Logger())
        auth.bootstrap()

        jwt = await auth.validate_refresh_token("candidate")

        self.assertEqual(jwt, "jwt-for-candidate")
        self.assertEqual(auth.refresh_token, "existing")
        self.assertEqual(auth.jwt, "")


if __name__ == "__main__":
    unittest.main()
