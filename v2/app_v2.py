"""Scout v2 — living-battlecard viewer (READ-ONLY).

Public view of pre-baked, git-committed battlecards. Surfaces the four "show the
agentic work" display elements around the verified brief. Does NOT trigger generation
(gated, last-stage) and does NOT run monitoring. Reads the store + git only.

Run:  streamlit run app_v2.py
"""
import base64
import html
import json
import os
import random
import re
import time
from datetime import datetime

import streamlit as st

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

from scout import analytics, config, display, page, selfserve, store

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

# The estimate the bar fills against (decoupled from the actual job). Measured first
# live run was ~12 min for a broad "general" card; pace to 10 min so the bar is still
# climbing near the end rather than sitting maxed-out (which reads as "stuck").
_SELFSERVE_ESTIMATE_S = 600

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


def _asset(*names):
    here = os.path.dirname(os.path.abspath(__file__))
    for n in names:
        p = os.path.join(here, "assets", n)
        if os.path.exists(p):
            return p
    return None


def _norm_title(s: str) -> str:
    """Loose key for matching a rendered brief block title back to its claim."""
    return re.sub(r"\s+", " ", s.strip().lower())


def _slug(text: str) -> str:
    """Stable heading id: lowercase, punctuation dropped, spaces -> hyphens. Used
    identically by the brief headings and the TOC links so anchors line up."""
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", s)


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


def _selfserve_meta(job_id: str, res: dict) -> dict | None:
    """competitor / my_company / focus for the brief title — from the result record if
    it carries them, else the original request. None if neither has a competitor."""
    meta = {k: res.get(k) for k in ("competitor", "my_company", "focus") if res.get(k)}
    if not meta.get("competitor"):
        try:
            req = json.loads(selfserve._read(f"{selfserve.REQUESTS_DIR}/{job_id}.json") or "{}")
            meta = {"competitor": req.get("competitor"), "my_company": req.get("my_company"),
                    "focus": req.get("focus")}
        except Exception:
            meta = {}
    return meta if meta.get("competitor") else None


def _reset_selfserve() -> None:
    """Clear the per-session job state + URL so 'Create another' returns to a fresh form."""
    for k in [k for k in st.session_state if k.startswith("job_start_")]:
        st.session_state.pop(k, None)
    st.session_state.pop("selfserve_job", None)
    st.query_params.clear()
    st.query_params["mode"] = "create"   # land on a fresh form, not back on the card viewer


