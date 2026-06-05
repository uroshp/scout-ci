"""v2's prompt building blocks. **Independent copy**, not shared with v1.

These were lifted from research.py as a starting point, but v1 is frozen and keeps
its own inline copies. v2 owns and evolves these freely; nothing in research.py or
app.py imports from scout/, so a change here can never break the shipped v1 app.
The duplication is deliberate: there is nothing to drift from because v1 won't change.
"""
import os

from scout import config
from scout import judgment

SOURCE_HIERARCHY = judgment.get("prompts.SOURCE_HIERARCHY")

WRITING_STYLE = judgment.get("prompts.WRITING_STYLE")

FORMATTING_RULES = judgment.get("prompts.FORMATTING_RULES")


def load_methodology():
    """The CI discipline the model is held to. Kept as an editable plain-English
    spec out of code on purpose (README 'Design decisions')."""
    return judgment.get("prompts.METHODOLOGY")       # the private judgment pack (scout/judgment.py)
