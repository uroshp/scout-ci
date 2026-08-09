"""Lead-election invariants (2026-08-08): the viewer's 'Today's angle' is the order-0 executive_summary
claim, historically frozen. When a run confirms an exec-summary verdict, a model judges whether that
fresh verdict is MATERIALLY more deal-moving than the incumbent and, if so, promotes it (order 0). The
bar IS the enforcement (auto-applies, no approval gate): decisive/clear promote, marginal/none HOLD,
within cooldown only decisive, any failure HOLDS. All model calls mocked (no network, no spend).

    python -m unittest discover -s tests
"""
import json
import unittest
from datetime import date, timedelta
from unittest import mock

from scout import config, notify, page, propagate
from scout.monitor import _lead_election_alert


def _elect_fake(payload):
    async def call(*args, **kwargs):
        return {"text": "```json\n" + json.dumps(payload) + "\n```", "cost_usd": 0.01}
    return call


def _es(sk, order, text, as_of="2026-08-01"):
    return {"subject_key": sk, "section": "executive_summary", "zone": None, "order": order,
            "status": "active", "claim": text, "as_of": as_of}


# incumbent at order 0, a fresh challenger at order 3 (the 8/8 Notion shape)
CLAIMS = [
    _es("data-gov", 0, "**Data governance is the wedge.**", as_of="2026-07-31"),
    _es("buyer-split", 1, "**Built for everyone else.**", as_of="2026-05-20"),
    _es("pricing", 3, "**Rovo looks free; the meter is running.**", as_of="2026-08-08"),
]
META = {"competitor": "Atlassian", "my_company": "Notion"}


class PromoteLeadReorder(unittest.TestCase):
    def test_winner_to_zero_peers_shift(self):
        out = propagate.promote_lead(CLAIMS, "pricing")
        by = {c["subject_key"]: c["order"] for c in out}
        self.assertEqual(by["pricing"], 0)
        self.assertEqual(sorted(by.values()), [0, 1, 2])          # renumbered, no gaps/dups

    def test_input_never_mutated(self):
        before = [(c["subject_key"], c["order"]) for c in CLAIMS]
        propagate.promote_lead(CLAIMS, "pricing")
        self.assertEqual(before, [(c["subject_key"], c["order"]) for c in CLAIMS])

    def test_unknown_key_is_noop(self):
        out = propagate.promote_lead(CLAIMS, "does-not-exist")
        self.assertEqual([(c["subject_key"], c["order"]) for c in out],
                         [(c["subject_key"], c["order"]) for c in CLAIMS])

    def test_already_lead_is_noop(self):
        out = propagate.promote_lead(CLAIMS, "data-gov")
        self.assertEqual({c["subject_key"]: c["order"] for c in out}["data-gov"], 0)

    def test_inactive_winner_skipped(self):
        claims = CLAIMS + [_es("retired-v", 5, "x")]
        claims[-1]["status"] = "retired"
        out = propagate.promote_lead(claims, "retired-v")
        self.assertEqual({c["subject_key"]: c["order"] for c in out
                          if c["subject_key"] == "data-gov"}["data-gov"], 0)


