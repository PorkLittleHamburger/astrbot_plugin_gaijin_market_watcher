"""自动交易日志的按日增量存储。

新日志追加到 ``logs/trade-YYYY-MM-DD.jsonl``，每条事件只写一行。旧版
``trade_events.json`` 会迁移到按日文件，但保留原文件作为备份。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .trade_models import TradeEvent

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOG_DIR = "logs"
LOG_FILE_RE = re.compile(r"^trade-(\d{4}-\d{2}-\d{2})\.jsonl$")
LEGACY_EVENTS_FILE = "trade_events.json"
LOG_META_FILE = "trade_logs_meta.json"
LOG_SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30


class TradeLogStorageError(RuntimeError):
    """交易日志无法追加或读取。"""


class TradeLogStore:
    def __init__(
        self,
        storage,
        logger,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        memory_limit: int = 500,
    ) -> None:
        self.storage = storage
        self.logger = logger
        self.retention_days = max(1, int(retention_days))
        self.memory_limit = max(1, int(memory_limit))
        self.log_dir = Path(storage.data_path(LOG_DIR))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[TradeEvent] = []
        self._last_event_id = 0
        self._last_prune_day: date | None = None
        self._migrate_legacy()
        self._prune_old_files()
        self._load_recent()

    @property
    def events(self) -> list[TradeEvent]:
        with self._lock:
            return list(self._events)

    @property
    def next_cursor(self) -> int:
        with self._lock:
            return int(self._last_event_id)

    def recent(self, limit: int = 500) -> list[TradeEvent]:
        count = max(1, min(self.memory_limit, int(limit)))
        with self._lock:
            return list(self._events[-count:])

    def after(self, event_id: int, limit: int = 500) -> list[TradeEvent]:
        count = max(1, min(self.memory_limit, int(limit)))
        with self._lock:
            if int(event_id) <= 0:
                return list(self._events[-count:])
            return [event for event in self._events if int(event.event_id) > int(event_id)][:count]

    def append(self, event: TradeEvent) -> TradeEvent:
        with self._lock:
            event.event_id = max(time.time_ns() // 1_000_000, self._last_event_id + 1)
            event.occurred_at = datetime.fromtimestamp(float(event.timestamp), SHANGHAI).isoformat(timespec="seconds")
            path = self._path_for_timestamp(event.timestamp)
            line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            try:
                with path.open("a", encoding="utf-8", newline="\n") as fp:
                    fp.write(line)
                    fp.flush()
                    os.fsync(fp.fileno())
            except OSError as exc:
                raise TradeLogStorageError(f"交易日志追加失败: {exc}") from exc

            self._events.append(event)
            self._events = self._events[-self.memory_limit :]
            self._last_event_id = int(event.event_id)
            self._maybe_prune()
            return event

    def _path_for_timestamp(self, timestamp: float) -> Path:
        day = datetime.fromtimestamp(float(timestamp), SHANGHAI).date().isoformat()
        return self.log_dir / f"trade-{day}.jsonl"

    def _load_recent(self) -> None:
        loaded: list[TradeEvent] = []
        for path in sorted(self.log_dir.glob("trade-*.jsonl"), reverse=True):
            try:
                with path.open("r", encoding="utf-8") as fp:
                    for line in fp:
                        event = self._parse_line(line)
                        if event is not None:
                            loaded.append(event)
            except OSError as exc:
                self.logger.warning(f"读取交易日志 {path.name} 失败: {exc}")
            if len(loaded) >= self.memory_limit:
                break
        loaded.sort(key=lambda item: (int(item.event_id), float(item.timestamp)))
        self._events = loaded[-self.memory_limit :]
        self._last_event_id = max((int(event.event_id) for event in self._events), default=0)

    def _parse_line(self, line: str) -> TradeEvent | None:
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(item, dict):
            return None
        try:
            return TradeEvent(
                timestamp=float(item.get("timestamp") or 0),
                rule_id=str(item.get("rule_id") or ""),
                kind=str(item.get("kind") or ""),
                message=str(item.get("message") or ""),
                level=str(item.get("level") or "info"),
                details=item.get("details") if isinstance(item.get("details"), dict) else {},
                event_id=int(item.get("event_id") or 0),
                occurred_at=str(item.get("occurred_at") or ""),
            )
        except (TypeError, ValueError):
            return None

    def _migrate_legacy(self) -> None:
        meta = self.storage.load_aux_json(LOG_META_FILE, {})
        if isinstance(meta, dict) and int(meta.get("schema_version") or 0) >= LOG_SCHEMA_VERSION:
            return
        raw_events = self.storage.load_aux_json(LEGACY_EVENTS_FILE, None)
        migrated = 0
        if isinstance(raw_events, list):
            existing = self._existing_event_keys()
            next_id = 0
            for item in raw_events:
                event = self._event_from_legacy(item)
                if event is None:
                    continue
                event_key = self._event_key(event)
                if event_key in existing:
                    continue
                next_id = max(time.time_ns() // 1_000_000, next_id + 1)
                event.event_id = next_id
                event.occurred_at = datetime.fromtimestamp(float(event.timestamp), SHANGHAI).isoformat(
                    timespec="seconds"
                )
                self._append_without_lock(event)
                existing.add(event_key)
                migrated += 1
        if not self.storage.save_aux_json(
            LOG_META_FILE,
            {
                "schema_version": LOG_SCHEMA_VERSION,
                "retention_days": self.retention_days,
                "legacy_migrated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            },
        ):
            self.logger.warning("交易日志迁移标记写入失败，下次启动会再次尝试。")
        if migrated:
            self.logger.info(f"已迁移 {migrated} 条旧交易日志到按日 JSONL 文件。")

    @staticmethod
    def _event_from_legacy(item: Any) -> TradeEvent | None:
        if not isinstance(item, dict):
            return None
        try:
            return TradeEvent(
                timestamp=float(item.get("timestamp") or 0),
                rule_id=str(item.get("rule_id") or ""),
                kind=str(item.get("kind") or ""),
                message=str(item.get("message") or ""),
                level=str(item.get("level") or "info"),
                details=item.get("details") if isinstance(item.get("details"), dict) else {},
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _event_key(event: TradeEvent) -> tuple[float, str, str, str, str]:
        return (
            float(event.timestamp),
            str(event.rule_id),
            str(event.kind),
            str(event.message),
            str(event.level),
        )

    def _existing_event_keys(self) -> set[tuple[float, str, str, str, str]]:
        keys: set[tuple[float, str, str, str, str]] = set()
        for path in self.log_dir.glob("trade-*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as fp:
                    for line in fp:
                        event = self._parse_line(line)
                        if event is not None:
                            keys.add(self._event_key(event))
            except OSError as exc:
                self.logger.warning(f"检查已迁移交易日志 {path.name} 失败: {exc}")
        return keys

    def _append_without_lock(self, event: TradeEvent) -> None:
        path = self._path_for_timestamp(event.timestamp)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with path.open("a", encoding="utf-8", newline="\n") as fp:
                fp.write(line)
                fp.flush()
                os.fsync(fp.fileno())
        except OSError as exc:
            raise TradeLogStorageError(f"迁移交易日志失败: {exc}") from exc

    def _maybe_prune(self) -> None:
        today = datetime.now(SHANGHAI).date()
        if self._last_prune_day == today:
            return
        self._prune_old_files(today=today)

    def _prune_old_files(self, *, today: date | None = None) -> None:
        current_day = today or datetime.now(SHANGHAI).date()
        cutoff = current_day - timedelta(days=self.retention_days - 1)
        for path in self.log_dir.glob("trade-*.jsonl"):
            matched = LOG_FILE_RE.match(path.name)
            if not matched:
                continue
            try:
                file_day = date.fromisoformat(matched.group(1))
            except ValueError:
                continue
            if file_day >= cutoff:
                continue
            try:
                path.unlink()
            except OSError as exc:
                self.logger.warning(f"清理过期交易日志 {path.name} 失败: {exc}")
        self._last_prune_day = current_day
