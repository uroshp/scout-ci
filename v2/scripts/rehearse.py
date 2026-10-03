"""Rehearsal mode (2026-10-03): the monitor's REAL write path on retired cards, into a store root
outside the checkout, with every private-store write under the `rehearsal/` prefix.

The rehearsal cards live on between rehearsals in the private store (`rehearsal/battlecards/<slug>/`
once SCOUT_SELFSERVE_DATA_PREFIX=rehearsal is set), so each rehearsal scans since the previous one,
an update lands once, and a second run on the same card finds only what is new, as in production.
The archive in the public repo is never modified.

  python scripts/rehearse.py prep  <slug[,slug]> --root $RUNNER_TEMP/rehearsal/battlecards
  python scripts/rehearse.py save  <slug[,slug]> --root ...
  python scripts/rehearse.py summary <slug[,slug]> --root ... [--out $GITHUB_STEP_SUMMARY]

prep:    restore each card from the private store if a previous rehearsal left one there, else copy
         it from v2/archive/<slug> (resetting its hold-window attempt counter so the hold path is
         exercised), into <root>/<slug>/; keep a pristine copy in <root>/.before/<slug>/ for the diff.
save:    write the updated card files back to the private store (unchanged files are skipped).
summary: a Markdown report of what the rehearsal did: the step table from today's rehearsal cost
         ledger, the alerts written, the claims that changed, the cost.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from scout import config, selfserve  # noqa: E402

FILES = ("meta.json", "claims.json", "current.md", "alerts.jsonl", "alerts.md")
ARCHIVE = os.path.join(config.APP_ROOT, "archive")


def _store_path(slug: str, name: str) -> str:
    return f"battlecards/{slug}/{name}"          # the rehearsal prefix is applied by selfserve


def _slugs(arg: str) -> list[str]:
    out = [s.strip() for s in (arg or "").split(",") if s.strip()]
    if not out:
        sys.exit("rehearse: no slug given")
    return out


def prep(slugs: list[str], root: str) -> None:
    if config.SELFSERVE_DATA_PREFIX != "rehearsal":
        sys.exit(f"rehearse prep: SCOUT_SELFSERVE_DATA_PREFIX must be 'rehearsal' (is {config.SELFSERVE_DATA_PREFIX!r})")
    os.makedirs(root, exist_ok=True)
    for slug in slugs:
        dest = os.path.join(root, slug)
        before = os.path.join(root, ".before", slug)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest)
        restored = 0
        meta_txt = selfserve.read_data(_store_path(slug, "meta.json")) if selfserve.use_github() else None
        if meta_txt:
            for name in FILES:
                txt = selfserve.read_data(_store_path(slug, name))
                if txt is not None:
                    with open(os.path.join(dest, name), "w") as f:
                        f.write(txt)
                    restored += 1
            print(f"[rehearse] {slug}: restored {restored} file(s) from the rehearsal store")
        else:
            src = os.path.join(ARCHIVE, slug)
            if not os.path.isdir(src) or not os.path.exists(os.path.join(src, "meta.json")):
                sys.exit(f"rehearse prep: no rehearsal copy in the store and no archive card at {src}")
            for name in FILES:
                p = os.path.join(src, name)
                if os.path.exists(p):
                    shutil.copy2(p, os.path.join(dest, name))
            meta = json.load(open(os.path.join(dest, "meta.json")))
            if meta.get("unresolved_attempts"):
                # the archive cards sit at attempt 2 of 3: without the reset every rehearsal would
                # abandon the window on its first hold and the hold path would never be exercised
                meta["unresolved_attempts"] = 0
            meta["monitored"] = True
            meta["rehearsal"] = {"from": "archive", "first_prep": date.today().isoformat()}
            with open(os.path.join(dest, "meta.json"), "w") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            print(f"[rehearse] {slug}: copied from the archive (attempt counter reset)")
        if os.path.isdir(before):
            shutil.rmtree(before)
        shutil.copytree(dest, before)
    print(f"[rehearse] store root {root}: {', '.join(slugs)}")


def save(slugs: list[str], root: str) -> None:
    if config.SELFSERVE_DATA_PREFIX != "rehearsal":
        sys.exit(f"rehearse save: SCOUT_SELFSERVE_DATA_PREFIX must be 'rehearsal' (is {config.SELFSERVE_DATA_PREFIX!r})")
    if not selfserve.use_github():
        sys.exit("rehearse save: store credentials missing (SELFSERVE_GH_TOKEN / SELFSERVE_REPO); nothing saved")
    for slug in slugs:
        d = os.path.join(root, slug)
        n = 0
        for name in FILES:
            p = os.path.join(d, name)
            if os.path.exists(p):
                selfserve.write_data(_store_path(slug, name), open(p).read(), f"rehearsal: {slug} {name}")
                n += 1
        print(f"[rehearse] {slug}: {n} file(s) saved to the rehearsal store")


def _latest_ledger() -> dict | None:
    names = sorted(selfserve.list_data("costs") or [])
    today = date.today().strftime("%Y%m%d")
    todays = [n for n in names if n.startswith(today)]
    if not todays:
        return None
    try:
        return json.loads(selfserve.read_data(f"costs/{todays[-1]}") or "null")
    except Exception:
        return None


def _claims_by_id(path: str) -> dict:
    try:
        return {c.get("id"): c for c in json.load(open(path))}
    except Exception:
        return {}


def summary(slugs: list[str], root: str, out: str | None) -> None:
    lines = [f"# Rehearsal: {', '.join(slugs)}", ""]
    ledger = _latest_ledger() if selfserve.use_github() else None
    rows = {r.get("slug"): r for r in (ledger or {}).get("cards", [])}
    for slug in slugs:
        lines += [f"## {slug}", ""]
        row = rows.get(slug)
        if row:
            lines += [f"Cost ${row.get('total', 0):.2f}; {row.get('alerts', 0)} alert(s); {row.get('claims', 0)} claim(s) shipped.", "",
                      "| step | status | detail | cost |", "|---|---|---|---|"]
            for st in row.get("steps") or []:
                lines.append(f"| {st.get('step')} | {st.get('status')} | {str(st.get('detail') or '').replace('|', '/')} | "
                             f"{('$%.3f' % st['cost']) if st.get('cost') else ''} |")
            lines.append("")
        else:
            lines += ["(no ledger row for this card today: the run did not reach the ledger write)", ""]
        before = os.path.join(root, ".before", slug)
        after = os.path.join(root, slug)
        b, a = _claims_by_id(os.path.join(before, "claims.json")), _claims_by_id(os.path.join(after, "claims.json"))
        added = [i for i in a if i not in b]
        removed = [i for i in b if i not in a]
        changed = [i for i in a if i in b and a[i] != b[i]]
        lines.append(f"Claims: {len(b)} before, {len(a)} after; {len(added)} added, {len(changed)} changed, {len(removed)} removed.")
        for i in added[:10]:
            lines.append(f"- added `{a[i].get('subject_key')}`: {str(a[i].get('claim', ''))[:160]}")
        for i in changed[:10]:
            lines.append(f"- changed `{a[i].get('subject_key')}`")
        for i in removed[:10]:
            lines.append(f"- removed `{b[i].get('subject_key')}`")
        try:
            bl = open(os.path.join(before, "alerts.jsonl")).read().splitlines() if os.path.exists(os.path.join(before, "alerts.jsonl")) else []
            al = open(os.path.join(after, "alerts.jsonl")).read().splitlines() if os.path.exists(os.path.join(after, "alerts.jsonl")) else []
            new_alerts = [json.loads(l) for l in al[len(bl):] if l.strip()]
        except Exception:
            new_alerts = []
        lines.append(f"Alerts appended: {len(new_alerts)}")
        for al_ in new_alerts[:10]:
            lines.append(f"- [{al_.get('severity', '').upper()}] {al_.get('headline')} ({al_.get('subject_key')})")
        try:
            mb = json.load(open(os.path.join(before, "meta.json"))); ma = json.load(open(os.path.join(after, "meta.json")))
            keys = ("last_checked", "unresolved_since", "unresolved_attempts", "pending_candidates")
            md = [f"{k}: {json.dumps(mb.get(k), default=str)[:80]} -> {json.dumps(ma.get(k), default=str)[:80]}" for k in keys if mb.get(k) != ma.get(k)]
            if md:
                lines.append("Meta: " + "; ".join(md))
        except Exception:
            pass
        lines.append("")
    text = "\n".join(lines)
    print(text)
    if out:
        with open(out, "a") as f:
            f.write(text + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("prep", "save", "summary"))
    ap.add_argument("slugs")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=os.environ.get("GITHUB_STEP_SUMMARY") or None)
    a = ap.parse_args()
    slugs = _slugs(a.slugs)
    root = os.path.abspath(os.path.expanduser(a.root))
    if a.cmd == "prep":
        prep(slugs, root)
    elif a.cmd == "save":
        save(slugs, root)
    else:
        summary(slugs, root, a.out)


if __name__ == "__main__":
    main()
