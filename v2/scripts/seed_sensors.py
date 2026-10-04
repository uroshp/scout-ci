"""Seed the sensor registries (Release 2): what code will watch for each company, for Uroš to review
before anything is written.

For every entity on the live cards (or the slugs given): the company hosts Scout has already cited
(collapsed from article permalinks to their section pages), the standard paths probed on the main
host (/news, /newsroom, /blog, /press, /pricing, /changelog, /release-notes, /docs/changelog, status.,
ir., /careers), feeds advertised in those pages' <head>, and the news queries (display name plus
focus terms). Every candidate page is probed with the hardened fetcher: kept when it is clean HTML or
PDF of 800+ characters; otherwise listed as blocked / thin-or-JavaScript / paywalled so the review
shows what cannot be sensed. Hosts the classifier calls a company host but that are not (9to5google
for Google) are listed for the review too.

  python scripts/seed_sensors.py [--slugs a,b] [--names "Google Cloud,Anthropic"] [--queries-file q.json]
                                 [--out seeding.json] [--html seeding.html] [--write]

Without --write nothing is stored: the JSON and the HTML are the review artifact. With --write, each
entity's registry goes to the private store under the current prefix (production root, or rehearsal/
when SCOUT_SELFSERVE_DATA_PREFIX=rehearsal). A --queries-file {entity_key: [queries]} overrides the
generated queries after the review. $0: no model calls.
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import sys
from collections import Counter
from datetime import date
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from scout import store  # noqa: E402
from scout._probe_reach import categorize  # noqa: E402
from scout.sensors import collect, feeds, pagediff, registry  # noqa: E402
from scout.sources import classify  # noqa: E402

# Official hosts that do not carry the company's name, and look-alikes that are not the company.
# The classifier's token match is loose on purpose for citations (aboutamazon.com counts for AWS);
# for WATCHING a host as the company's own voice it has to be exact or listed here.
OFFICIAL_HOSTS = {
    "aws": ["aboutamazon.com", "aws.amazon.com", "docs.aws.amazon.com"],
    "anthropic": ["claude.com", "status.claude.com", "platform.claude.com", "status.anthropic.com"],
    "google": ["blog.google", "workspace.google.com", "deepmind.google"],
    "google cloud": ["cloud.google.com", "googlecloudpresscorner.com", "status.cloud.google.com", "blog.google"],
    "microsoft teams": ["techcommunity.microsoft.com", "microsoft.com", "news.microsoft.com", "adoption.microsoft.com"],
    "slack": ["slack.com", "status.slack.com", "salesforce.com", "slack.engineering"],
    "openai": ["openai.com", "developers.openai.com", "status.openai.com", "deploymentsafety.openai.com"],
    "mistral": ["mistral.ai", "help.mistral.ai"],
    "perplexity": ["perplexity.ai", "docs.perplexity.ai", "research.perplexity.ai", "www.perplexity.ai"],
    "notion": ["notion.com", "notion.so", "notion-status.com", "status.notion.com"],
    "atlassian": ["atlassian.com", "status.atlassian.com"],
    "cursor": ["cursor.com", "cursor.sh", "anysphere.inc"],
    "cognition": ["cognition.ai", "devin.ai"],
    "salesforce": ["salesforce.com", "investor.salesforce.com"],
    "hubspot": ["hubspot.com", "ir.hubspot.com"],
}
NOT_COMPANY = {"salesforceben.com", "9to5google.com", "9to5mac.com", "macrumors.com", "windowscentral.com", "androidpolice.com"}
FEED_GUESSES = ("/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/news/rss.xml", "/news/feed", "/blog/rss.xml", "/blog/feed", "/blog/feed/")

STANDARD_PATHS = {
    "newsroom": ("/news", "/newsroom", "/news/", "/company/news", "/press-releases", "/announcements"),
    "blog": ("/blog", "/blog/", "/research", "/engineering"),
    "press": ("/press", "/media", "/company/press"),
    "pricing": ("/pricing", "/plans", "/pricing/", "/enterprise/pricing"),
    "releases": ("/changelog", "/release-notes", "/releases", "/docs/changelog", "/docs/release-notes", "/whats-new", "/updates"),
    "docs": ("/docs",),
    "careers": ("/careers", "/jobs"),
}
SUBDOMAINS = {"status": "status.{host}", "ir": "ir.{host}", "investors": "investor.{host}", "blog": "blog.{host}", "news": "news.{host}"}
MAX_CITED_PAGES = 6
_FOCUS_STOP = {"general", "and", "vs", "for", "the", "of", "in", "inside", "ai", "features"}


def _section_page(url: str) -> str:
    """An article permalink collapsed to its section page: /news/2026/10/x -> /news."""
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return f"{p.scheme}://{p.netloc}/"
    first = parts[0]
    if re.fullmatch(r"\d{4}|[a-f0-9]{8,}", first):
        return f"{p.scheme}://{p.netloc}/"
    return f"{p.scheme}://{p.netloc}/{first}"


BROAD_NAMES = {"google", "microsoft", "amazon", "aws", "apple", "meta", "alphabet", "ibm", "oracle"}


def _registrable(host: str) -> str:
    """developers.openai.com -> openai.com (the standard paths live on the registrable domain)."""
    bare = host[4:] if host.startswith("www.") else host
    labels = bare.split(".")
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac", "gov") and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else bare


def _main_host(hosts: Counter, name: str) -> str | None:
    """The company's primary host among the cited ones: the host whose labels match the most of the
    name's own tokens (cloud.google.com for "Google Cloud" beats blog.google); when only the
    registrable label matches (developers.openai.com for "OpenAI"), the registrable domain
    (openai.com) is the host the standard paths are probed on."""
    own = {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3}
    alias = classify._company_tokens(name) - own
    best, best_score = None, -1
    for h, n in hosts.most_common():
        bare = h[4:] if h.startswith("www.") else h
        labels = bare.split(".")
        score = 3 * len(own & set(labels)) + len(alias & set(labels))
        if score > best_score:
            best, best_score = bare, score
    if not best or best_score <= 0:
        return _registrable(best) if best else None
    reg = _registrable(best)
    extra = (own & set(best.split("."))) - set(reg.split("."))
    return best if extra else reg


# Product names worth their own news query, per entity (reviewed with the registries; a product entity
# such as "Microsoft Teams" or "Google Cloud" must not inherit its parent company's list).
PRODUCT_QUERIES = {
    "anthropic": ["Claude"], "openai": ["ChatGPT"], "google": ["Gemini", "DeepMind"], "mistral": ["Magistral"],
    "atlassian": ["Jira", "Confluence"], "aws": ["Bedrock"], "cognition": ["Devin"], "hubspot": ["Breeze"],
    "salesforce": ["Agentforce"], "cursor": ["Anysphere"], "perplexity": ["Comet"],
}


def _aliases(name: str) -> list[str]:
    return list(PRODUCT_QUERIES.get(name.lower(), []))


def _focus_phrase(card: dict, name: str) -> str | None:
    """One card's focus area as a query phrase (its own words, the company name and stop words removed)."""
    words = []
    for w in re.findall(r"[a-z][a-z0-9]{2,}", str(card.get("focus") or "").lower().replace("-", " ")):
        if w not in _FOCUS_STOP and w not in name.lower() and w not in words:
            words.append(w)
    return " ".join(words[:6]) or None


