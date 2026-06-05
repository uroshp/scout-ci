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

from scout import config, display, selfserve, store

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
  font-size:1rem; font-weight:600; line-height:1.2; color:inherit;
  border-left:3px solid transparent;
  transition:background .15s ease, color .15s ease, border-color .15s ease; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover{
  background:rgba(34,168,136,.10); color:#2a8; }
/* The radio dot is the label's first child div (the text is a later child); hide it for a
   clean nav look. The <input> is a direct child of the label, so it stays for accessibility. */
section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child{
  display:none; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked){
  background:rgba(34,168,136,.14); color:#2a8; border-left-color:#2a8; }
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
#scout-brief h3 { margin: 2rem 0 1rem; color: #888; text-transform: uppercase;
                  letter-spacing: .04em; font-size: .9rem; }
#scout-brief .block { margin: 0 0 1.9rem; padding-left: .9rem;
                      border-left: 3px solid rgba(34,168,136,.5); }
#scout-brief .block .btitle { font-size: 1.3rem; font-weight: 700; line-height: 1.3;
                              margin: 0 0 .6rem; }
#scout-brief .block .bbody { margin: .55rem 0 .55rem 1.1rem; }
#scout-brief .block .bbody:last-child { margin-top: .7rem; opacity: .92; }
#scout-brief p { margin: .6rem 0; }
#scout-brief ul { padding-left: 1.2rem; margin: .6rem 0; }
#scout-brief li { margin-bottom: .4rem; }
#scout-brief a { color: #2a8; text-decoration: none; }
#scout-brief a:hover { text-decoration: underline; }
#scout-brief code { background: rgba(136,136,136,.2); padding: .1rem .3rem; border-radius: 4px; }
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


def _render_brief_html(md: str) -> str:
    """md -> raw HTML with the prose-block hierarchy. Each full-line-bold title plus
    its following body paragraphs is grouped into a .block; headings and bullet lists
    are breaks (never absorbed), matching scripts/render_static.py."""
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
            parts = [f'<p class="btitle">{_inline_md(val[2:-2].strip())}</p>']
            i += 1
            while i < len(tokens) and tokens[i][0] == "body":
                parts.append(f'<p class="bbody">{_inline_md(tokens[i][1])}</p>')
                i += 1
            out.append(f'<div class="block">{"".join(parts)}</div>')
        elif kind == "ul":
            out.append("<ul>" + "".join(f"<li>{_inline_md(b)}</li>" for b in val) + "</ul>")
            i += 1
        elif kind.startswith("h"):
            out.append(f'<{kind} id="{_slug(val)}">{_inline_md(val)}</{kind}>')
            i += 1
        else:  # a stray body paragraph not preceded by a title
            out.append(f"<p>{_inline_md(val)}</p>")
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
#scout-rt { margin:.2rem 0 1.1rem; }
#scout-rt h1 { font-size:1.85rem; font-weight:800; line-height:1.2; margin:0; }
#scout-rt .rt-focus { font-weight:600; color:#2a8; }
#scout-5min { margin:.2rem 0 1.6rem; }
#scout-5min .fm-lbl { color:#2a8; font-size:.78rem; font-weight:700; text-transform:uppercase;
                      letter-spacing:.06em; margin:0 0 .55rem; }
#scout-5min .fm-angle { border-left:3px solid rgba(34,168,136,.6); padding:.1rem 0 .1rem .9rem;
                        margin-bottom:1.6rem; }
#scout-5min .fm-atext { font-size:1.12rem; line-height:1.5; margin:.1rem 0 .5rem; }
#scout-5min .fm-src { font-size:.8rem; color:#888; }
#scout-5min .fm-src a, #scout-5min a { color:#2a8; text-decoration:none; }
#scout-5min .fm-src a:hover, #scout-5min a:hover { text-decoration:underline; }
#scout-5min .fm-plays { display:grid; gap:1rem; }
#scout-5min .fm-play { border:1px solid rgba(136,136,136,.25);
                       border-left:3px solid rgba(34,168,136,.5); border-radius:10px; padding:.85rem 1.05rem; }
