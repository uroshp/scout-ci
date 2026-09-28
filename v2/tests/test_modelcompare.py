"""On-device model comparison: role specs, strict parsers, alignment, delta ids, scorecard populations
and periods (scout/rolespecs.py, scout/modelcompare.py). Pure, no model, no network."""
import json
import unittest
from unittest import mock

from scout import modelcompare as mc, rolespecs, config, selfserve


def rec(role, text, call_id="c_000000000001", model=None, slug="a__vs__b", est=3000, status="ok", truncated=()):
    return {"call_id": call_id, "role": role, "slug": slug, "run_ts": "2026-09-28T11:00:00", "source": "monitor",
            "fidelity": "exact", "status": status, "model": model or rolespecs.spec(role)["primary_model"](),
            "system": {"kind": "string", "text": "S"}, "user": "U", "transcript": [],
            "result": {"text": text, "cost_usd": 0.3, "duration_ms": 900}, "sizes": {"est_tokens_in": est},
            "truncated": list(truncated)}


def rep(text, backend="apple_ondevice", status="ok", reason=None, version=None):
    return {"status": status, "reason": reason, "text": text, "backend": backend, "duration_ms": 100,
            "backend_model": backend, "backend_version": version or {"v": 1}, "cost_usd": 0.0}


class Parsers(unittest.TestCase):
    def test_judge_strict_off_vocab_is_abstain_not_reject(self):
        p = rolespecs.parse_judge('{"verdicts":[{"op_index":0,"verdict":"confirm","reason":"r"},'
                                  '{"op_index":1,"verdict":"APPROVE","reason":"r"},{"verdict":"reject"}]}')
        self.assertEqual(p["items"], {"0": "confirm"})
        self.assertEqual(p["abstain"]["1"], "off_vocab")
        self.assertIn("missing_key", p["abstain"].values())

    def test_parse_failure_is_none(self):
        self.assertIsNone(rolespecs.parse_judge("no json here"))
        self.assertIsNone(rolespecs.parse_gate_judge(""))

    def test_persona_and_election_and_route(self):
        self.assertEqual(rolespecs.parse_persona('{"persona":"economic_buyer"}')["items"], {"call": "economic_buyer"})
        self.assertEqual(rolespecs.parse_persona('{"persona":"cfo"}')["abstain"], {"call": "off_vocab"})
        e = rolespecs.parse_election('{"winner_subject_key":"x|y","margin":"Clear","rationale":"."}')
        self.assertEqual(e["items"], {"margin": "clear"})
        r = rolespecs.parse_route('{"surface_ops":[{"operation":"revise","section":"battlecard","derived_from":"c_f","target_subject_key":"A | b"}],"no_surface":[],"run_verdict":{"consequential":true}}')
        self.assertEqual(r["items"], {"consequential": "consequential"})
        self.assertEqual(len(r["extra"]["ops"]), 1)

    def test_reference_eligibility(self):
        ok, why = rolespecs.reference_eligible(rec("judge", '{"verdicts":[{"op_index":0,"verdict":"confirm","reason":"r"}]}'))
        self.assertTrue(ok)
        ok, why = rolespecs.reference_eligible(rec("judge", '{"verdicts":[]}', model=config.JUDGE_FALLBACK_MODEL))
        self.assertEqual((ok, why), (False, "fallback_model"))
        ok, why = rolespecs.reference_eligible(rec("judge", "garbage"))
        self.assertEqual((ok, why), (False, "unparsed"))


