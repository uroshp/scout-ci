"""Static HTML render of a Scout living-battlecard — websocket-free fallback.

Mirrors app_v2.py (the Streamlit viewer) but emits a single self-contained HTML
file served over plain HTTP, so it renders through proxies that block Streamlit's
websocket. READ-ONLY: same display layer, no generation, no monitoring.

Surfaces the launch display elements: live countdown to next check (A2), times on
the change feed + alerts (A3), and a NEW badge on claims a monitor run touched in
the last 24h (A4 — keyed off an emitted alert, so a fresh baseline badges nothing).

    python scripts/render_static.py            # -> out/index.html
    python -m http.server 8502 -d out          # serve it
"""
import base64
import html
import io
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import display, store

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSETS = os.path.join(_REPO, "assets")


def _asset(name: str, fallback: str) -> str | None:
    """Prefer the transparent (_t) variant of a mascot asset, else the original."""
    for n in (name, fallback):
        p = os.path.join(_ASSETS, n)
        if os.path.exists(p):
            return p
    return None


def _img_data_uri(path, height=None, square=False) -> str | None:
    """Crop a transparent PNG to its content, optionally square-pad it, scale to
    `height`, and return a base64 data URI so the page stays self-contained."""
    if not path:
        return None
    try:
        from PIL import Image
        im = Image.open(path).convert("RGBA")
        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)
        if square:
            s = max(im.size)
            bg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            bg.paste(im, ((s - im.width) // 2, (s - im.height) // 2))
            im = bg
        if height:
            w = max(1, round(im.width * height / im.height))
            im = im.resize((w, height), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _pretty(slug: str) -> str:
    return slug.replace("__vs__", " vs ").replace("__", " · ").replace("-", " ")


def _read_current(slug: str) -> str:
    path = os.path.join(store.battlecard_dir(slug), "current.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return "_No rendered battlecard found for this slug._"


def _fmt_dt(iso: str | None) -> str:
    """'2026-06-04T23:46:11' (naive UTC, see scout.display) -> 'Jun 4, 7:46 PM ET'.
    Pass non-ISO through."""
    if not iso:
        return "—"
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(iso)
        if len(iso.strip()) <= 10:                  # date-only — no wall-clock to convert
            return dt.strftime("%b %-d, %Y")
        dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(display._ET_TZ)
        return dt.strftime("%b %-d, %-I:%M %p ET")
    except ValueError:
        return iso


def render(status: dict, brief_md: str, now: datetime) -> str:
    slug = status["slug"]
    cp, act = status["checkpoints"], status["agent_activity"]
    feed = status["change_feed"]
    alerts = display.load_alerts(slug)
    rows = status["claim_timestamps"]
    recent = status["recent_updates"]
    new_count = sum(1 for r in rows if r.get("is_new"))

    # Countdown seconds at render time; the page ticks DOWN from here using elapsed
    # real seconds (NOT the browser wall clock, which is on a different timeline).
    remaining = 0.0
    if cp["next_check"]:
        try:
            remaining = (datetime.fromisoformat(cp["next_check"]) - now).total_seconds()
        except ValueError:
            remaining = 0.0

    metrics = [
        ("Last checked", _fmt_dt(cp["last_checked_ts"] or cp["last_checked"]), ""),
        ("Next check", _fmt_dt(cp["next_check"]),
         '<div class="count" id="countdown">…</div>'),   # A2 live countdown
        ("Baseline", cp["baseline_date"] or "—", ""),
        ("Verified claims", str(act["claims_tracked"]),
         f'<div class="sub">cadence {cp["cadence_hours"]}h</div>'),
    ]
    metric_html = "".join(
        f'<div class="metric"><div class="mlabel">{html.escape(l)}</div>'
        f'<div class="mval">{html.escape(str(v))}</div>{extra}</div>'
        for l, v, extra in metrics
    )

    if feed:
        feed_html = "".join(
            f'<li><code>{html.escape(e["date"])}</code> — {html.escape(e["subject"])}</li>'
            for e in feed
        )
    else:
        feed_html = '<li class="muted">No changes recorded yet.</li>'

    # "Just updated" — claims a monitor run touched in the last 24h (A4). Empty on a
    # fresh baseline, which is the point: NEW is tied to a monitor action, not age.
    if recent:
        recent_html = "".join(
            f'<li><span class="badge">NEW</span> <b>{html.escape(_fmt_dt(r.get("detected_at") or r.get("date")))}</b> — '
            f'{html.escape(str(r.get("headline", r.get("subject_key", ""))))}'
            + (f'<br><span class="muted">{html.escape(str(r.get("old_value")))} → '
               f'{html.escape(str(r.get("new_value")))}</span>' if r.get("new_value") else "")
            + "</li>"
            for r in recent
        )
    else:
        recent_html = '<li class="muted">Nothing updated in the last 24h.</li>'

    if alerts:
        alert_html = "".join(
            f'<li><b>{html.escape(_fmt_dt(a.get("detected_at") or a.get("date")))}</b> — '
            f'{html.escape(str(a.get("headline", a.get("so_what", a))))}</li>'
            for a in alerts
        )
    else:
        alert_html = '<li class="muted">No material changes alerted yet.</li>'

    row_html = "".join(
        f"<tr><td>{'<span class=\"badge\">NEW</span>' if r.get('is_new') else ''}</td>"
        f"<td>{html.escape(str(r.get('subject_key','')))}</td>"
        f"<td>{html.escape(str(r.get('section','')))}</td>"
        f"<td>{html.escape(str(r.get('as_of','')))}</td>"
        f"<td>{html.escape(str(r.get('verified_on','')))}</td></tr>"
        for r in rows
    )

    brief_json = json.dumps(brief_md)

    logo_uri = _img_data_uri(_asset("scout_logo_t.png", "scout_logo.png"), height=64)
    favicon_uri = _img_data_uri(_asset("scout_icon_t.png", "scout_icon.png"), height=64, square=True)
    favicon_tag = f'<link rel="icon" type="image/png" href="{favicon_uri}">' if favicon_uri else ""
    logo_tag = f'<img class="logo" alt="Scout" src="{logo_uri}">' if logo_uri else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scout — {html.escape(_pretty(slug))}</title>
{favicon_tag}
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 2rem; max-width: 1200px; margin-inline: auto; line-height: 1.5; }}
  h1 {{ margin: 0 0 .25rem; }}
  .brand {{ display: flex; align-items: center; gap: .65rem; }}
  .brand .logo {{ height: 58px; width: auto; }}
  .cap {{ color: #888; margin-bottom: 1.25rem; }}
  .activity {{ font-weight: 600; font-size: 1.05rem; margin: 1rem 0; }}
  .metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin: 1rem 0 1.5rem; }}
  .metric {{ border: 1px solid #8884; border-radius: 10px; padding: .75rem 1rem; }}
  .mlabel {{ color: #888; font-size: .8rem; }}
  .mval {{ font-size: 1.4rem; font-weight: 700; }}
  .count {{ color: #2a8; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: .2rem; }}
  .count.due {{ color: #e85; }}
  .sub {{ color: #888; font-size: .8rem; margin-top: .2rem; }}
  .cols {{ display: grid; grid-template-columns: 3fr 1fr; gap: 2rem; }}
  .brief {{ min-width: 0; }}
  /* Prose-block hierarchy (exec summary, battlecard zones, objection handling).
     A JS pass groups each title + its body paragraphs into .block. */
  #brief h2 {{ margin: 2.4rem 0 1rem; padding-bottom: .3rem; border-bottom: 1px solid #8883; }}
  #brief h3 {{ margin: 2rem 0 1rem; color: #888; text-transform: uppercase;
              letter-spacing: .04em; font-size: .9rem; }}
  #brief .block {{ margin: 0 0 1.9rem; padding-left: .9rem; border-left: 3px solid #2a85; }}
  #brief .block .btitle {{ font-size: 1.3rem; font-weight: 700; line-height: 1.3;
                          margin: 0 0 .6rem; }}
  #brief .block .bbody {{ margin: .55rem 0 .55rem 1.1rem; }}
  /* the soundbite / so-what line: the closer of each block */
  #brief .block .bbody:last-child {{ margin-top: .7rem; opacity: .92; }}
  .side h3 {{ margin: 1.25rem 0 .5rem; }}
  ul {{ padding-left: 1.1rem; }}
  li {{ margin-bottom: .35rem; }}
  code {{ background: #8882; padding: .1rem .3rem; border-radius: 4px; }}
  .muted {{ color: #888; }}
  .badge {{ background: #2a8; color: #fff; font-size: .65rem; font-weight: 700; padding: .1rem .35rem;
           border-radius: 4px; letter-spacing: .03em; vertical-align: middle; }}
  details {{ margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
  th, td {{ border: 1px solid #8884; padding: .35rem .5rem; text-align: left; }}
  th {{ background: #8881; }}
  hr {{ border: none; border-top: 1px solid #8884; margin: 1.5rem 0; }}
</style></head><body>
  <div class="brand">{logo_tag}<h1>Scout</h1></div>
  <div class="cap">Living competitive battlecards — every claim verified against its source, and
    kept current by an agent. &nbsp;·&nbsp; <b>{html.escape(_pretty(slug))}</b></div>

  <div class="activity">{html.escape(act["line"])}</div>
  <div class="metrics">{metric_html}</div>
  <hr>

  <div class="cols">
    <div class="brief"><div id="brief"></div></div>
    <div class="side">
      <h3>Just updated <span class="muted">({new_count})</span></h3><ul>{recent_html}</ul>
      <h3>Change feed</h3><ul>{feed_html}</ul>
      <h3>Alerts</h3><ul>{alert_html}</ul>
    </div>
  </div>

  <details open>
    <summary>Claim freshness — {len(rows)} claims ({new_count} updated &lt;24h)</summary>
    <p class="cap"><code>as_of</code> = the date the fact is true as-of ·
       <code>verified_on</code> = when grounding last confirmed the wording ·
       <b>NEW</b> = a monitor run touched this claim in the last 24h.</p>
    <table><thead><tr><th></th><th>subject_key</th><th>section</th><th>as_of</th>
      <th>verified_on</th></tr></thead><tbody>{row_html}</tbody></table>
  </details>

  <script>
    const brief = document.getElementById("brief");
    brief.innerHTML = window.marked ? marked.parse({brief_json}) : "<pre></pre>";

    // Group each prose block (a bold-only title <p> + its following body/closer
    // paragraphs) into a .block with hierarchy. Only exec-summary / battlecard /
    // objection sections produce title-only-bold paragraphs, so bullet sections
    // (rendered as <ul>) are untouched.
    const isTitle = el => el.tagName === "P" && el.childNodes.length === 1
      && el.firstElementChild && el.firstElementChild.tagName === "STRONG";
    const isBreak = el => isTitle(el) || /^H[1-6]$/.test(el.tagName)
      || el.tagName === "UL" || el.tagName === "OL";
    const kids = [...brief.children];
    let i = 0;
    while (i < kids.length) {{
      if (!isTitle(kids[i])) {{ i++; continue; }}
      const block = document.createElement("div");
      block.className = "block";
      brief.insertBefore(block, kids[i]);
      kids[i].classList.add("btitle");
      block.appendChild(kids[i]);
      let j = i + 1;
      while (j < kids.length && !isBreak(kids[j])) {{
        kids[j].classList.add("bbody");
        block.appendChild(kids[j]);
        j++;
      }}
      i = j;
    }}

    // Tick the countdown from elapsed REAL seconds since page load, starting at the
    // server-computed remaining — independent of the browser's wall clock.
    let remaining = {remaining:.0f};
    const t0 = performance.now();
    const el = document.getElementById("countdown");
    function tick() {{
      const left = remaining - (performance.now() - t0) / 1000;
      if (left <= 0) {{ el.textContent = "check due now"; el.classList.add("due"); return; }}
      const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = Math.floor(left % 60);
      el.textContent = "next check in " + h + "h " + String(m).padStart(2,"0") + "m "
        + String(s).padStart(2,"0") + "s";
    }}
    tick(); setInterval(tick, 1000);
  </script>
</body></html>"""


def main():
    cards = display.list_battlecards()
    if not cards:
        print("No battlecards found.", file=sys.stderr)
        sys.exit(1)
    slug = sys.argv[1] if len(sys.argv) > 1 else cards[0]
    status = display.card_status(slug)
    out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "out")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "index.html")
    with open(out, "w") as f:
        f.write(render(status, _read_current(slug), datetime.now()))
    print(f"Wrote {out} for slug '{slug}'  (next_check={status['checkpoints']['next_check']}, "
          f"new={sum(1 for r in status['claim_timestamps'] if r.get('is_new'))})")


if __name__ == "__main__":
    main()