#scout-5min .fm-pt { font-size:1.1rem; font-weight:700; line-height:1.3; margin-bottom:.35rem; }
#scout-5min .fm-pwhy { margin:.25rem 0 .55rem; line-height:1.45; }
#scout-5min .fm-sb { margin:.5rem 0 0; padding-top:.5rem; border-top:1px dashed rgba(136,136,136,.35);
                     font-size:.93rem; color:#9a9a9a; }
#scout-5min .fm-sb em { color:inherit; }
#scout-5min .fm-sb-lbl { font-weight:700; color:#2a8; font-style:normal; }
</style>"""


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
        body_text = " ".join(pa["body"])
        inner = (f'<strong>{_inline_md(pa["title"])}</strong> {_inline_md(body_text)}'
                 if pa["title"] else _inline_md(body_text))
        src, asof = a.get("source_url", ""), a.get("as_of", "")
        src_line = (f'<div class="fm-src"><b>{html.escape(asof)}</b> · '
                    f'<a href="{html.escape(src)}" target="_blank" rel="noopener">'
                    f'{html.escape(_domain(src))}</a></div>') if src else ""
        angle_html = ('<div class="fm-angle"><div class="fm-lbl">Today\'s angle</div>'
                      f'<p class="fm-atext">{inner}</p>{src_line}</div>')

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
        plays.append(f'<div class="fm-play"><div class="fm-pt">{_inline_md(p["title"])}</div>'
                     f'{why}{sb}</div>')
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
    """The master report title, shown above Today's angle. Appends the focus area
    (from meta) when the report has one, so visitors see what it covers."""
    m = re.search(r"^#\s+(.*)$", md, flags=re.M)
    title = m.group(1).strip() if m else "Competitive Intelligence Brief"
    focus = (meta or {}).get("focus")
    focus_html = f'<span class="rt-focus"> · {html.escape(focus)}</span>' if focus else ""
    return f'<div id="scout-rt"><h1>{html.escape(title)}{focus_html}</h1></div>'


_TOC_CSS = """<style>
#scout-toc { margin-bottom:.4rem; }
#scout-toc .toc-lbl { color:#2a8; font-size:.78rem; font-weight:700; text-transform:uppercase;
                      letter-spacing:.06em; margin-bottom:.45rem; }
#scout-toc ul { list-style:none; margin:0; padding:0; border-left:2px solid rgba(136,136,136,.2); }
#scout-toc li a { display:block; padding:.2rem 0 .2rem .75rem; margin-left:-2px; color:inherit;
                  text-decoration:none; font-size:.9rem; line-height:1.35;
                  border-left:2px solid transparent; }
