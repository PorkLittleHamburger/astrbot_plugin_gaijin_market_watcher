"""行情/交易输出模板设置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .payload import (
    ITEM_FIELDS,
    ROUND_FIELDS,
    TRADE_FIELD_GROUPS,
    TRADE_FIELDS,
    TRADE_KNOWN_FIELDS,
    render_message,
    validate_payload,
)

OUTPUT_FILE = "output.json"

DEFAULT_QUOTE_PAYLOAD = """{
    "title":"$item_name行情报告",
    "datetime":"时间：$datetime",
    "content":["卖单最低$sell_lowest_price，较上次变化$sell_change_amount($sell_change_rate)",
                "买单最高$buy_highest_price，较上次变化$buy_change_amount($buy_change_rate)",
                "卖单数$sell_count,较上次变化$sell_count_change_amount($sell_count_change_rate)",
                "买单数$buy_count,较上次变化$buy_count_change_amount($buy_count_change_rate)"]
}"""

DEFAULT_TRADE_PAYLOAD = '{"text":"【GJM 自动交易】$trade_message"}'

TRADE_SAMPLE_VALUES: dict[str, str] = {
    "trade_kind": "cycle_complete",
    "trade_kind_text": "周期完成",
    "trade_level": "info",
    "trade_message": "F-16XL 已完成一轮模拟交易，税后盈亏 +2.55 GJN",
    "trade_datetime": "2026-10-24-21-30-00",
    "trade_datetime_plain": "2026-10-24 21:30:00",
    "trade_timestamp": "1790266200",
    "trade_rule_id": "T1A2B3C4",
    "trade_name": "F-16XL",
    "trade_market_name": "id50381_f_16xl_usa",
    "trade_url": "https://trade.gaijin.net/market/1067/id50381_f_16xl_usa",
    "trade_item_type": "commodity",
    "trade_mode": "loop",
    "trade_live": "否",
    "trade_enabled": "是",
    "trade_phase": "waiting_buy",
    "trade_last_error": "",
    "trade_held_qty": "0",
    "trade_listed_qty": "0",
    "trade_available_qty": "0",
    "trade_cost_basis": "0.00",
    "trade_proceeds": "27.20",
    "trade_profit": "+2.55",
    "trade_cycle_count": "3",
    "trade_buy_price": "20.00",
    "trade_sell_net_price": "27.20",
    "trade_sell_gross_price": "32.00",
    "trade_last_buy_price": "20.00",
    "trade_last_sell_price": "32.00",
    "trade_side": "SELL",
    "trade_price": "32.00",
    "trade_quantity": "1",
    "trade_amount": "32.00",
    "trade_fee": "15%",
    "trade_net_price": "27.20",
    "trade_order_id": "order-demo-123",
}


@dataclass(slots=True)
class OutputSettings:
    quote_payload: str = DEFAULT_QUOTE_PAYLOAD
    trade_payload: str = DEFAULT_TRADE_PAYLOAD

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_payload": str(self.quote_payload or ""),
            "trade_payload": str(self.trade_payload or ""),
        }

    @classmethod
    def from_dict(cls, data: Any) -> OutputSettings:
        if not isinstance(data, dict):
            return cls()
        quote_payload = data.get("quote_payload")
        trade_payload = data.get("trade_payload")
        return cls(
            quote_payload=DEFAULT_QUOTE_PAYLOAD if quote_payload is None else str(quote_payload),
            trade_payload=DEFAULT_TRADE_PAYLOAD if trade_payload is None else str(trade_payload),
        )


class OutputStore:
    def __init__(self, storage, logger) -> None:
        self.storage = storage
        self.logger = logger
        self.persisted = False
        raw = storage.load_aux_json(OUTPUT_FILE, None)
        if isinstance(raw, dict):
            self.settings = OutputSettings.from_dict(raw)
            self.persisted = True
        else:
            self.settings = OutputSettings()

    @property
    def quote_payload(self) -> str:
        return self.settings.quote_payload

    @property
    def trade_payload(self) -> str:
        return self.settings.trade_payload

    def migrate_legacy(self, raw_config: dict) -> bool:
        if self.persisted:
            return True
        quote = DEFAULT_QUOTE_PAYLOAD if "payload" not in raw_config else str(raw_config.get("payload") or "")
        self.settings = OutputSettings(quote_payload=quote, trade_payload=DEFAULT_TRADE_PAYLOAD)
        return self.save(quote_payload=quote, trade_payload=DEFAULT_TRADE_PAYLOAD, _force=True)

    def save(self, *, quote_payload: str, trade_payload: str, _force: bool = False) -> bool:
        if len(quote_payload) > 100_000 or len(trade_payload) > 100_000:
            raise ValueError("输出模板不能超过 100000 个字符")
        ok_quote, quote_note = self.validate(quote_payload, "quote")
        ok_trade, trade_note = self.validate(trade_payload, "trade")
        if not ok_quote or not ok_trade:
            raise ValueError(quote_note if not ok_quote else trade_note)
        settings = OutputSettings(
            quote_payload=str(quote_payload or ""),
            trade_payload=str(trade_payload or ""),
        )
        if not self.storage.save_aux_json(OUTPUT_FILE, settings.to_dict()):
            return False
        self.settings = settings
        self.persisted = True
        return True

    @staticmethod
    def validate(template: str, kind: str) -> tuple[bool, str]:
        if kind == "trade":
            return validate_payload(template, known_fields=TRADE_KNOWN_FIELDS, sample=TRADE_SAMPLE_VALUES)
        return validate_payload(template)

    @staticmethod
    def preview(template: str, kind: str) -> dict[str, Any]:
        if kind not in {"quote", "trade"}:
            raise ValueError("模板类型只能是 quote 或 trade")
        if not str(template or "").strip():
            return {
                "ok": True,
                "text": "",
                "lines": [],
                "images": [],
                "unknown_fields": [],
                "error": "",
                "note": "留空将使用默认可读文本",
            }
        if kind == "trade":
            sample = {name: name for name in TRADE_FIELDS}
            sample.update(TRADE_SAMPLE_VALUES)
        else:
            sample = {name: name for name in (*ROUND_FIELDS, *ITEM_FIELDS)}
            sample.update(
                {
                    "title": "Gaijin 行情",
                    "datetime": "2026-10-24-21-30-00",
                    "name": "F-16XL",
                    "item_name": "F-16XL",
                    "sell_text": "45.10",
                    "sell_lowest_price": "45.10",
                    "buy_text": "43.00",
                    "buy_highest_price": "43.00",
                }
            )
        model = render_message(template, sample)
        return {
            "ok": not model.error,
            "text": model.text,
            "lines": list(model.lines),
            "images": list(model.images),
            "unknown_fields": list(model.unknown_fields),
            "error": model.error,
            "note": "",
        }

    @staticmethod
    def fields() -> dict[str, Any]:
        return {
            "quote": {"整轮字段": dict(ROUND_FIELDS), "物品字段": dict(ITEM_FIELDS)},
            "trade": {name: dict(values) for name, values in TRADE_FIELD_GROUPS.items()},
        }
