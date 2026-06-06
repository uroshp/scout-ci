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


_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,ital,'
    'wght@9..144,0,400;9..144,0,500;9..144,0,600;9..144,1,400;9..144,1,500'
    '&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">')


# In-page mode toggle as a connected SEGMENTED CONTROL (one line, no wrap, slate-filled active).
_MODE_CSS = (
    'div[role="radiogroup"]{display:inline-flex!important;flex-wrap:nowrap!important;gap:0!important;'
    'border:1px solid #dfdbcf;border-radius:8px;overflow:hidden;background:#fbfaf6;}'
    'div[role="radiogroup"]>label{margin:0!important;padding:.42rem 1.05rem;font-weight:600;'
    'font-size:.92rem;color:#5f5e54;cursor:pointer;white-space:nowrap;'
    'border-right:1px solid #dfdbcf;transition:background .15s,color .15s;}'
    'div[role="radiogroup"]>label:last-child{border-right:none;}'
    'div[role="radiogroup"]>label:hover{color:#34566b;}'
    'div[role="radiogroup"]>label:has(input:checked){background:#34566b!important;}'
    # Streamlit nests the label text in a child with its own color, so white must be forced on
    # the descendants too — otherwise the active segment is dark text on the dark slate fill.
    'div[role="radiogroup"]>label:has(input:checked),'
    'div[role="radiogroup"]>label:has(input:checked) *{color:#fff!important;}'
    'div[role="radiogroup"]>label>div:first-child{display:none!important;}'
)


def _card_label(slug: str) -> str:
    """Short, proper-cased dropdown label from meta, e.g. 'OpenAI vs Anthropic'. (Focus area is
    already shown in the title block, so it's dropped here to keep the dropdown compact.)"""
    m = store.load_meta(slug) or {}
    comp = (m.get("competitor") or "").strip()
    mine = (m.get("my_company") or "").strip()
    if not comp:
        return _pretty(slug)
    return f"{comp} vs {mine}" if mine else comp


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
        '<style>'
        # High-specificity + !important so it beats Streamlit's own wide-layout rule.
        '[data-testid="stAppViewContainer"] section[data-testid="stMain"] '
        '[data-testid="stMainBlockContainer"],'
        'section[data-testid="stMain"] .block-container,.stMainBlockContainer,.block-container'
        '{max-width:1240px!important;margin-left:auto!important;margin-right:auto!important;'
        'padding:1.2rem 2rem 2.5rem!important;}'
        '[data-testid="stSidebar"],[data-testid="collapsedControl"]{display:none!important;}'
        # Tighten the vertical rhythm between masthead, the control row, and the brief.
        '[data-testid="stVerticalBlock"]{gap:.3rem!important;}'
        '[data-testid="stHorizontalBlock"]{margin-bottom:0!important;}'
        + _MODE_CSS + '</style>', unsafe_allow_html=True)

    job_param = st.query_params.get("job")
    # Fonts (separate <link>), CSS, and masthead each go in their OWN st.markdown call —
    # Streamlit's sanitizer drops a <style> bundled with body HTML (or one containing @import),
    # which is what garbled the earlier cuts.
    st.markdown(_FONT_LINKS, unsafe_allow_html=True)
    st.markdown(page.style_block(), unsafe_allow_html=True)
    st.markdown(page.masthead_html(), unsafe_allow_html=True)

    # Mode switch + card picker on ONE tight row (no sidebar). A ?job= deep link opens the
    # create surface; a ?card=<slug> deep link selects that battlecard directly (permalink).
    cards = display.list_battlecards()
    mc1, mc2, _sp = st.columns([1.2, 1.1, 2.2], gap="small", vertical_alignment="center")
    with mc1:
        mode = st.radio("Mode", ["Living battlecards", "Create your own"],
                        index=1 if job_param else 0, horizontal=True, label_visibility="collapsed")
    is_create = bool(job_param) or mode == "Create your own"
    slug = None
    with mc2:
        if not is_create and cards:
            card_param = st.query_params.get("card")
            if "card_select" not in st.session_state and card_param in cards:
                st.session_state["card_select"] = card_param        # honor the ?card= permalink
            slug = st.selectbox("Battlecard", cards, format_func=_card_label,
                                label_visibility="collapsed", key="card_select")

    if is_create:
        _render_selfserve(job_param)
        _footer()
        return
    if not cards:
        st.info("No battlecards have been generated yet.")
        return
    # Reflect the selection in the URL so each card has a shareable permalink (?card=<slug>).
    if st.query_params.get("card") != slug:
        st.query_params["card"] = slug
    # Land at the top of a newly selected card (Streamlit preserves scroll across reruns).
    if st.session_state.get("_last_slug") != slug:
        st.session_state["_last_slug"] = slug
        components.html("<script>window.parent.scrollTo({top:0});</script>", height=0)
    # The battlecard body, rendered INLINE in Streamlit's own document (scroll + #anchor jumps
    # work natively; an iframe broke both on Streamlit Cloud). CSS scoped to #scout-page.
    # Credit ("Built by …") lives in the left rail, under Alerts — no bottom footer here.
    st.markdown(page.content_html(slug), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
