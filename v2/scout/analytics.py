"""Google Analytics (GA4) for the Streamlit viewer.

Streamlit Community Cloud serves the app behind an auth-bootstrap redirect and
gives us no way to edit the served page <head>, so a static-file patch is neither
reliable nor verifiable there. Instead we inject the gtag tag from a Streamlit
component into the PARENT document: the component iframe is same-origin
(`allow-same-origin`), so its script can append gtag.js to `window.parent`'s
<head> and run it in the real top-level page — correct URL, referrer, and geo.

This runs on every page load (no cold-start gap) and needs no filesystem write.
A guard flag on the parent window makes it idempotent across Streamlit's reruns,
so a visit is counted once, not once per interaction.
"""
import json
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from scout import config


def ga_component_html(measurement_id: str | None = None) -> str:
    """HTML to hand to st.components.v1.html(..., height=0). Empty string when
    analytics is disabled (no measurement id)."""
    mid = measurement_id or config.GA_MEASUREMENT_ID
    if not mid:
        return ""
    return (
        "<script>(function(){"
        "var p=window.parent;"
        "if(!p||p.__scoutGA)return;"          # already loaded in this top-level page
        "p.__scoutGA=true;"
        "var d=p.document,s=d.createElement('script');"
        "s.async=true;"
        f"s.src='https://www.googletagmanager.com/gtag/js?id={mid}';"
        "d.head.appendChild(s);"
        "p.dataLayer=p.dataLayer||[];"
        "function gtag(){p.dataLayer.push(arguments);}"
        "p.gtag=gtag;"
        "gtag('js',new Date());"
        f"gtag('config','{mid}');"
        "})();</script>"
    )


# --- Server-side, unblockable visit capture -----------------------------------
# The client gtag above is blocked by ad blockers, Safari ITP, and privacy tools, which is
# exactly the cohort that matters (recruiters, execs, AI people). These run on OUR server
# instead, so nothing in the visitor's browser can stop them. record_visit() fires once per
# session and is fully wrapped: analytics can never break the viewer. Assumes low traffic
# (a portfolio tool), so a per-visit private-store write is fine.

def _visitor_ctx(st):
    """Visitor IP / referrer / user-agent / card from the request headers. Best-effort."""
    try:
        h = dict(st.context.headers or {})
    except Exception:
        h = {}
    g = lambda *keys: next((h[k] for k in keys if k in h), "")
    xff = g("X-Forwarded-For", "x-forwarded-for")
    ip = xff.split(",")[0].strip() if xff else ""
    try:
        card = st.query_params.get("card") or ""
    except Exception:
        card = ""
    return ip, g("Referer", "referer"), g("User-Agent", "user-agent"), card


_BOT_UA = ("headlesschrome", "playwright", "bot", "spider", "crawl", "slurp",
           "python-requests", "curl/", "wget", "lighthouse", "pingdom", "uptime", "monitor")


def _is_bot(ua):
    """True for the keep-warm headless browser and other bots, so they don't pollute the
    real-visitor log. Genuine Chrome/Safari/Firefox UAs match none of these."""
    u = (ua or "").lower()
    return any(b in u for b in _BOT_UA)


def _geo(ip):
    """City, Country for an IP via a free lookup. '' on any failure (geo is a bonus)."""
    if not ip:
        return ""
    try:
        url = f"http://ip-api.com/json/{urllib.parse.quote(ip)}?fields=status,city,country"
        req = urllib.request.Request(url, headers={"User-Agent": "scout-visitlog/1"})
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("status") == "success":
            return ", ".join(x for x in (d.get("city"), d.get("country")) if x)
    except Exception:
        pass
    return ""


def _ga4_server_event(client_id, ip, ref, card):
    """Fire a GA4 Measurement Protocol 'server_visit' event (server-side, unblockable). A
    distinct event name (not page_view) so it never double-counts the client gtag and gives a
    clean reliable visit metric. No-op without an API secret. Best-effort."""
    mid, secret = config.GA_MEASUREMENT_ID, config.GA_API_SECRET
    if not (mid and secret):
        return
    try:
        loc = config.SELFSERVE_APP_URL + (f"/?card={card}" if card else "")
        payload = {"client_id": client_id, "events": [{
            "name": "server_visit",
            "params": {"session_id": client_id, "engagement_time_msec": "1",
                       "page_location": loc, "page_referrer": ref or "(direct)"},
        }]}
        url = ("https://www.google-analytics.com/mp/collect"
               f"?measurement_id={urllib.parse.quote(mid)}&api_secret={urllib.parse.quote(secret)}")
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass


def _append_visit(rec):
    """Append one visit to a per-day JSONL log in the PRIVATE store (real geo/referrer/card,
    fully ours, unblockable). Low traffic, so a read-modify-write per visit is fine."""
    from scout import selfserve
    path = f"analytics/visits-{rec['ts'][:10]}.jsonl"
    try:
        existing = (selfserve.read_data(path) or "").rstrip("\n")
    except Exception:
        existing = ""
    body = (existing + "\n" if existing else "") + json.dumps(rec, ensure_ascii=False) + "\n"
    selfserve.write_data(path, body, f"analytics: visit {rec['ts']} {rec.get('geo') or rec.get('ip') or '?'}")


def _log_async(client_id, ip, ref, ua, card):
    """The slow part (geo lookup, GA4 POST, private-store write) off the render thread."""
    _ga4_server_event(client_id, ip, ref, card)
    try:
        _append_visit({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ip": ip, "geo": _geo(ip), "card": card, "referrer": ref, "user_agent": ua,
        })
    except Exception as e:
        print(f"[analytics] visit-log write skipped ({type(e).__name__}: {e})", file=sys.stderr)


def record_visit():
    """Reliable, server-side visit capture: a first-party JSONL log (with real geo) plus a GA4
    server-side event. Both run on our server, so ad blockers can't stop them. Fires once per
    session, hands the slow I/O to a daemon thread, and is wrapped so it can NEVER break the
    page render."""
    import streamlit as st
    try:
        if st.session_state.get("_visit_logged"):
            return
        st.session_state["_visit_logged"] = True
        cid = st.session_state.setdefault("_visit_cid", uuid.uuid4().hex)
        ip, ref, ua, card = _visitor_ctx(st)   # fast, from headers, on the render thread
        if _is_bot(ua):                          # keep-warm + crawlers out of the real-visitor log
            return
        threading.Thread(target=_log_async, args=(cid, ip, ref, ua, card), daemon=True).start()
    except Exception as e:
        print(f"[analytics] record_visit skipped ({type(e).__name__}: {e})", file=sys.stderr)