#scout-toc li a:hover { color:#2a8; border-left-color:#2a8; }
#scout-toc .toc-h3 a { padding-left:1.5rem; font-size:.82rem; color:#888; }
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
 #ms{font-family:-apple-system,system-ui,"Segoe UI",Roboto,sans-serif;color-scheme:light dark;color:#1a1a1a;}
 @media (prefers-color-scheme: dark){ #ms{color:#fafafa;} }
 #ms .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:.6rem;}
 /* On a narrow (mobile) viewport, 4 cramped columns clip their values — go 2x2 instead. */
 @media (max-width:520px){ #ms .grid{grid-template-columns:repeat(2,1fr);} }
 #ms .m{border:1px solid rgba(136,136,136,.35);border-radius:10px;padding:.7rem .85rem .85rem;
        min-height:62px;display:flex;flex-direction:column;justify-content:center;}
 #ms .ml{color:#888;font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;}
 #ms .mv{font-size:1.5rem;font-weight:800;line-height:1.15;margin-top:.18rem;}
 #ms .mv .t{display:block;font-size:.82rem;font-weight:500;color:#9a9a9a;margin-top:.12rem;}
 #ms .cd{color:#2a8;font-weight:700;font-size:.78rem;font-variant-numeric:tabular-nums;margin-top:.3rem;}
 #ms .cd.due{color:#e85;}
 #ms .sub{color:#888;font-size:.72rem;margin-top:.3rem;}
 #ms .mlink{color:inherit;text-decoration:none;border-bottom:2px solid rgba(34,168,136,.55);}
 #ms .mlink:hover{border-bottom-color:#2a8;}
 #ms .sub a{color:#2a8;text-decoration:none;}
 #ms .sub a:hover{text-decoration:underline;}
</style>
<div class="grid">
 <div class="m"><div class="ml">Last checked</div><div class="mv" data-utc="__LAST__">—</div></div>
 <div class="m"><div class="ml">Next check</div><div class="mv" data-utc="__NEXT__">—</div><div class="cd" id="cd"></div></div>
 <div class="m"><div class="ml">Baseline</div><div class="mv">__BASE__</div></div>
 <div class="m"><div class="ml">Verified claims</div><div class="mv"><a class="mlink" href="#verified-claims" target="_parent">__N__</a></div><div class="sub"><a href="#verified-claims" target="_parent">see all ↓</a> · cadence __CAD__h</div></div>
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
  if(l<=0){cd.textContent='check due now';cd.classList.add('due');return;}
  var h=Math.floor(l/3600),m=Math.floor(l%3600/60),s=Math.floor(l%60);
  cd.textContent='next check in '+h+'h '+String(m).padStart(2,'0')+'m '+String(s).padStart(2,'0')+'s';}
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
        st.markdown(f"**DM for access:** **{config.SELFSERVE_CONTACT}**")
    else:
        st.error(res.get("message", "Something went wrong generating this report."))


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

    gate = selfserve.gate()
    if not gate["open"]:
        st.warning("The free launch window is full.")
        st.markdown(f"**DM me for access:** **{gate.get('contact', config.SELFSERVE_CONTACT)}**")
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
        if not selfserve.gate()["open"]:                # re-check; the view can be stale
            st.warning("The free window just closed. DM for access.")
            return
        req = selfserve.submit(competitor, my_company, focus)
        st.session_state["selfserve_job"] = req["job_id"]
        st.query_params["job"] = req["job_id"]
        st.rerun()


