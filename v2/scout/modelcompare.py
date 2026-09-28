"""Comparison + scorecard for the on-device model comparison (2026-09-28; plan section E, R4/R6/R7/R10).

A replay result pairs a captured live call (the REFERENCE: what production acted on) with a
candidate backend's output on the same inputs. Per role (scout/rolespecs.py) the two are aligned
into items with status agree / disagree / abstain; disagreements get an ITEM-keyed delta_id
(`m_` + sha256(call_id|item_id)[:12], no backend in the key) so ONE human truth label scores every
backend and the reference alike. The scorecard has the challenger.scorecard shape so the three eval
lanes are diffable, and every number is computed twice: on the `common` population (call_ids every
enabled backend could attempt) and on `full` (that backend's own eligibility).
"""
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime

from scout import challenger, evalrule, rolespecs, selfserve

SCHEMA_VERSION = 1
SHADOW_MODELS_DIR = "shadow_models"
LABELS_PATH = "shadow_models/labels.jsonl"
SIZE_BUCKETS = ((0, 4000, "<4k"), (4000, 8000, "4-8k"), (8000, 16000, "8-16k"), (16000, 10 ** 9, ">16k"))
# The label a rater emits when it makes the role's COSTLY error (docs/model-substitution-exit-criteria.md):
# judge wrong confirm; gate_judge false pass; challenger slop (kept a bad claim); route missed
# consequential; materiality missed material; triage local quiet on a live escalation.
COSTLY_LABEL = {"judge": "confirm", "gate_judge": "confirm", "challenger": "keep", "route": "routine",
                "materiality": "immaterial", "triage": "quiet"}


def delta_id(call_id: str, item_id: str) -> str:
    return "m_" + hashlib.sha256(f"{call_id}|{item_id}".encode()).hexdigest()[:12]


def pair_delta_id(call_id: str, item_id: str, backend: str) -> str:
    """Prose roles: pair-keyed (the preference is between two specific outputs)."""
    return "p_" + hashlib.sha256(f"{call_id}|{item_id}|{backend}".encode()).hexdigest()[:12]


def size_bucket(est_tokens: int | None) -> str:
    n = est_tokens or 0
    for lo, hi, name in SIZE_BUCKETS:
        if lo <= n < hi:
            return name
    return ">16k"


