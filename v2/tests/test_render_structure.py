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


if __name__ == "__main__":
    unittest.main()
