"""One report per visitor (2026-11-06). One visitor ran three reports in an hour ($10.88): the
only limits were a shared pool and an in-memory burst guard. A visitor is now known by the exact
IPv4, the IPv6 /64 and the viewer cookie; the index lives in the private store so the app and the
Action see the same thing; the Action re-checks at the point of spend; a blocked key is refused;
and the word "free" is gone from every visitor-facing line. Pure, no network (the unit suite's
store is a temp directory). Run from v2/:

    python -m unittest discover -s tests
"""
import importlib
import os
import re
import unittest
import uuid
from unittest import mock

import server
from scout import config, display, ratelimit, render, selfserve

run_selfserve = importlib.import_module("scripts.run_selfserve")

FREE = re.compile(r"(?i)\bfree\b")


def _cid():
    return "cid" + uuid.uuid4().hex[:20]


class Keys(unittest.TestCase):
    def test_ipv4_exact_ipv6_block_and_cookie(self):
        self.assertEqual(selfserve.requester_keys("157.50.188.233", None), ["ip:157.50.188.233"])
        k = selfserve.requester_keys("2409:40f2:2041:2b3c:117:ff51:5464:8387", "abcdef1234567890")
        self.assertEqual(k, ["net:2409:40f2:2041:2b3c::/64", "cid:abcdef1234567890"])
        # another host in the same /64 is the same visitor; another /64 is not
        self.assertEqual(selfserve.requester_keys("2409:40f2:2041:2b3c::1", None),
                         ["net:2409:40f2:2041:2b3c::/64"])
        self.assertEqual(selfserve.requester_keys("2409:40f2:2041:2b3d::1", None),
                         ["net:2409:40f2:2041:2b3d::/64"])

    def test_garbage_is_ignored(self):
        self.assertEqual(selfserve.requester_keys("?", "<script>"), [])
        self.assertEqual(selfserve.requester_keys("not an ip", "short"), [])
        self.assertEqual(selfserve.requester_keys(None, None), [])


class Index(unittest.TestCase):
    def _submit(self, ip, cid, competitor=None):
        # job ids carry a one-second stamp, so two tests naming the same competitor in the same
        # second would share one id; a unique name per call keeps them apart
        competitor = competitor or "Acme " + uuid.uuid4().hex[:8]
        who = {"ip": ip, "cid": cid, "keys": selfserve.requester_keys(ip, cid)}
        with mock.patch.object(selfserve, "dispatch_generation", return_value=False):
            return selfserve.submit(competitor, None, None, requester=who), who

    def test_submit_records_the_visitor_and_prior_report_finds_them(self):
        cid = _cid()
        req, who = self._submit("203.0.113.7", cid)
        self.assertEqual(req["requester"]["keys"], who["keys"])
        self.assertEqual(selfserve.get_request(req["job_id"])["requester"]["ip"], "203.0.113.7")
        # still generating: counts, by the address alone and by the cookie alone
        self.assertEqual(selfserve.prior_report(who["keys"])["job_id"], req["job_id"])
        self.assertEqual(selfserve.prior_report(["ip:203.0.113.7"])["job_id"], req["job_id"])
        self.assertEqual(selfserve.prior_report([f"cid:{cid}"])["job_id"], req["job_id"])
        self.assertFalse(selfserve.prior_report(who["keys"])["blocked"])
        # done: still counts
        selfserve.save_result(req["job_id"], {"job_id": req["job_id"], "status": "done"})
        self.assertEqual(selfserve.prior_report(who["keys"])["job_id"], req["job_id"])
        # a stranger is new
        self.assertIsNone(selfserve.prior_report(["ip:203.0.113.8", f"cid:{_cid()}"]))

    def test_errored_and_rejected_requests_do_not_count(self):
        for status in ("error", "rejected"):
            req, who = self._submit("198.51.100.20", _cid())
            selfserve.save_result(req["job_id"], {"job_id": req["job_id"], "status": status})
            self.assertIsNone(selfserve.prior_report(who["keys"]), status)

    def test_a_later_request_is_not_prior_to_an_earlier_one(self):
        cid = _cid()
        first, who = self._submit("198.51.100.30", cid)
        second, _ = self._submit("198.51.100.30", cid)
        self.assertIsNone(selfserve.prior_report(who["keys"], exclude_job_id=first["job_id"]))
        self.assertEqual(selfserve.prior_report(who["keys"], exclude_job_id=second["job_id"])["job_id"],
                         first["job_id"])

    def test_blocked_key_is_refused_even_with_no_report(self):
        d = selfserve.load_requesters()
        d["blocked"].append("ip:198.51.100.99")
        selfserve.save_requesters(d)
        self.assertEqual(selfserve.prior_report(["ip:198.51.100.99"]), {"job_id": None, "blocked": True})
        self.assertIsNone(selfserve.prior_report(["ip:198.51.100.98"]))

    def test_missing_or_hand_edited_index_is_tolerated(self):
        with mock.patch.object(selfserve, "_read", return_value="not json"):
            self.assertEqual(selfserve.load_requesters(), {"used": {}, "blocked": []})
            self.assertIsNone(selfserve.prior_report(["ip:1.2.3.4"]))
        with mock.patch.object(selfserve, "_read", return_value='{"used": "nope", "blocked": 3}'):
            self.assertEqual(selfserve.load_requesters(), {"used": {}, "blocked": []})


