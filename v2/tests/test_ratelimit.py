"""scout/ratelimit.py + the viewer's soft limits on /api/ask and /api/request (2026-09-29)."""
import unittest
from unittest import mock

import server
from scout import config, display, ratelimit


class Limiter(unittest.TestCase):
    def test_minute_and_day_windows(self):
        L = ratelimit.Limiter(per_minute=2, per_day=3)
        self.assertEqual(L.hit("a", now=1000), (True, ""))
        self.assertEqual(L.hit("a", now=1001), (True, ""))
        self.assertEqual(L.hit("a", now=1002), (False, "minute"))       # burst
        self.assertEqual(L.hit("b", now=1002), (True, ""))              # other key untouched
        self.assertEqual(L.hit("a", now=1061), (True, ""))              # the window slid; day count 3
        self.assertEqual(L.hit("a", now=1200), (False, "day"))          # quota
        self.assertEqual(L.hit("a", now=1300), (False, "day"))          # a refusal is not counted

    def test_day_rolls_over(self):
        L = ratelimit.Limiter(per_day=1)
        with mock.patch.object(ratelimit.Limiter, "_today", return_value="2000-01-01"):
            self.assertTrue(L.hit("a")[0]); self.assertFalse(L.hit("a")[0])
        with mock.patch.object(ratelimit.Limiter, "_today", return_value="2000-01-02"):
            self.assertTrue(L.hit("a")[0])


class ViewerLimits(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False),
                  mock.patch.object(config, "ANALYTICS_ENABLED", False), mock.patch.object(display, "_commits_via_api", return_value=[]),
                  mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", ""),
                  mock.patch.object(config, "ASK_ENGINE_URL", "https://engine.test"), mock.patch.object(config, "ASK_VIEWER_SECRET", "shh"),
                  mock.patch.object(server, "_ASK_IP", ratelimit.Limiter(per_minute=3)), mock.patch.object(server, "_ASK_CID", ratelimit.Limiter(per_day=2)),
                  mock.patch.object(server, "_REQ_IP", ratelimit.Limiter(per_minute=1, per_day=5)), mock.patch.object(config, "ASK_VISITOR_QUOTA", 2)]
        for p in self.p:
            p.start()
        server.app.config["TESTING"] = True

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_visitor_quota_per_cookie_and_burst_per_ip(self):
        c = server.app.test_client()
        c.set_cookie("scout_cid", "visitor-a")
        h = {"X-Forwarded-For": "1.1.1.1, 203.0.113.9"}                       # the LAST entry is the client
        self.assertEqual(c.post("/api/ask", json={"question": "q", "rid": "browser-token-0123456789"}, headers=h).status_code, 200)
        self.assertEqual(c.post("/api/ask", json={"question": "q", "rid": "browser-token-0123456789"}, headers=h).status_code, 200)
        r = c.post("/api/ask", json={"question": "q", "rid": "browser-token-0123456789"}, headers=h)
        self.assertEqual(r.status_code, 429); self.assertIn("today's 2 questions", r.get_json()["message"])
        c2 = server.app.test_client(); c2.set_cookie("scout_cid", "visitor-b")
        self.assertEqual(c2.post("/api/ask", json={"question": "q"}, headers=h).status_code, 200)      # 3rd from this IP this minute
        r = c2.post("/api/ask", json={"question": "q"}, headers=h)
        self.assertEqual(r.status_code, 429); self.assertIn("your network", r.get_json()["message"])
        h2 = {"X-Forwarded-For": "1.1.1.1, 198.51.100.7"}
        self.assertEqual(c2.post("/api/ask", json={"question": "q"}, headers=h2).status_code, 200)     # another client IP
        with mock.patch.object(config, "ASK_QUOTA_BYPASS_CIDS", ["visitor-a"]):
            self.assertEqual(c.post("/api/ask", json={"question": "q"}, headers=h2).status_code, 200)  # the owner's cid is exempt

    def test_request_endpoint_is_limited_per_ip(self):
        c = server.app.test_client()
        h = {"X-Forwarded-For": "203.0.113.9"}
        with mock.patch.object(server.selfserve, "gate", return_value={"open": False, "reason": "closed"}):
            r1 = c.post("/api/request", json={"competitor": "X"}, headers=h)
            self.assertNotEqual(r1.status_code, 429)
            r2 = c.post("/api/request", json={"competitor": "X"}, headers=h)
            self.assertEqual(r2.status_code, 429); self.assertIn("your network", r2.get_json()["error"])
