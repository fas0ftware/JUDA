"""Kural tabanlı Türkçe komut ayrıştırıcı.

Dil modeli kullanmaz: aynı girdi her zaman aynı niyeti üretir, bu yüzden
test edilebilir ve çevrimdışı çalışır. Anlaşılmayan veya zor işler
`Intent(kind="handoff")` olarak Daedalus'a yönlendirilir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TR_MAP = str.maketrans(
    {"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"}
)


def normalize(text: str) -> str:
    """Küçük harfe çevirir, Türkçe harfleri ASCII'ye indirger, noktalamayı siler."""
    text = text.replace("İ", "i").replace("I", "ı").lower()
    text = text.translate(_TR_MAP)
    text = re.sub(r"[^\w%\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


ROOM_PREFIXES = {
    "salon": ("salon",),
    "yatak_odasi": ("yatak", "yatag"),
    "mutfak": ("mutfak", "mutfag"),
    "banyo": ("banyo",),
    "antre": ("antre", "hol", "giris"),
}

KIND_PREFIXES = {
    "light": ("isik", "isig", "lamba", "aydinlat", "avize"),
    "thermostat": ("klima", "termostat", "isitic", "sicaklik", "sicaklig", "kalorifer"),
    "lock": ("kapi", "kilit", "kilid"),
    "plug": ("priz", "fis"),
}

ON_WORDS = {"ac", "acar", "acin", "acsana", "yak", "yakar", "calistir", "baslat"}
OFF_WORDS = {"kapat", "kapatir", "kapatin", "kapatsana", "sondur", "sondurur", "durdur"}
LOCK_WORDS = ("kilitle",)
ALL_WORDS = {"tum", "tumu", "butun", "hepsi", "hepsini", "her"}
QUESTION_PARTICLES = {"mi", "mu"}

SCENES = {
    "arrive": ("eve geldim", "geldim", "eve dondum"),
    "leave": ("cikiyorum", "evden cikiyorum", "gidiyorum", "ise gidiyorum"),
    "night": ("iyi geceler", "yatiyorum", "gece modu", "uyuyorum"),
}

HANDOFF_HINTS = (
    "kod yaz", "kodu", "proje", "arastir", "script", "rapor", "ozetle",
    "hazirla", "uygulama yap", "program yaz", "hata ayikla",
)

HELP_HINTS = ("yardim", "neler yapabilirsin", "ne yapabilirsin", "komutlar")


@dataclass
class Intent:
    kind: str  # device | scene | status | handoff | help | clarify | unknown
    action: str | None = None
    device_kind: str | None = None
    rooms: list[str] = field(default_factory=list)
    all_devices: bool = False
    value: int | None = None
    scene: str | None = None
    note: str | None = None
    raw: str = ""


def parse(text: str) -> Intent:
    norm = normalize(text)
    tokens = norm.split()
    if not tokens:
        return Intent(kind="unknown", raw=text)

    if any(h in norm for h in HELP_HINTS):
        return Intent(kind="help", raw=text)

    for scene, phrases in SCENES.items():
        if any(_contains_phrase(norm, p) for p in phrases):
            return Intent(kind="scene", scene=scene, raw=text)

    rooms = _find_rooms(tokens)
    device_kind = _find_kind(tokens)
    all_devices = any(t in ALL_WORDS for t in tokens)
    is_question = any(t in QUESTION_PARTICLES for t in tokens) or "durum" in tokens or "kac derece" in norm

    if is_question:
        if device_kind is None and "derece" in tokens:
            device_kind = "thermostat"
        return Intent(kind="status", device_kind=device_kind, rooms=rooms, raw=text)

    action, value = _find_action(norm, tokens, device_kind)
    if device_kind is None:
        device_kind = _infer_kind(action, value, tokens)

    if action is not None and device_kind is not None:
        action = _resolve_action_for_kind(action, device_kind)
        return Intent(
            kind="device", action=action, device_kind=device_kind, rooms=rooms,
            all_devices=all_devices, value=value, raw=text,
        )

    if action in ("on", "off") and (rooms or all_devices):
        return Intent(
            kind="device", action=action, device_kind="light", rooms=rooms,
            all_devices=all_devices, value=None, raw=text,
        )

    if any(h in norm for h in HANDOFF_HINTS):
        return Intent(kind="handoff", raw=text)

    if action in ("on", "off"):
        return Intent(kind="clarify", action=action, note="Neyi kastettiğini anlayamadım.", raw=text)

    return Intent(kind="handoff", note="unrecognized", raw=text)


def _contains_phrase(norm: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", norm) is not None


def _find_rooms(tokens: list[str]) -> list[str]:
    found: list[str] = []
    for token in tokens:
        for room, prefixes in ROOM_PREFIXES.items():
            if any(token.startswith(p) for p in prefixes) and room not in found:
                found.append(room)
    return found


def _find_kind(tokens: list[str]) -> str | None:
    for token in tokens:
        for kind, prefixes in KIND_PREFIXES.items():
            if any(token.startswith(p) for p in prefixes):
                return kind
    return None


def _find_action(norm: str, tokens: list[str], device_kind: str | None) -> tuple[str | None, int | None]:
    brightness = _find_brightness(norm)
    if brightness is not None:
        return "set_brightness", brightness

    if "los" in tokens or any(t.startswith("kis") and len(t) <= 5 for t in tokens):
        if device_kind in (None, "light"):
            return "set_brightness", 30

    temp = re.search(r"(\d{1,2})\s*dere", norm)
    if temp:
        return "set_target", int(temp.group(1))
    if device_kind == "thermostat":
        plain = re.search(r"(?<![\d%])(\d{2})(?![\d%])", norm)
        if plain:
            return "set_target", int(plain.group(1))

    for token in tokens:
        if token.startswith("isit") and not token.startswith("isitic"):
            return "adjust_target", 2
        if token.startswith("sogut"):
            return "adjust_target", -2

    if any(t.startswith(LOCK_WORDS) for t in tokens):
        return "lock", None
    if any(t in OFF_WORDS or t.startswith("sondur") for t in tokens):
        return "off", None
    if any(t in ON_WORDS for t in tokens):
        return "on", None
    return None, None


def _find_brightness(norm: str) -> int | None:
    patterns = (
        r"%\s*(\d{1,3})",
        r"(\d{1,3})\s*%",
        r"yuzde\s*(\d{1,3})",
        r"parlak\w*\s*(\d{1,3})",
    )
    for pattern in patterns:
        m = re.search(pattern, norm)
        if m:
            return int(m.group(1))
    return None


def _infer_kind(action: str | None, value: int | None, tokens: list[str]) -> str | None:
    if action in ("set_target", "adjust_target"):
        return "thermostat"
    if action == "set_brightness":
        return "light"
    if action == "lock":
        return "lock"
    return None


def _resolve_action_for_kind(action: str, device_kind: str) -> str:
    if device_kind == "lock":
        if action == "on":
            return "unlock"
        if action == "off":
            return "lock"
    return action
