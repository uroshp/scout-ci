"""14-DAY EVAL CHECK-IN for the v3.5 shadow-eval judges that may TAKE OVER the deterministic code:
the VERIFICATION challenger (support-judge over grounding) and the AUTHORSHIP judge (propose->judge).

This is the promotion instrument for `docs/eval-exit-criteria.md`: each run reports the current
metrics, the TREND vs the prior check-in, and applies the PRE-REGISTERED go/no-go rule (promote /
continue / diagnose / kill). NOT the consequentiality filter — that is a separate, faster track
(additive, not a code-takeover; spot-checked over days, not weeks).

Reporting is FREE — it reads the stored challenger results + human labels. The challenger MODEL refresh
(spends API budget, bounded) is a SEPARATE step run before this in the scheduled job:
    python scripts/run_challenger.py --run --write --limit 25

    python scripts/eval_checkin.py                 # print the check-in (no spend, no write)
    python scripts/eval_checkin.py --snapshot      # also persist this check-in (enables next trend)
    python scripts/eval_checkin.py --snapshot --email   # also email the report
"""
import argparse
import json
import sys
from datetime import datetime

sys.path.insert(0, ".")
from scout import adjudicate, adjudicate_challenger, challenger, config, notify, selfserve

EVAL_DIR = "eval_checkin"                 # per-check-in snapshots in the private store

# Pre-registered bars (docs/eval-exit-criteria.md). Disagreement precision = when the judge overrules
# the incumbent, is it right (human-confirmed). The niche/overrule cases carry the higher bar.
PRECISION_BAR = 0.80                       # share of overrules a human confirms
MIN_ADJUDICATED = {"verification": 15, "authorship": adjudicate.AUTHORSHIP_GATE}  # sample sufficiency
KILL_STREAK = 3                           # check-ins below bar with no improvement -> conclude it can't take over


def _precision(right: int, adjudicated: int):
    return round(right / adjudicated, 3) if adjudicated else None


def verification_metrics() -> dict:
    sc = challenger.scorecard(adjudicate_challenger.load_results())
    adj = sc.get("adjudication", {})
    return {
        "adjudicated": adj.get("adjudicated", 0),
        "right": adj.get("challenger_right", 0),
        "wrong": adj.get("challenger_wrong", 0),
        "precision": _precision(adj.get("challenger_right", 0), adj.get("adjudicated", 0)),
        "kappa_vs_code": sc.get("kappa_champion_vs_challenger"),
        "disagreements": sc.get("disagreements", 0),
        "pending": len(sc.get("pending_disagreements", [])),
        "slices": sc.get("slices"),
    }


def authorship_metrics() -> dict:
    d = adjudicate.digest()
    return {
        "adjudicated": d.get("adjudicated", 0),
        "right": d.get("judge_right", 0),
        "wrong": d.get("judge_wrong", 0),
        "precision": _precision(d.get("judge_right", 0), d.get("adjudicated", 0)),
        "net_positive": d.get("gate", {}).get("net_positive"),
        "pending": len(d.get("pending", [])),
    }


def verdict(kind: str, cur: dict, prior: dict | None) -> dict:
    """The pre-registered rule. Returns {status, note, no_improve_streak}. Since 2026-09-28 the state
    machine lives in scout/evalrule.py (shared with the on-device model lane); this wrapper passes
    this lane's unchanged constants, so its behaviour is identical (tests pin it)."""
    from scout import evalrule
    return evalrule.verdict(kind, cur, prior, bar=PRECISION_BAR, min_adjudicated=MIN_ADJUDICATED[kind],
                            kill_streak=KILL_STREAK)


def _slices_block(sl: dict | None) -> str:
    """Pre/post derived-capture-fix + per-evidence-mode precision (2026-09-28). The pre-fix period
    is the old capture (interpretations judged blind); post-fix is the measurement that counts."""
    if not sl:
        return ""
    def one(name, x):
        return (f"  - {name}: precision {x['precision']} [{x['challenger_right']} right / "
                f"{x['challenger_wrong']} wrong of {x['adjudicated']} adjudicated; "
                f"{x['disagreements']} disagreements]\n")
    out = f"  - PERIODS (derived-capture fix {sl['fix_stamp'][:10]}):\n"
    out += one("  pre-fix (old capture, interpretations judged blind)", sl["period"]["pre_fix"])
    out += one("  post-fix", sl["period"]["post_fix"])
    out += "  - BY EVIDENCE MODE:\n"
    for m, x in sl["by_evidence_mode"].items():
        out += one(f"  {m}", x)
    return out


