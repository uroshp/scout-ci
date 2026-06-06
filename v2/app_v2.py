"""Scout v2 — living-battlecard viewer (READ-ONLY).

Public view of pre-baked, git-committed battlecards. Surfaces the four "show the
agentic work" display elements around the verified brief. Does NOT trigger generation
(gated, last-stage) and does NOT run monitoring. Reads the store + git only.

Run:  streamlit run app_v2.py
"""
import base64
import html
import os
import random
import re
import time
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

# Streamlit Community Cloud exposes configured secrets via st.secrets, NOT as env vars. Bridge
# them into the environment BEFORE importing scout.config (which reads env at import time), so the
# deployed app picks up SELFSERVE_GH_TOKEN / SELFSERVE_REPO / SELFSERVE_BRANCH. setdefault never
# clobbers a real env var (local dev still wins); the guard makes it a no-op when no secrets exist.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass

from scout import config, display, page, selfserve, store

# Rotating status copy — ported verbatim from v1 app.py (the "v1 progress messages") so the
# self-serve wait feels like the rest of Scout. The bar is a timed estimate (the real job runs
# out-of-band in a GitHub Action), so these are decorative, honest-about-the-wait flavor.
PROGRESS_MESSAGES = {
    "research": [
        "Reading everything the internet says about them so you don't have to...",
        "Digging through funding announcements and earnings calls...",
        "Stalking their careers page for hiring tells...",
        "Lurking in the forums where people say what they really think...",
    ],
    "draft": [
        "Connecting dots a human would need three coffees to connect...",
        "Figuring out what actually matters and what is just noise...",
        "Writing the verdict, not the encyclopedia...",
    ],
    "verify": [
        "Catching the AI before it makes things up...",
        "Fact-checking every claim like a paranoid editor...",
        "Cutting anything we cannot prove. Sorry, juicy rumors...",
        "Cross-examining the numbers until they confess...",
        "Making sure every link actually goes somewhere...",
    ],
    "final": [
        "Polishing. Almost ready to make you look smart in that meeting...",
    ],
}

# "~6–8 minutes" — the estimate the bar fills against. Decoupled from the actual job.
_SELFSERVE_ESTIMATE_S = 420

# Sidebar nav styling: make the mode radio read as intentional nav items, not a raw radio list —
# full-width padded rows, hover highlight, an accent-bar selected state, and the default radio dot
# hidden. ':has(input)' hides ONLY the control wrapper (never the label text); on a browser without
# :has the dot just stays visible, so it degrades gracefully.
_NAV_CSS = """<style>
section[data-testid="stSidebar"] div[role="radiogroup"]{ gap:.3rem; margin:.15rem 0 .4rem; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label{
  display:flex; align-items:center; width:100%; box-sizing:border-box;
  padding:.6rem .85rem; border-radius:9px; cursor:pointer;
  font-size:1.12rem; font-weight:600; line-height:1.2; color:#34566b;
  border-left:3px solid transparent;
  transition:background .15s ease, color .15s ease, border-color .15s ease; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover{
  background:rgba(52,86,107,.10); color:#34566b; }
/* The radio dot is the label's first child div (the text is a later child); hide it for a
   clean nav look. The <input> is a direct child of the label, so it stays for accessibility. */
section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child{
  display:none; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked){
  background:rgba(52,86,107,.14); color:#34566b; border-left-color:#34566b; }
</style>"""

# --- Brief render: block-card hierarchy ported from scripts/render_static.py ---
# Streamlit's st.markdown can't run the static renderer's client-side JS grouping
# pass (it strips <script>), so we reproduce md->HTML + the .block grouping here in
# Python and emit one raw-HTML string. CSS is scoped to #scout-brief and mirrors the
# static renderer 1:1 (left rail, larger title, indented body/so-what, block gaps).
_BRIEF_CSS = """<style>
#scout-brief { line-height: 1.55; }
#scout-brief h1 { font-size: 1.7rem; font-weight: 700; margin: .2rem 0 1.3rem; }
#scout-brief h2 { margin: 2.4rem 0 1rem; padding-bottom: .3rem; font-size: 1.4rem;
                  border-bottom: 1px solid rgba(136,136,136,.35); }
/* The brief follows the 5-minute block, so its first heading must not add a big top
   margin on top of the inter-block gap — keep it tight under the Top 3 plays. */
#scout-brief > h1:first-child, #scout-brief > h2:first-child,
#scout-brief > h3:first-child { margin-top: .2rem; }
#scout-brief h3 { margin: 1.5rem 0 .9rem; color: #5f5e54; text-transform: uppercase;
                  letter-spacing: .04em; font-size: 1.08rem; font-weight: 700; }
/* When a subsection heading sits directly under a section heading (e.g. "Competitive
   Battlecard" -> "Where … wins"), drop the big stacked top margin so they read together. */
#scout-brief h2 + h3 { margin-top: .7rem; }
#scout-brief .block { margin: 0 0 1.9rem; padding-left: .9rem;
                      border-left: 3px solid rgba(52,86,107,.5); }
#scout-brief .block .btitle { font-size: 1.3rem; font-weight: 700; line-height: 1.3;
                              margin: 0 0 .6rem; }
#scout-brief .block .bbody { margin: .55rem 0 .55rem 1.1rem; }
#scout-brief .block .bbody:last-child { margin-top: .7rem; opacity: .92; }
#scout-brief p { margin: .6rem 0; }
#scout-brief ul { padding-left: 1.2rem; margin: .6rem 0; }
#scout-brief li { margin-bottom: .4rem; }
#scout-brief a { color: #34566b; text-decoration: none; }
#scout-brief a:hover { text-decoration: underline; }
#scout-brief code { background: rgba(136,136,136,.2); padding: .1rem .3rem; border-radius: 4px; }
/* So what / Soundbite callout — same treatment as the Top 3 plays soundbite: a dashed
   separating line above, a green (non-italic) label, italic light-grey text. */
#scout-brief .scout-callout { display: block; margin: .7rem 0 0; padding-top: .55rem;
    border-top: 1px dashed rgba(136,136,136,.35); font-style: italic;
    color: #5f5e54; opacity: 1; }
#scout-brief li .scout-callout.co-inline { margin: .55rem 0 0; }
#scout-brief .co-lbl { color: #34566b; font-weight: 700; font-style: normal; }
#scout-brief .scout-callout em { color: inherit; font-style: italic; }
#scout-brief .btitle.has-persona { display:flex; justify-content:space-between;
    align-items:flex-start; gap:1rem; }
#scout-brief .scout-persona { flex:none; font-family:"IBM Plex Mono",ui-monospace,monospace;
    font-size:.6rem; font-weight:700; letter-spacing:.05em; text-transform:uppercase; color:#2a4658;
    background:rgba(52,86,107,.06); border:1px solid rgba(52,86,107,.24); border-radius:4px;
    padding:.14rem .42rem; margin-top:.3rem; white-space:nowrap; }
#scout-brief .scout-persona .pk { color:#908e82; margin-right:.25rem; }
</style>"""


