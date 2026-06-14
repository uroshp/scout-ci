"""Rendering: structured claims -> display Markdown, plus deterministic cleanup.

`clean_output`/`format_report` are **independent copies** of v1's helpers (originally
in app.py), kept here as a starting point for v2. v1 is frozen and retains its own
inline copies — app.py does NOT import from scout/. v2 evolves these freely without
any risk to the shipped v1 app. The duplication is intentional.
"""
import re
from urllib.parse import urlparse

from scout.schema import SECTIONS, ZONES

# Section enum -> the ## header text required by FORMATTING_RULES.
SECTION_TITLES = {
    "executive_summary": "Executive Summary",
    "snapshot": "Snapshot",
    "recent_moves": "Recent Strategic Moves",
    "positioning": "Positioning and Differentiation",
    "pricing": "Pricing and Packaging",
    "battlecard": "Competitive Battlecard",
    "sentiment": "Sentiment",
    "objection_handling": "Objection Handling",
}


# --- v1 deterministic cleanup (moved verbatim from app.py) --------------------
def clean_output(text):
    idx = text.find("# Competitive Intelligence Brief")
    if idx != -1:
        text = text[idx:]
    # Escape dollar signs so Streamlit doesn't treat $...$ as LaTeX math.
    # IDEMPOTENT (don't touch an already-escaped \$): the monitor re-runs the carried-forward
    # Cut Log through this function on every material change, and the naive replace doubled
    # the backslashes each time (\$ -> \\$ -> ...), which leaked into the rendered page.
    text = re.sub(r"(?<!\\)\$", r"\\$", text)
    # Remove leading whitespace on lines so they aren't rendered as code blocks
    lines = [line.lstrip() if line.startswith(("    ", "\t")) else line for line in text.split("\n")]
    text = "\n".join(lines)
    return text


def format_report(text):
    """Deterministic formatting cleanup - fixes what the model won't do reliably."""
    lines = text.split("\n")
    out = []

    # 1. Drop any cover-block lines before the real title
    title_found = False
    for line in lines:
        stripped = line.strip()
        if not title_found:
            if "Competitive Intelligence Brief" in stripped:
                title_found = True
                out.append("# " + stripped.lstrip("#").strip().lstrip("🔵 ").strip())
                continue
            else:
                continue
        out.append(line)

    if not title_found:
        out = lines  # fallback: leave as-is if no title found

    # 2. Stitch broken bullets: a "- " line whose text is on the next non-empty line
    stitched = []
    i = 0
    while i < len(out):
        line = out[i]
        if line.strip() == "-" or (line.strip().startswith("- ") and len(line.strip()) <= 2):
            j = i + 1
            while j < len(out) and out[j].strip() == "":
                j += 1
            if j < len(out):
                stitched.append("- " + out[j].strip())
                i = j + 1
                continue
        stitched.append(line)
        i += 1

    # 3. Collapse blank lines between consecutive bullets
    final = []
    for k, line in enumerate(stitched):
        if line.strip() == "":
            prev = final[-1].strip() if final else ""
            nxt = ""
            for m in range(k + 1, len(stitched)):
                if stitched[m].strip():
                    nxt = stitched[m].strip()
                    break
            if prev.startswith("- ") and nxt.startswith("- "):
                continue
        final.append(line)

    return "\n".join(final)


# --- v2: structured claims -> Markdown ---------------------------------------
def _source_label(url: str) -> str:
    netloc = urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or url


def _resolve_source_url(c: dict, by_id: dict | None) -> str | None:
    """The URL to link for a claim. An ordinary claim carries its own `source_url`. A PROPAGATED
    interpretation has none — its provenance is the grounded fact it descends from, so follow
    `derived_from` to that parent and borrow its source (claim-object.md §2.3)."""
    if c.get("source_url"):
        return c["source_url"]
    df = c.get("derived_from")
    if df and by_id:
        parent = by_id.get(df)
        if parent and parent.get("source_url"):
            return parent["source_url"]
    return None


def _source_link(c: dict, by_id: dict | None = None) -> str:
    url = _resolve_source_url(c, by_id)
    return f"([{_source_label(url)}]({url}))" if url else ""


def _zone_title(zone: str, my_company: str | None, competitor: str | None) -> str:
    me = my_company or "You"
    them = competitor or "They"
    return {
        "where_we_win": f"Where {me} wins",
        "contested": "Where it's a fight",
        "where_they_win": f"Where {them} wins",
    }[zone]


