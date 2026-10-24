"""账户设置存储。

登录凭据与拥有者 UID 从 AstrBot 插件配置中迁出，单独保存到插件数据目录。
API 与日志只暴露脱敏状态，不返回密码。
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

ACCOUNT_FILE = "account.json"


def normalize_uids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace("\n", ",").split(",")
    elif isinstance(value, list | tuple | set):
        parts = list(value)
    else:
        return []
    result: list[str] = []
    for item in parts:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


@dataclass(slots=True)
class AccountSettings:
    email: str = ""
    password: str = ""
    owner_uids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": str(self.email or ""),
            "password": str(self.password or ""),
            "owner_uids": list(self.owner_uids),
        }

    @classmethod
    def from_dict(cls, data: Any) -> AccountSettings:
        if not isinstance(data, dict):
            return cls()
        return cls(
            email=str(data.get("email") or ""),
            password=str(data.get("password") or ""),
            owner_uids=normalize_uids(data.get("owner_uids")),
        )

    def sanitized(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "password_configured": bool(self.password),
            "owner_uids": list(self.owner_uids),
        }


class AccountStore:
    """账户设置的唯一权威来源。"""

    def __init__(self, storage, logger) -> None:
        self.storage = storage
        self.logger = logger
        self.persisted = False
        raw = storage.load_aux_json(ACCOUNT_FILE, None)
        if isinstance(raw, dict):
            self.settings = AccountSettings.from_dict(raw)
            self.persisted = True
        else:
            self.settings = AccountSettings()

    @property
    def email(self) -> str:
        return self.settings.email

    @property
    def password(self) -> str:
        return self.settings.password

    @property
    def owner_uids(self) -> list[str]:
        return list(self.settings.owner_uids)

    def migrate_legacy(self, raw_config: dict) -> bool:
        if self.persisted:
            return True
        self.settings = AccountSettings(
            email=str(raw_config.get("account_email") or ""),
            password=str(raw_config.get("account_password") or ""),
            owner_uids=normalize_uids(raw_config.get("owner_uids")),
        )
        return self.save(
            email=self.settings.email,
            password=self.settings.password,
            owner_uids=self.settings.owner_uids,
            _force=True,
        )

    def save(self, *, email: str, password: str | None, owner_uids: Any, _force: bool = False) -> bool:
        email = str(email or "").strip()
        next_password = self.settings.password if password in (None, "") and not _force else str(password or "")
        normalized_uids = normalize_uids(owner_uids)
        if len(email) > 320:
            raise ValueError("账号邮箱过长")
        if len(next_password) > 2048:
            raise ValueError("账号密码过长")
        if len(normalized_uids) > 32 or any(len(uid) > 128 for uid in normalized_uids):
            raise ValueError("拥有者 UID 数量或长度超出限制")
        next_settings = AccountSettings(
            email=email,
            password=next_password,
            owner_uids=normalized_uids,
        )
        ok = self.storage.save_aux_json(ACCOUNT_FILE, next_settings.to_dict())
        if not ok:
            return False
        self.settings = next_settings
        self.persisted = True
        self._restrict_permissions()
        return True

    def _restrict_permissions(self) -> None:
        path = self.storage.data_path(ACCOUNT_FILE)
        # Windows 等平台可能不支持 POSIX 权限；失败不影响持久化。
        with suppress(OSError):
            os.chmod(path, 0o600)

    def sanitized(self) -> dict[str, Any]:
        return self.settings.sanitized()
