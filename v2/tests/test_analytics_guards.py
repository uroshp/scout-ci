"""The 2026-07-21 server-analytics guards.

Pins: (1) suspicious_query kills exploit-spray query strings (the 7/20 Laravel barrage
minted 231 phantom GA users) but NEVER blocks clean human links — undercounting humans is
the worse error (Uroš's rule); (2) _device_from_ua fills GA4 MP device dims from the UAs
we already hold, so server events stop reading '(not set)'. Pure, no network.

    python -m unittest discover -s tests
"""
import unittest

from scout import analytics


class SuspiciousQuery(unittest.TestCase):
    def test_exploit_patterns_blocked(self):
        barrage = "config=..%2F..%2Fstorage%2Flogs%2Flaravel.log&anything=x"
        self.assertTrue(analytics.suspicious_query(barrage))
        self.assertTrue(analytics.suspicious_query("file=../../etc/passwd"))
        self.assertTrue(analytics.suspicious_query("page=.env"))
        self.assertTrue(analytics.suspicious_query("x=shell.php"))
        self.assertTrue(analytics.suspicious_query("redirect=http://evil.example"))
        self.assertTrue(analytics.suspicious_query("a=" + "Z" * 600))

    def test_clean_human_queries_pass(self):
        for qs in ("", None, "card=groq__vs__cerebras", "utm_source=resume&utm_medium=pdfB",
                   "fbclid=IwAR2abc123", "me=1", "utm_campaign=july"):
            self.assertFalse(analytics.suspicious_query(qs), qs)


class DeviceFromUa(unittest.TestCase):
    MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
    WIN_EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.2)")
    IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1")

    def test_mac_chrome_desktop(self):
        d = analytics._device_from_ua(self.MAC_CHROME)
        self.assertEqual((d["category"], d["browser"], d["operating_system"]),
                         ("desktop", "Chrome", "Macintosh"))
        self.assertEqual(d["browser_version"], "150")

    def test_edge_beats_chrome_token(self):
        d = analytics._device_from_ua(self.WIN_EDGE)
        self.assertEqual((d["browser"], d["operating_system"]), ("Edge", "Windows"))

    def test_iphone_is_mobile_safari(self):
        d = analytics._device_from_ua(self.IPHONE)
        self.assertEqual((d["category"], d["browser"], d["operating_system"]),
                         ("mobile", "Safari", "iOS"))

    def test_empty_ua_still_yields_category(self):
        self.assertIn("category", analytics._device_from_ua(""))


class ServerVisitOnlyOn200(unittest.TestCase):
    """A 404 (scanner miss) or a redirect must never fire server_visit — only a real page."""
    UA = DeviceFromUa.MAC_CHROME

    def _fired(self, path):
        from unittest import mock
        import server
        calls = []
        import os
        # the feed is live only on the production service (server._ga_server_events_live); this
        # class tests the 200-only rule, so it stands in for production and mocks the sender thread
        with mock.patch.dict(os.environ, {"K_SERVICE": "agent-scout"}), \
             mock.patch.object(server.config, "ANALYTICS_ENABLED", True), \
             mock.patch.object(server.threading, "Thread",
                               side_effect=lambda **kw: mock.Mock(start=lambda: calls.append(1))):
            server.app.test_client().get(path, headers={"User-Agent": self.UA})
        return bool(calls)

    def test_200_fires(self):
        self.assertTrue(self._fired("/"))

    def test_404_does_not_fire(self):
        self.assertFalse(self._fired("/wp-login.php"))


if __name__ == "__main__":
    unittest.main()


class LiveRuntimeLock(unittest.TestCase):
    """No path reaches GA or the visit log outside a production runtime (2026-10-01: the route
    tests and the stub's AppTest were minting real server_visit events on every full run)."""

    def _send(self, env):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(analytics.os.path, "isdir", return_value=False), \
             mock.patch.object(analytics.config, "GA_MEASUREMENT_ID", "G-TEST"), \
             mock.patch.object(analytics.config, "GA_API_SECRET", "s3cret"), \
             mock.patch.object(analytics.urllib.request, "urlopen") as urlopen:
            for k in ("K_SERVICE", "SCOUT_GA_SERVER_EVENTS"):
                if k not in env:
                    os.environ.pop(k, None)
            analytics._ga4_server_event("cid", "1.2.3.4", "", "", {}, ua="Mozilla/5.0 Safari")
            return urlopen.called

    def test_sender_is_silent_outside_production(self):
        self.assertFalse(self._send({}))
        self.assertFalse(self._send({"K_SERVICE": "agent-scout-rc"}))

    def test_sender_fires_on_the_production_service(self):
        self.assertTrue(self._send({"K_SERVICE": "agent-scout"}))

    def test_visit_log_is_not_written_outside_production(self):
        import os
        from unittest import mock
        with mock.patch.object(analytics.os.path, "isdir", return_value=False), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("scout.selfserve.append_data") as append:
            os.environ.pop("K_SERVICE", None); os.environ.pop("SCOUT_GA_SERVER_EVENTS", None)
            analytics._log_async("cid", "1.2.3.4", "", "Mozilla/5.0 Safari", "")
        self.assertFalse(append.called)
