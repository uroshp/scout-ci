"""Replay captured paid calls on the Mac mini against on-device models (2026-09-28, plan section D).

Reads the call bundles the live runs wrote to the private store (calls/<YYYY-MM>/...), sends each
eligible call to the chosen backend(s) via scout.replaybackends, aligns the answer against the live
output (scout.modelcompare) and persists one result per (call_id, backend, mode[, rep]) under
shadow_models/<backend>/. Spend-safe default: with no --run/--write it only prints an estimate and
calls nothing. Local backends cost $0; the `anthropic` reference re-run refuses without --allow-spend.

    python scripts/replay_calls.py --backend apple_ondevice --backend ollama --run --write
    python scripts/replay_calls.py --backend apple_ondevice --roles judge,gate_judge --limit 3 --run
    python scripts/replay_calls.py --backend ollama --mode frozen --roles triage --run --write

Creds come from ~/scout-replay/env (600), loaded by absolute path; the runner hard-exits before any
store call if they are missing, and runs a positive-control read on costs/ (an empty store read is a
broken read, not an empty store). Runs only inside --window (default 05:00-08:30 PT) unless
--no-window; --deadline HH:MM and --max-minutes stop it cleanly, partial progress already persisted.
"""
import argparse
import json
import os
import sys
import time

import httpx
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOOKBACK_DAYS = 14
ENV_PATH = os.path.expanduser("~/scout-replay/env")
STATE_PATH = os.path.expanduser("~/scout-replay/state.json")


def _load_env():
    if not os.path.exists(ENV_PATH):
        return
    for line in open(ENV_PATH):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _require_store():
    from scout import selfserve
    if not selfserve.use_github():
        sys.exit("CREDS MISSING: SELFSERVE_GH_TOKEN / SELFSERVE_REPO not set (expected in ~/scout-replay/env)")
    if not (selfserve.list_data("costs") or []):
        sys.exit("STORE READ BROKEN: costs/ listed empty (positive control failed)")


def _in_window(window: str) -> bool:
    lo, hi = window.split("-")
    now = datetime.now().strftime("%H:%M")
    return lo <= now < hi


def list_bundles(since: str | None) -> list:
    from scout import calllog, selfserve
    out = []
    for month in sorted(selfserve.list_data(calllog.CALLS_DIR, include_dirs=True) or []):
        if "." in month:
            continue
        for fn in sorted(selfserve.list_data(f"{calllog.CALLS_DIR}/{month}") or []):
            if not fn.endswith(".json"):
                continue
            stamp = fn.split("_")[1] if "_" in fn else fn
            if since and stamp < since:
                continue
            out.append(f"{calllog.CALLS_DIR}/{month}/{fn}")
    return out


def load_calls(paths: list) -> list:
    from scout import selfserve
    calls = []
    for p in paths:
        raw = selfserve.read_data(p)
        if not raw:
            print(f"  ! empty read for {p} (bundle over the read ceiling?)", file=sys.stderr)
            continue
        try:
            b = json.loads(raw)
        except json.JSONDecodeError:
            print(f"  ! unparseable bundle {p}", file=sys.stderr)
            continue
        for c in b.get("calls") or []:
            c["_call_ref"] = p
            calls.append(c)
    return calls


def _state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(st, open(STATE_PATH, "w"), indent=1)


