"""Lanes canary (2026-09-30): did today's unattended lanes do their job? Checks the ARTIFACTS, not
the process status: a green workflow that wrote to the wrong place, or a replay that scored a
backlog, both exit 0. Emails "Scout could not run: lanes canary" only when something is missing;
silence means every lane left the evidence it should. Runs on the mini after the replay window
(launchd com.urosh.scout-lanes-canary, 09:00 PT). $0: no model calls.

Checks (all skipped on a monitor skip day, i.e. Sunday, except the app probe):
  1. monitor: a cost ledger dated today at the PRODUCTION path (costs/<today>T*.json) and none of
     today's ledgers under rc/ from a main run (rc/costs/<today>* without an rc build is a misfile).
  1b. steps (2026-10-03): the step table inside that ledger has a row per checked card and no
     `failed` step (a crashed step inside a green workflow).
  2. capture: a call bundle dated today at calls/<month>/monitor_<today>*.json.
  3. replay: ~/scout-replay/state.json last_run is today AND its log's "newest bundle" line for
     today's run names a bundle dated today (the lane scored fresh calls, not a backlog).
  4. app: the viewer and the engine answer their health checks.
Usage: python scripts/lanes_canary.py [--dry] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime

import httpx

from scout import config, notify, selfserve

VIEWER = os.environ.get("SCOUT_CANARY_VIEWER", "https://agent-scout.ai/healthcheck")
ENGINE = os.environ.get("SCOUT_CANARY_ENGINE", "https://scout-engine-y5fw7otaqa-uw.a.run.app/healthcheck")
REPLAY_DIR = os.path.expanduser(os.environ.get("SCOUT_REPLAY_DIR", "~/scout-replay"))


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
    has a row, and no row says `failed`. A green workflow with a crashed step (the 10/3 audience
    crash: four cards, no email, green canary) is what this catches. Rehearsal ledgers live under
    rehearsal/ and are not read here."""
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
            continue
        failed = [r for r in steps if r.get("status") == "failed"]
        for r in failed:
            problems.append(f"steps: {c.get('slug')} {r.get('step')} FAILED: {str(r.get('detail') or '')[:160]}")
    return problems


def check_lifecycle(day: date) -> list[str]:
    """The lifecycle audit of today's PRODUCTION run (2026-10-04): the run wrote lifecycle/<stamp>.json
    and its verdict is not RED. A missing audit on a day with a ledger is itself a problem: the step
    that follows every finding to the card and the evals did not run."""
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
        if doc.get("verdict") == "RED":
            problems.append(f"lifecycle: {n} RED: {str(doc.get('summary') or '')[:300]}")
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


def check_replay(day: date) -> list[str]:
    problems = []
    try:
        st = json.load(open(os.path.join(REPLAY_DIR, "state.json")))
        last = str(st.get("last_run") or "")[:10]
    except Exception as e:
        return [f"replay: state.json unreadable ({type(e).__name__})"]
    if _replay_running():
        # The window is wide on purpose (2026-10-02: "let's not artificially limit anything"), so a
        # replay can still be working at the 09:00 pass. In progress is not a finding; the 15:15
        # pass, after the backstop deadline, checks the finished run.
        return []
    if last != day.isoformat():
        problems.append(f"replay: last_run is {last or 'unknown'}, not today (the 05:15 replay did not run)")
        return problems
    try:
        log = open(os.path.join(REPLAY_DIR, "launchd.out.log")).read()
    except OSError:
        return problems + ["replay: launchd.out.log unreadable"]
    today_log = log[log.find(f"{day.isoformat()} ") :] if f"{day.isoformat()} " in log else ""
    newest = re.findall(r"newest bundle: (\S+)", today_log)
    if not newest:
        problems.append("replay: today's run printed no 'newest bundle' line (old runner or aborted before listing)")
    elif not any(n.startswith(day.strftime("%Y%m%d")) for n in newest):
        problems.append(f"replay: scored a backlog, newest bundle {newest[-1]} is not today's")
    problems += arm_problems(today_log)
    if "=== replay end ===" not in today_log:
        problems.append("replay: no '=== replay end ===' today (aborted or still running)")
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
        notify.send_could_not_run("lanes canary", f"{len(problems)} lane check(s) failed on {day}",
                                  "\n".join(problems), dry_run=dry)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
