"""scout/ask.py (WS2): the goal-based verify loop with fake research / verify / rewrite / grounder.
The invariants: nothing reaches the answer without a resolving cite and numbers the cited evidence
supports (the model-free floor), the judge can only cut, a rewrite is judged again, an unparseable
judge is a reject, unanswered topics carry no digits, the stop condition is the evaluator's. No
model, no network."""
import json
import unittest
from unittest import mock

from scout import ask

XBRL = ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax end=2026-07-31 value=11345000000 unit=USD "
        "form=10-Q fy=2027 fp=Q2 filed=2026-08-27 accn=0001108524-26-000190")
NEWS = "Salesforce said Agentforce had closed more than 1,000 paid deals as of the quarter, up from 200 in the prior quarter."


def _fact(fid, url, excerpt, tier="primary", claim="c"):
    return {"id": fid, "claim": claim, "claim_type": "fact", "source_url": url, "source_tier": tier,
            "evidence_excerpt": excerpt, "as_of": "2026-07-31"}


def _research(facts, answer, unanswered=(), cost=0.6):
    body = {"facts": facts, "answer": answer, "unanswered": list(unanswered)}
    return lambda q, known, ctx: {"text": "```json\n" + json.dumps(body) + "\n```", "cost_usd": cost, "num_turns": 3}


def _verify(verdicts, cost=0.3):
    def fn(entries, facts_by_id):
        vs = [{"op_index": i, "verdict": verdicts.get(i, ("reject", "none", "not supported"))[0],
               "material": True, "cure": verdicts.get(i, ("reject", "none", "x"))[1], "reason": verdicts.get(i, ("reject", "none", "x"))[2]}
              for i in range(len(entries))]
        return {"text": "```json\n" + json.dumps({"verdicts": vs}) + "\n```", "cost_usd": cost}
    return fn


def _grounder(kept_ids):
    def fn(claims):
        kept = [dict(c, grounding={"checked": True, "match": True, "method": "substring", "fetched_at": "2026-09-28"}) for c in claims if c["id"] in kept_ids]
        results = [{"claim_id": c["id"], "status": "grounded" if c["id"] in kept_ids else "absent", "detail": None if c["id"] in kept_ids else "best partial_ratio 0.41 < 0.92"} for c in claims]
        return {"kept": kept, "cut": [], "failed": [], "results": results, "counts": {}, "substituted": []}
    return fn