# --- alignment ---------------------------------------------------------------------------------------
def compare_call(record: dict, replay: dict) -> dict:
    """Align reference vs candidate for one call. Returns {unit, family, items[], summary{}, extra{}}."""
    role = record.get("role")
    spec = rolespecs.spec(role) or {}
    family = spec.get("family")
    ref_text = ((record.get("result") or {}).get("text")) or ""
    out = {"unit": spec.get("unit"), "family": family, "items": [], "summary": {}, "extra": {}}
    if replay.get("status") != "ok":
        out["summary"] = {"judged": 0, "agree": 0, "disagree": 0, "abstain": 0, "agreement_rate": None,
                          "kappa": None, "candidate_parse": replay.get("reason")}
        return out
    parse = spec.get("parse")
    ref = parse(ref_text) if parse else None
    cand = parse(replay.get("text") or "") if parse else None
    if cand is None:
        out["summary"] = {"judged": 0, "agree": 0, "disagree": 0, "abstain": 0, "agreement_rate": None,
                          "kappa": None, "candidate_parse": "parse_fail"}
        return out
    out["extra"]["candidate_slips"] = cand.get("abstain") or {}
    if family == rolespecs.CLASSIFICATION and role not in ("triage", "materiality"):
        ref_items = (ref or {}).get("items") or {}
        ca, cb = [], []
        for iid, rlabel in ref_items.items():
            clabel = (cand.get("items") or {}).get(iid)
            if clabel is None:
                out["items"].append({"item_id": iid, "reference": rlabel, "candidate": None, "status": "abstain",
                                     "slip_kind": (cand.get("abstain") or {}).get(iid, "missing")})
                continue
            ca.append(rlabel)
            cb.append(clabel)
            if clabel == rlabel:
                out["items"].append({"item_id": iid, "reference": rlabel, "candidate": clabel, "status": "agree"})
            else:
                out["items"].append({"item_id": iid, "reference": rlabel, "candidate": clabel, "status": "disagree",
                                     "delta_id": delta_id(record["call_id"], iid)})
        agree = sum(1 for x, y in zip(ca, cb) if x == y)
        out["summary"] = {"judged": len(ca), "agree": agree, "disagree": len(ca) - agree,
                          "abstain": sum(1 for i in out["items"] if i["status"] == "abstain"),
                          "agreement_rate": round(agree / len(ca), 3) if ca else None,
                          "kappa": (round(k, 3) if ca and (k := challenger.cohens_kappa(ca, cb)) is not None else None),
                          "candidate_parse": "ok"}
        if role == "route":
            out["extra"]["ops"] = _set_compare(_route_keys((ref or {}).get("extra", {}).get("ops")),
                                               _route_keys(cand.get("extra", {}).get("ops")))
        if role == "lead_election":
            rw, cw = (ref or {}).get("extra", {}).get("winner_subject_key"), cand.get("extra", {}).get("winner_subject_key")
            out["extra"]["winner_agree"] = (str(rw).strip().lower() == str(cw).strip().lower()) if rw and cw else None
        return out
    if family == rolespecs.GENERATIVE:
        rt = (ref or {}).get("extra", {}).get("texts") or {}
        ct = cand.get("extra", {}).get("texts") or {}
        for iid, rtext in rt.items():
            ctext = ct.get(iid)
            if ctext is None:
                out["items"].append({"item_id": iid, "status": "abstain", "slip_kind": "missing"})
            else:
                out["items"].append({"item_id": iid, "status": "pair", "reference_len": len(rtext),
                                     "candidate_len": len(ctext),
                                     "pair_delta_id": pair_delta_id(record["call_id"], iid, replay.get("backend") or "")})
        pairs = sum(1 for i in out["items"] if i["status"] == "pair")
        out["summary"] = {"judged": pairs, "agree": None, "disagree": None,
                          "abstain": len(out["items"]) - pairs, "agreement_rate": None, "kappa": None,
                          "candidate_parse": "ok", "produced": len(ct), "expected": len(rt)}
        return out
    # run-level / set-valued (triage as escalation decision; materiality per candidate)
    if role == "triage":
        r_esc = _escalates((ref or {}).get("extra", {}).get("candidates"))
        c_esc = _escalates(cand.get("extra", {}).get("candidates"))
        status = "agree" if r_esc == c_esc else "disagree"
        item = {"item_id": "escalation", "reference": "escalate" if r_esc else "quiet",
                "candidate": "escalate" if c_esc else "quiet", "status": status}
        if status == "disagree":
            item["delta_id"] = delta_id(record["call_id"], "escalation")
        out["items"].append(item)
        out["summary"] = {"judged": 1, "agree": int(status == "agree"), "disagree": int(status == "disagree"),
                          "abstain": 0, "agreement_rate": float(status == "agree"), "kappa": None,
                          "candidate_parse": "ok"}
        return out
    if role == "materiality":
        rm = {_sk(c) for c in (ref or {}).get("extra", {}).get("material", [])}
        cm = {_sk(c) for c in cand.get("extra", {}).get("material", [])}
        ri = {_sig(c) for c in (ref or {}).get("extra", {}).get("immaterial", [])}
        ci = {_sig(c) for c in cand.get("extra", {}).get("immaterial", [])}
        ca, cb = [], []
        for key in sorted((rm | ri) - {None}):
            rl = "material" if key in rm else "immaterial"
            cl = "material" if key in cm else ("immaterial" if key in ci else None)
            if cl is None:
                out["items"].append({"item_id": key, "reference": rl, "candidate": None, "status": "abstain", "slip_kind": "missing"})
                continue
            ca.append(rl); cb.append(cl)
            it = {"item_id": key, "reference": rl, "candidate": cl, "status": "agree" if rl == cl else "disagree"}
            if rl != cl:
                it["delta_id"] = delta_id(record["call_id"], key)
            out["items"].append(it)
        agree = sum(1 for x, y in zip(ca, cb) if x == y)
        out["summary"] = {"judged": len(ca), "agree": agree, "disagree": len(ca) - agree,
                          "abstain": sum(1 for i in out["items"] if i["status"] == "abstain"),
                          "agreement_rate": round(agree / len(ca), 3) if ca else None,
                          "kappa": (round(k, 3) if ca and (k := challenger.cohens_kappa(ca, cb)) is not None else None),
                          "candidate_parse": "ok", "candidate_only_material": sorted(cm - rm - ri - {None})}
        return out
    out["summary"] = {"judged": 0, "agree": 0, "disagree": 0, "abstain": 0, "agreement_rate": None,
                      "kappa": None, "candidate_parse": "ok"}
    return out