def main():
    _load_env()
    from scout import modelcompare, replaybackends, rolespecs
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", action="append", choices=list(replaybackends.BACKENDS), required=True)
    ap.add_argument("--roles", default=None, help="comma list; default = every exact role (or tools-on for loop/frozen)")
    ap.add_argument("--mode", choices=["exact", "frozen", "loop", "all"], default="exact")
    ap.add_argument("--repeat", type=int, default=0, help="re-run rep N of a loop call (variance floor)")
    ap.add_argument("--since", default=None, help=f"bundle stamp YYYYMMDDTHHMMSS; default = {LOOKBACK_DAYS} days ago")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--slug", default=None)
    ap.add_argument("--call-id", default=None)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--allow-spend", action="store_true")
    ap.add_argument("--window", default="05:00-08:30")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--deadline", default=None, help="HH:MM local; stop cleanly")
    ap.add_argument("--max-minutes", type=int, default=240)
    args = ap.parse_args()
    run = args.run or args.write

    _require_store()
    st = _state()
    # Default lookback = 14 days. Idempotency (one result file per call/backend/mode/rep) makes
    # re-listing safe, so no shared "last stamp" can skip a bundle for the second arm or after an
    # early stop; older bundles are reachable with --since.
    since = args.since or (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%dT%H%M%S")
    modes = ["exact", "frozen", "loop"] if args.mode == "all" else [args.mode]
    roles = set(args.roles.split(",")) if args.roles else None

    paths = list_bundles(since)
    calls = load_calls(paths)
    calls = [c for c in calls if c.get("status") == "ok" and (rolespecs.spec(c.get("role")) is not None)]
    if args.slug:
        calls = [c for c in calls if c.get("slug") == args.slug]
    if args.call_id:
        calls = [c for c in calls if c.get("call_id") == args.call_id]
    if roles:
        calls = [c for c in calls if c.get("role") in roles]
    plan = []
    for mode in modes:
        for c in calls:
            tools_on = bool((rolespecs.spec(c["role"]) or {}).get("tools_on"))
            if mode == "exact" and tools_on:
                continue
            if mode in ("frozen", "loop") and not tools_on:
                continue
            plan.append((mode, c))
    if args.limit is not None:
        plan = plan[:args.limit]
    print(f"bundles={len(paths)} since={since or 'all'}  calls={len(calls)}  planned={len(plan)}  "
          f"backends={args.backend}  modes={modes}")
    # Freshness (2026-09-30): the lane exists to score TODAY's production calls. If the newest
    # bundle is older than the monitor's cadence allows, the scorecard would be a backlog dressed
    # as the day's result (that morning's capture had landed under rc/ and nobody was told). Say
    # so by email, print the bundle dates it will score, and carry on with what exists.
    newest = max((os.path.basename(p).split("_")[1] for p in paths if "_" in os.path.basename(p)), default=None)
    print(f"newest bundle: {newest or 'none'}")
    if run and not args.since and not os.environ.get("SCOUT_REPLAY_NO_FRESH_CHECK"):
        fresh_h = float(os.environ.get("SCOUT_REPLAY_FRESH_HOURS", "30"))
        age_h = None
        try:
            age_h = (datetime.now() - datetime.strptime(newest, "%Y%m%dT%H%M%S")).total_seconds() / 3600 if newest else None
        except ValueError:
            pass
        if age_h is None or age_h > fresh_h:
            from scout import notify
            why = "no capture bundle at all" if newest is None else f"newest capture is {age_h:.0f} h old (limit {fresh_h:.0f} h)"
            print(f"  NO FRESH CAPTURE: {why}; scoring the backlog and emailing", file=sys.stderr)
            notify.send_could_not_run("on-device replay", f"no fresh capture: {why}",
                                      f"newest bundle {newest or 'none'}; {len(paths)} bundle(s) since {since}. "
                                      "Check the monitor ran and wrote calls/ at the production path.",
                                      dry_run=os.environ.get("SCOUT_REPLAY_LIVE") != "1")
    by_role = {}
    for mode, c in plan:
        by_role[(mode, c["role"])] = by_role.get((mode, c["role"]), 0) + 1
    for (mode, role), n in sorted(by_role.items()):
        print(f"  {mode:6} {role:14} {n}")
    if not run:
        print("\nESTIMATE ONLY — no model calls made. Re-run with --run (and --write to persist).")
        return

    t_start = time.monotonic()
    deadline = None
    if args.deadline:
        h, m = args.deadline.split(":")
        deadline = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if deadline < datetime.now():
            deadline += timedelta(days=1)
    done_total = 0
    for backend in args.backend:
        if backend == "anthropic" and not args.allow_spend:
            print(f"{backend}: refused (pass --allow-spend to spend money)"); continue
        have_by_month: dict[str, set] = {}

        def have(month: str) -> set:
            if args.force:
                return set()
            if month not in have_by_month:
                have_by_month[month] = modelcompare.existing(backend, month)
            return have_by_month[month]
        searx_ok = True
        if "loop" in modes:
            from scout import localagent
            try:
                searx_ok = httpx.get(f"{localagent.SEARXNG_URL}/search", params={"q": "ping", "format": "json"}, timeout=15).status_code == 200
            except Exception:
                searx_ok = False
            if not searx_ok:
                print(f"  SEARXNG DOWN at {localagent.SEARXNG_URL}: loop-mode calls will be skipped (not persisted)")
        print(f"\n--- {backend} ---")
        n_done = n_skip = 0
        transport_stop = False
        if backend == "ollama":
            res = replaybackends.ollama_resident()
            if res is None:
                print("  ollama: model not loaded yet; the first call loads it")
        for mode, c in plan:
            if not args.no_window and not _in_window(args.window):
                print("  outside the window; stopping"); break
            if deadline and datetime.now() >= deadline:
                print("  deadline reached; stopping"); break
            if (time.monotonic() - t_start) / 60 > args.max_minutes:
                print("  max-minutes reached; stopping"); break
            month = modelcompare.result_month(c.get("run_ts"))
            if modelcompare.result_name(c["call_id"], mode, args.repeat) in have(month):
                n_skip += 1; continue
            if args.repeat and modelcompare.result_name(c["call_id"], mode, 0) not in have(month):
                n_skip += 1; continue                  # a rep needs its rep-0 twin first
            if mode == "loop":
                from scout import localagent
                if not searx_ok:
                    print("  SEARXNG DOWN: loop mode skipped for this call"); n_skip += 1; continue
                replay = localagent.run_loop(c, backend, rep=args.repeat)
            else:
                replay = replaybackends.drive_replay(c, backend, mode=mode, allow_spend=args.allow_spend)
            if backend == "ollama" and replay.get("status") == "ok":
                ps = replaybackends.ollama_resident()
                if ps and ps.get("size_vram") is not None and ps.get("size") is not None and ps["size_vram"] != ps["size"]:
                    print(f"  ollama: model NOT fully on Metal (size_vram={ps['size_vram']} size={ps['size']}); aborting arm")
                    break
            if replay.get("reason") == "transport":
                # The backend server is gone (the Apple bridge exits on a context overflow):
                # infrastructure, not a model result. Never persisted; the arm stops with exit 3
                # so run.sh restarts the server and resumes (results are idempotent).
                print(f"  {mode:6} {c['role']:14} {str(c.get('slug'))[:26]:26} TRANSPORT: {str(replay.get('text'))[:100]}; arm stops for a restart")
                transport_stop = True
                break
            cmp_ = modelcompare.compare_call(c, {**replay, "backend": backend})
            result = modelcompare.result_record(c, replay, cmp_, backend=backend, mode=mode, rep=args.repeat,
                                                call_ref=c.get("_call_ref"))
            s = cmp_.get("summary") or {}
            print(f"  {mode:6} {c['role']:14} {str(c.get('slug'))[:26]:26} {replay['status']:7} "
                  f"{(replay.get('reason') or ''):16} agree={s.get('agree')}/{s.get('judged')} "
                  f"k={s.get('kappa')} {replay.get('duration_ms') or 0}ms")
            if args.write:
                # A store hiccup (a GitHub 500 took the Apple arm down on 2026-09-30 after its
                # first call) must not end the arm: retry once, then log and move on.
                for attempt in (1, 2):
                    try:
                        modelcompare.persist(result); break
                    except Exception as e:
                        print(f"  persist failed ({type(e).__name__}: {str(e)[:120]}) attempt {attempt}", file=sys.stderr)
                        if attempt == 1:
                            time.sleep(5)
            n_done += 1
        print(f"{backend}: {n_done} replayed, {n_skip} already done")
        done_total += n_done
        if transport_stop:
            raise SystemExit(3)
        if backend == "ollama":
            replaybackends.ollama_unload()
    if args.write:
        st["last_run"] = datetime.now().isoformat(timespec="seconds")
        st["last_backends"] = args.backend
        st["last_replays"] = done_total
        _save_state(st)
    print(f"\ndone: {done_total} replays" + (" (persisted)" if args.write else " (NOT persisted; add --write)"))


if __name__ == "__main__":
    main()
