"""Pre-registered per-role specification for the on-device model comparison (2026-09-28, R6/R7).

For every paid role: its family (how it is scored), its label set, how items are keyed and matched,
its costly error direction, the STRICT candidate parser (maps ONLY the pre-registered label tokens;
anything else is an abstain with a slip_kind, never coerced), the reference parser (the live parser,
what production acted on), and the JSON schema the local backends are constrained to. Written
before any replay data is read; changing it after data exists is a new period.

Families:
  classification  -> paired labels per item; kappa + human precision (comparable to the older lanes)
  set             -> precision/recall/Jaccard over keyed ops; unmatched ops queue for a human; no kappa
  generative      -> pass-rate through the live deterministic gates + blinded pairwise preference
"""
import json
import re

from scout import config, schema

CLASSIFICATION, SET, GENERATIVE = "classification", "set", "generative"

_VERDICT_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["confirm", "reject"]}, "reason": {"type": "string"}},
    "required": ["verdict", "reason"]}


def _judge_schema():
    return {"type": "object", "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "properties": {
            "op_index": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["confirm", "reject"]},
            "reason": {"type": "string"},
            "material": {"type": "boolean"},
            "cure": {"type": "string", "enum": ["prose", "root", "none"]}},
        "required": ["op_index", "verdict", "reason"]}}}, "required": ["verdicts"]}


def _challenger_schema():
    return {"type": "object", "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "properties": {
            "item_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["keep", "cut"]},
            "reason": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
        "required": ["item_id", "verdict", "reason"]}}}, "required": ["verdicts"]}


def _route_schema():
    return {"type": "object", "properties": {
        "surface_ops": {"type": "array", "items": {"type": "object"}},
        "no_surface": {"type": "array", "items": {"type": "object"}},
        "run_verdict": {"type": "object", "properties": {
            "consequential": {"type": "boolean"}, "consequence_rationale": {"type": "string"},
            "headline": {"type": "string"}}, "required": ["consequential"]}},
        "required": ["surface_ops", "no_surface", "run_verdict"]}


def _election_schema():
    return {"type": "object", "properties": {
        "winner_subject_key": {"type": "string"},
        "margin": {"type": "string", "enum": ["decisive", "clear", "marginal", "none"]},
        "rationale": {"type": "string"}}, "required": ["winner_subject_key", "margin", "rationale"]}


def _persona_schema():
    return {"type": "object", "properties": {"persona": {"type": "string", "enum": list(schema.PERSONAS)}},
            "required": ["persona"]}


def _authored_schema():
    return {"type": "object", "properties": {"authored": {"type": "array", "items": {
        "type": "object", "properties": {"op_index": {"type": "integer"}, "claim": {"type": "string"},
                                         "persona": {"type": "string"}},
        "required": ["op_index", "claim"]}}}, "required": ["authored"]}


def _reformat_schema():
    return {"type": "object", "properties": {"claim": {"type": "string"}}, "required": ["claim"]}


def _materiality_schema():
    return {"type": "object", "properties": {
        "material": {"type": "array", "items": {"type": "object"}},
        "immaterial": {"type": "array", "items": {"type": "object"}}}, "required": ["material", "immaterial"]}


def _my_facts_schema():
    return {"type": "object", "properties": {
        "facts": {"type": "array", "items": {"type": "object"}},
        "immaterial": {"type": "array", "items": {"type": "object"}}}, "required": ["facts", "immaterial"]}


def _triage_schema():
    return {"type": "object", "properties": {
        "has_candidates": {"type": "boolean"},
        "candidates": {"type": "array", "items": {"type": "object", "properties": {
            "signal": {"type": "string"}, "subject_key": {"type": "string"},
            "about": {"type": "string"}, "valence": {"type": "string"},
            "substantial": {"type": "boolean"}, "why_new": {"type": "string"},
            "source_hint": {"type": "string"}}, "required": ["signal", "subject_key", "about", "substantial"]}}},
        "required": ["has_candidates", "candidates"]}


# --- strict candidate parsers -----------------------------------------------------------------------
# Each returns {"items": {item_id: label}, "abstain": {item_id: slip_kind}, "extra": {...}} or None
# when the text has no parseable JSON object at all (parse failure, counted in parse_ok).
def _extract(text):
    from scout.generate import _extract_json
    try:
        return _extract_json(text or "")
    except Exception:
        return None


