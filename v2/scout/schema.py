"""Claim-object identity + validation — the executable side of docs/claim-object.md.

The JSON Schema embedded here is the runtime source of truth; the doc is the human
spec. Keep them in sync. `claim_id` / `normalize_subject_key` implement §3 verbatim.
"""
import copy
import hashlib
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
SECTIONS = [
    "executive_summary", "snapshot", "recent_moves", "positioning",
    "pricing", "battlecard", "sentiment", "objection_handling",
]
ZONES = ["where_we_win", "contested", "where_they_win"]
SOURCE_TIERS = ["primary", "reputable_secondary", "sentiment_only"]
# Primary buyer persona a play is aimed at / that tends to raise an objection. Optional, and
# meaningful ONLY for the rep-facing prose sections (battlecard + objection_handling); null
# everywhere else. Lets the viewer badge plays/objections by audience (see docs/claim-object.md).
PERSONAS = ["eng_led", "technical_evaluator", "economic_buyer", "security_regulated", "exec_top_down"]

CLAIM_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Scout v2 claim object",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id", "subject_key", "claim", "claim_type", "section", "zone", "order",
        "source_url", "source_tier", "evidence_excerpt", "verified", "confidence",
        "grounding",
    ],
    "properties": {
        "id": {"type": "string", "pattern": "^c_[0-9a-f]{12}$"},
        "subject_key": {"type": "string", "minLength": 1},
        "claim": {"type": "string", "minLength": 1},
        "claim_type": {"enum": ["fact", "interpretation", "sentiment"]},
        "section": {"enum": SECTIONS},
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
    },
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
    ],
}

_validator = Draft202012Validator(CLAIM_SCHEMA)

# Pre-grounding shape: the verifier emits everything EXCEPT `grounding`, which the
# deterministic grounding step fills in (and `id`, which code derives from
# subject_key). Validate this before spending a fetch on a malformed claim.
PREGROUNDING_SCHEMA = copy.deepcopy(CLAIM_SCHEMA)
PREGROUNDING_SCHEMA["required"] = [r for r in CLAIM_SCHEMA["required"] if r != "grounding"]
_pre_validator = Draft202012Validator(PREGROUNDING_SCHEMA)


def _errors(validator, claim):
    return [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(claim), key=lambda e: list(e.path))
    ]


def validation_errors(claim: dict) -> list[str]:
    """Return human-readable schema errors for one (final, grounded) claim ([] if valid)."""
    return _errors(_validator, claim)


def pregrounding_errors(claim: dict) -> list[str]:
    """Schema errors for a claim before grounding has been attached ([] if valid)."""
    return _errors(_pre_validator, claim)


def is_valid(claim: dict) -> bool:
    return not validation_errors(claim)


def check_id(claim: dict, slug: str) -> str | None:
    """Confirm the claim's id matches the deterministic hash of its subject_key.
    Returns an error string if mismatched, else None."""
    expected = claim_id(slug, claim["subject_key"])
    if claim.get("id") != expected:
        return f"id {claim.get('id')!r} != expected {expected!r} for subject_key {claim['subject_key']!r}"
    return None
