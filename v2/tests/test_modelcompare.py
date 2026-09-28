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


class LoopMode(unittest.TestCase):
    """Question 2 scoring (plan C): the floor on both sides, retrieval vs judgment misses, tool failure
    kept out of agreement, the repeat floor, and the loop slices."""
    USER = ("Competitor: X\n\nTRACKED SUBJECTS (subject_key — current value already known):\n"
            "- x|price — $10 per seat\n- x|ceo — Jane\n")

    def _loop_rep(self, text, hosts=(), tool_failure=False, protocol="native", lag=30.0, backend="ollama"):
        r = rep(text, backend=backend)
        r["loop"] = {"tool_protocol": protocol, "turns": 3, "turn_at_exceed": None, "tool_calls": [{"name": "search"}],
                     "tool_call_parse_ok": True, "tool_arg_schema_ok": True, "tool_failure": tool_failure,
                     "hosts": list(hosts), "replay_lag_hours": lag, "format_retry": False}
        return r

    def test_tracked_keys_recovered_from_the_prompt(self):
        self.assertEqual([c["subject_key"] for c in mc.tracked_claims_from_prompt(self.USER)], ["x|price", "x|ceo"])
        self.assertEqual(mc.tracked_claims_from_prompt("no digest here"), [])

    def test_floor_applied_to_both_sides(self):
        # both sides surface a tracked subject with substantial=false: the floor lifts both → agree
        ref = '{"has_candidates":true,"candidates":[{"signal":"X price $12 2026-09-27","subject_key":"x|price","about":"competitor","substantial":false}]}'
        r = rec("triage", ref); r["user"] = self.USER
        c = mc.compare_call(r, rep(ref))
        self.assertEqual((c["items"][0]["reference"], c["items"][0]["candidate"], c["items"][0]["status"]),
                         ("escalate", "escalate", "agree"))
        self.assertEqual(c["extra"]["floor_applied"], {"reference": 1, "candidate": 1})

    def test_retrieval_vs_judgment_miss(self):
        ref = '{"has_candidates":true,"candidates":[{"signal":"A 2026-09-27","subject_key":"NEW","about":"competitor","substantial":true,"source_hint":"https://www.news.example.com/a"}]}'
        quiet = '{"has_candidates":false,"candidates":[]}'
        r = rec("triage", ref); r["user"] = self.USER
        seen = mc.compare_call(r, self._loop_rep(quiet, hosts=["news.example.com", "other.org"]))
        unseen = mc.compare_call(r, self._loop_rep(quiet, hosts=["other.org"]))
        exact = mc.compare_call(r, rep(quiet))
        self.assertEqual(seen["items"][0]["miss_kind"], "judgment_miss")
        self.assertEqual(unseen["items"][0]["miss_kind"], "retrieval_miss")
        self.assertNotIn("miss_kind", exact["items"][0])                       # exact mode: no attribution
        self.assertEqual(seen["extra"]["loop"]["tool_protocol"], "native")
        # live transcript fetches are the fallback host source when source_hint is not a URL
        ref2 = ref.replace("https://www.news.example.com/a", "some outlet")
        r2 = rec("triage", ref2); r2["user"] = self.USER
        r2["transcript"] = [{"kind": "tool_use", "name": "mcp__scoutfetch__fetch_page", "input": {"url": "https://press.example.com/x"}}]
        self.assertEqual(mc.compare_call(r2, self._loop_rep(quiet, hosts=["press.example.com"]))["items"][0]["miss_kind"], "judgment_miss")

    def test_materiality_miss_kind_from_source_urls(self):
        ref = '{"material":[{"claim":{"subject_key":"x | y","source_url":"https://a.example.com/p"}}],"immaterial":[]}'
        r = rec("materiality", ref)
        c = mc.compare_call(r, self._loop_rep('{"material":[],"immaterial":[{"signal":"x | y"}]}', hosts=["b.example.com"]))
        self.assertEqual(c["items"][0]["miss_kind"], "retrieval_miss")

    def test_tool_failure_is_excluded_from_agreement(self):
        ref = '{"has_candidates":true,"candidates":[{"signal":"A","subject_key":"NEW","about":"competitor","substantial":true}]}'
        r = rec("triage", ref); r["user"] = self.USER
        c = mc.compare_call(r, self._loop_rep('{"has_candidates":false,"candidates":[]}', tool_failure=True))
        self.assertEqual(c["summary"]["candidate_parse"], "tool_failure")
        self.assertEqual(c["items"], [])
        rp = self._loop_rep('{"has_candidates":false,"candidates":[]}', tool_failure=True)
        row = mc.result_record(r, rp, c, backend="ollama", mode="loop")
        ok_rp = self._loop_rep(ref, hosts=["h"])
        row2 = mc.result_record(r, ok_rp, mc.compare_call(r, ok_rp), backend="ollama", mode="loop")
        cell = mc._score_cell([row, row2], {})
        self.assertEqual(cell["tool_failure_rate"], 0.5)
        self.assertEqual(cell["items_judged"], 1); self.assertEqual(cell["parse_ok"], 1.0)

    def test_retrieval_miss_is_not_a_costly_error(self):
        ref = '{"has_candidates":true,"candidates":[{"signal":"A","subject_key":"NEW","about":"competitor","substantial":true,"source_hint":"https://n.example.com/a"}]}'
        quiet = '{"has_candidates":false,"candidates":[]}'
        r = rec("triage", ref); r["user"] = self.USER
        rp = self._loop_rep(quiet, hosts=["elsewhere.org"])                      # retrieval_miss
        row = mc.result_record(r, rp, mc.compare_call(r, rp), backend="ollama", mode="loop")
        did = row["comparison"]["items"][0]["delta_id"]
        cell = mc._score_cell([row], {did: {"truth": "escalate"}})
        self.assertEqual(cell["adjudicated"], 1); self.assertEqual(cell["candidate_right"], 0)
        self.assertEqual(cell["costly_error_rate"], 0.0)                          # tooling gap, not judgment
        self.assertEqual(cell["miss_kinds"], {"retrieval_miss": 1})
        rp2 = self._loop_rep(quiet, hosts=["n.example.com"])                     # judgment_miss
        row2 = mc.result_record(r, rp2, mc.compare_call(r, rp2), backend="ollama", mode="loop")
        self.assertEqual(mc._score_cell([row2], {did: {"truth": "escalate"}})["costly_error_rate"], 1.0)

    def test_loop_slices_and_repeat_floor(self):
        ref = '{"has_candidates":true,"candidates":[{"signal":"A","subject_key":"NEW","about":"competitor","substantial":true}]}'
        quiet = '{"has_candidates":false,"candidates":[]}'
        r = rec("triage", ref); r["user"] = self.USER
        rows = []
        for i, (txt, lag, proto) in enumerate([(ref, 10.0, "native"), (quiet, 50.0, "native"), (ref, 100.0, "action_json")]):
            rp = self._loop_rep(txt, hosts=["h"], lag=lag, protocol=proto)
            rows.append(mc.result_record(r, rp, mc.compare_call(r, rp), backend="ollama", mode="loop"))
        rp_rep = self._loop_rep(quiet, hosts=["h"])
        rows.append(mc.result_record(r, rp_rep, mc.compare_call(r, rp_rep), backend="ollama", mode="loop", rep=1))
        ms = mc.mode_summary(rows, {})
        cell = ms["ollama|triage|loop"]
        self.assertEqual(set(cell["by_tool_protocol"]), {"native", "action_json"})
        self.assertEqual(set(cell["by_replay_lag"]), {"<24h", "24-72h", ">72h"})
        self.assertIn("ollama|triage|loop|rep", ms)
        floor = mc.repeat_floor(rows)
        self.assertEqual(floor["ollama|triage"]["pairs"], 1)
        self.assertIn(floor["ollama|triage"]["self_agreement"], (0.0, 1.0))


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
    def test_paths_encode_backend_month_mode_rep(self):
        # month-sharded by the LIVE call's run_ts (Contents API lists ≤1000 entries per dir)
        self.assertEqual(mc.result_path("ollama", "c_1", run_ts="2026-09-28T04:10:00"), "shadow_models/ollama/2026-09/c_1.json")
        self.assertEqual(mc.result_path("ollama", "c_1", "loop", 2, "2026-10-01T04:00:00"),
                         "shadow_models/ollama/2026-10/c_1.loop.r2.json")
        self.assertEqual(mc.result_path("ollama", "c_1"), "shadow_models/ollama/unknown/c_1.json")
        self.assertEqual(mc.result_name("c_1", "frozen", 0), "c_1.frozen.json")
        with mock.patch.object(selfserve, "list_data", return_value=["c_1.json", "c_1.loop.json"]) as ld:
            self.assertEqual(mc.existing("ollama", "2026-09"), {"c_1.json", "c_1.loop.json"})
            ld.assert_called_once_with("shadow_models/ollama/2026-09")

    def test_cost_view(self):
        b = {"stamp": "s", "calls": [{"role": "judge", "result": {"cost_usd": 0.4}}, {"role": "judge", "result": {"cost_usd": 0.6}}]}
        cv = mc.cost_view([b], days=30)
        self.assertEqual(cv["live_usd_by_role"]["judge"], 1.0)
        self.assertEqual(cv["projected_monthly_usd_by_role"]["judge"], 1.0)


if __name__ == "__main__":
    unittest.main()