def _load_prior() -> dict | None:
    try:
        files = sorted(f for f in selfserve.list_data(EVAL_DIR) if f.endswith(".json"))
        if not files:
            return None
        return json.loads(selfserve.read_data(f"{EVAL_DIR}/{files[-1]}"))
    except Exception as e:
        print(f"[eval] prior snapshot unreadable ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def build(now: datetime) -> tuple[dict, str]:
    prior = _load_prior()
    ver, auth = verification_metrics(), authorship_metrics()
    v_ver = verdict("verification", ver, (prior or {}).get("verification"))
    v_auth = verdict("authorship", auth, (prior or {}).get("authorship"))
    snapshot = {
        "stamp": now.isoformat(timespec="seconds"),
        "prior_stamp": (prior or {}).get("stamp"),
        "verification": {**ver, **{"no_improve_streak": v_ver["no_improve_streak"]}},
        "authorship": {**auth, **{"no_improve_streak": v_auth["no_improve_streak"]}},
        "verdicts": {"verification": v_ver["status"], "authorship": v_auth["status"]},
    }

    def block(name, m, vd):
        return (f"## {name}\n"
                f"- precision (overrule correctness): **{m['precision']}** (bar {PRECISION_BAR})  "
                f"[{m['right']} right / {m['wrong']} wrong of {m['adjudicated']} adjudicated]\n"
                f"- pending to adjudicate: {m['pending']}\n"
                f"- **VERDICT: {vd['status']}** — {vd['note']}\n")

    body = (f"# 14-day eval check-in — {now.date()}\n\n"
            f"Prior check-in: {(prior or {}).get('stamp', 'none (first run)')}\n\n"
            + block("Verification challenger (support over grounding)", ver, v_ver)
            + f"  - alignment κ vs code grader: {ver['kappa_vs_code']} (sanity, not the gate); "
              f"open disagreements: {ver['disagreements']}\n"
            + _slices_block(ver.get("slices")) + "\n"
            + block("Authorship judge (propose→judge)", auth, v_auth)
            + "\nPer-slice κ (by claim type / section) is the next refinement; v1 reports aggregate "
              "disagreement precision. Bar + rule: docs/eval-exit-criteria.md.\n"
            + "\nNOTE: the consequentiality filter is a SEPARATE, faster track — not in this check-in.\n")
    return snapshot, body


def _fmt(x, pct=False):
    if x is None:
        return "n/a"
    return f"{x:.0%}" if pct else str(x)


def brief(snapshot: dict, ver: dict, auth: dict, v_ver: dict, v_auth: dict, model_snap: dict | None) -> str:
    """The executive brief (Uroš, 2026-10-01: "what it is, what it means, proposed next steps").
    One block per lane: the one number that matters, what it means in words, the next step. The
    full tables follow as an appendix and live in the snapshot; this is what gets read."""
    out = ["# Scout evals: the brief", ""]
    sl = ver.get("slices") or {}
    post = (sl.get("period") or {}).get("post_fix") or {}
    pre = (sl.get("period") or {}).get("pre_fix") or {}
    out += ["## 1. Verification challenger: is the second model right when it overrules the code grader?",
            f"- Number: precision **{_fmt(ver.get('precision'))}** of {ver.get('adjudicated')} labelled overrules (bar {PRECISION_BAR}). Verdict: **{v_ver['status']}**."]
    if sl:
        out += [f"- What it means: every labelled item so far is from before the {str(sl.get('fix_stamp', ''))[:10]} fix "
                f"(pre-fix {_fmt(pre.get('precision'))} on {pre.get('adjudicated')}; post-fix {_fmt(post.get('precision'))} on {post.get('adjudicated')} "
                f"with {post.get('disagreements')} disagreement(s) waiting). The number cannot move until post-fix items are labelled."]
    out += [f"- Next step: label the {ver.get('pending')} pending item(s) (`python -m scout.adjudicate_challenger`); read the post-fix line only.", ""]
    out += ["## 2. Authorship judge: when it overrules the proposed update, is it right?",
            f"- Number: precision **{_fmt(auth.get('precision'))}** ({auth.get('right')} right, {auth.get('wrong')} wrong of {auth.get('adjudicated')}). Verdict: **{v_auth['status']}**.",
            ("- What it means: this judge has been LIVE (auto-applying confirmed updates) since 2026-09-29; the number is its running report card, at or above the bar."
             if v_auth["status"] == "ELIGIBLE" else
             "- What it means: below the bar or not yet sustained; the live judge's decisions deserve a look."),
            f"- Next step: {auth.get('pending')} items await labels; label them when convenient, nothing to change otherwise.", ""]
    b = (model_snap or {}).get("brief") or {}
    if b:
        out += ["## 3. On-device models: could a local model do a Scout role?",
                f"- Numbers: {b.get('results')} replays across {len(b.get('arms') or [])} arms, **{b.get('labels')} human labels**, so no verdict exists yet; "
                f"below: did it run the call, agreement with Claude on the judge role (a sanity number), and speed."]
        for a in b.get("arms") or []:
            ja = a.get("judge_agree"); p50 = a.get("judge_p50_ms")
            out.append(f"  - {a['vendor']} {a['tag']}: {a['results']} replays ({a['today']} today); runs {_fmt(a.get('exact_coverage'), pct=True)} of exact calls; "
                       f"judge agreement {_fmt(ja, pct=True)} on {a.get('judge_n') or 0}; judge p50 {int(p50 / 1000) if p50 else 'n/a'} s; "
                       + ("comparable" if a.get("eligible") else "warming up" if a.get("warming_up") else "NOT comparable (ran too few of the set)"))
        out += ["- What it means: the metric (was the local model right when it disagreed) needs blind labels; nothing here is a verdict yet.",
                "- Next step: label 15 judge disagreements blind (`python -m scout.adjudicate_models`), then read precision at the next check-in.", ""]
    out += ["Detail for every number is in the appendix below and in the stored snapshot.", ""]
    return "\n".join(out)


_VERDICT_COLOR = {"ELIGIBLE": ("#2f7d4f", "#e8f3ec"), "ACCUMULATE": ("#8a6322", "#f7efe0"),
                  "BASELINE": ("#34566b", "#e9eff3"), "NOT_ELIGIBLE": ("#b5473b", "#f9e9e7"), "KILL": ("#b5473b", "#f9e9e7")}


def _pill(status: str) -> str:
    fg, bg = _VERDICT_COLOR.get(status, ("#5f5e54", "#f1efe8"))
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:999px;background:{bg};color:{fg};'
            f'font-weight:700;font-size:12px;letter-spacing:.04em">{notify._esc(status)}</span>')


