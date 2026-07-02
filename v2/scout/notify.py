"""Email digest of material changes — a deterministic side-effect in CODE (not an
agent tool, per the control line). Uses a transactional email API (Resend).

SAFE BY DEFAULT: send_digest is a no-op (returns the rendered payload without sending)
unless RESEND_API_KEY + SCOUT_ALERT_TO are configured AND dry_run is False. So dev/test
runs and unconfigured environments can never email anyone.
"""
import difflib
import smtplib
from email.message import EmailMessage

import httpx

from scout import config

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _dispatch(subject: str, body: str, dry_run: bool = True) -> dict:
    """Send one owner alert to ALERT_EMAIL_TO, preferring the owner's own Gmail (SMTP + app
    password — no third-party service) and falling back to Resend. SAFE BY DEFAULT: a no-op (returns
    a preview) when dry_run is set or nothing is configured; never raises on a config/send path."""
    to = config.ALERT_EMAIL_TO
    if dry_run or not to:
        return {"sent": False, "reason": "dry_run or no recipient (no email sent)",
                "subject": subject, "to": to, "preview": body}

    guser, gpass = config.GMAIL_USER, (config.GMAIL_APP_PASSWORD or "").replace(" ", "")
    if guser and gpass:
        try:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = guser, to, subject
            msg.set_content(body)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
                s.login(guser, gpass)
                s.send_message(msg)
            return {"sent": True, "via": "gmail", "subject": subject}
        except Exception as e:
            return {"sent": False, "via": "gmail", "reason": f"send error: {type(e).__name__}: {e}",
                    "subject": subject}

    key = config.RESEND_API_KEY
    if key:
        try:
            resp = httpx.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"from": config.ALERT_EMAIL_FROM, "to": [to], "subject": subject, "text": body},
                timeout=20,
            )
            return {"sent": resp.status_code < 300, "via": "resend",
                    "status": resp.status_code, "subject": subject}
        except Exception as e:
            return {"sent": False, "via": "resend", "reason": f"send error: {type(e).__name__}",
                    "subject": subject}

    return {"sent": False, "reason": "no email backend configured (Gmail or Resend)",
            "subject": subject, "preview": body}


def _card_label(meta: dict) -> str:
    """Name the brief the way the rep thinks of it: 'Mistral vs OpenAI', not just 'OpenAI'. The same
    competitor (e.g. OpenAI) appears on several cards, so the label must say WHICH card."""
    me, comp = meta.get("my_company"), meta.get("competitor")
    return f"{me} vs {comp}" if me else (comp or "this card")


def render_digest(meta: dict, alerts: list[dict], deferred_note: str | None = None) -> tuple[str, str]:
    """Subject + plain-text body. One material change per block, each with its so-what. Labeled by
    CARD ('Mistral vs OpenAI'), since a competitor's news can land on more than one brief.
    `deferred_note` is the consequentiality gate's audit line (a routine run deferred N routed
    updates) — a deferral is never silent."""
    comp = meta.get("competitor") or "the competitor"
    card = _card_label(meta)
    n = len(alerts)
    subject = f"Scout: {n} material change{'s' if n != 1 else ''} on {card}"
    lines = [f"{n} material change{'s' if n != 1 else ''} detected on the {card} brief "
             f"(competitor: {comp}).", ""]
    for a in alerts:
        sev = f"[{a['severity'].upper()}] " if a.get("severity") else ""
        lines.append(f"• {sev}{a.get('headline', a.get('subject_key', 'change'))}")
        old, new = a.get("old_value"), a.get("new_value")
        if old or new:
            lines.append(f"    {old} → {new}")
        if a.get("so_what"):
            lines.append(f"    So what: {a['so_what']}")
        if a.get("source_url"):
            lines.append(f"    Source: {a['source_url']}")
        lines.append("")
    if deferred_note:
        lines += [deferred_note, ""]
    lines.append("— Scout (every claim verified against its source)")
    return subject, "\n".join(lines)


