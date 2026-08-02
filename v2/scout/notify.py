"""Email digest of material changes — a deterministic side-effect in CODE (not an
agent tool, per the control line). Uses a transactional email API (Resend).

SAFE BY DEFAULT: send_digest is a no-op (returns the rendered payload without sending)
unless RESEND_API_KEY + SCOUT_ALERT_TO are configured AND dry_run is False. So dev/test
runs and unconfigured environments can never email anyone.
"""
import difflib
import html as _htmlmod
import smtplib
from email.message import EmailMessage

import httpx

from scout import config

RESEND_ENDPOINT = "https://api.resend.com/emails"

# --- HTML email styling (2026-07-31: the plain-text WHAT-CHANGED diff was unreadable). Inline
# styles only — email clients (Gmail/Apple Mail) ignore <style> blocks. Additions = bold green on a
# light-green highlight so a change is impossible to miss; removals = strikethrough red on light red.
_C_ADD = "color:#1a7f37;background:#d7f5dd;font-weight:700;border-radius:3px;padding:0 2px"
_C_DEL = "color:#b0301c;text-decoration:line-through"
_C_MUTED = "color:#6a6a6a"
_C_LINK = "color:#34566b"
_FONT = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;color:#1c1d16"


def _esc(s) -> str:
    return _htmlmod.escape(str(s or ""))


def _ins(words) -> str:
    return f'<span style="{_C_ADD}">{_esc(" ".join(words))}</span>'


def _del(words) -> str:                     # removals: strikethrough IN PARENTHESES (owner's ask)
    return f'(<span style="{_C_DEL}">{_esc(" ".join(words))}</span>)'


def _diff_html(old, new) -> str:
    """Tracked-changes view of a REVISE: the paragraph read normally, with only the CHANGED words
    called out — additions bold green, removals struck red in (parentheses), unchanged text plain.
    Word-level, HTML-escaped. (Adds are NOT diffed — an add is all-new, so it renders plainly; only
    revises have an old→new to mark.)"""
    o, n = str(old or "").split(), str(new or "").split()
    if not o:
        return _ins(n)
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=o, b=n, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append(_esc(" ".join(n[j1:j2])))         # unchanged: plain, so changes stand out
        elif tag == "insert":
            out.append(_ins(n[j1:j2]))
        elif tag == "delete":
            out.append(_del(o[i1:i2]))
        else:  # replace: removed (struck, in parens) then added (green), inline
            out.append(_del(o[i1:i2]) + " " + _ins(n[j1:j2]))
    return " ".join(out)


def _hblock(text) -> str:
    """A full claim body in a light card, line breaks preserved, HTML-escaped — the WAS/NEW context."""
    inner = _esc(text).replace("\n", "<br>")
    return (f'<div style="background:#fbfaf6;border:1px solid #eceadf;border-radius:6px;'
            f'padding:10px 12px;margin:4px 0;white-space:normal">{inner}</div>')


def _hcard(inner: str, accent: str = "#dfdbcf") -> str:
    """One proposal/alert card: a left accent stripe + border so sections are visually separate."""
    return (f'<div style="border:1px solid #e4e1d5;border-left:4px solid {accent};border-radius:8px;'
            f'padding:12px 14px;margin:0 0 14px">{inner}</div>')


def _hhead(op: str, section: str, zone: str, kind: str, subject_key: str, accent="#34566b") -> str:
    z = f" / {_esc(zone)}" if zone else ""
    k = f' <span style="{_C_MUTED};font-weight:400">[{_esc(kind)}]</span>' if kind else ""
    return (f'<div style="font-weight:700;color:{accent};font-size:15px">'
            f'{_esc(op)} in {_esc(section)}{z}{k}</div>'
            f'<div style="{_C_MUTED};font-size:13px;margin:1px 0 8px">{_esc(subject_key)}</div>')


