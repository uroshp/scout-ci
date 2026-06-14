"""Review-mode approval: applying a logged proposal to the live card + the proposal email (§17).

The headless monitor in review mode proposes + emails + logs but never touches the card. These tests
pin the two human-facing pieces: review.apply materializes an approved op onto the card (persisting
the my_company anchor it derives from), and the email surfaces where/what/how. Pure, no model/network.

    python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from scout import review, notify
from scout.schema import claim_id, validation_errors, ANCHOR_SECTION

SLUG = "anthropic-vs-openai"
FACT = claim_id(SLUG, "anthropic|fable-5-availability|current")
META = {"competitor": "OpenAI", "my_company": "Anthropic"}

ANCHOR = {"id": FACT, "subject_key": "anthropic|fable-5-availability|current",
          "claim": "Anthropic paused Fable 5 access for all customers.", "claim_type": "fact",
          "section": ANCHOR_SECTION, "zone": None, "order": 0, "as_of": "2026-06-13",
          "source_url": "https://news.test/fable5", "source_tier": "reputable_secondary",
          "evidence_excerpt": "z" * 45, "verified": True, "confidence": "high",
          "grounding": {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-06-13"}}

OP = {"operation": "add", "section": "objection_handling", "zone": None, "valence": "back_foot",
      "target_subject_key": None, "subject_key": "anthropic|fable-5-objection|current",
      "claim": "**\"Is your access stable?\"**\n\nThe net effect is all customers are affected.\n\n**So what:** standardize on Opus 4.8.",
      "claim_type": "interpretation", "derived_from": FACT, "persona": "economic_buyer"}

PLAY = {"id": claim_id(SLUG, "anthropic|live-win|current"), "subject_key": "anthropic|live-win|current",
        "claim": "**Live win**\n\nReal.\n\n**Soundbite:** *\"x\"*", "claim_type": "interpretation",
        "section": "battlecard", "zone": "where_we_win", "order": 0, "source_url": "https://own.test/a",
        "source_tier": "reputable_secondary", "evidence_excerpt": "z" * 45, "as_of": "2026-02-01",
        "verified": True, "confidence": "high", "persona": "technical_evaluator",
        "grounding": {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-02-01"}}


class ReviewApply(unittest.TestCase):
    def test_apply_persists_anchor_and_adds_objection(self):
        with mock.patch.object(review.store, "load_meta", return_value=dict(META)), \
             mock.patch.object(review.store, "load_claims", return_value=[dict(PLAY)]), \
             mock.patch.object(review, "_current_md", return_value=""), \
             mock.patch.object(review.store, "write_baseline") as wb:
            res = review.apply(SLUG, [OP], [ANCHOR], write=True)
        self.assertEqual([a["operation"] for a in res["applied"]], ["add"])
        ids = {c["id"] for c in res["claims"]}
        self.assertIn(FACT, ids)                            # the my_company anchor got persisted
        self.assertIn(claim_id(SLUG, OP["subject_key"]), ids)   # the objection is on the card
        self.assertTrue(all(not validation_errors(c) for c in res["claims"]))
        wb.assert_called_once()

    def test_apply_no_ops_is_a_noop(self):
        res = review.apply(SLUG, [], [], write=True)
        self.assertEqual(res["applied"], [])


class ProposalEmail(unittest.TestCase):
    def test_render_surfaces_where_what_how(self):
        decisions = [{"operation": "add", "section": "objection_handling", "zone": None,
                      "subject_key": "a|b|c", "old_text": None,
                      "new_text": "**\"Q?\"**\n\nThe answer here.\n\n**So what:** pivot.",
                      "judge_verdict": "confirm", "judge_reason": "grounded + pivots",
                      "trigger_source_url": "https://news.test/x"}]
        subject, body = notify.render_propagation_proposals(SLUG, META, decisions)
        self.assertIn("awaiting approval", subject)
        self.assertIn("Anthropic vs OpenAI", subject)
        self.assertIn("ADD in objection_handling", body)    # where + what
        self.assertIn("The answer here", body)              # how it looks (flattened prose)
        self.assertIn("Judge:", body)
        self.assertIn("UNCHANGED", body)                    # makes clear the card didn't move

    def test_send_is_dry_by_default(self):
        out = notify.send_propagation_proposals(SLUG, META, [{"operation": "add", "section": "x",
                                                              "subject_key": "a|b|c", "judge_verdict": "confirm"}])
        self.assertFalse(out["sent"])                       # dry_run default -> never emails in tests


class Dispatch(unittest.TestCase):
    def test_prefers_gmail_and_strips_app_password_spaces(self):
        with mock.patch.object(notify.config, "ALERT_EMAIL_TO", "me@example.com"), \
             mock.patch.object(notify.config, "GMAIL_USER", "me@gmail.com"), \
             mock.patch.object(notify.config, "GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop"), \
             mock.patch("scout.notify.smtplib.SMTP_SSL") as smtp:
            out = notify._dispatch("subj", "body", dry_run=False)
        self.assertTrue(out["sent"])
        self.assertEqual(out["via"], "gmail")
        session = smtp.return_value.__enter__.return_value
        session.login.assert_called_once_with("me@gmail.com", "abcdefghijklmnop")  # spaces stripped
        session.send_message.assert_called_once()

    def test_dry_run_default_never_sends(self):
        out = notify._dispatch("s", "b")
        self.assertFalse(out["sent"])


if __name__ == "__main__":
    unittest.main()
