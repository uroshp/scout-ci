# Scout — Roadmap & Known Limitations

This is a study project and I decided to stop when Scout v2 cleared the bar it was built to clear: a working agentic system with verified generation and live monitoring. Items below are the ideas popping up as I was working on the project.

Knowing where MVP ends and gold-plating begins was itself one of the project’s decisions. This document makes those decisions explicit.

-----

## Verification & accuracy

**Grounding proves provenance, not currency.**
The deterministic grounding check confirms a claim’s evidence excerpt is really on its cited page. It does *not* prove the fact is still true so a stale page can ground perfectly and be wrong about the world. Currently mitigated by a verifier disconfirmation search on status claims (“has X been discontinued”) and by the monitoring loop catching changes over time.
*Next:* a dedicated currency layer: timestamp-aware source weighting and a confidence decay on claims whose source predates the last material event in their subject area.

**Materiality judgment is high-variance run-to-run.**
The monitoring materiality pass (the model deciding what’s a material change) returns different results across runs on the same window so it’s inherently non-deterministic. Mitigated by frequency: more checks raise the odds a given change is caught within a day or two, rather than relying on perfect single-run recall.
*Next:* ensemble or multi-pass materiality with a consensus threshold; track recall against a labeled change set to quantify the miss rate instead of estimating it.

**~33% of cited sources are unreachable by the grounding fetcher.**
The independent verification fetch uses a plain HTTP client, which can’t reach Cloudflare-protected, paywalled or datacenter-IP-blocked pages (SEC.gov, some primary sources). Claims anchored only on unreachable sources get cut. This is a deliberate trade of coverage for provability, but it does narrow the source pool.
*Next:* a fallback fetch path (headless browser / residential proxy) for high-tier sources that block plain HTTP, used only to confirm grounding on otherwise-cut claims, never to expand the claim set unverified. In a demo environment this is acceptable, in a real deployment it would not be. Ideally, MCP integration with reliable sources such as Bloomberg, Crunchbase. However, this being a study project and these tools carry a real cost, that was not in the spec.

-----

## Monitoring

**Strict triage trades recall for cost.**
The cost-control design escalates to the expensive materiality pass only when triage flags genuinely substantial news. A borderline-material item tagged “minor” by the cheap triage model isn’t lost, but it’s caught on the next check rather than same-day. Fine for a daily-cadence battlecard; wrong for real-time alerting. In a demo environment this is acceptable, in a real deployment it would not be.
*Next:* a tunable triage threshold per card (tighter for slow/stable competitors, looser for fast movers) and a periodic “deep sweep” that re-runs full materiality regardless of triage, to catch what the gate missed.

**Per-competitor cadence is supported but not tuned.**
The `cadence_hours` field lets each card check at its own rate, but the launch config is uniform. Fast movers and stable competitors don’t need the same frequency. e.g. frontier labs move faster than legacy software businesses.
*Next:* auto-tune cadence from observed change frequency: a card that hasn’t moved in N checks backs off automatically; one that just had a material change tightens temporarily.

-----

## Self-serve generation

**No per-user authentication.**
The “free generation” launch window is a single global pool on a public URL so there’s no per-user accounting. The pool can be consumed by anyone. The hard spend cap (per-run and aggregate ledger) is the real backstop, not the counter.
*Next:* lightweight auth (email magic-link or OAuth) for per-user quotas, which also enables the feedback-for-access gating cleanly. This was acceptable given the nature of the project.

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

These are directions I chose not to take, noted so the boundaries are clear:

- **Quick-scan / cheap tier** — built, measured and removed. A deliberately lower-quality mode next to the verified one undercut the credibility the product rests on.
- **Multi-perspective cards** — each card arms one side. A neutral “both sides” view is a different product (market analysis, not a battlecard) and wasn’t the goal.
- **Real-time alerting** — the monitoring cadence is daily-to-weekly by design. Sub-hour breaking-news alerting would require the looser triage and the live-status infra above, and isn’t what a battlecard needs.

## Writing: say more with less (todo, 2026-10-08)

Owner's principle (Uroš, 2026-10-08): the 170-word render cap was an artificial limit put in because
paragraphs ran long. The real issue is saying more with less: what does a sales rep need to hear,
plainly, in English, not in telegraph, so they can have the conversation and digest it fast. If it
can be said in 50 words, great; in 100, great; 170 is a last resort for dense text. The models
should not be writing toward the cap and then cutting every time; told to cut, they produce
telegraph. It comes down to only the information that is relevant, said plainly.

Assessment and plan (the message as discussed, kept verbatim):

You have the diagnosis right, and the fix is in the incentives, not in the number.

Why it keeps happening. Two rules push in the same direction. The author is asked to be faithful and
complete, so it writes everything the source supports. Then the fidelity judge, whose job is to catch
invented or altered facts, rejects any condensed version that "deletes a still-true fact". So
completeness is rewarded twice, relevance is never judged, and the only thing that ever says "too
much" is a word counter at the end. The condenser is then told to shorten, which is how you get
telegraph: it drops articles and connectors because every fact is protected and the words are not.
Today's lead is the pattern in miniature: 224 words, two Soundbites that restated the body, and six
sentences of mechanism. A rep needs the one-line claim, the one condition, one line to say, and the
ask. About 70 words.

What to change. Three rules, all in the private instruction blocks, none in code:

1. The author writes for a rep with thirty seconds, once. The target is "what does a rep need to
   say, plainly, to have the conversation", with the principle you stated: if it fits in 50 words,
   50. The budget by claim type is guidance, not a target. One Soundbite at most, and it may not
   restate the body.
2. The condense judge guards truth, not completeness. Its rule becomes: nothing false added, every
   number and date intact, meaning preserved. Leaving out detail a rep does not need is allowed, and
   that is the whole point of a condense. Today's verdict ("deletes still-true facts") is the rule
   that makes every cut fail.
3. The authorship judge gets a relevance question. Alongside "is it faithful", it asks "is there a
   sentence here a rep does not need", and rejects on that too. That moves the judgment to where the
   text is born instead of to a counter at the end.

The 170 cap stays as the backstop, raised to 180 since you said a few words over is fine, and it
should rarely fire once the author is aiming at 70.

On "Claude writes horribly". Partly true and partly the prompt. Told to compress, any of these models
writes telegraph. Told to write for a specific reader with a specific job, they write readable
English. The second and third rules above make that the standing instruction, so it is worth trying
before paying for a different writer.

How you would see it working. Nothing new to read. The ten applied edits you grade each week are the
sample; if the leads landing from next week read like something you would say on a call, it worked.
I would add one number to the weekly check-in, median lead length, and nothing else.

These are edits to three instruction blocks in the private pack, which you calibrate. When you want
them, I draft the wording for you to approve, and they take effect on the next morning run.

Status: edits drafted for the owner's approval (private pack blocks: the writing style, the
propagation author and judge, the condense reformatter and its fidelity judge, the audience author
addendum) plus the code backstop raise to 180. Not applied until approved.
