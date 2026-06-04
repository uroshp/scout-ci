"""Scout v2 — living-battlecard viewer (READ-ONLY).

Public view of pre-baked, git-committed battlecards. Surfaces the four "show the
agentic work" display elements around the verified brief. Does NOT trigger generation
(gated, last-stage) and does NOT run monitoring. Reads the store + git only.

Run:  streamlit run app_v2.py
"""
import os

import streamlit as st

from scout import display, store


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

    # --- Elements 1 + 3: the agentic-work banner ---
    st.markdown(f"**{act['line']}**")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Last checked", cp["last_checked"] or "—")
    c2.metric("Next check", cp["next_check"] or "—")
    c3.metric("Baseline", cp["baseline_date"] or "—")
    c4.metric("Verified claims", act["claims_tracked"])
    st.divider()

    brief_col, side_col = st.columns([3, 1], gap="large")

    with brief_col:
        st.markdown(_read_current(slug))

    with side_col:
        # --- Element 2: per-card change feed (git heartbeat) ---
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
                st.markdown(f"- **{a.get('date', '')}** — {a.get('headline', a.get('so_what', a))}")
        else:
            st.caption("No material changes alerted yet.")

    # --- Element 4: timestamps on every claim ---
    rows = status["claim_timestamps"]
    with st.expander(f"Claim freshness — {len(rows)} claims (as-of vs last-verified)"):
        st.caption("`as_of` = the date the fact is true as-of · `verified_on` = when grounding "
                   "last confirmed the exact wording on its source page.")
        st.dataframe(rows, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
