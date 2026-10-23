"""Lanes canary (2026-09-30): did today's unattended lanes do their job? Checks the ARTIFACTS, not
the process status: a green workflow that wrote to the wrong place, or a replay that scored a
backlog, both exit 0. Emails "Scout could not run: lanes canary" only when something is missing;
silence means every lane left the evidence it should. Runs on the mini from ~/scout-tools/scout-canary
(launchd com.urosh.scout-canary, 09:00 and 15:15 PT). $0: no model calls.

Checks (all skipped on a monitor skip day, i.e. Sunday, except the app probe):
  1. monitor: a cost ledger dated today at the PRODUCTION path (costs/<today>T*.json) and none of
     today's ledgers under rc/ from a main run (rc/costs/<today>* without an rc build is a misfile).
  1b. steps (2026-10-03): the step table inside that ledger has a row per checked card. A step that
     did not finish is already a line in the morning FYI (email policy 2026-10-10), so it is not
     repeated here; a card with no step table at all is.
  2. capture: a call bundle dated today at calls/<month>/monitor_<today>*.json.
  3. replay: the newest run in ~/scout-replay/launchd.out.log (the lane fires at 23:00 PT) ended, state.json
     advanced within 30 h, its "newest bundle" line is that day's capture or the day before's (a backlog
     scored instead is a finding), every arm left an exit line, the arbiter ruled, the mirror pulled.
  4. app: the viewer and the engine answer their health checks.
Usage: python scripts/lanes_canary.py [--dry] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import httpx

from scout import config, notify, selfserve

VIEWER = os.environ.get("SCOUT_CANARY_VIEWER", "https://agent-scout.ai/healthcheck")
ENGINE = os.environ.get("SCOUT_CANARY_ENGINE", "https://scout-engine-y5fw7otaqa-uw.a.run.app/healthcheck")
REPLAY_DIR = os.path.expanduser(os.environ.get("SCOUT_REPLAY_DIR", "~/scout-replay"))
_now = datetime.now          # patched by tests


def _today(argv) -> date:
    if "--date" in argv:
        return date.fromisoformat(argv[argv.index("--date") + 1])
    return date.today()


def check_monitor(day: date) -> list[str]:
    stamp = day.strftime("%Y%m%d")
    problems = []
    names = selfserve.list_data("costs") or []
    if not any(n.startswith(stamp) for n in names):
        problems.append(f"monitor: no cost ledger costs/{stamp}T*.json (the 4 AM run did not write to the production paths)")
    misfiled = [n for n in (selfserve.list_data("rc/costs") or []) if n.startswith(stamp)]
    if misfiled:
        problems.append(f"monitor: {len(misfiled)} cost ledger(s) dated today under rc/costs/ ({', '.join(misfiled)}); a main run misfiled, or an rc run happened")
    return problems


def check_steps(day: date) -> list[str]:
    """The step table inside today's PRODUCTION cost ledger (2026-10-03): every card the run checked
    has a row. A failed step inside the table is the FYI's job (one plain line in run health, and the
    FYI always goes out when health is not clean, 2026-10-10); what this catches is a card whose
    steps were never recorded at all. Rehearsal ledgers live under rehearsal/ and are not read here."""
    stamp = day.strftime("%Y%m%d")
    names = sorted(n for n in (selfserve.list_data("costs") or []) if n.startswith(stamp))
    if not names:
        return []                                   # check_monitor already reports the missing ledger
    problems = []
    try:
        doc = json.loads(selfserve.read_data(f"costs/{names[-1]}") or "{}")
    except Exception as e:
        return [f"steps: today's ledger costs/{names[-1]} is unreadable ({type(e).__name__})"]
    cards = doc.get("cards") or []
    if cards and not any("steps" in c for c in cards):
        return []                                   # a ledger written by code that predates the step table
    for c in cards:
        steps = c.get("steps") or []
        if not steps:
            problems.append(f"steps: {c.get('slug')} has no step table in the ledger (the check did not record its steps)")
    return problems


def check_lifecycle(day: date) -> list[str]:
    """The lifecycle audit of today's PRODUCTION run (2026-10-04): the run wrote lifecycle/<stamp>.json.
    A missing audit on a day with a ledger is the problem: the step that follows every finding to the
    card and the evals did not run. A RED verdict is said in the morning FYI, rule by rule (email
    policy 2026-10-10), so it is not repeated here."""
    stamp = day.strftime("%Y%m%d")
    ledgers = sorted(n for n in (selfserve.list_data("costs") or []) if n.startswith(stamp))
    if not ledgers:
        return []
    audits = sorted(n for n in (selfserve.list_data("lifecycle") or []) if n.startswith(stamp))
    if not audits:
        try:
            doc = json.loads(selfserve.read_data(f"costs/{ledgers[-1]}") or "{}")
            if not any("steps" in c for c in (doc.get("cards") or [])):
                return []                           # a ledger from code that predates the audit
        except Exception:
            pass
        return [f"lifecycle: no audit for today's run(s) ({', '.join(ledgers)}); the finding trace was not checked"]
    problems = []
    for n in audits:
        try:
            doc = json.loads(selfserve.read_data(f"lifecycle/{n}") or "{}")
        except Exception as e:
            problems.append(f"lifecycle: {n} unreadable ({type(e).__name__})")
            continue
        if not doc.get("verdict"):
            problems.append(f"lifecycle: {n} carries no verdict (the audit did not finish)")
    return problems


SENSOR_READY_MARK = "sensors/_compare/_ready_notified.json"


def sensor_checkpoint(day: date, dry: bool) -> None:
    """The sensor cutover checkpoint, tracked FOR the owner (Uroš 2026-10-03: "track that for me and
    report, so I don't have to deal with it"): when the shadow streak reaches the gate, one email
    with the numbers the call needs; never twice for the same streak. Nothing here fails the canary."""
    try:
        from scout import config as _cfg
        from scout.sensors import compare
        if getattr(_cfg, "SENSORS_MODE", "off") != "shadow":
            return
        st = compare.load_streak()
        if not st.get("ready"):
            return
        mark = json.loads(selfserve.read_data(SENSOR_READY_MARK) or "{}")
        if mark.get("streak_date") == st.get("updated_at", "")[:10]:
            return
        runs = [r for r in st.get("runs", []) if not r.get("baseline")]
        clean = runs[-int(st.get("gate_runs") or 7):]
        alerts = sum(int(r.get("triage_subst") or 0) for r in clean)
        cost = sum(float(r.get("screen_cost") or 0) for r in clean)
        n_cards = sum(r.get("cards", 0) for r in clean) or 1
        esc_s = sum(int(r.get("screen_escalating_cards") or 0) for r in clean)
        esc_t = sum(int(r.get("triage_escalating_cards") or 0) for r in clean)
        body = (f"Sensors have run in shadow next to the morning triage and reached the checkpoint: "
                f"{st.get('clean_streak')} consecutive clean runs (gate {st.get('gate_runs')}), {day.isoformat()}.\n\n"
                f"Over those runs: {n_cards} card checks, {alerts} substantial triage candidates, "
                f"0 alerts that landed without a finding behind them, {sum(int(r.get('errors') or 0) for r in clean)} source errors, "
                f"screen cost ${cost:.2f} in total.\n\n"
                f"Cost projection for gate mode: the screen would have sent {esc_s} of {n_cards} card checks to the paid judge "
                f"({esc_s / n_cards:.0%}); triage sent {esc_t} ({esc_t / n_cards:.0%}). At about $0.80 per escalation that is "
                f"${esc_s / max(1, len(clean)) * 0.80:.2f} a day of materiality in gate against ${esc_t / max(1, len(clean)) * 0.80:.2f} today, "
                f"plus about $0.15 a day of screens, minus the daily triage that stops (about $1.10 a day today). "
                f"The run ceiling bounds any morning at ${_cfg.RUN_MAX_USD:.0f}.\n\n"
                f"Your call: flip the repo variable SCOUT_SENSORS from shadow to gate (the screen's candidates replace the daily "
                f"model search; the weekly sweep keeps auditing), or keep shadow longer. Nothing changes until you say so.\n\n"
                f"Streak file: sensors/_compare/streak.json; compare records: sensors/_compare/<date>/<card>.json.")
        notify._dispatch("Scout: sensors reached the cutover checkpoint, your call", body, dry_run=dry)
        if not dry:
            selfserve.write_data(SENSOR_READY_MARK, json.dumps({"streak_date": st.get("updated_at", "")[:10], "notified_at": datetime.now().isoformat(timespec="seconds")}),
                                 "sensors: checkpoint notified")
        print(f"[canary] sensor checkpoint reached: emailed ({'dry' if dry else 'sent'})")
    except Exception as e:
        print(f"[canary] sensor checkpoint check skipped ({type(e).__name__}: {e})")


def check_capture(day: date) -> list[str]:
    stamp = day.strftime("%Y%m%d")
    month = day.strftime("%Y-%m")
    names = selfserve.list_data(f"calls/{month}") or []
    if not any(n.startswith(f"monitor_{stamp}") for n in names):
        return [f"capture: no call bundle calls/{month}/monitor_{stamp}*.json (the replay lane has nothing fresh to score)"]
    return []


def latest_replay_run(log: str) -> str:
    """The newest run in launchd.out.log: from its '=== replay start' line to the end of the file. The
    lane fires at 23:00 PT (2026-10-23; 05:15 before that), so the run the 09:00 pass judges began the
    day before, and a by-hand catch-up can begin at any hour: a run is found by its marker, not by date."""
    i = log.rfind("=== replay start")
    if i < 0:
        return ""
    return log[log.rfind("\n", 0, i) + 1:]


def check_replay(day: date) -> list[str]:
    """Last night's replay left its evidence: state.json advanced within the last 30 h; the newest run in
    the log scored the newest bundle that existed when it began (that day's 4 AM capture, or the day
    before's after a Sunday or for a catch-up started after midnight); every arm left an exit line; the
    run ended; the arbiter ruled; the mirror pulled."""
    try:
        st = json.load(open(os.path.join(REPLAY_DIR, "state.json")))
        last_run = datetime.fromisoformat(str(st.get("last_run") or ""))
    except Exception as e:
        return [f"replay: state.json unreadable ({type(e).__name__})"]
    try:
        log = open(os.path.join(REPLAY_DIR, "launchd.out.log")).read()
    except OSError:
        return ["replay: launchd.out.log unreadable"]
    run_log = latest_replay_run(log)
    try:
        started = datetime.strptime(run_log[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        started = None
    if _replay_running():
        # A run in progress is not a finding (the 09:00 pass can meet a long night). One still alive
        # 12 h after it began has run through its 06:30 backstop, and is.
        if started and (_now() - started).total_seconds() > 12 * 3600:
            return [f"replay: still running {(_now() - started).total_seconds() / 3600:.0f} h after it began at "
                    f"{started:%H:%M} (through its backstop deadline)"]
        return []
    age_h = (_now() - last_run).total_seconds() / 3600
    if age_h > 30:
        return [f"replay: last_run is {last_run:%Y-%m-%d %H:%M}, {age_h:.0f} h ago (the 23:00 replay did not run)"]
    problems = []
    newest = re.findall(r"newest bundle: (\S+)", run_log)
    fresh = {(started.date() - timedelta(days=d)).strftime("%Y%m%d") for d in (0, 1)} if started else set()
    if not newest:
        problems.append("replay: the last run printed no 'newest bundle' line (old runner or aborted before listing)")
    elif not any(n[:8] in fresh for n in newest):
        problems.append(f"replay: scored a backlog, newest bundle {newest[-1]} is older than the capture before the run began")
    problems += arm_problems(run_log)
    if "=== replay end ===" not in run_log:
        problems.append("replay: the last run has no '=== replay end ===' line (aborted)")
    else:
        problems += arbiter_problems(run_log)
        problems += mirror_problems(run_log)
    return problems


def _replay_running() -> bool:
    """True while run.sh holds its lock and the process named in it is alive."""
    try:
        pid = int(open(os.path.join(REPLAY_DIR, "lock")).read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def arm_problems(today_log: str) -> list[str]:
    """Every configured Ollama arm must leave a trace in today's log: an "<arm> arm exit" line, or
    the runner's explicit skip ("ollama arms skipped ..."). A silent arm is a finding (2026-09-30:
    one oversize call zeroed the Apple arm for days and nothing said so)."""
    from scout import replaybackends
    if not today_log:
        return []
    if "ollama arms skipped" in today_log:
        return []
    out = []
    for name in replaybackends.OLLAMA_MODELS:
        if f"{name} arm exit" not in today_log:
            out.append(f"replay: arm {name} left no exit line today (never started, or the runner predates it)")
    if "apple arm exit" not in today_log and "apple arm skipped" not in today_log:
        out.append("replay: the Apple arm left no exit line today")
    return out


def mirror_problems(today_log: str) -> list[str]:
    """A failed pull of the store's clone (2026-10-19): the read-out and the arbiter then read yesterday's
    copy, and nothing else reports it (the pull never raises by design). Judged only after the run ended."""
    if not today_log or "=== replay end ===" not in today_log:
        return []
    fails = [ln.strip() for ln in today_log.splitlines() if "[store] mirror pull failed" in ln]
    if not fails:
        return []
    why = fails[-1].split("pull failed", 1)[1].strip().strip("();: ")[:140]
    return [f"store mirror: {len(fails)} pull(s) failed today, so the read-out and the arbiter read a stale copy ({why})"]


def arbiter_problems(today_log: str) -> list[str]:
    """The research layer must leave its count in today's log once the run has ended: "[arbiter] N
    arbitrated" with N > 0, or nothing to rule on ("not yet arbitrated 0"), or the daily ceiling
    already reached. A traceback in the arbiter section, a missing count line, or an exit line other
    than 0 is a finding (2026-10-11 to 10-19: the arbiter died on a 403 nine mornings running; run.sh
    logged tail's exit 0 after the pipe and this canary looked only at the arms)."""
    if not today_log or "=== replay end ===" not in today_log:
        return []
    if "--- arbiter ---" not in today_log:
        return ["arbiter: today's run has no arbiter section (an old runner, or the run ended before it)"]
    sec = today_log[today_log.rfind("--- arbiter ---"):]
    count = re.search(r"\[arbiter\] (\d+) arbitrated", sec)
    todo = re.search(r"not yet arbitrated (\d+)", sec)
    exit_line = re.search(r"arbiter exit (\d+)", sec)
    if "Traceback" in sec or count is None:
        errs = [ln.strip() for ln in sec.splitlines() if re.match(r"\s*[\w.]*(Error|Exception)\b", ln)]
        why = errs[-1][:140] if errs else "no '[arbiter] N arbitrated' line"
        return [f"arbiter: did not rule today ({why})"]
    if exit_line and exit_line.group(1) != "0":
        return [f"arbiter: exit {exit_line.group(1)} today after ruling on {count.group(1)}"]
    if int(count.group(1)) == 0 and todo and int(todo.group(1)) > 0 and "daily ceiling reached" not in sec:
        return [f"arbiter: ruled on 0 of {todo.group(1)} pending today (see the replay log)"]
    return []


def check_app() -> list[str]:
    problems = []
    for name, url in (("viewer", VIEWER), ("engine", ENGINE)):
        try:
            r = httpx.get(url, timeout=30, follow_redirects=True)
            if r.status_code != 200:
                problems.append(f"app: {name} {url} -> {r.status_code}")
        except Exception as e:
            problems.append(f"app: {name} {url} -> {type(e).__name__}")
    return problems


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry" in argv
    day = _today(argv)
    skip_day = day.weekday() in config.MONITOR_SKIP_WEEKDAYS
    problems = []
    if not skip_day:
        problems += check_monitor(day)
        problems += check_steps(day)
        problems += check_lifecycle(day)
        problems += check_capture(day)
        problems += check_replay(day)
    problems += check_app()
    sensor_checkpoint(day, dry)
    print(f"[canary] {day} skip_day={skip_day} problems={len(problems)}")
    for p in problems:
        print("  -", p)
    if problems:
        notify.send_could_not_run("lanes canary", f"{len(problems)} lane check(s) did not pass on {day}",
                                  "\n".join(problems), dry_run=dry)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
