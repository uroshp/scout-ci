"""The 14-day check-in's pre-registered go/no-go rule (docs/eval-exit-criteria.md), the promotion
instrument for the v3.5 takeover judges. Pure-function tests of the verdict logic incl. the kill branch."""
import unittest
import sys
sys.path.insert(0, "scripts")
import eval_checkin as ec


def cur(adjudicated, precision, pending=0):
    return {"adjudicated": adjudicated, "right": 0, "wrong": 0, "precision": precision, "pending": pending}


class Verdict(unittest.TestCase):
    K = "verification"   # MIN_ADJUDICATED = 15, bar 0.80

    def test_accumulate_when_too_few(self):
        self.assertEqual(ec.verdict(self.K, cur(5, 1.0), None)["status"], "ACCUMULATE")

    def test_baseline_first_time_on_bar(self):
        self.assertEqual(ec.verdict(self.K, cur(20, 0.85), None)["status"], "BASELINE")

    def test_eligible_sustained(self):
        self.assertEqual(ec.verdict(self.K, cur(20, 0.90), {"precision": 0.85})["status"], "ELIGIBLE")

    def test_watch_above_bar_but_down(self):
        self.assertEqual(ec.verdict(self.K, cur(20, 0.82), {"precision": 0.90})["status"], "WATCH")

    def test_diagnose_below_bar_improving(self):
        v = ec.verdict(self.K, cur(20, 0.60), {"precision": 0.50})
        self.assertEqual(v["status"], "DIAGNOSE")
        self.assertEqual(v["no_improve_streak"], 0)

    def test_streak_increments_then_kills(self):
        # below bar, not improving, streak builds to KILL_STREAK
        v1 = ec.verdict(self.K, cur(20, 0.50), {"precision": 0.60, "no_improve_streak": 0})
        self.assertEqual((v1["status"], v1["no_improve_streak"]), ("DIAGNOSE", 1))
        v3 = ec.verdict(self.K, cur(20, 0.50), {"precision": 0.60, "no_improve_streak": 2})
        self.assertEqual((v3["status"], v3["no_improve_streak"]), ("KILL?", 3))


if __name__ == "__main__":
    unittest.main()
