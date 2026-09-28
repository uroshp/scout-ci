"""Derived capture for the verification challenger (2026-09-28). Interpretations carry no excerpt;
the challenger used to cut them for 'no evidence' (June) and then skipped them entirely. Now the
capture pairs each new/revised derived claim with its parent fact, the challenger judges SUPPORT on
it, results are tagged by evidence mode, and the scoreboard splits pre/post fix. Pure, no model."""
import unittest

from scout import shadow, challenger

FACT = {"id": "c_fact", "subject_key": "f|1", "claim": "Vendor X cut prices 20% on Sep 22.",
        "section": "tracked_facts", "evidence_excerpt": "prices are 20% lower effective Sep 22",
        "source_url": "https://news.test/x", "source_tier": "reputable_secondary", "as_of": "2026-09-22"}
PLAY = {"id": "c_play", "subject_key": "p|1", "claim": "**Cheaper now**\n\nX cut prices 20%.",
        "section": "battlecard", "zone": "where_we_win", "derived_from": "c_fact",
        "updated_on": "2026-09-28", "as_of": "2026-09-22"}
OLD_PLAY = {**PLAY, "id": "c_old", "subject_key": "p|old", "updated_on": "2026-08-01", "as_of": "2026-08-01"}
STAMPED = {**PLAY, "id": "c_prov", "subject_key": "p|prov",
           "provenance": {"source_url": "https://judge.test/seen", "source_tier": "primary"}}


class DerivedRows(unittest.TestCase):
    def test_pairs_changed_derived_claims_with_their_parent(self):
        rows = shadow.derived_rows([FACT, PLAY, OLD_PLAY], changed_on="2026-09-28")
        self.assertEqual([r["id"] for r in rows], ["c_play"])
        self.assertEqual(rows[0]["parent"]["claim"], FACT["claim"])
        self.assertEqual(rows[0]["parent"]["evidence_excerpt"], FACT["evidence_excerpt"])
        self.assertEqual(rows[0]["parent"]["source_url"], "https://news.test/x")

    def test_provenance_by_value_wins_for_the_parent_source(self):
        rows = shadow.derived_rows([FACT, STAMPED], changed_on="2026-09-28")
        self.assertEqual(rows[0]["parent"]["source_url"], "https://judge.test/seen")

    def test_facts_and_orphans_are_excluded(self):
        orphan = {**PLAY, "id": "c_orphan", "derived_from": "c_missing"}
        self.assertEqual(shadow.derived_rows([FACT, orphan], changed_on="2026-09-28"), [])

    def test_kept_rows_carry_parent_and_mode(self):
        rows = shadow._kept_rows(shadow.derived_rows([FACT, PLAY], changed_on="2026-09-28"))
        self.assertEqual(rows[0]["evidence_mode"], "parent_fact")
        self.assertEqual(rows[0]["parent"]["subject_key"], "f|1")


class ChallengerItems(unittest.TestCase):
    def test_parent_fact_item_is_judged_with_the_fact_as_evidence(self):
        record = {"slug": "s", "run_ts": "2026-09-28T11:00:00",
                  "kept": shadow._kept_rows(shadow.derived_rows([FACT, PLAY], "2026-09-28")), "cut": []}
        items, champ = challenger._items(record)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["evidence_mode"], "parent_fact")
        self.assertEqual(items[0]["supporting_fact"], FACT["claim"])
        self.assertEqual(items[0]["evidence"], FACT["evidence_excerpt"])
        self.assertEqual(champ["c_play"], "keep")

    def test_excerpt_less_claim_without_parent_is_still_skipped(self):
        record = {"slug": "s", "run_ts": "t", "kept": [{"id": "c_x", "claim": "x"}], "cut": []}
        self.assertEqual(challenger._items(record)[0], [])

    def test_compare_tags_evidence_mode_and_scorecard_slices_by_period(self):
        record = {"slug": "s", "run_ts": "2026-09-28T11:00:00",
                  "kept": shadow._kept_rows(shadow.derived_rows([FACT, PLAY], "2026-09-28")), "cut": []}
        judged = {"verdicts": {"c_play": {"verdict": "cut", "reason": "r", "confidence": "high"}}}
        cmp = challenger.compare(record, judged)
        self.assertEqual(cmp["items"][0]["evidence_mode"], "parent_fact")
        self.assertEqual(cmp["items"][0]["status"], "disagree")
        post = {**challenger.result_record(record, judged, cmp), "judged_at": "2026-09-29T00:00:00"}
        pre = {**post, "judged_at": "2026-06-22T00:00:00", "items": [{**post["items"][0], "delta_id": "x_old",
                                                                       "evidence_mode": None}]}
        sl = challenger.scorecard_slices([pre, post])
        self.assertEqual(sl["period"]["pre_fix"]["disagreements"], 1)
        self.assertEqual(sl["period"]["post_fix"]["disagreements"], 1)
        self.assertIn("parent_fact", sl["by_evidence_mode"])
        self.assertIn("legacy", sl["by_evidence_mode"])
