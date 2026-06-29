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
from scout.prompts import WRITING_STYLE

STRATEGIC_SYSTEM = """You are a top sales rep for {me}, selling against {comp}{w}. From your battlecard's
claims, pick THE single most strategic argument to LEAD with right now to win the customer.

Weigh IMPACT and FRESHNESS together: a stale claim must not override a genuinely new, decisive development,
and mere recency must not override a more decisive point. The winner most changes WHY a buyer chooses you,
given what is true now.

A sales rep skims for ten seconds. The LEAD you write must be SHORT and scannable, four tight parts:
- headline: the angle in ONE line.
- proof: ONE short sentence with the single most convincing fact or stat. No list, no second sentence.
- soundbite: ONE line the rep can say out loud. It MUST be advice the buyer would actually act on.
  The value is choice and control; never imply churn, constant switching, or redoing work (a line like
  "change your model every week" is lazy and would lose the room). Say what the buyer GETS and keeps.
- move: ONE line on what the rep should do next, a concrete action. Do NOT write "frame it as X versus Y"
  or any A-versus-B mirror; say the actual thing to do.

Separately, give the REASONING (for the human approving the lead, NOT shown to the rep, so it can be
fuller): why this beats the alternatives, the freshness note, a stress-test (the strongest buyer counter
and whether the lead survives it), and which current lead it supersedes.

Also judge CONSEQUENTIALITY: does the freshest driving development actually change what the rep DOES or
the brief's thesis (consequential), or is it a routine fact update that keeps the card accurate without
changing the argument (routine)? An OpenAI IPO, a daily box-office number, or "the vendor's own
employees can now access the model" are routine. The US government restricting frontier models, or a
thaw that restores CUSTOMER access, is consequential. When unsure, mark it consequential.

Every line you write must pass the WRITING STYLE rules given above, especially the FINAL CHECK.

Output JSON only:
{{"headline":"<the angle, one line>",
"proof":"<one short sentence, the single killer fact/stat>",
"soundbite":"<one line to say out loud>",
"move":"<one line: what to do next>",
"consequential":<true if the freshest development changes the rep's play or the thesis; false if it is a routine fact update>,
"consequence_rationale":"<one line: why it does or does not change the play>",
"why_most_strategic":"<why this beats the other candidates on impact x freshness>",
"freshness_note":"<how recent the driving evidence is, and whether recency decided it>",
"stress_test":{{"strongest_counter":"<...>","survives":true,"how":"<why it holds>"}},
"supersedes":"<the current lead this would replace, or 'none'>"}}"""


def claim_text(lead: dict) -> str:
    """Build the compact executive-summary lead claim from a strategic-pass result: bold headline, one
    proof line, a Soundbite block, a So-what (the move). This is the rep-facing card form — short."""
    sb = str(lead.get("soundbite", "")).strip().strip('"')
    return (f"**{str(lead.get('headline','')).strip()}**\n\n"
            f"{str(lead.get('proof','')).strip()}\n\n"
            f'**Soundbite:** "{sb}"\n\n'
            f"**So what:** {str(lead.get('move','')).strip()}")


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
    system = WRITING_STYLE + "\n\n" + STRATEGIC_SYSTEM.format(me=me, comp=comp, w=w)
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
        print("HEADLINE: ", lead.get("headline"))
        print("PROOF:    ", lead.get("proof"))
        print("SAY:      ", lead.get("soundbite"))
        print("MOVE:     ", lead.get("move"))
        print("WHY:      ", lead.get("why_most_strategic"))
        print("FRESHNESS:", lead.get("freshness_note"))
        st = lead.get("stress_test") or {}
        print("STRESS:   ", f"counter={st.get('strongest_counter')} | survives={st.get('survives')} | {st.get('how')}")
        print("SUPERSEDES:", lead.get("supersedes"))
    print(f"\n--- total opus cost: ${total:.3f} ---")