def _focus_phrases(cards: list[dict], name: str) -> list[str]:
    """One phrase PER CARD, never merged across cards (2026-10-03: a merged three-word phrase for OpenAI kept
    the sovereign-enterprise card's words and dropped the collaboration card's; the focus area is a scope of
    its own on every card and no cap may cut it)."""
    out = []
    for c in cards:
        fp = _focus_phrase(c, name)
        if fp and fp not in out:
            out.append(fp)
    return out


def _queries(name: str, cards: list[dict]) -> list[str]:
    """The display name (unless it is a broad word like Google: then only qualified forms), up to two
    product aliases, and the name with EACH card's focus phrase (one query per focused card, uncapped)."""
    q = []
    if name.lower() not in BROAD_NAMES:
        q.append(f'"{name}"')
    for a in _aliases(name)[:2]:
        q.append(f'"{a}"' if a.lower() not in BROAD_NAMES else f'"{name}" {a}')
    fps = _focus_phrases(cards, name)
    for fp in fps:
        q.append(f'"{name}" {fp}')
    if not fps and name.lower() in BROAD_NAMES:
        q.append(f'"{name}" AI')
    return list(dict.fromkeys(q))


def _is_company_page(host: str, name: str) -> tuple[bool, bool]:
    """(company host?, suspicious?) for WATCHING: an own-name token equal to a whole label, or a host
    listed in OFFICIAL_HOSTS; a loose match (salesforceben.com for Slack) is suspicious, not kept."""
    bare = host[4:] if host.startswith("www.") else host
    if bare in NOT_COMPANY or any(bare.endswith("." + x) for x in NOT_COMPANY):
        return False, True
    official = OFFICIAL_HOSTS.get(name.lower(), [])
    if bare in official or any(bare.endswith("." + o) for o in official):
        return True, False
    own = {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3}
    if own & set(bare.split(".")):
        return True, False
    if classify.is_company_host(host, name):
        return False, True
    return False, False


