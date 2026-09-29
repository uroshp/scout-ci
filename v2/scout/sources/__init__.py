"""Structured sources for Scout (2026-09-28, the Scout-as-agent plan, WS0/WS1).

`classify` is the deterministic, model-free source-class map: given a URL (and the card's company
names) it says what KIND of source this is (a filing, the company's own statement, a job board, a
news outlet, a review site, a forum ...). The class is stamped on every claim at write time and
shown to the reader as a chip beside the citation; where the class is known, it also normalizes
the model-asserted `source_tier` so tier is code-owned wherever code can know it. Everything here
imports only the standard library so it can run in the viewer image, the engine and the tests.
"""
