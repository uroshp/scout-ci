# Scout v2 — Architecture

Three views of how Agent Scout works. The throughline is the **control-vs-autonomy boundary**:
the model owns judgment; deterministic code owns anything trust-critical. The clearest expression
of that line is the **grounding check** — a model-free step that re-fetches each cited source and
confirms the quoted evidence is actually on the page. No claim survives without passing it.

> Diagrams are [Mermaid](https://mermaid.js.org/); GitHub renders them inline.

## 1. Generation flow

An Opus orchestrator plans the brief and delegates legwork to Sonnet subagents. Every drafted
claim then hits the grounding gate (highlighted) — the **model-free trust boundary**. Claims whose
verbatim excerpt is found on the live page are kept; everything else is cut and logged.

```mermaid
flowchart TD
    Q["Input: competitor (+ your company, focus area)"] --> O["Orchestrator — Opus<br/>plans the brief, delegates, synthesizes"]
    O -->|parallel research tasks| R["Researcher subagents — Sonnet<br/>web search, read sources"]
    R --> O
    O -->|draft claims| V["Verifier subagent — Sonnet<br/>re-checks each claim; picks one anchor source + a verbatim excerpt"]
    V --> O
    O -->|each claim| G{{"GROUNDING CHECK — model-free<br/>re-fetch source_url, confirm the excerpt is on the page"}}
    G -->|excerpt found| K["Keep claim"]
    G -->|missing / unreachable / excluded source| C["CUT — recorded in the Cut Log"]
    K --> RC["Living battlecard<br/>claims.json + current.md"]
    C --> RC
    classDef gate stroke:#b91c1c,stroke-width:3px;
    class G gate;
```

## 2. Monitoring loop

Each card is re-checked on a fixed daily schedule. A cheap Haiku **triage gate** runs every time;
the expensive Opus **materiality** judgment runs only when triage flags genuinely substantial
news — so a quiet day costs pennies, and only *material* changes ever touch a claim.

```mermaid
flowchart TD
    B["Baseline battlecard (committed claims)"] --> T["Daily trigger — Cloud Scheduler / cron<br/>per-card cadence, fixed wall-clock windows"]
    T --> TR{"Triage gate — Haiku<br/>cheap, few searches:<br/>any substantial news?"}
    TR -->|no material news| L["Log 'all current', bump last_checked, stop<br/>(the near-zero quiet-day floor)"]
    TR -->|substantial candidate| M{"Materiality judge — Opus<br/>does it change how we WIN, LOSE,<br/>or HANDLE AN OBJECTION?"}
    M -->|not material| L
    M -->|material change| U["Update claim(s), re-ground,<br/>commit + alert"]
```

## 3. Control vs. autonomy

The design rule for every step: **deterministically fixable → code; requires judgment → model.**
Provenance, identity, and side effects are never left to the model.

```mermaid
flowchart LR
    subgraph MODEL["Model owns — judgment"]
        direction TB
        m1["What to research"]
        m2["Drafting the claims"]
        m3["Verification: read the source, choose the anchor"]
        m4["Materiality judgment — what's worth an update"]
        m5["What to cut"]
    end
    subgraph CODE["Code owns — trust-critical, deterministic"]
        direction TB
        c1["Grounding check — re-fetch + match the excerpt"]
        c2["Claim ID — hash of the subject_key"]
        c3["Git commits"]
        c4["Email / alerts"]
        c5["Rendering, sorting, dedup"]
    end
```

See [`v2-agent-spec.md`](v2-agent-spec.md) for the full design and [`claim-object.md`](claim-object.md)
for the claim schema + grounding contract.
