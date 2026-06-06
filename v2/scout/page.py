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
from datetime import datetime

from scout import config, display, store

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
        return (dt.strftime("%b %-d, %Y"), dt.strftime("%-I:%M %p"))
    except ValueError:
        try:
            return (datetime.strptime(s[:10], "%Y-%m-%d").strftime("%b %-d, %Y"), "")
        except ValueError:
            return (s, "")


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
    return (f'<span class="persona"><span class="pk">{_html.escape(prefix)}</span> '
            f'{_html.escape(label)}</span>')


def _verified(url: str, asof: str = "") -> str:
    bits = ['<span class="verified"><span class="tick">✓</span>Verified</span>']
    if url:
        bits.append(f'<span class="sep">·</span><a href="{_html.escape(url)}" target="_blank" '
                    f'rel="noopener">{_html.escape(_domain(url))}</a>')
    if asof:
        bits.append(f'<span class="sep">·</span>{_html.escape(asof)}')
    return '<div class="srcline">' + "".join(bits) + "</div>"


def _callout(kind: str, label: str, text: str) -> str:
    return f'<div class="callout {kind}"><b>{_html.escape(label)}</b>{_inline(text)}</div>'


def _prose_item(c: dict, *, callout_label=None, callout_kind="sw", badge_prefix=None) -> str:
    p = _parse_claim(c)
    badge = _badge(c, badge_prefix) if badge_prefix else ""
    if p["title"]:
        head = (f'<div class="ihead"><h4>{_inline(p["title"])}</h4>{badge}</div>' if badge
                else f'<h4>{_inline(p["title"])}</h4>')
    else:
        head = ""
    body = "".join(f"<p>{_inline(b)}</p>" for b in p["body"])
    call = ""
    if p["soundbite"]:
        call = _callout("sb", "Soundbite", p["soundbite"])
    elif p["so_what"] and callout_label:
        call = _callout(callout_kind, callout_label, p["so_what"])
    return f'<div class="item">{head}{body}{call}{_verified(c.get("source_url",""))}</div>'


def _bullet_item(c: dict) -> str:
    return (f'<div class="item"><p>{_inline(c.get("claim",""))}</p>'
            f'{_verified(c.get("source_url",""), _fmt_asof(c.get("as_of")))}</div>')


def _snapshot_box(c: dict) -> str:
    return (f'<div class="box">{_inline(c.get("claim",""))}'
            f'{_verified(c.get("source_url",""), _fmt_asof(c.get("as_of")))}</div>')


def _section(sid: str, title: str, count_label: str, inner: str) -> str:
    # <details>/<summary> => collapsible, open by default. Survives st.markdown sanitization.
    return (f'<details class="sec" id="{sid}" open><summary>'
            f'<span class="stitle">{_html.escape(title)}</span>'
            f'<span class="scount">{_html.escape(count_label)}</span>'
            f'<span class="chev">›</span></summary>'
            f'<div class="sbody">{inner}</div></details>')


def _battlecard(claims: list) -> str:
    subs = []
    for zid, zlabel, zcls in _ZONES:
        zc = sorted([c for c in claims if c.get("zone") == zid], key=lambda c: c.get("order", 0))
        if not zc:
            continue
        items = "".join(_prose_item(c, badge_prefix="Best for") for c in zc)
        subs.append(f'<div class="sub"><span class="sublabel {zcls}">{_html.escape(zlabel)}</span>'
                    f'<span class="subcount">{len(zc)} item{"s" if len(zc)!=1 else ""}</span>{items}</div>')
    n = len([c for c in claims if c.get("zone")])
    return _section("bc", "Competitive Battlecard", f"{n} across 3 zones", "".join(subs))


def _cut_log(md: str):
    m = re.search(r"^##\s+Cut Log\s*$(.*?)(?=^##\s|\Z)", md, re.S | re.M)
    if not m:
        return "", 0
    rows, n = [], 0
    for line in m.group(1).splitlines():
        mm = re.match(r"-\s+\*\*(CUT|REVISED)\s+—\s+(.*?):\*\*\s*(.*)$", line.strip())
        if not mm:
            continue
        n += 1
        tag, subj, why = mm.group(1), mm.group(2), mm.group(3)
        cls = "cut" if tag == "CUT" else "rev"
        rows.append(f'<div class="cut"><span class="cuttag {cls}">{tag}</span>'
                    f'<div class="body"><b>{_inline(subj)}</b> — {_inline(why)}</div></div>')
    if not rows:
        return "", 0
    note = ('<div class="cutnote">This is what verification removed or corrected during '
            'fact-checking, and why.</div>')
    return _section("cut", "Cut Log", f"{n} removed / revised", note + "".join(rows)), n


