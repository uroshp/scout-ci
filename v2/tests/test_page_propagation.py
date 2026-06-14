"""Viewer (page.py) handling of propagated + retired claims (propagation §17, step C).

When propagation goes live the store gains claims the original viewer never anticipated: propagated
interpretations with no source_url of their own, and retired claims that must leave the active card.
These tests pin the viewer's handling — retired off the active card into a lineage view, a propagated
claim's source link resolved through derived_from — so going live can't render a broken card. Pure
HTML render, no model/network. Run from v2/:

    python -m unittest discover -s tests
"""
import unittest

from scout.page import _prepare_display, _lineage, static_brief_html

PARENT = {"id": "c_aaaaaaaaaaaa", "subject_key": "anthropic|fable-5-availability|current",
          "claim": "Anthropic paused Fable 5 access for all customers.", "claim_type": "fact",
          "section": "recent_moves", "zone": None, "order": 0, "as_of": "2026-06-13",
          "source_url": "https://news.test/fable5"}

OBJ = {"id": "c_bbbbbbbbbbbb", "subject_key": "anthropic|fable-5-objection|current",
       "claim": "**\"Is your access stable?\"**\n\nThe net effect is all customers are affected.\n\n**So what:** standardize on Opus 4.8.",
       "claim_type": "interpretation", "section": "objection_handling", "zone": None, "order": 0,
       "as_of": "2026-06-13", "derived_from": "c_aaaaaaaaaaaa"}        # NO source_url of its own

PLAY = {"id": "c_cccccccccccc", "subject_key": "anthropic|live-win|current",
        "claim": "**Live win**\n\nReal and current.\n\n**Soundbite:** *\"build today\"*",
        "claim_type": "interpretation", "section": "battlecard", "zone": "where_we_win", "order": 0,
        "source_url": "https://own.test/a", "as_of": "2026-02-01"}

RETIRED = {"id": "c_dddddddddddd", "subject_key": "anthropic|dead-play|current",
           "claim": "**Old dead play**\n\nNo longer holds.\n\n**Soundbite:** *\"gone\"*",
           "claim_type": "interpretation", "section": "battlecard", "zone": "where_we_win", "order": 1,
           "status": "retired", "retired_on": "2026-06-14", "retired_reason": "invalidated: superseded",
           "derived_from": "c_aaaaaaaaaaaa", "source_url": "https://own.test/b", "as_of": "2026-01-01"}

CLAIMS = [PARENT, OBJ, PLAY, RETIRED]


class ViewerPropagation(unittest.TestCase):
    def test_prepare_display_splits_active_from_retired(self):
        active, retired = _prepare_display(CLAIMS)
        self.assertEqual({c["id"] for c in active}, {PARENT["id"], OBJ["id"], PLAY["id"]})
        self.assertEqual([c["id"] for c in retired], [RETIRED["id"]])

    def test_propagated_claim_borrows_parent_source_for_display(self):
        active, _ = _prepare_display(CLAIMS)
        obj = next(c for c in active if c["id"] == OBJ["id"])
        self.assertEqual(obj["source_url"], PARENT["source_url"])
        self.assertNotIn("source_url", OBJ)                 # input dict never mutated

    def test_lineage_renders_retired_entry(self):
        _, retired = _prepare_display(CLAIMS)
        html = _lineage(retired)
        self.assertIn("Old dead play", html)
        self.assertIn("Retired 2026-06-14", html)
        self.assertIn("invalidated: superseded", html)

    def test_static_brief_excludes_retired_from_active_but_keeps_lineage(self):
        html = static_brief_html(CLAIMS, md="", meta={"competitor": "OpenAI", "my_company": "Anthropic"})
        self.assertIn("Lineage", html)                      # lineage view present
        self.assertIn("Live win", html)                     # active play shown
        self.assertIn("news.test", html)                    # propagated objection's resolved source
        # The retired play appears ONLY in the lineage section, never in the live battlecard zone.
        self.assertEqual(html.count("Old dead play"), 1)


if __name__ == "__main__":
    unittest.main()
