"""The lifecycle audit (Uroš, 2026-10-04): follow every finding of a run from the searches to the
card AND to the eval lanes, and check the product promise at each step, in plain English.

Why: two bugs found on 10/3 and 10/4 invalidated the tool while every signal was green. The card's
focus area had never reached a prompt since June (no focus-area search was ever run on a focused
card), and a model change made the schema drop every own-side fact before grounding without a word.
Both are visible in a trace that asks, at each step, what went in, what came out and what was
dropped and why. The rehearsals answered "did it crash"; this answers "did it do the job".

The audit reads what a run already leaves behind (nothing new is asked of the models, $0):
  the cost ledger's step table, the captured model calls (searches, prompts, outputs), the card
  files it wrote (claims, alerts, meta), the shadow records (grounding kept/cut, dismissals, filter
  verdicts), the propagation decision log and the sensor compare record.

Four invariant groups, each a plain rule written from the promise:
  A  focus coverage        a focused card gets focus-area searches first, and its focus reaches
                           every model step
  B  boundary accounting   every fact a model emitted is accepted, or rejected with a named reason;
                           nothing vanishes between steps; no step failed
  C  continuity            every grounded material fact becomes an alert (or is a known repeat);
                           every alert has a verified, grounded claim behind it; every applied op
                           has its claim on the card
  D  eval continuity       every role that ran was captured with a spec the eval lanes score;
                           grounding, dismissal and decision records exist for what ran; one pack
                           version across the run

Runs INSIDE the monitor at the end of a run (in-memory rows and calls, the rest from the store) and
OFFLINE for any past run (`python -m scout.lifecycle <stamp>`), so Monday's run can be audited even
before this code ran there. The verdict is GREEN or RED; RED names the rule and the evidence.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta

from scout import config, selfserve, store
from scout.rolespecs import ROLE_SPECS
from scout.schema import normalize_subject_key
from scout.sensors.vocab import PRODUCT_QUERIES

LIFECYCLE_DIR = "lifecycle"
RUN_WINDOW_HOURS = 3                   # records stamped within this window after the run start belong to it
_STOP = {"and", "the", "for", "vs", "general", "inside", "with", "from", "into", "that", "this",
         "features", "area", "focus"}
_ROLE_PHASE = {"triage": "triage", "materiality": "materiality", "my_facts": "own_company",
               "screen": "screen", "route": "propagation", "author": "propagation", "judge": "propagation",
               "rewrite": "propagation", "reformat": "propagation", "gate_judge": "propagation",
               "lead_election": "propagation", "audience_author": "audience", "audience_judge": "audience",
               "persona": "propagation"}


# --- small helpers --------------------------------------------------------------------------------
def _extract_json(text: str):
    """The first JSON object in a model's text output (fenced or bare), or None."""
    if not text:
        return None
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return None


def _nk(k) -> str:
    return normalize_subject_key(str(k or ""))


def _stamp_of(name: str) -> str | None:
    m = re.search(r"(\d{8}T\d{6})", name or "")
    return m.group(1) if m else None


def _in_window(stamp: str | None, start: str, end: str | None = None) -> bool:
    """A record stamped at or after the run start, before the next run's start (or within
    RUN_WINDOW_HOURS when no later run is known), belongs to the run."""
    if not stamp:
        return False
    try:
        t0 = datetime.strptime(start, "%Y%m%dT%H%M%S")
        t = datetime.strptime(stamp, "%Y%m%dT%H%M%S")
        t1 = datetime.strptime(end, "%Y%m%dT%H%M%S") if end else t0 + timedelta(hours=RUN_WINDOW_HOURS)
    except Exception:
        return False
    return t0 - timedelta(minutes=1) <= t < min(t1, t0 + timedelta(hours=RUN_WINDOW_HOURS))


def next_run_stamp(stamp: str) -> str | None:
    """The stamp of the first later run in the cost ledger (runs never overlap: one concurrency group)."""
    try:
        later = sorted(_stamp_of(n) for n in (selfserve.list_data("costs") or []) if _stamp_of(n) and _stamp_of(n) > stamp)
        return later[0] if later else None
    except Exception:
        return None