class Loop(unittest.TestCase):
    def test_happy_path_answer_from_a_grounded_filing_line(self):
        facts = [_fact("n1", "https://data.sec.gov/api/xbrl/companyconcept/CIK0001108524/us-gaap/Revenues.json", XBRL)]
        answer = [{"text": "Salesforce reported $11.3 billion of revenue for the quarter ended July 31, 2026.", "cites": ["n1"]}]
        a = ask.ask("What did Salesforce report?", slug=None, research=_research(facts, answer),
                    verify=_verify({0: ("confirm", "none", "ok")}), grounder=_grounder({"n1"}))
        self.assertTrue(a["verified"]); self.assertEqual(len(a["paragraphs"]), 1)
        self.assertEqual(a["paragraphs"][0]["cites"], [1])
        self.assertEqual(a["sources"][0]["class"], "filing"); self.assertEqual(a["sources"][0]["tier"], "primary")
        self.assertEqual(a["cut_log"], []); self.assertAlmostEqual(a["cost_usd"], 0.9)
        self.assertEqual(a["trajectory"]["rounds"], 1)
        self.assertIn("[1]", ask.render_text(a))

    def test_ungrounded_fact_is_cut_and_its_sentence_drops_at_the_floor(self):
        facts = [_fact("n1", "https://www.cnbc.com/x", NEWS, "reputable_secondary"), _fact("n2", "https://www.cnbc.com/y", "z" * 50, "reputable_secondary")]
        answer = [{"text": "Agentforce passed 1,000 paid deals.", "cites": ["n1"]}, {"text": "It also launched in Japan.", "cites": ["n2"]}]
        a = ask.ask("q", research=_research(facts, answer), verify=_verify({0: ("confirm", "none", "ok")}), grounder=_grounder({"n1"}))
        self.assertEqual(len(a["paragraphs"]), 1)
        self.assertEqual(a["trajectory"]["cut"], 1); self.assertEqual(a["trajectory"]["floor_dropped"], 1)
        reasons = " | ".join(c["reason"] for c in a["cut_log"])
        self.assertIn("not on the cited page", reasons); self.assertIn("no cite resolves", reasons)

    def test_floor_cuts_a_number_the_evidence_does_not_support_before_any_judge(self):
        facts = [_fact("n1", "https://www.cnbc.com/x", NEWS, "reputable_secondary")]
        answer = [{"text": "Agentforce passed 1,000 paid deals.", "cites": ["n1"]},
                  {"text": "Agentforce passed 1,500 paid deals.", "cites": ["n1"]},
                  {"text": "Deals grew 400% quarter over quarter.", "cites": ["n1"]}]
        seen = {}
        def verify(entries, facts_by_id):
            seen.setdefault("entries", []).append([e["text"] for e in entries])
            return _verify({0: ("confirm", "none", "ok")})(entries, facts_by_id)
        def rewrite(entries, verdicts, facts_by_id):
            seen["rewrite"] = {i: v["reason"] for i, v in verdicts.items()}
            # the rewriter drops the unsupported figure from one, returns nothing for the other
            return {"text": "```json\n" + json.dumps({"answer": [{"index": 1, "text": "Agentforce closed more than 1,000 paid deals, up from 200 the prior quarter.", "cites": ["n1"]}]}) + "\n```", "cost_usd": 0.2}
        a = ask.ask("q", research=_research(facts, answer), verify=verify, rewrite=rewrite, grounder=_grounder({"n1"}))
        self.assertEqual(seen["entries"][0], ["Agentforce passed 1,000 paid deals."])   # the judge never saw the bad ones
        self.assertTrue(all(r.startswith("floor:") for r in seen["rewrite"].values()))   # floor failures got ONE rewrite
        self.assertEqual(a["trajectory"]["floor_dropped"], 2)
        self.assertEqual(len(a["paragraphs"]), 2)                                        # the repaired one came back
        self.assertTrue(any("no rewrite returned" in c["reason"] for c in a["cut_log"]))

    def test_zero_cite_sentences_and_bad_facts_never_reach_the_answer(self):
        facts = [{"id": "n1", "claim": "x", "source_url": "ftp://nope", "source_tier": "primary", "evidence_excerpt": "short"}]
        answer = [{"text": "A bold claim with no support.", "cites": []}, {"text": "Cites a cut fact.", "cites": ["n1"]}]
        a = ask.ask("q", research=_research(facts, answer), verify=_verify({}), grounder=_grounder(set()))
        self.assertEqual(a["paragraphs"], []); self.assertFalse(a["verified"])
        self.assertTrue(any("malformed fact" in c["reason"] for c in a["cut_log"]))
        self.assertEqual(a["trajectory"]["floor_dropped"], 2)

    def test_judge_reject_with_prose_cure_is_rewritten_then_judged_again(self):
        facts = [_fact("n1", "https://www.cnbc.com/x", NEWS, "reputable_secondary")]
        answer = [{"text": "Agentforce's 1,000 deals prove enterprises prefer it.", "cites": ["n1"]}]
        calls = {"verify": 0}
        def verify(entries, facts_by_id):
            calls["verify"] += 1
            if calls["verify"] == 1:
                return _verify({0: ("reject", "prose", "'prove enterprises prefer it' is not in the fact")})(entries, facts_by_id)
            return _verify({0: ("confirm", "none", "ok")})(entries, facts_by_id)
        def rewrite(entries, verdicts, facts_by_id):
            self.assertEqual(list(verdicts), [0]); self.assertIn("prove", verdicts[0]["reason"])
            return {"text": "```json\n" + json.dumps({"answer": [{"index": 0, "text": "Salesforce said Agentforce had closed more than 1,000 paid deals.", "cites": ["n1"]}]}) + "\n```", "cost_usd": 0.2}
        a = ask.ask("q", research=_research(facts, answer), verify=verify, rewrite=rewrite, grounder=_grounder({"n1"}))
        self.assertEqual(calls["verify"], 2)
        self.assertEqual(a["paragraphs"][0]["text"], "Salesforce said Agentforce had closed more than 1,000 paid deals.")
        self.assertEqual(a["trajectory"], {"rounds": 2, "research_turns": 3, "cut": 0, "floor_dropped": 0, "judge_rejected": 1, "rewritten": 1, "thread_turns": 0})
        self.assertAlmostEqual(a["cost_usd"], 0.6 + 0.3 + 0.2 + 0.3)

    def test_root_or_none_cure_is_dropped_and_unparseable_judge_is_a_reject(self):
        facts = [_fact("n1", "https://www.cnbc.com/x", NEWS, "reputable_secondary")]
        answer = [{"text": "Agentforce closed 1,000 deals.", "cites": ["n1"]}, {"text": "Agentforce closed 200 deals last quarter.", "cites": ["n1"]}]
        rewrite = mock.Mock()
        a = ask.ask("q", research=_research(facts, answer), verify=_verify({0: ("reject", "root", "not there"), 1: ("reject", "none", "nothing")}),
                    rewrite=rewrite, grounder=_grounder({"n1"}))
        rewrite.assert_not_called(); self.assertEqual(a["paragraphs"], []); self.assertEqual(a["trajectory"]["judge_rejected"], 2)
        a2 = ask.ask("q", research=_research(facts, answer), verify=lambda e, f: {"text": "no json here", "cost_usd": 0.1},
                     rewrite=rewrite, grounder=_grounder({"n1"}))
        self.assertEqual(a2["paragraphs"], []); self.assertTrue(all("fail-closed" in c["reason"] for c in a2["cut_log"]))

    def test_unanswered_topics_carry_no_digits(self):
        a = ask.ask("q", research=_research([], [], unanswered=["2026 revenue of $5 billion", "headcount"]), verify=_verify({}), grounder=_grounder(set()))
        self.assertEqual(a["unanswered"], ["a figure revenue of a figure", "headcount"])
        self.assertFalse(a["verified"])

    def test_card_facts_are_evidence_without_a_search(self):
        known = [_fact("c_0123456789ab", "https://www.anthropic.com/news/x", "Claude Code Pro costs $20 per month and Max $100 or $200.")]
        with mock.patch.object(ask, "card_facts", return_value=known):
            a = ask.ask("q", slug="a__vs__b__c", research=_research([], [{"text": "Claude Code Pro is $20 a month.", "cites": ["c_0123456789ab"]}]),
                        verify=_verify({0: ("confirm", "none", "ok")}), grounder=_grounder(set()))
        self.assertEqual(len(a["paragraphs"]), 1); self.assertTrue(a["sources"][0]["from_card"])