class App(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False),
                  mock.patch.object(config, "ANALYTICS_ENABLED", False),
                  mock.patch.object(display, "_commits_via_api", return_value=[]),
                  mock.patch.object(server, "_REQ_IP", ratelimit.Limiter(per_minute=50, per_day=50)),
                  mock.patch.object(selfserve, "gate", return_value={"open": True, "free_left": 3, "reason": None}),
                  mock.patch.object(selfserve, "dispatch_generation", return_value=False)]
        for p in self.p:
            p.start()
        server.app.config["TESTING"] = True

    def tearDown(self):
        for p in self.p:
            p.stop()

    def _client(self, cid):
        c = server.app.test_client()
        c.set_cookie("scout_cid", cid)
        return c

    def test_second_request_from_the_same_cookie_is_refused(self):
        c = self._client(_cid())
        h = {"X-Forwarded-For": "203.0.113.50"}
        r1 = c.post("/api/request", json={"competitor": "Acme"}, headers=h)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True)); self.assertIn("job_id", r1.get_json())
        r2 = c.post("/api/request", json={"competitor": "Beta"}, headers=h)
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.get_json()["error"], selfserve.ONE_PER_VISITOR_MSG)
        self.assertNotRegex(r2.get_json()["error"], FREE)
        # same cookie from another address: still the same visitor
        r3 = c.post("/api/request", json={"competitor": "Gamma"}, headers={"X-Forwarded-For": "203.0.113.51"})
        self.assertEqual(r3.status_code, 403)

    def test_same_ipv6_block_with_a_new_cookie_is_refused(self):
        a = self._client(_cid()).post("/api/request", json={"competitor": "Acme"},
                                      headers={"X-Forwarded-For": "2001:db8:1:2::10"})
        self.assertEqual(a.status_code, 200)
        b = self._client(_cid()).post("/api/request", json={"competitor": "Acme"},
                                      headers={"X-Forwarded-For": "2001:db8:1:2:aaaa:bbbb:cccc:dddd"})
        self.assertEqual(b.status_code, 403)
        c = self._client(_cid()).post("/api/request", json={"competitor": "Acme"},
                                      headers={"X-Forwarded-For": "2001:db8:1:3::1"})
        self.assertEqual(c.status_code, 200)                     # a different /64 is someone else

    def test_create_page_for_a_known_visitor_links_their_report(self):
        c = self._client(_cid())
        h = {"X-Forwarded-For": "203.0.113.60"}
        job = c.post("/api/request", json={"competitor": "Acme"}, headers=h).get_json()["job_id"]
        html = c.get("/create", headers=h).get_data(as_text=True)
        self.assertIn(f"/result/{job}", html); self.assertIn("open your report", html)
        self.assertNotIn('id="ss-form"', html)
        self.assertNotRegex(html, FREE)

    def test_create_page_for_a_new_visitor_shows_the_form_without_the_word_free(self):
        html = self._client(_cid()).get("/create", headers={"X-Forwarded-For": "203.0.113.61"}).get_data(as_text=True)
        self.assertIn('id="ss-form"', html); self.assertIn("One report per visitor", html)
        self.assertNotRegex(html, FREE)

    def test_blocked_visitor_sees_by_request_copy_and_cannot_submit(self):
        d = selfserve.load_requesters(); d["blocked"].append("ip:203.0.113.70"); selfserve.save_requesters(d)
        c = self._client(_cid()); h = {"X-Forwarded-For": "203.0.113.70"}
        html = c.get("/create", headers=h).get_data(as_text=True)
        self.assertIn("by request", html); self.assertNotIn('id="ss-form"', html); self.assertNotRegex(html, FREE)
        r = c.post("/api/request", json={"competitor": "Acme"}, headers=h)
        self.assertEqual(r.status_code, 403); self.assertNotRegex(r.get_json()["error"], FREE)

    def test_closed_pool_copy_has_no_free(self):
        with mock.patch.object(selfserve, "gate", return_value={"open": False, "free_left": 0, "reason": "window_closed"}):
            c = self._client(_cid()); h = {"X-Forwarded-For": "203.0.113.80"}
            html = c.get("/create", headers=h).get_data(as_text=True)
            self.assertIn("by request", html); self.assertNotRegex(html, FREE)
            r = c.post("/api/request", json={"competitor": "Acme"}, headers=h)
            self.assertEqual(r.status_code, 403); self.assertEqual(r.get_json()["error"], selfserve.BY_REQUEST_MSG)

    def test_rejected_result_page_and_status_copy_have_no_free(self):
        job = selfserve.new_job_id("Acme", None, None)
        with mock.patch.object(selfserve, "get_result", return_value={"status": "rejected"}):
            c = self._client(_cid())
            self.assertNotRegex(c.get(f"/result/{job}").get_data(as_text=True), FREE)
            j = c.get(f"/api/status/{job}").get_json()
            self.assertEqual(j["status"], "rejected"); self.assertNotRegex(j["message"], FREE)


