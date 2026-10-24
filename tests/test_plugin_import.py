from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from core.storage import PluginStorage


class _Group:
    def __init__(self, function=None) -> None:
        self.function = function

    def __call__(self, *args, **kwargs):
        if self.function is None and len(args) == 1 and callable(args[0]) and not kwargs:
            self.function = args[0]
            return self
        if self.function is not None:
            return self.function(*args, **kwargs)
        return None

    @staticmethod
    def _decorator(*_args, **_kwargs):
        def apply(function):
            return function

        return apply

    def command(self, *_args, **_kwargs):
        return self._decorator()

    def group(self, *_args, **_kwargs):
        def apply(function):
            return _Group(function)

        return apply


class _Filter:
    class EventMessageType:
        ALL = "ALL"

    @staticmethod
    def _decorator(*_args, **_kwargs):
        def apply(function):
            return function

        return apply

    command = staticmethod(_decorator)
    command_group = staticmethod(lambda *_args, **_kwargs: _Group())
    event_message_type = staticmethod(_decorator)
    on_astrbot_loaded = staticmethod(_decorator)

    @staticmethod
    def group(*_args, **_kwargs):
        def apply(function):
            return _Group(function)

        return apply


class _Response:
    pass


class _Request:
    username = "tester"


def _install_astrbot_stubs() -> None:
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.filter = _Filter()

    star = types.ModuleType("astrbot.api.star")
    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {"__init__": lambda self, context: None})
    star.StarTools = type("StarTools", (), {"get_data_dir": staticmethod(lambda _name: ".")})
    star.register = lambda *_args, **_kwargs: (lambda cls: cls)

    web = types.ModuleType("astrbot.api.web")
    web.error_response = lambda *args, **kwargs: _Response()
    web.json_response = lambda *args, **kwargs: _Response()
    web.request = _Request()

    package = types.ModuleType("astrbot")
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "astrbot": package,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.web": web,
        }
    )


class PluginImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _install_astrbot_stubs()
        path = Path(__file__).resolve().parents[1] / "main.py"
        spec = importlib.util.spec_from_file_location("gjm_plugin_import_smoke", path)
        if spec is None:
            raise RuntimeError("cannot build import spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.module = module

    def test_main_module_imports_with_astrbot_api_surface(self) -> None:
        self.assertTrue(hasattr(self.module, "GaijinMarketWatcher"))
        self.assertEqual(self.module.PLUGIN_NAME, "astrbot_plugin_gaijin_market_watcher")
        self.assertTrue(hasattr(self.module.GaijinMarketWatcher, "gjm"))
        self.assertTrue(hasattr(self.module.GaijinMarketWatcher, "gjm_trade_add"))
        self.assertTrue(hasattr(self.module.GaijinMarketWatcher, "_trade_web_overview"))
        for handler in (
            "_account_web_overview",
            "_account_web_save",
            "_account_web_login_start",
            "_account_web_login_code",
            "_monitor_web_overview",
            "_monitor_web_save",
            "_monitor_web_delete",
            "_output_web_overview",
            "_output_web_save",
            "_output_web_preview",
            "_trade_web_drill",
        ):
            self.assertTrue(hasattr(self.module.GaijinMarketWatcher, handler), handler)
        metadata = yaml.safe_load((Path(__file__).resolve().parents[1] / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["name"], "astrbot_plugin_gaijin_market_watcher")
        self.assertEqual(metadata["display_name"], "Gaijin 市场行情监控")
        self.assertEqual(metadata["astrbot_version"], ">=4.29.0,<5")
        self.assertEqual(metadata["version"], "2.20.1")
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command_group("gjm"', source)
        self.assertIn('"/gjma"', source)

    def test_trade_access_requires_owner_and_friend_session(self) -> None:
        watcher_class = self.module.GaijinMarketWatcher
        fake_self = SimpleNamespace(
            accounts=SimpleNamespace(owner_uids=["owner"]),
            _is_owner=lambda _event: True,
        )
        group_event = SimpleNamespace(unified_msg_origin="p:GroupMessage:g")
        private_event = SimpleNamespace(unified_msg_origin="p:FriendMessage:owner")
        self.assertIn("私聊", watcher_class._trade_access_error(fake_self, group_event))
        self.assertEqual(watcher_class._trade_access_error(fake_self, private_event), "")
        fake_self.accounts.owner_uids = []
        self.assertIn("拥有者 UID", watcher_class._trade_access_error(fake_self, private_event))
        fake_self.accounts.owner_uids = ["owner"]
        fake_self._is_owner = lambda _event: False
        self.assertIn("仅插件拥有者", watcher_class._trade_access_error(fake_self, private_event))

    def test_former_autotrader_trade_data_is_merged_once(self) -> None:
        class _Logger:
            def warning(self, *_args, **_kwargs) -> None:
                pass

            def info(self, *_args, **_kwargs) -> None:
                pass

        watcher_class = self.module.GaijinMarketWatcher
        with tempfile.TemporaryDirectory() as tmp:
            plugin_data = Path(tmp) / "plugin_data"
            target = plugin_data / self.module.PLUGIN_NAME
            source = plugin_data / self.module.FORMER_AUTOTRADER_PLUGIN_NAME
            target.mkdir(parents=True)
            (source / "logs").mkdir(parents=True)
            (source / "trade_rules.json").write_text('[{"rule_id":"T1"}]', encoding="utf-8")
            (source / "logs" / "trade-2026-10-25.jsonl").write_text('{"event_id":1}\n', encoding="utf-8")

            logger = _Logger()
            fake_self = SimpleNamespace(
                storage=PluginStorage(target, logger),
                data_dir=target,
                logger=logger,
            )
            watcher_class._migrate_former_autotrader_data(fake_self)

            self.assertEqual((target / "trade_rules.json").read_text(encoding="utf-8"), '[{"rule_id":"T1"}]')
            self.assertTrue((target / "logs" / "trade-2026-10-25.jsonl").is_file())
            self.assertTrue((target / self.module.FORMER_MIGRATION_MARKER).is_file())
