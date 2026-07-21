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


def _hosts_js() -> str:
    """The analytics hostname allow-list as a JS array literal, shared by BOTH gtag injectors
    (the Streamlit component below and server.py's _ga_head) so the guard can never drift
    between surfaces."""
    return json.dumps(list(config.ANALYTICS_HOSTNAMES))


def ga_component_html(measurement_id: str | None = None) -> str:
    """HTML to hand to st.iframe(..., height=1). Empty string when analytics is disabled
    (no measurement id). The injected script SELF-GATES client-side so GA only fires for real
    prod visitors: (1) it bails unless the page hostname is the prod host — so the app running in
    a Codespace / localhost / any dev preview never fires GA (the github.dev + localhost dev
    traffic we saw); (2) it bails if the `scout_me` opt-out cookie is set — so your own marked
    devices are excluded EVERYWHERE (cellular, Codespace, anywhere), not just by a stale IP filter."""
    mid = measurement_id or config.GA_MEASUREMENT_ID
    if not mid:
        return ""
    # Only run on the allow-listed PROD hostnames (config.ANALYTICS_HOSTNAMES — its own config,
    # NOT derived from SELFSERVE_APP_URL: the 2026-07-08 incident showed a link-base repoint can
    # silently disarm the tag). If the list is somehow empty, skip the guard rather than block GA
    # everywhere (matches the old unset-URL fallback).
    host_guard = (f"try{{if({_hosts_js()}.indexOf(p.location.hostname)===-1)return;}}"
                  "catch(e){return;}"
                  if config.ANALYTICS_HOSTNAMES else "")
    js = (
        "(function(){"
        "var p=window.parent;if(!p)return;"
        + host_guard +
        # opt-out marker: visiting ?me=1 sets a 2-year cookie so this device's OWN visits are
        # excluded from the client tag here AND the server feed/log (record_visit checks it too).
        "try{if(p.location.search.indexOf('me=1')>-1){p.document.cookie='scout_me=1; max-age=63072000; path=/; SameSite=Lax';}}catch(e){}"
        "try{if((p.document.cookie||'').indexOf('scout_me=1')>-1)return;}catch(e){}"
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
        "})();"
    )
    return f"<script>{js}</script>"


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
    """Visitor IP / referrer / user-agent / card / utm from the request + URL. Best-effort.
    Reads the UTM params HERE, at landing (record_visit runs early in main(), before app_v2
    rewrites the query string — it sets ?card= and drops the utm a beat later). Capturing them
    now is what makes campaign attribution reliable: Streamlit strips utm from the live URL, so
    the client gtag often never sees it, but this server-side read does."""
    try:
        h = dict(st.context.headers or {})
    except Exception:
        h = {}
    g = lambda *keys: next((h[k] for k in keys if k in h), "")
    try:
        qp = st.query_params
        card = qp.get("card") or ""
        utm = {k: qp.get(k) for k in ("utm_source", "utm_medium", "utm_campaign",
                                      "utm_term", "utm_content") if qp.get(k)}
    except Exception:
        card, utm = "", {}
    return _client_ip(h), g("Referer", "referer"), g("User-Agent", "user-agent"), card, utm


# Public-domain (agent-scout.ai) gets crawled/scanned constantly; the server-side catcher can't
# tell a cookie-less bot from a blocked human, so the UA list must be broad. Observed offenders on
# .ai: TLM-Audit-Scanner (scanner/audit), CheckMarkNetwork (checkmark), okhttp. None of these tokens
# occur in a real Chrome/Safari/Firefox UA. (Browser-UA datacenter bots still slip through; that's
# why human counts read off the CLIENT page_view, which bots don't fire, not server_visit.)
_BOT_UA = ("headlesschrome", "headless", "playwright", "bot", "spider", "crawl", "slurp",
           "python-requests", "python/", "httpx", "scoutprobe", "curl/", "wget", "lighthouse",
           "pingdom", "uptime",
           "monitor", "scanner", "audit", "checkmark", "okhttp", "ahrefs", "semrush", "dataforseo",
           "facebookexternalhit", "go-http-client", "java/", "node-fetch", "axios", "scrapy")


def _is_bot(ua):
    """True for the keep-warm headless browser and other bots, so they don't pollute the
    real-visitor log. Genuine Chrome/Safari/Firefox UAs match none of these."""
    u = (ua or "").lower()
    return any(b in u for b in _BOT_UA)


def _ga4_server_event(client_id, ip, ref, card, utm=None):
    """Fire a GA4 Measurement Protocol 'server_visit' event (server-side, unblockable). A
    distinct event name (not page_view) so it never double-counts the client gtag and gives a
    clean reliable visit metric. No-op without an API secret. Best-effort."""
    mid, secret = config.GA_MEASUREMENT_ID, config.GA_API_SECRET
    if not (mid and secret):
        return
    try:
        utm = utm or {}
        # Build page_location with the captured utm + card. The utm rides in the URL so GA4 reads the
        # campaign source off page_location — Streamlit strips utm from the live client URL before the
        # gtag reliably sees it, so this server-side copy is the source of truth for attribution.
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}"
                      for k, v in list(utm.items()) + ([("card", card)] if card else []))
        loc = config.SELFSERVE_APP_URL + (f"/?{qs}" if qs else "")
        # The Streamlit app sends ONE page_title for every card, so GA's default reports can't tell
        # which battlecard held a visitor. Stamp the slug onto this server event three ways: a
        # per-card page_title (shows in the default Page-title report, no setup), page_location with
        # ?card (drives the Pages/path report), and a clean `card` param (register as an event-scoped
        # custom dimension in GA to slice server_visit by battlecard).
        slug = card or "(home)"
        # session_id must be a POSITIVE NUMBER (the old hex client_id here was invalid — GA
        # couldn't form sessions from these events, hence the "missing session_start" warning).
        # server_visit fires once per session, so a fresh timestamp IS the session id.
        params = {"session_id": int(time.time()), "engagement_time_msec": "1",
                  "page_location": loc, "page_referrer": ref or "(direct)",
                  "page_title": f"Agent Scout · {slug}", "card": slug}
        params.update(utm)   # also expose utm_* as event params for custom-dimension slicing
        payload = {"client_id": client_id, "events": [{"name": "server_visit", "params": params}],
                   # ip_override: GA geolocates the REAL visitor IP, so server_visit rows carry
                   # city/country like client events (2026-07-21 — was all "(not set)").
                   "ip_override": ip,
                   # Truthful consent state (no ads run, no ad data collected) — also what GA
                   # wants declared so MP events stop tripping the consent-mode warning.
                   "consent": {"ad_user_data": "DENIED", "ad_personalization": "DENIED"}}
        url = ("https://www.google-analytics.com/mp/collect"
               f"?measurement_id={urllib.parse.quote(mid)}&api_secret={urllib.parse.quote(secret)}")
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass


