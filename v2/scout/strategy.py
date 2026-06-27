"""Strategic-impact pass (spec §17, strategy layer): a rep-minded Opus selection of the single most
strategic argument a brief should LEAD with right now.

Fact-level propagation patches individual claims. This sits one layer above and asks the question
those patches keep missing: given everything in the brief, what is the most consequential point to
open with to win the deal? It weighs IMPACT and FRESHNESS together (a day-old structural shift can
outrank an evergreen pitch, but mere recency never outranks a more decisive point), proposes an
actual thesis, and stress-tests it against the buyer's strongest counter before returning it.

Reuses the EXISTING production Opus judge (config.ORCHESTRATOR_MODEL) and its tools-off driver +
budget guards — no new model, no new judge. Intended trigger: an ACT-grade material change on a card
(the same moment propagation fires), so quiet runs cost nothing. One call ≈ $0.27 on Opus.
"""
import asyncio
import json

from claude_agent_sdk import ClaudeAgentOptions

from scout import config, store
from scout.generate import _drive, _extract_json

STRATEGIC_SYSTEM = """You are a top sales rep for {me}, selling against {comp}{w}. You are looking at your
own competitive battlecard's claims. Pick THE single most strategic argument to LEAD with right now to win
the customer.

Selection rule — weigh IMPACT and FRESHNESS together:
- Do not let a stale claim (months old) override a genuinely new, decisive development.
- Do not let mere recency override a point that is more strategically decisive.
- The winner is the argument that most changes WHY a buyer chooses you, given what is true now.

Then STRESS-TEST your pick: state the strongest counter a sharp buyer would raise and whether your thesis
survives it. If it does not clearly survive, choose a better argument and stress-test that one instead.

Writing style: plain and direct. No em dashes, no rule-of-three, no hype.

Output JSON only:
{{"thesis":"<one tight paragraph: the lead argument the rep opens with>",
"soundbite":"<one sentence the rep can say out loud>",
"based_on":["<subject_key(s) this is built on>"],
"why_most_strategic":"<why this beats the other candidates on impact x freshness>",
"freshness_note":"<how recent the driving evidence is, and whether recency decided it>",
"stress_test":{{"strongest_counter":"<...>","survives":true,"how":"<why it holds>"}},
"supersedes":"<the argument the card currently leads with that this would replace, or 'none'>"}}"""


def _digest(claims: list[dict]) -> list[dict]:
    """Active claims with their dates, so the model can weigh freshness. Retired claims are excluded."""
    out = []
    for c in claims:
        if c.get("status") == "retired":
            continue
        out.append({"subject_key": c.get("subject_key"), "section": c.get("section"),
                    "as_of": c.get("as_of"), "updated_on": c.get("updated_on"),
                    "claim": (c.get("claim") or "")[:500]})
    return out


async def _run(meta: dict, claims: list[dict]) -> dict:
    me, comp = meta.get("my_company") or "us", meta.get("competitor") or "the competitor"
    focus = meta.get("focus") or meta.get("focus_area")
    w = f" in {focus}" if focus else ""
    system = STRATEGIC_SYSTEM.format(me=me, comp=comp, w=w)
    user = (f"We are: {me}.   Competitor: {comp}.{(' Focus: ' + focus) if focus else ''}\n\n"
            "BRIEF CLAIMS (with dates so you can weigh freshness):\n"
            + json.dumps(_digest(claims), ensure_ascii=False, indent=2))
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,                  # the existing production Opus judge model
        system_prompt={"type": "preset", "preset": "claude_code", "append": system},
        mcp_servers={}, allowed_tools=[], disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.JUDGE_MAX_TURNS, max_budget_usd=config.JUDGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "strategic_lead")


def strategic_lead(meta: dict, claims: list[dict]) -> dict:
    """Run the strategic-lead selection over a brief's claims. Returns {lead: {...}|None, cost_usd,
    raw}. `lead` is the parsed proposal (thesis/soundbite/stress_test/supersedes/...) or None if the
    model returned nothing parseable. Never raises on a parse miss."""
    res = asyncio.run(_run(meta, claims))
    cost = res.get("cost_usd") or 0.0
    try:
        lead = _extract_json(res["text"])
    except Exception:
        lead = None
    return {"lead": lead, "cost_usd": cost, "raw": res.get("text", "")}


if __name__ == "__main__":
    import sys
    slugs = sys.argv[1:]
    if not slugs:
        print("usage: python -m scout.strategy <slug> [<slug> ...]")
        sys.exit(1)
    total = 0.0
    for slug in slugs:
        meta = store.load_meta(slug) or {}
        claims = store.load_claims(slug)
        card = f"{meta.get('my_company')} vs {meta.get('competitor')}" if meta.get("my_company") else slug
        print("\n" + "=" * 70 + f"\n{card}\n" + "=" * 70)
        r = strategic_lead(meta, claims)
        total += r["cost_usd"]
        print(f"(opus cost ${r['cost_usd']:.3f})")
        lead = r["lead"]
        if not lead:
            print("  no parseable proposal:\n", r["raw"][:800])
            continue
        print("THESIS:    ", lead.get("thesis"))
        print("SOUNDBITE: ", lead.get("soundbite"))
        print("WHY:       ", lead.get("why_most_strategic"))
        print("FRESHNESS: ", lead.get("freshness_note"))
        st = lead.get("stress_test") or {}
        print("STRESS:    ", f"counter={st.get('strongest_counter')} | survives={st.get('survives')} | {st.get('how')}")
        print("SUPERSEDES:", lead.get("supersedes"))
    print(f"\n--- total opus cost: ${total:.3f} ---")