def _inline_md(text: str) -> str:
    """Convert the inline markdown these briefs use (links, bold, italic, code) to
    HTML. Dollar signs become &#36; so Streamlit never reads `$…$` as LaTeX."""
    s = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)          # bold first
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)                    # then italic
    s = s.replace("\\$", "$").replace("$", "&#36;")
    return s


def _is_title(para: str) -> bool:
    """A paragraph that is a single full-line bold span — the block headline. Mirrors
    the static renderer's isTitle (a <p> whose only child is <strong>). `**So what:**
    rest` / `**Soundbite:** *"…"*` have trailing text, so they stay body."""
    p = para.strip()
    return p.startswith("**") and p.endswith("**") and p.count("**") == 2 and len(p) > 4


# So what / Soundbite are ALWAYS a callout, rendered identically everywhere: the label word
# in the accent green, the text after it in the default color. One set of helpers feeds every
# surface (full-brief paragraphs, full-brief bullets, the 5-minute Today's angle) so the three
# never drift. Briefs write the label two ways — bold `**So what:**` paragraphs, and a plain
# inline `So what for us:` trailer inside Recent-Moves/Pricing bullets — so we match both.
_CALLOUT_LABELS = r"So what for us|So what|Soundbite"           # longest alt first (greedy match)
_CALLOUT_PARA_RE = re.compile(r"^\*\*(" + _CALLOUT_LABELS + r"):\*\*\s*(.*)$", re.S)
_CALLOUT_INLINE_RE = re.compile(r"\s*\b(" + _CALLOUT_LABELS + r"):\s+")


def _callout_span(label: str, rest: str) -> str:
    """The shared inner markup: green label word + default-color text after it."""
    return f'<span class="co-lbl">{html.escape(label)}:</span> {_inline_md(rest)}'


def _maybe_callout_para(text: str, cls: str = "") -> str:
    """A body paragraph that IS a `**So what:**` / `**Soundbite:**` line -> a green-label
    callout; anything else -> a normal paragraph in the given class."""
    m = _CALLOUT_PARA_RE.match(text.strip())
    classes = (cls + " scout-callout").strip() if m else cls
    attr = f' class="{classes}"' if classes else ""
    if m:
        return f"<p{attr}>{_callout_span(m.group(1), m.group(2))}</p>"
    return f"<p{attr}>{_inline_md(text)}</p>"


def _bullet_html(text: str) -> str:
    """A bullet whose prose carries an inline 'So what[ for us]:' trailer -> split the
    trailer onto its own green-label callout line inside the <li>."""
    m = _CALLOUT_INLINE_RE.search(text)
    if m:
        main, rest = text[:m.start()].strip(), text[m.end():].strip()
        return (f"<li>{_inline_md(main)}"
                f'<span class="scout-callout co-inline">{_callout_span(m.group(1), rest)}</span></li>')
    return f"<li>{_inline_md(text)}</li>"


def _split_callout(text: str):
    """(main, label, rest) splitting an inline So what/Soundbite trailer out of prose, or
    (text, None, None) when there's none. Used by the 5-minute Today's angle."""
    m = _CALLOUT_INLINE_RE.search(text)
    if m:
        return text[:m.start()].strip(), m.group(1), text[m.end():].strip()
    return text, None, None


