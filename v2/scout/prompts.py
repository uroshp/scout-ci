"""v2's prompt building blocks. **Independent copy**, not shared with v1.

These were lifted from research.py as a starting point, but v1 is frozen and keeps
its own inline copies. v2 owns and evolves these freely; nothing in research.py or
app.py imports from scout/, so a change here can never break the shipped v1 app.
The duplication is deliberate: there is nothing to drift from because v1 won't change.
"""
import os

from scout import config

SOURCE_HIERARCHY = """SOURCE TRUST HIERARCHY (match the claim type to the right source type - not all "official" sources are equal):
- TIER 1A - AUTHORITATIVE FOR FACT: audited financials and regulatory filings (10-K, 10-Q, 8-K, S-1, proxy statements), earnings-call transcripts, court documents, signed/announced contracts. Trust these for hard facts and numbers.
- TIER 1B - AUTHORITATIVE ONLY FOR SELF-POSITIONING: company blogs, press releases, marketing pages, exec keynotes, official docs, changelogs, and pricing pages. These reliably tell you what the company CHOSE to say and how it positions itself. Use them for positioning, product existence, stated pricing, and announced moves. Do NOT treat them as fact for evaluative or market claims ("fastest", "market leader", "best-in-class") - that is positioning; attribute it as the company's own claim.
- TIER 2 - REPUTABLE NEWS: Reuters, Bloomberg, CNBC, The Information, TechCrunch, Axios, FT, WSJ. Fine to stand alone for the events and facts they report. Prefer the most recent.
- TIER 2E - ANALYST & MARKET ESTIMATES (label as ESTIMATE, never as audited fact): industry analysts (Gartner, Forrester, IDC), market-share/data providers (Synergy Research, Canalys, Statista), private-market research (Sacra, Contrary), and funding databases (Crunchbase, PitchBook). Market share, TAM, ARR estimates and Magic-Quadrant-style placements are MODELED OPINIONS - attribute them to the firm and frame as an estimate, not as a filed number.
- TIER 3 - ORIGINATING SECONDARY: lesser-known blogs/aggregators, acceptable only when clearly the origin of a fact and reasonably reputable.
- TIER 3S - STRUCTURED REVIEW PLATFORMS (sentiment, but weightier than forums): G2, Capterra, TrustRadius, Gartner Peer Insights. Use for sentiment with more confidence than raw social, given review volume and verified reviewers. Still sentiment, not fact.
- TIER 4 - RAW SOCIAL/FORUMS: Reddit, X, Glassdoor, Indeed, HN comments. Valid ONLY for sentiment, NEVER as the source of a hard factual claim. Easily astroturfed - treat with caution.

EXCLUDED SOURCES (never permitted, not as anchor, not as corroboration):
- WIKIPEDIA AND ALL WIKIS / TERTIARY ENCYCLOPEDIAS: Wikipedia, Wikimedia, Fandom/Wikia, Britannica, and the like. They lag and are gameable; for breaking competitive moves they are worthless. A claim that rests on one is CUT. Find the original reputable news report or primary document instead. (A deterministic check also cuts any claim anchored on these domains.)
- PROMO LISTICLES / SEO ROUNDUPS / AGGREGATORS / AI-GENERATED CONTENT FARMS: "best X of 2026" roundups, exchange/affiliate blogs (e.g. crypto-exchange "product lineup" pages), and link-aggregator posts. They are stale and unreliable for current status. Do not anchor a fact or a product-status claim on one. Trace to the originating news outlet or company source.
- CRYPTO-EXCHANGE / OFF-TOPIC DOMAINS and HOW-TO / TUTORIAL BLOGS (e.g. gate.com, kucoin.com, codersera.com): weak and off-topic for competitive intelligence. Never an anchor, not even for sentiment. (A deterministic check also cuts the enumerable offenders.)
- POSITIONING / LEADERSHIP claims ("X is the quality leader", "developers prefer X") must anchor on reputable NEWS or a primary/benchmark source, NEVER a tutorial blog, forum, or sentiment site. If only sentiment supports it, frame it explicitly as sentiment in the Sentiment section, not as a positioning fact.

NEWS-FIRST FOR CURRENCY: recent events and current status are anchored on reputable NEWS (Tier 2) or primary documents, not reference sites. The freshest reputable news wins for "what is true now".

RULES:
- A factual claim resting only on Tier 3S/4 is NOT verified. A direct quote must trace to a Tier 1/2/3 source.
- TIER-1 NEWS REQUIRED for recency/status: every "Recent Strategic Moves" item and every current-state/status claim (who leads, latest figure, current/flagship product, a launch, a cancellation, a price/limit change) MUST anchor on a reputable news outlet (Tier 2) or a primary filing/announcement (Tier 1). Never a wiki, listicle, or aggregator. Company PR alone is fine for what the company announced, but an ADVERSE fact about a competitor (a cancellation, a loss, churn) should trace to independent reporting, not only the affected party.
- Distinguish AUDITED revenue (public companies) from COMPANY-STATED ARR/metrics (private, unaudited) - always say which it is.
- For any "current state" claim (who leads, latest figures, who holds a role), a recent Tier 2 source can override an older Tier 1 filing. Prefer the most recent verified figure and note the as-of date when it matters.
- NO PROXY ATTRIBUTION: if a Tier 3 source merely REPORTS a figure it attributes to a more authoritative origin (a named survey, filing, or analyst firm), find and cite that origin directly, or cut the claim. Never cite an aggregator or directory blog as a stand-in for the origin it is quoting (e.g. do not cite a blog "reporting a JetBrains survey" - cite JetBrains, or cut it).
- Keep four things explicitly separate and never blur them: VERIFIED FACT, the COMPANY'S OWN CLAIM/positioning, ANALYST/MODELED ESTIMATE, and SENTIMENT."""