def _render_job_status(job_id: str) -> str | None:
    """Show a self-serve job: a timed-estimate progress bar while pending, the rendered card
    when done, or the gate message if it was rejected. Polls by sleeping then rerunning.
    Returns the job status ('done'/'rejected'/'error') or None while still pending."""
    try:
        res = selfserve.get_result(job_id)
    except Exception:
        # A backend hiccup (rate limit, expired token, GitHub outage) must never stacktrace
        # on a public page. Tell the visitor we're retrying and back off harder than the
        # normal poll so a crowd of waiters doesn't burn the API quota.
        st.info("We're having trouble reaching the report backend — retrying automatically. "
                "Your report is safe; this page will recover on its own.")
        time.sleep(45)
        st.rerun()
        return None
    if res is None:
        # Distinguish "still generating" from "never existed", so a mistyped or made-up
        # ?job= link gets a clear error instead of an infinite progress bar. One backend
        # check per session; on backend trouble give the link the benefit of the doubt.
        if not st.session_state.get(f"job_known_{job_id}"):
            try:
                known = selfserve.get_request(job_id) is not None
            except Exception:
                known = True
            if not known:
                st.error("We can't find that report request — the link may be incomplete or "
                         "mistyped. Double-check the URL, or start a new report.")
                return "error"
            st.session_state[f"job_known_{job_id}"] = True
        started = st.session_state.setdefault(f"job_start_{job_id}", time.time())
        elapsed = time.time() - started
        frac = min(elapsed / _SELFSERVE_ESTIMATE_S, 0.99)
        if st.session_state.get("selfserve_notify"):
            wait_note = ("**This usually takes 8–10 minutes, sometimes longer — perfection takes "
                         "time!** You can close this tab; we'll email you a link the moment it's "
                         "ready.")
        else:
            wait_note = ("**This usually takes 8–10 minutes, sometimes longer — perfection takes "
                         "time!** Keep this tab open, or bookmark this URL and come back; your "
                         "report will be here when it's done.")
        st.info(wait_note)
        st.progress(frac)
        st.markdown("*" + _phase_message(frac) + "*")
        st.caption(f"Elapsed {int(elapsed // 60)}m {int(elapsed % 60)}s")
        # 20s, not faster: each poll is a GitHub API read on one shared PAT (5k req/hr),
        # and a launch-day crowd of pending tabs must not rate-limit the whole job view.
        time.sleep(20)
        st.rerun()
        return None
    status = res.get("status")
    if status == "done":
        st.success("Your report is ready.")
        md = res.get("markdown", "")
        claims = res.get("claims") or []
        meta = _selfserve_meta(job_id, res)
        # EXPORT actions at the top — reports are long, so a reader can grab the artifact without
        # scrolling first. Print / Save as PDF leads (the rep-friendly call sheet); Markdown is the
        # secondary power-user export. "Create another" is deliberately NOT up here: it's a restart,
        # and at equal weight it just invites a bounce off the report before it's been read. It
        # lives at the bottom, where a "what next" action belongs.
        if claims:
            a1, a2 = st.columns(2, gap="small", vertical_alignment="center")
            with a1:
                st.iframe(_print_button(page.call_sheet_from_claims(claims, meta), full=True),
                          height=48)
            with a2:
                st.download_button("⬇ Download (Markdown)", data=md, use_container_width=True,
                                   file_name=f"{res.get('slug') or 'competitive-brief'}.md",
                                   mime="text/markdown")
            # Render through the SAME engine as the living-battlecard viewer (rich CSS already
            # injected in main()), minus the monitoring furniture — so a self-serve card and a
            # roster card share one consistent UI.
            st.markdown(page.static_brief_html(claims, md, meta=meta), unsafe_allow_html=True)
        else:  # older job with no stored claims — no call sheet, so Markdown export stands alone
            st.download_button("⬇ Download (Markdown)", data=md, use_container_width=True,
                               file_name=f"{res.get('slug') or 'competitive-brief'}.md",
                               mime="text/markdown")
            st.markdown("---")
            st.markdown(_BRIEF_CSS, unsafe_allow_html=True)
            st.markdown(_render_brief_html(md), unsafe_allow_html=True)
        # Quiet escape hatch, set apart and AFTER the report — not competing with it up top.
        st.markdown("<div style='margin-top:1.25rem'></div>", unsafe_allow_html=True)
        _, restart_col = st.columns([3, 1])
        with restart_col:
            if st.button("✚ Create another", use_container_width=True):
                _reset_selfserve()
                st.rerun()
    elif status == "rejected":
        st.warning(res.get("message", "The free window is closed."))
        st.markdown(f"**For access, {_contact_md()}.**")
    else:
        st.error(res.get("message", "Something went wrong generating this report."))
    return status


def _contact_md() -> str:
    """Markdown for the 'get in touch' link: 'DM me on LinkedIn' when AUTHOR_LINKEDIN is
    configured, else a fallback to the contact email. Reused by every access/contact prompt."""
    if config.AUTHOR_LINKEDIN:
        return f"[DM me on LinkedIn]({config.AUTHOR_LINKEDIN})"
    return f"reach me at **{config.SELFSERVE_CONTACT}**"


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
        status = _render_job_status(job_id)
        # The done view renders its own "Create another" (a quiet one, below the report); only the
        # pending/rejected/error views (which are short) need this bottom restart button.
        if status != "done" and st.button("← Start another report"):
            _reset_selfserve()
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
    competitor = st.text_input("Competitor to research (required)", placeholder="e.g. OpenAI",
                               max_chars=60)
    my_company = st.text_input("Your company (optional)", placeholder="e.g. Anthropic",
                               max_chars=60)
    focus = st.text_input("Focus area (optional)", placeholder="e.g. enterprise coding",
                          max_chars=80)
    # Only offered when the backend can actually deliver it (Resend wired in the Action), so the
    # form never promises a notification that won't arrive. Reports take ~10 min, so this is the
    # difference between a visitor coming back and silently dropping out of the funnel.
    email = ""
    if config.SELFSERVE_EMAIL_ENABLED:
        email = st.text_input("Email me when it's ready (optional)",
                              placeholder="you@company.com")
    if st.button("Generate my report", type="primary"):
        if not competitor.strip():
            st.warning("Please enter a competitor to research.")
            return
        email_clean = email.strip()
        if email_clean and not selfserve.valid_email(email_clean):
            st.warning("That email doesn't look right — fix it or clear it to continue.")
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
            st.warning(f"Easy there, one report at a time. Try again in {wait}s.")
            return
        if len(hist) >= 3:
            st.warning("You've reached this session's limit of 3 reports.")
            st.markdown(f"Want more? {_contact_md()}.")
            return
        try:
            if not selfserve.gate()["open"]:            # re-check; the view can be stale
                st.warning("The free window just closed. For access, see the DM link below.")
                return
            req = selfserve.submit(competitor, my_company, focus,
                                   notify_email=email_clean or None)
        except Exception:
            st.warning("Couldn't reach the report backend just now — nothing was submitted. "
                       "Please try again in a minute.")
            st.markdown(f"If it keeps happening, {_contact_md()}.")
            return
        hist.append(now_dt)
        st.session_state["selfserve_job"] = req["job_id"]
        # Remember (this session) whether they asked to be emailed, so the pending view can tell
        # them they're free to close the tab instead of babysitting a 10-minute progress bar.
        st.session_state["selfserve_notify"] = bool(req.get("notify_email"))
        st.query_params["job"] = req["job_id"]
        st.rerun()


