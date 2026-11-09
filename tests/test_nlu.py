import unittest

from juda.nlu import normalize, parse


class NormalizeTests(unittest.TestCase):
    def test_turkish_characters(self):
        self.assertEqual(normalize("IŞIĞI Aç!"), "isigi ac")
        self.assertEqual(normalize("İyi geceler"), "iyi geceler")


class ParseTests(unittest.TestCase):
    def test_light_on_with_room(self):
        i = parse("Salon ışığını aç")
        self.assertEqual((i.kind, i.action, i.device_kind, i.rooms), ("device", "on", "light", ["salon"]))

    def test_light_off_all(self):
        i = parse("tüm ışıkları kapat")
        self.assertEqual((i.kind, i.action, i.device_kind), ("device", "off", "light"))
        self.assertTrue(i.all_devices)

    def test_room_without_device_word_defaults_to_light(self):
        i = parse("mutfağı kapat")
        self.assertEqual((i.kind, i.device_kind, i.action), ("device", "light", "off"))

    def test_bedroom_with_suffix(self):
        i = parse("yatak odasındaki ışığı kapat")
        self.assertEqual(i.rooms, ["yatak_odasi"])

    def test_brightness_percent(self):
        i = parse("yatak odası ışığını %40 yap")
        self.assertEqual((i.action, i.value, i.device_kind), ("set_brightness", 40, "light"))

    def test_brightness_yuzde(self):
        i = parse("salon ışığı yüzde 70 olsun")
        self.assertEqual((i.action, i.value), ("set_brightness", 70))

    def test_thermostat_degree(self):
        i = parse("klimayı 23 derece yap")
        self.assertEqual((i.kind, i.action, i.value, i.device_kind), ("device", "set_target", 23, "thermostat"))

    def test_thermostat_bare_number(self):
        i = parse("klimayı 24 yap")
        self.assertEqual((i.action, i.value), ("set_target", 24))

    def test_heat_and_cool(self):
        self.assertEqual(parse("evi ısıt").action, "adjust_target")
        self.assertEqual(parse("evi ısıt").value, 2)
        self.assertEqual(parse("salonu soğut").value, -2)

    def test_lock_and_unlock(self):
        self.assertEqual(parse("kapıyı kilitle").action, "lock")
        self.assertEqual(parse("kapıyı aç").action, "unlock")
        self.assertEqual(parse("kapıyı kapat").action, "lock")

    def test_consonant_softening_forms(self):
        # k -> ğ / t -> d yumuşaması: mutfak/mutfağı, yatak/yatağı, kilit/kilidi
        self.assertEqual(parse("mutfağı kapat").rooms, ["mutfak"])
        self.assertEqual(parse("yatağın ışığını aç").rooms, ["yatak_odasi"])
        i = parse("kilidi aç")
        self.assertEqual((i.device_kind, i.action), ("lock", "unlock"))
        self.assertEqual(parse("kilidi kapat").action, "lock")

    def test_status_questions(self):
        i = parse("kapı kilitli mi?")
        self.assertEqual((i.kind, i.device_kind), ("status", "lock"))
        self.assertEqual(parse("salon ışığı açık mı").kind, "status")
        self.assertEqual(parse("durum").kind, "status")

    def test_polite_request_is_a_command_not_a_question(self):
        i = parse("salon ışığını açar mısın")
        self.assertEqual((i.kind, i.action), ("device", "on"))

    def test_scenes(self):
        self.assertEqual(parse("Eve geldim").scene, "arrive")
        self.assertEqual(parse("evden çıkıyorum").scene, "leave")
        self.assertEqual(parse("iyi geceler").scene, "night")

    def test_help(self):
        self.assertEqual(parse("neler yapabilirsin?").kind, "help")

    def test_hard_tasks_go_to_daedalus(self):
        self.assertEqual(parse("bana bir python scripti için kod yaz").kind, "handoff")
        self.assertEqual(parse("yarınki toplantı için rapor hazırla").kind, "handoff")
        # Hava durumu bir ev cihazı sorusu değil; ev durumu sanılmamalı.
        self.assertEqual(parse("bugünün hava durumu nasıl").kind, "handoff")

    def test_unknown_goes_to_handoff(self):
        self.assertEqual(parse("mavi zürafalar dans ediyor").kind, "handoff")

    def test_empty(self):
        self.assertEqual(parse("   ").kind, "unknown")

    def test_light_without_room_needs_clarification_later(self):
        i = parse("ışığı aç")
        self.assertEqual((i.kind, i.device_kind, i.rooms), ("device", "light", []))
        self.assertFalse(i.all_devices)


if __name__ == "__main__":
    unittest.main()