def _render_brief_html(md: str, personas: dict | None = None) -> str:
    """md -> raw HTML with the prose-block hierarchy. Each full-line-bold title plus
    its following body paragraphs is grouped into a .block; headings and bullet lists
    are breaks (never absorbed), matching scripts/render_static.py.

    `personas` (optional) maps a normalized block title -> (label, prefix) so battlecard
    plays and objections can show an audience badge (e.g. "Best for: Economic buyer").
    Built from claim objects by _persona_title_map; the self-serve path passes none, and
    cards generated before persona tagging simply match nothing — the badge is omitted."""
    # 1) tokenize into block-level elements (h1/h2/h3, ul, title, body paragraph)
    tokens, para, bullets = [], [], []
    def flush_para():
        if para:
            txt = " ".join(para).strip()
            tokens.append(("title" if _is_title(txt) else "body", txt))
            para.clear()
    def flush_bullets():
        if bullets:
            tokens.append(("ul", list(bullets)))
            bullets.clear()
    for line in md.splitlines():
        s = line.rstrip()
        if not s.strip():
            flush_para(); flush_bullets(); continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush_para(); flush_bullets()
            tokens.append((f"h{len(m.group(1))}", m.group(2).strip())); continue
        if s.lstrip().startswith("- "):
            flush_para()
            bullets.append(s.lstrip()[2:].strip()); continue
        flush_bullets()
        para.append(s.strip())
    flush_para(); flush_bullets()

    # 2) emit, grouping each title + its trailing body paragraphs into a .block
    out, i = [], 0
    while i < len(tokens):
        kind, val = tokens[i]
        if kind == "title":
            ttext = val[2:-2].strip()
            hit = (personas or {}).get(_norm_title(ttext))
            badge = ""
            if hit:
                label, prefix = hit
                badge = (f'<span class="scout-persona"><span class="pk">{html.escape(prefix)}</span> '
                         f'{html.escape(label)}</span>')
            cls = "btitle has-persona" if badge else "btitle"
            parts = [f'<p class="{cls}">{_inline_md(ttext)}{badge}</p>']
            i += 1
            while i < len(tokens) and tokens[i][0] == "body":
                parts.append(_maybe_callout_para(tokens[i][1], cls="bbody"))
                i += 1
            out.append(f'<div class="block">{"".join(parts)}</div>')
        elif kind == "ul":
            out.append("<ul>" + "".join(_bullet_html(b) for b in val) + "</ul>")
            i += 1
        elif kind.startswith("h"):
            out.append(f'<{kind} id="{_slug(val)}">{_inline_md(val)}</{kind}>')
            i += 1
        else:  # a stray body paragraph not preceded by a title
            out.append(_maybe_callout_para(val))
            i += 1
    return '<div id="scout-brief">' + "".join(out) + "</div>"

def _pretty(slug: str) -> str:
    return slug.replace("__vs__", " vs ").replace("__", " · ").replace("-", " ")


