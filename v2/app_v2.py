"""Retirement stub for agent-scout.streamlit.app (2026-09-04, per the 7/21 decision).

The Streamlit viewer is retired; battlecards live at https://agent-scout.ai. This stub is the
whole app now: it renders an always-working link to the new home (carrying every query param, so
resume UTM tracking survives the hop), attempts a meta-refresh auto-redirect on top, and keeps the
server-side visit log (scout.analytics.record_visit -> JSONL + GA server event) so remaining
June-resume clicks stay visible. The old viewer is one `git revert` away.

FAIL-SAFE BY CONSTRUCTION: the load-bearing element is the plain link — the auto-redirect is an
enhancement (Streamlit sanitizes script tags; meta-refresh works in mainstream browsers but is
never relied on). Nothing here may crash the render (viewer-render-path rule, 2026-06-18): every
non-trivial call is wrapped.
"""
import os
from urllib.parse import urlencode

import streamlit as st

# Streamlit Community Cloud exposes configured secrets via st.secrets, NOT as env vars. Bridge
# them into the environment BEFORE importing scout modules (config reads env at import time), so
# the deployed stub still reaches the private store for visit logging. setdefault never clobbers
# a real env var (local dev still wins); the guard makes it a no-op when no secrets exist.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass

NEW_HOME = "https://agent-scout.ai/"
REDIRECT_SECONDS = 5

st.set_page_config(page_title="Scout has moved", page_icon="🔭", layout="centered")

# Carry every query param over (utm_source/utm_medium from the June resume PDFs, ?me=1, etc.) so
# visit attribution survives the hop. Any failure falls back to the bare new-home URL.
target = NEW_HOME
try:
    params = {k: v for k, v in st.query_params.items() if v}
    if params:
        target = NEW_HOME + "?" + urlencode(params)
except Exception:
    pass

# Best-effort auto-redirect. Streamlit strips <script> tags; meta-refresh survives in mainstream
# browsers. If it doesn't fire, the visitor still has the link below — no dead end either way.
try:
    st.markdown(f'<meta http-equiv="refresh" content="{REDIRECT_SECONDS};url={target}">',
                unsafe_allow_html=True)
except Exception:
    pass

st.title("Scout has moved 🔭")
st.markdown(
    f"Scout's living battlecards now live at **[agent-scout.ai]({target})** — "
    f"you'll be taken there automatically in a few seconds."
)
st.link_button("Open Scout →", target)
st.caption("This address (agent-scout.streamlit.app) is retiring. "
           "Please update any bookmarks to agent-scout.ai.")

# Server-side visit capture (JSONL ground truth + GA server event). record_visit is internally
# wrapped, bot-filtered (keep-warm/probes excluded) and me-aware (?me=1 / scout_me cookie) — it
# can never break the render, and the outer guard keeps even an import-time surprise out.
try:
    from scout import analytics
    analytics.record_visit()
except Exception as _e:
    print(f"[stub] visit logging skipped ({type(_e).__name__}: {_e})")