def _append_visit(rec):
    """Append one visit to a per-day JSONL log in the PRIVATE store (real geo/referrer/card, fully
    ours, unblockable). Conflict-safe append (selfserve.append_data re-reads + retries on a sha
    conflict) so two visits in the same window serialize instead of the second 409ing and being
    lost — the bug that swallowed real visits."""
    from scout import selfserve
    path = f"analytics/visits-{rec['ts'][:10]}.jsonl"
    selfserve.append_data(path, json.dumps(rec, ensure_ascii=False),
                          f"analytics: visit {rec['ts']} {rec.get('geo') or rec.get('ip') or '?'}")


def _log_async(client_id, ip, ref, ua, card, utm=None):
    """GA4 server event + the first-party visit write, off the render thread. geo starts empty
    and capture_city() fills it in from the browser, since Streamlit Cloud strips the real IP."""
    utm = utm or {}
    _ga4_server_event(client_id, ip, ref, card, utm)
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "_cid": client_id, "ip": ip, "geo": "", "card": card, "referrer": ref, "user_agent": ua,
        }
        rec.update(utm)   # utm_source / utm_medium / ... captured at landing, before Streamlit strips them
        _append_visit(rec)
    except Exception as e:
        print(f"[analytics] visit-log write skipped ({type(e).__name__}: {e})", file=sys.stderr)


_config_logged = False


def _log_config_once():
    """Print, once per process, whether GA is actually wired up in this environment — so a missing
    Streamlit secret is visible in the logs instead of silently no-opping the server feed. The MP
    event only fires when BOTH are present (see _ga4_server_event)."""
    global _config_logged
    if _config_logged:
        return
    _config_logged = True
    mid = "present" if config.GA_MEASUREMENT_ID else "absent"
    sec = "present" if config.GA_API_SECRET else "absent"
    server_feed = "ON" if (config.GA_MEASUREMENT_ID and config.GA_API_SECRET) else "OFF"
    print(f"[analytics] GA config — measurement_id: {mid}, MP api_secret: {sec} "
          f"(server_visit feed: {server_feed})", file=sys.stderr)


def record_visit():
    """Reliable, server-side visit capture: a first-party JSONL log (with real geo) plus a GA4
    server-side event. Both run on our server, so ad blockers can't stop them. Fires once per
    session, hands the slow I/O to a daemon thread, and is wrapped so it can NEVER break the
    page render."""
    import streamlit as st
    try:
        _log_config_once()
        if st.session_state.get("_visit_logged"):
            return
        st.session_state["_visit_logged"] = True
        # skip your own devices: the scout_me cookie (a returning marked device sends it in the
        # request) OR ?me= in the URL right now. The query-param check is the fix — it catches the
        # FIRST ?me=1 load too, before the cookie is ever in the request headers, which is exactly
        # why server_visit kept firing for your own visits even after the client tag was suppressed.
        try:
            h = dict(st.context.headers or {})
            opted_out = "scout_me=1" in (h.get("Cookie") or h.get("cookie") or "")
        except Exception:
            opted_out = False
        try:
            opted_out = opted_out or bool(st.query_params.get("me"))
        except Exception:
            pass
        if opted_out:
            return
        cid = st.session_state.setdefault("_visit_cid", uuid.uuid4().hex)
        ip, ref, ua, card, utm = _visitor_ctx(st)   # fast, from headers + URL, on the render thread
        if _is_bot(ua):                             # keep-warm + crawlers out of the real-visitor log
            return
        threading.Thread(target=_log_async, args=(cid, ip, ref, ua, card, utm), daemon=True).start()
    except Exception as e:
        print(f"[analytics] record_visit skipped ({type(e).__name__}: {e})", file=sys.stderr)


def _patch_city(cid, city, day):
    """Stitch the browser-resolved city onto this session's visit record (matched by _cid). The
    immediate server write may still be in flight, so wait for the record to land, then patch it
    conflict-safe (selfserve.update_data re-reads + retries on a sha conflict, so a concurrent
    visit append can't make this patch clobber it or 409 away). Best-effort."""
    from scout import selfserve
    path = f"analytics/visits-{day}.jsonl"

    def _set_geo(cur):
        lines = (cur or "").splitlines()
        for i, ln in enumerate(lines):
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("_cid") == cid and not rec.get("geo"):
                rec["geo"] = city
                lines[i] = json.dumps(rec, ensure_ascii=False)
                return "\n".join(lines) + "\n"
        return None   # record not landed yet (or already patched) -> no write

    for _ in range(6):
        try:
            if selfserve.update_data(path, _set_geo, f"analytics: geo {city} for {cid[:8]}"):
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
