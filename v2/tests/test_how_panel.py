"""The masthead's "How this works" panel (2026-10-02): what a visitor may and may not be shown."""
import re
import unittest
from unittest import mock

from scout import config, page, replaybackends


class HowPanel(unittest.TestCase):
    def setUp(self):
        self.html = page.masthead_html()
        self.panel = self.html[self.html.index('id="how"'):]

    def test_closed_button_and_panel(self):
        self.assertIn('data-how aria-expanded="false" aria-controls="how"', self.html)
        self.assertIn('<div class="how" id="how" hidden>', self.html)
        self.assertIn("location.hash==='#how'", self.html)          # the direct link opens it

    def test_wording_decided_by_the_author(self):
        self.assertIn(page._STATEMENT, self.html)
        self.assertIn(page._SYSTEM_LINE, self.panel)                   # first line of the panel
        self.assertNotIn("livebox", self.html)
        self.assertNotIn("orchestra", self.html)
        for state in ("<b>Publish</b><span>With its source and date</span>", "<b>Cut</b>", "<b>Hold</b>"):
            self.assertIn(state, self.panel)
        self.assertLess(self.panel.index("Default: <i>Anthropic</i>"), self.panel.index("Evaluated"))
        self.assertLess(self.panel.index("Evaluated"), self.panel.index(">Mistral<"))

    def test_no_cost_no_performance_no_genai_dashes(self):
        text = re.sub(r"<script.*?</script>", "", self.panel, flags=re.S)
        self.assertNotIn("$", text)
        self.assertNotIn("%", text.replace("95% interval", ""))        # the method names its interval; no result carries a percent
        self.assertNotIn("—", text)
        self.assertNotRegex(text, r"\d+ of \d+")                    # no scoreboard

    def test_default_lane_reads_config(self):
        with mock.patch.object(config, "FAST_MODEL", "claude-haiku-9-1-20300101"), \
             mock.patch.object(config, "SUBAGENT_MODEL", "claude-sonnet-8"):
            html = page.masthead_html()
        self.assertIn("<span>Haiku 9.1</span>", html)
        self.assertIn("<span>Sonnet 8</span>", html)

    def test_every_challenger_arm_has_an_equal_lane(self):
        vendors = {cfg.get("vendor") or cfg.get("maker") or "" for cfg in replaybackends.OLLAMA_MODELS.values()}
        lanes = dict(page._HOW_CHALLENGERS)
        self.assertEqual(len(lanes), len(replaybackends.LOCAL_BACKENDS))
        for tag_word, company in (("magistral", "Mistral"), ("gemma4", "Google"), ("nemotron", "NVIDIA")):
            self.assertTrue(any(tag_word in str(c) for c in replaybackends.OLLAMA_MODELS.values()), tag_word)
            self.assertIn(company, lanes)
        self.assertIn("Apple", lanes)
        self.assertEqual(self.panel.count('class="hw-track"'), len(lanes))     # one plain band each

    def test_figures_are_live_or_absent(self):
        fig = page._how_figures()
        self.assertIsNotNone(fig)
        self.assertIn(f'<span class="hw-num">{fig["claims"]}</span> on', self.panel)
        with mock.patch.object(page, "_how_figures", return_value=None):
            html = page.masthead_html()
        self.assertNotIn("Claims tracked:", html)
        self.assertNotIn("hw-asof", html)
        self.assertIn('id="how"', html)

    def test_a_broken_panel_never_breaks_the_page(self):
        with mock.patch.object(page, "_how_panel", side_effect=RuntimeError("boom")):
            html = page.masthead_html()
        self.assertIn("Agent Scout", html)
        self.assertNotIn("data-how", html)
        self.assertNotIn("<script", html)


if __name__ == "__main__":
    unittest.main()


class TopOfPage(unittest.TestCase):
    """The app bar, the strip and the header (2026-10-02)."""
    def test_brief_parts_normalise_the_area(self):
        self.assertEqual(page.brief_parts({"competitor": "A", "my_company": "B", "focus": "None"})[2], "General")
        self.assertEqual(page.brief_parts({"focus": "enterprise coding/developers"})[2], "Enterprise coding and developers")
        self.assertEqual(page.brief_parts({"focus": "AI/ML infrastructure"})[2], "AI and ML infrastructure")

    def test_header_has_one_size_and_no_vs(self):
        h = page._title_block({"competitor": "OpenAI", "my_company": "Anthropic", "focus": "x"}, print_href="/print/s")
        self.assertIn('Competitive Brief: </span><span class="co">OpenAI</span> for Anthropic sales reps', h)
        self.assertIn("Area:", h); self.assertIn("Print call sheet", h); self.assertNotIn(" vs ", h)

    def test_strip_only_with_a_brief_and_tabs_carry_area_and_tooltip(self):
        from scout import display
        cards = display.list_battlecards()
        self.assertNotIn("sc-strip", page.masthead_html(cards, None))
        h = page.masthead_html(cards, cards[0])
        self.assertIn('class="sc-tab on"', h)
        self.assertEqual(len(re.findall(r'class="sc-tab( on)?" href', h)), len(cards))
        self.assertIn('title="', h); self.assertIn('class="ar"', h)
        self.assertIn(f'<span class="sc-cnt">{len(cards)}</span>', h)