def _row(label: str, value_html: str) -> str:
    if not label:                                   # a full-width row (the per-model table)
        return f'<tr><td colspan="2" style="padding:6px 0;vertical-align:top">{value_html}</td></tr>'
    return (f'<tr><td style="padding:6px 10px 6px 0;color:#5f5e54;white-space:nowrap;vertical-align:top;width:34%">{notify._esc(label)}</td>'
            f'<td style="padding:6px 0;vertical-align:top">{value_html}</td></tr>')


def _lane_card(title: str, question: str, status: str, rows: list[tuple[str, str]], accent: str) -> str:
    inner = (f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">'
             f'<div style="font-weight:700;font-size:16px;color:{accent}">{notify._esc(title)}</div>{_pill(status)}</div>'
             f'<div style="color:#5f5e54;font-size:13px;margin:2px 0 8px">{notify._esc(question)}</div>'
             f'<table style="border-collapse:collapse;width:100%;font-size:14px">' + "".join(_row(k, v) for k, v in rows) + '</table>')
    return notify._hcard(inner, accent=accent)


def brief_html(now: datetime, ver: dict, auth: dict, v_ver: dict, v_auth: dict, model_snap: dict | None) -> str:
    """The brief as the email body (Uroš, 2026-10-01: legible, tables and bullets, colour, only what
    deserves to be there, plain full sentences). One card per lane: what it checks, the number that
    matters, what the number means, the next step. The plain-text part keeps the full appendix."""
    e = notify._esc
    lead = (f'<div style="font-size:20px;font-weight:700;margin:4px 0 2px">Scout evals, {now.strftime("%-d %B %Y")}</div>'
            f'<div style="color:#5f5e54;font-size:14px;margin-bottom:14px">Three evaluation lanes. Each card says what the lane checks, '
            f'the one number that matters, what that number means right now, and the next step.</div>')
    cards = []
    # lane 1
    sl = ver.get("slices") or {}
    post = (sl.get("period") or {}).get("post_fix") or {}
    pre = (sl.get("period") or {}).get("pre_fix") or {}
    meaning = (f"Every labelled item so far predates the {str(sl.get('fix_stamp', ''))[:10]} fix: before it the challenger judged "
               f"interpretations without their parent facts, and it was right only {_fmt(pre.get('precision'))} of the time on those "
               f"{pre.get('adjudicated')} items. Since the fix there are {post.get('disagreements')} new disagreement(s) and none labelled, "
               f"so the number cannot move yet.") if sl else "Waiting on labelled disagreements."
    cards.append(_lane_card("1. Verification challenger", "When a second model overrules the code grader on a verification verdict, is the second model right?",
                            v_ver["status"],
                            [("The number", f"<b>{e(_fmt(ver.get('precision')))}</b> precision on {ver.get('adjudicated')} labelled overrules (bar {PRECISION_BAR})"),
                             ("What it means", e(meaning)),
                             ("Next step", f"Label the {ver.get('pending')} pending item(s) with <code>python -m scout.adjudicate_challenger</code>, then judge this lane on the post-fix line only.")],
                            "#8a6322" if v_ver["status"] == "ACCUMULATE" else "#34566b"))
    # lane 2
    live = v_auth["status"] == "ELIGIBLE"
    cards.append(_lane_card("2. Authorship judge", "When the judge overrules a proposed card update, is it right?",
                            v_auth["status"],
                            [("The number", f"<b>{e(_fmt(auth.get('precision')))}</b> precision: {auth.get('right')} right, {auth.get('wrong')} wrong, of {auth.get('adjudicated')} labelled"),
                             ("What it means", e("This judge has been live since 29 September, applying confirmed updates without approval. The number is its running report card and it is above the bar." if live
                                                 else "The judge is below the bar or not yet sustained; its live decisions deserve a look.")),
                             ("Next step", e(f"{auth.get('pending')} items await your labels. Label them when convenient; nothing needs changing otherwise."))],
                            "#2f7d4f" if live else "#b5473b"))
    # lane 3
    b = (model_snap or {}).get("brief") or {}
    if b:
        head = ('<tr>' + "".join(f'<th style="text-align:left;padding:6px 8px;border-bottom:1px solid #e4e1d5;font-size:12px;color:#5f5e54">{e(h)}</th>'
                                 for h in ("Model", "Replays", "Runs the call", "Agrees with Claude (judge)", "Judge speed", "Comparable")) + '</tr>')
        trs = []
        for a in b.get("arms") or []:
            p50 = a.get("judge_p50_ms"); cov = a.get("exact_coverage"); ja = a.get("judge_agree")
            if a.get("eligible"):
                note = '<span style="color:#2f7d4f;font-weight:700">yes</span>'
            elif a.get("warming_up"):
                note = '<span style="color:#8a6322">not yet, warming up</span>'
            else:
                note = f'<span style="color:#b5473b">no, ran {_fmt(cov, pct=True)} of the set</span>'
            trs.append('<tr>' + "".join(f'<td style="padding:6px 8px;border-bottom:1px solid #f0eee6;font-size:13px">{c}</td>' for c in (
                f"<b>{e(a['vendor'])}</b> {e(a['tag'])}", f"{a['results']} ({a['today']} today)",
                e(_fmt(cov, pct=True)), f"{e(_fmt(ja, pct=True))} on {a.get('judge_n') or 0}",
                f"{int(p50 / 1000)} s" if p50 else "n/a", note)) + '</tr>')
        table = f'<table style="border-collapse:collapse;width:100%;margin:6px 0 2px">{head}{"".join(trs)}</table>'
        cards.append(_lane_card("3. On-device models", "Could a model running on the Mac mini take over a Scout role from Claude?",
                                "ACCUMULATE" if not b.get("labels") else "BASELINE",
                                [("The number", f"<b>{b.get('labels')}</b> human labels across {b.get('results')} replays, so there is no verdict yet. "
                                                f"Agreement and speed below are on the comparable set: the {b.get('common_n')} calls every established arm ran under one setting. "
                                                f"An arm that ran under {int((b.get('eligible_coverage') or 0.9) * 100)}% of the calls is not comparable."),
                                 ("", table),
                                 ("What it means", e("Agreement is a sanity check, not the metric. The metric is whether the local model was right when it disagreed, and that needs blind labels.")),
                                 ("Next step", "Label 15 judge disagreements blind with <code>python -m scout.adjudicate_models</code>; precision appears at the next check-in.")],
                                "#34566b"))
    footer = ('<div style="color:#5f5e54;font-size:12px;margin-top:6px">The full tables for every lane are in the plain-text part of this email and in the stored snapshot. '
              'Bars and rules: docs/eval-exit-criteria.md and docs/model-substitution-exit-criteria.md.</div>')
    return notify._hdoc(lead, "".join(cards), footer)


