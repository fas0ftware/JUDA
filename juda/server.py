"""JUDA web sunucusu (yalnızca standart kütüphane).

Telefon tarayıcısından açılabilen basit bir arayüz ve JSON API sunar.
Varsayılan olarak sadece 127.0.0.1'e bağlanır; aynı ağdaki telefondan
erişmek için `--host 0.0.0.0` verilmelidir.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import Assistant
from .devices import Home
from .router import DaedalusStub
from .users import User, UserStore, can_view_log

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
    assistant = Assistant(home, daedalus, log_path=os.path.join(data_dir, "events.jsonl"))
    users = UserStore(os.path.join(data_dir, "users.json"))
    return assistant, users


def make_handler(assistant: Assistant, users: UserStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JUDA/0.1"

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

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="JUDA prototip sunucusu")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args(argv)

    assistant, users = build_assistant(args.data_dir)
    if users.created_defaults:
        print("İlk çalıştırma: kullanıcılar oluşturuldu. Tokenlar data/users.json içinde (dosyayı paylaşma).")
    for name, (role, token) in users.tokens().items():
        print(f"  {name} ({role}): {token}")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(assistant, users))
    print(f"JUDA http://{args.host}:{args.port} adresinde çalışıyor. Durdurmak için Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKapatılıyor.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
