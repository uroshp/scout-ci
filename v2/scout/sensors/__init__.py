"""Sensors (Release 2, 2026-10-04): code reads every source about each company every morning; a
model reads all of it instead of searching for some of it.

An ENTITY is a company named on a card (its display name, slugified). Its REGISTRY (private store,
`sensors/<entity>/registry.json`) lists the pages and feeds code watches and the news queries it
runs. The daily PASS (collect.py) fetches them, diffs pages at element level, pulls the feeds and
two keyless news indexes, and writes everything new as durable FINDINGS (events.py: one event per
finding, consumed per card like a signal). The SCREEN (screen.py, one direct Haiku call per card,
no tools) turns a card's open findings into candidates in exactly the shape triage emits, so the
escalation floor, materiality, grounding, propagation, the judge and audience leads run unchanged.
COMPARE (compare.py) is the shadow's proof: for every alert that lands through today's triage, was
a finding behind it? Cutover is a repo variable (SCOUT_SENSORS = off | shadow | gate).

Only screen.py calls a model. Nothing here imports grounding's model-free fetcher into a module
that also calls a model (tests/test_sources.py keeps that line).
"""
