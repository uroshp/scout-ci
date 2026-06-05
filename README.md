# Scout — competitive intelligence that fact-checks itself

Give Scout a competitor — and optionally your own company and a focus area — and it returns a
strategic brief (executive summary, battlecard, objection handling, and more) where **every
factual claim is verified against public sources, and anything that can't be verified is cut and
logged**. The verification step is the product, not a polish pass.

This repo holds two generations of Scout, each **self-contained in its own folder**:

| | What it is | Folder |
|---|---|---|
| **v1** | The shipped **pipeline** — a fixed `generate → verify → render` flow (two Claude API calls, web search on both). Frozen and runnable on its own. | [`v1/`](v1/) |
| **v2** | **Agent Scout** — the model-driven evolution: a tool-use loop that decides its own steps, a fetch-and-read-the-source grounding tool, and **living, monitored battlecards** kept current by a daily agent. The deployed app. | [`v2/`](v2/) |

🔗 **[Live demo](https://agent-scout.streamlit.app)** (v2)

## Quick start

```bash
git clone https://github.com/uroshp/scout-ci.git
cd scout-ci
cp .env.example .env                  # add your Anthropic API key

# v2 (Agent Scout — the living battlecard viewer)
pip install -r v2/requirements.txt
streamlit run v2/app_v2.py

# v1 (the verified-brief pipeline)
pip install -r v1/requirements.txt
streamlit run v1/app.py
```

Each app anchors its data paths to its own folder, so both run correctly from any working
directory. See each folder's README for the full story: **[v1/README.md](v1/README.md)** ·
**[v2/README.md](v2/README.md)**.

## Repo layout

```
scout-ci/
├── v1/                     # the pipeline (app.py, research.py, methodology.md, reports/)
├── v2/                     # Agent Scout (app_v2.py, scout/ package, battlecards/, …)
├── .github/workflows/      # v2 automation: monitor (daily) + selfserve (on-demand), run inside v2/
├── .streamlit/             # shared Streamlit config
├── .env.example            # shared env template (ANTHROPIC_API_KEY, …)
├── CLAUDE.md · LICENSE
└── README.md               # you are here
```

> **Deployment note:** the live app is v2 with its Streamlit Cloud "Main file path" set to
> `v2/app_v2.py`. The two GitHub Actions run inside `v2/`.

## Why two versions

The v1→v2 jump is the real story: same interface, same methodology, but the orchestration core
moves from *a pipeline I control* to *a loop the model drives*. Keeping both side by side — each
clean and independently launchable — is deliberate; the git history is meant to show the evolution.

— [LinkedIn](https://www.linkedin.com/in/urospajic) · MIT — see [LICENSE](LICENSE).
