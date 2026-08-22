"""InvoiceFlow review dashboard.

Browse pipeline runs, inspect each agent's reasoning, and work the escalation
queue: invoices the agents sent to a human land here with Approve & Pay /
Reject actions that update the registry and the persisted run.

Run with:  uv run streamlit run ui/app.py
"""

from datetime import UTC, datetime

import streamlit as st

from invoiceflow.config import Settings
from invoiceflow.db import Database
from invoiceflow.models import FinalStatus, InvoiceRunResult, Severity
from invoiceflow.payment import execute_payment
from invoiceflow.pipeline import load_results

st.set_page_config(page_title="InvoiceFlow", page_icon="🧾", layout="wide")

STATUS_BADGE = {
    FinalStatus.PAID: "✅ paid",
    FinalStatus.REJECTED: "⛔ rejected",
    FinalStatus.NEEDS_REVIEW: "🟡 needs review",
    FinalStatus.DUPLICATE: "🔁 duplicate",
    FinalStatus.FAILED: "💥 failed",
}
SEVERITY_ICON = {Severity.CRITICAL: "🔴", Severity.WARNING: "🟠", Severity.INFO: "🔵"}

settings = Settings()
db = Database(settings.db_path)
results = load_results(settings.results_dir)

st.title("🧾 InvoiceFlow")
st.caption(
    "Multi-agent invoice processing — ingestion → validation → approval → payment. "
    f"{len(results)} recorded run(s)."
)

if not results:
    st.info(
        "No runs yet. Process some invoices first:\n\n"
        "```\nuv run python main.py --init-db\nuv run python main.py --all\n```"
    )
    st.stop()


def save(result: InvoiceRunResult) -> None:
    (settings.results_dir / f"{result.run_id}.json").write_text(result.model_dump_json(indent=2))


def resolve(result: InvoiceRunResult, approve: bool, reviewer_note: str) -> None:
    inv, decision = result.invoice, result.decision
    if inv is None or decision is None:
        # Only needs_review runs reach the queue, and those always carry both —
        # but this button writes to the payment registry, so check locally
        # rather than trusting an invariant enforced two modules away.
        st.error("This run has no extracted invoice or decision to act on.")
        return

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    if approve:
        result.payment = execute_payment(db, inv, result.run_id)
        result.final_status = (
            FinalStatus.PAID if result.payment.status == "success" else FinalStatus.DUPLICATE
        )
    else:
        result.final_status = FinalStatus.REJECTED
    verdict = "approved and paid" if approve else "rejected"
    decision.reasoning += f"\n\nHuman override at {stamp}: {verdict}." + (
        f" Note: {reviewer_note}" if reviewer_note else ""
    )
    db.record_processed(
        inv.invoice_number,
        inv.content_hash(),
        inv.vendor,
        inv.total,
        result.final_status.value,
        result.run_id,
    )
    save(result)


def render_run(result: InvoiceRunResult, *, in_queue: bool = False) -> None:
    inv = result.invoice
    left, right = st.columns([3, 2])
    with left:
        if inv is not None:
            st.markdown(f"**{inv.invoice_number}** · {inv.vendor or '_no vendor_'}")
            st.dataframe(
                [
                    {
                        "item": li.item,
                        "qty": li.quantity,
                        "unit price": li.unit_price,
                        "line total": li.line_total,
                        "note": li.note or "",
                    }
                    for li in inv.line_items
                ],
                width="stretch",
                hide_index=True,
            )
            c1, c2, c3 = st.columns(3)
            c1.metric("Total", f"{inv.currency} {inv.total:,.2f}" if inv.total else "—")
            c2.metric("Due", str(inv.due_date or inv.due_date_raw or "—"))
            c3.metric("Terms", inv.payment_terms or "—")
        if result.error:
            st.error(result.error)
    with right:
        if result.validation is not None:
            st.markdown("**Validation**")
            if result.validation.issues:
                for issue in result.validation.issues:
                    st.markdown(f"{SEVERITY_ICON[issue.severity]} `{issue.code}` — {issue.detail}")
            else:
                st.markdown("🟢 all checks passed")
            st.caption(result.validation.summary)
        if result.decision is not None:
            st.markdown("**Approval decision**")
            st.markdown(result.decision.reasoning)
        for i, r in enumerate(result.critique_rounds, 1):
            st.markdown(f"_Critique round {i}:_ **{r.critique.verdict}** — {r.critique.feedback}")
        if result.payment is not None:
            st.success(
                f"Payment: {result.payment.status} — "
                f"${result.payment.amount:,.2f} to {result.payment.vendor}"
            )

    with st.expander("Full agent trace"):
        for ev in result.trace:
            st.text(f"[{ev.stage:>10}] {ev.event}  {ev.detail}")

    if in_queue:
        note = st.text_input("Reviewer note", key=f"note-{result.run_id}")
        a, b = st.columns(2)
        if a.button("✅ Approve & pay", key=f"approve-{result.run_id}", type="primary"):
            resolve(result, approve=True, reviewer_note=note)
            st.rerun()
        if b.button("⛔ Reject", key=f"reject-{result.run_id}"):
            resolve(result, approve=False, reviewer_note=note)
            st.rerun()


tab_queue, tab_runs, tab_db = st.tabs(["🟡 Escalation queue", "📚 All runs", "🗄 Database"])

with tab_queue:
    queue = [r for r in results if r.final_status == FinalStatus.NEEDS_REVIEW]
    if not queue:
        st.success("Queue is empty — nothing needs human review.")
    for result in queue:
        header = (
            f"{result.invoice.invoice_number} · {result.invoice.vendor or 'unknown vendor'}"
            if result.invoice
            else result.source_file_path
        )
        with st.container(border=True):
            st.subheader(header)
            render_run(result, in_queue=True)

with tab_runs:
    counts: dict[FinalStatus, int] = {}
    for r in results:
        counts[r.final_status] = counts.get(r.final_status, 0) + 1
    cols = st.columns(len(STATUS_BADGE))
    for col, (status, badge) in zip(cols, STATUS_BADGE.items(), strict=True):
        col.metric(badge, counts.get(status, 0))
    options = {
        f"{STATUS_BADGE[r.final_status]} · "
        f"{r.invoice.invoice_number if r.invoice else '?'} · {r.run_id}": r
        for r in results
    }
    choice = st.selectbox("Pick a run", list(options))
    render_run(options[choice])

with tab_db:
    st.markdown("**Inventory**")
    st.dataframe(
        [vars(rec) for rec in db.all_items()] if settings.db_path.exists() else [],
        width="stretch",
        hide_index=True,
    )
    st.markdown("**Processed-invoice registry**")
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM processed_invoices").fetchall()]
    st.dataframe(rows, width="stretch", hide_index=True)
