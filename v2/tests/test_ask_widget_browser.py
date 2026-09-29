"""The Ask Scout widget driven in a real (headless) browser (2026-09-28, after RC review found it
unusable: the panel showed by default, minimize did nothing, Enter added a newline, Ask navigated
the page and lost the question). Every request the browser makes is answered from the Flask test
client, so the test is hermetic; it skips where Playwright or its Chromium is not installed.

Run alone:  python -m unittest tests.test_ask_widget_browser
"""
import json
import unittest
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
    _HAVE_PW = True
except Exception:   # pragma: no cover
    _HAVE_PW = False

import server
from scout import askui, asktoken, config, display, ratelimit

ORIGIN = "http://scout.test"
ANSWER = {"id": "a_0123456789ab", "question": "What is X's revenue?", "slug": None, "competitor": "X",
          "asked_at": "2026-09-28T20:00:00", "seconds": 61.2, "verified": True, "cost_usd": 1.1,
          "paragraphs": [{"text": "X reported $42.2 billion in Q2 2026.", "cites": [1]}],
          "sources": [{"n": 1, "id": "n1", "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary",
                       "as_of": "2026-07-30", "excerpt": "X: $42.2 billion vs. $40.54 billion expected", "from_card": False}],
          "cut_log": [], "unanswered": [], "trajectory": {"rounds": 1, "rewritten": 0}}


def _serve(route):
    """Answer the browser's request from the Flask test client (stages sped up so the canned
    replay finishes in milliseconds)."""
    req = route.request
    if not req.url.startswith(ORIGIN):
        return route.fulfill(status=404, body="")
    path = req.url[len(ORIGIN):]
    c = server.app.test_client()
    resp = c.open(path, method=req.method, data=req.post_data, headers={"Content-Type": req.headers.get("content-type", "")})
    body = resp.data
    if path == "/api/ask" and resp.status_code == 200:
        j = resp.get_json()
        for s in j.get("stages") or []:
            s["ms"] = 1
        body = json.dumps(j).encode()
    route.fulfill(status=resp.status_code, body=body, content_type=resp.content_type)


