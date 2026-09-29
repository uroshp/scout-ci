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
            seen["entries"] = [e["text"] for e in entries]
            return _verify({0: ("confirm", "none", "ok")})(entries, facts_by_id)
        a = ask.ask("q", research=_research(facts, answer), verify=verify, grounder=_grounder({"n1"}))
        self.assertEqual(seen["entries"], ["Agentforce passed 1,000 paid deals."])   # the judge never saw the bad ones
        self.assertEqual(a["trajectory"]["floor_dropped"], 2)
        self.assertEqual(len(a["paragraphs"]), 1)

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
        self.assertEqual(a["trajectory"], {"rounds": 2, "research_turns": 3, "cut": 0, "floor_dropped": 0, "judge_rejected": 1, "rewritten": 1})
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