class Floor(unittest.TestCase):
    FB = {"n1": {"evidence_excerpt": XBRL + " · 840 open postings, 22.4% growth", "claim": ""}}

    def test_precision_aware_numbers(self):
        ok = ["Revenue was $11.3 billion in the quarter ended July 31, 2026.", "It has 840 open roles, growing 22%.", "Revenue of $11.345B", "The 10-Q for Q2."]
        bad = ["Revenue was $11.9 billion.", "Revenue was $11.35 billion.", "It has about 850 open roles.", "In 2025 revenue rose.", "Growth of 30%."]
        for t in ok:
            self.assertEqual(ask.floor_check({"text": t, "cites": ["n1"]}, self.FB), [], t)
        for t in bad:
            self.assertTrue(ask.floor_check({"text": t, "cites": ["n1"]}, self.FB), t)

    def test_fact_schema(self):
        good = _fact("n1", "https://x.example/p", "e" * 40)
        self.assertEqual(ask.fact_errors(good), [])
        self.assertIn("source_tier not in", " ".join(ask.fact_errors(dict(good, source_tier="gold"))))
        self.assertIn("sentiment-only", " ".join(ask.fact_errors(dict(good, source_tier="sentiment_only"))))
        self.assertIn("shorter than 40", " ".join(ask.fact_errors(dict(good, evidence_excerpt="x"))))
        self.assertEqual(ask.fact_errors("nope"), ["not an object"])