def main():
    icon = _asset("scout_icon_t.png", "scout_icon.png")
    logo = _asset("scout_logo_t.png", "scout_logo.png")
    # Cropped, margin-free version for the header so the dog sits tight against the name.
    head_logo = _asset("scout_logo_crop_t.png") or logo
    st.set_page_config(page_title="Agent Scout — Living Battlecards", layout="wide",
                       page_icon=icon or "🐕")
    if logo:
        st.logo(logo, icon_image=icon)
    # Header rendered as one inline flex row (logo then name, small gap) so it reads like a
    # normal site masthead instead of two stretched columns with a big void between them.
    if head_logo:
        with open(head_logo, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        st.markdown(
            '<div style="display:flex;align-items:center;gap:.45rem;margin:.2rem 0 .4rem;">'
            f'<img src="data:image/png;base64,{b64}" style="height:88px;width:auto;" alt="Agent Scout">'
            '<span style="font-size:2.6rem;font-weight:800;line-height:1;">Agent Scout</span>'
            '</div>',
            unsafe_allow_html=True)
    else:
        st.title("Agent Scout")
    st.caption("Living competitive battlecards — every claim verified against its source, "
               "and kept current by an agent.")

    # Mode: the public living-battlecard viewer, or the gated 'create your own' entry point.
    # A ?job= URL (a returning self-serve visitor) forces the create view.
    job_param = st.query_params.get("job")
    st.sidebar.markdown(_NAV_CSS, unsafe_allow_html=True)
    st.sidebar.markdown("###### Navigate")
    mode = st.sidebar.radio(
        "Mode", ["📋 Living battlecards", "✨ Create your own"],
        index=1 if job_param else 0, label_visibility="collapsed")
    if job_param or mode == "✨ Create your own":
        _render_selfserve(job_param)
        return

    cards = display.list_battlecards()
    if not cards:
        st.info("No battlecards have been generated yet.")
        return

    slug = st.sidebar.selectbox("Choose a battlecard", cards, format_func=_pretty)
    # Switching battlecards should land you at the TOP of the new card, not at whatever
    # scroll depth you'd reached in the previous one. Streamlit preserves scroll across
    # reruns, so we reset it ourselves the first render after the selection changes.
    if st.session_state.get("_last_slug") != slug:
        st.session_state["_last_slug"] = slug
        components.html(
            "<script>var d=window.parent.document;"
            "['section.main','.stMain','[data-testid=\"stMain\"]',"
            "'[data-testid=\"stAppViewContainer\"]'].forEach(function(s){"
            "var e=d.querySelector(s);if(e)e.scrollTo({top:0});});"
            "window.parent.scrollTo({top:0});</script>",
            height=0)
    status = display.card_status(slug)
    cp, act = status["checkpoints"], status["agent_activity"]
    recent = status["recent_updates"]
    rows = status["claim_timestamps"]
    new_count = sum(1 for r in rows if r.get("is_new"))

    # --- Elements 1 + 3: the monitoring / freshness strip (kept) ---
    st.markdown(f"**{act['line']}**")
    try:
        remaining = int((datetime.fromisoformat(cp["next_check"]) - datetime.now()).total_seconds())
    except (ValueError, TypeError):
        remaining = 0
    strip = (_METRICS_STRIP
             .replace("__LAST__", str(cp["last_checked_ts"] or cp["last_checked"] or ""))
             .replace("__NEXT__", str(cp["next_check"] or ""))
             .replace("__BASE__", _fmt_date_human(cp["baseline_date"]))
             .replace("__N__", str(act["claims_tracked"]))
             .replace("__CAD__", str(cp["cadence_hours"]))
             .replace("__R__", str(max(remaining, 0))))
    # Initial height covers the mobile 2x2 layout so nothing is clipped before fitFrame() runs;
    # the in-iframe script then shrinks it to the real content height (one row on desktop).
    components.html(strip, height=170)
    st.divider()

    md = _read_current(slug)
    meta = store.load_meta(slug)
    brief_col, side_col = st.columns([3, 1], gap="large")

    with brief_col:
        st.markdown(_FIVE_MIN_CSS, unsafe_allow_html=True)
        # Master report title (with focus) sits above Today's angle so visitors
        # immediately see what this is.
        st.markdown(_report_title_html(md, meta), unsafe_allow_html=True)
        # 5-minute brief — derived from the verified claims (no new prose).
        st.markdown(_five_min_html(store.load_claims(slug)), unsafe_allow_html=True)
        # Full verified brief — always visible, full Executive Summary included,
        # title stripped (rendered above).
        st.markdown(_BRIEF_CSS, unsafe_allow_html=True)
        st.markdown(_render_brief_html(_strip_h1(md)), unsafe_allow_html=True)

    with side_col:
        # --- Jump navigation over the brief's sections ---
        st.markdown(_TOC_CSS, unsafe_allow_html=True)
        st.markdown(_toc_html(md), unsafe_allow_html=True)
        st.divider()
        # --- A4: claims a monitor run touched in the last 24h (empty on a fresh baseline) ---
        st.subheader(f"Just updated ({new_count})")
        if recent:
            for r in recent:
                when = r.get("detected_at") or r.get("date") or ""
                st.markdown(f"- 🟢 **NEW** `{when}` — {r.get('headline', r.get('subject_key',''))}")
        else:
            st.caption("Nothing updated in the last 24h.")

        # --- Element 2: per-card change feed (git heartbeat, now with time) ---
        st.subheader("Change feed")
        feed = status["change_feed"]
        if feed:
            for e in feed:
                st.markdown(f"- `{e['date']}` — {e['subject']}")
        else:
            st.caption("No changes recorded yet.")

        # --- Alert log (populated once monitoring runs) ---
        st.subheader("Alerts")
        alerts = display.load_alerts(slug)
        if alerts:
            for a in alerts:
                when = a.get("detected_at") or a.get("date", "")
                st.markdown(f"- **{when}** — {a.get('headline', a.get('so_what', a))}")
        else:
            st.caption("No material changes alerted yet.")

    # --- Element 4: timestamps on every claim (+ NEW flag) ---
    # Anchor target for the top "Verified claims" stat (which links here).
    st.markdown('<div id="verified-claims" style="scroll-margin-top:1rem"></div>',
                unsafe_allow_html=True)
    with st.expander(f"Claim freshness — {len(rows)} claims ({new_count} updated <24h)",
                     expanded=True):
        st.caption("`as_of` = the date the fact is true as-of · `verified_on` = when grounding "
                   "last confirmed the exact wording · `is_new` = a monitor run touched it <24h ago.")
        st.dataframe(rows, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
