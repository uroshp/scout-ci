"""Ask Scout (WS2, 2026-09-28): a goal-based loop that answers a competitor question with prose that
rests on nothing but code-verified facts, and says plainly what it could not verify.

The loop, and who owns each step (the control-vs-model line):

  1. scope     code    the card the question is about; its grounded FACTS are the first evidence
  2. research  model   tools-on (WebSearch, fetch_page, scoutsources): new facts in the claim shape
                       + a draft answer whose every sentence cites fact ids
  3. gates     code    ASK_FACT_SCHEMA, source class, then grounding.ground_claims: a new fact that
                       is not on its page is CUT (Cut Log), never argued with
  4. floor     code    every cite resolves to a surviving fact; every number / percent / money /
                       year in a sentence appears in the union of its cited excerpts (scale-aware:
                       "$11.3 billion" is supported by "value=11345000000"); zero-cite sentences drop
  5. verify    model   tools-off judge: is each surviving sentence SUPPORTED by what it cites?
                       confirm | reject + cure, fail-closed (unparseable = reject)
  6. rewrite   model   ONE rewrite of the sentences the judge rejected with cure=prose, citing only
                       survivors, then floor, then a SECOND judge call on those; else dropped
  7. output    code    the confirmed sentences, their sources with class chips, the Cut Log, the
                       topics it could not verify (digits scrubbed), cost, and the trajectory

STOP CONDITION (the goal): every remaining sentence passed the floor AND the judge confirmed it.
The evaluator decides done, not the writer. A model may cut, never add, and the floor runs before
every judge, so no ungrounded model judgment sits in the seat of final authority.

Money: the research call is capped at ASK_RESEARCH_BUDGET_USD, each judge call at
ASK_VERIFY_BUDGET_USD, the rewrite at ASK_REWRITE_BUDGET_USD (all native max_budget_usd on the
Agent SDK); ask() refuses to start above ASK_MAX_USD. Every call goes through generate._drive, so
calllog captures it and the on-device lane can replay it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date, datetime

from scout import config, display, store
from scout.grounding import _normalize, ground_claims
from scout.prompts import SOURCE_HIERARCHY, WRITING_STYLE
from scout.sources import classify

ASK_RESEARCH_BUDGET_USD = config.ASK_RESEARCH_BUDGET_USD
ASK_VERIFY_BUDGET_USD = config.ASK_VERIFY_BUDGET_USD
ASK_REWRITE_BUDGET_USD = config.ASK_REWRITE_BUDGET_USD
ASK_MAX_USD = config.ASK_MAX_USD
ASK_RESEARCH_MAX_TURNS = 8
MAX_ROUNDS = 2
MAX_CARD_FACTS = 40
ASK_DIR = "ask"

ANSWER_CONTRACT = """You are Scout, a competitive-intelligence analyst who answers ONE question with prose that
rests on verified facts only. Read the KNOWN FACTS first (already verified; cite them by id). Search
and fetch only for what they do not cover. Then return ONLY a single fenced ```json block:

{"facts": [ <new facts you found, each in the CLAIM CONTRACT shape below, with an "id" you assign
            like "n1", "n2"; "claim_type" must be "fact"; section "recent_moves"; zone null; order 0> ],
 "answer": [ {"text": "<one sentence or short paragraph>", "cites": ["<fact id>", ...]}, ... ],
 "unanswered": ["<a topic the question asked about that no source you could verify covers>", ...]}

RULES. Every answer entry cites at least one fact id (a KNOWN FACT id like "c_..." or one of your new
"n" ids); an entry with no cite will be deleted by code. Every number, percentage, money amount, and
year in an entry must appear in the evidence_excerpt of a fact it cites; code checks this and deletes
what fails. Never paraphrase a filing figure: the sec_fact tool gives you the exact line to copy as an
evidence_excerpt. If the question asks for something you cannot verify, put the TOPIC in
"unanswered" (no numbers) instead of guessing. Do not pad: three tight, well-cited entries beat eight.
Write for the reader named in CONTEXT when one is given.
"""

VERIFY_SYSTEM = """You are the VERIFIER of an answer written from verified facts. You have no tools, on purpose:
judge ONLY against the facts given. For EACH numbered sentence decide whether it is SUPPORTED by the
facts it cites: every claim it makes must be licensed by the cited facts' text and excerpts, at their
LITERAL scope. REJECT a sentence if it asserts a number, entity, date, mechanism, motive or causal
link the cited facts do not state; if it generalizes a scoped fact; or if it cites nothing relevant.
Default to reject when not convinced. On a reject, name the cure: "prose" when the point is right but
the wording overreaches (a rewrite citing the same facts can fix it), "root" when the point itself is
not in the facts, "none" when nothing citable supports it. Your reason is the only feedback the
rewriter gets: name the complete fix.

Return ONLY a single fenced ```json block:
{"verdicts": [{"op_index": <sentence number>, "verdict": "confirm|reject", "material": true,
               "cure": "prose|root|none", "reason": "<complete diagnosis>"}]}
