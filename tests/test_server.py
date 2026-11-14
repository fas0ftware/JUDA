import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from juda.server import build_assistant, make_handler


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        assistant, users = build_assistant(cls.tmp.name)
        cls.assistant = assistant
        tokens = users.tokens()
        cls.admin = tokens["yonetici"][1]
        cls.family = tokens["aile"][1]
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(assistant, users))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, token=None, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", str(len(data)))
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def test_index_page_is_public(self):
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"JUDA", body)

    def test_api_requires_token(self):
        self.assertEqual(self.call("GET", "/api/devices")[0], 401)
        self.assertEqual(self.call("GET", "/api/devices", token="yanlis")[0], 401)
        self.assertEqual(self.call("POST", "/api/command", body={"text": "durum"})[0], 401)

    def test_devices_and_me(self):
        status, body = self.call("GET", "/api/devices", token=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["devices"]), 8)
        status, body = self.call("GET", "/api/me", token=self.family)
        self.assertEqual(json.loads(body)["role"], "family")

    def test_command_roundtrip(self):
        status, body = self.call("POST", "/api/command", token=self.admin, body={"text": "mutfak ışığını aç"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertTrue(self.assistant.home.get("kitchen_light").state["on"])

    def test_family_cannot_unlock_over_api(self):
        status, body = self.call("POST", "/api/device", token=self.family,
                                 body={"id": "front_door", "action": "unlock"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["ok"])
        self.assertTrue(self.assistant.home.get("front_door").state["locked"])

    def test_log_is_admin_only(self):
        self.assertEqual(self.call("GET", "/api/log", token=self.family)[0], 403)
        self.assertEqual(self.call("GET", "/api/log", token=self.admin)[0], 200)
        self.assertEqual(self.call("GET", "/api/handoffs", token=self.family)[0], 403)

    def test_bad_bodies(self):
        self.assertEqual(self.call("POST", "/api/command", token=self.admin, raw=b"{bozuk")[0], 400)
        self.assertEqual(self.call("POST", "/api/command", token=self.admin, body={"text": 5})[0], 400)
        self.assertEqual(self.call("POST", "/api/command", token=self.admin, body=["x"])[0], 400)
        self.assertEqual(self.call("POST", "/api/device", token=self.admin, body={"id": "x"})[0], 400)

    def test_oversized_body_rejected(self):
        big = json.dumps({"text": "a" * 70000}).encode()
        self.assertEqual(self.call("POST", "/api/command", token=self.admin, raw=big)[0], 400)

    def test_unknown_path(self):
        self.assertEqual(self.call("GET", "/api/yok", token=self.admin)[0], 404)

    def test_non_ascii_token_gets_401_not_a_dropped_connection(self):
        # Regression: hmac.compare_digest raised TypeError for non-ASCII str tokens,
        # which killed the handler thread and closed the connection without a reply.
        host, port = self.server.server_address
        request = ("GET /api/me HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer é-token\r\n"
                   "Connection: close\r\n\r\n").encode("latin-1")
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(request)
            reply = b""
            while chunk := sock.recv(4096):
                reply += chunk
        self.assertTrue(reply.startswith(b"HTTP/1.0 401") or reply.startswith(b"HTTP/1.1 401"), reply[:40])

    def test_security_headers(self):
        for path in ("/", "/api/me"):
            with self.subTest(path=path):
                req = urllib.request.Request(self.base + path)
                req.add_header("Authorization", f"Bearer {self.admin}")
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
                    self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
        with urllib.request.urlopen(self.base + "/") as resp:
            self.assertIn("frame-ancestors 'none'", resp.headers["Content-Security-Policy"])


    def test_server_header_hides_python_version(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req) as resp:
            self.assertTrue(resp.headers["Server"].startswith("JUDA/"))
            self.assertNotIn("Python", resp.headers["Server"])

    def test_schedule_api(self):
        status, body = self.call("POST", "/api/command", self.family, {"text": "30 dakika sonra mutfağı kapat"})
        self.assertEqual(status, 200)
        self.assertIn("zamanlama #", json.loads(body)["text"])
        status, body = self.call("GET", "/api/schedules", self.family)
        jobs = json.loads(body)["schedules"]
        self.assertEqual(status, 200)
        self.assertEqual(jobs[-1]["text"], "mutfağı kapat")
        job_id = jobs[-1]["id"]
        self.assertEqual(self.call("DELETE", f"/api/schedules/{job_id}")[0], 401)
        self.assertEqual(self.call("DELETE", "/api/schedules/abc", self.family)[0], 400)
        self.assertEqual(self.call("DELETE", f"/api/schedules/{job_id}", self.family)[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/schedules/{job_id}", self.family)[0], 404)

if __name__ == "__main__":
    unittest.main()


class SilentClientsAreDropped(unittest.TestCase):
    """A client that connects and says nothing must not hold a server thread forever."""

    def test_idle_connection_is_closed_after_the_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            assistant, users = build_assistant(tmp)
            handler = make_handler(assistant, users)
            handler.timeout = 0.5
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with socket.create_connection(server.server_address) as idle:
                    idle.settimeout(5)
                    self.assertEqual(idle.recv(1), b"")  # closed by the server, not by our 5 s limit
            finally:
                server.shutdown()
                server.server_close()

    def test_default_timeout_is_set(self):
        from juda.server import REQUEST_TIMEOUT
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(make_handler(*build_assistant(tmp)).timeout, REQUEST_TIMEOUT)
            self.assertGreater(REQUEST_TIMEOUT, 0)

