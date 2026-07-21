"""Agent Scout — Flask viewer (the de-Streamlit'd app, for Cloud Run / agent-scout.ai).

Parallel build per v2/docs/cloud-run-migration-spec.md. This replaces ONLY the ~400-line
Streamlit shell (app_v2.py); it reuses scout.page / scout.display / scout.selfserve /
scout.config / scout.store untouched. Nothing here imports streamlit.

Why this is so thin: page.render_page() already emits a complete standalone HTML document
(fonts + CSS + masthead + content). The Streamlit app only added: query-param routing, a GA
iframe hack, the control bar, the countdown, and the self-serve form. Here those become real
routes, GA straight in <head>, and ~15 lines of client-side fetch-and-poll — no iframe, so the
sticky/height/$-LaTeX/iOS-viewport bug classes can't recur.

Run locally:
    cd v2 && ./.venv/bin/python -m flask --app server run --debug --port 8080
Deploy: see v2/docs/cloud-run-setup.md
"""
import html as _html
import json as _json
import os
import threading
import urllib.parse
import uuid
from datetime import datetime

from flask import Flask, Response, abort, jsonify, request, send_from_directory, url_for

from scout import analytics, config, display, page, selfserve, store

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(HERE, "assets")

app = Flask(__name__)

# ----------------------------------------------------------------------- card ordering
# Ported verbatim from app_v2.py (pure functions — no Streamlit). The Batman vs Superman
# showcase card is pinned to the 4th dropdown slot; everything else sorts most-recently-updated.
_PINNED_SLUG = "batman__vs__superman__general"
_PINNED_POSITION = 3
_EMOJI = {"batman": "🦇", "superman": "🦸"}


def _pretty(slug: str) -> str:
    return slug.replace("__vs__", " vs ").replace("__", " · ").replace("-", " ")


def _ts(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _last_update_ts(slug: str) -> datetime:
    return display.last_update_ts(slug)      # canonical impl (alerts + claims updated_on + baseline)


def _ordered_cards() -> list:
    return display.ordered_cards(pinned_slug=_PINNED_SLUG, pinned_position=_PINNED_POSITION)


def _card_label(slug: str) -> str:
    m = store.load_meta(slug) or {}
    comp = (m.get("competitor") or "").strip()
    mine = (m.get("my_company") or "").strip()
    if not comp:
        return _pretty(slug)

    def _name(n: str) -> str:
        e = _EMOJI.get(n.lower())
        return f"{e} {n}" if e else n

    return f"{_name(comp)} vs {_name(mine)}" if mine else _name(comp)


def _selfserve_meta(job_id: str, res: dict):
    """competitor / my_company / focus for the brief title — from the result record if it
    carries them, else the original request. None if neither has a competitor. (Ported.)"""
    meta = {k: res.get(k) for k in ("competitor", "my_company", "focus") if res.get(k)}
    if not meta.get("competitor"):
        try:
            import json
            req = json.loads(selfserve._read(f"{selfserve.REQUESTS_DIR}/{job_id}.json") or "{}")
            meta = {"competitor": req.get("competitor"), "my_company": req.get("my_company"),
                    "focus": req.get("focus")}
        except Exception:
            meta = {}
    return meta if meta.get("competitor") else None


# ----------------------------------------------------------------------- head / chrome
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,ital,'
    'wght@9..144,0,400;9..144,0,500;9..144,0,600;9..144,1,400;9..144,1,500'
    '&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">')

