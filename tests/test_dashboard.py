"""Dashboard tests: the human-override paths, driven through Streamlit's own
AppTest runner so the real script executes.

These cover the half of the system no pipeline test reaches — what a person can
do to a run after the agents are done with it.
"""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from invoiceflow.config import PROJECT_ROOT
from invoiceflow.models import FinalStatus
from invoiceflow.pipeline import Pipeline
from tests.conftest import INVOICES_DIR

APP = str(PROJECT_ROOT / "ui" / "app.py")


@pytest.fixture
def dashboard(settings, db, fake_brain, monkeypatch):
    """A dashboard over two real runs: one paid, one auto-rejected.

    The app builds its own Settings from the environment, so the tmp paths have
    to be exported rather than injected.
    """
    monkeypatch.setenv("INVOICEFLOW_DB_PATH", str(settings.db_path))
    monkeypatch.setenv("INVOICEFLOW_RESULTS_DIR", str(settings.results_dir))
    pipe = Pipeline(settings, llm=fake_brain)
    pipe.run(INVOICES_DIR / "invoice_1001.txt")  # clean -> paid
    rejected = pipe.run(INVOICES_DIR / "invoice_1003.txt")  # zero-stock -> rejected
    assert rejected.final_status == FinalStatus.REJECTED
    return settings.results_dir


def saved(results_dir: Path, run_id: str) -> dict:
    return json.loads((results_dir / f"{run_id}.json").read_text())


def test_rejected_runs_are_actionable_and_counted(dashboard):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    # Auto-rejections get their own tab, not the escalation queue: the queue
    # means "this needs a decision from you", these are decided already.
    assert "⛔ Rejected (1 unchecked)" in [t.label for t in at.tabs]
    labels = {b.label for b in at.button}
    assert {"✅ Overturn & pay", "⛔ Confirm rejection"} <= labels


def test_confirming_a_rejection_stamps_the_reviewer(dashboard):
    at = AppTest.from_file(APP, default_timeout=60).run()
    confirm = next(b for b in at.button if b.label == "⛔ Confirm rejection")
    assert confirm.key is not None
    run_id = confirm.key.removeprefix("reject-")
    confirm.click().run()
    assert not at.exception, [e.value for e in at.exception]

    result = saved(dashboard, run_id)
    assert result["final_status"] == FinalStatus.REJECTED  # unchanged
    assert result["human_reviewed_at"]  # but no longer unchecked
    assert "Human confirmation" in result["decision"]["reasoning"]
    assert "Human override" not in result["decision"]["reasoning"]
    # And it stops being counted as outstanding: the tab loses its "unchecked".
    after = [t.label for t in AppTest.from_file(APP, default_timeout=60).run().tabs]
    assert "⛔ Rejected" in after
    assert not any("unchecked" in label for label in after)


def test_overturning_a_rejection_pays_it(dashboard):
    at = AppTest.from_file(APP, default_timeout=60).run()
    overturn = next(b for b in at.button if b.label == "✅ Overturn & pay")
    assert overturn.key is not None
    run_id = overturn.key.removeprefix("approve-")
    overturn.click().run()
    assert not at.exception, [e.value for e in at.exception]

    result = saved(dashboard, run_id)
    assert result["final_status"] == FinalStatus.PAID
    assert result["payment"]["status"] == "success"
    assert "Human override" in result["decision"]["reasoning"]
    assert result["human_reviewed_at"]
