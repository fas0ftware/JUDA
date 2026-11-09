# JUDA

Evdeki teknolojileri telefondan kontrol etmek için akıllı ev dijital asistanı. Bu sürüm bir **PoC / prototip**: cihazlar simüle edilir, komutlar Türkçe yazılır, arayüz telefon tarayıcısında açılır. Sadece Python standart kütüphanesini kullanır (kurulum gerekmez).

## Çalıştırma

```bash
python3 -m juda                      # http://127.0.0.1:8765
python3 -m juda --host 0.0.0.0       # aynı ağdaki telefondan erişmek için
python3 -m juda --port 9000 --data-dir /yol/veri
```

İlk çalıştırmada `data/users.json` oluşur ve her kullanıcı için bir erişim anahtarı (token) konsola yazılır. Arayüzde bu anahtarla giriş yapılır. `data/` klasörü `.gitignore` içindedir; tokenları paylaşma.

`--host 0.0.0.0` kullanırsan sunucu yerel ağdaki herkese açılır ve trafik şifresizdir (HTTP). Bunu sadece güvendiğin ağda kullan.

## Neler yapabilir

| Komut örneği | Etkisi |
|---|---|
| `salon ışığını aç` / `mutfağı kapat` | oda ışığı aç/kapat |
| `tüm ışıkları kapat` | hepsi |
| `yatak odası ışığını %40 yap` | parlaklık |
| `klimayı 23 derece yap`, `evi ısıt`, `salonu soğut` | klima (10–30 °C) |
| `kapıyı kilitle` / `kapıyı aç` | kapı kilidi |
| `kapı kilitli mi?`, `durum` | durum sorgusu |
| `eve geldim`, `çıkıyorum`, `iyi geceler` | sahneler |
| `bana şu script için kod yaz` | anlaşılmayan/ağır iş → Daedalus'a devir |

## Roller

- **admin**: her şey, kayıtları ve Daedalus görevlerini görür.
- **family**: günlük cihaz kontrolü; kapı kilidini **açamaz**, Daedalus'a iş gönderemez.
- **guest**: sadece durum sorabilir.

## Daedalus ile ilişki

JUDA basit ev işlerini yapar, zor işleri Daedalus'a devreder. İki sistem bağlam/hafıza paylaşmaz: devirde sadece görev metni ve isteyen kullanıcının adı gider. Şimdilik Daedalus bir stub'dır (`juda/router.py`), görevleri `data/handoffs.jsonl` dosyasına yazar. Gerçek entegrasyon için `DaedalusClient` arayüzünü (`submit`, `list_tasks`) sağlayan bir sınıf yeterli.

## Yapı

```
juda/devices.py   simüle cihazlar + durum makinesi + kalıcılık (data/state.json)
juda/nlu.py       kural tabanlı Türkçe komut ayrıştırıcı
juda/users.py     roller, yetkiler, token doğrulama
juda/core.py      komut -> niyet -> yetki -> cihaz; sahneler; olay kaydı
juda/router.py    Daedalus devri (stub)
juda/server.py    HTTP API + arayüz (stdlib)
juda/static/      telefon uyumlu arayüz
tests/            birim ve sunucu testleri
```

API: `GET /api/me`, `GET /api/devices`, `POST /api/command {"text": "..."}`, `POST /api/device {"id", "action", "value"}`, `GET /api/log` (admin), `GET /api/handoffs` (admin). Hepsi `Authorization: Bearer <token>` ister.

## Test

```bash
python3 -m unittest discover -s tests -t .
```

## Bilinen sınırlar / sonraki adımlar

- Cihazlar simüle; gerçek entegrasyon (MQTT, Zigbee, Tuya, Home Assistant vb.) yok.
- Ayrıştırıcı kural tabanlı: bilinen kalıpları anlar, serbest cümlelerde Daedalus'a düşer. Zamanlama ("akşam 8'de aç") yok.
- Kullanıcı yönetimi sadece dosyadan; arayüzden kullanıcı ekleme yok.
- HTTP üzerinden çalışır; evin dışından erişim için TLS ve ters proxy gerekir.