"""

REWRITE_SYSTEM = """Rewrite ONLY the sentences listed, so each says no more than the facts it cites state, keeping the
same cites (drop a cite only if you also drop what it supported). Return ONLY a fenced ```json block:
{"answer": [{"index": <sentence number>, "text": "<rewritten>", "cites": [...]}]}
"""

_FACT_KEYS = ("id", "claim", "source_url", "source_tier", "evidence_excerpt", "as_of")
_TIERS = ("primary", "reputable_secondary", "sentiment_only")


# --- 1. scope ------------------------------------------------------------------------------------------
def _find_slug(competitor: str | None, my_company: str | None = None) -> str | None:
    if not competitor:
        return None
    want = classify._company_tokens(competitor)
    for slug in display.list_battlecards():
        meta = store.load_meta(slug) or {}
        if want & classify._company_tokens(meta.get("competitor")):
            if not my_company or classify._company_tokens(my_company) & classify._company_tokens(meta.get("my_company")):
                return slug
    return None


def card_facts(slug: str | None, limit: int = MAX_CARD_FACTS) -> list[dict]:
    """The card's grounded FACTS (own source, grounding.match), newest first. Interpretations (plays,
    objections, summaries) are NOT evidence here (C19): they were judged once by a model."""
    if not slug:
        return []
    meta = store.load_meta(slug) or {}
    out = []
    for c in store.load_claims(slug):
        if c.get("status") == "retired" or c.get("claim_type") != "fact":
            continue
        if not (c.get("source_url") and c.get("evidence_excerpt") and (c.get("grounding") or {}).get("match")):
            continue
        d = {k: c.get(k) for k in _FACT_KEYS}
        d["subject_key"] = c.get("subject_key")
        d["source_class"] = c.get("source_class") or classify.classify(c["source_url"], meta.get("competitor"), meta.get("my_company"))
        out.append(d)
    out.sort(key=lambda f: str(f.get("as_of") or ""), reverse=True)
    return out[:limit]


# --- 3. gates -----------------------------------------------------------------------------------------
def fact_errors(f: dict) -> list[str]:
    """ASK_FACT_SCHEMA (C12): the few fields grounding needs, not the card's render contract."""
    errs = []
    if not isinstance(f, dict):
        return ["not an object"]
    if not str(f.get("id") or "").strip():
        errs.append("missing id")
    if not str(f.get("claim") or "").strip():
        errs.append("missing claim")
    u = str(f.get("source_url") or "")
    if not u.startswith(("http://", "https://")):
        errs.append("source_url must be http(s)")
    if f.get("source_tier") not in _TIERS:
        errs.append("source_tier not in " + "|".join(_TIERS))
    if len(str(f.get("evidence_excerpt") or "")) < 40:
        errs.append("evidence_excerpt shorter than 40 chars")
    if f.get("claim_type", "fact") != "fact":
        errs.append("claim_type must be fact")
    if f.get("source_tier") == "sentiment_only":
        errs.append("a fact may not rest on a sentiment-only source")
    return errs


def gate_facts(new_facts: list, meta: dict | None = None, grounder=ground_claims) -> tuple[list, list]:
    """(surviving new facts, cut log entries). Schema first, then class, then the grounding check."""
    ok, cut = [], []
    for f in new_facts or []:
        errs = fact_errors(f)
        if errs:
            cut.append({"label": str((f or {}).get("id") or "?"), "reason": "malformed fact: " + "; ".join(errs),
                        "source_url": (f or {}).get("source_url")})
            continue
        f = {k: f.get(k) for k in (*_FACT_KEYS, "subject_key")}
        f["claim_type"] = "fact"
        classify.stamp(f, meta)
        ok.append(f)
    if not ok:
        return [], cut
    g = grounder(ok)
    kept = {c["id"]: c for c in g["kept"]}
    for f in ok:
        if f["id"] not in kept:
            r = next((x for x in g["results"] if x.get("claim_id") == f["id"]), {})
            cut.append({"label": f["id"], "reason": f"not on the cited page ({r.get('status', 'absent')}: {r.get('detail') or 'excerpt not found'})",
                        "source_url": f["source_url"]})
    return [kept[f["id"]] for f in ok if f["id"] in kept], cut


# --- 4. floor -----------------------------------------------------------------------------------------
_NUM = re.compile(r"(?<![\w.])(\$|€|£)?\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:[\s-]*(%|(?:percent|billion|million|thousand|bn|mn|m|b|k|x)\b))?(?![\w.])", re.I)
_CITE_MARK = re.compile(r"\[(?:n\d+|c_[0-9a-f]{12}|\d{1,2})(?:,\s*(?:n\d+|c_[0-9a-f]{12}|\d{1,2}))*\]")
_SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


def _numbers(text: str) -> list[tuple[float, int, str]]:
    """[(value, significant figures as written, kind)] for every numeric token; kind = pct | money |
    plain. "11.3 billion" -> (1.13e10, 3, plain); "$22.5B" -> (2.25e10, 3, money); "22.4%" -> (22.4, 3, pct)."""
    out = []
    for m in _NUM.finditer(text or ""):
        cur, whole, frac, suf = m.group(1), m.group(2), m.group(3), (m.group(4) or "").lower()
        digits = whole.replace(",", "")
        raw = float(digits + ("." + frac if frac else ""))
        sig = len(digits.lstrip("0") or "0") + len(frac or "")
        if suf in ("%", "percent"):
            out.append((raw, sig, "pct"))
            continue
        if suf == "x":
            out.append((raw, sig, "plain"))
            continue
        scale = _SCALE.get(suf, 1)
        out.append((raw * scale, sig, "money" if cur else "plain"))
    return out


def _supported(value: float, sig: int, kind: str, evidence_nums: list) -> bool:
    """A sentence number is supported when some evidence number rounds to it at the sentence's own
    precision: "11.3 billion" (3 sig figs) <- 11,345,000,000; "22%" <- 22.4%; "11.9 billion" is NOT
    supported by 11,345,000,000."""
    if value == 0:
        return any(v == 0 for v, _, _ in evidence_nums)
    mag = 10 ** (len(str(int(abs(value)))) - 1)          # 1.13e10 -> 1e10
    unit = mag / (10 ** max(sig - 1, 0))                 # 3 sig figs -> 1e8
    for ev, _, ek in evidence_nums:
        if kind == "pct" and ek != "pct":
            continue
        if ev == value:
            return True
        if unit > 0 and round(ev / unit) == round(value / unit):
            return True
    return False


def floor_check(entry: dict, facts_by_id: dict) -> list[str]:
    """Model-free: cites resolve to surviving facts; every number / percent / money amount / year in
    the text appears in the cited excerpts. Returns violations ([] = passes)."""
    text = str(entry.get("text") or "")
    cites = [str(c) for c in (entry.get("cites") or []) if str(c) in facts_by_id]
    if not text.strip():
        return ["empty"]
    if not cites:
        return ["no cite resolves to a surviving fact"]
    evidence = " ".join(_normalize(str(facts_by_id[c].get("evidence_excerpt") or "")) + " " + _normalize(str(facts_by_id[c].get("claim") or ""))
                        for c in cites)
    ev_nums = _numbers(evidence)
    errs = []
    text = _CITE_MARK.sub(" ", text)                 # inline [1] / [n2] markers are not numbers
    years = {m.group(0) for m in _YEAR.finditer(text)}
    for v, dec, kind in _numbers(text):
        if kind == "plain" and v == int(v) and str(int(v)) in years:
            continue                                     # years are checked literally below
        if not _supported(v, dec, kind, ev_nums):
            errs.append(f"number {v:g}{'%' if kind == 'pct' else ''} not in the cited evidence")
    for y in years:
        if y not in evidence:
            errs.append(f"year {y} not in the cited evidence")
    return errs


# --- 5/6. model calls (injectable) -------------------------------------------------------------------
def _json_or_none(text: str):
    from scout.generate import _extract_json
    try:
        return _extract_json(text or "")
    except Exception:
        return None


def _digest(facts: list) -> list:
    return [{"id": f["id"], "claim": f.get("claim"), "source_url": f.get("source_url"), "source_class": f.get("source_class"),
             "source_tier": f.get("source_tier"), "as_of": f.get("as_of"), "evidence_excerpt": f.get("evidence_excerpt")} for f in facts]


def research_call(question: str, known: list, context: str | None) -> dict:
    """The tools-on research pass (role ask_research). Returns _drive's dict (text, cost_usd, ...)."""
    from claude_agent_sdk import ClaudeAgentOptions
    from scout.fetch_tool import FETCH_SERVER, FETCH_TOOL_NAME
    from scout.generate import CLAIM_CONTRACT, _drive
    from scout import sources_tool
    system = ANSWER_CONTRACT + "\n\nCLAIM CONTRACT (for each NEW fact):\n" + CLAIM_CONTRACT + "\n\n" + SOURCE_HIERARCHY \
        + sources_tool.PROMPT_NOTE + "\n\n" + WRITING_STYLE
    user = (f"QUESTION: {question}\n" + (f"CONTEXT: {context}\n" if context else "") + "\nKNOWN FACTS (verified; cite by id):\n"
            + json.dumps(_digest(known), ensure_ascii=False, indent=1))
    options = ClaudeAgentOptions(
        model=config.SUBAGENT_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code", "append": system},
        mcp_servers={"scoutfetch": FETCH_SERVER, "scoutsources": sources_tool.SOURCES_SERVER},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME, *sources_tool.SOURCES_TOOL_NAMES],
        disallowed_tools=["WebFetch"], permission_mode="bypassPermissions",
        max_turns=ASK_RESEARCH_MAX_TURNS, max_budget_usd=ASK_RESEARCH_BUDGET_USD)
    return asyncio.run(_drive(user, options, "ask_research"))


def _judge_options(system: str, budget: float, model: str | None = None):
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(model=model or config.ORCHESTRATOR_MODEL, system_prompt=system, mcp_servers={},
                              allowed_tools=[], disallowed_tools=["WebSearch", "WebFetch"], permission_mode="bypassPermissions",
                              max_turns=config.JUDGE_MAX_TURNS, max_budget_usd=budget)


def verify_call(entries: list, facts_by_id: dict) -> dict:
    """The tools-off judge (role ask_verify) on the numbered sentences."""
    from scout.generate import _drive
    cited = sorted({c for e in entries for c in e.get("cites", []) if c in facts_by_id})
    user = ("FACTS (the only admissible evidence):\n" + json.dumps(_digest([facts_by_id[c] for c in cited]), ensure_ascii=False, indent=1)
            + "\n\nSENTENCES TO JUDGE:\n" + json.dumps([{"op_index": i, "text": e["text"], "cites": e.get("cites", [])} for i, e in enumerate(entries)],
                                                        ensure_ascii=False, indent=1))
    return asyncio.run(_drive(user, _judge_options(VERIFY_SYSTEM, ASK_VERIFY_BUDGET_USD), "ask_verify"))


def rewrite_call(entries: list, verdicts: dict, facts_by_id: dict) -> dict:
    from scout.generate import _drive
    todo = [{"index": i, "text": e["text"], "cites": e.get("cites", []), "judge_reason": verdicts[i]["reason"]}
            for i, e in enumerate(entries) if i in verdicts]
    cited = sorted({c for t in todo for c in t["cites"] if c in facts_by_id})
    user = ("FACTS:\n" + json.dumps(_digest([facts_by_id[c] for c in cited]), ensure_ascii=False, indent=1)
            + "\n\nREWRITE THESE:\n" + json.dumps(todo, ensure_ascii=False, indent=1))
    return asyncio.run(_drive(user, _judge_options(REWRITE_SYSTEM + "\n\n" + WRITING_STYLE, ASK_REWRITE_BUDGET_USD, config.SUBAGENT_MODEL), "ask_rewrite"))


# --- the loop ------------------------------------------------------------------------------------------
def _scrub_digits(s: str) -> str:
    return re.sub(r"[\$€£]?\d[\d,.]*(?:\s*(?:%|(?:percent|billion|million|thousand|bn|mn|[mbk])\b))?", "a figure", str(s or ""), flags=re.I).strip()


def ask(question: str, *, competitor: str | None = None, my_company: str | None = None, context: str | None = None,
        persona: str | None = None, slug: str | None = None, research=research_call, verify=verify_call, rewrite=rewrite_call,
        grounder=ground_claims, persist: bool = False) -> dict:
    t0 = datetime.now()
    slug = slug or _find_slug(competitor, my_company)
    meta = (store.load_meta(slug) or {}) if slug else {"competitor": competitor, "my_company": my_company}
    known = card_facts(slug)
    ctx = " ".join(x for x in [context, f"Reader: {persona.replace('_', ' ')} buyer." if persona else ""] if x) or None
    cost = 0.0
    trajectory = {"rounds": 0, "research_turns": None, "cut": 0, "floor_dropped": 0, "judge_rejected": 0, "rewritten": 0}
    cut_log: list = []

    # 2. research
    r = research(question, known, ctx)
    cost += float(r.get("cost_usd") or 0.0)
    trajectory["research_turns"] = r.get("num_turns")
    data = _json_or_none(r.get("text") or "") or {}
    new_facts = data.get("facts") if isinstance(data.get("facts"), list) else []
    entries = [e for e in (data.get("answer") if isinstance(data.get("answer"), list) else []) if isinstance(e, dict)]
    unanswered = [_scrub_digits(u) for u in (data.get("unanswered") or []) if isinstance(u, str) and u.strip()]

    # 3. gates
    survivors, cut = gate_facts(new_facts, meta, grounder)
    cut_log.extend(cut)
    trajectory["cut"] = len(cut)
    facts_by_id = {f["id"]: f for f in known}
    facts_by_id.update({f["id"]: f for f in survivors})

    # 4. floor, 5. verify, 6. one rewrite + second judge
    confirmed: list = []
    pending = [dict(e, text=str(e.get("text") or ""), cites=[str(c) for c in (e.get("cites") or [])]) for e in entries]
    for rnd in range(1, MAX_ROUNDS + 1):
        trajectory["rounds"] = rnd
        passed, floor_failed = [], []
        for e in pending:
            errs = floor_check(e, facts_by_id)
            if errs:
                trajectory["floor_dropped"] += 1
                if rnd < MAX_ROUNDS and any(c in facts_by_id for c in e.get("cites", [])):
                    floor_failed.append((e, "floor: " + "; ".join(errs)))   # one rewrite may drop the unsupported bit
                else:
                    cut_log.append({"label": e["text"][:80], "reason": "floor: " + "; ".join(errs)})
            else:
                passed.append(e)
        verdicts = {}
        if passed:
            v = verify(passed, facts_by_id)
            cost += float(v.get("cost_usd") or 0.0)
            from scout.propagate import _parse_verdicts
            verdicts = _parse_verdicts(v.get("text") or "")
        to_rewrite = {}
        for i, e in enumerate(passed):
            vd = verdicts.get(i)
            if vd and vd.get("verdict") == "confirm":
                confirmed.append(e)
            else:
                trajectory["judge_rejected"] += 1
                reason = (vd or {}).get("reason") or "no parseable verdict (fail-closed)"
                if rnd < MAX_ROUNDS and vd and vd.get("cure") == "prose":
                    to_rewrite[i] = {"reason": reason}
                else:
                    cut_log.append({"label": e["text"][:80], "reason": "verifier: " + reason})
        # floor failures ride the same rewrite, numbered after the judged sentences
        for j, (e, why) in enumerate(floor_failed):
            passed.append(e)
            to_rewrite[len(passed) - 1] = {"reason": why + ". Remove or correct the unsupported figure; keep only what the cited facts state."}
        if not to_rewrite:
            break
        rw = rewrite(passed, to_rewrite, facts_by_id)
        cost += float(rw.get("cost_usd") or 0.0)
        rdata = _json_or_none(rw.get("text") or "") or {}
        pending, returned = [], set()
        for item in (rdata.get("answer") if isinstance(rdata.get("answer"), list) else []):
            if isinstance(item, dict) and item.get("index") in to_rewrite and str(item.get("text") or "").strip():
                trajectory["rewritten"] += 1
                returned.add(item["index"])
                pending.append({"text": str(item["text"]), "cites": [str(c) for c in (item.get("cites") or [])]})
        for i in to_rewrite:
            if i not in returned:
                cut_log.append({"label": passed[i]["text"][:80], "reason": "verifier: " + to_rewrite[i]["reason"] + " (no rewrite returned)"})
    cited_ids = []
    for e in confirmed:
        for c in e["cites"]:
            if c in facts_by_id and c not in cited_ids:
                cited_ids.append(c)
    sources = [{"n": i + 1, "id": c, "url": facts_by_id[c].get("source_url"), "class": facts_by_id[c].get("source_class"),
                "tier": facts_by_id[c].get("source_tier"), "as_of": facts_by_id[c].get("as_of"),
                "excerpt": facts_by_id[c].get("evidence_excerpt"), "from_card": c in {f["id"] for f in known}} for i, c in enumerate(cited_ids)]
    num = {s["id"]: s["n"] for s in sources}
    answer = {
        "id": "a_" + hashlib.sha256(f"{question}|{slug}|{t0.isoformat()}".encode()).hexdigest()[:12],
        "question": question, "slug": slug, "competitor": meta.get("competitor") or competitor, "context": ctx,
        "asked_at": t0.isoformat(timespec="seconds"), "seconds": round((datetime.now() - t0).total_seconds(), 1),
        "paragraphs": [{"text": e["text"], "cites": [num[c] for c in e["cites"] if c in num]} for e in confirmed],
        "sources": sources, "cut_log": cut_log, "unanswered": unanswered,
        "verified": bool(confirmed), "cost_usd": round(cost, 4), "trajectory": trajectory,
        "models": {"research": config.SUBAGENT_MODEL, "verify": config.ORCHESTRATOR_MODEL},
    }
    if persist:
        _persist(answer)
    return answer


def _persist(answer: dict) -> str | None:
    from scout import selfserve
    try:
        path = f"{ASK_DIR}/{answer['asked_at'][:7]}/{answer['id']}.json"
        selfserve.write_data(path, json.dumps(answer, indent=1, ensure_ascii=False), f"ask: {answer['id']}")
        return path
    except Exception as e:
        print(f"[ask] persist skipped ({type(e).__name__}: {e})")
        return None


def render_text(a: dict) -> str:
    lines = [f"Q: {a['question']}" + (f"  (about {a['competitor']})" if a.get("competitor") else ""), ""]
    if not a["paragraphs"]:
        lines.append("Scout could not verify an answer to this question.")
    for p in a["paragraphs"]:
        lines.append(p["text"] + " " + "".join(f"[{n}]" for n in p["cites"]))
        lines.append("")
    if a["sources"]:
        lines.append("Sources")
        for s in a["sources"]:
            lines.append(f"  [{s['n']}] {s['url']}  ({classify.CLASS_LABEL.get(s.get('class'), 'Web')}, {s.get('tier')}, as of {s.get('as_of')})")
    if a["unanswered"]:
        lines.append("Could not verify: " + "; ".join(a["unanswered"]))
    if a["cut_log"]:
        lines.append("Cut log")
        for c in a["cut_log"]:
            lines.append(f"  CUT {c['label']}: {c['reason']}")
    t = a["trajectory"]
    lines.append(f"verified in {a['seconds']} s · {len(a['sources'])} sources · {t['cut'] + t['floor_dropped'] + t['judge_rejected']} cut · ${a['cost_usd']:.2f}")
    return "\n".join(lines)


def main(argv=None):
    import argparse
    from scout import calllog
    ap = argparse.ArgumentParser(description="Ask Scout a question about a competitor (spends money: ~$1-2 per question).")
    ap.add_argument("question")
    ap.add_argument("--competitor")
    ap.add_argument("--my-company")
    ap.add_argument("--context")
    ap.add_argument("--persona", choices=["eng_led", "technical_evaluator", "economic_buyer", "security_regulated", "exec_top_down"])
    ap.add_argument("--slug")
    ap.add_argument("--persist", action="store_true", help="write the answer record to the private store")
    ap.add_argument("--yes", action="store_true", help="required: acknowledges the spend")
    args = ap.parse_args(argv)
    if not args.yes:
        raise SystemExit("This spends money (~$1-2). Re-run with --yes.")
    calllog.begin_run("ask")
    try:
        a = ask(args.question, competitor=args.competitor, my_company=args.my_company, context=args.context,
                persona=args.persona, slug=args.slug, persist=args.persist)
    finally:
        calllog.flush_run(True)
    print(render_text(a))
    return a


if __name__ == "__main__":
    main()