# Sections whose `claim` field is an authored multi-line prose block
# (title -> paragraph -> soundbite/so-what), rendered verbatim rather than as a
# one-line bullet. The battlecard is here too but renders per-zone (see below).
BLOCK_SECTIONS = {"executive_summary", "objection_handling"}


def _render_block(c, by_id=None):
    """A claim authored as a prose block: emit it verbatim with its grounded
    source link appended. Mirrors how the executive summary has always rendered;
    the orchestrator writes the title/paragraph/soundbite structure into `claim`."""
    return f"{c['claim'].strip()} {_source_link(c, by_id)}".rstrip()


def _ordered(section, items):
    """Sort a section's claims for rendering. Recent Strategic Moves reads
    reverse-chronologically (newest first) off each claim's as_of date — a quiet
    item without a date sinks to the bottom. Every other section keeps the
    orchestrator's importance order (`order`, most important first)."""
    if section == "recent_moves":
        return sorted(items, key=lambda c: (c.get("as_of") or "", c["order"]), reverse=True)
    return sorted(items, key=lambda c: c["order"])


def claims_to_markdown(claims, title, my_company=None, competitor=None):
    """Render the brief body from claim objects, deterministically, by section
    and (in the battlecard) zone, ordered by each claim's `order`.

    Exec summary, battlecard zones, and objection handling render as PROSE BLOCKS
    (the v1-readable format); the remaining sections stay as single-line bullets.

    NOTE: corroboration is intentionally NOT rendered in the body — only the
    grounded anchor source appears, per the claim-object.md §2.1 render rule.
    """
    # Resolve a propagated claim's inherited source against EVERY claim (incl. retired + facts in
    # other sections), then render only the ACTIVE card — retired claims live in the lineage view,
    # never on the active card (claim-object.md §2.3).
    by_id = {c["id"]: c for c in claims if c.get("id")}
    active = [c for c in claims if str(c.get("status", "active")) == "active"]

    by_section = {}
    for c in active:
        by_section.setdefault(c["section"], []).append(c)

    lines = [title.rstrip(), ""]
    for section in SECTIONS:
        items = by_section.get(section)
        if not items:
            continue
        lines += [f"## {SECTION_TITLES[section]}", ""]

        if section == "battlecard":
            by_zone = {}
            for c in items:
                by_zone.setdefault(c["zone"], []).append(c)
            for zone in ZONES:
                z = by_zone.get(zone)
                if not z:
                    continue
                lines += [f"### {_zone_title(zone, my_company, competitor)}", ""]
                # Each zone entry is a prose block (title/paragraph/soundbite),
                # not a bullet — the v1-readable battlecard format.
                for c in sorted(z, key=lambda c: c["order"]):
                    lines += [_render_block(c, by_id), ""]
        elif section in BLOCK_SECTIONS:
            for c in _ordered(section, items):
                lines += [_render_block(c, by_id), ""]
        else:
            for c in _ordered(section, items):
                lines.append(f"- {c['claim'].strip()} {_source_link(c, by_id)}".rstrip())
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_cut_log(entries):
    """entries: list of {action, claim, reason} dicts — but models sometimes emit
    plain strings or omit keys, so render defensively rather than crash."""
    lines = ["## Cut Log", "",
             "This is what verification removed or corrected during fact-checking, and why."]
    if not entries:
        lines.append("- No claims required cutting or revision — every claim in the draft "
                     "verified cleanly against a reliable source.")
    else:
        for e in entries:
            if isinstance(e, dict):
                action = str(e.get("action", "")).upper().strip()
                claim = str(e.get("claim", "")).strip()
                reason = str(e.get("reason", "")).strip()
                prefix = f"**{action} — {claim}:** " if (action or claim) else ""
                lines.append(f"- {prefix}{reason}".rstrip())
            else:
                lines.append(f"- {str(e).strip()}")
    return "\n".join(lines)


def extract_cut_log(md):
    """Pull the verbatim `## Cut Log` section (header through end of section) out
    of a rendered brief, or "" if absent. Used to carry the generation-time Cut
    Log forward when the body is later regenerated from claims (monitoring),
    since cut-log entries live only in the markdown — never in the claim store."""
    m = re.search(r"^##\s+Cut Log\s*$.*?(?=^##\s|\Z)", md, re.S | re.M)
    return m.group(0).rstrip() if m else ""
