# Scout v2 — Claim Object Schema & Grounding Contract

**Status:** first concrete v2 artifact. Schema + written contracts only — no agent code yet.
**Date:** June 2026.

The claim object is the **state of record** for a battlecard. Markdown (`current.md`) is rendered *from* these objects; the monitor diffs *these objects*, never prose. This doc pins down (1) the object schema, (2) the deterministic ID scheme, and (3) the grounding-check contract. It supersedes the inline claim sketch in `v2-agent-spec.md` §7.

-----

## 1. Design rules that drive the schema

1. **Identity = subject, not text.** The `id` is a deterministic hash of `subject_key` — *what the claim is about* (`entity | attribute | qualifier`), independent of its current value. When the AWS FY-revenue figure changes, or a CEO is replaced, the new claim hashes to the **same id**, so the monitor updates in place instead of creating a duplicate. The value lives only in `claim`.
2. **Identity excludes presentation.** `section` / `zone` / `order` are mutable display attributes. Moving a claim between sections must NOT change its `id` — the monitor should see the same claim, not a new one. **Invariant:** `subject_key` is unique within a battlecard — it *is* the dedup key. Two genuinely distinct claims must never share a `subject_key` (they would collide to one `id` and silently overwrite). If the same subject legitimately belongs in two sections, that is **one** claim rendered once, not two. This is what keeps "identity excludes section" safe for accuracy: section is purely presentational, while everything that makes a claim verifiable (`source_url`, `evidence_excerpt`, `source_tier`, `grounding`) travels with the claim and is re-checked regardless of where it renders.
3. **Provenance (code) is separate from support (model).** The verifier subagent judges whether a source *supports* a claim. The deterministic grounding check (§4) only proves the `evidence_excerpt` is *really on the page*. A claim must pass both; they are different jobs, split along the v1 control-vs-autonomy line.
4. **One proven anchor per claim; corroboration is explicitly unproven.** A claim has exactly **one** grounded source: its `source_url` + `evidence_excerpt`. Additional sources that confirm the same value live in `corroboration[]` (§2.1) and are **never grounded** — they carry no excerpt, are marked `grounded: false`, and must never render in a way that implies they passed the check. This preserves v1's reconciliation nuance ("cross-checked against B and C") as structured state without re-opening the fabrication gap: only the anchor is proven.
4. **`claims.json` only ever holds survivors.** Every object persisted in `claims.json` has `verified: true` and `grounding.match: true`. Anything that fails verification or grounding is never stored — it is removed and recorded in the cut log / alert log with a reason.

-----

## 2. Schema

One battlecard's state of record is `claims.json`: a JSON array of claim objects. Each object:

