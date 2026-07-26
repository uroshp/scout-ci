"""Claim-object identity + validation — the executable side of docs/claim-object.md.

The JSON Schema embedded here is the runtime source of truth; the doc is the human
spec. Keep them in sync. `claim_id` / `normalize_subject_key` implement §3 verbatim.
"""
import hashlib
import os
import re

from jsonschema import Draft202012Validator

# --- ID scheme (claim-object.md §3) ------------------------------------------
# Identity is the claim's SUBJECT, not its text or its current value, so a changed
# figure or a replaced CEO hashes to the same id and the monitor updates in place.


def normalize_subject_key(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9|]+", " ", s)   # keep the pipe field-separator; else -> space
    s = re.sub(r"\s*\|\s*", "|", s)       # tighten around pipes
    s = re.sub(r"\s+", " ", s)            # collapse whitespace
    return s


def claim_id(slug: str, subject_key: str) -> str:
    key = f"{slug}||{normalize_subject_key(subject_key)}"
    return "c_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


# --- Schema (claim-object.md §2) ---------------------------------------------
# The RENDERED/GENERATED sections — what generation emits, render iterates, and the viewer shows.
SECTIONS = [
    "executive_summary", "snapshot", "recent_moves", "positioning",
    "pricing", "battlecard", "sentiment", "objection_handling",
]
# Non-rendered ANCHOR section (propagation §17). Holds grounded my_company facts purely as
# provenance anchors: a back-foot own-development (e.g. a model pulled) that a propagated objection
# derives_from. Deliberately NOT in SECTIONS, so it is invisible to render/generation/the viewer
# (our own news never appears in the competitor feed) yet is a tracked, monitorable claim the
# retire-cascade can later falsify. The schema accepts it; nothing renders it.
ANCHOR_SECTION = "tracked_facts"
ALL_SECTIONS = SECTIONS + [ANCHOR_SECTION]
ZONES = ["where_we_win", "contested", "where_they_win"]

# Render-contract word cap for block claims (exec summary / objections / battlecard): over this,
# a claim is a wall no rep reads — condensed by the repair path, rejected at generation. Healthy
# claims run 95-115 words (2026-07-02 audit); 170 leaves room for a genuinely layered story.
RENDER_MAX_WORDS = int(os.environ.get("SCOUT_RENDER_MAX_WORDS", "170"))
SOURCE_TIERS = ["primary", "reputable_secondary", "sentiment_only"]
# Primary buyer persona a play is aimed at / that tends to raise an objection. Optional, and
# meaningful ONLY for the rep-facing prose sections (battlecard + objection_handling); null
# everywhere else. Lets the viewer badge plays/objections by audience (see docs/claim-object.md).
PERSONAS = ["eng_led", "technical_evaluator", "economic_buyer", "security_regulated", "exec_top_down"]

# Fields every claim carries regardless of provenance. The own-source fields
# (source_url/source_tier/evidence_excerpt/grounding) are NOT here: they are required
# conditionally — only when a claim is NOT a derived_from-anchored interpretation (see the
# allOf below). This is what lets a propagated play/objection persist without its own URL.
_BASE_REQUIRED = [
    "id", "subject_key", "claim", "claim_type", "section", "zone", "order",
    "verified", "confidence",
]

