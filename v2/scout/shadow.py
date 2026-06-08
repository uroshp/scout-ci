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
