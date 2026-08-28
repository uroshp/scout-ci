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

    def test_apply_confirmed_retire_skips_render_gate(self):
        """A judge-confirmed RETIRE carries no prose (claim=None by contract). It must NOT be pushed
        through the render gate — that demands a **So what:** on empty text and holds the op forever
        (2026-08-28: the first retire ever to reach --approve was held this way). It flips the target
        to retired, keeps its text for lineage, and never calls the reformatter."""
        target = {**PLAY, "section": "executive_summary", "zone": None,
                  "claim": "**Parent is distracted.**\n\nStock down a third.\n\n**So what:** counter."}
        ex = dict(ANCHOR); ex["subject_key"] = "salesforce|q2-beat|current"
        ex["id"] = claim_id(SLUG, ex["subject_key"]); ex["claim"] = "Salesforce beat Q2."
        retire = {"operation": "retire", "section": "executive_summary", "zone": None,
                  "subject_key": PLAY["subject_key"], "target_subject_key": PLAY["subject_key"],
                  "derived_from": ex["id"], "retired_reason": "invalidated: Q2 beat",
                  "feed_note": "Removed the parent-distracted talking point."}
        with mock.patch.object(review.store, "load_meta", return_value=dict(META)), \
             mock.patch.object(review.store, "load_claims", return_value=[target, ex]), \
             mock.patch.object(review, "_current_md", return_value=""), \
             mock.patch.object(review.store, "write_baseline") as wb, \
             mock.patch("scout.reformat.repair_or_hold",
                        side_effect=AssertionError("render gate must not see a retire")):
            res = review.apply(SLUG, [retire], [ex], write=True)
        self.assertEqual([a["operation"] for a in res["applied"]], ["retire"])
        self.assertEqual(res["held"], [])
        flipped = next(c for c in res["claims"] if c["subject_key"] == PLAY["subject_key"])
        self.assertEqual(flipped["status"], "retired")
        self.assertEqual(flipped["derived_from"], ex["id"])
        self.assertIn("Parent is distracted", flipped["claim"])     # lineage text kept
        wb.assert_called_once()

    def test_apply_revise_already_on_card_is_skipped_not_reapplied(self):
        """Re-approving a card must be idempotent: a revise whose text is already the target's text is
        routed to skipped — no updated_on re-stamp, no duplicate feed row (2026-08-28)."""
        revise = {"operation": "revise", "section": "battlecard", "zone": "where_we_win",
                  "subject_key": PLAY["subject_key"], "target_subject_key": PLAY["subject_key"],
                  "claim": PLAY["claim"], "claim_type": "interpretation", "derived_from": ANCHOR["id"],
                  "persona": "technical_evaluator"}
        with mock.patch.object(review.store, "load_meta", return_value=dict(META)), \
             mock.patch.object(review.store, "load_claims", return_value=[dict(PLAY)]), \
             mock.patch.object(review, "_current_md", return_value=""), \
             mock.patch.object(review.store, "write_baseline") as wb:
            res = review.apply(SLUG, [revise], [ANCHOR], write=True)
        self.assertEqual(res["applied"], [])
        self.assertEqual([s["reason"] for s in res["skipped"]], ["already on card (identical text)"])
        wb.assert_not_called()

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

    _HELD = {"operation": "revise", "section": "executive_summary", "zone": None,
             "subject_key": "a|readiness|current", "judge_verdict": "confirm",
             "held_for_format": True,
             "hold_reason": "executive_summary: 201 words exceeds the 170-word render cap — "
                            "condense; auto-repair exhausted",
             "old_text": "**Old lead.**\n\nOld body.\n\n**So what:** old.",
             "new_text": "**New lead.**\n\nNew body kept in FULL for the human.\n\n**So what:** cure me."}

    def test_render_held_section_and_footer(self):
        """A render-gate hold rides the SAME email: counted in the subject, full text + exact
        reason in the body, and the footer gives the literal publish/cure next actions (7/18:
        a held op previously rode the email invisibly and was held silently at approve time)."""
        decisions = [{"operation": "add", "section": "objection_handling", "zone": None,
                      "subject_key": "a|b|c", "old_text": None,
                      "new_text": "**\"Q?\"**\n\nThe answer here.\n\n**So what:** pivot.",
                      "judge_verdict": "confirm", "judge_reason": "grounded"}]
        subject, body = notify.render_propagation_proposals(SLUG, META, decisions, held=[self._HELD])
        self.assertIn("(+1 needs curing)", subject)
        self.assertIn("NEEDS CURING", body)
        self.assertIn("201 words exceeds the 170-word render cap", body)   # the exact why
        self.assertIn("New body kept in FULL for the human.", body)        # never truncated
        self.assertIn("never dropped", body)
        self.assertIn("pending_publish", body)
        self.assertIn(f"scout-proposals --approve {SLUG}", body)           # exact publish command
        self.assertIn('"publish Anthropic vs OpenAI"', body)
        self.assertIn('"cure the executive_summary update on Anthropic vs OpenAI"', body)

    def test_render_held_only_still_sends(self):
        """held-only run: no confirmed items, but the email still goes out and says why."""
        subject, body = notify.render_propagation_proposals(SLUG, META, [], held=[self._HELD])
        self.assertIn("needs curing", subject)
        self.assertIn("HELD needing curing", body)
        out = notify.send_propagation_proposals(SLUG, META, [], held=[self._HELD])
        self.assertNotEqual(out.get("reason"), "no proposals")   # the guard lets held-only through

    def test_pending_splits_held_from_confirmed(self):
        """review.pending() must mirror the email: a held_for_format confirm is NOT approvable
        (approve would re-apply/re-hold it) — it comes back under 'held' instead."""
        log = {"run_ts": "2026-07-18T11:00:00",
               "decisions": [{"judge_verdict": "confirm", "subject_key": "a|clean|c"},
                             {"judge_verdict": "confirm", "subject_key": "a|readiness|current",
                              "held_for_format": True, "hold_reason": "cap"}],
               "facts": []}
        with mock.patch.object(review, "_latest_log", return_value=log):
            p = review.pending(SLUG)
        self.assertEqual([d["subject_key"] for d in p["confirmed"]], ["a|clean|c"])
        self.assertEqual([d["subject_key"] for d in p["held"]], ["a|readiness|current"])

    def test_human_declined_excluded_from_confirmed(self):
        """A confirmed proposal the human declined (human_declined flag) drops out of the
        approvable list, while the judge's verdict stays 'confirm' for the eval corpus."""
        log = {"run_ts": "2026-07-29T11:00:00",
               "decisions": [{"judge_verdict": "confirm", "subject_key": "a|keep|c"},
                             {"judge_verdict": "confirm", "subject_key": "a|dropped|c",
                              "human_declined": True,
                              "declined_reason": "human dropped it"}],
               "facts": []}
        with mock.patch.object(review, "_latest_log", return_value=log):
            p = review.pending(SLUG)
        self.assertEqual([d["subject_key"] for d in p["confirmed"]], ["a|keep|c"])
        # judge verdict on the declined record is preserved (corpus integrity)
        self.assertEqual(log["decisions"][1]["judge_verdict"], "confirm")

    def test_advisory_manual_log_never_becomes_the_approvable_run(self):
        """The 2026-07-25 trap: a manual-source audit log (e.g. manual-supersede-sweep) landed as
        the newest file and --approve applied ITS op instead of the morning run's. _latest_log must
        skip any source beginning 'manual' and fall through to the latest PIPELINE run."""
        import json as _json
        logs = {
            "propagation/s/20260725T110805.json": {"source": "monitor",
                "run_ts": "2026-07-25T11:08:05",
                "decisions": [{"judge_verdict": "confirm", "operation": "revise",
                               "subject_key": "a|flagship|current"}], "facts": []},
            "propagation/s/20260725T223426.json": {"source": "manual-supersede-sweep",
                "run_ts": "2026-07-25T22:34:26",
                "decisions": [{"judge_verdict": "confirm", "operation": "retire",
                               "subject_key": "a|flagship|current"}], "facts": []},
        }
        with mock.patch.object(review.selfserve, "list_data",
                               return_value=[k.split("/")[-1] for k in logs]), \
             mock.patch.object(review.selfserve, "read_data",
                               side_effect=lambda path: _json.dumps(logs[path])):
            p = review.pending("s")
        self.assertEqual(p["run_ts"], "2026-07-25T11:08:05")     # the pipeline run, not the sweep
        self.assertEqual([d["operation"] for d in p["confirmed"]], ["revise"])

    def test_only_advisory_logs_means_nothing_pending(self):
        import json as _json
        log = {"source": "manual-supersede-sweep", "run_ts": "2026-07-25T22:34:26",
               "decisions": [{"judge_verdict": "confirm", "operation": "retire",
                              "subject_key": "a|x|c"}], "facts": []}
        with mock.patch.object(review.selfserve, "list_data",
                               return_value=["20260725T223426.json"]), \
             mock.patch.object(review.selfserve, "read_data",
                               side_effect=lambda path: _json.dumps(log)):
            p = review.pending("s")
        self.assertEqual(p["confirmed"], [])
        self.assertIsNone(p["run_ts"])


