"""Shadow-mode capture for v3.5 challenger qualification (docs/vnext-roadmap.md §v3.5).

PURE OBSERVER. On each REAL generation/monitor run, it records the production CHAMPION
decisions — the deterministic code grader's grounding outcomes plus the verifier's cut log —
so an OFFLINE challenger (the calibrated model-judge) can later be scored against them WITHOUT
re-running or re-paying for anything. The expensive work (generation + grounding fetches) has
already happened; this only logs its byproduct.

NON-DISRUPTION CONTRACT — this module must NEVER alter or break a production run:
  * gated behind config.SHADOW_EVAL_ENABLED (SCOUT_SHADOW_EVAL=1); a no-op otherwise.
  * called only on REAL (write=True) runs by its callers, never on dry/measurement runs.
  * the whole capture body is wrapped so it can only ever WARN, never raise into the caller.
  * writes to a SEPARATE store (shadow/<slug>/) in the PRIVATE data repo via selfserve's
    backend-aware writer — never the public battlecards/ or the user-facing card.
  * read-only over already-computed data: triggers no model call, no fetch, no claim write.

The record is the CHAMPION side only. The challenger runs in a separate scheduled Action over
these records (shadow-eval.yml, not yet built), so a failure there can't touch live v2.
"""
import json
import sys
from datetime import datetime

from scout import config, selfserve

SCHEMA_VERSION = 1
SHADOW_DIR = "shadow"
FILTER_DIR = "filter"       # consequentiality-filter verdicts (the gate's decisions), for longitudinal eval
DISMISSAL_DIR = "dismissals" # what a run SURFACED but did NOT alert on (triage candidates + materiality
                             # immaterial verdicts + own-company signals) — the dismissal stream the
                             # eval's "never drop anything important" bar needs auditable


def _ground_rows(grounding):
    """Per-claim grounding instrumentation, incl. best_ratio — the 0.80-0.92 band that flags
    true-but-cut claims (the recovery target). Tolerates GroundingResult dataclasses, plain
    dicts, or junk; a bad row degrades to Nones rather than failing the whole capture."""
    rows = []
    for r in (grounding or {}).get("results", []) or []:
        get = r.get if isinstance(r, dict) else (lambda k: getattr(r, k, None))
        rows.append({
            "claim_id": get("claim_id"),
            "subject_key": get("subject_key"),
            "url": get("url"),
            "status": get("status"),          # grounded | absent | unreachable
            "method": get("method"),          # substring | fuzzy | None
            "best_ratio": get("best_ratio"),  # 0..1; watch 0.80-0.92 for true-claim cuts
            "http_status": get("http_status"),
            "excerpt": get("excerpt"),
        })
    return rows


def _kept_rows(kept):
    """Trim kept claims to what the challenger needs to judge slop-admission."""
    rows = []
    for c in kept or []:
        if not isinstance(c, dict):
            continue
        rows.append({
            "id": c.get("id"),
            "claim": c.get("claim"),
            "claim_type": c.get("claim_type"),
            "section": c.get("section"),
            "zone": c.get("zone"),
            "source_url": c.get("source_url"),
            "source_tier": c.get("source_tier"),
            "evidence_excerpt": c.get("evidence_excerpt"),
            "confidence": c.get("confidence"),
            "grounding_method": (c.get("grounding") or {}).get("method"),
        })
    return rows


def _cut_rows(cut):
    """The cut log (verifier CUT/REVISED + deterministic grounding cuts), as {action, claim, reason}."""
    rows = []
    for e in cut or []:
        if not isinstance(e, dict):
            continue
        rows.append({
            "action": e.get("action", "CUT"),
            "claim": e.get("claim"),
            "reason": e.get("reason"),
        })
    return rows


def capture(slug, source, *, kept, cut, grounding,
            competitor=None, my_company=None, focus=None):
    """Record one run's champion decisions for shadow eval. No-op unless SHADOW_EVAL_ENABLED.

    GUARANTEED never to raise — the caller sits in the live write path, so any failure here is
    swallowed with a stderr warning rather than propagated. `source` is "generate" | "monitor".
    """
    if not config.SHADOW_EVAL_ENABLED:
        return
    try:
        now = datetime.now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "slug": slug,
            "source": source,
            "run_ts": now.isoformat(timespec="seconds"),
            "competitor": competitor,
            "my_company": my_company,
            "focus": focus,
            "kept": _kept_rows(kept),
            "cut": _cut_rows(cut),
            "grounding_results": _ground_rows(grounding),
        }
        stamp = now.strftime("%Y%m%dT%H%M%S")
        path = f"{SHADOW_DIR}/{slug}/{stamp}.json"
        selfserve.write_data(
            path,
            json.dumps(record, indent=2, default=str, ensure_ascii=False),
            f"shadow: capture {source} {slug} {stamp}",
        )
    except Exception as e:  # NEVER let shadow capture break a production run
        print(f"[shadow] capture skipped ({type(e).__name__}: {e})", file=sys.stderr)