_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,ital,'
    'wght@9..144,0,400;9..144,0,500;9..144,0,600;9..144,1,400;9..144,1,500'
    '&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">')


# In-page control bar (mode tabs + card dropdown) — all custom HTML in the editorial palette so it
# matches the print button instead of reading like default Streamlit widgets. Anchors drive state
# via query params (?mode=create / ?card=<slug>): a full, reliable navigation, no native widget.
_CTRL_CSS = (
    ".scout-ctl,.scout-ctl *{box-sizing:border-box;"
    "font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;}"
    ".scout-ctl a{text-decoration:none;}"
    # segmented mode tabs (slate-filled active, matches the old look but fully owned)
    ".scout-tabs{display:inline-flex;border:1px solid #dfdbcf;border-radius:8px;overflow:hidden;"
    "background:#fbfaf6;}"
    ".scout-tabs a{display:inline-flex;align-items:center;min-height:40px;padding:0 1.05rem;"
    "font-weight:600;font-size:13px;color:#5f5e54;"
    "white-space:nowrap;border-right:1px solid #dfdbcf;transition:background .15s,color .15s;}"
    ".scout-tabs a:last-child{border-right:none;}"
    ".scout-tabs a:hover{color:#34566b;}"
    ".scout-tabs a.on{background:#34566b;color:#fff;}"
    # custom dropdown — a native <details> (survives Streamlit's sanitizer), menu overlays
    ".scout-dd{position:relative;display:block;width:auto;min-width:240px;max-width:340px;}"
    ".scout-dd>summary{list-style:none;cursor:pointer;display:flex;align-items:center;min-height:40px;"
    "justify-content:space-between;gap:10px;padding:0 .8rem;font-size:13px;font-weight:500;"
    "color:#1c1d16;background:#fbfaf6;border:1px solid #dfdbcf;border-radius:8px;"
    "transition:border-color .15s;}"
    ".scout-dd>summary::-webkit-details-marker{display:none;}"
    ".scout-dd>summary:hover,.scout-dd[open]>summary{border-color:#34566b;}"
    ".scout-dd>summary .cv{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;"
    "color:#908e82;transition:transform .15s;}"
    ".scout-dd[open]>summary .cv{transform:rotate(180deg);}"
    ".scout-dd .menu{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:60;"
    "background:#fbfaf6;border:1px solid #dfdbcf;border-radius:8px;"
    "box-shadow:0 6px 24px rgba(28,29,22,.12);padding:5px;max-height:62vh;overflow:auto;}"
    ".scout-dd .menu a{display:block;padding:8px 11px;border-radius:6px;font-size:13px;color:#33312a;}"
    ".scout-dd .menu a:hover{background:rgba(52,86,107,.08);color:#1c1d16;}"
    ".scout-dd .menu a.on{background:#34566b;color:#fff;}"
    # the whole bar is ONE flex row with three direct children spread across it (tabs left, picker
    # middle, print right). align-items centers all three so they line up BY CONSTRUCTION — no
    # Streamlit columns and no iframe, which is what kept the print button mis-registered. The
    # margin-top restores the breathing room the removed column wrapper used to give us.
    ".scout-bar{display:flex;align-items:center;justify-content:space-between;gap:18px;"
    "flex-wrap:wrap;margin:16px 0 6px;}"
    ".scout-left{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}"
    # print is now an inline link (opens the call sheet in a new tab) so it shares the row's exact
    # box model — the same 40px pill as the tabs and dropdown
    ".scout-print{display:inline-flex;align-items:center;gap:7px;box-sizing:border-box;"
    "min-height:40px;padding:0 15px;font-family:'IBM Plex Mono',ui-monospace,monospace;"
    "font-weight:600;font-size:12px;color:#34566b;background:#fbfaf6;border:1px solid #dfdbcf;"
    "border-radius:8px;white-space:nowrap;transition:border-color .15s,color .15s;}"
    ".scout-print:hover{border-color:#34566b;color:#2a4658;}"
)

