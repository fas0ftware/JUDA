"""Simüle edilmiş akıllı ev cihazları.

Gerçek cihaz entegrasyonu (MQTT, Zigbee, Tuya vb.) sonraya bırakıldı; bu modül
aynı arayüzü sunan bir simülasyon katmanıdır. Gerçek sürücüler `Home.apply`
içindeki davranışın yerine geçebilir.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any

KINDS = ("light", "thermostat", "lock", "plug")

MIN_TARGET = 10
MAX_TARGET = 30


@dataclass
class Device:
    id: str
    name: str
    room: str
    kind: str
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "room": self.room,
            "kind": self.kind,
            "state": dict(self.state),
        }


def default_devices() -> list[Device]:
    return [
        Device("living_room_light", "Salon ışığı", "salon", "light", {"on": False, "brightness": 100}),
        Device("bedroom_light", "Yatak odası ışığı", "yatak_odasi", "light", {"on": False, "brightness": 100}),
        Device("kitchen_light", "Mutfak ışığı", "mutfak", "light", {"on": False, "brightness": 100}),
        Device("bathroom_light", "Banyo ışığı", "banyo", "light", {"on": False, "brightness": 100}),
        Device("hall_light", "Antre ışığı", "antre", "light", {"on": False, "brightness": 100}),
        Device("living_room_ac", "Salon kliması", "salon", "thermostat", {"on": False, "target": 22, "current": 21}),
        Device("front_door", "Ana kapı", "antre", "lock", {"locked": True}),
        Device("tv_plug", "TV prizi", "salon", "plug", {"on": False}),
    ]


class Home:
    """Cihaz kaydı + durum makinesi. İş parçacığı güvenlidir."""

    def __init__(self, state_path: str | None = None, devices: list[Device] | None = None):
        self._lock = threading.RLock()
        self._state_path = state_path
        self._devices: dict[str, Device] = {}
        for dev in devices if devices is not None else default_devices():
            self._devices[dev.id] = dev
        self._load()

    # --- okuma -----------------------------------------------------------
    def list_devices(self, kind: str | None = None, rooms: list[str] | None = None) -> list[Device]:
        with self._lock:
            result = []
            for dev in self._devices.values():
                if kind and dev.kind != kind:
                    continue
                if rooms and dev.room not in rooms:
                    continue
                result.append(dev)
            return result

    def get(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [d.to_dict() for d in self._devices.values()]

    # --- yazma -----------------------------------------------------------
    def apply(self, device_id: str, action: str, value: Any = None) -> tuple[bool, str]:
        """Bir cihaza eylem uygular. (başarılı_mı, Türkçe mesaj) döner."""
        with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                return False, "Böyle bir cihaz bulamadım."
            handler = getattr(self, f"_apply_{dev.kind}", None)
            if handler is None:
                return False, f"{dev.name} için desteklenmeyen cihaz türü."
            ok, msg = handler(dev, action, value)
            if ok:
                self._save()
            return ok, msg

    def _apply_light(self, dev: Device, action: str, value: Any) -> tuple[bool, str]:
        if action == "on":
            dev.state["on"] = True
            return True, f"{dev.name} açıldı."
        if action == "off":
            dev.state["on"] = False
            return True, f"{dev.name} kapatıldı."
        if action == "set_brightness":
            level = _to_int(value)
            if level is None or not 0 <= level <= 100:
                return False, "Parlaklık 0 ile 100 arasında olmalı."
            dev.state["brightness"] = level
            dev.state["on"] = level > 0
            return True, f"{dev.name} parlaklığı %{level} yapıldı."
        return False, f"{dev.name} bu eylemi desteklemiyor."

    def _apply_thermostat(self, dev: Device, action: str, value: Any) -> tuple[bool, str]:
        if action == "on":
            dev.state["on"] = True
            return True, f"{dev.name} açıldı."
        if action == "off":
            dev.state["on"] = False
            return True, f"{dev.name} kapatıldı."
        if action in ("set_target", "adjust_target"):
            number = _to_int(value)
            if number is None:
                return False, "Sıcaklık değerini anlayamadım."
            target = number if action == "set_target" else dev.state.get("target", 22) + number
            if not MIN_TARGET <= target <= MAX_TARGET:
                return False, f"Sıcaklık {MIN_TARGET} ile {MAX_TARGET} derece arasında olmalı."
            dev.state["target"] = target
            dev.state["on"] = True
            return True, f"{dev.name} {target}°C'ye ayarlandı."
        return False, f"{dev.name} bu eylemi desteklemiyor."

    def _apply_lock(self, dev: Device, action: str, value: Any) -> tuple[bool, str]:
        if action == "lock":
            dev.state["locked"] = True
            return True, f"{dev.name} kilitlendi."
        if action == "unlock":
            dev.state["locked"] = False
            return True, f"{dev.name} kilidi açıldı."
        return False, f"{dev.name} bu eylemi desteklemiyor."

    def _apply_plug(self, dev: Device, action: str, value: Any) -> tuple[bool, str]:
        if action == "on":
            dev.state["on"] = True
            return True, f"{dev.name} açıldı."
        if action == "off":
            dev.state["on"] = False
            return True, f"{dev.name} kapatıldı."
        return False, f"{dev.name} bu eylemi desteklemiyor."

    # --- kalıcılık -------------------------------------------------------
    def _save(self) -> None:
        if not self._state_path:
            return
        data = {dev.id: dev.state for dev in self._devices.values()}
        directory = os.path.dirname(self._state_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _load(self) -> None:
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for dev_id, state in data.items():
            dev = self._devices.get(dev_id)
            if dev and isinstance(state, dict):
                dev.state.update(state)


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def describe_state(dev: Device) -> str:
    """Cihazın durumunu tek cümlelik Türkçe metne çevirir."""
    if dev.kind == "light":
        if dev.state.get("on"):
            return f"{dev.name} açık (%{dev.state.get('brightness', 100)})."
        return f"{dev.name} kapalı."
    if dev.kind == "thermostat":
        if dev.state.get("on"):
            return f"{dev.name} açık, hedef {dev.state.get('target')}°C, oda {dev.state.get('current')}°C."
        return f"{dev.name} kapalı, oda {dev.state.get('current')}°C."
    if dev.kind == "lock":
        return f"{dev.name} {'kilitli' if dev.state.get('locked') else 'açık'}."
    if dev.kind == "plug":
        return f"{dev.name} {'açık' if dev.state.get('on') else 'kapalı'}."
    return f"{dev.name}: durum bilinmiyor."