def send_selfserve_ready(to: str, job_id: str, label: str | None = None) -> dict:
    """Email a self-serve user that their report is ready, with the deep link back to it. Sent
    from the ACTION (not the app) because the user may have closed the tab. SAFE BY DEFAULT: a
    no-op unless RESEND_API_KEY and a recipient are both present, so unconfigured runs send nothing.
    Never raises — a notification failure must not fail the job."""
    key = config.RESEND_API_KEY
    if not key or not to:
        return {"sent": False, "reason": "unconfigured or no recipient (no email sent)"}
    link = f"{config.SELFSERVE_APP_URL.rstrip('/')}/?job={job_id}"
    what = f": {label}" if label else ""
    subject = f"Your Scout battlecard is ready{what}"
    body = (
        f"Your competitive battlecard{what} is ready.\n\n"
        f"View it here: {link}\n\n"
        "Every claim was verified against its source; anything that couldn't be verified was cut "
        "and logged in the Cut Log.\n\n"
        "— Scout"
    )
    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": config.ALERT_EMAIL_FROM, "to": [to], "subject": subject, "text": body},
            timeout=20,
        )
        return {"sent": resp.status_code < 300, "status": resp.status_code}
    except Exception as e:
        return {"sent": False, "reason": f"send error: {type(e).__name__}"}


def send_digest(meta: dict, alerts: list[dict], dry_run: bool = True,
                deferred_note: str | None = None) -> dict:
    """Send ONE digest of the run's material deltas. No-op (dry) unless fully configured.
    Returns a result dict; never raises on a missing-config path."""
    if not alerts:
        return {"sent": False, "reason": "no material changes"}
    subject, body = render_digest(meta, alerts, deferred_note=deferred_note)
    return _dispatch(subject, body, dry_run=dry_run)


def _block(text) -> str:
    """Full claim prose, indented, with its line breaks preserved — NEVER truncated. The human
    needs the WHOLE thing to assess a proposed edit."""
    return "\n".join(("  " + ln).rstrip() for ln in str(text or "").strip().splitlines())


def _flat(text) -> str:
    return " ".join(str(text or "").split())


def _change_summary(old, new) -> str:
    """A scannable word-level delta of old→new: additions as [+ ...], removals as [- ...], a swap as
    [- ... → + ...], with long unchanged runs collapsed to 'first … last'. Lets the reader catch what
    actually changed without re-reading the whole NEW block. Plain-text safe."""
    o, n = str(old or "").split(), str(new or "").split()
    if not o:
        return "(new addition) " + " ".join(n)
    parts = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=o, b=n, autojunk=False).get_opcodes():
        if tag == "equal":
            run = n[j1:j2]
            parts.append(" ".join(run) if len(run) <= 6 else f"{run[0]} … {run[-1]}")
        elif tag == "insert":
            parts.append("[+ " + " ".join(n[j1:j2]) + "]")
        elif tag == "delete":
            parts.append("[- " + " ".join(o[i1:i2]) + "]")
        else:  # replace
            parts.append("[- " + " ".join(o[i1:i2]) + " → + " + " ".join(n[j1:j2]) + "]")
    return " ".join(parts)


