"""JUDA çekirdeği: komutu al, anla, yetkiyi kontrol et, cihazlara uygula."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import nlu
from .devices import Device, Home, describe_state
from .router import DaedalusClient
from .users import User, can_control, can_handoff

HELP_TEXT = (
    "Şunları yapabilirim: ışıkları açıp kapatmak (\"salon ışığını aç\"), parlaklık ayarlamak "
    "(\"yatak odası ışığını %40 yap\"), klima ayarlamak (\"klimayı 23 derece yap\"), kapıyı "
    "kilitlemek (\"kapıyı kilitle\"), durum sormak (\"kapı kilitli mi?\") ve sahneler: "
    "\"eve geldim\", \"çıkıyorum\", \"iyi geceler\". Ağır işleri Daedalus'a iletirim."
)

SCENE_STEPS = {
    "arrive": [
        ("light", "on", None, ["salon", "antre"]),
    ],
    "leave": [
        ("light", "off", None, None),
        ("plug", "off", None, None),
        ("thermostat", "off", None, None),
        ("lock", "lock", None, None),
    ],
    "night": [
        ("light", "off", None, None),
        ("thermostat", "set_target", 19, None),
        ("lock", "lock", None, None),
    ],
}

SCENE_TITLES = {"arrive": "Hoş geldin sahnesi", "leave": "Çıkış sahnesi", "night": "Gece sahnesi"}


@dataclass
class Reply:
    text: str
    ok: bool = True
    handoff_id: str | None = None
    changed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"text": self.text, "ok": self.ok, "handoff_id": self.handoff_id, "changed": self.changed}


class Assistant:
    def __init__(self, home: Home, daedalus: DaedalusClient, log_path: str | None = None):
        self.home = home
        self.daedalus = daedalus
        self._log_path = log_path
        self._log: deque[dict] = deque(maxlen=200)
        self._log_lock = threading.Lock()

    # --- genel giriş noktası --------------------------------------------
    def handle(self, text: str, user: User) -> Reply:
        text = (text or "").strip()
        if not text:
            return self._finish(text, user, Reply("Bir şey söylemedin.", ok=False))
        if len(text) > 500:
            return self._finish(text, user, Reply("Komut çok uzun; 500 karakterin altında tut.", ok=False))

        intent = nlu.parse(text)
        if intent.kind == "help":
            reply = Reply(HELP_TEXT)
        elif intent.kind == "status":
            reply = self._status(intent)
        elif intent.kind == "scene":
            reply = self._scene(intent, user)
        elif intent.kind == "device":
            reply = self._device(intent, user)
        elif intent.kind == "handoff":
            reply = self._handoff(text, user)
        elif intent.kind == "clarify":
            reply = Reply("Neyi kastettiğini anlayamadım. Örneğin \"salon ışığını aç\" diyebilirsin.", ok=False)
        else:
            reply = Reply("Bunu anlayamadım.", ok=False)
        return self._finish(text, user, reply)

    def control(self, device_id: str, action: str, value, user: User) -> Reply:
        """Arayüzdeki düğmeler için doğrudan cihaz kontrolü."""
        dev = self.home.get(device_id)
        if dev is None:
            return self._finish(f"[ui] {device_id}", user, Reply("Böyle bir cihaz yok.", ok=False))
        if not can_control(user.role, dev.kind, action):
            return self._finish(f"[ui] {device_id} {action}", user,
                                Reply(f"{dev.name} için yetkin yok.", ok=False))
        ok, msg = self.home.apply(device_id, action, value)
        return self._finish(f"[ui] {device_id} {action}", user,
                            Reply(msg, ok=ok, changed=[device_id] if ok else []))

    # --- niyet işleyicileri ---------------------------------------------
    def _status(self, intent: nlu.Intent) -> Reply:
        devices = self.home.list_devices(kind=intent.device_kind, rooms=intent.rooms or None)
        if not devices:
            return Reply("Bu isteğe uyan bir cihaz bulamadım.", ok=False)
        if not intent.device_kind and not intent.rooms:
            devices = self.home.list_devices()
        return Reply(" ".join(describe_state(d) for d in devices))

    def _select(self, intent: nlu.Intent) -> tuple[list[Device], str | None]:
        devices = self.home.list_devices(kind=intent.device_kind, rooms=intent.rooms or None)
        if not devices:
            return [], "Bu isteğe uyan bir cihaz bulamadım."
        if (
            intent.device_kind in ("light", "plug")
            and not intent.rooms
            and not intent.all_devices
            and len(devices) > 1
        ):
            return [], "Hangi odadakini? Oda adını söyle ya da \"tüm ışıkları kapat\" de."
        return devices, None

    def _device(self, intent: nlu.Intent, user: User) -> Reply:
        devices, problem = self._select(intent)
        if problem:
            return Reply(problem, ok=False)
        return self._run_actions([(d, intent.action, intent.value) for d in devices], user)

    def _scene(self, intent: nlu.Intent, user: User) -> Reply:
        steps = []
        for kind, action, value, rooms in SCENE_STEPS[intent.scene]:
            for dev in self.home.list_devices(kind=kind, rooms=rooms):
                steps.append((dev, action, value))
        reply = self._run_actions(steps, user, summarize=True)
        title = SCENE_TITLES[intent.scene]
        reply.text = f"{title}: {reply.text}"
        return reply

    def _run_actions(self, steps, user: User, summarize: bool = False) -> Reply:
        done: list[str] = []
        messages: list[str] = []
        denied: list[str] = []
        failed: list[str] = []
        for dev, action, value in steps:
            if not can_control(user.role, dev.kind, action):
                denied.append(dev.name)
                continue
            ok, msg = self.home.apply(dev.id, action, value)
            if ok:
                done.append(dev.id)
                messages.append(msg)
            else:
                failed.append(msg)

        parts: list[str] = []
        if messages:
            parts.append(f"{len(messages)} cihaz güncellendi." if summarize or len(messages) > 3 else " ".join(messages))
        if denied:
            parts.append("Yetkin olmadığı için yapılamadı: " + ", ".join(denied) + ".")
        if failed:
            parts.append(" ".join(failed))
        if not parts:
            parts.append("Yapılacak bir şey bulamadım.")
        return Reply(" ".join(parts), ok=bool(done) and not failed, changed=done)

    def _handoff(self, text: str, user: User) -> Reply:
        if not can_handoff(user.role):
            return Reply("Bunu anlayamadım. Daedalus'a iş iletmek sadece yöneticiye açık.", ok=False)
        task_id = self.daedalus.submit(text, user.name)
        return Reply(f"Bu işi Daedalus'a ilettim (görev {task_id}).", handoff_id=task_id)

    # --- kayıt -----------------------------------------------------------
    def _finish(self, text: str, user: User, reply: Reply) -> Reply:
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "user": user.name,
            "role": user.role,
            "input": text,
            "reply": reply.text,
            "ok": reply.ok,
        }
        with self._log_lock:
            self._log.append(entry)
            if self._log_path:
                os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
                with open(self._log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return reply

    def recent_log(self, limit: int = 50) -> list[dict]:
        with self._log_lock:
            return list(self._log)[-limit:]