def _escalates(cands) -> bool:
    return any(bool(c.get("substantial")) and str(c.get("about", "")).strip().lower() != "my_company"
               for c in (cands or []) if isinstance(c, dict))


def _sk(c):
    cl = c.get("claim") if isinstance(c, dict) else None
    return str((cl or {}).get("subject_key")).strip() if isinstance(cl, dict) and (cl or {}).get("subject_key") else None


def _sig(c):
    return str(c.get("signal")).strip()[:80] if isinstance(c, dict) and c.get("signal") else None


def _route_keys(ops) -> set:
    from scout.schema import normalize_subject_key
    keys = set()
    for o in ops or []:
        op = str(o.get("operation", "")).lower()
        base = (o.get("derived_from"), o.get("section"), op)
        keys.add(base + ((normalize_subject_key(str(o.get("target_subject_key") or "")),) if op in ("revise", "retire") else ()))
    return keys


def _set_compare(a: set, b: set) -> dict:
    inter = len(a & b)
    return {"reference": len(a), "candidate": len(b), "matched": inter,
            "precision": round(inter / len(b), 3) if b else None,
            "recall": round(inter / len(a), 3) if a else None,
            "jaccard": round(inter / len(a | b), 3) if (a | b) else None}


# --- persistence ---------------------------------------------------------------------------------
def result_record(record: dict, replay: dict, comparison: dict, *, backend: str, mode: str,
                  rep: int = 0, call_ref: str | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "call_id": record["call_id"], "call_ref": call_ref,
        "backend": backend, "backend_model": replay.get("backend_model"),
        "backend_version": replay.get("backend_version"), "role": record.get("role"),
        "slug": record.get("slug"), "run_ts": record.get("run_ts"), "source": record.get("source"),
        "fidelity": record.get("fidelity"), "mode": mode, "rep": rep,
        "replayed_at": datetime.now().isoformat(timespec="seconds"),
        "status": replay.get("status"), "reason": replay.get("reason"),
        "est_tokens_in": (record.get("sizes") or {}).get("est_tokens_in"),
        "observed_token_count": replay.get("observed_token_count"),
        "duration_ms": replay.get("duration_ms"), "tokens": replay.get("tokens"),
        "cost_usd": replay.get("cost_usd") or 0.0, "schema_enforced": replay.get("schema_enforced"),
        "reasoning": replay.get("reasoning"), "text": replay.get("text"), "thinking": replay.get("thinking"),
        "reference": {"model": record.get("model"), "cost_usd": (record.get("result") or {}).get("cost_usd"),
                      "duration_ms": (record.get("result") or {}).get("duration_ms"),
                      "eligible": rolespecs.reference_eligible(record)},
        "comparison": comparison,
    }


def result_path(backend: str, call_id: str, mode: str = "exact", rep: int = 0) -> str:
    suffix = "" if mode == "exact" else f".{mode}"
    suffix += f".r{rep}" if rep else ""
    return f"{SHADOW_MODELS_DIR}/{backend}/{call_id}{suffix}.json"


def persist(result: dict) -> str:
    path = result_path(result["backend"], result["call_id"], result.get("mode", "exact"), result.get("rep", 0))
    selfserve.write_data(path, json.dumps(result, indent=1, ensure_ascii=False),
                         f"shadow-models: {result['backend']} {result['role']} {result['call_id']}")
    return path


def existing(backend: str) -> set:
    return set(selfserve.list_data(f"{SHADOW_MODELS_DIR}/{backend}") or [])


def load_results(backend: str | None = None) -> list:
    out = []
    for b in (selfserve.list_data(SHADOW_MODELS_DIR, include_dirs=True) or []):
        if "." in b or (backend and b != backend):
            continue
        for fn in (selfserve.list_data(f"{SHADOW_MODELS_DIR}/{b}") or []):
            if not fn.endswith(".json"):
                continue
            raw = selfserve.read_data(f"{SHADOW_MODELS_DIR}/{b}/{fn}")
            if raw:
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    return out


def load_labels() -> dict:
    return evalrule.load_label_rows(LABELS_PATH)


# --- scorecard ----------------------------------------------------------------------------------------
def _wilson(k: int, n: int, z: float = 1.96) -> tuple | None:
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 3), round(c + h, 3))


