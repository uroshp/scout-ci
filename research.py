import os
from datetime import datetime
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-6"

SOURCE_HIERARCHY = """SOURCE TRUST HIERARCHY (match the claim type to the right source type - not all "official" sources are equal):
- TIER 1A - AUTHORITATIVE FOR FACT: audited financials and regulatory filings (10-K, 10-Q, 8-K, S-1, proxy statements), earnings-call transcripts, court documents, signed/announced contracts. Trust these for hard facts and numbers.
- TIER 1B - AUTHORITATIVE ONLY FOR SELF-POSITIONING: company blogs, press releases, marketing pages, exec keynotes, official docs, changelogs, and pricing pages. These reliably tell you what the company CHOSE to say and how it positions itself. Use them for positioning, product existence, stated pricing, and announced moves. Do NOT treat them as fact for evaluative or market claims ("fastest", "market leader", "best-in-class") - that is positioning; attribute it as the company's own claim.
- TIER 2 - REPUTABLE NEWS: Reuters, Bloomberg, CNBC, The Information, TechCrunch, Axios, FT, WSJ. Fine to stand alone for the events and facts they report. Prefer the most recent.
- TIER 2E - ANALYST & MARKET ESTIMATES (label as ESTIMATE, never as audited fact): industry analysts (Gartner, Forrester, IDC), market-share/data providers (Synergy Research, Canalys, Statista), private-market research (Sacra, Contrary), and funding databases (Crunchbase, PitchBook). Market share, TAM, ARR estimates and Magic-Quadrant-style placements are MODELED OPINIONS - attribute them to the firm and frame as an estimate, not as a filed number.
- TIER 3 - ORIGINATING SECONDARY: lesser-known blogs/aggregators, acceptable only when clearly the origin of a fact and reasonably reputable.
- TIER 3S - STRUCTURED REVIEW PLATFORMS (sentiment, but weightier than forums): G2, Capterra, TrustRadius, Gartner Peer Insights. Use for sentiment with more confidence than raw social, given review volume and verified reviewers. Still sentiment, not fact.
- TIER 4 - RAW SOCIAL/FORUMS: Reddit, X, Glassdoor, Indeed, HN comments. Valid ONLY for sentiment, NEVER as the source of a hard factual claim. Easily astroturfed - treat with caution.

RULES:
- A factual claim resting only on Tier 3S/4 is NOT verified. A direct quote must trace to a Tier 1/2/3 source.
- Distinguish AUDITED revenue (public companies) from COMPANY-STATED ARR/metrics (private, unaudited) - always say which it is.
- For any "current state" claim (who leads, latest figures, who holds a role), a recent Tier 2 source can override an older Tier 1 filing. Prefer the most recent verified figure and note the as-of date when it matters.
- NO PROXY ATTRIBUTION: if a Tier 3 source merely REPORTS a figure it attributes to a more authoritative origin (a named survey, filing, or analyst firm), find and cite that origin directly, or cut the claim. Never cite an aggregator or directory blog as a stand-in for the origin it is quoting (e.g. do not cite a blog "reporting a JetBrains survey" - cite JetBrains, or cut it).
- Keep four things explicitly separate and never blur them: VERIFIED FACT, the COMPANY'S OWN CLAIM/positioning, ANALYST/MODELED ESTIMATE, and SENTIMENT."""

FORMATTING_RULES = """MARKDOWN FORMATTING RULES (follow exactly so the report renders cleanly):
- Use ## for the main section headers ONLY (Executive Summary, Snapshot, Recent Strategic Moves, Positioning and Differentiation, Pricing and Packaging, Competitive Battlecard, Sentiment, Objection Handling, Cut Log). Use ### for sub-headers within a section (e.g. battlecard zones). Never use headers for normal content.
- Every list item is a SINGLE line starting with "- " (dash space), with all of that item's text on that one line. NEVER break a bullet's text onto a separate line. NEVER put a blank line between a bullet's dash and its text. NEVER put blank lines between consecutive bullets. A bullet and its text are one unbroken line.
- Put a blank line between separate paragraphs, between a header and the text under it, and before and after a list as a whole. But do NOT put blank lines between consecutive bullets in the same list, and do NOT put a blank line inside a single bullet.
- In the Executive Summary, write each numbered conclusion as: a bolded one-sentence verdict using **bold**, then a blank line, then the supporting detail as a normal paragraph.
- In the Executive Summary, EVERY numbered point MUST end with a "So what:" takeaway. Format it as: the verdict and detail, then a blank line, then "**So what:**" starting its own new line, followed by the actionable implication. The "So what:" is mandatory for every executive summary point and must always be on its own bolded line, never blended into the preceding text.
- Other labels (**Implication:**, **What tips it:**, **Why they win:**, **Usable soundbite:**) must also each start their own new bolded line, preceded by a blank line, never inline in a paragraph.
- Bold the single most important phrase in each major point with **bold**, but do not over-bold.
- Soundbites: put on their own line, in italics, e.g. *"..."*
- Do not indent any line with spaces or tabs (indented lines render as gray code blocks).
- Do NOT add a "Prepared for", "Classification", "Date", or any internal-memo header block. Do NOT add a horizontal rule (---) immediately under the title. Start directly with the report title (# Competitive Intelligence Brief: ...) followed by the Executive Summary. No cover-page or addressee lines."""


