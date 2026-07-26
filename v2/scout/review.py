"""In-session approval of REVIEW-mode propagation proposals (spec §17).

The headless monitor (SCOUT_PROPAGATE_MODE=review) proposes -> the judge confirms -> it EMAILS the
human and LOGS the proposal with its grounding fact to the private store, but NEVER touches the card.
The human approves a proposal out-of-band; this applies the approved op to the LIVE card store
(claims.json + current.md), reusing the deterministic apply_ops. Commit/push is the caller's.

Two ways to source the op being approved:
  - pending(slug) reads the latest logged proposals from the private store (needs the SELFSERVE
    creds in the env, same as the monitor Action), or
  - apply(slug, ops, facts) takes the op(s) + grounding fact(s) explicitly (e.g. lifted from the
    approval email), so approval never hard-depends on local store access.
"""
import json
import re
import sys
from datetime import date

from scout import selfserve, store
from scout.propagate import PROP_DIR, apply_ops
from scout.render import claims_to_markdown, clean_output, extract_cut_log, format_report


def _latest_log(slug: str) -> dict | None:
    """The most recent APPROVABLE propagation decision-log payload for a card (None if none /
    unreadable). ADVISORY-RUN GUARD (2026-07-25): a log whose source starts with 'manual' is an
    audit record of an out-of-band pass (e.g. 'manual-supersede-sweep'), NOT an approvable run —
    the trap: such a log became 'latest' and scout-proposals --approve applied its already-mooted
    retire instead of the morning run's revises. Advisory logs are skipped here (loudly), so the
    approve surface always shows the latest PIPELINE run."""
    for fn in reversed(sorted(selfserve.list_data(f"{PROP_DIR}/{slug}"))):
        if not fn.endswith(".json"):
            continue
        raw = selfserve.read_data(f"{PROP_DIR}/{slug}/{fn}")
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(payload.get("source", "")).startswith("manual"):
                print(f"[review] skipping advisory log {fn} (source="
                      f"{payload.get('source')!r} — audit record, not approvable)", file=sys.stderr)
                continue
            return payload
    return None


def pending(slug: str) -> dict:
    """Judge-confirmed proposals from the latest logged run awaiting approval, plus the grounding
    facts needed to apply them. `unjudged` are drafts the judge never ruled on (judge_unavailable —
    an outage took out the primary AND fallback judge): applying one requires the explicit
    allow_unjudged flag, because the human doing so IS the judge. `held` are confirmed ops the
    pre-email render gate could not repair (held_for_format, 2026-07-18) — they live in
    pending_publish awaiting a cure and are EXCLUDED from confirmed so approve can't re-apply them.
    {run_ts, confirmed: [...], unjudged: [...], held: [...], facts: [...]}."""
    payload = _latest_log(slug) or {}
    decisions = payload.get("decisions", [])
    confirmed = [d for d in decisions if d.get("judge_verdict") == "confirm"
                 and not d.get("held_for_format")]
    unjudged = [d for d in decisions if d.get("judge_verdict") == "judge_unavailable"]
    held = [d for d in decisions if d.get("held_for_format")]
    return {"run_ts": payload.get("run_ts"), "confirmed": confirmed, "unjudged": unjudged,
            "held": held, "facts": payload.get("facts", [])}


def _title(meta: dict) -> str:
    me, comp = meta.get("my_company"), meta.get("competitor")
    return (f"# Competitive Intelligence Brief: {me} vs {comp}" if me
            else f"# Competitive Intelligence Brief: {comp}")


def _decision_to_op(d: dict) -> dict:
    """Map a LOGGED propagation decision (subject_key/new_text, as written by propagate.log_decisions)
    to the apply-able op shape apply_ops expects (target_subject_key/claim). Idempotent: an op already
    in apply shape (carries 'claim', no 'new_text') is returned unchanged. Without this, a logged
    REVISE skips ('target not active') because its target_subject_key is never set."""
    if d.get("claim") is not None or "new_text" not in d:
        return d                                          # already apply-shaped (e.g. lifted from email)
    op = {"operation": d.get("operation"), "section": d.get("section"), "zone": d.get("zone"),
          "subject_key": d.get("subject_key"), "derived_from": d.get("derived_from"),
          "feed_note": d.get("feed_note"),                # the one-line updates-panel entry

          "change_kind": d.get("change_kind"), "feed_note": d.get("feed_note"),
          "claim_type": "interpretation"}
    if d.get("operation") in ("revise", "retire"):
        op["target_subject_key"] = d.get("subject_key")   # revise/retire act in place on this subject_key
    if d.get("operation") == "retire":
        op["retired_reason"] = d.get("retired_reason")
    else:
        op["claim"] = d.get("new_text")
    if d.get("persona"):
        op["persona"] = d["persona"]
    return op