def _hdoc(lead_html: str, body_html: str, footer_html: str) -> str:
    """Wrap the email content in a simple, client-safe container."""
    return (f'<div style="{_FONT};max-width:680px;margin:0 auto;padding:8px">'
            f'{lead_html}{body_html}{footer_html}</div>')


def _dispatch(subject: str, body: str, dry_run: bool = True, html: str = None) -> dict:
    """Send one owner alert to ALERT_EMAIL_TO, preferring the owner's own Gmail (SMTP + app
    password — no third-party service) and falling back to Resend. `body` is the plain-text version
    (always sent, the accessible fallback); `html`, when given, is sent as the rich alternative that
    clients render preferentially. SAFE BY DEFAULT: a no-op (returns a preview) when dry_run is set
    or nothing is configured; never raises on a config/send path."""
    to = config.ALERT_EMAIL_TO
    if dry_run or not to:
        return {"sent": False, "reason": "dry_run or no recipient (no email sent)",
                "subject": subject, "to": to, "preview": body, "html": html}

    guser, gpass = config.GMAIL_USER, (config.GMAIL_APP_PASSWORD or "").replace(" ", "")
    if guser and gpass:
        try:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = guser, to, subject
            msg.set_content(body)
            if html:
                msg.add_alternative(html, subtype="html")   # multipart/alternative: text + rich HTML
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
            payload = {"from": config.ALERT_EMAIL_FROM, "to": [to], "subject": subject, "text": body}
            if html:
                payload["html"] = html
            resp = httpx.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload, timeout=20,
            )
            return {"sent": resp.status_code < 300, "via": "resend",
                    "status": resp.status_code, "subject": subject}
        except Exception as e:
            return {"sent": False, "via": "resend", "reason": f"send error: {type(e).__name__}",
                    "subject": subject}

    return {"sent": False, "reason": "no email backend configured (Gmail or Resend)",
            "subject": subject, "preview": body, "html": html}


def _card_label(meta: dict) -> str:
    """Name the brief the way the rep thinks of it: 'Mistral vs OpenAI', not just 'OpenAI'. The same
    competitor (e.g. OpenAI) appears on several cards, so the label must say WHICH card."""
    me, comp = meta.get("my_company"), meta.get("competitor")
    return f"{me} vs {comp}" if me else (comp or "this card")


def render_digest(meta: dict, alerts: list[dict], deferred_note: str | None = None,
                  cost_note: str | None = None) -> tuple[str, str]:
    """Subject + plain-text body. One material change per block, each with its so-what. Labeled by
    CARD ('Mistral vs OpenAI'), since a competitor's news can land on more than one brief.
    `deferred_note` is the consequentiality gate's audit line (a routine run deferred N routed
    updates) — a deferral is never silent. `cost_note` is the run's $/claim line (monitor builds
    it), so spend-per-output is visible where the output lands, not just in the ledger."""
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
    if cost_note:
        lines += [cost_note, ""]
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


