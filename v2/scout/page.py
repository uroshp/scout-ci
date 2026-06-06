"""Data-driven renderer for the living-battlecard page.

The viewer must look EXACTLY like the approved mockup (docs/mockups/command-center.html),
which Streamlit's own page chrome can't deliver when you merely inject CSS over its widgets.
So we render the whole battlecard as that mockup's OWN self-contained HTML document, filled
from the claim/meta/status data, and (in app_v2) drop it into the page as one components.html
iframe — Streamlit styling can't bleed in.

Single source of truth for styling: the <head> (fonts + <style>) is read verbatim from the
mockup file, so the look can't drift from what was approved. Only the <body> is generated here.

Pure module — no Streamlit import — so the same output can be written to a static preview file
and eyeballed without running the app.
"""
import html as _html
import os
import re
from datetime import datetime

from scout import config, display, store

# subject-key substring -> persona (back-fill / fallback). The authoritative source is the
# claim's own `persona` field once cards are (re)generated; this only fills older cards.
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


# --- inline markdown -> html -------------------------------------------------
def _inline(text: str) -> str:
    s = _html.escape(text or "", quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
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
    """(date, time) human strings for the metric cards, e.g. ('Jun 5, 2026','6:33 PM')."""
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
    """Split a claim's prose into title / body / so_what / soundbite (verbatim, no rewrite)."""
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


def _persona_of(c: dict) -> str | None:
    return c.get("persona")


def _badge(c: dict, prefix: str) -> str:
    label = _PERSONA_LABELS.get(_persona_of(c))
    if not label:
        return ""
    return (f'<span class="persona"><span class="pk">{_html.escape(prefix)}</span> '
            f'{_html.escape(label)}</span>')


# --- shared fragments --------------------------------------------------------
def _verified(url: str, asof: str = "", grounded: str = "") -> str:
    bits = ['<span class="verified"><span class="tick">✓</span>Verified</span>']
    if url:
        bits.append(f'<span class="sep">·</span><a href="{_html.escape(url)}" target="_blank" '
                    f'rel="noopener">{_html.escape(_domain(url))}</a>')
    if asof:
        bits.append(f'<span class="sep">·</span>{_html.escape(asof)}')
    return '<div class="srcline">' + "".join(bits) + "</div>"


def _callout(kind: str, label: str, text: str) -> str:
    return (f'<div class="callout {kind}"><b>{_html.escape(label)}</b>{_inline(text)}</div>')


# --- section renderers -------------------------------------------------------
def _prose_item(c: dict, *, callout_label=None, callout_kind="sw", badge_prefix=None) -> str:
    p = _parse_claim(c)
    title = _inline(p["title"])
    badge = _badge(c, badge_prefix) if badge_prefix else ""
    head = (f'<div class="ihead"><h4>{title}</h4>{badge}</div>' if badge
            else f"<h4>{title}</h4>") if p["title"] else ""
    body = "".join(f"<p>{_inline(b)}</p>" for b in p["body"])
    call = ""
    if p["soundbite"]:
        call = _callout("sb", "Soundbite", p["soundbite"])
    elif p["so_what"] and callout_label:
        call = _callout(callout_kind, callout_label, p["so_what"])
    return (f'<div class="item">{head}{body}{call}'
            f'{_verified(c.get("source_url",""))}</div>')


def _bullet_item(c: dict) -> str:
    return (f'<div class="item"><p>{_inline(c.get("claim",""))}</p>'
            f'{_verified(c.get("source_url",""), _fmt_asof(c.get("as_of")))}</div>')


def _snapshot_box(c: dict) -> str:
    return (f'<div class="box">{_inline(c.get("claim",""))}'
            f'{_verified(c.get("source_url",""), _fmt_asof(c.get("as_of")))}</div>')


def _section(sid: str, title: str, count_label: str, inner: str) -> str:
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


def _cut_log(md: str) -> str:
    m = re.search(r"^##\s+Cut Log\s*$(.*?)(?=^##\s|\Z)", md, re.S | re.M)
    if not m:
        return ""
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
        return ""
    note = ('<div class="cutnote">This is what verification removed or corrected during '
            'fact-checking, and why.</div>')
    return _section("cut", "Cut Log", f"{n} removed / revised", note + "".join(rows))


# --- the four "show the work" rail modules + freshness table -----------------
def _rail(status: dict, sections_present: list) -> str:
    toc = ['<div class="grp brief first">Your daily briefing</div>',
           '<a href="#brief" class="on">Today\'s angle</a>',
           '<a href="#brief2">Top 3 plays <span class="c">3</span></a>',
           '<div class="grp">The full brief</div>']
    for sid, title, n in sections_present:
        toc.append(f'<a href="#{sid}">{_html.escape(title)} <span class="c">{n}</span></a>')
    toc.append('<a href="#claims">Claim freshness <span class="c">'
               f'{len(status["claim_timestamps"])}</span></a>')
    nav = '<nav class="toc" id="toc">' + "".join(toc) + "</nav>"

    recent = status["recent_updates"]
    if recent:
        ju = "".join(
            f'<div class="row"><span class="new">NEW</span>'
            f'<span>{_html.escape(str(r.get("headline", r.get("subject_key",""))))}</span></div>'
            for r in recent)
    else:
        ju = '<div class="empty">Nothing updated in the last 24h.</div>'
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

    return ('<aside class="rail">' + nav
            + panel("Just updated", ju, f'<span class="ph-n">{new_count}</span>')
            + panel("Change feed", cf)
            + panel("Alerts", al)
            + "</aside>")


def _freshness(rows: list) -> str:
    new_count = sum(1 for r in rows if r.get("is_new"))
    trs = []
    for r in rows:
        isnew = r.get("is_new")
        trs.append(
            f'<tr><td class="key">{_html.escape(str(r.get("subject_key","")))}</td>'
            f'<td class="sec">{_html.escape(str(r.get("section","")))}</td>'
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


# --- the 5-minute briefing (Today's angle + Top 3 plays) ---------------------
def _briefing(claims: list) -> str:
    moves = [c for c in claims if c.get("section") == "recent_moves"]
    # Today's angle = the sharpest opener, not merely the newest: prefer a pricing/billing
    # disruption (the strongest sales wedge) if one is live, else the freshest recent move.
    angle = None
    pri = [c for c in moves if re.search(r"billing|pricing|price|metered",
                                         (c.get("subject_key", "") + c.get("claim", "")), re.I)]
    pool = pri or moves
    if pool:
        angle = max(pool, key=lambda c: (c.get("as_of") or "", -c.get("order", 0)))
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


# --- masthead ----------------------------------------------------------------
def _metrics(cp: dict, claims_n: int) -> str:
    last_d, last_t = _fmt_dt(cp.get("last_checked_ts") or cp.get("last_checked"))
    next_d, next_t = _fmt_dt(cp.get("next_check"))
    base_d, _ = _fmt_dt(cp.get("baseline_date"))

    def card(label, val, sub_t="", cd=False, claims=False):
        t = f'<span class="t">{_html.escape(sub_t)}</span>' if sub_t else ""
        cls = "metric claims" if claims else "metric"
        cdiv = '<div class="cd" id="cd"></div>' if cd else ""
        return f'<div class="{cls}"><div class="ml">{label}</div><div class="mv">{val}{t}</div>{cdiv}</div>'

    claims_val = f'<a href="#claims">{claims_n}</a>'
    claims_sub = '<div class="sub"><a href="#claims">see all ↓</a></div>'
    return ('<div class="metrics">'
            + card("Last updated", _html.escape(last_d), last_t)
            + card("Next update", _html.escape(next_d), next_t, cd=True)
            + card("Baseline", _html.escape(base_d))
            + f'<div class="metric claims"><div class="ml">Claims tracked &amp; verified</div>'
              f'<div class="mv">{claims_val}</div>{claims_sub}</div>'
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

_COUNTDOWN_JS = """<script>
(function(){
 var el=document.getElementById('cd');
 if(el){var s=__R__;(function t(){if(s<=0){el.textContent='update due now';return;}
   var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=Math.floor(s%60);
   el.textContent='in '+h+'h '+String(m).padStart(2,'0')+'m '+String(x).padStart(2,'0')+'s';s--;
   setTimeout(t,1000);})();}
 // When embedded (components.html iframe), grow the iframe to the content height so the page
 // scrolls as ONE document — no internal scrollbar. No-op as a standalone file (no frameElement).
 function fit(){try{if(!window.frameElement)return;
   var h=document.documentElement.scrollHeight;
   window.frameElement.style.height=h+'px';
   var w=window.frameElement.parentElement; if(w)w.style.height=h+'px';}catch(e){}}
 window.addEventListener('load',fit);[60,300,900,1800].forEach(function(d){setTimeout(fit,d);});
 if(window.ResizeObserver){new ResizeObserver(fit).observe(document.body);}
 document.querySelectorAll('details').forEach(function(d){d.addEventListener('toggle',fit);});
 // Anchor jumps: scroll the PARENT (the iframe is full-height, so it can't scroll itself).
 function jump(id){var t=document.querySelector(id);if(!t)return;
   try{ if(window.frameElement){
     var y=window.parent.scrollY+window.frameElement.getBoundingClientRect().top
           +t.getBoundingClientRect().top-12;
     window.parent.scrollTo({top:y,behavior:'smooth'});
   } else { t.scrollIntoView({behavior:'smooth',block:'start'}); } }
   catch(e){ try{t.scrollIntoView();}catch(_){} } }
 [].slice.call(document.querySelectorAll('a[href^="#"]')).forEach(function(a){
   a.addEventListener('click',function(ev){var href=a.getAttribute('href');
     if(href&&href.length>1){ev.preventDefault();jump(href);}});});
})();
</script>"""


def _head() -> str:
    """Reuse the approved mockup's <head> (fonts + <style>) verbatim — single source of truth
    for styling, so the rendered page cannot drift from what was signed off."""
    path = os.path.join(config.APP_ROOT, "docs", "mockups", "command-center.html")
    with open(path) as f:
        doc = f.read()
    m = re.search(r"<head>(.*?)</head>", doc, re.S)
    return m.group(1) if m else "<meta charset='utf-8'>"


def render_page(slug: str) -> str:
    """Full self-contained HTML document for one battlecard, in the approved mockup's look."""
    status = display.card_status(slug)
    cp = status["checkpoints"]
    claims = store.load_claims(slug)
    meta = store.load_meta(slug)
    md = _read_current(slug)
    rows = status["claim_timestamps"]
    by_sec = {}
    for c in claims:
        by_sec.setdefault(c.get("section"), []).append(c)

    # full-brief sections, in canonical order, each as an open <details> card
    secs, present = [], []
    for sid in _SECTION_ORDER:
        cs = sorted(by_sec.get(sid, []), key=lambda c: c.get("order", 0))
        if not cs:
            continue
        title = _SECTION_TITLES[sid]
        present.append((sid, title, len(cs)))
        if sid == "battlecard":
            secs.append(_battlecard(cs))
        elif sid == "snapshot":
            boxes = "".join(_snapshot_box(c) for c in cs)
            secs.append(_section(sid, title, f"{len(cs)} facts",
                                 f'<div class="snap">{boxes}</div>'))
        elif sid == "executive_summary":
            items = "".join(_prose_item(c, callout_label="So what") for c in cs)
            secs.append(_section(sid, title, f"{len(cs)} takeaways", items))
        elif sid == "objection_handling":
            items = "".join(_prose_item(c, callout_label="So what", badge_prefix="Raised by")
                            for c in cs)
            secs.append(_section(sid, title, f"{len(cs)} objections", items))
        else:  # recent_moves / positioning / pricing / sentiment — plain bullets
            label = {"recent_moves": "moves", "sentiment": "signal"}.get(sid, "items")
            items = "".join(_bullet_item(c) for c in cs)
            secs.append(_section(sid, title, f"{len(cs)} {label}", items))
    cut = _cut_log(md)
    if cut:
        present.append(("cut", "Cut Log", "8"))

    # next-check countdown seconds
    try:
        remaining = int((datetime.fromisoformat(cp["next_check"]) - datetime.now()).total_seconds())
    except (ValueError, TypeError):
        remaining = 0

    body = (
        '<div class="wrap">'
        '<div class="top"><div class="brand"><span class="d"></span>'
        '<span class="nm">Agent Scout</span></div>' + _LIVE_BOX + '</div>'
        + _TAGLINE + _title_block(meta)
        + '<hr class="rule">'
        '<div class="cols">' + _rail(status, present)
        + '<main>' + _metrics(cp, status["agent_activity"]["claims_tracked"])
        + _briefing(claims)
        + '<div class="divider"><span class="t">The full brief</span><span class="ln"></span></div>'
        + "".join(secs) + cut + _freshness(rows)
        + '</main></div></div>'
        + _COUNTDOWN_JS.replace("__R__", str(max(remaining, 0))))
    return f"<!doctype html><html lang=\"en\"><head>{_head()}</head><body>{body}</body></html>"


def _read_current(slug: str) -> str:
    path = os.path.join(store.battlecard_dir(slug), "current.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""