# Control-bar CSS, ported from app_v2._CTRL_CSS (self-contained, raw palette colors).
_CTRL_CSS = (
    ".scout-ctl,.scout-ctl *{box-sizing:border-box;"
    "font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;}"
    ".scout-ctl a{text-decoration:none;}"
    ".scout-tabs{display:inline-flex;border:1px solid #dfdbcf;border-radius:8px;overflow:hidden;background:#fbfaf6;}"
    ".scout-tabs a{display:inline-flex;align-items:center;min-height:40px;padding:0 1.05rem;font-weight:600;"
    "font-size:14px;color:#5f5e54;white-space:nowrap;border-right:1px solid #dfdbcf;transition:background .15s,color .15s;}"
    ".scout-tabs a:last-child{border-right:none;}"
    ".scout-tabs a:hover{color:#34566b;}"
    ".scout-tabs a.on{background:#34566b;color:#fff;}"
    ".scout-dd{position:relative;display:block;width:auto;min-width:240px;max-width:340px;}"
    ".scout-dd>summary{list-style:none;cursor:pointer;display:flex;align-items:center;min-height:40px;"
    "justify-content:space-between;gap:10px;padding:0 .8rem;font-size:14px;font-weight:500;color:#1c1d16;"
    "background:#fbfaf6;border:1px solid #dfdbcf;border-radius:8px;transition:border-color .15s;}"
    ".scout-dd>summary::-webkit-details-marker{display:none;}"
    ".scout-dd>summary:hover,.scout-dd[open]>summary{border-color:#34566b;}"
    ".scout-dd>summary .cv{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;color:#908e82;transition:transform .15s;}"
    ".scout-dd[open]>summary .cv{transform:rotate(180deg);}"
    ".scout-dd .menu{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:60;background:#fbfaf6;"
    "border:1px solid #dfdbcf;border-radius:8px;box-shadow:0 6px 24px rgba(28,29,22,.12);padding:5px;max-height:62vh;overflow:auto;}"
    ".scout-dd .menu a{display:block;padding:8px 11px;border-radius:6px;font-size:14px;color:#33312a;}"
    ".scout-dd .menu a:hover{background:rgba(52,86,107,.08);color:#1c1d16;}"
    ".scout-dd .menu a.on{background:#34566b;color:#fff;}"
    ".scout-bar{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;margin:16px 0 6px;}"
    ".scout-left{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}"
    ".scout-print{display:inline-flex;align-items:center;gap:7px;box-sizing:border-box;min-height:40px;padding:0 15px;"
    "font-family:'IBM Plex Mono',ui-monospace,monospace;font-weight:600;font-size:12px;color:#34566b;background:#fbfaf6;"
    "border:1px solid #dfdbcf;border-radius:8px;white-space:nowrap;transition:border-color .15s,color .15s;}"
    ".scout-print:hover{border-color:#34566b;color:#2a4658;}"
    # self-serve form (own palette; the old _FORM_CSS only existed to drag Streamlit widgets onto our font)
    ".ss-wrap{font-family:'Inter',system-ui,sans-serif;color:#1c1d16;max-width:640px;}"
    ".ss-title{font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:28px;margin:.4rem 0 .3rem;}"
    ".ss-cap{font-size:14px;color:#5f5e54;line-height:1.5;margin:0 0 1.1rem;}"
    "#ss-form label{display:block;font-size:14px;font-weight:600;color:#33312a;margin:0 0 .9rem;}"
    "#ss-form input{display:block;width:100%;margin-top:5px;padding:10px 12px;font-size:14px;font-family:inherit;"
    "color:#1c1d16;background:#fbfaf6;border:1px solid #dfdbcf;border-radius:8px;font-weight:400;}"
    "#ss-form input:focus{outline:none;border-color:#34566b;}"
    "#ss-go{min-height:42px;padding:0 1.3rem;font-family:inherit;font-weight:600;font-size:14px;color:#fff;"
    "background:#34566b;border:none;border-radius:8px;cursor:pointer;transition:background .15s;}"
    "#ss-go:hover{background:#2a4658;} #ss-go:disabled{opacity:.6;cursor:default;}"
    ".ss-msg{margin-top:.8rem;font-size:14px;color:#8a6322;min-height:1em;}"
    ".ss-note{font-size:14px;color:#33312a;line-height:1.55;margin-bottom:1rem;}"
    ".ss-bar{height:8px;background:#e7e3d8;border-radius:5px;overflow:hidden;margin:.6rem 0;}"
    ".ss-bar-fill{height:100%;width:0;background:#2f6149;transition:width .9s linear;}"
    ".ss-elapsed{font-family:'IBM Plex Mono',monospace;font-size:12px;color:#908e82;}"
    # The creating state (2026-07-19): elapsed promoted to just under the title, and the rotating
    # build quip in italics under the bar.
    ".ss-elapsed-big{font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:21px;"
    "color:#33312a;margin:0 0 .8rem;}"
    ".ss-quip{font-size:13.5px;font-style:italic;color:#5f5e54;margin-top:.5rem;min-height:1.3em;}"
)