def render_digest_html(meta: dict, alerts: list[dict], deferred_note: str | None = None,
                       cost_note: str | None = None) -> str:
    """Rich HTML twin of render_digest: one card per material change, its old→new value shown as a
    struck-red → bold-green delta so the move is instantly scannable, headline bold, so-what muted."""
    comp = meta.get("competitor") or "the competitor"
    card = _card_label(meta)
    n = len(alerts)
    lead = (f'<p><strong>{n} material change{"s" if n != 1 else ""}</strong> on {_esc(card)} '
            f'(competitor: {_esc(comp)}).</p>')
    blocks = []
    for a in alerts:
        sev = a.get("severity")
        badge = (f'<span style="font-size:11px;font-weight:700;color:#34566b;background:#eef1f4;'
                 f'border-radius:3px;padding:1px 5px;margin-right:6px">{_esc(sev.upper())}</span>'
                 if sev else "")
        parts = [f'<div style="font-weight:700">{badge}'
                 f'{_esc(a.get("headline", a.get("subject_key", "change")))}</div>']
        old, new = a.get("old_value"), a.get("new_value")
        if old or new:
            delta = ((f'<span style="{_C_DEL}">{_esc(old)}</span> → ' if old else "")
                     + (f'<span style="{_C_ADD}">{_esc(new)}</span>' if new else ""))
            parts.append(f'<div style="margin-top:4px">{delta}</div>')
        if a.get("so_what"):
            parts.append(f'<div style="{_C_MUTED};font-size:13px;margin-top:4px">So what: '
                         f'{_esc(a["so_what"])}</div>')
        if a.get("source_url"):
            parts.append(f'<div style="font-size:13px;margin-top:4px"><a style="{_C_LINK}" '
                         f'href="{_esc(a["source_url"])}">source</a></div>')
        blocks.append(_hcard("".join(parts)))
    foot = []
    if deferred_note:
        foot.append(f'<div style="{_C_MUTED};font-size:13px">{_esc(deferred_note)}</div>')
    if cost_note:
        foot.append(f'<div style="{_C_MUTED};font-size:13px">{_esc(cost_note)}</div>')
    foot.append(f'<div style="{_C_MUTED};font-size:12px;margin-top:10px">'
                '— Scout (every claim verified against its source)</div>')
    return _hdoc(lead, "".join(blocks), "".join(foot))


