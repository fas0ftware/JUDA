import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from juda import schedule as sched
from juda.core import Assistant
from juda.devices import Home
from juda.router import DaedalusStub
from juda.server import main as server_main
from juda.users import User, UserStore

ADMIN = User("yonetici", "admin")
FAMILY = User("aile", "family")
GUEST = User("misafir", "guest")

# A fixed local time: 2026-11-11 21:00:00
NOW = dt.datetime(2026, 11, 11, 21, 0, 0).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class SplitTimeTests(unittest.TestCase):
    def test_clock_time_today_and_tomorrow(self):
        spec, rest = sched.split_time("saat 22:30'da ışıkları kapat", NOW)
        self.assertEqual(rest, "ışıkları kapat")
        self.assertEqual(dt.datetime.fromtimestamp(spec.at), dt.datetime(2026, 11, 11, 22, 30))
        spec, _ = sched.split_time("07:15'te salon ışığını aç", NOW)
        self.assertEqual(dt.datetime.fromtimestamp(spec.at), dt.datetime(2026, 11, 12, 7, 15))

    def test_hour_only_and_suffixes(self):
        spec, rest = sched.split_time("saat 23'te kapıyı kilitle", NOW)
        self.assertEqual((dt.datetime.fromtimestamp(spec.at).hour, rest), (23, "kapıyı kilitle"))
        spec, rest = sched.split_time("kapıyı kilitle saat 23.45", NOW)
        self.assertEqual(spec.label, "23:45")
        self.assertEqual(rest, "kapıyı kilitle")

    def test_relative(self):
        spec, rest = sched.split_time("10 dakika sonra mutfağı kapat", NOW)
        self.assertEqual((spec.at - NOW, rest), (600, "mutfağı kapat"))
        spec, _ = sched.split_time("2 saat sonra iyi geceler", NOW)
        self.assertEqual(spec.at - NOW, 7200)
        self.assertIsNone(sched.split_time("0 dakika sonra ışıkları kapat", NOW)[0])
        self.assertIsNone(sched.split_time("9999 saat sonra ışıkları kapat", NOW)[0])

    def test_daily(self):
        spec, rest = sched.split_time("her gün 07:00'de salon ışığını aç", NOW)
        self.assertTrue(spec.daily)
        self.assertEqual(rest, "salon ışığını aç")
        self.assertEqual(spec.label, "her gün 07:00")

    def test_no_time_and_bad_times(self):
        for text in ("salon ışığını aç", "klimayı 23 derece yap", "yatak odası ışığını %40 yap",
                     "saat 25:00'te ışıkları aç", "24:30 ışık"):
            spec, rest = sched.split_time(text, NOW)
            if spec is not None:
                self.fail(f"unexpected time in {text!r}: {spec}")
            self.assertEqual(rest, text)


class SchedulerTests(unittest.TestCase):
    def test_persistence_and_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "schedules.json")
            clock = Clock(NOW)
            s = sched.Scheduler(path, clock=clock)
            once = s.add("aile", "family", "ışıkları kapat", sched.TimeSpec(NOW + 60, False, "x"))
            daily = s.add("aile", "family", "salon ışığını aç", sched.TimeSpec(NOW + 120, True, "y"))
            reloaded = sched.Scheduler(path, clock=clock)
            self.assertEqual([j.id for j in reloaded.list()], [once.id, daily.id])
            clock.t = NOW + 3 * 86400  # long outage: each job fires once, not three times
            self.assertEqual(sorted(j.id for j in reloaded.pop_due()), [once.id, daily.id])
            self.assertEqual([j.id for j in reloaded.list()], [daily.id])
            self.assertGreater(reloaded.list()[0].at, clock.t)
            self.assertEqual(reloaded.pop_due(), [])

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "schedules.json")
            with open(path, "w") as fh:
                fh.write('{"jobs": [{"id": "x"}, {"id": 3, "user": "a", "role": "family", "text": "t", "at": 1}]')
            self.assertEqual(sched.Scheduler(path).list(), [])
            with open(path, "w") as fh:
                json.dump({"jobs": [{"id": "x"}, {"id": 3, "user": "a", "role": "family", "text": "t", "at": 1}]}, fh)
            self.assertEqual([j.id for j in sched.Scheduler(path).list()], [3])

    def test_cancel_rules_and_limit(self):
        s = sched.Scheduler(None)
        a = s.add("aile", "family", "t", sched.TimeSpec(NOW))
        b = s.add("baska", "family", "t", sched.TimeSpec(NOW))
        self.assertFalse(s.cancel(b.id, "aile", is_admin=False))
        self.assertTrue(s.cancel(a.id, "aile", is_admin=False))
        self.assertTrue(s.cancel(b.id, "yonetici", is_admin=True))
        for _ in range(sched.MAX_JOBS):
            s.add("aile", "family", "t", sched.TimeSpec(NOW))
        with self.assertRaises(ValueError):
            s.add("aile", "family", "t", sched.TimeSpec(NOW))


class AssistantScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(NOW)
        self.users = {"yonetici": ADMIN, "aile": FAMILY, "misafir": GUEST}
        self.scheduler = sched.Scheduler(None, clock=self.clock)
        self.a = Assistant(Home(), DaedalusStub(), scheduler=self.scheduler,
                           user_lookup=self.users.get, clock=self.clock)

    def test_schedule_runs_later_not_now(self):
        reply = self.a.handle("10 dakika sonra salon ışığını aç", FAMILY)
        self.assertTrue(reply.ok, reply.text)
        self.assertIn("zamanlama #1", reply.text)
        self.assertFalse(self.a.home.get("living_room_light").state["on"])
        self.clock.t += 601
        for job in self.scheduler.pop_due():
            self.a.run_job(job)
        self.assertTrue(self.a.home.get("living_room_light").state["on"])

    def test_permissions_checked_when_scheduling(self):
        self.assertFalse(self.a.handle("saat 23:00'te kapıyı aç", FAMILY).ok)   # family cannot unlock
        self.assertFalse(self.a.handle("saat 23:00'te ışıkları kapat", GUEST).ok)
        self.assertFalse(self.a.handle("saat 23:00'te iyi geceler", GUEST).ok)
        self.assertTrue(self.a.handle("saat 23:00'te kapıyı kilitle", FAMILY).ok)
        self.assertTrue(self.a.handle("saat 23:00'te tüm ışıkları kapat", FAMILY).ok)

    def test_permissions_rechecked_when_running(self):
        self.assertTrue(self.a.handle("5 dakika sonra salon ışığını aç", FAMILY).ok)
        self.users["aile"] = User("aile", "guest")  # demoted in the meantime
        self.clock.t += 400
        job = self.scheduler.pop_due()[0]
        self.assertFalse(self.a.run_job(job).ok)
        self.assertFalse(self.a.home.get("living_room_light").state["on"])
        del self.users["aile"]  # removed users' jobs do nothing
        self.assertIsNone(self.a.run_job(job))

    def test_only_device_and_scene_commands(self):
        for text in ("saat 22:00'de kapı kilitli mi", "saat 22:00'de bana kod yaz", "saat 22:00'de"):
            self.assertFalse(self.a.handle(text, ADMIN).ok, text)

    def test_list_and_cancel_commands(self):
        self.assertTrue(self.a.handle("saat 22:30'da tüm ışıkları kapat", FAMILY).ok)
        self.a.handle("her gün 07:00'de salon ışığını aç", ADMIN)
        self.assertIn("#1", self.a.handle("zamanlamalar", FAMILY).text)
        self.assertNotIn("#2", self.a.handle("zamanlamalar", FAMILY).text)  # not theirs
        self.assertIn("#2", self.a.handle("zamanlamaları göster", ADMIN).text)
        self.assertFalse(self.a.handle("zamanlama 2 iptal", FAMILY).ok)
        self.assertTrue(self.a.handle("2 numaralı zamanlamayı iptal et", ADMIN).ok)
        self.assertIn("1 zamanlama iptal", self.a.handle("tüm zamanlamaları iptal et", FAMILY).text)
        self.assertEqual(self.a.handle("zamanlamalar", ADMIN).text, "Bekleyen zamanlama yok.")

    def test_plain_commands_still_work(self):
        self.assertTrue(self.a.handle("klimayı 23 derece yap", ADMIN).ok)
        self.assertEqual(self.a.home.get("living_room_ac").state["target"], 23)


class UserStoreTests(unittest.TestCase):
    def test_add_remove_rotate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UserStore(os.path.join(tmp, "users.json"))
            token = store.add("cocuk", "guest")
            self.assertEqual(store.authenticate(token), User("cocuk", "guest"))
            new = store.rotate("cocuk")
            self.assertIsNone(store.authenticate(token))
            self.assertEqual(store.authenticate(new).name, "cocuk")
            store.remove("cocuk")
            self.assertIsNone(store.authenticate(new))
            for bad in (("", "guest"), ("a b", "guest"), ("x" * 33, "guest"), ("ok", "root"), ("aile", "family")):
                with self.assertRaises(ValueError):
                    store.add(*bad)
            with self.assertRaises(ValueError):
                store.remove("yonetici")  # the last admin
            self.assertEqual(oct(os.stat(os.path.join(tmp, "users.json")).st_mode & 0o777), "0o600")

    def test_users_cli_never_lists_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(server_main(["users", "--data-dir", tmp, "add", "cocuk", "guest"]), 0)
                self.assertEqual(server_main(["users", "--data-dir", tmp, "list"]), 0)
                self.assertEqual(server_main(["users", "--data-dir", tmp, "remove", "nope"]), 2)
            text = out.getvalue()
            token = text.split("token: ")[1].split()[0]
            listing = text.split("\n", 1)[1]
            self.assertIn("cocuk", listing)
            self.assertNotIn(token, listing)


if __name__ == "__main__":
    unittest.main()
