"""Element-level page diff (model-free).

Grounding's text has no boundaries (every element joined with a space, whitespace collapsed), so
fixed-width chunks would shift whenever anything upstream changed. Here each HTML element that
carries text becomes one BLOCK (a table row is one block), blocks shorter than MIN_CHARS are
ignored, and the page's state is the set of block hashes plus, for blocks that carry numbers, a
SHAPE (the text with its digits replaced) so a changed figure inside an otherwise identical block
is reported as a `value_change` with old and new, never dropped. A page where more than
REDESIGN_RATIO of the blocks changed is a `redesign`: logged as a finding and re-baselined.

Noise filters (the 9/29 objection "nonces and dates flip page digests"): a block that is only a
date, a time, a counter or a number is not a finding; a block that is mostly digits and symbols is
not a finding; the first fetch of a page only sets the baseline.
"""
from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

MIN_CHARS = 40
MAX_BLOCKS = 400
MAX_BLOCK_CHARS = 600
REDESIGN_RATIO = 0.60

_BLOCK_TAGS = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "dd", "dt", "blockquote", "pre",
               "figcaption", "summary", "td", "th", "article", "section", "div", "span", "a", "time", "label")
_LEAF_FIRST = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "dd", "dt", "blockquote", "pre",
               "figcaption", "summary", "time", "label")
_DROP = ("script", "style", "noscript", "template", "svg", "nav", "footer", "header", "form", "iframe", "button")
_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d")
# a block that is only a stamp: "Updated October 3, 2026", "Last updated: 2026-10-03", "12:40 PM PT",
# "© 2026 Acme", "1,204 views". Letters other than the stamp words give it away as prose.
_STAMP_WORDS = r"(?:updated|last updated|published|posted|as of|effective|modified|copyright|©|all rights reserved|views|likes|comments|replies|stars|forks|am|pm|utc|pt|et|est|pst|gmt)"
_DATE_ONLY = re.compile(
    r"^(?:" + _STAMP_WORDS + r"|[A-Za-z]{3,9}\.?|\d[\d,./:-]*|\s|,|:|\.|-)+$", re.I)


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace(" ", " ")).strip()


def blocks_from_html(html: str) -> list[str]:
    """One text block per text-bearing element, in document order, deduped, capped."""
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(list(_DROP)):
        t.decompose()
    out, seen = [], set()

    def add(text: str) -> None:
        text = _norm(text)
        if len(text) < MIN_CHARS or len(out) >= MAX_BLOCKS:
            return
        text = text[:MAX_BLOCK_CHARS]
        if text in seen:
            return
        seen.add(text)
        out.append(text)

    # leaf-first: a <p>, <li>, heading or table row is a block; its text must not also count again
    # for every ancestor <div>/<section>, so ancestors contribute only their own direct text.
    for el in soup.find_all(_LEAF_FIRST):
        if el.name == "tr":
            cells = [_norm(c.get_text(" ")) for c in el.find_all(["td", "th"])]
            add(" | ".join(c for c in cells if c))
        else:
            if el.find(_LEAF_FIRST):           # a container of blocks, not a block
                continue
            add(el.get_text(" "))
    for el in soup.find_all(("div", "section", "article", "main", "aside")):
        if el.find_parent(_LEAF_FIRST):
            continue
        own = " ".join(str(c) for c in el.contents if getattr(c, "name", None) is None)
        add(own)
    return out


def is_noise(block: str) -> bool:
    b = _norm(block)
    if not b:
        return True
    if _DATE_ONLY.match(b):
        return True
    digits_and_symbols = sum(1 for ch in b if not ch.isalpha() and not ch.isspace())
    letters = sum(1 for ch in b if ch.isalpha())
    return letters < 12 or digits_and_symbols > letters


def shape(block: str) -> str:
    """The block with its digits replaced: two versions of a price row share a shape."""
    return _DIGITS.sub("#", _norm(block).lower())


def h(text: str) -> str:
    return hashlib.sha256(_norm(text).lower().encode()).hexdigest()[:16]


def diff(prev: dict | None, blocks: list[str]) -> dict:
    """Compare today's blocks with a page's stored state.

    prev: {"hashes": [h], "shapes": {shape_hash: text}} or None (first fetch).
    Returns {"first": bool, "new": [text], "value_changes": [{"old", "new"}], "ratio": float,
             "redesign": bool, "state": {"hashes", "shapes"}}; `new` excludes value changes and noise.
    """
    hashes = [h(b) for b in blocks]
    shapes = {h(shape(b)): b for b in blocks if _DIGITS.search(b)}
    state = {"hashes": hashes, "shapes": shapes}
    if not prev or not prev.get("hashes"):
        return {"first": True, "new": [], "value_changes": [], "ratio": 0.0, "redesign": False, "state": state}
    old_hashes = set(prev.get("hashes") or [])
    old_shapes = prev.get("shapes") or {}
    changed = [b for b, hh in zip(blocks, hashes) if hh not in old_hashes]
    ratio = (len(changed) / len(blocks)) if blocks else 0.0
    value_changes, new = [], []
    for b in changed:
        if is_noise(b):
            continue
        sk = h(shape(b))
        old = old_shapes.get(sk)
        if old is not None and _norm(old) != _norm(b):
            value_changes.append({"old": old, "new": b})
        else:
            new.append(b)
    redesign = bool(blocks) and ratio > REDESIGN_RATIO
    if redesign:
        return {"first": False, "new": [], "value_changes": [], "ratio": round(ratio, 3), "redesign": True, "state": state}
    return {"first": False, "new": new, "value_changes": value_changes, "ratio": round(ratio, 3),
            "redesign": False, "state": state}
