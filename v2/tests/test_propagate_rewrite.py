"""Rewrite-loop invariants (the 2026-07-01 Sonnet-5 silent drop): a judge-rejected op whose defect
is PROSE-ONLY gets one guided rewrite (judge reason as the fix list, upgraded writer) and a BLIND
re-judge; exhaustion surfaces loudly (proposals email) — a material edit is never dropped silently.
All model calls are mocked (no network, no spend). Run from v2/:

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import config, notify, propagate
from scout.propagate import (_decision_records, _parse_verdicts, _rewritable_indices,
                             _rewrite_worklist, _active_targets)


CLAIMS = [
    {"subject_key": "openai|flagship-model|current", "section": "battlecard",
     "zone": "where_they_win", "status": "active", "claim": "They lead on the frontier model."},
]
FACT_ID = "c_aaaaaaaaaaaa"
FACT = {"id": FACT_ID, "subject_key": "anthropic|sonnet-5-launch", "claim": "Sonnet 5 shipped.",
        "source_url": "https://example.com/launch", "about": "my_company", "valence": "front_foot"}
ACTIVE = _active_targets(CLAIMS)

SURFACE_OP = {"operation": "revise", "section": "battlecard", "zone": "where_they_win",
              "change_kind": "update", "valence": "front_foot",
              "target_subject_key": "openai|flagship-model|current",
              "subject_key": "openai|flagship-model|current",
              "derived_from": FACT_ID, "feed_note": "note", "persona": None, "why": "pricing moved"}


def _fake(payload):
    """An async model-call fake returning the _drive shape with a fenced-JSON payload."""
    async def call(*args, **kwargs):
        return {"text": "```json\n" + json.dumps(payload) + "\n```", "cost_usd": 0.0}
    return call


def _route_fake(surface_ops):
    return lambda meta, fwa, claims: {"surface_ops": surface_ops, "no_surface": [],
                                      "run_verdict": {}, "cost_usd": 0.0, "raw": ""}


def _authored(prose, i=0):
    return {"authored": [{"op_index": i, "claim": prose, "persona": None}]}


def _verdict(verdict, reason, rewritable=False, i=0):
    return {"verdicts": [{"op_index": i, "verdict": verdict, "reason": reason,
                          "rewritable": rewritable}]}


def _run_propagate():
    return propagate.propagate({"competitor": "OpenAI", "my_company": "Anthropic"},
                               [{"fact": FACT, "alert": {"severity": "act"}}], [],
                               CLAIMS, slug=None, persist=False)


class ParseRewritable(unittest.TestCase):
    def _one(self, payload):
        return _parse_verdicts("```json\n" + json.dumps(payload) + "\n```")[0]

    def test_missing_defaults_false(self):
        v = self._one({"verdicts": [{"op_index": 0, "verdict": "reject", "reason": "r"}]})
        self.assertFalse(v["rewritable"])                # fail-closed: no accidental loops

    def test_only_boolean_true_counts(self):
        for bad in ("true", 1, "yes"):
            v = self._one(_verdict("reject", "r", rewritable=bad))
            self.assertFalse(v["rewritable"], f"non-boolean {bad!r} must not enter the loop")
        self.assertTrue(self._one(_verdict("reject", "r", rewritable=True))["rewritable"])


def _mat(cure="prose"):                       # a material, curable reject verdict (loop-eligible)
    return {"verdict": "reject", "reason": "r", "material": True, "cure": cure}


class RewritableSelection(unittest.TestCase):
    def test_retire_never_rewritable(self):
        ops = [{"operation": "retire", "claim": None}]
        self.assertEqual(_rewritable_indices(ops, [[]], {0: _mat()}), [])   # retire guard, even if material

    def test_floor_rejected_never_enters_loop(self):
        ops = [{"operation": "revise", "claim": "x"}]
        self.assertEqual(_rewritable_indices(ops, [["bad structure"]], {0: _mat()}), [])

    def test_selects_material_curable_rejects_only(self):
        ops = [{"operation": "revise", "claim": "a"}, {"operation": "add", "claim": "b"},
               {"operation": "add", "claim": "c"}, {"operation": "add", "claim": "d"},
               {"operation": "add", "claim": "e"}]
        vs = {0: _mat("prose"),                                  # material+prose -> in
              1: _mat("root"),                                   # material+root  -> in
              2: {"verdict": "reject", "reason": "r", "material": False, "cure": "none"},  # immaterial -> out
              3: {"verdict": "reject", "reason": "r", "material": True, "cure": "none"},   # cure:none -> out (urgent, not loop)
              4: {"verdict": "confirm", "reason": "ok", "material": False}}                # confirm -> out
        self.assertEqual(_rewritable_indices(ops, [[]] * 5, vs), [0, 1])

    def test_worklist_carries_feedback_and_cure(self):
        ops = [{"operation": "revise", "claim": "the failed prose"}]
        vs = {0: {"verdict": "reject", "reason": "wrong pivot", "material": True, "cure": "root"}}
        wl = _rewrite_worklist([0], [SURFACE_OP], ops, vs, ACTIVE)
        self.assertEqual(len(wl), 1)
        self.assertEqual(wl[0]["op_index"], 0)
        self.assertEqual(wl[0]["failed_prose"], "the failed prose")
        self.assertEqual(wl[0]["judge_reason"], "wrong pivot")
        self.assertEqual(wl[0]["cure"], "root")               # branches the addendum
        self.assertEqual(wl[0]["current_text"], "They lead on the frontier model.")


class RewriteLoop(unittest.TestCase):
    def setUp(self):
        self._saved = config.PROPAGATE_MAX_REWRITES
        config.PROPAGATE_MAX_REWRITES = 1

    def tearDown(self):
        config.PROPAGATE_MAX_REWRITES = self._saved

    def test_rewrite_confirm_flows_to_confirmed_with_final_prose(self):
        judge_calls = []

        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            judge_calls.append(indexed_ops)
            verdict = (_verdict("reject", "invented number: 'a fifth of the cost'", rewritable=True)
                       if len(judge_calls) == 1 else _verdict("confirm", "cured"))
            return {"text": "```json\n" + json.dumps(verdict) + "\n```", "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad prose $1/5"))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", _fake(_authored("honest prose"))):
            res = _run_propagate()

        self.assertEqual(len(res["confirmed"]), 1)
        self.assertEqual(res["confirmed"][0]["claim"], "honest prose")
        self.assertEqual(len(judge_calls), 2)
        # The re-judge saw ONLY the rewritten op, at its ORIGINAL index, and is BLIND: the op dict
        # carries no judge_reason / attempts / rewrite marker.
        self.assertEqual([i for i, _ in judge_calls[1]], [0])
        rejudged_op = judge_calls[1][0][1]
        for k in ("judge_reason", "attempts", "rewrite", "failed_prose"):
            self.assertNotIn(k, rejudged_op)
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "confirm")
        self.assertEqual(rec["rewrite_attempts"], 1)
        self.assertFalse(rec["rewrite_exhausted"])
        self.assertEqual([a["verdict"] for a in rec["attempts"]], ["reject", "confirm"])

    def test_bound_exhausts_loudly(self):
        rewrite_calls = []

        async def rewrite_fake(meta, worklist, facts):
            rewrite_calls.append(worklist)
            return {"text": "```json\n" + json.dumps(_authored("still bad")) + "\n```",
                    "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad"))), \
             mock.patch.object(propagate, "_run_judge",
                               _fake(_verdict("reject", "invented", rewritable=True))), \
             mock.patch.object(propagate, "_run_rewrite", rewrite_fake):
            res = _run_propagate()

        self.assertEqual(len(rewrite_calls), 1)          # the bound: exactly one rewrite round
        self.assertEqual(res["confirmed"], [])
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "reject")
        self.assertEqual(rec["rewrite_attempts"], 1)
        self.assertTrue(rec["rewrite_exhausted"])
        self.assertEqual(len(rec["attempts"]), 2)        # original + the failed rewrite
        self.assertTrue(all(a["reason"] for a in rec["attempts"]))

    def test_honest_empty_claim_is_terminal_reject_without_rejudge(self):
        judge_calls = []

        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            judge_calls.append(indexed_ops)
            return {"text": "```json\n"
                    + json.dumps(_verdict("reject", "invented", rewritable=True)) + "\n```",
                    "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad"))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", _fake(_authored(""))):
            res = _run_propagate()

        self.assertEqual(len(judge_calls), 1)            # no re-judge for an uncurable op
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "reject")
        self.assertTrue(rec["rewrite_exhausted"])
        self.assertIn("floor", rec["attempts"][-1]["reason"])
        self.assertEqual(res["floor_results"][0], [])    # top-level floor result NOT overwritten
        self.assertNotEqual(rec["judge_verdict"], "floor_reject")

    def test_knob_zero_is_todays_behavior(self):
        config.PROPAGATE_MAX_REWRITES = 0
        rewrite = mock.MagicMock()
        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad"))), \
             mock.patch.object(propagate, "_run_judge",
                               _fake(_verdict("reject", "invented", rewritable=True))), \
             mock.patch.object(propagate, "_run_rewrite", rewrite):
            res = _run_propagate()
        rewrite.assert_not_called()
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "reject")
        self.assertEqual(rec["attempts"], [])
        self.assertEqual(rec["rewrite_attempts"], 0)
        self.assertFalse(rec["rewrite_exhausted"])       # never looped != exhausted

    def test_rewrite_crash_degrades_not_raises(self):
        async def boom(*a, **k):
            raise RuntimeError("rewrite model fell over")

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad"))), \
             mock.patch.object(propagate, "_run_judge",
                               _fake(_verdict("reject", "invented", rewritable=True))), \
             mock.patch.object(propagate, "_run_rewrite", boom):
            res = _run_propagate()                       # must not raise
        self.assertEqual(res["confirmed"], [])
        self.assertEqual(res["decisions"][0]["judge_verdict"], "reject")


class DecisionRecordShape(unittest.TestCase):
    def test_additive_fields_on_never_looped_ops(self):
        ops = [{"operation": "revise", "section": "battlecard", "zone": "where_they_win",
                "valence": "front_foot", "change_kind": "update",
                "target_subject_key": "openai|flagship-model|current",
                "subject_key": "openai|flagship-model|current", "claim": "x",
                "claim_type": "interpretation", "derived_from": FACT_ID, "feed_note": None}]
        recs = _decision_records(ops, [[]], {0: {"verdict": "confirm", "reason": "ok"}},
                                 {FACT_ID: FACT}, ACTIVE)
        self.assertEqual(recs[0]["attempts"], [])
        self.assertEqual(recs[0]["rewrite_attempts"], 0)
        self.assertFalse(recs[0]["rewrite_exhausted"])
        self.assertEqual(recs[0]["judge_verdict"], "confirm")   # pre-existing fields intact
        self.assertTrue(recs[0]["committed"])

    def test_schema_version_bumped(self):
        self.assertEqual(propagate.SCHEMA_VERSION, 7)


class ExhaustedEmail(unittest.TestCase):
    META = {"my_company": "Anthropic", "competitor": "OpenAI"}
    EXHAUSTED = [{"operation": "revise", "section": "pricing", "zone": None,
                  "change_kind": "update", "subject_key": "anthropic|api-list-price|current",
                  "trigger_source_url": "https://example.com/launch",
                  "attempts": [{"claim": "bad $1/5", "verdict": "reject",
                                "reason": "invented number", "rewritable": True},
                               {"claim": "still bad", "verdict": "reject",
                                "reason": "new invention", "rewritable": False}],
                  "rewrite_attempts": 1, "rewrite_exhausted": True}]

    def test_exhausted_only_email_sends(self):
        out = notify.send_propagation_proposals("slug", self.META, [], dry_run=True,
                                                exhausted=self.EXHAUSTED)
        self.assertNotEqual(out.get("reason"), "no proposals")  # got past the guard
        self.assertIn("authoring-failed", out["subject"])

    def test_both_empty_still_noop(self):
        out = notify.send_propagation_proposals("slug", self.META, [], dry_run=True, exhausted=[])
        self.assertEqual(out, {"sent": False, "reason": "no proposals"})

    def test_render_exhausted_section(self):
        subject, body = notify.render_propagation_proposals("slug", self.META, [],
                                                            exhausted=self.EXHAUSTED)
        self.assertIn("(+1 authoring-failed)", subject)
        self.assertIn("NOT applied", body)
        self.assertIn("invented number", body)           # every attempt's judge reason
        self.assertIn("new invention", body)
        self.assertIn("still bad", body)                 # the full last-attempt prose
        self.assertIn("attempt 2 (rewrite)", body)

    def test_confirmed_footer_survives_exhausted(self):
        confirmed = [{"operation": "revise", "section": "battlecard", "zone": None,
                      "subject_key": "s", "old_text": "a", "new_text": "b",
                      "judge_reason": "ok", "trigger_source_url": None, "feed_note": None}]
        _, body = notify.render_propagation_proposals("slug", self.META, confirmed,
                                                      exhausted=self.EXHAUSTED)
        self.assertIn("To publish, tell Claude", body)   # approval footer intact alongside failures


class LengthCureLoop(unittest.TestCase):
    """Step 4b2 (2026-07-25, the 182-word hold): the judge certifies substance, code certifies
    length. A confirmed-but-over-cap op gets a dedicated condense round + blind re-judge; an
    uncured op is RESTORED to its confirmed state (never an exhausted reject)."""

    LONG = "**Play**\n\n" + ("beat " * 185) + "\n\n**Soundbite:** zinger."
    SHORT = "**Play**\n\nCurrent state, every number kept.\n\n**Soundbite:** zinger."

    def setUp(self):
        self._saved = (config.PROPAGATE_MAX_REWRITES, config.PROPAGATE_MAX_LENGTH_CURES)
        config.PROPAGATE_MAX_REWRITES = 1
        config.PROPAGATE_MAX_LENGTH_CURES = 1

    def tearDown(self):
        config.PROPAGATE_MAX_REWRITES, config.PROPAGATE_MAX_LENGTH_CURES = self._saved

    def test_demote_selects_only_confirmed_overcap_prose_ops(self):
        from scout.propagate import _demote_overcap_confirms
        long_claim = "w " * 200
        ops = [{"operation": "revise", "section": "battlecard", "claim": long_claim},   # demote
               {"operation": "revise", "section": "battlecard", "claim": "short"},      # under cap
               {"operation": "retire", "section": "battlecard", "claim": None},         # no prose
               {"operation": "revise", "section": "battlecard", "claim": long_claim},   # rejected
               {"operation": "revise", "section": "recent_moves", "claim": long_claim}] # exempt
        verdicts = {0: {"verdict": "confirm", "reason": "ok"},
                    1: {"verdict": "confirm", "reason": "ok"},
                    2: {"verdict": "confirm", "reason": "ok"},
                    3: {"verdict": "reject", "reason": "no"},
                    4: {"verdict": "confirm", "reason": "ok"}}
        demoted = _demote_overcap_confirms(ops, [[], [], [], [], []], verdicts)
        self.assertEqual(set(demoted), {0})
        self.assertEqual(verdicts[0]["verdict"], "reject")
        self.assertTrue(verdicts[0]["rewritable"])
        self.assertIn("render cap", verdicts[0]["reason"])
        self.assertIn("condense ONLY", verdicts[0]["reason"])
        self.assertEqual(demoted[0]["verdict"]["verdict"], "confirm")   # restorable
        self.assertEqual(verdicts[3]["verdict"], "reject")              # untouched

    def test_725_replay_content_rewrite_then_length_cure(self):
        """The exact 7/25 shape: draft rejected on substance (dropped Soundbite) -> content rewrite
        confirmed but over-cap -> demoted -> cure round condenses -> blind re-judge confirms.
        The content rewrite consumed PROPAGATE_MAX_REWRITES; the cure ran on its own budget."""
        judge_calls, rewrite_calls = [], []

        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            judge_calls.append([i for i, _ in indexed_ops])
            if len(judge_calls) == 1:
                v = _verdict("reject", "dropped the required Soundbite block", rewritable=True)
            else:
                v = _verdict("confirm", "grounded and faithful")
            return {"text": "```json\n" + json.dumps(v) + "\n```", "cost_usd": 0.0}

        async def rewrite_fake(meta, worklist, facts):
            rewrite_calls.append(worklist)
            prose = self.LONG if len(rewrite_calls) == 1 else self.SHORT
            return {"text": "```json\n" + json.dumps(_authored(prose)) + "\n```", "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("draft, no soundbite"))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", rewrite_fake):
            res = _run_propagate()

        self.assertEqual(len(rewrite_calls), 2)          # content round + cure round
        self.assertEqual(len(judge_calls), 3)            # initial, content re-judge, cure re-judge
        self.assertEqual(len(res["confirmed"]), 1)
        final = res["confirmed"][0]["claim"]
        self.assertEqual(final, self.SHORT)
        self.assertLessEqual(len(final.split()), 170)
        # the cure worklist carried the DETERMINISTIC cap reason as the judge feedback
        self.assertIn("render cap", rewrite_calls[1][0]["judge_reason"])
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "confirm")
        self.assertTrue(rec["length_demoted"])
        self.assertTrue(rec["length_cured"])
        self.assertEqual(rec["length_cure_attempts"], 1)
        self.assertFalse(rec["rewrite_exhausted"])

    def test_cure_failure_restores_the_confirm_never_an_exhausted_reject(self):
        """Cure returns the honest empty claim -> terminal reject inside the loop -> step 4b2
        RESTORES the original confirmed op (text and all); the render gate becomes the backstop."""
        judge_calls = []

        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            judge_calls.append(1)
            return {"text": "```json\n" + json.dumps(_verdict("confirm", "grounded")) + "\n```",
                    "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored(self.LONG))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", _fake(_authored(""))):
            res = _run_propagate()

        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "confirm")   # restored, not exhausted
        self.assertFalse(rec["rewrite_exhausted"])
        self.assertTrue(rec["length_demoted"])
        self.assertFalse(rec["length_cured"])
        self.assertEqual(len(res["confirmed"]), 1)
        self.assertEqual(res["confirmed"][0]["claim"], self.LONG)   # original text intact

    def test_rejudge_confirm_on_still_overcap_prose_is_not_cured(self):
        """A blind re-judge may confirm substance on prose still over the cap — code, not the
        judge, owns length: not cured, original restored."""
        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            return {"text": "```json\n" + json.dumps(_verdict("confirm", "fine")) + "\n```",
                    "cost_usd": 0.0}
        still_long = "**Play**\n\n" + ("beat " * 190) + "\n\n**Soundbite:** z."
        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored(self.LONG))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", _fake(_authored(still_long))):
            res = _run_propagate()
        rec = res["decisions"][0]
        self.assertTrue(rec["length_demoted"])
        self.assertFalse(rec["length_cured"])
        self.assertEqual(res["confirmed"][0]["claim"], self.LONG)   # restored original

    def test_knob_zero_disables_the_cure(self):
        config.PROPAGATE_MAX_LENGTH_CURES = 0
        rewrite = mock.MagicMock()
        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            return {"text": "```json\n" + json.dumps(_verdict("confirm", "ok")) + "\n```",
                    "cost_usd": 0.0}
        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored(self.LONG))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", rewrite):
            res = _run_propagate()
        rewrite.assert_not_called()
        rec = res["decisions"][0]
        self.assertNotIn("length_demoted", rec)
        self.assertEqual(res["confirmed"][0]["claim"], self.LONG)   # today's behavior (gate holds)


def _mverdict(verdict, reason, material=None, cure=None, i=0):
    """New-schema verdict payload (materiality-first). Omit material/cure to test legacy back-compat."""
    v = {"op_index": i, "verdict": verdict, "reason": reason}
    if material is not None:
        v["material"] = material
    if cure is not None:
        v["cure"] = cure
    return {"verdicts": [v]}


class MaterialityFirstCure(unittest.TestCase):
    """2026-07-31: the judge answers materiality first, then cure routing. A deal-moving point is
    never dropped — prose or root cure up to the cap, else urgent. Models the containment incident."""

    def setUp(self):
        self._saved = config.PROPAGATE_MAX_REWRITES
        config.PROPAGATE_MAX_REWRITES = 3

    def tearDown(self):
        config.PROPAGATE_MAX_REWRITES = self._saved

    def test_root_cure_reapproach_confirms(self):
        """The 7/31 replay: round-1 material+cure:root (judge names the honest approach) -> the
        rewrite worklist carries cure:'root' -> a grounded re-approach -> re-judge confirm."""
        judge_calls, worklists = [], []

        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            judge_calls.append(indexed_ops)
            v = (_mverdict("reject", "The point IS material (buyers fear agentic containment). But "
                           "the Foundry-governance pivot invents a mechanism. Correct approach: "
                           "acknowledge eval-only scope + the fast containment response.",
                           material=True, cure="root")
                 if len(judge_calls) == 1 else _mverdict("confirm", "grounded honest pivot"))
            return {"text": "```json\n" + json.dumps(v) + "\n```", "cost_usd": 0.0}

        async def rewrite_fake(meta, worklist, facts):
            worklists.append(worklist)
            return {"text": "```json\n" + json.dumps(_authored("honest eval-scope pivot")) + "\n```",
                    "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad foundry pivot"))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", rewrite_fake):
            res = _run_propagate()

        self.assertEqual(len(res["confirmed"]), 1)
        self.assertEqual(res["confirmed"][0]["claim"], "honest eval-scope pivot")
        self.assertEqual(worklists[0][0]["cure"], "root")     # the addendum got the root branch
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "confirm")
        self.assertFalse(rec["material_uncured"])

    def test_immaterial_reject_drops_no_loop_no_urgent(self):
        rewrite = mock.MagicMock()
        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("meh"))), \
             mock.patch.object(propagate, "_run_judge",
                               _fake(_mverdict("reject", "not deal-moving", material=False, cure="none"))), \
             mock.patch.object(propagate, "_run_rewrite", rewrite):
            res = _run_propagate()
        rewrite.assert_not_called()                            # immaterial -> no cure loop
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "reject")
        self.assertFalse(rec["material_uncured"])              # not urgent
        self.assertFalse(rec["rewrite_exhausted"])
        self.assertEqual(res["confirmed"], [])

    def test_cure_none_is_material_uncured_without_looping(self):
        rewrite = mock.MagicMock()
        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("draft"))), \
             mock.patch.object(propagate, "_run_judge",
                               _fake(_mverdict("reject", "material but no grounded expression",
                                               material=True, cure="none"))), \
             mock.patch.object(propagate, "_run_rewrite", rewrite):
            res = _run_propagate()
        rewrite.assert_not_called()                            # cure:none -> straight to urgent, no loop
        rec = res["decisions"][0]
        self.assertTrue(rec["material_uncured"])               # urgent trigger
        self.assertFalse(rec["rewrite_exhausted"])             # never looped
        self.assertEqual(rec["material"], True)
        self.assertEqual(rec["cure"], "none")

    def test_cap_three_retries_then_material_uncured(self):
        judge_calls = []

        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            judge_calls.append(1)
            return {"text": "```json\n" + json.dumps(
                _mverdict("reject", "still wrong root", material=True, cure="root")) + "\n```",
                "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad"))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", _fake(_authored("still bad"))):
            res = _run_propagate()

        # 1 initial judge + 3 cure-round re-judges = 4 (cap=3)
        self.assertEqual(len(judge_calls), 4)
        rec = res["decisions"][0]
        self.assertTrue(rec["rewrite_exhausted"])
        self.assertTrue(rec["material_uncured"])               # -> urgent email
        self.assertEqual(rec["rewrite_attempts"], 3)

    def test_cap_three_cures_on_third_try(self):
        judge_calls = []

        async def judge_fake(meta, facts, claims, indexed_ops, model=None):
            judge_calls.append(1)
            v = (_mverdict("confirm", "cured") if len(judge_calls) == 4
                 else _mverdict("reject", "root still off", material=True, cure="root"))
            return {"text": "```json\n" + json.dumps(v) + "\n```", "cost_usd": 0.0}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("bad"))), \
             mock.patch.object(propagate, "_run_judge", judge_fake), \
             mock.patch.object(propagate, "_run_rewrite", _fake(_authored("getting there"))):
            res = _run_propagate()

        self.assertEqual(len(res["confirmed"]), 1)             # cured within the cap
        rec = res["decisions"][0]
        self.assertFalse(rec["material_uncured"])
        self.assertEqual(rec["rewrite_attempts"], 3)


class UrgentMaterialEmail(unittest.TestCase):
    META = {"my_company": "Anthropic", "competitor": "OpenAI"}
    _U = {"operation": "add", "section": "objection_handling", "subject_key": "obj|x|current",
          "cure": "none", "rewrite_attempts": 0,
          "judge_reason": "material point: buyers fear containment; correct approach is eval-only scope",
          "trigger_source_url": "https://s/x", "attempts": [{"claim": "bad draft prose"}]}

    def test_render_carries_diagnosis_and_last_prose(self):
        subj, body = notify.render_urgent_material("s", self.META, [dict(self._U)])
        self.assertIn("URGENT", subj)
        self.assertIn("undrafted", subj)
        self.assertIn("JUDGE'S DIAGNOSIS", body)
        self.assertIn("eval-only scope", body)                # the honest framing surfaces
        self.assertIn("bad draft prose", body)                # last attempt shown
        self.assertIn("cure:none", body)

    def test_exhausted_reason_shown(self):
        u = dict(self._U, cure="root", rewrite_attempts=3)
        _, body = notify.render_urgent_material("s", self.META, [u])
        self.assertIn("exhausted after 3", body)

    def test_send_noops_on_empty(self):
        self.assertEqual(notify.send_urgent_material("s", self.META, []),
                         {"sent": False, "reason": "no urgent material"})

    def test_send_dry_run_never_raises(self):
        out = notify.send_urgent_material("s", self.META, [dict(self._U)], dry_run=True)
        self.assertIn("sent", out)                             # dispatch returned a dict, no raise


if __name__ == "__main__":
    unittest.main()
