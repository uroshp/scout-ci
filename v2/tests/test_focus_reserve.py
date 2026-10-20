"""The focus guarantee (2026-10-19). Uroš: "it's not an even game if the model sometimes is not looking
at the right thing, because sensors are looking for the right thing always." Code writes the reserved
focus searches from the focus area's own words, lists them in the triage request, reads the searches
the call actually ran (graded by the lifecycle audit's matcher) and runs the missing ones in a bounded
second pass under the same instructions. The audit stays the independent judge."""
import asyncio
import json
import unittest
from datetime import datetime
from unittest import mock

from scout import config, lifecycle, monitor

MISTRAL = {"competitor": "OpenAI", "my_company": "Mistral", "focus": "enterprise and sovereign AI for developers and agents"}
NAMES = ["OpenAI", "Mistral"]
NOW = datetime(2026, 10, 19, 4, 0)


def _call(queries, text=None, cost=0.10):
    return {"text": text or json.dumps({"has_candidates": False, "candidates": []}), "cost_usd": cost,
            "tool_uses": [{"name": "WebSearch", "input": {"query": q}} for q in queries]}


class FocusQueries(unittest.TestCase):
    def test_three_queries_each_grade_focus_under_the_audit(self):
        qs = monitor._focus_queries(MISTRAL, NOW)
        self.assertEqual(qs, ["OpenAI enterprise sovereign AI developers agents",
                              "ChatGPT enterprise sovereign AI developers agents October 2026",
                              "Mistral enterprise sovereign AI developers agents"])
        terms = lifecycle.focus_terms(MISTRAL["focus"], NAMES)
        for q in qs:
            self.assertEqual(lifecycle.search_scope(q, terms, NAMES), "focus", q)

    def test_a_focus_with_slashes_and_stop_words_keeps_its_own_words_in_order(self):
        meta = {"competitor": "Anthropic", "my_company": "OpenAI", "focus": "Enterprise collaboration agents (inside Slack and Teams)"}
        self.assertEqual(monitor._focus_queries(meta, NOW)[0], "Anthropic Enterprise collaboration agents Slack Teams")
        meta = {"competitor": "AWS", "my_company": "Google Cloud", "focus": "AI/ML infrastructure"}
        self.assertEqual(monitor._focus_queries(meta, NOW), ["AWS AI ML infrastructure", "Bedrock AI ML infrastructure October 2026",
                                                              "Google Cloud AI ML infrastructure"])

    def test_general_card_and_wordless_focus_get_no_queries(self):
        self.assertEqual(monitor._focus_queries({"competitor": "A", "my_company": "B", "focus": "General"}, NOW), [])
        self.assertEqual(monitor._focus_queries({"competitor": "A", "my_company": "B", "focus": None}, NOW), [])
        # a focus made only of the companies' names leaves no distinctive word: the audit says n/a, code forces nothing
        self.assertEqual(monitor._focus_queries({"competitor": "Mistral", "my_company": "OpenAI", "focus": "Mistral"}, NOW), [])

    def test_no_product_falls_back_to_an_announcement_query(self):
        meta = {"competitor": "Acme", "my_company": None, "focus": "widgets"}
        self.assertEqual(monitor._focus_queries(meta, NOW), ["Acme widgets", "Acme widgets announcement October 2026",
                                                              "Acme widgets launch OR partnership OR pricing"])


class TriageRequest(unittest.TestCase):
    def _capture(self, meta, **kw):
        seen = {}

        async def fake_drive(prompt, options, role):
            seen["prompt"], seen["options"], seen["role"] = prompt, options, role
            return _call([])
        with mock.patch.object(monitor, "_drive", fake_drive):
            asyncio.run(monitor._run_triage(meta, "2026-10-18", [], my_since="2026-10-18", **kw))
        return seen

    def test_listing_rides_the_user_message_and_leaves_the_instructions_alone(self):
        seen = self._capture(MISTRAL)
        self.assertIn("FOCUS SEARCHES", seen["prompt"])
        qs = monitor._focus_queries(MISTRAL)
        self.assertIn("1. " + qs[0], seen["prompt"])
        self.assertIn("3. " + qs[2], seen["prompt"])
        append = (seen["options"].system_prompt or {}).get("append") or ""
        self.assertNotIn("FOCUS SEARCHES", append)
        self.assertNotIn(qs[0], append)

    def test_the_switch_the_forced_pass_and_a_general_card_list_nothing(self):
        with mock.patch.object(config, "TRIAGE_FOCUS_FORCE", False):
            self.assertNotIn("FOCUS SEARCHES", self._capture(MISTRAL)["prompt"])
        self.assertNotIn("FOCUS SEARCHES", self._capture(MISTRAL, focus_queries=[], extra="\n\nFOCUS RESERVE UNMET: x")["prompt"])
        self.assertIn("FOCUS RESERVE UNMET", self._capture(MISTRAL, focus_queries=[], extra="\n\nFOCUS RESERVE UNMET: x")["prompt"])
        self.assertNotIn("FOCUS SEARCHES", self._capture({"competitor": "Slack", "my_company": "Microsoft Teams", "focus": None})["prompt"])


