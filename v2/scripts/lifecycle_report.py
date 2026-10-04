"""Render the lifecycle audit of a run as one self-contained HTML page (Uroš, 2026-10-04): the
verdict, then per card the plain-English trace (searches with their scope, what each step took in,
put out and dropped, what landed) and every rule with its evidence. No model, no network beyond the
private store, no external assets.

  python scripts/lifecycle_report.py <stamp> [--out report.html] [--audit]   (--audit re-runs the audit
  instead of reading lifecycle/<stamp>.json; honours SCOUT_SELFSERVE_DATA_PREFIX and SCOUT_STORE_ROOT)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from scout import lifecycle, selfserve  # noqa: E402


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _label(slug: str) -> str:
    parts = slug.split("__vs__")
    if len(parts) == 2:
        a, rest = parts[0], parts[1].split("__", 1)
        b, focus = rest[0], (rest[1] if len(rest) > 1 else "")
        return f"{a.replace('-', ' ').title()} vs {b.replace('-', ' ').title()}" + (f" · {focus.replace('-', ' ')}" if focus and focus != "general" else "")
    return slug


STYLE = """
<style>
:root{--bg:#f6f4ef;--ink:#1d1d1b;--mute:#6b6a64;--line:#dedbd2;--card:#fffdf8;--green:#1f7a3a;--amber:#9a6a00;--red:#b0301c;--chip:#ebe7dc;--focus:#2b5ea8;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15161a;--ink:#ecebe6;--mute:#a09f98;--line:#2e3038;--card:#1d1f25;--green:#5fcf84;--amber:#e0b64a;--red:#ff7b66;--chip:#2a2c34;--focus:#8fb4ff}}
:root[data-theme="dark"]{--bg:#15161a;--ink:#ecebe6;--mute:#a09f98;--line:#2e3038;--card:#1d1f25;--green:#5fcf84;--amber:#e0b64a;--red:#ff7b66;--chip:#2a2c34;--focus:#8fb4ff}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;margin:0;padding-block:24px;padding-inline:16px}
main{max-width:980px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;text-wrap:balance}h2{font-size:17px;margin:28px 0 8px}h3{font-size:14px;margin:18px 0 6px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--mute);margin:0 0 18px}
.verdict{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;font-size:13px;letter-spacing:.03em}
.GREEN{background:var(--green);color:#fff}.AMBER{background:var(--amber);color:#fff}.RED{background:var(--red);color:#fff}.EMPTY{background:var(--chip)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;padding:6px 8px;border-top:1px solid var(--line)}th{color:var(--mute);font-weight:600;border-top:0}
.tbl{overflow-x:auto}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;font-size:12px;margin-right:4px}
.scope-focus{color:var(--focus);font-weight:600}.scope-corporate{color:var(--mute)}.scope-unclear{color:var(--amber)}
.st-pass{color:var(--green);font-weight:700}.st-fail{color:var(--red);font-weight:700}.st-warn{color:var(--amber);font-weight:700}.st-na{color:var(--mute)}
.ev{color:var(--mute);font-size:13px}
code{font-family:var(--mono);font-size:12.5px;background:var(--chip);padding:1px 5px;border-radius:4px}
.k{font-family:var(--mono);font-size:12.5px}
</style>
"""


def render(doc: dict) -> str:
    v = doc.get("verdict", "EMPTY")
    out = [f"<title>Lifecycle audit {_e(doc.get('run_date') or doc.get('stamp'))}</title>", STYLE, "<main>",
           f"<h1>Lifecycle audit <span class='verdict {v}'>{_e(v)}</span></h1>",
           f"<p class='sub'>Run {_e(doc.get('stamp'))} · {_e(doc.get('summary'))}</p>",
           "<p class='sub'>Every finding of the run followed from the searches to the card and to the eval lanes, "
           "checked against the promise's rules. Nothing here asked a model anything.</p>"]
    for c in doc.get("cards") or []:
        tr, inv = c.get("trace") or {}, c.get("invariants") or []
        out.append(f"<section class='card'><h2>{_e(_label(c['slug']))} <span class='verdict {c.get('verdict', 'EMPTY')}'>{_e(c.get('verdict'))}</span></h2>")
        if tr.get("focused"):
            out.append(f"<p class='sub'>Focus area: <strong>{_e(tr.get('focus'))}</strong> · focus words: "
                       + " ".join(f"<span class='chip'>{_e(t)}</span>" for t in tr.get("focus_terms") or []) + "</p>")
        else:
            out.append("<p class='sub'>General card: corporate scope only.</p>")
        # rules
        out.append("<h3>Rules</h3><div class='tbl'><table><thead><tr><th>rule</th><th>result</th><th>evidence</th></tr></thead><tbody>")
        for i in inv:
            cls = {"pass": "st-pass", "fail": "st-fail", "warn": "st-warn"}.get(i["status"], "st-na")
            out.append(f"<tr><td><span class='k'>{_e(i['id'])}</span> {_e(i['rule'])}</td><td class='{cls}'>{_e(i['status'])}</td><td class='ev'>{_e(i['evidence'])}</td></tr>")
        out.append("</tbody></table></div>")
        # steps
        out.append("<h3>Steps</h3><div class='tbl'><table><thead><tr><th>step</th><th>status</th><th>what happened</th><th>cost</th></tr></thead><tbody>")
        for s in tr.get("steps") or []:
            cls = {"ran": "", "failed": "st-fail", "skipped": "st-na"}.get(s.get("status"), "")
            out.append(f"<tr><td>{_e(s.get('step'))}</td><td class='{cls}'>{_e(s.get('status'))}</td><td class='ev'>{_e(s.get('detail'))}</td>"
                       f"<td class='k'>{('$%.3f' % s['cost']) if s.get('cost') else ''}</td></tr>")
        out.append("</tbody></table></div>")
        # searches
        sc = Counter(s["scope"] for s in tr.get("searches") or [])
        out.append(f"<h3>Searches ({len(tr.get('searches') or [])}: " + ", ".join(f"{k} {n}" for k, n in sorted(sc.items())) + ")</h3>")
        if tr.get("searches"):
            out.append("<div class='tbl'><table><tbody>")
            for s in tr["searches"]:
                out.append(f"<tr><td class='scope-{_e(s['scope'])}'>{_e(s['scope'])}</td><td>{_e(s['query'])}</td></tr>")
            out.append("</tbody></table></div>")
        pw = tr.get("prompts_with_focus") or {}
        if pw:
            out.append("<p class='ev'>Focus area in the prompt: " + ", ".join(f"{_e(r)} {'yes' if ok else 'NO'}" for r, ok in pw.items()) + "</p>")
        # findings flow
        out.append("<h3>Findings</h3><div class='tbl'><table><thead><tr><th>stage</th><th>items</th></tr></thead><tbody>")
        cands = tr.get("candidates") or []
        out.append(f"<tr><td>triage candidates</td><td>{len(cands)} ({sum(1 for x in cands if x.get('substantial'))} substantial)<br>"
                   + "<br>".join(f"<span class='ev'>[{_e(x.get('about'))}] {_e(x.get('signal'))}</span>" for x in cands[:12]) + "</td></tr>")
        mat = tr.get("material") or []
        imm = tr.get("immaterial") or []
        out.append(f"<tr><td>materiality</td><td>{len(mat)} material, {len(imm)} immaterial<br>"
                   + "<br>".join(f"<span class='ev'>material: <span class='k'>{_e(m.get('subject_key'))}</span></span>" for m in mat)
                   + ("<br>" if mat and imm else "")
                   + "<br>".join(f"<span class='ev'>immaterial: {_e(i.get('signal'))} — {_e(i.get('why_not'))}</span>" for i in imm[:8]) + "</td></tr>")
        own = tr.get("own_facts") or []
        out.append(f"<tr><td>own-company facts</td><td>{len(own)} emitted<br>" + "<br>".join(f"<span class='ev k'>{_e(f.get('subject_key'))}</span>" for f in own) + "</td></tr>")
        g = tr.get("grounding") or {}
        out.append(f"<tr><td>grounding</td><td>{len(g.get('kept') or [])} grounded, {len(g.get('cut') or [])} cut · {g.get('records', 0)} record(s), {g.get('results', 0)} per-claim result(s)<br>"
                   + "<br>".join(f"<span class='ev k'>{_e(k.get('subject_key'))}</span> <span class='ev'>{_e(k.get('url') or '')}</span>" for k in (g.get('kept') or [])[:12])
                   + "".join(f"<br><span class='ev'>cut: <span class='k'>{_e(k.get('subject_key'))}</span> {_e(k.get('status') or k.get('reason') or '')}</span>" for k in (g.get('cut') or [])[:8]) + "</td></tr>")
        dec = tr.get("decisions") or []
        out.append(f"<tr><td>propagation</td><td>{len(dec)} op(s), {sum(1 for d in dec if d.get('committed'))} applied<br>"
                   + "<br>".join(f"<span class='ev'>{_e(d.get('verdict'))}{' · applied' if d.get('committed') else ''} · {_e(d.get('operation'))} <span class='k'>{_e(d.get('subject_key'))}</span> — {_e(d.get('reason'))}</span>" for d in dec[:16]) + "</td></tr>")
        al = tr.get("alerts") or []
        out.append(f"<tr><td>published</td><td>{len(al)} alert(s), {tr.get('claims_touched', 0)} claim(s) touched<br>"
                   + "<br>".join(f"<span class='ev'>[{_e(str(a.get('severity') or '').upper())}] {_e(a.get('headline'))} <span class='k'>{_e(a.get('subject_key'))}</span></span>" for a in al) + "</td></tr>")
        ev = tr.get("evals") or {}
        out.append(f"<tr><td>eval lanes</td><td class='ev'>captured roles: {_e(', '.join(ev.get('captured_roles') or []) or 'none')}"
                   + (f"<br><span class='st-fail'>captured without a spec: {_e(', '.join(ev.get('roles_without_spec')))}</span>" if ev.get("roles_without_spec") else "")
                   + f"<br>grounding records {ev.get('shadow_records', 0)} · dismissal records {ev.get('dismissal_records', 0)} · decision logs {ev.get('decision_logs', 0)} · filter records {ev.get('filter_records', 0)}"
                   + f"<br>pack {_e(', '.join(ev.get('pack_versions') or []) or 'missing')} · calls without an instructions hash: {ev.get('calls_without_sha', 0)}</td></tr>")
        out.append("</tbody></table></div></section>")
    out.append("</main>")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stamp")
    ap.add_argument("--out", default="")
    ap.add_argument("--audit", action="store_true", help="re-run the audit instead of reading the stored document")
    ap.add_argument("--write", action="store_true", help="with --audit: also store lifecycle/<stamp>.json")
    a = ap.parse_args()
    doc = None
    if not a.audit:
        try:
            doc = json.loads(selfserve.read_data(f"{lifecycle.LIFECYCLE_DIR}/{a.stamp}.json") or "null")
        except Exception:
            doc = None
    if not doc:
        doc = lifecycle.audit(a.stamp, write=a.write)
    page = render(doc)
    out = a.out or f"lifecycle_{a.stamp}.html"
    with open(out, "w") as f:
        f.write(page)
    print(f"{doc.get('verdict')}: {doc.get('summary')} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
