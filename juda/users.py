"""Kullanıcılar, roller ve yetkiler.

Yönetici (admin) her şeyi yapabilir. Aile üyeleri günlük cihaz kontrolünü
yapabilir ama kapı kilidini açamaz ve Daedalus'a iş gönderemez. Misafir
yalnızca durum sorabilir.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass

LOG = logging.getLogger("juda.users")

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


def _valid_users(data) -> dict[str, dict[str, str]]:
    if not isinstance(data, dict):
        return {}
    return {
        name: info for name, info in data.items()
        if isinstance(info, dict) and info.get("role") in ROLES
        and isinstance(info.get("token"), str) and info["token"]
    }


class UserStore:
    """Basit token tabanlı kullanıcı deposu (JSON dosyası).

    Sunucu çalışırken `python -m juda users rotate/remove` dosyayı değiştirir;
    depo her sorguda dosyanın değişip değişmediğine bakar ve gerekirse yeniden
    okur. Böylece yenilenen ya da silinen bir kullanıcının eski token'ı hemen
    geçersiz olur ve zamanlanmış işleri eski yetkiyle çalışmaz.
    """

    def __init__(self, path: str):
        self._path = path
        self._users: dict[str, dict[str, str]] = {}
        self._stamp = None
        self._lock = threading.RLock()
        self.created_defaults = False
        self._load()

    def _file_stamp(self):
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{self._path} okunamadı ({exc}); dosyayı düzelt ya da sil") from None
            self._users = _valid_users(data)
        if not self._users:
            self._users = {
                "yonetici": {"role": "admin", "token": secrets.token_urlsafe(24)},
                "aile": {"role": "family", "token": secrets.token_urlsafe(24)},
            }
            self.created_defaults = True
            self._save()
        self._stamp = self._file_stamp()

    def _refresh(self) -> None:
        """Dosya başka bir süreç tarafından değiştirildiyse yeniden oku."""
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                users = _valid_users(json.load(fh))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Bozuk ya da silinmiş dosya: kimseyi içeri alma (eski tokenlara güvenme).
            LOG.error("%s okunamadı (%s); dosya düzelene kadar tüm girişler reddedilecek", self._path, exc)
            users = {}
        self._users = users
        self._stamp = stamp

    def _save(self) -> None:
        # Geçici dosyaya yaz, sonra tek adımda değiştir: yarım kalmış bir yazma
        # users.json'u asla bozmaz. mkstemp dosyayı 0600 izniyle oluşturur.
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".users-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._users, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._stamp = self._file_stamp()

    def authenticate(self, token: str | None) -> User | None:
        if not token:
            return None
        # Bayt olarak karşılaştır: hmac.compare_digest, ASCII dışı karakter içeren
        # str değerlerde TypeError verir; başlıktaki "é" gibi bir token sunucuyu
        # çökertmemeli, sadece 401 almalı.
        candidate = token.encode("utf-8", "surrogateescape")
        found: User | None = None
        with self._lock:
            self._refresh()
            users = dict(self._users)
        for name, info in users.items():
            # Tüm kayıtlar taranır; zamanlama farkı token'ı sızdırmaz.
            if hmac.compare_digest(info["token"].encode("utf-8"), candidate):
                found = User(name=name, role=info["role"])
        return found

    def get(self, name: str) -> User | None:
        with self._lock:
            self._refresh()
            info = self._users.get(name)
        return User(name=name, role=info["role"]) if info else None

    def add(self, name: str, role: str) -> str:
        """Yeni kullanıcı ekler ve token'ını döndürür."""
        if not NAME_RE.match(name or ""):
            raise ValueError("Kullanıcı adı 1-32 karakter: harf, rakam, '_' veya '-'.")
        if role not in ROLES:
            raise ValueError(f"Rol şunlardan biri olmalı: {', '.join(ROLES)}.")
        with self._lock:
            self._refresh()
            if name in self._users:
                raise ValueError(f"'{name}' zaten var.")
            token = secrets.token_urlsafe(24)
            self._users[name] = {"role": role, "token": token}
            self._save()
        return token

    def remove(self, name: str) -> None:
        with self._lock:
            self._refresh()
            if name not in self._users:
                raise ValueError(f"'{name}' diye bir kullanıcı yok.")
            remaining_admins = [n for n, i in self._users.items() if i["role"] == "admin" and n != name]
            if self._users[name]["role"] == "admin" and not remaining_admins:
                raise ValueError("Son yöneticiyi silemezsin.")
            del self._users[name]
            self._save()

    def rotate(self, name: str) -> str:
        """Token'ı yeniler (eski token, çalışan sunucuda da hemen geçersiz olur)."""
        with self._lock:
            self._refresh()
            if name not in self._users:
                raise ValueError(f"'{name}' diye bir kullanıcı yok.")
            token = secrets.token_urlsafe(24)
            self._users[name] = {**self._users[name], "token": token}
            self._save()
        return token

    def roles(self) -> dict[str, str]:
        with self._lock:
            self._refresh()
            return {name: info["role"] for name, info in self._users.items()}

    def tokens(self) -> dict[str, tuple[str, str]]:
        with self._lock:
            return {name: (info["role"], info["token"]) for name, info in self._users.items()}
