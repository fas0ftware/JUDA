"""JUDA web sunucusu (yalnızca standart kütüphane).

Telefon tarayıcısından açılabilen basit bir arayüz ve JSON API sunar.
Varsayılan olarak sadece 127.0.0.1'e bağlanır; aynı ağdaki telefondan
erişmek için `--host 0.0.0.0` verilmelidir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .core import Assistant
from .devices import Home
from .router import DaedalusStub
from .schedule import Scheduler
from .users import ROLES, User, UserStore, can_view_log

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024

# Her yanıta eklenir: tarayıcının içeriği tahmin etmesini, sayfanın çerçeveye
# alınmasını ve adresin başka sitelere sızmasını engeller.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# Arayüz tek dosya (satır içi script ve stil) ve yalnızca aynı sunucuya istek atar.
PAGE_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def build_assistant(data_dir: str) -> tuple[Assistant, UserStore]:
    os.makedirs(data_dir, exist_ok=True)
    home = Home(state_path=os.path.join(data_dir, "state.json"))
    daedalus = DaedalusStub(path=os.path.join(data_dir, "handoffs.jsonl"))
    users = UserStore(os.path.join(data_dir, "users.json"))
    scheduler = Scheduler(os.path.join(data_dir, "schedules.json"))
    assistant = Assistant(home, daedalus, log_path=os.path.join(data_dir, "events.jsonl"),
                          scheduler=scheduler, user_lookup=users.get)
    return assistant, users


def make_handler(assistant: Assistant, users: UserStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"JUDA/{__version__}"
        sys_version = ""  # do not advertise the Python version in the Server header

        def version_string(self):
            return self.server_version

        # --- yardımcılar -------------------------------------------------
        def _send_security_headers(self) -> None:
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)

        def _send_json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _user(self) -> User | None:
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                return users.authenticate(header[7:].strip())
            return None

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY:
                return None
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        def log_message(self, fmt, *args):  # sessiz; olaylar events.jsonl'e yazılıyor
            return

        # --- yönlendirme -------------------------------------------------
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", PAGE_CSP)
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(body)
                return

            user = self._user()
            if user is None:
                self._send_json(401, {"error": "Geçersiz veya eksik token."})
                return

            if self.path == "/api/me":
                self._send_json(200, {"name": user.name, "role": user.role})
            elif self.path == "/api/devices":
                self._send_json(200, {"devices": assistant.home.snapshot()})
            elif self.path == "/api/log":
                if not can_view_log(user.role):
                    self._send_json(403, {"error": "Kayıtları sadece yönetici görebilir."})
                else:
                    self._send_json(200, {"log": assistant.recent_log()})
            elif self.path == "/api/schedules":
                if assistant.scheduler is None:
                    self._send_json(200, {"schedules": []})
                else:
                    mine = None if user.role == "admin" else user.name
                    self._send_json(200, {"schedules": [j.public() for j in assistant.scheduler.list(mine)]})
            elif self.path == "/api/handoffs":
                if not can_view_log(user.role):
                    self._send_json(403, {"error": "Sadece yönetici görebilir."})
                else:
                    self._send_json(200, {"tasks": assistant.daedalus.list_tasks()})
            else:
                self._send_json(404, {"error": "Bulunamadı."})

        def do_POST(self):
            user = self._user()
            if user is None:
                self._send_json(401, {"error": "Geçersiz veya eksik token."})
                return
            data = self._read_json()
            if data is None:
                self._send_json(400, {"error": "Geçersiz istek gövdesi."})
                return

            if self.path == "/api/command":
                text = data.get("text")
                if not isinstance(text, str):
                    self._send_json(400, {"error": "'text' alanı gerekli."})
                    return
                self._send_json(200, assistant.handle(text, user).to_dict())
            elif self.path == "/api/device":
                device_id = data.get("id")
                action = data.get("action")
                if not isinstance(device_id, str) or not isinstance(action, str):
                    self._send_json(400, {"error": "'id' ve 'action' alanları gerekli."})
                    return
                reply = assistant.control(device_id, action, data.get("value"), user)
                self._send_json(200, reply.to_dict())
            else:
                self._send_json(404, {"error": "Bulunamadı."})

        def do_DELETE(self):
            user = self._user()
            if user is None:
                self._send_json(401, {"error": "Geçersiz veya eksik token."})
                return
            prefix = "/api/schedules/"
            if not self.path.startswith(prefix) or assistant.scheduler is None:
                self._send_json(404, {"error": "Bulunamadı."})
                return
            raw = self.path[len(prefix):]
            if not raw.isdigit() or len(raw) > 9:
                self._send_json(400, {"error": "Geçersiz zamanlama numarası."})
                return
            if assistant.scheduler.cancel(int(raw), user.name, user.role == "admin"):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"error": "Zamanlama bulunamadı."})

    return Handler


def users_command(argv: list[str]) -> int:
    """python -m juda users list | add NAME ROLE | remove NAME | rotate NAME"""
    parser = argparse.ArgumentParser(prog="python -m juda users", description="Kullanıcı yönetimi")
    parser.add_argument("--data-dir", default=os.environ.get("JUDA_DATA_DIR", "data"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="kullanıcıları ve rollerini listele (token göstermez)")
    add = sub.add_parser("add", help="yeni kullanıcı ekle ve token'ını göster")
    add.add_argument("name")
    add.add_argument("role", choices=ROLES)
    rm = sub.add_parser("remove", help="kullanıcıyı sil")
    rm.add_argument("name")
    rot = sub.add_parser("rotate", help="kullanıcının token'ını yenile")
    rot.add_argument("name")
    args = parser.parse_args(argv)

    store = UserStore(os.path.join(args.data_dir, "users.json"))
    try:
        if args.cmd == "list":
            for name, role in store.roles().items():
                print(f"{name:<20} {role}")
        elif args.cmd == "add":
            print(f"{args.name} ({args.role}) token: {store.add(args.name, args.role)}")
        elif args.cmd == "remove":
            store.remove(args.name)
            print(f"{args.name} silindi.")
        elif args.cmd == "rotate":
            print(f"{args.name} yeni token: {store.rotate(args.name)}")
    except ValueError as exc:
        print(f"Hata: {exc}")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["users"]:
        return users_command(argv[1:])
    parser = argparse.ArgumentParser(description="JUDA prototip sunucusu (kullanıcılar için: users --help)")
    parser.add_argument("--host", default=os.environ.get("JUDA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JUDA_PORT", "8765")))
    parser.add_argument("--data-dir", default=os.environ.get("JUDA_DATA_DIR", "data"))
    args = parser.parse_args(argv)

    assistant, users = build_assistant(args.data_dir)
    if users.created_defaults:
        # Tokens are shown once, on first run only (afterwards: `users rotate NAME`).
        print("İlk çalıştırma: kullanıcılar oluşturuldu. Tokenlar yalnızca şimdi gösteriliyor:")
        for name, (role, token) in users.tokens().items():
            print(f"  {name} ({role}): {token}")
    else:
        print("Kullanıcılar: " + ", ".join(f"{n} ({r})" for n, r in users.roles().items())
              + ". Yeni token için: python -m juda users rotate <ad>")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"UYARI: {args.host} adresinde dinleniyor; trafik şifresiz (HTTP). Sadece güvendiğin ağda kullan.")

    assistant.scheduler.start(assistant.run_job)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(assistant, users))
    print(f"JUDA {__version__} http://{args.host}:{args.port} adresinde çalışıyor. Durdurmak için Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKapatılıyor.")
    finally:
        assistant.scheduler.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
