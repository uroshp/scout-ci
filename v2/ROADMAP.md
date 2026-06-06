# Scout — Roadmap & Known Limitations

This is a deliberate stopping point, not a finished product. Scout v2 cleared the bar it was built to clear — a working agentic system with verified generation and live monitoring — and the items below are the things I consciously deferred. Each is paired: the current limitation, and the next step I’d take.

Knowing where MVP ends and gold-plating begins was itself one of the project’s decisions. This document is that line, made explicit.

-----

## Verification & accuracy

**Grounding proves provenance, not currency.**
The deterministic grounding check confirms a claim’s evidence excerpt is really on its cited page. It does *not* prove the fact is still true — a stale page can ground perfectly and be wrong about the world. Currently mitigated by a verifier disconfirmation search on status claims (“has X been discontinued”) and by the monitoring loop catching changes over time.
*Next:* a dedicated currency layer — timestamp-aware source weighting, and a confidence decay on claims whose source predates the last material event in their subject area.

**Materiality judgment is high-variance run-to-run.**
The monitoring materiality pass (the model deciding what’s a material change) returns different results across runs on the same window — it’s inherently non-deterministic. Mitigated by frequency: more checks raise the odds a given change is caught within a day or two, rather than relying on perfect single-run recall.
*Next:* ensemble or multi-pass materiality with a consensus threshold; track recall against a labeled change set to quantify the miss rate instead of estimating it.

**~33% of cited sources are unreachable by the grounding fetcher.**
The independent verification fetch uses a plain HTTP client, which can’t reach Cloudflare-protected, paywalled, or datacenter-IP-blocked pages (SEC.gov, some primary sources). Claims anchored only on unreachable sources get cut. This trades coverage for provability — deliberately — but it does narrow the source pool.
*Next:* a fallback fetch path (headless browser / residential proxy) for high-tier sources that block plain HTTP, used only to confirm grounding on otherwise-cut claims, never to expand the claim set unverified.

-----

## Monitoring

**Strict triage trades recall for cost.**
The cost-control design escalates to the expensive materiality pass only when triage flags genuinely substantial news. A borderline-material item tagged “minor” by the cheap triage model isn’t lost, but it’s caught on the next check rather than same-day. Fine for a daily-cadence battlecard; wrong for real-time alerting.
*Next:* a tunable triage threshold per card (tighter for slow/stable competitors, looser for fast movers), and a periodic “deep sweep” that re-runs full materiality regardless of triage, to catch what the gate missed.

**Per-competitor cadence is supported but not tuned.**
The `cadence_hours` field lets each card check at its own rate, but the launch config is uniform. Fast movers and stable competitors don’t need the same frequency.
*Next:* auto-tune cadence from observed change frequency — a card that hasn’t moved in N checks backs off automatically; one that just had a material change tightens temporarily.

-----

## Self-serve generation

**No per-user authentication.**
The “free generation” launch window is a single global pool on a public URL — there’s no per-user accounting, so the pool can be consumed by anyone. The hard spend cap (per-run and aggregate ledger) is the real backstop, not the counter.
*Next:* lightweight auth (email magic-link or OAuth) for per-user quotas, which also enables the feedback-for-access gating cleanly.

**Generation is async, not in-app synchronous.**
Self-serve requests run out-of-band on the same SDK pipeline (triggered via a committed request + a GitHub Action), and the user polls for the result. This was chosen over a container-hosted synchronous path to avoid standing infrastructure and a Docker/CLI-in-container risk — at the cost of an instant in-browser result.
*Next:* a container host (Cloud Run or similar) for synchronous in-app generation, if instant results ever justify the standing service. The pipeline is identical; only the trigger/topology changes.

**The progress bar is a timed estimate, not a live job feed.**
Because generation runs out-of-band, the in-app progress bar is decorative — timed to the average run, with honest “~6–8 minutes” copy — not a real-time readout of the actual job.
*Next:* have the async job write status checkpoints to the store that the app polls, for a true live progress feed.

-----

## Presentation

**Summary view is derived by selection, not generation.**
The “5-minute brief” at the top selects and reorganizes existing verified claims rather than generating a fresh digest. This keeps it grounded (it inherits the claims’ verification) but reads slightly more mechanical than a written synthesis would.
*Next:* a constrained summary-generation pass that writes a digest *from* the verified claims and cites each — readability of generation, grounding of selection — if it can be done without reintroducing ungrounded prose.

-----

## Scope deliberately not pursued

These aren’t bugs or gaps — they’re directions I chose not to take, noted so the boundaries are clear:

- **Quick-scan / cheap tier** — built, measured, and removed. A deliberately lower-quality mode next to the verified one undercut the credibility the product rests on.
- **Multi-perspective cards** — each card arms one side. A neutral “both sides” view is a different product (market analysis, not a battlecard) and wasn’t the goal.
- **Real-time alerting** — the monitoring cadence is daily-to-weekly by design. Sub-hour breaking-news alerting would require the looser triage and the live-status infra above, and isn’t what a battlecard needs.