class Election(unittest.TestCase):
    def _elect(self, payload, within_cooldown=False, raise_exc=False):
        if raise_exc:
            async def boom(*a, **k):
                raise RuntimeError("model down")
            fake = boom
        else:
            fake = _elect_fake(payload)
        with mock.patch.object(propagate, "_run_election", fake):
            return propagate._lead_election(META, CLAIMS, ["pricing"],
                                            within_cooldown=within_cooldown)

    def test_decisive_promotes_challenger(self):
        el = self._elect({"winner_subject_key": "pricing", "margin": "decisive", "rationale": "cost"})
        self.assertTrue(el["promoted"])
        self.assertEqual(el["winner_key"], "pricing")
        self.assertEqual(el["margin"], "decisive")

    def test_clear_promotes(self):
        el = self._elect({"winner_subject_key": "pricing", "margin": "clear", "rationale": "r"})
        self.assertTrue(el["promoted"])

    def test_marginal_holds(self):
        el = self._elect({"winner_subject_key": "pricing", "margin": "marginal", "rationale": "r"})
        self.assertFalse(el["promoted"])
        self.assertEqual(el["winner_key"], "data-gov")            # incumbent held

    def test_none_holds(self):
        el = self._elect({"winner_subject_key": "data-gov", "margin": "none", "rationale": "r"})
        self.assertFalse(el["promoted"])

    def test_within_cooldown_clear_holds(self):
        el = self._elect({"winner_subject_key": "pricing", "margin": "clear", "rationale": "r"},
                         within_cooldown=True)
        self.assertFalse(el["promoted"])                          # only decisive may displace in cooldown

    def test_within_cooldown_decisive_promotes(self):
        el = self._elect({"winner_subject_key": "pricing", "margin": "decisive", "rationale": "r"},
                         within_cooldown=True)
        self.assertTrue(el["promoted"])

    def test_model_failure_holds(self):
        el = self._elect({}, raise_exc=True)
        self.assertFalse(el["promoted"])
        self.assertEqual(el["margin"], "none")
        self.assertEqual(el["winner_key"], "data-gov")

    def test_garbage_winner_falls_back_to_incumbent(self):
        el = self._elect({"winner_subject_key": "not-a-real-key", "margin": "decisive"})
        self.assertFalse(el["promoted"])                          # winner not in valid set -> hold

    def test_no_distinct_challenger_returns_none(self):
        # the only "challenger" IS the incumbent -> nothing to decide
        with mock.patch.object(propagate, "_run_election", _elect_fake({})):
            self.assertIsNone(propagate._lead_election(META, CLAIMS, ["data-gov"]))

    def test_no_exec_summary_returns_none(self):
        with mock.patch.object(propagate, "_run_election", _elect_fake({})):
            self.assertIsNone(propagate._lead_election(META, [], ["pricing"]))


class Helpers(unittest.TestCase):
    def test_pinned(self):
        self.assertTrue(propagate._lead_pinned({"lead_election": {"pinned": True}}))
        self.assertFalse(propagate._lead_pinned({}))
        self.assertFalse(propagate._lead_pinned({"lead_election": {}}))

    def test_within_cooldown(self):
        recent = (date.today() - timedelta(days=2)).isoformat()
        old = (date.today() - timedelta(days=config.LEAD_COOLDOWN_DAYS + 5)).isoformat()
        self.assertTrue(propagate._lead_within_cooldown({"lead_election": {"last_promoted_on": recent}}))
        self.assertFalse(propagate._lead_within_cooldown({"lead_election": {"last_promoted_on": old}}))
        self.assertFalse(propagate._lead_within_cooldown({}))          # never promoted

    def test_headline_from_bold(self):
        self.assertEqual(propagate._lead_headline("**A punchy lead.** and body"), "A punchy lead")


class RecordAndAlert(unittest.TestCase):
    EL = {"promoted": True, "winner_key": "pricing", "incumbent_key": "data-gov",
          "challenger_keys": ["pricing"], "margin": "decisive", "within_cooldown": False,
          "rationale": "cost exposure wins", "incumbent_as_of": "2026-07-31",
          "winner_as_of": "2026-08-08", "feed_note": "Today's angle changed"}

    def test_election_record_shape(self):
        r = propagate._election_record(self.EL)
        self.assertEqual(r["judge_verdict"], "lead_election")      # adjudicate keys on confirm/reject -> ignored
        self.assertTrue(r["lead_promoted"])
        self.assertEqual(r["lead_winner_key"], "pricing")
        self.assertEqual(r["lead_incumbent_key"], "data-gov")
        self.assertTrue(r["committed"])

    def test_feed_alert_has_six_keys(self):
        a = _lead_election_alert(self.EL)
        for k in ("date", "detected_at", "subject_key", "old_value", "new_value",
                  "headline", "so_what", "severity", "source_url", "fingerprint"):
            self.assertIn(k, a)
        self.assertEqual(a["subject_key"], "pricing")
        self.assertEqual(a["severity"], "act")


