"""The lifecycle audit (2026-10-04): the two production-critical bugs of 10/3-10/4 must turn the
audit RED from the run's own artifacts, and a clean run must stay GREEN."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import lifecycle, monitor  # noqa: E402

FOCUS = "Enterprise collaboration agents (inside Slack and Teams)"
NAMES = ["Anthropic", "OpenAI"]


def _trace(**over):
    """A clean focused-card trace; tests override the parts that reproduce a bug."""
    base = {
        "focused": True, "focus": FOCUS, "focus_terms": sorted(lifecycle.focus_terms(FOCUS, NAMES)), "names": NAMES,
        "steps": [{"step": "triage", "status": "ran", "cost": 0.4}, {"step": "materiality", "status": "ran", "cost": 0.3, "detail": "1 grounded of 1 candidate(s)"},
                  {"step": "own_company", "status": "ran", "cost": 0.2, "detail": "2 anchor fact(s) grounded of 2 candidate(s)"},
                  {"step": "propagation", "status": "ran", "cost": 0.5}, {"step": "write", "status": "ran"}],
        "searches": [{"query": "Claude Tag Slack adoption", "scope": "focus"}, {"query": "OpenAI agents Teams launch", "scope": "focus"},
                     {"query": "Anthropic Slack collaboration", "scope": "focus"}, {"query": "Anthropic IPO November", "scope": "corporate"}],
        "prompts_with_focus": {"triage": True, "materiality": True, "my_facts": True}, "triage_parsed": True,
        "candidates": [{"signal": "x", "about": "competitor", "subject_key": "anthropic | ipo-plan | current", "substantial": True}],
        "material": [{"subject_key": "anthropic | ipo-plan | current", "new_value": "v", "source_url": "https://x.test/a"}],
        "immaterial": [],
        "own_facts": [{"subject_key": "openai | flagship-product | collaboration-agent", "new_value": "v", "source_url": "https://x.test/b"},
                      {"subject_key": "openai | chatgpt-space", "new_value": "v2", "source_url": "https://x.test/c"}],
        "grounding": {"kept": [{"subject_key": "anthropic | ipo-plan | current", "status": "grounded"},
                               {"subject_key": "openai | flagship-product | collaboration-agent", "status": "grounded"},
                               {"subject_key": "openai | chatgpt-space", "status": "grounded"}], "cut": [], "records": 1, "results": 3},
        "decisions": [{"operation": "revise", "subject_key": "anthropic | positioning | ipo", "verdict": "confirm", "reason": "r", "committed": True}],
        "alerts": [{"subject_key": "anthropic | ipo-plan | current", "severity": "watch", "headline": "h"},
                   {"subject_key": "openai | flagship-product | collaboration-agent", "severity": "act", "headline": "h2"},
                   {"subject_key": "openai | chatgpt-space", "severity": "watch", "headline": "h3"}],
        "claims_touched": 3,
        "evals": {"captured_roles": ["triage", "materiality", "my_facts", "route", "author", "judge"], "roles_without_spec": [],
                  "shadow_records": 1, "dismissal_records": 1, "decision_logs": 1, "filter_records": 0,
                  "pack_versions": ["d18113eddd727e13"], "calls_without_sha": 0},
        "counts": {"materiality": {"grounded": 1, "candidates": 1}, "own_company": {"grounded": 2, "candidates": 2}},
        "_claims": [{"subject_key": "anthropic | ipo-plan | current", "verified": True, "source_url": "https://x.test/a", "grounding": {"match": True}},
                    {"subject_key": "openai | flagship-product | collaboration-agent", "verified": True, "source_url": "https://x.test/b", "grounding": {"match": True}},
                    {"subject_key": "openai | chatgpt-space", "verified": True, "source_url": "https://x.test/c", "grounding": {"match": True}},
                    {"subject_key": "anthropic | positioning | ipo", "verified": True}],
    }
    base.update(over)
    return base


def _by_id(inv):
    out = {}
    for i in inv:
        out.setdefault(i["id"], []).append(i["status"])
    return out


class FocusVocabulary(unittest.TestCase):
    def test_focus_terms_drop_stop_words_and_company_names_and_add_products(self):
        t = lifecycle.focus_terms(FOCUS, NAMES)
        self.assertTrue({"enterprise", "collaboration", "agents", "slack", "teams", "claude", "chatgpt"} <= t)
        self.assertNotIn("inside", t)
        self.assertNotIn("anthropic", t)

    def test_two_letter_focus_words_count(self):
        t = lifecycle.focus_terms("AI/ML infrastructure", ["Google Cloud", "AWS"])
        self.assertTrue({"ai", "ml", "infrastructure", "bedrock", "gemini"} <= t)

    def test_search_scope(self):
        t = lifecycle.focus_terms(FOCUS, NAMES)
        self.assertEqual(lifecycle.search_scope("Anthropic Claude Tag Slack adoption September 2026", t, NAMES), "focus")
        self.assertEqual(lifecycle.search_scope("Anthropic IPO November 2026 timeline", t, NAMES), "corporate")
        self.assertEqual(lifecycle.search_scope("AI slowdown lawsuit antitrust", t, NAMES), "unclear")


class TheTwoBugsTurnRed(unittest.TestCase):
    def test_clean_trace_is_green(self):
        inv = lifecycle.check_card(_trace())
        self.assertEqual([i for i in inv if i["status"] in ("fail", "warn")], [], inv)

    def test_bug_one_focus_never_reached_the_prompts(self):
        """10/3: no focus-area search, the focus absent from every prompt (since June)."""
        tr = _trace(searches=[{"query": "Anthropic IPO November", "scope": "corporate"}, {"query": "OpenAI Pro pause", "scope": "corporate"}],
                    prompts_with_focus={"triage": False, "materiality": False, "my_facts": False})
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["A1"], ["fail"])
        self.assertEqual(st["A2"], ["fail", "fail", "fail"])

    def test_fewer_focus_searches_than_reserved_is_a_warning_not_a_failure(self):
        tr = _trace(searches=[{"query": "Claude Tag Slack", "scope": "focus"}, {"query": "Anthropic IPO", "scope": "corporate"},
                              {"query": "OpenAI revenue", "scope": "corporate"}, {"query": "Anthropic execs", "scope": "corporate"}])
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["A1"], ["warn"])

    def test_bug_two_facts_emitted_none_grounded_no_reason(self):
        """10/4: four own-side facts emitted, the step row said 0 grounded, nothing named the drop."""
        tr = _trace(steps=[{"step": "triage", "status": "ran", "cost": 0.4},
                           {"step": "own_company", "status": "ran", "cost": 0.2, "detail": "0 anchor fact(s) grounded of 4 candidate(s)"},
                           {"step": "write", "status": "ran"}],
                    own_facts=[{"subject_key": f"openai | k{i}", "new_value": "v"} for i in range(4)],
                    grounding={"kept": [], "cut": [], "records": 1, "results": 0}, alerts=[], decisions=[],
                    counts={"materiality": {}, "own_company": {"grounded": 0, "candidates": 4}})
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["B3"], ["fail"])

    def test_every_emitted_fact_rejected_by_the_schema_fails_even_when_named(self):
        tr = _trace(steps=[{"step": "own_company", "status": "failed", "cost": 0.2,
                            "detail": "0 anchor fact(s) grounded of 4 candidate(s); 4 of 4 emitted fact(s) rejected by the schema before grounding: x"}],
                    own_facts=[{"subject_key": f"openai | k{i}"} for i in range(4)], grounding={"kept": [], "cut": [], "records": 0, "results": 0},
                    alerts=[], decisions=[], counts={"materiality": {}, "own_company": {"grounded": 0, "candidates": 4, "schema_rejected": 4, "emitted": 4}})
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["B3"], ["fail"])
        self.assertEqual(st["B5"], ["fail"])

    def test_material_fact_that_vanished_between_judge_and_grounding(self):
        tr = _trace(material=[{"subject_key": "anthropic | ipo-plan | current"}, {"subject_key": "anthropic | compute-strain"}])
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["B2"], ["fail"])
        self.assertIn("compute strain", [i for i in lifecycle.check_card(tr) if i["id"] == "B2"][0]["evidence"])

    def test_own_side_grounding_missing_from_the_eval_record(self):
        tr = _trace(grounding={"kept": [{"subject_key": "anthropic | ipo-plan | current", "status": "grounded"}], "cut": [], "records": 1, "results": 1})
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["D2"], ["fail"])
        self.assertEqual(st["C1"], ["pass"])

    def test_alert_without_a_claim_fails_and_a_lead_alert_is_not_a_claim(self):
        tr = _trace(alerts=_trace()["alerts"] + [{"subject_key": "audience-lead | technical_evaluator | c_1", "severity": "watch", "headline": "lead"},
                                                  {"subject_key": "openai | ghost", "severity": "watch", "headline": "no claim"}])
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["C2"], ["fail"])
        ev = [i for i in lifecycle.check_card(tr) if i["id"] == "C2"][0]["evidence"]
        self.assertIn("ghost", ev)
        self.assertNotIn("audience-lead", ev)

    def test_an_applied_audience_lead_is_not_a_missing_claim(self):
        tr = _trace(decisions=[{"operation": "add", "subject_key": "audience-lead | security_regulated | c_1", "verdict": "confirm", "reason": "r", "committed": True}])
        st = _by_id(lifecycle.check_card(tr))
        self.assertNotIn("C3", st)

    def test_a_role_that_ran_but_was_not_captured(self):
        tr = _trace(evals=dict(_trace()["evals"], captured_roles=["triage", "materiality"]))
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["D1"], ["fail"])

    def test_general_card_skips_the_focus_group(self):
        tr = _trace(focused=False, focus="", focus_terms=[], prompts_with_focus={})
        st = _by_id(lifecycle.check_card(tr))
        self.assertEqual(st["A1"], ["n/a"])
        self.assertNotIn("A2", st)


class RunWindow(unittest.TestCase):
    def test_records_belong_to_the_run_until_the_next_run_starts(self):
        self.assertTrue(lifecycle._in_window("20261004T012614", "20261004T012302", "20261004T041224"))
        self.assertFalse(lifecycle._in_window("20261004T041959", "20261004T012302", "20261004T041224"))
        self.assertTrue(lifecycle._in_window("20261004T041959", "20261004T041224", None))
        self.assertFalse(lifecycle._in_window("20261004T091959", "20261004T041224", None))


class AuditEndToEnd(unittest.TestCase):
    def test_audit_assembles_from_rows_and_calls_and_writes_a_document(self):
        rows = [{"slug": "a__vs__b__x", "steps": [{"step": "triage", "status": "ran", "cost": 0.1}], "total": 0.1}]
        calls = [{"slug": "a__vs__b__x", "role": "triage", "user": "Changes since. FOCUS AREA: widgets. both scopes", "judgment_version": "v1", "instructions_sha": "s",
                  "transcript": [{"kind": "tool_use", "name": "WebSearch", "input": {"query": "Acme widgets launch"}}],
                  "result": {"text": json.dumps({"has_candidates": False, "candidates": []})}}]
        meta = {"competitor": "Acme", "my_company": "Beta", "focus": "widgets"}
        written = {}
        with mock.patch.object(lifecycle, "_card_file", side_effect=lambda slug, name: {"meta.json": json.dumps(meta), "claims.json": "[]", "alerts.jsonl": ""}.get(name)), \
             mock.patch.object(lifecycle, "_records_for", return_value=[]), \
             mock.patch.object(lifecycle, "next_run_stamp", return_value=None), \
             mock.patch.object(lifecycle.selfserve, "write_data", side_effect=lambda p, t, m: written.setdefault(p, t)):
            doc = lifecycle.audit("20261004T120000", rows=rows, calls=calls, write=True)
        self.assertEqual(doc["verdict"], "GREEN", doc["summary"])
        self.assertEqual(list(written), ["lifecycle/20261004T120000.json"])
        card = doc["cards"][0]
        self.assertEqual(card["trace"]["searches"][0]["scope"], "focus")
        self.assertEqual(_by_id(card["invariants"])["A2"], ["pass"])


class BothArmsReachTheEvalMirror(unittest.TestCase):
    def test_ground_best_keeps_the_per_claim_results(self):
        claim = {"subject_key": "k", "claim": "t", "source_url": "https://x.test/a", "source_tier": "primary", "evidence_excerpt": "e"}
        with mock.patch.object(monitor, "ground_claims", return_value={"kept": [dict(claim, grounding={"match": True})], "cut": [], "failed": [],
                                                                       "results": [{"subject_key": "k", "status": "grounded"}]}):
            g = monitor._ground_best([claim])
        self.assertEqual(len(g["kept"]), 1)
        self.assertEqual(g["results"], [{"subject_key": "k", "status": "grounded"}])


if __name__ == "__main__":
    unittest.main()
