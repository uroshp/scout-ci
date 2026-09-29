"""Reachability of the structured sources from wherever this runs (WS1, 2026-09-28).

    python scripts/probe_sources.py

Read-only, keyless, $0. Prints one line per source with the HTTP status and a sample, and a summary.
Run from a GitHub Actions runner (probe-sources.yml) to learn whether sec.gov / data.sec.gov answer
from a datacenter IP; the plan's fallback (verify XBRL facts from the mini's poller) is decided by
the answer. Exit 0 always: the answer is the output, not the exit code.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout.sources import edgar, jobs, registry, wayback  # noqa: E402


def probe(label, fn):
    t0 = time.monotonic()
    try:
        out = fn()
        ms = int((time.monotonic() - t0) * 1000)
        print(f"OK   {label:32} {ms:5d} ms  {str(out)[:110]}")
        return True
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        print(f"FAIL {label:32} {ms:5d} ms  {type(e).__name__}: {str(e)[:120]}")
        return False


def main():
    results = {
        "data.sec.gov submissions": probe("data.sec.gov submissions", lambda: edgar.filings(1404655, forms=("8-K",), limit=1)[0]["url"]),
        "data.sec.gov xbrl": probe("data.sec.gov xbrl concept", lambda: edgar.fact(1108524, "RevenueFromContractWithCustomerExcludingAssessedTax", limit=1)["rows"][0]["line"]),
        "www.sec.gov archives": probe("www.sec.gov archives doc", lambda: _head("https://www.sec.gov/Archives/edgar/data/1404655/000119312526335148/hubs-20260803.htm")),
        "greenhouse": probe("greenhouse (anthropic)", lambda: len(jobs.postings("greenhouse", "anthropic"))),
        "ashby": probe("ashby (openai)", lambda: len(jobs.postings("ashby", "openai"))),
        "wayback cdx": probe("wayback cdx (claude.com/pricing)", lambda: wayback.snapshots("https://claude.com/pricing", since="2026", limit=2)[0]["date"]),
    }
    ok = sum(results.values())
    print(f"\n{ok}/{len(results)} reachable from this host. "
          + ("sec.gov answers here: EDGAR grounding can run in this environment." if results["data.sec.gov xbrl"] and results["www.sec.gov archives"]
             else "sec.gov does NOT answer here: XBRL verification must run from the mini (plan WS1 fallback)."))


def _head(url):
    from scout.grounding import _fetch_response
    r = _fetch_response(url)
    if r is None or r.status_code >= 400:
        raise RuntimeError(f"HTTP {getattr(r, 'status_code', '?')}")
    return f"HTTP {r.status_code}, {len(r.content)} bytes"


if __name__ == "__main__":
    main()
