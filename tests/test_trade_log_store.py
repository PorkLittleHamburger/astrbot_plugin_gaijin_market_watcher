from __future__ import annotations

import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.storage import PluginStorage
from core.trade_log_store import LOG_META_FILE, TradeLogStore
from core.trade_models import TradeEvent

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass


class TradeLogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = PluginStorage(Path(self.tmp.name), _Logger())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _event(message: str, timestamp: float | None = None) -> TradeEvent:
        return TradeEvent(
            timestamp=time.time() if timestamp is None else timestamp,
            rule_id="T1",
            kind="paper",
            message=message,
        )

    def test_append_only_daily_jsonl_and_cursor(self) -> None:
        store = TradeLogStore(self.storage, _Logger())
        first = store.append(self._event("first"))
        second = store.append(self._event("second"))

        self.assertGreater(second.event_id, first.event_id)
        self.assertLess(second.event_id, 9_007_199_254_740_991)
        self.assertTrue(first.occurred_at)
        self.assertEqual([event.message for event in store.after(first.event_id)], ["second"])
        self.assertEqual([event.message for event in store.recent(10)], ["first", "second"])

        path = next((Path(self.tmp.name) / "logs").glob("trade-*.jsonl"))
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["event_id"], first.event_id)
        self.assertEqual(json.loads(lines[1])["event_id"], second.event_id)
        before_append = path.read_text(encoding="utf-8")
        store.append(self._event("third"))
        self.assertTrue(path.read_text(encoding="utf-8").startswith(before_append))

    def test_concurrent_appends_are_ordered_without_rewriting(self) -> None:
        store = TradeLogStore(self.storage, _Logger())
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda index: store.append(self._event(f"event-{index}")), range(20)))
        events = store.recent(30)
        self.assertEqual(len(events), 20)
        self.assertEqual([event.event_id for event in events], sorted(event.event_id for event in events))
        path = next((Path(self.tmp.name) / "logs").glob("trade-*.jsonl"))
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 20)

    def test_legacy_events_are_migrated_once(self) -> None:
        timestamp = time.time()
        self.storage.save_aux_json(
            "trade_events.json",
            [
                {
                    "timestamp": timestamp,
                    "rule_id": "T1",
                    "kind": "legacy",
                    "message": "legacy event",
                    "level": "info",
                    "details": {},
                }
            ],
        )
        first = TradeLogStore(self.storage, _Logger())
        self.assertEqual(len(first.recent(10)), 1)
        self.assertTrue(self.storage.load_aux_json(LOG_META_FILE, {}))

        second = TradeLogStore(self.storage, _Logger())
        self.assertEqual(len(second.recent(10)), 1)
        self.assertEqual(second.recent(10)[0].message, "legacy event")
        (Path(self.tmp.name) / LOG_META_FILE).unlink()
        third = TradeLogStore(self.storage, _Logger())
        self.assertEqual(len(third.recent(10)), 1)

    def test_logs_older_than_retention_are_removed(self) -> None:
        log_dir = Path(self.tmp.name) / "logs"
        log_dir.mkdir()
        old_day = datetime.now(SHANGHAI).date() - timedelta(days=31)
        recent_day = datetime.now(SHANGHAI).date() - timedelta(days=2)
        old_path = log_dir / f"trade-{old_day.isoformat()}.jsonl"
        recent_path = log_dir / f"trade-{recent_day.isoformat()}.jsonl"
        old_path.write_text("", encoding="utf-8")
        recent_path.write_text("", encoding="utf-8")

        TradeLogStore(self.storage, _Logger(), retention_days=30)

        self.assertFalse(old_path.exists())
        self.assertTrue(recent_path.exists())