if __name__ == "__main__":
    unittest.main()


class Thread(unittest.TestCase):
    """One conversation across every card (2026-09-28): the question names the scope, previous
    answers' sources are reused as known facts, the research prompt gets the thread."""

    def test_question_names_the_competitor_over_the_card_hint(self):
        with mock.patch.object(ask, "infer_competitor", return_value=("OpenAI", "anthropic__vs__openai__x")), \
             mock.patch.object(ask, "card_facts", return_value=[]) as cf, \
             mock.patch.object(ask.store, "load_meta", return_value={"competitor": "OpenAI", "my_company": "Anthropic"}):
            a = ask.ask("What is OpenAI hiring for?", slug="google-cloud__vs__aws__y", research=_research([], []), verify=_verify({}), grounder=_grounder(set()))
        self.assertEqual(a["competitor"], "OpenAI"); self.assertEqual(a["slug"], "anthropic__vs__openai__x")
        cf.assert_called_with("anthropic__vs__openai__x")

    def test_history_sources_become_known_facts_and_reach_the_prompt(self):
        prior = {"id": "a_0123456789ab", "sources": [{"id": "n7", "url": "https://www.cnbc.com/x", "excerpt": NEWS, "tier": "reputable_secondary", "class": "news", "as_of": "2026-07-30"}]}
        seen = {}
        def research(q, known, ctx, history=None):
            seen["known_ids"] = [k["id"] for k in known]; seen["history"] = history
            return _research([], [{"text": "Agentforce passed 1,000 paid deals.", "cites": ["n7"]}])(q, known, ctx)
        with mock.patch.object(ask.selfserve if hasattr(ask, "selfserve") else __import__("scout.selfserve", fromlist=["x"]), "list_data", return_value=["2026-09"]), \
             mock.patch("scout.selfserve.read_data", return_value=json.dumps(prior)), \
             mock.patch.object(ask, "infer_competitor", return_value=(None, None)):
            a = ask.ask("and how many deals?", history=[{"question": "What is Agentforce?", "answer_id": "a_0123456789ab"}, {"question": "bad", "answer_id": "zzz"}],
                        research=research, verify=_verify({0: ("confirm", "none", "ok")}), grounder=_grounder(set()))
        self.assertEqual(seen["known_ids"], ["n7"])
        self.assertEqual([h["question"] for h in seen["history"]], ["What is Agentforce?", "bad"])
        self.assertEqual(len(a["paragraphs"]), 1); self.assertTrue(a["sources"][0]["from_thread"]); self.assertFalse(a["sources"][0]["from_card"])
        self.assertEqual(a["trajectory"]["thread_turns"], 2)

    def test_follow_up_naming_nobody_keeps_the_thread_scope_not_the_card(self):
        # RC 2026-09-28: "Which of the two is GA today?" asked from the Batman card, after a turn about
        # Anthropic vs OpenAI, came back headed "About Batman vs Superman"
        prior = {"id": "a_0123456789ab", "competitor": "OpenAI", "slug": "anthropic__vs__openai__x", "sources": []}
        metas = {"anthropic__vs__openai__x": {"competitor": "OpenAI", "my_company": "Anthropic"},
                 "batman__vs__superman__general": {"competitor": "Superman", "my_company": "Batman"}}
        with mock.patch("scout.selfserve.list_data", return_value=["2026-09"]), \
             mock.patch("scout.selfserve.read_data", return_value=json.dumps(prior)), \
             mock.patch.object(ask, "card_facts", return_value=[]) as cf, \
             mock.patch.object(ask.store, "load_meta", side_effect=lambda s: metas.get(s)):
            a = ask.ask("Which of the two is generally available today?", slug="batman__vs__superman__general",
                        history=[{"question": "How do their Slack agents compare?", "answer_id": "a_0123456789ab"}],
                        research=_research([], []), verify=_verify({}), grounder=_grounder(set()))
            self.assertEqual((a["competitor"], a["slug"]), ("OpenAI", "anthropic__vs__openai__x"))
            cf.assert_called_with("anthropic__vs__openai__x")
            # a follow-up that names a company still switches
            with mock.patch.object(ask, "infer_competitor", return_value=("Mistral", "mistral__vs__openai__z")):
                a = ask.ask("And what is Mistral hiring for?", slug="batman__vs__superman__general",
                            history=[{"question": "q", "answer_id": "a_0123456789ab"}],
                            research=_research([], []), verify=_verify({}), grounder=_grounder(set()))
            self.assertEqual((a["competitor"], a["slug"]), ("Mistral", "mistral__vs__openai__z"))


