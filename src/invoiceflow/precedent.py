"""Precedent: what human reviewers have already settled, and what it is worth.

Some of what this pipeline escalates is a question about *this* invoice and
always will be. Some of it is a question about a vendor's habits — whether they
really do bill in EUR, whether their totals drift by pennies because they round
each line — and a habit is exactly the kind of thing a handful of consistent
human answers settles for good. `human_reviews` has been recording those answers
since the dashboard shipped; this module is what reads them back.

Two quantities, both deterministic, both explainable term by term:

  burden   what has to be proven before a finding can be settled without a
           person, scaled by what is actually at risk — which is not always the
           invoice total. For an arithmetic drift the money at risk is the gap.
  support  what history supplies: one entry per prior invoice where a person
           answered this same question, weighted by how comparable and how
           recent it was.

Deliberately *not* a probability. Nothing here is calibrated against outcomes,
and printing "87% confident" over an ordinal evidence budget would be a claim
the system cannot support. The day it is fitted rather than hand-set, the terms
persisted in `precedent_citations.terms` are the feature vector and
`human_reviews` is the label (docs/beyond-the-brief.md §19).

The scoring is here; the *policy* — which findings may be settled this way, and
the release decision itself — is in `rules.py`, beside the allowlist it is
decided against.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

from .config import Settings
from .models import (
    FinalStatus,
    Invoice,
    IssueCode,
    Precedent,
    PrecedentBundle,
    PrecedentCase,
    Severity,
    ValidationIssue,
    ValidationReport,
)
from .prompts import Tag
from .rules import PRECEDENT_RELEASABLE, precedent_releases
from .validation import line_total_gap, subtotal_gap, total_gap

if TYPE_CHECKING:
    from .runstore import RunStore

#: Full exposure for an arithmetic finding. Fifty dollars is the point at which
#: a discrepancy stops reading as a rounding artifact and starts reading as a
#: disagreement about the bill — below it the term is small and one confirming
#: human answer can carry a release; at or above it, nothing realistically can.
ARITHMETIC_CEILING = 50.0

#: Half a cent. Below this there is no money in question at all, which matters
#: for comparability: two invoices that both put nothing at risk are comparable,
#: and one that puts nothing at risk says nothing about one that does.
NEGLIGIBLE = 0.005


def _worst_line_gap(invoice: Invoice) -> float:
    return max((line_total_gap(li) for li in invoice.line_items), default=0.0)


def _stated_total(invoice: Invoice) -> float:
    return abs(invoice.total or 0.0)


@dataclass(frozen=True)
class RiskModel:
    """What a finding costs to be wrong about, and what that cost is measured on.

    The `at_risk` function is the interesting half. A currency finding puts the
    whole sum in question; an arithmetic finding puts only the gap in question,
    and pricing it off the invoice total instead would make a two-cent drift on
    a large invoice look expensive and a four-hundred-dollar drift on the same
    invoice look identical. Comparability between cases is measured on this same
    quantity, for the same reason.
    """

    base: float
    coefficient: float
    at_risk: Callable[[Invoice], float]
    #: What full exposure means. None -> the settings' scrutiny threshold, i.e.
    #: the number the business has already set as "a person looks at this".
    ceiling: float | None
    #: Prose for the citation and the dashboard: what this finding puts at stake.
    stake: str


RISK: dict[IssueCode, RiskModel] = {
    IssueCode.UNEXPECTED_CURRENCY: RiskModel(
        # The heaviest base of the releasable set: every other finding here
        # leaves the sum owed known and something about it odd. This one leaves
        # the sum itself unknown, because we hold no rate to convert it at.
        base=2.0,
        coefficient=2.0,
        at_risk=_stated_total,
        ceiling=None,
        stake="the whole sum, stated in a currency the company cannot convert",
    ),
    IssueCode.TOTAL_MISMATCH: RiskModel(
        base=0.5,
        coefficient=2.0,
        at_risk=total_gap,
        ceiling=ARITHMETIC_CEILING,
        stake="the gap between the stated grand total and its parts",
    ),
    IssueCode.SUBTOTAL_MISMATCH: RiskModel(
        base=0.5,
        coefficient=2.0,
        at_risk=subtotal_gap,
        ceiling=ARITHMETIC_CEILING,
        stake="the gap between the stated subtotal and the line values",
    ),
    IssueCode.LINE_TOTAL_MISMATCH: RiskModel(
        base=0.5,
        coefficient=2.0,
        at_risk=_worst_line_gap,
        ceiling=ARITHMETIC_CEILING,
        stake="the widest gap on any one line",
    ),
    IssueCode.SUSPICIOUS_DUE_DATE: RiskModel(
        # Half the coefficient of a money finding: a dating quirk risks paying
        # at the wrong time, not paying the wrong amount.
        base=0.5,
        coefficient=1.0,
        at_risk=_stated_total,
        ceiling=None,
        stake="when the invoice falls due, not what is owed",
    ),
    IssueCode.MISSING_DUE_DATE: RiskModel(
        base=0.5,
        coefficient=1.0,
        at_risk=_stated_total,
        ceiling=None,
        stake="when the invoice falls due, not what is owed",
    ),
}

#: Every releasable finding must be priceable, and nothing else may be. Asserted
#: at import so the two tables cannot drift into a KeyError on a real invoice.
assert set(RISK) == set(PRECEDENT_RELEASABLE), (
    "rules.PRECEDENT_RELEASABLE and precedent.RISK must name the same findings"
)

_PUNCT = re.compile(r"[^a-z0-9]+")


def vendor_key(vendor: str) -> str:
    """The vendor, reduced to something two documents can be compared on.

    "Fabrikam GmbH", "FABRIKAM  GmbH." and "fabrikam gmbh" are one company and
    must accumulate one history. The single definition, in Python rather than in
    SQL, so a normalisation that decides who gets paid automatically cannot be
    implemented twice and drift.
    """
    return _PUNCT.sub(" ", vendor.strip().lower()).strip()


def _comparable(a: float, b: float) -> bool:
    """Within 2x of each other, on whatever the finding puts at risk.

    Approving a two-cent drift does not establish approving a four-hundred-dollar
    one, and the gap between those two is exactly the kind of thing a raw count
    of prior approvals cannot see.
    """
    lo, hi = sorted((abs(a), abs(b)))
    if hi <= NEGLIGIBLE:
        return True  # neither puts money in question
    if lo <= NEGLIGIBLE:
        return False  # one does and one does not
    return hi / lo <= 2.0


def _days_since(timestamp: str) -> float | None:
    """Age of an ISO timestamp in days, or None when it cannot be read.

    Unreadable is treated as stale by the caller, never as fresh: a weight that
    fails open is a weight that pays invoices on a parse error.
    """
    try:
        when = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds() / 86_400


def lookup_precedents(
    store: RunStore,
    invoice: Invoice,
    report: ValidationReport,
    settings: Settings,
) -> PrecedentBundle:
    """Everything history has to say about this invoice's open questions.

    Only findings on the releasable allowlist are looked up. That keeps the work
    proportional — a clean invoice, or one carrying only critical findings, does
    not touch the database at all — and it keeps the Approver's tool honest: it
    is offered exactly where an answer could change something, and the outcome
    is already forced everywhere else.
    """
    if not settings.precedent_enabled:
        return PrecedentBundle()

    relevant = [i for i in report.issues if i.code in RISK]
    if not relevant:
        return PrecedentBundle()

    blocked_by = _bar(invoice, report, settings)
    vendor = vendor_key(invoice.vendor)
    # One registry read for the whole bundle rather than one per finding.
    familiar = vendor in {vendor_key(v) for v in store.paid_vendors()}

    # One entry per *question*, not per issue row. Two drifting lines are two
    # findings and one question — "does this vendor round each line?" — and the
    # at-risk figure already aggregates across them. It is also what keeps
    # `precedent_citations`' UNIQUE (run_id, code, subject) satisfiable: without
    # this, an invoice with two bad lines aborts its own persistence.
    questions: dict[tuple[IssueCode, str], ValidationIssue] = {}
    for issue in relevant:
        questions.setdefault((issue.code, issue.subject), issue)

    # Every *other* open question on the invoice, released or not. Counted by
    # question rather than by issue row, so one habit tripping three lines is
    # not charged three times — and counted regardless of its own outcome,
    # which is both conservative and the way out of a circularity: a release
    # that depended on other releases would have to be solved for.
    open_questions = {(i.code, i.subject) for i in report.issues if i.severity == Severity.WARNING}

    findings: list[Precedent] = []
    for key, issue in questions.items():
        model = RISK[issue.code]
        at_risk = model.at_risk(invoice)
        others = len(open_questions - {key})
        burden, terms = _burden(model, at_risk, settings, others=others, familiar=familiar)
        cases, rejections = _cases(store, issue.code, issue.subject, vendor, at_risk, settings)
        support = round(sum(c.weight for c in cases), 4) if not rejections else 0.0
        found = Precedent(
            code=issue.code,
            subject=issue.subject,
            vendor=invoice.vendor,
            detail=issue.detail,
            at_risk=round(at_risk, 4),
            burden=round(burden, 4),
            support=support,
            cases=cases,
            rejections=rejections,
            blocked_by=blocked_by,
            terms=terms | {"support": support},
        )
        found.released = precedent_releases(found)
        findings.append(found)
    return PrecedentBundle(findings=findings)


def _bar(invoice: Invoice, report: ValidationReport, settings: Settings) -> str:
    """Why nothing on this run may be released, whatever the arithmetic says.

    These are ceilings, not terms: no amount of accumulated support gets past
    one, which is the difference between a policy and a preference.
    """
    if invoice.total is None:
        return "no total could be established, so there is no sum precedent could vouch for"
    if invoice.total > settings.scrutiny_threshold:
        # For a foreign-currency invoice this compares the stated number against
        # a dollar threshold, which is not a conversion and is not pretended to
        # be one. It errs toward a person, which is the safe direction to err in.
        return (
            f"the invoice total {invoice.total:,.2f} is above the "
            f"{settings.scrutiny_threshold:,.0f} scrutiny threshold, which no weight of "
            "precedent reaches past"
        )
    if any(i.code == IssueCode.PROMPT_INJECTION_ATTEMPT for i in report.issues):
        return (
            "the source document forged this pipeline's prompt fences, and precedent must "
            "not be reachable by a document that does that"
        )
    return ""


def _burden(
    model: RiskModel,
    at_risk: float,
    settings: Settings,
    *,
    others: int,
    familiar: bool,
) -> tuple[float, dict[str, float]]:
    ceiling = model.ceiling if model.ceiling is not None else settings.scrutiny_threshold
    exposure = model.coefficient * min(abs(at_risk) / ceiling, 1.0) if ceiling > 0 else 0.0
    # Precedent answers one question. Every other warning still open on the
    # invoice is a question it says nothing about, and a pile of them is a
    # reason to want a person even where the one question is settled.
    company = 0.5 * max(others, 0)
    # A brand-new vendor with a brand-new quirk is the fraud shape, not the
    # supplier shape. Once the company has actually paid them once, it is not.
    stranger = 0.0 if familiar else 1.0
    terms = {
        "base": model.base,
        "at_risk": round(abs(at_risk), 4),
        "exposure": round(exposure, 4),
        "other_warnings": round(company, 4),
        "unfamiliar_vendor": stranger,
    }
    burden = model.base + exposure + company + stranger
    return burden, terms | {"burden": round(burden, 4)}


def _cases(
    store: RunStore,
    code: IssueCode,
    subject: str,
    vendor: str,
    at_risk: float,
    settings: Settings,
) -> tuple[list[PrecedentCase], int]:
    """Prior human decisions on this exact question, weighted. Also the count of
    human *rejections*, which zeroes the support wherever it is not zero."""
    cases: list[PrecedentCase] = []
    rejections = 0
    for row in store.precedent_rows(code.value, subject):
        if vendor_key(row["vendor"]) != vendor:
            continue  # another company's habits are not this one's
        if row["to_status"] == FinalStatus.REJECTED:
            rejections += 1
            continue
        if row["to_status"] != FinalStatus.PAID:
            continue
        prior = store.invoice_for_run(row["run_id"])
        if prior is None:
            continue  # a run whose invoice did not survive; not evidence
        prior_risk = RISK[code].at_risk(prior)
        weight = 1.0
        if not _comparable(prior_risk, at_risk):
            weight *= 0.6
        age = _days_since(row["reviewed_at"])
        if age is None or age > settings.precedent_max_age_days:
            # Habits go stale — vendors change billing systems, and a rate
            # somebody accepted two years ago is not this year's rate.
            weight *= 0.7
        cases.append(
            PrecedentCase(
                run_id=row["run_id"],
                invoice_number=row["invoice_number"],
                total=row["total"],
                currency=row["currency"],
                at_risk=round(prior_risk, 4),
                reviewed_at=row["reviewed_at"],
                action=row["action"],
                note=row["note"],
                weight=round(weight, 4),
            )
        )
    return cases, rejections


# ---------------------------------------------------------------------------
# What the Approver reads
# ---------------------------------------------------------------------------

TOOL_NAME = "find_similar_invoices"

TOOL_DESCRIPTION = """\
Look up how people have decided invoices raising the same open findings as this
one, from the same vendor. Returns each prior case with the amount, the date,
and the reviewer's own note. Takes no arguments — it already knows the invoice.