def send_digest(meta: dict, alerts: list[dict], dry_run: bool = True,
                deferred_note: str | None = None, cost_note: str | None = None) -> dict:
    """Send ONE digest of the run's material deltas (rich HTML + plain-text fallback). No-op (dry)
    unless fully configured. Returns a result dict; never raises on a missing-config path."""
    if not alerts:
        return {"sent": False, "reason": "no material changes"}
    subject, body = render_digest(meta, alerts, deferred_note=deferred_note, cost_note=cost_note)
    html = render_digest_html(meta, alerts, deferred_note=deferred_note, cost_note=cost_note)
    return _dispatch(subject, body, dry_run=dry_run, html=html)


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
                                 unjudged: list[dict] = None,
                                 held: list[dict] = None,
                                 cost_note: str | None = None) -> tuple[str, str]:
    """Subject + body for the REVIEW-mode approval email: each judge-confirmed proposal with where
    (card + section), what (op), how it looks (the FULL old→new prose, never truncated), and the
    judge's reasoning, spaced for reading. The card is untouched; these await the human's approval.

    `exhausted` are rewrite-loop failures on ACT-GRADE facts (judge rejected, rewrite rejected
    again): the card was NOT changed and nothing else will surface them — this email is the loud
    signal (2026-07-01: two Sonnet-5 pricing ops died silently in the decision log).

    `unjudged` are drafts the judge NEVER ruled on (judge_unavailable — both the primary and the
    fallback model failed to return verdicts, the 2026-07-01 Opus outage): drafted, unverified,
    unapplied. The human is the judge of last resort — approve with allow_unjudged only after
    reading the prose.

    `held` are judge-confirmed updates the PRE-EMAIL render gate could not auto-repair (e.g. over
    the 170-word render cap): durably stored in pending_publish, owed to the card, never dropped —
    this email is their loud flag (2026-07-18: a held op previously rode the email looking fine and
    was held silently at approve time)."""
    exhausted, unjudged, held = exhausted or [], unjudged or [], held or []
    me, comp = meta.get("my_company"), meta.get("competitor")
    card = f"{me} vs {comp}" if me else (comp or slug)
    n = len(decisions)
    rule = "─" * 48
    subject = (f"Scout: {n} proposed card update{'s' if n != 1 else ''} awaiting approval"
               + (f" (+{len(exhausted)} authoring-failed)" if exhausted else "")
               + (f" (+{len(unjudged)} unverified)" if unjudged else "")
               + (f" (+{len(held)} needs curing)" if held else "") + f" — {card}")
    if decisions:
        out = [f"Propagation proposed {n} rep-facing change{'s' if n != 1 else ''} for {card}.",
               "The card is UNCHANGED — these need your approval before they go live.", "", rule]
    else:
        bits = ([f"{len(exhausted)} op(s) on a material fact FAILED authoring"] if exhausted else []) \
             + ([f"{len(unjudged)} drafted update(s) could NOT be verified (judge unavailable)"]
                if unjudged else []) \
             + ([f"{len(held)} judge-confirmed update(s) are HELD needing curing"] if held else [])
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
        # 2026-07-25 length-cure loop: an auto-condensed body is flagged so the owner knows the NEW
        # text above is a machine condense that a blind judge re-verified — read it with that lens.
        if d.get("length_cured") or d.get("condensed_at_gate"):
            out += ["", "Note: auto-condensed to fit the 170-word render cap after judge "
                        "confirmation; re-verified by a blind judge — the NEW text above is the "
                        "condensed version."]
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
    for d in held:
        op = str(d.get("operation", "")).upper()
        zone = f" / {d.get('zone')}" if d.get("zone") else ""
        kind = f"  [{d.get('change_kind')}]" if d.get("change_kind") else ""
        out += ["", "⚠ NEEDS CURING — judge-confirmed but HELD, never dropped",
                f"{op} in {d.get('section', '')}{zone}{kind}", f"({d.get('subject_key')})"]
        out += ["", f"Why: {_flat(d.get('hold_reason') or '(no reason recorded)')}"]
        if d.get("trigger_source_url"):
            out += [f"Fact: {d['trigger_source_url']}"]
        if d.get("old_text"):
            out += ["", "WAS:", _block(d["old_text"])]
        out += ["", "PROPOSED (full text — cure it before it can publish):",
                _block(d.get("new_text"))]
        out += ["", "It is stored in pending_publish and stays owed to the card. Approving the "
                    "clean items above does not touch it.", "", rule]
    if cost_note:
        out += ["", cost_note]
    # Exact next actions, one line each — the owner should be able to act from this email alone.
    actions = []
    if decisions:
        actions.append(f'To publish, tell Claude: "publish {card}" — or run: '
                       f"~/scout-tools/scout-proposals --approve {slug}")
    for d in held:
        actions.append(f'To fix the held item, tell Claude: '
                       f'"cure the {d.get("section", "held")} update on {card}"')
    if actions:
        out += [""] + actions + ["— Scout (proposed by the authorship judge; awaiting your approval)"]
    else:
        out += ["", "— Scout (nothing to approve; the failures above need a human decision)"]
    return subject, "\n".join(out)