class Restatements(unittest.TestCase):
    A = "So today, Claude Tag is limited to Team and Enterprise plans, with no Pro or Free access, while OpenAI's workspace agents launched on ChatGPT Business at $20 per user a month plus variably priced Enterprise, Edu and Teachers plans."
    B = "OpenAI's workspace agents, which plug into Slack, rolled out on ChatGPT Business at $20 per user a month, plus variably priced Enterprise, Edu and Teachers plans."
    C = "Anthropic's Slack agent, Claude Tag, launched in beta on June 23, 2026 for Enterprise and Team customers on Opus 4.8."
    D = "Both vendors gate a standing Slack presence with admin controls: Claude Tag caps token spend per organization and per channel with a full activity log."

    def test_the_shorter_restatement_is_dropped_and_logged(self):
        cut, tr = [], {}
        kept = ask.drop_restatements([{"text": self.C, "cites": ["1"]}, {"text": self.A, "cites": ["1", "2"]}, {"text": self.B, "cites": ["2"]}, {"text": self.D, "cites": ["1"]}], cut, tr)
        self.assertEqual([k["text"] for k in kept], [self.C, self.A, self.D])
        self.assertEqual(len(cut), 1); self.assertIn("restates", cut[0]["reason"]); self.assertEqual(tr["restated"], 1)

    def test_distinct_sentences_are_untouched(self):
        cut = []
        kept = ask.drop_restatements([{"text": self.C, "cites": []}, {"text": self.B, "cites": []}, {"text": self.D, "cites": []}], cut)
        self.assertEqual(len(kept), 3); self.assertEqual(cut, [])
        # sharing the subject while ADDING a fact is not a restatement
        kept = ask.drop_restatements([{"text": "Agentforce passed 1,000 paid deals.", "cites": []},
                                      {"text": "Agentforce closed more than 1,000 paid deals, up from 200 the prior quarter.", "cites": []}], cut)
        self.assertEqual(len(kept), 2); self.assertEqual(cut, [])

    def test_a_reordered_restatement_is_caught(self):
        cut = []
        kept = ask.drop_restatements([{"text": "On ChatGPT Business, workspace agents cost $20 per user a month.", "cites": []},
                                      {"text": "Workspace agents on ChatGPT Business cost $20 per user a month.", "cites": []}], cut)
        self.assertEqual(len(kept), 1); self.assertEqual(len(cut), 1)