def render_propagation_proposals(slug: str, meta: dict, decisions: list[dict],
                                 exhausted: list[dict] = None,
                                 unjudged: list[dict] = None) -> tuple[str, str]:
    """Subject + body for the REVIEW-mode approval email: each judge-confirmed proposal with where
    (card + section), what (op), how it looks (the FULL old→new prose, never truncated), and the
    judge's reasoning, spaced for reading. The card is untouched; these await the human's approval.

    `exhausted` are rewrite-loop failures on ACT-GRADE facts (judge rejected, rewrite rejected
    again): the card was NOT changed and nothing else will surface them — this email is the loud
    signal (2026-07-01: two Sonnet-5 pricing ops died silently in the decision log).

    `unjudged` are drafts the judge NEVER ruled on (judge_unavailable — both the primary and the
    fallback model failed to return verdicts, the 2026-07-01 Opus outage): drafted, unverified,
    unapplied. The human is the judge of last resort — approve with allow_unjudged only after
    reading the prose."""
    exhausted, unjudged = exhausted or [], unjudged or []
    me, comp = meta.get("my_company"), meta.get("competitor")
    card = f"{me} vs {comp}" if me else (comp or slug)
    n = len(decisions)
    rule = "─" * 48
    subject = (f"Scout: {n} proposed card update{'s' if n != 1 else ''} awaiting approval"
               + (f" (+{len(exhausted)} authoring-failed)" if exhausted else "")
               + (f" (+{len(unjudged)} unverified)" if unjudged else "") + f" — {card}")
    if decisions:
        out = [f"Propagation proposed {n} rep-facing change{'s' if n != 1 else ''} for {card}.",
               "The card is UNCHANGED — these need your approval before they go live.", "", rule]
    else:
        bits = ([f"{len(exhausted)} op(s) on a material fact FAILED authoring"] if exhausted else []) \
             + ([f"{len(unjudged)} drafted update(s) could NOT be verified (judge unavailable)"]
                if unjudged else [])
        out = [f"Propagation confirmed no changes for {card}, but " + " and ".join(bits)
               + " — details below.", "", rule]
    for d in decisions:
        op = str(d.get("operation", "")).upper()
        zone = f" / {d.get('zone')}" if d.get("zone") else ""
        kind = f"  [{d.get('change_kind')}]" if d.get("change_kind") else ""
        out += ["", f"{op} in {d.get('section', '')}{zone}{kind}", f"({d.get('subject_key')})"]
        # Scannable delta first ([+ added] / [- removed]) so the changed bit jumps out, then the full
        # WAS/NEW for context — history kept, edit highlighted.
        if d.get("operation") != "retire" and (d.get("old_text") or d.get("new_text")):
            out += ["", "WHAT CHANGED:", _block(_change_summary(d.get("old_text"), d.get("new_text")))]
        if d.get("old_text"):
            out += ["", "WAS:", _block(d["old_text"])]
        if d.get("operation") != "retire":
            out += ["", "NEW:", _block(d.get("new_text"))]
        # The one-line note the LEFT updates panel will show — especially load-bearing for a RETIRE,
        # so a removal is explained, never a silent disappearance.
        if d.get("feed_note"):
            out += ["", f"Updates-feed note: {_flat(d['feed_note'])}"]
        if d.get("judge_reason"):
            out += ["", f"Judge: {_flat(d['judge_reason'])}"]
        if d.get("trigger_source_url"):
            out += ["", f"From: {d['trigger_source_url']}"]
        out += ["", rule]
    for d in exhausted:
        op = str(d.get("operation", "")).upper()
        zone = f" / {d.get('zone')}" if d.get("zone") else ""
        kind = f"  [{d.get('change_kind')}]" if d.get("change_kind") else ""
        att = d.get("attempts") or []
        out += ["", "✗ AUTHORING FAILED — NOT applied, needs your eyes",
                f"{op} in {d.get('section', '')}{zone}{kind}", f"({d.get('subject_key')})"]
        if d.get("trigger_source_url"):
            out += [f"Fact: {d['trigger_source_url']}"]
        out += ["", f"The judge rejected {len(att)} attempt(s) to write this change:"]
        for k, a in enumerate(att):
            label = f"attempt {k + 1}" + (" (rewrite)" if k else "")
            out += [f"  {label}: {_flat(a.get('reason') or '(no reason recorded)')}"]
        last_prose = next((a.get("claim") for a in reversed(att) if a.get("claim")), None)
        if last_prose:
            out += ["", "LAST ATTEMPT PROSE:", _block(last_prose)]
        out += ["", "The card was NOT changed. If this fact matters, edit the card manually "
                    "or re-run.", "", rule]
    for d in unjudged:
        op = str(d.get("operation", "")).upper()
        zone = f" / {d.get('zone')}" if d.get("zone") else ""
        kind = f"  [{d.get('change_kind')}]" if d.get("change_kind") else ""
        out += ["", "⚠ DRAFTED BUT UNVERIFIED — the judge was unavailable",
                f"{op} in {d.get('section', '')}{zone}{kind}", f"({d.get('subject_key')})"]
        if d.get("trigger_source_url"):
            out += [f"Fact: {d['trigger_source_url']}"]
        if d.get("old_text"):
            out += ["", "WAS:", _block(d["old_text"])]
        out += ["", "DRAFTED (no judge ruled on this — read it before trusting it):",
                _block(d.get("new_text"))]
        out += ["", "The card was NOT changed. To apply after checking the prose yourself, "
                    "approve it in a session with allow_unjudged.", "", rule]
    if decisions:
        out += ["",
                f'To apply, tell Claude in a session — e.g. "approve the '
                f'{decisions[0].get("section", "objection")} update on {card}".',
                "— Scout (proposed by the authorship judge; awaiting your approval)"]
    else:
        out += ["", "— Scout (nothing to approve; the failures above need a human decision)"]
    return subject, "\n".join(out)


