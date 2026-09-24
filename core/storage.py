"""持久化层：本项目所有落盘行为都集中在这里。

AstrBot 约定：持久化数据必须写在 data/ 目录（data/plugin_data/<插件名>/），
而不是插件自身目录，否则插件更新 / 重装时数据会被覆盖。
本模块由 main.py 通过 StarTools.get_data_dir() 得到目录后注入。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .models import Quote


class PluginStorage:
    """极简 JSON 存储。

    文件划分：
      names.json    物品 ID -> 显示名 的缓存（让推送里显示人话）
      sessions.json 用户 UID -> 会话 UMO 的映射（支持"用 UID 配置推送对象"）
      pending_login.json 待提交验证码的登录中间态（会话 Cookie + 表单字段，不含密码）
      quotes.json   每个物品"最近一次"的行情快照（用于比对价格变化）
      history.json  每个物品最近 N 条行情（用于走势回溯）
      subs.json     订阅会话（umo 列表）
      auth.json     鉴权状态（刷新后的最新 JWT / refresh_token）
    """

    MAX_HISTORY = 200  # 每个物品最多保留的历史点数

    def __init__(self, data_dir: Path, logger) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self._names_file = self.dir / "names.json"
        self._sessions_file = self.dir / "sessions.json"
        self._pending_login_file = self.dir / "pending_login.json"
        self._quotes_file = self.dir / "quotes.json"
        self._history_file = self.dir / "history.json"
        self._subs_file = self.dir / "subs.json"
        self._auth_file = self.dir / "auth.json"
        self._meta_file = self.dir / "items_meta.json"

    # ------------------------------------------------------------------ 基础 IO

    def _load(self, path: Path, default: Any) -> Any:
        try:
            if not path.exists():
                return default
            with path.open("r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception as exc:  # 配置损坏不应让插件崩溃
            self.logger.warning(f"读取 {path.name} 失败，使用默认值: {exc}")
            return default

    def _save(self, path: Path, data: Any) -> None:
        # 先写临时文件再原子替换，避免进程中断留下半个 JSON
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            self.logger.warning(f"写入 {path.name} 失败: {exc}")

    # ------------------------------------------------------------- 行情快照

    def get_last_quote(self, market_name: str) -> Quote | None:
        raw = self._load(self._quotes_file, {})
        item = raw.get(market_name) if isinstance(raw, dict) else None
        return Quote.from_dict(item) if isinstance(item, dict) else None

    def set_last_quote(self, quote: Quote) -> None:
        data = self._load(self._quotes_file, {})
        if not isinstance(data, dict):
            data = {}
        data[quote.market_name] = quote.to_dict()
        self._save(self._quotes_file, data)
        self.append_history(quote)

    # ------------------------------------------------------------- 历史记录

    def append_history(self, quote: Quote) -> None:
        if not quote.ok:
            return
        data = self._load(self._history_file, {})
        if not isinstance(data, dict):
            data = {}
        points = data.get(quote.market_name) or []
        points.append(
            {
                "t": quote.timestamp,
                "sell": quote.sell_min,
                "buy": quote.buy_max,
            }
        )
        data[quote.market_name] = points[-self.MAX_HISTORY :]
        self._save(self._history_file, data)

    def get_history(self, market_name: str) -> list[dict[str, Any]]:
        data = self._load(self._history_file, {})
        points = data.get(market_name) if isinstance(data, dict) else None
        return points if isinstance(points, list) else []

    # ------------------------------------------------------------- 订阅会话

    def list_subscriptions(self) -> list[str]:
        subs = self._load(self._subs_file, [])
        return [s for s in subs if isinstance(s, str)] if isinstance(subs, list) else []

    # ------------------------------------------------------------- 登录中间态

    def get_pending_login(self) -> dict:
        """读取等待提交验证码的登录状态（不存在返回空 dict）。"""
        data = self._load(self._pending_login_file, {})
        return data if isinstance(data, dict) else {}

    def set_pending_login(self, data: dict) -> None:
        """保存登录中间态：只含会话 Cookie 与表单字段，**不含账号密码**。"""
        if not isinstance(data, dict) or not data:
            self.clear_pending_login()
            return
        self._save(self._pending_login_file, data)

    def clear_pending_login(self) -> None:
        try:
            if self._pending_login_file.exists():
                self._pending_login_file.unlink()
        except Exception as exc:
            self.logger.warning(f"清理登录中间态失败: {exc}")

    # ------------------------------------------------------------- 会话映射

    def remember_session(self, uid: str, umo: str, platform: str = "", name: str = "") -> None:
        """记住 UID -> 会话(UMO) 的映射，以及该用户的显示名。

        UID -> 会话是"用 UID 配置推送对象 / @ 订阅者"的关键：
        AstrBot 发送消息只能按会话(UMO)寻址，所以必须先把 UID 落到具体会话上。
        """
        if not uid or not umo:
            return
        data = self._load(self._sessions_file, {})
        if not isinstance(data, dict):
            data = {}
        entry = data.get(uid) if isinstance(data.get(uid), dict) else {}
        if entry.get("umo") == umo and entry.get("platform") == platform and (not name or entry.get("name") == name):
            return  # 无变化则不落盘
        data[uid] = {
            "umo": umo,
            "platform": platform or entry.get("platform", ""),
            "name": name or entry.get("name", ""),
            "updated_at": time.time(),
        }
        self._save(self._sessions_file, data)

    def get_user_name(self, uid: str) -> str:
        """取缓存里的用户显示名（@ 时用；没有则返回空串）。"""
        data = self._load(self._sessions_file, {})
        entry = data.get(uid) if isinstance(data, dict) else None
        return str(entry.get("name") or "") if isinstance(entry, dict) else ""

    def resolve_uid(self, uid: str) -> str:
        """UID -> 会话 UMO；未记录过则返回空串。"""
        data = self._load(self._sessions_file, {})
        entry = data.get(uid) if isinstance(data, dict) else None
        return str(entry.get("umo") or "") if isinstance(entry, dict) else ""

    def known_platforms(self) -> set[str]:
        """已知的平台标识集合（用于给陌生 UID 拼私聊会话）。"""
        data = self._load(self._sessions_file, {})
        if not isinstance(data, dict):
            return set()
        return {str(v.get("platform")) for v in data.values() if isinstance(v, dict) and v.get("platform")}

    # ------------------------------------------------------------- 显示名缓存

    def get_item_name(self, market_name: str) -> str:
        """取物品的显示名（未缓存则返回空串）。"""
        names = self._load(self._names_file, {})
        if not isinstance(names, dict):
            return ""
        return str(names.get(market_name) or "")

    def set_item_name(self, market_name: str, name: str) -> None:
        """缓存物品显示名，避免推送里出现冰冷的 ID。"""
        if not market_name or not name:
            return
        names = self._load(self._names_file, {})
        if not isinstance(names, dict):
            names = {}
        if names.get(market_name) == name:
            return
        names[market_name] = name
        self._save(self._names_file, names)

    # ------------------------------------------------------------- 物品元数据

    def get_item_meta(self, market_name: str) -> dict[str, Any]:
        """取物品元数据（图标 / 标签 / 稀有度颜色）；未缓存返回空 dict。"""
        data = self._load(self._meta_file, {})
        item = data.get(market_name) if isinstance(data, dict) else None
        return item if isinstance(item, dict) else {}

    def set_item_meta(self, market_name: str, meta: dict[str, Any]) -> None:
        """缓存物品元数据（供 payload 模板的 image / tags / rarity / color 使用）。"""
        if not market_name or not meta:
            return
        data = self._load(self._meta_file, {})
        if not isinstance(data, dict):
            data = {}
        data[market_name] = meta
        self._save(self._meta_file, data)

    # ------------------------------------------------------------- 鉴权状态

    def get_auth_state(self) -> dict[str, Any]:
        state = self._load(self._auth_file, {})
        return state if isinstance(state, dict) else {}

    def set_auth_state(self, token: str, refresh_token: str) -> None:
        """只保存令牌，绝不保存账号密码。"""
        if not token and not refresh_token:
            return
        state = self.get_auth_state()
        if token:
            state["token"] = token
            state["token_updated_at"] = time.time()
        if refresh_token:
            state["refresh_token"] = refresh_token
            state["refresh_updated_at"] = time.time()
        self._save(self._auth_file, state)

    def stats(self) -> dict[str, int]:
        history = self._load(self._history_file, {})
        return {
            "monitored_items": len(self._load(self._quotes_file, {}) or {}),
            "history_points": sum(len(v) for v in (history or {}).values() if isinstance(v, list)),
            "subscriptions": len(self.list_subscriptions()),
        }
