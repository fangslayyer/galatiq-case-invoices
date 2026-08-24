"""Structuring: several invoices under the threshold that are one payment over it.

Three layers, each tested where it lives — the query that finds a vendor's
window, the rule that applies the threshold to the sum of it, and the demo that
walks three documents through the whole pipeline in the order they arrive.
"""

from datetime import UTC, date, datetime

import pytest

from invoiceflow.models import FinalStatus, Invoice, RecentInvoice, VendorWindow
from invoiceflow.pipeline import Pipeline
from invoiceflow.rules import evaluate_rules
from invoiceflow.structuring import lookup_vendor_window
from tests.conftest import STRUCTURING_DIR
from tests.test_rules_and_agents import make_report
from tests.test_validation import make_invoice

#: The demo, in the order the documents arrive. Four days, three formats, one
#: vendor: $4,860 + $4,320 + $5,400 = $14,580.
TRACK = ["invoice_6001.txt", "invoice_6002.csv", "invoice_6003.json"]


def window(*invoices: RecentInvoice, days: int = 14) -> VendorWindow:
    return VendorWindow(days=days, invoices=list(invoices))


def prior(number: str, total: float, day: int, status=FinalStatus.PAID) -> RecentInvoice:
    return RecentInvoice(
        invoice_number=number,
        vendor="Test Vendor",
        invoice_date=date(2026, 5, day),
        total=total,
        final_status=status,
    )


