"""Propagation authorship adjudication surface (spec §17 step 6).

A pure reader/append over what propagation captured + the human labels. These tests pin the digest
math and the label round-trip against an in-memory store — only the judge's confirm/reject calls are
adjudicatable (a deterministic floor_reject is never queued), and the promotion gate counts only
human-adjudicated deltas. Run from v2/:

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import adjudicate, selfserve


class _Store:
    """Minimal path->text store backing selfserve's read/list/write for the test."""
    def __init__(self):
        self.files = {}

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text, message=None):
        self.files[path] = text

    def listdir(self, path, include_dirs=False):
        prefix = path.rstrip("/") + "/"
        kids = set()
        for p in self.files:
            if p.startswith(prefix):
                kids.add(p[len(prefix):].split("/")[0])
        return sorted(kids)

    def append(self, path, line, message=None):
        existing = (self.files.get(path) or "").rstrip("\n")
        self.files[path] = (existing + "\n" if existing else "") + line + "\n"


PAYLOAD = {
    "schema_version": 1, "slug": "a__vs__b__x", "source": "monitor",
    "run_ts": "2026-06-14T11:00:00",
    "decisions": [
        {"operation": "add", "section": "objection_handling", "subject_key": "x|obj|current",
         "derived_from": "c_111111111111", "old_text": None, "new_text": "A real objection.",
         "judge_verdict": "confirm", "judge_reason": "grounded + pivots", "committed": True,
         "trigger_source_url": "https://s/1", "floor_violations": []},
        {"operation": "retire", "section": "battlecard", "subject_key": "y|play|current",
         "derived_from": "c_222222222222", "old_text": "An old play.", "new_text": None,
         "judge_verdict": "reject", "judge_reason": "weak retire", "committed": False,
         "trigger_source_url": "https://s/2", "floor_violations": []},
        {"operation": "add", "section": "objection_handling", "subject_key": "z|obj|current",
         "derived_from": "c_333333333333", "new_text": "floored", "judge_verdict": "floor_reject",
         "judge_reason": "n/a", "committed": False, "floor_violations": ["dangling derived_from"]},
    ],
}


class Adjudicate(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.store.files["propagation/a__vs__b__x/20260614T110000.json"] = json.dumps(PAYLOAD)
        self._patches = [
            mock.patch.object(selfserve, "list_data", self.store.listdir),
            mock.patch.object(selfserve, "read_data", self.store.read),
            mock.patch.object(selfserve, "write_data", self.store.write),
            mock.patch.object(selfserve, "append_data", self.store.append),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_only_judge_calls_are_adjudicatable_floor_reject_excluded(self):
        d = adjudicate.digest()
        self.assertEqual(d["captured"], 3)
        self.assertEqual(d["by_verdict"], {"confirm": 1, "reject": 1, "floor_reject": 1,
                                           "judge_unavailable": 0, "gated_routine": 0})
        # 2 judge deltas (confirm + reject) pending; the floor_reject is NOT queued.
        self.assertEqual(len(d["pending"]), 2)
        self.assertNotIn("z|obj|current", [p["subject_key"] for p in d["pending"]])

    def test_label_roundtrip_moves_delta_from_pending_to_adjudicated(self):
        confirm_delta = next(p for p in adjudicate.digest()["pending"]
                             if p["judge_verdict"] == "confirm")
        adjudicate.label(confirm_delta["delta_id"], "agree", "judge was right")
        d = adjudicate.digest()
        self.assertEqual(d["adjudicated"], 1)
        self.assertEqual(d["judge_right"], 1)
        self.assertEqual(d["judge_wrong"], 0)
        self.assertEqual(len(d["pending"]), 1)              # only the reject delta remains
        self.assertTrue(d["gate"]["net_positive"])
        self.assertFalse(d["gate"]["ready"])                # 1/20, nowhere near the gate

    def test_disagree_label_counts_against_the_judge(self):
        reject_delta = next(p for p in adjudicate.digest()["pending"]
                            if p["judge_verdict"] == "reject")
        adjudicate.label(reject_delta["delta_id"], "disagree", "should have confirmed")
        d = adjudicate.digest()
        self.assertEqual(d["judge_wrong"], 1)
        self.assertEqual(d["judge_right"], 0)
        self.assertFalse(d["gate"]["net_positive"])

    def test_v6_material_cure_fields_do_not_affect_the_corpus(self):
        """Corpus-integrity (2026-07-31): a v6 record carrying material/cure/material_uncured
        adjudicates identically — adjudicate reads only judge_verdict etc., never the new fields."""
        v6 = dict(PAYLOAD, schema_version=6)
        v6["decisions"] = [dict(d) for d in PAYLOAD["decisions"]]
        v6["decisions"][1].update(material=True, cure="root", material_uncured=False)  # the reject
        self.store.files["propagation/a__vs__b__x/20260614T110000.json"] = json.dumps(v6)
        d = adjudicate.digest()
        self.assertEqual(d["captured"], 3)                    # unchanged
        self.assertEqual(d["by_verdict"], {"confirm": 1, "reject": 1, "floor_reject": 1,
                                           "judge_unavailable": 0, "gated_routine": 0})
        self.assertEqual(len(d["pending"]), 2)                # the reject is still adjudicatable

    def test_delta_id_stable_across_reads(self):
        a = adjudicate.load_deltas()
        b = adjudicate.load_deltas()
        self.assertEqual([d["delta_id"] for d in a], [d["delta_id"] for d in b])

    def test_fail_closed_reject_excluded_from_gate_and_surfaced(self):
        # A judge that returned no verdict is defaulted to reject (fail-closed) — a hiccup, not a
        # judgment. It must not count toward the gate or sit in the human queue; it is surfaced for re-run.
        fc = {"schema_version": 1, "slug": "a__vs__b__x", "source": "monitor",
              "run_ts": "2026-06-15T11:00:00",
              "decisions": [
                  {"operation": "revise", "section": "objection_handling", "subject_key": "q|obj|current",
                   "derived_from": "c_444444444444", "old_text": "old", "new_text": "new",
                   "judge_verdict": "reject", "judge_reason": "no verdict returned (fail-closed)",
                   "committed": False, "trigger_source_url": "https://s/4", "floor_violations": []}]}
        self.store.files["propagation/a__vs__b__x/20260615T110000.json"] = json.dumps(fc)
        d = adjudicate.digest()
        self.assertEqual(len(d["fail_closed"]), 1)
        self.assertEqual(d["fail_closed"][0]["subject_key"], "q|obj|current")
        self.assertNotIn("q|obj|current", [p["subject_key"] for p in d["pending"]])  # not queued
        self.assertEqual(len(d["pending"]), 2)            # still only the original confirm + reject
        self.assertEqual(d["adjudicated"], 0)             # fail-close moves nothing toward the gate


if __name__ == "__main__":
    unittest.main()
