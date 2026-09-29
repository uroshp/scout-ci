"""engine/app.py + scout/ledger.py (WS2 step 4b): auth (owner key / page token), input checks, the
ledger's hard ceiling and fail-closed store, the SSE shape (stages then done, or an honest error with
the cost settled), the dry endpoint. ask.ask is faked; the store is in memory."""
import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from engine import app as eng
from scout import asktoken, config, ledger, selfserve


class _Store:
    def __init__(self):
        self.files = {}

    def read(self, path):
        return self.files.get(path)

    def update(self, path, transform, message, **kw):
        new = transform(self.files.get(path))
        if new is None:
            return False
        self.files[path] = new
        return True


class Ledger(unittest.TestCase):
    def test_ceiling_is_hard_and_settle_replaces_the_reservation(self):
        st = _Store()
        with mock.patch.object(selfserve, "read_data", side_effect=st.read), mock.patch.object(selfserve, "update_data", side_effect=st.update):
            L = ledger.Ledger("ask/state.json", ceiling_usd=5.0, reserve_usd=3.0)
            ok1, s1 = L.start(); self.assertTrue(ok1); self.assertEqual(s1["in_flight_usd"], 3.0)
            ok2, s2 = L.start(); self.assertFalse(ok2); self.assertEqual(s2["refused"], 1)     # 3 + 3 > 5
            L.settle(1.1)
            s = L.read(); self.assertEqual((s["spend_usd"], s["in_flight_usd"], s["questions"]), (1.1, 0.0, 1))
            self.assertEqual(L.room(), 3.9)
            ok3, _ = L.start(); self.assertTrue(ok3)                                        # 1.1 + 3 <= 5

    def test_store_down_fails_closed(self):
        with mock.patch.object(selfserve, "update_data", side_effect=RuntimeError("github down")):
            ok, _ = ledger.Ledger("ask/state.json", 10, 1).start()
        self.assertFalse(ok)

    def test_day_rollover(self):
        st = _Store(); st.files["ask/state.json"] = json.dumps({"day": "2000-01-01", "spend_usd": 9.0, "in_flight_usd": 0, "questions": 4})
        with mock.patch.object(selfserve, "read_data", side_effect=st.read), mock.patch.object(selfserve, "update_data", side_effect=st.update):
            L = ledger.Ledger("ask/state.json", 10, 3)
            ok, s = L.start(); self.assertTrue(ok); self.assertEqual(s["questions"], 1)


def _quick(question, kw):
    return {"id": kw.get("record_id") or "a_0123456789ab", "kind": "quick", "cost_usd": 0.2, "seconds": 20.0, "verified": True, "question": question,
            "paragraphs": [{"text": "X grew.", "cites": [1]}], "sources": [{"n": 1, "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary"}],
            "cut_log": [], "unanswered": [], "trajectory": {}, "competitor": "X", "card": None}