def _ga_head() -> str:
    """The GA tag, straight into <head> — no iframe, no window.parent. Same self-gating as the
    Streamlit component: fire only on the allow-listed prod hostnames (config.ANALYTICS_HOSTNAMES,
    shared via analytics._hosts_js so the two injectors can't drift — 2026-07-08 incident), honor
    the scout_me opt-out cookie, and let ?me=1 set it. Empty when no measurement id is configured."""
    mid = config.GA_MEASUREMENT_ID
    if not mid:
        return ""
    host_guard = (f"if({analytics._hosts_js()}.indexOf(location.hostname)===-1)return;"
                  if config.ANALYTICS_HOSTNAMES else "")
    js = (
        "(function(){"
        + host_guard +
        "try{if(location.search.indexOf('me=1')>-1){document.cookie='scout_me=1; max-age=63072000; path=/; SameSite=Lax';}}catch(e){}"
        "try{if((document.cookie||'').indexOf('scout_me=1')>-1)return;}catch(e){}"
        "var s=document.createElement('script');s.async=true;"
        "s.src='https://www.googletagmanager.com/gtag/js?id=" + mid + "';"
        "document.head.appendChild(s);"
        "window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
        "window.gtag=gtag;gtag('js',new Date());gtag('config','" + mid + "');"
        "})();"
    )
    return "<script>" + js + "</script>"


def _countdown_js() -> str:
    """Live 'Next update' countdown + client-timezone localization of .scout-ld/-lt/-lts. Same
    logic as app_v2._countdown_component but run directly on `document` (no iframe/window.parent
    indirection, and no viewport-meta patching — we control <head> and set the meta there)."""
    return (
        "<script>"
        "(function(){function pad(n){return (n<10?'0':'')+n;}"
        "setInterval(function(){var el=document.getElementById('scout-countdown');if(!el)return;"
        "if(el.__end===undefined){var r=parseInt(el.getAttribute('data-remaining')||'0',10);if(r<=0)return;el.__end=Date.now()+r*1000;}"
        "var s=Math.round((el.__end-Date.now())/1000);"
        "if(s<=0){el.textContent='refresh due now';return;}"
        "var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;"
        "el.textContent='in '+h+'h '+pad(m)+'m '+pad(sec)+'s';},1000);})();"
        "(function(){"
        "function fd(d){return d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});}"
        "function fds(d){return d.toLocaleDateString(undefined,{month:'short',day:'numeric'});}"
        "function ft(d){return d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});}"
        "setInterval(function(){var els=document.querySelectorAll('.scout-ld[data-utc],.scout-lt[data-utc],.scout-lts[data-utc]');"
        "for(var i=0;i<els.length;i++){var el=els[i];if(el.__loc)continue;"
        "var d=new Date(el.getAttribute('data-utc'));if(isNaN(d.getTime()))continue;el.__loc=true;"
        "var c=el.className;"
        "if(c.indexOf('scout-lts')>-1){el.textContent=fds(d)+' \\u00b7 '+ft(d);}"
        "else if(c.indexOf('scout-ld')>-1){el.textContent=fd(d);}"
        "else{el.textContent=ft(d);}}},1000);})();"
        "</script>")


def _control_bar(is_create: bool, slug, cards: list, right_html: str = "") -> str:
    """The mode tabs + card dropdown + print link, ported to real routes (no query params).
    `right_html` overrides the right-aligned slot (the .scout-bar is flex space-between) — the
    result page puts its Print/Download actions there, on the same row as the tabs."""
    tabs = ('<div class="scout-tabs">'
            f'<a class="{"" if is_create else "on"}" href="/">Living battlecards</a>'
            f'<a class="{"on" if is_create else ""}" href="/create">Create your own</a></div>')
    left = tabs
    print_btn = ""
    if not is_create and slug:
        opts = "".join(
            f'<a class="{"on" if c == slug else ""}" href="/c/{c}">{_html.escape(_card_label(c))}</a>'
            for c in cards)
        dd = ('<details class="scout-dd">'
              f'<summary><span>{_html.escape(_card_label(slug))}</span>'
              '<span class="cv">&#9662;</span></summary>'
              f'<div class="menu">{opts}</div></details>')
        left = f'<div class="scout-left">{tabs}{dd}</div>'
        print_btn = (f'<a class="scout-print" href="/print/{slug}" target="_blank" rel="noopener">'
                     '&#128424; Print call sheet</a>')
    right = right_html or print_btn
    return f'<div class="scout-ctl scout-bar">{left}{right}</div>'


