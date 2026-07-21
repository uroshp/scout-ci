"""Rendered-surface probe for both public hosts (2026-07-20, born from the Streamlit
hot-reload crash: the viewer served HTTP 200 for ~38h while every visitor saw a crash
page — HTTP checks can't see what a browser sees, so this loads each host in headless
Chromium and requires REAL card content to render).

Two callers, same check:
  - .github/workflows/postdeploy.yml — after every push that redeploys (PROBE_EMAIL=1
    → emails the owner on failure via scout.notify).
  - ~/scout-tools/scout-canary (hosts check) — daily 08:00 PT on the mini; the canary
    folds this script's output into its own failure email, so no PROBE_EMAIL here.

Exit 0 = both hosts render. Exit 1 = failure(s), one per line on stdout.

Deps: playwright + chromium (NOT in requirements.txt on purpose — Streamlit Cloud must
not install a browser; the mini's venv and the Action install it themselves).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

# The app is broken if a visitor sees any of these (crash page, sleep page) — or nothing.
CRASH_MARKERS = [
    "This app has encountered an error",
    "Error running app",
    "This app has gone to sleep",
    "get this app back up",
]
# The app works only if a visitor sees the product. Both hosts render this masthead line.
CONTENT_MARKER = "Living battlecards"

HOSTS = {
    "agent-scout.ai": "https://agent-scout.ai/",
    "streamlit.app": "https://agent-scout.streamlit.app/",
}
WAIT_TOTAL_S = 90          # Streamlit cold start + websocket render can be slow
POLL_S = 5


def _visible_text(page):
    """All rendered text across the page AND its frames — streamlit.app serves the app
    inside a /~/+/ iframe, so the top document's body is empty even when healthy."""
    parts = []
    for frame in page.frames:
        try:
            parts.append(frame.inner_text("body"))
        except Exception:
            pass
    return " ".join(" ".join(parts).split())


def probe_host(browser, name, url):
    page = browser.new_page()
    try:
        page.goto(url, timeout=60_000)
    except Exception as e:
        page.close()
        return f"{name}: page load failed ({type(e).__name__}: {e})"
    try:
        for _ in range(WAIT_TOTAL_S // POLL_S):
            page.wait_for_timeout(POLL_S * 1000)
            text = _visible_text(page)
            for marker in CRASH_MARKERS:
                if marker in text:
                    return f"{name}: BROKEN — visitors see \"{marker}\""
            if CONTENT_MARKER in text:
                return None
        return (f"{name}: BROKEN — no card content rendered after {WAIT_TOTAL_S}s "
                f"(missing \"{CONTENT_MARKER}\"; page text: \"{text[:120]}\")")
    finally:
        page.close()


def run_probe():
    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, url in HOSTS.items():
            err = probe_host(browser, name, url)
            if err:                                    # one retry: cold starts, blips
                err = probe_host(browser, name, url)
            if err:
                failures.append(err)
        browser.close()
    return failures


def main():
    failures = run_probe()
    for f in failures:
        print(f)
    if failures and os.environ.get("PROBE_EMAIL") == "1":
        from scout import notify
        notify._dispatch(
            f"Scout post-deploy probe: {len(failures)} HOST(S) BROKEN",
            "A push just deployed and the rendered-surface probe failed:\n\n"
            + "\n".join(failures)
            + "\n\nVisitors are seeing this RIGHT NOW. Streamlit fix: Manage app -> Reboot."
            "\n\n— probe_hosts via postdeploy.yml",
            dry_run=False,
        )
    if not failures:
        print("OK both hosts render card content")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
