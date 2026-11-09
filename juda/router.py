"""Zor işlerin Daedalus'a devredilmesi.

JUDA basit ev işlerini kendisi yapar; kod yazma, araştırma gibi ağır işleri
Daedalus'a bırakır. İki sistem bağlam/hafıza paylaşmaz: devir sırasında
yalnızca görev metni ve isteyen kullanıcının adı gönderilir.

Şimdilik Daedalus gerçek değil, `DaedalusStub` görevleri bir JSONL dosyasına
yazar. Gerçek entegrasyon geldiğinde aynı `submit` arayüzünü sağlayan başka
bir sınıf bunun yerine kullanılabilir.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Protocol


class DaedalusClient(Protocol):
    def submit(self, task: str, requested_by: str) -> str: ...

    def list_tasks(self) -> list[dict]: ...


class DaedalusStub:
    def __init__(self, path: str | None = None):
        self._path = path
        self._tasks: list[dict] = []
        self._lock = threading.Lock()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            self._tasks.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

    def submit(self, task: str, requested_by: str) -> str:
        entry = {
            "id": uuid.uuid4().hex[:8],
            "task": task,
            "requested_by": requested_by,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": "queued",
        }
        with self._lock:
            self._tasks.append(entry)
            if self._path:
                os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry["id"]

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return list(self._tasks)
