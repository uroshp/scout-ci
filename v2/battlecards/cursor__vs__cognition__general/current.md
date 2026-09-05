# Competitive Intelligence Brief: Cursor vs Cognition

## Executive Summary

**Our five biggest vulnerabilities: billing trust, the now-closed SpaceX acquisition, OpenAI's model cutoff and two RCE disclosures (DuneSlide, patched; Mindgard's git.exe flaw, fixed quietly with no CVE yet). Have answers ready.**

The \$60B SpaceX deal became effective August 14, 2026, and OpenAI is now ending Cursor's direct model access November 12, 2026, about 5% of traffic. The 2025 pricing blow-ups still sting. DuneSlide RCE is patched in Cursor 3.0. Mindgard's git.exe auto-executes on Windows with no click; Cursor shipped a fix July 13 but has no advisory or CVE yet.

**Soundbite:** "The SpaceX deal is closed, the OpenAI slice is small and what you're buying is what ships today: Grok 4.5, Cursor Router and Fortune 500-scale adoption."

**So what:** Answer each directly. On billing, cite split usage pools, the spend dashboard and dollar-threshold alerts. On SpaceX and the OpenAI cutoff, lead with today's shipping product, Grok 4.5 and Cursor Router. On DuneSlide, point to Cursor 3.0. On Mindgard, confirm the fix shipped with no advisory or CVE yet. ([x.com](https://x.com/OpenAI/status/2093515564786540695))

**Cognition has graduated from viral demo to a scaled competitor: sell against it that way.**

Cognition raised over \$1B at a \$26B valuation and its run-rate has grown to approaching \$1 billion, roughly double the \$492M it reported in May, with named customers including Goldman Sachs, Mercedes-Benz and the U.S. Army.

**Soundbite:** "Cognition is real and growing fast, so we sell on proven scale and reliability, not on Devin still being a toy."

**So what:** Retire any 'Devin is a toy that fails most tasks' talk track. It's stale and makes us look uninformed. Win on proven scale and reliability. ([finance.yahoo.com](https://finance.yahoo.com/technology/ai/articles/ai-startup-cognition-funding-talks-035336592.html))

**Cursor still leads on revenue, though Cognition is closing the gap.**

Cursor's roughly \$2B ARR is now about 2x Cognition's run-rate, which has grown to approaching \$1 billion, down from a 4x gap in May, and Cursor still sits in more than half the Fortune 500.

**Soundbite:** "We're still twice the scale, in production across more than half the Fortune 500, not funding round math."