# The "Create your own" form is built from NATIVE Streamlit widgets (st.text_input / st.button /
# st.subheader / st.caption / st.warning), which render in Streamlit's default typeface — out of
# step with the rest of the page (custom HTML on the Inter/Fraunces stack). Pull the form controls
# onto the app's font. Scoped to widget classes that the brief (pure custom HTML inside #scout-page)
# never uses, so this can't reach into the battlecard render. Form controls don't inherit font, so
# the labels/inputs/button are targeted explicitly; !important beats Streamlit's own widget rules.
_FORM_CSS = (
    'section[data-testid="stMain"] .stTextInput,'
    'section[data-testid="stMain"] .stTextInput input,'
    'section[data-testid="stMain"] .stTextInput label,'
    'section[data-testid="stMain"] [data-testid="stWidgetLabel"],'
    'section[data-testid="stMain"] .stButton button,'
    'section[data-testid="stMain"] [data-testid="stCaptionContainer"],'
    'section[data-testid="stMain"] [data-testid="stCaptionContainer"] *,'
    'section[data-testid="stMain"] [data-testid="stAlertContainer"],'
    'section[data-testid="stMain"] [data-testid="stAlertContainer"] *'
    "{font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif!important;}"
    # The form heading (st.subheader) is a TITLE, so it matches the brief's <h1> display serif
    # (Fraunces) — not the Inter body stack the controls above use.
    'section[data-testid="stMain"] [data-testid="stHeading"],'
    'section[data-testid="stMain"] [data-testid="stHeading"] *'
    "{font-family:'Fraunces',Georgia,Cambria,'Times New Roman',serif!important;}"
)


def _control_bar_html(is_create: bool, slug: str, cards: list, living_href: str) -> str:
    """The entire control row as ONE inline flex bar: mode tabs (left), then the card dropdown +
    print link grouped (right). All three are sibling inline elements with the same 40px box, so
    align-items:center lines them up exactly — no Streamlit columns or iframe to mis-register."""
    tabs = ('<div class="scout-tabs">'
            f'<a class="{"" if is_create else "on"}" href="{living_href}" target="_self">'
            'Living battlecards</a>'
            f'<a class="{"on" if is_create else ""}" href="?mode=create" target="_self">'
            'Create your own</a></div>')
    left = tabs
    print_btn = ""
    if not is_create and slug:
        opts = "".join(
            f'<a class="{"on" if c == slug else ""}" href="?card={c}" target="_self">'
            f'{html.escape(_card_label(c))}</a>' for c in cards)
        dd = ('<details class="scout-dd">'
              f'<summary><span>{html.escape(_card_label(slug))}</span>'
              '<span class="cv">&#9662;</span></summary>'
              f'<div class="menu">{opts}</div></details>')
        # mode tabs + card picker are both "what am I viewing" — group them on the left; print is
        # the action, alone on the right (never the picker glued to print).
        left = f'<div class="scout-left">{tabs}{dd}</div>'
        print_btn = (f'<a class="scout-print" href="?print={slug}" target="_blank" rel="noopener">'
                     '&#128424; Print call sheet</a>')
    return f'<div class="scout-ctl scout-bar">{left}{print_btn}</div>'


def _autoprint(sheet_html: str) -> str:
    """Inject an auto-print into the call-sheet document so the ?print tab opens the print dialog
    on load (the user already gestured by clicking the link). Degrades gracefully — if the dialog
    doesn't fire, the polished, print-optimized call sheet is still shown for a manual ⌘P."""
    script = ("<script>window.addEventListener('load',function(){"
              "setTimeout(function(){try{window.print();}catch(e){}},500);});</script>")
    return sheet_html.replace("</body>", script + "</body>", 1)


