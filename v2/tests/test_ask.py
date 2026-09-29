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

    def test_unanswered_topics_carry_no_quantities(self):
        a = ask.ask("q", research=_research([], [], unanswered=["2026 revenue of $5 billion", "headcount"]), verify=_verify({}), grounder=_grounder(set()))
        self.assertEqual(a["unanswered"], ["2026 revenue of a figure", "headcount"])   # a year is not a claim
        self.assertFalse(a["verified"])
        # quantities go; names with digits and bare years stay (RC 9/28: "GPT-6" became "GPT-a figure")
        cases = {"benchmark results between Mistral's latest model and GPT-6 Astra": "benchmark results between Mistral's latest model and GPT-6 Astra",
                 "Opus 4.8 pricing at $10 per million tokens": "Opus 4.8 pricing at a figure per million tokens",
                 "growth of 35% and 1,200 seats in Q2": "growth of a figure and a figure seats in Q2",
                 "€20/user, 3x faster, 12000 employees": "a figure/user, 3x faster, a figure employees",
                 "a price for 500 seats in 2027": "a price for a figure seats in 2027"}
        for src, want in cases.items():
            self.assertEqual(ask._scrub_digits(src), want, src)

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


class Scope(unittest.TestCase):
    METAS = {"anthropic__vs__openai__x": {"competitor": "OpenAI", "my_company": "Anthropic"},
             "mistral__vs__openai__y": {"competitor": "OpenAI", "my_company": "Mistral"},
             "google-cloud__vs__aws__z": {"competitor": "AWS", "my_company": "Google Cloud"}}

    def test_the_card_matching_the_most_named_companies_wins(self):
        with mock.patch.object(ask.display, "list_battlecards", return_value=list(self.METAS)), \
             mock.patch.object(ask.store, "load_meta", side_effect=lambda s: self.METAS.get(s)):
            # RC 9/28: "Mistral instead of OpenAI" went to the Anthropic card (first card naming OpenAI)
            self.assertEqual(ask.infer_competitor("My client wants Mistral instead of OpenAI for knowledge work"), ("OpenAI", "mistral__vs__openai__y"))
            self.assertEqual(ask.infer_competitor("What is OpenAI shipping?"), ("OpenAI", "anthropic__vs__openai__x"))   # tie: card order
            self.assertEqual(ask.infer_competitor("Is AWS cheaper?"), ("AWS", "google-cloud__vs__aws__z"))
            self.assertEqual(ask.infer_competitor("Which is best?"), (None, None))


class Activity(unittest.TestCase):
    def test_tool_calls_become_reader_lines(self):
        self.assertEqual(ask.activity_text("WebSearch", {"query": "Mistral Le Chat Enterprise pricing"}), "Searching: Mistral Le Chat Enterprise pricing")
        self.assertEqual(ask.activity_text("mcp__scoutfetch__fetch_page", {"url": "https://www.mistral.ai/news/x"}), "Reading: mistral.ai")
        self.assertEqual(ask.activity_text("mcp__scoutsources__page_history", {"url": "https://openai.com/pricing"}), "Reading: openai.com (archived copy)")
        self.assertEqual(ask.activity_text("mcp__scoutsources__sec_fact", {"company": "Salesforce", "concept": "Revenues"}), "Checking SEC EDGAR: Salesforce")
        self.assertEqual(ask.activity_text("mcp__scoutsources__job_postings", {"host": "greenhouse", "token": "anthropic"}), "Checking job postings: greenhouse")
        self.assertEqual(ask.activity_text("ToolSearch", {"query": "x"}), "")

    def test_the_hook_is_live_only_during_research_and_reaches_on_stage(self):
        from scout import generate
        seen = []
        def research(q, known, ctx, history=None):
            self.assertIsNotNone(generate._ON_TOOL)
            generate._emit_tool("WebSearch", {"query": "q1"})
            return _research([], [])(q, known, ctx)
        ask.ask("q", research=research, verify=_verify({}), grounder=_grounder(set()), on_stage=lambda k, e="": seen.append((k, e)))
        self.assertIn(("activity", "Searching: q1"), seen); self.assertIsNone(generate._ON_TOOL)

    def test_record_id_and_failure_record(self):
        a = ask.ask("q", research=_research([], []), verify=_verify({}), grounder=_grounder(set()), record_id="a_abcdefabcdef")
        self.assertEqual(a["id"], "a_abcdefabcdef")
        written = {}
        with mock.patch("scout.selfserve.write_data", side_effect=lambda path, text, msg: written.__setitem__(path, text)):
            ask.persist_failure("a_abcdefabcdef", "q", "Scout hit a problem answering that.", 0.4, "2026-09-28T22:05:50")
        rec = json.loads(written["ask/2026-09/a_abcdefabcdef.json"])
        self.assertTrue(rec["failed"]); self.assertEqual(rec["cost_usd"], 0.4); self.assertEqual(rec["paragraphs"], [])


