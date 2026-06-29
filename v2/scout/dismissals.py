"""Review the DISMISSAL stream: what each monitor run SURFACED but did not alert on — the triage
candidates (substantial vs minor), the materiality judge's IMMATERIAL verdicts (signal + why_not),
and the own-company signals that escalated, with what DID survive for contrast. Captured by
shadow.dismissal_capture to dismissals/<slug>/. This is the "what got surfaced and cut" view that
was previously discarded for a no-alert run.

    python -m scout.dismissals                 # recent dismissals across all cards, newest first
    python -m scout.dismissals <slug>          # just one card
"""
import json
import sys

from scout import selfserve, shadow


def load(slug: str | None = None) -> list:
    out = []
    try:
        slugs = ([slug] if slug
                 else [s for s in selfserve.list_data(shadow.DISMISSAL_DIR, include_dirs=True)
                       if "." not in s])
        for s in slugs:
            for fn in sorted(selfserve.list_data(f"{shadow.DISMISSAL_DIR}/{s}")):
                if not fn.endswith(".json"):
                    continue
                raw = selfserve.read_data(f"{shadow.DISMISSAL_DIR}/{s}/{fn}")
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[dismissals] load skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return out


def _print(recs: list, limit: int = 10) -> None:
    if not recs:
        print("No dismissal records yet — captured on the next run that surfaces a candidate "
              "(needs SCOUT_SHADOW_EVAL=1).")
        return
    for r in sorted(recs, key=lambda x: x.get("run_ts") or "", reverse=True)[:limit]:
        card = f"{r.get('my_company')} vs {r.get('competitor')}" if r.get("my_company") else r.get("slug")
        surfaced = r.get("surfaced", [])
        print(f"\n=== {card}  {r.get('run_ts')} ===")
        print(f"surfaced {len(surfaced)}  |  became material {len(r.get('became_material', []))}  |  "
              f"immaterial {len(r.get('materiality_immaterial', []))}  |  "
              f"own-co signals {len(r.get('my_company_substantial', []))}")
        for s in surfaced:
            mark = "SUBSTANTIAL" if s.get("substantial") else "minor"
            print(f"  [{s.get('about') or 'competitor'}/{mark}] {str(s.get('signal'))[:130]}")
            if s.get("source_hint"):
                print(f"        source_hint: {s.get('source_hint')}")
        for im in r.get("materiality_immaterial", []):
            print(f"  CUT (judged immaterial): {str(im.get('signal'))[:90]}")
            print(f"        why_not: {str(im.get('why_not'))[:160]}")
        for m in r.get("my_company_substantial", []):
            print(f"  OWN-CO escalated: {str(m.get('signal'))[:130]}")
        if r.get("became_material"):
            print(f"  -> survived as material: {r['became_material']}")


if __name__ == "__main__":
    _print(load(sys.argv[1] if len(sys.argv) > 1 else None))
