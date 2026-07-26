"""The render-structure gate (schema.render_structure_errors): block-section claims must carry the
markers the viewer needs, or they render as a blob. This is the gate that stops a malformed
objection/play from ever being applied or published (see scout/page.py)."""
import unittest

from scout import schema


def _c(section, text, zone=None, persona="economic_buyer"):
    return {"section": section, "zone": zone, "claim": text, "persona": persona}


class RenderStructure(unittest.TestCase):
    def test_objection_without_so_what_rejected(self):
        errs = schema.render_structure_errors(_c("objection_handling", '**"Q?"**\n\nprose only, no marker.'))
        self.assertTrue(any("So what" in e for e in errs))

    def test_objection_with_so_what_passes(self):
        self.assertEqual(
            schema.render_structure_errors(_c("objection_handling", '**"Q?"**\n\nbody.\n\n**So what:** move.')),
            [])

    def test_exec_summary_requires_so_what(self):
        # exec summary always carries a So-what (38/38 in data; renderer renders the box) -> gated
        self.assertTrue(schema.render_structure_errors(_c("executive_summary", "verdict prose, no marker")))
        self.assertEqual(
            schema.render_structure_errors(_c("executive_summary", "verdict.\n\n**So what:** the decision.")), [])

    def test_strategic_pricing_recentmoves_contested_not_forced(self):
        # these legitimately have no response block — never force one (recent_moves = "Recent Strategic Moves")
        for section in ("recent_moves", "pricing", "packaging", "snapshot", "positioning", "sentiment", "tracked_facts"):
            self.assertEqual(schema.render_structure_errors(_c(section, "prose with no marker")), [],
                             f"{section} must not be forced to carry a marker")
        # battlecard CONTESTED is a neutral framing, not a win/lose play -> not forced
        self.assertEqual(schema.render_structure_errors(_c("battlecard", "no soundbite", zone="contested")), [])

    def test_battlecard_win_requires_soundbite(self):
        self.assertTrue(schema.render_structure_errors(_c("battlecard", "play prose", zone="where_we_win")))
        self.assertEqual(
            schema.render_structure_errors(_c("battlecard", "play.\n\n**Soundbite:** zinger.", zone="where_we_win")),
            [])

    def test_non_block_sections_unaffected(self):
        self.assertEqual(schema.render_structure_errors(_c("recent_moves", "anything goes here")), [])

    def test_objection_and_play_require_persona(self):
        # the "Raised by"/"Best for" buyer badge needs a persona — required for objections + win/lose plays
        obj = {"section": "objection_handling", "zone": None, "claim": '**"Q?"**\n\nbody.\n\n**So what:** move.'}
        self.assertTrue(any("persona" in e for e in schema.render_structure_errors(obj)))
        play = {"section": "battlecard", "zone": "where_we_win", "claim": 'play.\n\n**Soundbite:** z.'}
        self.assertTrue(any("persona" in e for e in schema.render_structure_errors(play)))

    def test_persona_not_required_where_no_badge(self):
        # exec summary needs a So-what but has no buyer badge; contested has neither
        exec_ok = {"section": "executive_summary", "zone": None, "claim": 'verdict.\n\n**So what:** d.'}
        self.assertEqual(schema.render_structure_errors(exec_ok), [])
        contested_ok = {"section": "battlecard", "zone": "contested", "claim": "neutral framing"}
        self.assertEqual(schema.render_structure_errors(contested_ok), [])

    def test_gate_wired_into_validation_errors(self):
        # a markerless objection must fail the full validator, not just the helper
        bad = {"section": "objection_handling", "zone": None, "claim": '**"Q?"**\n\nno marker'}
        self.assertTrue(any("So what" in e for e in schema.validation_errors(bad)))


class WordCap(unittest.TestCase):
    """The deterministic 170-word render cap (2026-07-25: first unit coverage — the rule shipped
    2026-07-02 untested) and its public factor-out word_cap_errors, which the propagation length
    floor and the condense repair trigger both key off. The two must agree byte-for-byte."""

    def _long(self, n):
        return "**H**\n\n" + ("w " * (n - 4)) + "\n\n**So what:** m."   # n words total

    def test_over_cap_rejected_with_exact_error(self):
        text = self._long(schema.RENDER_MAX_WORDS + 1)
        errs = schema.render_structure_errors(_c("executive_summary", text))
        self.assertEqual(len(errs), 1)
        self.assertIn(f"{schema.RENDER_MAX_WORDS + 1} words exceeds the "
                      f"{schema.RENDER_MAX_WORDS}-word render cap", errs[0])

    def test_at_cap_passes(self):
        text = self._long(schema.RENDER_MAX_WORDS)
        self.assertEqual(len(text.split()), schema.RENDER_MAX_WORDS)
        self.assertEqual(schema.render_structure_errors(_c("executive_summary", text)), [])

    def test_non_capped_sections_exempt(self):
        long = "w " * (schema.RENDER_MAX_WORDS * 2)
        for section in ("recent_moves", "pricing", "snapshot", "positioning", "sentiment"):
            self.assertEqual(schema.word_cap_errors(_c(section, long)), [],
                             f"{section} carries no cap")

    def test_word_cap_errors_is_the_exact_length_subset(self):
        over = _c("objection_handling", "w " * (schema.RENDER_MAX_WORDS + 30))  # no marker either
        full = schema.render_structure_errors(over)
        cap = schema.word_cap_errors(over)
        self.assertEqual(len(cap), 1)
        self.assertEqual([e for e in full if "render cap" in e], cap)


if __name__ == "__main__":
    unittest.main()
