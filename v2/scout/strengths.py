"""my_company STANDING STRENGTHS for propagation pivots (spec: docs/my-company-search-and-outage-spec.md §A).

A back-foot objection's rebuttal must pivot to a grounded my_company strength (propagate._PROPOSE_SYSTEM:
"the strength you pivot to MUST itself be grounded"). But the propagation judge admits ONLY `facts` as
evidence (propagate._JUDGE_SYSTEM), so a pivot to a strength that lives only as an existing card claim (an
interpretation, often never grounded on its own) reads as an invented capability and is rejected — the
"catch-22" the first authorship adjudication surfaced (decision-log §12; adjudication #5). This module
supplies those strengths AS GROUNDED FACTS in the facts pool, so a correctly-grounded pivot can pass.

v1 is DETERMINISTIC — NO model, NO network: it re-grounds the card's EXISTING grounded my_company
strengths (active where_we_win plays + my_company tracked_facts) that already carry resolvable provenance
into standing-strength facts (marked `standing_strength: True`). A strength whose pivot was never grounded
anywhere on the card is NOT recoverable here; a cold web-search enrichment for the standard defensive
dimensions (availability/SLA, security, pricing) is the documented fast-follow — it costs model+search
spend, so it is gated on a back-foot trigger and is where the `strengths/<slug>.json` cache earns its keep.

A standing-strength fact is admissible EVIDENCE for a rebuilt pivot; it is NEVER a trigger for a new op.
propagate() excludes it from the derivable-fact set, so the deterministic floor rejects any op whose
derived_from is a strength (see propagate._trigger_fact_ids)."""
import re

from scout.schema import ANCHOR_SECTION, claim_id

STRENGTHS_DIR = "strengths"   # where the (future) search-built cache lives in the private store

# Strip the rep-facing So-what/Soundbite block so the fact carries the grounded ASSERTION, not the pitch.
_BLOCK_RE = re.compile(r"\n\s*\*\*(?:So what|Soundbite):\*\*.*", re.IGNORECASE | re.DOTALL)


def _assertion(claim_text) -> str:
    return _BLOCK_RE.sub("", str(claim_text or "")).strip()


def _is_strength(c: dict) -> bool:
    """A my_company strength claim: an active where_we_win play, or a my_company tracked_fact."""
    if str(c.get("status", "active")) != "active":
        return False
    if c.get("section") == "battlecard" and c.get("zone") == "where_we_win":
        return True
    if c.get("section") == ANCHOR_SECTION and str(c.get("about", "")).lower() == "my_company":
        return True
    return False


def _resolve_source(c: dict, by_id: dict) -> dict | None:
    """The grounded source backing a strength: its OWN (source_url+excerpt), else its derived_from
    parent's. None when neither is grounded — that strength's pivot was never grounded, so we cannot
    supply it as a fact here (a cold search would be needed; the fast-follow)."""
    if c.get("source_url") and c.get("evidence_excerpt"):
        return {"source_url": c["source_url"], "source_tier": c.get("source_tier"),
                "evidence_excerpt": c["evidence_excerpt"], "as_of": c.get("as_of")}
    parent = by_id.get(c.get("derived_from"))
    if isinstance(parent, dict) and parent.get("source_url") and parent.get("evidence_excerpt"):
        return {"source_url": parent["source_url"], "source_tier": parent.get("source_tier"),
                "evidence_excerpt": parent["evidence_excerpt"], "as_of": parent.get("as_of")}
    return None


def build_from_claims(slug: str, claims: list) -> list:
    """Deterministically re-ground the card's existing grounded my_company strengths into standing-
    strength FACTS (marked `standing_strength: True`). No model, no network. Skips a strength with no
    resolvable grounded source. Returns a list of fact dicts shaped for propagation's facts pool."""
    by_id = {c.get("id"): c for c in claims if c.get("id")}
    out, seen = [], set()
    for c in claims:
        if not _is_strength(c):
            continue
        sk = str(c.get("subject_key") or "").strip()
        assertion = _assertion(c.get("claim"))
        if not sk or not assertion or sk in seen:
            continue
        src = _resolve_source(c, by_id)
        if not src:
            continue
        seen.add(sk)
        out.append({
            "id": claim_id(slug, f"strength::{sk}"),
            "subject_key": sk,
            "claim": assertion,
            "claim_type": "fact",
            "about": "my_company",
            "valence": "front_foot",
            "source_url": src["source_url"],
            "source_tier": src.get("source_tier"),
            "evidence_excerpt": src["evidence_excerpt"],
            "as_of": src.get("as_of"),
            "standing_strength": True,
        })
    return out


def get(slug: str, meta: dict, claims: list) -> list:
    """Standing-strength facts to add to the propagation facts pool for `slug`.

    v1 rebuilds deterministically from the current claims (cheap, always-current, no spend). The public
    `get()` shape is stable so the cold-search enrichment (the fast-follow) can slot in here later —
    load `strengths/<slug>.json`, refresh on staleness, union with the deterministic set — without
    touching the call site. Never raises into the caller."""
    try:
        return build_from_claims(slug, claims)
    except Exception:
        return []