def load_methodology():
    with open("methodology.md", "r") as f:
        return f.read()


def _from_title(text):
    """Drop any model preamble that appears before the report title.
    Backstop for the prompt instruction: even if the model thinks out loud,
    the saved/returned brief always starts at the title."""
    idx = text.find("# Competitive Intelligence Brief")
    return text[idx:] if idx != -1 else text


def _extract(response):
    """Rebuild the model's prose from its content blocks and append the real
    source link(s) after each cited span. Citations come from the web_search
    tool's results, so the URLs are genuine retrieved sources - the model
    cannot fabricate them. Blocks are joined with "" (not "\\n") because cited
    spans are contiguous fragments of one passage."""
    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        citations = getattr(block, "citations", None) or []
        links, seen = [], set()
        for c in citations:
            if isinstance(c, dict):
                url, title = c.get("url"), c.get("title")
            else:
                url, title = getattr(c, "url", None), getattr(c, "title", None)
            if not url or url in seen:
                continue
            seen.add(url)
            label = (title or url).strip().replace("\n", " ")
            if len(label) > 60:
                label = label[:57].rstrip() + "..."
            links.append(f"[{label}]({url})")
        if links:
            text = text.rstrip() + " (" + ", ".join(links) + ")"
        parts.append(text)
    return "".join(parts)


