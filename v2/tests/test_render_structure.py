"""The render-structure gate (schema.render_structure_errors): block-section claims must carry the
markers the viewer needs, or they render as a blob. This is the gate that stops a malformed
objection/play from ever being applied or published (see scout/page.py)."""
import unittest

from scout import schema


def _c(section, text, zone=None):
    return {"section": section, "zone": zone, "claim": text}


class RenderStructure(unittest.TestCase):
    def test_objection_without_so_what_rejected(self):
        errs = schema.render_structure_errors(_c("objection_handling", '**"Q?"**\n\nprose only, no marker.'))
        self.assertTrue(any("So what" in e for e in errs))

    def test_objection_with_so_what_passes(self):
        self.assertEqual(
            schema.render_structure_errors(_c("objection_handling", '**"Q?"**\n\nbody.\n\n**So what:** move.')),
            [])

    def test_exec_summary_requires_so_what(self):
        self.assertTrue(schema.render_structure_errors(_c("executive_summary", "no marker here")))

    def test_battlecard_win_requires_soundbite(self):
        self.assertTrue(schema.render_structure_errors(_c("battlecard", "play prose", zone="where_we_win")))
        self.assertEqual(
            schema.render_structure_errors(_c("battlecard", "play.\n\n**Soundbite:** zinger.", zone="where_we_win")),
            [])

    def test_non_block_sections_unaffected(self):
        self.assertEqual(schema.render_structure_errors(_c("recent_moves", "anything goes here")), [])

    def test_gate_wired_into_validation_errors(self):
        # a markerless objection must fail the full validator, not just the helper
        bad = {"section": "objection_handling", "zone": None, "claim": '**"Q?"**\n\nno marker'}
        self.assertTrue(any("So what" in e for e in schema.validation_errors(bad)))


if __name__ == "__main__":
    unittest.main()
