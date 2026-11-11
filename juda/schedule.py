"""Zamanlanmış komutlar: "saat 22:30'da ışıkları kapat", "10 dakika sonra ...",
"her gün 07:00'de ...".

Zaman ifadesi komuttan ayrılır; geri kalan metin normal bir komut olarak
saklanır ve vakti gelince *o anki* yetkilerle çalıştırılır. Kullanıcı bu arada
silinmiş ya da rolü değişmişse yeni durum geçerlidir.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass

MAX_JOBS = 100
MAX_DELAY_MINUTES = 7 * 24 * 60

_SUFFIX = r"(?:\s*'?\s*(?:de|da|te|ta|'de|'da|'te|'ta))?"
_CLOCK = re.compile(r"(?:\bsaat\s*)?\b([01]?\d|2[0-3])[:.]([0-5]\d)\b" + _SUFFIX, re.I)
_HOUR_ONLY = re.compile(r"\bsaat\s*([01]?\d|2[0-3])\b" + _SUFFIX, re.I)
_RELATIVE = re.compile(r"\b(\d{1,4})\s*(dakika|dk|saat)\s*sonra\b", re.I)
_DAILY = re.compile(r"\bher\s*g[uü]n\b", re.I)

LIST_WORDS = re.compile(r"^\s*(zamanlamalar\w*|zamanlanm\w*\s*(komut|i[sş])\w*)(\s*(g[oö]ster|listele)\w*)?\s*[?.!]*\s*$", re.I)
CANCEL_ONE = re.compile(r"zamanlama\w*\s*#?(\d+)\w*\s*(iptal|sil)|#?(\d+)\s*numaral\w*\s*zamanlama\w*\s*(iptal|sil)", re.I)
CANCEL_ALL = re.compile(r"(t[uü]m|b[uü]t[uü]n)\s*zamanlama\w*\s*(iptal|sil)", re.I)


@dataclass
class TimeSpec:
    at: float            # ilk çalışma zamanı (unix saniye)
    daily: bool = False  # her gün aynı saatte tekrar
    label: str = ""      # kullanıcıya gösterilecek "22:30" / "10 dakika sonra"


def split_time(text: str, now: float) -> tuple[TimeSpec | None, str]:
    """Zaman ifadesini bul ve çıkar. (spec, kalan komut) döner; zaman yoksa (None, text)."""
    daily = bool(_DAILY.search(text))
    rest = _DAILY.sub(" ", text)

    m = _RELATIVE.search(rest)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        minutes = amount * 60 if unit == "saat" else amount
        if not 1 <= minutes <= MAX_DELAY_MINUTES:
            return None, text
        rest = (rest[: m.start()] + " " + rest[m.end():]).strip()
        return TimeSpec(at=now + minutes * 60, daily=False, label=f"{amount} {unit} sonra"), _tidy(rest)

    m = _CLOCK.search(rest) or _HOUR_ONLY.search(rest)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.re is _CLOCK else 0
        rest = (rest[: m.start()] + " " + rest[m.end():]).strip()
        return TimeSpec(at=next_occurrence(now, hour, minute), daily=daily,
                        label=("her gün " if daily else "") + f"{hour:02d}:{minute:02d}"), _tidy(rest)
    return None, text


def next_occurrence(now: float, hour: int, minute: int) -> float:
    """Bugün hh:mm geçmediyse bugün, geçtiyse yarın (yerel saat)."""
    current = dt.datetime.fromtimestamp(now)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += dt.timedelta(days=1)
    return target.timestamp()


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ,.;")


@dataclass
class Job:
    id: int
    user: str
    role: str
    text: str
    at: float
    daily: bool
    label: str

    def public(self) -> dict:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.at))
        return {**asdict(self), "next_run": when}


class Scheduler:
    """İş parçacığı güvenli, JSON dosyasına kaydeden zamanlayıcı."""

    def __init__(self, path: str | None = None, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._next_id = 1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # --- kalıcılık --------------------------------------------------------
    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("jobs", []) if isinstance(raw, dict) else []:
            try:
                job = Job(int(item["id"]), str(item["user"]), str(item["role"]), str(item["text"])[:500],
                          float(item["at"]), bool(item.get("daily", False)), str(item.get("label", ""))[:40])
            except (KeyError, TypeError, ValueError):
                continue
            self._jobs[job.id] = job
        self._next_id = max(self._jobs, default=0) + 1

    def _save(self) -> None:
        if not self._path:
            return
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".schedules-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"jobs": [asdict(j) for j in self._jobs.values()]}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # --- işlemler ---------------------------------------------------------
    def add(self, user: str, role: str, text: str, spec: TimeSpec) -> Job:
        with self._lock:
            if len(self._jobs) >= MAX_JOBS:
                raise ValueError(f"En fazla {MAX_JOBS} zamanlama olabilir.")
            job = Job(self._next_id, user, role, text, spec.at, spec.daily, spec.label)
            self._jobs[job.id] = job
            self._next_id += 1
            self._save()
            return job

    def list(self, user: str | None = None) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: (j.at, j.id))
        return [j for j in jobs if user is None or j.user == user]

    def cancel(self, job_id: int, user: str, is_admin: bool) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or (not is_admin and job.user != user):
                return False
            del self._jobs[job_id]
            self._save()
            return True

    def cancel_all(self, user: str, is_admin: bool) -> int:
        with self._lock:
            ids = [i for i, j in self._jobs.items() if is_admin or j.user == user]
            for i in ids:
                del self._jobs[i]
            if ids:
                self._save()
            return len(ids)

    def pop_due(self) -> list[Job]:
        """Vakti gelen işleri döndürür; tekrarlayanları bir gün ileri alır."""
        now = self._clock()
        due: list[Job] = []
        with self._lock:
            for job in list(self._jobs.values()):
                if job.at > now:
                    continue
                due.append(Job(**asdict(job)))
                if job.daily:
                    while job.at <= now:  # uzun bir kesintiden sonra kaçırılanları tekrarlama
                        job.at += 24 * 3600
                else:
                    del self._jobs[job.id]
            if due:
                self._save()
        return due

    # --- arka plan iş parçacığı ------------------------------------------
    def start(self, run, interval: float = 1.0) -> None:
        def loop():
            while not self._stop.wait(interval):
                for job in self.pop_due():
                    try:
                        run(job)
                    except Exception:  # bir iş hatası zamanlayıcıyı durdurmamalı
                        logging.getLogger("juda.schedule").exception("zamanlama #%s çalıştırılamadı", job.id)

        self._thread = threading.Thread(target=loop, name="juda-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