def _rail(status: dict, present: list) -> str:
    toc = ['<div class="grp brief first">Your daily briefing</div>',
           '<a href="#brief">Today\'s angle</a>',
           '<a href="#brief2">Top 3 plays <span class="c">3</span></a>',
           '<div class="grp">The full brief</div>']
    for sid, title, n in present:
        toc.append(f'<a href="#{sid}">{_html.escape(title)} <span class="c">{n}</span></a>')
    toc.append('<a href="#claims">Claim freshness <span class="c">'
               f'{len(status["claim_timestamps"])}</span></a>')
    nav = '<div class="toc" id="toc">' + "".join(toc) + "</div>"

    recent = status["recent_updates"]
    ju = ("".join(f'<div class="row"><span class="new">NEW</span>'
                  f'<span>{_html.escape(str(r.get("headline", r.get("subject_key",""))))}</span></div>'
                  for r in recent)
          if recent else '<div class="empty">Nothing updated in the last 24h.</div>')
    feed = status["change_feed"]
    cf = ("".join(f'<div class="row"><span class="dt">{_html.escape(e["date"])}</span>'
                  f'<span>{_html.escape(e["subject"])}</span></div>' for e in feed)
          if feed else '<div class="empty">No changes recorded yet.</div>')
    alerts = display.load_alerts(status["slug"])
    al = ("".join(f'<div class="row"><span class="dt">{_html.escape(a.get("detected_at", a.get("date","")))}'
                  f'</span><span>{_html.escape(str(a.get("headline", a.get("so_what",""))))}</span></div>'
                  for a in alerts)
          if alerts else '<div class="empty">No material changes alerted yet.</div>')
    new_count = sum(1 for r in status["claim_timestamps"] if r.get("is_new"))

    def panel(label, body, extra=""):
        return (f'<div class="panel"><div class="phead"><span class="ey">{label}</span>{extra}</div>'
                f'<div class="feed">{body}</div></div>')

    name = _html.escape(config.AUTHOR_NAME or "Urosh P")
    credit = (f'Built by <a href="{_html.escape(config.AUTHOR_LINKEDIN)}" target="_blank" '
              f'rel="noopener">{name}</a>') if config.AUTHOR_LINKEDIN else f"Built by {name}"
    tip = ('<div class="rail-tip">Tip: print this page (⌘P / Ctrl+P) for a '
           'one-page call sheet.</div>')
    return ('<div class="rail">' + nav
            + panel("Just updated", ju, f'<span class="ph-n">{new_count}</span>')
            + panel("Change feed", cf) + panel("Alerts", al)
            + f'<div class="rail-credit">{credit}</div>' + tip + "</div>")


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
    return (f'<div class="freshness" id="claims"><div class="fhead">'
            f'<div class="ftitle">Claim freshness — {len(rows)} claims '
            f'({new_count} updated &lt;24h)</div>'
            '<div class="fcap"><code>as_of</code> = the date the fact is true as-of · '
            '<code>verified_on</code> = when grounding last confirmed the exact wording · '
            '<code>is_new</code> = a monitor run touched it &lt;24h ago.</div></div>'
            '<div class="ftab-scroll"><table class="ftab"><thead><tr>'
            '<th>subject_key</th><th>section</th><th>as_of</th><th>verified_on</th><th>is_new</th>'
            '</tr></thead><tbody>' + "".join(trs) + "</tbody></table></div></div>")


