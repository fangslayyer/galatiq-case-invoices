"""Prompt construction: the fenced blocks every agent prompt is assembled from.

Structured context is embedded in tagged blocks so each prompt has clearly
delimited sections — it separates instructions from vendor-supplied text, and
names the payloads the system prompts refer to (`rule_constraints.must_reject`
is a real `<rule_constraints>` block). The tags are a contract between the
agents that write them and the test fakes that read them back, so they are an
enum rather than loose strings: a typo on either side is a type error, not a
silently empty block.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Tag(StrEnum):
    """A fence label, and the only way to build or read the block it labels.

    StrEnum specifically: members interpolate as their value in f-strings and
    regexes, so `f"<{self}>"` yields `<invoice_document>`. A plain `str, Enum`
    mixin would emit `<Tag.DOC>` into live prompts.
    """

    DOC = "invoice_document"
    ERRORS = "extraction_errors"
    INVOICE = "invoice_json"
    ISSUES = "validation_issues"
    REPORT = "validation_report"
    CONSTRAINTS = "rule_constraints"
    DECISION = "proposed_decision"
    FEEDBACK = "critic_feedback"

    def wrap(self, content: str) -> str:
        """Fence `content` under this tag."""
        return f"<{self}>\n{content}\n</{self}>"

    def unwrap(self, text: str) -> str | None:
        """Recover the content this tag fences, or None if it is absent.

        Named `unwrap`, not `find`: StrEnum members *are* `str`, so a method
        called `find` would shadow `str.find` with an incompatible signature.

        The newlines are part of the pattern because `wrap` emits them: the two
        are a matched pair and must be edited together.
        """
        m = re.search(rf"<{self}>\n(.*?)\n</{self}>", text, re.DOTALL)
        return m.group(1) if m else None
