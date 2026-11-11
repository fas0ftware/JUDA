"""JUDA çekirdeği: komutu al, anla, yetkiyi kontrol et, cihazlara uygula."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import nlu
from . import schedule as sched
from .devices import Device, Home, describe_state
from .router import DaedalusClient
from .users import User, can_control, can_handoff

HELP_TEXT = (
    "Şunları yapabilirim: ışıkları açıp kapatmak (\"salon ışığını aç\"), parlaklık ayarlamak "
    "(\"yatak odası ışığını %40 yap\"), klima ayarlamak (\"klimayı 23 derece yap\"), kapıyı "
    "kilitlemek (\"kapıyı kilitle\"), durum sormak (\"kapı kilitli mi?\") ve sahneler: "
    "\"eve geldim\", \"çıkıyorum\", \"iyi geceler\". Zamanlama: \"saat 22:30'da ışıkları kapat\", "
    "\"10 dakika sonra mutfağı kapat\", \"her gün 07:00'de salon ışığını aç\"; \"zamanlamalar\" ile listele, "
    "\"zamanlama 2 iptal\" ile sil. Ağır işleri Daedalus'a iletirim."
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
    def __init__(self, home: Home, daedalus: DaedalusClient, log_path: str | None = None,
                 scheduler: sched.Scheduler | None = None, user_lookup=None, clock=time.time):
        self.home = home
        self.daedalus = daedalus
        self.scheduler = scheduler
        self._user_lookup = user_lookup  # name -> User | None, used when a scheduled job runs
        self._clock = clock
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

        if self.scheduler is not None:
            scheduled = self._schedule_command(text, user)
            if scheduled is not None:
                return self._finish(text, user, scheduled)

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

    # --- zamanlama --------------------------------------------------------
    def _schedule_command(self, text: str, user: User) -> Reply | None:
        """Zamanlama listele / iptal / oluştur. Zaman ifadesi yoksa None."""
        is_admin = user.role == "admin"
        if sched.LIST_WORDS.match(text):
            jobs = self.scheduler.list(None if is_admin else user.name)
            if not jobs:
                return Reply("Bekleyen zamanlama yok.")
            lines = [f"#{j.id} {j.label} → {j.text} ({j.user})" for j in jobs]
            return Reply("Zamanlamalar: " + "; ".join(lines))
        if sched.CANCEL_ALL.search(text):
            n = self.scheduler.cancel_all(user.name, is_admin)
            return Reply(f"{n} zamanlama iptal edildi." if n else "İptal edilecek zamanlama yok.", ok=bool(n))
        m = sched.CANCEL_ONE.search(text)
        if m:
            job_id = int(m.group(1) or m.group(3))
            if self.scheduler.cancel(job_id, user.name, is_admin):
                return Reply(f"Zamanlama #{job_id} iptal edildi.")
            return Reply(f"#{job_id} numaralı (senin) bir zamanlama bulamadım.", ok=False)

        spec, rest = sched.split_time(text, self._clock())
        if spec is None:
            return None
        if not rest:
            return Reply("Zamanı anladım ama ne yapacağımı söylemedin.", ok=False)
        intent = nlu.parse(rest)
        if intent.kind not in ("device", "scene"):
            return Reply("Sadece cihaz komutları ve sahneler zamanlanabilir, örneğin "
                         "\"saat 23:00'te ışıkları kapat\".", ok=False)
        if intent.kind == "device":
            devices, problem = self._select(intent)
            if problem:
                return Reply(problem, ok=False)
            denied = [d.name for d in devices if not can_control(user.role, d.kind, intent.action)]
            if denied:
                return Reply("Bunu zamanlayamazsın, yetkin yok: " + ", ".join(denied) + ".", ok=False)
        elif user.role == "guest":
            return Reply("Misafirler sahne zamanlayamaz.", ok=False)
        try:
            job = self.scheduler.add(user.name, user.role, rest, spec)
        except ValueError as exc:
            return Reply(str(exc), ok=False)
        return Reply(f"Tamam, {spec.label}: \"{rest}\" (zamanlama #{job.id}).")

    def run_job(self, job: sched.Job) -> Reply | None:
        """Zamanı gelen işi, kullanıcının *şu anki* rolüyle çalıştırır."""
        user = self._user_lookup(job.user) if self._user_lookup else User(job.user, job.role)
        if user is None:
            self._finish(f"[zamanlama #{job.id}] {job.text}", User(job.user, "removed"),
                         Reply("Kullanıcı artık yok, zamanlama çalıştırılmadı.", ok=False))
            return None
        intent = nlu.parse(job.text)
        if intent.kind == "scene":
            reply = self._scene(intent, user)
        elif intent.kind == "device":
            reply = self._device(intent, user)
        else:
            reply = Reply("Zamanlanmış komut artık anlaşılmıyor.", ok=False)
        return self._finish(f"[zamanlama #{job.id}] {job.text}", user, reply)

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
