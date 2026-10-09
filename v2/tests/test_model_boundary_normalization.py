"""The model boundary normalizes REPRESENTATION in code (2026-10-04).

Sonnet 5.5 writes `confidence` as 0.85 / 0.9 where Sonnet 5 wrote "high". The prompts never spell
the enum out (it lives in scout/schema.py), so with the 10/3 model change EVERY own-side fact failed
the pre-grounding schema check and was dropped without a word: the rehearsal of the OpenAI vs
Anthropic card found the DevDay collaboration launches, sourced them with verbatim excerpts, and the
step row read "0 anchor fact(s) grounded of 4 candidate(s)" as if grounding had failed. These tests
pin the fix: numbers and loose strings map onto the enum before any schema check, in both monitor
arms and in generation, and a fact the schema still rejects is NAMED in the step row (and fails the
step when nothing survived)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import monitor, schema  # noqa: E402


class NormalizeConfidence(unittest.TestCase):
    def test_numbers_map_onto_the_enum(self):
        self.assertEqual(schema.normalize_confidence(0.9), "high")
        self.assertEqual(schema.normalize_confidence(0.85), "high")
        self.assertEqual(schema.normalize_confidence(0.75), "high")
        self.assertEqual(schema.normalize_confidence(0.6), "medium")
        self.assertEqual(schema.normalize_confidence(0.3), "low")
        self.assertEqual(schema.normalize_confidence(1), "high")
        self.assertEqual(schema.normalize_confidence(90), "high")      # a 0-100 scale
        self.assertEqual(schema.normalize_confidence(50), "medium")

    def test_strings_are_trimmed_and_lowercased(self):
        self.assertEqual(schema.normalize_confidence(" High "), "high")
        self.assertEqual(schema.normalize_confidence("MEDIUM"), "medium")
        self.assertEqual(schema.normalize_confidence("very high"), "high")
        self.assertEqual(schema.normalize_confidence("0.9"), "high")
        self.assertEqual(schema.normalize_confidence("certain"), "certain")   # still named by the schema

    def test_normalize_claim_touches_representation_only(self):
        c = {"subject_key": "x", "claim": "Text.", "confidence": 0.9, "source_tier": "Primary ", "claim_type": "Fact"}
        out = schema.normalize_claim(c)
        self.assertIs(out, c)
        self.assertEqual(c["confidence"], "high")
        self.assertEqual(c["source_tier"], "primary")
        self.assertEqual(c["claim_type"], "fact")
        self.assertEqual(c["claim"], "Text.")


def _sonnet55_fact(subject_key: str, conf) -> dict:
    """The shape the own-company step emitted on 2026-10-04 (verbatim keys, numeric confidence)."""
    return {"claim": {"subject_key": subject_key,
                      "claim": "At DevDay on September 29 OpenAI made @ChatGPT mentionable in Slack and Microsoft Teams channels.",
                      "claim_type": "fact", "section": "tracked_facts", "zone": None, "order": 1,
                      "source_url": "https://thenextweb.com/news/openai-devday", "source_tier": "reputable_secondary",
                      "evidence_excerpt": "People can now mention @ChatGPT in Slack and Microsoft Teams channels, threads and direct messages.",
                      "as_of": "2026-09-29", "confidence": conf,
                      "candidate_sources": [{"source_url": "https://thenextweb.com/news/openai-devday",
                                             "source_tier": "reputable_secondary",
                                             "evidence_excerpt": "People can now mention @ChatGPT in Slack and Microsoft Teams channels, threads and direct messages."}]},
            "alert": {"old_value": "Workspace Agents", "new_value": "@ChatGPT in Slack and Teams", "headline": "h", "so_what": "s", "severity": "act"}}


class OwnCompanyArmBoundary(unittest.TestCase):
    """_my_company_facts with the model faked to Sonnet 5.5's output and grounding faked to keep
    whatever reaches it: the facts must REACH grounding."""

    def _run(self, facts):
        text = json.dumps({"facts": facts, "immaterial": []})
        kept_ids = []

        def fake_ground_best(claims):
            # what the real grounder returns: candidate_sources stripped, the grounding record attached
            kept_ids.extend(c["subject_key"] for c in claims)
            kept = []
            for c in claims:
                k = {kk: v for kk, v in c.items() if kk != "candidate_sources"}
                k["grounding"] = {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-10-04", "detail": None}
                kept.append(k)
            return {"kept": kept, "failed": [], "cut": []}

        with mock.patch.object(monitor, "_run_my_facts", new=mock.AsyncMock(return_value={"text": text, "cost_usd": 0.2})), \
             mock.patch.object(monitor, "_ground_best", side_effect=fake_ground_best):
            out = monitor._my_company_facts("openai__vs__anthropic__x", {"competitor": "Anthropic", "my_company": "OpenAI"},
                                            "2026-09-15", [{"signal": "s", "about": "my_company"}], [])
        return out, kept_ids

    def test_numeric_confidence_reaches_grounding_and_lands(self):
        out, kept = self._run([_sonnet55_fact("openai|flagship-product|collaboration-agent", 0.9),
                               _sonnet55_fact("openai|chatgpt-space", 0.88)])
        self.assertEqual(kept, ["openai|flagship-product|collaboration-agent", "openai|chatgpt-space"])
        self.assertEqual(len(out["grounded"]), 2)
        self.assertEqual(out["schema_rejected"], [])
        self.assertEqual(out["emitted"], 2)
        self.assertEqual(out["grounded"][0][0]["confidence"], "high")

    def test_a_fact_the_schema_still_rejects_is_named(self):
        bad = _sonnet55_fact("openai|bad", 0.9)
        bad["claim"]["source_tier"] = "blog"                   # not a tier the schema knows
        del bad["claim"]["candidate_sources"]
        out, kept = self._run([bad, _sonnet55_fact("openai|good", "High")])
        self.assertEqual(kept, ["openai|good"])
        self.assertEqual(len(out["schema_rejected"]), 1)
        self.assertTrue(out["schema_rejected"][0].startswith("openai|bad: "))


class StepRowNamesSchemaRejects(unittest.TestCase):
    def test_everything_rejected_fails_the_step(self):
        status, detail = monitor._arm_status(0, 4, "anchor fact(s) grounded", 4,
                                             ["a: confidence: 0.9 is not one of [...]"] * 4)
        self.assertEqual(status, "failed")
        self.assertIn("4 of 4 emitted fact(s) rejected by the schema before grounding", detail)
        self.assertIn("0 anchor fact(s) grounded of 4 candidate(s)", detail)

    def test_partial_rejects_are_named_but_the_step_ran(self):
        status, detail = monitor._arm_status(1, 3, "grounded", 2, ["x: as_of: 'soon' is not a date"])
        self.assertEqual(status, "ran")
        self.assertIn("1 of 2 emitted fact(s) rejected", detail)

    def test_clean_row_is_unchanged(self):
        self.assertEqual(monitor._arm_status(2, 3, "grounded", 2, []), ("ran", "2 grounded of 3 candidate(s)"))


if __name__ == "__main__":
    unittest.main()


class OneUpdatePerClaimPerRun(unittest.TestCase):
    """2026-10-04: both arms landed the DevDay @ChatGPT launch on the same claim id -> two alerts."""

    def _claim(self, key, text):
        from scout.schema import claim_id
        return {"id": claim_id("s", key), "subject_key": key, "claim": text, "claim_type": "fact",
                "section": "tracked_facts", "zone": None, "order": 1, "source_url": "https://x.test/a",
                "source_tier": "primary", "evidence_excerpt": "e", "as_of": "2026-09-29", "confidence": "high", "verified": True}

    def test_second_arm_cannot_re_alert_the_same_claim(self):
        stored = [self._claim("openai | flagship-product | collaboration-agent", "old text")]
        comp = (self._claim("openai | flagship-product | collaboration-agent", "competitor-arm text"),
                {"new_value": "v1", "headline": "h1", "severity": "act"})
        own = (self._claim("openai|flagship-product|collaboration-agent", "own-arm text"),
               {"new_value": "v2", "headline": "h2", "severity": "act"})
        self.assertEqual(comp[0]["id"], own[0]["id"])              # ids already ignore the spacing
        updated = set()
        claims, alerts = monitor._apply_updates(stored, [comp], [], updated)
        claims, alerts2 = monitor._apply_updates(claims, [own], [], updated)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts2, [])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["claim"], "competitor-arm text")
        self.assertEqual(claims[0]["subject_key"], "openai | flagship-product | collaboration-agent")

    def test_revision_keeps_the_stored_spelling_and_new_keys_take_the_convention(self):
        stored = [self._claim("anthropic | list-price | enterprise", "old")]
        rev = (self._claim("anthropic|list-price|enterprise", "new"), {"new_value": "n", "headline": "h", "severity": "watch"})
        new = (self._claim("openai|pro-plan|reopen-halved", "brand new"), {"new_value": "n2", "headline": "h2", "severity": "act"})
        claims, alerts = monitor._apply_updates(stored, [rev, new], [])
        keys = sorted(c["subject_key"] for c in claims)
        self.assertEqual(keys, ["anthropic | list-price | enterprise", "openai | pro-plan | reopen-halved"])
        self.assertEqual([a["subject_key"] for a in alerts], ["anthropic | list-price | enterprise", "openai | pro-plan | reopen-halved"])


class DecidedSubjectsDoNotHoldTheWindow(unittest.TestCase):
    """2026-10-04: the $517B compute deal was judged immaterial (old news) and still held the window."""

    def test_judged_immaterial_subjects_are_matched_on_the_signal(self):
        subst = [{"signal": "2026-10-02: Anthropic secures $517 billion in compute", "subject_key": "anthropic|compute-strain"},
                 {"signal": "2026-09-29: OpenAI puts @ChatGPT in Slack", "subject_key": "openai | flagship-product | collaboration-agent"}]
        imm = [{"signal": "2026-10-02: Anthropic secures $517 billion in compute", "why_not": "old news"}]
        self.assertEqual(monitor._judged_immaterial_subjects(subst, imm), {"anthropic|compute strain"})

    def test_a_window_held_for_a_now_decided_subject_closes(self):
        meta = {"unresolved_since": "2026-09-15", "unresolved_attempts": 1, "unresolved_subjects": ["anthropic|compute-strain"]}
        result = {}
        monitor._resolve_or_hold(meta, [], result, decided={"anthropic|compute strain"})
        self.assertIsNone(meta.get("unresolved_since"))
        self.assertEqual(result["unresolved_resolved"]["by"], "judged immaterial")

    def test_an_undecided_held_subject_keeps_the_window(self):
        meta = {"unresolved_since": "2026-09-15", "unresolved_attempts": 1, "unresolved_subjects": ["anthropic|compute-strain", "openai|x"]}
        result = {}
        monitor._resolve_or_hold(meta, [], result, decided={"anthropic|compute strain"})
        self.assertEqual(meta.get("unresolved_since"), "2026-09-15")
        self.assertEqual(meta.get("unresolved_attempts"), 2)

    def test_an_alert_on_a_held_subject_resolves_regardless_of_spacing(self):
        meta = {"unresolved_since": "2026-09-15", "unresolved_attempts": 1, "unresolved_subjects": ["openai|flagship-product|collaboration-agent"]}
        result = {}
        monitor._resolve_or_hold(meta, [{"subject_key": "openai | flagship-product | collaboration-agent"}], result)
        self.assertIsNone(meta.get("unresolved_since"))
        self.assertEqual(result["unresolved_resolved"]["by"], "alert")


class PreBaselineAlertsAreNotMisses(unittest.TestCase):
    def test_level_a_marks_events_before_the_baseline(self):
        from scout.sensors import compare
        landed = [({"subject_key": "k1", "as_of": "2026-09-29", "claim": "OpenAI launched ChatGPT Space at DevDay", "source_url": "https://thenextweb.com/a"},
                   {"headline": "h"}),
                  ({"subject_key": "k2", "as_of": "2026-10-06", "claim": "Anthropic raised the price of Opus", "source_url": "https://www.anthropic.com/news/x"},
                   {"headline": "h"})]
        rows = compare.level_a(landed, [], [], set(), names=["OpenAI", "Anthropic"], baseline="2026-10-05")
        self.assertEqual(rows[0].get("covered_by"), "pre_baseline")
        self.assertFalse(rows[0].get("miss"))
        self.assertTrue(rows[1].get("miss"))

    def test_without_a_baseline_nothing_changes(self):
        from scout.sensors import compare
        landed = [({"subject_key": "k1", "as_of": "2026-09-29", "claim": "x y z", "source_url": "https://thenextweb.com/a"}, {"headline": "h"})]
        rows = compare.level_a(landed, [], [], set(), names=[])
        self.assertTrue(rows[0].get("miss"))


class ZoneAliasesAndStoredHome(unittest.TestCase):
    """2026-10-05, the first audited morning: Opus 5.5 wrote `zone: "weaknesses"` on a fact keyed like a
    stored play minus its last field; the schema dropped it and Batman's window stayed held."""

    def test_zone_aliases_read_from_our_side(self):
        self.assertEqual(schema.normalize_zone("weaknesses"), "where_we_win")
        self.assertEqual(schema.normalize_zone("Their strengths"), "where_they_win")
        self.assertEqual(schema.normalize_zone("toss-up"), "contested")
        self.assertEqual(schema.normalize_zone("where_we_win"), "where_we_win")
        self.assertEqual(schema.normalize_zone("bogus"), "bogus")          # still named by the schema

    def test_resolve_subject_key(self):
        stored = ["superman | key-person-concentration | current", "superman | box-office | opening", "superman | box-office | total"]
        self.assertEqual(schema.resolve_subject_key("superman|key-person-concentration|current", stored), stored[0])
        self.assertEqual(schema.resolve_subject_key("superman | key-person-concentration", stored), stored[0])   # unique prefix
        self.assertEqual(schema.resolve_subject_key("superman | box-office", stored), "superman | box-office")   # ambiguous: unchanged
        self.assertEqual(schema.resolve_subject_key("superman | new-thing", stored), "superman | new-thing")

    def _stored(self):
        return [{"subject_key": "superman | key-person-concentration | current", "claim_type": "interpretation",
                 "section": "battlecard", "zone": "where_we_win", "id": "c_1"}]

    def test_a_fact_aimed_at_a_play_lands_in_recent_moves_under_its_own_key(self):
        c = {"subject_key": "superman | key-person-concentration", "claim_type": "fact", "section": "battlecard", "zone": "weaknesses", "confidence": 0.9}
        schema.normalize_claim(c)
        monitor._adopt_home(c, self._stored())
        self.assertEqual(c["subject_key"], "superman | key-person-concentration | current | fact")
        self.assertEqual((c["section"], c["zone"], c["persona"]), ("recent_moves", None, None))

    def test_an_interpretation_revision_keeps_the_stored_home_when_the_zone_is_unknown(self):
        c = {"subject_key": "superman | key-person-concentration", "claim_type": "interpretation", "section": "battlecard", "zone": "bogus"}
        monitor._adopt_home(schema.normalize_claim(c), self._stored())
        self.assertEqual(c["subject_key"], "superman | key-person-concentration | current")
        self.assertEqual(c["zone"], "where_we_win")

    def test_a_valid_different_zone_is_the_judges_call_and_stays(self):
        c = {"subject_key": "superman | key-person-concentration | current", "claim_type": "interpretation", "section": "battlecard", "zone": "contested"}
        monitor._adopt_home(schema.normalize_claim(c), self._stored())
        self.assertEqual(c["zone"], "contested")

    def test_a_new_fact_in_a_fact_section_is_untouched(self):
        c = {"subject_key": "superman | sequel | greenlight", "claim_type": "fact", "section": "recent_moves", "zone": None}
        monitor._adopt_home(schema.normalize_claim(c), self._stored())
        self.assertEqual(c["subject_key"], "superman | sequel | greenlight")
        self.assertEqual(c["section"], "recent_moves")


class DecidedSubjectsMatchOnMeaning(unittest.TestCase):
    """2026-10-09: three windows were 'abandoned' in the owner's email after three mornings of paid
    re-checks, every one for a subject the judge had already ruled immaterial, because the judge echoes
    the signal in its own words and the exact-prefix match failed."""

    def test_the_judges_paraphrase_still_counts_as_decided(self):
        subst = [{"signal": "Slack reworked its service-level agreement to be less generous, reducing SLA credits from 100× to 10× customer fees",
                  "subject_key": "slack | service-level-agreement"}]
        imm = [{"signal": "Slack reworked its SLA to be less generous (credits cut from 100x to 10x)", "why_not": "a 2019 story"}]
        self.assertEqual(monitor._judged_immaterial_subjects(subst, imm), {"slack|service level agreement"})

    def test_unrelated_stories_do_not_match(self):
        subst = [{"signal": "Anthropic raises prices for Opus 5.5 by 20 percent", "subject_key": "anthropic | list-price"}]
        imm = [{"signal": "Slack reworked its SLA to be less generous", "why_not": "old"}]
        self.assertEqual(monitor._judged_immaterial_subjects(subst, imm), set())