def render_propagation_proposals_html(slug: str, meta: dict, decisions: list[dict],
                                      exhausted: list[dict] = None, unjudged: list[dict] = None,
                                      held: list[dict] = None, cost_note: str | None = None) -> str:
    """The RICH HTML twin of render_propagation_proposals, built for scanning: each proposal is a
    card, the WHAT-CHANGED diff is the hero (additions bold-green highlighted, removals struck red),
    and failure/held sections are colour-accented callouts. Plain text stays the fallback."""
    decisions, exhausted = decisions or [], exhausted or []
    unjudged, held = unjudged or [], held or []
    card = _card_label(meta)
    n = len(decisions)
    if decisions:
        lead = (f'<p><strong>{n} proposed rep-facing change{"s" if n != 1 else ""}</strong> for '
                f'{_esc(card)}. The card is <strong>unchanged</strong> — approve before they go live.</p>')
    else:
        lead = (f'<p>No changes confirmed for {_esc(card)}, but items below need your eyes.</p>')
    blocks = []
    for d in decisions:
        op = d.get("operation")
        parts = [_hhead(str(op or "").upper(), d.get("section", ""), d.get("zone"),
                        d.get("change_kind"), d.get("subject_key"))]
        if op == "revise" and d.get("old_text"):
            # tracked-changes: the edited paragraph with ONLY the changed words marked
            parts.append('<div style="font-weight:600;margin:8px 0 3px">Edited paragraph '
                         '(green = added, (struck) = removed)</div>'
                         + f'<div style="background:#fbfaf6;border:1px solid #eceadf;border-radius:6px;'
                         f'padding:12px 14px">{_diff_html(d.get("old_text"), d.get("new_text"))}</div>')
        elif op == "retire":
            parts.append('<div style="font-weight:600;margin:8px 0 3px;color:#b0301c">Removed from '
                         'the card</div>')
        else:  # add (or a revise with no prior text) — all-new content, shown plainly, not diffed
            parts.append('<div style="font-weight:600;margin:8px 0 3px">New (all content is added)</div>'
                         + _hblock(d.get("new_text")))
        if d.get("length_cured") or d.get("condensed_at_gate"):
            parts.append('<div style="color:#8a6322;font-size:14px;margin-top:8px">Auto-condensed to '
                         'the 170-word cap after confirmation, re-verified by a blind judge.</div>')
        # Feed note and Judge: each its OWN readable block, same body font, labeled — not tiny grey.
        if d.get("feed_note"):
            parts.append('<div style="margin-top:12px"><div style="font-weight:600">Feed note</div>'
                         f'<div style="margin-top:2px">{_esc(_flat(d["feed_note"]))}</div></div>')
        if d.get("judge_reason"):
            parts.append('<div style="margin-top:12px"><div style="font-weight:600">Judge</div>'
                         f'<div style="margin-top:2px">{_esc(_flat(d["judge_reason"]))}</div></div>')
        if d.get("trigger_source_url"):
            parts.append(f'<div style="margin-top:12px"><a style="{_C_LINK}" '
                         f'href="{_esc(d["trigger_source_url"])}">source</a></div>')
        blocks.append(_hcard("".join(parts)))
    for d in exhausted:
        att = d.get("attempts") or []
        rows = "".join(f'<li>{_esc(_flat(a.get("reason") or "(no reason)"))}</li>' for a in att)
        last = next((a.get("claim") for a in reversed(att) if a.get("claim")), None)
        inner = (f'<div style="font-weight:700;color:#b0301c">✗ AUTHORING FAILED — needs your eyes</div>'
                 + _hhead(str(d.get("operation", "")).upper(), d.get("section", ""), d.get("zone"),
                          d.get("change_kind"), d.get("subject_key"), accent="#b0301c")
                 + f'<div style="font-size:13px">The judge rejected {len(att)} attempt(s):</div>'
                 + f'<ul style="font-size:13px;{_C_MUTED};margin:4px 0">{rows}</ul>'
                 + (f'<div style="font-weight:600;margin:6px 0 2px">Last attempt</div>' + _hblock(last)
                    if last else "")
                 + '<div style="font-size:13px;margin-top:6px">Card NOT changed. Edit manually or re-run.</div>')
        blocks.append(_hcard(inner, accent="#b0301c"))
    for d in unjudged:
        inner = (f'<div style="font-weight:700;color:#8a6322">⚠ DRAFTED BUT UNVERIFIED — judge unavailable</div>'
                 + _hhead(str(d.get("operation", "")).upper(), d.get("section", ""), d.get("zone"),
                          d.get("change_kind"), d.get("subject_key"), accent="#8a6322")
                 + '<div style="font-weight:600;margin:6px 0 2px">Drafted (no judge ruled)</div>'
                 + _hblock(d.get("new_text"))
                 + '<div style="font-size:13px;margin-top:6px">Card NOT changed. Approve with '
                   'allow_unjudged only after reading it yourself.</div>')
        blocks.append(_hcard(inner, accent="#8a6322"))
    for d in held:
        inner = (f'<div style="font-weight:700;color:#8a6322">⚠ NEEDS CURING — confirmed but held</div>'
                 + _hhead(str(d.get("operation", "")).upper(), d.get("section", ""), d.get("zone"),
                          d.get("change_kind"), d.get("subject_key"), accent="#8a6322")
                 + f'<div style="font-size:13px">Why: {_esc(_flat(d.get("hold_reason") or "(no reason)"))}</div>'
                 + '<div style="font-weight:600;margin:6px 0 2px">Proposed (cure before publish)</div>'
                 + _hblock(d.get("new_text")))
        blocks.append(_hcard(inner, accent="#8a6322"))
    foot = []
    if cost_note:
        foot.append(f'<div style="{_C_MUTED};font-size:13px">{_esc(cost_note)}</div>')
    if decisions:
        foot.append(f'<div style="margin-top:8px">To publish, tell Claude '
                    f'<em>"publish {_esc(card)}"</em> or run '
                    f'<code>~/scout-tools/scout-proposals --approve {_esc(slug)}</code></div>')
    foot.append(f'<div style="{_C_MUTED};font-size:12px;margin-top:10px">— Scout</div>')
    return _hdoc(lead, "".join(blocks), "".join(foot))