def send_propagation_proposals(slug: str, meta: dict, decisions: list[dict], dry_run: bool = True,
                               exhausted: list[dict] = None, unjudged: list[dict] = None) -> dict:
    """Email the run's judge-confirmed propagation proposals (REVIEW mode), plus any rewrite-loop
    EXHAUSTED failures and any UNJUDGED drafts (judge unavailable) — both must reach the owner even
    when nothing was confirmed: a dropped material edit is never silent. No-op (dry) unless fully
    configured; never raises on a missing-config path."""
    if not decisions and not exhausted and not unjudged:
        return {"sent": False, "reason": "no proposals"}
    subject, body = render_propagation_proposals(slug, meta, decisions, exhausted=exhausted,
                                                 unjudged=unjudged)
    return _dispatch(subject, body, dry_run=dry_run)


def render_strategic_shift(meta: dict, lead: dict) -> tuple[str, str]:
    """Subject + body for the STRATEGIC-SHIFT email (strategy layer): the single most strategic lead
    the brief should open with now, with the stress-test and the current lead it would replace. A
    proposal — the card is untouched until the human approves it in-session."""
    card = _card_label(meta)
    rule = "─" * 48
    subject = f"Scout: STRATEGIC SHIFT proposed — {card}"
    st = lead.get("stress_test") or {}
    out = [f"A material change may have shifted the LEAD argument for {card}.",
           "The card is UNCHANGED — this is the proposed new Today's-angle lead, for your approval.",
           "", rule, "",
           "PROPOSED LEAD (rep-facing, this is what goes on the card):",
           f"  {_flat(lead.get('headline'))}",
           f"  {_flat(lead.get('proof'))}",
           f"  Say:  {_flat(lead.get('soundbite'))}",
           f"  Move: {_flat(lead.get('move'))}",
           "", rule, "",
           "WHY (for you, not shown to the rep):",
           f"  Most strategic: {_flat(lead.get('why_most_strategic'))}",
           f"  Freshness: {_flat(lead.get('freshness_note'))}",
           f"  Stress test: counter — {_flat(st.get('strongest_counter'))}",
           f"               survives={st.get('survives')} — {_flat(st.get('how'))}",
           f"  Replaces: {_flat(lead.get('supersedes'))}",
           "", rule, "",
           f'To apply, tell Claude: "apply the strategic lead on {card}".',
           "— Scout (strategic pass, Opus; awaiting your approval)"]
    return subject, "\n".join(out)


def send_strategic_shift(meta: dict, lead: dict, dry_run: bool = True) -> dict:
    """Email the run's proposed strategic lead. No-op (dry) unless configured; never raises."""
    if not lead:
        return {"sent": False, "reason": "no lead"}
    subject, body = render_strategic_shift(meta, lead)
    return _dispatch(subject, body, dry_run=dry_run)
