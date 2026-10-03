"""Answer dict -> Slack text and blocks. Pure functions: the unit-test surface of the bot."""
from __future__ import annotations

import re

SITE = "https://agent-scout.ai"
STAGES = {"facts": "Starting from what Scout already verified",
          "draft": "Writing an answer from what Scout knows",
          "search": "Finding and reading sources",
          "ground": "Confirming each quote is really on its page",
          "floor": "Matching every number to its source",
          "verify": "Fact-checking each sentence before you see it"}
WELCOME = ("Hi, I am Agent Scout. Ask me anything about the competitors Scout tracks and I answer "
           "only from claims that were verified against their sources. A quick answer takes about a "
           "minute; say *research deeper* and I read today's sources, which takes a few minutes.")
EXAMPLES = ["The buyer says OpenAI is cheaper than Anthropic. Is it?",
            "What has Google shipped against Perplexity in the last 30 days?",
            "What will a Microsoft Teams rep say against Slack, and is it true?"]


def _mrkdwn(text: str) -> str:
    """Markdown bold and italics to Slack mrkdwn; HTML-sensitive characters escaped."""
    t = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"\*\*(.+?)\*\*", "\x00\\1\x00", t)                 # bold, held aside
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"_\1_", t)   # italics
    return t.replace("\x00", "*")


def stage_text(stage: str, extra: str = "") -> str:
    base = STAGES.get(stage, stage.replace("_", " ").capitalize())
    return f":hourglass_flowing_sand: {base}" + (f" · _{extra}_" if extra else "")


def answer_text(answer: dict) -> str:
    paras = [p.get("text", "") for p in (answer.get("paragraphs") or []) if p.get("text")]
    return "\n\n".join(_mrkdwn(p) for p in paras) or "Scout found nothing it could verify for that."


def verified_line(answer: dict) -> str:
    n_src = len(answer.get("sources") or []); n_cut = len(answer.get("cut_log") or [])
    kind = "researched today and fact-checked" if answer.get("kind") == "deep" else "answered from what Scout already verified"
    bits = [f"Verified · {kind}", f"{n_src} source{'s' if n_src != 1 else ''}"]
    if n_cut:
        bits.append(f"{n_cut} claim{'s' if n_cut != 1 else ''} cut")
    return " · ".join(bits)


def answer_blocks(answer: dict) -> tuple[str, list]:
    """(fallback text, blocks) for the final message."""
    text = answer_text(answer)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text": verified_line(answer)}]}]
    aid = answer.get("id") or ""
    buttons = []
    if answer.get("kind") != "deep":
        buttons.append({"type": "button", "action_id": "deeper", "value": aid,
                        "text": {"type": "plain_text", "text": "Research deeper"}})
    if answer.get("sources"):
        buttons.append({"type": "button", "action_id": "sources", "value": aid, "text": {"type": "plain_text", "text": "Sources"}})
    if answer.get("cut_log"):
        buttons.append({"type": "button", "action_id": "cut", "value": aid, "text": {"type": "plain_text", "text": "What was cut"}})
    if answer.get("permalink"):
        buttons.append({"type": "button", "action_id": "open", "url": answer["permalink"],
                        "text": {"type": "plain_text", "text": "Open on agent-scout.ai"}})
    if buttons:
        blocks.append({"type": "actions", "elements": buttons[:5]})
    return text, blocks


def sources_text(answer: dict) -> str:
    rows = []
    for s in answer.get("sources") or []:
        tier = {"primary": "primary", "reputable_secondary": "secondary", "sentiment_only": "sentiment"}.get(s.get("tier") or "", s.get("tier") or "")
        asof = f" · as of {s['as_of']}" if s.get("as_of") else ""
        rows.append(f"[{s.get('n')}] <{s.get('url')}|{_host(s.get('url'))}> · {tier}{asof}")
    return "*Sources*\n" + "\n".join(rows) if rows else "No sources on this answer."


def cut_text(answer: dict) -> str:
    rows = [f"• *{_mrkdwn(str(c.get('label') or c.get('claim') or ''))}* {_mrkdwn(str(c.get('reason') or ''))}".rstrip()
            for c in answer.get("cut_log") or []]
    return "*What was cut* (claims the verifier could not confirm)\n" + "\n".join(rows) if rows else "Nothing was cut from this answer."


def _host(url: str | None) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) if m else url or "").removeprefix("www.")


def refusal_text(message: str) -> str:
    return f":no_entry_sign: {message}"


def limit_text(kind: str, n: int) -> str:
    what = "deep research question" if kind == "deep" else "quick questions"
    return (f"You have used today's {n} {what} here. The limit resets at midnight Pacific. "
            f"The briefs at {SITE} are open all day.")
