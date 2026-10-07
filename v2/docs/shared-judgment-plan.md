# One judgment per development (plan, 2026-10-07, not built)

Uroš, 2026-10-07: "find everything that's material and handle it, not limiting it artificially,
but with economies of scale in mind." This is the scale design: the expensive work per development
is done once per company and shared by every card that names it.

## Today

Every card pays its own full chain every morning: triage ($0.20 to $0.30), screen ($0.02),
materiality with web search ($0.30 to $0.90 per judged set), own-company facts, propagation.
Three live cards watch OpenAI and two watch Anthropic, so the same story is found, judged, searched
and grounded more than once on the same morning.

Measured on the last three mornings (lifecycle audits, 7 to 8 cards): of the 7, 8 and 3 substantial
triage candidates a day, 1, 1 and 0 were the same development judged on two cards. At seven cards
the duplicated spend is cents. The mechanism matters when cards grow and companies repeat: at 30
cards over 15 companies the chain would run about twice per story, at 100 cards over 40 companies
about two and a half times.

## The split

Materiality today does two jobs in one call: establish the fact (search, fetch, excerpt, source)
and decide what it means for this card. The first is card-independent. The second is not. So:

1. **Facts per company, once a day.** A development found for a company (by the sensors or by any
   card's triage) is judged, searched and grounded once, keyed by company and a development
   fingerprint (source URL or normalized title). The result is a grounded fact with its excerpt
   and tier, stored under the company, valid for the day. Every card that names the company reads
   it. The sensors' findings and events are already per company; this extends that to the judge.
2. **Relevance per card, cheap.** Each card then asks one tools-off question: is this grounded fact
   material for this card's focus and tracked subjects, and what is the alert? No web search,
   no fetch, a few thousand tokens, about $0.03 on the live judge model. This is a classification
   over an established fact, the exact shape the on-device lane shows local models can match
   (the screen agrees 8 of 8), so it is also the first judgment step a local model could take.
3. **Propagation stays per card.** Which claims to revise and how to word them is the card's.

The own-company arm follows the same split: OpenAI as "our company" on the collaboration card and
as the competitor on the Mistral card is one grounded fact with two card-level readings.

## What it saves

Per story per extra card: one materiality search-and-ground call (about $0.30 to $0.90) becomes one
tools-off relevance call (about $0.03). The watching cost (triage or screens) is unchanged by this
plan; the sensor cutover handles that line. At today's seven cards the saving is under $0.50 a day.
At 30 cards over 15 companies it is roughly a third of the materiality line, at 100 cards over 40
companies about 60% of it, and the judge's spend stops growing with the number of cards on a company.

## What it must not change

The fact is still grounded by code against its source before any card uses it. The per-card
relevance call is still a judgment the judge makes, captured for the eval lanes under its own role.
A fact found by one card's triage and judged immaterial for that card is still offered to every
other card that names the company: shared establishment never means shared dismissal.

## Order of work (about a day, when there is a reason)

1. Measure first, free: count cross-card duplicate judgments in the lifecycle audits for two weeks
   (the number above, automated into the checkpoint email).
2. Build the per-company fact store and the fingerprint, feed it from materiality's existing output
   (no prompt change).
3. Add the relevance role (new private block, captured, in ROLE_SPECS from day one) and route cards
   through it when a fact for their company already exists today.
4. Shadow for a week: every card still runs today's chain; the shared chain's verdicts are compared
   per card; the lifecycle audit's continuity rules cover the new path.

Nothing here is scheduled. It is the plan to reach for when the card count makes it worth a day.