```json
{
  "id": "c_a1b2c3d4e5f6",
  "subject_key": "aws | fy-revenue | 2025",
  "claim": "AWS full-year 2025 revenue was $128.7B.",
  "claim_type": "fact",
  "section": "snapshot",
  "zone": null,
  "order": 2,
  "source_url": "https://www.sec.gov/...",
  "source_tier": "primary",
  "evidence_excerpt": "Amazon Web Services segment sales were $128,693 million for the year ended December 31, 2025",
  "as_of": "2026-02-06",
  "verified": true,
  "confidence": "high",
  "grounding": {
    "checked": true,
    "match": true,
    "method": "substring",
    "fetched_at": "2026-06-03",
    "detail": null
  },
  "corroboration": [
    {
      "source_url": "https://www.cnbc.com/...",
      "source_tier": "reputable_secondary",
      "note": "CNBC independently reports the same $128.7B FY2025 AWS figure",
      "grounded": false
    }
  ]
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | `c_` + first 12 hex of the ID hash (§3). Deterministic from `subject_key` + slug. Never authored by hand. |
| `subject_key` | string | yes | Canonical `entity \| attribute \| qualifier` description of *what the claim is about*, value-independent. Drives `id`. See §3. |
| `claim` | string | yes | The claim as it reads in the brief, including its current value. Carries the source link when rendered. |
| `claim_type` | enum | yes | `fact` \| `interpretation` \| `sentiment`. A `fact` may not rest on `sentiment_only` (carried from the v1 source hierarchy). |
| `section` | enum | yes | `executive_summary` \| `snapshot` \| `recent_moves` \| `positioning` \| `pricing` \| `battlecard` \| `sentiment` \| `objection_handling`. Mutable; not part of `id`. |
| `zone` | enum or null | yes (nullable) | Battlecard only: `where_we_win` \| `contested` \| `where_they_win`. `null` for every other section. |
| `order` | integer ≥ 0 | yes | Sort order within `(section, zone)`. Lets rendering be deterministic code, not an LLM step. |
| `source_url` | string (URL) | yes | The single load-bearing source for this claim (one source per claim, as in v1). |
| `source_tier` | enum | yes | `primary` (Tier 1A) \| `reputable_secondary` (Tier 2/2E, labeled) \| `sentiment_only` (Tier 3S/4). Maps to the v1 hierarchy. |
| `evidence_excerpt` | string | yes | A **verbatim** span copied from the fetched source that backs the claim. Min 40 chars / 6 words so the substring check is meaningful; keep it tight. |
| `as_of` | string (date) or null | optional (→ **required for `fact` claims**, see §6) | The date the fact is true as-of / source publication date. Supports recency overrides and date-scoped monitoring. |
| `persona` | enum or null | optional | **Battlecard plays + `objection_handling` only.** The primary buyer persona a play is aimed at / who tends to raise an objection: `eng_led` \| `technical_evaluator` \| `economic_buyer` \| `security_regulated` \| `exec_top_down`. `null`/absent for every other section. Presentational only (like `section`/`zone`); lets the viewer badge plays and objections by audience. |
| `verified` | boolean | yes | Always `true` for stored claims (see Design rule 4). Kept explicit for audit. |
| `confidence` | enum | yes | `high` \| `medium` \| `low`. The verifier's confidence in support, independent of grounding. |
| `grounding` | object | yes | Result of the deterministic grounding check (§4). |
| `corroboration` | array | optional | Secondary sources that confirm the same value. **Never grounded** — each entry carries no excerpt and `grounded` is always `false`. Preserves reconciliation nuance; the single `evidence_excerpt`/`source_url` stays the only proven anchor. See §2.1. |
| `anchor_substitution` | object | optional | Present only when the grounded anchor was substituted for a *blocked* higher-tier source. Records the preferred (unreadable) source and an `agreement_verified` flag that **must be true**. See §2.2. |

### `grounding` sub-object

| Field | Type | Notes |
|---|---|---|
| `checked` | boolean | Whether the grounding check ran. |
| `match` | boolean | Whether the excerpt was found on the page. `true` for every stored claim. |
| `method` | enum | `substring` (exact, normalized) \| `fuzzy` (high-threshold token match) \| `none` (not yet checked). |
| `fetched_at` | string (date) | When the source was last fetched for grounding. |
| `detail` | string or null | Optional note, e.g. `"fuzzy 0.94"`, `"404"`, `"paywall"`. Set on failures for the cut log. |

### 2.1 `corroboration` — unproven secondary sources (the fabrication-gap guard)

v1 frequently reconciled a value across several sources ("audited 8-K $128.7B; $129B is the rounded figure"; a DoD event confirmed by NPR + CNBC + Mayer Brown). `corroboration[]` keeps that as **structured state** instead of burying it in cut-log prose — but it must never re-open the fabrication gap that grounding closes. So the line between *proven* and *merely asserted* is enforced in the data, not left to convention:

- **Only the anchor is proven.** Exactly one source per claim is grounded: `source_url` + `evidence_excerpt`, checked by §4. That is the only source whose backing is mechanically verified.
- **Corroboration entries are never grounded.** Each has **no `evidence_excerpt`** (schema-forbidden) and **`grounded: false`** (a `const`, so a fabricated "verified" flag can't be smuggled in). They name a source and a `note` describing what it independently confirms — nothing more.
- **Render rule.** Corroboration is **not** rendered as a body source link, because an identical-looking link would imply it passed grounding. If surfaced at all (audit view, expandable detail), it must sit under an explicit label such as *"Corroborating sources (not independently grounded)"*, visually distinct from the anchor link. The body's single inline source is always the grounded anchor.
- **What it is for:** trust/audit ("cross-checked against B and C") and monitoring (when an anchored value changes, the prior corroborators are useful context). It is never a substitute for grounding, and a claim with only corroboration and no valid anchor is **cut**, exactly as if it had no source at all.

### 2.2 `anchor_substitution` — fetch-weakness only, never a support shortcut

Reachability probing (build step 5) showed ~25–32% of cited sources are bot-blocked or paywalled to an independent fetcher — including reputable, groundable sources (SEC, major news outlets, OpenAI). (Note: Wikipedia, wikis, and encyclopedias are EXCLUDED sources — `grounding.is_excluded_source` deterministically cuts any claim anchored on them; they are never a valid anchor or corroboration. See `prompts.SOURCE_HIERARCHY`.) When the *best* source for a claim can't be fetched, the verifier may ground a **fetchable reputable source** as the anchor and record the blocked higher-tier source in `corroboration`. But substitution covers **fetch weakness, not support judgment** — and the guard is strict:

- **The verifier must still make the support call on the preferred source.** It reads BOTH (its own `WebFetch` is fine here — the verifier's job *is* judgment), and may substitute **only if it judged them to AGREE**.
- **Conflict → never substitute.** If the fetchable source and the blocked primary disagree, the verifier must resolve it (revise per the hierarchy/recency rules, or cut) and log it in the Cut Log. A fetch failure on the primary must **never** silently let a conflicting secondary through as if grounded.
- **Recorded for audit.** A substitution sets `anchor_substitution = {preferred_url, preferred_tier, agreement_verified: true, note}`, and the preferred source also appears in `corroboration`. `agreement_verified` is a `const true`: the object's mere existence asserts a verified agreement. Grounding flags every substituted claim (`substituted: true`) in its instrumentation so substitutions can be eyeballed at #7 — we have both URLs, so the verifier's agreement call is checkable, not just asserted.

This is honest about tiers: the *proven* source becomes the fetchable one (often Tier 2), while the un-fetchable Tier 1A source is preserved as named-but-ungrounded corroboration — never presented as if it passed the check.

### Formal JSON Schema (Draft 2020-12)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Scout v2 claim object",
  "type": "object",
  "additionalProperties": false,
  "required": ["id","subject_key","claim","claim_type","section","zone","order",
               "source_url","source_tier","evidence_excerpt","verified","confidence","grounding"],
  "properties": {
    "id": { "type": "string", "pattern": "^c_[0-9a-f]{12}$" },
    "subject_key": { "type": "string", "minLength": 1 },
    "claim": { "type": "string", "minLength": 1 },
    "claim_type": { "enum": ["fact","interpretation","sentiment"] },
    "section": { "enum": ["executive_summary","snapshot","recent_moves","positioning",
                          "pricing","battlecard","sentiment","objection_handling"] },
    "zone": { "enum": ["where_we_win","contested","where_they_win", null] },
    "order": { "type": "integer", "minimum": 0 },
    "source_url": { "type": "string", "format": "uri" },
    "source_tier": { "enum": ["primary","reputable_secondary","sentiment_only"] },
    "evidence_excerpt": { "type": "string", "minLength": 40 },
    "as_of": { "type": ["string","null"], "format": "date" },
    "persona": { "enum": ["eng_led","technical_evaluator","economic_buyer","security_regulated","exec_top_down", null] },
    "verified": { "const": true },
    "confidence": { "enum": ["high","medium","low"] },
    "grounding": {
      "type": "object",
      "additionalProperties": false,
      "required": ["checked","match","method","fetched_at"],
      "properties": {
        "checked": { "type": "boolean" },
        "match": { "const": true },
        "method": { "enum": ["substring","fuzzy"] },
        "fetched_at": { "type": "string", "format": "date" },
        "detail": { "type": ["string","null"] }
      }
    },
    "corroboration": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["source_url","source_tier","note","grounded"],
        "properties": {
          "source_url": { "type": "string", "format": "uri" },
          "source_tier": { "enum": ["primary","reputable_secondary","sentiment_only"] },
          "note": { "type": "string", "minLength": 1 },
          "grounded": { "const": false }
        }
      }
    },
    "anchor_substitution": {
      "type": "object",
      "additionalProperties": false,
      "required": ["preferred_url","preferred_tier","agreement_verified","note"],
      "properties": {
        "preferred_url": { "type": "string", "format": "uri" },
        "preferred_tier": { "enum": ["primary","reputable_secondary","sentiment_only"] },
        "agreement_verified": { "const": true },
        "note": { "type": "string", "minLength": 1 }
      }
    }
  },
  "allOf": [
    {
      "comment": "zone is non-null only inside the battlecard",
      "if":   { "properties": { "section": { "const": "battlecard" } } },
      "then": { "properties": { "zone": { "enum": ["where_we_win","contested","where_they_win"] } } },
      "else": { "properties": { "zone": { "const": null } } }
    },
    {
      "comment": "a fact may not rest on a sentiment-only source",
      "if":   { "properties": { "claim_type": { "const": "fact" } } },
      "then": { "properties": { "source_tier": { "enum": ["primary","reputable_secondary"] } } }
    }
  ]
}
```