def _chrome_with_actions(cards: list, right_html: str) -> str:
    """Chrome variant for the result page: the action buttons ride the control bar's right slot
    (same row as the tabs, right-aligned) so the report content starts tight underneath —
    no floating button block, no dead space (2026-07-19 layout note)."""
    return (page.masthead_html()
            + f'<div class="wrap" style="padding:0 0 16px">'
              f'{_control_bar(True, None, cards, right_html=right_html)}</div>')


def _doc(body_inner: str, *, title: str) -> str:
    """Wrap inner HTML in a full document: viewport + GA + fonts + the card CSS + control CSS."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{_html.escape(title)}</title>'
        '<link rel="icon" href="/favicon.ico">'
        + _ga_head()
        + _FONT_LINKS
        + page.style_block()
        + f'<style>{_CTRL_CSS}</style>'
        + '</head><body style="background:#f4f2ec;margin:0;padding:6px 0 24px">'
        + body_inner
        + '</body></html>')


def _chrome(is_create: bool, slug, cards: list) -> str:
    """Masthead + the control bar (centered in a .wrap, matching the card body's own .wrap)."""
    # The control bar lives OUTSIDE #scout-page, so it'd inherit the mockup's base
    # .wrap{padding:0 24px 56px} — a 56px gap above the title + a 24px indent vs the rest.
    # Override to align it (no L/R pad, matching the #scout-page .wrap) and close the gap.
    # Balance the bar: the gap ABOVE it (masthead 6px + .scout-bar 16px top = ~22px) should equal
    # the gap BELOW it. .scout-bar adds 6px below, so the wrap carries the remaining ~16px.
    return (page.masthead_html()
            + f'<div class="wrap" style="padding:0 0 16px">{_control_bar(is_create, slug, cards)}</div>')


# --- server-side GA4 visit (the unblockable catcher, ported from the Streamlit app) ----------
# Fires a once-per-session GA4 Measurement Protocol "server_visit" event from our server, so
# visitors who block the client gtag (ad blockers, Safari ITP) still register IN GA — the cohort
# that matters. No-ops until GA_MP_API_SECRET is set. The first-party JSONL log is deliberately
# NOT ported: Cloud Run access logs already give the raw server-side count. Reuses the proven
# analytics helpers; only the request/cookie plumbing is Flask-specific (was Streamlit's).
analytics._log_config_once()

_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")


@app.after_request
def _noindex(resp):
    """Keep the whole site OUT of search engines (2026-07-01, Uroš's call): a portfolio viewer with
    high-traffic AI keywords has no business ranking. X-Robots-Tag on EVERY response (HTML, print
    sheets, JSON, assets) is the mechanism Google/Bing honor for de-indexing already-indexed pages
    on their next crawl."""
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.after_request
def _server_visit(resp):
    try:
        if (request.method != "GET" or request.path == "/healthcheck"
                or not (resp.content_type or "").startswith("text/html")):
            return resp
        if request.cookies.get("scout_me") == "1" or request.args.get("me"):
            return resp                                    # your own marked devices, everywhere
        if analytics._is_bot(request.headers.get("User-Agent", "")):
            return resp                                    # crawlers/uptime bots out of the count
        cid = request.cookies.get("scout_cid")
        if not cid:                                        # stable per-visitor id (2-year cookie)
            cid = uuid.uuid4().hex
            resp.set_cookie("scout_cid", cid, max_age=63072000, samesite="Lax")
        if not request.cookies.get("scout_sv"):            # fire once per session
            resp.set_cookie("scout_sv", "1", samesite="Lax")   # session cookie (no max-age)
            card = request.path[3:] if request.path.startswith("/c/") else ""
            ip = analytics._client_ip(dict(request.headers))
            ref = request.headers.get("Referer", "")
            utm = {k: request.args.get(k) for k in _UTM_KEYS if request.args.get(k)}
            threading.Thread(target=analytics._ga4_server_event,
                             args=(cid, ip, ref, card, utm), daemon=True).start()
    except Exception:
        pass
    return resp


# ----------------------------------------------------------------------------- routes
# NB: not "/healthz" — Google's Cloud Run frontend reserves/intercepts that path (it returns
# the GFE 404 before the request reaches the container), so we expose our own under a free name.
@app.get("/healthcheck")
def healthcheck():
    return "ok", 200


@app.get("/robots.txt")
def robots():
    # Deliberately PERMISSIVE, counterintuitively: to DROP already-indexed pages, crawlers must be
    # able to fetch them and see the X-Robots-Tag noindex header above. A "Disallow: /" here would
    # block the crawl, hide the noindex, and leave stale entries in the index indefinitely.
    return Response("User-agent: *\nAllow: /\n", mimetype="text/plain")


@app.get("/favicon.ico")
def favicon():
    for name in ("scout_icon_t.png", "scout_icon.png", "favicon.ico"):
        if os.path.exists(os.path.join(ASSETS_DIR, name)):
            return send_from_directory(ASSETS_DIR, name)
    return ("", 404)


@app.get("/assets/<path:fname>")
def assets(fname):
    return send_from_directory(ASSETS_DIR, fname)


@app.get("/")
def index():
    cards = _ordered_cards()
    if not cards:
        return _doc(_chrome(False, None, cards)
                    + '<div class="wrap"><p>No battlecards have been generated yet.</p></div>',
                    title="Agent Scout — Living Battlecards")
    return _card_page(cards[0], cards)


@app.get("/c/<slug>")
def card(slug):
    cards = _ordered_cards()
    if slug not in cards:
        abort(404)
    return _card_page(slug, cards)


def _card_page(slug: str, cards: list) -> str:
    inner = (_chrome(False, slug, cards)
             + page.title_html(slug)
             + page.content_html(slug)
             + _countdown_js())
    return _doc(inner, title=f"{_card_label(slug)} — Agent Scout")


@app.get("/print/<slug>")
def print_sheet(slug):
    if slug not in display.list_battlecards():
        abort(404)
    sheet = page.call_sheet_html(slug)
    autoprint = ("<script>window.addEventListener('load',function(){"
                 "setTimeout(function(){try{window.print();}catch(e){}},500);});</script>")
    return sheet.replace("</body>", autoprint + "</body>", 1)


@app.get("/create")
def create():
    cards = _ordered_cards()
    chrome = _chrome(True, None, cards)
    try:
        gate = selfserve.gate()
    except Exception:
        body = ('<div class="wrap ss-wrap"><p>Create-your-own is temporarily unavailable — '
                'please check back shortly.</p></div>')
        return _doc(chrome + body, title="Create your own — Agent Scout")
    if not gate.get("open"):
        body = (f'<div class="wrap ss-wrap"><h2 class="ss-title">Create your own battlecard</h2>'
                f'<p class="ss-cap">The free launch window is full. '
                f'For access, <a href="{_html.escape(config.SELFSERVE_CONTACT)}">get in touch</a>.</p></div>')
        return _doc(chrome + body, title="Create your own — Agent Scout")
    return _doc(chrome + _form_html(gate), title="Create your own — Agent Scout")


def _form_html(gate: dict) -> str:
    email_field = ('<label>Email me when it&#39;s ready (optional)'
                   '<input type="email" id="ss-email" placeholder="you@company.com"></label>'
                   if config.SELFSERVE_EMAIL_ENABLED else "")
    return (
        '<div class="wrap ss-wrap">'
        '<h2 class="ss-title">Create your own battlecard</h2>'
        f'<p class="ss-cap"><b>{gate.get("free_left", 0)} free reports left.</b> Two companies, '
        'optional focus. We research, verify every claim against its source, then show you the card.</p>'
        '<div id="ss-form">'
        '<label>Competitor to research (required)<input id="ss-comp" maxlength="60" placeholder="e.g. OpenAI"></label>'
        '<label>Your company (optional)<input id="ss-mine" maxlength="60" placeholder="e.g. Anthropic"></label>'
        '<label>Focus area (optional)<input id="ss-focus" maxlength="80" placeholder="e.g. enterprise coding"></label>'
        f'{email_field}'
        '<button id="ss-go">Generate my report</button>'
        '<div id="ss-msg" class="ss-msg"></div>'
        '</div>'
        '<div id="ss-status" style="display:none"></div>'
        '</div>'
        f'<script>{_CREATING_LIB}</script><script>{_FORM_JS}</script>')


# The CREATING state (2026-07-19 UX pass): one shared JS library used by BOTH the post-submit
# swap and the revisit /result pending page, so the two renditions of "your card is being built"
# can never drift. The bar and the rotating build quips key off the REAL pipeline stage
# (progress.json written by the Action, surfaced via /api/status) with a time-based fallback,
# and the bar creeps asymptotically within a stage so it never parks at full.
_CREATING_LIB = ("""
(function(){
  var MSG=""" + _json.dumps(page.PROGRESS_MESSAGES) + """;
  var BUCKET=""" + _json.dumps(page.STAGE_BUCKETS) + """;
  var ANCHOR=""" + _json.dumps(page.STAGE_ANCHORS) + """;
  var ORDER=["preflight_ok","researching","verifying","grounding","rendering"];
  window.scoutStartCreating=function(jobId){
    var t=document.querySelector('.ss-title'); if(t) t.textContent='Creating your battlecard\\u2026';
    var cap=document.querySelector('.ss-cap'); if(cap) cap.style.display='none';
    var form=document.getElementById('ss-form'); if(form) form.style.display='none';
    var box=document.getElementById('ss-status'); if(!box) return; box.style.display='block';
    var started=Date.now(); var stage=null; var stageAt=Date.now();
    box.innerHTML='<div class="ss-elapsed-big" id="ss-elapsed">Elapsed 0m 0s</div>'
      +'<div class="ss-note"><b>Doing deep research for you: this can take 10\\u201320 minutes.</b> '
      +'Keep this tab open, or bookmark this URL and come back; your report will be here when it\\u2019s done.</div>'
      +'<div class="ss-bar"><div class="ss-bar-fill" id="ss-fill"></div></div>'
      +'<div class="ss-quip" id="ss-quip"></div>';
    function frac(){
      var el=(Date.now()-started)/1000;
      if(!stage) return Math.min(0.95,1-Math.exp(-el/720));
      var i=ORDER.indexOf(stage); var a=ANCHOR[stage]||0.05;
      var b=(i>=0&&i<ORDER.length-1)?ANCHOR[ORDER[i+1]]:0.95;
      var since=(Date.now()-stageAt)/1000;
      return Math.min(b-0.01, a+(b-a)*(1-Math.exp(-since/360)));
    }
    function bucket(){
      if(stage&&BUCKET[stage]) return BUCKET[stage];
      var el=(Date.now()-started)/1000;
      return el<300?'research':(el<540?'draft':(el<960?'verify':'final'));
    }
    function tick(){
      var el=(Date.now()-started)/1000;
      var f=document.getElementById('ss-fill'); if(f) f.style.width=(frac()*100).toFixed(1)+'%';
      var e=document.getElementById('ss-elapsed');
      if(e) e.textContent='Elapsed '+Math.floor(el/60)+'m '+Math.floor(el%60)+'s';
      var q=document.getElementById('ss-quip');
      if(q){ var pool=MSG[bucket()]||MSG.research;
             q.textContent=pool[Math.floor(Date.now()/45000)%pool.length]; }
    }
    tick(); setInterval(tick,1000);
    function poll(){
      fetch('/api/status/'+encodeURIComponent(jobId)).then(function(r){return r.json();})
        .then(function(j){
          if(j.status==='done'){ window.location.href=j.result_url||('/result/'+jobId); return; }
          if(j.status==='rejected'||j.status==='error'){
            box.innerHTML='<div class="ss-note">'+(j.message||'Something went wrong.')+'</div>'; return; }
          if(j.stage&&j.stage!==stage){ stage=j.stage; stageAt=Date.now(); }
          setTimeout(poll,(j.retry||20)*1000);
        }).catch(function(){ setTimeout(poll,30000); });
    }
    setTimeout(poll,8000);
  };
})();
""")

_FORM_JS = """
(function(){
  var go=document.getElementById('ss-go'); if(!go) return;
  var msg=document.getElementById('ss-msg');
  function show(t){ if(msg) msg.textContent=t; }
  go.addEventListener('click',function(){
    var comp=(document.getElementById('ss-comp')||{}).value||'';
    var mine=(document.getElementById('ss-mine')||{}).value||'';
    var focus=(document.getElementById('ss-focus')||{}).value||'';
    var emailEl=document.getElementById('ss-email'); var email=emailEl?emailEl.value:'';
    if(!comp.trim()){ show('Please enter a competitor to research.'); return; }
    go.disabled=true; show('Submitting\\u2026');
    fetch('/api/request',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({competitor:comp,my_company:mine,focus:focus,email:email})})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(o){
        if(!o.ok||o.j.error){ go.disabled=false; show(o.j.error||'Something went wrong.'); return; }
        window.scoutStartCreating(o.j.job_id);
      }).catch(function(){ go.disabled=false; show('Network error \\u2014 please try again.'); });
  });
})();
"""


@app.post("/api/request")
def api_request():
    data = request.get_json(silent=True) or {}
    competitor = (data.get("competitor") or "").strip()
    my_company = (data.get("my_company") or "").strip()
    focus = (data.get("focus") or "").strip()
    email = (data.get("email") or "").strip()
    if not competitor:
        return jsonify(error="Please enter a competitor to research."), 400
    if email and not selfserve.valid_email(email):
        return jsonify(error="That email doesn't look right — fix it or clear it to continue."), 400
    try:
        if not selfserve.gate().get("open"):
            return jsonify(error="The free window just closed. For access, see the contact link."), 403
        # submit() writes the request AND dispatches the Action internally (no-op without a token).
        req = selfserve.submit(competitor, my_company or None, focus or None,
                               notify_email=email or None)
    except Exception:
        return jsonify(error="Couldn't reach the report backend just now — nothing was submitted. "
                             "Please try again in a minute."), 502
    return jsonify(job_id=req["job_id"])


@app.get("/api/status/<job_id>")
def api_status(job_id):
    if not selfserve.valid_job_id(job_id):
        return jsonify(status="error", message="That job link looks malformed."), 400
    try:
        res = selfserve.get_result(job_id)
    except Exception:
        return jsonify(status="pending", retry=45)
    if res is None:
        try:
            known = selfserve.get_request(job_id) is not None
        except Exception:
            known = True
        if not known:
            return jsonify(status="error", message="We can't find that report request — the link "
                                                    "may be incomplete or mistyped.")
        # `stage` = the runner's real pipeline stage (progress.json), when it exists — the bar and
        # the build quips key off it. One extra store read per poll (20s cadence); acceptable at
        # current traffic, revisit if a crowd of waiters ever rate-limits the shared PAT.
        return jsonify(status="pending", retry=20,
                       stage=(selfserve.read_progress(job_id) or {}).get("stage"))
    status = res.get("status")
    if status == "done":
        return jsonify(status="done", result_url=url_for("result", job_id=job_id))
    if status == "rejected":
        return jsonify(status="rejected", message=res.get("message", "The free window is closed."))
    return jsonify(status="error",
                   message=res.get("message", "Something went wrong generating this report."))


@app.get("/result/<job_id>")
def result(job_id):
    cards = _ordered_cards()
    chrome = _chrome(True, None, cards)
    if not selfserve.valid_job_id(job_id):
        return _doc(chrome + '<div class="wrap ss-wrap"><p>That job link looks malformed.</p></div>',
                    title="Agent Scout")
    try:
        res = selfserve.get_result(job_id)
    except Exception:
        res = None
    if res is None:
        # Pending (or unknown) — render a poll page that redirects here once the result lands.
        return _doc(chrome + _pending_html(job_id), title="Generating… — Agent Scout")
    status = res.get("status")
    if status == "done":
        md = res.get("markdown", "")
        claims = res.get("claims") or []
        meta = _selfserve_meta(job_id, res)
        # Actions ride the control bar's RIGHT slot — same row as the tabs, right-aligned —
        # so the report starts tight underneath (no floating button block, no dead space).
        actions = (f'<span style="display:inline-flex;gap:10px;align-items:center">'
                   f'<a class="scout-print" href="/result/{job_id}/print" target="_blank" rel="noopener">'
                   f'&#128424; Print call sheet</a>'
                   f'<a class="scout-print" href="/result/{job_id}/brief.md">&#11015; Download (Markdown)</a></span>')
        chrome = _chrome_with_actions(cards, actions)
        if claims:
            # briefing=True: the 2-minute payoff after the wait — exec-summary leads + top plays
            # rendered as a start-here digest above the full brief (2026-07-19 UX pass).
            brief = page.static_brief_html(
                claims, md, meta=meta, briefing=True,
                briefing_label="Your 2-minute brief",
                briefing_tag="the fast read first · fresh off the research run")
        else:
            brief = f'<div class="wrap"><pre style="white-space:pre-wrap">{_html.escape(md)}</pre></div>'
        return _doc(chrome + brief, title="Your report — Agent Scout")
    if status == "rejected":
        msg = _html.escape(res.get("message", "The free window is closed."))
        return _doc(chrome + f'<div class="wrap ss-wrap"><p>{msg}</p></div>', title="Agent Scout")
    # Error fall-through: `message` is ALWAYS the runner's human-facing copy — raw internals live
    # only in the owner-facing detail_internal field, which is never rendered (7/18 incident).
    msg = _html.escape(res.get("message", "We hit a snag generating this report and it's been "
                                          "flagged to the owner. Check back in a while."))
    return _doc(chrome + f'<div class="wrap ss-wrap"><p>{msg}</p></div>', title="Agent Scout")


def _pending_html(job_id: str) -> str:
    """Revisit view of a job still generating — the SAME creating state as the post-submit swap
    (shared _CREATING_LIB), so a bookmarked/return visitor sees an identical experience."""
    return (
        '<div class="wrap ss-wrap">'
        '<h2 class="ss-title">Creating your battlecard…</h2>'
        '<div id="ss-status"></div></div>'
        f'<script>{_CREATING_LIB}</script>'
        f'<script>window.scoutStartCreating({_json.dumps(job_id)});</script>')


@app.get("/result/<job_id>/brief.md")
def result_md(job_id):
    if not selfserve.valid_job_id(job_id):
        abort(404)
    try:
        res = selfserve.get_result(job_id)
    except Exception:
        res = None
    if not res or res.get("status") != "done":
        abort(404)
    fname = (res.get("slug") or "competitive-brief") + ".md"
    # Server-side download event (2026-07-21): GA's file_download never fires for .md (not on
    # its extension list) and blocked browsers fire nothing — but this fetch hits US, so tell
    # GA directly. Same gating as _server_visit (owner + bot UAs out); never break the download.
    try:
        ua = request.headers.get("User-Agent", "")
        if (request.cookies.get("scout_me") != "1" and not request.args.get("me")
                and not analytics._is_bot(ua)):
            cid = request.cookies.get("scout_cid") or uuid.uuid4().hex
            threading.Thread(target=analytics._ga4_server_event,
                             args=(cid, analytics._client_ip(dict(request.headers)),
                                   request.headers.get("Referer", ""), job_id),
                             kwargs={"event": "brief_download"}, daemon=True).start()
    except Exception:
        pass
    return Response(res.get("markdown", ""), mimetype="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/result/<job_id>/print")
def result_print(job_id):
    if not selfserve.valid_job_id(job_id):
        abort(404)
    try:
        res = selfserve.get_result(job_id)
    except Exception:
        res = None
    if not res or res.get("status") != "done":
        abort(404)
    meta = _selfserve_meta(job_id, res)
    sheet = page.call_sheet_from_claims(res.get("claims") or [], meta)
    autoprint = ("<script>window.addEventListener('load',function(){"
                 "setTimeout(function(){try{window.print();}catch(e){}},500);});</script>")
    return sheet.replace("</body>", autoprint + "</body>", 1)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
