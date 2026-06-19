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
import ipaddress
import json
import sys
import threading
import time
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
        "var p=window.parent;if(!p)return;"
        # opt-out marker: visiting ?me=1 sets a 2-year cookie so this device's OWN visits are
        # excluded from the log (checked server-side in record_visit). Harmless if a stranger sets it.
        "try{if(p.location.search.indexOf('me=1')>-1){p.document.cookie='scout_me=1; max-age=63072000; path=/; SameSite=Lax';}}catch(e){}"
        "if(p.__scoutGA)return;"              # GA already loaded in this top-level page
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

def _client_ip(h):
    """First PUBLIC client IP across the common proxy headers. Streamlit Cloud's
    X-Forwarded-For can LEAD with internal k8s IPs (192.168/10.x), so skip private ones
    and take the first real public address."""
    cands = []
    for k in ("X-Forwarded-For", "x-forwarded-for"):
        if h.get(k):
            cands += [p.strip() for p in h[k].split(",") if p.strip()]
    for k in ("X-Real-IP", "x-real-ip", "CF-Connecting-IP", "cf-connecting-ip",
              "True-Client-IP", "true-client-ip", "Fastly-Client-IP", "X-Client-IP"):
        if h.get(k):
            cands.append(h[k].strip())
    for ip in cands:
        try:
            a = ipaddress.ip_address(ip)
            if not (a.is_private or a.is_loopback or a.is_reserved or a.is_link_local):
                return ip
        except ValueError:
            continue
    return cands[0] if cands else ""


def _visitor_ctx(st):
    """Visitor IP / referrer / user-agent / card from the request headers. Best-effort."""
    try:
        h = dict(st.context.headers or {})
    except Exception:
        h = {}
    g = lambda *keys: next((h[k] for k in keys if k in h), "")
    try:
        card = st.query_params.get("card") or ""
    except Exception:
        card = ""
    return _client_ip(h), g("Referer", "referer"), g("User-Agent", "user-agent"), card


_BOT_UA = ("headlesschrome", "playwright", "bot", "spider", "crawl", "slurp",
           "python-requests", "curl/", "wget", "lighthouse", "pingdom", "uptime", "monitor")


def _is_bot(ua):
    """True for the keep-warm headless browser and other bots, so they don't pollute the
    real-visitor log. Genuine Chrome/Safari/Firefox UAs match none of these."""
    u = (ua or "").lower()
    return any(b in u for b in _BOT_UA)


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
    """GA4 server event + the first-party visit write, off the render thread. geo starts empty
    and capture_city() fills it in from the browser, since Streamlit Cloud strips the real IP."""
    _ga4_server_event(client_id, ip, ref, card)
    try:
        _append_visit({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "_cid": client_id, "ip": ip, "geo": "", "card": card, "referrer": ref, "user_agent": ua,
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
        # skip your own devices, each marked once by visiting ?me=1 (sets the scout_me cookie)
        try:
            h = dict(st.context.headers or {})
            if "scout_me=1" in (h.get("Cookie") or h.get("cookie") or ""):
                return
        except Exception:
            pass
        cid = st.session_state.setdefault("_visit_cid", uuid.uuid4().hex)
        ip, ref, ua, card = _visitor_ctx(st)     # fast, from headers, on the render thread
        if _is_bot(ua):                          # keep-warm + crawlers out of the real-visitor log
            return
        threading.Thread(target=_log_async, args=(cid, ip, ref, ua, card), daemon=True).start()
    except Exception as e:
        print(f"[analytics] record_visit skipped ({type(e).__name__}: {e})", file=sys.stderr)


def _patch_city(cid, city, day):
    """Stitch the browser-resolved city onto this session's visit record (matched by _cid).
    The immediate server write may still be in flight, so retry a few times. Best-effort."""
    from scout import selfserve
    path = f"analytics/visits-{day}.jsonl"
    for _ in range(6):
        try:
            lines = (selfserve.read_data(path) or "").splitlines()
            for i, ln in enumerate(lines):
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("_cid") == cid and not rec.get("geo"):
                    rec["geo"] = city
                    lines[i] = json.dumps(rec, ensure_ascii=False)
                    selfserve.write_data(path, "\n".join(lines) + "\n",
                                         f"analytics: geo {city} for {cid[:8]}")
                    return
        except Exception:
            pass
        time.sleep(2)   # the immediate visit write may not have landed yet


def capture_city(st):
    """Best-effort CLIENT-side city, since Streamlit Cloud never gives the app the real IP. A
    hidden component has the visitor's browser ask a free geo API; when it resolves, the city is
    stitched onto this session's visit record. Fully wrapped: worst case is no city, never a
    visible or broken page. Must be called every rerun so the component can resolve."""
    try:
        if st.session_state.get("_geo_done") or not st.session_state.get("_visit_cid"):
            return
        from streamlit_javascript import st_javascript
        city = st_javascript(
            "await (async () => { try {"
            "  const r = await fetch('https://get.geojs.io/v1/ip/geo.json');"
            "  const d = await r.json();"
            "  return [d.city, d.country].filter(Boolean).join(', ');"
            "} catch (e) { return ''; } })()"
        )
        if city is None or city == 0:        # component not resolved yet; retry next rerun
            return
        st.session_state["_geo_done"] = True
        if city:
            day = datetime.now(timezone.utc).date().isoformat()
            threading.Thread(target=_patch_city, args=(st.session_state["_visit_cid"], city, day),
                             daemon=True).start()
    except Exception as e:
        st.session_state["_geo_done"] = True     # don't loop forever on a broken setup
        print(f"[analytics] capture_city skipped ({type(e).__name__}: {e})", file=sys.stderr)
