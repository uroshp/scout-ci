"""Wayback Machine (WS1, 2026-09-28): dated copies of a page, as a READ tool for the model ("what
did their pricing page say in June?"). Not a change trigger (plan C34: nonces and dates flip
digests on every crawl). Keyless; the CDX API rate-limits bursts (429 seen 2026-09-28), so calls
are spaced.
"""
from __future__ import annotations

import time

CDX = "http://web.archive.org/cdx/search/cdx"
MIN_SPACING_S = 2.0
_last_call = 0.0


def _spaced():
    global _last_call
    wait = MIN_SPACING_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def snapshots(url: str, since: str | None = None, limit: int = 20) -> list[dict]:
    """Distinct captures of `url` (collapsed by content digest), newest first:
    {timestamp, date, digest, length, snapshot_url}. `since` = YYYY or YYYYMM or YYYYMMDD."""
    from scout.grounding import _fetch_response
    _spaced()
    params = {"url": url, "output": "json", "filter": "statuscode:200", "collapse": "digest", "limit": str(limit)}
    if since:
        params["from"] = since
    q = "&".join(f"{k}={v}" for k, v in params.items())
    resp = _fetch_response(f"{CDX}?{q}")
    if resp is None or resp.status_code >= 400:
        raise RuntimeError(f"HTTP {getattr(resp, 'status_code', '?')} from the Wayback CDX")
    data = resp.json() if resp.text.strip() else []
    rows = []
    for r in data[1:]:
        ts, original, digest, length = r[1], r[2], r[5], r[6]
        rows.append({"timestamp": ts, "date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", "digest": digest, "length": int(length or 0),
                     "snapshot_url": f"https://web.archive.org/web/{ts}/{original}",
                     "raw_url": f"https://web.archive.org/web/{ts}id_/{original}"})
    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    return rows


def snapshot_text(url: str, timestamp: str, max_chars: int = 12000) -> tuple[str, str]:
    """(text, snapshot_url) for one capture, through grounding's extractor (all visible text)."""
    from scout.grounding import _extract_text, _fetch_response
    _spaced()
    raw = f"https://web.archive.org/web/{timestamp}id_/{url}"
    resp = _fetch_response(raw)
    if resp is None or resp.status_code >= 400:
        raise RuntimeError(f"HTTP {getattr(resp, 'status_code', '?')} for the snapshot")
    text, _kind = _extract_text(resp)
    import re
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars], f"https://web.archive.org/web/{timestamp}/{url}"


def render_snapshots_text(url: str, rows: list[dict]) -> str:
    if not rows:
        return f"No archived captures of {url} in that range."
    out = [f"{len(rows)} distinct captures of {url}, newest first (date · snapshot URL). Pass a timestamp to page_history to read one."]
    for r in rows:
        out.append(f"{r['date']} · ts={r['timestamp']} · {r['snapshot_url']}")
    return "\n".join(out)