def apply(slug: str, ops: list, facts: list, write: bool = True) -> dict:
    """Apply judge-confirmed propagation ops to the LIVE card. Persists any my_company tracked_facts
    anchor an op derives from that isn't already on the card (review/shadow never wrote it), applies
    via the deterministic apply_ops, re-renders current.md (carrying the Cut Log forward), and writes
    the baseline. Returns {applied, skipped, claims}. The CALLER commits + pushes.

    `ops`/`facts` come from pending(slug) or straight from the approval email. Each op is normalized
    through _decision_to_op so a logged decision (subject_key/new_text) applies the same as an
    apply-shaped op — apply itself is the deterministic, model-free step."""
    ops = [_decision_to_op(o) for o in (ops or [])]
    if not ops:
        return {"applied": [], "skipped": [], "held": [], "reason": "no ops to apply", "claims": None}

    # NO-DROP GUARANTEE (decision-log §11): a judge-confirmed op can never be cut or silently dropped
    # for a formatting problem. Each op is repaired (a model adds the required **So what:**/
    # **Soundbite:** block, substance unchanged) or, if unrepairable, HELD + flagged — never applied
    # malformed, never sent to the Cut Log. Only well-formed ops reach apply_ops below.
    from scout import reformat
    ready, held = [], []
    for o in ops:
        status, o2 = reformat.repair_or_hold(slug, o)
        (held if status == "held" else ready).append(o2)
    ops = ready
    if not ops:
        return {"applied": [], "skipped": [], "held": held, "reason": "all ops held pending format",
                "claims": None}
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)

    # Bring on the card any grounding-fact anchor an op needs but the card doesn't have yet
    # (a my_company tracked_facts fact — non-rendered; competitor facts already live in recent_moves).
    by_id = {c.get("id"): c for c in claims}
    facts_by_id = {f.get("id"): f for f in (facts or []) if f.get("id")}
    for fid in {o.get("derived_from") for o in ops}:
        if fid and fid not in by_id and fid in facts_by_id:
            claims = claims + [facts_by_id[fid]]

    # Stamp updated_on with the APPLY date, not the proposal run's date — the "Recently updated"
    # panel and the last-updated ordering must reflect when the card actually changed.
    today = date.today().isoformat()
    res = apply_ops(claims, ops, facts or [], slug, today)
    new_claims = res["claims"]

    if write and res["applied"]:
        body = claims_to_markdown(new_claims, _title(meta),
                                  my_company=meta.get("my_company"), competitor=meta.get("competitor"))
        cut_log = extract_cut_log(_current_md(slug))
        if cut_log:
            body = body.rstrip() + "\n\n" + cut_log
        store.write_baseline(slug, new_claims, meta, format_report(clean_output(body)))
        # SURFACE EVERY APPLIED OP in the updates panel (2026-07-02: approved revises were invisible
        # — no feed row, no viewer reorder). Each op's feed_note lands as a feed entry, so a card
        # change is never silent and the last-updated ordering keys see it.
        try:
            from scout import monitor
            feed_alerts = monitor._applied_feed_alerts(ops, res["applied"])
            if feed_alerts:
                monitor._append_alerts(slug, feed_alerts)
        except Exception as e:
            print(f"[review] feed-note skipped ({type(e).__name__}: {e})")
        # Close the adjudication leak: approving a proposal means the human judged the authorship
        # judge RIGHT to confirm it. Record that 'agree' (this used to be lost — apply wrote the card
        # but never labeled the judge), feeding the §17 promotion gate. Best-effort, never blocks.
        applied_keys = {(a.get("subject_key"), a.get("operation")) for a in res["applied"]}
        _log_human_verdict(slug, [o for o in ops if (o.get("subject_key"), o.get("operation")) in applied_keys],
                           "agree", "auto-logged by review.apply on approval")
    return {"applied": res["applied"], "skipped": res["skipped"], "held": held, "claims": new_claims}


