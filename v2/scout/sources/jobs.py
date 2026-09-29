"""Public job boards (WS1, 2026-09-28): Greenhouse, Ashby and Lever expose a keyless JSON board per
company. What a company is hiring for is a leading indicator of what it is building, and the board
is the company's own statement (source_class job_posting, tier primary).

`postings(host, token)` normalizes the three shapes to one row; `diff(prev, cur)` is the delta the
signals poller (WS3) keys on; `summarize(rows)` is the compact text the model sees.
"""
from __future__ import annotations

from collections import Counter

BOARD_URL = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
}
HOSTS = tuple(BOARD_URL)


def board_url(host: str, token: str) -> str:
    if host not in BOARD_URL:
        raise ValueError(f"unknown job-board host {host!r}; one of {HOSTS}")
    return BOARD_URL[host].format(token=token)


def _get_json(url: str):
    from scout.grounding import _fetch_response
    resp = _fetch_response(url)
    if resp is None or resp.status_code >= 400:
        raise RuntimeError(f"HTTP {getattr(resp, 'status_code', '?')} for {url}")
    return resp.json()


def normalize(host: str, data) -> list[dict]:
    """One row per posting: {id, title, location, department, updated_at, url}."""
    rows = []
    if host == "greenhouse":
        for j in (data or {}).get("jobs", []):
            depts = j.get("departments") or []
            rows.append({"id": str(j.get("id")), "title": j.get("title") or "",
                         "location": ((j.get("location") or {}).get("name") or ""),
                         "department": (depts[0].get("name") if depts else "") or "",
                         "updated_at": (j.get("updated_at") or "")[:10], "url": j.get("absolute_url") or ""})
    elif host == "ashby":
        for j in (data or {}).get("jobs", []):
            rows.append({"id": str(j.get("id")), "title": j.get("title") or "", "location": j.get("location") or "",
                         "department": j.get("department") or j.get("team") or "",
                         "updated_at": (j.get("publishedAt") or "")[:10], "url": j.get("jobUrl") or ""})
    elif host == "lever":
        for j in (data or []):
            cats = j.get("categories") or {}
            ts = j.get("createdAt")
            from datetime import datetime, timezone
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat() if isinstance(ts, (int, float)) else ""
            rows.append({"id": str(j.get("id")), "title": j.get("text") or "", "location": cats.get("location") or "",
                         "department": cats.get("department") or cats.get("team") or "",
                         "updated_at": day, "url": j.get("hostedUrl") or ""})
    return rows


def postings(host: str, token: str) -> list[dict]:
    return normalize(host, _get_json(board_url(host, token)))


def diff(prev: list[dict], cur: list[dict]) -> dict:
    """{added:[rows], removed:[rows], net:int, by_department:{dept: +n/-n}} between two snapshots."""
    p = {r["id"]: r for r in prev}
    c = {r["id"]: r for r in cur}
    added = [c[i] for i in c if i not in p]
    removed = [p[i] for i in p if i not in c]
    by_dept: Counter = Counter()
    for r in added:
        by_dept[r.get("department") or "(none)"] += 1
    for r in removed:
        by_dept[r.get("department") or "(none)"] -= 1
    return {"added": added, "removed": removed, "net": len(added) - len(removed),
            "by_department": {k: v for k, v in by_dept.items() if v}}


def summarize(rows: list[dict], top: int = 12, newest: int = 10) -> str:
    """Counts by department + the newest postings, as text for the model."""
    if not rows:
        return "No open postings on the board."
    depts = Counter((r.get("department") or "(none)") for r in rows)
    lines = [f"{len(rows)} open postings. By department: "
             + ", ".join(f"{d} {n}" for d, n in depts.most_common(top))]
    lines.append("Newest postings (date · title · location · url):")
    for r in sorted(rows, key=lambda r: r.get("updated_at") or "", reverse=True)[:newest]:
        lines.append(f"{r.get('updated_at') or '?'} · {r['title']} · {r.get('location') or ''} · {r.get('url') or ''}")
    return "\n".join(lines)
