"""Scout v1 (retired): the fixed generate -> verify -> save pipeline.

The pipeline's control flow and its deterministic helpers stay here. Its instruction text (the
two prompts, the source hierarchy, the formatting rules and methodology.md) is part of Scout's
private judgment pack and is no longer published; the full v1 engine is archived there. v2 loads
its own blocks through v2/scout/judgment.py."""
import os
from datetime import datetime
from dotenv import load_dotenv
from anthropic import Anthropic
load_dotenv()
client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-sonnet-4-6"


def load_methodology():
    raise RuntimeError("Scout v1 is retired and its prompts are archived in the private judgment pack. "
                       "Use v2 (the living battlecards at agent-scout.ai).")


def _from_title(text):
    """Drop any model preamble that appears before the report title.
    Backstop for the prompt instruction: even if the model thinks out loud,
    the saved/returned brief always starts at the title."""
    idx = text.find("# Competitive Intelligence Brief")
    return text[idx:] if idx != -1 else text


def _extract(response):
    """Rebuild the model's prose from its content blocks and append the real
    source link(s) after each cited span. Citations come from the web_search
    tool's results, so the URLs are genuine retrieved sources - the model
    cannot fabricate them. Blocks are joined with "" (not "\\n") because cited
    spans are contiguous fragments of one passage."""
    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        citations = getattr(block, "citations", None) or []
        links, seen = [], set()
        for c in citations:
            if isinstance(c, dict):
                url, title = c.get("url"), c.get("title")
            else:
                url, title = getattr(c, "url", None), getattr(c, "title", None)
            if not url or url in seen:
                continue
            seen.add(url)
            label = (title or url).strip().replace("\n", " ")
            if len(label) > 60:
                label = label[:57].rstrip() + "..."
            links.append(f"[{label}]({url})")
        if links:
            text = text.rstrip() + " (" + ", ".join(links) + ")"
        parts.append(text)
    return "".join(parts)


def generate_brief(target, perspective=None, product=None):
    raise RuntimeError("Scout v1 is retired and its prompts are archived in the private judgment pack. "
                       "Use v2 (the living battlecards at agent-scout.ai).")


def verify_brief(draft):
    raise RuntimeError("Scout v1 is retired and its prompts are archived in the private judgment pack. "
                       "Use v2 (the living battlecards at agent-scout.ai).")


def save_report(text, target, perspective=None):
    reports_dir = os.path.join(_DIR, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    label = f"{perspective}_vs_{target}" if perspective else target
    label = label.replace(" ", "-").replace("/", "-")
    path = os.path.join(reports_dir, f"{label}_{stamp}.md")
    with open(path, "w") as f:
        f.write(text)
    return path


def research_competitor(target, perspective=None, product=None):
    print("Researching and drafting brief...")
    draft = generate_brief(target, perspective, product)
    print("Verifying claims against sources (this is the slow part)...")
    final = verify_brief(draft)
    path = save_report(final, target, perspective)
    print(f"Report saved to: {path}")
    return final


if __name__ == "__main__":
    result = research_competitor("Slack", perspective="Microsoft Teams")
    print("\n" + "=" * 60 + "\n")
    print(result)
