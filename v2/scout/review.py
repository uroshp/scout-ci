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
from datetime import date

from scout import selfserve, store
from scout.propagate import PROP_DIR, apply_ops
from scout.render import claims_to_markdown, clean_output, extract_cut_log, format_report


def _latest_log(slug: str) -> dict | None:
    """The most recent propagation decision-log payload for a card (None if none / unreadable)."""
    for fn in reversed(sorted(selfserve.list_data(f"{PROP_DIR}/{slug}"))):
        if not fn.endswith(".json"):
            continue
        raw = selfserve.read_data(f"{PROP_DIR}/{slug}/{fn}")
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
    return None


def pending(slug: str) -> dict:
    """Judge-confirmed proposals from the latest logged run awaiting approval, plus the grounding
    facts needed to apply them. {run_ts, confirmed: [...], facts: [...]}."""
    payload = _latest_log(slug) or {}
    confirmed = [d for d in payload.get("decisions", []) if d.get("judge_verdict") == "confirm"]
    return {"run_ts": payload.get("run_ts"), "confirmed": confirmed, "facts": payload.get("facts", [])}


def _title(meta: dict) -> str:
    me, comp = meta.get("my_company"), meta.get("competitor")
    return (f"# Competitive Intelligence Brief: {me} vs {comp}" if me
            else f"# Competitive Intelligence Brief: {comp}")


def apply(slug: str, ops: list, facts: list, write: bool = True) -> dict:
    """Apply judge-confirmed propagation ops to the LIVE card. Persists any my_company tracked_facts
    anchor an op derives from that isn't already on the card (review/shadow never wrote it), applies
    via the deterministic apply_ops, re-renders current.md (carrying the Cut Log forward), and writes
    the baseline. Returns {applied, skipped, claims}. The CALLER commits + pushes.

    `ops`/`facts` come from pending(slug) or straight from the approval email — apply itself is the
    deterministic, model-free step."""
    if not ops:
        return {"applied": [], "skipped": [], "reason": "no ops to apply", "claims": None}
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)

    # Bring on the card any grounding-fact anchor an op needs but the card doesn't have yet
    # (a my_company tracked_facts fact — non-rendered; competitor facts already live in recent_moves).
    by_id = {c.get("id"): c for c in claims}
    facts_by_id = {f.get("id"): f for f in (facts or []) if f.get("id")}
    for fid in {o.get("derived_from") for o in ops}:
        if fid and fid not in by_id and fid in facts_by_id:
            claims = claims + [facts_by_id[fid]]

    today = (pending(slug).get("run_ts") or date.today().isoformat())[:10]
    res = apply_ops(claims, ops, facts or [], slug, today)
    new_claims = res["claims"]

    if write and res["applied"]:
        body = claims_to_markdown(new_claims, _title(meta),
                                  my_company=meta.get("my_company"), competitor=meta.get("competitor"))
        cut_log = extract_cut_log(_current_md(slug))
        if cut_log:
            body = body.rstrip() + "\n\n" + cut_log
        store.write_baseline(slug, new_claims, meta, format_report(clean_output(body)))
    return {"applied": res["applied"], "skipped": res["skipped"], "claims": new_claims}


def approve(slug: str, subject_keys=None, write: bool = True) -> dict:
    """Convenience: read the latest logged proposals and apply them (optionally only the ops whose
    subject_key is in `subject_keys`). Needs store access to read the log; otherwise call apply()
    with ops/facts lifted from the email."""
    p = pending(slug)
    ops = p["confirmed"]
    if subject_keys is not None:
        wanted = {str(s) for s in subject_keys}
        ops = [o for o in ops if str(o.get("subject_key")) in wanted]
    return apply(slug, ops, p["facts"], write=write)


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
