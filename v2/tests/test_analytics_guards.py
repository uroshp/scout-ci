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


if __name__ == "__main__":
    unittest.main()