def focus_terms(focus: str | None, names: list[str]) -> set:
    """The focus area's own words (stop words and the companies' names removed) plus the companies'
    product names: a search that names a product serves the focus area, a search about an IPO or
    an executive serves the corporate scope. A lower bound: the audit cannot read intent, so it
    fails only on ZERO focus searches (the June failure) and warns below the reserved count."""
    own = set()
    for n in names or []:
        own |= set(re.findall(r"[a-z0-9]{2,}", str(n or "").lower()))
    words = re.findall(r"[a-z][a-z0-9]+", str(focus or "").lower().replace("/", " ").replace("-", " "))
    terms = {w for w in words if w not in _STOP and w not in own}
    for n in names or []:
        for key, prods in PRODUCT_QUERIES.items():
            if key in str(n or "").lower():
                terms |= {p.lower() for p in prods}
    return terms


def search_scope(query: str, terms: set, names: list[str]) -> str:
    """focus | corporate | unclear for one search query."""
    q = set(re.findall(r"[a-z][a-z0-9]+", str(query or "").lower()))
    if terms and (q & terms):
        return "focus"
    own = set()
    for n in names or []:
        own |= set(re.findall(r"[a-z0-9]{2,}", str(n or "").lower()))
    return "corporate" if (q & own) else "unclear"


def _parse_counts(detail: str) -> dict:
    """Counts out of a materiality / own-company step row's detail text (older runs have only the text)."""
    out = {}
    m = re.search(r"(\d+) (?:anchor fact\(s\) )?grounded of (\d+) candidate", detail or "")
    if m:
        out["grounded"], out["candidates"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+) of (\d+) emitted fact\(s\) rejected by the schema", detail or "")
    if m:
        out["schema_rejected"], out["emitted"] = int(m.group(1)), int(m.group(2))
    return out


# --- data assembly --------------------------------------------------------------------------------
def _card_file(slug: str, name: str):
    """A card file from the local store root, else from the private store (a rehearsal's cards)."""
    import os
    p = os.path.join(store.STORE_ROOT, slug, name)
    if os.path.exists(p):
        with open(p) as f:
            return f.read()
    try:
        return selfserve.read_data(f"battlecards/{slug}/{name}")
    except Exception:
        return None


def _json_or(txt, default):
    try:
        return json.loads(txt) if txt else default
    except Exception:
        return default


def _records_for(dir_: str, slug: str, start: str, end: str | None = None) -> list[dict]:
    out = []
    try:
        for n in sorted(selfserve.list_data(f"{dir_}/{slug}") or []):
            if _in_window(_stamp_of(n), start, end):
                d = _json_or(selfserve.read_data(f"{dir_}/{slug}/{n}"), None)
                if isinstance(d, dict):
                    out.append(d)
    except Exception:
        pass
    return out


def load_calls(stamp: str) -> list[dict]:
    """Every captured call of the run (all parts). The capture stamps its bundle when the run begins
    and the ledger when the loop begins, so the two can differ by a few seconds."""
    month = f"{stamp[:4]}-{stamp[4:6]}"
    calls: list[dict] = []
    try:
        t0 = datetime.strptime(stamp, "%Y%m%dT%H%M%S")
        for n in sorted(selfserve.list_data(f"calls/{month}") or []):
            st = _stamp_of(n)
            if not st or not n.startswith("monitor_"):
                continue
            try:
                if abs((datetime.strptime(st, "%Y%m%dT%H%M%S") - t0).total_seconds()) > 20:
                    continue
            except Exception:
                continue
            b = _json_or(selfserve.read_data(f"calls/{month}/{n}"), {})
            calls += list(b.get("calls") or [])
    except Exception:
        pass
    return calls


def load_ledger(stamp: str) -> dict | None:
    return _json_or(selfserve.read_data(f"costs/{stamp}.json"), None)