_PROPERTIES = {
        "id": {"type": "string", "pattern": "^c_[0-9a-f]{12}$"},
        "subject_key": {"type": "string", "minLength": 1},
        "claim": {"type": "string", "minLength": 1},
        "claim_type": {"enum": ["fact", "interpretation", "sentiment"]},
        "section": {"enum": ALL_SECTIONS},
        "zone": {"enum": ZONES + [None]},
        "order": {"type": "integer", "minimum": 0},
        "source_url": {"type": "string", "format": "uri"},
        "source_tier": {"enum": SOURCE_TIERS},
        "evidence_excerpt": {"type": "string", "minLength": 40},
        "as_of": {"type": ["string", "null"], "format": "date"},
        # Optional audience tag for battlecard plays + objections; null/absent elsewhere.
        "persona": {"enum": PERSONAS + [None]},
        "verified": {"const": True},
        "confidence": {"enum": ["high", "medium", "low"]},
        "grounding": {
            "type": "object",
            "additionalProperties": False,
            "required": ["checked", "match", "method", "fetched_at"],
            "properties": {
                "checked": {"type": "boolean"},
                "match": {"const": True},
                "method": {"enum": ["substring", "fuzzy"]},
                "fetched_at": {"type": "string", "format": "date"},
                "detail": {"type": ["string", "null"]},
            },
        },
        "corroboration": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_url", "source_tier", "note", "grounded"],
                "properties": {
                    "source_url": {"type": "string", "format": "uri"},
                    "source_tier": {"enum": SOURCE_TIERS},
                    "note": {"type": "string", "minLength": 1},
                    "grounded": {"const": False},
                },
            },
        },
        # Present ONLY when the grounded anchor was substituted for a blocked
        # higher-tier source (claim-object.md §2.2). Its existence asserts the
        # verifier read both and judged them to AGREE, hence agreement_verified
        # is a const true — a substitution on conflicting sources is never valid.
        "anchor_substitution": {
            "type": "object",
            "additionalProperties": False,
            "required": ["preferred_url", "preferred_tier", "agreement_verified", "note"],
            "properties": {
                "preferred_url": {"type": "string", "format": "uri"},
                "preferred_tier": {"enum": SOURCE_TIERS},
                "agreement_verified": {"const": True},
                "note": {"type": "string", "minLength": 1},
            },
        },
        # --- Propagation / living-card lifecycle (v2.5, claim-object.md §2.3 / spec §17) ---
        # All OPTIONAL and additive: existing claims (and the 8 committed cards) omit them and
        # stay valid; absence of `status` means "active", so no backfill is needed. A propagated
        # play/objection carries `derived_from` = the grounded fact's id it descends from (its
        # provenance anchor, since an interpretation has no source of its own). Retiring a claim
        # is a `status` transition into the lineage view, never a delete. The conditional that
        # EXEMPTS a derived_from claim from its own-source grounding, and the one that REQUIRES
        # retired_on/derived_from when status==retired, are deferred to the propose/judge build
        # (they ship with the code that emits these claims — see §7 Planned changes).
        "derived_from": {"type": ["string", "null"], "pattern": "^c_[0-9a-f]{12}$"},
        "status": {"enum": ["active", "retired"]},
        "retired_on": {"type": ["string", "null"], "format": "date"},
        "updated_on": {"type": ["string", "null"], "format": "date"},
        "retired_reason": {"type": ["string", "null"], "minLength": 1},
}


def _build_claim_schema(require_grounding: bool) -> dict:
    """Assemble the claim schema. `require_grounding` distinguishes the PERSISTENCE contract
    (grounding present) from the PRE-grounding shape the verifier emits (grounding filled in
    later). The two propagation conditionals (claim-object.md §7) ship HERE, with the code that
    first emits propagated claims:

      1. derived_from EXEMPTS own-source grounding. A propagated interpretation has no source of
         its own — its provenance is the parent grounded fact. So when `derived_from` is a present
         id AND no `source_url` is given, the own-source fields are not required, but the claim
         MUST be claim_type 'interpretation' (propagation never mints a fact). A claim WITH its own
         source_url (e.g. a retired claim that kept its evidence and gained a killer-fact link)
         takes the normal branch and is unaffected.
      2. status: retired REQUIRES retired_on + derived_from (the killing fact). Retirement is a
         tracked transition, never a half-built delete.
    """
    own_source = ["source_url", "source_tier", "evidence_excerpt"]
    if require_grounding:
        own_source = own_source + ["grounding"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Scout v2 claim object",
        "type": "object",
        "additionalProperties": False,
        "required": _BASE_REQUIRED,
        "properties": _PROPERTIES,
        "allOf": [
            {
                "comment": "zone is non-null only inside the battlecard",
                "if": {"properties": {"section": {"const": "battlecard"}}},
                "then": {"properties": {"zone": {"enum": ZONES}}},
                "else": {"properties": {"zone": {"const": None}}},
            },
            {
                "comment": "a fact may not rest on a sentiment-only source",
                "if": {"properties": {"claim_type": {"const": "fact"}}},
                "then": {"properties": {"source_tier": {"enum": ["primary", "reputable_secondary"]}}},
            },
            {
                "comment": "derived_from (no own source) exempts own-source grounding but forces "
                           "claim_type=interpretation; everything else carries its own anchor",
                "if": {
                    "required": ["derived_from"],
                    "properties": {"derived_from": {"type": "string"}},
                    "not": {"required": ["source_url"]},
                },
                "then": {"properties": {"claim_type": {"const": "interpretation"}}},
                "else": {"required": own_source},
            },
            {
                "comment": "a retired claim records when + why (the killing fact via derived_from)",
                "if": {"required": ["status"], "properties": {"status": {"const": "retired"}}},
                "then": {
                    "required": ["retired_on", "derived_from"],
                    "properties": {"retired_on": {"type": "string"},
                                   "derived_from": {"type": "string"}},
                },
            },
        ],
    }


# Persistence contract (what's stored in claims.json): grounding present.
CLAIM_SCHEMA = _build_claim_schema(require_grounding=True)
_validator = Draft202012Validator(CLAIM_SCHEMA)