**So what:** Anchor enterprise conversations on installed base and revenue durability, and drop the old 4x figure. A buyer who has seen the Bloomberg number will catch it. ([finance.yahoo.com](https://finance.yahoo.com/technology/ai/articles/ai-startup-cognition-funding-talks-035336592.html))

**Devin's reliability ceiling is our sharpest wedge, and Cognition admits it.**

In its own 2025 performance review Cognition concedes Devin 'can't independently tackle an ambiguous coding project end-to-end' and degrades when requirements change mid-task. Roughly one in three of Devin's autonomous runs still doesn't produce code the team ships.

**So what:** Steer evaluations toward ambiguous, iterative, architecture-heavy work where human-in-the-loop wins. Use Cognition's own words against them. ([cognition.ai](https://cognition.ai/blog/devin-annual-performance-review-2025))

**The Windsurf-to-Devin-Desktop forced migration is a live churn window: work it now.**

On June 2 Cognition retired the Windsurf brand, relaunched it as Devin Desktop, and is sunsetting the Cascade local agent on July 1, forcing every Windsurf/Cascade user through a re-onboarding. Some Windsurf users already defected to Cursor during last year's Anthropic model cutoff.

**So what:** Target Windsurf/Cascade accounts through June with a 'skip the forced migration' message before the July 1 cutover locks them in. ([devin.ai](https://devin.ai/blog/windsurf-is-now-devin-desktop/))

## Snapshot

- Cognition raised over \$1B in a Series D at a \$26B post-money valuation (\$25B pre-money), announced May 27, 2026, up from a \$10.2B valuation just eight months earlier. Round led by Lux Capital, General Catalyst and 8VC. ([techcrunch.com](https://techcrunch.com/2026/05/27/ai-coding-startup-cognition-raises-1b-at-25b-pre-money-valuation/))
- Cognition has now raised more than \$2.5B in total funding to date. ([thenextweb.com](https://thenextweb.com/news/cognition-just-raised-1-billion-at-a-26-billion-valuation-and-90-of-its-own-code-is-written-by-its-ai))
- Cognition's annualized revenue run-rate is now approaching \$1 billion, per Bloomberg's reporting on its funding talks (unaudited, sourced to people familiar), roughly double the \$492M it reported in May 2026 (company-stated, unaudited). That \$492M was up roughly 13x from \$37M a year earlier, and Cognition says enterprise usage has grown more than 10x since January 2026. ([finance.yahoo.com](https://finance.yahoo.com/technology/ai/articles/ai-startup-cognition-funding-talks-035336592.html))
- Cognition's annualized revenue run-rate is now approaching \$1 billion, roughly double the \$492M it reported in May 2026, per Bloomberg's reporting on its new funding talks (unaudited, sourced to people familiar with the matter). Cursor's roughly \$2B ARR still leads, but the gap has narrowed from about 4x to roughly 2x. ([finance.yahoo.com](https://finance.yahoo.com/technology/ai/articles/ai-startup-cognition-funding-talks-035336592.html))
- Cognition (Cognition Labs) was founded in November 2023 and is headquartered in San Francisco; its founders are CEO Scott Wu, CTO Steven Hao, and CPO Walden Yan. ([research.contrary.com](https://research.contrary.com/company/cognition))
- Cognition's flagship is Devin, an autonomous cloud-based AI software engineer; as of June 2, 2026 its acquired Windsurf IDE was rebranded 'Devin Desktop,' positioned as a command center for managing local and cloud agents. ([devin.ai](https://devin.ai/blog/windsurf-is-now-devin-desktop/))
- Cognition acquired Windsurf in July 2025 (price undisclosed) after Google paid \$2.4B to hire away Windsurf's CEO and research leaders; Windsurf had reached ~\$82M ARR and 350+ enterprise customers at the time of the deal. ([techcrunch.com](https://techcrunch.com/2025/07/14/cognition-maker-of-the-ai-coding-agent-devin-acquires-windsurf/))

## Recent Strategic Moves

- Cognition is set to close a new funding round at about a \$47 billion valuation, raising around \$1 billion, per Bloomberg (Sept 2, 2026). That is up 81% from the \$26 billion it reached in May, four months earlier, and investor interest hit nearly \$10 billion, so the final raise may run larger. Its annualized revenue is now above \$900 million, roughly double the \$492 million it reported in late May. The round is not yet finalized and terms may still change. ([bloomberg.com](https://www.bloomberg.com/news/articles/2026-09-02/ai-startup-cognition-set-to-raise-around-1-billion-at-a-47-billion-value))
- Cognition acquired The Interaction Company, maker of the consumer texting agent Poke, in a deal valuing it in the low nine figures, announced July 23, 2026. Cognition plans to fold Poke's personality and orchestration into Devin: co-founder Marvin von Hagen told TechCrunch that Poke could run multiple Devin pull requests at once and give Devin memory that persists across sessions. Poke is a consumer messaging assistant used for travel, scheduling and email, so it does not compete with Cursor's coding product today. ([cognition.com](https://cognition.com/blog/interaction))
- On July 8, 2026 Cognition launched SWE-1.7, its own coding model that it says reaches frontier-level quality at a fraction of the cost: about \$1.97 per task on its FrontierCode benchmark, running at 1000 tokens/sec on Cerebras inside Devin (Web, Desktop and CLI). On third-party benchmarks it trails Opus 4.8 and GPT-5.5 by a few points (42.3% on FrontierCode 1.1 vs 46.5% and 43.0%; 81.5% on Terminal-Bench 2.1) yet beats GPT-5.5 on SWE-Bench Multilingual (77.8% to 76.8%), and it far outscores Cursor's own Composer 2.5 (25.6% on FrontierCode). SWE-1.7 runs only inside Devin and is not sold as an API. ([cognition.com](https://cognition.com/blog/swe-1-7))
- On July 1, 2026 Cognition launched Devin Security Swarm, an autonomous product that finds, validates and remediates code vulnerabilities, available to enterprise customers globally on day one. Cognition's own benchmark of 50 real-world GitHub Security Advisories says Devin caught 36 (72% recall) at 30% lower cost per finding than the next most accurate tool, and its product page ranks Cursor's security scanning last at 26% recall. This is a Cognition-run benchmark in a category adjacent to core coding, and the product ships with no customer references yet. ([prnewswire.com](https://www.prnewswire.com/news-releases/cognition-launches-devin-security-swarm-to-tackle-the-vulnerability-backlog-302814800.html))
- **Cognition shipped Devin Fusion, a multi-model harness it says cuts cost 35% while holding frontier performance: prep the cost rebuttal.** On June 29, 2026 Cognition released Devin Fusion in preview, a 'sidekick' setup that runs a cheaper model in parallel with a frontier model and delegates execution to it while the frontier model plans and reviews. Cognition claims a 35% cost cut (41% with its Fable 5 model) on its own FrontierCode benchmark while matching frontier quality. It is preview-only and the numbers are company-stated on a Cognition-built benchmark, so treat the figure as unverified. ([cognition.com](https://cognition.com/blog/devin-fusion))
- June 4, 2026: Cognition launched an 'AI Productivity Guarantee' for enterprise customers: if Devin delivers less engineering value than the customer pays for, Cognition will fund usage until it does, up to \$10M. So what for Cursor: it signals enterprise buyers are pushing back on AI ROI claims. Bring measurable-lift proof points to every enterprise deal rather than competing on a financial backstop. ([cognition.ai](https://cognition.ai/blog/2))
- June 2, 2026: Cognition retired the Windsurf brand, relaunching it as 'Devin Desktop' and deprecating the Cascade local agent (hard sunset July 1, 2026) in favor of a Rust-rewritten 'Devin Local.' So what for Cursor: every Windsurf/Cascade user faces a forced migration before July 1, a concrete, time-boxed churn window to target. ([devin.ai](https://devin.ai/blog/windsurf-is-now-devin-desktop/))
- May 27, 2026: Cognition closed a \$1B+ Series D at a \$26B post-money valuation (led by Lux Capital, General Catalyst, 8VC), 2.5x its valuation eight months prior. So what for Cursor: Cognition is well-capitalized for a sustained enterprise push and won't be outspent on GTM. Compete on product fit, not on who has more runway. ([techcrunch.com](https://techcrunch.com/2026/05/27/ai-coding-startup-cognition-raises-1b-at-25b-pre-money-valuation/))
- April 14, 2026: Cognition restructured Devin's self-serve pricing, retiring the no-minimum Core plan and starting to charge for previously-free products (Ask Devin, DeepWiki, Devin Review); it conceded the change hits lighter users. So what for Cursor: price-sensitive lighter Devin users got pushed to a higher floor, a defection trigger to mine. ([cognition.ai](https://cognition.ai/blog/new-self-serve-plans-for-devin))
- August 2025: three weeks after acquiring Windsurf, Cognition laid off 30 staff and offered buyouts to the ~200 remaining; CEO Scott Wu told staff 'We don't believe in work-life balance.' So what for Cursor: raise integration and talent-retention risk with enterprises weighing Devin Desktop continuity: the team that built the IDE was largely cleared out. ([techcrunch.com](https://techcrunch.com/2025/08/05/three-weeks-after-acquiring-windsurf-cognition-offers-staff-the-exit-door/))

## Positioning and Differentiation

- Cognition still positions itself as cloud-agent vs local-agent, arguing a local agent's 'ceiling is your attention' and stops when you close your laptop. That line no longer holds against Cursor: since June 29, 2026 Cursor's iOS app lets developers launch always-on cloud agents and steer local Remote Control agents from their phone, then review diffs and merge PRs on the go. ([cursor.com](https://cursor.com/blog/ios-mobile-app))
- Cognition brands Devin as 'the first AI software engineer' and frames cloud agents as the fastest-growing way to build software (the company's own claim). ([cognition.ai](https://cognition.ai/blog/series-d))
- Cognition still markets itself as an independent, model-agnostic 'agent lab' that routes tasks across all major foundation models, an implicit contrast with Cursor's model dependence (the company's own claim). But on July 8, 2026 it shipped its own in-house model, SWE-1.7, sold only inside Devin and not offered as an API, adding a proprietary model to the same portfolio it says stays neutral. Cursor now routes across models too: Cursor Router, launched July 22, 2026, classifies every request and sends it to the best-suited model across desktop, web, iOS, CLI and the SDK, closing the gap Cognition's positioning claimed as its own. ([cursor.com](https://cursor.com/blog/router))
- Industry analysis frames the AI-coding market as a split bet: IDE-first (keep the engineer in the loop: Cursor) vs agent-first (delegate whole tasks to an autonomous agent: Devin). Useful framing because it lets a Cursor rep define the axis of the comparison on our terms. ([techtimes.com](https://www.techtimes.com/articles/317354/20260529/ai-coding-agents-cognitions-26b-raise-bets-agent-first-architecture-beats-ide-tools.htm))
- Grok 4.5 is the first model Cursor has built for more than software engineering, aimed at long-running work across data science, finance, legal work and other knowledge work in addition to coding. It is live now across desktop, web, iOS, CLI and the SDK, broadening Cursor's pitch from a coding tool to a platform for knowledge work generally. ([cursor.com](https://cursor.com/blog/grok-4-5))

## Pricing and Packaging

- Devin's self-serve pricing (post-April 2026): Free; Pro \$20/mo; Max \$200/mo; and Teams at usage-based pricing with an \$80/month minimum, plus custom Enterprise. The \$80 base plus per-seat cost makes small-team adoption structurally pricier than Cursor's per-seat Teams plan. ([cognition.ai](https://cognition.ai/blog/new-self-serve-plans-for-devin))
- Cursor's Teams pricing keeps Standard at \$32/seat/mo annual (\$40 monthly) and Premium at \$96/seat/mo annual (\$120 monthly, 5x the usage at 3x the cost), and now splits every seat into two usage pools, one for first-party Composer and Auto models, one for third-party API calls, with a real-time dashboard split by pool and dollar-threshold spend alerts over Slack or email. There's still no flat team base fee, undercutting Cognition's \$80 base plus per-seat Teams structure for smaller teams.
- Cursor's individual tiers run Hobby (free), Pro \$20/mo, and Ultra \$200/mo (20x Pro usage), matching Devin's \$20 entry and \$200 power tier on headline price while keeping the engineer in the loop. ([cursor.com](https://cursor.com/blog/new-tier))
- On July 8, 2026 Cursor released Grok 4.5, trained jointly with SpaceXAI, priced at \$2 per million input tokens and \$6 per million output tokens, with a faster variant at \$4 per million input tokens and \$18 per million output tokens. It is included in individual and team plans today, with double usage for the first week, and Composer 2.5 remains available alongside it. ([cursor.com](https://cursor.com/blog/grok-4-5))
- Cursor Router is on by default for Teams plans, and enterprise admins turn it on from the dashboard with per-team controls, mode restrictions and model allow/block lists (Grok 4.5 cannot be excluded from routing). Balance and Intelligence modes bill at the routed model's rate rather than a flat price: Balance runs \$4.63 per commit and Intelligence \$6.76, versus \$7.34 for Opus 4.8 and \$12.69 for Fable 5. ([cursor.com](https://cursor.com/blog/router))

## Competitive Battlecard

### Where Cursor wins

**Reliability on ambiguous work is where we win, and Cognition says so itself.**

Their own 2025 review admits Devin 'can't independently tackle an ambiguous coding project end-to-end' and degrades when requirements change mid-task; independent testing found low real-world completion. Cursor's human-in-the-loop catches errors at every step, not only at PR time.

**Soundbite:** *"For exploratory or changing work, Devin's own performance review says it struggles, and you find out only when the PR lands. With Cursor your engineer is steering the whole way."* ([cognition.ai](https://cognition.ai/blog/devin-annual-performance-review-2025))

**We bring roughly 2x the revenue and a vastly larger installed base.**

Cursor's ~\$2B ARR is about 2x Cognition's run-rate, which has grown to approaching \$1 billion, and Cursor still sits in more than half the Fortune 500, a switching-cost moat Devin has to overcome account by account.

**Soundbite:** "More than half the Fortune 500 already build on Cursor. We're the standard your engineers already know, not the experiment in your stack." ([finance.yahoo.com](https://finance.yahoo.com/technology/ai/articles/ai-startup-cognition-funding-talks-035336592.html))

**You see the spend before the invoice does.**

Cursor's Teams pricing gives every seat two separate included-usage pools, one for first-party Composer and Auto models and one for third-party API usage, so admins can tell exactly where the money goes. A real-time dashboard splits usage by pool, and rebuilt spend alerts fire on dollar thresholds over Slack or email, so budget owners set their own ceilings and get warned well before a bill lands.

**Soundbite:** You'll know exactly what you're spending and on what, and set your own dollar alerts, before the invoice ever shows up.

**Cursor now ships its own frontier model, priced to beat lock-in.**

Grok 4.5, released July 8, 2026 and trained jointly with SpaceXAI, runs across Cursor's desktop, web, iOS, CLI and SDK, priced at \$2 per million input tokens and \$6 per million output tokens (a faster variant at \$4 input and \$18 output), and sits alongside Composer 2.5 rather than replacing it. Devin's answer, SWE-1.7, only runs inside Devin and is not sold as an API, so teams building multi-surface workflows have nowhere to take it.

**Soundbite:** "Grok 4.5 runs everywhere you build, desktop, CLI and SDK, at \$2 and \$6 per million tokens, while Devin's model only runs inside Devin." ([cursor.com](https://cursor.com/blog/grok-4-5))

**Cursor Router cuts cost per commit without cutting quality.**

Cursor's per-request classifier picks the right model for each task instead of defaulting to one daily-driver model for everything, and reports frontier-quality performance at 60% lower cost in online A/B tests across millions of live requests. Three high-volume enterprise accounts with thousands of users saved 30-50% on routed requests versus sending everything to Opus 4.8, with no drop in quality. Cost per commit lands at \$4.63 for Balance mode and \$6.76 for Intelligence mode, against \$7.34 for Opus 4.8 and \$12.69 for Fable 5.

**Soundbite:** Ask what they pay per commit today, then show Cursor Router hitting frontier quality for \$4.63 to \$6.76 against \$7.34 for Opus 4.8 and \$12.69 for Fable 5. ([cursor.com](https://cursor.com/blog/router))

### Where it's a fight

**Daily developer mindshare is ours, but the lead has stopped widening.**

JetBrains' early-2026 survey of 10,000+ developers shows Cursor used at work by 18%, now tied with Claude Code, with growth that 'has slowed down.' Devin-style async agents are still niche for most teams. We have to keep earning this ground.

**Soundbite:** *"More developers open Cursor every day than any agent. Engineers choose Cursor because it speeds up the code they're already writing and they stay in control of every change."* ([blog.jetbrains.com](https://blog.jetbrains.com/research/2026/04/which-ai-coding-tools-do-developers-actually-use-at-work/))

**"Cursor for devs, Devin for enterprise" is outdated. We compete for the same enterprise deals now.**

Some enterprises run both (Cursor for senior architectural work, Devin for a parallel maintenance fleet), so deals increasingly hinge on governance, predictability and which workflow the team actually lives in.

**Soundbite:** *"Plenty of teams run both. Your senior engineers work in the editor all day, and that's where Cursor is strongest."* ([techtimes.com](https://www.techtimes.com/articles/317354/20260529/ai-coding-agents-cognitions-26b-raise-bets-agent-first-architecture-beats-ide-tools.htm))

### Where Cognition wins

**Devin wins the 'fleet of async agents' use case.**

When the buyer wants to assign well-scoped tickets and review PRs later (migrations, vulnerability fixes, batch maintenance), Devin's sandboxed-VM, run-to-PR model is purpose-built for it, and Cognition has published ROI like Mercedes-Benz compressing an eight-month modernization to eight days. Our background agents are newer and narrower here.

**Soundbite:** *"If you want a fleet of agents knocking out scoped maintenance overnight, Devin is genuinely good at that. Let's map which of your work fits that and which still needs an engineer driving."* ([cognition.ai](https://cognition.ai/blog/series-d))

**Devin has marquee regulated and government references we can't fully match.**

Cognition names Goldman Sachs, Citi, Mercedes-Benz, Santander, the U.S. Army and U.S. Navy as customers. In defense and big-bank deals, that reference base is a real objection we have to meet head-on.

**Soundbite:** *"They've got the logos, no question. Worth asking what's in production at scale versus a pilot, and what the multi-week setup cost was to get there."* ([cognition.ai](https://cognition.ai/blog/series-d))

## Sentiment

- Sentiment: developers repeatedly call Devin's ACU (Agent Compute Unit) billing opaque and hard to budget: 'ACU are entirely too opaque/confusing/complicated' (Hacker News). A recurring friction point across independent reviews, useful when a prospect prizes predictable spend. ([news.ycombinator.com](https://news.ycombinator.com/item?id=46711589))
- Sentiment: independent reviewers report low real-world task completion (one widely-cited hands-on test had Devin succeed on just 3 of 20 tasks), reinforcing a 'doesn't finish the job' perception among developers. ([trickle.so](https://trickle.so/blog/devin-ai-review))
- Sentiment: Windsurf users cite instability and eroded trust post-acquisition: its Trustpilot page is 'mostly 1-star reviews, highlighting wasted credits, unstable performance', compounded by roadmap uncertainty under Cognition and the looming Cascade sunset. ([secondtalent.com](https://www.secondtalent.com/resources/windsurf-review/))
- Sentiment (our own side, for objection prep): Cursor's 2025 pricing changes drew heavy developer backlash over surprise overages, and a perception persists that agent-mode limits 'tighten quarterly' and real spend runs above the \$20 headline. Reps should expect this raised in deals. ([vibecoding.app](https://vibecoding.app/blog/cursor-problems-2026))

## Objection Handling

**"Devin actually ships PRs autonomously while my team sleeps. Cursor just autocompletes."**

Devin runs end-to-end to a PR, and Cognition says 89% of its own code is now committed by Devin. But Cursor runs long autonomous background and cloud agents too, and since June 29, 2026 you can launch one from the Cursor iOS app, close your laptop, and come back to review the diff and merge the PR from your phone. The real difference is oversight granularity: Cognition's own review admits Devin struggles on ambiguous, changing work, and Cursor keeps an engineer checking the work at every step, not only at PR time.

**So what:** Open the Cursor iOS app on the call and show an always-on agent running. Move the conversation from 'autonomy vs autocomplete' to 'where do you want a human checking the work,' then put both tools on their own messy tickets. ([cursor.com](https://cursor.com/blog/ios-mobile-app))

**"Cognition just raised \$1B at \$26B. They have all the momentum."**

Real, and they're well-funded, with their run-rate now approaching \$1 billion, roughly double what it was in May. But Cursor's roughly \$2B ARR is still about 2x that, we're in more than half the Fortune 500, and Cognition's revenue multiple remains a bet on catching up, not evidence they have. Capital doesn't close the product-fit gap on your team's daily work.

**So what:** Don't get pulled into a funding contest. Pivot to proven scale and to whose workflow your engineers actually prefer. ([finance.yahoo.com](https://finance.yahoo.com/technology/ai/articles/ai-startup-cognition-funding-talks-035336592.html))

**"I've seen the Cursor pricing blow-ups. How do I know our budget won't explode?"**

Fair: the 2025 rollout was botched and our CEO apologized publicly. Since then we've rebuilt Teams pricing around spend predictability. Every seat now carries separate usage pools for first-party models and third-party API calls, admins get a real-time dashboard split by pool and spend alerts fire on dollar thresholds over Slack or email. For budget certainty, sign an enterprise contract with a capped monthly pool rather than per-seat usage-based plans. Cursor Router now also works to hold spend down directly: it's on by default for Teams plans and reports frontier-quality performance at 60% lower cost in live A/B tests, with early enterprise accounts saving 30-50% on routed requests versus sending everything to Opus 4.8, with no drop in quality.

**So what:** Own the history, then walk through the pools, dashboard, dollar alerts and Cursor Router's on-by-default savings. Getting defensive is what loses this deal. ([cursor.com](https://cursor.com/blog/router))

**"SpaceX just closed its acquisition of Cursor. Who am I contracting with now, and does the product I bought change?"**

The deal closed effective August 14, 2026: Cursor now operates inside SpaceX's SpaceXAI unit. The next beat landed August 29: OpenAI is ending its partnership and cutting Cursor's direct access to OpenAI's models on November 12, 2026, roughly 5% of Cursor's traffic by Cursor's own count. That doesn't reset what you get today: Cursor still ships its own frontier model, Grok 4.5, across desktop, web, iOS, CLI and SDK, and Cursor Router still cuts cost per commit 30-50% versus one daily-driver model, backed by roughly \$2B in ARR and adoption across more than half the Fortune 500.

**So what:** Standardize on Cursor's current product now. Grok 4.5 and Cursor Router are shipping and priced today and already cover the OpenAI slice, and that scale and adoption don't reset because ownership moved to SpaceX. ([x.com](https://x.com/OpenAI/status/2093515564786540695))

**"Didn't DuneSlide leave Cursor's sandbox wide open?"**

Cato Networks disclosed two critical RCE flaws in Cursor, DuneSlide (CVE-2026-50548 and CVE-2026-50549), both rated 9.8 CVSS. A zero-click prompt injection delivered through an MCP server request or a poisoned web result could escape the terminal sandbox and reach full RCE on the developer's machine and any connected SaaS workspace, no click needed from the user. Every version before 3.0 was exposed. Both are already patched in Cursor 3.0, released April 2, 2026, and neither source reports any active exploitation.

**So what:** Move the account to Cursor 3.0 or later now. Both CVEs are closed there and there is no known active exploitation. ([catonetworks.com](https://www.catonetworks.com/blog/duneslide-two-critical-rce-vulnerabilities/))

**"Devin's new SWE-1.7 hits frontier quality for \$1.97 a task. Why pay more for Cursor?"**

SWE-1.7 is genuinely cheap and it does beat Cursor's own Composer 2.5 on Cognition's FrontierCode benchmark (42.3% to 25.6%). But it still trails Opus 4.8 and GPT-5.5 on that same benchmark and on Terminal-Bench 2.1, and it only runs inside Devin: it is not sold as an API you can build on elsewhere. Cursor Router now routes each request to the model best suited to the task, reporting frontier-quality performance at 60% lower cost in live A/B tests.

**So what:** Cursor Router puts cost per commit at \$4.63 for Balance mode and \$6.76 for Intelligence mode, versus \$7.34 for Opus 4.8. Point the buyer to Grok 4.5 too, shipped at \$2 per million input tokens and \$6 per million output tokens, available today across desktop, web, iOS, CLI and the SDK, and tell them to standardize on Cursor now instead of a model locked inside one vendor's own product. ([cursor.com](https://cursor.com/blog/router))

**"Cursor has an unpatched RCE. Why should we trust it?"**

Mindgard disclosed on July 14, 2026 that a malicious git.exe placed in a repo's project root auto-executes on Windows, no click, prompt or warning, first reported to Cursor in December 2025. Cursor quietly shipped a fix on July 13, one day before that disclosure, but as of July 17 has issued no security advisory, assigned no CVE and not said which version contains the fix, so no customer can confirm their build is patched.

**So what:** Acknowledge the flaw is now fixed, but be straight that there's no advisory, CVE or version number yet to verify against. Anchor the conversation on Cursor's Fortune 500-scale adoption and roughly \$2B ARR, the installed base a buyer is standardizing on today. ([mindgard.ai](https://mindgard.ai/blog/cursor-0day-when-full-disclosure-becomes-the-only-protection-left))

**"OpenAI just said it's cutting off Cursor's direct access to its models on November 12. Does that mean I lose GPT support?"**

OpenAI announced on August 29, 2026 that it is ending its partnership with Cursor following the SpaceX acquisition, citing doubts that SpaceX will keep the technology within OpenAI's terms of service. Under the proposal, direct access to OpenAI's models, including future ones, stops November 12, 2026. Cursor's own count puts OpenAI models at roughly 5% of Cursor's traffic. Cursor ships its own frontier model, Grok 4.5, across desktop, web, iOS, CLI and SDK, and Cursor Router already classifies every request to the best-suited model at lower cost than a single daily-driver model.

**So what:** Point to Grok 4.5 and Cursor Router as the path forward today. Offer to show the account's routing mix now, so the November 12 cutoff lands on a sliver of traffic that's already served elsewhere. ([x.com](https://x.com/OpenAI/status/2093515564786540695))

## Cut Log

This is what verification removed or corrected during fact-checking, and why.
- **CUT — Fiserv signed a partnership to deploy Devin for core banking modernization (May 28, 2026).:** Best available anchor (Fiserv investor PR / Yahoo Finance aggregation) timed out on fetch and could not be grounded with a verbatim excerpt; an adverse-to-us competitor win should rest on independently fetchable reporting, so cut rather than ground on a shaky source.
- **REVISED — AI Productivity Guarantee, anchored on the direct blog post URL.:** Every direct-post slug (cognition.ai/blog/ai-productivity-guarantee, etc.) returns 404 and is unfetchable. Re-anchored on the fetchable Cognition blog index (cognition.ai/blog/2), which renders the announcement text verbatim, so the claim grounds.
- **REVISED — Independent tests show Devin completes only ~15% of real-world tasks (stated as verified fact).:** The 3-of-20 / 14-failure figure traces to hands-on reviewer testing, not an audited benchmark; reframed as SENTIMENT in the sentiment section rather than asserted as a verified fact, per the fact/sentiment separation rule.
- **CUT — Objection: 'Anthropic ends agent subscription subsidies June 15. Cursor users hit a wall in 10 days.':** The billing change is industry-wide (all Claude subscribers running agent workloads), not Cursor-specific, and does not cleanly change how a Cursor-vs-Cognition deal is won; cut to keep the objection set focused on objections a buyer would actually pin on Cursor.
- **CUT — Cursor's own positive moves (Composer 2.5 launch, IPO filing, \$2B raise talks) as standalone Recent Strategic Moves items.:** Per battlecard routing, our own positive/neutral news is not competitor recent-moves; folded the competitively-relevant pieces (revenue scale) into the battlecard and exec summary instead, and omitted the rest as noise to the reader.
- **CUT — Cognition headcount ~286 (Feb 2026) / ~369 (Apr 2026).:** Only available from third-party aggregators (Contrary/Tracxn) with conflicting figures and no company confirmation; not decision-changing for the brief, so cut rather than present an unreconciled, weakly-sourced number.