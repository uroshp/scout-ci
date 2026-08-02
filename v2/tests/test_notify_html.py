"""Rich HTML emails (2026-07-31): the plain-text WHAT-CHANGED diff was unreadable, so the owner's
emails now carry a scannable HTML alternative (additions bold-green highlighted, removals struck
red) with the plain text as fallback. These tests pin the diff rendering, HTML-escaping (email
bodies embed fact text + URLs, so XSS-escaping is load-bearing), and that send_* pass html to
_dispatch. All dry-run — no network. Run from v2/:

    python -m unittest discover -s tests
"""
import unittest

from scout import notify

META = {"my_company": "Anthropic", "competitor": "OpenAI"}
_GREEN = "#1a7f37"      # additions
_RED = "#b0301c"        # removals


class DiffHtml(unittest.TestCase):
    def test_insert_is_bold_green(self):
        h = notify._diff_html("Opus at $5", "Opus 5 at $5")
        self.assertIn(_GREEN, h)
        self.assertIn("font-weight:700", h)
        self.assertIn("5", h)

    def test_delete_is_struck_red_in_parentheses(self):
        h = notify._diff_html("Opus 4.8 at $5", "Opus at $5")
        self.assertIn(_RED, h)
        self.assertIn("line-through", h)
        self.assertIn("(<span", h)          # removals wrapped in parentheses
        self.assertIn("</span>)", h)

    def test_unchanged_words_are_plain(self):
        h = notify._diff_html("Opus 4.8 at $5", "Opus 5 at $5")
        # 'Opus', 'at', '$5' unchanged -> not inside any colored span
        self.assertNotIn(f'<span style="{notify._C_ADD}">Opus', h)

    def test_replace_shows_old_then_new(self):
        h = notify._diff_html("cost 4.8", "cost 5")
        self.assertIn(_RED, h)          # 4.8 struck
        self.assertIn(_GREEN, h)        # 5 added

    def test_pure_addition_when_no_old(self):
        h = notify._diff_html(None, "brand new claim")
        self.assertIn(_GREEN, h)
        self.assertIn("brand new claim", h)

    def test_html_escaped(self):
        h = notify._diff_html("a", "a <script>x</script> & b")
        self.assertNotIn("<script>x", h)
        self.assertIn("&lt;script&gt;", h)


class ProposalsHtml(unittest.TestCase):
    D = [{"operation": "revise", "section": "pricing", "zone": None, "change_kind": "update",
          "subject_key": "anthropic | api-list-price | current",
          "old_text": "Opus 4.8 at $5/$25.", "new_text": "Opus 5 at $5/$25, same price.",
          "judge_reason": "clean reconcile", "trigger_source_url": "https://a/x"}]

    def test_revise_shows_tracked_changes_paragraph(self):
        h = notify.render_propagation_proposals_html("slug", META, self.D)
        self.assertIn("Edited paragraph", h)
        self.assertIn(_GREEN, h)                     # additions green
        self.assertIn("line-through", h)             # removals struck
        self.assertNotIn("<details", h)              # no redundant collapsed full-text block
        self.assertIn("api-list-price", h)           # subject_key shown

    def test_add_is_shown_plainly_not_all_green(self):
        add = [{"operation": "add", "section": "battlecard", "zone": "where_we_win",
                "subject_key": "x | y | z", "old_text": None,
                "new_text": "A brand new play with several words."}]
        h = notify.render_propagation_proposals_html("slug", META, add)
        self.assertIn("New (all content is added)", h)
        self.assertNotIn(_GREEN, h)                  # an add is NOT rendered as a green diff
        self.assertIn("A brand new play", h)

    def test_feed_note_and_judge_are_own_readable_blocks(self):
        d = [dict(self.D[0], feed_note="the feed note text")]
        h = notify.render_propagation_proposals_html("slug", META, d)
        self.assertIn('<div style="font-weight:600">Feed note</div>', h)
        self.assertIn('<div style="font-weight:600">Judge</div>', h)
        self.assertNotIn("font-size:13px;margin-top:6px\">Feed note:", h)   # not the old tiny-grey inline

    def test_exhausted_and_held_are_red_amber_callouts(self):
        h = notify.render_propagation_proposals_html(
            "slug", META, [],
            exhausted=[{"operation": "add", "section": "obj", "subject_key": "a|b|c",
                        "attempts": [{"reason": "invented", "claim": "bad"}]}],
            held=[{"operation": "add", "section": "obj", "subject_key": "d|e|f",
                   "hold_reason": "over cap", "new_text": "held text"}])
        self.assertIn("AUTHORING FAILED", h)
        self.assertIn("NEEDS CURING", h)
        self.assertIn(_RED, h)

    def test_body_text_is_escaped(self):
        d = [{"operation": "add", "section": "x", "subject_key": "a|b|c",
              "new_text": "<script>evil</script> & co"}]
        h = notify.render_propagation_proposals_html("s", META, d)
        self.assertNotIn("<script>evil", h)
        self.assertIn("&lt;script&gt;", h)


class DigestAndUrgentHtml(unittest.TestCase):
    def test_digest_shows_old_to_new_delta(self):
        h = notify.render_digest_html(META, [{"severity": "act", "headline": "Opus 5 shipped",
                                              "old_value": "Opus 4.8", "new_value": "Opus 5",
                                              "so_what": "update refs", "source_url": "https://x"}])
        self.assertIn(_RED, h)          # old struck
        self.assertIn(_GREEN, h)        # new highlighted
        self.assertIn("Opus 5 shipped", h)

    def test_urgent_leads_with_judge_diagnosis(self):
        h = notify.render_urgent_material_html("s", META, [{"operation": "add", "section": "obj",
            "subject_key": "o|c|current", "cure": "none",
            "judge_reason": "the material point and the correct grounded approach",
            "attempts": [{"claim": "bad draft"}]}])
        self.assertIn("diagnosis", h.lower())
        self.assertIn("correct grounded approach", h)
        self.assertIn(_RED, h)


class DispatchHtml(unittest.TestCase):
    def test_dry_run_preview_carries_html(self):
        out = notify._dispatch("subj", "plain body", dry_run=True, html="<b>rich</b>")
        self.assertFalse(out["sent"])
        self.assertEqual(out["preview"], "plain body")   # text fallback preserved
        self.assertEqual(out["html"], "<b>rich</b>")

    def test_html_is_optional_backcompat(self):
        out = notify._dispatch("subj", "plain only", dry_run=True)
        self.assertIsNone(out["html"])

    def test_sends_pass_html_through(self):
        p = notify.send_propagation_proposals("s", META, ProposalsHtml.D, dry_run=True)
        self.assertIn(_GREEN, p["html"])                 # the rich diff reached dispatch
        d = notify.send_digest(META, [{"severity": "act", "headline": "h", "old_value": "a",
                                       "new_value": "b"}], dry_run=True)
        self.assertIn(_GREEN, d["html"])


if __name__ == "__main__":
    unittest.main()
