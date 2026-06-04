"""Scout v2 — living-battlecard viewer (READ-ONLY).

Public view of pre-baked, git-committed battlecards. Surfaces the four "show the
agentic work" display elements around the verified brief. Does NOT trigger generation
(gated, last-stage) and does NOT run monitoring. Reads the store + git only.

Run:  streamlit run app_v2.py
"""
import os
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

from scout import display, store

# A2: ticks DOWN from the server-computed remaining seconds (__R__) using elapsed
# real seconds since load — independent of the browser's wall clock.
_COUNTDOWN_HTML = """<div id="cd" style="font:600 14px -apple-system,system-ui,sans-serif;
color:#2a8;font-variant-numeric:tabular-nums"></div><script>
let r=__R__;const t0=performance.now(),el=document.getElementById("cd");
function tick(){let l=r-(performance.now()-t0)/1000;
if(l<=0){el.textContent="check due now";el.style.color="#e85";return;}
let h=Math.floor(l/3600),m=Math.floor(l%3600/60),s=Math.floor(l%60);
el.textContent="next check in "+h+"h "+String(m).padStart(2,"0")+"m "+String(s).padStart(2,"0")+"s";}
tick();setInterval(tick,1000);</script>"""


def _pretty(slug: str) -> str:
    return slug.replace("__vs__", " vs ").replace("__", " · ").replace("-", " ")


def _read_current(slug: str) -> str:
    path = os.path.join(store.battlecard_dir(slug), "current.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return "_No rendered battlecard found for this slug._"


def main():
    st.set_page_config(page_title="Scout — Living Battlecards", layout="wide")
    st.title("Scout")
    st.caption("Living competitive battlecards — every claim verified against its source, "
               "and kept current by an agent.")

    cards = display.list_battlecards()
    if not cards:
        st.info("No battlecards have been generated yet.")
        return

    slug = st.sidebar.selectbox("Battlecard", cards, format_func=_pretty)
    status = display.card_status(slug)
    cp, act = status["checkpoints"], status["agent_activity"]
    recent = status["recent_updates"]
    rows = status["claim_timestamps"]
    new_count = sum(1 for r in rows if r.get("is_new"))

    # --- Elements 1 + 3: the agentic-work banner ---
    st.markdown(f"**{act['line']}**")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Last checked", cp["last_checked_ts"] or cp["last_checked"] or "—")
    c2.metric("Next check", cp["next_check"] or "—", help=f"cadence {cp['cadence_hours']}h")
    c3.metric("Baseline", cp["baseline_date"] or "—")
    c4.metric("Verified claims", act["claims_tracked"])

    # A2: live countdown (clock-independent — ticks from elapsed seconds since load).
    if cp["next_check"]:
        try:
            remaining = int((datetime.fromisoformat(cp["next_check"]) - datetime.now()).total_seconds())
        except ValueError:
            remaining = 0
        components.html(_COUNTDOWN_HTML.replace("__R__", str(max(remaining, 0))), height=34)
    st.divider()

    brief_col, side_col = st.columns([3, 1], gap="large")

    with brief_col:
        st.markdown(_read_current(slug))

    with side_col:
        # --- A4: claims a monitor run touched in the last 24h (empty on a fresh baseline) ---
        st.subheader(f"Just updated ({new_count})")
        if recent:
            for r in recent:
                when = r.get("detected_at") or r.get("date") or ""
                st.markdown(f"- 🟢 **NEW** `{when}` — {r.get('headline', r.get('subject_key',''))}")
        else:
            st.caption("Nothing updated in the last 24h.")

        # --- Element 2: per-card change feed (git heartbeat, now with time) ---
        st.subheader("Change feed")
        feed = status["change_feed"]
        if feed:
            for e in feed:
                st.markdown(f"- `{e['date']}` — {e['subject']}")
        else:
            st.caption("No changes recorded yet.")

        # --- Alert log (populated once monitoring runs) ---
        st.subheader("Alerts")
        alerts = display.load_alerts(slug)
        if alerts:
            for a in alerts:
                when = a.get("detected_at") or a.get("date", "")
                st.markdown(f"- **{when}** — {a.get('headline', a.get('so_what', a))}")
        else:
            st.caption("No material changes alerted yet.")

    # --- Element 4: timestamps on every claim (+ NEW flag) ---
    with st.expander(f"Claim freshness — {len(rows)} claims ({new_count} updated <24h)"):
        st.caption("`as_of` = the date the fact is true as-of · `verified_on` = when grounding "
                   "last confirmed the exact wording · `is_new` = a monitor run touched it <24h ago.")
        st.dataframe(rows, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
