"""Prompt construction helpers.

Structured context is embedded in tagged blocks so every agent prompt has
clearly-delimited, machine-checkable sections (the test fakes also key off
them to answer deterministically).
"""

from __future__ import annotations

import re

DOC_TAG = "invoice_document"
ERRORS_TAG = "extraction_errors"
INVOICE_TAG = "invoice_json"
ISSUES_TAG = "validation_issues"
REPORT_TAG = "validation_report"
CONSTRAINTS_TAG = "rule_constraints"
DECISION_TAG = "proposed_decision"
FEEDBACK_TAG = "critic_feedback"


def block(tag: str, content: str) -> str:
    return f"<{tag}>\n{content}\n</{tag}>"


def extract_block(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", text, re.DOTALL)
    return m.group(1) if m else None
