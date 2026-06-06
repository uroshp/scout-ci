"""Top-level Google Analytics (GA4) injection for the Streamlit viewer.

Streamlit Community Cloud gives us no way to edit the served page <head>, and the
usual workarounds load tracking inside a sandboxed iframe (st.components.html) —
which reports the iframe's URL with no referrer, breaking exactly the "where did
they come from" data we want. So instead we patch the gtag snippet into
Streamlit's own static index.html ONCE per process, so it runs in the TOP-LEVEL
page (correct URL, referrer, geo). The container filesystem is writable and the
patch re-applies on every cold start, so it survives Streamlit upgrades.

Safe by construction: idempotent (keyed on a marker comment), and every failure
is swallowed — analytics must never take the app down.
"""
import os

from scout import config

_MARKER = "<!-- scout-ga4 -->"
_done = False  # process-level guard: Streamlit re-runs the entry script every interaction


def _snippet(measurement_id: str) -> str:
    return (
        f"{_MARKER}\n"
        f'<script async src="https://www.googletagmanager.com/gtag/js?id={measurement_id}"></script>\n'
        "<script>window.dataLayer=window.dataLayer||[];"
        "function gtag(){dataLayer.push(arguments);}"
        "gtag('js',new Date());"
        f"gtag('config','{measurement_id}');</script>\n"
    )


def _index_html_path() -> str | None:
    try:
        import streamlit as st
        path = os.path.join(os.path.dirname(st.__file__), "static", "index.html")
        return path if os.path.exists(path) else None
    except Exception:
        return None


def inject_ga(measurement_id: str | None = None) -> bool:
    """Insert the GA4 tag into Streamlit's static index.html <head>. Returns True
    if the tag is present afterwards (newly written or already there), False if
    disabled or the patch couldn't be applied."""
    global _done
    if _done:
        return True
    mid = measurement_id or config.GA_MEASUREMENT_ID
    if not mid:
        return False
    path = _index_html_path()
    if not path:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
        if _MARKER in html:
            _done = True
            return True  # already patched this container
        if "</head>" not in html:
            return False
        html = html.replace("</head>", _snippet(mid) + "</head>", 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        _done = True
        return True
    except Exception:
        return False