@unittest.skipUnless(_HAVE_PW, "playwright not installed")
class Widget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.slugs = display.list_battlecards()[:2]
        cls.p = [mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False),
                 mock.patch.object(config, "ANALYTICS_ENABLED", False), mock.patch.object(display, "_commits_via_api", return_value=[]),
                 mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", "a_0123456789ab"),
                 mock.patch.object(server, "_load_answer", side_effect=lambda aid: ANSWER if aid == "a_0123456789ab" else None)]
        for p in cls.p:
            p.start()
        server.app.config["TESTING"] = True
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:   # pragma: no cover
            cls.pw.stop()
            for p in cls.p:
                p.stop()
            raise unittest.SkipTest(f"chromium not available: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()
        for p in cls.p:
            p.stop()

    def _page(self, width=1200, height=900):
        ctx = self.browser.new_context(viewport={"width": width, "height": height})
        page = ctx.new_page()
        page.route("**/*", _serve)
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"{ORIGIN}/c/{self.slugs[0]}")
        return page

    def test_the_whole_interaction(self):
        page = self._page()
        panel, fab, q = page.locator("#ask-panel"), page.locator("#ask-fab"), page.locator("#ask-q")
        # 1. closed by default: the launcher shows, the panel does not
        self.assertTrue(fab.is_visible()); self.assertTrue(panel.is_hidden())
        # 2. opening holds the page in place and focuses the composer
        page.evaluate("window.scrollTo(0, 600)")
        y0 = page.evaluate("window.scrollY")
        fab.click()
        self.assertTrue(panel.is_visible()); self.assertTrue(fab.is_hidden())
        page.wait_for_function("document.activeElement && document.activeElement.id === 'ask-q'")
        self.assertEqual(page.evaluate("window.scrollY"), y0)
        # 2b. clicking into the composer holds the page in place too (his report, 2026-09-29)
        page.mouse.click(1000, 800)                                          # somewhere over the page, then the field
        q.click(); page.wait_for_timeout(450)
        self.assertEqual(page.evaluate("window.scrollY"), y0)
        # 3. Enter sends (Shift+Enter is a newline), the question stays in the thread, the page never navigates
        url0 = page.url
        q.fill("line one"); q.press("Shift+Enter")
        self.assertIn("\n", q.input_value()); self.assertEqual(page.locator(".ask-user").count(), 0)
        q.fill("What is X's revenue?"); q.press("Enter")
        self.assertEqual(page.locator(".ask-user").count(), 1); self.assertEqual(q.input_value(), "")
        self.assertIn("What is X's revenue?", page.locator(".ask-user").inner_text())
        self.assertTrue(page.locator("#ask-go").is_disabled())   # busy while Scout works
        page.wait_for_selector(".ask-answer", timeout=15000)
        self.assertEqual(page.url, url0); self.assertEqual(page.evaluate("window.scrollY"), y0)
        self.assertIn("42.2", page.locator(".ask-answer").inner_text())
        self.assertFalse(page.locator("#ask-go").is_disabled()); self.assertEqual(q.get_attribute("placeholder"), "Ask a follow-up")
        # 4. the Ask button works too
        q.fill("and their margin?"); page.locator("#ask-go").click()
        self.assertEqual(page.locator(".ask-user").count(), 2)
        page.wait_for_function("document.querySelectorAll('.ask-answer').length === 2", timeout=15000)
        # 5. minimize: the panel goes away, the launcher returns with the turn count
        page.locator("#ask-close").click()
        self.assertTrue(panel.is_hidden()); self.assertTrue(fab.is_visible()); self.assertEqual(page.locator("#ask-fab-n").inner_text(), "2")
        # 6. reopen in the same session: the conversation is still there; Esc minimizes
        fab.click()
        self.assertTrue(panel.is_visible()); self.assertEqual(page.locator(".ask-user").count(), 2)
        page.keyboard.press("Escape"); self.assertTrue(panel.is_hidden())
        # 7. another card, and a reload: one thread across cards and visits, minimized as it was left
        page.goto(f"{ORIGIN}/c/{self.slugs[-1]}")
        self.assertTrue(panel.is_hidden()); self.assertEqual(page.locator("#ask-fab-n").inner_text(), "2")
        fab.click()
        self.assertEqual(page.locator(".ask-user").count(), 2); self.assertEqual(page.locator(".ask-answer").count(), 2)
        page.reload()
        self.assertTrue(panel.is_visible())   # left open, so it opens
        self.assertEqual(page.locator(".ask-user").count(), 2)
        # 8. a new thread empties it
        page.locator("#ask-clear").click()
        self.assertEqual(page.locator(".ask-user").count(), 0); self.assertTrue(page.locator("#ask-intro").is_visible())
        self.assertTrue(page.locator("#ask-fab-n").is_hidden() or page.locator("#ask-fab-n").inner_text() == "")
        self.assertEqual(self.errors, [])
        page.context.close()

    def test_phone_is_a_full_sheet(self):
        page = self._page(390, 844)
        page.locator("#ask-fab").click()
        box = page.locator("#ask-panel").bounding_box()
        self.assertEqual((round(box["x"]), round(box["width"])), (0, 390))
        self.assertGreaterEqual(round(box["height"]), 800)
        self.assertEqual(self.errors, [])
        page.context.close()


