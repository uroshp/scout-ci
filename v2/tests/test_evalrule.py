"""scout/evalrule.py: the shared promotion rule must reproduce scripts/eval_checkin.verdict exactly
for the existing lanes (parity), and honour a different bar for the model lane."""
import unittest

from scout import evalrule


def cur(adj, prec, pending=0):
    return {"adjudicated": adj, "precision": prec, "pending": pending}


class Parity(unittest.TestCase):
    """The table the check-in's own tests pin (tests/test_eval_checkin.py), reproduced via the
    check-in wrapper so any drift between the two shows up here."""

    def test_wrapper_matches_shared_rule(self):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "ec", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "eval_checkin.py")
        ec = importlib.util.module_from_spec(spec); spec.loader.exec_module(ec)
        K = "verification"
        table = [(cur(5, 1.0), None), (cur(20, 0.85), None), (cur(20, 0.90), {"precision": 0.85}),
                 (cur(20, 0.82), {"precision": 0.90}), (cur(20, 0.60), {"precision": 0.50}),
                 (cur(20, 0.60), {"precision": 0.70, "no_improve_streak": 2}),
                 (cur(20, None), None)]
        for c, p in table:
            self.assertEqual(ec.verdict(K, c, p),
                             evalrule.verdict(K, c, p, bar=ec.PRECISION_BAR,
                                              min_adjudicated=ec.MIN_ADJUDICATED[K], kill_streak=ec.KILL_STREAK))


class OwnBar(unittest.TestCase):
    def test_model_lane_parity_bar(self):
        v = evalrule.verdict("judge", cur(15, 0.52), None, bar=0.50, min_adjudicated=15,
                             bar_label="bar 0.50 (parity)")
        self.assertEqual(v["status"], "BASELINE")
        self.assertIn("parity", v["note"])
        self.assertEqual(evalrule.verdict("judge", cur(14, 0.9), None, bar=0.50, min_adjudicated=15)["status"],
                         "ACCUMULATE")

    def test_kill_streak_counts_only_non_improving_below_bar(self):
        v1 = evalrule.verdict("k", cur(20, 0.30), {"precision": 0.35, "no_improve_streak": 1}, bar=0.5, min_adjudicated=15)
        self.assertEqual((v1["status"], v1["no_improve_streak"]), ("DIAGNOSE", 2))
        v2 = evalrule.verdict("k", cur(20, 0.30), {"precision": 0.35, "no_improve_streak": 2}, bar=0.5, min_adjudicated=15)
        self.assertEqual(v2["status"], "KILL?")


class Labels(unittest.TestCase):
    def test_last_wins_loader(self):
        from unittest import mock
        from scout import selfserve
        raw = '{"delta_id":"m_1","truth":"confirm"}\n\nnot json\n{"delta_id":"m_1","truth":"reject"}\n{"delta_id":"m_2","truth":"keep"}\n'
        with mock.patch.object(selfserve, "read_data", return_value=raw):
            rows = evalrule.load_label_rows("shadow_models/labels.jsonl")
        self.assertEqual(rows["m_1"]["truth"], "reject")
        self.assertEqual(set(rows), {"m_1", "m_2"})