def _countdown_component() -> str:
    """A height-0 component that drives the live 'Next update' countdown AND localizes
    timestamps. Same parent-injection trick as GA (proven to reach window.parent on Cloud):
    one global interval re-renders #scout-countdown every second from its data-remaining,
    anchored to the client clock at load. A second interval rewrites .scout-ld/.scout-lt/
    .scout-lts elements (server-rendered in ET as the no-JS fallback — see page._utc_attr)
    to the VIEWER's timezone from their data-utc; once localized there's no tz suffix, the
    local convention. Fresh elements each rerun re-localize within a tick. Degrades
    gracefully — if the parent isn't reachable, the server-rendered ET simply stays put.

    Also normalizes the viewport meta on the TOP-LEVEL document: Streamlit's index.html ships
    none and its React app only injects one at runtime — too late for iOS Safari, which commits
    its 980px desktop layout at first paint and (unlike Chrome) won't re-evaluate a LATE-INSERTED
    meta. So opening the bare app frame (/~/+/ — the public viewer link) on a phone rendered
    desktop columns. WebKit DOES reliably re-parse the viewport when an EXISTING meta's content
    attribute CHANGES, so we rewrite it to our canonical value rather than skip when one exists
    (the original skip-if-present guard never fired — Streamlit's ignored meta was already
    there). Patched on window.top (same-origin on Cloud); viewport metas only matter on the
    top-level document, so when the host page frames the app its own (parse-time, working) meta
    is already canonical-equivalent and is left alone."""
    return (
        "<script>(function(){"
        "var t=null;try{t=window.top;void t.document.head;}catch(e){}"
        "if(!t)try{t=window.parent;void t.document.head;}catch(e){return;}"
        "if(!t||t.__scoutVP)return;t.__scoutVP=true;"
        "try{var d=t.document,want='width=device-width, initial-scale=1';"
        "var m=d.querySelector('meta[name=\"viewport\"]');"
        "if(!m){m=d.createElement('meta');m.name='viewport';m.content=want;"
        "d.head.appendChild(m);}"
        "else if((m.getAttribute('content')||'').indexOf('device-width')<0||"
        "m.getAttribute('content')!==want&&"
        "(m.getAttribute('content')||'').indexOf('user-scalable')<0){"
        "m.setAttribute('content',want);}}catch(e){}"
        "})();"
        "(function(){"
        "var p=window.parent;if(!p||p.__scoutCD)return;p.__scoutCD=true;"
        "function pad(n){return (n<10?'0':'')+n;}"
        "p.setInterval(function(){"
        "var el=p.document.getElementById('scout-countdown');if(!el)return;"
        "if(el.__end===undefined){var r=parseInt(el.getAttribute('data-remaining')||'0',10);"
        "if(r<=0)return;el.__end=Date.now()+r*1000;}"
        "var s=Math.round((el.__end-Date.now())/1000);"
        "if(s<=0){el.textContent='refresh due now';return;}"
        "var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;"
        "el.textContent='in '+h+'h '+pad(m)+'m '+pad(sec)+'s';"
        "},1000);})();"
        "(function(){"
        "var p=window.parent;if(!p||p.__scoutLT)return;p.__scoutLT=true;"
        "function fd(d){return d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});}"
        "function fds(d){return d.toLocaleDateString(undefined,{month:'short',day:'numeric'});}"
        "function ft(d){return d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});}"
        "p.setInterval(function(){"
        "var els=p.document.querySelectorAll('.scout-ld[data-utc],.scout-lt[data-utc],.scout-lts[data-utc]');"
        "for(var i=0;i<els.length;i++){var el=els[i];if(el.__loc)continue;"
        "var d=new Date(el.getAttribute('data-utc'));if(isNaN(d.getTime()))continue;el.__loc=true;"
        "var c=el.className;"
        "if(c.indexOf('scout-lts')>-1){el.textContent=fds(d)+' \\u00b7 '+ft(d);}"
        "else if(c.indexOf('scout-ld')>-1){el.textContent=fd(d);}"
        "else{el.textContent=ft(d);}"
        "}},1000);})();</script>")


def _render_print_tab(slug: str):
    """The ?print=<slug> tab: strip all Streamlit chrome and render the call sheet full-bleed so
    it both displays cleanly and prints cleanly (the call sheet carries its own print CSS)."""
    if slug not in display.list_battlecards():
        st.error("That battlecard could not be found.")
        return
    st.markdown(
        '<style>[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stStatusWidget"],'
        'footer,[data-testid="stSidebar"]{display:none!important;}'
        '[data-testid="stMainBlockContainer"],.block-container{max-width:920px!important;'
        'padding:0.5rem 0 0!important;}[data-testid="stAppViewContainer"]{background:#fff!important;}'
        '</style>', unsafe_allow_html=True)
    st.iframe(_autoprint(page.call_sheet_html(slug)), height=1100)   # st.iframe always allows scrolling