def _events(resp):
    out = []
    for chunk in resp.text.split("\n\n"):
        for line in chunk.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class Engine(unittest.TestCase):
    def setUp(self):
        self.st = _Store()
        self.p = [mock.patch.object(selfserve, "read_data", side_effect=self.st.read),
                  mock.patch.object(selfserve, "update_data", side_effect=self.st.update),
                  mock.patch.object(eng, "ASK_API_KEYS", ["owner-key"]), mock.patch.object(eng, "ASK_VIEWER_SECRET", "shh"),
                  mock.patch.object(eng, "LEDGER", ledger.Ledger("ask/state.json", 10, 3)),
                  mock.patch.object(eng.calllog, "begin_run"), mock.patch.object(eng.calllog, "flush_run")]
        for p in self.p:
            p.start()
        self.c = TestClient(eng.app)

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_auth_and_input(self):
        self.assertEqual(self.c.post("/ask", json={"question": "x"}).status_code, 401)
        self.assertEqual(self.c.post("/ask", json={"question": "x"}, headers={"Authorization": "Bearer nope"}).status_code, 401)
        bad = asktoken.page_token("abcd", "other-secret")
        self.assertEqual(self.c.post("/ask", json={"question": "x"}, headers={"Authorization": "Bearer " + bad}).status_code, 401)
        self.assertEqual(self.c.post("/ask", json={"question": ""}, headers={"Authorization": "Bearer owner-key"}).status_code, 400)
        self.assertEqual(self.c.get("/healthcheck").text, "ok")

    def test_stream_stages_then_done_and_ledger_settles(self):
        def fake_ask(question, **kw):
            kw["on_stage"]("search", "3 known facts"); kw["on_stage"]("verify", "2 sentences")
            return {"id": "a_0123456789ab", "cost_usd": 1.23, "seconds": 61.0, "verified": True, "question": question,
                    "paragraphs": [{"text": "X grew.", "cites": [1]}], "sources": [{"n": 1, "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary"}],
                    "cut_log": [], "unanswered": [], "trajectory": {}, "competitor": "X", "card": None}
        tok = asktoken.page_token("visitor1", "shh")
        with mock.patch.object(eng.ask, "ask", side_effect=fake_ask) as fa:
            r = self.c.post("/ask", json={"question": "Did X grow?", "mode": "deep", "persona": "economic_buyer", "history": [{"question": "q", "answer_id": "a_ffffffffffff"}, {"x": 1}]},
                            headers={"Authorization": "Bearer " + tok})
        self.assertEqual(r.status_code, 200); self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        ev = _events(r)
        self.assertEqual([e.get("stage") for e in ev if "stage" in e], ["facts", "search", "verify"])
        done = [e for e in ev if e.get("done")][0]
        self.assertEqual(done["id"], "a_0123456789ab"); self.assertIn("srcclass-news", done["html"]); self.assertEqual(done["cost_usd"], 1.23)
        self.assertEqual(fa.call_args.kwargs["persona"], "economic_buyer")
        self.assertEqual(fa.call_args.kwargs["history"], [{"question": "q", "answer_id": "a_ffffffffffff"}])   # malformed turn dropped
        s = json.loads(self.st.files["ask/state.json"]); self.assertEqual((s["spend_usd"], s["in_flight_usd"]), (1.23, 0.0))

    def test_failure_is_honest_and_settles_the_real_cost(self):
        err = RuntimeError("Claude Code returned an error result: Reached maximum budget ($1.5)"); err.scout_cost_usd = 1.47
        with mock.patch.object(eng.ask, "ask", side_effect=err):
            r = self.c.post("/ask", json={"question": "broad", "mode": "deep"}, headers={"Authorization": "Bearer owner-key"})
        ev = _events(r); e = [x for x in ev if "error" in x][0]
        self.assertIn("ran out of research budget", e["error"]); self.assertEqual(e["cost_usd"], 1.47)
        self.assertNotIn("Nothing was charged", e["error"])
        self.assertEqual(json.loads(self.st.files["ask/state.json"])["spend_usd"], 1.47)

    def test_request_token_fixes_the_answer_id_and_activity_streams(self):
        rid = "browser-token-0123456789"
        want = asktoken.record_id_for(rid)
        def fake_ask(question, **kw):
            self.assertEqual(kw["record_id"], want)
            kw["on_stage"]("search", "3 known facts"); kw["on_stage"]("activity", "Searching: X pricing"); kw["on_stage"]("activity", "")
            return {"id": kw["record_id"], "cost_usd": 1.0, "seconds": 5.0, "verified": True, "question": question,
                    "paragraphs": [{"text": "X grew.", "cites": [1]}], "sources": [{"n": 1, "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary"}],
                    "cut_log": [], "unanswered": [], "trajectory": {}, "competitor": "X", "card": None}
        with mock.patch.object(eng.ask, "ask", side_effect=fake_ask):
            r = self.c.post("/ask", json={"question": "q", "mode": "deep", "rid": rid}, headers={"Authorization": "Bearer owner-key"})
        ev = _events(r)
        self.assertEqual(ev[0].get("id"), want)                                   # the first frame carries the id
        self.assertEqual([e["activity"] for e in ev if "activity" in e], ["Searching: X pricing"])   # empty lines are not sent
        self.assertEqual([e for e in ev if e.get("done")][0]["id"], want)
        # a bad token gets no id (and still works)
        with mock.patch.object(eng.ask, "ask", side_effect=fake_ask) as fa:
            fa.side_effect = lambda question, **kw: dict(fake_ask(question, **dict(kw, record_id=want)), id="a_0123456789ab")
            r = self.c.post("/ask", json={"question": "q", "mode": "deep", "rid": "x y"}, headers={"Authorization": "Bearer owner-key"})
        self.assertIsNone(_events(r)[0].get("id"))

    def test_same_token_replays_the_finished_record_for_free(self):
        rid = "browser-token-0123456789"; aid = asktoken.record_id_for(rid)
        self.st.files["ask/2026-09/" + aid + ".json"] = json.dumps({"id": aid, "question": "q", "seconds": 5, "verified": True, "cost_usd": 1.0,
                                                                   "paragraphs": [{"text": "X grew.", "cites": [1]}], "sources": [{"n": 1, "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary"}], "cut_log": [], "unanswered": [], "trajectory": {}})
        with mock.patch.object(selfserve, "list_data", return_value=["2026-09"]), mock.patch.object(eng.ask, "ask") as fa:
            r = self.c.post("/ask", json={"question": "q", "mode": "deep", "rid": rid}, headers={"Authorization": "Bearer owner-key"})
        ev = _events(r); self.assertTrue(ev[-1]["done"]); self.assertTrue(ev[-1]["replay"]); self.assertIn("X grew", ev[-1]["html"])
        fa.assert_not_called(); self.assertNotIn("ask/state.json", self.st.files)   # no run, no ledger entry

    def test_failure_leaves_a_record_at_the_expected_id(self):
        rid = "browser-token-0123456789"; aid = asktoken.record_id_for(rid)
        err = RuntimeError("Command failed with exit code 1"); err.scout_cost_usd = 0.4
        written = {}
        with mock.patch.object(eng.ask, "ask", side_effect=err), mock.patch.object(selfserve, "write_data", side_effect=lambda path, text, msg: written.__setitem__(path, text)):
            r = self.c.post("/ask", json={"question": "q", "mode": "deep", "rid": rid}, headers={"Authorization": "Bearer owner-key"})
        e = [x for x in _events(r) if "error" in x][0]; self.assertEqual(e["id"], aid)
        path = [k for k in written if k.endswith(aid + ".json")][0]
        rec = json.loads(written[path]); self.assertTrue(rec["failed"]); self.assertIn("hit a problem", rec["error"]); self.assertEqual(rec["cost_usd"], 0.4)
        # and the replay of a failed record is the honest error, not a second run
        self.st.files["ask/2026-09/" + aid + ".json"] = written[path]
        with mock.patch.object(selfserve, "list_data", return_value=["2026-09"]), mock.patch.object(eng.ask, "ask") as fa:
            r = self.c.post("/ask", json={"question": "q", "mode": "deep", "rid": rid}, headers={"Authorization": "Bearer owner-key"})
        self.assertIn("hit a problem", _events(r)[-1]["error"]); fa.assert_not_called()

    def test_turn_cap_failure_says_so(self):
        err = RuntimeError("Claude Code returned an error result: Reached maximum number of turns (16)"); err.scout_cost_usd = 0.9
        with mock.patch.object(eng.ask, "ask", side_effect=err):
            r = self.c.post("/ask", json={"question": "q", "mode": "deep"}, headers={"Authorization": "Bearer owner-key"})
        e = [x for x in _events(r) if "error" in x][0]
        self.assertIn("ran out of research steps", e["error"]); self.assertNotIn("Exception", e["error"]); self.assertEqual(e["cost_usd"], 0.9)

    def test_failure_cost_known_zero_vs_unknown(self):
        # a process that died before its first message is a KNOWN $0 (the Cloud Run root-user
        # ProcessError); a failure with no figure at all settles the research cap (fail closed)
        for attached, expect in ((0.0, 0.0), (None, config.ASK_RESEARCH_BUDGET_USD)):
            self.st.files.pop("ask/state.json", None)
            err = RuntimeError("Command failed with exit code 1")
            if attached is not None:
                err.scout_cost_usd = attached
            with mock.patch.object(eng.ask, "ask", side_effect=err):
                r = self.c.post("/ask", json={"question": "q", "mode": "deep"}, headers={"Authorization": "Bearer owner-key"})
            e = [x for x in _events(r) if "error" in x][0]
            self.assertIn("hit a problem", e["error"]); self.assertEqual(e["cost_usd"], expect)
            self.assertEqual(json.loads(self.st.files["ask/state.json"])["spend_usd"], expect)

    def test_ceiling_refuses_with_429(self):
        self.st.files["ask/state.json"] = json.dumps({"day": ledger.Ledger._today(), "spend_usd": 9.0, "in_flight_usd": 0, "questions": 5, "refused": 0})
        r = self.c.post("/ask", json={"question": "x", "mode": "deep"}, headers={"Authorization": "Bearer owner-key"})
        self.assertEqual(r.status_code, 429); self.assertIn("budget is spent", r.json()["message"])
        # the quick path reserves its own, smaller cap ($1), so it still fits where the deep one ($3) did not
        with mock.patch.object(eng.ask, "quick_ask", side_effect=lambda question, **kw: _quick(question, kw)):
            r = self.c.post("/ask", json={"question": "x"}, headers={"Authorization": "Bearer owner-key"})
        self.assertEqual(r.status_code, 200)
        s = json.loads(self.st.files["ask/state.json"]); self.assertEqual((s["spend_usd"], s["in_flight_usd"]), (9.2, 0.0))

    def test_quick_is_the_default_and_streams_its_own_stages(self):
        seen = {}
        def fake_quick(question, **kw):
            seen["kw"] = kw
            kw["on_stage"]("draft", "61 verified facts, 9 takes"); kw["on_stage"]("verify", "3 sentences")
            return _quick(question, kw)
        with mock.patch.object(eng.ask, "quick_ask", side_effect=fake_quick), mock.patch.object(eng.ask, "ask") as deep:
            r = self.c.post("/ask", json={"question": "Is X cheaper?", "rid": "browser-token-0123456789", "history": [{"question": "q", "answer_id": "a_ffffffffffff"}]},
                            headers={"Authorization": "Bearer owner-key"})
        deep.assert_not_called()
        ev = _events(r)
        self.assertEqual(ev[0]["kind"], "quick")
        self.assertEqual([e.get("stage") for e in ev if "stage" in e], ["facts", "draft", "verify"])
        done = [e for e in ev if e.get("done")][0]; self.assertEqual(done["kind"], "quick"); self.assertIn("X grew", done["html"])
        self.assertEqual(seen["kw"]["record_id"], asktoken.record_id_for("browser-token-0123456789"))
        self.assertEqual(seen["kw"]["history"], [{"question": "q", "answer_id": "a_ffffffffffff"}])

    def test_dry_replays_a_stored_answer(self):
        self.st.files["ask/2026-09/a_0123456789ab.json"] = json.dumps({"id": "a_0123456789ab", "question": "q", "seconds": 5, "verified": True, "paragraphs": [], "sources": [], "cut_log": [], "unanswered": [], "trajectory": {}})
        with mock.patch.dict(eng.os.environ, {"SCOUT_ASK_CANNED": "a_0123456789ab"}), mock.patch.object(selfserve, "list_data", return_value=["2026-09"]):
            r = self.c.get("/ask/dry")
        ev = _events(r); self.assertTrue(ev[-1]["done"]); self.assertTrue(ev[-1]["dry"])
        with mock.patch.dict(eng.os.environ, {"SCOUT_ASK_CANNED": ""}):
            self.assertEqual(self.c.get("/ask/dry").status_code, 503)


if __name__ == "__main__":
    unittest.main()