class FactIdRepair(unittest.TestCase):
    """RC 2026-09-28: the research model returned four facts without an "id" field; all four were
    cut as malformed, every sentence lost its cites, and $1.03 bought an empty answer."""

    def test_missing_or_aliased_ids_and_loose_cites_are_repaired(self):
        facts = [{"claim": "a", "source_url": "https://x/1"}, {"fact_id": "z9", "claim": "b"}, {"id": "n1", "claim": "collides with a known id"}]
        entries = [{"text": "one", "cites": ["1"]}, {"text": "two", "cites": ["[z9]", "N1"]}, {"text": "three", "cites": ["c_known", "F3"]}]
        ask.repair_fact_ids(facts, entries, {"n1", "c_known"})
        self.assertEqual([f["id"] for f in facts], ["f1", "z9", "f3"])
        self.assertEqual([e["cites"] for e in entries], [["f1"], ["z9", "n1"], ["c_known", "f3"]])

    def test_end_to_end_a_fact_without_an_id_still_reaches_the_answer(self):
        facts = [{"claim": "c", "claim_type": "fact", "source_url": "https://www.cnbc.com/x", "source_tier": "reputable_secondary",
                  "evidence_excerpt": NEWS, "as_of": "2026-07-31"}]   # no "id"
        answer = [{"text": "Agentforce passed 1,000 paid deals.", "cites": ["1"]}]
        a = ask.ask("q", research=_research(facts, answer), verify=_verify({0: ("confirm", "none", "ok")}), grounder=_grounder({"f1"}))
        self.assertEqual(len(a["paragraphs"]), 1); self.assertEqual(a["cut_log"], [])
        self.assertEqual(a["sources"][0]["id"], "f1")


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


