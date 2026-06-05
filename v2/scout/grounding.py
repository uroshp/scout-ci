"""Deterministic grounding check (claim-object.md §4) — the anti-fabrication backstop.

==============================================================================
PROVENANCE — why the model cannot influence the checked bytes (read this first)
==============================================================================
The entire guarantee is: the text we match the excerpt against is the REAL page,
fetched independently, never authored or paraphrased by any model.

We proved (probe, build step 5) that the SDK's `WebFetch` is MODEL-MEDIATED: its
tool input carries a model-written `prompt`, and its result is a model-written
*summary* that demonstrably altered the page wording. So WebFetch results are
unusable for grounding. Instead, the bytes here come from ONE place only:

    response = httpx.get(url)              # our own HTTP socket
    text = _extract_text(response)         # HTML->text (bs4) or PDF->text (pypdf)

There is NO model in that path. As a structural proof, this module imports only
httpx / BeautifulSoup / pypdf / rapidfuzz / stdlib — it imports NOTHING from
`anthropic` or `claude_agent_sdk`, so there is no model object it could route
bytes through. (CI asserts this: no 'anthropic'/'claude_agent_sdk' in this file.)

What this check does NOT do: judge whether the excerpt SUPPORTS the claim — that
is the verifier model's job. Grounding only proves the excerpt is on the page.
Likewise, anchor SUBSTITUTION (claim-object.md §2.2) is a verifier decision made
only when the verifier judged the sources AGREE; grounding just records that a
substitution happened (`substituted`) so #7 can audit it.
"""
import io
import re
import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import date
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from rapidfuzz import fuzz

from scout import config

# Two User-Agents, tried in order, because the block causes differ:
#  - a real browser UA gets past naive UA filters;
#  - a descriptive contact UA satisfies SEC's fair-access policy (sec.gov 403s
#    requests without a declared contact) and sites that want an honest crawler ID.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_CONTACT_UA = f"ScoutGrounding/0.2 (+https://github.com/uroshp/ci-agent; contact: {config.GROUNDING_CONTACT})"
_UAS = [_BROWSER_UA, _CONTACT_UA]

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# --- SSRF guard --------------------------------------------------------------
# The URL fetched here is chosen by the model (web_search citations / the agent's fetch tool),
# and a prompt-injected input could try to steer it inward. We fetch from a GitHub Action, so
# treat every fetch as untrusted: allow only http(s) to PUBLIC hosts, and re-validate on EVERY
# redirect hop (a single 302 to http://169.254.169.254 or http://localhost is the classic bypass,
# which follow_redirects=True would walk into blindly).
class BlockedURLError(Exception):
    """Raised when a URL is not safe to fetch (bad scheme or non-public host)."""


def _host_is_public(host: str) -> bool:
    """True only if EVERY resolved address for `host` is a normal public IP — reject loopback,
    private, link-local (incl. cloud metadata 169.254.169.254), reserved, multicast."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _assert_fetchable(url: str) -> None:
    """Raise BlockedURLError unless `url` is http(s) to a resolvable public host."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise BlockedURLError(f"blocked scheme: {p.scheme or '(none)'}")
    if not p.hostname or not _host_is_public(p.hostname):
        raise BlockedURLError(f"blocked non-public host: {p.hostname}")


def _safe_get(url: str, headers: dict, timeout: float, max_redirects: int = 6) -> httpx.Response:
    """httpx.get with SSRF protection: validate the initial URL and each redirect hop ourselves
    (follow_redirects=False) so a redirect can't smuggle us to an internal address."""
    resp = None
    for _ in range(max_redirects):
        _assert_fetchable(url)
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=False)
        if resp.is_redirect and resp.headers.get("location"):
            url = str(httpx.URL(url).join(resp.headers["location"]))
            continue
        return resp
    return resp  # too many redirects — hand back the last response

_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", "​": "", "﻿": "",
})


