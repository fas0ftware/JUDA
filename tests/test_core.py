import json
import os
import tempfile
import unittest

from juda.core import Assistant
from juda.devices import Home
from juda.router import DaedalusStub
from juda.users import User, UserStore

ADMIN = User("yonetici", "admin")
FAMILY = User("aile", "family")
GUEST = User("misafir", "guest")


def make_assistant(tmp=None):
    state = os.path.join(tmp, "state.json") if tmp else None
    return Assistant(Home(state_path=state), DaedalusStub())


class HomeTests(unittest.TestCase):
    def test_brightness_bounds(self):
        home = Home()
        ok, _ = home.apply("living_room_light", "set_brightness", 101)
        self.assertFalse(ok)
        ok, _ = home.apply("living_room_light", "set_brightness", 0)
        self.assertTrue(ok)
        self.assertFalse(home.get("living_room_light").state["on"])

    def test_target_bounds(self):
        home = Home()
        self.assertFalse(home.apply("living_room_ac", "set_target", 45)[0])
        self.assertTrue(home.apply("living_room_ac", "set_target", 25)[0])
        self.assertFalse(home.apply("living_room_ac", "adjust_target", 10)[0])

    def test_bool_is_not_a_number(self):
        home = Home()
        self.assertFalse(home.apply("living_room_ac", "set_target", True)[0])

    def test_unknown_device(self):
        self.assertFalse(Home().apply("nope", "on")[0])

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            home = Home(state_path=path)
            home.apply("kitchen_light", "on")
            again = Home(state_path=path)
            self.assertTrue(again.get("kitchen_light").state["on"])

    def test_corrupt_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                fh.write("{bozuk")
            self.assertFalse(Home(state_path=path).get("kitchen_light").state["on"])


class AssistantTests(unittest.TestCase):
    def test_turn_on_room_light(self):
        a = make_assistant()
        r = a.handle("salon ışığını aç", ADMIN)
        self.assertTrue(r.ok)
        self.assertTrue(a.home.get("living_room_light").state["on"])
        self.assertFalse(a.home.get("bedroom_light").state["on"])

    def test_light_without_room_asks_which(self):
        a = make_assistant()
        r = a.handle("ışığı aç", ADMIN)
        self.assertFalse(r.ok)
        self.assertIn("Hangi oda", r.text)

    def test_all_lights_off(self):
        a = make_assistant()
        a.handle("tüm ışıkları aç", ADMIN)
        a.handle("tüm ışıkları kapat", ADMIN)
        self.assertTrue(all(not d.state["on"] for d in a.home.list_devices(kind="light")))

    def test_thermostat(self):
        a = make_assistant()
        r = a.handle("klimayı 24 derece yap", FAMILY)
        self.assertTrue(r.ok)
        self.assertEqual(a.home.get("living_room_ac").state["target"], 24)

    def test_thermostat_out_of_range_is_reported(self):
        a = make_assistant()
        r = a.handle("klimayı 50 derece yap", ADMIN)
        self.assertFalse(r.ok)
        self.assertEqual(a.home.get("living_room_ac").state["target"], 22)

    def test_family_cannot_unlock_door(self):
        a = make_assistant()
        r = a.handle("kapıyı aç", FAMILY)
        self.assertFalse(r.ok)
        self.assertTrue(a.home.get("front_door").state["locked"])

    def test_admin_can_unlock_and_family_can_lock(self):
        a = make_assistant()
        self.assertTrue(a.handle("kapıyı aç", ADMIN).ok)
        self.assertFalse(a.home.get("front_door").state["locked"])
        self.assertTrue(a.handle("kapıyı kilitle", FAMILY).ok)
        self.assertTrue(a.home.get("front_door").state["locked"])

    def test_guest_cannot_control_but_can_ask(self):
        a = make_assistant()
        self.assertFalse(a.handle("salon ışığını aç", GUEST).ok)
        self.assertFalse(a.home.get("living_room_light").state["on"])
        self.assertTrue(a.handle("kapı kilitli mi", GUEST).ok)

    def test_status_text(self):
        a = make_assistant()
        r = a.handle("kapı kilitli mi?", ADMIN)
        self.assertIn("kilitli", r.text)

    def test_arrive_scene_turns_on_hall_and_living_room(self):
        a = make_assistant()
        a.handle("eve geldim", ADMIN)
        self.assertTrue(a.home.get("living_room_light").state["on"])
        self.assertTrue(a.home.get("hall_light").state["on"])
        self.assertFalse(a.home.get("bedroom_light").state["on"])

    def test_leave_scene(self):
        a = make_assistant()
        a.handle("tüm ışıkları aç", ADMIN)
        a.handle("kapıyı aç", ADMIN)
        a.handle("çıkıyorum", ADMIN)
        self.assertTrue(all(not d.state["on"] for d in a.home.list_devices(kind="light")))
        self.assertTrue(a.home.get("front_door").state["locked"])

    def test_scene_reports_denied_steps_for_family(self):
        a = make_assistant()
        r = a.handle("iyi geceler", FAMILY)  # kilitleme serbest, kapı açma yok
        self.assertTrue(r.ok)
        self.assertEqual(a.home.get("living_room_ac").state["target"], 19)

    def test_handoff_only_for_admin(self):
        a = make_assistant()
        r = a.handle("bana bir script için kod yaz", ADMIN)
        self.assertIsNotNone(r.handoff_id)
        self.assertEqual(len(a.daedalus.list_tasks()), 1)
        r2 = a.handle("bana bir script için kod yaz", FAMILY)
        self.assertIsNone(r2.handoff_id)
        self.assertEqual(len(a.daedalus.list_tasks()), 1)

    def test_handoff_carries_only_task_and_user(self):
        a = make_assistant()
        a.handle("kod yaz: hesap makinesi", ADMIN)
        task = a.daedalus.list_tasks()[0]
        self.assertEqual(set(task), {"id", "task", "requested_by", "created_at", "status"})

    def test_empty_and_too_long_input(self):
        a = make_assistant()
        self.assertFalse(a.handle("", ADMIN).ok)
        self.assertFalse(a.handle("a" * 501, ADMIN).ok)

    def test_ui_control_respects_permissions(self):
        a = make_assistant()
        self.assertFalse(a.control("front_door", "unlock", None, FAMILY).ok)
        self.assertTrue(a.control("front_door", "unlock", None, ADMIN).ok)
        self.assertFalse(a.control("nope", "on", None, ADMIN).ok)

    def test_event_log_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            a = Assistant(Home(), DaedalusStub(), log_path=path)
            a.handle("salon ışığını aç", ADMIN)
            with open(path, encoding="utf-8") as fh:
                entry = json.loads(fh.readline())
            self.assertEqual(entry["user"], "yonetici")
            self.assertEqual(len(a.recent_log()), 1)


class UserStoreTests(unittest.TestCase):
    def test_defaults_and_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UserStore(os.path.join(tmp, "users.json"))
            self.assertTrue(store.created_defaults)
            role, token = store.tokens()["yonetici"]
            self.assertEqual(store.authenticate(token), User("yonetici", "admin"))
            self.assertIsNone(store.authenticate("yanlis"))
            self.assertIsNone(store.authenticate(None))
            again = UserStore(os.path.join(tmp, "users.json"))
            self.assertFalse(again.created_defaults)
            self.assertEqual(again.authenticate(token).role, "admin")

    def test_users_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "users.json")
            UserStore(path)
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
