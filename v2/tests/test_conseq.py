"""Conseq. track: readiness threshold + the one-time 'ready to review' notification (fires once, only
after enough shadow verdicts accumulate)."""
import unittest
from unittest import mock

from scout import conseq, selfserve, config


def vs(n_cons, n_routine):
    return ([{"consequential": True, "slug": "a", "run_ts": "2026-06-28"} for _ in range(n_cons)]
            + [{"consequential": False, "slug": "b", "run_ts": "2026-06-28"} for _ in range(n_routine)])


class Readiness(unittest.TestCase):
    def test_not_ready_below_threshold(self):
        r = conseq.readiness(vs(2, 3), threshold=8)
        self.assertFalse(r["ready"])
        self.assertEqual((r["consequential"], r["routine"], r["count"]), (2, 3, 5))

    def test_ready_at_threshold(self):
        self.assertTrue(conseq.readiness(vs(4, 4), threshold=8)["ready"])


class NotifyOnce(unittest.TestCase):
    def _run(self, verdicts, marker=None):
        sent = {}

        def fake_dispatch(subject, body, dry_run=True):
            sent["subject"] = subject
            return {"sent": True, "via": "test"}

        with mock.patch.object(config, "CONSEQUENTIAL_FILTER", "shadow"), \
             mock.patch.object(conseq, "load_verdicts", lambda: verdicts), \
             mock.patch.object(selfserve, "read_data", lambda p: marker), \
             mock.patch.object(selfserve, "write_data") as w, \
             mock.patch("scout.notify._dispatch", fake_dispatch):
            res = conseq.maybe_notify_ready(send=True)
        return res, sent, w

    def test_emails_and_marks_when_ready_and_unnotified(self):
        res, sent, w = self._run(vs(5, 5), marker=None)
        self.assertTrue(res["notified"])
        self.assertIn("ready to review", sent["subject"])
        w.assert_called_once()                      # marker written so it won't repeat

    def test_silent_when_below_threshold(self):
        res, sent, w = self._run(vs(1, 1), marker=None)
        self.assertFalse(res["notified"])
        w.assert_not_called()

    def test_silent_when_already_notified(self):
        res, sent, w = self._run(vs(5, 5), marker='{"count": 10}')
        self.assertFalse(res["notified"])
        self.assertEqual(res["reason"], "already notified")


if __name__ == "__main__":
    unittest.main()
