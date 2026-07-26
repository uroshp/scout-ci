"""Supersede-retire sweep (2026-07-25, the no-stale-state philosophy): the router NAMES superseded
identifiers, code VERIFIES them against grounded evidence and SWEEPS active claims citing them into
judge-decided retire candidates; a confirmed candidate retires through the existing lineage-keeping
machinery, and the monitor hunts the replacement value for a bounded window. All pure helpers —
no model calls, no network. Run from v2/:

    python -m unittest discover -s tests
"""
import unittest

from scout import config, monitor
from scout.propagate import (apply_ops, floor_check, supersede_candidates,
                             verified_superseded_terms, _active_targets)
from scout.route import _clean_ops

FID = "c_aaaaaaaaaaaa"
FACT = {"id": FID, "claim": "Claude Opus 5 shipped July 24, replacing Opus 4.8 at the same price.",
        "evidence_excerpt": "Opus 5 replaces Opus 4.8; Opus 4.7 is two generations back."}
FACTS_BY_ID = {FID: FACT}

LEADERBOARD = {"id": "c_bbbbbbbbbbbb", "subject_key": "claude-vs-gpt|coding-benchmark|current",
               "section": "battlecard", "zone": "contested", "status": "active", "order": 0,
               "claim": "GPT-5.5 leads SWE-bench at 88.7% to Claude Opus 4.7's 87.6%."}
UNRELATED = {"id": "c_cccccccccccc", "subject_key": "openai|enterprise-deal|current",
             "section": "positioning", "zone": None, "status": "active", "order": 0,
             "claim": "OpenAI leads on enterprise logos."}
RETIRED = {"id": "c_dddddddddddd", "subject_key": "old|thing|current",
           "section": "battlecard", "zone": "contested", "status": "retired",
           "claim": "Opus 4.7 was flagged here once.", "retired_on": "2026-07-01",
           "retired_reason": "invalidated: gone"}
RECENT_MOVE = {"id": "c_eeeeeeeeeeee", "subject_key": "anthropic|move|2026-06",
               "section": "recent_moves", "zone": None, "status": "active",
               "claim": "Opus 4.7 shipped in April."}


def _sop(terms, target=None):
    return {"operation": "revise", "section": "snapshot", "zone": None, "change_kind": "update",
            "derived_from": FID, "target_subject_key": target, "subject_key": target,
            "superseded_terms": terms}


class VerifiedTerms(unittest.TestCase):
    def test_term_grounded_in_fact_text_survives(self):
        out = verified_superseded_terms([_sop(["Opus 4.8"])], FACTS_BY_ID, {})
        self.assertEqual(out, [{"term": "Opus 4.8", "trigger_claim_id": FID, "as_of": None}])

    def test_term_grounded_only_in_excerpt_survives(self):
        out = verified_superseded_terms([_sop(["Opus 4.7"])], FACTS_BY_ID, {})
        self.assertEqual([t["term"] for t in out], ["Opus 4.7"])

    def test_ungrounded_term_dropped(self):
        out = verified_superseded_terms([_sop(["Opus 3.9", "GPT-4o"])], FACTS_BY_ID, {})
        self.assertEqual(out, [])

    def test_term_grounded_in_the_revised_claims_current_text(self):
        active = _active_targets([dict(LEADERBOARD)])
        op = _sop(["GPT-5.5"], target=LEADERBOARD["subject_key"])
        out = verified_superseded_terms([op], FACTS_BY_ID, active)
        self.assertEqual([t["term"] for t in out], ["GPT-5.5"])

    def test_short_numeric_and_junk_terms_dropped(self):
        out = verified_superseded_terms([_sop(["4.8", "24", "O5", "  ", 42, None])],
                                        FACTS_BY_ID, {})
        self.assertEqual(out, [])

    def test_dedupes_casefold_across_ops(self):
        out = verified_superseded_terms([_sop(["Opus 4.8"]), _sop(["opus 4.8"])], FACTS_BY_ID, {})
        self.assertEqual(len(out), 1)


