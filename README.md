#  Agent Scout: living competitive intelligence tool that fact-checks itself

Give Scout a competitor - and optionally your own company and a focus area — and it returns a
strategic brief (executive summary, battlecard, objection handling, and more). **Every
factual claim is verified against reputable public sources with a journalistic bar, and anything that can’t be verified is cut and logged**. This verification and restraint alongside judgement about what's material to include is the main product.

🔗 **Live app: [agent-scout.ai](https://agent-scout.ai)** (public, read-only viewer of the living battlecards)

Verification is **deterministic and model-free**: separate code independently re-fetches each
cited source and confirms the quoted evidence is really there. No model can fake a citation,
because the code that checks provenance shares nothing with the code that writes the claim. And
the v2 battlecards are **living** — a daily agent monitors each competitor before the work day starts and at lunchtime and updates/includes a claim only when it judges the change *material* to the GTM story. The bar is: could a sales rep use this in a customer conversation?

# But.. why?

I grew up with technology - putting together desktop machines, compiling linux kernels, learning about the world through the internet. AI is the most fascinating technology for me personally. 2001: A Space Odyssey has always been my favorite movie.

What's been fascinating about the AI space is letting the models run free and make decisions. The question is - do I trust them? How do I build in and teach to increase trust? Can a model just go do a task and that's actually useful and not slop? What about multiple models doing it with little visibility into the mechanics? 

Scout was born from that curiosity. Can I get a multi-agent orchestra to solve a real business problem and produce a genuinely useful tool, repeatedly and with reliability?

This build answers that question. It was built by directing Claude Code while I made the architecture, system-design and product decisions.

# Why Competitive briefs?
Competitive Battlecards - What GTM teams use in preparing for customer conversations. A great competitive brief used by a skilled rep can influence closed business - it directly impacts revenue. These need to be factual, up to date, punchy, opinionated, relevant and even self-aware. 

Business process: These are produced through copious research and locked as a document. The moment they are shared, they become stale.

Business pain: Time accelerates staleness. Industries are moving faster than ever. GTM teams take time to onboard.

Solution: Live battlecards with rigorous fact checking, updated regularly, enabling GTM teams to act on the information instantly. 


# How it came to be

This repo holds two generations of Scout, each **self-contained in its own folder**:

|      |What it is                                                                                                                                                                                                                  |Folder      |
|------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------|
|**v1**|The shipped **pipeline** — a fixed `generate → verify → render` flow (two Claude API calls, web search on both). Frozen and runnable on its own.                                                                            |[`v1/`](v1/)|
|**v2**|**Agent Scout** — the model-driven evolution: a tool-use loop that decides its own steps, a fetch-and-read-the-source grounding tool, and **living, monitored battlecards** kept current by a daily agent. The deployed app.|[`v2/`](v2/)|

🔗 **[Live demo](https://agent-scout.ai)** (v2)

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
directory. See each folder’s README for the full story: **<v1/README.md>** ·
**<v2/README.md>**.

## How v2 works

- **Agentic orchestration** — an Opus orchestrator decides what to research, delegates to Sonnet
  research/verifier subagents, judges materiality and decides when to update or stay silent.
- **Deterministic grounding** — a model-free check (httpx + fuzzy match) re-fetches each source
  and confirms the verbatim evidence; unverifiable claims are cut and recorded in a visible Cut Log.
- **Living battlecards** — agents are asked daily to refresh each competitor; a cheap triage gate
  escalates to materiality judgment only when there’s substantial news, keeping quiet-day cost
  near zero.
- **Control vs. autonomy** — the model owns judgment; code owns the trust-critical side effects
  (grounding, commits, email, rendering).

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

> **Deployment note:** the live app is v2 with its Streamlit Cloud “Main file path” set to
> `v2/app_v2.py`. The two GitHub Actions run inside `v2/`.

## Why two versions

The v1→v2 jump is the real story: same interface, same methodology but the orchestration core
moves from *a pipeline I control* to *a loop the model drives*. Keeping both side by side — each
clean and independently launchable — is deliberate; the git history is meant to show the evolution.

**Roadmap & known limitations:** what I deferred on purpose, and the next step for each, lives
in [`v2/ROADMAP.md`](v2/ROADMAP.md). Knowing where to stop was itself one of the decisions.

— [LinkedIn](https://www.linkedin.com/in/urospajic) · MIT — see <LICENSE>.