The schema above is the *persistence* contract (what's stored in `claims.json`). The verifier subagent emits the same shape **without** `grounding` (and may emit `verified: false`); the deterministic post-step runs grounding, sets `grounding`, and either persists the claim or cuts it.

-----

## 3. ID scheme (deterministic, value-independent)

The `id` must be **stable across regenerations and across value changes** so the monitor can match "the AWS FY-revenue claim" to its prior record. It is a pure function of the slug and `subject_key`:

```python
import hashlib, re

def normalize_subject_key(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9|]+", " ", s)   # keep the pipe field-separator; everything else -> space
    s = re.sub(r"\s*\|\s*", "|", s)       # tighten around pipes
    s = re.sub(r"\s+", " ", s)            # collapse whitespace
    return s

def claim_id(slug: str, subject_key: str) -> str:
    key = f"{slug}||{normalize_subject_key(subject_key)}"
    return "c_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
```

### `subject_key` authoring rules (the orchestrator must follow these)

- Form: `entity | attribute | qualifier`. Examples:
  - `aws | fy-revenue | 2025` — period-bound fact. FY2026 is a *different* subject (correctly a new id).
  - `aws | ceo | current` — the qualifier `current` means a leadership change reuses the id, so the monitor updates in place and can alert on it.
  - `google-cloud | list-price | bigquery-on-demand` — a pricing change reuses the id.
- The qualifier decides update-vs-new. Use `current`/`latest` for "the present holder of a role/price/flagship" (so changes update in place). Use a fixed period (`2025`, `q1-2026`) only when a new period is genuinely a new fact.
- `subject_key` never contains the value. `aws | fy-revenue | 2025`, never `aws | fy-revenue-128.7b`.
- Identity is namespaced by `slug` (the battlecard folder), so the same subject in two battlecards gets distinct ids.

**Collisions:** 12 hex (48 bits) is ample within one battlecard's claim count. If two genuinely distinct subjects ever collide, disambiguate by making one `subject_key` more specific — do not hand-edit the `id`.

-----

## 4. Grounding-check contract

A deterministic post-step (plain code, **not** an agent tool) run after the verifier emits a claim during generation, and after the monitor creates or updates any claim. It is the mechanical anti-fabrication backstop that makes "backed by fetched evidence" literally true.

### What it does

1. **Fetch** `source_url` (the headless worker uses the SDK `WebFetch`; the v1 in-app path uses its existing fetch). 
2. **Normalize** both the fetched page text and `evidence_excerpt` identically: lowercase; unify smart quotes/dashes to ASCII; strip HTML; collapse all whitespace (incl. newlines) to single spaces; trim.
3. **Match:**
   - **substring** — excerpt is a contiguous substring of the normalized page text → pass, `method: "substring"`.
   - **fuzzy fallback** — if not an exact substring, compute the best token-sequence ratio of the excerpt against a sliding window of the page; ratio ≥ **0.92** → pass, `method: "fuzzy"`, `detail: "fuzzy <ratio>"`. (Tolerates HTML artifacts and minor whitespace/entity differences without letting a fabricated excerpt through.)
   - otherwise → **fail**.
4. **On pass:** set `grounding = {checked: true, match: true, method, fetched_at, detail}`, keep `verified: true`, persist the claim.
5. **On fail:** the claim is **CUT** — never written to `claims.json` — and recorded with a precise reason:
   - excerpt not found → cut reason `"evidence excerpt not found in source"`.
   - fetch failed / 404 / timeout / paywall → cut reason `"source unreachable for grounding"` (`detail` carries the cause). Distinct from a fabrication so it can be retried rather than treated as a lie.

### Excerpt requirements (enforced before the match)

- **Verbatim** — copied from the source, not paraphrased. Paraphrase defeats the substring check by design.
- **Length** — ≥ 40 chars / 6 words (schema-enforced) and ≤ ~300 chars. Long enough that a match is meaningful; short enough to be a real span, not a whole paragraph.

### Scope

- Applies to **every** claim that carries a `source_url` + `evidence_excerpt`, regardless of `claim_type`. A `fact` that fails is cut; a `sentiment` quote that can't be found on the page is cut too (we couldn't prove anyone said it).
- One source per claim (v1 convention). A claim needing multiple supporting sources is split into multiple claims, each independently grounded.

