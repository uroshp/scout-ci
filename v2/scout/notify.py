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
    subject = f"Scout — {n} material change{'s' if n != 1 else ''}: {competitor}"
    lines = [f"{n} material change{'s' if n != 1 else ''} detected for {competitor}.", ""]
    for a in alerts:
        lines.append(f"• {a.get('headline', a.get('subject_key', 'change'))}")
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
