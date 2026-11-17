"""The owner's quiet hours (2026-11-17): an email written before 9 AM Pacific is held in the store
and delivered after; nothing is lost and nothing is delivered twice."""
import json
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from scout import config, notify, selfserve

PT = ZoneInfo("America/Los_Angeles")
_REAL_QUIET = notify._quiet_now


def at(hour):
    return datetime(2026, 11, 17, hour, 20, tzinfo=PT)


class QuietHoursTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.store = {}

        def update(path, transform, message, **kw):
            new = transform(self.store.get(path))
            if new is None:
                return False
            self.store[path] = new
            return True

        def append(path, line, message):
            self.store[path] = (self.store.get(path) or "") + line + "\n"

        def send(subject, body, html=None):
            self.sent.append(subject)
            return {"sent": self.send_ok, "subject": subject}
        self.send_ok = True
        for target, new in ((selfserve, {"update_data": update, "append_data": append}),
                            (notify, {"_send_now": send}),
                            (config, {"ALERT_EMAIL_TO": "owner@example.com", "QUIET_HOURS": "0-9", "REHEARSAL": False})):
            for k, v in new.items():
                p = mock.patch.object(target, k, v)
                p.start()
                self.addCleanup(p.stop)

    def _clock(self, hour):
        p = mock.patch.object(notify, "_quiet_now", lambda now=None: _REAL_QUIET(at(hour)))
        p.start()
        self.addCleanup(p.stop)

    def test_window(self):
        self.assertTrue(notify._quiet_now(at(4)))
        self.assertTrue(notify._quiet_now(at(0)))
        self.assertFalse(notify._quiet_now(at(9)))
        self.assertFalse(notify._quiet_now(at(23)))
        with mock.patch.object(config, "QUIET_HOURS", "off"):
            self.assertFalse(notify._quiet_now(at(4)))
        with mock.patch.object(config, "QUIET_HOURS", "21-9"):
            self.assertTrue(notify._quiet_now(at(22)))
            self.assertFalse(notify._quiet_now(at(12)))

    def test_an_early_email_is_held_not_sent(self):
        self._clock(4)
        r = notify._dispatch("Scout this morning", "body", dry_run=False, html="<b>x</b>")
        self.assertTrue(r["held"])
        self.assertEqual(self.sent, [])
        held = [json.loads(x) for x in self.store[notify.HELD_PATH].splitlines()]
        self.assertEqual((held[0]["subject"], held[0]["html"]), ("Scout this morning", "<b>x</b>"))

    def test_a_dry_run_holds_nothing(self):
        self._clock(4)
        notify._dispatch("x", "body", dry_run=True)
        self.assertEqual(self.store, {})

    def test_held_mail_goes_out_after_nine_in_order_and_once(self):
        self._clock(4)
        notify._dispatch("first", "b", dry_run=False)
        notify._dispatch("second", "b", dry_run=False)
        self.assertEqual(notify.flush_held(), {"delivered": 0, "quiet": True})
        self._clock(9)
        self.assertEqual(notify.flush_held()["delivered"], 2)
        self.assertEqual(self.sent, ["first", "second"])
        self.assertEqual(notify.flush_held()["delivered"], 0)
        self.assertEqual(self.sent, ["first", "second"])

    def test_a_daytime_email_delivers_the_held_ones_first(self):
        self._clock(4)
        notify._dispatch("overnight", "b", dry_run=False)
        self._clock(10)
        notify._dispatch("daytime", "b", dry_run=False)
        self.assertEqual(self.sent, ["overnight", "daytime"])

    def test_a_failed_delivery_is_put_back(self):
        self._clock(4)
        notify._dispatch("overnight", "b", dry_run=False)
        self._clock(9)
        self.send_ok = False
        self.assertEqual(notify.flush_held(), {"delivered": 0, "put_back": 1})
        self.send_ok = True
        self.assertEqual(notify.flush_held()["delivered"], 1)

    def test_if_the_store_refuses_the_email_is_sent_now(self):
        self._clock(4)
        with mock.patch.object(selfserve, "append_data", side_effect=RuntimeError("store down")):
            r = notify._dispatch("Scout this morning", "body", dry_run=False)
        self.assertEqual(self.sent, ["Scout this morning"])
        self.assertNotIn("held", r)

    def test_a_rehearsal_email_is_never_held(self):
        self._clock(4)
        with mock.patch.object(config, "REHEARSAL", True):
            notify._dispatch("x", "body", dry_run=False)
        self.assertEqual(self.sent, ["[rehearsal] x"])


if __name__ == "__main__":
    unittest.main()