class Align(unittest.TestCase):
    JUDGE_REF = '{"verdicts":[{"op_index":0,"verdict":"confirm","reason":"r"},{"op_index":1,"verdict":"reject","reason":"r"},{"op_index":2,"verdict":"confirm","reason":"r"}]}'

    def test_judge_items_and_item_keyed_delta(self):
        r = rec("judge", self.JUDGE_REF)
        c = mc.compare_call(r, rep('{"verdicts":[{"op_index":0,"verdict":"reject","reason":"x"},{"op_index":1,"verdict":"reject","reason":"x"}]}'))
        st = {i["item_id"]: i["status"] for i in c["items"]}
        self.assertEqual(st, {"0": "disagree", "1": "agree", "2": "abstain"})
        self.assertEqual(c["summary"]["judged"], 2)
        d = next(i for i in c["items"] if i["status"] == "disagree")["delta_id"]
        # a second backend disagreeing on the same item yields the SAME delta_id (item-keyed, no backend)
        c2 = mc.compare_call(r, rep('{"verdicts":[{"op_index":0,"verdict":"reject","reason":"x"}]}', backend="ollama"))
        self.assertEqual(next(i for i in c2["items"] if i["status"] == "disagree")["delta_id"], d)
        self.assertTrue(d.startswith("m_"))

    def test_candidate_parse_failure_and_skips(self):
        r = rec("judge", self.JUDGE_REF)
        self.assertEqual(mc.compare_call(r, rep("nope"))["summary"]["candidate_parse"], "parse_fail")
        self.assertEqual(mc.compare_call(r, rep(None, status="skipped", reason="context_exceeded"))["summary"]["candidate_parse"],
                         "context_exceeded")

    def test_prose_role_has_pairs_no_kappa(self):
        r = rec("reformat", '{"claim":"**A**\\n\\nb.\\n\\n**Soundbite:** \\"c\\""}')
        c = mc.compare_call(r, rep('{"claim":"**A**\\n\\nb2.\\n\\n**Soundbite:** \\"c\\""}'))
        self.assertEqual(c["family"], rolespecs.GENERATIVE)
        self.assertIsNone(c["summary"]["kappa"])
        self.assertEqual(c["items"][0]["status"], "pair")
        self.assertTrue(c["items"][0]["pair_delta_id"].startswith("p_"))

    def test_election_kappa_on_margin_and_winner_agree(self):
        r = rec("lead_election", '{"winner_subject_key":"a|b","margin":"clear","rationale":"."}')
        c = mc.compare_call(r, rep('{"winner_subject_key":"a|b","margin":"marginal","rationale":"."}'))
        self.assertEqual(c["items"][0]["item_id"], "margin")
        self.assertEqual(c["items"][0]["status"], "disagree")
        self.assertTrue(c["extra"]["winner_agree"])

    def test_triage_escalation_unit_never_collapses_new(self):
        ref = '{"has_candidates":true,"candidates":[{"signal":"A on 2026-09-27","subject_key":"NEW","about":"competitor","substantial":true},{"signal":"B on 2026-09-27","subject_key":"NEW","about":"competitor","substantial":false}]}'
        r = rec("triage", ref)
        c = mc.compare_call(r, rep('{"has_candidates":true,"candidates":[{"signal":"B","subject_key":"NEW","about":"competitor","substantial":false}]}'))
        self.assertEqual(c["items"][0]["reference"], "escalate")
        self.assertEqual(c["items"][0]["candidate"], "quiet")
        self.assertEqual(c["items"][0]["status"], "disagree")

    def test_materiality_per_candidate(self):
        ref = '{"material":[{"claim":{"subject_key":"x | y"}}],"immaterial":[{"signal":"z","why_not":"."}]}'
        c = mc.compare_call(rec("materiality", ref), rep('{"material":[],"immaterial":[{"signal":"x | y"},{"signal":"z"}]}'))
        st = {i["item_id"]: i["status"] for i in c["items"]}
        self.assertEqual(st, {"x | y": "disagree", "z": "agree"})


