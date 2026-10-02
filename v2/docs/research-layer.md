# The research layer (2026-10-01)

A perpetual layer on top of the evals: whenever the live judge and the local models disagree on a
verdict, a stronger, different model (the arbiter, Opus 5.5) settles the truth from the facts the
proposer had (web search allowed, capped, only when the facts cannot settle a point), then grades
every side's reasoning, anonymised, on who reasoned best for the tool's goal and why the others
failed, in a fixed failure vocabulary. Uroš reads three things per item (the edit and its decisive
facts, the ruling, the diagnosis) and ratifies or overrules; ratified rulings become labels, unread
ones stay a separate line. Decided with Uroš on 2026-10-01 after a three-item calibration.

- Code: `scout/arbiter.py` (context from the captured judge call, anonymised arms, the call, records,
  ratification), `scripts/run_arbiter.py` (daily caps: per item, $5 and 20 items a day,
  `research/arbiter_state.json`), `scout/research.py` (aggregates), `scripts/research_report.py`.
- Records: `research/arbiter/<YYYY-MM>/<delta_id>.json` in the private store.
- Runs: on the mini after the morning replay (`~/scout-replay/run.sh`, "--- arbiter ---").
- Review: `~/scout-tools/scout-labels` queue 0 (Enter = agree; `overrule confirm|reject <why>`).
- Digest: `~/scout-tools/scout-research`; the check-in brief carries the short form (card 4).

What is tracked (an AI lab's view): variance (how often each arm disagrees with the live model,
per role), outcomes vs the arbiter (right/wrong per side, the live judge included), causes (the
failure modes and how often, per arm), human review of the arbiter (agreed / overruled / pending;
the overrule rate is the arbiter's own error rate), by period (settings drift), by prompt size
(does the big picture get lost in long prompts), cost.

Failure vocabulary (add, never rename): overreach, invented_contrast, missed_materiality,
wrong_section, misread_rule, right_for_wrong_reason, ignored_fact, fidelity_loss.

Independence: the arbiter is a different and stronger model than the live judge (Opus 4.8), it
never sees which side is which, and it never changes a card. The local models receive the live
judge's exact system and user prompt (byte-identical, verified 2026-10-01); what differs is an
enforced JSON schema on their output, their thinking settings, and capacity.
