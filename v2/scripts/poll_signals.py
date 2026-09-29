"""Poll every watched card's structured sources (WS3), $0: EDGAR filings and job boards. Writes
state + events to the private store; a new filing dispatches that card's check (capped). Runs on
the mini hourly (com.urosh.scout-signals) with creds from ~/scout-replay/env; the 4 AM Action can
run it too with --no-dispatch as a read-only sweep.

  python scripts/poll_signals.py [--slugs a,b] [--no-write] [--no-dispatch]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
ENV_PATH = os.path.expanduser("~/scout-replay/env")


def _load_env():
    if not os.path.exists(ENV_PATH):
        return
    for line in open(ENV_PATH):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", default="")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--no-dispatch", action="store_true")
    ap.add_argument("--watch-file", default="", help="JSON {slug: watch} that overrides meta.watch (the mini, before the cards carry it)")
    a = ap.parse_args()
    _load_env()
    import json
    from scout import config, display, signals, store
    overrides = json.load(open(os.path.expanduser(a.watch_file))) if a.watch_file else {}
    if not (config.SELFSERVE_GH_TOKEN and config.SELFSERVE_REPO):
        sys.exit("CREDS MISSING: SELFSERVE_GH_TOKEN / SELFSERVE_REPO not set (expected in ~/scout-replay/env)")
    want = {s.strip() for s in a.slugs.split(",") if s.strip()}
    n_dispatched = 0
    for slug in display.list_battlecards():
        if want and slug not in want:
            continue
        meta = store.load_meta(slug) or {}
        if meta.get("monitored") is False:
            continue
        if slug in overrides:
            meta = dict(meta, watch=overrides[slug])
        # one dispatch per pass: a second filing card waits an hour (the cap is 2 a day anyway)
        r = signals.poll_card(slug, meta, write=not a.no_write, allow_dispatch=(not a.no_dispatch and n_dispatched == 0))
        if r.get("skipped"):
            continue
        ctx = "; ".join(f"{c['host']} {c['open']} open net {c['net']:+d}" for c in r.get("context") or [])
        comp = ", ".join(str(c) for c in r.get("companies") or [])
        print(f"{slug[:44]:44s} edgar=[{comp}] filings={r['filings']} new_departments={r['new_departments']} "
              f"dispatch={r.get('dispatched') or '-'} {ctx}" + (f" ERRORS: {r['errors']}" if r["errors"] else ""))
        if r.get("dispatched") == "dispatched":
            n_dispatched += 1
    print(f"done; dispatched {n_dispatched}")


if __name__ == "__main__":
    main()
