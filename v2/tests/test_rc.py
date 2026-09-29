"""The RC environment (2026-09-28): the same viewer code deployed twice. On RC (SCOUT_RC=1 +
SCOUT_RC_PASSWORD) every page is gated and ribboned, robots disallows, analytics is off; in
production (nothing set) all of it is inert. Store reads under a data prefix fall through to the
production path; writers never do. Flask test client + mocks; no network."""
import unittest
from unittest import mock

import server
from scout import config, selfserve


def _client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


class Inert(unittest.TestCase):
    """Production: no password, no RC mode -> exactly today's behaviour."""

    def test_no_gate_no_ribbon_permissive_robots(self):
        with mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False):
            c = _client()
            r = c.get("/robots.txt")
            self.assertIn(b"Allow: /", r.data)
            r = c.get("/healthcheck")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(c.post("/rc-login", data={"password": "x"}).status_code, 404)
            html = server._doc("<p>x</p>", title="T")
            self.assertNotIn("rc-ribbon", html)
            self.assertIn("<title>T</title>", html)


class Gate(unittest.TestCase):
    def setUp(self):
        self.p1 = mock.patch.object(config, "RC_PASSWORD", "sesame")
        self.p2 = mock.patch.object(config, "RC_MODE", True)
        self.p1.start(); self.p2.start()
        self.c = _client()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def test_pages_require_the_cookie(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"RC password", r.data)
        self.assertIn(b"rc-ribbon", r.data)                     # the login page is ribboned too
        with mock.patch.object(config, "ASK_ENABLED", True):
            self.assertNotIn(b'id="ask-fab"', self.c.get("/").data)   # no Ask widget before you are in
        for path in ("/healthcheck", "/robots.txt", "/favicon.ico"):
            self.assertNotEqual(self.c.get(path).status_code, 401, path)

    def test_wrong_password_is_refused(self):
        r = self.c.post("/rc-login", data={"password": "nope", "next": "/c/x"})
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"did not match", r.data)
        self.assertNotIn("scout_rc", r.headers.get("Set-Cookie", ""))

    def test_right_password_sets_cookie_and_opens_pages(self):
        r = self.c.post("/rc-login", data={"password": "sesame", "next": "/robots.txt"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["Location"].rstrip("/").endswith("/robots.txt"), True)
        self.assertIn("scout_rc=", r.headers["Set-Cookie"]); self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        r = self.c.get("/")                                   # cookie jar carries it
        self.assertNotEqual(r.status_code, 401)

    def test_next_cannot_leave_the_site(self):
        r = self.c.post("/rc-login", data={"password": "sesame", "next": "//evil.example/x"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["Location"].endswith("/"))
        self.assertNotIn("evil", r.headers["Location"])

    def test_forged_cookie_is_refused(self):
        self.c.set_cookie("scout_rc", "deadbeef" * 4)
        self.assertEqual(self.c.get("/").status_code, 401)

    def test_rc_mode_ribbon_title_and_robots(self):
        self.assertIn(b"Disallow: /", self.c.get("/robots.txt").data)
        html = server._doc("<p>x</p>", title="T")
        self.assertIn("rc-ribbon", html); self.assertIn("<title>[RC] T</title>", html)


class AnalyticsSwitch(unittest.TestCase):
    def test_off_means_no_tag_and_no_server_event(self):
        with mock.patch.object(config, "ANALYTICS_ENABLED", False), \
             mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False), \
             mock.patch.object(server.threading, "Thread") as th:
            self.assertEqual(server._ga_head("card"), "")
            _client().get("/robots.txt", headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/140.0"})
            th.assert_not_called()

    def test_on_keeps_the_tag(self):
        with mock.patch.object(config, "ANALYTICS_ENABLED", True), mock.patch.object(config, "GA_MEASUREMENT_ID", "G-TEST"):
            self.assertIn("G-TEST", server._ga_head("card"))


class ReadThrough(unittest.TestCase):
    """rc/ prefix + fallback: reads fall through to production, writers never do."""

    def _patched(self, fallback=True):
        return [mock.patch.object(config, "SELFSERVE_DATA_PREFIX", "rc"),
                mock.patch.object(config, "SELFSERVE_DATA_READ_FALLBACK", fallback),
                mock.patch.object(selfserve, "use_github", return_value=True)]

    def test_read_falls_through_when_prefixed_missing(self):
        calls = []
        def gh_get(path, prefixed=True):
            calls.append((path, prefixed))
            return (None, None) if prefixed else ("PROD", "sha-prod")
        with mock.patch.object(selfserve, "_gh_get", side_effect=gh_get):
            for p in self._patched(): p.start()
            try:
                self.assertEqual(selfserve.read_data("propagation/x/1.json"), "PROD")
            finally:
                for p in self._patched(): p.stop()
        self.assertEqual(calls, [("propagation/x/1.json", True), ("propagation/x/1.json", False)])

    def test_prefixed_hit_wins_and_no_fallback_without_flag(self):
        with mock.patch.object(selfserve, "_gh_get", side_effect=lambda path, prefixed=True: ("RC", "s") if prefixed else ("PROD", "p")) as g:
            ps = self._patched(); [p.start() for p in ps]
            try:
                self.assertEqual(selfserve.read_data("a"), "RC"); self.assertEqual(g.call_count, 1)
            finally:
                [p.stop() for p in ps]
        with mock.patch.object(selfserve, "_gh_get", return_value=(None, None)) as g:
            ps = self._patched(fallback=False); [p.start() for p in ps]
            try:
                self.assertIsNone(selfserve.read_data("a")); self.assertEqual(g.call_count, 1)
            finally:
                [p.stop() for p in ps]

    def test_list_falls_through_too(self):
        def gh_list(path, include_dirs=False, prefixed=True):
            return [] if prefixed else ["2026-09"]
        with mock.patch.object(selfserve, "_gh_list", side_effect=gh_list):
            ps = self._patched(); [p.start() for p in ps]
            try:
                self.assertEqual(selfserve.list_data("propagation/x", include_dirs=True), ["2026-09"])
            finally:
                [p.stop() for p in ps]

    def test_writers_never_fall_through(self):
        """update_data reads its sha via _gh_get(path) with the prefix only; a production sha must
        never be PUT under rc/."""
        seen = []
        def gh_get(path, prefixed=True):
            seen.append(prefixed); return (None, None)
        with mock.patch.object(selfserve, "_gh_get", side_effect=gh_get), \
             mock.patch.object(selfserve, "_gh_put") as put:
            ps = self._patched(); [p.start() for p in ps]
            try:
                selfserve.update_data("ask/state.json", lambda cur: "{}", "m")
            finally:
                [p.stop() for p in ps]
        self.assertTrue(all(seen)); put.assert_called_once()
        self.assertEqual(put.call_args[0][0], "ask/state.json")

    def test_repo_path_prefix(self):
        with mock.patch.object(config, "SELFSERVE_DATA_PREFIX", "rc"):
            self.assertEqual(selfserve._repo_path("a/b"), "rc/a/b")
            self.assertEqual(selfserve._repo_path("a/b", prefixed=False), "a/b")


if __name__ == "__main__":
    unittest.main()