def generate_brief(target, perspective=None, product=None):
    methodology = load_methodology()
    focus = f" Focus specifically on their product: {product}." if product else ""

    if perspective:
        framing = f"""You are a competitive intelligence analyst working for {perspective}. Research {target} and produce a competitive intelligence brief that helps {perspective} compete against {target}.{focus}

This brief is RELATIVE. The battlecard and objection handling are framed from {perspective}'s perspective competing against {target}. Research BOTH companies enough to compare on real evidence. Do not assume {perspective} is superior. Be honest in both directions."""
        title = f"# Competitive Intelligence Brief: {perspective} vs {target}"
        battlecard_note = f"Three zones: where {perspective} WINS against {target}, where it is a FIGHT, where {target} BEATS {perspective}. One usable soundbite per zone."
        objection_note = f"Up to 3 objections a prospect raises to {perspective} while citing {target}, each with a specific, evidence-based response."
    else:
        framing = f"""You are a competitive intelligence analyst. Research {target} using web search and produce a specific, evidence-grounded competitive intelligence brief.{focus}"""
        title = f"# Competitive Intelligence Brief: {target}"
        battlecard_note = "Three zones (where they win, where it is a fight, where they are vulnerable), one usable soundbite per zone."
        objection_note = f"Up to 3 likely objections a prospect raises citing {target}, grounded in real weaknesses, each with a specific response."

    prompt = f"""{framing}

Follow this methodology exactly:

<methodology>
{methodology}
</methodology>

{SOURCE_HIERARCHY}

{FORMATTING_RULES}

HARD SOURCING RULE: every factual claim and every number must be followed immediately by its source as a markdown link [SourceName](full-url) with the real name and real URL from your search. If you cannot attach a real source to a claim, do not write the claim. Never invent URLs. Quotes must carry their source link or be omitted.

NUMBER INTEGRITY (strict): for any single entity, never present two materially different counts for the same thing without explicitly explaining why they differ. If the draft says one company has both "1,000,000 contractors" and "50,000 expert contractors," either (a) make the distinction explicit and consistent everywhere ("~1M total annotators, of which ~50,000 are vetted expert contractors"), or (b) if you cannot confirm the distinction, keep only the figure you can verify and cut the other. The same applies to "300,000 talent pool" vs "30,000 contractors" — distinguish pool/applicants from active contractors clearly, or cut the unverifiable one. No two conflicting numbers for the same metric anywhere in the brief.

ENTITY & VERSION CONSISTENCY (strict): the same company, product, or model VERSION must be named identically in every section of the brief. Never refer to the same thing by two different version numbers or names in different places (e.g. "Claude Opus 4.8" in the executive summary and "Opus 4.7" in the pricing section is a disqualifying contradiction). For any "current", "latest", or "flagship" designation, use the single most recent value your freshest verified source supports, and use that one value consistently throughout. A brief that contradicts itself on a product name or version loses all credibility.

Search thoroughly. Prefer recent, reputable sources. Produce:

{title}

## Executive Summary
3 to 5 conclusions, ordered by impact (most decision-changing first). Each conclusion has TWO mandatory parts: (a) the finding stated as a verdict with its evidence, then (b) a "So what:" line that states the concrete action or implication for the reader. The "So what:" is the entire point of the brief - it is what makes this intelligence rather than information. NEVER write an executive summary point without its "So what:". A finding without a "So what:" is incomplete and unacceptable. Format the "So what:" on its own line, bolded as **So what:**, after a blank line.

## Snapshot ({target})
What they do, who they serve, stage/size/funding, hiring signals, leadership changes and what they signal, earnings/analyst or investor commentary. Each claim sourced.

## Recent Strategic Moves
Launches, pricing, partnerships, funding, public talks, with dates and what they signal. Each sourced.

## Positioning and Differentiation
How they actually position (real language) and real differentiators. Specific. Sourced.

## Pricing and Packaging
How they price and package: tiers, pricing model (per-seat, usage, flat), public list prices, and any recent pricing changes with dates. State pricing-page figures as their stated prices with the source link (Tier 1B). Where pricing is opaque or quote-only, say so and note what that signals about their buyer/motion. If no reliable pricing information exists, say "No public pricing found" rather than guessing.

## Competitive Battlecard
{battlecard_note}

## Sentiment
Praise and complaints from forums/reviews, framed as sentiment, with sources.

## Objection Handling
{objection_note}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )
    return _from_title(_extract(response))


def verify_brief(draft):
    prompt = f"""You are the final verification layer for a competitive intelligence brief. Your output is the FINAL CLEAN BRIEF a busy professional reads and acts on. It must contain ONLY verified claims, AND it must be sharp and actionable, not a dry fact list.

CRITICAL OUTPUT DISCIPLINE: Begin your response immediately with the title line "# Competitive Intelligence Brief: ...". Do NOT write any planning, reconciliation notes, claim-by-claim audit, or other preamble before the title - do that reasoning silently. The Cut Log at the end of the brief is the ONLY place you record what you removed or revised. Any text before the title line is a failure.

{SOURCE_HIERARCHY}

{FORMATTING_RULES}

Use web search to verify each claim against findable reputable sources. Then return the clean brief.

KEEP a claim only if ALL are true:
- You can confirm it from a reputable (Tier 1/2, or clearly-originating reputable Tier 3) source via search.
- It has a real, working source link.
- It is specific and decision-relevant (it changes how the reader sells, builds, prices, or positions).
- It is not redundant with another claim.

CUT (remove from the body entirely - no strikethrough, no inline flag, no "[unverified]" tag in the brief itself), then RECORD it in the Cut Log described below:
- Anything you cannot verify, any unattributable quote, any forum-only factual claim, any conflicting/unconfirmable number, anything generic or non-actionable.

SHARPNESS AND ORDERING (critical - do not produce a dry fact list):
- Lead with the verdict. Each section and the brief as a whole must be ordered strongest/most impactful first, weakest last.
- The Executive Summary opens with the single most decision-changing finding, stated as a verdict with its "so what" and a clear implied action, then descends by impact. Keep the punchy analytic voice (e.g. "X is the single biggest opening right now — be in every affected account this week"), but every such statement rests on a verified, linked claim.
- In the Battlecard and Objection Handling, keep the usable soundbites sharp and conversational. Soundbites must be grounded in verified claims but should read like something a person would actually say.
- Preserve the "so what" on every point. A verified fact with no implication is noise — either give it its decision-relevance or cut it.

