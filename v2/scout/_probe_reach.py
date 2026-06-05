"""Throwaway reachability probe: can the (hardened) grounding fetcher READ the
sources the v1 samples cite? Pure HTTP, no API. Categorizes every cited URL.

Run:  python -m scout._probe_reach
"""
import glob
import os
import re
from collections import Counter
from urllib.parse import urlparse

import httpx

from scout import config
from scout.grounding import _fetch_response, _is_pdf, _pdf_to_text, _html_to_text, _normalize

# v1's committed samples now live in ../v1/reports (sibling of the v2/ app root).
_V1_REPORTS = os.path.join(os.path.dirname(config.APP_ROOT), "v1", "reports")

PAYWALL_KW = [
    "subscribe to read", "subscribe now", "create a free account",
    "to continue reading", "this content is for subscribers",
    "already a subscriber", "register to read", "sign in to read",
]


def collect_urls():
    urls = {}
    for path in sorted(glob.glob(os.path.join(_V1_REPORTS, "*.md"))):
        text = open(path).read()
        for m in re.finditer(r"\]\((https?://[^)\s]+)\)", text):
            urls.setdefault(m.group(1), set()).add(path.split("/")[-1])
    return urls


def categorize(url):
    try:
        r = _fetch_response(url)
    except httpx.TimeoutException:
        return "timeout", None
    except Exception as e:
        return "connect_error", type(e).__name__

    status = r.status_code
    if status in (401, 403, 429, 451):
        return "blocked_403", str(status)
    if status >= 400:
        return "http_error", str(status)

    if _is_pdf(r):
        try:
            n = len(_normalize(_pdf_to_text(r.content)))
        except Exception as e:
            return "pdf_parse_error", type(e).__name__
        return ("pdf_clean" if n >= 800 else "pdf_thin"), f"{n} chars"

    text = _html_to_text(r.text)
    n = len(_normalize(text))
    if any(kw in text.lower() for kw in PAYWALL_KW):
        return "paywall", f"{n} chars + marker"
    if n < 800:
        return "thin_or_js", f"{n} chars"
    return "clean_html", f"{n} chars"


def main():
    urls = collect_urls()
    print(f"Collected {len(urls)} unique cited URLs\n")
    cats = Counter()
    rows = []
    for url in urls:
        cat, detail = categorize(url)
        cats[cat] += 1
        rows.append((cat, urlparse(url).netloc.replace("www.", ""), detail))

    for cat, dom, detail in sorted(rows):
        print(f"  {cat:15} {dom:26} {detail or ''}")

    total = sum(cats.values())
    print("\n=== BREAKDOWN ===")
    for cat, n in cats.most_common():
        print(f"  {cat:15} {n:3}  ({100*n/total:.0f}%)")
    readable = cats["clean_html"] + cats["pdf_clean"]
    print(f"\nReadable (clean_html + pdf_clean): {readable}/{total} ({100*readable/total:.0f}%)")


if __name__ == "__main__":
    main()