class SweepCandidates(unittest.TestCase):
    TERMS = [{"term": "Opus 4.7", "trigger_claim_id": FID}]

    def test_active_routable_claim_citing_term_becomes_candidate(self):
        claims = [dict(LEADERBOARD), dict(UNRELATED)]
        cands = supersede_candidates(claims, self.TERMS, set())
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["operation"], "retire")
        self.assertEqual(c["change_kind"], "supersede_retire")
        self.assertEqual(c["target_subject_key"], LEADERBOARD["subject_key"])
        self.assertEqual(c["superseded_term"], "Opus 4.7")
        self.assertTrue(c["retired_reason"].startswith("superseded:"))
        self.assertTrue(c["feed_note"])                    # a removal is never silent
        self.assertIsNone(c["claim"])

    def test_candidate_passes_the_deterministic_floor(self):
        claims = [dict(LEADERBOARD)]
        cands = supersede_candidates(claims, self.TERMS, set())
        active = _active_targets(claims)
        self.assertEqual(floor_check(cands[0], {FID}, active), [])

    def test_skips_routed_retired_recent_moves_and_triggers(self):
        trigger_claim = {"id": FID, "subject_key": "anthropic|flagship|current",
                         "section": "snapshot", "zone": None, "status": "active",
                         "claim": "Opus 4.7 era snapshot."}
        routed = {"id": "c_ffffffffffff", "subject_key": "x|routed|current",
                  "section": "battlecard", "zone": "contested", "status": "active",
                  "claim": "Opus 4.7 mention on an already-routed claim."}
        claims = [dict(LEADERBOARD), dict(RETIRED), dict(RECENT_MOVE), trigger_claim, routed]
        cands = supersede_candidates(claims, self.TERMS, {"x|routed|current"})
        self.assertEqual([c["target_subject_key"] for c in cands],
                         [LEADERBOARD["subject_key"]])

    def test_reconciled_history_filter_skips_freshly_updated_claims(self):
        """A claim touched on/after the supersession date mentions the old identifier as
        deliberate compressed history ("Opus 5 replaces Opus 4.8") — never a sweep candidate."""
        dated_terms = [{"term": "Opus 4.7", "trigger_claim_id": FID, "as_of": "2026-07-24"}]
        fresh = dict(LEADERBOARD, updated_on="2026-07-25")
        stale = dict(LEADERBOARD, subject_key="other|bench|current", updated_on="2026-07-01")
        cands = supersede_candidates([fresh, stale], dated_terms, set())
        self.assertEqual([c["target_subject_key"] for c in cands], ["other|bench|current"])
        undated = [{"term": "Opus 4.7", "trigger_claim_id": FID}]
        self.assertEqual(len(supersede_candidates([fresh], undated, set())), 1)

    def test_input_never_mutated_and_empty_terms_noop(self):
        claims = [dict(LEADERBOARD)]
        before = [dict(c) for c in claims]
        self.assertEqual(supersede_candidates(claims, [], set()), [])
        supersede_candidates(claims, self.TERMS, set())
        self.assertEqual(claims, before)


class ApplyAndLineage(unittest.TestCase):
    def test_confirmed_candidate_retires_with_superseded_reason_and_lineage(self):
        from scout.schema import claim_id
        sk = LEADERBOARD["subject_key"]
        full = {
            "id": claim_id("slug-x", sk), "subject_key": sk, "claim": LEADERBOARD["claim"],
            "claim_type": "interpretation", "section": "battlecard", "zone": "contested",
            "order": 0, "source_url": "https://news.test/bench", "source_tier": "reputable_secondary",
            "evidence_excerpt": "b" * 45, "as_of": "2026-06-01", "verified": True,
            "confidence": "high",
            "grounding": {"checked": True, "match": True, "method": "substring",
                          "fetched_at": "2026-06-01"},
        }
        claims = [full]
        cand = supersede_candidates(claims, SweepCandidates.TERMS, set())[0]
        res = apply_ops(claims, [cand],
                        [dict(FACT, subject_key="anthropic|flagship|current", as_of="2026-07-24",
                              source_url="https://news.test/opus5")],
                        "slug-x", "2026-07-25")
        self.assertEqual(len(res["applied"]), 1)
        gone = next(c for c in res["claims"] if c["subject_key"] == LEADERBOARD["subject_key"])
        self.assertEqual(gone["status"], "retired")
        self.assertTrue(gone["retired_reason"].startswith("superseded:"))
        self.assertEqual(gone["retired_on"], "2026-07-25")
        self.assertEqual(gone["claim"], LEADERBOARD["claim"])   # text kept for lineage
        self.assertEqual(str(claims[0].get("status", "active")), "active")  # input untouched


class CleanOpsSanitization(unittest.TestCase):
    BASE = {"operation": "revise", "section": "snapshot", "zone": None, "change_kind": "update"}

    def test_valid_terms_pass_junk_dropped(self):
        ops = _clean_ops([dict(self.BASE, superseded_terms=["Opus 4.8", "", 3, None, " x "])])
        self.assertEqual(ops[0]["superseded_terms"], ["Opus 4.8", "x"])

    def test_non_list_normalizes_to_empty(self):
        for bad in ("Opus 4.8", {"a": 1}, 7, None):
            ops = _clean_ops([dict(self.BASE, superseded_terms=bad)])
            self.assertEqual(ops[0]["superseded_terms"], [])


class HuntTargets(unittest.TestCase):
    def _retired(self, on, reason="superseded: still cites Opus 4.7, replaced per the linked update"):
        return {"subject_key": "claude-vs-gpt|coding-benchmark|current", "section": "battlecard",
                "status": "retired", "retired_on": on, "retired_reason": reason}

    def test_recent_supersede_retire_is_hunted(self):
        out = monitor._supersede_hunt_targets([self._retired("2026-07-20")], today="2026-07-25")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["subject_key"], "claude-vs-gpt|coding-benchmark|current")

    def test_expires_after_the_window(self):
        old = self._retired("2026-07-01")
        self.assertEqual(monitor._supersede_hunt_targets([old], today="2026-07-25"), [])
        edge = self._retired("2026-07-11")                 # exactly SUPERSEDE_HUNT_DAYS=14 back
        self.assertEqual(len(monitor._supersede_hunt_targets([edge], today="2026-07-25")), 1)

    def test_other_retirements_and_active_claims_ignored(self):
        rows = [self._retired("2026-07-24", reason="invalidated: parent falsified"),
                {"status": "active", "retired_on": "2026-07-24",
                 "retired_reason": "superseded: x"},
                self._retired(None)]
        self.assertEqual(monitor._supersede_hunt_targets(rows, today="2026-07-25"), [])


if __name__ == "__main__":
    unittest.main()