def _briefing(claims: list) -> str:
    moves = [c for c in claims if c.get("section") == "recent_moves"]
    pri = [c for c in moves if re.search(r"billing|pricing|price|metered",
                                         (c.get("subject_key", "") + c.get("claim", "")), re.I)]
    pool = pri or moves
    angle = max(pool, key=lambda c: (c.get("as_of") or "", -c.get("order", 0))) if pool else None
    angle_html = ""
    if angle:
        p = _parse_claim(angle)
        text = " ".join([p["title"]] + p["body"]) if p["title"] else " ".join(p["body"])
        angle_html = (
            '<div class="bsub">Today\'s angle</div><div class="angle">'
            f'<p>{_inline(text)}</p>'
            + (_callout("sw", "So what for us", p["so_what"]) if p["so_what"] else "")
            + _verified(angle.get("source_url", ""), _fmt_asof(angle.get("as_of"))) + "</div>")

    wins = sorted([c for c in claims if c.get("section") == "battlecard"
                   and c.get("zone") == "where_we_win"], key=lambda c: c.get("order", 0))[:3]
    plays = []
    for i, c in enumerate(wins, 1):
        p = _parse_claim(c)
        badge = _badge(c, "Best for")
        top = (f'<div class="ptop"><span class="num">PLAY {i:02d}</span>{badge}</div>'
               if badge else f'<div class="num">PLAY {i:02d}</div>')
        why = "".join(f"<p>{_inline(b)}</p>" for b in p["body"])
        sb = _callout("sb", "Soundbite", p["soundbite"]) if p["soundbite"] else ""
        plays.append(f'<div class="play">{top}<h4>{_inline(p["title"])}</h4>{why}{sb}'
                     f'{_verified(c.get("source_url",""))}</div>')
    plays_html = ('<div class="bsub two" id="brief2">Top 3 plays</div>' + "".join(plays)) if plays else ""

    return ('<div class="briefing" id="brief"><div class="bhead">'
            '<span class="l"><span class="dot"></span>Your Daily Briefing</span>'
            '<span class="r">the 2-min version before your call · refreshed today</span></div>'
            f'<div class="bbody">{angle_html}{plays_html}</div></div>')


def _metrics(cp: dict, claims_n: int, remaining: int) -> str:
    last_d, last_t = _fmt_dt(cp.get("last_checked_ts") or cp.get("last_checked"))
    next_d, next_t = _fmt_dt(cp.get("next_check"))
    base_d, _ = _fmt_dt(cp.get("baseline_date"))
    if remaining > 0:
        h, m = remaining // 3600, (remaining % 3600) // 60
        cd = f'<div class="cd">in {h}h {m:02d}m</div>'
    else:
        cd = '<div class="cd">update due now</div>'

    def card(label, val, sub_t=""):
        t = f'<span class="t">{_html.escape(sub_t)}</span>' if sub_t else ""
        return f'<div class="metric"><div class="ml">{label}</div><div class="mv">{val}{t}</div></div>'

    return ('<div class="metrics">'
            + card("Last updated", _html.escape(last_d), last_t)
            + f'<div class="metric"><div class="ml">Next update</div>'
              f'<div class="mv">{_html.escape(next_d)}<span class="t">{_html.escape(next_t)}</span></div>{cd}</div>'
            + card("Baseline", _html.escape(base_d))
            + '<div class="metric claims"><div class="ml">Claims tracked &amp; verified</div>'
              f'<div class="mv"><a href="#claims">{claims_n}</a></div>'
              '<div class="sub"><a href="#claims">see all ↓</a></div></div>'
            + "</div>")


def _title_block(meta: dict) -> str:
    comp = _html.escape((meta.get("competitor") or "").strip())
    mine = _html.escape((meta.get("my_company") or "").strip())
    focus = _html.escape((meta.get("focus") or "").strip())
    sub = f"Researched: <b>{comp}</b>" + (f" · For <b>{mine}</b> reps" if mine else "")
    foc = f'<div class="rt-focus">Focus area: {focus}</div>' if focus else ""
    return ('<div class="rt"><h1>Competitive Intelligence Brief</h1>'
            f'<div class="rt-sub">{sub}</div>{foc}</div>')


_LIVE_BOX = (
    '<div class="livebox"><div class="lb-live"><span class="live">'
    '<span class="pulse"></span>LIVE</span></div>'
    '<div class="lb-agents">Monitored by a team of specialized agents (Sonnet &amp; Haiku) '
    '+ 1 orchestrator (Opus).</div></div>')