def assemble(stamp: str, rows: list[dict] | None = None, calls: list[dict] | None = None) -> dict:
    """Everything the audit needs for one run: {stamp, rows, calls, cards: {slug: {...}}}."""
    if rows is None:
        led = load_ledger(stamp) or {}
        rows = list(led.get("cards") or [])
    if calls is None:
        calls = load_calls(stamp)
    end = next_run_stamp(stamp)
    cards = {}
    for r in rows:
        slug = r.get("slug")
        if not slug:
            continue
        meta = _json_or(_card_file(slug, "meta.json"), {}) or {}
        claims = _json_or(_card_file(slug, "claims.json"), []) or []
        alerts = [_json_or(l, None) for l in (_card_file(slug, "alerts.jsonl") or "").splitlines() if l.strip()]
        alerts = [a for a in alerts if isinstance(a, dict)]
        cards[slug] = {
            "row": r, "meta": meta, "claims": claims,
            "alerts": [a for a in alerts if _in_window(_stamp_of(str(a.get("detected_at", "")).replace("-", "").replace(":", "")), stamp, end)],
            "calls": [c for c in calls if c.get("slug") == slug],
            "shadow": _records_for("shadow", slug, stamp, end),
            "dismissals": _records_for("dismissals", slug, stamp, end),
            "propagation": _records_for("propagation", slug, stamp, end),
            "filter": _records_for("filter", slug, stamp, end),
        }
    return {"stamp": stamp, "rows": rows, "calls": calls, "cards": cards, "end": end}


# --- the trace ------------------------------------------------------------------------------------
def _calls_by_role(calls: list[dict]) -> dict:
    out: dict = {}
    for c in calls:
        out.setdefault(c.get("role"), []).append(c)
    return out


def _tool_uses(call: dict, name: str) -> list[dict]:
    return [t.get("input") or {} for t in (call.get("transcript") or [])
            if t.get("kind") == "tool_use" and t.get("name") == name]


