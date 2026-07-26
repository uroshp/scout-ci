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
