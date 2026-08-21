"""Rule engine + agent behavior with the stub brain, including the
self-correction paths that clean fixtures never trigger."""

from invoiceflow.agents import run_approver, run_critic, run_validator
from invoiceflow.models import (
    ApprovalDecision,
    ApprovalStatus,
    CritiqueVerdict,
    IssueCode,
    Severity,
    ValidationIssue,
    ValidationReport,
)
from invoiceflow.rules import evaluate_rules
from invoiceflow.validation import ValidationContext
from tests.fakes import FakeBrain
from tests.test_validation import make_invoice


def make_report(*issues: ValidationIssue) -> ValidationReport:
    return ValidationReport(issues=list(issues), summary="test")


def critical(code=IssueCode.STOCK_EXCEEDED, detail="stock exceeded"):
    return ValidationIssue(code=code, severity=Severity.CRITICAL, detail=detail)


def warning(code=IssueCode.UNKNOWN_ITEM, detail="unknown item"):
    return ValidationIssue(code=code, severity=Severity.WARNING, detail=detail)


class TestRuleEngine:
    def test_critical_issue_forces_rejection(self):
        c = evaluate_rules(make_invoice(), make_report(critical()), 10_000)
        assert c.must_reject
        assert "stock exceeded" in c.reject_reasons[0]

    def test_warnings_are_advisory(self):
        c = evaluate_rules(make_invoice(), make_report(warning()), 10_000)
        assert not c.must_reject
        assert c.advisory_warnings

    def test_scrutiny_threshold(self):
        c = evaluate_rules(make_invoice(total=10_001.0), make_report(), 10_000)
        assert c.requires_scrutiny
        assert not evaluate_rules(
            make_invoice(total=9_999.0), make_report(), 10_000
        ).requires_scrutiny


class TestApprovalAgents:
    llm = FakeBrain()

    def test_approver_rejects_on_must_reject(self):
        inv, rep = make_invoice(), make_report(critical())
        c = evaluate_rules(inv, rep, 10_000)
        decision = run_approver(self.llm, inv, rep, c)
        assert decision.status == ApprovalStatus.REJECTED

    def test_approver_escalates_on_warnings(self):
        inv, rep = make_invoice(), make_report(warning())
        c = evaluate_rules(inv, rep, 10_000)
        assert run_approver(self.llm, inv, rep, c).status == ApprovalStatus.NEEDS_REVIEW

    def test_approver_notes_scrutiny_on_high_value(self):
        inv, rep = make_invoice(total=15_000.0), make_report()
        c = evaluate_rules(inv, rep, 10_000)
        decision = run_approver(self.llm, inv, rep, c)
        assert decision.status == ApprovalStatus.APPROVED
        assert "scrutiny" in decision.reasoning.lower()

    def test_critic_forces_revision_of_bad_approval(self):
        """The reflection loop: an approval that contradicts hard rules is sent back."""
        inv, rep = make_invoice(), make_report(critical())
        c = evaluate_rules(inv, rep, 10_000)
        bad = ApprovalDecision(status=ApprovalStatus.APPROVED, reasoning="looks fine to me")
        critique = run_critic(self.llm, inv, rep, c, bad)
        assert critique.verdict == CritiqueVerdict.REVISE
        assert "critical" in critique.feedback.lower()

    def test_critic_rejects_unfounded_rejection(self):
        inv, rep = make_invoice(), make_report()
        c = evaluate_rules(inv, rep, 10_000)
        bad = ApprovalDecision(status=ApprovalStatus.REJECTED, reasoning="I don't like it")
        assert run_critic(self.llm, inv, rep, c, bad).verdict == CritiqueVerdict.REVISE

    def test_critic_accepts_consistent_decision(self):
        inv, rep = make_invoice(), make_report()
        c = evaluate_rules(inv, rep, 10_000)
        good = ApprovalDecision(status=ApprovalStatus.APPROVED, reasoning="all checks passed")
        assert run_critic(self.llm, inv, rep, c, good).verdict == CritiqueVerdict.ACCEPT


class TestValidatorAgent:
    def test_tool_loop_runs_all_checks(self, db):
        ctx = ValidationContext(make_invoice(), db)
        report = run_validator(FakeBrain(), ctx)
        assert set(report.tools_used) == {
            "check_inventory",
            "verify_arithmetic",
            "check_integrity",
            "check_duplicate",
        }
        assert report.issues == []
        assert report.summary