WRITING_STYLE = """WRITING STYLE (applies to EVERY sentence you write: claims, so-whats,
headlines, soundbites, cut-log reasons):
- ABSOLUTELY NO EM DASHES OR EN DASHES USED AS PUNCTUATION (— or –). This is a hard, non-negotiable
  constraint, checked on every output. There is no sentence where one is acceptable, including inside
  quotes, soundbites, and parentheticals. Replace it: a period when it joins two independent clauses,
  a comma for a brief aside, a colon before an explanation or list, or parentheses, or split into two
  sentences. One stray em dash anywhere in any string is a failed output, not a minor blemish.
- No "rule of three" filler: do not reflexively list exactly three parallel items ("X, Y, and Z")
  when the evidence gives you two, or four, or one. List what the evidence supports.
- No negation-contrast framing: never "it's not X, it's Y", "this isn't about X but Y",
  "X is no longer the point". State directly what IS true.
- Write like a sharp human analyst briefing a colleague: concrete subjects, active verbs, plain
  words. No throat-clearing ("It's worth noting", "Importantly"), no marketing gloss
  ("seamless", "robust", "game-changing"), no symmetrical sentence patterns repeated across
  bullets. If a sentence would sound canned read aloud, rewrite it.
- Soundbites are the line a salesperson actually says to a buyer on a call. State plainly what the
  buyer gets and the reason to decide now, in concrete terms a procurement lead would repeat out
  loud (for example the real dollar cost, or the lock-in it removes). When the
  competitor has a genuine advantage, concede it first, then give the Anthropic counter as a number
  or a clear outcome. Never end on a tease like "try it and see" or "see what happens", and never
  lean on insider jargon such as "tokens-to-resolution" or "merge rate"."""

FORMATTING_RULES = """MARKDOWN FORMATTING RULES (follow exactly so the report renders cleanly):
- Use ## for the main section headers ONLY (Executive Summary, Snapshot, Recent Strategic Moves, Positioning and Differentiation, Pricing and Packaging, Competitive Battlecard, Sentiment, Objection Handling, Cut Log). Use ### for sub-headers within a section (e.g. battlecard zones). Never use headers for normal content.
- Three sections are written as PROSE BLOCKS, not bullets: the Executive Summary, the Competitive Battlecard (inside each zone), and Objection Handling. Each entry there is a short multi-line block (a bolded title line, a blank line, a 1-2 sentence paragraph, a blank line, then its labeled soundbite/so-what line), NOT a one-line bullet. Do not prefix these blocks with "- ".
- In ALL OTHER sections (Snapshot, Recent Strategic Moves, Positioning, Pricing, Sentiment), every list item is a SINGLE line starting with "- " (dash space), with all of that item's text on that one line. NEVER break a bullet's text onto a separate line. NEVER put a blank line between a bullet's dash and its text. NEVER put blank lines between consecutive bullets. A bullet and its text are one unbroken line.
- Put a blank line between separate paragraphs, between a header and the text under it, and before and after a list as a whole. But do NOT put blank lines between consecutive bullets in the same list, and do NOT put a blank line inside a single bullet.
- In the Executive Summary, write each numbered conclusion as: a bolded one-sentence verdict using **bold**, then a blank line, then the supporting detail as a normal paragraph.
- In the Executive Summary, EVERY numbered point MUST end with a "So what:" takeaway. Format it as: the verdict and detail, then a blank line, then "**So what:**" starting its own new line, followed by the actionable implication. The "So what:" is mandatory for every executive summary point and must always be on its own bolded line, never blended into the preceding text.
- Other labels (**Implication:**, **What tips it:**, **Why they win:**, **Usable soundbite:**) must also each start their own new bolded line, preceded by a blank line, never inline in a paragraph.
- Bold the single most important phrase in each major point with **bold**, but do not over-bold.
- Soundbites: put on their own line, in italics, e.g. *"..."*
- Do not indent any line with spaces or tabs (indented lines render as gray code blocks).
- Do NOT add a "Prepared for", "Classification", "Date", or any internal-memo header block. Do NOT add a horizontal rule (---) immediately under the title. Start directly with the report title (# Competitive Intelligence Brief: ...) followed by the Executive Summary. No cover-page or addressee lines."""


def load_methodology():
    """The CI discipline the model is held to. Kept as an editable plain-English
    spec out of code on purpose (README 'Design decisions')."""
    with open(os.path.join(config.APP_ROOT, "methodology.md"), "r") as f:
        return f.read()