def plan_entity(key: str, name: str, cards: list[dict], probe=categorize, render: bool = True) -> dict:
    hosts: Counter = Counter()
    pages: Counter = Counter()
    suspicious = []
    for c in cards:
        for url in c.get("urls") or []:
            h = feeds.host_of(url)
            if not h:
                continue
            ok, sus = _is_company_page(h, name)
            if sus:
                suspicious.append(h)
                continue
            if ok:
                hosts[h] += 1
                pages[_section_page(url)] += 1
    main = _main_host(hosts, name)
    candidates: dict[str, str] = {}
    for url, _ in pages.most_common(MAX_CITED_PAGES):
        candidates.setdefault(url, "cited")
    probe_hosts = []
    if main:
        probe_hosts.append(main)
    for h in OFFICIAL_HOSTS.get(name.lower(), []):          # every official host, not only the main one
        if h not in probe_hosts and not h.startswith(("status.", "ir.", "investor.")):
            probe_hosts.append(h)
    for i, host in enumerate(probe_hosts[:4]):
        paths = STANDARD_PATHS if i == 0 else {k: v for k, v in STANDARD_PATHS.items() if k in ("newsroom", "blog", "pricing", "releases", "docs")}
        for kind, pths in paths.items():
            for pth in pths:
                candidates.setdefault(f"https://{host}{pth}", kind)
        if i == 0:
            for kind, tmpl in SUBDOMAINS.items():
                candidates.setdefault(f"https://{tmpl.format(host=host)}/", kind)
    for h in OFFICIAL_HOSTS.get(name.lower(), []):
        if h.startswith("status."):
            candidates.setdefault(f"https://{h}/", "status")
    sources, unreadable, feeds_found = [], [], {}
    seen_bare: set = set()
    seen_text: dict = {}
    import hashlib as _hl
    from scout.sensors import rendered as _rendered
    walled_hosts: set = set()
    for url, kind in candidates.items():
        bare = _bare(url)
        if bare in seen_bare:
            continue
        seen_bare.add(bare)
        host = feeds.host_of(url)
        if host in walled_hosts:
            unreadable.append({"url": url, "kind": kind, "why": "walled host (bot wall even for a browser)", "guessed": kind != "cited", "challenge": True})
            continue
        cat, detail = probe(url)
        read, html, why = None, None, f"{cat} {detail or ''}".strip()
        if cat in ("clean_html", "pdf_clean"):
            try:
                html = collect.fetch(url).get("text") or ""
            except Exception:
                html = ""
            if len(pagediff.blocks_from_html(html)) >= collect.THIN_BLOCKS:
                read = "plain"
            else:
                cat, why = "thin_or_js", f"plain read has {len(pagediff.blocks_from_html(html))} text blocks (a JavaScript shell)"
        if read is None and cat in ("blocked_403", "thin_or_js", "timeout", "connect_error") and "BlockedURLError" not in (detail or "") \
                and not (cat == "connect_error" and detail in ("ConnectError",)):
            rr = _rendered.fetch(url) if render else {"text": None, "challenge": False, "error": "renderer off"}
            if rr.get("challenge"):
                read, why = "challenge", f"bot wall even for a browser (plain: {why})"
                walled_hosts.add(host)
            elif rr.get("text") and len(pagediff.blocks_from_html(rr["text"])) >= collect.THIN_BLOCKS:
                read, html = "rendered", rr["text"]
            elif rr.get("status") == 404:
                read, why = None, "http_error 404"
            else:
                read, why = None, f"{why}; browser: {rr.get('error') or 'empty page'}"
        if read in ("plain", "rendered"):
            text_hash = _hl.sha256((html or "").encode("utf-8", "replace")).hexdigest()[:16]
            found_feeds = feeds.discover(html or "", url)
            if text_hash in seen_text:
                continue                                   # /blog and /blog/, or a path that redirects to a page already kept
            seen_text[text_hash] = url
            for f in found_feeds:
                feeds_found.setdefault(f, kind)
            sources.append({"url": url, "kind": kind, "feed": False, "added_by": "cited" if kind == "cited" else "seed",
                            "added_on": date.today().isoformat(), "cadence": "daily", "noise": 0, "read": read,
                            "probe": f"{read}: {len(pagediff.blocks_from_html(html or ''))} blocks", "_feeds": found_feeds})
        else:
            unreadable.append({"url": url, "kind": kind, "why": why, "guessed": kind != "cited",
                               "challenge": read == "challenge"})
    if main:
        for pth in FEED_GUESSES:              # feeds are often open where the pages are not (openai.com 403s the fetcher)
            feeds_found.setdefault(f"https://{main}{pth}", "feed")
    feed_seen: set = set()
    feed_sigs: set = set()
    for f, kind in feeds_found.items():
        if _bare(f) in feed_seen:
            continue
        feed_seen.add(_bare(f))
        try:
            items = feeds.parse(collect.fetch(f).get("text") or "", f)
        except Exception:
            items = []
        sig = tuple(sorted((i.get("link") or i.get("id") or i.get("title")) for i in items[:20]))
        if items and sig in feed_sigs:
            continue                                   # the same feed under a second URL
        feed_sigs.add(sig)
        if items:
            sources.append({"url": f, "kind": "feed", "feed": True, "added_by": "seed", "added_on": date.today().isoformat(),
                            "cadence": "daily", "noise": 0, "probe": f"feed with {len(items)} items"})
            # a page that advertises a working feed is read weekly: the feed carries its daily items
            for s_ in sources:
                if f in (s_.get("_feeds") or []):
                    s_["cadence"] = "weekly"
    for s_ in sources:
        s_.pop("_feeds", None)
    reg = {"entity": key, "name": name, "names": [name] + _aliases(name), "cards": [c["slug"] for c in cards],
           "main_host": main, "sources": sources, "news_queries": _queries(name, cards),
           "walled_hosts": sorted(walled_hosts), "seeded_at": date.today().isoformat(),
           "notes": ([f"classifier called {h} a company host; ignored" for h in sorted(set(suspicious))])}
    return {"registry": reg, "unreadable": unreadable, "cited_hosts": dict(hosts.most_common(12)), "walled_hosts": sorted(walled_hosts)}