def _normalize(s: str) -> str:
    s = s.translate(_TRANSLATE).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _html_to_text(html: str) -> str:
    """All visible text — NOT main-content extraction. A cited figure often lives
    in a table/footnote/sidebar that aggressive extraction would drop."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(separator=" ")


def _pdf_to_text(content: bytes) -> str:
    """Tier 1A (filings, transcripts) is largely PDF — grounding must read it."""
    reader = PdfReader(io.BytesIO(content))
    return " ".join((page.extract_text() or "") for page in reader.pages)


def _is_pdf(resp: httpx.Response) -> bool:
    ct = resp.headers.get("content-type", "").lower()
    path = urlparse(str(resp.url)).path.lower()
    return "application/pdf" in ct or path.endswith(".pdf")


def _fetch_response(url: str) -> httpx.Response:
    """Hardened independent fetch: rotate UAs on 401/403, one backoff on 429.
    Returns the response (which may still be 4xx — caller categorizes). Raises only
    on a genuine connection/timeout failure. NO model involved."""
    last = None
    for ua in _UAS:
        headers = {**_BASE_HEADERS, "User-Agent": ua}
        for attempt in range(2):  # allow one 429 backoff-retry per UA
            resp = _safe_get(url, headers, config.GROUNDING_TIMEOUT_S)
            last = resp
            if resp.status_code == 429 and attempt == 0:
                wait = min(float(resp.headers.get("retry-after", "2") or 2), 5.0)
                time.sleep(wait)
                continue
            break
        if last is not None and last.status_code not in (401, 403, 429):
            return last  # success or a non-block error — stop rotating
    return last  # exhausted UAs; hand back the last (blocked) response


def _extract_text(resp: httpx.Response) -> tuple[str, str]:
    """Returns (text, kind) where kind is 'pdf' or 'html'."""
    if _is_pdf(resp):
        return _pdf_to_text(resp.content), "pdf"
    return _html_to_text(resp.text), "html"


@dataclass
class GroundingResult:
    """One claim's grounding outcome + instrumentation for #7 tuning/audit."""
    claim_id: str
    subject_key: str
    url: str
    status: str            # "grounded" | "absent" | "unreachable"
    method: str | None     # "substring" | "fuzzy" | None
    best_ratio: float | None   # 0..1 — watch the 0.80-0.92 band for true-claim cuts
    http_status: int | None
    content_kind: str | None   # "html" | "pdf" | None
    substituted: bool          # anchor was substituted for a blocked higher-tier source
    excerpt: str
    fetched_at: str
    detail: str | None
    excerpt_offset: int | None = None  # char position of the excerpt in the full page
    page_len: int | None = None        # full page length — together: how DEEP the fact sat

    def is_grounded(self) -> bool:
        return self.status == "grounded"


CUT_ABSENT = "evidence excerpt not found in source"
CUT_UNREACHABLE = "source unreachable for grounding"
CUT_EXCLUDED = "source on excluded list — re-source from reputable news or cut"

# Hard exclusion (control line): a claim anchored on any of these is cut BEFORE any
# fetch and routed to repair, so a true-but-badly-sourced fact gets re-anchored on
# reputable news rather than lost. (The fuzzy long tail — generic SEO roundups, random
# how-to blogs — stays a model judgment in the prompts; these sets are the enumerable
# core that we enforce deterministically.)
#
# Wikis / tertiary encyclopedias: lag and are gameable.
BLOCKED_SOURCE_DOMAINS = {
    "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org",
    "wikinews.org", "wikivoyage.org", "wikibooks.org", "wikiquote.org",
    "wikisource.org", "fandom.com", "wikia.com", "britannica.com",
    "everipedia.org", "infogalactic.com",
}
# Weak / off-topic: crypto-exchange content marketing (gate.com's "2026 product
# lineup" listicle, kucoin sentiment), AI SEO content-mills, and how-to/tutorial blogs
# — never an acceptable anchor for a fact, status, or positioning claim.
WEAK_SOURCE_DOMAINS = {
    "gate.com", "gate.io", "kucoin.com", "binance.com", "bybit.com", "okx.com",
    "mexc.com", "coinmarketcap.com",
    "glbgpt.com", "laozhang.ai", "digen.ai", "codersera.com",
}
_ALL_EXCLUDED = BLOCKED_SOURCE_DOMAINS | WEAK_SOURCE_DOMAINS


def _host(url: str) -> str:
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, domains: set) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_excluded_source(url: str) -> bool:
    """True if url's host is a banned wiki/encyclopedia OR weak/off-topic domain."""
    return _matches(_host(url), _ALL_EXCLUDED)


def exclusion_reason(url: str) -> str | None:
    """Why a source is excluded (for the cut log), or None if it's allowed."""
    host = _host(url)
    if _matches(host, BLOCKED_SOURCE_DOMAINS):
        return "wiki/encyclopedia not permitted"
    if _matches(host, WEAK_SOURCE_DOMAINS):
        return "weak source (crypto-exchange / content-mill / tutorial-blog) not permitted"
    return None


def _fetch_text(url: str):
    """Independent fetch -> (normalized_page_text|None, http_status, kind, error).
    Model-free. None page_text means unreachable (with the reason in `error`)."""
    try:
        resp = _fetch_response(url)
    except httpx.TimeoutException:
        return None, None, None, "timeout"
    except Exception as e:
        return None, None, None, f"{type(e).__name__}: {e}"[:200]
    if resp is None or resp.status_code >= 400:
        return None, getattr(resp, "status_code", None), None, f"HTTP {getattr(resp, 'status_code', '?')}"
    try:
        text, kind = _extract_text(resp)
    except Exception as e:
        return None, resp.status_code, None, f"extract error: {type(e).__name__}"
    return _normalize(text), resp.status_code, kind, None