class FocusReserve(unittest.TestCase):
    def _run(self, first, second=None, raise_second=None, meta=MISTRAL):
        calls = []

        async def fake_triage(meta_, since, claims, my_since=None, extra="", **kw):
            calls.append({"extra": extra, **kw})
            if raise_second is not None:
                raise raise_second
            return second
        steps = []
        with mock.patch.object(monitor, "_run_triage", fake_triage):
            triage, cands = monitor._focus_reserve(meta, "2026-10-18", [], "2026-10-18", first, [{"signal": "old"}], steps, {})
        return triage, cands, steps, calls

    def test_a_covered_first_pass_needs_no_second_call(self):
        first = _call(monitor._focus_queries(MISTRAL) + ["OpenAI funding round", "Mistral CEO interview"])
        triage, cands, steps, calls = self._run(first)
        self.assertEqual(calls, [])
        self.assertIs(triage, first)
        self.assertEqual(cands, [{"signal": "old"}])
        self.assertEqual([(r["step"], r["status"]) for r in steps], [("focus_reserve", "ran")])
        self.assertIn("3 of 3 reserved focus searches ran in the first pass (5 searches)", steps[0]["detail"])

    def test_the_models_own_focus_wording_counts(self):
        first = _call(["OpenAI sovereign cloud agents Europe", "Mistral developers platform", "ChatGPT Enterprise agents"])
        _, _, steps, calls = self._run(first)
        self.assertEqual(calls, [])
        self.assertEqual(steps[0]["status"], "ran")

    def test_missing_searches_are_forced_with_the_exact_queries_and_merged(self):
        qs = monitor._focus_queries(MISTRAL)
        first = _call(["OpenAI funding round", "Mistral CEO interview"])
        second = _call(qs, text=json.dumps({"has_candidates": True, "candidates": [
            {"signal": "Mistral launches a sovereign agents platform", "subject_key": "NEW", "about": "my_company", "substantial": True},
            {"signal": "old", "subject_key": "NEW"}, "not a dict"]}), cost=0.07)
        triage, cands, steps, calls = self._run(first, second)
        self.assertEqual(len(calls), 1)
        for q in qs:
            self.assertIn(q, calls[0]["extra"])
        self.assertIn("ran 2 search(es), 0 of the 3 reserved", calls[0]["extra"])
        self.assertEqual(calls[0]["max_turns"], config.TRIAGE_FOCUS_RETRY_TURNS)
        self.assertEqual(calls[0]["budget"], config.TRIAGE_FOCUS_RETRY_BUDGET_USD)
        self.assertEqual(calls[0]["focus_queries"], [])
        self.assertEqual(calls[0]["focus_searches"], config.TRIAGE_FOCUS_SEARCHES)
        self.assertEqual([c["signal"] for c in cands], ["old", "Mistral launches a sovereign agents platform"])
        self.assertAlmostEqual(triage["cost_usd"], 0.17)
        self.assertEqual(len(triage["tool_uses"]), 5)
        self.assertEqual(triage["focus_retry"]["candidates_added"], 1)
        self.assertEqual(triage["focus_retry"]["queries"], qs)
        self.assertEqual(steps[-1]["status"], "ran")
        self.assertIn("the forced pass ran 3 of 3 for $0.07, 1 candidate(s) added", steps[-1]["detail"])
        self.assertEqual(first["cost_usd"], 0.10)       # the first pass's record is not mutated

    def test_a_partial_first_pass_forces_only_what_is_missing(self):
        qs = monitor._focus_queries(MISTRAL)
        first = _call([qs[0], "OpenAI funding round"])
        second = _call(qs[1:])
        triage, cands, steps, calls = self._run(first, second)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(qs[0], calls[0]["extra"])
        self.assertIn(qs[1], calls[0]["extra"])
        self.assertIn(qs[2], calls[0]["extra"])
        self.assertEqual(steps[-1]["status"], "ran")
        self.assertIn("1 of 3 reserved focus searches ran in the first pass; the forced pass ran 2 of 2", steps[-1]["detail"])

    def test_a_forced_pass_that_dies_is_a_failed_row_never_an_abort(self):
        first = _call(["OpenAI funding round"])
        err = RuntimeError("boom"); err.scout_cost_usd = 0.03
        triage, cands, steps, calls = self._run(first, raise_second=err)
        self.assertEqual(len(calls), 1)
        self.assertIs(triage, first)
        self.assertEqual(cands, [{"signal": "old"}])
        self.assertEqual(steps[-1]["status"], "failed")
        self.assertIn("the forced pass died: RuntimeError: boom", steps[-1]["detail"])
        self.assertEqual(steps[-1]["cost"], 0.03)

    def test_a_forced_pass_that_still_skips_the_focus_is_failed(self):
        first = _call(["OpenAI funding round"])
        second = _call(["OpenAI funding round again"], cost=0.05)
        triage, cands, steps, calls = self._run(first, second)
        self.assertEqual(steps[-1]["status"], "failed")
        self.assertIn("0 of 3 in total", steps[-1]["detail"])
        self.assertAlmostEqual(triage["cost_usd"], 0.15)      # the spend is still booked

    def test_the_switch_and_a_general_card_leave_triage_alone(self):
        first = _call(["OpenAI funding round"])
        with mock.patch.object(config, "TRIAGE_FOCUS_FORCE", False):
            triage, cands, steps, calls = self._run(first)
        self.assertEqual((calls, steps), ([], []))
        self.assertIs(triage, first)
        triage, cands, steps, calls = self._run(first, meta={"competitor": "Slack", "my_company": "Microsoft Teams", "focus": None})
        self.assertEqual((calls, steps), ([], []))


if __name__ == "__main__":
    unittest.main()