# The Batman vs Superman showcase/stress-test card: always pinned to the 4th dropdown slot, and
# its players are flagged with their emoji so it reads as the deliberate easter-egg it is.
_PINNED_SLUG = "batman__vs__superman__general"
_PINNED_POSITION = 3  # 0-based -> the 4th item
_EMOJI = {"batman": "🦇", "superman": "🦸"}


def _last_update_ts(slug: str) -> datetime:
    """When a card's CONTENT last changed: most recent material-change alert, else creation date.
    Deliberately NOT last_checked (uniform refresh cadence can't tell which card changed).
    Defined HERE in the always-reloaded entry script — using only pre-existing scout.display /
    scout.store symbols — to dodge Streamlit Cloud's stale-module cache for NEW cross-module
    attributes (the same reason _title_block_html is built here; see its note)."""
    def _ts(s):
        try:
            return datetime.fromisoformat(s) if s else None
        except (ValueError, TypeError):
            return None
    times = [t for a in display.load_alerts(slug)
             if (t := _ts(a.get("detected_at") or a.get("date")))]
    base = _ts((store.load_meta(slug) or {}).get("baseline_date"))
    if base:
        times.append(base)
    return max(times) if times else datetime.min


def _ordered_cards() -> list:
    """Dropdown order: most-recently-UPDATED card first (by content-change time, not refresh),
    with the Batman vs Superman showcase card pinned to the 4th slot regardless of its age."""
    slugs = display.list_battlecards()
    rest = sorted((s for s in slugs if s != _PINNED_SLUG),
                  key=_last_update_ts, reverse=True)
    if _PINNED_SLUG in slugs:
        rest.insert(min(_PINNED_POSITION, len(rest)), _PINNED_SLUG)
    return rest


def _card_label(slug: str) -> str:
    """Short, proper-cased dropdown label from meta, e.g. 'OpenAI vs Anthropic'. (Focus area is
    already shown in the title block, so it's dropped here to keep the dropdown compact.)"""
    m = store.load_meta(slug) or {}
    comp = (m.get("competitor") or "").strip()
    mine = (m.get("my_company") or "").strip()
    if not comp:
        return _pretty(slug)

    def _name(n: str) -> str:  # prefix a known player with its emoji (showcase card only)
        e = _EMOJI.get(n.lower())
        return f"{e} {n}" if e else n

    return f"{_name(comp)} vs {_name(mine)}" if mine else _name(comp)


def _title_block_html(slug: str) -> str:
    """The report title (Competitive Intelligence Brief / Researched / Focus area). Built HERE in
    the always-reloaded entry script — not via a new page.py symbol — so it can't hit Streamlit
    Cloud's stale-module cache (the cross-module-new-attr crash we keep tripping)."""
    m = store.load_meta(slug) or {}
    comp = html.escape((m.get("competitor") or "").strip())
    mine = html.escape((m.get("my_company") or "").strip())
    focus = html.escape((m.get("focus") or "").strip())
    sub = f"Researched: <b>{comp}</b>" + (f" · For <b>{mine}</b> reps" if mine else "")
    foc = f'<div class="rt-focus">Focus area: {focus}</div>' if focus else ""
    return ('<div id="scout-page"><div class="wrap tw"><div class="rt">'
            '<h1>Competitive Intelligence Brief</h1>'
            f'<div class="rt-sub">{sub}</div>{foc}</div></div></div>')