def register(store, invoice: Invoice, status: FinalStatus = FinalStatus.PAID) -> None:
    """Leave behind the rows a finished run leaves behind.

    Written out rather than driven through the pipeline because the window is
    read from a *join*: the registry carries the standing and the sum, and the
    `invoices` row carries the date the vendor stamped on the document. A test
    that seeded only the registry would pass against a query that finds nothing.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with store.connect() as conn:
        run_pk = conn.execute(
            "INSERT INTO runs (run_id, source_path, started_at, final_status) VALUES (?, ?, ?, ?)",
            (f"run-{invoice.invoice_number}", f"{invoice.invoice_number}.txt", now, status.value),
        ).lastrowid
        turn_pk = conn.execute(
            "INSERT INTO agent_invocations (run_id, seq, node, agent, outcome, started_at) "
            "VALUES (?, 1, 'ingest', 'extractor', 'ok', ?)",
            (run_pk, now),
        ).lastrowid
        conn.execute(
            "INSERT INTO invoices (run_id, invocation_id, invoice_number, vendor, invoice_date, "
            "total, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_pk,
                turn_pk,
                invoice.invoice_number,
                invoice.vendor,
                str(invoice.invoice_date) if invoice.invoice_date else None,
                invoice.total,
                invoice.content_hash(),
            ),
        )
    store.record_processed(
        invoice.invoice_number,
        invoice.content_hash(),
        invoice.vendor,
        invoice.total,
        status.value,
        run_pk,
    )


def scrutiny_reasons(store, run_id: str) -> list[str]:
    """The scrutiny reasons persisted for a run — what a reviewer opens later."""
    with store.connect() as conn:
        return [
            row["reason"]
            for row in conn.execute(
                "SELECT rr.reason FROM rule_reasons rr "
                "JOIN rule_evaluations re ON re.id = rr.rule_evaluation_id "
                "JOIN runs r ON r.id = re.run_id "
                "WHERE r.run_id = ? AND rr.kind = 'scrutiny'",
                (run_id,),
            )
        ]


class TestTheRule:
    """`evaluate_rules` with a window on the table. The threshold is about money
    leaving the company, not about the size of any one page."""

    def test_one_small_invoice_alone_is_not_scrutinised(self):
        c = evaluate_rules(make_invoice(total=4_860.0), make_report(), 10_000, None, window())
        assert not c.requires_scrutiny

    def test_a_window_that_stays_under_the_threshold_changes_nothing(self):
        c = evaluate_rules(
            make_invoice(total=4_320.0),
            make_report(),
            10_000,
            None,
            window(prior("INV-6001", 4_860.0, 4)),
        )
        assert not c.requires_scrutiny

    def test_the_invoice_that_takes_the_vendor_over_is_scrutinised(self):
        c = evaluate_rules(
            make_invoice(total=5_400.0, invoice_date=date(2026, 5, 8)),
            make_report(),
            10_000,
            None,
            window(prior("INV-6001", 4_860.0, 4), prior("INV-6002", 4_320.0, 6)),
        )
        assert c.requires_scrutiny
        reason = c.scrutiny_reasons[0]
        assert "$14,580.00" in reason  # the sum it is really being asked to approve
        assert "3 invoices dated within 14 days" in reason
        # and it names them: the reviewer's next question is "which ones?"
        assert "INV-6001" in reason and "INV-6002" in reason

    def test_the_finding_is_a_scrutiny_flag_and_nothing_harder(self):
        """Three invoices in a week may be three deliveries. The pattern is
        established; the intent is not, and nothing here accuses anyone."""
        c = evaluate_rules(
            make_invoice(total=5_400.0),
            make_report(),
            10_000,
            None,
            window(prior("INV-6001", 4_860.0, 4), prior("INV-6002", 4_320.0, 6)),
        )
        assert not c.must_reject
        assert not c.must_review
        assert not c.outcome_is_forced

    def test_an_invoice_over_the_threshold_alone_is_not_told_twice(self):
        c = evaluate_rules(
            make_invoice(total=12_000.0),
            make_report(),
            10_000,
            None,
            window(prior("INV-6001", 4_860.0, 4)),
        )
        assert c.requires_scrutiny
        assert len(c.scrutiny_reasons) == 1
        assert "exceeds the $10,000 review threshold" in c.scrutiny_reasons[0]

    def test_no_window_is_the_rule_as_it_was_before_this_existed(self):
        c = evaluate_rules(make_invoice(total=9_999.0), make_report(), 10_000)
        assert not c.requires_scrutiny


class TestTheLookup:
    """What counts as 'the same vendor, close in time' — and what does not."""

    def test_it_finds_the_vendors_other_invoices(self, store, settings):
        register(store, make_invoice(invoice_number="INV-6001", total=4_860.0))
        found = lookup_vendor_window(
            store, make_invoice(invoice_number="INV-6003", total=5_400.0), settings
        )
        assert [rec.invoice_number for rec in found.invoices] == ["INV-6001"]
        assert found.total == 4_860.0

    def test_another_company_is_not_this_one(self, store, settings):
        register(store, make_invoice(invoice_number="INV-6001", vendor="Other Co.", total=9_000.0))
        found = lookup_vendor_window(store, make_invoice(invoice_number="INV-6003"), settings)
        assert found.invoices == []

    def test_one_company_spelled_two_ways_is_one_history(self, store, settings):
        register(store, make_invoice(invoice_number="INV-6001", vendor="TEST  vendor.", total=1.0))
        found = lookup_vendor_window(store, make_invoice(invoice_number="INV-6003"), settings)
        assert [rec.invoice_number for rec in found.invoices] == ["INV-6001"]

    def test_an_invoice_outside_the_window_is_a_different_payment(self, store, settings):
        register(
            store,
            make_invoice(invoice_number="INV-6001", invoice_date=date(2026, 3, 1), total=9_000.0),
        )
        found = lookup_vendor_window(
            store,
            make_invoice(invoice_number="INV-6003", invoice_date=date(2026, 5, 8)),
            settings,
        )
        assert found.invoices == []

    def test_a_rejected_invoice_is_not_money_leaving(self, store, settings):
        register(
            store,
            make_invoice(invoice_number="INV-6001", total=9_000.0),
            status=FinalStatus.REJECTED,
        )
        found = lookup_vendor_window(store, make_invoice(invoice_number="INV-6003"), settings)
        assert found.invoices == []

    def test_an_invoice_awaiting_review_still_counts(self, store, settings):
        """Nothing has been paid on it yet, but it is queued to be — and the
        question is how much is heading out of the door in this window."""
        register(
            store,
            make_invoice(invoice_number="INV-6001", total=9_000.0),
            status=FinalStatus.NEEDS_REVIEW,
        )
        found = lookup_vendor_window(store, make_invoice(invoice_number="INV-6003"), settings)
        assert [rec.invoice_number for rec in found.invoices] == ["INV-6001"]

    def test_an_invoice_never_finds_itself(self, store, settings):
        """A re-run or a revision must not clear the threshold on its own back."""
        register(store, make_invoice(invoice_number="INV-6003", total=9_000.0))
        found = lookup_vendor_window(store, make_invoice(invoice_number="INV-6003"), settings)
        assert found.invoices == []

    @pytest.mark.parametrize("missing", [{"invoice_date": None}, {"total": None}, {"vendor": " "}])
    def test_an_invoice_missing_what_a_window_needs_gets_an_empty_one(
        self, store, settings, missing
    ):
        register(store, make_invoice(invoice_number="INV-6001", total=9_000.0))
        found = lookup_vendor_window(
            store, make_invoice(invoice_number="INV-6003", **missing), settings
        )
        assert found.invoices == []
        assert found.days == settings.structuring_window_days


class TestTheDemo:
    """data/demo/structuring, end to end, in arrival order."""

    @staticmethod
    def run(pipeline, name):
        return pipeline.run(STRUCTURING_DIR / name)

    def test_the_first_two_are_paid_with_nothing_to_notice_yet(self, settings, db, fake_brain):
        """Not a miss: at $4,860 and $9,180 there is nothing over the threshold
        to see, and a pipeline cannot scrutinise an invoice that has not arrived."""
        pipeline = Pipeline(settings, llm=fake_brain)
        for name in TRACK[:2]:
            result = self.run(pipeline, name)
            assert result.final_status == FinalStatus.PAID, name
            assert "review threshold" not in result.decision.reasoning

    def test_the_third_is_scrutinised_as_the_one_payment_they_add_up_to(
        self, settings, db, store, fake_brain
    ):
        pipeline = Pipeline(settings, llm=fake_brain)
        for name in TRACK[:2]:
            self.run(pipeline, name)
        third = self.run(pipeline, TRACK[2])

        assert third.invoice.total == 5_400.0  # the document itself stays under $10,000
        events = [ev for ev in third.trace if ev.event == "vendor_window"]
        assert len(events) == 1  # resolved once, even across a redraft
        assert "$9,180.00" in events[0].detail  # the two already through

        reason = scrutiny_reasons(store, third.run_id)
        assert len(reason) == 1
        assert "$14,580.00" in reason[0]
        assert "INV-6001" in reason[0] and "INV-6002" in reason[0]
        # and the Approver was told, in the words the reviewer will read
        assert "$14,580.00" in third.decision.reasoning

    def test_the_split_is_flagged_and_never_treated_as_an_accusation(
        self, settings, db, fake_brain
    ):
        """The flag is the one a single $14,580 invoice would have raised: a
        closer look, not a rejection and not a forced escalation."""
        pipeline = Pipeline(settings, llm=fake_brain)
        for name in TRACK[:2]:
            self.run(pipeline, name)
        third = self.run(pipeline, TRACK[2])
        assert third.final_status != FinalStatus.REJECTED
        assert not third.overrides  # no hard rule fired, and none should have
