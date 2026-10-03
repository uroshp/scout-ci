"""Coverage audit (Release 2 gate, Uroš 2026-10-03: "if we can't monitor all sources used from day 0
until today, this doesn't go into production").

Every host ever cited on the live cards (anchors and corroboration), with how the sensors would
reach it:
  registry   a company host with a readable registry source on it (plain, rendered or a feed)
  index      a news or other host that at least one news index carries (`site:<host> "<name>"` on
             Google News and Bing News returns items in the last 30 days)
  walled     a company host whose pages defeat both the plain fetcher and the headless browser
             (the TinyFish tier, or news-only until then)
  uncovered  nothing above reaches it

An index carrying an outlet proves the outlet is reachable, not that every story is; that is what the
shadow's Level A misses measure afterwards. $0, no model.

  python scripts/sensor_coverage.py --seeding seeding.json [--out coverage.json] [--html coverage.html]
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from scout import store  # noqa: E402
from scout.display import list_battlecards  # noqa: E402
from scout.sensors import collect, feeds, news  # noqa: E402
from scout.sources import classify  # noqa: E402


def cited_hosts() -> dict:
    """{host: {"n": citations, "cards": set, "names": set(company names on those cards), "urls": [..]}}"""
    out: dict = defaultdict(lambda: {"n": 0, "cards": set(), "names": set(), "urls": []})
    for slug in list_battlecards():
        meta = store.load_meta(slug) or {}
        names = {n for n in (meta.get("competitor"), meta.get("my_company")) if n}
        for c in store.load_claims(slug):
            urls = ([c.get("source_url")] if c.get("source_url") else []) + \
                   [co.get("source_url") for co in (c.get("corroboration") or []) if co.get("source_url")]
            for u in urls:
                h = feeds.host_of(u)
                if not h:
                    continue
                out[h]["n"] += 1
                out[h]["cards"].add(slug)
                out[h]["names"] |= names
                if len(out[h]["urls"]) < 3:
                    out[h]["urls"].append(u)
    return out


def registry_hosts(seeding: dict) -> tuple[set, set]:
    """(hosts with a readable source, hosts with only walled pages) from the seeding plan."""
    ok, walled = set(), set()
    for pl in seeding.values():
        for s in pl["registry"]["sources"]:
            ok.add(feeds.host_of(s["url"]))
        for b in pl["unreadable"]:
            if b.get("challenge"):
                walled.add(feeds.host_of(b["url"]))
    return ok, walled - ok


def index_covers(host: str, names: set) -> dict:
    """Query both indexes for the host; the first name that returns items wins."""
    res = {"google": 0, "bing": 0, "query": None}
    for name in sorted(names)[:2] or [""]:
        q = f'site:{host} "{name}"' if name else f"site:{host}"
        g = collect.fetch(news.google_url(q, 14))
        gi = feeds.parse(g.get("text") or "", "") if g.get("text") else []
        b = collect.fetch(news.bing_url(q))
        bi = feeds.parse(b.get("text") or "", "") if b.get("text") else []
        res = {"google": len(gi), "bing": len(bi), "query": q}
        if gi or bi:
            return res
    # a quiet host: try the bare site query once (any story in the window proves the outlet is indexed)
    g = collect.fetch(news.google_url(f"site:{host}", 14))
    gi = feeds.parse(g.get("text") or "", "") if g.get("text") else []
    if gi:
        res.update({"google": len(gi), "query": f"site:{host}"})
    return res


def audit(seeding_path: str) -> list[dict]:
    seeding = json.load(open(seeding_path))
    reg_ok, reg_walled = registry_hosts(seeding)
    rows = []
    hosts = cited_hosts()
    for host, info in sorted(hosts.items(), key=lambda kv: -kv[1]["n"]):
        bare = host[4:] if host.startswith("www.") else host
        names = info["names"]
        is_company = any(classify.is_company_host(host, n) for n in names)
        cls = classify.classify(f"https://{host}/", *names)
        row = {"host": bare, "citations": info["n"], "cards": sorted(info["cards"]), "class": cls,
               "company": is_company, "example": info["urls"][0] if info["urls"] else None}
        if bare in reg_ok or any(bare.endswith("." + h) or h.endswith("." + bare) for h in reg_ok):
            row["channel"] = "registry"
        elif bare in reg_walled:
            row["channel"] = "walled"
        else:
            r = index_covers(bare, names)
            row.update(r)
            row["channel"] = "index" if (r["google"] or r["bing"]) else "uncovered"
            if row["channel"] == "uncovered" and is_company:
                row["channel"] = "walled" if bare in reg_walled else "uncovered"
        rows.append(row)
        print(f"[coverage] {bare:40s} {row['channel']:10s} citations={info['n']:3d} class={cls}"
              + (f" google={row.get('google')} bing={row.get('bing')}" if "google" in row else ""), flush=True)
    return rows


def render_html(rows: list[dict]) -> str:
    c = Counter(r["channel"] for r in rows)
    tot = len(rows)
    cit = Counter()
    for r in rows:
        cit[r["channel"]] += r["citations"]
    def esc(x): return _html.escape(str(x))
    head = (f"<p><strong>{tot} hosts cited on the live cards since day 0.</strong> Reachable through the registry: {c['registry']} "
            f"({cit['registry']} citations); through a news index: {c['index']} ({cit['index']} citations); walled (needs the TinyFish tier): "
            f"{c['walled']} ({cit['walled']}); uncovered: {c['uncovered']} ({cit['uncovered']}).</p>")
    body = "".join(
        f"<tr class='{esc(r['channel'])}'><td><a href='{esc(r.get('example') or '#')}' target='_blank' rel='noopener'>{esc(r['host'])}</a></td>"
        f"<td>{r['citations']}</td><td>{esc(r['class'])}</td><td><b>{esc(r['channel'])}</b></td>"
        f"<td class='mut'>{('google ' + str(r.get('google')) + ' · bing ' + str(r.get('bing'))) if 'google' in r else ''}</td>"
        f"<td class='mut'>{esc(', '.join(x.split('__vs__')[1].split('__')[0] for x in r['cards']))}</td></tr>" for r in rows)
    return ("<section id='coverage'><h2>Coverage audit: every host ever cited</h2>" + head +
            "<div class='tbl'><table><thead><tr><th>host</th><th>citations</th><th>kind</th><th>reached through</th><th>index items (14 days)</th><th>cards</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div></section>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeding", required=True)
    ap.add_argument("--out", default="coverage.json")
    ap.add_argument("--html", default="")
    a = ap.parse_args()
    rows = audit(a.seeding)
    json.dump(rows, open(a.out, "w"), indent=1)
    if a.html:
        open(a.html, "w").write(render_html(rows))
    c = Counter(r["channel"] for r in rows)
    print(f"[coverage] {len(rows)} hosts: {dict(c)}")


if __name__ == "__main__":
    main()
