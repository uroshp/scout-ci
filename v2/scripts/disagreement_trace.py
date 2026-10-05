"""Disagreement trace for the on-device model lane (Uroš, 2026-10-05: "make sure it's the models,
not us"). For every disagreement between a local model and the live model it checks, from stored
data only ($0):

  the harness   the replay's instructions hash equals the captured call's (same system prompt), the
                mode is exact (the user prompt is the captured one by construction) or frozen (the
                captured tool results), the output parsed, nothing was truncated
  the reference the live model's self-agreement on the same calls (the `anthropic` re-run), the
                ceiling any candidate is measured against
  the verdicts  both outputs side by side with the reason each gave, the arbiter's blinded grade
                where one exists, and for tools-on roles whether a miss was retrieval or judgment

  python scripts/disagreement_trace.py [--roles judge,triage] [--out trace.html] [--cache path]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from scout import arbiter, modelcompare, selfserve  # noqa: E402

LOCAL = ("ollama", "ollama_gemma4", "ollama_nemotron", "apple_ondevice")
REFERENCE = "anthropic"


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _extract_json(text):
    if not text:
        return None
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return None


def load_all(cache: str | None) -> tuple[list, dict]:
    """(replay results, arbiter records by delta_id), cached locally so one session reads the store once."""
    if cache and os.path.exists(cache):
        d = json.load(open(cache))
        return d["results"], d["arbiter"]
    results = modelcompare.load_results()
    grades = {}
    try:
        for rec in arbiter.load_records():
            grades[rec.get("delta_id")] = rec
    except Exception as e:
        print(f"[trace] arbiter records unreadable ({type(e).__name__}: {e})", file=sys.stderr)
    if cache:
        json.dump({"results": results, "arbiter": grades}, open(cache, "w"))
    return results, grades


def load_reference_calls(results: list, cache: str | None) -> dict:
    """{call_id: captured call record} for every call a result refers to (the live output and prompt)."""
    if cache and os.path.exists(cache):
        return json.load(open(cache))
    want = {r.get("call_id") for r in results if r.get("call_id")}
    by_month: dict = defaultdict(set)
    for r in results:
        ts = str(r.get("run_ts") or "")
        if len(ts) >= 7:
            by_month[ts[:7]].add(ts.replace("-", "").replace(":", "")[:15])
    out = {}
    for month, stamps in by_month.items():
        try:
            names = selfserve.list_data(f"calls/{month}") or []
        except Exception:
            names = []
        for n in names:
            if not any(f"_{st}_" in n for st in stamps):
                continue
            try:
                b = json.loads(selfserve.read_data(f"calls/{month}/{n}") or "{}")
            except Exception:
                continue
            for c in b.get("calls") or []:
                if c.get("call_id") in want:
                    out[c["call_id"]] = {"role": c.get("role"), "slug": c.get("slug"), "model": c.get("model"),
                                         "instructions_sha": c.get("instructions_sha"), "user": c.get("user"),
                                         "text": (c.get("result") or {}).get("text"), "sizes": c.get("sizes")}
    if cache:
        json.dump(out, open(cache, "w"))
    return out


def judge_reason(text: str, op_index) -> tuple[str | None, str]:
    d = _extract_json(text or "") or {}
    for v in d.get("verdicts") or []:
        if str(v.get("op_index")) == str(op_index):
            return (str(v.get("verdict") or "")).strip().lower() or None, str(v.get("reason") or "")[:300]
    return None, ""


def op_summary(user: str, op_index) -> str:
    """The op the judge was asked about, from the captured user prompt (a JSON ops list)."""
    if not user:
        return ""
    m = re.search(r'"op_index"\s*:\s*%s\b' % re.escape(str(op_index)), user)
    if not m:
        return ""
    start = user.rfind("{", 0, m.start())
    depth, i = 0, start
    while i < len(user):
        if user[i] == "{":
            depth += 1
        elif user[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    try:
        op = json.loads(user[start:i + 1])
        return f"{op.get('operation')} {op.get('section')}/{op.get('zone') or ''} {op.get('subject_key')}: {str(op.get('claim') or '')[:160]}"
    except Exception:
        return user[m.start():m.start() + 160]


def build(results: list, grades: dict, refs: dict, roles: list[str]) -> dict:
    """The trace document: per backend x role x mode the harness checks, the agreement, the disagreements."""
    by = defaultdict(list)
    for r in results:
        if r.get("role") in roles and r.get("rep") in (None, 0):
            by[(r.get("backend"), r.get("role"), r.get("mode"))].append(r)
    cells = []
    for (backend, role, mode), rows in sorted(by.items()):
        ok = [r for r in rows if r.get("status") == "ok"]
        skipped = Counter(r.get("reason") for r in rows if r.get("status") != "ok")
        sha_mismatch = [r for r in ok if refs.get(r["call_id"]) and refs[r["call_id"]].get("instructions_sha")
                        and r.get("instructions_sha") and refs[r["call_id"]]["instructions_sha"] != r["instructions_sha"]]
        short = [r for r in ok if r.get("est_tokens_in") and r.get("observed_token_count")
                 and r["observed_token_count"] < 0.8 * r["est_tokens_in"]]
        judged = agree = 0
        disagreements = []
        parse_fail = 0
        for r in ok:
            cmp_ = r.get("comparison") or {}
            s = cmp_.get("summary") or {}
            if s.get("candidate_parse") not in (None, "ok"):
                parse_fail += 1
            judged += int(s.get("judged") or 0)
            agree += int(s.get("agree") or 0)
            for it in cmp_.get("items") or []:
                if it.get("status") != "disagree":
                    continue
                ref = refs.get(r["call_id"]) or {}
                d = {"call_id": r["call_id"], "slug": r.get("slug"), "item": it.get("item_id"),
                     "reference": it.get("reference"), "candidate": it.get("candidate"),
                     "miss_kind": it.get("miss_kind"), "delta_id": it.get("delta_id"),
                     "sha_ok": (not ref.get("instructions_sha")) or ref.get("instructions_sha") == r.get("instructions_sha")}
                if role == "judge":
                    d["reference_reason"] = judge_reason(ref.get("text"), it.get("item_id"))[1]
                    d["candidate_reason"] = judge_reason(r.get("text"), it.get("item_id"))[1]
                    d["op"] = op_summary(ref.get("user"), it.get("item_id"))
                g = grades.get(it.get("delta_id")) if it.get("delta_id") else None
                if g:
                    gr = g.get("grades") or {}                 # keyed by arm: the backend name, and live_judge
                    cand = gr.get(backend)
                    live = gr.get("live_judge") or gr.get("reference")
                    d["arbiter"] = {"truth": g.get("verdict"), "resolution": str(g.get("resolution") or "")[:240],
                                    "candidate_right": cand.get("right") if cand else None,
                                    "candidate_modes": (cand or {}).get("modes") or [],
                                    "reference_right": live.get("right") if live else None,
                                    "reference_modes": (live or {}).get("modes") or [],
                                    "graded_candidate": cand is not None,
                                    "review": (g.get("review") or {}).get("status")}
                disagreements.append(d)
        cells.append({"backend": backend, "role": role, "mode": mode, "n": len(rows), "ok": len(ok), "skipped": dict(skipped),
                      "judged": judged, "agree": agree, "agreement": round(agree / judged, 3) if judged else None,
                      "parse_fail": parse_fail, "sha_mismatch": len(sha_mismatch), "short_prompts": len(short),
                      "disagreements": disagreements,
                      "graded": sum(1 for d in disagreements if (d.get("arbiter") or {}).get("graded_candidate")),
                      "candidate_right": sum(1 for d in disagreements if (d.get("arbiter") or {}).get("candidate_right")),
                      "reference_right": sum(1 for d in disagreements if (d.get("arbiter") or {}).get("reference_right") and (d.get("arbiter") or {}).get("graded_candidate")),
                      "candidate_modes": dict(Counter(m for d in disagreements for m in ((d.get("arbiter") or {}).get("candidate_modes") or []))),
                      "reference_modes": dict(Counter(m for d in disagreements for m in ((d.get("arbiter") or {}).get("reference_modes") or []))),
                      "miss_kinds": dict(Counter(d.get("miss_kind") for d in disagreements if d.get("miss_kind")))})
    ceiling = {c["role"]: c["agreement"] for c in cells if c["backend"] == REFERENCE and c["judged"]}
    return {"cells": cells, "ceiling": ceiling, "roles": roles}


STYLE = """
<style>
:root{--bg:#f6f4ef;--ink:#1d1d1b;--mute:#6b6a64;--line:#dedbd2;--card:#fffdf8;--green:#1f7a3a;--amber:#9a6a00;--red:#b0301c;--chip:#ebe7dc;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15161a;--ink:#ecebe6;--mute:#a09f98;--line:#2e3038;--card:#1d1f25;--green:#5fcf84;--amber:#e0b64a;--red:#ff7b66;--chip:#2a2c34}}
:root[data-theme="dark"]{--bg:#15161a;--ink:#ecebe6;--mute:#a09f98;--line:#2e3038;--card:#1d1f25;--green:#5fcf84;--amber:#e0b64a;--red:#ff7b66;--chip:#2a2c34}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;margin:0;padding-block:24px;padding-inline:16px}
main{max-width:1040px;margin:0 auto}h1{font-size:22px;margin:0 0 6px}h2{font-size:17px;margin:26px 0 8px}h3{font-size:13px;margin:16px 0 6px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--mute);margin:0 0 14px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;padding:6px 8px;border-top:1px solid var(--line)}th{color:var(--mute);font-weight:600;border-top:0}.tbl{overflow-x:auto}
.k{font-family:var(--mono);font-size:12.5px}.good{color:var(--green);font-weight:700}.bad{color:var(--red);font-weight:700}.warn{color:var(--amber);font-weight:700}.mut{color:var(--mute)}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;font-size:12px}
</style>
"""


def render(doc: dict) -> str:
    out = ["<title>On-device disagreement trace</title>", STYLE, "<main><h1>On-device disagreement trace</h1>",
           "<p class='sub'>Every disagreement between a local model and the live model, with the harness checks that say "
           "whether the comparison itself was sound, the live model's own self-agreement as the ceiling, and the blinded grade where one exists.</p>"]
    if doc["ceiling"]:
        out.append("<div class='card'><h3>Reference ceiling (the live model re-run on the same calls)</h3><p>"
                   + " · ".join(f"<strong>{_e(r)}</strong>: self-agreement {_e(v)}" for r, v in doc["ceiling"].items()) + "</p></div>")
    else:
        out.append("<div class='card'><h3>Reference ceiling</h3><p class='mut'>not measured yet (the anthropic re-run has no results)</p></div>")
    out.append("<div class='card'><h3>Cells</h3><div class='tbl'><table><thead><tr><th>arm</th><th>role</th><th>mode</th><th>calls</th><th>ran</th><th>skipped</th>"
               "<th>same instructions</th><th>short prompts</th><th>parse fail</th><th>agree</th><th>disagree</th><th>graded</th><th>local right</th><th>live right</th><th>miss kinds</th></tr></thead><tbody>")
    for c in doc["cells"]:
        sha_cls = "good" if c["sha_mismatch"] == 0 else "bad"
        out.append(f"<tr><td>{_e(c['backend'])}</td><td>{_e(c['role'])}</td><td>{_e(c['mode'])}</td><td>{c['n']}</td><td>{c['ok']}</td>"
                   f"<td class='mut'>{_e(', '.join(f'{k} {v}' for k, v in c['skipped'].items()) or '')}</td>"
                   f"<td class='{sha_cls}'>{c['ok'] - c['sha_mismatch']}/{c['ok']}</td><td class='{'warn' if c['short_prompts'] else ''}'>{c['short_prompts']}</td>"
                   f"<td class='{'warn' if c['parse_fail'] else ''}'>{c['parse_fail']}</td>"
                   f"<td>{c['agree']}/{c['judged']}" + (f" ({c['agreement']})" if c['agreement'] is not None else "") + "</td>"
                   f"<td>{len(c['disagreements'])}</td><td>{c['graded']}</td><td>{c['candidate_right']}</td><td>{c['reference_right']}</td>"
                   f"<td class='mut'>{_e(', '.join(f'{k} {v}' for k, v in c['miss_kinds'].items()))}</td></tr>")
    out.append("</tbody></table></div></div>")
    for c in doc["cells"]:
        if not c["disagreements"] or c["backend"] == REFERENCE and c["role"] != "judge":
            continue
        out.append(f"<section class='card'><h2>{_e(c['backend'])} · {_e(c['role'])} · {_e(c['mode'])}: {len(c['disagreements'])} disagreement(s)</h2>")
        out.append("<div class='tbl'><table><thead><tr><th>call</th><th>item</th><th>live said</th><th>local said</th><th>grade</th></tr></thead><tbody>")
        for d in c["disagreements"]:
            a = d.get("arbiter") or {}
            grade = ("<span class='mut'>not graded</span>" if not a or not a.get("graded_candidate") else
                     f"truth <strong>{_e(a.get('truth'))}</strong> · local {'right' if a.get('candidate_right') else 'wrong'}"
                     + (f" ({_e(', '.join(a.get('candidate_modes') or []))})" if a.get('candidate_modes') else "")
                     + (f" · live {'right' if a.get('reference_right') else 'wrong'}" + (f" ({_e(', '.join(a.get('reference_modes') or []))})" if a.get('reference_modes') else "") if a.get('reference_right') is not None else "")
                     + f"<br><span class='mut'>{_e(a.get('resolution'))}</span>" + (f"<br><span class='chip'>review {_e(a.get('review'))}</span>" if a.get('review') else ""))
            harness = "" if d.get("sha_ok") else "<br><span class='bad'>instructions hash differs</span>"
            out.append(f"<tr><td class='k'>{_e(str(d.get('slug') or '')[:28])}<br>{_e(d['call_id'])}{harness}</td>"
                       f"<td>{_e(d.get('item'))}" + (f"<br><span class='mut'>{_e(d.get('op'))}</span>" if d.get("op") else "") + "</td>"
                       f"<td><strong>{_e(d.get('reference'))}</strong>" + (f"<br><span class='mut'>{_e(d.get('reference_reason'))}</span>" if d.get("reference_reason") else "") + "</td>"
                       f"<td><strong>{_e(d.get('candidate'))}</strong>" + (f"<br><span class='mut'>{_e(d.get('candidate_reason'))}</span>" if d.get("candidate_reason") else "")
                       + (f"<br><span class='chip'>{_e(d.get('miss_kind'))}</span>" if d.get("miss_kind") else "") + "</td>"
                       f"<td>{grade}</td></tr>")
        out.append("</tbody></table></div></section>")
    out.append("</main>")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roles", default="judge,triage,materiality,screen")
    ap.add_argument("--out", default="disagreement_trace.html")
    ap.add_argument("--cache", default=os.path.expanduser("~/scout-replay/trace-cache.json"))
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and read the store again")
    a = ap.parse_args()
    if a.refresh:
        for p in (a.cache, a.cache + ".calls"):
            if os.path.exists(p):
                os.remove(p)
    results, grades = load_all(a.cache)
    refs = load_reference_calls(results, a.cache + ".calls")
    doc = build(results, grades, refs, [r.strip() for r in a.roles.split(",") if r.strip()])
    with open(a.out, "w") as f:
        f.write(render(doc))
    print("ceiling:", doc["ceiling"] or "not measured")
    for c in doc["cells"]:
        if c["judged"] or c["disagreements"]:
            print(f"{c['backend']:16s} {c['role']:12s} {c['mode']:6s} ran {c['ok']:3d}/{c['n']:3d} same-instructions {c['ok'] - c['sha_mismatch']}/{c['ok']} "
                  f"short {c['short_prompts']} parse-fail {c['parse_fail']} agree {c['agree']}/{c['judged']} "
                  f"disagree {len(c['disagreements'])} graded {c['graded']} local-right {c['candidate_right']} live-right {c['reference_right']} {c['miss_kinds'] or ''}")
            if c["candidate_modes"] or c["reference_modes"]:
                print(f"{'':16s} local failure modes {c['candidate_modes']} | live failure modes {c['reference_modes']}")
    print("->", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