def _bare(url: str) -> str:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host + p.path.rstrip("/")


def collect_cards(slugs: list[str] | None, archive: bool = False) -> dict:
    """{entity_key: {name, cards: [{slug, focus, urls, subject_keys}]}} from the live cards (or, with
    --archive, the retired cards in v2/archive: the rehearsal entities)."""
    from scout.display import list_battlecards
    out: dict = {}
    root = os.path.join(store.config.APP_ROOT, "archive") if archive else None
    names = sorted(d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "meta.json"))) if root else list_battlecards()
    for slug in names:
        if slugs and slug not in slugs:
            continue
        if root:
            meta = json.load(open(os.path.join(root, slug, "meta.json")))
            claims = json.load(open(os.path.join(root, slug, "claims.json")))
        else:
            meta = store.load_meta(slug) or {}
            claims = store.load_claims(slug)
        urls = [c.get("source_url") for c in claims if c.get("source_url")]
        urls += [co.get("source_url") for c in claims for co in (c.get("corroboration") or []) if co.get("source_url")]
        for e in registry.entities_for(meta):
            rec = out.setdefault(e["key"], {"name": e["name"], "cards": []})
            rec["cards"].append({"slug": slug, "focus": meta.get("focus"), "urls": urls,
                                 "subject_keys": [c.get("subject_key") for c in claims]})
    return out


