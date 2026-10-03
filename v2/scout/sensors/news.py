"""The news channel: two keyless news indexes queried per entity, as RSS.

Google News RSS (`news.google.com/rss/search?q=<query>+when:<N>d`) returns up to about 100 items a
day for a specific query; its item links are redirects, so an item carries the outlet host (from
<source url=...>), the title and the date. Bing News RSS (`bing.com/news/search?q=<query>&format=rss`)
carries the publisher URL inside its click-through link (`url=` parameter), decoded here without a
request. Items are deduped on a normalized title, Bing's publisher URL winning.

Hosts: `classify._NEWS` outlets and the entity's own hosts are kept; `unknown` hosts are kept only
when the title names the entity and are flagged; forums and review sites are dropped (they are
sentiment_only and cannot anchor a fact). `news.google.com` is never a source URL.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, quote_plus, urlparse

from scout.sensors import feeds
from scout.sources import classify

GOOGLE = "https://news.google.com/rss/search?q={q}+when:{days}d&hl=en-US&gl=US&ceid=US:en"
BING = "https://www.bing.com/news/search?q={q}&format=rss"
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def google_url(query: str, days: int) -> str:
    return GOOGLE.format(q=quote_plus(query), days=max(1, min(int(days), 14)))


def bing_url(query: str) -> str:
    return BING.format(q=quote_plus(query))


def decode_bing(link: str) -> str | None:
    """The publisher URL inside a Bing News click-through link, or the link itself when it is not one."""
    try:
        p = urlparse(link or "")
    except Exception:
        return None
    if "bing.com" in (p.hostname or "") and "apiclick" in p.path:
        u = parse_qs(p.query).get("url", [None])[0]
        return u or None
    return link or None


def normalize_title(title: str) -> str:
    """Lowercase, punctuation out, trailing ' - Outlet' / ' | Outlet' suffix out, whitespace collapsed."""
    t = (title or "").lower()
    t = re.split(r"\s+[-|–—]\s+[^-|–—]{2,40}$", t)[0]
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def item_host(item: dict) -> str:
    link = item.get("publisher_url") or item.get("link") or ""
    h = feeds.host_of(link)
    if h.endswith("news.google.com") or not h:
        return item.get("source_host") or ""
    return h


def keep(item: dict, entity_names: list[str]) -> tuple[bool, str]:
    """(keep?, flag) by source class. Forums/review sites drop; unknown hosts keep only when the title
    names the entity, flagged `unknown_host`."""
    host = item_host(item)
    if not host:
        return False, "no_host"
    cls = classify.classify(f"https://{host}/", *entity_names)
    if cls in ("forum", "review_site"):
        return False, cls
    if cls == "unknown":
        toks = classify._company_tokens(*entity_names)
        title = (item.get("title") or "").lower()
        if not any(t in title for t in toks):
            return False, "unknown_host_no_name"
        return True, "unknown_host"
    return True, cls


def merge(google_items: list[dict], bing_items: list[dict]) -> list[dict]:
    """Union on normalized title; Bing's decoded publisher URL wins; Google's outlet host is kept."""
    by_title: dict[str, dict] = {}
    for it in bing_items:
        pub = decode_bing(it.get("link") or "")
        rec = dict(it, publisher_url=pub, channel="bing")
        if pub:
            rec["source_host"] = feeds.host_of(pub)
        by_title[normalize_title(it.get("title") or "")] = rec
    for it in google_items:
        key = normalize_title(it.get("title") or "")
        if key in by_title:
            if not by_title[key].get("published") and it.get("published"):
                by_title[key]["published"] = it["published"]
            continue
        by_title[key] = dict(it, publisher_url=None, channel="google")
    out = []
    for key, rec in by_title.items():
        if not key:
            continue
        rec["title_key"] = key
        out.append(rec)
    return out