def filter_capture(slug, *, run_ts, verdict, act_subject_keys,
                   competitor=None, my_company=None, mode=None):
    """Record one run's CONSEQUENTIALITY-FILTER decision (the strategic/consequential gate) for
    longitudinal eval. Separate dir from grounding capture, by design: the v3.5 champion/challenger
    eval studies the claim keep/cut in shadow/; THIS records a different judge (does the change
    matter enough to act on), and it is captured DOWNSTREAM of grounding so it can never alter what
    the grounding eval sees (docs/consequential-filter-spec.md, Fold A). No-op unless
    SHADOW_EVAL_ENABLED. GUARANTEED never to raise — the caller sits in the live write path."""
    if not config.SHADOW_EVAL_ENABLED:
        return
    try:
        v = verdict if isinstance(verdict, dict) else {}
        now = datetime.now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "slug": slug,
            "run_ts": run_ts or now.isoformat(timespec="seconds"),
            "mode": mode,
            "competitor": competitor,
            "my_company": my_company,
            "consequential": bool(v["consequential"]) if "consequential" in v else None,
            "consequence_rationale": v.get("consequence_rationale"),
            "lead_headline": v.get("headline"),
            "act_subject_keys": act_subject_keys or [],
        }
        stamp = now.strftime("%Y%m%dT%H%M%S")
        path = f"{FILTER_DIR}/{slug}/{stamp}.json"
        selfserve.write_data(
            path,
            json.dumps(record, indent=2, default=str, ensure_ascii=False),
            f"filter: verdict {slug} {stamp} (consequential={record['consequential']})",
        )
    except Exception as e:  # NEVER let filter capture break a production run
        print(f"[shadow] filter_capture skipped ({type(e).__name__}: {e})", file=sys.stderr)


def dismissal_capture(slug, *, run_ts, candidates, immaterial, became_material, alerts,
                      my_substantial, competitor=None, my_company=None):
    """Record what a run SURFACED but did NOT alert on: the triage candidates (each with whether it
    was substantial), the materiality judge's IMMATERIAL verdicts (signal + why_not — investigated
    and cut, with reasons), and the own-company signals that escalated, alongside what DID survive
    for contrast. This is the dismissal stream — where a silent miss ("dropped something important")
    would hide; grounding kept/cut in shadow/ only covers survivors-of-materiality, so dismissals
    were previously unauditable. No-op unless SHADOW_EVAL_ENABLED, and skipped on a truly quiet run
    (nothing surfaced) so we don't write empty shells. GUARANTEED never to raise (live write path)."""
    if not config.SHADOW_EVAL_ENABLED:
        return
    if not (candidates or immaterial or my_substantial):
        return
    try:
        now = datetime.now()
        record = {
            "schema_version": SCHEMA_VERSION, "slug": slug,
            "run_ts": run_ts or now.isoformat(timespec="seconds"),
            "competitor": competitor, "my_company": my_company,
            "surfaced": [{"about": c.get("about"), "substantial": c.get("substantial"),
                          "signal": c.get("signal"), "why_new": c.get("why_new"),
                          "source_hint": c.get("source_hint")} for c in (candidates or [])],
            "materiality_immaterial": immaterial or [],     # [{signal, why_not}] — cut, with reasons
            "became_material": became_material or [],         # subject_keys that survived (contrast)
            "alerts": alerts or [],
            "my_company_substantial": [{"signal": c.get("signal"), "why_new": c.get("why_new"),
                                        "source_hint": c.get("source_hint")} for c in (my_substantial or [])],
        }
        stamp = now.strftime("%Y%m%dT%H%M%S")
        selfserve.write_data(
            f"{DISMISSAL_DIR}/{slug}/{stamp}.json",
            json.dumps(record, indent=2, default=str, ensure_ascii=False),
            f"dismissals: {slug} {stamp} (surfaced {len(record['surfaced'])})",
        )
    except Exception as e:  # NEVER let dismissal capture break a production run
        print(f"[shadow] dismissal_capture skipped ({type(e).__name__}: {e})", file=sys.stderr)
