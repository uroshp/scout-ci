"""check() two-arm wiring invariants (propagation §17, step A: my_company grounding).

The monitor core gained a my_company arm that can fire on its own (our news, competitor quiet).
These tests lock the control flow deterministically — model + network calls are mocked — so the
risky check() restructure can't silently regress. The guardrail that matters most: with
PROPAGATE_MODE=off the arm is dead and the quiet window early-returns exactly as before. Run from v2/:

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import monitor, config
from scout.schema import claim_id, ANCHOR_SECTION

SLUG = "anthropic-vs-openai"


def _triage(candidates):
    async def _fake(*a, **k):
        return {"text": "```json\n" + json.dumps({"has_candidates": True, "candidates": candidates})
                + "\n```", "cost_usd": 0.0}
    return _fake


PLAY = {"id": claim_id(SLUG, "anthropic|reliability|current"),
        "subject_key": "anthropic|reliability|current",
        "claim": "**We win on uptime**\n\nReliable.\n\n**Soundbite:** *\"x\"*",
        "claim_type": "interpretation", "section": "battlecard", "zone": "where_we_win", "order": 0,
        "source_url": "https://own.test/a", "source_tier": "reputable_secondary",
        "evidence_excerpt": "z" * 45, "as_of": "2026-02-01", "verified": True, "confidence": "high",
        "persona": "technical_evaluator",
        "grounding": {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-02-01"}}

MY_FACT = {"id": claim_id(SLUG, "anthropic|fable-5-availability|current"),
           "subject_key": "anthropic|fable-5-availability|current",
           "claim": "Anthropic paused Fable 5 access for all users.", "claim_type": "fact",
           "section": ANCHOR_SECTION, "zone": None, "order": 0,
           "source_url": "https://news.test/x", "source_tier": "reputable_secondary",
           "evidence_excerpt": "z" * 45, "as_of": "2026-06-13", "verified": True, "confidence": "high",
           "grounding": {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-06-13"}}

MY_ALERT = {"severity": "act", "old_value": None, "new_value": "paused for all users",
            "headline": "Anthropic paused Fable 5", "so_what": "migrate to Opus 4.8"}

META = {"slug": SLUG, "competitor": "OpenAI", "my_company": "Anthropic",
        "last_checked": "2026-06-13T00:00:00", "baseline_date": "2026-06-01"}


class MyCompanyArm(unittest.TestCase):
    def setUp(self):
        self._mode = config.PROPAGATE_MODE

    def tearDown(self):
        config.PROPAGATE_MODE = self._mode

    def _run(self, mode, candidates, fake_propagate):
        config.PROPAGATE_MODE = mode
        with mock.patch.object(monitor, "_run_triage", _triage(candidates)), \
             mock.patch.object(monitor, "_my_company_facts",
                               return_value={"grounded": [(MY_FACT, MY_ALERT)], "cost": 0.1}), \
             mock.patch.object(monitor, "propagate", side_effect=fake_propagate), \
             mock.patch.object(monitor.store, "load_meta", return_value=dict(META)), \
             mock.patch.object(monitor.store, "load_claims", return_value=[dict(PLAY)]), \
             mock.patch.object(monitor.store, "write_baseline") as wb, \
             mock.patch.object(monitor, "_append_alerts"), \
             mock.patch.object(monitor, "_current_md", return_value="# card"):
            res = monitor.check(SLUG, write=True)
        return res, wb

    def test_my_only_window_escalates_in_shadow_without_mutating_card(self):
        seen = {}

        def fake_propagate(meta, act_facts, new_claims, **k):
            seen["act_facts"] = act_facts
            return {"ops": [], "no_change": ["noted, no rep-facing change"], "floor_results": [],
                    "verdicts": {}, "confirmed": [], "decisions": [], "cost_usd": {"propose": 0.1, "judge": 0.1}}

        res, wb = self._run("shadow", [{"about": "my_company", "substantial": True,
                                        "signal": "Fable 5 paused", "valence": "back_foot"}], fake_propagate)
        # Escalated (NOT the quiet early-return) on the my_company arm alone.
        self.assertEqual(res["my_substantial"], 1)
        self.assertIn("my_company_facts", res)
        self.assertIn("propagation", res)
        # Propagation saw the grounded my_company act-fact.
        self.assertEqual([f["id"] for f in seen["act_facts"]], [MY_FACT["id"]])
        # SHADOW: the card written is the ORIGINAL claims (no tracked_fact persisted, no objection).
        written_claims = wb.call_args.args[1]
        self.assertEqual([c["id"] for c in written_claims], [PLAY["id"]])
        # last_checked advanced (gate stays honest).
        self.assertEqual(wb.call_args.args[2]["last_checked"], res["last_checked"])

    def test_mode_off_my_company_signal_is_inert_quiet_return(self):
        def fake_propagate(*a, **k):
            raise AssertionError("propagation must not run when PROPAGATE_MODE=off")

        res, wb = self._run("off", [{"about": "my_company", "substantial": True,
                                     "signal": "Fable 5 paused", "valence": "back_foot"}], fake_propagate)
        # do_my is False -> quiet early-return: the arm is fully inert.
        self.assertNotIn("propagation", res)
        self.assertNotIn("my_company_facts", res)
        self.assertFalse(res["no_change"])              # a my_company signal WAS detected (just not acted on)


if __name__ == "__main__":
    unittest.main()
