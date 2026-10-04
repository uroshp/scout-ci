"""The daily sensor pass (code only, $0): once per monitor run, before the card loop, for every
entity on a due card. Pages are fetched with grounding's hardened fetcher (SSRF guard, UA rotation,
20 s timeout) plus a conditional GET (ETag / Last-Modified: most pages answer 304) and one second of
spacing per host; feeds are parsed; the two news indexes are queried. Everything new becomes a
finding (events.py). A page that fails is recorded and the others still run. When an entity's news
channel fails or the pass fails wholesale, the entity is `unavailable` and its cards take today's
model triage (the monitor decides; this module only reports).

Summary per entity: {"pages_checked", "pages_changed", "pages_failed", "feed_items", "news_hits",
"findings", "errors": [str], "news_failed": bool, "unavailable": bool, "no_registry": bool}.
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime

import httpx

from scout import grounding
from scout.sensors import events, feeds, news, pagediff, registry
from scout.sources import classify

HOST_SPACING_S = 1.0
NEWS_PER_QUERY = 30          # the newest items per query; a broad name cannot crowd the screen
_LAST: dict[str, float] = {}


def _space(host: str) -> None:
    last = _LAST.get(host)
    if last is not None:
        wait = HOST_SPACING_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _LAST[host] = time.monotonic()


def fetch(url: str, etag: str | None = None, last_modified: str | None = None) -> dict:
    """{"status": int|None, "text": str|None, "etag", "last_modified", "error": str|None,
    "unchanged": bool}. Uses grounding's SSRF-guarded GET with its UA rotation; 304 -> unchanged."""
    host = feeds.host_of(url)
    _space(host)
    last_err, resp = None, None
    for ua in grounding._uas_for(url):
        headers = {**grounding._BASE_HEADERS, "User-Agent": ua}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            resp = grounding._safe_get(url, headers, grounding.config.GROUNDING_TIMEOUT_S)
        except grounding.BlockedURLError as e:
            return {"status": None, "text": None, "etag": etag, "last_modified": last_modified, "error": f"blocked: {e}", "unchanged": False}
        except (httpx.HTTPError, OSError) as e:
            last_err = f"{type(e).__name__}"
            continue
        if resp is not None and resp.status_code not in (401, 403, 429):
            break
    if resp is None:
        return {"status": None, "text": None, "etag": etag, "last_modified": last_modified, "error": last_err or "no response", "unchanged": False}
    if resp.status_code == 304:
        return {"status": 304, "text": None, "etag": etag, "last_modified": last_modified, "error": None, "unchanged": True}
    if resp.status_code >= 400:
        return {"status": resp.status_code, "text": None, "etag": etag, "last_modified": last_modified, "error": f"HTTP {resp.status_code}", "unchanged": False}
    return {"status": resp.status_code, "text": resp.text, "etag": resp.headers.get("etag") or etag,
            "last_modified": resp.headers.get("last-modified") or last_modified, "error": None, "unchanged": False}


THIN_BLOCKS = 3


def read_page(url: str, pst: dict, src: dict) -> tuple[dict, list[str], str]:
    """The three-tier read: plain httpx (conditional GET), then the headless browser when the plain
    read is blocked or a JavaScript shell, then `challenge` when the browser hits a bot wall.
    Returns (response-like dict, blocks, tier) with tier in plain | rendered | challenge | failed |
    unchanged. The tier that worked is remembered on the source so tomorrow goes straight to it."""
    from scout import config as _cfg
    mode = src.get("read") or "auto"
    r = None
    if mode != "rendered":
        r = fetch(url, pst.get("etag"), pst.get("last_modified"))
        if r["unchanged"]:
            return r, [], "unchanged"
        if not r["error"]:
            blocks = pagediff.blocks_from_html(r["text"] or "")
            if len(blocks) >= THIN_BLOCKS or mode == "plain":
                return r, blocks, "plain"
            plain_reason = f"thin or JavaScript-only ({len(blocks)} blocks)"
        else:
            plain_reason = r["error"]
        if not (_cfg.SENSOR_RENDERED and (r["status"] in (401, 403, 429, None) or not r["error"])):
            return r, [], "failed"
    else:
        plain_reason = "read mode: rendered"
    from scout.sensors import rendered
    rr = rendered.fetch(url)
    if rr.get("challenge"):
        src["read"] = "challenge"
        return {"status": rr["status"], "text": None, "etag": pst.get("etag"), "last_modified": pst.get("last_modified"),
                "error": f"bot wall (plain: {plain_reason})", "unchanged": False}, [], "challenge"
    if rr.get("error") or not rr.get("text"):
        return {"status": rr.get("status"), "text": None, "etag": pst.get("etag"), "last_modified": pst.get("last_modified"),
                "error": f"{rr.get('error') or 'empty'} (plain: {plain_reason})", "unchanged": False}, [], "failed"
    blocks = pagediff.blocks_from_html(rr["text"])
    if len(blocks) < THIN_BLOCKS:
        return {"status": rr.get("status"), "text": None, "etag": None, "last_modified": None,
                "error": f"rendered but empty ({len(blocks)} blocks; plain: {plain_reason})", "unchanged": False}, [], "failed"
    src["read"] = "rendered"
    return {"status": rr.get("status"), "text": rr["text"], "etag": None, "last_modified": None, "error": None, "unchanged": False}, blocks, "rendered"


