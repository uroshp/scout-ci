import os
import random
from datetime import date

import streamlit as st

from research import generate_brief, verify_brief, save_report

APP_NAME = "Scout"
# Anchor to this file's folder so the app finds reports/ from any working directory
# (Streamlit Cloud runs from the repo root, not v1/).
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
DAILY_RUN_LIMIT = 15

APP_PASSWORD = os.environ.get("APP_PASSWORD", "worksmarter")

PROGRESS_MESSAGES = {
    "research": [
        "Reading everything the internet says about them so you don't have to...",
        "Digging through funding announcements and earnings calls...",
        "Stalking their careers page for hiring tells...",
        "Lurking in the forums where people say what they really think...",
    ],
    "draft": [
        "Connecting dots a human would need three coffees to connect...",
        "Figuring out what actually matters and what is just noise...",
        "Writing the verdict, not the encyclopedia...",
    ],
    "verify": [
        "Catching the AI before it makes things up...",
        "Fact-checking every claim like a paranoid editor...",
        "Cutting anything we cannot prove. Sorry, juicy rumors...",
        "Cross-examining the numbers until they confess...",
        "Making sure every link actually goes somewhere...",
    ],
    "final": [
        "Polishing. Almost ready to make you look smart in that meeting...",
    ],
}


def clean_output(text):
    idx = text.find("# Competitive Intelligence Brief")
    if idx != -1:
        text = text[idx:]
    # Escape dollar signs so Streamlit doesn't treat $...$ as LaTeX math
    text = text.replace("$", "\\$")
    # Remove leading whitespace on lines so they aren't rendered as code blocks
    lines = [line.lstrip() if line.startswith(("    ", "\t")) else line for line in text.split("\n")]
    text = "\n".join(lines)
    return text


def list_samples():
    if not os.path.isdir(REPORTS_DIR):
        return []
    files = [f for f in os.listdir(REPORTS_DIR) if f.endswith(".md")]
    files.sort()
    samples = []
    for f in files:
        base = f.rsplit("_", 2)[0]
        label = base.replace("_", " ").replace("-", " ")
        samples.append((label, os.path.join(REPORTS_DIR, f)))
    return samples


def read_sample(path):
    with open(path, "r") as fh:
        return format_report(clean_output(fh.read()))

def check_daily_limit():
    today = date.today().isoformat()
    if st.session_state.get("run_date") != today:
        st.session_state["run_date"] = today
        st.session_state["run_count"] = 0
    return st.session_state.get("run_count", 0) < DAILY_RUN_LIMIT


def increment_run_count():
    st.session_state["run_count"] = st.session_state.get("run_count", 0) + 1


def password_gate():
    if st.session_state.get("authed"):
        return True
    st.title(APP_NAME)
    st.caption("Competitive intelligence that fact-checks itself.")
    pw = st.text_input("Enter access password", type="password")
    if pw:
        if pw == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()


def main():
    st.set_page_config(page_title=APP_NAME, layout="centered")
    password_gate()

    st.title(APP_NAME)
    st.caption("Competitive intelligence that fact-checks itself. Every claim is verified against real sources before it reaches you.")

    tab_samples, tab_run = st.tabs(["Sample Reports", "Run Your Own"])

    with tab_samples:
        st.subheader("See it in action")
        st.write("These are real reports the tool generated. Pick one to read.")
        samples = list_samples()
        if not samples:
            st.info("No sample reports found yet.")
        else:
            labels = [s[0] for s in samples]
            choice = st.selectbox("Choose a sample", labels)
            path = dict(zip(labels, [s[1] for s in samples]))[choice]
            st.markdown("---")
            st.markdown(read_sample(path))

    with tab_run:
        st.subheader("Generate a live report")
        st.write("Enter a company to research. Optionally add your own company to get a head-to-head battlecard, and a focus area to narrow it down.")
        st.caption("This takes about 2 minutes. We research the company, then fact-check every single claim against real sources before showing you anything.")

        target = st.text_input("Company to research (required)", placeholder="e.g. Microsoft")
        perspective = st.text_input("Your company (optional)", placeholder="e.g. Google")

        focus = st.text_input("Focus area (optional)", placeholder="e.g. enterprise GTM, cloud infrastructure, ads")

        if st.button("Generate report", type="primary"):
            if not target.strip():
                st.warning("Please enter a company to research.")
                st.stop()
            if not check_daily_limit():
                st.error("Daily report limit reached. Please check back tomorrow.")
                st.stop()

            progress = st.progress(0)
            status = st.empty()

            try:
                status.markdown("*" + random.choice(PROGRESS_MESSAGES["research"]) + "*")
                progress.progress(10)
                draft = generate_brief(target.strip(), perspective.strip() or None, focus.strip() or None)
                status.markdown("*" + random.choice(PROGRESS_MESSAGES["draft"]) + "*")
                progress.progress(55)

                status.markdown("*" + random.choice(PROGRESS_MESSAGES["verify"]) + "*")
                final = verify_brief(draft)
                progress.progress(90)
                status.markdown("*" + random.choice(PROGRESS_MESSAGES["final"]) + "*")

                final = format_report(clean_output(final))
                save_report(final, target.strip(), perspective.strip() or None)
                increment_run_count()

                progress.progress(100)
                status.empty()
                progress.empty()

                st.markdown("---")
                st.markdown(final)
                fname = ((perspective.strip() or "report") + "_vs_" + target.strip() + ".md").replace(" ", "-")
                st.download_button("Download report (.md)", data=final, file_name=fname, mime="text/markdown")
            except Exception as e:
                progress.empty()
                status.empty()
                st.error("Something went wrong generating the report: " + str(e))

def format_report(text):
    """Deterministic formatting cleanup - fixes what the model won't do reliably."""
    lines = text.split("\n")
    out = []

    # 1. Drop any cover-block lines before the real title
    #    (e.g. "Prepared for:", "Classification:", "Date:", decorative title lines)
    title_found = False
    for line in lines:
        stripped = line.strip()
        if not title_found:
            # The real title contains this phrase
            if "Competitive Intelligence Brief" in stripped:
                title_found = True
                # normalize to a clean H1
                out.append("# " + stripped.lstrip("#").strip().lstrip("🔵 ").strip())
                continue
            else:
                # skip everything before the title (cover junk, emojis, classification)
                continue
        out.append(line)

    if not title_found:
        out = lines  # fallback: leave as-is if no title found

    # 2. Stitch broken bullets: a "- " line whose text is on the next non-empty line
    stitched = []
    i = 0
    while i < len(out):
        line = out[i]
        if line.strip() == "-" or (line.strip().startswith("- ") and len(line.strip()) <= 2):
            # bullet marker with no text - pull next non-empty line up
            j = i + 1
            while j < len(out) and out[j].strip() == "":
                j += 1
            if j < len(out):
                stitched.append("- " + out[j].strip())
                i = j + 1
                continue
        stitched.append(line)
        i += 1

    # 3. Collapse blank lines between consecutive bullets
    final = []
    for k, line in enumerate(stitched):
        if line.strip() == "":
            prev = final[-1].strip() if final else ""
            # look ahead to next non-empty
            nxt = ""
            for m in range(k + 1, len(stitched)):
                if stitched[m].strip():
                    nxt = stitched[m].strip()
                    break
            if prev.startswith("- ") and nxt.startswith("- "):
                continue  # skip blank line between two bullets
        final.append(line)

    return "\n".join(final)



if __name__ == "__main__":
    main()