class PhoneBar(unittest.TestCase):
    def test_contents_and_audience_bar_is_rendered_once_above_the_columns(self):
        from scout import display
        slug = display.list_battlecards()[0]
        h = page.content_html(slug)
        self.assertEqual(h.count('class="sc-obar"'), 1)
        self.assertLess(h.index('class="sc-obar"'), h.index('class="cols"'))
        self.assertIn('<details class="ob"><summary>Contents', h)
        self.assertIn('class="toc" id="toc"', h)                           # the rail keeps its own


class AudienceMode(unittest.TestCase):
    """Level 1 (2026-10-02 evening): the buyer's items lead, other buyers' fold, facts never hide."""
    def setUp(self):
        from scout import display
        self.slug = "anthropic__vs__openai__enterprise-coding-developers"
        self.assertIn(self.slug, display.list_battlecards())

    def test_no_audience_shows_everything_unfolded(self):
        h = page.content_html(self.slug)
        self.assertNotIn('class="fold"', h)

    def test_audience_folds_other_buyers_and_reorders_sections(self):
        h = page.content_html(self.slug, persona="economic_buyer")
        self.assertIn("for other audiences", h)
        self.assertLess(h.index('id="pricing"'), h.index('id="bc"'))          # pricing moves up
        base = page.content_html(self.slug)
        self.assertLess(base.index('id="positioning"'), base.index('id="pricing"'))
        # facts stay complete: the same snapshot and moves counts
        import re
        for sid in ("snapshot", "recent_moves"):
            a = re.search(rf'id="{sid}"[^>]*>.*?<span class="scount">([^<]+)', base, re.S).group(1)
            b = re.search(rf'id="{sid}"[^>]*>.*?<span class="scount">([^<]+)', h, re.S).group(1)
            self.assertEqual(a, b, sid)

    def test_header_chip_and_print_link_carry_the_audience(self):
        h = page.title_html(self.slug, "security_regulated")
        self.assertIn("Audience:", h); self.assertIn("Security &amp; regulated", h)
        self.assertIn(f"/print/{self.slug}?persona=security_regulated", h)
        self.assertNotIn("Audience:", page.title_html(self.slug))

    def test_fold_grammar(self):
        self.assertIn("1 more play for other audiences", page._fold(["x"], "plays"))
        self.assertIn("2 more plays for other audiences", page._fold(["x", "y"], "plays"))


class AudienceFocus(unittest.TestCase):
    """2026-10-02 night: with an audience, the briefing shows only that buyer's plays and pulls their
    objections up; sections holding nothing for them render closed."""
    slug = "anthropic__vs__openai__enterprise-coding-developers"

    def test_only_the_buyers_plays_lead_and_their_objections_follow(self):
        import re
        h = page.content_html(self.slug, persona="economic_buyer")
        plays = h[h.find('id="brief2"'):h.find("Objections they raise")]
        self.assertNotIn("Exec / top-down", plays); self.assertNotIn("Security", plays)   # no other audience in the top plays
        self.assertIn("Objections they raise", h[:h.find('class="divider"')])
        self.assertGreaterEqual(h.count('class="aud-obj"'), 1)
        self.assertIn("Raised by", h[h.find("Objections they raise"):h.find('class="divider"')])   # tagged

    def test_sections_without_the_buyers_material_are_closed(self):
        h = page.content_html(self.slug, persona="economic_buyer")
        self.assertIn('<details class="sec folded" id="snapshot">', h)
        self.assertIn('<details class="sec" id="bc" open>', h)
        self.assertIn('<details class="sec folded" id="objection_handling">', h)   # pulled up, so closed below
        self.assertIn('id="pricing"', h); self.assertNotIn('class="sec folded" id="pricing"', h)   # the economic buyer reads pricing
        self.assertNotIn("sec folded", page.content_html(self.slug))

    def test_buyer_with_no_plays_gets_an_honest_line(self):
        h = page.content_html(self.slug, persona="eng_led")
        self.assertTrue(("No plays written for the eng-led champion" in h) or ("PLAY 01" in h))


class NoRepeats(unittest.TestCase):
    def test_pulled_up_plays_and_objections_do_not_appear_below(self):
        import re
        h = page.content_html("anthropic__vs__openai__enterprise-coding-developers", persona="economic_buyer")
        top, rest = h[:h.find('class="divider"')], h[h.find('class="divider"'):]
        titles = re.findall(r'<div class="play">.*?<h4>(.*?)</h4>', top, re.S)
        self.assertTrue(titles)
        for t in titles:
            self.assertNotIn(t, rest)                       # a play shown at the top is not in the battlecard below
        objs = re.findall(r'<div class="aud-obj">.*?<h4>(.*?)</h4>', top, re.S)
        for t in objs:
            self.assertNotIn(t, rest)
        base = page.content_html("anthropic__vs__openai__enterprise-coding-developers")
        self.assertIn(titles[0], base[base.find('class="divider"'):])   # without an audience the battlecard is complete