def send_propagation_proposals(slug: str, meta: dict, decisions: list[dict], dry_run: bool = True,
                               exhausted: list[dict] = None, unjudged: list[dict] = None,
                               held: list[dict] = None, cost_note: str | None = None) -> dict:
    """Email the run's judge-confirmed propagation proposals (REVIEW mode), plus any rewrite-loop
    EXHAUSTED failures, any UNJUDGED drafts (judge unavailable), and any render-gate HELD updates —
    all must reach the owner even when nothing was confirmed: a dropped material edit is never
    silent. Sends a rich HTML body (scannable diff) with the plain-text version as fallback. No-op
    (dry) unless fully configured; never raises on a missing-config path."""
    if not decisions and not exhausted and not unjudged and not held:
        return {"sent": False, "reason": "no proposals"}
    subject, body = render_propagation_proposals(slug, meta, decisions, exhausted=exhausted,
                                                 unjudged=unjudged, held=held, cost_note=cost_note)
    html = render_propagation_proposals_html(slug, meta, decisions, exhausted=exhausted,
                                             unjudged=unjudged, held=held, cost_note=cost_note)
    return _dispatch(subject, body, dry_run=dry_run, html=html)


def render_urgent_material(slug: str, meta: dict, urgent: list[dict]) -> tuple[str, str]:
    """Subject + body for the URGENT separate alert (2026-07-31): DEAL-MOVING points that could NOT
    be authored onto the card — either the judge ruled the point material but with no grounded
    correct expression (`cure:"none"`), or the cure loop exhausted its tries. This is a superset of
    the proposals email's AUTHORING-FAILED section and carries the `cure:"none"` never-drafted case
    that section never had. The whole point of the alert is the judge's FINAL reason — it names the
    material point and (for a root reject) the correct approach the author could not reach — so the
    owner can author it by hand."""
    card = _card_label(meta)
    n = len(urgent)
    rule = "─" * 48
    subject = f"Scout: URGENT — {n} material point{'s' if n != 1 else ''} undrafted — {card}"
    out = [f"{n} deal-moving point{'s' if n != 1 else ''} could NOT be authored onto the {card} "
           "brief. The card was NOT changed. Each is material (would move a deal) but the honest "
           "version was not reachable automatically — author it by hand or re-run.", "", rule]
    for d in urgent:
        op = str(d.get("operation", "")).upper()
        zone = f" / {d.get('zone')}" if d.get("zone") else ""
        kind = f"  [{d.get('change_kind')}]" if d.get("change_kind") else ""
        why = "no grounded correct expression (cure:none)" if d.get("cure") == "none" \
            else f"cure exhausted after {d.get('rewrite_attempts', 0)} rewrite(s)"
        out += ["", "⚠ URGENT — material point undrafted",
                f"{op} in {d.get('section', '')}{zone}{kind}", f"({d.get('subject_key')})",
                f"Why undrafted: {why}"]
        if d.get("trigger_source_url"):
            out += [f"Fact: {d['trigger_source_url']}"]
        if d.get("judge_reason"):                          # the honest framing — the point of the alert
            out += ["", "JUDGE'S DIAGNOSIS (the material point + the correct approach):",
                    _block(d["judge_reason"])]
        att = d.get("attempts") or []
        last_prose = next((a.get("claim") for a in reversed(att) if a.get("claim")), None)
        if last_prose:
            out += ["", "LAST ATTEMPT PROSE (rejected):", _block(last_prose)]
        out += ["", "Author this point on the card by hand (use the judge's diagnosis), or re-run.",
                "", rule]
    out += ["", "— Scout (a deal-moving point went undrafted; this needs your hand)"]
    return subject, "\n".join(out)


