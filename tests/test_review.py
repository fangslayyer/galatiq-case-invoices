"""Human review: what a person's decision does to a run.

These paths were reachable only through Streamlit's AppTest while the logic
lived in ui/app.py, which is why a drifted copy of it went unnoticed. It is in
src now, so the rules are tested directly.
"""

import pytest

from invoiceflow.models import FinalStatus, PaymentResult, PaymentStatus
from invoiceflow.pipeline import Pipeline
from invoiceflow.review import apply_human_review
from tests.conftest import INVOICES_DIR


@pytest.fixture
def pipe(settings, db, fake_brain) -> Pipeline:
    return Pipeline(settings, llm=fake_brain)


class TestHumanReview:
    def test_overturning_a_rejection_pays_in_full(self, pipe):
        rejected = pipe.run(INVOICES_DIR / "invoice_1003.txt")
        assert rejected.final_status == FinalStatus.REJECTED

        out = apply_human_review(pipe.store, rejected, approve=True, note="confirmed with vendor")
        assert out.recorded
        assert out.action == "override_approve"
        assert out.to_status == FinalStatus.PAID
        assert out.payment is not None and out.payment.status == PaymentStatus.SUCCESS
        assert out.payment.amount == rejected.invoice.total

    def test_confirming_a_rejection_changes_nothing_but_the_record(self, pipe):
        rejected = pipe.run(INVOICES_DIR / "invoice_1003.txt")

        out = apply_human_review(pipe.store, rejected, approve=False, note="agreed")
        assert out.recorded and out.action == "confirm"
        assert out.to_status == FinalStatus.REJECTED
        assert out.payment is None  # a confirmation moves no money
        reloaded = pipe.store.load_result(rejected.run_id)
        assert reloaded.human_reviewed_at  # no longer unchecked

    def test_approving_an_amendment_sends_only_the_balance(self, pipe):
        """The dashboard bug, at the level it actually lives: approving a
        revision used to send the restated total, paying the original sum
        twice."""
        paid = pipe.run(INVOICES_DIR / "invoice_1004.json")
        assert paid.final_status == FinalStatus.PAID
        revision = pipe.run(INVOICES_DIR / "invoice_1004_revised.json")
        assert revision.final_status == FinalStatus.NEEDS_REVIEW

        out = apply_human_review(pipe.store, revision, approve=True, note="PO amended")
        assert out.recorded
        assert out.to_status == FinalStatus.PAID
        assert out.payment is not None
        assert out.payment.amount == 4_050.0  # 5,940 claimed - 1,890 already sent
        registry = pipe.store.get_processed("INV-1004")
        assert registry.final_status == "paid" and registry.total == 5_940.0

    def test_approving_twice_sends_nothing_the_second_time(self, pipe):
        pipe.run(INVOICES_DIR / "invoice_1004.json")
        revision = pipe.run(INVOICES_DIR / "invoice_1004_revised.json")
        assert apply_human_review(pipe.store, revision, approve=True).recorded

        again = apply_human_review(pipe.store, revision, approve=True)
        assert not again.recorded  # refused, and nothing is written
        assert "nothing to pay" in again.message
        assert again.payment is not None
        assert again.payment.status == PaymentStatus.SKIPPED_ALREADY_PAID
        registry = pipe.store.get_processed("INV-1004")
        assert registry.final_status == "paid" and registry.total == 5_940.0

    def test_a_refused_approval_leaves_no_review_behind(self, pipe):
        pipe.run(INVOICES_DIR / "invoice_1004.json")
        revision = pipe.run(INVOICES_DIR / "invoice_1004_revised.json")
        apply_human_review(pipe.store, revision, approve=True)
        before = len(pipe.store.load_result(revision.run_id).human_reviews)

        apply_human_review(pipe.store, revision, approve=True)  # refused
        after = pipe.store.load_result(revision.run_id).human_reviews
        assert len(after) == before  # a refusal is not a review


class TestMoneySent:
    def test_counts_only_payments_that_moved(self, pipe):
        paid = pipe.run(INVOICES_DIR / "invoice_1004.json")  # sends 1,890
        revision = pipe.run(INVOICES_DIR / "invoice_1004_revised.json")
        apply_human_review(pipe.store, revision, approve=True)  # sends the 4,050 balance
        assert pipe.store.money_sent() == {"USD": 5_940.0}

        # A declined payment records the sum it refused. That is not money that
        # moved, and summing `payments.amount` blind would report it as such.
        pipe.store.add_payment(
            paid.run_id,
            PaymentResult(
                status=PaymentStatus.SKIPPED_ALREADY_PAID,
                vendor="Precision Parts Ltd.",
                amount=99_999.0,
                reference=paid.run_id,
                paid_at="2026-01-01T00:00:00+00:00",
            ),
        )
        assert pipe.store.money_sent() == {"USD": 4_050.0}