def _model_lane(now: datetime) -> tuple[dict | None, str | None]:
    """The on-device model comparison lane (2026-09-28, scripts/model_checkin.py) rides this
    check-in: same 1st/15th cadence, same email, its own snapshot dir and bars. Fail-soft: any
    error here leaves the two existing lanes untouched."""
    try:
        import model_checkin                      # sibling script; scripts/ is on sys.path when run
        snap, text = model_checkin.build(now)
        return snap, text
    except Exception as e:
        print(f"[eval] model lane skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return None, None


def main() -> None:
    ap = argparse.ArgumentParser(description="14-day eval check-in for the v3.5 takeover judges.")
    ap.add_argument("--snapshot", action="store_true", help="persist this check-in (enables next trend)")
    ap.add_argument("--email", action="store_true", help="email the report (needs SMTP creds)")
    args = ap.parse_args()

    now = datetime.now()
    snapshot, body = build(now)
    model_snap, model_body = _model_lane(now)
    if model_body:
        body += "\n\n---\n\n" + model_body
    html = None
    try:
        ver, auth = verification_metrics(), authorship_metrics()
        prior = _load_prior()
        v1, v2 = verdict("verification", ver, (prior or {}).get("verification")), verdict("authorship", auth, (prior or {}).get("authorship"))
        head = brief(snapshot, ver, auth, v1, v2, model_snap)
        body = head + "\n\n---\n\n# Appendix: full detail\n\n" + body
        html = brief_html(now, ver, auth, v1, v2, model_snap)
    except Exception as e:                      # the brief must never block the check-in
        print(f"[eval] brief skipped ({type(e).__name__}: {e})", file=sys.stderr)
    print(body)

    if args.snapshot:
        try:
            stamp = now.strftime("%Y%m%dT%H%M%S")
            selfserve.write_data(f"{EVAL_DIR}/{stamp}.json",
                                 json.dumps(snapshot, indent=2, default=str, ensure_ascii=False),
                                 f"eval: check-in {stamp}")
            print(f"\n[eval] snapshot persisted -> {EVAL_DIR}/{stamp}.json")
        except Exception as e:
            print(f"[eval] snapshot write failed ({type(e).__name__}: {e})", file=sys.stderr)
        if model_snap is not None:
            try:
                import model_checkin
                path = f"{model_checkin.CHECKIN_DIR}/{now.strftime('%Y%m%dT%H%M%S')}.json"
                selfserve.write_data(path, json.dumps(model_snap, indent=1, default=str, ensure_ascii=False),
                                     f"model-checkin: snapshot {now.date()}")
                print(f"[eval] model-lane snapshot persisted -> {path}")
            except Exception as e:
                print(f"[eval] model-lane snapshot write failed ({type(e).__name__}: {e})", file=sys.stderr)

    if args.email:
        v = snapshot["verdicts"]
        subject = f"Scout eval check-in {now.date()} — verif {v['verification']} / authorship {v['authorship']}"
        res = notify._dispatch(subject, body, dry_run=False, html=html)
        print(f"[eval] email: {res.get('sent')} ({res.get('via') or res.get('reason')})")


if __name__ == "__main__":
    main()