def trace_card(card: dict) -> dict:
    """The plain-English trace of one card's run: what each step took in, put out and dropped."""
    meta, row = card["meta"], card["row"]
    names = [n for n in (meta.get("competitor"), meta.get("my_company")) if n]
    focus = str(meta.get("focus") or "").strip()
    focused = bool(focus) and focus.lower() not in ("general", "none")
    terms = focus_terms(focus, names) if focused else set()
    by_role = _calls_by_role(card["calls"])
    steps = {s.get("step"): s for s in (row.get("steps") or [])}

    # 1. searches
    searches = []
    for c in by_role.get("triage", []):
        for inp in _tool_uses(c, "WebSearch"):
            q = inp.get("query") or ""
            searches.append({"query": q, "scope": search_scope(q, terms, names) if focused else "corporate"})
    # 2. the focus reaching each model step's prompt
    prompts = {}
    for role in ("triage", "materiality", "my_facts"):
        for c in by_role.get(role, []):
            prompts[role] = bool(focus) and focus.lower() in str(c.get("user") or "").lower()
    # 3. candidates
    candidates = []
    for c in by_role.get("triage", []):
        d = _extract_json((c.get("result") or {}).get("text") or "")
        for x in (d or {}).get("candidates") or []:
            candidates.append({"signal": str(x.get("signal") or "")[:200], "about": x.get("about"),
                               "subject_key": x.get("subject_key"), "substantial": x.get("substantial") is True})
    triage_parsed = (not by_role.get("triage")) or any(_extract_json((c.get("result") or {}).get("text") or "") is not None
                                                      for c in by_role.get("triage", []))
    # 4. materiality: emitted material facts + immaterial verdicts
    material, immaterial = [], []
    for c in by_role.get("materiality", []):
        d = _extract_json((c.get("result") or {}).get("text") or "") or {}
        for m in d.get("material") or []:
            cl = (m.get("claim") or {}) if isinstance(m, dict) else {}
            material.append({"subject_key": cl.get("subject_key"), "new_value": (m.get("alert") or {}).get("new_value"),
                             "source_url": cl.get("source_url")})
        for i in d.get("immaterial") or []:
            if isinstance(i, dict):
                immaterial.append({"signal": str(i.get("signal") or "")[:160], "why_not": str(i.get("why_not") or "")[:200]})
    # 5. own-company facts emitted
    own_facts = []
    for c in by_role.get("my_facts", []):
        d = _extract_json((c.get("result") or {}).get("text") or "") or {}
        for m in d.get("facts") or []:
            cl = (m.get("claim") or {}) if isinstance(m, dict) else {}
            own_facts.append({"subject_key": cl.get("subject_key"), "new_value": (m.get("alert") or {}).get("new_value"),
                              "source_url": cl.get("source_url")})
    # 6. grounding, from the shadow records: the kept claims (by id, mapped to the card's subject
    #    keys; a derived-capture record has kept rows without a source and is not grounding) plus
    #    the per-claim results when the capture carries them
    key_by_id = {c.get("id"): c.get("subject_key") for c in card["claims"] if c.get("id")}
    kept, cut = [], []
    for s in card["shadow"]:
        for k in s.get("kept") or []:
            if k.get("source_url") or k.get("grounding_method"):
                kept.append({"subject_key": key_by_id.get(k.get("id")) or k.get("subject_key"), "status": "grounded",
                             "url": k.get("source_url"), "id": k.get("id")})
        for c in s.get("cut") or []:
            cl = c.get("claim") if isinstance(c.get("claim"), dict) else {}
            cut.append({"subject_key": cl.get("subject_key"), "status": "cut", "reason": c.get("reason")})
        for g in s.get("grounding_results") or []:
            if g.get("status") != "grounded":
                cut.append({"subject_key": g.get("subject_key"), "status": g.get("status"), "url": g.get("url")})
    results_n = sum(len(s.get("grounding_results") or []) for s in card["shadow"])
    # 7. propagation decisions
    decisions = []
    for p in card["propagation"]:
        for d in p.get("decisions") or []:
            decisions.append({"operation": d.get("operation"), "subject_key": d.get("subject_key") or d.get("target_subject_key"),
                              "verdict": d.get("judge_verdict"), "reason": str(d.get("judge_reason") or "")[:200],
                              "committed": d.get("committed"), "rewrite_attempts": d.get("rewrite_attempts")})
    # 8. published
    alerts = [{"subject_key": a.get("subject_key"), "severity": a.get("severity"), "headline": a.get("headline"),
               "source_url": a.get("source_url"), "fingerprint": a.get("fingerprint")} for a in card["alerts"]]
    run_date = card["row"].get("_run_date")
    touched = [c for c in card["claims"] if run_date and str(c.get("updated_on") or "")[:10] == run_date]
    # 9. evals
    captured_roles = sorted(r for r in by_role if r)
    return {
        "focused": focused, "focus": focus, "focus_terms": sorted(terms), "names": names,
        "steps": row.get("steps") or [],
        "searches": searches, "prompts_with_focus": prompts, "triage_parsed": triage_parsed,
        "candidates": candidates, "material": material, "immaterial": immaterial, "own_facts": own_facts,
        "grounding": {"kept": kept, "cut": cut, "records": len(card["shadow"]), "results": results_n},
        "decisions": decisions, "alerts": alerts, "claims_touched": len(touched),
        "evals": {"captured_roles": captured_roles,
                  "roles_without_spec": sorted(r for r in captured_roles if r not in ROLE_SPECS),
                  "shadow_records": len(card["shadow"]), "dismissal_records": len(card["dismissals"]),
                  "decision_logs": len(card["propagation"]), "filter_records": len(card["filter"]),
                  "pack_versions": sorted({str(c.get("judgment_version")) for c in card["calls"] if c.get("judgment_version")}),
                  "calls_without_sha": sum(1 for c in card["calls"] if not c.get("instructions_sha"))},
        "counts": {"materiality": _parse_counts((steps.get("materiality") or {}).get("detail") or ""),
                   "own_company": _parse_counts((steps.get("own_company") or {}).get("detail") or "")},
    }


# --- the invariants -------------------------------------------------------------------------------
def _ran(steps: dict, name: str) -> bool:
    return (steps.get(name) or {}).get("status") == "ran"