class Quick(unittest.TestCase):
    """The quick path (2026-09-28): everything Scout holds about the named companies across every
    card, labelled takes, one draft, the code floor, the SAME verifier, rejects cut (never rewritten)."""
    METAS = {"anthropic__vs__openai__x": {"competitor": "OpenAI", "my_company": "Anthropic"},
             "mistral__vs__openai__y": {"competitor": "OpenAI", "my_company": "Mistral"},
             "google-cloud__vs__aws__z": {"competitor": "AWS", "my_company": "Google Cloud"}}
    CLAIMS = {
        "anthropic__vs__openai__x": [
            {"id": "c_a1", "claim_type": "fact", "claim": "OpenAI set GPT-6 Astra's price at $10 per million input tokens.", "source_url": "https://openai.com/pricing",
             "source_tier": "primary", "evidence_excerpt": "GPT-6 Astra: $10 per million input tokens, $50 per million output tokens", "as_of": "2026-09-03",
             "grounding": {"match": True}, "subject_key": "openai | api-list-price"},
            {"id": "c_a2", "claim_type": "interpretation", "claim": "OpenAI is often already in the building.", "zone": "where_they_win", "section": "battlecard",
             "as_of": "2026-06-24", "subject_key": "battlecard | install-base"}],
        "mistral__vs__openai__y": [
            {"id": "c_m1", "claim_type": "fact", "claim": "Mistral closed a €3 billion Series D on September 8, 2026.", "source_url": "https://mistral.ai/news/d",
             "source_tier": "primary", "evidence_excerpt": "Mistral AI announced a €3 billion Series D on September 8, 2026 at a valuation above €21 billion", "as_of": "2026-09-08",
             "grounding": {"match": True}, "subject_key": "mistral | valuation"},
            {"id": "c_m2", "claim_type": "fact", "claim": "retired", "status": "retired", "source_url": "https://x/y", "source_tier": "primary", "evidence_excerpt": "z" * 50, "grounding": {"match": True}}],
        "google-cloud__vs__aws__z": [
            {"id": "c_g1", "claim_type": "fact", "claim": "AWS thing", "source_url": "https://aws.amazon.com/x", "source_tier": "primary", "evidence_excerpt": "a" * 50, "as_of": "2026-09-01", "grounding": {"match": True}}]}

    def setUp(self):
        self.p = [mock.patch.object(ask.display, "list_battlecards", return_value=list(self.METAS)),
                  mock.patch.object(ask.store, "load_meta", side_effect=lambda s: self.METAS.get(s)),
                  mock.patch.object(ask.store, "load_claims", side_effect=lambda s: self.CLAIMS.get(s, []))]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_fact_base_is_every_card_naming_the_companies_plus_labelled_takes(self):
        known = ask.known_facts_for(["OpenAI"])
        ids = [f["id"] for f in known]
        self.assertEqual(sorted(ids), ["c_a1", "c_a2", "c_m1"])              # both OpenAI cards; AWS card and the retired fact excluded
        take = [f for f in known if f["id"] == "c_a2"][0]
        self.assertTrue(take["take"]); self.assertEqual(take["source_class"], "scout_take"); self.assertEqual(take["card"], "Anthropic vs OpenAI")
        self.assertTrue(take["source_url"].startswith("/c/anthropic__vs__openai__x#"))
        self.assertEqual(ask.named_companies("My client wants Mistral instead of OpenAI"), ["OpenAI", "Mistral"])
        self.assertEqual([f["id"] for f in ask.known_facts_for(["Mistral"], takes=False)], ["c_m1"])

    def test_quick_answer_verifies_and_cuts_instead_of_rewriting(self):
        calls = {"verify": 0, "draft": []}
        def draft(q, known, ctx, history=None):
            calls["draft"].append([f["id"] for f in known])
            return {"text": "```json\n" + json.dumps({"answer": [
                {"text": "OpenAI's GPT-6 Astra lists at $10 per million input tokens.", "cites": ["c_a1"]},
                {"text": "Mistral closed a €3 billion Series D on September 8, 2026.", "cites": ["C_M1"]},        # cite spelling normalized
                {"text": "Scout's take: OpenAI is often already in the building.", "cites": ["c_a2"]},
                {"text": "OpenAI charges $12 per million.", "cites": ["c_a1"]}],                                # floor: 12 not in evidence
                "unanswered": ["a head-to-head price for 500 seats"]}) + "\n```", "cost_usd": 0.12}
        def verify(entries, facts_by_id):
            calls["verify"] += 1
            self.assertEqual(len(entries), 3)                                                                  # the floor failure never reached the judge
            return _verify({0: ("confirm", "none", "ok"), 1: ("confirm", "none", "ok"), 2: ("reject", "prose", "overreaches")})(entries, facts_by_id)
        seen = []
        a = ask.quick_ask("Is OpenAI or Mistral the better bet?", slug="google-cloud__vs__aws__z", draft=draft, verify=verify, on_stage=lambda k, e="": seen.append(k))
        self.assertEqual(a["kind"], "quick"); self.assertEqual(calls["verify"], 1)
        self.assertEqual(sorted(calls["draft"][0]), ["c_a1", "c_a2", "c_m1"])                                    # both companies, every card
        self.assertEqual([p["text"][:20] for p in a["paragraphs"]], ["OpenAI's GPT-6 Astra", "Mistral closed a €3 "])
        self.assertEqual(a["trajectory"]["rewritten"], 0); self.assertEqual(a["trajectory"]["judge_rejected"], 1); self.assertEqual(a["trajectory"]["floor_dropped"], 1)
        reasons = " | ".join(c["reason"] for c in a["cut_log"]); self.assertIn("verifier: overreaches", reasons); self.assertIn("floor:", reasons)
        self.assertEqual(a["unanswered"], ["a head-to-head price for a figure seats"])
        self.assertEqual((a["competitor"], a["slug"]), ("OpenAI", "mistral__vs__openai__y"))                    # scope: the card naming both
        self.assertEqual(seen, ["facts", "draft", "floor", "verify", "done"])
        self.assertTrue(a["verified"]); self.assertLess(a["cost_usd"], 1.0)

    def test_quick_take_renders_as_a_take_and_all_cut_is_honest(self):
        draft = lambda q, known, ctx, history=None: {"text": "```json\n" + json.dumps({"answer": [{"text": "Scout's take: OpenAI is often already in the building.", "cites": ["c_a2"]}], "unanswered": []}) + "\n```", "cost_usd": 0.1}
        a = ask.quick_ask("Where does OpenAI win?", draft=draft, verify=_verify({0: ("confirm", "none", "ok")}))
        self.assertTrue(a["sources"][0]["take"]); self.assertEqual(a["sources"][0]["class"], "scout_take"); self.assertEqual(a["sources"][0]["card"], "Anthropic vs OpenAI")
        a = ask.quick_ask("Where does OpenAI win?", draft=draft, verify=_verify({}))
        self.assertEqual(a["paragraphs"], []); self.assertFalse(a["verified"]); self.assertEqual(a["trajectory"]["judge_rejected"], 1)


