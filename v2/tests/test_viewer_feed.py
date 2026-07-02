"""Viewer-truthfulness invariants (2026-07-02: approved card updates were invisible — no feed row,
no dropdown reorder, and a 353-word objection wall had no gate). Pins: every APPLIED op writes a
feed entry; ordering sees claims' updated_on; the render word cap. Pure, no model/network.

    python -m unittest discover -s tests
"""
import unittest
from datetime import date, datetime
from unittest import mock

from scout import display, monitor, review, store
from scout.schema import RENDER_MAX_WORDS, render_structure_errors


OPS = [
    {"operation": "revise", "subject_key": "a|x|current", "feed_note": "x updated with the new beat"},
    {"operation": "add", "subject_key": "a|y|current", "feed_note": "new play y"},
    {"operation": "retire", "subject_key": "a|z|current", "feed_note": None,
     "retired_reason": "invalidated: z reversed"},
]
APPLIED = [{"operation": o["operation"], "subject_key": o["subject_key"]} for o in OPS]


class AppliedFeedAlerts(unittest.TestCase):
    MD_KEYS = ("date", "headline", "subject_key", "old_value", "new_value", "so_what")

    def test_every_applied_op_gets_an_entry_with_all_md_keys(self):
        out = monitor._applied_feed_alerts(OPS, APPLIED)
        self.assertEqual([a["subject_key"] for a in out],
                         ["a|x|current", "a|y|current", "a|z|current"])
        for a in out:                       # _append_alerts hard-indexes these six keys
            for k in self.MD_KEYS:
                self.assertIn(k, a)
            self.assertIn(a["severity"], ("act", "watch"))
            self.assertTrue(a["fingerprint"].startswith("f_"))

    def test_only_landed_ops_are_surfaced(self):
        out = monitor._applied_feed_alerts(OPS, APPLIED[:1])       # only the revise landed
        self.assertEqual([a["subject_key"] for a in out], ["a|x|current"])

    def test_feed_note_fallbacks(self):
        out = monitor._applied_feed_alerts(OPS, APPLIED)
        self.assertEqual(out[0]["headline"], "x updated with the new beat")
        self.assertEqual(out[2]["headline"], "invalidated: z reversed")  # retired_reason fallback

    def test_retire_only_alias_for_the_live_path(self):
        out = monitor._retire_feed_alerts(OPS, APPLIED)
        self.assertEqual([a["subject_key"] for a in out], ["a|z|current"])
        self.assertEqual((out[0]["old_value"], out[0]["new_value"]), ("on the card", "removed"))


class ReviewApplyFeed(unittest.TestCase):
    TARGET = {"id": "c_" + "1" * 12, "subject_key": "openai|flagship|current", "status": "active",
              "section": "battlecard", "zone": "contested", "order": 0,
              "claim": "Neutral framing of the flagship race.", "claim_type": "interpretation",
              "source_url": "https://news.test/x", "source_tier": "reputable_secondary",
              "evidence_excerpt": "z" * 45, "as_of": "2026-06-01", "verified": True,
              "confidence": "high",
              "grounding": {"checked": True, "match": True, "method": "substring",
                            "fetched_at": "2026-06-01"}}
    OP = {"operation": "revise", "section": "battlecard", "zone": "contested", "valence": "front_foot",
          "target_subject_key": "openai|flagship|current", "subject_key": "openai|flagship|current",
          "claim": "Updated neutral framing.", "claim_type": "interpretation",
          "derived_from": "c_" + "a" * 12, "feed_note": "flagship framing updated"}
    FACT = {"id": "c_" + "a" * 12, "subject_key": "f", "claim": "x", "claim_type": "fact",
            "section": "tracked_facts", "zone": None, "order": 0}

    def test_applied_revise_writes_feed_entry_and_apply_date(self):
        with mock.patch.object(review.store, "load_meta", return_value={"competitor": "OpenAI"}), \
             mock.patch.object(review.store, "load_claims", return_value=[dict(self.TARGET)]), \
             mock.patch.object(review, "_current_md", return_value=""), \
             mock.patch.object(review.store, "write_baseline"), \
             mock.patch.object(monitor, "_append_alerts") as appended, \
             mock.patch.object(review, "_log_human_verdict"):
            res = review.apply("slug", [dict(self.OP)], [dict(self.FACT)], write=True)
        self.assertEqual(len(res["applied"]), 1)
        appended.assert_called_once()
        entries = appended.call_args.args[1]
        self.assertEqual(entries[0]["headline"], "flagship framing updated")
        self.assertEqual(entries[0]["date"], date.today().isoformat())
        # updated_on stamps the APPLY date, not the proposal run's date
        revised = [c for c in res["claims"] if c.get("subject_key") == "openai|flagship|current"]
        self.assertEqual(revised[0].get("updated_on"), date.today().isoformat())


class OrderingSeesContentUpdates(unittest.TestCase):
    def test_claims_updated_on_moves_a_card_with_no_alerts(self):
        with mock.patch.object(display, "load_alerts", return_value=[]), \
             mock.patch.object(store, "load_claims",
                               return_value=[{"updated_on": "2026-07-02"}]), \
             mock.patch.object(store, "load_meta", return_value={"baseline_date": "2026-06-05"}):
            self.assertEqual(display.last_update_ts("s"), datetime(2026, 7, 2))

    def test_newer_alert_still_wins(self):
        with mock.patch.object(display, "load_alerts",
                               return_value=[{"detected_at": "2026-07-02T15:00:00"}]), \
             mock.patch.object(store, "load_claims", return_value=[{"updated_on": "2026-07-01"}]), \
             mock.patch.object(store, "load_meta", return_value={}):
            self.assertEqual(display.last_update_ts("s"), datetime(2026, 7, 2, 15, 0, 0))

    def test_pinned_card_holds_its_slot(self):
        ts = {"a": datetime(2026, 7, 2), "b": datetime(2026, 7, 1), "pin": datetime(2026, 1, 1),
              "c": datetime(2026, 6, 30), "d": datetime(2026, 6, 29)}
        with mock.patch.object(display, "list_battlecards", return_value=list(ts)), \
             mock.patch.object(display, "last_update_ts", side_effect=ts.get):
            out = display.ordered_cards(pinned_slug="pin", pinned_position=3)
        self.assertEqual(out, ["a", "b", "c", "pin", "d"])


class RenderWordCap(unittest.TestCase):
    def _objection(self, words):
        body = " ".join(["word"] * words)
        return {"section": "objection_handling", "zone": None, "persona": "economic_buyer",
                "claim": f"**\"Q?\"**\n\n{body}\n\n**So what:** act."}

    def test_over_cap_errors_under_cap_passes(self):
        over = self._objection(RENDER_MAX_WORDS + 10)
        under = self._objection(RENDER_MAX_WORDS - 30)
        self.assertTrue(any("render cap" in e for e in render_structure_errors(over)))
        self.assertFalse(any("render cap" in e for e in render_structure_errors(under)))

    def test_non_rendered_sections_exempt(self):
        wall = {"section": "tracked_facts", "zone": None,
                "claim": " ".join(["word"] * (RENDER_MAX_WORDS * 2))}
        self.assertEqual(render_structure_errors(wall), [])
        note = {"section": "sentiment", "zone": None,
                "claim": " ".join(["word"] * (RENDER_MAX_WORDS + 50))}
        self.assertEqual(render_structure_errors(note), [])


if __name__ == "__main__":
    unittest.main()
