"""Self-serve generation: async, gated, git-as-store (v2 launch, Parts 2 + 3).

A visitor requests a report (two companies + optional focus). The DEPLOYED app does NOT
generate inline — it commits a request to `selfserve/requests/<job_id>.json` (via the GitHub
Contents API), which triggers `.github/workflows/selfserve.yml`. That Action runs the SAME
SDK generation pipeline headless and writes the card to `user_reports/<job_id>/` — NOT to
`battlecards/`. So user cards are PRIVATE to the repo owner and never enter the public
showcase (`display.list_battlecards`) or the monitor (`run_all`): the separation is physical
(different directory), not a flag that can leak. The app polls `user_reports/<job_id>/` and
renders the card when it appears. No email/notify — the job id lives in the URL so a visitor
can leave and come back.

Two INDEPENDENT gates (config): a launch WINDOW (first N free, then the entry point locks to
"DM me for access") and a hard SPEND CEILING in dollars, in case per-report cost spikes. Both
live in `selfserve/state.json`. The Action — serialized via the workflow's `concurrency` group
— is the AUTHORITATIVE writer of state (it re-checks the gate before spending and records the
real cost). The app reads state only to display "X free left" and lock the button; a stale read
can never overspend because the Action gates again at the point of spend.

BACKEND: when SELFSERVE_GH_TOKEN + SELFSERVE_REPO are set (deployed app), reads/writes go through
the GitHub API on SELFSERVE_BRANCH. Otherwise everything falls back to the local filesystem
(dev/test), so the whole flow is runnable without a token.
"""
import base64
import json
import os
import re
from datetime import datetime

import httpx

from scout import config, store

STATE_PATH = "selfserve/state.json"
REQUESTS_DIR = "selfserve/requests"
RESULTS_DIR = "user_reports"
_GH_API = "https://api.github.com"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- backend switch ----------------------------------------------------------
def use_github() -> bool:
    """True when the GitHub-API backend is configured (the deployed app)."""
    return bool(config.SELFSERVE_GH_TOKEN and config.SELFSERVE_REPO)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.SELFSERVE_GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_path(path: str) -> str:
    """Repo-root-relative path for the GitHub API. The local store paths are anchored to
    _REPO_ROOT (the v2/ dir), but the GitHub API addresses files from the GIT REPO root,
    where v2/ is a subdirectory — so prepend it for API calls only."""
    return f"{config.REPO_SUBDIR}/{path}" if config.REPO_SUBDIR else path


def _gh_get(path: str) -> tuple[str | None, str | None]:
    """Return (text_content, sha) for a repo file, or (None, None) if it 404s."""
    url = f"{_GH_API}/repos/{config.SELFSERVE_REPO}/contents/{_repo_path(path)}"
    r = httpx.get(url, headers=_headers(), params={"ref": config.SELFSERVE_BRANCH}, timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def _gh_put(path: str, text: str, message: str, sha: str | None = None) -> None:
    """Create or update a repo file. Passing the current sha updates in place; omitting
    it creates. A 409/422 here means someone else moved the file first (optimistic lock)."""
    url = f"{_GH_API}/repos/{config.SELFSERVE_REPO}/contents/{_repo_path(path)}"
    body = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": config.SELFSERVE_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = httpx.put(url, headers=_headers(), json=body, timeout=20)
    r.raise_for_status()


def _local(path: str) -> str:
    return os.path.join(_REPO_ROOT, path)


def _read(path: str) -> str | None:
    if use_github():
        return _gh_get(path)[0]
    p = _local(path)
    return open(p).read() if os.path.exists(p) else None


def _write(path: str, text: str, message: str) -> None:
    if use_github():
        # Re-fetch the sha so an update targets the live file (state.json especially).
        _, sha = _gh_get(path)
        _gh_put(path, text, message, sha)
        return
    p = _local(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


# --- state / gates -----------------------------------------------------------
def default_state() -> dict:
    return {
        "used": 0,
        "free_limit": config.SELFSERVE_FREE_LIMIT,
        "spend_usd": 0.0,
        "spend_ceiling_usd": config.SELFSERVE_SPEND_CEILING_USD,
    }


def load_state() -> dict:
    raw = _read(STATE_PATH)
    if not raw:
        return default_state()
    try:
        s = json.loads(raw)
    except json.JSONDecodeError:
        return default_state()
    # Tolerate a hand-edited/partial file; config is the source of truth for the limits.
    s.setdefault("used", 0)
    s.setdefault("spend_usd", 0.0)
    s["free_limit"] = s.get("free_limit", config.SELFSERVE_FREE_LIMIT)
    s["spend_ceiling_usd"] = s.get("spend_ceiling_usd", config.SELFSERVE_SPEND_CEILING_USD)
    return s


def gate(state: dict | None = None) -> dict:
    """Decide whether a new generation is allowed. Returns {open, free_left, reason}.
    The TWO gates are independent: the window (count) and the ceiling (dollars)."""
    s = state or load_state()
    free_left = max(0, int(s["free_limit"]) - int(s["used"]))
    # Reserve one run's worth of headroom: since every generation is SDK-capped at
    # GEN_MAX_BUDGET_USD, refusing to START a run that *could* cross the ceiling guarantees
    # total spend never exceeds it — a genuine hard cap, not a best-effort one.
    if s["spend_usd"] + config.GEN_MAX_BUDGET_USD > s["spend_ceiling_usd"]:
        return {"open": False, "free_left": free_left,
                "reason": "spend_ceiling", "contact": config.SELFSERVE_CONTACT}
    if free_left <= 0:
        return {"open": False, "free_left": 0,
                "reason": "window_closed", "contact": config.SELFSERVE_CONTACT}
    return {"open": True, "free_left": free_left, "reason": None}


# --- jobs --------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_job_id(competitor: str, my_company: str | None, focus: str | None) -> str:
    """A request id that is human-legible and collision-resistant: the perspective slug
    plus a timestamp. (Same slug helper the store uses, so the id reads like the card.)"""
    base = store.make_slug(competitor, my_company, focus)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{base}__{stamp}"


def submit(competitor: str, my_company: str | None, focus: str | None) -> dict:
    """Capture a request (does NOT generate). Writes selfserve/requests/<id>.json, which
    triggers the Action. Returns the request record (incl. job_id) for the app to track."""
    job_id = new_job_id(competitor, my_company, focus)
    req = {
        "job_id": job_id,
        "competitor": (competitor or "").strip(),
        "my_company": (my_company or "").strip() or None,
        "focus": (focus or "").strip() or None,
        "requested_at": _now(),
        "status": "queued",
    }
    _write(f"{REQUESTS_DIR}/{job_id}.json", json.dumps(req, indent=2, ensure_ascii=False),
           f"selfserve: queue request {job_id}")
    return req


def get_result(job_id: str) -> dict | None:
    """Return the finished result record for a job, or None if still pending.
    status is one of: done | rejected | error. card.md is fetched alongside on done."""
    raw = _read(f"{RESULTS_DIR}/{job_id}/result.json")
    if not raw:
        return None
    try:
        res = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if res.get("status") == "done" and "markdown" not in res:
        res["markdown"] = _read(f"{RESULTS_DIR}/{job_id}/card.md") or ""
    return res


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{3,120}$")


def valid_job_id(job_id: str) -> bool:
    """Guard a job id coming from a URL query param before using it in a path."""
    return bool(job_id and _SAFE_ID.match(job_id) and ".." not in job_id)
