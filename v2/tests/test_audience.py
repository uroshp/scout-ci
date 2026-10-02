"""Audience leads, Level 2 (2026-10-02): planned from the card, authored and judged like any edit,
applied only when confirmed, never without the pack block, never a retire without its replacement."""
import unittest
from unittest import mock

from scout import audience, config, judgment


def _card():
    lead = {"id": "c_lead", "section": "executive_summary", "order": 0, "claim": "**Lead.** body",
            "derived_from": "c_fact1", "subject_key": "x | lead | current"}
    fact1 = {"id": "c_fact1", "section": "tracked_facts", "claim": "fact one", "source_url": "https://a.example/1",
             "source_tier": "1A", "as_of": "2026-10-01", "subject_key": "x | f1"}
    fact2 = {"id": "c_fact2", "section": "tracked_facts", "claim": "fact two", "source_url": "https://a.example/2",
             "source_tier": "1A", "as_of": "2026-10-01", "subject_key": "x | f2"}
    plays = [{"id": f"c_p{i}", "section": "battlecard", "zone": "where_we_win", "persona": "economic_buyer",
              "claim": f"play {i}", "derived_from": "c_fact2", "subject_key": f"x | p{i}", "order": i} for i in range(2)]
    one = [{"id": "c_s1", "section": "battlecard", "zone": "contested", "persona": "security_regulated",
            "claim": "sec play", "derived_from": "c_fact2", "subject_key": "x | s1", "order": 9}]
    return [lead, fact1, fact2] + plays + one


class Plan(unittest.TestCase):
    def test_skips_itself_without_the_pack_block(self):
        with mock.patch.object(judgment, "optional", return_value=None):
            pl = audience.plan(_card())
        self.assertEqual(pl["ops"], []); self.assertIn("audience._OP_BRIEF", pl["skipped"])

    def test_plans_one_add_per_buyer_with_enough_plays(self):
        with mock.patch.object(judgment, "optional", return_value="Brief for the ⟦persona_label⟧: ⟦n_plays⟧ plays"):
            pl = audience.plan(_card())
        self.assertEqual(pl["personas"], ["economic_buyer"])          # security has 1 play: below the bar
        self.assertEqual([o["operation"] for o in pl["ops"]], ["add"])
        op = pl["ops"][0]
        self.assertEqual(op["section"], "executive_summary"); self.assertIsNone(op["zone"])
        self.assertEqual(op["persona"], "economic_buyer"); self.assertEqual(op["derived_from"], "c_fact1")
        self.assertEqual(op["subject_key"], "audience-lead | economic_buyer | c_lead")
        self.assertIn("economic buyer: 2 plays", op["why"])
        self.assertEqual({f["id"] for f in pl["facts"]}, {"c_fact1", "c_fact2"})

    def test_current_lead_is_not_rewritten_and_a_stale_one_is_retired(self):
        card = _card()
        with mock.patch.object(judgment, "optional", return_value="b"):
            card.append({"id": "c_al", "section": "executive_summary", "persona": "economic_buyer", "order": 7,
                         "subject_key": "audience-lead | economic_buyer | c_lead", "claim": "written", "derived_from": "c_fact1"})
            self.assertEqual(audience.plan(card)["ops"], [])
            card[-1]["subject_key"] = "audience-lead | economic_buyer | c_old"
            ops = audience.plan(card)["ops"]
        self.assertEqual([o["operation"] for o in ops], ["retire", "add"])
        self.assertEqual(ops[0]["target_subject_key"], "audience-lead | economic_buyer | c_old")

    def test_no_anchor_fact_means_nothing(self):
        card = [c for c in _card() if c["id"] != "c_fact1"]
        with mock.patch.object(judgment, "optional", return_value="b"):
            self.assertEqual(audience.plan(card)["ops"], [])


class Refresh(unittest.TestCase):
    def _run(self, verdict, write=True):
        calls = {}
        def author(meta, ops, facts, claims):
            calls["author"] = ops
            return {"ops": [dict(o, claim="**Angle for you.** body", claim_type="interpretation") for o in ops], "cost_usd": 0.05}
        def floor(op, surviving, active): return []
        def judge(meta, facts, claims, indexed):
            calls["judged"] = [i for i, _ in indexed]
            return {"verdicts": {i: {"verdict": verdict, "judged_by": "claude-opus-4-8", "reason": "r"} for i, _ in indexed}, "cost_usd": 0.25}
        def apply(claims, confirmed, facts, slug, today):
            calls["applied"] = confirmed
            return {"claims": claims + [{"id": "new"}], "applied": [{"operation": "add", "subject_key": confirmed[0]["subject_key"]}], "skipped": [], "held": []}
        def log(slug, records, source="", facts=None): calls["log"] = (source, len(records))
        with mock.patch.object(judgment, "optional", return_value="b"):
            out = audience.refresh("slug", {"competitor": "A"}, _card(), "2026-10-02", write=write,
                                   author=author, floor=floor, judge=judge, apply=apply, log=log)
        return out, calls

    def test_confirmed_lead_is_applied_and_logged_with_cost(self):
        out, calls = self._run("confirm")
        self.assertEqual(len(out["applied"]), 1); self.assertAlmostEqual(out["cost_usd"], 0.30)
        self.assertEqual(calls["log"], ("audience", 1)); self.assertEqual(out["personas"], ["economic_buyer"])

    def test_rejected_lead_touches_nothing(self):
        out, calls = self._run("reject")
        self.assertEqual(out["applied"], []); self.assertNotIn("applied", calls)
        self.assertEqual(out["rejected"][0]["persona"], "economic_buyer")

    def test_dry_run_applies_nothing(self):
        out, calls = self._run("confirm", write=False)
        self.assertNotIn("applied", calls); self.assertTrue(out["applied"][0]["dry_run"])

    def test_cap_per_card_per_run(self):
        card = _card() + [{"id": f"c_t{i}", "section": "battlecard", "zone": "contested", "persona": "technical_evaluator",
                           "claim": "t", "derived_from": "c_fact2", "subject_key": f"x | t{i}", "order": 20 + i} for i in range(2)]
        with mock.patch.object(judgment, "optional", return_value="b"), mock.patch.object(config, "AUDIENCE_MAX_PER_CARD_RUN", 1):
            self.assertEqual(len(audience.plan(card, limit=config.AUDIENCE_MAX_PER_CARD_RUN)["personas"]), 1)
            self.assertEqual(len(audience.plan(card)["personas"]), 2)


class Optional(unittest.TestCase):
    def test_optional_never_registers_a_block(self):
        before = list(judgment._asked)
        self.assertIsNone(judgment.optional("nope._MISSING"))
        self.assertEqual(judgment._asked, before)


if __name__ == "__main__":
    unittest.main()
