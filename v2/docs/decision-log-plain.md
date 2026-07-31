# Scout — Decision Log (plain language)

A companion to `launch-decision-log.md`, in everyday language. Same decisions, no jargon, so anyone
(not just an engineer) can follow what we changed and why. Newest first. Each entry points to the
technical section for the details.

---

## 2026-06-22 — How we test whether an AI can take over the judging, and where each judge stands
*(technical: decision-log §11; vnext-roadmap §v3.5)*

Scout's whole value is that it checks itself and cuts anything it can't verify. The big question of
v3.5: can a smarter AI take over the judgment calls that plain code makes today? We answer it the safe
way — run an AI "judge" quietly in the background, have a human grade its calls, and promote it only if
it earns it. While it's being studied, nothing it says touches a live card.

There are **two** such judges, at **two different stages**:

1. **The rewrite judge** — should we reshape a sales talking-point when news breaks? This is the one a
   human grades via the approval emails (and the backlog we went through together). It has passed its
   **first checkpoint**: right on 21 of 23 calls reviewed. But "passing" means *so far*. That ~20-call
   checkpoint is one we picked to get started, **not a finished bar**. We have not yet defined the real
   exit criteria — how accurate, over how large and how varied a sample, for how long — or what
   "promote" would concretely change. Settling that is its own open question, due before it earns more
   independence.

2. **The fact-checking judge** — should a claim survive verification? This one is barely off the
   starting line. We ran it once and it flagged 56 claims as "should've been cut," but most were false
   alarms (we'd set it too suspicious, and we only saved one scrap of evidence per claim). With fair
   settings about 80% of the flags vanish, so we're re-running it fairly to get a small, clean batch
   (~a dozen) of real disagreements for a human to grade. No human has graded a single one of its calls
   yet.

Bottom line: same method, two judges. One is partway through and passing its first checkpoint; the
other is just starting. And for both, the finish line itself still needs to be defined.

---

## 2026-06-22 — Fast-moving stories now get updated, not rewritten
*(technical: decision-log §12 + the reconciliation fix)*

Scout keeps sales cheat-sheets current as news breaks. The snag: when a story unfolds in chapters (a
company gets banned, then the government softens, then walks it back), the tool would throw out the old
entry and rewrite it from scratch, losing details that were still true. And once in a while it just
choked and quietly dropped the update entirely.

The cause was almost silly: it was only reading the first sentence or two of an existing entry before
rewriting it, so it couldn't tell what to keep. We fixed it to read the whole entry and update in place
(keep what's still true, fold in the new development), and we stopped it from silently losing good
updates when a step hiccuped.

We proved it on two real stories (the government easing off Anthropic; Salesforce buying a rival to a
competitor) and pushed the refreshed cards live. Bottom line: when the market moves fast, the
salesperson sees the freshest and most complete picture, and nothing lands on a card unless a real
source backs it up.

---

## 2026-06-22 — Answering tough questions with our own real strengths
*(technical: my-company-search-and-outage-spec.md)*

When a salesperson hits a hard question ("your service went down, can we rely on you?"), a good answer
has to pivot to a real strength ("yes, and here's why you're covered"). The tool kept getting blocked:
it would write a pivot like "we run on Amazon's and Google's clouds with their own guarantees," but the
fact-checker rejected it because that strength wasn't mentioned in the specific news item that triggered
the question. So true, useful answers got thrown away.

We fixed it by handing the tool a grounded list of our own real strengths to pivot to, so the
fact-checker accepts them. We proved the fix works (an answer that used to be rejected now passes the
moment the strength is available). One catch: today it only draws on strengths already written on the
card; pulling in strengths we haven't documented yet is a follow-up.

We also made a small judgment call on outages: a routine or single-region cloud blip is noise (it
happens constantly and doesn't change a deal), so the tool now ignores those and only acts on a big,
sustained outage.

## 2026-07-25 — Word count can never kill a good claim

We found a quality claim stuck in limbo twice in one week: the fact-checker approved it, but it was a
few dozen words over the card's length limit, and the repair step only knew how to fix missing
formatting, not length. So a claim the system itself judged material sat waiting for a human to
shorten it by hand.

The fix follows the product's dividing line. Counting words is mechanical, so code does it, at the
moment the approved text exists. Shortening without losing meaning is judgment, so a model does that,
and a second independent model then compares the short version against the original to confirm every
number, date and fact survived and nothing new was invented. If that check fails, the claim is held
for a human as before, but that is now the last resort instead of the first response. The very first
live run of this check caught a real error: the shortened text had quietly dropped a true statement
about competitor pricing, and the checker refused it until the statement was restored.

## 2026-07-25 — When a product is replaced, claims about the old one leave the card

A new model release exposed a gap: the card's snapshot moved to the new flagship, but a leaderboard
block two sections down still argued from a model two generations old. Nothing was false, it had
just stopped mattering, and a rep quoting it would sound out of date.

We decided against a "stale" label. A claim either moves deals or it doesn't; a label just tells the
reader to skip it, which is the same as it not being there, minus the honesty. So now, when a
verified fact says a named product or model has been replaced, code finds every other claim on the
card still citing the old name and proposes retiring each one. The fact-checker rules on every
proposal separately, keeping any claim whose point survives the replacement. Retired claims stay in
the card's history, and for two weeks the daily monitor actively hunts for the replacement's numbers
so a fresh claim can take the old one's seat through the normal verification path. Guardrails: the
model may only name replaced identifiers that literally appear in the verified source, and claims
edited after the news broke are never swept, since they mention the old name on purpose.

## 2026-07-31 — A claim that would win a deal never gets dropped for a fixable reason

A real objection died this week on a small authoring mistake. When Anthropic disclosed its own
containment incident (making OpenAI's earlier one a wash), the tool correctly retired the "their
model broke out" attack and tried to turn it into an objection buyers would now raise about Claude
too. But the draft leaned on a bad answer — "just run it on Microsoft's cloud" — which doesn't
actually address a model misbehaving. The fact-checker rightly refused it, but by the time it spelled
out the honest answer (acknowledge it was a lab test, point to how fast Anthropic caught and
disclosed it), the one rewrite it was allowed had already been spent chasing the wrong fix. A
deal-relevant objection ended up addressed nowhere.

The fix changes the fact-checker's first question. Instead of "can the wording be patched?", it now
asks "would this point actually move a deal or a customer conversation?" If no, it's dropped, as
before. If yes, it can never be silently dropped — it must be cured. Sometimes that's just better
wording; sometimes the whole approach is wrong and needs re-thinking while keeping the valuable
point. The checker also has to spell out the correct approach the first time, not discover it two
rounds later. The tool now gets up to three tries instead of one, and if a genuinely deal-moving
point still can't be written honestly, it sends an urgent email so a person can write it by hand.
Nothing that would help close a deal disappears quietly again.
