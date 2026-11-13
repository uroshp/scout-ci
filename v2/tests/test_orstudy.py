"""The OpenRouter study: what is sent, what is stored, what stops it, how a call is scored. Offline: httpx is faked."""
import json
import os
import shutil
import tempfile
import unittest
from collections import Counter
from unittest import mock

from scout import orstudy


class _Resp:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._data = status, data
        self.text = text if text is not None else json.dumps(data)

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def _answer(text='{"candidates": []}', provider="Nvidia", model="nvidia/x:free", cost=0, reasoning="thought", finish="stop"):
    return _Resp(200, {"provider": provider, "model": model,
                       "choices": [{"finish_reason": finish, "message": {"content": text, "reasoning": reasoning}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": cost}})


def _err(code, message):
    return _Resp(code, {"error": {"code": code, "message": message}})


ENTRY = {"model": "nvidia/x:free", "providers": ["nvidia"], "provider_names": ["Nvidia"], "reasoning": True,
         "max_out": 65536, "router": False}
RECORD = {"call_id": "c1", "role": "screen", "status": "ok", "run_ts": "2026-11-10T04:00:00",
          "system": {"kind": "string", "text": "SYS"}, "user": "USER", "result": {"text": '{"candidates": []}'}}


class OrStudyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for k, v in {"STUDY_DIR": self.tmp, "MIN_GAP_S": 0.0}.items():
            p = mock.patch.object(orstudy, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orstudy.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        orstudy.reset_stop()
        orstudy._used.update(day=None, n=0)
        self.addCleanup(orstudy.reset_stop)

    def _post(self, *responses):
        p = mock.patch.object(orstudy.httpx, "post", side_effect=list(responses))
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def _call(self, *responses, **kw):
        self._post(*responses)
        return orstudy.call(ENTRY, RECORD, "SYS", "USER", **kw)


class BodyTests(OrStudyCase):
    def test_same_plain_request_for_every_model(self):
        body = orstudy.body_for(ENTRY, "SYS", "USER")
        self.assertFalse({"temperature", "top_p", "top_k", "seed", "response_format"} & set(body))
        self.assertEqual(body["provider"], {"only": ["nvidia"]})
        self.assertEqual(body["reasoning"], {"enabled": True})
        self.assertEqual([m["content"] for m in body["messages"]], ["SYS", "USER"])
        self.assertEqual(body["max_tokens"], orstudy.MAX_OUT)

    def test_output_room_is_the_endpoint_ceiling_and_absent_when_unknown(self):
        self.assertEqual(orstudy.body_for({**ENTRY, "max_out": 8192}, "S", "U")["max_tokens"], 8192)
        self.assertNotIn("max_tokens", orstudy.body_for({**ENTRY, "max_out": None}, "S", "U"))


class CallTests(OrStudyCase):
    def test_answer_carries_who_served_it_and_is_counted(self):
        r = self._call(_answer())
        self.assertEqual((r["status"], r["outcome"]), ("ok", "store"))
        self.assertEqual(r["backend_version"]["served_by"], "Nvidia")
        self.assertEqual(orstudy.used_today(), 1)
        self.assertEqual(orstudy.load_attempts()[0]["rule"], {"only": ["nvidia"]})

    def test_any_provider_off_the_approved_list_stops_everything(self):
        for who in ("Baidu", "Baidu Qianfan", "Some New Host", None):
            with self.subTest(who=who):
                r = self._call(_answer(provider=who))
                self.assertEqual((r["reason"], r["outcome"]), ("unapproved_provider_served", "stop_all"))

    def test_a_charge_stops_everything(self):
        self.assertEqual(self._call(_answer(cost=0.002))["outcome"], "stop_all")

    def test_rate_limit_and_broken_responses_are_not_results(self):
        for resp in (_err(429, "temporarily rate-limited upstream"), _Resp(200, None, text=""), _Resp(200, {}),
                     _Resp(200, {"choices": []}), _err(500, "upstream exploded"),
                     _Resp(200, {"provider": "Nvidia", "choices": [{"finish_reason": "error", "error": {"message": "boom"},
                                                                    "message": {"content": "partial"}}]})):
            with self.subTest(resp=resp.text[:40]):
                self.assertEqual(self._call(resp)["outcome"], "retry_later")

    def test_only_the_account_daily_limit_stops_everything_on_a_429(self):
        self.assertEqual(self._call(_err(429, "Rate limit exceeded: free-models-per-day"))["outcome"], "stop_all")
        self.assertEqual(self._call(_err(429, "this model's daily upstream quota is spent"))["outcome"], "retry_later")

    def test_a_model_refusal_rests_that_model_only(self):
        r = self._call(_err(403, "x:free is only available on agentic harnesses"))
        self.assertEqual((r["reason"], r["outcome"]), ("model_refused", "model_off"))
        self.assertIsNone(orstudy.stop_reason())

    def test_a_moderated_prompt_is_that_calls_result(self):
        r = self._call(_err(403, "Your input was flagged by moderation"))
        self.assertEqual((r["reason"], r["outcome"]), ("moderated", "store"))

    def test_output_room_spent_is_a_stored_failure_never_an_answer(self):
        r = self._call(_answer(text="", finish="length"))
        self.assertEqual((r["status"], r["reason"], r["outcome"]), ("error", "reasoning_used_all_output", "store"))
        r = self._call(_answer(text='{"candidates": [', finish="length"))
        self.assertEqual((r["status"], r["reason"], r["outcome"]), ("error", "cut_off", "store"))

    def test_list_valued_content_is_read(self):
        r = self._call(_answer(text=[{"type": "text", "text": '{"candidates": []}'}]))
        self.assertEqual((r["status"], r["text"]), ("ok", '{"candidates": []}'))

    def test_cap_and_stop_send_nothing(self):
        post = self._post()
        with mock.patch.object(orstudy, "DAILY_CAP", 0):
            self.assertEqual(orstudy.call(ENTRY, RECORD, "SYS", "USER")["reason"], "daily_cap")
        orstudy._stop["why"] = "something"
        self.assertEqual(orstudy.call(ENTRY, RECORD, "SYS", "USER")["outcome"], "stop_all")
        post.assert_not_called()
        self.assertEqual(orstudy.used_today(), 0)

    def test_every_sent_request_is_counted_and_logged_once(self):
        self._post(_answer(), _err(429, "rate-limited upstream"), _Resp(200, {}), _answer(text="", finish="length"))
        for _ in range(4):
            orstudy.call(ENTRY, RECORD, "SYS", "USER")
        self.assertEqual((orstudy.used_today(), len(orstudy.load_attempts())), (4, 4))


class RunTests(OrStudyCase):
    def _calls(self, n):
        return [{**RECORD, "call_id": f"c{i}"} for i in range(n)]

    def test_stores_once_and_skips_what_it_has(self):
        self._post(_answer())
        out = orstudy.run_model(ENTRY, [RECORD], 5, say=lambda s: None)
        self.assertEqual((out["stored"], out["ended"]), (1, "done"))
        with open(orstudy.result_path(ENTRY["model"], "c1")) as fh:
            stored = json.load(fh)
        self.assertEqual(stored["backend"], "or:nvidia/x:free")
        self.assertNotIn("outcome", stored)
        self.assertNotIn("SYS", json.dumps(stored))            # the prompt is never written to the result
        self.assertTrue(stored["study"]["format_ok"])
        post = self._post()
        self.assertEqual(orstudy.run_model(ENTRY, [RECORD], 5, say=lambda s: None)["stored"], 0)
        post.assert_not_called()

    def test_a_rate_limited_call_is_retried_not_skipped_over(self):
        self._post(_err(429, "rate-limited upstream"), _answer(), _answer())
        out = orstudy.run_model(ENTRY, self._calls(2), 5, say=lambda s: None)
        self.assertEqual(out["stored"], 2)
        self.assertEqual([a["call_id"] for a in orstudy.load_attempts()], ["c0", "c0", "c1"])

    def test_two_failures_in_a_row_rest_the_model_with_nothing_stored(self):
        self._post(_err(429, "rate-limited upstream"), _err(429, "rate-limited upstream"))
        out = orstudy.run_model(ENTRY, self._calls(2), 5, say=lambda s: None)
        self.assertEqual((out["stored"], out["ended"]), (0, "rate_limited"))
        self.assertEqual(orstudy.have(ENTRY["model"]), set())

    def test_quota_bounds_requests(self):
        self._post(_answer(), _answer())
        out = orstudy.run_model(ENTRY, self._calls(5), 2, say=lambda s: None)
        self.assertEqual((out["stored"], out["attempts"], out["ended"]), (2, 2, "quota"))

    def test_a_refused_model_ends_without_stopping_the_others(self):
        self._post(_err(403, "only available on agentic harnesses"))
        out = orstudy.run_model(ENTRY, self._calls(3), 5, say=lambda s: None)
        self.assertEqual((out["stored"], out["ended"]), (0, "model_refused"))
        self.assertIsNone(orstudy.stop_reason())

    def test_a_call_the_provider_keeps_rejecting_is_stored_as_never_answered(self):
        prior = Counter({(ENTRY["model"], "c0"): orstudy.GIVE_UP_AFTER - 1})
        self._post(_err(400, "unsupported parameter"), _answer())
        out = orstudy.run_model(ENTRY, self._calls(2), 5, prior_errors=prior, say=lambda s: None)
        self.assertEqual(out["stored"], 2)
        with open(orstudy.result_path(ENTRY["model"], "c0")) as fh:
            first = json.load(fh)
        self.assertEqual((first["status"], first["reason"]), ("error", "provider_rejected_repeatedly"))
        self.assertFalse(orstudy.score_row(first)["same"])

    def test_duplicate_call_ids_are_sent_once(self):
        post = self._post(_answer())
        orstudy.run_model(ENTRY, [RECORD, dict(RECORD)], 5, say=lambda s: None)
        self.assertEqual(post.call_count, 1)


class ScoreTests(unittest.TestCase):
    def _cmp(self, *items, parse="ok"):
        return {"summary": {"candidate_parse": parse}, "items": [dict(zip(("status", "reference"), i)) for i in items]}

    def test_same_only_when_every_judged_item_agrees(self):
        self.assertTrue(orstudy.score(self._cmp(("agree", "confirm"), ("agree", "reject")), "judge")["same"])
        self.assertFalse(orstudy.score(self._cmp(("agree", "confirm"), ("disagree", "reject")), "judge")["same"])

    def test_an_item_left_out_counts_against_the_model(self):
        s = orstudy.score(self._cmp(("agree", "confirm"), ("abstain", "reject")), "judge")
        self.assertEqual((s["same"], s["agree"], s["judged"]), (False, 1, 2))

    def test_a_quiet_item_left_unnamed_is_the_quiet_verdict_a_loud_one_is_a_miss(self):
        self.assertTrue(orstudy.score(self._cmp(("abstain", "minor")), "screen")["same"])
        self.assertFalse(orstudy.score(self._cmp(("abstain", "substantial")), "screen")["same"])

    def test_no_answer_and_unreadable_answers_are_never_the_same_decision(self):
        self.assertFalse(orstudy.score(self._cmp(), "screen", status="error", reason="empty")["same"])
        self.assertFalse(orstudy.score(self._cmp(parse="parse_fail"), "screen")["same"])
        self.assertFalse(orstudy.score(self._cmp(), "screen", fmt_ok=False)["same"])

    def test_an_object_without_the_required_keys_is_not_a_readable_answer(self):
        self.assertTrue(orstudy.format_ok("screen", '{"candidates": []}'))
        for text in ("{}", '{"note": "nothing"}', "I am not sure.", ""):
            self.assertFalse(orstudy.format_ok("screen", text), text)


class CatalogueTests(OrStudyCase):
    def test_only_approved_providers_and_the_router_inherits_them(self):
        free = {"prompt": "0", "completion": "0"}
        models = {"data": [
            {"id": "nvidia/x:free", "pricing": free, "supported_parameters": ["reasoning"]},
            {"id": "baidu/y:free", "pricing": free, "supported_parameters": []},
            {"id": "newlab/z:free", "pricing": free, "supported_parameters": []},
            {"id": "google/song", "pricing": free, "architecture": {"output_modalities": ["text", "audio"]}},
            {"id": "vendor/zero-priced", "pricing": free, "supported_parameters": []},
            {"id": "openrouter/free", "pricing": free, "supported_parameters": ["reasoning"]},
            {"id": "paid/model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
        eps = {"nvidia/x:free": [{"tag": "nvidia/nvfp4", "provider_name": "Nvidia", "max_completion_tokens": 65536}],
               "baidu/y:free": [{"tag": "baidu", "provider_name": "Baidu"}],
               "newlab/z:free": [{"tag": "newhost/fp8", "provider_name": "New Host"}]}

        def fetch(url):
            if url.endswith("/models"):
                return models
            return {"data": {"endpoints": eps[url.split("/models/")[1].rsplit("/endpoints", 1)[0]]}}
        cat = orstudy.catalogue(fetch)
        self.assertEqual([m["model"] for m in cat["models"]], ["nvidia/x:free", "openrouter/free"])
        router = cat["models"][1]
        self.assertEqual((router["providers"], router["provider_names"]), (["nvidia"], ["Nvidia"]))
        why = {d["model"]: d["why"] for d in cat["dropped"]}
        self.assertEqual(set(why), {"baidu/y:free", "newlab/z:free", "google/song", "vendor/zero-priced"})
        self.assertIn("not ruled on", why["newlab/z:free"])
        self.assertIn("China", why["baidu/y:free"])


if __name__ == "__main__":
    unittest.main()