def _print_button(sheet_html: str, full: bool = False) -> str:
    """A real, reliable print button: opens the call sheet in a FRESH window and prints that
    (no dependency on the cross-origin parent, and immune to the page's scroll-container clip).
    Rendered via st.iframe so its inline <script> actually runs. `full=True` makes it a
    full-width, sans-serif button that sits flush with native Streamlit buttons in the
    self-serve action bar (default is the right-aligned mono pill for the roster row)."""
    tmpl = sheet_html.replace("</script>", "<\\/script>")   # don't break the template script
    if full:
        wrap = "display:flex;align-items:center;height:100%;"
        btn = ("font:400 14px/1.4 'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;"
               "color:#1c1d16;background:#fbfaf6;border:1px solid #dfdbcf;border-radius:8px;"
               "padding:0 14px;min-height:40px;width:100%;cursor:pointer;white-space:nowrap;"
               "display:inline-flex;align-items:center;justify-content:center;gap:7px;"
               "transition:border-color .15s,color .15s;")
    else:
        wrap = "display:flex;justify-content:flex-end;align-items:center;height:100%;"
        btn = ("font:600 12px/1 ui-monospace,'IBM Plex Mono',monospace;color:#34566b;"
               "background:#fbfaf6;border:1px solid #dfdbcf;border-radius:8px;box-sizing:border-box;"
               "min-height:40px;display:inline-flex;align-items:center;"
               "padding:0 15px;cursor:pointer;white-space:nowrap;")
    return (
        f'<div style="{wrap}">'
        '<style>html,body{margin:0;background:transparent;overflow:hidden;}'
        '#psb:hover{border-color:#34566b!important;color:#34566b!important;}</style>'
        f'<button id="psb" style="{btn}">🖨 Print call sheet</button>'
        '<script type="text/html" id="cs">' + tmpl + '</script>'
        '<script>document.getElementById("psb").addEventListener("click",function(){'
        'var h=document.getElementById("cs").textContent;var w=window.open("","_blank");'
        'if(!w){alert("Please allow pop-ups to open the printable call sheet.");return;}'
        'w.document.open();w.document.write(h);w.document.close();'
        'setTimeout(function(){try{w.focus();w.print();}catch(e){}},450);});</script>'
        '</div>')


def _footer():
    credit = (f"[{config.AUTHOR_NAME}]({config.AUTHOR_LINKEDIN})"
              if config.AUTHOR_LINKEDIN else config.AUTHOR_NAME)
    st.caption(f"Built by {credit}")