def render_html(plans: dict) -> str:
    parts = ["<title>Sensor registries</title><style>body{font-family:Inter,system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1c1d16}"
             "h2{margin-top:28px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #e3e0d6;padding:5px 8px;text-align:left;vertical-align:top}"
             ".bad{color:#b0301c}.muted{color:#6a6a6a}code{font-size:12px}</style>",
             "<h1>What Scout would watch</h1><p class=muted>One registry per company. Pages kept are clean HTML or PDF of 800+ characters; the rest are listed as unreadable so the gap is visible. News queries run every morning on two indexes.</p>"]
    for key, pl in plans.items():
        reg, bad = pl["registry"], pl["unreadable"]
        parts.append(f"<h2>{_html.escape(reg['name'])} <span class=muted>({key}; cards: {', '.join(reg['cards'])})</span></h2>")
        parts.append(f"<p>Main host: <code>{_html.escape(str(reg.get('main_host')))}</code>. Cited company hosts: "
                     + ", ".join(f"<code>{_html.escape(h)}</code> ×{n}" for h, n in pl["cited_hosts"].items()) + "</p>")
        parts.append("<p>News queries: " + ", ".join(f"<code>{_html.escape(q)}</code>" for q in reg["news_queries"]) + "</p>")
        parts.append("<table><tr><th>kind</th><th>url</th><th>how it is read</th><th>cadence</th></tr>")
        for s_ in reg["sources"]:
            how = "feed" if s_["feed"] else (s_.get("read") or "plain")
            parts.append(f"<tr><td>{_html.escape(s_['kind'])}</td><td><a href='{_html.escape(s_['url'])}'>{_html.escape(s_['url'])}</a></td>"
                         f"<td>{_html.escape(how)} <span class=muted>({_html.escape(s_.get('probe') or '')})</span></td><td class=muted>{_html.escape(s_.get('cadence') or 'daily')}</td></tr>")
        challenge = [b for b in bad if b.get("challenge")]
        other_bad = [b for b in bad if not b.get("challenge") and (not b.get("guessed") or not (b["why"].startswith("http_error 404") or "BlockedURLError" in b["why"]))]
        for b in challenge:
            parts.append(f"<tr><td class=bad>{_html.escape(b['kind'])}</td><td class=bad>{_html.escape(b['url'])}</td><td class=bad>needs TinyFish (bot wall)</td><td></td></tr>")
        for b in other_bad:
            parts.append(f"<tr><td class=bad>{_html.escape(b['kind'])}</td><td class=bad>{_html.escape(b['url'])}</td><td class=bad>unreadable: {_html.escape(b['why'])}</td><td></td></tr>")
        parts.append("</table>")
        guessed = len(bad) - len(challenge) - len(other_bad)
        if guessed:
            parts.append(f"<p class=muted>{guessed} guessed standard path(s) do not exist on this host (not listed).</p>")
        for n in reg.get("notes") or []:
            parts.append(f"<p class=muted>{_html.escape(n)}</p>")
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", default="")
    ap.add_argument("--names", default="", help="limit to these display names (comma list)")
    ap.add_argument("--queries-file", default="")
    ap.add_argument("--out", default="seeding.json")
    ap.add_argument("--html", default="")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--archive", action="store_true", help="plan from v2/archive (the rehearsal cards) instead of the live cards")
    ap.add_argument("--no-render", action="store_true", help="skip the headless-browser tier")
    ap.add_argument("--cover", default="", help="coverage.json from sensor_coverage.py: add every uncovered host's cited section page as a third-party source")
    a = ap.parse_args()
    slugs = [s.strip() for s in a.slugs.split(",") if s.strip()] or None
    names = {n.strip().lower() for n in a.names.split(",") if n.strip()}
    cards = collect_cards(slugs, archive=a.archive)
    overrides = json.load(open(a.queries_file)) if a.queries_file else {}
    plans = {}
    for key, rec in cards.items():
        if names and rec["name"].lower() not in names:
            continue
        print(f"[seed] {rec['name']} ({key}) from {len(rec['cards'])} card(s) ...", flush=True)
        pl = plan_entity(key, rec["name"], rec["cards"], render=not a.no_render)
        if key in overrides:
            pl["registry"]["news_queries"] = list(overrides[key])
        plans[key] = pl
        print(f"       {len(pl['registry']['sources'])} source(s) kept, {len(pl['unreadable'])} unreadable, queries {pl['registry']['news_queries']}")
    if a.cover:
        # the gate: a host Scout cited that no index carries is watched directly (its section page)
        from urllib.parse import urlparse
        for r in json.load(open(a.cover)):
            if r.get("channel") != "uncovered" or not r.get("example"):
                continue
            pth = urlparse(r["example"])
            parts = [x for x in pth.path.split("/") if x]
            sec = f"{pth.scheme}://{pth.netloc}/" + (parts[0] if parts and not parts[0][:4].isdigit() else "")
            for slug in r.get("cards") or []:
                key = slug.split("__vs__")[1].split("__")[0]        # the card's competitor, slugified = the entity key
                if key not in plans:
                    continue
                reg = plans[key]["registry"]
                if any(_bare(s_["url"]) == _bare(sec) for s_ in reg["sources"]):
                    continue
                cat, detail = categorize(sec)
                ok = cat in ("clean_html", "pdf_clean")
                reg["sources"].append({"url": sec, "kind": "third_party", "feed": False, "added_by": "coverage", "added_on": date.today().isoformat(),
                                       "cadence": "daily", "noise": 0, "read": "plain", "probe": f"{'plain' if ok else cat}: cited host no index carries"})
                print(f"[seed] coverage: {r['host']} -> {reg['name']} ({sec}, {cat})")
    json.dump(plans, open(a.out, "w"), indent=1, ensure_ascii=False)
    print(f"[seed] wrote {a.out}")
    if a.html:
        open(a.html, "w").write(render_html(plans))
        print(f"[seed] wrote {a.html}")
    if a.write:
        for key, pl in plans.items():
            reg = dict(pl["registry"])
            for s_ in reg["sources"]:
                s_.pop("probe", None)
            reg["challenge"] = [{"url": b["url"], "kind": b["kind"]} for b in pl["unreadable"] if b.get("challenge")]
            registry.save(key, reg, f"sensors: seed {key}")
            print(f"[seed] saved registry {key} ({len(reg['sources'])} sources)")


if __name__ == "__main__":
    main()