def check_card(tr: dict) -> list[dict]:
    """[{id, group, rule, status: pass|fail|n/a, evidence}] for one card's trace."""
    steps = {s.get("step"): s for s in tr["steps"]}
    out = []

    def add(id_, group, rule, status, evidence):
        out.append({"id": id_, "group": group, "rule": rule, "status": status, "evidence": str(evidence)[:400]})

    # A. focus coverage
    if tr["focused"] and _ran(steps, "triage") and not tr["focus_terms"]:
        add("A1", "focus", "focus-area searches", "n/a", f"the focus '{tr['focus']}' leaves no distinctive search word")
        for role, ok in tr["prompts_with_focus"].items():
            add("A2", "focus", f"the card's focus area reaches the {role} prompt", "pass" if ok else "fail",
                "present" if ok else f"the {role} prompt does not mention '{tr['focus']}'")
    elif tr["focused"] and _ran(steps, "triage"):
        n_focus = sum(1 for s in tr["searches"] if s["scope"] == "focus")
        need = min(config.TRIAGE_FOCUS_SEARCHES, max(1, len(tr["searches"])))
        add("A1", "focus", f"a focused card gets focus-area searches ({config.TRIAGE_FOCUS_SEARCHES} reserved)",
            "fail" if n_focus == 0 else ("warn" if n_focus < need else "pass"),
            f"{n_focus} of {len(tr['searches'])} searches name the focus area '{tr['focus']}' or a product"
            + (" (fewer than reserved: read the search list)" if 0 < n_focus < need else ""))
        for role, ok in tr["prompts_with_focus"].items():
            add("A2", "focus", f"the card's focus area reaches the {role} prompt", "pass" if ok else "fail",
                "present" if ok else f"the {role} prompt does not mention '{tr['focus']}'")
    else:
        add("A1", "focus", "focus-area searches", "n/a", "general card" if not tr["focused"] else "triage did not run")

    # B. boundary accounting
    if _ran(steps, "triage"):
        add("B1", "boundary", "triage output parsed", "pass" if tr["triage_parsed"] else "fail",
            f"{len(tr['candidates'])} candidate(s)" if tr["triage_parsed"] else "no JSON in the triage output")
    if _ran(steps, "materiality"):
        emitted = {_nk(m["subject_key"]) for m in tr["material"] if m.get("subject_key")}
        kept = {_nk(k.get("subject_key")) for k in tr["grounding"]["kept"] if k.get("subject_key")}
        cut = {_nk(k.get("subject_key")) for k in tr["grounding"]["cut"] if k.get("subject_key")}
        c = tr["counts"]["materiality"]
        rejected = int(c.get("schema_rejected") or 0)
        detail = (steps.get("materiality") or {}).get("detail") or ""
        vanished = sorted(emitted - kept - cut) if tr["grounding"]["records"] else []
        if tr["grounding"]["records"] == 0 and emitted:
            add("B2", "boundary", "every material fact the judge emitted is grounded, cut with a reason, or rejected by name",
                "fail", f"{len(emitted)} material fact(s) emitted but no grounding record was captured")
        else:
            ok = not vanished or len(vanished) <= rejected
            add("B2", "boundary", "every material fact the judge emitted is grounded, cut with a reason, or rejected by name",
                "pass" if ok else "fail",
                f"emitted {len(emitted)}, grounded {len(kept & emitted) if emitted else len(kept)}, cut {len(cut)}, schema-rejected {rejected}"
                + (f"; vanished without a reason: {', '.join(vanished)}" if not ok else "") + f" | step: {detail[:140]}")
    if _ran(steps, "own_company") or (steps.get("own_company") or {}).get("status") == "failed":
        c = tr["counts"]["own_company"]
        emitted_n = len(tr["own_facts"])
        grounded = int(c.get("grounded") or 0)
        rejected = int(c.get("schema_rejected") or 0)
        detail = (steps.get("own_company") or {}).get("detail") or ""
        if emitted_n and grounded == 0 and rejected == 0:
            status, ev = "fail", f"{emitted_n} fact(s) emitted, 0 grounded, no reject named | step: {detail[:160]}"
        elif emitted_n and rejected >= emitted_n:
            status, ev = "fail", f"every emitted fact rejected by the schema | step: {detail[:200]}"
        else:
            status, ev = "pass", f"emitted {emitted_n}, grounded {grounded}, schema-rejected {rejected} | step: {detail[:140]}"
        add("B3", "boundary", "every own-side fact the model emitted is grounded or rejected by name", status, ev)
    if tr["decisions"]:
        bad = [d for d in tr["decisions"] if not d.get("verdict") or (d.get("verdict") == "confirm" and not d.get("committed") and not d.get("reason"))]
        add("B4", "boundary", "every propagation op has a judge verdict with a reason; a confirmed op is applied or held with a reason",
            "pass" if not bad else "fail", f"{len(tr['decisions'])} op(s): " + ", ".join(
                f"{d['verdict']}{'/applied' if d.get('committed') else ''}" for d in tr["decisions"][:12]))
    failed = [s for s in tr["steps"] if s.get("status") == "failed"]
    add("B5", "boundary", "no step failed", "pass" if not failed else "fail",
        "; ".join(f"{s.get('step')}: {str(s.get('detail'))[:120]}" for s in failed) or f"{len(tr['steps'])} step rows")

    # C. continuity
    alerted = {_nk(a["subject_key"]) for a in tr["alerts"] if a.get("subject_key")}
    grounded_all = {_nk(k.get("subject_key")) for k in tr["grounding"]["kept"] if k.get("subject_key")}
    if grounded_all:
        missing = sorted(grounded_all - alerted)
        add("C1", "continuity", "every grounded material fact becomes an alert this run (or is a known repeat)",
            "pass" if not missing else "fail",
            f"{len(grounded_all)} grounded, {len(alerted)} alerted" + (f"; grounded but not alerted: {', '.join(missing)}" if missing else ""))
    if tr["alerts"]:
        by_key = {}
        for cl in tr.get("_claims") or []:
            by_key[_nk(cl.get("subject_key"))] = cl
        bad = []
        for a in tr["alerts"]:
            if str(a.get("subject_key") or "").startswith("audience-lead"):
                continue                                  # a lead is written to the audience store, not the claims
            cl = by_key.get(_nk(a["subject_key"]))
            if not cl:
                bad.append(f"{a['subject_key']}: no claim on the card")
            elif cl.get("verified") is not True:
                bad.append(f"{a['subject_key']}: claim not verified")
            elif cl.get("source_url") and not (cl.get("grounding") or {}).get("match"):
                bad.append(f"{a['subject_key']}: fact without a grounding match")
        add("C2", "continuity", "every alert has a verified, grounded claim behind it on the card",
            "pass" if not bad else "fail", "; ".join(bad) if bad else f"{len(tr['alerts'])} alert(s) checked")
    committed = [d for d in tr["decisions"] if d.get("committed")]
    if committed:
        keys = {_nk(cl.get("subject_key")) for cl in (tr.get("_claims") or [])}
        missing = [d["subject_key"] for d in committed if d.get("subject_key") and _nk(d["subject_key"]) not in keys]
        add("C3", "continuity", "every applied op has its claim on the card", "pass" if not missing else "fail",
            f"{len(committed)} applied" + (f"; not on the card: {', '.join(map(str, missing))}" if missing else ""))

    # D. eval continuity
    ran_roles = set()
    for s in tr["steps"]:
        if s.get("status") == "ran" and s.get("cost"):
            ran_roles.add(s.get("step"))
    phase_captured = {_ROLE_PHASE.get(r, r) for r in tr["evals"]["captured_roles"]}
    gaps = [p for p in ran_roles if p in ("triage", "materiality", "own_company", "screen", "propagation", "audience") and p not in phase_captured]
    add("D1", "evals", "every model step that ran was captured for the eval lanes, under a role with a spec",
        "pass" if not gaps and not tr["evals"]["roles_without_spec"] else "fail",
        f"captured roles: {', '.join(tr['evals']['captured_roles']) or 'none'}"
        + (f"; ran but not captured: {', '.join(gaps)}" if gaps else "")
        + (f"; captured without a spec: {', '.join(tr['evals']['roles_without_spec'])}" if tr["evals"]["roles_without_spec"] else ""))
    if _ran(steps, "materiality") or _ran(steps, "own_company"):
        own_grounded = int(tr["counts"]["own_company"].get("grounded") or 0)
        kept_keys = {_nk(k.get("subject_key")) for k in tr["grounding"]["kept"] if k.get("subject_key")}
        own_keys = {_nk(f.get("subject_key")) for f in tr["own_facts"] if f.get("subject_key")}
        own_in_shadow = len(own_keys & kept_keys)
        problems = []
        if tr["evals"]["shadow_records"] == 0:
            problems.append("no grounding record")
        if own_grounded and own_in_shadow == 0:
            problems.append(f"{own_grounded} own-side fact(s) grounded but none in the grounding record")
        if tr["candidates"] and tr["evals"]["dismissal_records"] == 0:
            problems.append("candidates surfaced but no dismissal record")
        add("D2", "evals", "grounding and dismissal records exist for what the arms did",
            "pass" if not problems else "fail",
            "; ".join(problems) if problems else f"{tr['evals']['shadow_records']} grounding, {tr['evals']['dismissal_records']} dismissal record(s)")
    if _ran(steps, "propagation"):
        add("D3", "evals", "the propagation decision log exists for the run", "pass" if tr["evals"]["decision_logs"] else "fail",
            f"{tr['evals']['decision_logs']} decision log(s), {len(tr['decisions'])} op(s)")
    if tr["evals"]["captured_roles"]:
        pv = tr["evals"]["pack_versions"]
        add("D4", "evals", "one pack version across the run and an instructions hash on every call",
            "pass" if len(pv) == 1 and not tr["evals"]["calls_without_sha"] else "fail",
            f"pack {', '.join(pv) or 'missing'}; {tr['evals']['calls_without_sha']} call(s) without a hash")
    return out