def _page_findings(entity: str, src: dict, state: dict, today: str) -> tuple[list[dict], str]:
    """Fetch + diff one page source. Returns (findings, status) with status in
    first | unchanged | changed | quiet | failed | challenge."""
    url = src["url"]
    pst = state["pages"].setdefault(url, {})
    r, blocks, tier = read_page(url, pst, src)
    pst["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    pst["tier"] = tier
    if tier in ("failed", "challenge"):
        pst["status"] = r["error"]
        return [], tier
    if tier == "unchanged":
        pst["status"] = "ok"
        return [], "unchanged"
    pst["etag"], pst["last_modified"], pst["status"] = r["etag"], r["last_modified"], "ok"
    d = pagediff.diff({"hashes": pst.get("hashes"), "shapes": pst.get("shapes")}, blocks)
    pst["hashes"], pst["shapes"] = d["state"]["hashes"], d["state"]["shapes"]
    pst["blocks_n"] = len(blocks)
    if d["first"]:
        return [], "first"
    out = []
    kind = src.get("kind") or "page"
    cls = classify.classify(url, *(state.get("names") or []))
    if d["redesign"]:
        out.append(events.make(entity, "redesign", url, f"{kind} page redesigned ({int(d['ratio'] * 100)}% of blocks changed)",
                               " ".join(blocks[:6]), page_kind=kind, source_class=cls, key=f"{url}|{today}"))
        return out, "changed"
    for vc in d["value_changes"]:
        out.append(events.make(entity, "value_change", url, f"{kind} page: a figure changed",
                               f"BEFORE: {vc['old']}\nAFTER: {vc['new']}", page_kind=kind, source_class=cls,
                               key=f"{url}|{pagediff.h(pagediff.shape(vc['new']))}"))
    if d["new"]:
        text = "\n".join(d["new"][:12])
        out.append(events.make(entity, "page_change", url, f"{kind} page: {len(d['new'])} new block(s)", text,
                               page_kind=kind, source_class=cls, key=f"{url}|{pagediff.h(text)}"))
    return out, ("changed" if out else "quiet")


def _feed_findings(entity: str, src: dict, state: dict, today: str) -> tuple[list[dict], str]:
    url = src["url"]
    pst = state["pages"].setdefault(url, {})
    r = fetch(url, pst.get("etag"), pst.get("last_modified"))
    pst["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    if r["error"]:
        pst["status"] = r["error"]
        return [], "failed"
    pst["etag"], pst["last_modified"], pst["status"] = r["etag"], r["last_modified"], "ok"
    if r["unchanged"]:
        return [], "unchanged"
    items = feeds.parse(r["text"] or "", url)
    if not items:
        pst["status"] = "not a feed"
        return [], "failed"
    first = not pst.get("baselined")
    out = []
    seen = state["seen"]
    for it in items:
        # keyed on the ITEM, not the feed URL: the same item on two feed URLs (news/rss.xml and
        # blog/rss.xml, feed.rss and feed.atom) is one finding
        key = f"feed|{it.get('link') or it.get('id') or it.get('title')}"
        if key in seen:
            continue
        seen[key] = today
        if first:
            continue
        link = it.get("link") or url
        out.append(events.make(entity, "feed_item", link, it.get("title") or "", it.get("summary") or "",
                               published=it.get("published"), page_kind=src.get("kind") or "feed",
                               source_class=classify.classify(link, *(state.get("names") or [])), key=key))
    pst["baselined"] = True
    return out, ("first" if first else ("changed" if out else "quiet"))


def _news_findings(entity: str, queries: list[str], days: int, state: dict, today: str, names: list[str]) -> tuple[list[dict], bool, list[str]]:
    """(findings, failed_wholesale, errors). The channel fails wholesale when EVERY query failed on
    BOTH indexes; one index answering is enough to call the channel up."""
    seen = state["seen"]
    out, errors, answered = [], [], 0
    first = not state.get("news_baselined")
    for q in queries:
        g = fetch(news.google_url(q, days))
        b = fetch(news.bing_url(q))
        g_items = feeds.parse(g["text"] or "", "") if g["text"] else []
        b_items = feeds.parse(b["text"] or "", "") if b["text"] else []
        if g["error"] and b["error"]:
            errors.append(f"news {q!r}: google {g['error']}; bing {b['error']}")
            continue
        answered += 1
        merged = news.merge(g_items, b_items)
        merged.sort(key=lambda it: it.get("published") or "", reverse=True)
        for it in merged[:NEWS_PER_QUERY]:
            key = f"news|{it['title_key']}"
            if key in seen:
                continue
            seen[key] = today
            if first:
                continue
            ok, flag = news.keep(it, names)
            if not ok:
                continue
            url = it.get("publisher_url") or (f"https://{it.get('source_host')}/" if it.get("source_host") else "")
            if not url or "news.google.com" in url:
                url = f"https://{it.get('source_host')}/" if it.get("source_host") else ""
            if not url:
                continue
            out.append(events.make(entity, "news", url, it.get("title") or "", it.get("summary") or "",
                                   published=it.get("published"), source_host=it.get("source_host"),
                                   source_class=classify.classify(url, *names),
                                   flag="unknown_host" if flag.startswith("unknown") else None,
                                   page_kind="news", key=key))
    if answered:
        state["news_baselined"] = True
    return out, (answered == 0 and bool(queries)), errors


def run_pass(entities: dict, *, write: bool, today: str | None = None, days_by_entity: dict | None = None) -> dict:
    """entities: {key: {"name": str, "names": [str], "cards": [slug]}}. Returns {key: summary}."""
    today = today or date.today().isoformat()
    summary = {}
    for key, ent in entities.items():
        s = {"pages_checked": 0, "pages_changed": 0, "pages_failed": 0, "feed_items": 0, "news_hits": 0,
             "findings": 0, "errors": [], "news_failed": False, "unavailable": False, "no_registry": False}
        summary[key] = s
        reg = registry.load(key)
        if not reg:
            s["no_registry"] = True
            s["unavailable"] = True
            continue
        try:
            state = registry.load_state(key)
            state["names"] = list(dict.fromkeys([ent.get("name") or reg.get("name") or key] + list(reg.get("names") or [])))
            found: list[dict] = []
            for src in registry.active_sources(reg, today):
                try:
                    if src.get("feed"):
                        f, status = _feed_findings(key, src, state, today)
                        s["feed_items"] += len(f)
                    else:
                        f, status = _page_findings(key, src, state, today)
                        s["pages_checked"] += 1
                        if status == "changed":
                            s["pages_changed"] += 1
                    if status in ("failed", "challenge"):
                        s["pages_failed"] += 1
                        s["errors"].append(f"{src.get('kind')} {src['url']}: {state['pages'].get(src['url'], {}).get('status')}")
                    found += f
                    src["last_ok"] = datetime.now().isoformat(timespec="seconds") if status not in ("failed", "challenge") else src.get("last_ok")
                    src["last_status"] = status
                except Exception as e:                        # one source must never take the pass down
                    s["pages_failed"] += 1
                    s["errors"].append(f"{src.get('url')}: {type(e).__name__}: {e}")
            nf, news_failed, nerr = _news_findings(key, list(reg.get("news_queries") or []),
                                                   int((days_by_entity or {}).get(key) or 2), state, today, state["names"])
            found += nf
            if not state.get("baselined_on") and (state.get("news_baselined") or any(p.get("baselined") for p in state.get("pages", {}).values())):
                # sensors can only be measured on events from the first pass on; the seen set carries
                # the day each item was first read, so an entity baselined before this field existed
                # still gets its true first day
                state["baselined_on"] = min(list((state.get("seen") or {}).values()) + [today])
            s["baselined_on"] = state.get("baselined_on")
            s["news_hits"] = len(nf)
            s["news_failed"] = news_failed
            s["errors"] += nerr
            # the news channel carries most alerts: when it is down, the entity's cards take the
            # model triage path today (the monitor decides on this flag)
            s["unavailable"] = news_failed
            s["findings"] = len(found)
            if write:
                events.append(key, found)
                registry.save_state(key, state)
                registry.save(key, reg, f"sensors: pass {today} {key}")
        except Exception as e:
            s["errors"].append(f"pass: {type(e).__name__}: {e}")
            s["unavailable"] = True
            print(f"[sensors] pass FAILED for {key} ({type(e).__name__}: {e})", file=sys.stderr)
    try:
        from scout.sensors import rendered
        rendered.close()
    except Exception:
        pass
    return summary
