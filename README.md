# JUDA

A smart home digital assistant for controlling home technologies from your phone. This version is a **proof of concept / prototype**: devices are simulated, commands are typed in Turkish, and the UI runs in a phone browser. It uses only the Python standard library (nothing to install).

## Running

```bash
python3 -m juda                      # http://127.0.0.1:8765
python3 -m juda --host 0.0.0.0       # to reach it from a phone on the same network
python3 -m juda --port 9000 --data-dir /path/to/data
```

On first run, `data/users.json` is created and an access token for each user is printed to the console. Log in to the UI with one of these tokens. The `data/` folder is listed in `.gitignore`; do not share the tokens.

If you use `--host 0.0.0.0`, the server is open to everyone on your local network and traffic is unencrypted (plain HTTP). Only use it on a network you trust.

## What it can do

The assistant understands Turkish commands:

| Example command | Effect |
|---|---|
| `salon ışığını aç` / `mutfağı kapat` | turn a room's light on/off |
| `tüm ışıkları kapat` | all lights |
| `yatak odası ışığını %40 yap` | set brightness |
| `klimayı 23 derece yap`, `evi ısıt`, `salonu soğut` | air conditioner (10–30 °C) |
| `kapıyı kilitle` / `kapıyı aç` | door lock / unlock |
| `kapı kilitli mi?`, `durum` | status queries |
| `eve geldim`, `çıkıyorum`, `iyi geceler` | scenes (arrive, leave, night) |
| `bana şu script için kod yaz` | unrecognized or heavy task → handed off to Daedalus |

## Roles

- **admin**: can do everything; can view the event log and Daedalus tasks.
- **family**: everyday device control; **cannot** unlock the door and cannot send tasks to Daedalus.
- **guest**: can only ask for status.

## Relationship with Daedalus

JUDA handles simple home tasks and hands hard tasks over to Daedalus. The two systems do not share context or memory: a handoff carries only the task text and the name of the requesting user. For now Daedalus is a stub (`juda/router.py`) that writes tasks to `data/handoffs.jsonl`. A real integration only needs a class that implements the `DaedalusClient` interface (`submit`, `list_tasks`).

## Project layout

```
juda/devices.py   simulated devices + state machine + persistence (data/state.json)
juda/nlu.py       rule-based Turkish command parser
juda/users.py     roles, permissions, token authentication
juda/core.py      command -> intent -> permission check -> devices; scenes; event log
juda/router.py    handoff to Daedalus (stub)
juda/server.py    HTTP API + UI server (stdlib only)
juda/static/      phone-friendly web UI
tests/            unit and server tests
```

API: `GET /api/me`, `GET /api/devices`, `POST /api/command {"text": "..."}`, `POST /api/device {"id", "action", "value"}`, `GET /api/log` (admin), `GET /api/handoffs` (admin). All endpoints require an `Authorization: Bearer <token>` header.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

## Known limitations / next steps

- Devices are simulated; there is no real integration yet (MQTT, Zigbee, Tuya, Home Assistant, etc.).
- The parser is rule-based: it understands known patterns, and free-form sentences fall through to Daedalus. There is no scheduling (e.g. "turn on at 8 pm").
- User management is file-based only; there is no way to add users from the UI.
- It runs over plain HTTP; access from outside the home would require TLS and a reverse proxy.