def audit(stamp: str, rows: list[dict] | None = None, calls: list[dict] | None = None, write: bool = True) -> dict:
    """The audit of one run. Returns {stamp, verdict, summary, cards: [{slug, verdict, invariants, trace}]}
    and writes lifecycle/<stamp>.json to the private store when `write`."""
    data = assemble(stamp, rows, calls)
    try:
        run_date = datetime.strptime(stamp, "%Y%m%dT%H%M%S").date().isoformat()
    except Exception:
        run_date = None
    cards_out, fails = [], 0
    for slug, card in data["cards"].items():
        card["row"]["_run_date"] = run_date
        tr = trace_card(card)
        tr["_claims"] = card["claims"]
        inv = check_card(tr)
        tr.pop("_claims", None)
        n_fail = sum(1 for i in inv if i["status"] == "fail")
        n_warn = sum(1 for i in inv if i["status"] == "warn")
        fails += n_fail
        cards_out.append({"slug": slug, "verdict": "RED" if n_fail else ("AMBER" if n_warn else "GREEN"), "failed": n_fail,
                          "warned": n_warn, "checked": sum(1 for i in inv if i["status"] != "n/a"), "invariants": inv, "trace": tr})
    warns = sum(c["warned"] for c in cards_out)
    verdict = "RED" if fails else ("AMBER" if warns else ("GREEN" if cards_out else "EMPTY"))
    checked = sum(c["checked"] for c in cards_out)
    summary = (f"{checked} check(s) on {len(cards_out)} card(s), {fails} failed" + (f", {warns} warning(s)" if warns else "")
               + ("; " + "; ".join(f"{c['slug'].split('__vs__')[0]}: " + ", ".join(i["id"] + " " + i["rule"] for i in c["invariants"] if i["status"] == "fail")
                                   for c in cards_out if c["failed"]) if fails else ""))
    doc = {"schema_version": 1, "stamp": stamp, "run_date": run_date, "verdict": verdict, "summary": summary,
           "checked": checked, "failed": fails, "cards": cards_out}
    if write:
        try:
            selfserve.write_data(f"{LIFECYCLE_DIR}/{stamp}.json", json.dumps(doc, indent=1, ensure_ascii=False, default=str),
                                 f"lifecycle: audit {stamp} {verdict}")
        except Exception as e:
            doc["write_error"] = f"{type(e).__name__}: {e}"
            print(f"[lifecycle] write skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return doc


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Audit one monitor run's findings from the searches to the card and the evals.")
    ap.add_argument("stamp", help="the run stamp, YYYYMMDDTHHMMSS (the costs/<stamp>.json ledger)")
    ap.add_argument("--write", action="store_true", help="write lifecycle/<stamp>.json to the private store")
    ap.add_argument("--json", action="store_true", help="print the full document")
    a = ap.parse_args(argv)
    doc = audit(a.stamp, write=a.write)
    if a.json:
        print(json.dumps(doc, indent=1, ensure_ascii=False, default=str))
        return 0 if doc["verdict"] != "RED" else 1
    print(f"{doc['verdict']}: {doc['summary']}")
    for c in doc["cards"]:
        print(f"\n== {c['slug']} [{c['verdict']}]")
        tr = c["trace"]
        print(f"   searches: {len(tr['searches'])} (" + ", ".join(f"{s}: {n}" for s, n in sorted(
            __import__('collections').Counter(x['scope'] for x in tr['searches']).items())) + ")")
        for i in c["invariants"]:
            mark = {"pass": "ok  ", "fail": "FAIL", "warn": "warn", "n/a": "n/a "}[i["status"]]
            print(f"   {mark} {i['id']} {i['rule']}\n        {i['evidence']}")
    return 0 if doc["verdict"] != "RED" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
