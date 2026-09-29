"""Flask route tests for the viewer (WS0, 2026-09-28): the first route-level coverage the viewer
has had. Runs against the committed roster (whichever card lists first), so it asserts shapes,
never counts. No network (the GitHub-backed change feed is mocked empty)."""
import re
import unittest
from unittest import mock

import server
from scout import config, display, page, schema


def _client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.slug = display.list_battlecards()[0]
        cls.p = [mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False),
                 mock.patch.object(config, "ANALYTICS_ENABLED", False),
                 mock.patch.object(display, "_commits_via_api", return_value=[])]
        for p in cls.p:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.p:
            p.stop()

    def test_home_and_card_render_with_chips_and_rail_panels(self):
        c = _client()
        for path in ("/", f"/c/{self.slug}"):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)
            h = r.data.decode()
            self.assertIn("Living battlecards", h)
            self.assertRegex(h, r'srcclass srcclass-[a-z_]+')          # a class chip on a citation
            self.assertIn("All sources by kind", h)                       # rail Sources panel
            self.assertIn("Pick your audience", h)                        # the persona picker
            self.assertIn('class="rail-history"', h)                      # git feed demoted to History
            self.assertNotIn("Change feed", h)

    def test_tabs_mark_the_active_page(self):
        c = _client()
        def on(path):
            h = c.get(path).data.decode()
            i = h.find('<div class="scout-tabs">')
            return [href for cls, href in re.findall(r'<a class="(on|)" href="([^"]+)"', h[i:i + 400]) if cls == "on"][:1]
        self.assertEqual(on("/"), ["/"])
        self.assertEqual(on("/create"), ["/create"])

    def test_sources_page(self):
        c = _client()
        r = c.get(f"/c/{self.slug}/sources")
        self.assertEqual(r.status_code, 200)
        h = r.data.decode()
        self.assertIn("What this card rests on", h)
        self.assertIn('class="srchost"', h)
        self.assertIn(f'href="/c/{self.slug}#u-', h)                      # deep links back to claims
        self.assertIn("Back to the card", h)
        self.assertEqual(c.get("/c/not-a-card/sources").status_code, 404)

    def test_persona_view_reorders_and_dims(self):
        c = _client()
        base = c.get(f"/c/{self.slug}").data.decode()
        self.assertNotIn(" pdim", base)
        present = [p for p in schema.PERSONAS if f"?persona={p}" in base]
        self.assertTrue(present, "the rail lists the personas present on the card")
        p = present[-1]
        h = c.get(f"/c/{self.slug}?persona={p}").data.decode()
        self.assertIn(f'class="pv p-{p} on" href="/c/{self.slug}?persona={p}"', h)
        self.assertIn(f'class="persona p-{p}"', h)                          # badge carries the colour class
        first = re.search(r'id="bc".*?<div class="item[^"]*" id="(u-[^"]+)"', h, re.S)
        self.assertIsNotNone(first)
        # the first play in the battlecard carries the chosen persona (when that persona has plays)
        claims = [x for x in page._prepare_display(__import__("scout.store", fromlist=["x"]).load_claims(self.slug))[0]
                  if x.get("zone") == "where_we_win"]
        if any(x.get("persona") == p for x in claims):
            self.assertTrue(any(page._anchor(x.get("subject_key", "")) == first.group(1) and x.get("persona") == p for x in claims))
        self.assertEqual(c.get(f"/c/{self.slug}?persona=hacker").status_code, 200)   # unknown = default
        self.assertEqual(c.get(f"/print/{self.slug}?persona={p}").status_code, 200)

    def test_healthcheck_and_robots_untouched(self):
        c = _client()
        self.assertEqual(c.get("/healthcheck").data, b"ok")
        self.assertIn(b"Allow: /", c.get("/robots.txt").data)


class Helpers(unittest.TestCase):
    def test_prepare_display_resolves_class_for_derived_claims(self):
        fact = {"id": "c_0123456789ab", "claim": "f", "claim_type": "fact", "source_url": "https://www.sec.gov/x",
                "source_tier": "primary", "section": "recent_moves"}
        derived = {"id": "c_0123456789ac", "claim": "d", "claim_type": "interpretation", "derived_from": "c_0123456789ab",
                   "section": "battlecard", "zone": "where_we_win"}
        prov = {"id": "c_0123456789ad", "claim": "p", "claim_type": "interpretation", "derived_from": "c_0123456789ab",
                "provenance": {"source_url": "https://www.cnbc.com/y", "source_tier": "reputable_secondary"}, "section": "battlecard"}
        active, _ = page._prepare_display([fact, derived, prov], {"competitor": "Acme"})
        by = {c["id"]: c for c in active}
        self.assertEqual(by["c_0123456789ab"]["source_class"], "filing")
        self.assertEqual((by["c_0123456789ac"]["source_url"], by["c_0123456789ac"]["source_class"]), ("https://www.sec.gov/x", "filing"))
        self.assertEqual((by["c_0123456789ad"]["source_url"], by["c_0123456789ad"]["source_class"]), ("https://www.cnbc.com/y", "news"))
        self.assertNotIn("source_class", fact)                                  # never mutates the store list


if __name__ == "__main__":
    unittest.main()
