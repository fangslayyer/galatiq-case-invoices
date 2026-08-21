"""End-to-end acceptance tests: every provided fixture through the full
LangGraph pipeline with the deterministic stub brain.

The expected statuses are the case's acceptance table. `fresh` expectations
assume an empty processed-invoice registry.
"""

import pytest

from invoiceflow.graph import build_graph
from invoiceflow.llm import StubChatModel
from invoiceflow.models import (
    ApprovalDecision,
    ApprovalStatus,
    Critique,
    CritiqueVerdict,
    FinalStatus,
    Invoice,
    IssueCode,
)
from tests.conftest import INVOICES_DIR

ACCEPTANCE = [
    # (file, expected status on a fresh registry, expected issue codes subset)
    ("invoice_1001.txt", FinalStatus.PAID, set()),
    ("invoice_1002.txt", FinalStatus.REJECTED, {IssueCode.STOCK_EXCEEDED}),
    ("invoice_1003.txt", FinalStatus.REJECTED, {IssueCode.OUT_OF_STOCK}),
    ("invoice_1004.json", FinalStatus.PAID, set()),
    ("invoice_1004_revised.json", FinalStatus.PAID, set()),  # fresh registry: no prior INV-1004
    ("invoice_1005.json", FinalStatus.REJECTED, {IssueCode.STOCK_EXCEEDED}),
    ("invoice_1006.csv", FinalStatus.PAID, set()),
    (
        "invoice_1007.csv",
        FinalStatus.REJECTED,
        {IssueCode.STOCK_EXCEEDED, IssueCode.TOTAL_MISMATCH},
    ),
    ("invoice_1008.txt", FinalStatus.NEEDS_REVIEW, {IssueCode.UNKNOWN_ITEM}),
    (
        "invoice_1009.json",
        FinalStatus.REJECTED,
        {IssueCode.NEGATIVE_QUANTITY, IssueCode.MISSING_VENDOR},
    ),
    ("invoice_1010.txt", FinalStatus.PAID, set()),
    ("invoice_1011.txt", FinalStatus.PAID, set()),
    ("invoice_1011.pdf", FinalStatus.PAID, set()),
    ("invoice_1012.txt", FinalStatus.PAID, set()),
    ("invoice_1012.pdf", FinalStatus.PAID, set()),
    ("invoice_1013.json", FinalStatus.REJECTED, {IssueCode.STOCK_EXCEEDED}),
    ("invoice_1013.pdf", FinalStatus.REJECTED, {IssueCode.STOCK_EXCEEDED}),
    ("invoice_1014.xml", FinalStatus.NEEDS_REVIEW, {IssueCode.UNEXPECTED_CURRENCY}),
    ("invoice_1015.csv", FinalStatus.PAID, set()),
    ("invoice_1016.json", FinalStatus.NEEDS_REVIEW, {IssueCode.UNKNOWN_ITEM}),
]


@pytest.mark.parametrize(("filename", "expected", "expected_codes"), ACCEPTANCE)
def test_acceptance(pipeline, filename, expected, expected_codes):
    result = pipeline.run(INVOICES_DIR / filename, persist=False)
    assert result.final_status == expected, result.decision and result.decision.reasoning
    found = {i.code for i in result.validation.issues}
    assert expected_codes <= found
    if expected == FinalStatus.PAID:
        assert result.payment is not None and result.payment.status == "success"
    else:
        assert result.payment is None


class TestRegistryOrdering:
    def test_revised_invoice_flagged_after_original_paid(self, pipeline):
        first = pipeline.run(INVOICES_DIR / "invoice_1004.json", persist=False)
        assert first.final_status == FinalStatus.PAID
        second = pipeline.run(INVOICES_DIR / "invoice_1004_revised.json", persist=False)
        assert second.final_status == FinalStatus.NEEDS_REVIEW
        assert IssueCode.REVISED_INVOICE in {i.code for i in second.validation.issues}
        # the paid record must survive the escalated revision
        assert pipeline.db.get_processed("INV-1004").final_status == "paid"

    def test_exact_duplicate_never_paid_twice(self, pipeline):
        pipeline.run(INVOICES_DIR / "invoice_1001.txt", persist=False)
        rerun = pipeline.run(INVOICES_DIR / "invoice_1001.txt", persist=False)
        assert rerun.final_status == FinalStatus.DUPLICATE
        assert rerun.payment is None

    def test_pdf_of_paid_invoice_is_cross_format_duplicate(self, pipeline):
        pipeline.run(INVOICES_DIR / "invoice_1011.txt", persist=False)
        pdf = pipeline.run(INVOICES_DIR / "invoice_1011.pdf", persist=False)
        assert pdf.final_status == FinalStatus.DUPLICATE


class TestResultPersistence:
    def test_run_writes_result_json(self, pipeline, settings):
        result = pipeline.run(INVOICES_DIR / "invoice_1001.txt")
        files = list(settings.results_dir.glob("*.json"))
        assert len(files) == 1
        assert result.run_id in files[0].name

    def test_unreadable_file_fails_gracefully(self, pipeline, tmp_path):
        missing = tmp_path / "nope.txt"
        result = pipeline.run(missing, persist=False)
        assert result.final_status == FinalStatus.FAILED
        assert result.error


class RogueApprover(StubChatModel):
    """A brain that approves everything and waves its own decision through —
    used to prove the graph's hard-rule guard is independent of the agents."""

    def with_structured_output(self, schema, **kwargs):
        from langchain_core.runnables import RunnableLambda

        if schema is ApprovalDecision:
            return RunnableLambda(
                lambda _: ApprovalDecision(status=ApprovalStatus.APPROVED, reasoning="ship it")
            )
        if schema is Critique:
            return RunnableLambda(
                lambda _: Critique(verdict=CritiqueVerdict.ACCEPT, feedback="lgtm")
            )
        return super().with_structured_output(schema, **kwargs)


def test_hard_rules_outrank_a_rogue_approver(settings, db):
    graph = build_graph(settings, db, RogueApprover())
    state = graph.invoke(
        {
            "source_file": str(INVOICES_DIR / "invoice_1003.txt"),  # fraud: zero-stock item
            "run_id": "rogue-test",
            "started_at": "",
            "trace": [],
            "critique_rounds": [],
        }
    )
    assert state["decision"].status == ApprovalStatus.REJECTED
    assert state["final_status"] == FinalStatus.REJECTED
    assert "payment" not in state


class AmnesiacExtractor(StubChatModel):
    """Fails extraction once, then succeeds — exercises the self-correction loop."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._calls = {"n": 0}

    def with_structured_output(self, schema, **kwargs):
        from langchain_core.runnables import RunnableLambda

        if schema is Invoice:
            inner = super().with_structured_output(schema, **kwargs)

            def flaky(prompt):
                self._calls["n"] += 1
                if self._calls["n"] == 1:
                    return Invoice(invoice_number="")  # fails the sanity check
                return inner.invoke(prompt)

            return RunnableLambda(flaky)
        return super().with_structured_output(schema, **kwargs)


def test_extractor_self_correction_recovers(settings, db):
    graph = build_graph(settings, db, AmnesiacExtractor())
    state = graph.invoke(
        {
            "source_file": str(INVOICES_DIR / "invoice_1001.txt"),
            "run_id": "retry-test",
            "started_at": "",
            "trace": [],
            "critique_rounds": [],
        }
    )
    assert state["extraction_retries"] == 1
    assert state["final_status"] == FinalStatus.PAID
