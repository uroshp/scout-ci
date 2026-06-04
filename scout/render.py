"""Rendering: structured claims -> display Markdown, plus deterministic cleanup.

`clean_output`/`format_report` are **independent copies** of v1's helpers (originally
in app.py), kept here as a starting point for v2. v1 is frozen and retains its own
inline copies — app.py does NOT import from scout/. v2 evolves these freely without
any risk to the shipped v1 app. The duplication is intentional.
"""
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
    # Escape dollar signs so Streamlit doesn't treat $...$ as LaTeX math
    text = text.replace("$", "\\$")
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


def _source_link(c: dict) -> str:
    return f"([{_source_label(c['source_url'])}]({c['source_url']}))"


def _zone_title(zone: str, my_company: str | None, competitor: str | None) -> str:
    me = my_company or "You"
    them = competitor or "They"
    return {
        "where_we_win": f"Where {me} wins",
        "contested": "Where it's a fight",
        "where_they_win": f"Where {them} wins",
    }[zone]


def claims_to_markdown(claims, title, my_company=None, competitor=None):
    """Render the brief body from claim objects, deterministically, by section
    and (in the battlecard) zone, ordered by each claim's `order`.

    NOTE: corroboration is intentionally NOT rendered in the body — only the
    grounded anchor source appears, per the claim-object.md §2.1 render rule.
    """
    by_section = {}
    for c in claims:
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
                for c in sorted(z, key=lambda c: c["order"]):
                    lines.append(f"- {c['claim'].strip()} {_source_link(c)}")
                lines.append("")
        elif section == "executive_summary":
            # Exec points are multi-line blocks (verdict + So what), written by the
            # orchestrator. Source link appended at the end of the block for now;
            # placement is a known refine-after-#7 item.
            for c in sorted(items, key=lambda c: c["order"]):
                lines += [f"{c['claim'].strip()} {_source_link(c)}", ""]
        else:
            for c in sorted(items, key=lambda c: c["order"]):
                lines.append(f"- {c['claim'].strip()} {_source_link(c)}")
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
