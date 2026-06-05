"""Per-claim source audit — prove the SOURCING DISCIPLINE held, not just that facts appeared.

Reads a battlecard's claims.json and prints, per claim, its section, subject_key,
self-reported source_tier, and the actual source DOMAIN — so a human can confirm
provenance. Hard-FAILS (deterministically) on:
  - a claim anchored on an EXCLUDED domain (wiki/encyclopedia), and
  - a Recent-Strategic-Moves or status/current-state claim NOT on a news/primary source.
Soft-WARNS on domains that look like promo listicles / aggregators (eyeball these).

    python scripts/source_audit.py [slug]      # exit 1 if any hard failure
"""
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import store
from scout.grounding import is_excluded_source

# Fuzzy long-tail that reads like a promo/SEO roundup or personal blog — NOT in the
# deterministic exclusion set (those already hard-fail via is_excluded_source), just
# flagged for a human to eyeball.
SUSPECT_AGGREGATORS = {
    "medium.com", "blogspot.com", "wordpress.com", "substack.com",
}

STATUS_QUALIFIERS = ("current", "latest", "flagship")
STATUS_WORDS = ("launch", "cancel", "discontinu", "sunset", "shut down", "price",
                "pricing", "limit", "acquire", "acquisition", "files for ipo", "ipo")


def _domain(url: str) -> str:
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _is_status_claim(c: dict) -> bool:
    if c.get("section") == "recent_moves":
        return True
    sk = (c.get("subject_key") or "").lower()
    if any(q in sk.split("|")[-1] for q in STATUS_QUALIFIERS):
        return True
    text = (c.get("claim") or "").lower()
    return any(w in text for w in STATUS_WORDS)


def audit(slug: str) -> int:
    claims = store.load_claims(slug)
    if not claims:
        print(f"No claims for slug {slug!r}", file=sys.stderr)
        return 2

    by_section = {}
    for c in claims:
        by_section.setdefault(c.get("section", "?"), []).append(c)

    fails, warns = [], []
    print(f"\nSOURCE AUDIT — {slug}   ({len(claims)} claims)\n" + "=" * 78)
    for section in sorted(by_section):
        print(f"\n## {section}")
        for c in sorted(by_section[section], key=lambda x: x.get("order", 0)):
            url = c.get("source_url", "")
            dom = _domain(url)
            tier = c.get("source_tier", "?")
            status = _is_status_claim(c)
            flag = "   "
            # The sentiment section is legitimately sentiment_only — don't demand
            # Tier-1 news there. Every OTHER section's status/recency claim must be.
            if is_excluded_source(url):
                flag = "❌ "; fails.append((c, f"EXCLUDED domain ({dom})"))
            elif status and tier == "sentiment_only" and section != "sentiment":
                flag = "❌ "; fails.append((c, f"status/recent claim on sentiment_only tier ({dom})"))
            elif dom in SUSPECT_AGGREGATORS:
                flag = "⚠️ "; warns.append((c, f"suspect aggregator/listicle ({dom})"))
            star = "•" if status else " "
            print(f"  {flag}{star} [{tier:18}] {dom:28} {(c.get('subject_key') or '')[:46]}")

    print("\n" + "=" * 78)
    print(f"• = recency/status claim (must be Tier-1 news or primary)")
    print(f"RESULT: {len(fails)} hard failure(s), {len(warns)} warning(s)")
    for c, why in fails:
        print(f"  ❌ {why}: {(c.get('claim') or '')[:70]}")
    for c, why in warns:
        print(f"  ⚠️  {why}: {(c.get('claim') or '')[:70]}")
    return 1 if fails else 0


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "anthropic__vs__openai__general"
    sys.exit(audit(slug))
