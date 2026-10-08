"""The rendered-fetch tier: a headless browser for pages the plain fetcher cannot read.

Three outcomes for any page, decided by code:
  plain      httpx reads it (grounding's hardened fetcher; the default, and what grounding uses later)
  rendered   httpx gets a 401/403/429 or a JavaScript shell with no text; a headless Chromium
             (Playwright, the same browser the deploy probe uses) renders it and the text is there
  challenge  even the browser gets a bot wall ("Just a moment", "verify you are human"): the page
             needs a browser infrastructure built for that (TinyFish Fetch, when a key is configured)
             or stays unreadable and is shown as such

The browser is launched once per process and reused; a missing Playwright install degrades to
"unavailable" and the pass records it, never raises. Same SSRF guard as the plain fetcher: only
http(s) to public hosts.
"""
from __future__ import annotations

import os
import sys

from scout import grounding

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
CHALLENGE_MARKERS = ("just a moment", "verify you are human", "enable javascript and cookies to continue",
                     "checking your browser", "cf-challenge", "attention required! | cloudflare", "access denied",
                     "please verify you are a human", "ddos-guard", "are you a robot")
TIMEOUT_MS = int(os.environ.get("SCOUT_RENDER_TIMEOUT_MS", "25000"))
_STATE: dict = {"pw": None, "browser": None, "context": None, "unavailable": None}


def available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _context():
    if _STATE["context"] is not None:
        return _STATE["context"]
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
    _STATE.update({"pw": pw, "browser": browser, "context": ctx})
    return ctx


def close() -> None:
    try:
        if _STATE["browser"] is not None:
            _STATE["browser"].close()
        if _STATE["pw"] is not None:
            _STATE["pw"].stop()
    except Exception:
        pass
    _STATE.update({"pw": None, "browser": None, "context": None})


def is_challenge(html: str, title: str = "") -> bool:
    low = ((title or "") + " " + (html or "")[:20000]).lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def fetch(url: str) -> dict:
    """{"status": int|None, "text": html|None, "error": str|None, "challenge": bool, "title": str}.
    The browser work runs on a daemon thread with a hard wall-clock bound (2026-10-08: a synchronous
    Playwright call after a certificate error never returned and the whole morning waited on it).
    A renderer that hangs once is retired for the rest of the process; the plain tier carries on."""
    try:
        grounding._assert_fetchable(url)
    except Exception as e:
        return {"status": None, "text": None, "error": f"blocked: {e}", "challenge": False, "title": ""}
    if _STATE.get("unavailable"):
        return {"status": None, "text": None, "error": f"renderer unavailable ({_STATE['unavailable']})", "challenge": False, "title": ""}
    if not available():
        _STATE["unavailable"] = "playwright not installed"
        return {"status": None, "text": None, "error": "renderer unavailable (playwright not installed)", "challenge": False, "title": ""}
    import threading
    from scout import config as _cfg
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(_fetch_blocking(url)), daemon=True)
    t.start()
    t.join(timeout=_cfg.SENSOR_RENDER_HARD_TIMEOUT_S)
    if t.is_alive():
        _STATE["unavailable"] = f"renderer hung on {url[:60]}"
        _STATE.update({"pw": None, "browser": None, "context": None})      # never touch the stuck browser again
        print(f"[sensors] rendered fetch HUNG for {url}: renderer retired for this run", file=sys.stderr, flush=True)
        return {"status": None, "text": None, "error": "renderer hung", "challenge": False, "title": ""}
    return box or {"status": None, "text": None, "error": "renderer returned nothing", "challenge": False, "title": ""}


def _fetch_blocking(url: str) -> dict:
    page = None
    try:
        ctx = _context()
        page = ctx.new_page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        html = page.content()
        title = page.title() or ""
        status = resp.status if resp else None
        if is_challenge(html, title):
            return {"status": status, "text": None, "error": "bot wall (challenge page)", "challenge": True, "title": title}
        if status and status >= 400:
            return {"status": status, "text": None, "error": f"HTTP {status}", "challenge": False, "title": title}
        return {"status": status, "text": html, "error": None, "challenge": False, "title": title}
    except Exception as e:
        print(f"[sensors] rendered fetch failed for {url} ({type(e).__name__}: {str(e)[:80]})", file=sys.stderr)
        return {"status": None, "text": None, "error": f"{type(e).__name__}", "challenge": False, "title": ""}
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
