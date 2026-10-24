from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml


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
        self.assertEqual(self.module.PLUGIN_NAME, "astrbot_plugin_gaijin_market_autotrader")
        self.assertTrue(hasattr(self.module.GaijinMarketWatcher, "gjma"))
        self.assertTrue(hasattr(self.module.GaijinMarketWatcher, "gjm_trade_add"))
        self.assertTrue(hasattr(self.module.GaijinMarketWatcher, "_trade_web_overview"))
        metadata = yaml.safe_load((Path(__file__).resolve().parents[1] / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["astrbot_version"], ">=4.29.0,<5")

    def test_trade_access_requires_owner_and_friend_session(self) -> None:
        watcher_class = self.module.GaijinMarketWatcher
        fake_self = SimpleNamespace(
            cfg=SimpleNamespace(owner_uids=["owner"]),
            _is_owner=lambda _event: True,
        )
        group_event = SimpleNamespace(unified_msg_origin="p:GroupMessage:g")
        private_event = SimpleNamespace(unified_msg_origin="p:FriendMessage:owner")
        self.assertIn("私聊", watcher_class._trade_access_error(fake_self, group_event))
        self.assertEqual(watcher_class._trade_access_error(fake_self, private_event), "")
        fake_self.cfg.owner_uids = []
        self.assertIn("owner_uids", watcher_class._trade_access_error(fake_self, private_event))
        fake_self.cfg.owner_uids = ["owner"]
        fake_self._is_owner = lambda _event: False
        self.assertIn("仅插件拥有者", watcher_class._trade_access_error(fake_self, private_event))
