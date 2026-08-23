"""InvoiceFlow review dashboard, reading straight from invoiceflow.db.

Browse pipeline runs, inspect each agent's reasoning, and act on the ones a
person should see. Two queues, kept apart on purpose:

  Escalation queue — the agents could not decide; this needs a decision.
  Rejected         — the rules already decided; overturn it or confirm it.

A human action never edits what the agents wrote: it lands as a `human_reviews`
row (plus a payment and a registry update where money moves), and the effective
status is derived from it. "Unchecked" is simply the absence of any review.

Run with:  uv run streamlit run ui/app.py
"""

import streamlit as st

from invoiceflow.config import Settings
from invoiceflow.db import Database
from invoiceflow.models import FinalStatus, InvoiceRunResult, PaymentStatus, Severity
from invoiceflow.review import apply_human_review
from invoiceflow.runstore import RunStore

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
store = RunStore(settings.runs_db_path)
results = store.load_results()

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


def resolve(result: InvoiceRunResult, approve: bool, reviewer_note: str) -> bool:
    """Hand the click to `apply_human_review` and show what it decided.

    Everything this used to work out for itself — what to pay, what status the
    run takes, whether the action counts as a confirmation or an override —
    now lives in `invoiceflow.review`. This function is the button and the
    error message; it decides nothing.
    """
    outcome = apply_human_review(store, result, approve=approve, note=reviewer_note)
    if not outcome.recorded:
        st.error(outcome.message)
    return outcome.recorded


def render_run(result: InvoiceRunResult, *, actionable: bool = False) -> None:
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
            # `amount` on a declined payment is what was claimed and refused,
            # not what moved — rendering both the same way is how $5,940 that
            # never left the bank read as a completed payment.
            pay = result.payment
            if pay.status == PaymentStatus.SUCCESS:
                st.success(f"💸 Sent **${pay.amount:,.2f}** to {pay.vendor}")
            else:
                st.warning(
                    f"🚫 Nothing sent to {pay.vendor} — the **${pay.amount:,.2f}** claimed "
                    f"was declined ({pay.status.replace('_', ' ')})"
                )
        for hr in result.human_reviews:
            st.info(
                f"🧑 {hr.action.replace('_', ' ')} by {hr.reviewer} at {hr.reviewed_at}: "
                f"{hr.from_status} → {hr.to_status}" + (f" — {hr.note}" if hr.note else "")
            )

    with st.expander("Full agent trace"):
        for ev in result.trace:
            st.text(f"[{ev.stage:>10}] {ev.event}  {ev.detail}")

    if actionable:
        # A rejection is already decided, so the same two buttons mean different
        # things there: pay it after all, or put a person's name to the refusal.
        rejected = result.final_status == FinalStatus.REJECTED
        pay_label = "✅ Overturn & pay" if rejected else "✅ Approve & pay"
        reject_label = "⛔ Confirm rejection" if rejected else "⛔ Reject"
        if result.human_reviewed_at:
            st.caption(f"Reviewed by a human at {result.human_reviewed_at}")
        note = st.text_input("Reviewer note", key=f"note-{result.run_id}")
        a, b = st.columns(2)
        # Only rerun when the review landed: a rerun would wipe the error
        # explaining why it did not.
        if a.button(pay_label, key=f"approve-{result.run_id}", type="primary") and resolve(
            result, approve=True, reviewer_note=note
        ):
            st.rerun()
        if b.button(reject_label, key=f"reject-{result.run_id}") and resolve(
            result, approve=False, reviewer_note=note
        ):
            st.rerun()


def run_header(result: InvoiceRunResult) -> str:
    return (
        f"{result.invoice.invoice_number} · {result.invoice.vendor or 'unknown vendor'}"
        if result.invoice
        else result.source_file_path
    )


rejected = store.rejected_runs()
unchecked = sum(1 for r in rejected if not r.human_reviewed_at)
tab_queue, tab_rejected, tab_runs, tab_db = st.tabs(
    [
        "🟡 Escalation queue",
        f"⛔ Rejected ({unchecked} unchecked)" if unchecked else "⛔ Rejected",
        "📚 All runs",
        "🗄 Database",
    ]
)

with tab_queue:
    queue = store.review_queue()
    if not queue:
        st.success("Queue is empty — nothing needs human review.")
    for result in queue:
        with st.container(border=True):
            st.subheader(run_header(result))
            render_run(result, actionable=True)

with tab_rejected:
    # Kept out of the escalation queue on purpose: the queue means "this needs a
    # decision from you", these are decided and merely open to being overturned.
    st.caption(
        "Rejected automatically by the hard business rules. Nothing here is waiting "
        "on you — overturn one that is wrong, or confirm it so it stops showing as "
        "unchecked."
    )
    if not rejected:
        st.info("No rejected runs.")
    for result in rejected:
        mark = "" if result.human_reviewed_at else " · 🔎 unchecked"
        with st.container(border=True):
            st.subheader(run_header(result) + mark)
            render_run(result, actionable=True)

with tab_runs:
    counts: dict[FinalStatus, int] = {}
    for r in results:
        counts[r.final_status] = counts.get(r.final_status, 0) + 1
    cols = st.columns(len(STATUS_BADGE))
    for col, (status, badge) in zip(cols, STATUS_BADGE.items(), strict=True):
        col.metric(badge, counts.get(status, 0))
    sent = store.money_sent()
    st.metric(
        "💸 Money actually sent",
        " · ".join(f"{cur} {amt:,.2f}" for cur, amt in sorted(sent.items())) if sent else "—",
        help="Successful payments only. A declined payment records the sum it refused, "
        "which is not money that moved.",
    )
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
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM invoice_registry").fetchall()]
    st.dataframe(rows, width="stretch", hide_index=True)

    st.markdown("**Cost by agent** · from `v_cost_by_agent`")
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM v_cost_by_agent").fetchall()]
    st.dataframe(rows, width="stretch", hide_index=True)

    st.markdown("**Issue frequency** · from `v_issue_frequency`")
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM v_issue_frequency").fetchall()]
    st.dataframe(rows, width="stretch", hide_index=True)

    reprocessed = []
    with store.connect() as conn:
        reprocessed = [
            dict(r) for r in conn.execute("SELECT * FROM v_reprocessed_documents").fetchall()
        ]
    if reprocessed:
        st.markdown("**Reprocessed documents** · same content, multiple runs")
        st.dataframe(reprocessed, width="stretch", hide_index=True)