_TAGLINE = ('<div class="tagline">Living competitive battlecards: Every claim verified for '
            'accuracy and kept current by an orchestra of AI agents.</div>')

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
#scout-page .wrap{padding-left:0;padding-right:0;padding-bottom:32px;}
#scout-page{max-width:1240px;margin-left:auto;margin-right:auto;}
#scout-page .maincol{min-width:0;}
#scout-page table.ftab td.secn{color:var(--muted);}
#scout-page [id]{scroll-margin-top:14px;}
#scout-page .rail-credit{font-family:var(--mono);font-size:10px;color:var(--faint);
  padding:12px 2px 0;line-height:1.4;}
#scout-page .rail-credit a{color:var(--muted);}
#scout-page .rail-tip{font-family:var(--mono);font-size:9.5px;color:var(--faint);
  padding:9px 2px 0;line-height:1.45;}
@media(min-width:861px){#scout-page .cols{grid-template-columns:218px 1fr!important;}}
@media(max-width:860px){#scout-page .cols{grid-template-columns:1fr!important;}}
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


def masthead_html() -> str:
    """Brand + tagline + right-hand LIVE box. Card-independent; rendered once, above the
    in-page mode switch."""
    top = ('<div class="top"><div class="brand"><span class="d"></span>'
           '<span class="nm">Agent Scout</span></div>' + _LIVE_BOX + '</div>')
    return '<div id="scout-page"><div class="wrap mast">' + top + _TAGLINE + '</div></div>'


def content_html(slug: str) -> str:
    """The card body: title → 5-min briefing → full brief → freshness. CSS + masthead are
    injected separately (see style_block / masthead_html)."""
    status = display.card_status(slug)
    cp = status["checkpoints"]
    claims = store.load_claims(slug)
    meta = store.load_meta(slug)
    md = _read_current(slug)
    rows = status["claim_timestamps"]
    by_sec = {}
    for c in claims:
        by_sec.setdefault(c.get("section"), []).append(c)

    secs, present = [], []
    for sid in _SECTION_ORDER:
        cs = sorted(by_sec.get(sid, []), key=lambda c: c.get("order", 0))
        if not cs:
            continue
        title = _SECTION_TITLES[sid]
        anchor = "bc" if sid == "battlecard" else sid   # battlecard's card uses id="bc"
        present.append((anchor, title, len(cs)))
        if sid == "battlecard":
            secs.append(_battlecard(cs))
        elif sid == "snapshot":
            secs.append(_section(sid, title, f"{len(cs)} facts",
                                 '<div class="snap">' + "".join(_snapshot_box(c) for c in cs) + "</div>"))
        elif sid == "executive_summary":
            secs.append(_section(sid, title, f"{len(cs)} takeaways",
                                 "".join(_prose_item(c, callout_label="So what") for c in cs)))
        elif sid == "objection_handling":
            secs.append(_section(sid, title, f"{len(cs)} objections",
                                 "".join(_prose_item(c, callout_label="So what",
                                                     badge_prefix="Raised by") for c in cs)))
        else:
            label = {"recent_moves": "moves", "sentiment": "signal"}.get(sid, "items")
            secs.append(_section(sid, title, f"{len(cs)} {label}",
                                 "".join(_bullet_item(c) for c in cs)))
    cut_html, cut_n = _cut_log(md)
    if cut_html:
        present.append(("cut", "Cut Log", str(cut_n)))

    try:
        remaining = int((datetime.fromisoformat(cp["next_check"]) - datetime.now()).total_seconds())
    except (ValueError, TypeError):
        remaining = 0

    inner = (
        _title_block(meta) + '<hr class="rule">'
        '<div class="cols">' + _rail(status, present)
        + '<div class="maincol">'
        + _metrics(cp, status["agent_activity"]["claims_tracked"], max(remaining, 0))
        + _briefing(claims)
        + '<div class="divider"><span class="t">The full brief</span><span class="ln"></span></div>'
        + "".join(secs) + cut_html + _freshness(rows)
        + '</div></div>')
    return '<div id="scout-page"><div class="wrap">' + inner + '</div></div>'


def render_page(slug: str) -> str:
    """Full standalone HTML document (fonts + CSS + masthead + content) — the static preview.
    The app renders the same pieces inline; this just wraps them so the file stands alone."""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            + FONT_HEAD + style_block()
            + '</head><body style="background:#f4f2ec;margin:0;padding:24px 0">'
            + masthead_html() + content_html(slug) + '</body></html>')
