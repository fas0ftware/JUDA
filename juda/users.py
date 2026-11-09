"""Kullanıcılar, roller ve yetkiler.

Yönetici (admin) her şeyi yapabilir. Aile üyeleri günlük cihaz kontrolünü
yapabilir ama kapı kilidini açamaz ve Daedalus'a iş gönderemez. Misafir
yalnızca durum sorabilir.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from dataclasses import dataclass

ROLES = ("admin", "family", "guest")


@dataclass(frozen=True)
class User:
    name: str
    role: str


def can_control(role: str, kind: str, action: str) -> bool:
    if role == "admin":
        return True
    if role == "family":
        return not (kind == "lock" and action == "unlock")
    return False


def can_handoff(role: str) -> bool:
    return role == "admin"


def can_view_log(role: str) -> bool:
    return role == "admin"


class UserStore:
    """Basit token tabanlı kullanıcı deposu (JSON dosyası)."""

    def __init__(self, path: str):
        self._path = path
        self._users: dict[str, dict[str, str]] = {}
        self.created_defaults = False
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._users = {
                    name: info for name, info in data.items()
                    if isinstance(info, dict) and info.get("role") in ROLES and info.get("token")
                }
        if not self._users:
            self._users = {
                "yonetici": {"role": "admin", "token": secrets.token_urlsafe(24)},
                "aile": {"role": "family", "token": secrets.token_urlsafe(24)},
            }
            self.created_defaults = True
            self._save()

    def _save(self) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._users, fh, ensure_ascii=False, indent=2)

    def authenticate(self, token: str | None) -> User | None:
        if not token:
            return None
        found: User | None = None
        for name, info in self._users.items():
            # Tüm kayıtlar taranır; zamanlama farkı token'ı sızdırmaz.
            if hmac.compare_digest(info["token"], token):
                found = User(name=name, role=info["role"])
        return found

    def tokens(self) -> dict[str, tuple[str, str]]:
        return {name: (info["role"], info["token"]) for name, info in self._users.items()}
