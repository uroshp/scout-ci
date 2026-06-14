"""Email digest of material changes — a deterministic side-effect in CODE (not an
agent tool, per the control line). Uses a transactional email API (Resend).

SAFE BY DEFAULT: send_digest is a no-op (returns the rendered payload without sending)
unless RESEND_API_KEY + SCOUT_ALERT_TO are configured AND dry_run is False. So dev/test
runs and unconfigured environments can never email anyone.
"""
import httpx

from scout import config

RESEND_ENDPOINT = "https://api.resend.com/emails"


def render_digest(competitor: str, alerts: list[dict]) -> tuple[str, str]:
    """Subject + plain-text body. One material change per block, each with its so-what."""
    n = len(alerts)
    subject = f"Scout: {n} material change{'s' if n != 1 else ''} for {competitor}"
    lines = [f"{n} material change{'s' if n != 1 else ''} detected for {competitor}.", ""]
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


def send_digest(competitor: str, alerts: list[dict], dry_run: bool = True) -> dict:
    """Send ONE digest of the run's material deltas. No-op (dry) unless fully configured.
    Returns a result dict; never raises on a missing-config path."""
    if not alerts:
        return {"sent": False, "reason": "no material changes"}
    subject, body = render_digest(competitor, alerts)
    to, key = config.ALERT_EMAIL_TO, config.RESEND_API_KEY

    if dry_run or not key or not to:
        return {"sent": False, "reason": "dry_run or unconfigured (no email sent)",
                "subject": subject, "to": to, "preview": body}

    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": config.ALERT_EMAIL_FROM, "to": [to], "subject": subject, "text": body},
            timeout=20,
        )
        return {"sent": resp.status_code < 300, "status": resp.status_code, "subject": subject}
    except Exception as e:
        return {"sent": False, "reason": f"send error: {type(e).__name__}", "subject": subject}


def _oneline(text, limit: int = 280) -> str:
    s = " ".join(str(text or "").split())
    return (s[:limit] + "…") if len(s) > limit else s


def render_propagation_proposals(slug: str, meta: dict, decisions: list[dict]) -> tuple[str, str]:
    """Subject + body for the REVIEW-mode approval email: each judge-confirmed proposal with where
    (card + section), what (op), how it looks (old→new prose), and the judge's reasoning. The card
    is untouched; these await the human's approval."""
    me, comp = meta.get("my_company"), meta.get("competitor")
    card = f"{me} vs {comp}" if me else (comp or slug)
    n = len(decisions)
    subject = f"Scout: {n} proposed card update{'s' if n != 1 else ''} awaiting approval — {card}"
    lines = [f"Propagation proposed {n} rep-facing change{'s' if n != 1 else ''} for {card}.",
             "The card is UNCHANGED — these need your approval before they go live.", ""]
    for d in decisions:
        op = str(d.get("operation", "")).upper()
        zone = f" / {d.get('zone')}" if d.get("zone") else ""
        lines.append(f"• {op} in {d.get('section', '')}{zone}  ({d.get('subject_key')})")
        if d.get("old_text"):
            lines.append(f"    OLD: {_oneline(d['old_text'])}")
        if d.get("operation") != "retire":
            lines.append(f"    NEW: {_oneline(d.get('new_text'))}")
        if d.get("judge_reason"):
            lines.append(f"    Judge: {_oneline(d['judge_reason'])}")
        if d.get("trigger_source_url"):
            lines.append(f"    From: {d['trigger_source_url']}")
        lines.append("")
    lines.append('To apply any of these, tell Claude in a session — e.g. '
                 f'"approve the {decisions[0].get("section", "objection")} update on {card}".')
    lines.append("— Scout (proposed by the authorship judge; awaiting your approval)")
    return subject, "\n".join(lines)


def send_propagation_proposals(slug: str, meta: dict, decisions: list[dict], dry_run: bool = True) -> dict:
    """Email the run's judge-confirmed propagation proposals (REVIEW mode). No-op (dry) unless fully
    configured; never raises on a missing-config path."""
    if not decisions:
        return {"sent": False, "reason": "no proposals"}
    subject, body = render_propagation_proposals(slug, meta, decisions)
    to, key = config.ALERT_EMAIL_TO, config.RESEND_API_KEY
    if dry_run or not key or not to:
        return {"sent": False, "reason": "dry_run or unconfigured (no email sent)",
                "subject": subject, "to": to, "preview": body}
    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": config.ALERT_EMAIL_FROM, "to": [to], "subject": subject, "text": body},
            timeout=20,
        )
        return {"sent": resp.status_code < 300, "status": resp.status_code, "subject": subject}
    except Exception as e:
        return {"sent": False, "reason": f"send error: {type(e).__name__}", "subject": subject}