class DeepCap(unittest.TestCase):
    def test_a_deep_answer_keeps_its_first_four_confirmed_sentences(self):
        facts = [_fact("n1", "https://www.cnbc.com/x", NEWS, "reputable_secondary")]
        answer = [{"text": t, "cites": ["n1"]} for t in (
            "Agentforce closed more than 1,000 paid deals.", "That is up from 200 in the prior quarter.",
            "Salesforce reported the figures for the quarter.", "The count refers to paid deals only.",
            "Growth was fivefold quarter over quarter.", "Agentforce is Salesforce's agent product.")]
        a = ask.ask("q", research=_research(facts, answer), verify=_verify({i: ("confirm", "none", "ok") for i in range(6)}), grounder=_grounder({"n1"}))
        self.assertEqual(len(a["paragraphs"]), 4); self.assertEqual(a["trajectory"]["trimmed"], 2); self.assertEqual(a["kind"], "deep")
        self.assertFalse(any("restates" in c["reason"] for c in a["cut_log"]))   # numbered distinct sentences are not restatements


class CiteRepair(unittest.TestCase):
    def test_a_number_from_an_uncited_known_fact_gets_its_cite(self):
        facts = {"c1": _fact("c1", "https://openai.com/p", "GPT-6 Astra: $10 per million input tokens, $50 per million output tokens"),
                 "c2": _fact("c2", "https://anthropic.com/p", "Claude Opus 5.5: $4 per million input tokens, $20 per million output tokens"),
                 "c3": _fact("c3", "https://x.com/p", "Nothing relevant here at all, no figures.")}
        e = {"text": "GPT-6 Astra lists at $10/$50 while Opus 5.5 lists at $4/$20.", "cites": ["c1"]}
        self.assertTrue(ask.floor_check(e, facts))                       # $4 and $20 are not in c1
        self.assertEqual(ask.repair_cites(e, facts), ["c2"])
        self.assertEqual(e["cites"], ["c1", "c2"]); self.assertEqual(ask.floor_check(e, facts), [])
        # an invented number stays unsupported: nothing is added, the floor still cuts
        e2 = {"text": "Astra costs $12 per million.", "cites": ["c1"]}
        self.assertEqual(ask.repair_cites(e2, facts), []); self.assertTrue(ask.floor_check(e2, facts))

    def test_quick_path_repairs_then_verifies(self):
        with mock.patch.object(ask, "known_facts_for", return_value=[
                dict(_fact("c1", "https://openai.com/p", "GPT-6 Astra: $10 per million input tokens, $50 per million output tokens"), from_card=True),
                dict(_fact("c2", "https://anthropic.com/p", "Claude Opus 5.5: $4 per million input tokens, $20 per million output tokens"), from_card=True)]), \
             mock.patch.object(ask, "infer_competitor", return_value=("OpenAI", "anthropic__vs__openai__x")), \
             mock.patch.object(ask.store, "load_meta", return_value={"competitor": "OpenAI", "my_company": "Anthropic"}), \
             mock.patch.object(ask, "named_companies", return_value=["OpenAI", "Anthropic"]):
            draft = lambda q, known, ctx, history=None: {"text": "```json\n" + json.dumps({"answer": [{"text": "GPT-6 Astra lists at $10/$50 while Opus 5.5 lists at $4/$20.", "cites": ["c1"]}], "unanswered": []}) + "\n```", "cost_usd": 0.1}
            seen = {}
            def verify(entries, facts_by_id):
                seen["cites"] = entries[0]["cites"]
                return _verify({0: ("confirm", "none", "ok")})(entries, facts_by_id)
            a = ask.quick_ask("Is OpenAI cheaper?", draft=draft, verify=verify)
        self.assertEqual(seen["cites"], ["c1", "c2"])                     # the judge saw both facts
        self.assertEqual(len(a["paragraphs"]), 1); self.assertEqual(a["paragraphs"][0]["cites"], [1, 2]); self.assertEqual(a["trajectory"]["cites_repaired"], 1)
