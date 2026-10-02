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
        self.assertIn('id="how-btn" aria-expanded="false" aria-controls="how"', self.html)
        self.assertIn('<div class="how" id="how" hidden>', self.html)
        self.assertIn("location.hash==='#how'", self.html)          # the direct link opens it

    def test_wording_decided_by_the_author(self):
        self.assertIn("Competitive briefs that stay fresh.", self.html)
        self.assertIn(page._LIVE_LINE, self.html)
        self.assertNotIn("orchestra", self.html)
        for state in ("<b>Publish</b><span>Along with source and date</span>", "<b>Cut</b>", "<b>Hold</b>"):
            self.assertIn(state, self.panel)
        self.assertLess(self.panel.index("Default: <i>Anthropic</i>"), self.panel.index("Evaluated"))
        self.assertLess(self.panel.index("Evaluated"), self.panel.index(">Mistral<"))

    def test_no_cost_no_performance_no_genai_dashes(self):
        text = re.sub(r"<script.*?</script>", "", self.panel, flags=re.S)
        self.assertNotIn("$", text)
        self.assertNotIn("%", text)
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
        self.assertIn(f'<span class="hw-num">{fig["claims"]}</span> total on', self.panel)
        with mock.patch.object(page, "_how_figures", return_value=None):
            html = page.masthead_html()
        self.assertNotIn("Claims:", html)
        self.assertNotIn("figures read live", html)
        self.assertIn('id="how"', html)

    def test_a_broken_panel_never_breaks_the_page(self):
        with mock.patch.object(page, "_how_panel", side_effect=RuntimeError("boom")):
            html = page.masthead_html()
        self.assertIn("Agent Scout", html)
        self.assertNotIn("how-btn", html)
        self.assertNotIn("<script", html)


if __name__ == "__main__":
    unittest.main()