class PullContaminated(unittest.TestCase):
    """A mis-grounded/false claim caught after publication is removed from the live card AND recorded
    in the Cut Log — never a silent drop (the retire path can't express it: no killing fact)."""
    _PULL = {"id": "c_pull00000000", "subject_key": "p|a|c", "section": "pricing", "zone": None,
             "claim": "**OpenAI pricing was here.**\n\nfigures the cited excerpt never supported.",
             "claim_type": "fact", "order": 1, "source_url": "https://s/p",
             "source_tier": "reputable_secondary", "evidence_excerpt": "an unrelated snippet",
             "as_of": "2026-01-01", "verified": True, "confidence": "high",
             "grounding": {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-01-01"}}

    def test_pull_removes_claim_and_records_in_cut_log(self):
        written = {}
        with mock.patch.object(review.store, "load_meta", return_value=dict(META)), \
             mock.patch.object(review.store, "load_claims", return_value=[dict(PLAY), dict(self._PULL)]), \
             mock.patch.object(review.store, "write_baseline",
                               side_effect=lambda s, c, m, md: written.update(claims=c, md=md)), \
             mock.patch.object(review, "_current_md",
                               return_value="## Cut Log\n\n**CUT — old item:** a prior cut."):
            res = review.pull_contaminated(SLUG, ["c_pull00000000"],
                                           "mis-grounded: cited excerpt does not support the claim")
        self.assertEqual([p["id"] for p in res["pulled"]], ["c_pull00000000"])
        ids = {c["id"] for c in written["claims"]}
        self.assertNotIn("c_pull00000000", ids)                     # contamination gone from the card
        self.assertIn(PLAY["id"], ids)                              # still-true claim kept
        self.assertIn("does not support the claim", written["md"])  # Cut Log records WHY (not silent)
        self.assertIn("**CUT — old item:**", written["md"])         # prior Cut Log carried forward

    def test_no_match_is_a_noop(self):
        with mock.patch.object(review.store, "load_meta", return_value=dict(META)), \
             mock.patch.object(review.store, "load_claims", return_value=[dict(PLAY)]), \
             mock.patch.object(review.store, "write_baseline") as wb:
            res = review.pull_contaminated(SLUG, ["c_nope00000000"], "x")
        self.assertEqual(res["pulled"], [])
        wb.assert_not_called()


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