OUTPUT RULES:
- Return ONLY the clean brief in the body, followed by the Cut Log. No editorial notes, no "[unverified]" tags, no strikethroughs, no fact-check commentary mixed into the claims in the body.
- Every surviving claim in the body carries a source link.
- Reconcile surviving numbers to one consistent value, and reconcile every product/model/version name to one consistent identity across all sections.
- Apply the MARKDOWN FORMATTING RULES above to the final output: proper "- " bullets, blank lines between elements, bold key phrases, labels like "Implication:" on their own bolded line, soundbites in italics, no indented lines, and NO horizontal rule directly under the title.
- Every Executive Summary point MUST end with a bolded "**So what:**" line giving the concrete action or implication for the reader. If any exec summary point is missing its "So what:", ADD one based on the verified finding. The "So what" is the core value of the brief - it is non-negotiable and must be present on every single executive summary conclusion.
- Sentiment stays framed as sentiment, attributed to its source.
- Fewer, solid, AND sharp beats long and weak. A short brief of bulletproof, ranked, actionable claims is the goal.
- Keep the section structure; a section can be short if few claims survive.

CUT LOG (this is a user-facing feature, not an internal note):
After the brief, on a new line, add a section with the exact header "## Cut Log". This section is the proof of the tool's discipline — it shows the reader exactly what was removed or corrected during verification and why. Begin the section with one plain sentence explaining what it is. Then list every claim you cut or revised, one per line, as bullets in EXACTLY this format:
- **CUT — <the specific claim, briefly stated>:** <the specific reason it could not be verified — e.g. no Tier 1/2 source found, forum-only, conflicting numbers, superseded by newer data>.
- **REVISED — <the specific claim>:** <what was wrong, and the corrected/verified value it was reconciled to>.
USE THE LABELS PRECISELY — this matters for credibility: "CUT" means the claim was removed from the brief ENTIRELY and does not appear in the body. "REVISED" means a corrected version of the claim REMAINS in the body. If a claim is still in the brief and you only changed its wording, its label, or its source attribution, that is REVISED — never CUT. Before writing each entry, check: is this claim still in the brief body? If yes, it is REVISED. If no, it is CUT. Mislabeling a kept claim as "CUT" is an error.
Each entry must be concrete enough that a skeptical reader can see exactly what was caught. Keep each entry to a single line (no line breaks inside an entry). Put no blank lines between entries. Do not add any other commentary after the Cut Log. If genuinely nothing was cut or revised, write the single bullet: "- No claims required cutting or revision — every claim in the draft verified cleanly against a reliable source."

FINAL CONSISTENCY SWEEP (mandatory — do this silently before you output the brief):
Re-read your finished brief as a whole and reconcile any internal contradictions before writing it out. Specifically:
- The same company, product, or model VERSION must be named identically in every section. If "Claude Opus 4.8" appears in one place and "Opus 4.7" in another, pick the one your most recent verified source supports and make it consistent everywhere.
- Any "current", "latest", or "flagship" designation must resolve to ONE value, supported by your freshest source.
- The same metric must never appear with two different numbers unless you explicitly explain why they differ.
- Every claim that survives into the body carries a real source link, and nothing in the Cut Log is also asserted as fact in the body.
A brief that contradicts itself is worse than one that says less. Internal consistency is non-negotiable.

DRAFT TO VERIFY:
{draft}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )
    return _from_title(_extract(response))


def save_report(text, target, perspective=None):
    os.makedirs("reports", exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    label = f"{perspective}_vs_{target}" if perspective else target
    label = label.replace(" ", "-").replace("/", "-")
    path = f"reports/{label}_{stamp}.md"
    with open(path, "w") as f:
        f.write(text)
    return path


def research_competitor(target, perspective=None, product=None):
    print("Researching and drafting brief...")
    draft = generate_brief(target, perspective, product)
    print("Verifying claims against sources (this is the slow part)...")
    final = verify_brief(draft)
    path = save_report(final, target, perspective)
    print(f"Report saved to: {path}")
    return final


if __name__ == "__main__":
    result = research_competitor("Slack", perspective="Microsoft Teams")
    print("\n" + "=" * 60 + "\n")
    print(result)
