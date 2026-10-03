"""RSS 2.0 and Atom feeds, parsed with the standard library; feed discovery from a page's <head>.

A feed item is {"id", "title", "link", "published" (YYYY-MM-DD or None), "summary", "source_host"}.
`id` is the guid/atom id, else the link, else the title. Google News items carry the outlet in
<source url=...>; that host is kept as `source_host` because the item link is a redirect.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_ATOM = "{http://www.w3.org/2005/Atom}"
_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def _text(el) -> str:
    if el is None:
        return ""
    t = "".join(el.itertext()) if len(el) else (el.text or "")
    return _WS.sub(" ", _TAGS.sub(" ", t)).strip()


def _date(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:25], fmt).date().isoformat()
        except Exception:
            continue
    return None


def host_of(url: str | None) -> str:
    try:
        h = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def parse(xml_text: str, feed_url: str = "") -> list[dict]:
    """Items of an RSS 2.0 or Atom document; [] when it is neither."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    items = []
    if root.tag.endswith("rss") or root.tag.endswith("RDF") or root.find("channel") is not None:
        channel = root.find("channel")
        for it in (channel.findall("item") if channel is not None else root.iter("item")):
            link = _text(it.find("link")) or (it.find("link").get("href") if it.find("link") is not None else "")
            src = it.find("source")
            items.append({
                "id": _text(it.find("guid")) or link or _text(it.find("title")),
                "title": _text(it.find("title")),
                "link": link,
                "published": _date(_text(it.find("pubDate")) or _text(it.find("{http://purl.org/dc/elements/1.1/}date"))),
                "summary": _text(it.find("description"))[:600],
                "source_host": host_of(src.get("url")) if src is not None and src.get("url") else host_of(link),
            })
    elif root.tag == _ATOM + "feed" or root.tag.endswith("feed"):
        for en in root.findall(_ATOM + "entry") or root.findall("entry"):
            link = ""
            for l in en.findall(_ATOM + "link") or en.findall("link"):
                if (l.get("rel") or "alternate") == "alternate" and l.get("href"):
                    link = l.get("href")
                    break
            items.append({
                "id": _text(en.find(_ATOM + "id")) or link or _text(en.find(_ATOM + "title")),
                "title": _text(en.find(_ATOM + "title")) or _text(en.find("title")),
                "link": urljoin(feed_url, link) if link else "",
                "published": _date(_text(en.find(_ATOM + "published")) or _text(en.find(_ATOM + "updated"))),
                "summary": (_text(en.find(_ATOM + "summary")) or _text(en.find(_ATOM + "content")))[:600],
                "source_host": host_of(link),
            })
    return [i for i in items if i["title"] or i["link"]]


def discover(html: str, page_url: str) -> list[str]:
    """Feed URLs a page advertises in its <head> (<link rel=alternate type=application/rss+xml|atom+xml>)."""
    out = []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return out
    for l in soup.find_all("link"):
        rel = " ".join(l.get("rel") or []).lower() if isinstance(l.get("rel"), list) else str(l.get("rel") or "").lower()
        typ = str(l.get("type") or "").lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ) and l.get("href"):
            out.append(urljoin(page_url, l["href"]))
    return list(dict.fromkeys(out))