def _prefetch(urls: list[str]) -> dict:
    """Free levers A+B: fetch each UNIQUE url once (dedup), concurrently (parallel).
    Returns {url: (page_text|None, status, kind, error)}."""
    uniq = list(dict.fromkeys(urls))
    if not uniq:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(uniq))) as ex:
        return dict(zip(uniq, ex.map(_fetch_text, uniq)))


def ground_claim(claim: dict, fetched=None) -> GroundingResult:
    """Check evidence_excerpt against the (independently fetched) page text.
    Pure string matching — no model involved (see PROVENANCE above). `fetched` is the
    pre-fetched (page, status, kind, error) tuple from _prefetch; if None, fetch now."""
    url = claim["source_url"]
    excerpt = claim["evidence_excerpt"]
    fetched_at = date.today().isoformat()
    substituted = bool(claim.get("anchor_substitution"))
    cid, skey = claim.get("id", "?"), claim.get("subject_key", "?")

    page, http_status, kind, err = fetched if fetched is not None else _fetch_text(url)
    if page is None:
        return GroundingResult(cid, skey, url, "unreachable", None, None, http_status,
                               None, substituted, excerpt, fetched_at, err)

    ex = _normalize(excerpt)
    best_ratio = round(fuzz.partial_ratio(ex, page) / 100.0, 4) if page else 0.0

    if ex and ex in page:
        return GroundingResult(cid, skey, url, "grounded", "substring", 1.0,
                               http_status, kind, substituted, excerpt, fetched_at, None,
                               excerpt_offset=page.find(ex), page_len=len(page))
    if best_ratio >= config.GROUNDING_FUZZY_THRESHOLD:
        return GroundingResult(cid, skey, url, "grounded", "fuzzy", best_ratio,
                               http_status, kind, substituted, excerpt, fetched_at,
                               f"partial_ratio {best_ratio:.3f}")
    return GroundingResult(cid, skey, url, "absent", None, best_ratio,
                           http_status, kind, substituted, excerpt, fetched_at,
                           f"best partial_ratio {best_ratio:.3f} < {config.GROUNDING_FUZZY_THRESHOLD}")


def ground_claims(claims: list[dict]) -> dict:
    """Ground every claim. Returns kept claims (with grounding filled in), cut-log
    entries for failures, the per-claim instrumentation log, and dual counters.

    NOTE: corroboration sources are NOT grounded — only the single anchor is the
    proven source (claim-object.md §2.1).
    """
    kept, cut_entries, failed, results = [], [], [], []
    counts = {"grounded": 0, "absent": 0, "unreachable": 0, "excluded": 0}

    # Deterministic exclusion FIRST: never spend a fetch on a blocked wiki/encyclopedia
    # source — cut it and route to repair so the claim can be re-sourced to news.
    allowed = []
    for claim in claims:
        if is_excluded_source(claim["source_url"]):
            counts["excluded"] += 1
            reason = f"{CUT_EXCLUDED} ({exclusion_reason(claim['source_url'])})"
            cut_entries.append({
                "action": "CUT",
                "claim": claim.get("claim", claim.get("subject_key", "?")),
                "reason": reason,
            })
            failed.append({"claim": claim, "status": "excluded", "reason": reason})
        else:
            allowed.append(claim)

    # Free levers A+B: prefetch unique URLs once, concurrently; reuse per claim.
    cache = _prefetch([c["source_url"] for c in allowed])

    for claim in allowed:
        res = ground_claim(claim, fetched=cache.get(claim["source_url"]))
        results.append(res)
        counts[res.status] += 1
        if res.is_grounded():
            claim = dict(claim)
            claim["grounding"] = {
                "checked": True, "match": True, "method": res.method,
                "fetched_at": res.fetched_at, "detail": res.detail,
            }
            kept.append(claim)
        else:
            reason = CUT_ABSENT if res.status == "absent" else CUT_UNREACHABLE
            cut_entries.append({
                "action": "CUT",
                "claim": claim.get("claim", claim.get("subject_key", "?")),
                "reason": reason,
            })
            # The full claim object + outcome, so the feedback retry can repair it.
            failed.append({"claim": claim, "status": res.status, "reason": reason})

    return {
        "kept": kept,
        "cut": cut_entries,
        "failed": failed,
        "results": [asdict(r) for r in results],
        "counts": counts,  # grounded vs absent (fabrication) vs unreachable (fetch)
        "substituted": sum(1 for r in results if r.substituted),
    }