# Pre-grounding shape: the verifier emits everything EXCEPT `grounding`, which the
# deterministic grounding step fills in (and `id`, which code derives from subject_key).
# Validate this before spending a fetch on a malformed claim.
PREGROUNDING_SCHEMA = _build_claim_schema(require_grounding=False)
_pre_validator = Draft202012Validator(PREGROUNDING_SCHEMA)


def _errors(validator, claim):
    return [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(claim), key=lambda e: list(e.path))
    ]


def render_structure_errors(claim: dict) -> list[str]:
    """Deterministic RENDER-CONTRACT check, scoped to ONLY the sections that actually render a
    structured response block (scout/page.py). The rest legitimately have none, so we never force one
    where it does not fit: recent_moves (Recent STRATEGIC Moves — its So-what is OPTIONAL), snapshot,
    positioning, pricing/packaging, sentiment, tracked_facts.
      - executive_summary + objection_handling MUST carry a '**So what:**' block;
      - battlecard win/lose plays (where_we_win / where_they_win) MUST carry a '**Soundbite:**' block
        (a 'contested' entry is a neutral framing, not a play, and is exempt);
      - objection_handling + win/lose plays MUST also carry a `persona` — it renders the per-claim
        buyer badge ('Raised by …' on objections, 'Best for …' on plays); without it the badge silently
        vanishes. (exec summary / contested have no such badge, so persona is not required there.)
    A missing block renders as an unstructured blob; a missing persona drops the badge — both are hard
    validity errors that trigger a re-ask / re-classify (NOT a drop; see the no-drop repair path).

    LENGTH CAP (2026-07-02, the 353-word Fable/Mythos objection): reconciling revises accrete beats
    until a claim is an unreadable wall. Rendered block claims (exec summary / objections /
    battlecard) over RENDER_MAX_WORDS are a render-contract error — condensed by the repair path
    (never dropped), rejected at generation. Healthy claims run 95-115 words; the cap leaves
    headroom for a genuinely layered story."""
    text = claim.get("claim") or ""
    section, zone = claim.get("section"), claim.get("zone")
    is_play = section == "battlecard" and zone in ("where_we_win", "where_they_win")
    errs = []
    if section in ("executive_summary", "objection_handling") and "**So what:**" not in text:
        errs.append(f"{section}: missing required '**So what:**' block (renders as an unstructured blob)")
    if is_play and "**Soundbite:**" not in text:
        errs.append("battlecard win/lose play: missing required '**Soundbite:**' block")
    if (section == "objection_handling" or is_play) and not claim.get("persona"):
        errs.append(f"{section}: missing required persona (renders the per-claim buyer badge)")
    errs.extend(word_cap_errors(claim))
    return errs


def word_cap_errors(claim: dict) -> list[str]:
    """The word-cap subset of the render contract, public + pure so the propagation length floor
    (propagate step 4b2) and the condense trigger in the no-drop repair key off the SAME count that
    ultimately gates render. The count is deliberately naive (len(text.split()), markdown markers
    included) — one number everywhere, no drift."""
    text = claim.get("claim") or ""
    section = claim.get("section")
    errs = []
    if section in ("executive_summary", "objection_handling", "battlecard"):
        words = len(text.split())
        if words > RENDER_MAX_WORDS:
            errs.append(f"{section}: {words} words exceeds the {RENDER_MAX_WORDS}-word render cap "
                        f"— condense (keep the current state + still-true numbers; collapse "
                        f"resolved history to one line)")
    return errs


def validation_errors(claim: dict) -> list[str]:
    """Return human-readable schema errors for one (final, grounded) claim ([] if valid). Includes the
    render-structure contract, so a block claim that would render as a blob is rejected here — the gate
    that stops a malformed objection/play from ever being applied or published."""
    return _errors(_validator, claim) + render_structure_errors(claim)


def pregrounding_errors(claim: dict) -> list[str]:
    """Schema errors for a claim before grounding has been attached ([] if valid). Also enforces the
    render-structure contract, so generation rejects a markerless block claim before spending a fetch
    — forcing the verifier to re-emit it properly rather than grounding then publishing a blob."""
    return _errors(_pre_validator, claim) + render_structure_errors(claim)


def is_valid(claim: dict) -> bool:
    return not validation_errors(claim)


def check_id(claim: dict, slug: str) -> str | None:
    """Confirm the claim's id matches the deterministic hash of its subject_key.
    Returns an error string if mismatched, else None."""
    expected = claim_id(slug, claim["subject_key"])
    if claim.get("id") != expected:
        return f"id {claim.get('id')!r} != expected {expected!r} for subject_key {claim['subject_key']!r}"
    return None
