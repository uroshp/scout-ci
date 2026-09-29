"""Flask route tests for the viewer (WS0, 2026-09-28): the first route-level coverage the viewer
has had. Runs against the committed roster (whichever card lists first), so it asserts shapes,
never counts. No network (the GitHub-backed change feed is mocked empty)."""
import re
import shutil
import subprocess
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

    def test_persona_view_reorders_and_never_dims(self):
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
        self.assertNotIn(" pdim", h); self.assertNotIn(".pdim", h)                    # no greyed-out items in the audience view (Uroš 2026-09-29)
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


class AskPanel(unittest.TestCase):
    """The in-page Ask dialog (WS2): off by default (byte-identical production), on with SCOUT_ASK=1,
    canned replay on RC, the permalink, and the answer renderer."""
    ANSWER = {"id": "a_0123456789ab", "question": "What is X's revenue?", "slug": None, "competitor": "X",
              "asked_at": "2026-09-28T20:00:00", "seconds": 61.2, "verified": True, "cost_usd": 1.1,
              "paragraphs": [{"text": "X reported $42.2 billion in Q2 2026.", "cites": [1]}],
              "sources": [{"n": 1, "id": "n1", "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary",
                           "as_of": "2026-07-30", "excerpt": "X: $42.2 billion vs. $40.54 billion expected", "from_card": False}],
              "cut_log": [{"label": "dropped", "reason": "floor: number 1e+06 not in the cited evidence"}],
              "unanswered": ["a figure of headcount"], "trajectory": {"rounds": 2, "rewritten": 1}}

    def setUp(self):
        self.slug = display.list_battlecards()[0]
        self.p = [mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False),
                  mock.patch.object(config, "ANALYTICS_ENABLED", False), mock.patch.object(display, "_commits_via_api", return_value=[]),
                  mock.patch.object(server, "_load_answer", side_effect=lambda aid: self.ANSWER if aid == "a_0123456789ab" else None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_off_by_default(self):
        c = _client()
        with mock.patch.object(config, "ASK_ENABLED", False):
            self.assertNotIn('id="ask-panel"', c.get(f"/c/{self.slug}").data.decode())
            self.assertEqual(c.post("/api/ask", json={"question": "x"}).status_code, 404)

    def test_panel_canned_replay_and_permalink(self):
        c = _client()
        with mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", "a_0123456789ab"):
            h = c.get(f"/c/{self.slug}?persona=security_regulated").data.decode()
            self.assertIn('id="ask-panel"', h); self.assertIn("for a security &amp; regulated buyer", h)
            self.assertIn('role="dialog"', h); self.assertIn("ask-fab", h)
            r = c.post("/api/ask", json={"question": "What is X's revenue?"})
            self.assertEqual(r.status_code, 200); j = r.get_json()
            self.assertEqual(j["mode"], "canned"); self.assertEqual([s["k"] for s in j["stages"]][0], "facts")
            self.assertEqual(c.post("/api/ask", json={"question": ""}).status_code, 400)
            self.assertEqual(c.post("/api/ask", json={"question": "x" * 401}).status_code, 400)
            j = c.get("/api/answers/a_0123456789ab").get_json()
            self.assertIn("srcclass-news", j["html"]); self.assertIn('class="ask-cite"', j["html"])
            self.assertIn("Could not verify", j["html"]); self.assertIn("Cut log", j["html"]); self.assertIn("1 cut", j["html"])
            self.assertIn("&#36;42.2", j["html"]) if "&#36;" in j["html"] else self.assertIn("$42.2", j["html"])
            self.assertEqual(c.get("/api/answers/a_ffffffffffff").status_code, 404)
            self.assertEqual(c.get("/api/answers/../etc").status_code, 404)
            r = c.get("/answers/a_0123456789ab"); self.assertEqual(r.status_code, 200)
            self.assertIn("ask-permalink", r.data.decode()); self.assertNotIn("Copy link", r.data.decode())
            self.assertEqual(c.get("/answers/a_ffffffffffff").status_code, 404)

    def test_widget_script_is_valid_javascript(self):
        # the f-string once turned the JS '\n\n' into two real newlines inside a string literal:
        # a syntax error that killed every handler on RC (2026-09-28). Parse what we ship.
        with mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", ""):
            h = _client().get(f"/c/{self.slug}").data.decode()
        k = h.find("var CFG=")
        self.assertGreater(k, 0)
        js = h[h.rfind("<script>", 0, k) + len("<script>"):h.find("</script>", k)]
        for lit in re.findall(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"", js):
            self.assertNotIn("\n", lit)
        self.assertNotIn("{{", js)   # no leftover f-string doubling
        self.assertNotIn("__CFG__", js); self.assertNotIn("__STAGES__", js)
        self.assertIn("split('\\n\\n')", js)   # the SSE frame separator reaches the browser as an escape
        node = shutil.which("node")
        if node:
            p = subprocess.run([node, "--check", "-"], input=js.encode(), capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr.decode()[:400])

    def test_engine_mode_returns_the_record_id_and_answers_api_serves_failures(self):
        from scout.asktoken import record_id_for
        c = _client()
        failed = {"id": "a_ffffffffff01", "question": "q", "failed": True, "error": "Scout ran out of research steps on that question.", "cost_usd": 0.7,
                  "paragraphs": [], "sources": [], "cut_log": [], "unanswered": [], "verified": False, "seconds": 0, "trajectory": {}}
        with mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", ""), \
             mock.patch.object(config, "ASK_ENGINE_URL", "https://engine.test"), mock.patch.object(config, "ASK_VIEWER_SECRET", "shh"), \
             mock.patch.object(server, "_load_answer", side_effect=lambda aid: failed if aid == "a_ffffffffff01" else None):
            j = c.post("/api/ask", json={"question": "q", "rid": "browser-token-0123456789"}).get_json()
            self.assertEqual(j["mode"], "engine"); self.assertEqual(j["id"], record_id_for("browser-token-0123456789"))
            self.assertIsNone(c.post("/api/ask", json={"question": "q", "rid": "bad token!"}).get_json()["id"])
            j = c.get("/api/answers/a_ffffffffff01").get_json()
            self.assertTrue(j["failed"]); self.assertIn("research steps", j["error"]); self.assertIn("ask-failed", j["html"])
            self.assertEqual(c.get("/api/answers/a_ffffffffff02").status_code, 404)

    def test_engine_mode_without_engine_is_an_honest_503(self):
        c = _client()
        with mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", ""):
            r = c.post("/api/ask", json={"question": "x"})
            self.assertEqual(r.status_code, 503); self.assertIn("wired up", r.get_json()["message"])


class KeyClause(unittest.TestCase):
    def test_the_marked_clause_is_bold_only_when_verbatim(self):
        from scout import askui
        h = askui._cites("GPT-6 Luna at $0.10 input undercuts Mistral's rate.", [1], "undercuts Mistral's rate")
        self.assertIn("<b>undercuts Mistral&#x27;s rate</b>", h)
        self.assertNotIn("<b>", askui._cites("GPT-6 Luna undercuts.", [1], "not in the sentence"))
        from scout import ask
        p = ask._para({"text": "A costs $10, B costs $2.", "cites": ["c1"], "key": "B costs $2"}, {"c1": 1})
        self.assertEqual(p["key"], "B costs $2")
        self.assertNotIn("key", ask._para({"text": "A costs $10.", "cites": ["c1"], "key": "A costs $10."}, {"c1": 1}))   # the whole sentence is not a key


class TriggeredBy(unittest.TestCase):
    def test_alert_row_shows_the_signal_chip(self):
        from scout import display as _d
        slug = _d.list_battlecards()[0]
        rows = _d.load_alerts(slug)
        with mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False), mock.patch.object(config, "ANALYTICS_ENABLED", False), \
             mock.patch.object(_d, "_commits_via_api", return_value=[]), \
             mock.patch.object(_d, "load_alerts", return_value=rows + [{"detected_at": "2026-09-29T11:05:00", "headline": "Q3 8-K: revenue guidance raised", "severity": "act",
                                                                         "source_url": "https://www.sec.gov/x", "triggered_by": {"kind": "filing", "summary": "New 8-K filed 2026-09-29"}}]):
            h = _client().get(f"/c/{slug}").data.decode()
        self.assertIn('class="trig"', h); self.assertIn("Triggered by: new filing", h); self.assertIn('title="New 8-K filed 2026-09-29"', h)


class CutLogShapes(unittest.TestCase):
    def test_both_entry_shapes_render(self):
        md = ("# Card\n\n## Cut Log\n\n- **CUT — analyst 3x estimate:** unverifiable.\n\n"
              "**REVISED — Copilot cost play (Executive Summary):** figures now quoted as projections.\n\n"
              "**CUT — GitHub Copilot moved every plan to usage-based billing:** removed as contamination. Human-labelled unsupported.\n")
        html, n = page._cut_log(md)
        self.assertEqual(n, 3)
        self.assertIn("removed as contamination", html); self.assertIn("quoted as projections", html); self.assertIn("unverifiable", html)
