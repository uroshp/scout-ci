"""The git-committed store: battlecards/<slug>/ layout (v2-agent-spec.md §7).

Markdown is presentation; JSON is state. The monitor reads/writes claims.json and
meta.json here; current.md is rendered from them. Only the headless runtime writes.
"""
import json
import os
import re
from datetime import date, datetime

from scout import config

STORE_ROOT = "battlecards"


def _slug_part(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)   # spaces/punct/slashes -> single dash
    return s.strip("-")


def make_slug(competitor: str, my_company: str | None = None, focus: str | None = None) -> str:
    """Perspective-encoded identity (claim-object.md §3). A battlecard is not
    "Google Cloud" — it's "AWS vs Google Cloud in cloud infrastructure".

    <my-company>__vs__<competitor>__<focus>, or scout__vs__<competitor>__<focus>.
    Deterministic (no random hash) so the monitor can locate the folder by inputs.
    """
    me = _slug_part(my_company) if my_company else "scout"
    comp = _slug_part(competitor)
    foc = _slug_part(focus) if focus else "general"
    return f"{me}__vs__{comp}__{foc}"


def battlecard_dir(slug: str) -> str:
    return os.path.join(STORE_ROOT, slug)


def _paths(slug: str) -> dict:
    d = battlecard_dir(slug)
    return {
        "dir": d,
        "claims": os.path.join(d, "claims.json"),
        "meta": os.path.join(d, "meta.json"),
        "current": os.path.join(d, "current.md"),
        "alerts_md": os.path.join(d, "alerts.md"),
        "alerts_jsonl": os.path.join(d, "alerts.jsonl"),
    }


def new_meta(competitor: str, my_company: str | None, focus: str | None, slug: str,
             cadence_hours: int | None = None) -> dict:
    today = date.today().isoformat()
    return {
        "slug": slug,
        "my_company": my_company,
        "competitor": competitor,
        "focus": focus,
        "baseline_date": today,
        # Full timestamp (not just a date) so the next-check countdown is precise
        # at hour-scale cadences. The monitor advances this on every check.
        "last_checked": datetime.now().isoformat(timespec="seconds"),
        "cadence_hours": cadence_hours if cadence_hours is not None else config.DEFAULT_CADENCE_HOURS,
        "alerted_fingerprints": [],
    }


def write_baseline(slug: str, claims: list[dict], meta: dict, current_md: str) -> dict:
    """Write a fresh tracked baseline. Returns the paths written."""
    p = _paths(slug)
    os.makedirs(p["dir"], exist_ok=True)
    with open(p["claims"], "w") as f:
        json.dump(claims, f, indent=2, ensure_ascii=False)
    with open(p["meta"], "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    with open(p["current"], "w") as f:
        f.write(current_md)
    return p


def load_claims(slug: str) -> list[dict]:
    p = _paths(slug)
    if not os.path.exists(p["claims"]):
        return []
    with open(p["claims"]) as f:
        return json.load(f)


def load_meta(slug: str) -> dict | None:
    p = _paths(slug)
    if not os.path.exists(p["meta"]):
        return None
    with open(p["meta"]) as f:
        return json.load(f)