def _labels_from_list(data, key, id_field, label_field, allowed, id_cast=str):
    if not isinstance(data, dict):
        return None
    rows = data.get(key)
    if not isinstance(rows, list):
        return {"items": {}, "abstain": {"*": "missing_key"}, "extra": {}}
    items, abstain = {}, {}
    for r in rows:
        if not isinstance(r, dict) or r.get(id_field) is None:
            abstain[f"row{len(items) + len(abstain)}"] = "missing_key"
            continue
        try:
            iid = str(id_cast(r[id_field]))          # item ids are strings everywhere (json keys, delta ids)
        except Exception:
            abstain[str(r.get(id_field))] = "wrong_type"
            continue
        v = r.get(label_field)
        if not isinstance(v, str):
            abstain[iid] = "wrong_type"
            continue
        v = v.strip().lower()
        if v in allowed:
            items[iid] = v
        else:
            abstain[iid] = "off_vocab"
    return {"items": items, "abstain": abstain, "extra": {}}


def parse_judge(text):
    return _labels_from_list(_extract(text), "verdicts", "op_index", "verdict", ("confirm", "reject"), int)


def parse_challenger(text):
    return _labels_from_list(_extract(text), "verdicts", "item_id", "verdict", ("keep", "cut"))


def parse_gate_judge(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    v = d.get("verdict")
    if not isinstance(v, str):
        return {"items": {}, "abstain": {"call": "wrong_type" if v is not None else "missing_key"}, "extra": {}}
    v = v.strip().lower()
    return {"items": {"call": v}, "abstain": {}, "extra": {}} if v in ("confirm", "reject") \
        else {"items": {}, "abstain": {"call": "off_vocab"}, "extra": {}}


def parse_persona(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    v = d.get("persona")
    if not isinstance(v, str):
        return {"items": {}, "abstain": {"call": "wrong_type" if v is not None else "missing_key"}, "extra": {}}
    return {"items": {"call": v}, "abstain": {}, "extra": {}} if v in schema.PERSONAS \
        else {"items": {}, "abstain": {"call": "off_vocab"}, "extra": {}}


def parse_route(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    rv = d.get("run_verdict")
    items, abstain = {}, {}
    if isinstance(rv, dict) and isinstance(rv.get("consequential"), bool):
        items["consequential"] = "consequential" if rv["consequential"] else "routine"
    else:
        abstain["consequential"] = "missing_key" if not isinstance(rv, dict) else "wrong_type"
    ops = d.get("surface_ops") if isinstance(d.get("surface_ops"), list) else []
    return {"items": items, "abstain": abstain, "extra": {"ops": [o for o in ops if isinstance(o, dict)],
                                                        "no_surface": d.get("no_surface") or []}}


def parse_election(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    mg = d.get("margin")
    items, abstain = {}, {}
    if isinstance(mg, str) and mg.strip().lower() in ("decisive", "clear", "marginal", "none"):
        items["margin"] = mg.strip().lower()
    else:
        abstain["margin"] = "off_vocab" if isinstance(mg, str) else ("missing_key" if mg is None else "wrong_type")
    return {"items": items, "abstain": abstain, "extra": {"winner_subject_key": d.get("winner_subject_key")}}


def parse_authored(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    rows = d.get("authored")
    if not isinstance(rows, list):
        return {"items": {}, "abstain": {"*": "missing_key"}, "extra": {}}
    out = {}
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("op_index"), int) and isinstance(r.get("claim"), str):
            out[str(r["op_index"])] = r["claim"]
    return {"items": {}, "abstain": {}, "extra": {"texts": out}}


def parse_reformat(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    c = d.get("claim")
    return {"items": {}, "abstain": {} if isinstance(c, str) else {"call": "missing_key"},
            "extra": {"texts": {"call": c} if isinstance(c, str) else {}}}


def parse_materiality(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    mat = d.get("material") if isinstance(d.get("material"), list) else []
    imm = d.get("immaterial") if isinstance(d.get("immaterial"), list) else []
    return {"items": {}, "abstain": {}, "extra": {"material": mat, "immaterial": imm}}


def parse_my_facts(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    return {"items": {}, "abstain": {}, "extra": {"facts": d.get("facts") if isinstance(d.get("facts"), list) else [],
                                                "immaterial": d.get("immaterial") if isinstance(d.get("immaterial"), list) else []}}


def _ask_research_schema():
    return {"type": "object", "properties": {"facts": {"type": "array"}, "answer": {"type": "array"}, "unanswered": {"type": "array"}},
            "required": ["facts", "answer"]}


def _ask_rewrite_schema():
    return {"type": "object", "properties": {"answer": {"type": "array"}}, "required": ["answer"]}


def _ask_quick_schema():
    return {"type": "object", "properties": {"answer": {"type": "array"}, "unanswered": {"type": "array"}}, "required": ["answer"]}


def parse_ask_quick(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    answer = [a for a in (d.get("answer") if isinstance(d.get("answer"), list) else []) if isinstance(a, dict)]
    return {"items": {}, "abstain": {}, "extra": {"answer": answer, "texts": {str(a.get("cites") or i): str(a.get("text") or "") for i, a in enumerate(answer)},
                                                "unanswered": d.get("unanswered") if isinstance(d.get("unanswered"), list) else []}}


def parse_ask_research(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    facts = [f for f in (d.get("facts") if isinstance(d.get("facts"), list) else []) if isinstance(f, dict)]
    answer = [a for a in (d.get("answer") if isinstance(d.get("answer"), list) else []) if isinstance(a, dict)]
    texts = {str(a.get("cites") or i): str(a.get("text") or "") for i, a in enumerate(answer)}
    return {"items": {}, "abstain": {}, "extra": {"facts": facts, "answer": answer, "texts": texts,
                                                "unanswered": d.get("unanswered") if isinstance(d.get("unanswered"), list) else []}}


def parse_ask_rewrite(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    answer = [a for a in (d.get("answer") if isinstance(d.get("answer"), list) else []) if isinstance(a, dict)]
    return {"items": {}, "abstain": {}, "extra": {"texts": {str(a.get("index")): str(a.get("text") or "") for a in answer}}}


def parse_triage(text):
    d = _extract(text)
    if not isinstance(d, dict):
        return None
    cands = d.get("candidates") if isinstance(d.get("candidates"), list) else []
    cands = [c for c in cands if isinstance(c, dict)]
    return {"items": {}, "abstain": {}, "extra": {"candidates": cands,
                                                "has_candidates": bool(d.get("has_candidates"))}}


# --- the table ---------------------------------------------------------------------------------------
ROLE_SPECS = {
    "judge": {"family": CLASSIFICATION, "unit": "op", "label_set": ("confirm", "reject"),
              "costly": "wrong confirm", "parse": parse_judge, "schema": _judge_schema,
              "primary_model": lambda: config.ORCHESTRATOR_MODEL, "output_reserve": 1024},
    "gate_judge": {"family": CLASSIFICATION, "unit": "call", "label_set": ("confirm", "reject"),
                   "costly": "false pass", "parse": parse_gate_judge, "schema": lambda: _VERDICT_SCHEMA,
                   "primary_model": lambda: config.ORCHESTRATOR_MODEL, "output_reserve": 256},
    "persona": {"family": CLASSIFICATION, "unit": "call", "label_set": tuple(schema.PERSONAS),
                "costly": "wrong class", "parse": parse_persona, "schema": _persona_schema,
                "primary_model": lambda: config.FAST_MODEL, "output_reserve": 128},
    "route": {"family": SET, "unit": "call", "label_set": ("consequential", "routine"),
              "costly": "missed consequential", "parse": parse_route, "schema": _route_schema,
              "primary_model": lambda: config.ORCHESTRATOR_MODEL, "output_reserve": 1024},
    "lead_election": {"family": CLASSIFICATION, "unit": "call", "label_set": ("decisive", "clear", "marginal", "none"),
                      "costly": "wrong promotion", "parse": parse_election, "schema": _election_schema,
                      "primary_model": lambda: config.LEAD_ELECTION_MODEL, "output_reserve": 256},
    "challenger": {"family": CLASSIFICATION, "unit": "item", "label_set": ("keep", "cut"),
                   "costly": "slop", "parse": parse_challenger, "schema": _challenger_schema,
                   "primary_model": lambda: config.CHALLENGER_MODEL, "output_reserve": 768},
    "author": {"family": GENERATIVE, "unit": "op", "label_set": (), "costly": "op failing the floor",
               "parse": parse_authored, "schema": _authored_schema,
               "primary_model": lambda: config.SUBAGENT_MODEL, "output_reserve": 1024},
    "rewrite": {"family": GENERATIVE, "unit": "op", "label_set": (), "costly": "confirmed update lost or held",
                "parse": parse_authored, "schema": _authored_schema,
                "primary_model": lambda: config.PROPAGATE_REWRITE_MODEL, "output_reserve": 1024},
    "reformat": {"family": GENERATIVE, "unit": "call", "label_set": (), "costly": "confirmed update lost or held",
                 "parse": parse_reformat, "schema": _reformat_schema,
                 "primary_model": lambda: config.CHALLENGER_MODEL, "output_reserve": 512},
    "triage": {"family": CLASSIFICATION, "unit": "run", "label_set": ("escalate", "quiet"),
               "costly": "local quiet on a live escalation", "parse": parse_triage, "schema": _triage_schema,
               "primary_model": lambda: config.FAST_MODEL, "output_reserve": 1024, "tools_on": True},
    "materiality": {"family": CLASSIFICATION, "unit": "candidate", "label_set": ("material", "immaterial"),
                    "costly": "missed material", "parse": parse_materiality, "schema": _materiality_schema,
                    "primary_model": lambda: config.ORCHESTRATOR_MODEL, "output_reserve": 2048, "tools_on": True},
    "my_facts": {"family": GENERATIVE, "unit": "fact", "label_set": (), "costly": "fact broader than source",
                 "parse": parse_my_facts, "schema": _my_facts_schema,
                 "primary_model": lambda: config.SUBAGENT_MODEL, "output_reserve": 2048, "tools_on": True},
    # Ask Scout (WS2, pre-registered 2026-09-28 before the first capture, plan C43): the research
    # pass is generative + tools-on (its facts are judged like my_facts); the verifier is a
    # confirm|reject classification per sentence, costly direction = a wrong confirm; the rewrite is
    # generative + tools-off.
    "ask_research": {"family": GENERATIVE, "unit": "fact", "label_set": (), "costly": "fact broader than source",
                     "parse": parse_ask_research, "schema": _ask_research_schema,
                     "primary_model": lambda: config.SUBAGENT_MODEL, "output_reserve": 2048, "tools_on": True},
    "ask_verify": {"family": CLASSIFICATION, "unit": "sentence", "label_set": ("confirm", "reject"),
                   "costly": "wrong confirm", "parse": parse_judge, "schema": _judge_schema,
                   "primary_model": lambda: config.ORCHESTRATOR_MODEL, "output_reserve": 1024},
    "ask_rewrite": {"family": GENERATIVE, "unit": "sentence", "label_set": (), "costly": "sentence broader than its facts",
                    "parse": parse_ask_rewrite, "schema": _ask_rewrite_schema,
                    "primary_model": lambda: config.SUBAGENT_MODEL, "output_reserve": 1024},
    # the quick path's draft (2026-09-28): tools off, prose from known facts + labelled takes only
    "ask_quick": {"family": GENERATIVE, "unit": "sentence", "label_set": (), "costly": "sentence broader than its facts",
                  "parse": parse_ask_quick, "schema": _ask_quick_schema,
                  "primary_model": lambda: config.SUBAGENT_MODEL, "output_reserve": 1024},
}

EXACT_ROLES = tuple(r for r, s in ROLE_SPECS.items() if not s.get("tools_on"))
TOOLS_ON_ROLES = tuple(r for r, s in ROLE_SPECS.items() if s.get("tools_on"))


def spec(role: str) -> dict | None:
    return ROLE_SPECS.get(role)


def role_schema(role: str) -> dict | None:
    s = ROLE_SPECS.get(role)
    return s["schema"]() if s else None


def output_reserve(role: str) -> int:
    s = ROLE_SPECS.get(role)
    return int(s["output_reserve"]) if s else 512


def reference_eligible(record: dict) -> tuple[bool, str | None]:
    """A live record enters the main cell only if it succeeded, parses with the strict parser, and
    ran on the role's primary model (a fallback-judged or unparsed reference is 'unusable')."""
    s = ROLE_SPECS.get(record.get("role"))
    if not s:
        return False, "unknown_role"
    if record.get("status") != "ok":
        return False, "status"
    if record.get("model") != s["primary_model"]():
        return False, "fallback_model"
    parsed = s["parse"](((record.get("result") or {}).get("text")) or "")
    if parsed is None or (s["family"] == CLASSIFICATION and not parsed["items"] and s["unit"] != "run"):
        return False, "unparsed"
    return True, None
