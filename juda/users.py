"""Kullanıcılar, roller ve yetkiler.

Yönetici (admin) her şeyi yapabilir. Aile üyeleri günlük cihaz kontrolünü
yapabilir ama kapı kilidini açamaz ve Daedalus'a iş gönderemez. Misafir
yalnızca durum sorabilir.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass

ROLES = ("admin", "family", "guest")
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")


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
        # Bayt olarak karşılaştır: hmac.compare_digest, ASCII dışı karakter içeren
        # str değerlerde TypeError verir; başlıktaki "é" gibi bir token sunucuyu
        # çökertmemeli, sadece 401 almalı.
        candidate = token.encode("utf-8", "surrogateescape")
        found: User | None = None
        for name, info in self._users.items():
            # Tüm kayıtlar taranır; zamanlama farkı token'ı sızdırmaz.
            if hmac.compare_digest(info["token"].encode("utf-8"), candidate):
                found = User(name=name, role=info["role"])
        return found

    def get(self, name: str) -> User | None:
        info = self._users.get(name)
        return User(name=name, role=info["role"]) if info else None

    def add(self, name: str, role: str) -> str:
        """Yeni kullanıcı ekler ve token'ını döndürür."""
        if not NAME_RE.match(name or ""):
            raise ValueError("Kullanıcı adı 1-32 karakter: harf, rakam, '_' veya '-'.")
        if role not in ROLES:
            raise ValueError(f"Rol şunlardan biri olmalı: {', '.join(ROLES)}.")
        if name in self._users:
            raise ValueError(f"'{name}' zaten var.")
        token = secrets.token_urlsafe(24)
        self._users[name] = {"role": role, "token": token}
        self._save()
        return token

    def remove(self, name: str) -> None:
        if name not in self._users:
            raise ValueError(f"'{name}' diye bir kullanıcı yok.")
        remaining_admins = [n for n, i in self._users.items() if i["role"] == "admin" and n != name]
        if self._users[name]["role"] == "admin" and not remaining_admins:
            raise ValueError("Son yöneticiyi silemezsin.")
        del self._users[name]
        self._save()

    def rotate(self, name: str) -> str:
        """Token'ı yeniler (eski token hemen geçersiz olur)."""
        if name not in self._users:
            raise ValueError(f"'{name}' diye bir kullanıcı yok.")
        token = secrets.token_urlsafe(24)
        self._users[name]["token"] = token
        self._save()
        return token

    def roles(self) -> dict[str, str]:
        return {name: info["role"] for name, info in self._users.items()}

    def tokens(self) -> dict[str, tuple[str, str]]:
        return {name: (info["role"], info["token"]) for name, info in self._users.items()}