def _p(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def _score_cell(rows: list, labels: dict) -> dict:
    """rows = results of one (backend, role) on one population."""
    n = len(rows)
    attempted = [r for r in rows if r.get("reason") != "context_exceeded" and r.get("reason") != "truncated"]
    ok = [r for r in attempted if r.get("status") == "ok"]
    parse_ok = [r for r in ok if (r.get("comparison") or {}).get("summary", {}).get("candidate_parse") == "ok"]
    refusals = sum(1 for r in attempted if r.get("reason") in ("refusal", "guardrail"))
    ca, cb, disagreements, adjud_c, adjud_r = [], [], [], [], []
    cand_on_adj, truth_on_adj, costly_c, costly_r = [], [], 0, 0
    slips = 0
    period = defaultdict(int)
    role = rows[0]["role"] if rows else None
    costly_label = COSTLY_LABEL.get(role)
    for r in ok:
        cmp_ = r.get("comparison") or {}
        slips += len((cmp_.get("extra") or {}).get("candidate_slips") or {})
        for it in cmp_.get("items") or []:
            if it["status"] in ("agree", "disagree"):
                ca.append(it["reference"]); cb.append(it["candidate"])
            if it["status"] == "disagree":
                d = {**it, "call_id": r["call_id"], "role": r["role"], "slug": r.get("slug"), "backend": r["backend"]}
                disagreements.append(d)
                lab = labels.get(it.get("delta_id"))
                if lab and lab.get("truth") is not None:
                    truth = lab["truth"]
                    adjud_c.append(1 if truth == it["candidate"] else 0)
                    adjud_r.append(1 if truth == it["reference"] else 0)
                    cand_on_adj.append(it["candidate"]); truth_on_adj.append(truth)
                    if costly_label:
                        costly_c += int(it["candidate"] == costly_label and truth != costly_label)
                        costly_r += int(it["reference"] == costly_label and truth != costly_label)
        period[(json.dumps(r.get("backend_version"), sort_keys=True), (r.get("reference") or {}).get("model"))] += 1
    right = sum(adjud_c)
    kappa_vs_human = (round(k, 3) if cand_on_adj and (k := challenger.cohens_kappa(cand_on_adj, truth_on_adj)) is not None
                      else None)
    return {
        "n_results": n, "attempted": len(attempted), "coverage": round(len(attempted) / n, 3) if n else None,
        "parse_ok": round(len(parse_ok) / len(ok), 3) if ok else None,
        "parse_slip_items": slips,
        "refusal_rate": round(refusals / len(attempted), 3) if attempted else None,
        "items_judged": len(ca), "agree": sum(1 for x, y in zip(ca, cb) if x == y),
        "agreement_rate": round(sum(1 for x, y in zip(ca, cb) if x == y) / len(ca), 3) if ca else None,
        "kappa_candidate_vs_reference": (round(k, 3) if ca and (k := challenger.cohens_kappa(ca, cb)) is not None else None),
        "disagreements": len(disagreements), "adjudicated": len(adjud_c),
        "candidate_right": right, "candidate_wrong": len(adjud_c) - right,
        "precision": round(right / len(adjud_c), 3) if adjud_c else None,
        "precision_ci95": _wilson(right, len(adjud_c)),
        "reference_right_on_adjudicated": sum(adjud_r),
        "kappa_candidate_vs_human": kappa_vs_human,
        "costly_error_rate": round(costly_c / len(adjud_c), 3) if adjud_c and costly_label else None,
        "reference_costly_error_rate": round(costly_r / len(adjud_c), 3) if adjud_c and costly_label else None,
        "latency_ms_p50": _p([r.get("duration_ms") for r in ok], 0.5),
        "latency_ms_p95": _p([r.get("duration_ms") for r in ok], 0.95),
        "n_calls": len({r["call_id"] for r in rows}), "n_cards": len({r.get("slug") for r in rows if r.get("slug")}),
        "periods": {f"{k[0]}|{k[1]}": v for k, v in period.items()},
        "pending_disagreements": [d for d in disagreements if d.get("delta_id") not in labels],
    }


def scorecard(results: list, labels: dict | None = None) -> dict:
    """Per (backend, role): the cell above on `full` (all that backend's results) and on `common`
    (call_ids every enabled backend attempted, i.e. not context_exceeded/truncated)."""
    labels = labels if labels is not None else load_labels()
    by_cell = defaultdict(list)
    backends = sorted({r["backend"] for r in results})
    attempted_by_backend = defaultdict(set)
    for r in results:
        if r.get("mode", "exact") != "exact" or r.get("rep"):
            continue
        by_cell[(r["backend"], r["role"])].append(r)
        if r.get("reason") not in ("context_exceeded", "truncated"):
            attempted_by_backend[r["backend"]].add(r["call_id"])
    common_ids = set.intersection(*(attempted_by_backend[b] for b in backends)) if backends else set()
    out = {"backends": backends, "common_n": len(common_ids), "cells": {}}
    for (b, role), rows in sorted(by_cell.items()):
        out["cells"][f"{b}|{role}"] = {
            "backend": b, "role": role, "family": (rolespecs.spec(role) or {}).get("family"),
            "full": _score_cell(rows, labels),
            "common": _score_cell([r for r in rows if r["call_id"] in common_ids], labels),
            "slices": scorecard_slices(rows, labels),
        }
    out["modes"] = mode_summary(results, labels)
    return out


def scorecard_slices(rows: list, labels: dict) -> dict:
    by_size, by_card = defaultdict(list), defaultdict(list)
    for r in rows:
        by_size[size_bucket(r.get("est_tokens_in"))].append(r)
        by_card[r.get("slug") or "?"].append(r)
    return {"by_prompt_size": {k: _slim(_score_cell(v, labels)) for k, v in sorted(by_size.items())},
            "by_card": {k: _slim(_score_cell(v, labels)) for k, v in sorted(by_card.items())}}


def _slim(cell: dict) -> dict:
    return {k: cell[k] for k in ("n_results", "coverage", "parse_ok", "agreement_rate",
                                 "kappa_candidate_vs_reference", "disagreements", "adjudicated", "precision")}


def mode_summary(results: list, labels: dict) -> dict:
    """Loop / frozen / repeat rows for the tools-on roles, kept apart from exact."""
    out = defaultdict(list)
    for r in results:
        if r.get("mode", "exact") != "exact":
            out[f"{r['backend']}|{r['role']}|{r['mode']}{'|rep' if r.get('rep') else ''}"].append(r)
    return {k: _slim(_score_cell(v, labels)) for k, v in sorted(out.items())}


def cost_view(bundles: list, days: int = 30) -> dict:
    """Live $ per role (from captured call records) + monthly projection; local backends are $0."""
    by_role = defaultdict(float)
    n_runs = len({b.get("stamp") for b in bundles})
    for b in bundles:
        for c in b.get("calls") or []:
            by_role[c.get("role")] += float((c.get("result") or {}).get("cost_usd") or 0.0)
    per_day = {r: round(v / max(days, 1), 4) for r, v in by_role.items()}
    return {"window_days": days, "runs": n_runs,
            "live_usd_by_role": {r: round(v, 4) for r, v in sorted(by_role.items())},
            "projected_monthly_usd_by_role": {r: round(v * 30, 2) for r, v in per_day.items()},
            "local_usd": 0.0}


def verdict_for_cell(cell: dict, prior: dict | None, *, bar: float = 0.50, min_adjudicated: int = 15) -> dict:
    """The shared rule on a cell's `full` numbers, with the sufficiency and coverage preconditions
    printed rather than fed to the streak."""
    full = cell["full"]
    cur = {"adjudicated": full["adjudicated"], "precision": full["precision"],
           "pending": len(full["pending_disagreements"])}
    sufficient = (full["adjudicated"] >= min_adjudicated and full["n_calls"] >= 5 and full["n_cards"] >= 2)
    v = evalrule.verdict(cell["role"], cur, prior, bar=bar, min_adjudicated=min_adjudicated,
                         bar_label=f"bar {bar} (parity)")
    if v["status"] != "ACCUMULATE" and not sufficient:
        v = {"status": "ACCUMULATE", "note": f"sufficiency not met (need >= {min_adjudicated} adjudicated, "
                                              f">= 5 calls, >= 2 cards; have {full['adjudicated']}/"
                                              f"{full['n_calls']}/{full['n_cards']})", "no_improve_streak": 0}
    v["coverage_ok"] = (full["coverage"] or 0) >= 0.95
    v["reliability_ok"] = ((full["parse_ok"] or 0) >= 0.98 and (full["refusal_rate"] or 0) <= 0.01)
    if v["status"] == "ELIGIBLE":
        v["note"] += " Non-inferior and sustained; no production switch exists."
    return v