@unittest.skipUnless(_HAVE_PW, "playwright not installed")
class Recovery(unittest.TestCase):
    """Engine mode in the browser: the stream drops (Safari did, 2026-09-28, on a 6-minute run), the
    reader reloads mid-question, the run fails. The engine is a route stub; the record the engine
    would have written appears in RECORDS when the test says so, and the panel polls it in."""
    RECORDS: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.slugs = display.list_battlecards()[:2]
        cls.p = [mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False),
                 mock.patch.object(config, "ANALYTICS_ENABLED", False), mock.patch.object(display, "_commits_via_api", return_value=[]),
                 mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", ""),
                 mock.patch.object(config, "ASK_ENGINE_URL", "http://engine.test"), mock.patch.object(config, "ASK_VIEWER_SECRET", "shh"),
                 mock.patch.object(askui, "POLL_MS", 250), mock.patch.object(askui, "POLL_MAX_MS", 60000),
                 mock.patch.object(server, "_ASK_IP", ratelimit.Limiter(per_minute=10_000)), mock.patch.object(server, "_ASK_CID", ratelimit.Limiter(per_day=10_000)),
                 mock.patch.object(server, "_load_answer", side_effect=lambda aid: cls.RECORDS.get(aid))]
        for p in cls.p:
            p.start()
        server.app.config["TESTING"] = True
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:   # pragma: no cover
            cls.pw.stop()
            for p in cls.p:
                p.stop()
            raise unittest.SkipTest(f"chromium not available: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()
        for p in cls.p:
            p.stop()

    def _page(self, finish=False):
        self.RECORDS.clear(); self.ids = []; self.finish = finish
        ctx = self.browser.new_context(viewport={"width": 1200, "height": 900})
        page = ctx.new_page()
        page.route("**/*", _serve)
        cors = {"Access-Control-Allow-Origin": ORIGIN, "Access-Control-Allow-Headers": "authorization,content-type", "Access-Control-Allow-Methods": "POST"}

        self.modes = []
        def engine(route):
            if route.request.method == "OPTIONS":
                return route.fulfill(status=200, headers=cors, body="")
            body_in = route.request.post_data_json or {}
            rid = body_in.get("rid"); self.modes.append(body_in.get("mode"))
            aid = asktoken.record_id_for(rid); self.ids.append(aid)
            if self.finish:                                                     # a complete answer, quick or deep
                kind = "deep" if body_in.get("mode") == "deep" else "quick"
                rec = dict(ANSWER, id=aid, kind=kind, question=body_in.get("question"), unanswered=["a figure of headcount"],
                           cut_log=[{"label": "dropped", "reason": "verifier: overreaches"}])
                body = ('data: {"stage": "facts", "id": "%s", "kind": "%s"}\n\n' % (aid, kind) + 'data: {"stage": "%s"}\n\n' % ("search" if kind == "deep" else "draft")
                        + "data: " + json.dumps({"done": True, "id": aid, "kind": kind, "html": askui.answer_html(rec, show_question=False, chat=True), "cost_usd": 0.2, "seconds": 21, "verified": True}) + "\n\n")
            else:
                body = ('data: {"stage": "facts", "id": "%s"}\n\n' % aid + 'data: {"stage": "search", "extra": "24 known facts"}\n\n'
                        + 'data: {"activity": "Searching: X pricing"}\n\n')      # ...and the connection drops here
            route.fulfill(status=200, headers=cors, content_type="text/event-stream", body=body)
        page.route("http://engine.test/ask", engine)
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"{ORIGIN}/c/{self.slugs[0]}")
        page.locator("#ask-fab").click()
        return page

    def _record(self, aid, failed=False):
        rec = dict(ANSWER, id=aid, question="What is X's revenue?")
        if failed:
            rec = {"id": aid, "question": "q", "failed": True, "error": "Scout ran out of research steps on that question.", "cost_usd": 0.7,
                   "paragraphs": [], "sources": [], "cut_log": [], "unanswered": [], "verified": False, "seconds": 0, "trajectory": {}}
        self.RECORDS[aid] = rec

    def test_dropped_stream_recovers_the_answer(self):
        page = self._page()
        page.locator("#ask-q").fill("What is X's revenue?"); page.locator("#ask-q").press("Enter")
        page.wait_for_selector(".ask-note", timeout=10000)
        self.assertIn("Connection dropped", page.locator(".ask-note").inner_text())
        self.assertIn("Searching: X pricing", page.locator(".ask-acts").inner_text())      # what it was doing stays visible
        self.assertIn("1 to 3 min", page.locator(".ask-hint").inner_text())
        self.assertIn("fact-checks every sentence", page.locator(".ask-why").inner_text())
        self.assertIsNotNone(page.evaluate("localStorage.getItem('scout_ask_pending_v1')"))
        page.wait_for_timeout(600)                                                          # a few 404 polls
        self._record(self.ids[-1])
        page.wait_for_selector(".ask-answer", timeout=10000)
        self.assertIn("42.2", page.locator(".ask-answer").inner_text())
        self.assertEqual(page.locator("#ask-fab-n").inner_text(), "1")
        self.assertIsNone(page.evaluate("localStorage.getItem('scout_ask_pending_v1')"))
        self.assertFalse(page.locator("#ask-go").is_disabled())
        self.assertEqual(self.errors, []); page.context.close()

    def test_reload_mid_question_picks_the_answer_up(self):
        page = self._page()
        page.locator("#ask-q").fill("What is X's revenue?"); page.locator("#ask-q").press("Enter")
        page.wait_for_selector(".ask-note", timeout=10000)
        page.goto(f"{ORIGIN}/c/{self.slugs[-1]}")                                            # another card, mid-question
        page.wait_for_selector(".ask-note", timeout=10000)
        self.assertIn("Still working", page.locator(".ask-note").inner_text())
        self.assertEqual(page.locator(".ask-user").count(), 1)                              # the question is back in the thread
        self.assertTrue(page.locator("#ask-go").is_disabled())
        self._record(self.ids[-1])
        page.wait_for_selector(".ask-answer", timeout=10000)
        self.assertEqual(page.locator("#ask-fab-n").inner_text(), "1")
        page.reload(); page.wait_for_selector(".ask-answer", timeout=10000)                  # and it is a normal turn now
        self.assertIsNone(page.evaluate("localStorage.getItem('scout_ask_pending_v1')"))
        self.assertEqual(self.errors, []); page.context.close()

    def test_failed_run_shows_the_honest_message_and_is_not_a_turn(self):
        page = self._page()
        page.locator("#ask-q").fill("What is X's revenue?"); page.locator("#ask-q").press("Enter")
        page.wait_for_selector(".ask-note", timeout=10000)
        self._record(self.ids[-1], failed=True)
        page.wait_for_selector(".ask-none", timeout=10000)
        self.assertIn("research steps", page.locator(".ask-none").inner_text())
        self.assertTrue(page.locator("#ask-fab-n").is_hidden())
        self.assertIsNone(page.evaluate("localStorage.getItem('scout_ask_pending_v1')"))
        self.assertEqual(self.errors, []); page.context.close()


    def test_quick_answer_layout_then_research_deeper(self):
        page = self._page(finish=True)
        # header: the name is the headline, Beta beside it, one line under it, no "currently on"
        head = page.locator(".ask-head").inner_text()
        self.assertIn("Ask Scout", head); self.assertIn("BETA", head.upper()); self.assertIn("Ask Scout about any competitor", head); self.assertNotIn("currently on", head)
        chips = page.locator(".ask-ex").all_inner_texts()
        self.assertEqual(len(chips), 3); self.assertTrue(any("is cheaper" in c for c in chips)); self.assertFalse(any("hiring" in c for c in chips))
        self.assertTrue(all("{competitor}" not in c for c in chips))
        page.locator("#ask-q").fill("Is X cheaper?"); page.locator("#ask-q").press("Enter")
        page.wait_for_selector(".ask-answer", timeout=10000)
        self.assertEqual(self.modes, ["quick"])                                            # Enter asks the quick path
        box = page.locator(".ask-answer").first
        self.assertIn("42.2", box.inner_text())
        # collapsed: the summary line shows, the sections do not
        self.assertEqual([b.strip() for b in page.locator(".ask-tgl").all_inner_texts()], ["1 source", "1 could not verify", "1 cut"])
        self.assertTrue(page.locator(".ask-srcs").is_hidden()); self.assertTrue(page.locator(".ask-unans").is_hidden())
        self.assertIn("answered from what Scout already knew", page.locator(".ask-foot").inner_text())
        page.locator(".ask-tgl").nth(1).click(); self.assertTrue(page.locator(".ask-unans").is_visible())
        page.locator(".ask-cite").first.click(); self.assertTrue(page.locator(".ask-srcs").is_visible())   # [n] opens the sources
        page.locator(".ask-tgl").first.click(); self.assertTrue(page.locator(".ask-srcs").is_hidden())
        # Research deeper runs the deep path as its own turn on the same question
        page.locator(".ask-deeper").click()
        self.assertEqual(page.locator(".ask-user").count(), 2); self.assertIn("RESEARCH DEEPER", page.locator(".ask-user").last.inner_text().upper())
        page.wait_for_function("document.querySelectorAll('.ask-answer').length === 2", timeout=10000)
        self.assertEqual(self.modes, ["quick", "deep"])
        self.assertIn("researched and fact-checked", page.locator(".ask-foot").last.inner_text())
        self.assertEqual(page.locator(".ask-deeper").count(), 1)                              # the deep answer offers no further deepening
        self.assertEqual(page.locator("#ask-fab-n").inner_text(), "2")
        page.reload(); page.wait_for_selector(".ask-answer", timeout=10000)
        self.assertEqual(page.locator(".ask-answer").count(), 2); self.assertEqual(page.locator(".ask-tgl").count(), 6)   # restored with its toggles
        page.locator(".ask-tgl").first.click(); self.assertTrue(page.locator(".ask-srcs").first.is_visible())          # ...and they work after restore
        self.assertEqual(self.errors, []); page.context.close()


