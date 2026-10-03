"""Data-driven renderer for the living-battlecard page.

The viewer must look EXACTLY like the approved mockup (docs/mockups/command-center.html).
Styling Streamlit's own widgets produced a hybrid (its fonts/spacing/sidebar bled through);
rendering inside a components.html IFRAME looked right but broke on Streamlit Cloud, where the
iframe is cross-origin — height auto-fit and anchor-scroll can't reach the parent, so the page
wouldn't scroll and TOC links went blank.

So we render the whole battlecard INLINE, as one big HTML string injected with
st.markdown(unsafe_allow_html=True): same document as the page, so scrolling and #anchor jumps
work natively, no JS required. To survive that path the markup is kept to tags Streamlit's
sanitizer passes (div/span/p/a/h1-4/ul/li/strong/em/table/style/img — no <details>/<script>),
sections are always-open cards, and every '$' is escaped so Streamlit never reads '$…$' as LaTeX.

Styling source of truth: the <style> block is read verbatim from the mockup file, plus a small
override block (section cards, fonts via @import, a wider 2-col breakpoint). Pure module — no
Streamlit import — so the same output can be wrapped into a static preview file.
"""
import html as _html
import os
import re
from datetime import datetime, timedelta, timezone

import contextvars

from scout import config, display, store
from scout.sources import classify as _classify
from scout.schema import PERSONAS as _PERSONAS

# The persona the reader chose (?persona=<key>, WS0 2026-09-28): that persona's plays and
# objections render first and the rest dim; None = the default order. A context variable so the
# choice is scoped to one request and never leaks between concurrent renders.
_PERSONA = contextvars.ContextVar("scout_persona", default=None)


def persona_or_none(value) -> str | None:
    return value if value in _PERSONAS else None


def _pkey(c: dict):
    """Sort key: the chosen persona's items first, then the stored order."""
    p = _PERSONA.get()
    return (0 if (p and c.get("persona") == p) else 1, c.get("order", 0))


# Audience mode, Level 1 (2026-10-02 evening): the chosen buyer's items lead, other buyers' items
# fold under a labelled handle, untagged items stay visible. Facts are never hidden. Section order
# shifts to what that buyer reads first.
_PERSONA_SECTION_ORDER = {
    "economic_buyer":      ["executive_summary", "snapshot", "pricing", "battlecard", "objection_handling", "recent_moves", "positioning", "sentiment"],
    "exec_top_down":       ["executive_summary", "snapshot", "recent_moves", "sentiment", "battlecard", "objection_handling", "positioning", "pricing"],
    "technical_evaluator": ["executive_summary", "snapshot", "positioning", "battlecard", "objection_handling", "pricing", "recent_moves", "sentiment"],
    "security_regulated":  ["executive_summary", "snapshot", "objection_handling", "battlecard", "positioning", "recent_moves", "pricing", "sentiment"],
    "eng_led":             ["executive_summary", "snapshot", "positioning", "battlecard", "recent_moves", "objection_handling", "pricing", "sentiment"],
}


# Sections a buyer reads first, kept open with that audience even without tagged items.
_PERSONA_OPEN = {"economic_buyer": ("pricing",), "technical_evaluator": ("positioning",),
                 "exec_top_down": ("recent_moves",), "eng_led": ("positioning",), "security_regulated": ()}


def _section_order() -> list:
    p = _PERSONA.get()
    return _PERSONA_SECTION_ORDER.get(p, _SECTION_ORDER) if p else _SECTION_ORDER


def _split_for_audience(cs: list) -> tuple[list, list]:
    """(shown, folded): with no audience everything is shown; with one, items tagged to OTHER
    buyers fold, the buyer's own and untagged items stay, the buyer's first."""
    p = _PERSONA.get()
    if not p:
        return list(cs), []
    shown = [c for c in cs if not c.get("persona") or c.get("persona") == p]
    folded = [c for c in cs if c.get("persona") and c.get("persona") != p]
    return sorted(shown, key=_pkey), folded


def _top_wins(claims: list, n: int = 3) -> list:
    """The plays the briefing leads with: with an audience, that buyer's own first, then untagged
    ones, then other buyers' only to fill."""
    wins_all = sorted([c for c in claims if c.get("section") == "battlecard"
                       and c.get("zone") == "where_we_win"], key=_pkey)
    p = _PERSONA.get()
    if not p:
        return wins_all[:n]
    # the buyer's own plays only (2026-10-02: filling from other audiences read as no filter at all)
    return [c for c in wins_all if c.get("persona") == p][:n]


def _pulled_up_ids(claims: list) -> set:
    """With an audience, the claims the briefing shows at the top (the buyer's top plays and
    objections); the sections below leave them out, so nothing appears twice (2026-10-02)."""
    p = _PERSONA.get()
    if not p:
        return set()
    ids = {c.get("id") for c in _top_wins(claims)}
    objs = sorted([c for c in claims if c.get("section") == "objection_handling" and c.get("persona") == p],
                  key=lambda c: c.get("order", 0))[:4]
    return ids | {c.get("id") for c in objs}


def _fold(items: list, noun: str) -> str:
    if not items:
        return ""
    n = len(items)
    return ('<details class="fold"><summary><span class="lbl-more">'
            f'{n} more {noun[:-1] if n == 1 else noun} for other audiences</span><span class="mchev">&#9662;</span></summary>'
            f'<div class="rest">{"".join(items)}</div></details>')


def _pdim(c: dict) -> str:
    """Kept as a no-op: the audience view once dimmed other personas' items to 55% with a hover
    un-dim; Uroš (2026-09-29) called it unwanted. Picking an audience REORDERS, never greys out."""
    return ""

# Rotating build-status copy for the self-serve wait (originally the v1 app's progress lines).
# ONE shared source (2026-07-19): the Flask viewer serializes these into its progress JS and the
# Streamlit app imports them, so the two hosts can't drift. Keyed by the REAL pipeline stage when
# the runner's progress.json is available, by elapsed-time buckets otherwise.
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

# Which message bucket fits each REAL pipeline stage (progress.json from the Action runner), and
# the bar anchor (fraction) per stage — the UI creeps within a stage by time so the bar never
# looks frozen, and never parks at full before the result exists.
STAGE_BUCKETS = {"preflight_ok": "research", "researching": "research", "verifying": "verify",
                 "grounding": "verify", "rendering": "final"}
STAGE_ANCHORS = {"preflight_ok": 0.05, "researching": 0.15, "verifying": 0.60,
                 "grounding": 0.85, "rendering": 0.92}

# Stored timestamps are naive UTC wall-clock (datetime.now() on the UTC Actions runner — the
# due-gate in scout.monitor compares against the same clock, so STORAGE must stay UTC). All
# conversion to Eastern happens here, at display time only.
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # no tzdata on host — fixed EST beats crashing the viewer
    _ET = timezone(timedelta(hours=-5), "ET")


