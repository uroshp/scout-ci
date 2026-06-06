"""One-off backfill: assign a `persona` to every battlecard + objection_handling
claim that's missing one. The hero card (anthropic/openai) was generated with
personas; the 6 batch cards were not (the field was optional and the model
omitted it). This classifies the eligible claims with Haiku and writes the
persona back into claims.json — presentational only, no grounding impact.

Run from v2/:  python scripts/backfill_personas.py [--write] [slug ...]
Without --write it prints proposed assignments and changes nothing.
"""
import json
import os
import sys

import anthropic

from scout import config, display, store

PERSONAS = {
    "eng_led": "Eng-led champion — a developer/engineer championing the tool bottom-up.",
    "technical_evaluator": "Technical evaluator — hands-on assessment of capability, "
                           "architecture, accuracy, model/infra depth.",
    "economic_buyer": "Economic buyer — owns budget/ROI/cost; cares about price, TCO, consolidation.",
    "security_regulated": "Security & regulated — security, compliance, data governance, "
                          "privacy, regulated-industry constraints.",
    "exec_top_down": "Exec / top-down — executive/strategic buyer driving a top-down mandate, "
                     "platform standardization, vendor strategy.",
}

TOOL = {
    "name": "assign_personas",
    "description": "Return one persona per claim id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "persona": {"type": "string", "enum": list(PERSONAS)},
                    },
                    "required": ["id", "persona"],
                },
            }
        },
        "required": ["assignments"],
    },
}

SYSTEM = (
    "You are tagging competitive-battlecard plays and objection-handling entries with the "
    "single buyer persona each is primarily aimed at (or that tends to raise the objection). "
    "Choose the SINGLE best fit. Personas:\n"
    + "\n".join(f"- {k}: {v}" for k, v in PERSONAS.items())
    + "\nReturn exactly one persona for every claim id provided, via the assign_personas tool."
)


def eligible(claims):
    return [c for c in claims if c.get("section") in ("battlecard", "objection_handling")]


def classify(client, claims):
    items = [
        {"id": c["id"], "section": c["section"], "zone": c.get("zone"),
         "text": (c.get("claim") or "")[:600]}
        for c in claims
    ]
    msg = client.messages.create(
        model=config.FAST_MODEL,
        max_tokens=2000,
        system=SYSTEM,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "assign_personas"},
        messages=[{"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "assign_personas":
            return {a["id"]: a["persona"] for a in block.input["assignments"]}
    return {}


def main():
    argv = sys.argv[1:]
    write = "--write" in argv
    slugs = [a for a in argv if not a.startswith("--")] or display.list_battlecards()
    client = anthropic.Anthropic()

    for slug in slugs:
        path = store.paths(slug)["claims"] if hasattr(store, "paths") else \
            os.path.join(store.battlecard_dir(slug), "claims.json")
        claims = json.load(open(path))
        elig = eligible(claims)
        missing = [c for c in elig if not c.get("persona")]
        if not missing:
            print(f"{slug}: all {len(elig)} eligible claims already tagged — skip")
            continue
        assigned = classify(client, missing)
        n = 0
        for c in claims:
            if c["id"] in assigned and not c.get("persona"):
                c["persona"] = assigned[c["id"]]
                n += 1
        print(f"\n{slug}: tagged {n}/{len(missing)} missing "
              f"({len(elig)} eligible total)")
        for c in claims:
            if c["id"] in assigned and assigned.get(c["id"]):
                title = (c.get("claim") or "").strip().splitlines()[0][:70]
                print(f"   {assigned[c['id']]:<20} {c['section']:<18} {title}")
        if write:
            with open(path, "w") as f:
                json.dump(claims, f, indent=2, ensure_ascii=False)
            print(f"   -> wrote {path}")
    if not write:
        print("\n(dry run — pass --write to persist)")


if __name__ == "__main__":
    main()