@unittest.skipUnless(_HAVE_PW, "playwright not installed")
class Touch(unittest.TestCase):
    """iPad (Uroš, 2026-09-29: "first tap focuses, second acts; wobbly on focus"): touch + mobile
    emulation on an iPad-sized viewport. Single taps act, nothing auto-focuses the composer (a
    programmatic focus pops the keyboard), hover rules are off, the page holds still."""

    @classmethod
    def setUpClass(cls):
        cls.slug = display.list_battlecards()[0]
        cls.p = [mock.patch.object(config, "RC_PASSWORD", ""), mock.patch.object(config, "RC_MODE", False),
                 mock.patch.object(config, "ANALYTICS_ENABLED", False), mock.patch.object(display, "_commits_via_api", return_value=[]),
                 mock.patch.object(config, "ASK_ENABLED", True), mock.patch.object(config, "ASK_CANNED_ID", "a_0123456789ab"),
                 mock.patch.object(server, "_load_answer", side_effect=lambda aid: ANSWER if aid == "a_0123456789ab" else None)]
        for p in cls.p:
            p.start()
        server.app.config["TESTING"] = True
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:   # pragma: no cover
            cls.pw.stop()
            for p in cls.p:
                p.stop()
            raise unittest.SkipTest(f"chromium not available: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()
        for p in cls.p:
            p.stop()

    def test_single_taps_and_no_auto_focus(self):
        ctx = self.browser.new_context(viewport={"width": 1024, "height": 768}, has_touch=True, is_mobile=True, device_scale_factor=2)
        page = ctx.new_page(); page.route("**/*", _serve)
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{ORIGIN}/c/{self.slug}")
        self.assertFalse(page.evaluate("matchMedia('(hover: hover)').matches"))
        self.assertFalse(page.evaluate("matchMedia('(pointer: fine)').matches"))
        page.evaluate("window.scrollTo(0, 500)"); y0 = page.evaluate("window.scrollY")
        page.tap("#ask-fab")                                                              # ONE tap opens
        self.assertTrue(page.locator("#ask-panel").is_visible())
        page.wait_for_timeout(250)
        self.assertNotEqual(page.evaluate("document.activeElement && document.activeElement.id"), "ask-q")   # no auto-focus on touch
        page.tap("#ask-q"); page.wait_for_timeout(250)                                   # ONE tap focuses
        self.assertEqual(page.evaluate("document.activeElement.id"), "ask-q")
        self.assertEqual(page.evaluate("document.body.style.top"), f"-{y0}px")           # the page is frozen where it was
        page.keyboard.type("Is X cheaper?"); page.keyboard.press("Enter")
        self.assertEqual(page.locator(".ask-user").count(), 1)
        page.wait_for_selector(".ask-answer", timeout=15000); page.wait_for_timeout(300)
        self.assertNotEqual(page.evaluate("document.activeElement && document.activeElement.id"), "ask-q")   # keyboard stays down after an answer
        page.tap(".ask-tgl >> nth=0"); self.assertTrue(page.locator(".ask-srcs").first.is_visible())   # ONE tap on a link
        page.tap("#ask-close"); self.assertTrue(page.locator("#ask-panel").is_hidden())              # ONE tap minimizes
        self.assertEqual(page.evaluate("window.scrollY"), y0)                                          # and the page is back where it was
        self.assertEqual(errors, []); ctx.close()

    def test_page_is_frozen_while_open_on_touch_and_restored_on_minimize(self):
        ctx = self.browser.new_context(viewport={"width": 1024, "height": 768}, has_touch=True, is_mobile=True)
        page = ctx.new_page(); page.route("**/*", _serve)
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{ORIGIN}/c/{self.slug}")
        page.evaluate("window.scrollTo(0, 640)"); y0 = page.evaluate("window.scrollY")
        page.tap("#ask-fab"); page.wait_for_timeout(300)
        self.assertFalse(page.evaluate("document.body.classList.contains('ask-lock')"))   # open alone does not freeze (no repaint flicker)
        box = page.locator("#ask-panel").bounding_box()
        self.assertAlmostEqual(box["x"] + box["width"], 1024 - 18, delta=2)              # bottom-right corner
        self.assertAlmostEqual(box["y"] + box["height"], 768 - 18, delta=2)
        page.tap("#ask-q"); page.wait_for_timeout(200)                                   # focus: NOW the page freezes (keyboard time)
        self.assertTrue(page.evaluate("document.body.classList.contains('ask-lock')"))
        self.assertEqual(page.evaluate("getComputedStyle(document.body).position"), "fixed")
        self.assertEqual(page.evaluate("document.body.style.top"), f"-{y0}px")
        page.keyboard.type("hello"); page.wait_for_timeout(300)
        self.assertEqual(page.locator("#ask-panel").bounding_box()["y"], box["y"])       # focus + typing: no move
        page.mouse.wheel(0, 400); page.wait_for_timeout(200)                             # the page cannot scroll under it
        self.assertEqual(page.locator("#ask-panel").bounding_box()["y"], box["y"])
        page.tap("#ask-close"); page.wait_for_timeout(200)
        self.assertFalse(page.evaluate("document.body.classList.contains('ask-lock')"))
        self.assertEqual(page.evaluate("window.scrollY"), y0)                            # exactly where it was
        page.tap("#ask-fab"); page.wait_for_timeout(300)
        self.assertEqual(page.locator("#ask-panel").bounding_box()["y"], box["y"])       # reopen: same corner
        self.assertEqual(errors, []); ctx.close()