def _log_human_verdict(slug: str, ops: list, human_verdict: str, note: str = "") -> int:
    """Best-effort: append the human's adjudication of the authorship judge (was it right to confirm
    these ops?) to adjudication/authorship_labels.jsonl, matching each op to its captured propagation
    decision via the canonical delta_id. Fully wrapped per the non-disruption contract — it can only
    ever WARN, never raise into the approval path. Returns how many labels it wrote."""
    n = 0
    try:
        from scout import adjudicate
        want = {(o.get("subject_key"), o.get("operation"), o.get("derived_from")) for o in ops}
        for d in adjudicate.load_deltas():
            if d.get("slug") != slug or d.get("judge_verdict") not in ("confirm", "reject"):
                continue
            if (d.get("subject_key"), d.get("operation"), d.get("derived_from")) in want:
                adjudicate.label(d["delta_id"], human_verdict, note)
                n += 1
    except Exception as e:
        print(f"[review] verdict-log skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return n


def reject(slug: str, subject_keys, note: str = "") -> int:
    """Record that the human DECLINED judge-confirmed proposal(s) for these subject_keys (the judge
    was WRONG to confirm). A decline leaves no git commit, so without this the disagreement signal is
    lost. Resolves the ops from the latest logged proposals and labels them 'disagree'. Returns the
    count. (The approve path auto-logs 'agree'; this is its explicit counterpart for rejections.)"""
    p = pending(slug)
    wanted = {str(s) for s in subject_keys}
    ops = [o for o in p["confirmed"] if str(o.get("subject_key")) in wanted]
    return _log_human_verdict(slug, ops, "disagree", note or "human declined the proposal")


def approve(slug: str, subject_keys=None, write: bool = True, allow_unjudged: bool = False) -> dict:
    """Convenience: read the latest logged proposals and apply them (optionally only the ops whose
    subject_key is in `subject_keys`). Needs store access to read the log; otherwise call apply()
    with ops/facts lifted from the email.

    allow_unjudged=True also applies judge_unavailable drafts (the judge never ruled — outage): the
    human approving IS the judge then. No agree/disagree adjudication label is written for those
    (there is no judge decision to grade; _log_human_verdict's confirm/reject filter enforces it)."""
    p = pending(slug)
    ops = p["confirmed"] + (p["unjudged"] if allow_unjudged else [])
    if subject_keys is not None:
        wanted = {str(s) for s in subject_keys}
        ops = [o for o in ops if str(o.get("subject_key")) in wanted]
    return apply(slug, ops, p["facts"], write=write)


def _cut_label(c: dict) -> str:
    """A short human label for a pulled claim — its bold headline, else its first line, trimmed."""
    t = str(c.get("claim", "")).strip()
    m = re.match(r"\*\*(.+?)\*\*", t)
    return (m.group(1).strip().strip('"') if m else t.split("\n")[0])[:90]


def pull_contaminated(slug: str, claim_ids, reason: str, write: bool = True) -> dict:
    """Immediately remove mis-grounded / false claims from a LIVE card and RECORD each in the Cut Log
    (transparent, never a silent drop). This is the contamination path for a claim caught AFTER
    publication (a bad-grounding leak), which `retire` can't express because retirement requires a
    killing fact (derived_from). Deterministic, model-free; re-renders + writes the baseline; the
    CALLER commits + pushes. `reason` is a short why, appended to every Cut Log entry. Returns
    {pulled, claims}."""
    want = {str(x) for x in (claim_ids or [])}
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)
    pulled = [c for c in claims if str(c.get("id")) in want]
    if not pulled:
        return {"pulled": [], "reason": "no matching claims on the card", "claims": claims}
    remaining = [c for c in claims if str(c.get("id")) not in want]
    if write:
        body = claims_to_markdown(remaining, _title(meta),
                                  my_company=meta.get("my_company"), competitor=meta.get("competitor"))
        cut_log = extract_cut_log(_current_md(slug))
        entries = "".join(f"\n\n**CUT — {_cut_label(c)}:** removed as contamination. {reason}"
                          for c in pulled)
        combined = (cut_log.rstrip() + entries) if cut_log else ("## Cut Log" + entries)
        store.write_baseline(slug, remaining, meta,
                             format_report(clean_output(body.rstrip() + "\n\n" + combined)))
    return {"pulled": [{"id": c.get("id"), "subject_key": c.get("subject_key")} for c in pulled],
            "claims": remaining}


def _current_md(slug: str) -> str:
    import os
    path = os.path.join(store.battlecard_dir(slug), "current.md")
    return open(path).read() if os.path.exists(path) else ""


if __name__ == "__main__":
    import sys
    if not sys.argv[1:]:
        print("usage: python -m scout.review <slug>   # show proposals awaiting approval")
        sys.exit(1)
    p = pending(sys.argv[1])
    print(f"run_ts: {p['run_ts']}   confirmed proposals: {len(p['confirmed'])}")
    for d in p["confirmed"]:
        print(f"  {str(d.get('operation','?')).upper()} {d.get('section')}  ({d.get('subject_key')})")
        if d.get("new_text"):
            print(f"     NEW: {str(d['new_text'])[:200]}")
