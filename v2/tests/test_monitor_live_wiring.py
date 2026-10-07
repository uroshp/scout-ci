"""LIVE-WIRING test (2026-10-03): check(write=True) in production's mode, end to end through the REAL
store, with fakes only at the model boundary.

Why: the 10/3 4 AM run crashed the audience step on four of six cards (`today` was bound only inside
the propagation block) and nothing caught it: every dry run has write=False, which skips the step,
and the unit tests ran check() in shadow/off modes. This file runs the four live branches the morning
can take and asserts every step row is `ran` or `skipped` with a reason, never `failed`, and that the
card files on disk changed the way they should. No network, no model, no spend. Run from v2/:

    python -m unittest tests.test_monitor_live_wiring
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from scout import config, monitor, selfserve, store

SLUG = "cursor__vs__cognition__general"
ARCHIVE = os.path.join(config.APP_ROOT, "archive", SLUG)
FACT_SK = "cognition | valuation | current"
TARGET_SK = "cognition | positioning | cloud-agent-vs-local"
TEN_KEYS = {"date", "detected_at", "subject_key", "old_value", "new_value", "headline", "so_what",
            "severity", "source_url", "fingerprint"}


def _triage(candidates):
    async def _fake(*a, **k):
        return {"text": json.dumps({"has_candidates": bool(candidates), "candidates": candidates}), "cost_usd": 0.11}
    return _fake


def _load(path):
    with open(path) as f:
        return json.load(f)


class _Live(unittest.TestCase):
    """Common harness: a temp copy of the archive card as the store root, a temp private store,
    production's flags, every model call faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "battlecards")
        os.makedirs(self.root)
        shutil.copytree(ARCHIVE, os.path.join(self.root, SLUG))
        self.card = os.path.join(self.root, SLUG)
        self.claims0 = _load(os.path.join(self.card, "claims.json"))
        self.meta0 = _load(os.path.join(self.card, "meta.json"))
        self.by_sk = {c["subject_key"]: c for c in self.claims0}
        self.patches = [
            mock.patch.object(store, "STORE_ROOT", self.root),
            mock.patch.object(selfserve, "_REPO_ROOT", os.path.join(self.tmp, "store")),
            mock.patch.object(config, "PROPAGATE_MODE", "live"),
            mock.patch.object(config, "AUDIENCE_LEADS", True),
            mock.patch.object(config, "SHADOW_EVAL_ENABLED", False),
            mock.patch.object(config, "CALL_CAPTURE_ENABLED", False),
            mock.patch.object(config, "SIGNALS_ENABLED", False),
            mock.patch.object(config, "CONSEQUENTIAL_FILTER", "off"),
            mock.patch.object(config, "RUN_MAX_USD", 0.0),
            mock.patch.object(config, "REHEARSAL", False),
            # tripwire: an invalid fake claim would otherwise reach a model-backed repair
            mock.patch("scout.reformat.repair_or_hold", side_effect=AssertionError("render repair reached a model")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- fixtures ---------------------------------------------------------------------------
    def fact(self, severity="watch"):
        """A fresh version of an existing snapshot fact (same id, new value), with its alert."""
        f = dict(self.by_sk[FACT_SK])
        f.pop("corroboration", None)
        f["claim"] = "Cognition closed a Series F at a $60B post-money valuation on October 1, 2026."
        f["evidence_excerpt"] = "Cognition has raised a Series F at a $60B valuation."
        f["as_of"] = "2026-10-01"
        alert = {"severity": severity, "old_value": "$48B valuation", "new_value": "$60B valuation",
                 "headline": "Cognition valued at $60B", "so_what": "Price pressure on the enterprise tier."}
        return f, alert

    def arm(self, pairs):
        def _fake(slug, meta, since, substantial, claims, result, sig_block=""):
            result["cost"]["materiality"] = 0.5
            return pairs, {"kept": [p[0] for p in pairs], "cut": [], "results": []}, []
        return _fake

    def confirmed_revise(self, fact):
        tgt = self.by_sk[TARGET_SK]
        op = {"operation": "revise", "target_subject_key": TARGET_SK, "subject_key": TARGET_SK,
              "section": tgt["section"], "zone": tgt.get("zone"), "derived_from": fact["id"],
              "claim": tgt["claim"] + " The $60B round widens that gap.", "feed_note": "positioning updated for the Series F"}
        dec = {"subject_key": TARGET_SK, "operation": "revise", "judge_verdict": "confirm", "judged_by": "judge",
               "new_text": op["claim"], "old_text": tgt["claim"], "feed_note": op["feed_note"]}

        def _fake(meta, facts_with_alerts, strength_facts, claims, **k):
            assert k.get("persist") is True, "live mode must persist the decision log"
            return {"ops": [op], "surface_ops": [op], "no_surface": [], "no_change": [], "run_verdict": {"verdict": "consequential"},
                    "floor_results": [None], "verdicts": {0: {"verdict": "confirm"}}, "confirmed": [op], "decisions": [dec],
                    "cost_usd": {"route": 0.2, "author": 0.1, "judge": 0.3}, "election": None}
        return _fake

    @staticmethod
    def audience_fake(calls):
        def _fake(slug, meta, claims, today, *, write, **k):
            calls.append({"slug": slug, "today": today, "write": write})
            return {"claims": claims, "applied": [], "rejected": [], "cost_usd": 0.0,
                    "personas": ["economic_buyer"], "skipped": None}
        return _fake

    # --- helpers ----------------------------------------------------------------------------
    @staticmethod
    def rows(res):
        return {r["step"]: r for r in res["steps"]}

    def assert_no_failed(self, res):
        failed = [r for r in res["steps"] if r["status"] == "failed"]
        self.assertEqual(failed, [], f"failed steps: {failed}")
        for r in res["steps"]:
            self.assertIn(r["status"], ("ran", "skipped"))
            if r["status"] == "skipped":
                self.assertTrue(r.get("detail"), f"a skipped step must say why: {r}")

    def alerts_on_disk(self):
        p = os.path.join(self.card, "alerts.jsonl")
        return [json.loads(l) for l in open(p).read().splitlines() if l.strip()]


class QuietMorning(_Live):
    def test_quiet_writes_a_heartbeat_and_abandons_the_exhausted_window(self):
        calls = []
        n_alerts0 = len(self.alerts_on_disk())
        with mock.patch.object(monitor, "_run_triage", _triage([])), \
             mock.patch.object(monitor, "_competitor_arm", side_effect=AssertionError("must not run")), \
             mock.patch.object(monitor, "_my_company_facts", side_effect=AssertionError("must not run")), \
             mock.patch.object(monitor, "propagate", side_effect=AssertionError("must not run")), \
             mock.patch("scout.audience.refresh", self.audience_fake(calls)):
            res = monitor.check(SLUG, write=True)
        self.assertTrue(res["no_change"])
        self.assert_no_failed(res)
        rows = self.rows(res)
        self.assertEqual(rows["triage"]["status"], "ran")
        for name in ("materiality", "own_company", "propagation", "audience"):
            self.assertEqual(rows[name]["status"], "skipped", name)
        self.assertEqual(rows["write"]["status"], "ran")
        self.assertEqual(calls, [], "the quiet path spends nothing on audience leads")
        # the archive card sat at attempt 2 of 3: this hold is the third -> the window abandons, loudly
        self.assertIn("abandoned_window", res)
        meta = _load(os.path.join(self.card, "meta.json"))
        self.assertNotIn("unresolved_since", meta)
        self.assertEqual(meta["last_checked"], res["last_checked"])
        self.assertEqual(len(self.alerts_on_disk()), n_alerts0)
        self.assertEqual(_load(os.path.join(self.card, "claims.json")), self.claims0)


class EscalatedNoActFact(_Live):
    """The 10/3 crash branch: a competitor development lands as WATCH, propagation has nothing act-grade,
    and the audience step runs with a name that exists."""

    def test_watch_alert_lands_and_audience_runs_with_checked_at(self):
        fact, alert = self.fact("watch")
        calls = []
        with mock.patch.object(monitor, "_run_triage", _triage([{"signal": "Series F", "subject_key": FACT_SK, "about": "competitor",
                                                                  "valence": "back_foot", "substantial": True}])), \
             mock.patch.object(monitor, "_competitor_arm", self.arm([(fact, alert)])), \
             mock.patch.object(monitor, "_my_company_facts", side_effect=AssertionError("no own-side candidate")), \
             mock.patch.object(monitor, "propagate", side_effect=AssertionError("no act-grade fact: propagation must not run")), \
             mock.patch("scout.audience.refresh", self.audience_fake(calls)):
            res = monitor.check(SLUG, write=True)
        self.assert_no_failed(res)
        rows = self.rows(res)
        self.assertEqual(rows["materiality"]["status"], "ran")
        self.assertEqual(rows["propagation"]["status"], "skipped")
        self.assertEqual(rows["audience"]["status"], "ran")
        self.assertNotIn("audience_error", res)
        self.assertNotIn("propagation", res)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["today"], res["last_checked"][:10])        # the 10/3 bug: `today` did not exist here
        self.assertTrue(calls[0]["write"])
        claims = _load(os.path.join(self.card, "claims.json"))
        self.assertIn("$60B", {c["subject_key"]: c for c in claims}[FACT_SK]["claim"])
        new_alerts = self.alerts_on_disk()[-1:]
        self.assertEqual(len(res["alerts"]), 1)
        self.assertEqual(set(new_alerts[0]) >= TEN_KEYS, True, new_alerts[0].keys())
        self.assertEqual(new_alerts[0]["severity"], "watch")
        meta = _load(os.path.join(self.card, "meta.json"))
        self.assertIn(new_alerts[0]["fingerprint"], meta["alerted_fingerprints"])
        self.assertIn(res["last_checked"], meta["last_checked"])


class EscalatedActFact(_Live):
    def test_confirmed_revise_is_applied_through_the_provenance_gate(self):
        fact, alert = self.fact("act")
        calls = []
        with mock.patch.object(monitor, "_run_triage", _triage([{"signal": "Series F", "subject_key": FACT_SK, "about": "competitor",
                                                                  "valence": "back_foot", "substantial": True}])), \
             mock.patch.object(monitor, "_competitor_arm", self.arm([(fact, alert)])), \
             mock.patch.object(monitor, "_my_company_facts", side_effect=AssertionError("no own-side candidate")), \
             mock.patch.object(monitor, "propagate", self.confirmed_revise(fact)), \
             mock.patch("scout.audience.refresh", self.audience_fake(calls)):
            res = monitor.check(SLUG, write=True)
        self.assert_no_failed(res)
        rows = self.rows(res)
        self.assertEqual(rows["propagation"]["status"], "ran")
        self.assertNotIn("propagation_error", res)
        prop = res["propagation"]
        self.assertEqual(prop["provenance_issues"] if "provenance_issues" in prop else [], [])
        self.assertEqual([a["subject_key"] for a in prop["applied"]], [TARGET_SK])
        claims = {c["subject_key"]: c for c in _load(os.path.join(self.card, "claims.json"))}
        self.assertIn("The $60B round widens that gap.", claims[TARGET_SK]["claim"])
        self.assertEqual(claims[TARGET_SK]["derived_from"], fact["id"])
        self.assertEqual(claims[TARGET_SK]["provenance"]["source_url"], fact["source_url"])
        self.assertEqual(len(calls), 1)                                          # audience ran after the apply
        self.assertEqual(res["alerts"][0]["severity"], "act")
        self.assertAlmostEqual(res["cost"]["propagation"], 0.6)


class OwnCompanyOnly(_Live):
    def test_anchor_fact_lands_under_tracked_facts(self):
        from scout.schema import ANCHOR_SECTION, claim_id
        sk = "cursor | funding | current"
        anchor = {"id": claim_id(SLUG, sk), "subject_key": sk, "claim": "Cursor raised a $2B round on October 1, 2026.",
                  "claim_type": "fact", "section": ANCHOR_SECTION, "zone": None, "order": 0,
                  "source_url": "https://cursor.com/blog/series-e", "source_tier": "primary",
                  "evidence_excerpt": "Cursor today announced a $2B round of financing at a $60B valuation.",
                  "as_of": "2026-10-01", "verified": True, "confidence": "high",
                  "grounding": {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-10-02"}}
        alert = {"severity": "act", "old_value": None, "new_value": "$2B round", "headline": "Cursor raises $2B", "so_what": "Runway objection retires."}
        seen = {}

        def fake_my(slug, meta, my_since, my_substantial, claims):
            seen["my_since"] = my_since
            return {"grounded": [(anchor, alert)], "cost": 0.4}

        def fake_prop(meta, facts_with_alerts, strength_facts, claims, **k):
            seen["trigger_ids"] = [p["fact"]["id"] for p in facts_with_alerts]
            return {"ops": [], "surface_ops": [], "no_surface": ["noted"], "no_change": ["noted"], "run_verdict": {},
                    "floor_results": [], "verdicts": {}, "confirmed": [], "decisions": [], "cost_usd": {"route": 0.1}}
        calls = []
        with mock.patch.object(monitor, "_run_triage", _triage([{"signal": "Cursor raises $2B", "subject_key": "NEW", "about": "my_company",
                                                                  "valence": "front_foot", "substantial": True}])), \
             mock.patch.object(monitor, "_competitor_arm", side_effect=AssertionError("no competitor candidate")), \
             mock.patch.object(monitor, "_my_company_facts", fake_my), \
             mock.patch.object(monitor, "propagate", fake_prop), \
             mock.patch("scout.audience.refresh", self.audience_fake(calls)):
            res = monitor.check(SLUG, write=True)
        self.assert_no_failed(res)
        rows = self.rows(res)
        self.assertEqual(rows["materiality"]["status"], "skipped")
        self.assertEqual(rows["own_company"]["status"], "ran")
        self.assertEqual(rows["propagation"]["status"], "ran")
        self.assertEqual(seen["trigger_ids"], [anchor["id"]])
        self.assertEqual(seen["my_since"], self.meta0["last_checked"][:10])  # the own-side cutoff is the last check, never the held window
        claims = {c["subject_key"]: c for c in _load(os.path.join(self.card, "claims.json"))}
        self.assertEqual(claims[sk]["section"], ANCHOR_SECTION)
        self.assertAlmostEqual(res["cost"]["my_company"], 0.4)
        self.assertEqual(len(res["alerts"]), 1)


class FailedArmCarryOver(_Live):
    """A paid arm that dies (its budget, an SDK error) no longer takes the check down or re-bills the
    detection: its candidates ride meta.pending_candidates to the next run, the spend is recorded."""

    def test_materiality_crash_is_carried_not_retried(self):
        class Boom(RuntimeError):
            pass
        err = Boom("budget exhausted")
        err.scout_cost_usd = 1.37
        calls = []
        with mock.patch.object(monitor, "_run_triage", _triage([{"signal": "Series F", "subject_key": FACT_SK, "about": "competitor",
                                                                  "valence": "back_foot", "substantial": True}])), \
             mock.patch.object(monitor, "_competitor_arm", side_effect=err), \
             mock.patch.object(monitor, "propagate", side_effect=AssertionError("nothing grounded")), \
             mock.patch("scout.audience.refresh", self.audience_fake(calls)):
            res = monitor.check(SLUG, write=True)                               # does NOT raise
        rows = self.rows(res)
        self.assertEqual(rows["materiality"]["status"], "failed")
        self.assertIn("carried", rows["materiality"]["detail"])
        self.assertAlmostEqual(rows["materiality"]["cost"], 1.37)
        self.assertAlmostEqual(res["cost"]["materiality"], 1.37)                 # the failed spend reaches the ledger
        self.assertEqual(res["carry_over"], {"competitor": 1})
        meta = _load(os.path.join(self.card, "meta.json"))
        self.assertEqual([c["signal"] for c in meta["pending_candidates"]["competitor"]], ["Series F"])
        self.assertEqual(meta["pending_candidates"]["attempts"], 1)
        self.assertEqual(meta["unresolved_attempts"], 2, "a failed arm does not spend a hold-window attempt")
        # next morning: triage quiet, the carried candidate goes straight to the arm and lands
        fact, alert = self.fact("watch")
        arm = mock.Mock(side_effect=self.arm([(fact, alert)]))
        with mock.patch.object(monitor, "_run_triage", _triage([])), \
             mock.patch.object(monitor, "_competitor_arm", arm), \
             mock.patch("scout.audience.refresh", self.audience_fake(calls)):
            res2 = monitor.check(SLUG, write=True)
        self.assertEqual(arm.call_args.args[3][0]["signal"], "Series F")
        self.assert_no_failed(res2)
        self.assertEqual(self.rows(res2)["carry_over"]["status"], "ran")
        self.assertNotIn("pending_candidates", _load(os.path.join(self.card, "meta.json")))
        self.assertEqual(len(res2["alerts"]), 1)

    def test_own_side_candidates_that_do_not_ground_get_one_more_attempt(self):
        def fake_my(slug, meta, my_since, my_substantial, claims):
            return {"grounded": [], "cost": 0.4}
        cand = {"signal": "Cursor cuts Pro usage limits", "subject_key": "NEW", "about": "my_company", "valence": "back_foot", "substantial": True}
        with mock.patch.object(monitor, "_run_triage", _triage([cand])), \
             mock.patch.object(monitor, "_my_company_facts", fake_my), mock.patch.object(monitor, "propagate", side_effect=AssertionError("nothing grounded")), \
             mock.patch("scout.audience.refresh", self.audience_fake([])):
            res = monitor.check(SLUG, write=True)
        meta = _load(os.path.join(self.card, "meta.json"))
        self.assertEqual([c["signal"] for c in meta["pending_candidates"]["my_company"]], [cand["signal"]])
        self.assertEqual(meta["pending_candidates"]["reason"], "own-side candidates did not ground")
        self.assertEqual(meta["unresolved_attempts"], 2, "the competitor window is not touched by an own-side miss")
        # second morning: the arm grounds nothing again -> dropped, recorded, no third attempt
        with mock.patch.object(monitor, "_run_triage", _triage([])), mock.patch.object(monitor, "_my_company_facts", fake_my), \
             mock.patch.object(monitor, "propagate", side_effect=AssertionError("nothing grounded")), mock.patch("scout.audience.refresh", self.audience_fake([])):
            res2 = monitor.check(SLUG, write=True)
        self.assertEqual(self.rows(res2)["carry_over"]["status"], "ran")
        self.assertNotIn("pending_candidates", _load(os.path.join(self.card, "meta.json")))

    def test_carry_over_abandons_loudly_at_the_bound(self):
        meta = _load(os.path.join(self.card, "meta.json"))
        meta["pending_candidates"] = {"competitor": [{"signal": "Series F", "subject_key": FACT_SK, "about": "competitor", "substantial": True}],
                                      "since": "2026-10-01", "my_since": "2026-10-01", "attempts": config.MONITOR_MAX_UNRESOLVED_RETRIES - 1,
                                      "reason": "failed step"}
        with open(os.path.join(self.card, "meta.json"), "w") as f:
            json.dump(meta, f)
        with mock.patch.object(monitor, "_run_triage", _triage([])), \
             mock.patch.object(monitor, "_competitor_arm", side_effect=RuntimeError("down again")), \
             mock.patch("scout.audience.refresh", self.audience_fake([])):
            res = monitor.check(SLUG, write=True)
        self.assertEqual(res["abandoned_carry_over"], ["Series F"])
        self.assertNotIn("pending_candidates", _load(os.path.join(self.card, "meta.json")))
        self.assertEqual(self.rows(res)["carry_over"]["status"], "failed")


class RunCeiling(_Live):
    def test_ceiling_defers_escalation_and_carries_candidates(self):
        with mock.patch.object(monitor, "_run_triage", _triage([{"signal": "Series F", "subject_key": FACT_SK, "about": "competitor",
                                                                  "valence": "back_foot", "substantial": True}])), \
             mock.patch.object(config, "RUN_MAX_USD", 15.0), \
             mock.patch.object(monitor, "_competitor_arm", side_effect=AssertionError("ceiling: must not run")), \
             mock.patch("scout.audience.refresh", self.audience_fake([])):
            res = monitor.check(SLUG, write=True, escalate=False)
        rows = self.rows(res)
        self.assertEqual(rows["ceiling"]["status"], "skipped")
        self.assertEqual(rows["materiality"]["status"], "skipped")
        self.assertEqual(res["ceiling_deferred"], ["Series F"])
        meta = _load(os.path.join(self.card, "meta.json"))
        self.assertEqual(meta["pending_candidates"]["reason"], "run ceiling")
        self.assertEqual(meta["last_checked"], res["last_checked"])


class RunAllReportsSteps(unittest.TestCase):
    """A failed step is a needs-you item and the FYI footer carries every card's health."""

    def test_failed_step_reaches_the_needs_you_email_and_the_ceiling_trips(self):
        res_ok = {"slug": "a", "alerts": [{"headline": "h", "subject_key": "s", "old_value": "1", "new_value": "2"}], "material": [],
                  "cost": {"triage": 0.1, "materiality": 9.0}, "no_change": False, "last_checked": "t",
                  "steps": [{"step": "triage", "status": "ran"}, {"step": "audience", "status": "failed", "detail": "NameError: today"}],
                  "propagation": {"decisions": [], "applied": [], "held": [], "skipped": [], "provenance_issues": [], "gated": None, "run_verdict": None}}
        res_b = {"slug": "b", "alerts": [], "material": [], "cost": {"triage": 0.1}, "no_change": True, "last_checked": "t",
                 "steps": [{"step": "triage", "status": "ran"}, {"step": "ceiling", "status": "skipped", "detail": "run ceiling"}],
                 "ceiling_deferred": ["x"]}
        seen = {}

        def fake_check(slug, write=False, **kw):
            seen[slug] = kw
            return res_ok if slug == "a" else res_b
        sent = {}
        with mock.patch("scout.display.list_battlecards", return_value=["a", "b"]), \
             mock.patch.object(monitor, "check", side_effect=fake_check), \
             mock.patch.object(monitor.store, "load_meta", return_value={"monitored": True, "competitor": "X", "my_company": "Y"}), \
             mock.patch.object(monitor, "_persist_run_cost"), mock.patch("scout.conseq.maybe_notify_ready"), \
             mock.patch.object(config, "PROPAGATE_MODE", "live"), mock.patch.object(config, "CONSEQUENTIAL_FILTER", "off"), \
             mock.patch.object(config, "RUN_MAX_USD", 5.0), mock.patch.object(config, "REHEARSAL", False), \
             mock.patch("scout.notify.send_run_fyi", side_effect=lambda cards, cost, dry_run=True, **kw: sent.__setitem__("fyi", (cards, cost, kw)) or {"sent": True}), \
             mock.patch("scout.notify.send_run_issues", side_effect=lambda cards, dry_run=True: sent.__setitem__("issues", cards) or {"sent": True}):
            monitor._run_all_impl(write=False, send=True, email_dry_run=True, force=True)
        self.assertEqual(seen["a"], {})
        self.assertEqual(seen["b"], {"escalate": False})                          # card a spent $9.1 > the $5 ceiling
        issues = {c["slug"]: c for c in sent["issues"]}
        self.assertIn("audience: NameError: today", issues["a"]["errors"])
        self.assertTrue(any("run ceiling" in e for e in issues["b"]["errors"]))
        cards, cost, kw = sent["fyi"]
        health = kw["health"]
        self.assertEqual([h["slug"] for h in health], ["a", "b"])
        self.assertEqual(health[0]["steps"][1]["status"], "failed")

    def test_double_failure_carries_the_steps_to_the_needs_you_email(self):
        def boom(slug, write=False, **kw):
            e = RuntimeError("store down")
            e.scout_steps = [{"step": "triage", "status": "failed", "detail": "RuntimeError: store down"}]
            raise e
        sent = {}
        with mock.patch("scout.display.list_battlecards", return_value=["a"]), \
             mock.patch.object(monitor, "check", side_effect=boom), \
             mock.patch.object(monitor.store, "load_meta", return_value={"monitored": True}), \
             mock.patch.object(monitor, "_persist_run_cost"), mock.patch("scout.conseq.maybe_notify_ready"), \
             mock.patch.object(config, "PROPAGATE_MODE", "live"), mock.patch.object(config, "REHEARSAL", False), \
             mock.patch("scout.notify.send_run_fyi", return_value={"sent": False}), \
             mock.patch("scout.notify.send_run_issues", side_effect=lambda cards, dry_run=True: sent.__setitem__("issues", cards) or {"sent": True}):
            out = monitor._run_all_impl(write=False, send=True, email_dry_run=True, force=True)
        self.assertIn("error", out[0])
        errs = sent["issues"][0]["errors"]
        self.assertTrue(any(e.startswith("check failed twice") for e in errs))
        self.assertIn("triage: RuntimeError: store down", errs)


if __name__ == "__main__":
    unittest.main()


class StoreRootAndDryGate(unittest.TestCase):
    def test_store_root_follows_the_environment(self):
        import importlib, subprocess, sys
        code = ("import os; os.environ['SCOUT_STORE_ROOT']='/tmp/scout-rehearsal-x/battlecards'; "
                "from scout import store; print(store.STORE_ROOT)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=config.APP_ROOT)
        self.assertEqual(out.stdout.strip(), "/tmp/scout-rehearsal-x/battlecards", out.stderr[-300:])
        code2 = ("import os; os.environ['SCOUT_STORE_ROOT']=''; from scout import store, config; "
                 "print(store.STORE_ROOT == os.path.join(config.APP_ROOT, 'battlecards'))")
        out2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True, cwd=config.APP_ROOT)
        self.assertEqual(out2.stdout.strip(), "True", out2.stderr[-300:])

    def test_a_dry_propagation_holds_nothing_and_emails_nothing(self):
        from scout import reformat
        with mock.patch.object(reformat.selfserve, "write_data", side_effect=AssertionError("dry hold wrote")), \
             mock.patch.object(reformat, "_alert_human", side_effect=AssertionError("dry hold emailed")):
            tok = reformat.DRY.set(True)
            try:
                path = reformat.hold("slug", {"claim": "x"}, "test")
            finally:
                reformat.DRY.reset(tok)
        self.assertIn("pending_publish/slug/", path)
        self.assertFalse(reformat.DRY.get())


class CatchUpTriage(unittest.TestCase):
    """A stale window earns more searches (the 19-day first check on the promoted card, 2026-10-03)."""

    def test_budget_scales_with_the_window_and_the_focus(self):
        from scout import config as c
        day = "2026-10-04T01:00:00"
        self.assertEqual(monitor._triage_budget("2026-10-03", day), (c.TRIAGE_MAX_SEARCHES, c.TRIAGE_MAX_TURNS, c.TRIAGE_MAX_BUDGET_USD, 0))
        self.assertEqual(monitor._triage_budget("2026-10-01", day), (c.TRIAGE_MAX_SEARCHES, c.TRIAGE_MAX_TURNS, c.TRIAGE_MAX_BUDGET_USD, 0))
        s, t, b, f = monitor._triage_budget("2026-09-15", day)              # 19 days: 5 + 2 * 3 weeks
        self.assertEqual((s, t, b, f), (11, 15, max(c.TRIAGE_MAX_BUDGET_USD, c.TRIAGE_CATCHUP_BUDGET_USD), 0))
        self.assertEqual(monitor._triage_budget("2026-06-01", day)[0], c.TRIAGE_CATCHUP_MAX_SEARCHES)
        self.assertEqual(monitor._triage_budget(None, day)[0], c.TRIAGE_MAX_SEARCHES)
        # a FOCUSED card: extra searches reserved for the focus area, on a normal day and on a catch-up
        s, t, b, f = monitor._triage_budget("2026-10-03", day, focused=True)
        self.assertEqual((s, t, f), (c.TRIAGE_MAX_SEARCHES + c.TRIAGE_FOCUS_SEARCHES, c.TRIAGE_MAX_SEARCHES + c.TRIAGE_FOCUS_SEARCHES + 4, c.TRIAGE_FOCUS_SEARCHES))
        self.assertGreaterEqual(b, 0.75)
        s, t, b, f = monitor._triage_budget("2026-09-15", day, focused=True)
        self.assertEqual(s, 11 + c.TRIAGE_FOCUS_SEARCHES); self.assertGreaterEqual(f, 6)   # half of a catch-up goes to the area

    def test_focused_card_gets_both_scopes_in_every_paid_step(self):
        """2026-10-03: the daily check never received the card's focus; its five searches on the
        collaboration-agents card were all corporate news and missed DevDay's agent updates."""
        meta = {"competitor": "Anthropic", "my_company": "OpenAI", "focus": "Enterprise collaboration agents (inside Slack and Teams)"}
        with mock.patch.object(monitor.judgment, "optional", side_effect=lambda name, subs=None: f"FOCUS NOTE {subs['focus']} / {subs['focus_searches']}" if name == "monitor._FOCUS_NOTE" else None):
            self.assertIn("FOCUS NOTE Enterprise collaboration agents (inside Slack and Teams) / 3", monitor._focus_note(meta, 3))
            self.assertEqual(monitor._focus_note({"competitor": "A", "focus": "General"}, 3), "")
            self.assertEqual(monitor._focus_note({"competitor": "A"}, 3), "")
        # the fallback when the pack lacks the block still names the focus and both scopes
        with mock.patch.object(monitor.judgment, "optional", return_value=None):
            n = monitor._focus_note(meta, 3)
            self.assertIn("FOCUS AREA: Enterprise collaboration agents", n); self.assertIn("corporate developments are both material", n)

    def test_catch_up_renders_the_block_with_the_larger_count_and_records_a_step(self):
        seen = {}

        async def fake_drive(user, options, role):
            seen["system"] = options.system_prompt["append"]; seen["turns"] = options.max_turns; seen["budget"] = options.max_budget_usd
            return {"text": json.dumps({"has_candidates": False, "candidates": []}), "cost_usd": 0.3}
        with mock.patch.object(monitor, "_drive", fake_drive), \
             mock.patch.object(monitor.judgment, "text", return_value="TRIAGE BLOCK WITH 11 SEARCHES") as jt, \
             mock.patch.object(monitor.store, "load_meta", return_value={"competitor": "A", "my_company": "B", "last_checked": "2026-09-15T10:00:00", "baseline_date": "2026-09-15"}), \
             mock.patch.object(monitor.store, "load_claims", return_value=[]), mock.patch.object(monitor.store, "write_baseline"), \
             mock.patch.object(monitor, "_current_md", return_value="# c"), mock.patch.object(config, "SIGNALS_ENABLED", False), \
             mock.patch.object(config, "PROPAGATE_MODE", "off"), mock.patch.object(config, "SENSORS_MODE", "off"):
            res = monitor.check("card-x", write=True)
        # the window is "since 2026-09-15 until now": the search count grows with the calendar up to the
        # catch-up cap, so the assertions follow the budget function rather than a frozen number
        exp_s, exp_t, exp_b, _ = monitor._triage_budget("2026-09-15", datetime.now().isoformat(timespec="seconds"), focused=False)
        self.assertIn("11 SEARCHES", seen["system"]); self.assertEqual(seen["turns"], exp_t); self.assertEqual(seen["budget"], exp_b)
        self.assertEqual(jt.call_args.args[1]["config.TRIAGE_MAX_SEARCHES"], exp_s)
        self.assertEqual({r["step"]: r for r in res["steps"]}["triage_budget"]["status"], "ran")