def _to_et(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(_ET)

_PERSONA_LABELS = {
    "eng_led": "Eng-led champion",
    "technical_evaluator": "Technical evaluator",
    "economic_buyer": "Economic buyer",
    "security_regulated": "Security & regulated",
    "exec_top_down": "Exec / top-down",
}

_SECTION_TITLES = {
    "executive_summary": "Executive Summary",
    "snapshot": "Snapshot",
    "recent_moves": "Recent Strategic Moves",
    "positioning": "Positioning and Differentiation",
    "pricing": "Pricing and Packaging",
    "battlecard": "Competitive Battlecard",
    "sentiment": "Sentiment",
    "objection_handling": "Objection Handling",
}
_SECTION_ORDER = ["executive_summary", "snapshot", "recent_moves", "positioning",
                  "pricing", "battlecard", "sentiment", "objection_handling"]
# Sections still generated and stored, but intentionally NOT rendered in the viewer. The Daily
# Briefing is the single summary surface, so the Executive Summary — a second summary of the same
# analysis written as the report's lead-off digest — is hidden to avoid redundant summaries. It
# stays in the schema, the prompts, and the saved brief; to bring it back, drop the id from here.
_HIDDEN_SECTIONS = {"executive_summary"}
_ZONES = [("where_we_win", "Where we win", "win"),
          ("contested", "Where it's a fight", "contested"),
          ("where_they_win", "Where they win", "lose")]


# --- inline markdown -> html (escapes $ so Streamlit doesn't LaTeX dollar amounts) -----------
def _inline(text: str) -> str:
    s = _html.escape(text or "", quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    # Cut-log text arrives Streamlit-escaped (\$ — historically even \\$ from a re-escape bug);
    # this HTML path needs no markdown escaping, so drop the backslashes before our own &#36;.
    s = re.sub(r"\\+\$", "$", s)
    s = s.replace("$", "&#36;")
    return s


def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return m.group(1).replace("www.", "") if m else "source"


def _fmt_asof(s: str | None) -> str:
    if not s:
        return ""
    try:
        return "as of " + datetime.strptime(s[:10], "%Y-%m-%d").strftime("%b %-d")
    except ValueError:
        return ""


def _fmt_dt(s: str | None):
    if not s:
        return ("—", "")
    try:
        dt = datetime.fromisoformat(s)
        if len(s.strip()) <= 10:   # date-only (e.g. baseline_date) — no wall-clock to convert;
            return (dt.strftime("%b %-d, %Y"), "")   # shifting it to ET would move the DAY back
        dt = _to_et(dt)
        return (dt.strftime("%b %-d, %Y"), dt.strftime("%-I:%M %p") + " ET")
    except ValueError:
        try:
            return (datetime.strptime(s[:10], "%Y-%m-%d").strftime("%b %-d, %Y"), "")
        except ValueError:
            return (s, "")


def _fmt_short(s: str | None) -> str:
    """Compact rail-row timestamp: 'Jun 10 · 1:09 PM ET' (year dropped), date-only passes
    through, unparseable strings come back as-is."""
    d, t = _fmt_dt(s)
    return f"{d.rsplit(', ', 1)[0]} · {t}" if t else d


def _utc_attr(s: str | None) -> str:
    """data-utc value for client-side localization: the stored naive-UTC timestamp with an
    explicit Z so JS Date() parses it as UTC. Empty for date-only / unparseable values (those
    stay server-rendered — a bare date must never shift a day under conversion)."""
    s = (s or "").strip()
    if len(s) <= 10:
        return ""
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return ""
    if dt.tzinfo:
        return _html.escape(dt.isoformat())
    return _html.escape(s + "Z")


def _parse_claim(c: dict) -> dict:
    out = {"title": "", "body": [], "so_what": "", "soundbite": ""}
    for p in (s.strip() for s in (c.get("claim", "") or "").split("\n\n")):
        if not p:
            continue
        if p.startswith("**So what:**"):
            out["so_what"] = p[len("**So what:**"):].strip()
        elif p.startswith("**Soundbite:**"):
            out["soundbite"] = p[len("**Soundbite:**"):].strip().strip("*").strip()
        elif p.startswith("**") and p.endswith("**") and p.count("**") == 2 and len(p) > 4:
            out["title"] = p[2:-2].strip()
        else:
            out["body"].append(p)
    return out


def _badge(c: dict, prefix: str) -> str:
    label = _PERSONA_LABELS.get(c.get("persona"))
    if not label:
        return ""
    return (f'<span class="persona p-{_html.escape(c["persona"])}"><span class="pk">{_html.escape(prefix)}</span> '
            f'{_html.escape(label)}</span>')


def _new_chip(new: bool) -> str:
    """The per-item 'Updated' badge — shown on a claim a monitor run OR an approved edit changed
    within display.NEW_BADGE_WINDOW_HOURS, so it ages out on its own."""
    return '<span class="scout-new">Updated</span>' if new else ""


def _anchor(subject_key: str) -> str:
    """Stable HTML id for a claim, so the changelog can deep-link straight to it."""
    return "u-" + re.sub(r"[^a-z0-9]+", "-", (subject_key or "").lower()).strip("-")


_CLASS_ORDER = ["filing", "court", "government", "company_statement", "job_posting", "research",
                "news", "page_snapshot", "review_site", "forum", "unknown"]
_CLASS_BLURB = {
    "filing": "Regulatory filings. Audited or sworn documents; the strongest source there is.",
    "court": "Court records. What was actually filed or ruled.",
    "government": "Regulators and government bodies, in their own words.",
    "company_statement": "The company's own site, press releases and investor pages. Reliable for what the company said; still the company's framing.",
    "job_posting": "Public job boards. What a company is hiring for is a leading indicator of what it is building.",
    "research": "Papers and journals.",
    "news": "Major outlets. Reputable, and secondhand: a paraphrase of a source, not the source.",
    "page_snapshot": "Archived copies of a page, dated.",
    "review_site": "Review platforms. Sentiment, never the anchor for a fact.",
    "forum": "Discussion and social. Sentiment, never the anchor for a fact.",
    "unknown": "Everything else on the web. The tier shown is the one Scout's verifier assigned; the kind of site is not one code can vouch for.",
}


def sources_html(slug: str) -> str:
    """The per-card sources page (WS0, 2026-09-28): every citation on the active card grouped by
    KIND (decided by code from the host), then by host, with the claims resting on each source and
    a deep link back to the claim. Conflicts (a fact anchored on a sentiment-grade host) are called
    out rather than hidden: the verifier's tier stands, and the reader can judge."""
    meta = store.load_meta(slug) or {}
    active, _retired = _prepare_display(store.load_claims(slug), meta)
    cited = [c for c in active if c.get("source_url")]
    by_class: dict = {}
    for c in cited:
        by_class.setdefault(c.get("source_class") or "unknown", {}).setdefault(_domain(c["source_url"]), []).append(c)
    counts = {k: sum(len(v) for v in hosts.values()) for k, hosts in by_class.items()}
    total = sum(counts.values())
    head = (f'<div class="srchead"><div class="srctitle"><span class="ey">Sources</span>'
            f'<h2>What this card rests on</h2>'
            f'<p class="srclede">{total} citations on the active card, sorted by the kind of source, '
            f'strongest first. The kind is decided by code from the host, never by the model; the tier '
            f'on each chip is the verifier\'s.</p></div>'
            f'<a class="srcback" href="/c/{_html.escape(slug)}">Back to the card</a></div>')
    chips = "".join(f'<span class="srcrow">{_class_chip(k)}<span class="srcn">{counts[k]}</span></span>'
                    for k in _CLASS_ORDER if k in counts)
    conflicts = [c for c in cited if c.get("source_class_conflict")]
    warn = ""
    if conflicts:
        items = "".join(f'<li>{_inline(_parse_claim(c)["title"] or str(c.get("claim", ""))[:120])} '
                        f'<span class="muted">rests on {_html.escape(_domain(c["source_url"]))}, a '
                        f'{_html.escape(_classify.CLASS_LABEL.get(c["source_class_conflict"], "sentiment").lower())} source; '
                        f'the verifier\'s tier ({_html.escape(str(c.get("source_tier")))}) is kept.</span></li>'
                        for c in conflicts)
        warn = f'<div class="srcwarn"><b>{len(conflicts)} fact{"s" if len(conflicts) > 1 else ""} on a sentiment-grade source</b><ul>{items}</ul></div>'
    secs = []
    for k in _CLASS_ORDER:
        hosts = by_class.get(k)
        if not hosts:
            continue
        rows = []
        for host, cs in sorted(hosts.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            first = cs[0]["source_url"]
            claim_rows = "".join(
                f'<li><a href="/c/{_html.escape(slug)}#{_anchor(c.get("subject_key", ""))}">'
                f'{_inline(_parse_claim(c)["title"] or str(c.get("claim", ""))[:110])}</a>'
                f'<span class="muted"> · {_html.escape(_SECTION_TITLES.get(c.get("section"), c.get("section") or ""))}'
                + (f' · {_html.escape(_fmt_asof(c.get("as_of")))}' if c.get("as_of") else "") + "</span></li>"
                for c in cs)
            rows.append(f'<div class="srchost"><div class="srchostline"><a href="{_html.escape(first)}" target="_blank" '
                        f'rel="noopener">{_html.escape(host)}</a><span class="srcn">{len(cs)}</span></div>'
                        f'<ul class="srcclaims">{claim_rows}</ul></div>')
        blurb = f'<p class="srcblurb">{_html.escape(_CLASS_BLURB.get(k, ""))}</p>'
        secs.append(_section(f"src-{k}", _classify.CLASS_LABEL.get(k, k), f"{counts[k]}", blurb + "".join(rows)))
    return ('<div id="scout-page"><div class="wrap srcpage">' + head + f'<div class="srcchips big">{chips}</div>'
            + warn + "".join(secs) + "</div></div>")


_TIER_TITLE = {
    "primary": "Primary source: the document itself, or the company's own statement",
    "reputable_secondary": "Reputable secondary source",
    "sentiment_only": "Sentiment: reviews or discussion, not a fact source",
}


def _class_chip(cls, tier=None) -> str:
    """The source-class chip beside a citation (WS0, 2026-09-28): what KIND of source this is,
    decided by code from the host (scout/sources/classify.py). The tier is the tooltip. Nothing
    renders when the class is missing (a derived claim with no resolvable source)."""
    if not cls:
        return ""
    label = _classify.CLASS_LABEL.get(cls, "Web")
    title = _TIER_TITLE.get(tier or _classify.CLASS_TIER.get(cls) or "", "")
    return (f'<span class="srcclass srcclass-{_html.escape(cls)}"'
            + (f' title="{_html.escape(title)}"' if title else "") + f'>{_html.escape(label)}</span>')


def _verified(url: str, asof: str = "", cls=None, tier=None) -> str:
    bits = ['<span class="verified"><span class="tick">✓</span>Verified</span>']
    if url:
        bits.append(f'<span class="sep">·</span><a href="{_html.escape(url)}" target="_blank" '
                    f'rel="noopener">{_html.escape(_domain(url))}</a>')
        chip = _class_chip(cls, tier)
        if chip:
            bits.append(chip)
    if asof:
        bits.append(f'<span class="sep">·</span>{_html.escape(asof)}')
    return '<div class="srcline">' + "".join(bits) + "</div>"


def _vsrc(c: dict, asof: str = "") -> str:
    """_verified from a display claim (source_url + class + tier already resolved by
    _prepare_display)."""
    return _verified(c.get("source_url", ""), asof, cls=c.get("source_class"), tier=c.get("source_tier"))


def _callout(kind: str, label: str, text: str) -> str:
    return f'<div class="callout {kind}"><b>{_html.escape(label)}</b>{_inline(text)}</div>'


def _prose_item(c: dict, *, callout_label=None, callout_kind="sw", badge_prefix=None,
                new: bool = False) -> str:
    p = _parse_claim(c)
    badge = _badge(c, badge_prefix) if badge_prefix else ""
    chip = _new_chip(new)
    if p["title"]:
        head = (f'<div class="ihead"><h4>{chip}{_inline(p["title"])}</h4>{badge}</div>' if badge
                else f'<h4>{chip}{_inline(p["title"])}</h4>')
    else:
        head = chip
    body = "".join(f"<p>{_inline(b)}</p>" for b in p["body"])
    call = ""
    if p["soundbite"]:
        call = _callout("sb", "Soundbite", p["soundbite"])
    elif p["so_what"] and callout_label:
        call = _callout(callout_kind, callout_label, p["so_what"])
    return (f'<div class="item" id="{_anchor(c.get("subject_key"))}">'
            f'{head}{body}{call}{_vsrc(c)}</div>')


def _bullet_item(c: dict, new: bool = False) -> str:
    return (f'<div class="item" id="{_anchor(c.get("subject_key"))}">'
            f'<p>{_new_chip(new)}{_inline(c.get("claim",""))}</p>'
            f'{_vsrc(c, _fmt_asof(c.get("as_of")))}</div>')


def _snapshot_box(c: dict, new: bool = False) -> str:
    return (f'<div class="box">{_new_chip(new)}{_inline(c.get("claim",""))}'
            f'{_vsrc(c, _fmt_asof(c.get("as_of")))}</div>')


def _section(sid: str, title: str, count_label: str, inner: str, open: bool = True) -> str:
    # <details>/<summary> => collapsible, open by default. Survives st.markdown sanitization.
    # With an audience chosen, a section with nothing written for that buyer renders CLOSED (2026-10-02):
    # the buyer's material leads, the rest of the brief waits one tap away.
    return (f'<details class="sec{"" if open else " folded"}" id="{sid}"{" open" if open else ""}><summary>'
            f'<span class="stitle">{_html.escape(title)}</span>'
            f'<span class="scount">{_html.escape(count_label)}</span>'
            f'<span class="chev">›</span></summary>'
            f'<div class="sbody">{inner}</div></details>')


# Long scrolling sections that open showing only a teaser (the first item[s]) plus a prominent
# "EXPAND SECTION" toggle, instead of dumping every item inline. The teaser stays always-visible;
# the remainder lives in a nested <details class="more"> so the toggle works without JS.
_PREVIEW_SECTIONS = ("snapshot", "recent_moves", "positioning", "pricing")


def _preview_section(sid: str, title: str, count_label: str, items: list,
                     n_first: int, *, snap: bool = False, open: bool = True) -> str:
    first, rest = items[:n_first], items[n_first:]
    if snap:
        first_html = '<div class="snap">' + "".join(first) + "</div>"
        rest_html = '<div class="snap">' + "".join(rest) + "</div>" if rest else ""
    else:
        first_html, rest_html = "".join(first), "".join(rest)
    head = (f'<div class="shead"><span class="stitle">{_html.escape(title)}</span>'
            f'<span class="scount">{_html.escape(count_label)}</span></div>')
    more = ""
    if rest:
        more = ('<details class="more"><summary>'
                f'<span class="lbl-more">Expand section · {len(rest)} more</span>'
                '<span class="lbl-less">Collapse section</span>'
                '<span class="mchev">▾</span></summary>'
                f'<div class="rest">{rest_html}</div></details>')
    if not open:                                       # audience chosen, nothing here for that buyer
        return _section(sid, title, count_label, first_html + more, open=False)
    return (f'<div class="sec preview" id="{sid}">{head}'
            f'<div class="sbody">{first_html}{more}</div></div>')


def _battlecard(claims: list, recent_keys: set | None = None, open: bool = True) -> str:
    recent_keys = recent_keys or set()
    subs = []
    for zid, zlabel, zcls in _ZONES:
        zc_all = sorted([c for c in claims if c.get("zone") == zid], key=_pkey)
        if not zc_all:
            continue
        zc, zfold = _split_for_audience(zc_all)
        mk = lambda c: _prose_item(c, badge_prefix="Best for", new=c.get("subject_key") in recent_keys)
        items = [mk(c) for c in zc]
        fold = _fold([mk(c) for c in zfold], "plays")
        head = (f'<div class="zhead"><span class="zlabel {zcls}">{_html.escape(zlabel)}</span>'
                f'<span class="subcount">{len(zc_all)} item{"s" if len(zc_all)!=1 else ""}</span></div>')
        if not items:                      # every play in this zone belongs to other buyers
            subs.append(f'<div class="sub zone {zcls}">{head}{fold}</div>')
            continue
        more = ""
        if items[1:]:
            more = ('<details class="more"><summary>'
                    f'<span class="lbl-more">Expand · {len(items) - 1} more</span>'
                    '<span class="lbl-less">Collapse</span>'
                    '<span class="mchev">▾</span></summary>'
                    f'<div class="rest">{"".join(items[1:])}</div></details>')
        subs.append(f'<div class="sub zone {zcls}">{head}{items[0]}{more}{fold}</div>')
    n = len([c for c in claims if c.get("zone")])
    return _section("bc", "Competitive Battlecard", f"{n} across 3 zones", "".join(subs), open=open)


def _cut_log(md: str):
    m = re.search(r"^##\s+Cut Log\s*$(.*?)(?=^##\s|\Z)", md, re.S | re.M)
    if not m:
        return "", 0
    rows, n = [], 0
    for line in m.group(1).splitlines():
        # both shapes the pipeline writes: the generator's "- **CUT — …:**" bullets and the review
        # path's / pull_contaminated's bare "**CUT — …:**" paragraphs (invisible before 2026-09-29)
        mm = re.match(r"(?:-\s+)?\*\*(CUT|REVISED)\s+—\s+(.*?):\*\*\s*(.*)$", line.strip())
        if not mm:
            continue
        n += 1
        tag, subj, why = mm.group(1), mm.group(2), mm.group(3)
        cls = "cut" if tag == "CUT" else "rev"
        rows.append(f'<div class="cut"><span class="cuttag {cls}">{tag}</span>'
                    f'<div class="body"><b>{_inline(subj)}</b> · {_inline(why)}</div></div>')
    if not rows:
        return "", 0
    note = ('<div class="cutnote">This is what verification removed or corrected during '
            'fact-checking, and why.</div>')
    return _section("cut", "Cut Log", f"{n} removed / revised", note + "".join(rows)), n


_OBAR = contextvars.ContextVar("scout_obar", default="")


def _rail(status: dict, present: list, plays_n: int = 3, nav_ids: set | None = None,
          sources: tuple | None = None) -> str:
    plays_lbl = "Top play" if plays_n == 1 else f"Top {plays_n} plays"
    toc = ['<div class="grp brief first">Contents</div>',
           '<a href="#brief">Today\'s angle</a>',
           f'<a href="#brief2">{plays_lbl}</a>',
           '<div class="grp">The full brief</div>']
    for sid, title, n in present:
        if sid not in _TRAIL_IDS:
            toc.append(f'<a href="#{sid}">{_html.escape(title)}</a>')
    # the verification trail: its own group, set off from the brief by a rule and a muted face,
    # so lineage / Cut Log / freshness do not read as more brief sections
    toc.append(f'<div class="grp trail">{_TRAIL_TITLE}</div>'
               f'<div class="grpsub">{_TRAIL_SUB}</div>')
    for sid, title, n in present:
        if sid in _TRAIL_IDS:
            toc.append(f'<a class="tr" href="#{sid}">{_html.escape(title)}</a>')
    toc.append('<a class="tr" href="#claims">Claim freshness</a>')
    nav = '<div class="toc" id="toc">' + "".join(toc) + "</div>"
    # The phone bar (2026-10-02 evening): the same contents and audience as two dropdowns in one
    # slim sticky row above the brief. The rail is hidden under 980px and this bar above it, so
    # one of them exists at a time (no restated surfaces).
    obar_toc = "".join(t.replace('class="grp brief first"', 'class="g"').replace('class="grp trail"', 'class="g"')
                       .replace('class="grp"', 'class="g"').replace('<div class="grpsub">', '<div class="gs">')
                       for t in toc)
    _OBAR.set('<details class="ob"><summary>Contents <span class="cv">&#9662;</span></summary>'
              f'<div class="sc-dd">{obar_toc}</div></details>')

    feed = status["change_feed"]
    def _cf_ts(e):
        try:    # git %at epoch -> data-utc, so the localizer can rewrite this row too
            iso = datetime.fromtimestamp(int(e["epoch"]), tz=timezone.utc) \
                .isoformat().replace("+00:00", "Z")
            return f'<span class="scout-lts" data-utc="{iso}">{_html.escape(e["date"])}</span>'
        except (KeyError, ValueError, TypeError):
            return _html.escape(e["date"])
    cf_rows = [f'<div class="row"><span class="dt">{_cf_ts(e)}</span>'
               f'<span>{_html.escape(e["subject"])}</span></div>' for e in feed]
    def _rail_ts(s):
        iso = _utc_attr(s)
        attr = f' class="scout-lts" data-utc="{iso}"' if iso else ""
        return f'<span{attr}>{_html.escape(_fmt_short(s))}</span>'

    # One "Material changes" panel: the full alert history, newest first. Recency is a property
    # of a row (the NEW chip), not a separate box — this replaced the old "Just updated" panel,
    # which was just the <48h slice of the same data restyled. Each row surfaces the judge's
    # stored severity (ACT = change what reps say/do now; WATCH = material context) and its
    # so_what reasoning behind a "Why it matters" toggle.
    # Cap each rail feed to a few rows and tuck the rest behind a native <details> expander.
    # This is not only tidiness: the rail is position:sticky, and a sticky box TALLER than the
    # viewport can't pin — it scrolls with the page until its bottom edge is reached, then
    # "catches". As these feeds grew, the rail outgrew the screen, so the panel looked frozen
    # while scrolling, then lurched near the end. Capping keeps the rail short enough to pin.
    def _collapse(rows, cap=4, noun="more"):
        if len(rows) <= cap:
            return "".join(rows)
        return ("".join(rows[:cap])
                + f'<details class="more"><summary>Show {len(rows) - cap} {noun}</summary>'
                + "".join(rows[cap:]) + "</details>")

    recent_fps = {a.get("fingerprint") or a.get("subject_key") for a in status["recent_updates"]}
    mc_rows = []
    for a in reversed(display.load_alerts(status["slug"])):
        chips = ""
        sev = str(a.get("severity") or "").lower()
        if sev in ("act", "watch"):
            chips += f'<span class="sev {sev}">{sev.upper()}</span>'
        if (a.get("fingerprint") or a.get("subject_key")) in recent_fps:
            chips += '<span class="new">NEW</span>'
        tb = a.get("triggered_by") or {}                      # WS3: this row came from a structured signal
        if tb.get("kind"):
            label = {"filing": "new filing", "new_department": "new department"}.get(tb["kind"], tb["kind"])
            chips += f'<span class="trig" title="{_html.escape(str(tb.get("summary") or ""))}">Triggered by: {_html.escape(label)}</span>'
        sw = a.get("so_what")
        swx = (f'<details class="swx"><summary>Why it matters</summary>'
               f'<div class="swb">{_inline(str(sw))}</div></details>') if sw else ""
        mc_rows.append(
            f'<div class="row"><div class="rmeta"><span class="dt">'
            f'{_rail_ts(a.get("detected_at", a.get("date","")))}</span>{chips}</div>'
            f'<span>{_html.escape(str(a.get("headline", a.get("so_what",""))))}</span>{swx}</div>')
    mc = _collapse(mc_rows) if mc_rows else '<div class="empty">No material changes detected yet.</div>'

    # Recently updated: claim-level changelog of approved edits, each a deep-link to the claim it
    # changed, so a rep or CMO sees what moved and jumps straight to it. Keep only rows whose claim
    # actually renders an anchor on this page (nav_ids) — a hidden section (e.g. executive_summary)
    # changing must never leave a dead link in the changelog.
    ru = [r for r in (status.get("recently_updated") or [])
          if nav_ids is None or _anchor(r.get("subject_key")) in nav_ids]
    ru_rows = [
        f'<div class="row"><span class="dt">Updated {_html.escape(str(r.get("updated_on") or ""))}</span>'
        f'<a href="#{_anchor(r.get("subject_key"))}">{_html.escape(str(r.get("label") or r.get("subject_key") or ""))}</a></div>'
        for r in ru]
    ru_html = _collapse(ru_rows) if ru_rows else '<div class="empty">No content changes recently.</div>'

    def panel(label, body, extra=""):
        return (f'<div class="panel"><div class="phead"><span class="ey">{label}</span>{extra}</div>'
                f'<div class="feed">{body}</div></div>')

    name = _html.escape(config.AUTHOR_NAME or "Urosh P")
    credit = (f'Built by <a href="{_html.escape(config.AUTHOR_LINKEDIN)}" target="_blank" '
              f'rel="noopener">{name}</a>') if config.AUTHOR_LINKEDIN else f"Built by {name}"
    if getattr(config, "SOURCE_REPO_URL", ""):
        credit += (f' · <a href="{_html.escape(config.SOURCE_REPO_URL)}" target="_blank" '
                   f'rel="noopener">GitHub</a>')
    # Sources (2026-09-28): what kinds of sources the active card rests on, decided by code from
    # the host, with the full per-source listing one click away.
    # Pick your audience (2026-09-28): the personas present on this card, each in its own colour
    # (the same colour as its badge on every play and objection, so the rail and the card tie
    # together); the chosen one's plays and objections lead and the others dim. Plain links, so it
    # works on the iPad and in print.
    view_panel = ""
    if sources:
        slug, _counts = sources
        present_p = [p for p in _PERSONAS if any(c.get("persona") == p for c in (status.get("_claims") or []))]
        if present_p:
            cur = _PERSONA.get()
            links = [f'<a class="pv{" on" if not cur else ""}" href="/c/{_html.escape(slug)}">Everyone</a>']
            for p in present_p:
                links.append(f'<a class="pv p-{p}{" on" if cur == p else ""}" href="/c/{_html.escape(slug)}?persona={p}">'
                             f'{_html.escape(_PERSONA_LABELS.get(p, p))}</a>')
            view_panel = panel("Pick your audience", '<div class="pviews">' + "".join(links) + "</div>") \
                .replace('<div class="panel">', '<div class="panel pviews-panel">', 1)
            cur_lbl = _PERSONA_LABELS.get(cur, cur) if cur else "Everyone"
            _OBAR.set(_OBAR.get() + '<details class="ob aud"><summary>Audience: '
                      f'{_html.escape(cur_lbl)} <span class="cv">&#9662;</span></summary>'
                      '<div class="sc-dd"><div class="g">Pick your audience</div>'
                      '<div class="pviews">' + "".join(links) + '</div></div></details>')
    src_panel = ""
    if sources:
        slug, counts = sources
        total = sum(counts.values())
        chips = "".join(f'<span class="srcrow">{_class_chip(k)}<span class="srcn">{n}</span></span>'
                        for k, n in sorted(counts.items(), key=lambda kv: (_CLASS_ORDER.index(kv[0]) if kv[0] in _CLASS_ORDER else 99)))
        body = (f'<div class="srcchips">{chips}</div>'
                f'<a class="srcall" href="/c/{_html.escape(slug)}/sources">All sources by kind</a>')
        src_panel = panel("Sources", body, f'<span class="ph-n">{total}</span>')
    # History (2026-09-28): the git feed is engineering history, not reader signal, so it collapses
    # into one line at the foot of the rail instead of a third panel beside Material changes and
    # Recently updated.
    history = ""
    if cf_rows:
        history = (f'<details class="rail-history"><summary>History <span class="ph-n">{len(cf_rows)}</span></summary>'
                   f'<div class="feed">{"".join(cf_rows)}</div></details>')
    return ('<div class="rail">' + nav + view_panel
            + panel("Material changes", mc, f'<span class="ph-n">{len(mc_rows)}</span>')
            + panel("Recently updated", ru_html, f'<span class="ph-n">{len(ru)}</span>')
            + src_panel + history
            + f'<div class="rail-credit">{credit}</div>' + "</div>")


def _freshness(rows: list) -> str:
    new_count = sum(1 for r in rows if r.get("is_new"))
    trs = []
    for r in rows:
        isnew = r.get("is_new")
        trs.append(
            f'<tr><td class="key">{_html.escape(str(r.get("subject_key","")))}</td>'
            f'<td class="secn">{_html.escape(str(r.get("section","")))}</td>'
            f'<td>{_html.escape(str(r.get("as_of") or "—"))}</td>'
            f'<td>{_html.escape(str(r.get("verified_on") or "—"))}</td>'
            f'<td class="{"" if isnew else "no"}">{"true" if isnew else "false"}</td></tr>')
    win = f"{display.NEW_BADGE_WINDOW_HOURS}h"   # one knob (display.py) drives badge + labels
    return (f'<div class="freshness" id="claims"><div class="fhead">'
            f'<div class="ftitle">Claim freshness · {len(rows)} claims '
            f'({new_count} updated &lt;{win})</div>'
            '<div class="fcap"><code>as_of</code> = the date the fact is true as-of · '
            '<code>verified_on</code> = when grounding last confirmed the exact wording · '
            f'<code>is_new</code> = a monitor run touched it &lt;{win} ago.</div></div>'
            '<div class="ftab-scroll"><table class="ftab"><thead><tr>'
            '<th>subject_key</th><th>section</th><th>as_of</th><th>verified_on</th><th>is_new</th>'
            '</tr></thead><tbody>' + "".join(trs) + "</tbody></table></div></div>")


def _briefing(claims: list, label: str = "Your Daily Briefing",
              tag: str = "the 2-min version before your call", clear_href: str | None = None) -> str:
    # Today's angle = the brief's STRATEGIC LEAD (the executive summary's first claim), the single
    # most consequential opener, set by the strategic pass. The executive_summary section itself is
    # hidden to avoid a redundant summary, so this is where that lead actually surfaces to the rep.
    # Fall back to the freshest recent move only when a card has no executive-summary lead.
    exec_leads = sorted((c for c in claims if c.get("section") == "executive_summary"),
                        key=lambda c: c.get("order", 0))
    aud = _PERSONA.get()
    own = [c for c in exec_leads if aud and c.get("persona") == aud]  # the buyer's own lead, if written
    angle = own[0] if own else (exec_leads[0] if exec_leads else None)
    if angle is None:
        moves = [c for c in claims if c.get("section") == "recent_moves"]
        pri = [c for c in moves if re.search(r"billing|pricing|price|metered",
                                             (c.get("subject_key", "") + c.get("claim", "")), re.I)]
        pool = pri or moves
        angle = max(pool, key=lambda c: (c.get("as_of") or "", -c.get("order", 0))) if pool else None
    angle_html = ""
    if angle:
        p = _parse_claim(angle)
        body = " ".join(p["body"])
        angle_html = (
            '<div class="bsub">Today\'s angle</div><div class="angle">'
            + (f'<p class="ah"><strong>{_inline(p["title"])}</strong></p>' if p["title"] else "")
            + (f'<p>{_inline(body)}</p>' if body else "")
            + (_callout("sb", "Say", p["soundbite"]) if p["soundbite"] else "")
            + (_callout("sw", "Move", p["so_what"]) if p["so_what"] else "")
            + _vsrc(angle, _fmt_asof(angle.get("as_of"))) + "</div>")

    wins = _top_wins(claims)
    plays = []
    for i, c in enumerate(wins, 1):
        p = _parse_claim(c)
        badge = _badge(c, "Best for")
        top = (f'<div class="ptop"><span class="num">PLAY {i:02d}</span>{badge}</div>'
               if badge else f'<div class="num">PLAY {i:02d}</div>')
        why = "".join(f"<p>{_inline(b)}</p>" for b in p["body"])
        sb = _callout("sb", "Soundbite", p["soundbite"]) if p["soundbite"] else ""
        plays.append(f'<div class="play">{top}<h4>{_inline(p["title"])}</h4>{why}{sb}'
                     f'{_vsrc(c)}</div>')
    plays_lbl = "Top play" if len(plays) == 1 else f"Top {len(plays)} plays"
    plays_html = (f'<div class="bsub two" id="brief2">{plays_lbl}</div>'
                  f'<div class="playbox">{"".join(plays)}</div>') if plays else ""
    if aud and not plays:
        plays_html = ('<div class="bsub two" id="brief2">Top plays</div>'
                      f'<p class="aud-none">No plays written for the {_html.escape(_PERSONA_LABELS.get(aud, aud).lower())} yet. '
                      'The full brief below is unchanged.</p>')
    if aud:   # the buyer's objections, pulled up: the other half of what a rep prepares for
        objs = [c for c in sorted(claims, key=lambda c: c.get("order", 0))
                if c.get("section") == "objection_handling" and c.get("persona") == aud][:4]
        if objs:
            rows = []
            for c in objs:
                q = _parse_claim(c)
                badge = _badge(c, "Raised by")
                rows.append(f'<div class="aud-obj">{f"<div class=\"ptop\">{badge}</div>" if badge else ""}'
                            f'<h4>{_inline(q["title"]) if q["title"] else _inline(c.get("claim", "")[:120])}</h4>'
                            + (f'<p>{_inline(" ".join(q["body"]))}</p>' if q["body"] else "")
                            + (_callout("sw", "So what", q["so_what"]) if q["so_what"] else "") + "</div>")
            plays_html += (f'<div class="bsub two">Objections they raise</div><div class="playbox">{"".join(rows)}</div>')

    # Honest freshness (2026-08-08): the tag reflects the LEAD's own as_of, not a blanket "refreshed
    # today" — the angle is an elected strategic lead that only moves when a fresher verdict clears
    # the deal-impact bar (see propagate._lead_election), so it can legitimately be days old.
    asof = _fmt_asof(angle.get("as_of")) if angle else ""     # _fmt_asof already yields "as of <date>"
    full_tag = f"{tag} · lead {asof}" if asof else tag
    # Audience mode (2026-10-02 night): the box says what it is filtered for, carries the buyer's
    # colour, and offers one way out.
    cls = f" p-{aud}" if aud else ""
    head_l = (f'{_html.escape(label)} <span class="filt">filtered for {_html.escape(_PERSONA_LABELS.get(aud, aud))}</span>'
              if aud else _html.escape(label))
    head_r = (f'<a class="clear" href="{_html.escape(clear_href)}">Clear filter</a>' if (aud and clear_href)
              else f'<span class="r">{_html.escape(full_tag)}</span>')
    return (f'<div class="briefing{cls}" id="brief"><div class="bhead">'
            f'<span class="l"><span class="dot"></span>{head_l}</span>'
            f'{head_r}</div>'
            f'<div class="bbody">{angle_html}{plays_html}</div></div>')


def _metrics(cp: dict, claims_n: int, remaining: int, new_count: int = 0) -> str:
    last_raw = cp.get("last_checked_ts") or cp.get("last_checked")
    last_d, last_t = _fmt_dt(last_raw)
    next_d, next_t = _fmt_dt(cp.get("next_check"))
    base_d, _ = _fmt_dt(cp.get("baseline_date"))
    last_iso, next_iso = _utc_attr(last_raw), _utc_attr(cp.get("next_check"))
    # The countdown ticks live: server-render the seconds, then a parent-injected script (see
    # app_v2._countdown_component) re-renders #scout-countdown every second from data-remaining.
    # An unmonitored card (next_check is None) is never re-checked — show that, never "due now".
    if not cp.get("next_check"):
        cd = '<div class="cd cd-off">not monitored</div>'
    elif remaining > 0:
        h, m, s = remaining // 3600, (remaining % 3600) // 60, remaining % 60
        cd = (f'<div class="cd" id="scout-countdown" data-remaining="{remaining}">'
              f'in {h}h {m:02d}m {s:02d}s</div>')
    else:
        cd = '<div class="cd" id="scout-countdown" data-remaining="0">refresh due now</div>'
    delta = (f'<span class="mdelta" title="{new_count} new since the last refresh">'
             f'+{new_count}</span>') if new_count else ""

    # Server renders ET; data-utc lets the parent-injected localizer (app_v2._countdown_component)
    # rewrite date + time to the VIEWER's timezone in-browser. No attribute -> stays ET.
    def card(label, val, sub_t="", iso=""):
        a = f' data-utc="{iso}"' if iso else ""
        d = f'<span class="scout-ld"{a}>{_html.escape(val)}</span>' if iso else _html.escape(val)
        t = f'<span class="t scout-lt"{a}>{_html.escape(sub_t)}</span>' if sub_t else ""
        return f'<div class="metric"><div class="ml">{label}</div><div class="mv">{d}{t}</div></div>'

    next_a = f' data-utc="{next_iso}"' if next_iso else ""
    next_dh = f'<span class="scout-ld"{next_a}>{_html.escape(next_d)}</span>' if next_iso else _html.escape(next_d)
    return ('<div class="metrics">'
            + card("Last refresh", last_d, last_t, last_iso)
            + f'<div class="metric"><div class="ml">Next refresh</div>'
              f'<div class="mv">{next_dh}</div>{cd}</div>'
            + card("Baseline", base_d)
            + '<div class="metric claims"><div class="ml">Claims tracked &amp; verified</div>'
              f'<div class="mv"><a href="#claims">{claims_n}</a>{delta}</div>'
              '<div class="sub"><a href="#claims">see all ↓</a></div></div>'
            + "</div>")


_EMOJI = {"batman": "🦇", "superman": "🦸"}


def _name(n: str) -> str:
    e = _EMOJI.get((n or "").lower())
    return f"{e} {n}" if e else n


def brief_parts(meta: dict | None) -> tuple[str, str, str]:
    """(competitor, my_company, area) as the page names them. A focus of none / "None" / general
    is the area "General" (2026-10-02), so every brief states one."""
    meta = meta or {}
    comp = (meta.get("competitor") or "").strip()
    mine = (meta.get("my_company") or "").strip()
    focus = (meta.get("focus") or "").strip()
    if not focus or focus.lower() in ("none", "general"):
        area = "General"
    else:                                              # "enterprise coding/developers" -> "Enterprise coding and developers"
        area = re.sub(r"\s*/\s*", " and ", focus)
        area = area[0].upper() + area[1:]
    return comp, mine, area


def _title_block(meta: dict, print_href: str | None = None, persona: str | None = None) -> str:
    """The brief header (2026-10-02): "Competitive Brief: <competitor> for <company> sales reps",
    the area under it, and Print beside what it prints. One size on the title line; the
    competitor is marked by colour, not size."""
    comp, mine, area = brief_parts(meta)
    who = f' for {_html.escape(_name(mine))} sales reps' if mine else ""
    print_btn = (f'<a class="sc-btn sc-quiet" href="{_html.escape(print_href)}" target="_blank" rel="noopener">'
                 f'{_ICON_PRINT}Print call sheet</a>' if print_href else "")
    return ('<div class="sc-head"><div>'
            f'<h1><span class="pre">Competitive Brief: </span><span class="co">{_html.escape(_name(comp))}</span>{who}</h1>'
            f'<div class="sc-area"><span class="k">Area:</span> {_html.escape(area)}'
            + (f' <span class="k">&middot; Audience:</span> <span class="aud p-{_html.escape(persona)}">'
               f'{_html.escape(_PERSONA_LABELS.get(persona, persona))}</span>' if persona else "")
            + '</div></div>'
            f'{print_btn}</div>')


_ICON_PRINT = ('<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">'
               '<path d="M4 6V2h8v4M4 12H2V7h12v5h-2M4 10h8v4H4z"/></svg>')
_ICON_PLUS = ('<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
              '<path d="M8 3v10M3 8h10"/></svg>')
# The Slack mark (four colours), inline so the button needs no asset.
_ICON_SLACK = ('<svg viewBox="0 0 122.8 122.8" aria-hidden="true" class="slk">'
               '<path d="M25.8 77.6a12.9 12.9 0 1 1-12.9-12.9h12.9zm6.5 0a12.9 12.9 0 0 1 25.8 0v32.3a12.9 12.9 0 0 1-25.8 0z" fill="#e01e5a"/>'
               '<path d="M45.2 25.8a12.9 12.9 0 1 1 12.9-12.9v12.9zm0 6.5a12.9 12.9 0 0 1 0 25.8H12.9a12.9 12.9 0 0 1 0-25.8z" fill="#36c5f0"/>'
               '<path d="M97 45.2a12.9 12.9 0 1 1 12.9 12.9H97zm-6.5 0a12.9 12.9 0 0 1-25.8 0V12.9a12.9 12.9 0 0 1 25.8 0z" fill="#2eb67d"/>'
               '<path d="M77.6 97a12.9 12.9 0 1 1-12.9 12.9V97zm0-6.5a12.9 12.9 0 0 1 0-25.8h32.3a12.9 12.9 0 0 1 0 25.8z" fill="#ecb22e"/></svg>')
_ICON_HOW = ('<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true" class="how-i">'
             '<rect x="1.5" y="6" width="5" height="8" rx="1"/><rect x="7.5" y="6" width="5" height="8" rx="1"/>'
             '<rect x="13.5" y="6" width="5" height="8" rx="1"/><path d="M6.5 10h1M12.5 10h1"/></svg>')
_ICON_MENU = ('<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">'
              '<path d="M2 4h12M2 8h12M2 12h12"/></svg>')


_SYSTEM_LINE = "Scout uses AI agents and human-calibrated model judgement."
_STATEMENT = "Deal-moving, always-fresh competitive briefs prepared by AI agents every morning."

# The challenger lanes: company, then the model as a reader would name it. The registry of what
# actually runs is scout/replaybackends.py (tests/test_how_panel.py keeps the two in step).
_HOW_CHALLENGERS = (("Mistral", "Magistral 24B"), ("Google", "Gemma 4 26B"),
                    ("NVIDIA", "Nemotron 3 Nano"), ("Apple", "Apple on-device model"))


def _model_label(model_id: str) -> str:
    """'claude-haiku-4-5-20251001' -> 'Haiku 4.5'; 'claude-sonnet-5' -> 'Sonnet 5'. The lanes read
    config, so a model change shows on the page without an edit here."""
    m = re.match(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", model_id or "")
    if not m:
        return model_id or ""
    return f"{m.group(1).title()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")


def _how_figures() -> dict | None:
    """Live figures for the panel, from the committed cards only (no network): active claims,
    cards, alert entries dated on the latest run day. None when they cannot be read, and the
    figure line is then left out; nothing is ever typed in by hand."""
    try:
        slugs = display.list_battlecards()
        if not slugs:
            return None
        total, last = 0, None
        for slug in slugs:
            total += sum(1 for c in store.load_claims(slug) if str(c.get("status", "active")) != "retired")
            lc = (store.load_meta(slug) or {}).get("last_checked")
            if lc and (last is None or lc > last):
                last = lc
        if not last:
            return None
        day = last[:10]
        updates = 0
        for slug in slugs:
            fp = os.path.join(store.STORE_ROOT, slug, "alerts.md")
            if os.path.isfile(fp):
                with open(fp, encoding="utf-8") as f:
                    updates += len(re.findall(r"(?m)^- \*\*\[[A-Z]+\] " + re.escape(day), f.read()))
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")
        run_pt = datetime.fromisoformat(last).replace(tzinfo=timezone.utc).astimezone(pt)
        today = datetime.now(pt).date() == run_pt.date()
        return {"claims": total, "cards": len(slugs), "updates": updates,
                "when": "this morning" if today else "in the last run",
                "run": f"{run_pt.strftime('%b')} {run_pt.day}"}
    except Exception:
        return None


def _how_panel() -> str:
    """The 'How this works' panel: what the system does, in one diagram and three short columns.
    It explains the system and stops there; per-claim evidence stays in the card's Verification
    trail (no surface restates another). No run cost and no model performance, by decision
    (2026-10-02): every model lane looks the same."""
    fast, orch, sub = (_model_label(m) for m in
                       (config.FAST_MODEL, config.ORCHESTRATOR_MODEL, config.SUBAGENT_MODEL))
    steps = (("Step 1", "Watch", "Scans each competitor for what changed since yesterday."),
             ("Step 2", "Weigh", "Decides whether a change matters in a deal and which parts of the card it touches."),
             ("Step 3", "Write", "Drafts the edit from the source, with its link and date attached."),
             ("Step 4", "Judge", "Checks the edit against the source and rules on it."))
    flow = "".join(f'<div class="hw-step"><div class="hw-n">{n}</div><div class="hw-t">{t}</div>'
                   f'<div class="hw-d">{d}</div></div>' for n, t, d in steps)
    flow += ('<div class="hw-step hw-dec"><div class="hw-n">Output</div><div class="hw-t">Decision</div>'
             '<div class="hw-states">'
             '<div class="hw-state pub"><b>Publish</b><span>Along with source and date</span></div>'
             '<div class="hw-state cut"><b>Cut</b><span>In the Cut Log, with the reason</span></div>'
             '<div class="hw-state held"><b>Hold</b><span>Waits for a person</span></div>'
             '</div></div>')
    default_lane = ('<div class="hw-lane"><div class="hw-ln">Default: <i>Anthropic</i></div>'
                    f'<div class="hw-track seg"><span>{_html.escape(fast)}</span><span>{_html.escape(orch)}</span>'
                    f'<span>{_html.escape(sub)}</span><span class="w2">{_html.escape(orch)}</span></div></div>')
    lanes = "".join(f'<div class="hw-lane"><div class="hw-ln">{co}</div>'
                    f'<div class="hw-track"><span>{mo}</span></div></div>' for co, mo in _HOW_CHALLENGERS)
    fig = _how_figures()
    fig_li = (f'<li>Claims: <span class="hw-num">{fig["claims"]}</span> total on '
              f'<span class="hw-num">{fig["cards"]}</span> cards, <span class="hw-num">{fig["updates"]}</span> '
              f'update{"" if fig["updates"] == 1 else "s"} {fig["when"]}.</li>') if fig else ""
    asof = ""                                              # the footer carries links only (2026-10-02 night)
    return (
        '<div class="how" id="how" hidden>'
        '<button type="button" class="hw-close" data-how aria-expanded="true" aria-controls="how" aria-label="Close">&#215;</button>'
        f'<div><div class="hw-sys">{_SYSTEM_LINE}</div>'
        '<div class="hw-h">How Scout keeps briefs true and useful</div>'
        '<p class="hw-lede">AI agents search for changes, decide what is material, and track the '
        'provenance and accuracy of every claim. Each model has one job. No decision is approved by '
        'the model that made it.</p></div>'
        '<div class="hw-diagram" role="img" aria-label="Four steps in order: watch, weigh, write, judge, '
        'ending in a decision: publish, cut or hold. Below, five model lanes run across all five stages: '
        'the default models, then Mistral, Google, NVIDIA and Apple.">'
        f'<div class="hw-row"><div class="hw-ln"></div><div class="hw-flow">{flow}</div></div>'
        f'<div class="hw-lanes">{default_lane}'
        '<div class="hw-ev"><div class="hw-evh">Evaluated</div>'
        '<p>Every decision above is logged and replayed by challenger models. Disputed calls go to a '
        'blind arbiter.</p></div>'
        f'{lanes}</div></div>'
        '<div class="hw-cols">'
        '<div class="hw-col"><h4>The product</h4><ul>'
        '<li>Every claim is a deal-mover.</li>'
        '<li>Decisions calibrated iteratively by the author.</li>'
        '<li>All claims are verified for accuracy and link to source and date.</li>'
        '<li>The Ask Scout agent answers only with verified information.</li>'
        '<li>The Cut Log shows unverified and stale claims.</li>'
        f'{fig_li}</ul></div>'
        '<div class="hw-col"><h4>The build</h4><ul>'
        '<li>A pipeline for the daily checks, event triggers between runs, and an agent for Ask Scout, '
        'because a question&rsquo;s path cannot be planned ahead.</li>'
        '<li>Ask Scout is also an MCP tool, so other agents can call it, and it answers in Slack.</li>'
        '<li>Code keeps the important gates: cost, retries, links, dates, format. Models are used for '
        'judgment, and the cheapest model that passes its eval gets the job.</li>'
        '<li>Fallback 1 is a model. Fallback 2 is the human author.</li>'
        '<li>Built with Claude Code on a Mac mini, with system design decisions by the author.</li>'
        '<li>Additional infrastructure: Google Cloud, GitHub, Ollama, Resend.</li></ul></div>'
        '<div class="hw-col"><h4>The evals</h4><ul>'
        '<li>Challengers run locally on a Mac mini and replay the exact calls the default models saw.</li>'
        '<li>The arbiter rules from the source, blind to which model said what, and names each failure '
        'from a fixed list.</li>'
        '<li>The author reviews every ruling and can overrule it.</li>'
        '<li>Models are compared only on the same set of calls.</li></ul></div>'
        '</div>'
        '<div class="hw-foot"><span class="hw-links">'
        f'<a href="{_html.escape(config.SOURCE_REPO_URL)}" target="_blank" rel="noopener">Code on GitHub</a>'
        f'<a href="{_html.escape(config.SOURCE_REPO_URL)}/blob/main/v2/docs/mcp.md" target="_blank" rel="noopener">Agent Scout via MCP</a>'
        f'<a href="{_html.escape(config.AUTHOR_LINKEDIN)}" target="_blank" rel="noopener">Contact me</a>'
        '</span></div>'
        '</div>')


# Opens and closes the panel from any [data-how] control (the app bar item, the phone menu);
# /#how opens it on arrival (a link for a resume or a message). Links whose target is not on this
# page (no card, Ask off) are hidden instead of going nowhere. The briefs strip shows as many tabs
# as fit on one line and folds the rest into "N more" (priority plus); phones scroll it sideways.
_HOW_JS = (
    "<script>(function(){var p=document.getElementById('how');var bs=[].slice.call(document.querySelectorAll('[data-how]'));"
    "if(!p||!bs.length)return;"
    "function set(o,how){bs.forEach(function(b){b.setAttribute('aria-expanded',o?'true':'false');});p.hidden=!o;"
    "if(o){try{window.gtag&&window.gtag('event','how_this_works_open',{method:how});}catch(e){}}}"
    "bs.forEach(function(b){b.addEventListener('click',function(e){e.preventDefault();var o=p.hidden;set(o,'button');"
    "var m=document.getElementById('sc-menu');if(m)m.removeAttribute('open');"
    "try{history.replaceState(null,'',o?'#how':location.pathname+location.search);}catch(x){}"
    "if(o){try{p.scrollIntoView({block:'start',behavior:'smooth'});}catch(x){}}});});"
    "function fromHash(){if(location.hash==='#how'&&p.hidden){set(true,'link');"
    "try{p.scrollIntoView({block:'start'});}catch(e){}}}"
    "fromHash();window.addEventListener('hashchange',fromHash);"
    "})();</script>")

_STRIP_JS = (
    "<script>(function(){var row=document.getElementById('sc-tabs'),more=document.getElementById('sc-more');"
    "if(!row||!more)return;var tabs=[].slice.call(row.children),menu=more.querySelector('.sc-dd');"
    "function layout(){var narrow=window.innerWidth<=760;tabs.forEach(function(t){t.style.display='';});"
    "more.hidden=true;var hidden=[];if(!narrow){more.hidden=false;"
    "for(var i=tabs.length-1;i>0&&row.scrollWidth>row.clientWidth+1;i--){if(tabs[i].classList.contains('on'))continue;"
    "tabs[i].style.display='none';hidden.unshift(tabs[i]);}}"
    "if(!hidden.length){more.hidden=true;more.removeAttribute('open');return;}"
    "more.querySelector('.n').textContent=hidden.length;"
    "menu.innerHTML=(tabs.length>12?'<input type=\"search\" placeholder=\"Find a brief\" aria-label=\"Find a brief\">':'')+"
    "hidden.map(function(t){return '<a href=\"'+t.getAttribute('href')+'\">'+t.querySelector('.nm').innerHTML+"
    "'<small>'+t.querySelector('.ar').textContent+'</small></a>';}).join('');"
    "var q=menu.querySelector('input');if(q){q.addEventListener('input',function(){var v=q.value.toLowerCase();"
    "[].forEach.call(menu.querySelectorAll('a'),function(a){a.style.display=a.textContent.toLowerCase().indexOf(v)>-1?'':'none';});});}}"
    "layout();var t;window.addEventListener('resize',function(){clearTimeout(t);t=setTimeout(layout,80);});"
    "document.addEventListener('click',function(e){[].forEach.call(document.querySelectorAll('details.sc-more[open],details.sc-menu[open]'),"
    "function(d){if(!d.contains(e.target))d.removeAttribute('open');});});"
    "})();</script>")


# Fonts load via <link> tags injected SEPARATELY from the main <style> — a sanitizer that
# dislikes @import can drop a whole <style> that contains it, which would wipe ALL styling and
# leave the "elements in place but unstyled" look. Injecting fonts on their own keeps the main
# stylesheet clean; if the <link> is ever stripped, only the typeface falls back.
FONT_HEAD = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,ital,'
    'wght@9..144,0,400;9..144,0,500;9..144,0,600;9..144,1,400;9..144,1,500'
    '&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">')

# Appended after the mockup CSS. Sections use the mockup's own <details class="sec"> styling;
# here we only cap width, fix the freshness column color, add the rail credit, and widen the
# 2-col breakpoint so a narrow viewport doesn't stack the rail on top of the brief.
_OVERRIDES = """
/* The top of the page (2026-10-02): app bar, lead row, briefs strip, brief header, index grid.
   sc-* names: the mockup CSS owns .top/.brand/.livebox/.tagline/.rt (unused now). */
#scout-page .wrap.mast{padding-bottom:0}
#scout-page .sc-bar{display:flex;align-items:center;justify-content:space-between;gap:18px;min-height:60px;padding:8px 0;border-bottom:1px solid var(--line)}
#scout-page .sc-brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:inherit;flex:none}
#scout-page .sc-brand .d{width:13px;height:13px;border-radius:4px;background:var(--accent-deep)}
#scout-page .sc-brand .nm{font-family:var(--display);font-weight:600;font-size:26px;line-height:1;letter-spacing:-.02em}
#scout-page .sc-nav{display:flex;align-items:center;gap:4px;margin-left:auto;position:relative}
#scout-page .sc-navlink{display:inline-flex;align-items:center;gap:7px;min-height:36px;padding:0 11px;border-radius:7px;font-size:14px;font-weight:500;color:var(--ink);text-decoration:none;white-space:nowrap}
#scout-page .sc-navlink:hover,#scout-page .sc-navlink[aria-expanded="true"]{background:var(--accent-soft)}
#scout-page .sc-navlink.on{font-weight:600;color:var(--accent-deep);background:var(--accent-soft)}
#scout-page .sc-cnt{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--accent-deep);background:var(--paper2);border:1px solid var(--accent-line);border-radius:999px;padding:0 7px;line-height:18px}
#scout-page .sc-btn{display:inline-flex;align-items:center;gap:8px;min-height:36px;padding:0 14px;border:1px solid var(--line);border-radius:8px;background:var(--paper);font-weight:600;font-size:14px;color:var(--ink);text-decoration:none;cursor:pointer;white-space:nowrap}
#scout-page .sc-btn:hover{border-color:var(--accent-line);background:var(--paper2)}
#scout-page .sc-btn.sc-pri{background:var(--accent-deep);border-color:var(--accent-deep);color:#fff;margin-left:6px}
#scout-page .sc-btn.sc-pri:hover,#scout-page .sc-btn.sc-pri.on{background:var(--accent)}
#scout-page .sc-btn.sc-quiet{border-color:transparent;background:transparent;color:var(--accent-deep);padding:0 8px}
#scout-page .sc-btn.sc-quiet:hover{background:var(--accent-soft);border-color:transparent}
#scout-page .sc-btn svg{width:15px;height:15px;flex:none}
#scout-page .sc-btn.sc-slackbtn{margin-left:6px;background:var(--paper2)}
#scout-page .sc-btn.sc-slackbtn svg.slk{width:16px;height:16px}
#scout-page details.sc-menu{display:none;position:relative}   /* phones: the strip is hidden there */
#scout-page details.sc-menu>summary{list-style:none;cursor:pointer}
#scout-page details.sc-menu>summary .cv{font-family:var(--mono);font-size:11px;color:var(--accent-deep);margin-left:2px;transition:transform .15s}
#scout-page details.sc-menu[open]>summary .cv{transform:rotate(180deg)}
#scout-page details.sc-menu[open]>summary{background:var(--accent-soft)}
#scout-page .sc-dd a.on{background:var(--accent-deep);color:#fff}
#scout-page .sc-dd a.on small{color:rgba(255,255,255,.8)}
#scout-page .sc-dd a.sc-see{border-top:1px solid var(--line2);margin-top:4px;padding-top:9px;color:var(--accent-deep);font-weight:600}
#scout-page .sc-dd a.sc-phone-only{display:none}
#scout-page details.sc-menu>summary::-webkit-details-marker{display:none}
#scout-page .sc-dd{position:absolute;top:calc(100% + 6px);right:0;z-index:60;min-width:280px;max-height:70vh;overflow:auto;background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:6px;box-shadow:0 8px 24px rgba(28,29,22,.10)}
#scout-page .sc-dd .mh{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:6px 10px 3px}
#scout-page .sc-dd input{width:100%;font:inherit;font-size:13px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--paper2);margin-bottom:4px}
#scout-page .sc-dd a{display:flex;flex-direction:column;align-items:flex-start;gap:0;padding:6px 10px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink);text-decoration:none;white-space:nowrap;line-height:1.3}
#scout-page .sc-dd a small{font-size:12px;color:var(--muted)}
#scout-page .sc-dd a:hover{background:var(--accent-soft)}
#scout-page .sc-lead{padding:12px 0 0}
#scout-page .sc-statement{font-family:var(--display);font-size:15px;font-weight:500;color:var(--muted);max-width:40ch;line-height:1.4;text-wrap:balance}
#scout-page .sc-strip{display:flex;align-items:flex-end;gap:2px;margin-top:14px;border-bottom:1px solid var(--line);min-width:0}
#scout-page .sc-tabs{display:flex;gap:2px;flex:1 1 auto;min-width:0;overflow:hidden}
#scout-page .sc-tab{display:inline-flex;flex-direction:column;justify-content:center;gap:1px;height:46px;padding:0 13px;font-size:14px;font-weight:500;color:var(--muted);text-decoration:none;white-space:nowrap;flex:none;border-bottom:2px solid transparent;margin-bottom:-1px;border-radius:7px 7px 0 0;line-height:1.2}
#scout-page .sc-tab > span{display:block}
#scout-page .sc-tab small{font-size:14px;font-weight:400;color:var(--muted)}
#scout-page .sc-tab .ar{font-family:var(--mono);font-size:10px;color:var(--muted);max-width:190px;overflow:hidden;text-overflow:ellipsis}
#scout-page .sc-tab:hover{color:var(--ink);background:var(--accent-soft)}
#scout-page .sc-tab.on{color:var(--ink);font-weight:600;border-bottom-color:var(--accent-deep)}
#scout-page details.sc-more{position:relative;flex:none;margin-bottom:-1px}
#scout-page details.sc-more[hidden]{display:none}
#scout-page details.sc-more>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;height:40px;padding:0 12px;font-size:14px;font-weight:600;color:var(--accent-deep);white-space:nowrap;border-radius:7px 7px 0 0}
#scout-page details.sc-more>summary:hover{background:var(--accent-soft)}
#scout-page details.sc-more>summary::-webkit-details-marker{display:none}
#scout-page .sc-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;padding:18px 0 8px}
#scout-page .wrap.tw{padding-bottom:0}
#scout-page hr.rule{margin-top:0}
#scout-page .sc-head h1{font-family:var(--display);font-weight:600;font-size:28px;line-height:1.12;letter-spacing:-.015em;color:var(--ink);margin:0}
#scout-page .sc-head h1 .co{color:var(--accent-deep)}
#scout-page .sc-area{font-size:15px;margin-top:6px;color:var(--ink)}
#scout-page .sc-area .k{color:var(--muted)}
#scout-page .sc-actions{display:flex;gap:10px;align-items:center;justify-content:flex-end;padding:12px 0 4px}
#scout-page .sc-idx{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-top:20px}
#scout-page .sc-idx h1{font-family:var(--display);font-weight:600;font-size:30px;letter-spacing:-.015em;margin:0}
#scout-page .sc-idx .n{font-family:var(--body);font-size:14px;color:var(--muted);margin-left:10px;font-weight:500}
#scout-page .sc-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:14px}
#scout-page .sc-bcard{border:1px solid var(--line);border-radius:9px;background:var(--paper);padding:14px 16px;text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:6px;min-height:120px}
#scout-page .sc-bcard:hover{border-color:var(--accent-line);background:var(--paper2)}
#scout-page .sc-bcard .ct{font-family:var(--display);font-weight:600;font-size:20px;line-height:1.15}
#scout-page .sc-bcard .ct .for{font-weight:400;font-size:.75em;color:var(--muted);margin-left:3px}
#scout-page .sc-bcard .cf{font-size:12.5px;color:var(--muted);line-height:1.4}
#scout-page .sc-bcard .cm{margin-top:auto;font-size:12.5px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
#scout-page .sc-bcard .cm b{color:var(--win);font-weight:600}
#scout-page details.fold{margin-top:10px;border-top:1px dashed var(--line);padding-top:6px}
#scout-page details.fold>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.04em;color:var(--accent-deep);padding:4px 0}
#scout-page details.fold>summary::-webkit-details-marker{display:none}
#scout-page details.fold>summary .mchev{display:inline-block;transition:transform .15s}
#scout-page details.fold[open]>summary .mchev{transform:rotate(180deg)}
#scout-page details.fold .rest{margin-top:6px}
#scout-page .sc-area .aud{font-weight:600;color:var(--accent-deep)}
#scout-page .sc-slack{max-width:62ch;padding:28px 0 40px}
#scout-page .sc-slack h1{font-family:var(--display);font-weight:600;font-size:30px;letter-spacing:-.015em;margin:0 0 10px}
#scout-page .sc-slack p{font-size:16px;line-height:1.55;margin:0 0 10px}
#scout-page .sc-slack p.how{color:var(--muted);font-size:14.5px}
#scout-page .sc-slack-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
#scout-page .how{position:relative}
#scout-page .hw-close{position:absolute;top:10px;right:12px;width:32px;height:32px;border:1px solid var(--line);border-radius:999px;background:var(--paper);color:var(--muted);font:400 20px/1 var(--body);cursor:pointer}
#scout-page .hw-close:hover{color:var(--ink);border-color:var(--accent-line)}
#scout-page .briefing[class*=" p-"]{background:var(--pf, var(--paper2));border-color:var(--pl, var(--line));border-top-color:var(--pc, var(--accent-deep))}
#scout-page .briefing[class*=" p-"] .bhead{background:var(--pf, var(--paper));border-bottom-color:var(--pl, var(--line2));color:var(--pc, var(--accent-deep))}
#scout-page .briefing[class*=" p-"] .bbody{background:var(--pf, var(--paper2))}
#scout-page .briefing .filt{font-weight:500;color:var(--pc, var(--accent-deep));font-size:.85em;margin-left:6px}
#scout-page .briefing .clear{font-size:12.5px;font-weight:600;color:var(--pc, var(--accent-deep));text-decoration:none;border-bottom:1px solid currentColor;white-space:nowrap}
#scout-page .aud-none{font-size:14px;color:var(--muted);margin:4px 0 0}
#scout-page .aud-obj{padding:10px 0;border-top:1px solid var(--line2)}
#scout-page .aud-obj:first-child{border-top:0;padding-top:0}
#scout-page .aud-obj h4{margin:0 0 4px;font-family:var(--display);font-size:16px;font-weight:600}
#scout-page .aud-obj .ptop{display:flex;justify-content:flex-end;margin-bottom:2px}
#scout-page .aud-obj p{margin:0 0 6px;font-size:14px}
#scout-page details.sec.folded>summary{opacity:.75}
#scout-page .hw-sys{font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:8px}
#scout-page .hw-asof .live{color:var(--win);font-weight:600;letter-spacing:.04em}
@media(max-width:760px){
  #scout-page .sc-bar{min-height:54px;gap:10px}
  #scout-page .sc-brand .nm{font-size:22px}
  #scout-page .sc-nav > a.sc-navlink{display:none}
  #scout-page details.sc-menu{display:block}
  #scout-page .sc-btn.sc-pri{padding:0 10px;margin-left:0}
  #scout-page .sc-btn.sc-pri .lbl{display:none}
  #scout-page .sc-btn.sc-pri::after{content:"Create"}
  #scout-page .sc-dd{position:fixed;left:12px;right:12px;top:64px;min-width:0;max-height:72vh}
  #scout-page .sc-dd a{white-space:normal;padding:5px 10px;font-size:13px}
  #scout-page .sc-dd a small{font-size:11.5px}
  #scout-page .sc-brand .nm{font-size:20px}
  #scout-page .sc-nav{gap:5px}
  #scout-page .sc-nav .sc-navlink{padding:0 9px;font-size:13.5px;min-height:34px}
  #scout-page .sc-btn.sc-pri{padding:0 9px;min-height:34px;font-size:13.5px}
  #scout-page .sc-btn.sc-slackbtn{display:inline-flex;margin-left:0;padding:0 8px;min-height:34px}
  #scout-page .sc-btn.sc-slackbtn .lbl{display:none}
  #scout-page .sc-dd a.top{flex-direction:row;align-items:center;justify-content:flex-start;gap:10px;min-height:46px;font-size:14.5px;font-weight:600;background:var(--paper2);border:1px solid var(--line);margin-bottom:6px}
  #scout-page .sc-dd a.top svg{width:18px;height:18px;flex:none}
  #scout-page .sc-dd a.top .how-i{color:var(--accent-deep)}
  #scout-page .sc-dd .mh{margin-top:4px;font-size:10.5px;display:flex;align-items:center;gap:8px}
  #scout-page .sc-dd a.brief{margin-left:12px}
  #scout-page .sc-dd a.brief:nth-of-type(odd){background:var(--accent-soft)}
  #scout-page .sc-dd a.brief.on{background:var(--accent-deep);color:#fff}
  #scout-page .sc-brand .nm{font-size:19px}
  #scout-page .sc-nav{gap:4px}
  #scout-page .sc-nav .sc-navlink{padding:0 8px}
  #scout-page .sc-btn.sc-pri{padding:0 8px}
  #scout-page .sc-btn.sc-slackbtn{padding:0 7px}
  #scout-page .sc-dd a.sc-see,#scout-page .sc-dd a.sc-phone-only{flex-direction:row}
  #scout-page .wrap.wrap{padding-left:14px;padding-right:14px}
  #scout-page .sc-lead{padding-top:8px}
  #scout-page .sc-statement{font-size:14px;max-width:none}
  #scout-page .sc-idx{padding-top:12px}
  #scout-page .sc-idx h1{font-size:22px}
  #scout-page .sc-idx .n{display:block;margin:2px 0 0;font-size:12.5px}
  #scout-page .sc-grid{gap:6px;margin-top:10px}
  #scout-page .sc-bcard{min-height:0;padding:9px 12px;gap:2px;flex-direction:row;flex-wrap:wrap;align-items:baseline;justify-content:space-between}
  #scout-page .sc-bcard .ct{font-size:16px;flex:1 1 100%}
  #scout-page .sc-bcard .ct .for{font-size:.85em}
  #scout-page .sc-bcard .cf{font-size:12px}
  #scout-page .sc-bcard .cm{margin-top:0;font-size:11.5px;gap:8px}
  #scout-page .sc-tabs{overflow-x:auto;scrollbar-width:none;-webkit-mask-image:linear-gradient(90deg,#000 88%,transparent)}
  #scout-page .sc-tabs::-webkit-scrollbar{display:none}
  #scout-page details.sc-more{display:none}
  #scout-page .sc-head{align-items:flex-end;flex-direction:row;gap:6px 12px}
  #scout-page .sc-head h1{font-size:24px}
  #scout-page .sc-grid{grid-template-columns:1fr}
}
@media(max-width:1000px) and (min-width:761px){ #scout-page .sc-grid{grid-template-columns:1fr 1fr} }
@media(max-width:380px){ #scout-page .sc-brand .nm{font-size:18px} #scout-page .sc-nav .sc-navlink{padding:0 7px} }
@media(max-width:359px){ #scout-page .sc-btn.sc-slackbtn{display:none} }
/* Phones and portrait tablets (2026-10-02 evening): the brief is the star. The rail drops BELOW
   the brief, its contents + audience become the sticky bar above it, and the metric tiles move to
   the end of the main column. Under 760px the strip and the statement shrink or go. */
#scout-page .sc-obar{display:none}
@media(max-width:980px){
  #scout-page .sc-obar{display:flex;gap:6px;position:sticky;top:0;z-index:40;background:var(--bg);padding:6px 0;border-bottom:1px solid var(--line2);margin-bottom:8px}
  #scout-page .sc-obar details{position:relative;flex:1 1 0;min-width:0}
  #scout-page .sc-obar summary{list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:36px;padding:0 11px;border:1px solid var(--line);border-radius:7px;background:var(--paper);font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #scout-page .sc-obar summary::-webkit-details-marker{display:none}
  #scout-page .sc-obar summary .cv{font-family:var(--mono);font-size:10px;color:var(--muted)}
  #scout-page .sc-obar details[open]>summary{border-color:var(--accent-line);background:var(--paper2)}
  #scout-page .sc-obar .sc-dd{position:absolute;top:calc(100% + 6px);left:0;right:auto;min-width:270px;max-width:calc(100vw - 28px);max-height:70vh}
  #scout-page .sc-obar details.aud .sc-dd{left:auto;right:0}
  #scout-page .sc-obar .sc-dd .g{font-family:var(--mono);font-size:9.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:7px 10px 3px}
  #scout-page .sc-obar .sc-dd .gs{display:none}
  #scout-page .sc-obar .sc-dd a{display:block;padding:7px 10px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink);text-decoration:none;white-space:normal}
  #scout-page .sc-obar .sc-dd a.tr{color:var(--muted)}
  #scout-page .sc-obar .sc-dd .pviews{padding:4px 8px 8px}
  #scout-page .sc-obar .sc-dd .pviews .pv{display:inline-block;padding:5px 10px;font-size:10.5px}
  #scout-page .cols{display:flex;flex-direction:column;align-items:stretch}
  #scout-page .maincol,#scout-page .rail{width:100%}
  #scout-page .ftab-scroll{overflow-x:auto;max-width:100%}
  #scout-page .rail{order:2;margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
  #scout-page .rail .toc,#scout-page .rail .panel.pviews-panel{display:none}
  #scout-page .rail #toc{display:none}
  #scout-page .maincol{display:flex;flex-direction:column;min-width:0}
  #scout-page .maincol>*{min-width:0;max-width:100%}
  #scout-page hr.rule{display:none}
  #scout-page .maincol>.metrics{order:99;margin-top:18px;grid-template-columns:repeat(2,minmax(0,1fr))}
  #scout-page .metric{min-width:0;overflow:hidden}
  #scout-page .metric .mv,#scout-page .metric .cd,#scout-page .metric .sub{white-space:normal}
}
@media(max-width:760px){
  #scout-page .sc-strip{display:none}
  #scout-page .sc-bar{min-height:50px;padding:5px 0}
  #scout-page .sc-lead{padding-top:6px}
  #scout-page .sc-statement{font-size:11.5px;line-height:1.3;max-width:none}   /* back by request: a visitor must know what this is */
  #scout-page hr.rule{display:none}
  #scout-page .sc-head{border-bottom:0;padding:6px 0 2px;gap:0 12px;align-items:baseline;flex-direction:row;flex-wrap:wrap}
  #scout-page .sc-head>div{flex:1 1 100%}
  #scout-page .sc-head h1{font-size:20px;line-height:1.15}
  #scout-page .sc-head h1 .pre{display:none}
  #scout-page .sc-head h1::before{content:"Brief: ";color:var(--muted);font-weight:500}
  #scout-page .sc-area{display:inline;font-size:12px;margin:0}
  #scout-page .sc-head .sc-btn.sc-quiet{display:none}   /* printing is a desktop act; the call sheet stays at /print/<slug> */
  #scout-page .sc-obar{margin:4px 0 2px;padding:4px 0}
  #scout-page .sc-obar summary{min-height:32px;font-size:13px}
}
#scout-page .how{border:1px solid var(--accent-line);background:var(--paper2);border-radius:9px;padding:20px 22px 16px;margin:14px 0 4px;display:flex;flex-direction:column;gap:18px;text-align:left}
#scout-page .how[hidden],#scout-page .how [hidden]{display:none}
#scout-page .hw-h{font-family:var(--display);font-weight:600;font-size:21px;line-height:1.15;letter-spacing:-.01em}
#scout-page .hw-lede{font-size:14px;color:var(--muted);max-width:78ch;margin:5px 0 0;line-height:1.5}
#scout-page .hw-diagram{display:flex;flex-direction:column;gap:9px}
#scout-page .hw-row,#scout-page .hw-lane{display:grid;grid-template-columns:70px minmax(0,1fr);gap:10px;align-items:center}
#scout-page .hw-flow{display:grid;grid-template-columns:repeat(4,1fr) 1.15fr;border:1px solid var(--line);border-radius:7px;background:var(--paper);overflow:hidden}
#scout-page .hw-step{padding:11px 14px 12px;position:relative;border-right:1px solid var(--line2)}
#scout-page .hw-step:last-child{border-right:0}
#scout-page .hw-step:not(:last-child)::after{content:"";position:absolute;right:-6px;top:50%;width:10px;height:10px;background:var(--paper);border-top:1px solid var(--line);border-right:1px solid var(--line);transform:translateY(-50%) rotate(45deg);z-index:1}
#scout-page .hw-dec{background:var(--paper2)}
#scout-page .hw-n{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--faint)}
#scout-page .hw-t{font-family:var(--display);font-weight:600;font-size:17px;line-height:1.2;margin-top:1px}
#scout-page .hw-d{font-size:12.5px;color:var(--muted);line-height:1.42;margin-top:4px}
#scout-page .hw-states{display:flex;flex-direction:column;gap:5px;margin-top:7px}
#scout-page .hw-state{border:1px solid;border-radius:5px;padding:4px 8px;font-size:11.5px;line-height:1.3}
#scout-page .hw-state b{font-family:var(--mono);font-size:10px;letter-spacing:.07em;text-transform:uppercase;font-weight:600;display:block}
#scout-page .hw-state span{color:var(--muted)}
#scout-page .hw-state.pub{background:rgba(47,97,73,.07);border-color:rgba(47,97,73,.26)}
#scout-page .hw-state.pub b{color:var(--win)}
#scout-page .hw-state.cut{background:rgba(154,67,33,.06);border-color:rgba(154,67,33,.24)}
#scout-page .hw-state.cut b{color:var(--cut)}
#scout-page .hw-state.held{background:rgba(138,99,34,.07);border-color:rgba(138,99,34,.26)}
#scout-page .hw-state.held b{color:var(--amber)}
#scout-page .hw-lanes{outline:1px solid var(--line);outline-offset:8px;border-radius:3px;margin:10px 0 8px;display:flex;flex-direction:column;gap:7px}
#scout-page .hw-ln{font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--ink);line-height:1.25}
#scout-page .hw-ln i{display:block;font-style:normal}
#scout-page .hw-track{display:grid;grid-template-columns:repeat(4,1fr) 1.15fr;height:26px;border:1px solid var(--accent-line);background:var(--accent-soft);border-radius:5px;overflow:hidden}
#scout-page .hw-track span{grid-column:1/-1;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:10.5px;font-weight:500;color:var(--accent-deep);padding:0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#scout-page .hw-track.seg span{grid-column:auto;border-right:1px solid var(--accent-line)}
#scout-page .hw-track.seg span.w2{grid-column:span 2;border-right:0}
#scout-page .hw-evh{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-deep);margin-top:5px}
#scout-page .hw-ev p{font-size:12.5px;color:var(--muted);line-height:1.42;margin:1px 0 2px}
#scout-page .hw-cols{display:grid;grid-template-columns:repeat(3,1fr);gap:22px;margin-top:22px}
#scout-page .hw-col h4{margin:0 0 6px;font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-deep);padding-bottom:6px;border-bottom:1px solid var(--line)}
#scout-page .hw-col ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:7px}
#scout-page .hw-col li{font-size:13px;line-height:1.45;color:var(--ink);padding-left:13px;position:relative;margin:0}
#scout-page .hw-col li::before{content:"";position:absolute;left:0;top:.62em;width:5px;height:5px;border-radius:50%;background:var(--accent-line)}
#scout-page .hw-num{font-family:var(--mono);font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;background:var(--line2);border-radius:3px;padding:0 4px;white-space:nowrap}
#scout-page .hw-foot{display:flex;gap:8px 22px;flex-wrap:wrap;align-items:baseline;justify-content:space-between;border-top:1px solid var(--line2);padding-top:11px;font-size:12.5px;color:var(--muted)}
#scout-page .hw-links{display:flex;gap:6px 16px;flex-wrap:wrap}
#scout-page .hw-foot a{color:var(--accent-deep);font-weight:600;text-decoration:none;border-bottom:1px solid var(--accent-line)}
#scout-page .hw-asof{font-family:var(--mono);font-size:10.5px;color:var(--faint)}
@media(max-width:760px){
  #scout-page .hw-row,#scout-page .hw-lane{grid-template-columns:1fr;gap:3px}
  #scout-page .hw-row > .hw-ln{display:none}
  #scout-page .hw-ln i{display:inline}
  #scout-page .hw-flow{grid-template-columns:1fr}
  #scout-page .hw-step{border-right:0;border-bottom:1px solid var(--line2)}
  #scout-page .hw-step:last-child{border-bottom:0}
  #scout-page .hw-step:not(:last-child)::after{right:auto;left:22px;top:auto;bottom:-6px;transform:rotate(135deg)}
  #scout-page .hw-cols{grid-template-columns:1fr;gap:16px}
  #scout-page .how{padding:16px 15px 14px}
  #scout-page .hw-track.seg span{font-size:9.5px;padding:0 2px}
}
@media print{#scout-page .how,#scout-page .sc-strip,#scout-page .sc-nav{display:none!important}}
#scout-page .wrap{padding-left:0;padding-right:0;padding-bottom:32px;}
/* Source-class chip on every citation (2026-09-28): the .persona chip idiom, one notch quieter.
   unknown ("Web") is outlined only, so the eye lands on the classes that carry meaning. */
#scout-page .srcclass{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--accent-deep);background:var(--accent-soft);border:1px solid var(--accent-line);border-radius:4px;padding:2px 6px;white-space:nowrap;line-height:1.3}
#scout-page .srcclass-unknown{color:var(--faint);background:transparent;border-color:var(--line)}
#scout-page .srcclass-filing,#scout-page .srcclass-court,#scout-page .srcclass-government{color:#1f4d2a;background:#e6f1e8;border-color:#bcd8c2}
#scout-page .srcclass-review_site,#scout-page .srcclass-forum{color:#6b5a1e;background:#f7f0dc;border-color:#e2d3a3}
/* Persona palette (2026-09-28): one colour per audience, on the badge of every play and
   objection AND on the rail's audience picker, so the two tie together. Muted hues on the paper
   palette; each pair is text/fill/line. */
#scout-page .p-eng_led{--pc:#1f6f6b;--pf:#e3f1ef;--pl:#b7dad5}
#scout-page .p-technical_evaluator{--pc:#2f4f9e;--pf:#e7ecf8;--pl:#bfcdef}
#scout-page .p-economic_buyer{--pc:#3d6b2e;--pf:#e8f1e2;--pl:#c0dab4}
#scout-page .p-security_regulated{--pc:#8a2f3d;--pf:#f7e7ea;--pl:#e4bcc5}
#scout-page .p-exec_top_down{--pc:#5b3d8c;--pf:#ede7f6;--pl:#cfc1e6}
#scout-page .persona[class*=" p-"]{color:var(--pc);background:var(--pf);border-color:var(--pl)}
#scout-page .persona[class*=" p-"] .pk{color:var(--pc);opacity:.7}
#scout-page .pviews{display:flex;flex-wrap:wrap;gap:6px}
#scout-page .pviews .pv{font-family:var(--mono);font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;padding:3px 9px;border:1px solid var(--line);border-radius:999px;color:var(--muted);background:transparent}
#scout-page .pviews .pv[class*=" p-"]{color:var(--pc);border-color:var(--pl)}
#scout-page .pviews .pv.on{font-weight:600;color:var(--accent-deep);background:var(--accent-soft);border-color:var(--accent-line)}
#scout-page .pviews .pv.on[class*=" p-"]{color:#fff;background:var(--pc);border-color:var(--pc)}
#scout-page .pviews .pv:hover{text-decoration:none;filter:brightness(.92)}
#scout-page .rail-history{margin:10px 0 6px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}
#scout-page .rail-history summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;letter-spacing:.12em;text-transform:uppercase;font-size:9px}
#scout-page .rail-history summary::-webkit-details-marker{display:none}
#scout-page .rail-history .feed{margin-top:6px}
#scout-page .srcchips{display:flex;flex-wrap:wrap;gap:6px 10px;margin:2px 0 8px}
#scout-page .srcrow{display:inline-flex;align-items:center;gap:5px}
#scout-page .srcn{font-family:var(--mono);font-size:10px;color:var(--faint)}
#scout-page .srcall{font-family:var(--mono);font-size:10.5px;color:var(--accent-deep)}
#scout-page .srcpage .srchead{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin:6px 0 10px}
#scout-page .srcpage h2{margin:2px 0 4px;font-size:22px}
#scout-page .srcpage .srclede{margin:0;max-width:62ch;color:var(--muted);font-size:13.5px;line-height:1.5}
#scout-page .srcback{font-family:var(--mono);font-size:11px;color:var(--accent-deep);white-space:nowrap}
#scout-page .srcchips.big{margin:8px 0 14px}
#scout-page .srcwarn{border:1px solid #e2d3a3;background:#f7f0dc;border-radius:8px;padding:10px 14px;margin:0 0 14px;font-size:13px}
#scout-page .srcwarn ul{margin:6px 0 0 18px;padding:0}
#scout-page .srcblurb{margin:0 0 10px;color:var(--muted);font-size:13px}
#scout-page .srchost{padding:8px 0;border-top:1px solid var(--line)}
#scout-page .srchostline{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12px}
#scout-page .srcclaims{margin:4px 0 0 0;padding-left:18px;font-size:13px;line-height:1.5}
#scout-page .srcclaims li{margin:2px 0}
#scout-page .srcclaims .muted{color:var(--faint);font-size:12px}
/* Tighten the top: the masthead + title blocks each sit in a .wrap whose 32px bottom padding
   opened big gaps above the control row and below the title. Trim those two (the content .wrap
   keeps its padding for the page end), pull the tagline up under the name, and close the
   Researched/focus lines under the brief title — lifting the whole header up. */
#scout-page .wrap.mast{padding-bottom:6px;}
#scout-page .wrap.tw{padding-bottom:6px;}
#scout-page .tagline{margin:5px 0 0;}
#scout-page .rt .rt-sub{margin-top:2px;}
#scout-page .rt .rt-focus{margin-top:5px;margin-bottom:12px;}  /* 2026-07-19: half the air above, double below — the chip hugged the rule under it */
#scout-page .rt h1{margin:0;}                                      /* kill default h1 margin — pull the title up (#1) */
#scout-page .rail{position:static;}                                /* rail scrolls with the page, not sticky (#5) */
#scout-page .panel .phead .ey{color:var(--ink);font-weight:600;}  /* much darker rail-panel titles (#3) */
#scout-page{max-width:1240px;margin-left:auto;margin-right:auto;}
#scout-page .maincol{min-width:0;}
#scout-page .rule{margin:2px 0 12px!important;}
#scout-page table.ftab td.secn{color:var(--muted);}
#scout-page [id]{scroll-margin-top:14px;}
#scout-page .rail-credit{font-family:var(--mono);font-size:10.5px;color:var(--faint);
  padding:12px 2px 0;line-height:1.4;}
#scout-page .rail-credit a{color:var(--muted);}
/* Rail feed rows: the mockup's flex row (timestamp column | label) only works for short labels —
   in the 218px rail a real headline got squeezed beside the ~110px timestamp and wrapped one
   word per line. Stack the timestamp ABOVE its label so the text gets the full rail width.
   ("Just updated" rows have no timestamp; their NEW chip stays inline before the text.) */
#scout-page .feed .row{display:block;padding:4px 0;}
#scout-page .feed .row .dt{display:block;margin-bottom:1px;}
#scout-page .feed .row+.row{border-top:1px solid var(--line2);margin-top:3px;padding-top:7px;}
/* Material-changes rows: timestamp + severity/NEW chips on one meta line above the headline. */
#scout-page .feed .rmeta{display:flex;align-items:center;gap:6px;margin-bottom:1px;}
#scout-page .feed .rmeta .dt{margin-bottom:0;}
#scout-page .sev{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:.08em;
  border-radius:4px;padding:0 4px;border:1px solid;line-height:1.5;}
#scout-page .sev.act{color:var(--amber);border-color:var(--amber-line);background:rgba(138,99,34,.09);}
#scout-page .trig{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:.06em;border-radius:4px;
  padding:0 5px;border:1px solid #bcd8c2;color:#1f4d2a;background:#e6f1e8;line-height:1.5;white-space:nowrap;}
#scout-page .sev.watch{color:var(--muted);border-color:var(--line);background:var(--paper2);}
#scout-page .swx{margin-top:2px;}
#scout-page .swx>summary{list-style:none;cursor:pointer;user-select:none;font-family:var(--mono);
  font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-deep);}
#scout-page .swx>summary::-webkit-details-marker{display:none;}
#scout-page .swx>summary::after{content:' \\25be';}
#scout-page .swx[open]>summary::after{content:' \\25b4';}
/* Rail-feed expander ("Show N more"). Same mono micro-link as .swx, but it must override the
   global summary{padding:11px 18px;border-bottom} or it would render like a section header. */
#scout-page .feed .more{margin-top:5px;}
#scout-page .feed .more>summary{list-style:none;cursor:pointer;user-select:none;display:inline-flex;
  font-family:var(--mono);font-size:9px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--accent-deep);padding:2px 0;border-bottom:none;}
#scout-page .feed .more>summary::-webkit-details-marker{display:none;}
#scout-page .feed .more>summary::after{content:' \\25be';}
#scout-page .feed .more[open]>summary::after{content:' \\25b4';}
#scout-page .swb{margin-top:3px;font-size:12px;color:var(--muted);line-height:1.45;
  border-left:2px solid var(--accent-line);padding-left:8px;}
#scout-page .metric .mv .mdelta{font-family:var(--mono);font-size:10.5px;font-weight:600;
  color:var(--win);background:var(--win-soft);border:1px solid var(--win-line);
  border-radius:5px;padding:1px 5px;margin-left:7px;vertical-align:middle;white-space:nowrap;}
/* Per-item NEW chip — the rail's "Just updated" signal shown on the claim itself. Same win-green
   treatment as .mdelta so "new" reads consistently everywhere. */
#scout-page .scout-new{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:.08em;
  color:var(--win);background:var(--win-soft);border:1px solid var(--win-line);border-radius:5px;
  padding:1px 5px;margin-right:8px;vertical-align:middle;white-space:nowrap;display:inline-block;
  transform:translateY(-1px);}
/* Unmonitored cards have no next-check: render the countdown slot muted, not as a live accent. */
#scout-page .metric .cd.cd-off{color:var(--faint);font-weight:500;}
@media(min-width:861px){#scout-page .cols{grid-template-columns:218px 1fr!important;}}
@media(max-width:860px){#scout-page .cols{grid-template-columns:1fr!important;}}
/* Belt-and-braces for phones whose LAYOUT viewport is stuck at Safari's 980px desktop default
   (viewport meta missing/ignored — e.g. the bare /~/+/ frame, or Request Desktop Website):
   max-device-width keys off the PHYSICAL screen, not the layout viewport, so the column still
   stacks. Never matches a desktop monitor, and width-based queries keep ruling when they work. */
@media(max-device-width:640px){
  #scout-page .cols{grid-template-columns:1fr!important;}
  #scout-page .rail{position:static;}
  #scout-page .snap{grid-template-columns:1fr;}
  #scout-page .metrics{grid-template-columns:1fr 1fr;}
}

/* --- Title hierarchy -------------------------------------------------------------------------
   Two top-level sections ("Your Daily Briefing" + "The full brief") read as real headlines, set
   clearly above the section titles (18px) below them. Their subheads ("Today's angle", "Top 3
   plays") are promoted above the play/item names they head — they were dwarfed by them before. */
#scout-page .bhead{padding:13px 18px;}
#scout-page .bhead .l{font-family:var(--display);font-size:20px;font-weight:600;
  letter-spacing:-.01em;text-transform:none;}
#scout-page .divider{margin:18px 0 14px;}
#scout-page .divider .t{font-family:var(--display);font-size:20px;font-weight:600;
  letter-spacing:-.01em;text-transform:none;color:var(--ink);}
#scout-page .bsub{font-family:var(--display);font-size:17px;font-weight:600;
  letter-spacing:-.005em;text-transform:none;color:var(--ink);}
#scout-page .play h4{font-size:17px;}
/* the angle's headline is the same rank as a play's title (it inherited the 14px body size) */
#scout-page .angle .ah{font-family:var(--display);font-size:17px;line-height:1.22;
  letter-spacing:-.01em;margin:0 0 6px;}
#scout-page .angle .ah strong{font-weight:600;}

/* --- Verification trail (2026-09-29) --------------------------------------------------------
   Lineage, Cut Log and claim freshness are the record of how the card was checked, not more of
   the brief. In the rail they sit under a rule as a muted group; in the body a quieter divider
   with a subtitle closes the brief before them. */
#scout-page .toc .grp.trail{margin-top:18px;padding-top:12px;border-top:1px solid var(--line);
  color:var(--faint);}
#scout-page .toc .grpsub{padding:0 10px 4px;font-size:11px;color:var(--faint);}
#scout-page .toc a.tr{color:var(--faint);font-size:11.5px;}
#scout-page .toc a.tr:hover,#scout-page .toc a.tr.on{color:var(--ink);}
#scout-page .divider.trail{margin:38px 0 12px;flex-wrap:wrap;gap:6px 12px;}
#scout-page .divider.trail .t{font-size:15px;color:var(--muted);}
#scout-page .divider.trail .s{font-family:var(--mono);font-size:10.5px;letter-spacing:.02em;
  color:var(--faint);}
#scout-page .divider.trail .ln{min-width:40px;}
#scout-page .divider.trail ~ .sec .stitle{color:var(--muted);}

/* --- Preview sections (snapshot / recent moves / positioning / pricing) ----------------------
   A .sec.preview is a DIV (not <details>), so it needs the card chrome the mockup pins to
   details.sec. It shows a teaser, then a prominent EXPAND toggle in the lower-right corner. */
#scout-page .sec.preview{background:var(--paper);border:1px solid var(--line);border-radius:7px;
  margin-bottom:12px;box-shadow:var(--shadow);overflow:hidden;scroll-margin-top:14px;}
#scout-page .sec.preview .shead{padding:11px 18px;display:flex;align-items:center;gap:11px;
  border-bottom:1px solid var(--line2);}
#scout-page .more{text-align:right;border-top:1px solid var(--line2);
  margin-top:12px;padding-top:12px;}
#scout-page .more>summary{list-style:none;cursor:pointer;user-select:none;
  display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:10.5px;
  font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--accent-deep);
  background:var(--accent-soft);border:1px solid var(--accent-line);border-radius:6px;
  padding:8px 14px;transition:background .12s,color .12s;}
#scout-page .more>summary::-webkit-details-marker{display:none;}
#scout-page .more>summary:hover{background:var(--accent-deep);color:#fff;
  border-color:var(--accent-deep);}
#scout-page .more .lbl-less{display:none;}
#scout-page .more[open]>summary .lbl-more{display:none;}
#scout-page .more[open]>summary .lbl-less{display:inline;}
#scout-page .more .mchev{font-size:10.5px;transition:transform .15s;}
#scout-page .more[open] .mchev{transform:rotate(180deg);}
#scout-page .rest{text-align:left;margin-top:8px;}

/* --- Battlecard zones --------------------------------------------------------------------------
   The three subsections ("Where we win" / "...a fight" / "Where they win") read as one continuous
   block before. Make each a DISTINCT tinted panel — green = we win, amber = a fight, slate = they
   win — with its colored heading over a divider, so a reader sees three clearly separate areas. */
#scout-page .sec .sub.zone{margin-top:16px;padding:13px 16px 6px;border:1px solid var(--line);
  border-left-width:3px;border-radius:8px;}
#scout-page .sec .sub.zone:first-child{margin-top:8px;}
#scout-page .sec .sub.zone.win{background:var(--win-soft);border-color:var(--win-line);}
#scout-page .sec .sub.zone.contested{background:rgba(138,99,34,.09);border-color:var(--amber-line);}
#scout-page .sec .sub.zone.lose{background:var(--accent-soft);border-color:var(--accent-line);}
#scout-page .zhead{display:flex;align-items:baseline;gap:9px;margin-bottom:9px;
  padding-bottom:7px;border-bottom:1px solid var(--line2);}
#scout-page .zlabel{font-family:var(--display);font-size:17px;font-weight:600;letter-spacing:-.005em;}
#scout-page .zlabel.win{color:var(--win);}
#scout-page .zlabel.contested{color:var(--amber);}
#scout-page .zlabel.lose{color:var(--accent-deep);}
#scout-page .sub.zone .subcount{font-family:var(--mono);font-size:9px;letter-spacing:.05em;
  text-transform:uppercase;color:var(--faint);}
#scout-page .sub.zone .zhead + .item{border-top:none;padding-top:0;}

/* --- Group "Top 3 plays" into one panel + box each objection on its own ------------------------
   Same "bring it together in a box" treatment as the battlecard zones. The 3 plays read as a
   single category, so wrap them in one panel matching the "Today's angle" box; each objection is
   its own self-contained Q+counter, so each item becomes its own card. */
#scout-page .playbox{background:var(--paper2);border:1px solid var(--line);
  border-left:3px solid var(--accent-deep);border-radius:0 8px 8px 0;padding:2px 16px 6px;}
#scout-page #objection_handling .sbody>.item,
#scout-page #objection_handling .sbody>.item:first-child{
  border:1px solid var(--line);border-radius:8px;padding:13px 16px;margin-bottom:10px;
  background:var(--paper2);}
#scout-page #objection_handling .sbody>.item:first-child{margin-top:8px;}  /* clear the section divider */
#scout-page #objection_handling .sbody>.item:last-child{margin-bottom:2px;}

/* --- Mobile: stack the item heads ---------------------------------------------------------------
   .ihead is a flex row (title left, persona badge right) and .persona is nowrap, so on a phone the
   badge ate most of the row and crushed the title into a skinny multi-line column (battlecard zones
   + objection handling). Below 640px the badge wraps under the full-width title; same guard for the
   play head (.ptop), whose badge is the identical nowrap chip. */
@media(max-width:640px){
  #scout-page .ihead{flex-wrap:wrap;gap:4px 14px;}
  #scout-page .ihead h4{flex:1 1 100%;}
  #scout-page .ihead .persona{margin-top:0;margin-bottom:4px;}
  #scout-page .ptop{flex-wrap:wrap;}
}
"""


def _style() -> str:
    """The mockup's <style> + overrides. CRITICAL: the mockup has GLOBAL selectors (* / body / a)
    that would clobber Streamlit's own layout if injected as-is, so we re-scope them under
    #scout-page. Everything else is class-based and only matches our injected markup."""
    path = os.path.join(config.APP_ROOT, "docs", "mockups", "command-center.html")
    css = ""
    try:
        with open(path) as f:
            m = re.search(r"<style>(.*?)</style>", f.read(), re.S)
            css = m.group(1) if m else ""
    except OSError:
        pass
    # Scope the global resets to ONLY inside our page, WITHOUT raising specificity — :where()
    # contributes zero specificity, so the mockup's class paddings (.metric/.angle/.callout/…)
    # still win exactly as they do standalone. (A plain `#scout-page *` would be ID-specificity
    # and clobber every box's internal padding to 0 — which is what flattened the boxes.)
    css = css.replace("*{box-sizing:border-box;margin:0;padding:0}",
                      ":where(#scout-page) *{box-sizing:border-box;margin:0;padding:0}")
    css = css.replace(
        "body{font-family:var(--body);color:var(--ink);background:var(--bg);"
        "-webkit-font-smoothing:antialiased;line-height:1.5}",
        ":where(#scout-page){font-family:var(--body);color:var(--ink);"
        "-webkit-font-smoothing:antialiased;line-height:1.5}")
    css = css.replace("a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}",
                      ":where(#scout-page) a{color:var(--accent);text-decoration:none}"
                      ":where(#scout-page) a:hover{text-decoration:underline}")
    return f"<style>{css}{_OVERRIDES}</style>"


def _read_current(slug: str) -> str:
    p = os.path.join(store.battlecard_dir(slug), "current.md")
    if os.path.exists(p):
        with open(p) as f:
            return f.read()
    return ""


def style_block() -> str:
    """The scoped CSS. MUST be injected in its OWN st.markdown call — Streamlit's sanitizer
    drops a <style> block when it's bundled in the same call as body HTML."""
    return _style()


def _tab(slug: str, on: bool) -> str:
    comp, mine, area = brief_parts(store.load_meta(slug))
    label = _html.escape(_name(comp) or slug)
    who = f' <small>for {_html.escape(_name(mine))}</small>' if mine else ""
    tip = _html.escape(f"{comp} for {mine} sales reps. Area: {area}" if mine else f"{comp}. Area: {area}")
    return (f'<a class="sc-tab{" on" if on else ""}" href="/c/{_html.escape(slug)}" title="{tip}"'
            f'{" aria-current=\"page\"" if on else ""}>'
            f'<span class="nm">{label}{who}</span><span class="ar">{_html.escape(area)}</span></a>')


def strip_html(cards: list, slug: str | None) -> str:
    """The briefs strip: the collection, above the brief it contains (2026-10-02)."""
    if not cards:
        return ""
    tabs = "".join(_tab(c, c == slug) for c in cards)
    return ('<div class="sc-strip"><nav class="sc-tabs" id="sc-tabs" aria-label="Briefs">' + tabs + '</nav>'
            '<details class="sc-more" id="sc-more" hidden><summary><span class="n">0</span> more &#9662;</summary>'
            '<div class="sc-dd"></div></details></div>')


def masthead_html(cards: list | None = None, slug: str | None = None, mode: str = "cards") -> str:
    """The top of every page (2026-10-02): the app bar (brand; Briefs with its count, How it
    works, Create your own; a menu on phones), the lead row with the positioning statement, the
    "How this works" panel (closed), and, on a brief, the briefs strip. Card-independent apart
    from the strip; the brief's own header is `title_html`."""
    cards = cards or []
    try:                       # the render path must never crash: no panel -> no door to it
        panel = _how_panel()
    except Exception:
        panel = ""
    n = len(cards)
    how = ('<a href="#how" class="sc-navlink" data-how aria-expanded="false" aria-controls="how">How Scout works</a>'
           if panel else "")
    items = []
    for c in cards:
        comp, mine, area = brief_parts(store.load_meta(c))
        who = f" for {_name(mine)} reps" if mine else ""           # "reps" for context, phones only (this menu is phone-only)
        items.append(f'<a href="/c/{_html.escape(c)}" class="brief{" on" if c == slug else ""}"{" aria-current=\"page\"" if c == slug else ""}>'
                     f'<span>{_html.escape((_name(comp) or c) + who)}</span><small>{_html.escape(area)}</small></a>')
    # Phones only (2026-10-02 night): one menu for everything, "Start Here". Two links with icons
    # first (How Scout works, Agent Scout in Slack), then the heading "Briefs (N)" and the indented
    # list, then the cards page. Desktop keeps the strip and never shows this.
    home = '<a href="/" class="sc-navlink sc-home">Home</a>'
    slack_on = bool(slack_live_paths() or config.SLACK_PREVIEW)
    top_links = (('<a class="top" href="#how" data-how aria-expanded="false">' + _ICON_HOW + '<span>How Scout works</span></a>') if panel else "") \
        + (('<a class="top" href="/slack">' + _ICON_SLACK + '<span>Agent Scout in Slack</span></a>') if slack_on else "")
    all_briefs = ('<details class="sc-menu" id="sc-menu"><summary class="sc-navlink">Start Here'
                  '<span class="cv">&#9662;</span></summary>'
                  f'<div class="sc-dd">{top_links}<div class="mh">Briefs <span class="sc-cnt">{n}</span></div>' + "".join(items)
                  + '<a class="sc-see" href="/briefs">See all briefs as cards</a></div></details>')
    slack_btn = (f'<a href="/slack" class="sc-btn sc-slackbtn" aria-label="Agent Scout in Slack">{_ICON_SLACK}<span class="lbl">Agent Scout in Slack</span></a>'
                 if slack_on else "")
    bar = ('<div class="sc-bar"><a class="sc-brand" href="/"><span class="d"></span><span class="nm">Agent Scout</span></a>'
           '<nav class="sc-nav" aria-label="Site">'
           f'{home}{all_briefs}{how}'
           f'<a class="sc-btn sc-pri{" on" if mode == "create" else ""}" href="/create">{_ICON_PLUS}<span class="lbl">Create your own</span></a>{slack_btn}'
           '</nav></div>')
    lead = f'<div class="sc-lead"><div class="sc-statement">{_STATEMENT}</div></div>'
    strip = strip_html(cards, slug) if slug else ""
    return ('<div id="scout-page"><div class="wrap mast">' + bar + lead + panel + strip + '</div></div>'
            + (_HOW_JS if panel else "") + (_STRIP_JS if strip else ""))


def slack_live_paths() -> list[tuple[str, str, str]]:
    """The Slack actions that are live, as (label, href, kind). Empty = no Slack item anywhere."""
    out = []
    if config.SLACK_INVITE_URL:
        out.append(("Join the demo workspace", config.SLACK_INVITE_URL, "pri"))
    if config.SLACK_APP_ID and config.SLACK_TEAM_ID:
        out.append(("Open Agent Scout in Slack", f"https://slack.com/app_redirect?app={config.SLACK_APP_ID}&team={config.SLACK_TEAM_ID}", ""))
    if config.SLACK_INSTALL_URL:
        out.append(("Add to your Slack", config.SLACK_INSTALL_URL, ""))
    return out


def slack_html() -> str:
    """The /slack page: what it is in two lines, then only the actions that are live."""
    paths = slack_live_paths()
    preview = ('' if paths else '<p class="how"><b>Release candidate preview.</b> The demo workspace is being set up; '
               'this page shows only live actions once it exists.</p>') if config.SLACK_PREVIEW else ""
    btns = "".join(f'<a class="sc-btn{" sc-pri" if kind else ""}" href="{_html.escape(href)}" target="_blank" rel="noopener">'
                   f'{_html.escape(label)}</a>' for label, href, kind in paths)
    return ('<div id="scout-page"><div class="wrap"><div class="sc-slack">'
            '<h1>Agent Scout in Slack</h1>'
            '<p>Ask Agent Scout a question in Slack the way you would ask a colleague. It answers in the '
            'thread from claims verified against their sources, shows the sources and what it cut, and '
            'can research deeper on request. The same engine that writes the briefs here.</p>'
            '<p class="how">Join the demo workspace, then open Agent Scout and ask. Ten quick answers and one '
            'deep research question per person per day.</p>'
            f'{preview}<div class="sc-slack-actions">{btns}</div>'
            '</div></div></div>')


def index_html(cards: list) -> str:
    """The home page (2026-10-02): every brief as a card, most recently updated first."""
    items = []
    for c in cards:
        meta = store.load_meta(c) or {}
        comp, mine, area = brief_parts(meta)
        claims = sum(1 for x in store.load_claims(c) if str(x.get("status", "active")) != "retired")
        lc = meta.get("last_checked") or ""
        when = ""
        try:
            from zoneinfo import ZoneInfo
            d = datetime.fromisoformat(lc).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
            when = f"refreshed {d.strftime('%b')} {d.day}"
        except Exception:
            pass
        who = f'<span class="for">for {_html.escape(_name(mine))} sales reps</span>' if mine else ""
        items.append(f'<a class="sc-bcard" href="/c/{_html.escape(c)}"><div class="ct">{_html.escape(_name(comp) or c)} {who}</div>'
                     f'<div class="cf">Area: {_html.escape(area)}</div>'
                     f'<div class="cm">{("<span><b>&#9679;</b> " + when + "</span>") if when else ""}<span>{claims} claims</span></div></a>')
    n = len(cards)
    return ('<div id="scout-page"><div class="wrap">'
            f'<div class="sc-idx"><h1>All briefs<span class="n">{n} live &middot; refreshed every morning</span></h1></div>'
            '<div class="sc-grid">' + "".join(items) + '</div></div></div>')


def title_html(slug: str, persona: str | None = None) -> str:
    """The brief header in its own row, with Print beside it (the call sheet keeps the audience)."""
    meta = store.load_meta(slug)
    persona = persona_or_none(persona)
    href = f"/print/{slug}" + (f"?persona={persona}" if persona else "")
    return ('<div id="scout-page"><div class="wrap tw">'
            + _title_block(meta, print_href=href, persona=persona) + "</div></div>")


def _prepare_display(claims: list, meta: dict | None = None):
    """Split claims for the viewer and resolve propagated source links. Returns (active, retired):
    a `status: retired` claim leaves the active card for the lineage view (claim-object.md §2.3); a
    propagated claim (no own source_url) borrows the source of the grounded fact it derives_from, so
    its 'Verified · <domain>' line still renders. Returns NEW dicts — never mutates the store list.

    Source class (2026-09-28): the stored `source_class` when the card carries it (stamped at write
    time / by the backfill), else classified at render time from the host with the card's company
    names, so the chip shows on every card before the backfill has landed. A propagated claim
    inherits the class of the source it renders: provenance first, then the parent fact."""
    names = ((meta or {}).get("competitor"), (meta or {}).get("my_company"))
    by_id = {c.get("id"): c for c in claims if c.get("id")}
    active, retired = [], []
    for c in claims:
        c = dict(c)
        prov = c.get("provenance") or {}
        if not c.get("source_url") and c.get("derived_from"):
            parent = by_id.get(c["derived_from"])
            if prov.get("source_url"):
                c["source_url"] = prov["source_url"]
                c["source_class"] = prov.get("source_class") or _classify.classify(prov["source_url"], *names)
                c["source_tier"] = prov.get("source_tier") or c.get("source_tier")
            elif parent and parent.get("source_url"):
                c["source_url"] = parent["source_url"]
                c["source_class"] = parent.get("source_class") or _classify.classify(parent["source_url"], *names)
                c["source_tier"] = parent.get("source_tier") or c.get("source_tier")
        elif c.get("source_url") and not c.get("source_class"):
            c["source_class"] = _classify.classify(c["source_url"], *names)
        (retired if str(c.get("status", "active")) == "retired" else active).append(c)
    return active, retired


def _lineage(retired: list) -> str:
    """The lineage / retired view: plays + objections a propagation retire moved OFF the active card,
    kept as a visible 'tracked since / retired' record — never deleted (claim-object.md §2.3)."""
    if not retired:
        return ""
    rows = []
    for c in sorted(retired, key=lambda c: c.get("retired_on") or "", reverse=True):
        p = _parse_claim(c)
        title = _inline(p["title"]) if p.get("title") else _inline(str(c.get("claim", ""))[:90])
        bits = []
        if c.get("retired_on"):
            bits.append(f'Retired {_html.escape(str(c["retired_on"]))}')
        if c.get("as_of"):
            bits.append(f'tracked since {_html.escape(str(c["as_of"]))}')
        reason = c.get("retired_reason") or "retired"
        rows.append(f'<div class="item retired"><div class="ihead"><h4>{title}</h4></div>'
                    f'<p class="rtmeta"><b>{" · ".join(bits)}</b> — {_inline(reason)}</p>'
                    f'{_vsrc(c)}</div>')
    return _section("lineage", "Lineage — retired plays & objections",
                    f"{len(retired)} retired", "".join(rows))


def _brief_sections(claims: list, md: str, recent_keys: set | None = None, retired: list | None = None):
    """The full-brief body rendered from claim objects (+ Cut Log parsed from md) — the
    part SHARED by the live viewer and the static self-serve render. Returns
    (sections_html, trail_html, present) where `present` is the section-nav list and `trail_html`
    is the VERIFICATION TRAIL (lineage + Cut Log): the record of what Scout checked, cut and
    retired, which reads under its own divider, not as part of the brief (Uroš 2026-09-29: "anything
    below objection handling is how this functions"). `claims` are the ACTIVE claims (callers
    pre-split via _prepare_display); recent_keys: subject_keys a monitor run touched recently."""
    recent_keys = recent_keys or set()
    by_sec = {}
    for c in claims:
        by_sec.setdefault(c.get("section"), []).append(c)

    def _new(c):
        return c.get("subject_key") in recent_keys

    secs, present = [], []
    aud = _PERSONA.get()
    pulled = _pulled_up_ids(claims)

    def _opened(sid, cs):
        """With an audience: open the sections that hold that buyer's own material, plus the one
        or two a buyer reads first (_PERSONA_OPEN). Objection handling closes: the buyer's
        objections were pulled up into the briefing, and open twice reads as a double."""
        if not aud:
            return True
        if sid == "objection_handling":
            return False
        return sid in _PERSONA_OPEN.get(aud, ()) or any(c.get("persona") == aud for c in cs)

    for sid in _section_order():
        if sid in _HIDDEN_SECTIONS:   # generated/stored but not shown (see _HIDDEN_SECTIONS)
            continue
        cs = sorted(by_sec.get(sid, []), key=lambda c: c.get("order", 0))
        if pulled and sid in ("battlecard", "objection_handling"):
            cs = [c for c in cs if c.get("id") not in pulled]      # already shown at the top
        opn = _opened(sid, cs)
        if sid == "recent_moves":
            # A chronological section: the latest development belongs on top, regardless of the
            # `order` the model assigned (monitor-added claims get arbitrary orders). Stable sort,
            # so same-day items keep their model-assigned order.
            cs = sorted(cs, key=lambda c: c.get("as_of") or "", reverse=True)
        if not cs:
            continue
        title = _SECTION_TITLES[sid]
        anchor = "bc" if sid == "battlecard" else sid   # battlecard's card uses id="bc"
        present.append((anchor, title, len(cs)))
        if sid == "battlecard":
            secs.append(_battlecard(cs, recent_keys, open=opn))
        elif sid == "snapshot":
            secs.append(_preview_section(sid, title, f"{len(cs)} facts",
                                         [_snapshot_box(c, _new(c)) for c in cs], 2, snap=True, open=opn))
        elif sid == "executive_summary":
            secs.append(_section(sid, title, f"{len(cs)} takeaways",
                                 "".join(_prose_item(c, callout_label="So what",
                                                     new=_new(c)) for c in cs)))
        elif sid == "objection_handling":
            shown, folded = _split_for_audience(cs)
            item = lambda c: _prose_item(c, callout_label="So what", badge_prefix="Raised by", new=_new(c))
            secs.append(_section(sid, title, f"{len(cs)} objections",
                                 "".join(item(c) for c in shown) + _fold([item(c) for c in folded], "objections"), open=opn))
        elif sid in _PREVIEW_SECTIONS:   # recent_moves, positioning, pricing
            label = {"recent_moves": "moves"}.get(sid, "items")
            secs.append(_preview_section(sid, title, f"{len(cs)} {label}",
                                         [_bullet_item(c, _new(c)) for c in cs], 1, open=opn))
        else:
            label = {"sentiment": "signal"}.get(sid, "items")
            secs.append(_section(sid, title, f"{len(cs)} {label}",
                                 "".join(_bullet_item(c, _new(c)) for c in cs), open=opn))
    trail = []
    lineage_html = _lineage(retired or [])
    if lineage_html:
        trail.append(lineage_html)
        present.append(("lineage", "Lineage", str(len(retired))))
    cut_html, cut_n = _cut_log(md)
    if cut_html:
        trail.append(cut_html)
        present.append(("cut", "Cut Log", str(cut_n)))
    return "".join(secs), "".join(trail), present


# The verification trail: the sections below the brief that record how the card was built and
# checked (lineage, Cut Log, claim freshness). Own group in the rail, own divider in the body.
_TRAIL_IDS = ("lineage", "cut", "claims")
_TRAIL_TITLE = "Verification trail"
_TRAIL_SUB = "Check Scout's work"                    # Uroš 2026-09-29: the group's tag line


def _trail_divider() -> str:
    return (f'<div class="divider trail" id="trail"><span class="t">{_TRAIL_TITLE}</span>'
            f'<span class="s">{_TRAIL_SUB}</span><span class="ln"></span></div>')


def static_brief_html(claims: list, md: str, meta: dict | None = None,
                      briefing: bool = False,
                      briefing_label: str = "Your Daily Briefing",
                      briefing_tag: str = "the 2-min version before your call"
                      ) -> str:
    """Render a one-off brief (e.g. a self-serve card) with the SAME look as the live
    viewer's full brief, MINUS the monitoring furniture — no left rail, metric strip,
    countdown, or freshness table, since those need monitoring state a fresh card has
    no. Title renders when `meta` (competitor/my_company/focus) is supplied. Lets the
    self-serve result share the viewer's exact styling instead of a separate UI.
    `briefing=True` opens with the summary box (the exec-summary leads + top plays — the
    2-minute payoff after a long generation wait); label/tag default to the live viewer's
    header so the monitored-card path renders byte-identically."""
    active, retired = _prepare_display(claims, meta)
    secs, trail, _present = _brief_sections(active, md, retired=retired)
    title = _title_block(meta) if meta else ""
    brief = _briefing(active, label=briefing_label, tag=briefing_tag) if briefing else ""
    inner = (title
             + '<hr class="rule"><div class="maincol">'
             + brief
             + '<div class="divider"><span class="t">The full brief</span>'
               '<span class="ln"></span></div>'
             + secs + (_trail_divider() + trail if trail else "")
             + '</div>')
    return '<div id="scout-page"><div class="wrap">' + inner + '</div></div>'


def content_html(slug: str, persona: str | None = None) -> str:
    """The card body below the title: rule → metric strip + 5-min briefing → full brief →
    freshness, plus the left rail. CSS + masthead + title are injected separately.
    `persona` (2026-09-28): the reader's chosen buyer persona; its plays and objections lead."""
    token = _PERSONA.set(persona_or_none(persona))
    try:
        return _content_html(slug)
    finally:
        _PERSONA.reset(token)


def _content_html(slug: str) -> str:
    status = display.card_status(slug)
    cp = status["checkpoints"]
    # Split retired off the active card (lineage view) and resolve propagated source links once, so
    # every downstream consumer (rail, briefing, sections) sees only active claims with live sources.
    claims, retired = _prepare_display(store.load_claims(slug), store.load_meta(slug))
    status["_claims"] = claims                      # the rail's persona switcher reads what is present
    md = _read_current(slug)
    rows = status["claim_timestamps"]
    secs, trail, present = _brief_sections(claims, md, set(status["recent_keys"]), retired=retired)

    try:
        remaining = int((datetime.fromisoformat(cp["next_check"]) - datetime.now()).total_seconds())
    except (ValueError, TypeError):
        remaining = 0

    plays_n = len([c for c in claims if c.get("section") == "battlecard"
                   and c.get("zone") == "where_we_win"][:3])
    nav_ids = set(re.findall(r'id="(u-[a-z0-9-]+)"', secs))   # anchors that actually render
    _OBAR.set("")
    rail = _rail(status, present, plays_n, nav_ids, sources=(slug, _classify.class_counts(claims)))
    obar = (f'<div class="sc-obar">{_OBAR.get()}</div>'
            "<script>(function(){var b=document.querySelector('.sc-obar');if(!b)return;"
            "b.addEventListener('click',function(e){var a=e.target.closest('a');if(!a)return;"
            "[].forEach.call(b.querySelectorAll('details[open]'),function(d){d.removeAttribute('open');});});"
            "document.addEventListener('click',function(e){if(b.contains(e.target))return;"
            "[].forEach.call(b.querySelectorAll('details[open]'),function(d){d.removeAttribute('open');});});"
            "})();</script>") if _OBAR.get() else ""
    inner = (
        '<hr class="rule">' + obar
        + '<div class="cols">' + rail
        + '<div class="maincol">'
        + _metrics(cp, status["agent_activity"]["claims_tracked"], max(remaining, 0),
                   sum(1 for r in rows if r.get("is_new")))
        + _briefing(claims, clear_href=f"/c/{slug}")
        + '<div class="divider"><span class="t">The full brief</span><span class="ln"></span></div>'
        + secs + _trail_divider() + trail + _freshness(rows)
        + '</div></div>')
    return '<div id="scout-page"><div class="wrap">' + inner + '</div></div>'


_CALL_SHEET_CSS = """
@page{margin:1.3cm;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;color:#1c1d16;
  line-height:1.45;font-size:12px;background:#fff;}
.cs{max-width:780px;margin:0 auto;padding:20px;}
.cs h1{font-family:'Fraunces',Georgia,serif;font-size:20px;font-weight:600;letter-spacing:-.01em;}
.cs .sub{color:#5f5e54;font-size:12px;margin:3px 0 2px;}.cs .sub b{color:#1c1d16;}
.cs .focus{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;color:#2a4658;margin-bottom:6px;}
.cs .lbl{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;letter-spacing:.13em;
  text-transform:uppercase;color:#8a6322;font-weight:600;margin:16px 0 7px;
  border-bottom:1px solid #e6e2d6;padding-bottom:3px;}
.cs .angle{border-left:3px solid #2a4658;padding-left:11px;margin-bottom:10px;}
.cs .angle p{margin-bottom:4px;}
.cs .play{margin-bottom:11px;break-inside:avoid;}
.cs .play .num{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;font-weight:600;
  color:#34566b;letter-spacing:.05em;}
.cs .play .h{font-family:'Fraunces',Georgia,serif;font-size:14px;font-weight:600;line-height:1.25;}
.cs .play .why{margin:3px 0;}
.cs .sb{font-family:'Fraunces',Georgia,serif;font-style:italic;color:#33312a;
  border-left:2px solid #8a6322;padding-left:9px;margin-top:4px;}
.cs .obj{margin-bottom:9px;break-inside:avoid;}
.cs .obj .q{font-weight:600;}
.cs .obj .a{color:#2a4658;margin-top:2px;}
.cs .obj .a .k,.cs .angle .k{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;
  text-transform:uppercase;letter-spacing:.08em;color:#34566b;font-weight:600;}
.cs a{color:inherit;text-decoration:none;}
.cs .ft{margin-top:18px;border-top:1px solid #e6e2d6;padding-top:6px;
  font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;color:#908e82;}
"""


def call_sheet_html(slug: str, persona: str | None = None) -> str:
    """A self-contained, print-optimized one-pager (the pitch + the rebuttals). The print button
    opens this in a fresh window and prints it — so it never touches Streamlit's layout and can't
    be clipped by the scroll container. `persona` puts that buyer's plays and objections first."""
    token = _PERSONA.set(persona_or_none(persona))
    try:
        return call_sheet_from_claims(store.load_claims(slug), store.load_meta(slug))
    finally:
        _PERSONA.reset(token)


def call_sheet_from_claims(claims: list, meta: dict | None) -> str:
    """The call sheet built directly from claim objects + meta — so the self-serve result can
    print the same one-pager as a roster card without the card being in the store."""
    meta = meta or {}
    comp = _html.escape((meta.get("competitor") or "").strip())
    mine = _html.escape((meta.get("my_company") or "").strip())
    focus = _html.escape((meta.get("focus") or "").strip())

    moves = [c for c in claims if c.get("section") == "recent_moves"]
    pri = [c for c in moves if re.search(r"billing|pricing|price|metered",
                                         (c.get("subject_key", "") + c.get("claim", "")), re.I)]
    pool = pri or moves
    angle_html = ""
    if pool:
        a = max(pool, key=lambda c: (c.get("as_of") or "", -c.get("order", 0)))
        p = _parse_claim(a)
        text = " ".join(([p["title"]] if p["title"] else []) + p["body"])
        sw = (f'<p><span class="k">So what:</span> {_inline(p["so_what"])}</p>'
              if p["so_what"] else "")
        angle_html = (f'<div class="lbl">Today\'s angle</div>'
                      f'<div class="angle"><p>{_inline(text)}</p>{sw}</div>')

    wins = _top_wins(claims)
    plays = []
    for i, c in enumerate(wins, 1):
        p = _parse_claim(c)
        why = f'<div class="why">{_inline(" ".join(p["body"]))}</div>' if p["body"] else ""
        sb = f'<div class="sb">{_inline(p["soundbite"])}</div>' if p["soundbite"] else ""
        plays.append(f'<div class="play"><div class="num">PLAY {i:02d}</div>'
                     f'<div class="h">{_inline(p["title"])}</div>{why}{sb}</div>')
    plays_lbl = "Top play" if len(plays) == 1 else f"Top {len(plays)} plays"
    plays_html = (f'<div class="lbl">{plays_lbl}</div>' + "".join(plays)) if plays else ""

    objs = sorted([c for c in claims if c.get("section") == "objection_handling"], key=_pkey)
    obj_items = []
    for c in objs:
        p = _parse_claim(c)
        ans = (f'<div class="a"><span class="k">Counter:</span> {_inline(p["so_what"])}</div>'
               if p["so_what"] else "")
        obj_items.append(f'<div class="obj"><div class="q">{_inline(p["title"])}</div>{ans}</div>')
    obj_html = ('<div class="lbl">Objection handling</div>' + "".join(obj_items)) if obj_items else ""

    sub = f"Researched: <b>{comp}</b>" + (f" · for <b>{mine}</b> reps" if mine else "")
    focus_html = f'<div class="focus">Focus: {focus}</div>' if focus else ""
    body = (f'<div class="cs"><h1>Competitive Brief: Call Sheet</h1>'
            f'<div class="sub">{sub}</div>{focus_html}{angle_html}{plays_html}{obj_html}'
            f'<div class="ft">Agent Scout · every claim verified against its source · '
            f'{len(claims)} claims tracked</div></div>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<title>Call sheet: {comp}</title>{FONT_HEAD}'
            f'<style>{_CALL_SHEET_CSS}</style></head><body>{body}</body></html>')


def render_page(slug: str) -> str:
    """Full standalone HTML document (fonts + CSS + masthead + content) — the static preview.
    The app renders the same pieces inline; this just wraps them so the file stands alone."""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            + FONT_HEAD + style_block()
            + '</head><body style="background:#f4f2ec;margin:0;padding:24px 0">'
            + masthead_html() + title_html(slug) + content_html(slug) + '</body></html>')