def render_urgent_material_html(slug: str, meta: dict, urgent: list[dict]) -> str:
    """Rich HTML twin of render_urgent_material — a red-accented alert per undrafted material point,
    the judge's honest diagnosis as the hero (that is what the owner authors from)."""
    card = _card_label(meta)
    n = len(urgent)
    lead = (f'<p style="color:#b0301c"><strong>{n} deal-moving point{"s" if n != 1 else ""}</strong> '
            f'could not be authored onto {_esc(card)}. The card is <strong>unchanged</strong>. '
            f'Each is material but the honest version was not reachable automatically.</p>')
    blocks = []
    for d in urgent:
        why = ("no grounded correct expression (cure:none)" if d.get("cure") == "none"
               else f"cure exhausted after {d.get('rewrite_attempts', 0)} rewrite(s)")
        att = d.get("attempts") or []
        last = next((a.get("claim") for a in reversed(att) if a.get("claim")), None)
        inner = (_hhead(str(d.get("operation", "")).upper(), d.get("section", ""), d.get("zone"),
                        d.get("change_kind"), d.get("subject_key"), accent="#b0301c")
                 + f'<div style="{_C_MUTED};font-size:13px">Why undrafted: {_esc(why)}</div>'
                 + (f'<div style="font-weight:600;margin:8px 0 2px">Judge\'s diagnosis '
                    f'(the material point + the correct approach)</div>' + _hblock(d["judge_reason"])
                    if d.get("judge_reason") else "")
                 + (f'<div style="font-weight:600;margin:8px 0 2px">Last attempt (rejected)</div>'
                    + _hblock(last) if last else "")
                 + (f'<div style="font-size:13px;margin-top:6px"><a style="{_C_LINK}" '
                    f'href="{_esc(d["trigger_source_url"])}">source</a></div>'
                    if d.get("trigger_source_url") else "")
                 + '<div style="font-size:13px;margin-top:6px">Author this on the card by hand '
                   '(use the diagnosis), or re-run.</div>')
        blocks.append(_hcard(inner, accent="#b0301c"))
    foot = f'<div style="{_C_MUTED};font-size:12px;margin-top:10px">— Scout (a deal-moving point went undrafted; this needs your hand)</div>'
    return _hdoc(lead, "".join(blocks), foot)


def send_urgent_material(slug: str, meta: dict, urgent: list[dict], dry_run: bool = True) -> dict:
    """Send the URGENT undrafted-material alert as a SEPARATE email (rich HTML + plain-text
    fallback), distinct from the proposals email. No-op (dry) unless configured; never raises on a
    missing-config path (inherits _dispatch's contract)."""
    if not urgent:
        return {"sent": False, "reason": "no urgent material"}
    subject, body = render_urgent_material(slug, meta, urgent)
    html = render_urgent_material_html(slug, meta, urgent)
    return _dispatch(subject, body, dry_run=dry_run, html=html)


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