def _read_current(slug: str) -> str:
    path = os.path.join(store.battlecard_dir(slug), "current.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return "_No rendered battlecard found for this slug._"


def _asset(*names):
    here = os.path.dirname(os.path.abspath(__file__))
    for n in names:
        p = os.path.join(here, "assets", n)
        if os.path.exists(p):
            return p
    return None


# --- The 5-minute brief: a DERIVED top layer ---------------------------------
# Pure selection/reorganization of already-verified claims. It generates NO new
# prose: today's angle is the freshest recent-move claim shown verbatim with its
# source; the Top 3 plays are the battlecard "where we win" claims shown in full —
# bold title, the why (their body), and their existing soundbite — all verbatim.
_FIVE_MIN_CSS = """<style>
#scout-rt { margin:-.9rem 0 1.1rem; }
#scout-rt h1 { font-size:2.3rem; font-weight:800; line-height:1.12; margin:0 0 .45rem; }
#scout-rt .rt-sub { font-size:1.5rem; font-weight:600; line-height:1.3; margin:.1rem 0; }
#scout-rt .rt-sub b { font-weight:800; }
#scout-rt .rt-focus { font-size:1.4rem; font-weight:600; color:#34566b; margin-top:.2rem; }
#scout-5min { margin:.2rem 0 .3rem; }
#scout-5min .fm-lbl { color:#34566b; font-size:.78rem; font-weight:700; text-transform:uppercase;
                      letter-spacing:.06em; margin:0 0 .55rem; }
#scout-5min .fm-angle { border-left:3px solid rgba(52,86,107,.6); padding:.1rem 0 .1rem .9rem;
                        margin-bottom:1.6rem; }
#scout-5min .fm-atext { font-size:1.12rem; line-height:1.5; margin:.1rem 0 .5rem; }
#scout-5min .fm-sowhat { font-size:1rem; line-height:1.5; margin:.5rem 0 .6rem; padding-top:.5rem;
                         border-top:1px dashed rgba(136,136,136,.35); font-style:italic; color:#5f5e54; }
#scout-5min .co-lbl { color:#34566b; font-weight:700; font-style:normal; }
#scout-5min .fm-src { font-size:.8rem; color:#908e82; }
#scout-5min .fm-src a, #scout-5min a { color:#34566b; text-decoration:none; }
#scout-5min .fm-src a:hover, #scout-5min a:hover { text-decoration:underline; }
#scout-5min .fm-plays { display:grid; gap:1rem; }
#scout-5min .fm-play { border:1px solid rgba(136,136,136,.25);
                       border-left:3px solid rgba(52,86,107,.5); border-radius:10px; padding:.85rem 1.05rem; }
#scout-5min .fm-pt { font-size:1.1rem; font-weight:700; line-height:1.3; margin-bottom:.35rem; }
#scout-5min .fm-pwhy { margin:.25rem 0 .55rem; line-height:1.45; }
#scout-5min .fm-sb { margin:.5rem 0 0; padding-top:.5rem; border-top:1px dashed rgba(136,136,136,.35);
                     font-size:.93rem; font-style:italic; color:#5f5e54; }
#scout-5min .fm-sb em { color:inherit; }
#scout-5min .fm-sb-lbl { font-weight:700; color:#34566b; font-style:normal; }
#scout-5min .fm-ptop { display:flex; justify-content:space-between; align-items:flex-start; gap:.7rem; }
#scout-5min .fm-ptop .fm-pt { margin-bottom:0; }
#scout-5min .fm-persona { flex:none; font-size:.62rem; font-weight:700; letter-spacing:.05em;
    text-transform:uppercase; color:#2a4658; background:rgba(52,86,107,.06);
    border:1px solid rgba(52,86,107,.24); border-radius:4px; padding:.15rem .45rem; margin-top:.12rem;
    white-space:nowrap; }
#scout-5min .fm-persona .pk { color:#908e82; margin-right:.25rem; }
</style>"""

# Persona enum (schema.PERSONAS) -> the rep-facing label rendered in the audience badge.
_PERSONA_LABELS = {
    "eng_led": "Eng-led champion",
    "technical_evaluator": "Technical evaluator",
    "economic_buyer": "Economic buyer",
    "security_regulated": "Security & regulated",
    "exec_top_down": "Exec / top-down",
}


def _persona_badge(claim: dict, prefix: str) -> str:
    """Audience badge for a play/objection, or '' when the claim carries no persona (older
    cards generated before persona tagging — degrade silently, never render an empty pill)."""
    label = _PERSONA_LABELS.get(claim.get("persona"))
    if not label:
        return ""
    return (f'<span class="fm-persona"><span class="pk">{html.escape(prefix)}</span> '
            f'{html.escape(label)}</span>')


def _norm_title(s: str) -> str:
    """Loose key for matching a rendered brief block title back to its claim."""
    return re.sub(r"\s+", " ", s.strip().lower())


def _persona_title_map(claims: list[dict]) -> dict:
    """{normalized title -> (label, prefix)} for battlecard plays + objections, so the
    full-brief renderer can badge those blocks by audience. Other sections and untagged
    claims are skipped (their titles simply won't match)."""
    out = {}
    for c in claims:
        sec, label = c.get("section"), _PERSONA_LABELS.get(c.get("persona"))
        if not label or sec not in ("battlecard", "objection_handling"):
            continue
        title = _parse_claim(c).get("title", "").strip()
        if title:
            out[_norm_title(title)] = (label, "Raised by" if sec == "objection_handling" else "Best for")
    return out


def _parse_claim(c: dict) -> dict:
    """Split a verified claim's markdown into its parts WITHOUT rewriting them: the
    bold headline, body paragraph(s), the **So what:** play line, and the
    **Soundbite:** line. The 5-minute view reuses these strings verbatim."""
    out = {"title": "", "body": [], "so_what": "", "soundbite": ""}
    for p in (s.strip() for s in c.get("claim", "").split("\n\n")):
        if not p:
            continue
        if p.startswith("**So what:**"):
            out["so_what"] = p[len("**So what:**"):].strip()
        elif p.startswith("**Soundbite:**"):
            out["soundbite"] = p[len("**Soundbite:**"):].strip()
        elif _is_title(p):
            out["title"] = p[2:-2].strip()
        else:
            out["body"].append(p)
    return out


def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return m.group(1).replace("www.", "") if m else "source"


def _five_min_html(claims: list[dict]) -> str:
    by_order = lambda lst: sorted(lst, key=lambda c: c.get("order", 0))

    # Today's angle — the freshest recent move, pointed at its claim + source.
    moves = [c for c in claims if c.get("section") == "recent_moves"]
    angle_html = ""
    if moves:
        a = max(moves, key=lambda c: (c.get("as_of") or "", -c.get("order", 0)))
        pa = _parse_claim(a)
        # Recent-move claims carry their takeaway as an inline 'So what for us:' trailer; pull
        # it onto its own callout line so the angle reads like the brief's So-what blocks.
        main, sw_label, sw_rest = _split_callout(" ".join(pa["body"]))
        inner = (f'<strong>{_inline_md(pa["title"])}</strong> {_inline_md(main)}'
                 if pa["title"] else _inline_md(main))
        sw_html = (f'<p class="fm-sowhat">{_callout_span(sw_label, sw_rest)}</p>'
                   if sw_label else "")
        src, asof = a.get("source_url", ""), a.get("as_of", "")
        src_line = (f'<div class="fm-src"><b>{html.escape(asof)}</b> · '
                    f'<a href="{html.escape(src)}" target="_blank" rel="noopener">'
                    f'{html.escape(_domain(src))}</a></div>') if src else ""
        angle_html = ('<div class="fm-angle"><div class="fm-lbl">Today\'s angle</div>'
                      f'<p class="fm-atext">{inner}</p>{sw_html}{src_line}</div>')

    # Top 3 plays — the battlecard "where we win" claims, shown in full: each carries
    # a bold title, the why (its body), and its existing soundbite. All verbatim.
    wins = by_order([c for c in claims
                     if c.get("section") == "battlecard" and c.get("zone") == "where_we_win"])[:3]
    plays = []
    for c in wins:
        p = _parse_claim(c)
        why = f'<p class="fm-pwhy">{_inline_md(" ".join(p["body"]))}</p>' if p["body"] else ""
        sb = (f'<p class="fm-sb"><span class="fm-sb-lbl">Soundbite:</span> '
              f'{_inline_md(p["soundbite"])}</p>') if p["soundbite"] else ""
        badge = _persona_badge(c, "Best for")
        title = (f'<div class="fm-ptop"><div class="fm-pt">{_inline_md(p["title"])}</div>{badge}</div>'
                 if badge else f'<div class="fm-pt">{_inline_md(p["title"])}</div>')
        plays.append(f'<div class="fm-play">{title}{why}{sb}</div>')
    plays_html = ('<div class="fm-lbl">Top 3 plays</div>'
                  f'<div class="fm-plays">{"".join(plays)}</div>') if plays else ""

    return '<div id="scout-5min">' + angle_html + plays_html + '</div>'


def _slug(text: str) -> str:
    """Stable heading id: lowercase, punctuation dropped, spaces -> hyphens. Used
    identically by the brief headings and the TOC links so anchors line up."""
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", s)


def _strip_h1(md: str) -> str:
    """Remove the leading '# …' report title — it's rendered separately, above the
    5-minute brief, so it must not repeat at the top of the full brief."""
    return re.sub(r"^#\s+.*$\n?", "", md, count=1, flags=re.M)


def _report_title_html(md: str, meta: dict | None) -> str:
    """The master report title, shown above Today's angle. Spelled out so it's crystal
    clear who/what the card covers: a big 'Competitive Intelligence Brief' headline, a
    'Researched: <competitor> · For <my company> reps' line, and a focus line when set.
    Falls back to the brief's own H1 if meta lacks the fields."""
    meta = meta or {}
    competitor = (meta.get("competitor") or "").strip()
    my_company = (meta.get("my_company") or "").strip()
    focus = (meta.get("focus") or "").strip()
    if not competitor:                       # no structured meta — use the brief's H1 verbatim
        m = re.search(r"^#\s+(.*)$", md, flags=re.M)
        title = m.group(1).strip() if m else "Competitive Intelligence Brief"
        return f'<div id="scout-rt"><h1>{html.escape(title)}</h1></div>'
    sub = f'Researched: <b>{html.escape(competitor)}</b>'
    if my_company:
        sub += f' · For <b>{html.escape(my_company)}</b> reps'
    focus_html = (f'<div class="rt-focus">Focus area: {html.escape(focus)}</div>'
                  if focus else "")
    return ('<div id="scout-rt"><h1>Competitive Intelligence Brief</h1>'
            f'<div class="rt-sub">{sub}</div>{focus_html}</div>')


_TOC_CSS = """<style>
#scout-toc { margin-bottom:.4rem; }
#scout-toc .toc-lbl { color:#34566b; font-size:.78rem; font-weight:700; text-transform:uppercase;
                      letter-spacing:.06em; margin-bottom:.45rem; }
#scout-toc ul { list-style:none; margin:0; padding:0; border-left:2px solid rgba(136,136,136,.2); }
#scout-toc li a { display:block; padding:.2rem 0 .2rem .75rem; margin-left:-2px; color:inherit;
                  text-decoration:none; font-size:.9rem; line-height:1.35;
                  border-left:2px solid transparent; }
#scout-toc li a:hover { color:#34566b; border-left-color:#34566b; }
#scout-toc .toc-h3 a { padding-left:1.5rem; font-size:.82rem; color:#908e82; }
/* On mobile Streamlit stacks the columns, so this right-rail jump-nav lands stranded at the
   very bottom of the page where it's useless — hide it there; you just scroll the brief. */
@media (max-width:520px){ #scout-toc { display:none; } }
</style>"""


def _toc_html(md: str) -> str:
    """Table of contents from the brief's ## / ### headings, each linking to the
    matching heading id. Pure navigation over the existing sections."""
    items = []
    for line in md.splitlines():
        m = re.match(r"^(##|###)\s+(.*)$", line)
        if not m:
            continue
        text = m.group(2).strip()
        cls = "toc-h2" if m.group(1) == "##" else "toc-h3"
        items.append(f'<li class="{cls}"><a href="#{_slug(text)}">{html.escape(text)}</a></li>')
    if not items:
        return ""
    return ('<div id="scout-toc"><div class="toc-lbl">Contents</div><ul>'
            + "".join(items) + "</ul></div>")


def _fmt_date_human(s: str | None) -> str:
    if not s:
        return "—"
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return s


# Metrics strip: compact cards whose timestamps are localized to the VIEWER's
# timezone client-side (server runs UTC and can't know it). Naive ISO values are
# treated as UTC. The Next-check card also carries the live countdown (A2).
_METRICS_STRIP = """<div id="ms">
<style>
 html,body{margin:0;padding:0;background:#f4f2ec;}
 /* This iframe is sandboxed so it does NOT inherit the Streamlit theme. The page is pinned
    light (.streamlit/config.toml [theme] base=light), so pin the iframe light too — cream bg,
    dark ink text — else its values render with the visitor's OS scheme and can vanish. */
 #ms{font-family:-apple-system,system-ui,"Segoe UI",Roboto,sans-serif;color:#1c1d16;}
 #ms .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:.6rem;}
 /* On a narrow (mobile) viewport, 4 cramped columns clip their values — go 2x2 instead. */
 @media (max-width:520px){ #ms .grid{grid-template-columns:repeat(2,1fr);} }
 #ms .m{background:#fbfaf6;border:1px solid rgba(136,136,136,.35);border-radius:7px;padding:.5rem .8rem .6rem;
        min-height:52px;display:flex;flex-direction:column;justify-content:center;}
 #ms .ml{color:#908e82;font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;}
 #ms .mv{font-size:1.05rem;font-weight:700;line-height:1.15;margin-top:.15rem;}
 #ms .mv .t{display:block;font-size:.82rem;font-weight:500;color:#5f5e54;margin-top:.12rem;}
 #ms .cd{color:#34566b;font-weight:700;font-size:.78rem;font-variant-numeric:tabular-nums;margin-top:.3rem;}
 #ms .cd.due{color:#8a6322;}
 #ms .sub{color:#908e82;font-size:.72rem;margin-top:.3rem;}
 #ms .mlink{color:inherit;text-decoration:none;border-bottom:2px solid rgba(52,86,107,.55);}
 #ms .mlink:hover{border-bottom-color:#34566b;}
 #ms .sub a{color:#34566b;text-decoration:none;}
 #ms .sub a:hover{text-decoration:underline;}
</style>
<div class="grid">
 <div class="m"><div class="ml">Last updated</div><div class="mv" data-utc="__LAST__">—</div></div>
 <div class="m"><div class="ml">Next update</div><div class="mv" data-utc="__NEXT__">—</div><div class="cd" id="cd"></div></div>
 <div class="m"><div class="ml">Baseline</div><div class="mv">__BASE__</div></div>
 <div class="m"><div class="ml">Claims tracked &amp; verified</div><div class="mv"><a class="mlink" href="#verified-claims" target="_parent">__N__</a></div><div class="sub"><a href="#verified-claims" target="_parent">see all ↓</a></div></div>
</div>
<script>
 function fmt(el){var iso=el.getAttribute('data-utc');
  if(!iso||iso==='None'||iso===''){el.textContent='—';return;}
  var d=new Date(/[zZ]|[+-]\\d\\d:?\\d\\d$/.test(iso)?iso:iso+'Z');
  if(isNaN(d)){el.textContent=iso;return;}
  var dt=d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
  var tm=d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit',timeZoneName:'short'});
  el.innerHTML=dt+'<span class="t">'+tm+'</span>';}
 document.querySelectorAll('#ms .mv[data-utc]').forEach(fmt);
 var r=__R__,t0=performance.now(),cd=document.getElementById('cd');
 function tick(){var l=r-(performance.now()-t0)/1000;
  if(l<=0){cd.textContent='update due now';cd.classList.add('due');return;}
  var h=Math.floor(l/3600),m=Math.floor(l%3600/60),s=Math.floor(l%60);
  cd.textContent='next update in '+h+'h '+String(m).padStart(2,'0')+'m '+String(s).padStart(2,'0')+'s';}
 tick();setInterval(tick,1000);
 // The strip is sandboxed (no top-navigation), but allow-same-origin lets us reach
 // the parent DOM — so jump to the claims list by scrolling the parent element.
 document.querySelectorAll('#ms a[href="#verified-claims"]').forEach(function(a){
  a.addEventListener('click',function(ev){ev.preventDefault();
   try{var t=window.parent.document.getElementById('verified-claims');
       if(t)t.scrollIntoView({behavior:'smooth',block:'start'});}catch(e){}});});
 // Auto-size the iframe to its content so the cards are never clipped: on mobile the grid
 // wraps to 2x2 (taller), on desktop it's one row (shorter). Same-origin lets us set our own
 // frame height; ResizeObserver re-fits when the timezone text fills in or the layout reflows.
 function fitFrame(){try{var el=document.getElementById('ms');if(!el||!window.frameElement)return;
   var h=(el.scrollHeight+12)+'px';
   // Size the iframe AND its immediate per-element wrapper (stElementContainer) to the content,
   // so the reserved block matches: a tall mobile 2x2 no longer overflows the divider, a short
   // desktop row leaves no gap. Stop at the wrapper — the block container above holds OTHER
   // elements and must keep auto height (setting it collapses the rest of the page).
   window.frameElement.style.height=h;
   var w=window.frameElement.parentElement;          // stElementContainer
   if(w)w.style.height=h;}catch(e){}}
 window.addEventListener('resize',fitFrame);
 if(window.ResizeObserver){new ResizeObserver(fitFrame).observe(document.getElementById('ms'));}
 setTimeout(fitFrame,50);fitFrame();
</script>
</div>"""


# LIVE box — the agentic story, shown above the fold. Pulsing dot + what the orchestra is.
# The live claim count + last-checked time live in the metric strip below, so this stays a
# one-liner about WHAT we are, not a duplicate of the counts. Rendered via st.markdown (not a
# sandboxed iframe), so the ::after pulse ring + keyframes animate in the main document.
_LIVE_BOX = """<style>
#scout-live{display:flex;justify-content:flex-end;margin:.1rem 0 .25rem;}
#scout-live .box{border:1px solid rgba(136,136,136,.35);border-radius:7px;background:#fbfaf6;
                 padding:.5rem .8rem;max-width:520px;text-align:right;}
#scout-live .lv{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.72rem;font-weight:700;
                letter-spacing:.06em;color:#2f6149;display:inline-flex;align-items:center;gap:.45rem;}
#scout-live .pulse{width:7px;height:7px;border-radius:50%;background:#2f6149;position:relative;flex:none;}
#scout-live .pulse::after{content:"";position:absolute;inset:-3px;border-radius:50%;
                          border:1px solid #2f6149;animation:scoutping 1.9s ease-out infinite;}
@keyframes scoutping{0%{transform:scale(.6);opacity:.85}100%{transform:scale(2.4);opacity:0}}
#scout-live .agents{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.68rem;
                    color:#5f5e54;margin-top:.35rem;line-height:1.45;}
</style>
<div id="scout-live"><div class="box">
  <div class="lv"><span class="pulse"></span>LIVE</div>
  <div class="agents">Monitored by a team of specialized agents (Sonnet &amp; Haiku) + 1 orchestrator (Opus).</div>
</div></div>"""


def _phase_message(frac: float) -> str:
    """Pick a rotating status line for where the timed estimate currently sits. Stable per
    ~20s tick so it doesn't flicker on every 6s poll rerun."""
    if frac < 0.40:
        pool = PROGRESS_MESSAGES["research"]
    elif frac < 0.70:
        pool = PROGRESS_MESSAGES["draft"]
    elif frac < 0.92:
        pool = PROGRESS_MESSAGES["verify"]
    else:
        pool = PROGRESS_MESSAGES["final"]
    return pool[int(time.time() // 20) % len(pool)]


def _render_job_status(job_id: str) -> None:
    """Show a self-serve job: a timed-estimate progress bar while pending, the rendered card
    when done, or the gate message if it was rejected. Polls by sleeping then rerunning."""
    res = selfserve.get_result(job_id)
    if res is None:
        started = st.session_state.setdefault(f"job_start_{job_id}", time.time())
        elapsed = time.time() - started
        frac = min(elapsed / _SELFSERVE_ESTIMATE_S, 0.99)
        st.info("**This usually takes ~6–8 minutes.** Keep this tab open, or bookmark this URL "
                "and come back — your report will be here when it's done.")
        st.progress(frac)
        st.markdown("*" + _phase_message(frac) + "*")
        st.caption(f"Job `{job_id}` · elapsed {int(elapsed // 60)}m {int(elapsed % 60)}s")
        time.sleep(6)
        st.rerun()
        return
    status = res.get("status")
    if status == "done":
        st.success("Your report is ready.")
        md = res.get("markdown", "")
        st.markdown("---")
        st.markdown(_BRIEF_CSS, unsafe_allow_html=True)
        st.markdown(_render_brief_html(md), unsafe_allow_html=True)
        st.download_button("Download report (.md)", data=md,
                           file_name=f"{job_id}.md", mime="text/markdown")
    elif status == "rejected":
        st.warning(res.get("message", "The free window is closed."))
        st.markdown(f"**For access, {_contact_md()}.**")
    else:
        st.error(res.get("message", "Something went wrong generating this report."))


def _contact_md() -> str:
    """Markdown for the 'get in touch' link: 'DM me on LinkedIn' when AUTHOR_LINKEDIN is
    configured, else a fallback to the contact email. Reused by every access/contact prompt."""
    if config.AUTHOR_LINKEDIN:
        return f"[DM me on LinkedIn]({config.AUTHOR_LINKEDIN})"
    return f"email **{config.SELFSERVE_CONTACT}**"


def _utc_attr(s) -> str:
    """Tag a naive-UTC ISO timestamp with 'Z' so the browser renders it in the viewer's LOCAL
    time. Our timestamps are written with datetime.now() on UTC runners (Actions/Streamlit) —
    correct but unlabeled; without the 'Z' new Date() misreads them as local and shifts the
    displayed time. Date-only / already-zoned values are passed through untouched."""
    s = str(s or "")
    if "T" in s and not s.endswith("Z") and "+" not in s:
        return s + "Z"
    return s


def _render_selfserve(job_param: str | None) -> None:
    """The 'Create your own' entry point: gate -> form -> async job view. User-generated cards
    are saved to user_reports/ (private), never the public showcase or the monitor."""
    st.subheader("Create your own battlecard")

    # A job id (from the URL ?job= or this session) takes you straight to its status/result.
    job_id = job_param or st.session_state.get("selfserve_job")
    if job_id:
        if not selfserve.valid_job_id(job_id):
            st.error("That job link looks malformed.")
            return
        st.session_state["selfserve_job"] = job_id
        _render_job_status(job_id)
        if st.button("← Start another report"):
            for k in [k for k in st.session_state if k.startswith("job_start_")]:
                st.session_state.pop(k, None)
            st.session_state.pop("selfserve_job", None)
            st.query_params.clear()
            st.rerun()
        return

    try:
        gate = selfserve.gate()
    except Exception:
        # The app-side gate is advisory only (the GitHub Action enforces the real spend cap),
        # so a backend read error — e.g. an expired SELFSERVE_GH_TOKEN — must degrade gracefully
        # rather than crash the page with a raw traceback on a public showcase.
        st.warning("Create-your-own is temporarily unavailable — please check back shortly.")
        st.markdown(f"In the meantime, {_contact_md()}.")
        return
    if not gate["open"]:
        st.warning("The free launch window is full.")
        st.markdown(f"**For access, {_contact_md()}.**")
        return

    st.caption(f"**{gate['free_left']} free reports left.** Two companies, optional focus. "
               "We research, verify every claim against its source, then show you the card.")
    competitor = st.text_input("Competitor to research (required)", placeholder="e.g. OpenAI")
    my_company = st.text_input("Your company (optional)", placeholder="e.g. Anthropic")
    focus = st.text_input("Focus area (optional)", placeholder="e.g. enterprise coding")
    if st.button("Generate my report", type="primary"):
        if not competitor.strip():
            st.warning("Please enter a competitor to research.")
            return
        # Soft per-session throttle (a 60s cooldown + a 3-per-session cap). This only blunts
        # double-clicks and casual spamming of the public form from one browser session — it is
        # NOT a security boundary (a determined actor can open new sessions). The HARD backstop
        # against runaway cost is the server-side gate: the free-window count + the $100 spend
        # ceiling, enforced authoritatively in the Action (selfserve.gate / state.json).
        hist = st.session_state.setdefault("_submit_times", [])
        now_dt = datetime.now()
        if hist and (now_dt - hist[-1]).total_seconds() < 60:
            wait = 60 - int((now_dt - hist[-1]).total_seconds())
            st.warning(f"Easy there — one report at a time. Try again in {wait}s.")
            return
        if len(hist) >= 3:
            st.warning("You've reached this session's limit of 3 reports.")
            st.markdown(f"Want more? {_contact_md()}.")
            return
        if not selfserve.gate()["open"]:                # re-check; the view can be stale
            st.warning("The free window just closed. For access, see the DM link below.")
            return
        req = selfserve.submit(competitor, my_company, focus)
        hist.append(now_dt)
        st.session_state["selfserve_job"] = req["job_id"]
        st.query_params["job"] = req["job_id"]
        st.rerun()


def _st_masthead(head_logo):
    """Branded Agent Scout header for the self-serve surface. The living-battlecard VIEWER
    renders its own masthead inside the page iframe, so this is NOT used there."""
    if head_logo:
        with open(head_logo, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        st.markdown(
            '<div style="display:flex;align-items:center;gap:.45rem;margin:.2rem 0 .4rem;">'
            f'<img src="data:image/png;base64,{b64}" style="height:88px;width:auto;" alt="Agent Scout">'
            '<span style="font-size:2.6rem;font-weight:800;line-height:1;">Agent Scout</span>'
            '</div>', unsafe_allow_html=True)
    else:
        st.title("Agent Scout")
    st.markdown(
        '<div style="color:#34566b;font-size:1.08rem;font-weight:600;line-height:1.4;'
        'margin:-.1rem 0 .5rem;">Living competitive battlecards: Every claim verified '
        'for accuracy and kept current by an orchestra of AI agents.</div>',
        unsafe_allow_html=True)


# Font <link>s, inlined here (not page.FONT_HEAD): app_v2 is the always-reloaded main script,
# so referencing a fresh page.py constant can hit Streamlit Cloud's stale module cache and crash.
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,ital,'
    'wght@9..144,0,400;9..144,0,500;9..144,0,600;9..144,1,400;9..144,1,500'
    '&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">')


# In-page mode switch styled as a sober segmented control (replaces the sidebar radio).
_MODE_CSS = (
    'div[role="radiogroup"]{gap:.5rem;margin:.1rem 0 .9rem;}'
    'div[role="radiogroup"]>label{border:1px solid #dfdbcf;background:#fbfaf6;border-radius:7px;'
    'padding:.4rem .95rem;font-weight:600;color:#5f5e54;cursor:pointer;'
    'transition:border-color .15s,color .15s;}'
    'div[role="radiogroup"]>label:hover{border-color:#34566b;color:#34566b;}'
    'div[role="radiogroup"]>label:has(input:checked){border-color:#34566b;color:#2a4658;'
    'background:rgba(52,86,107,.08);box-shadow:inset 0 0 0 1px #34566b;}'
    'div[role="radiogroup"]>label>div:first-child{display:none;}'
)


def _footer():
    credit = (f"[{config.AUTHOR_NAME}]({config.AUTHOR_LINKEDIN})"
              if config.AUTHOR_LINKEDIN else config.AUTHOR_NAME)
    st.caption(f"Built by {credit}")


def main():
    icon = _asset("scout_icon_t.png", "scout_icon.png")
    st.set_page_config(page_title="Agent Scout — Living Battlecards", layout="wide",
                       page_icon=icon or "🐕", initial_sidebar_state="collapsed")
    # Center the content to the same 1240px as the page's own .wrap (so masthead, the mode
    # switch, the card picker and the brief all line up), pull it up, and remove the sidebar
    # entirely — navigation lives IN the page now.
    st.markdown(
        '<style>[data-testid="stMainBlockContainer"],[data-testid="stAppViewBlockContainer"],'
        'section.main>div.block-container,.block-container{max-width:1240px!important;'
        'margin-left:auto!important;margin-right:auto!important;padding:2.2rem 2rem 3rem!important;}'
        '[data-testid="stSidebar"],[data-testid="collapsedControl"]{display:none!important;}'
        + _MODE_CSS + '</style>', unsafe_allow_html=True)

    job_param = st.query_params.get("job")
    # Fonts (separate <link>), CSS, and masthead each go in their OWN st.markdown call —
    # Streamlit's sanitizer drops a <style> bundled with body HTML (or one containing @import),
    # which is what garbled the earlier cuts.
    st.markdown(_FONT_LINKS, unsafe_allow_html=True)
    st.markdown(page.style_block(), unsafe_allow_html=True)
    st.markdown(page.masthead_html(), unsafe_allow_html=True)

    # Mode switch — in the page, right below the masthead (no sidebar). A ?job= deep link
    # (returning self-serve visitor) opens the create surface.
    mode = st.radio("Mode", ["📋 Living battlecards", "✨ Create your own"],
                    index=1 if job_param else 0, horizontal=True, label_visibility="collapsed")

    if job_param or mode == "✨ Create your own":
        _render_selfserve(job_param)
        _footer()
        return

    cards = display.list_battlecards()
    if not cards:
        st.info("No battlecards have been generated yet.")
        return
    slug = st.selectbox("Choose a battlecard", cards, format_func=_pretty,
                        label_visibility="collapsed")
    # Land at the top of a newly selected card (Streamlit preserves scroll across reruns).
    if st.session_state.get("_last_slug") != slug:
        st.session_state["_last_slug"] = slug
        components.html("<script>window.parent.scrollTo({top:0});</script>", height=0)
    # The battlecard body, rendered INLINE in Streamlit's own document (scroll + #anchor jumps
    # work natively; an iframe broke both on Streamlit Cloud). CSS scoped to #scout-page.
    st.markdown(page.content_html(slug), unsafe_allow_html=True)
    _footer()


if __name__ == "__main__":
    main()