class Runner(unittest.TestCase):
    def _run(self, prior, gate):
        saved = {}
        req = {"job_id": "j2", "status": "queued", "competitor": "Acme",
               "requester": {"ip": "203.0.113.77", "cid": None, "keys": ["ip:203.0.113.77"]}}
        with mock.patch.object(selfserve, "list_pending_jobs", return_value=[req]), \
             mock.patch.object(selfserve, "prior_report", return_value=prior) as pr, \
             mock.patch.object(selfserve, "gate", return_value=gate), \
             mock.patch.object(selfserve, "save_result", side_effect=lambda j, rec, **k: saved.setdefault(j, rec)), \
             mock.patch.object(run_selfserve, "generate", side_effect=AssertionError("must not generate")):
            run_selfserve.main()
        return saved, pr

    def test_runner_refuses_a_second_report_for_a_known_visitor(self):
        saved, pr = self._run({"job_id": "j1", "blocked": False}, {"open": True, "free_left": 1, "reason": None})
        pr.assert_called_once_with(["ip:203.0.113.77"], exclude_job_id="j2")
        rec = saved["j2"]
        self.assertEqual((rec["status"], rec["reason"], rec["prior_job_id"]), ("rejected", "one_per_visitor", "j1"))
        self.assertNotRegex(rec["message"], FREE)

    def test_runner_pool_closed_copy_has_no_free(self):
        saved, _ = self._run(None, {"open": False, "free_left": 0, "reason": "window_closed"})
        rec = saved["j2"]
        self.assertEqual((rec["status"], rec["reason"]), ("rejected", "window_closed"))
        self.assertEqual(rec["message"], selfserve.BY_REQUEST_MSG)


class OneSourceForTheLimits(unittest.TestCase):
    def test_workflow_does_not_override_the_pool_limits(self):
        """The app and the Action read the same config defaults; an env override in the workflow
        drifted from the app (5 vs 10) and the page offered reports the Action would refuse."""
        path = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows", "selfserve.yml")
        with open(path) as f:
            y = f.read()
        self.assertNotIn("SCOUT_SELFSERVE_FREE_LIMIT", y)
        self.assertNotIn("SCOUT_SELFSERVE_SPEND_CEILING_USD", y)


class ZoneTitle(unittest.TestCase):
    def test_no_own_company_reads_as_a_sentence(self):
        self.assertEqual(render._zone_title("where_we_win", None, "Acme"), "Where you win")
        self.assertEqual(render._zone_title("where_we_win", "Beta", "Acme"), "Where Beta wins")
        self.assertEqual(render._zone_title("where_they_win", None, "Acme"), "Where Acme wins")


if __name__ == "__main__":
    unittest.main()