class Scorecard(unittest.TestCase):
    JUDGE_REF = '{"verdicts":[{"op_index":0,"verdict":"confirm","reason":"r"},{"op_index":1,"verdict":"reject","reason":"r"}]}'

    def _res(self, backend, call_id, text, reason=None, status="ok", est=3000, version=None):
        r = rec("judge", self.JUDGE_REF, call_id=call_id, est=est)
        rp = rep(text, backend=backend, status=status, reason=reason, version=version)
        return mc.result_record(r, rp, mc.compare_call(r, rp), backend=backend, mode="exact")

    def test_common_vs_full_and_one_label_scores_everyone(self):
        a1 = self._res("apple_ondevice", "c_1", '{"verdicts":[{"op_index":0,"verdict":"reject","reason":"x"},{"op_index":1,"verdict":"reject","reason":"x"}]}')
        a2 = self._res("apple_ondevice", "c_2", None, status="skipped", reason="context_exceeded")
        o1 = self._res("ollama", "c_1", '{"verdicts":[{"op_index":0,"verdict":"reject","reason":"x"},{"op_index":1,"verdict":"reject","reason":"x"}]}')
        o2 = self._res("ollama", "c_2", '{"verdicts":[{"op_index":0,"verdict":"confirm","reason":"x"},{"op_index":1,"verdict":"reject","reason":"x"}]}')
        did = mc.delta_id("c_1", "0")
        labels = {did: {"delta_id": did, "truth": "reject"}}      # the truth: reject (both candidates right)
        sc = mc.scorecard([a1, a2, o1, o2], labels)
        self.assertEqual(sc["common_n"], 1)                          # only c_1 was attempted by both
        ap, ol = sc["cells"]["apple_ondevice|judge"], sc["cells"]["ollama|judge"]
        self.assertEqual(ap["full"]["coverage"], 0.5)
        self.assertEqual(ol["full"]["coverage"], 1.0)
        self.assertEqual(ol["common"]["n_results"], 1)
        for cell in (ap, ol):
            self.assertEqual(cell["full"]["adjudicated"], 1)
            self.assertEqual(cell["full"]["candidate_right"], 1)
            self.assertEqual(cell["full"]["precision"], 1.0)
            self.assertEqual(cell["full"]["reference_right_on_adjudicated"], 0)
            self.assertEqual(cell["full"]["reference_costly_error_rate"], 1.0)   # reference confirmed a bad op
            self.assertEqual(cell["full"]["costly_error_rate"], 0.0)
        self.assertEqual(len(ap["full"]["pending_disagreements"]), 0)

    def test_unlabeled_disagreements_are_pending(self):
        a1 = self._res("apple_ondevice", "c_1", '{"verdicts":[{"op_index":0,"verdict":"reject","reason":"x"},{"op_index":1,"verdict":"reject","reason":"x"}]}')
        sc = mc.scorecard([a1], {})
        self.assertEqual(sc["cells"]["apple_ondevice|judge"]["full"]["adjudicated"], 0)
        self.assertEqual(len(sc["cells"]["apple_ondevice|judge"]["full"]["pending_disagreements"]), 1)

    def test_periods_keyed_on_backend_version(self):
        a1 = self._res("apple_ondevice", "c_1", self.JUDGE_REF, version={"macos_build": "26A428"})
        a2 = self._res("apple_ondevice", "c_2", self.JUDGE_REF, version={"macos_build": "26B100"})
        sc = mc.scorecard([a1, a2], {})
        self.assertEqual(len(sc["cells"]["apple_ondevice|judge"]["full"]["periods"]), 2)

    def test_prompt_size_slices(self):
        a1 = self._res("apple_ondevice", "c_1", self.JUDGE_REF, est=2000)
        a2 = self._res("apple_ondevice", "c_2", self.JUDGE_REF, est=9000)
        sl = mc.scorecard([a1, a2], {})["cells"]["apple_ondevice|judge"]["slices"]["by_prompt_size"]
        self.assertEqual(set(sl), {"<4k", "8-16k"})

    def test_verdict_uses_parity_bar_and_sufficiency(self):
        a1 = self._res("apple_ondevice", "c_1", self.JUDGE_REF)
        cell = mc.scorecard([a1], {})["cells"]["apple_ondevice|judge"]
        v = mc.verdict_for_cell(cell, None)
        self.assertEqual(v["status"], "ACCUMULATE")

    def test_modes_kept_apart(self):
        r = rec("triage", '{"has_candidates":false,"candidates":[]}')
        rp = rep('{"has_candidates":false,"candidates":[]}', backend="ollama")
        loop = mc.result_record(r, rp, mc.compare_call(r, rp), backend="ollama", mode="loop")
        sc = mc.scorecard([loop], {})
        self.assertEqual(sc["cells"], {})
        self.assertIn("ollama|triage|loop", sc["modes"])


class Persist(unittest.TestCase):
    def test_paths_encode_backend_mode_rep(self):
        self.assertEqual(mc.result_path("ollama", "c_1"), "shadow_models/ollama/c_1.json")
        self.assertEqual(mc.result_path("ollama", "c_1", "loop", 2), "shadow_models/ollama/c_1.loop.r2.json")

    def test_cost_view(self):
        b = {"stamp": "s", "calls": [{"role": "judge", "result": {"cost_usd": 0.4}}, {"role": "judge", "result": {"cost_usd": 0.6}}]}
        cv = mc.cost_view([b], days=30)
        self.assertEqual(cv["live_usd_by_role"]["judge"], 1.0)
        self.assertEqual(cv["projected_monthly_usd_by_role"]["judge"], 1.0)


if __name__ == "__main__":
    unittest.main()
