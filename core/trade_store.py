"""自动交易的持久化门面。

规则、运行状态、安全设置、每日账本和事件日志分别文件保存，避免一次高频
日志写入反复重写全部规则。底层读写仍由 :class:`core.storage.PluginStorage`
统一处理。
"""

from __future__ import annotations

from typing import Any

from .trade_log_store import TradeLogStorageError, TradeLogStore
from .trade_models import TradeEvent, TradeLedger, TradeRule, TradeRuntime, TradeSettings

RULES_FILE = "trade_rules.json"
RUNTIME_FILE = "trade_runtime.json"
SETTINGS_FILE = "trade_settings.json"
LEDGER_FILE = "trade_ledger.json"
MAX_EVENTS = 500


class TradeStorageError(RuntimeError):
    """交易数据无法安全落盘。"""


class TradeStore:
    def __init__(self, storage, logger) -> None:
        self.storage = storage
        self.logger = logger
        self.rules: list[TradeRule] = []
        self.runtimes: dict[str, TradeRuntime] = {}
        self.settings = TradeSettings()
        self.ledger = TradeLedger()
        self.events: list[TradeEvent] = []
        self.log_store = TradeLogStore(storage, logger, memory_limit=MAX_EVENTS)
        self.reload()

    # ------------------------------------------------------------------ 载入

    def reload(self) -> None:
        raw_rules = self.storage.load_aux_json(RULES_FILE, [])
        self.rules = []
        for item in raw_rules if isinstance(raw_rules, list) else []:
            if not isinstance(item, dict) or not item.get("rule_id") or not item.get("market_name"):
                continue
            try:
                self.rules.append(TradeRule.from_dict(item))
            except Exception as exc:
                self.logger.warning(f"忽略损坏的交易规则: {exc}")

        raw_runtime = self.storage.load_aux_json(RUNTIME_FILE, {})
        self.runtimes = {}
        for rule in self.rules:
            item = raw_runtime.get(rule.rule_id) if isinstance(raw_runtime, dict) else None
            if isinstance(item, dict):
                try:
                    self.runtimes[rule.rule_id] = TradeRuntime.from_dict(item)
                except Exception as exc:
                    self.logger.warning(f"忽略损坏的交易状态 {rule.rule_id}: {exc}")
            if rule.rule_id not in self.runtimes:
                self.runtimes[rule.rule_id] = TradeRuntime(rule_id=rule.rule_id)

        raw_settings = self.storage.load_aux_json(SETTINGS_FILE, {})
        try:
            self.settings = TradeSettings.from_dict(raw_settings if isinstance(raw_settings, dict) else {})
        except Exception:
            self.settings = TradeSettings()

        raw_ledger = self.storage.load_aux_json(LEDGER_FILE, {})
        try:
            self.ledger = TradeLedger.from_dict(raw_ledger if isinstance(raw_ledger, dict) else {})
        except Exception:
            self.ledger = TradeLedger()

        self.events = self.log_store.recent(MAX_EVENTS)

    # ------------------------------------------------------------------ 规则

    def get_rule(self, rule_id: str) -> TradeRule | None:
        return next((rule for rule in self.rules if rule.rule_id == rule_id), None)

    def get_runtime(self, rule_id: str) -> TradeRuntime:
        runtime = self.runtimes.get(rule_id)
        if runtime is None:
            runtime = TradeRuntime(rule_id=rule_id)
            self.runtimes[rule_id] = runtime
        return runtime

    def save_rules(self) -> None:
        data = [rule.to_dict() for rule in self.rules]
        if not self.storage.save_aux_json(RULES_FILE, data):
            raise TradeStorageError("交易规则写入失败")

    def save_runtime(self) -> None:
        data = {rule_id: runtime.to_dict() for rule_id, runtime in self.runtimes.items()}
        if not self.storage.save_aux_json(RUNTIME_FILE, data):
            raise TradeStorageError("交易运行状态写入失败")

    def save_settings(self) -> None:
        if not self.storage.save_aux_json(SETTINGS_FILE, self.settings.to_dict()):
            raise TradeStorageError("交易安全设置写入失败")

    def save_ledger(self) -> None:
        if not self.storage.save_aux_json(LEDGER_FILE, self.ledger.to_dict()):
            raise TradeStorageError("交易账本写入失败")

    def append_event(self, event: TradeEvent) -> TradeEvent:
        try:
            self.log_store.append(event)
            self.events = self.log_store.recent(MAX_EVENTS)
        except TradeLogStorageError as exc:
            # 日志写失败不应阻止交易主流程，但必须在插件日志里留下痕迹。
            self.logger.warning(str(exc))
        return event

    def events_after(self, event_id: int, limit: int = 100) -> list[TradeEvent]:
        return self.log_store.after(event_id, limit)

    @property
    def log_cursor(self) -> int:
        return self.log_store.next_cursor

    def overview(self) -> dict[str, Any]:
        return {
            "rules": [rule.to_dict() for rule in self.rules],
            "runtimes": {rule_id: runtime.to_dict() for rule_id, runtime in self.runtimes.items()},
            "settings": self.settings.to_dict(),
            "ledger": self.ledger.to_dict(),
        }
