"""Pre-email render gate (2026-07-18): judge-confirmed ops are validated + auto-repaired BEFORE
the decision log and the proposals email, so the email shows publishable text and calls out holds
inline. (The incident: a 201-word confirmed op rode the email looking fine, then was silently held
at approve time — the render gate used to exist only at apply.) All model calls are mocked. Run
from v2/:

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import config, propagate, reformat
from scout.propagate import _active_targets, _render_gate

CLEAN_OBJ = ("**\"Q?\"**\n\nAnswer with substance.\n\n**So what:** pivot to the play.")
NO_SOWHAT = ("**\"Q?\"**\n\nAnswer with substance but no closing block.")


def _op(claim=CLEAN_OBJ, operation="add", section="objection_handling", persona="economic_buyer",
        **kw):
    d = {"operation": operation, "section": section, "zone": None, "claim": claim,
         "subject_key": "a|b|c", "target_subject_key": None, "persona": persona}
    d.update(kw)
    return d


def _confirm(n):
    return {i: {"verdict": "confirm"} for i in range(n)}


class RenderGateUnit(unittest.TestCase):
    def test_clean_op_costs_nothing(self):
        def boom(s, o):
            raise AssertionError("repair must not run for a clean op")
        ops = [_op()]
        held = _render_gate("s", ops, [[]], _confirm(1), {}, repair=boom)
        self.assertEqual(held, {})
        self.assertEqual(ops[0]["claim"], CLEAN_OBJ)

    def test_unconfirmed_and_retire_ops_are_skipped(self):
        def boom(s, o):
            raise AssertionError("repair must not run for skipped ops")
        ops = [_op(claim=NO_SOWHAT),                      # floor-rejected
               _op(claim=NO_SOWHAT),                      # judge-rejected
               _op(operation="retire", claim=None)]       # retires carry no prose
        verdicts = {1: {"verdict": "reject"}, 2: {"verdict": "confirm"}}
        held = _render_gate("s", ops, [["floored"], [], []], verdicts, {}, repair=boom)
        self.assertEqual(held, {})

    def test_repaired_op_is_replaced_in_place(self):
        fixed = _op()                                     # valid after "repair"
        ops = [_op(claim=NO_SOWHAT)]
        held = _render_gate("s", ops, [[]], _confirm(1),
                            {}, repair=lambda s, o: ("repaired", fixed))
        self.assertEqual(held, {})
        self.assertEqual(ops[0]["claim"], CLEAN_OBJ)      # email will show the publishable text

    def test_unrepairable_op_is_held_with_the_exact_reason(self):
        ops = [_op(claim=NO_SOWHAT)]
        held = _render_gate("s", ops, [[]], _confirm(1),
                            {}, repair=lambda s, o: ("held", dict(o)))
        self.assertIn(0, held)
        self.assertIn("So what", held[0]["reason"])       # the residual violation, verbatim class
        self.assertIn("auto-repair exhausted", held[0]["reason"])

    def test_revise_inherits_persona_from_the_target(self):
        """A REVISE missing only its persona takes the badge the card already carries — a field
        fix, zero model calls."""
        def boom(s, o):
            raise AssertionError("persona inheritance should have made this op clean")
        target = {"subject_key": "a|b|c", "section": "objection_handling", "zone": None,
                  "status": "active", "claim": "old", "persona": "economic_buyer"}
        active = _active_targets([target])
        ops = [_op(operation="revise", persona=None, target_subject_key="a|b|c")]
        held = _render_gate("s", ops, [[]], _confirm(1), active, repair=boom)
        self.assertEqual(held, {})
        self.assertEqual(ops[0]["persona"], "economic_buyer")

    def test_a_crashing_repair_degrades_to_the_old_behavior(self):
        def crash(s, o):
            raise RuntimeError("api down")
        ops = [_op(claim=NO_SOWHAT)]
        held = _render_gate("s", ops, [[]], _confirm(1), {}, repair=crash)
        self.assertEqual(held, {})                        # not held, not lost: apply-time gate remains
        self.assertEqual(ops[0]["claim"], NO_SOWHAT)


class RenderGateWiring(unittest.TestCase):
    """Through the real propagate(): the records handed to the log/email already carry the gate's
    verdicts, confirmed excludes held ops, and repair is invoked with alert=False (the proposals
    email is the flag — no separate hold email)."""

    CLAIMS = [{"subject_key": "openai|flagship-model|current", "section": "battlecard",
               "zone": "where_they_win", "status": "active",
               "claim": "They lead on the frontier model."}]
    FACT_ID = "c_aaaaaaaaaaaa"
    FACT = {"id": FACT_ID, "subject_key": "anthropic|sonnet-5-launch", "claim": "Sonnet 5 shipped.",
            "source_url": "https://example.com/launch", "about": "my_company",
            "valence": "front_foot"}
    SURFACE_OP = {"operation": "revise", "section": "battlecard", "zone": "where_they_win",
                  "change_kind": "update", "valence": "front_foot",
                  "target_subject_key": "openai|flagship-model|current",
                  "subject_key": "openai|flagship-model|current",
                  "derived_from": FACT_ID, "feed_note": "note", "persona": None, "why": "moved"}

    def _run(self, mode, repair_mock):
        route_fake = lambda meta, fwa, claims: {"surface_ops": [dict(self.SURFACE_OP)],
                                                "no_surface": [], "run_verdict": {},
                                                "cost_usd": 0.0, "raw": ""}
        author_fake = lambda meta, sops, facts, claims: {
            "ops": [dict(self.SURFACE_OP, claim_type="interpretation",
                         claim="**Play**\n\nProse without a soundbite block.")],
            "cost_usd": 0.0}
        judge_fake = lambda meta, facts, claims, survivors: {
            "verdicts": {0: {"verdict": "confirm", "reason": "holds", "rewritable": False}},
            "cost_usd": 0.0, "raw_failures": []}
        with mock.patch.object(propagate, "route", route_fake), \
             mock.patch.object(propagate, "author", author_fake), \
             mock.patch.object(propagate, "judge", judge_fake), \
             mock.patch.object(config, "PROPAGATE_MODE", mode), \
             mock.patch.object(config, "PROPAGATE_MAX_REWRITES", 0), \
             mock.patch.object(reformat, "repair_or_hold", repair_mock):
            return propagate.propagate({"competitor": "OpenAI", "my_company": "Anthropic"},
                                       [{"fact": dict(self.FACT), "alert": {"severity": "act"}}],
                                       [], self.CLAIMS, slug="wiring-test", persist=False)

    def test_review_mode_holds_ride_the_records_and_confirmed_excludes_them(self):
        calls = []
        def repair_mock(slug, op, **kw):
            calls.append(kw)
            return ("held", dict(op))
        res = self._run("review", repair_mock)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("alert"), False)     # no separate hold email
        d = res["decisions"][0]
        self.assertTrue(d.get("held_for_format"))
        self.assertIn("auto-repair exhausted", d.get("hold_reason", ""))
        self.assertFalse(d.get("committed"))
        self.assertEqual(res["confirmed"], [])             # live apply / approve can't touch it

    def test_shadow_mode_never_runs_the_gate(self):
        def repair_mock(slug, op, **kw):
            raise AssertionError("shadow mode must not spend on the render gate")
        res = self._run("shadow", repair_mock)
        self.assertNotIn("held_for_format", res["decisions"][0])
        self.assertEqual(len(res["confirmed"]), 1)         # unchanged shadow behavior


class ReformatAlertFlag(unittest.TestCase):
    def _held_claim(self):
        return {"operation": "add", "section": "objection_handling", "zone": None,
                "subject_key": "a|b|c", "persona": "economic_buyer", "claim": NO_SOWHAT}

    def test_alert_false_skips_the_standalone_email_but_still_stores(self):
        writes = []
        with mock.patch.object(reformat.selfserve, "write_data",
                               side_effect=lambda p, t, m: writes.append((p, t))), \
             mock.patch.object(reformat, "_alert_human") as alert:
            status, _ = reformat.repair_or_hold("s", self._held_claim(),
                                                reformatter=lambda *a: None, alert=False)
        self.assertEqual(status, "held")
        alert.assert_not_called()
        self.assertEqual(len(writes), 1)                   # durably held either way
        rec = json.loads(writes[0][1])
        self.assertIn("So what", rec["reason"])            # the exact residual violation travels

    def test_alert_defaults_on_for_every_legacy_caller(self):
        with mock.patch.object(reformat.selfserve, "write_data"), \
             mock.patch.object(reformat, "_alert_human") as alert:
            reformat.repair_or_hold("s", self._held_claim(), reformatter=lambda *a: None)
        alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