### Explicitly NOT in scope

- **Does not judge support.** Whether the excerpt actually *backs* the claim is the verifier subagent's semantic call (model). Grounding only proves the excerpt is on the page (code). Two layers, never merged.
- **Does not re-tier the source.** `source_tier` is the verifier's call against the hierarchy.

-----

## 5. Deferred (noted, resolved when monitoring is built)

- **#5 — semantic dedup.** Hash-of-headline/URL fingerprints re-alert the same event reported by a second outlet. Resolve toward a semantic fingerprint (competitor + event-type + normalized entity/date) or pass the alerted set to the Opus materiality step. Not a schema concern yet.
- **#6 — `last_checked` vs no-commit-on-no-change.** If quiet runs don't commit, `last_checked` never advances and the search window widens nightly. Decide whether the weekly heartbeat advances `last_checked`. A `meta.json` concern, out of scope for the claim object.

-----

## 6. Open for confirmation during build

- `evidence_excerpt` upper bound (300 chars is a starting point).
- Fuzzy threshold 0.92 — tune against real fetched pages before locking.
- ID hash width (12 hex) — revisit only if a real collision appears.

## 7. Planned changes (decided, not yet enforced)

- **`as_of` becomes required for `fact` claims.** Currently optional across the board. The intent (confirmed) is to make it mandatory when `claim_type == "fact"` — a fact without an as-of date is hard to monitor for recency overrides. To enforce later, add a conditional to the JSON Schema: `if claim_type == "fact" then required: ["as_of"]` with a non-null `as_of`. Deferred only so the first generation port isn't blocked on backfilling dates; the verifier should already populate `as_of` for facts wherever the source gives one.
