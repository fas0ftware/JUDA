import json
import os
import tempfile
import time
import unittest
from unittest import mock

from juda import schedule as sched
from juda.users import UserStore


class RunningServerSeesCliChanges(unittest.TestCase):
    """`python -m juda users ...` runs in another process; the server must notice at once."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "users.json")
        self.server = UserStore(self.path)
        self.cli = lambda: UserStore(self.path)

    def test_rotated_token_stops_working_in_the_running_server(self):
        old = self.server.tokens()["aile"][1]
        new = self.cli().rotate("aile")
        self.assertIsNone(self.server.authenticate(old))
        self.assertEqual(self.server.authenticate(new).name, "aile")

    def test_removed_user_is_gone_for_the_server_and_its_schedules(self):
        token = self.server.tokens()["aile"][1]
        self.cli().remove("aile")
        self.assertIsNone(self.server.authenticate(token))
        self.assertIsNone(self.server.get("aile"))

    def test_added_user_can_log_in_without_restart(self):
        token = self.cli().add("cocuk", "guest")
        self.assertEqual(self.server.authenticate(token).role, "guest")

    def test_server_change_does_not_lose_a_cli_change(self):
        self.cli().add("misafir", "guest")
        self.server.add("komsu", "guest")  # must merge with the file, not overwrite it
        self.assertEqual(set(self.cli().roles()), {"yonetici", "aile", "misafir", "komsu"})

    def test_corrupt_file_while_running_denies_everyone(self):
        token = self.server.tokens()["yonetici"][1]
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertLogs("juda.users", "ERROR"):
            self.assertIsNone(self.server.authenticate(token))

    def test_corrupt_file_at_start_is_a_clear_error_and_is_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            UserStore(self.path)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_file_is_private_and_written_atomically(self):
        self.server.add("x", "guest")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn("x", json.load(fh))


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
class DailyJobsKeepWallClockTime(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Berlin"  # DST: 2026-03-29 and 2026-10-25
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def local(self, *args):
        return time.mktime((*args, 0, 0, -1))

    def test_daily_job_stays_at_07_00_across_dst_changes(self):
        for day in ((2026, 3, 28), (2026, 10, 24)):
            at = self.local(*day, 7, 0, 0)
            nxt = sched.next_daily(at, at)
            self.assertEqual(time.localtime(nxt)[3:5], (7, 0))
            self.assertLess(nxt - at, 26 * 3600)
            self.assertGreater(nxt - at, 22 * 3600)

    def test_scheduler_reschedules_with_wall_clock(self):
        at = self.local(2026, 3, 28, 7, 0, 0)
        now = [at + 1]
        s = sched.Scheduler(None, clock=lambda: now[0])
        s.add("u", "admin", "salon ışığını aç", sched.TimeSpec(at=at, daily=True, label="07:00"))
        self.assertEqual(len(s.pop_due()), 1)
        self.assertEqual(time.localtime(s.list()[0].at)[3:5], (7, 0))

    def test_long_downtime_runs_once_and_lands_in_the_future(self):
        at = self.local(2026, 3, 20, 7, 0, 0)
        now = at + 10 * 86400 + 3600
        nxt = sched.next_daily(at, now)
        self.assertGreater(nxt, now)
        self.assertLess(nxt - now, 86400)
        self.assertEqual(time.localtime(nxt)[3:5], (7, 0))


if __name__ == "__main__":
    unittest.main()