Use it when the evidence in front of you leaves the decision genuinely open. A
run of prior approvals is evidence that a finding is this vendor's habit rather
than a problem; a single one is worth knowing about but is not on its own a
reason to approve.\
"""


def precedent_block(bundle: PrecedentBundle) -> str:
    """The fenced block every prompt and tool result hands the model.

    Reviewer notes are free text written by people and, before that, invoice
    details written by vendors. `Tag.wrap` escapes any forged fence inside them,
    which is why the same function builds the block on both routes into the
    model rather than each caller formatting its own.
    """
    lines: list[str] = []
    for found in bundle.findings:
        lines.append(found.summary_line())
        for case in found.cases:
            amount = f"{case.currency} {case.total:,.2f}" if case.total is not None else "—"
            line = (
                f"  · {case.invoice_number} ({amount}) — a person set this to paid on "
                f"{case.reviewed_at[:10]} ({case.action.replace('_', ' ')}), "
                f"counted at {case.weight:.2f}"
            )
            if case.note:
                line += f'; their note: "{case.note}"'
            lines.append(line)
        if found.rejections:
            lines.append(
                f"  · and {found.rejections} invoice(s) on this same question that a person "
                "REJECTED — which is why nothing here is settled"
            )
    if not lines:
        lines.append("No comparable prior decision exists for any finding on this invoice.")
    return Tag.PRECEDENT.wrap("\n".join(lines))


def build_precedent_tool(bundle: PrecedentBundle) -> BaseTool:
    """`find_similar_invoices`, closed over what was already looked up.

    No arguments, like every other tool in this pipeline: the model cannot
    hallucinate a vendor or a code to search under, and what it gets back is the
    same evidence the rule engine acted on rather than a second query that might
    answer differently.
    """

    @tool(TOOL_NAME, description=TOOL_DESCRIPTION)
    def _run() -> str:
        return precedent_block(bundle)

    return _run