def main():
    icon = _asset("scout_icon_t.png", "scout_icon.png")
    st.set_page_config(page_title="Agent Scout — Living Battlecards", layout="wide",
                       page_icon=icon or "🐕", initial_sidebar_state="collapsed")
    # GA4: inject the tag into the parent (top-level) document via a same-origin
    # component, so referrer/geo are real. Idempotent per page-load; no-op if
    # disabled. Height 0 so it adds no visible space. Must come AFTER
    # set_page_config (Streamlit requires that to be the first call).
    _ga = analytics.ga_component_html()
    if _ga:
        try:
            st.iframe(_ga, height=1)   # st.iframe rejects height<=0; 1px is effectively invisible
        except Exception:
            pass   # never let the analytics tag crash the viewer (StreamlitInvalidHeightError, etc.)
    # Reliable, server-side visit capture (first-party log with real geo + GA4 server feed),
    # once per session. Off the render thread, and wrapped here so analytics can NEVER break
    # the viewer (incl. a hot-reload race where the module in memory predates this call).
    try:
        analytics.record_visit()
    except Exception:
        pass
    try:
        analytics.capture_city(st)   # best-effort browser-side city, stitched onto the visit
    except Exception:
        pass
    # ?print=<slug> opens a clean tab showing just the call sheet and auto-fires the print dialog.
    # The inline "Print call sheet" link points here: a link can't call window.open, but it CAN
    # navigate to this route, where the call sheet's own document triggers print on load.
    if st.query_params.get("print"):
        _render_print_tab(st.query_params["print"])
        return
    # Center the content to the same 1240px as the page's own .wrap (so masthead, the mode
    # switch, the card picker and the brief all line up), pull it up, and remove the sidebar
    # entirely — navigation lives IN the page now.
    st.markdown(
        '<style>'
        # High-specificity + !important so it beats Streamlit's own wide-layout rule.
        '[data-testid="stAppViewContainer"] section[data-testid="stMain"] '
        '[data-testid="stMainBlockContainer"],'
        'section[data-testid="stMain"] .block-container,.stMainBlockContainer,.block-container'
        '{max-width:1240px!important;margin-left:auto!important;margin-right:auto!important;'
        'padding:2.9rem 2rem 2.5rem!important;}'
        '[data-testid="stSidebar"],[data-testid="collapsedControl"]{display:none!important;}'
        # Tighten the vertical rhythm between masthead, the control row, and the brief.
        '[data-testid="stVerticalBlock"]{gap:.3rem!important;}'
        '[data-testid="stHorizontalBlock"]{margin-bottom:0!important;}'
        # Print / one-page call sheet (⌘P): drop all chrome + prep-only sections, keep the pitch
        # (title + Today's angle + Top 3 plays) and the rebuttals (objections, condensed).
        '@media print{'
        'header,[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stSidebar"],'
        '[data-testid="stHorizontalBlock"],[data-testid="stStatusWidget"],footer{display:none!important;}'
        '[data-testid="stMainBlockContainer"],.block-container{max-width:none!important;padding:0!important;}'
        # Streamlit's main view is a FIXED-HEIGHT scroll container, so print clips to one
        # screenful. Force the chain to flow so the whole sheet paginates instead of cutting off.
        'html,body,[data-testid="stApp"],[data-testid="stAppViewContainer"],[data-testid="stMain"],'
        'section[data-testid="stMain"],[data-testid="stMainBlockContainer"],.block-container,'
        '[data-testid="stVerticalBlock"],#scout-page,#scout-page .cols,#scout-page .maincol'
        '{height:auto!important;min-height:0!important;max-height:none!important;'
        'overflow:visible!important;position:static!important;}'
        '#scout-page .rail,#scout-page .metrics,#scout-page .livebox,#scout-page .divider,'
        '#scout-page #executive_summary,#scout-page #snapshot,#scout-page #recent_moves,'
        '#scout-page #positioning,#scout-page #pricing,#scout-page #bc,#scout-page #sentiment,'
        '#scout-page #cut,#scout-page .freshness,#scout-page .srcline{display:none!important;}'
        '#scout-page .cols,#scout-page .maincol{display:block!important;}'
        '#scout-page #objection_handling .item p{display:none!important;}'
        '#scout-page .sec,#scout-page .briefing,#scout-page .item,#scout-page .play'
        '{break-inside:avoid;box-shadow:none!important;}'
        '@page{margin:1.4cm;}}'
        + _CTRL_CSS + _FORM_CSS + '</style>', unsafe_allow_html=True)

    job_param = st.query_params.get("job")
    # Fonts (separate <link>), CSS, and masthead each go in their OWN st.markdown call —
    # Streamlit's sanitizer drops a <style> bundled with body HTML (or one containing @import),
    # which is what garbled the earlier cuts.
    st.markdown(_FONT_LINKS, unsafe_allow_html=True)
    st.markdown(page.style_block(), unsafe_allow_html=True)
    st.markdown(page.masthead_html(), unsafe_allow_html=True)

    # Control bar — mode tabs + card dropdown + print as ONE inline flex row (no Streamlit columns,
    # no iframe), so all three align by construction. Anchors drive state via query params (full
    # navigation). A ?job= deep link opens the create surface; ?card=<slug> selects a card directly.
    cards = _ordered_cards()  # most-recently-updated first; Batman vs Superman pinned to #4
    mode_param = st.query_params.get("mode")
    card_param = st.query_params.get("card")
    # mode is authoritative when present (an explicit tab click wins over a lingering ?job=).
    is_create = (mode_param == "create") if mode_param is not None else bool(job_param)
    slug = card_param if card_param in cards else (cards[0] if cards else None)
    living_href = f"?card={slug}" if slug else "?mode=battlecards"
    st.markdown(_control_bar_html(is_create, slug, cards, living_href), unsafe_allow_html=True)

    if is_create:
        if "card" in st.query_params:   # don't leave a stale ?card= on the create surface
            del st.query_params["card"]
        _render_selfserve(job_param)
        _footer()
        return
    if not cards:
        st.info("No battlecards have been generated yet.")
        return
    # Clean, shareable URL: just ?card=<slug> (drop the mode/job scaffolding once resolved).
    if st.query_params.get("card") != slug:
        st.query_params["card"] = slug
    for _k in ("job", "mode"):           # a roster card carries neither (no Frankenlinks)
        if _k in st.query_params:
            del st.query_params[_k]
    # Land at the top of a newly selected card (Streamlit preserves scroll across reruns).
    if st.session_state.get("_last_slug") != slug:
        st.session_state["_last_slug"] = slug
        st.iframe("<script>window.parent.scrollTo({top:0});</script>", height=1)
    # Title (full width) then the brief body — both inline. Credit lives in the left rail.
    st.markdown(_title_block_html(slug), unsafe_allow_html=True)
    st.markdown(page.content_html(slug), unsafe_allow_html=True)
    st.iframe(_countdown_component(), height=1)   # live-tick the "Next update" countdown


if __name__ == "__main__":
    main()
