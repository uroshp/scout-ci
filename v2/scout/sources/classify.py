"""Deterministic source classification (WS0, 2026-09-28).

`source_class` answers "what kind of source is this URL" from the host alone, with no model:

    filing             SEC EDGAR (sec.gov)                                   -> tier primary
    court              court records (courtlistener, pacer, uscourts)        -> tier primary
    government         a regulator / government statement (.gov, europa.eu) -> tier primary
    company_statement  the company's own site, press-release wires, IR site -> tier primary
    job_posting        Greenhouse / Ashby / Lever boards, careers.* hosts    -> tier primary
    page_snapshot      Wayback Machine snapshots                             -> tier kept
    news               a major outlet (allow-listed)                          -> tier reputable_secondary
    research           arxiv / journals / .edu                               -> tier reputable_secondary
    review_site        G2, Capterra, TrustRadius, Gartner Peer Insights ...  -> tier sentiment_only
    forum              Hacker News, Reddit, X, community.*, forums           -> tier sentiment_only
    unknown            anything else: the class is shown as-is, the model's tier stands

Tier normalization is deliberately conservative: it only ever REPLACES a model-asserted tier with
the class's tier when the result keeps the claim valid (a `fact` may not rest on `sentiment_only`,
schema.py). When the class disagrees with the claim's shape, the claim keeps its tier and the
disagreement is recorded as `source_class_conflict`, which the sources page surfaces to the
reader. The map is data, tested, and deliberately incomplete: an outlet that is not on the news
list is `unknown`, never guessed.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

SOURCE_CLASSES = ["filing", "court", "government", "company_statement", "job_posting",
                  "page_snapshot", "news", "research", "review_site", "forum", "unknown"]

# Class -> the tier code asserts for it (None = keep the model's tier).
CLASS_TIER = {
    "filing": "primary", "court": "primary", "government": "primary",
    "company_statement": "primary", "job_posting": "primary",
    "page_snapshot": None,
    "news": "reputable_secondary", "research": "reputable_secondary",
    "review_site": "sentiment_only", "forum": "sentiment_only",
    "unknown": None,
}

# Reader-facing labels (the chip text) and the one-line tooltip.
CLASS_LABEL = {
    "filing": "Filing", "court": "Court record", "government": "Government",
    "company_statement": "Company statement", "job_posting": "Job posting",
    "page_snapshot": "Page snapshot", "news": "News", "research": "Research",
    "review_site": "Reviews", "forum": "Forum", "unknown": "Web",
}

_FILING = ("sec.gov",)
_COURT = ("courtlistener.com", "pacer.gov", "uscourts.gov", "supremecourt.gov")
_GOVERNMENT_SUFFIXES = (".gov", ".gov.uk", ".europa.eu", ".gc.ca", ".gov.au")
_PRESS_WIRES = ("prnewswire.com", "globenewswire.com", "businesswire.com", "newswire.ca",
                "accesswire.com", "prweb.com")
_JOB_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com",
              "jobs.lever.co", "boards-api.greenhouse.io", "api.ashbyhq.com", "api.lever.co")
_SNAPSHOT = ("web.archive.org", "archive.org")
_RESEARCH = ("arxiv.org", "openalex.org", "semanticscholar.org", "nature.com", "science.org",
             "acm.org", "ieee.org", "ssrn.com", "biorxiv.org", "medrxiv.org", "nber.org")
_REVIEW = ("g2.com", "capterra.com", "trustradius.com", "gartner.com", "softwareadvice.com",
           "getapp.com", "trustpilot.com", "producthunt.com", "peerspot.com",
           "rottentomatoes.com", "metacritic.com", "imdb.com")
_FORUM = ("news.ycombinator.com", "reddit.com", "x.com", "twitter.com", "stackoverflow.com",
          "quora.com", "lobste.rs", "mastodon.social", "bsky.app", "threads.net", "discord.com")
_FORUM_PREFIXES = ("community.", "forum.", "forums.", "discuss.", "discussions.")
_JOB_PREFIXES = ("careers.", "jobs.", "career.")

# Major outlets (2026-09-28 inventory of every citation on the 10 cards, plus the usual suspects).
# Deliberately an allow-list: a host that is not here is `unknown`, not `news`.
_NEWS = (
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "nytimes.com", "washingtonpost.com",
    "theinformation.com", "cnbc.com", "cnn.com", "nbcnews.com", "abcnews.go.com", "cbsnews.com",
    "npr.org", "bbc.com", "bbc.co.uk", "aljazeera.com", "apnews.com", "theguardian.com",
    "economist.com", "axios.com", "semafor.com", "politico.com", "japantimes.co.jp",
    "techcrunch.com", "theverge.com", "wired.com", "arstechnica.com", "venturebeat.com",
    "fortune.com", "forbes.com", "businessinsider.com", "fastcompany.com", "inc.com",
    "theregister.com", "zdnet.com", "engadget.com", "thenextweb.com", "siliconangle.com",
    "techradar.com", "9to5mac.com", "9to5google.com", "macrumors.com", "digitaltrends.com",
    "bleepingcomputer.com", "thehackernews.com", "securityweek.com", "csoonline.com",
    "helpnetsecurity.com", "darkreading.com", "cio.com", "computerworld.com", "infoworld.com",
    "techtimes.com", "the-decoder.com", "marktechpost.com", "pymnts.com", "benzinga.com",
    "finance.yahoo.com", "marketwatch.com", "barrons.com", "cfodive.com", "ciodive.com",
    "variety.com", "deadline.com", "hollywoodreporter.com", "boxofficemojo.com",
    "the-numbers.com", "itpro.com", "uctoday.com", "cmswire.com", "techstartups.com",
    "unite.ai", "247wallst.com", "marketscreener.com", "tradingkey.com", "securitybrief.co.uk",
    "news.bloomberglaw.com", "bloomberglaw.com", "law360.com", "fool.com",
)

# A company's own hosts that do not carry its name: the alias tokens are added when the company
# name contains the key (AWS -> amazon: aboutamazon.com; Anthropic -> claude: claude.com,
# platform.claude.com, status.claude.com; Cognition -> devin: devin.ai; Slack -> salesforce).
COMPANY_ALIASES = {
    "aws": ["amazon"], "amazon": ["aws"], "google": ["alphabet", "youtube", "deepmind"],
    "anthropic": ["claude"], "openai": ["chatgpt"], "cognition": ["devin"], "slack": ["salesforce"],
    "microsoft": ["azure", "github", "copilot"], "github": ["microsoft"], "meta": ["facebook"],
    "x": ["twitter"], "mistral": ["magistral"],
}
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"inc", "corp", "corporation", "llc", "ltd", "the", "and", "ai", "cloud", "labs", "com",
         "co", "group", "technologies", "technology", "software", "systems", "team", "teams"}


def host_of(url) -> str:
    try:
        h = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _ends(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _company_tokens(*names) -> set:
    toks = set()
    for n in names:
        for t in _TOKEN.findall(str(n or "").lower()):
            if len(t) >= 3 and t not in _STOP:
                toks.add(t)
            for alias in COMPANY_ALIASES.get(t, ()):
                toks.add(alias)
    return toks


def is_company_host(host: str, *names) -> bool:
    """The company's own domain: a distinctive token of its name equals a label of the host
    (anthropic.com, cloud.google.com, aws.amazon.com, ir.hubspot.com, cognition.ai)."""
    toks = _company_tokens(*names)
    if not toks or not host:
        return False
    labels = {l for l in host.split(".") if l}
    if toks & labels:
        return True
    # long, distinctive tokens may also sit INSIDE a label (aboutamazon.com for AWS); short ones
    # never do (meta must not claim metacritic.com; review hosts are matched first anyway)
    return any(t in l for t in toks if len(t) >= 5 for l in labels)


def classify(url, *company_names) -> str:
    """Pure: a URL (+ the card's company names for company_statement) -> a SOURCE_CLASSES value."""
    host = host_of(url)
    if not host:
        return "unknown"
    if _ends(host, _FILING):
        return "filing"
    if _ends(host, _COURT):
        return "court"
    if _ends(host, _SNAPSHOT):
        return "page_snapshot"
    if _ends(host, _JOB_HOSTS) or (host.startswith(_JOB_PREFIXES) and is_company_host(host, *company_names)):
        return "job_posting"
    if host.startswith(_FORUM_PREFIXES) or _ends(host, _FORUM):
        return "forum"
    if _ends(host, _REVIEW):
        return "review_site"
    if _ends(host, _RESEARCH) or host.endswith(".edu"):
        return "research"
    if host.endswith(_GOVERNMENT_SUFFIXES):
        return "government"
    if _ends(host, _PRESS_WIRES) or host.startswith(("ir.", "investor.", "investors.")):
        return "company_statement"
    if is_company_host(host, *company_names):
        return "company_statement"
    if _ends(host, _NEWS):
        return "news"
    return "unknown"


def _valid_tier_for(claim: dict, tier: str | None) -> bool:
    """A `fact` may not rest on `sentiment_only` (schema.py); everything else is fine."""
    if tier is None:
        return False
    return not (claim.get("claim_type") == "fact" and tier == "sentiment_only")


def stamp(claim: dict, meta: dict | None = None) -> dict:
    """Stamp `source_class` (and normalize `source_tier` where code can know it) on ONE claim,
    in place, and return it. Never raises; never makes a valid claim invalid.

    - `source_class` from `source_url` (the anchor); `provenance.source_class` from
      `provenance.source_url`; each `corroboration[]` entry gets its own.
    - `source_tier` is replaced by the class's tier when the class is known AND the result keeps
      the claim valid; otherwise the model's tier stands and `source_class_conflict` names the
      class the code saw (e.g. a fact anchored on a forum).
    """
    try:
        names = ((meta or {}).get("competitor"), (meta or {}).get("my_company"))
        if claim.get("source_url"):
            cls = classify(claim["source_url"], *names)
            claim["source_class"] = cls
            want = CLASS_TIER.get(cls)
            if want and claim.get("source_tier") != want:
                if _valid_tier_for(claim, want):
                    claim["source_tier"] = want
                    claim.pop("source_class_conflict", None)
                else:
                    claim["source_class_conflict"] = cls
            else:
                claim.pop("source_class_conflict", None)
        prov = claim.get("provenance")
        if isinstance(prov, dict) and prov.get("source_url"):
            pcls = classify(prov["source_url"], *names)
            prov["source_class"] = pcls
            pwant = CLASS_TIER.get(pcls)
            if pwant and prov.get("source_tier") in (None, "primary", "reputable_secondary", "sentiment_only") \
                    and prov.get("source_tier") != pwant:
                prov["source_tier"] = pwant
        for s in claim.get("corroboration") or []:
            if isinstance(s, dict) and s.get("source_url"):
                s["source_class"] = classify(s["source_url"], *names)
    except Exception:
        pass
    return claim


def stamp_all(claims: list, meta: dict | None = None) -> list:
    for c in claims or []:
        if isinstance(c, dict):
            stamp(c, meta)
    return claims


def class_counts(claims: list) -> dict:
    """{source_class: n} over the ACTIVE claims' anchors, for the rail panel and the sources page."""
    out: dict = {}
    for c in claims or []:
        if c.get("status") == "retired" or not c.get("source_url"):
            continue
        k = c.get("source_class") or "unknown"
        out[k] = out.get(k, 0) + 1
    return out