class Email(unittest.TestCase):
    # the FYI is called with the DECISION RECORD (el_rec), not the raw election dict
    REC = propagate._election_record(RecordAndAlert.EL)

    def test_fyi_text_and_html(self):
        subj, body = notify.render_lead_election_fyi("slug", META, self.REC)
        self.assertIn("Today's angle", subj)
        self.assertIn("cost exposure wins", body)                 # rationale -> judge_reason in record
        html = notify.render_lead_election_fyi_html("slug", META, self.REC)
        self.assertIn("New angle", html)
        self.assertIn("decisive", html)

    def test_send_noop_when_not_promoted(self):
        res = notify.send_lead_election_fyi("slug", META, {"lead_promoted": False}, dry_run=True)
        self.assertFalse(res.get("sent"))

    def test_send_dry_run_when_promoted(self):
        res = notify.send_lead_election_fyi("slug", META, self.REC, dry_run=True)
        self.assertNotEqual(res.get("reason"), "no promotion")


class ViewerAngle(unittest.TestCase):
    def test_hero_leads_with_promoted_verdict(self):
        promoted = propagate.promote_lead(CLAIMS, "pricing")
        html = page.static_brief_html(promoted, "", briefing=True)
        # the promoted verdict's headline is now the 'Today's angle' hero
        angle = html.split("Today's angle", 1)[1][:400]
        self.assertIn("the meter is running", angle)
        self.assertNotIn("refreshed today", html)


class PropagateIntegration(unittest.TestCase):
    """End-to-end (mocked route/author/judge + election): a confirmed exec-summary revise fires the
    election and the promotion rides propagate()'s return as prop['election'] + a decision record."""

    def _run(self, margin):
        fid = "c_aaaaaaaaaaaa"
        revise = {"operation": "revise", "section": "executive_summary", "zone": None,
                  "change_kind": "update", "valence": "front_foot", "claim_type": "interpretation",
                  "target_subject_key": "pricing", "subject_key": "pricing",
                  "derived_from": fid, "feed_note": "pricing sharpened", "persona": None}
        fact = {"id": fid, "subject_key": "notion|pricing", "claim": "Rovo meter change.",
                "source_url": "https://x", "about": "competitor", "as_of": "2026-08-08"}
        route_fake = lambda meta, fwa, claims: {"surface_ops": [revise], "no_surface": [],
                                                "run_verdict": {}, "cost_usd": 0.0, "raw": ""}
        with mock.patch.object(config, "PROPAGATE_MODE", "review"), \
             mock.patch.object(config, "LEAD_ELECTION", True), \
             mock.patch.object(propagate, "route", route_fake), \
             mock.patch.object(propagate, "author",
                               lambda *a, **k: {"ops": [{**revise, "claim": "**Rovo meter is running.**"}],
                                                "cost_usd": 0.0}), \
             mock.patch.object(propagate, "judge",
                               lambda *a, **k: {"verdicts": {0: {"verdict": "confirm", "reason": "ok",
                                                                 "material": True, "cure": "none",
                                                                 "rewritable": False, "judged_by": "m"}},
                                                "cost_usd": 0.0, "raw_failures": []}), \
             mock.patch.object(propagate, "_render_gate",
                               lambda *a, **k: {"held": {}, "condensed": {}, "cost_usd": {}}), \
             mock.patch.object(propagate, "_run_election",
                               _elect_fake({"winner_subject_key": "pricing", "margin": margin,
                                            "rationale": "cost"})):
            return propagate.propagate(META, [{"fact": fact, "alert": {"severity": "act"}}], [],
                                       CLAIMS, slug=None, persist=False)

    def test_decisive_fires_election_and_records_it(self):
        prop = self._run("decisive")
        self.assertIsNotNone(prop["election"])
        self.assertTrue(prop["election"]["promoted"])
        self.assertIn("feed_note", prop["election"])
        rec = [d for d in prop["decisions"] if d.get("judge_verdict") == "lead_election"]
        self.assertEqual(len(rec), 1)
        self.assertTrue(rec[0]["lead_promoted"])

    def test_marginal_records_hold_no_promotion(self):
        prop = self._run("marginal")
        self.assertIsNotNone(prop["election"])
        self.assertFalse(prop["election"]["promoted"])
        rec = [d for d in prop["decisions"] if d.get("judge_verdict") == "lead_election"]
        self.assertEqual(len(rec), 1)
        self.assertFalse(rec[0]["lead_promoted"])


if __name__ == "__main__":
    unittest.main()
