"""InvoiceFlow review dashboard, reading straight from invoiceflow.db.

Browse pipeline runs, inspect each agent's reasoning, and act on the ones a
person should see. Two queues, kept apart on purpose:

  Escalation queue — the agents could not decide; this needs a decision.
  Rejected         — the rules already decided; overturn it or confirm it.

A human action never edits what the agents wrote: it lands as a `human_reviews`
row (plus a payment and a registry update where money moves), and the effective
status is derived from it. "Unchecked" is simply the absence of any review.

Uploads land in a third list, the Inbox, which is intake rather than judgement:
a file is queued here, processed by a background worker one at a time, and only
then becomes a run the two queues above can act on.

Run with:  uv run streamlit run ui/app.py
"""

import streamlit as st

from invoiceflow import inbox
from invoiceflow.config import PROJECT_ROOT, Settings
from invoiceflow.db import Database
from invoiceflow.inbox import InboxWorker, UploadProbe
from invoiceflow.loaders import SUPPORTED_EXTENSIONS
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
INBOX_BADGE = {
    "queued": "⏳ queued",
    "processing": "⚙️ processing",
    "processed": "✅ done",
    "failed": "💥 failed",
}
#: Shown instead of the status badge while a run is still in flight.
IN_FLIGHT_BADGE = "⚙️ processing"


def in_flight(result: InvoiceRunResult) -> bool:
    """Whether this run is still being worked on right now.

    `begin_run` writes the row pessimistically as `failed` / "run did not
    complete" so that a crash leaves an honest audit row, and `finish_run`
    corrects it at the end. That makes an in-flight run indistinguishable from
    a crashed one *by status alone* — but not by `finished_at`, which is NULL
    until the run lands. Presentation only: the stored status stays exactly as
    pessimistic as it should be.
    """
    return not result.finished_at


#: st.session_state is new to this app and used only where a local cannot work.
#: @st.dialog wraps its body in a fragment, so a widget inside the modal reruns
#: only the modal — the `if st.button(...)` that opened it is never
#: re-evaluated, and any full rerun would otherwise close it mid-upload.
UPLOAD_OPEN, PROBES = "inbox.upload_open", "inbox.probes"

settings = Settings()
db = Database(settings.db_path)
store = RunStore(settings.runs_db_path)
results = store.load_results()

title_col, upload_col = st.columns([5, 1], vertical_alignment="bottom")
title_col.title("🧾 InvoiceFlow")
title_col.caption(
    "Multi-agent invoice processing — ingestion → validation → approval → payment. "
    f"{len(results)} recorded run(s)."
)
if upload_col.button("📤 Upload", type="primary", width="stretch", key="open-upload"):
    st.session_state[UPLOAD_OPEN] = True

if not settings.db_path.exists():
    st.warning(
        "No inventory database — the Validator has nothing to check items against, so "
        "anything processed now will fail. Run `uv run python main.py --init-db` first."
    )


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
    running = in_flight(result)
    if running:
        # Everything below is empty for a run that has not landed: finish_run
        # writes the invoice, validation, decision and trace in one transaction
        # at the end. Say so, rather than rendering a screen of blanks under
        # "run did not complete" — which reads as a failure and is not one.
        st.info(
            f"⚙️ **Still processing** — started {result.started_at}. The agents write "
            "their findings in one transaction when the run lands, so this stays blank "
            "until then. Watch it advance node by node in the 📥 Inbox tab."
        )
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
        if result.error and not running:
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


@st.cache_resource(show_spinner=False)
def get_worker(runs_db_path: str, _settings: Settings) -> InboxWorker:
    """The dashboard's one background worker.

    Cached because Streamlit re-executes this whole script on every interaction
    in every session — a bare InboxWorker(...) here would start a thread per
    click. The default scope is global, which is the point: all browser
    sessions share one worker, and that is what keeps runs serial across tabs.
    Keyed on the database path so a second store in the same process gets its
    own. The real singleton lives in inbox.worker_for(); this is only a memo in
    front of it, because this cache is clearable from the app's own ⋮ menu.
    """
    return inbox.worker_for(_settings)


worker = get_worker(str(settings.runs_db_path), settings) if settings.inbox_worker else None


def queue_files(chosen: list[UploadProbe], *, discard: list[UploadProbe]) -> None:
    for probe in discard:
        inbox.discard_upload(probe.stored_path)
    for probe in chosen:
        inbox.enqueue(store, probe)
    if chosen and worker is not None:
        worker.wake()  # skip the poll interval; the Event is already waiting
    close_upload()
    # scope="app", deliberately: it closes the modal AND re-runs the script,
    # which is the only thing that can start the Inbox tab's polling.
    st.rerun()


def close_upload() -> None:
    st.session_state[UPLOAD_OPEN] = False
    st.session_state.pop(PROBES, None)


@st.dialog("Upload invoices", width="large", on_dismiss=close_upload)
def upload_dialog() -> None:
    if not settings.resolve_api_key():
        st.warning(
            "No XAI_API_KEY is set, so nothing queued here can be processed — Grok is "
            "the pipeline's only reasoning engine and there is no fallback parser. You "
            "can still upload; the files will wait."
        )
    probes: list[UploadProbe] = st.session_state.get(PROBES, [])
    if not probes:
        files = st.file_uploader(
            "Invoice files",
            # Derived, never re-listed: a format the loader learns is a format
            # the dashboard accepts, with no second list to forget.
            type=sorted(e.lstrip(".") for e in SUPPORTED_EXTENSIONS),
            accept_multiple_files=True,
            key="inbox-uploader",
        )
        st.caption(f"Accepted: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        if files and st.button("Check files", type="primary", key="inbox-check"):
            # Saved before they are read: load_invoice_text takes a path and
            # pdfplumber wants a real file. Anything the user then skips is
            # deleted again — cheaper than a temp-file dance across 5 formats.
            st.session_state[PROBES] = [
                inbox.probe_upload(
                    store, inbox.save_upload(settings.uploads_dir, f.name, f.getvalue())
                )
                for f in files
            ]
            # A full rerun, not scope="fragment": the dialog is gated on
            # session_state, so it reopens with the probes rendered — and a
            # fragment-scoped rerun is only legal during a fragment rerun,
            # which is not what a click through AppTest produces.
            st.rerun()
        if st.button("Load the 20 sample invoices", key="inbox-samples"):
            sample_probes = [
                inbox.probe_upload(store, path)
                for path in sorted((PROJECT_ROOT / "data" / "invoices").iterdir())
                if path.suffix.lower() in SUPPORTED_EXTENSIONS
            ]
            for probe in sample_probes:
                inbox.enqueue(store, probe, source="samples")
            if worker is not None:
                worker.wake()
            close_upload()
            st.rerun()
        return

    readable = [p for p in probes if p.readable]
    for probe in probes:
        if not probe.readable:
            st.error(f"**{probe.filename}** — {probe.error}")
        elif probe.is_rerun:
            st.warning(
                f"**{probe.filename}** — these exact bytes have already been through the "
                f"pipeline {probe.prior_runs} time(s). Running them again costs six Grok "
                f"calls and reaches the same answer."
            )
        else:
            st.success(f"**{probe.filename}** — {probe.byte_size:,} bytes, ready.")

    fresh = [p for p in readable if not p.is_rerun]
    a, b, c = st.columns(3)
    if fresh and a.button(f"Queue {len(fresh)} new", type="primary", key="inbox-queue-new"):
        queue_files(fresh, discard=[p for p in probes if p not in fresh])
    if readable and b.button(f"Queue all {len(readable)}", key="inbox-queue-all"):
        queue_files(readable, discard=[p for p in probes if not p.readable])
    if c.button("Cancel", key="inbox-cancel"):
        queue_files([], discard=probes)


if st.session_state.get(UPLOAD_OPEN):
    upload_dialog()

rejected = store.rejected_runs()
unchecked = sum(1 for r in rejected if not r.human_reviewed_at)
inbox_counts = store.inbox_counts()
pending = inbox_counts.get("queued", 0) + inbox_counts.get("processing", 0)
# run_every is fixed when the fragment is *declared*, and the browser rebuilds
# its auto-rerun timers from scratch on every full app run — nothing the server
# sends later can cancel one. So recomputing it here is how the polling both
# starts and, via the st.rerun() at the end of the fragment, stops.
refresh = 2.0 if pending else None

tab_inbox, tab_queue, tab_rejected, tab_runs, tab_db = st.tabs(
    [
        f"📥 Inbox ({pending} in flight)" if pending else "📥 Inbox",
        "🟡 Escalation queue",
        f"⛔ Rejected ({unchecked} unchecked)" if unchecked else "⛔ Rejected",
        "📚 All runs",
        "🗄 Database",
    ]
)

with tab_inbox:
    st.caption(
        "Uploaded files are processed one at a time, in the background, by the same "
        "pipeline the CLI runs — the queue is the only difference. Serial on purpose: "
        "payment idempotency is a read of the registry in `pay` and a write of it in "
        "`record`, so two runs of one invoice number in flight together could both "
        "conclude nothing had been paid."
    )

    @st.fragment(run_every=refresh)
    def inbox_panel() -> None:
        rows = store.inbox_rows()
        if not rows:
            st.info("Nothing uploaded yet. Use 📤 Upload, top right.")
            return
        st.dataframe(
            [
                {
                    "file": r["filename"],
                    "state": INBOX_BADGE[r["state"]] + (f" · {r['stage']}" if r["stage"] else ""),
                    "outcome": STATUS_BADGE[FinalStatus(r["final_status"])]
                    if r["final_status"]
                    else "—",
                    "invoice": r["invoice_number"] or "—",
                    "vendor": r["vendor"] or "—",
                    "total": f"{r['currency']} {r['total']:,.2f}" if r["total"] else "—",
                    "seconds": round((r["duration_ms"] or 0) / 1000, 1) or "—",
                    "cost": f"${r['cost_usd']:.4f}" if r["cost_usd"] else "—",
                    "note": r["error"]
                    or ("re-run of a document already processed" if r["prior_runs"] else ""),
                }
                for r in rows
            ],
            width="stretch",
            hide_index=True,
        )
        for r in rows:
            if r["state"] not in ("failed", "processed"):
                continue
            left, right, _ = st.columns([1, 1, 6])
            if r["state"] == "failed" and left.button(
                "↻ Retry", key=f"retry-{r['id']}", help=r["filename"]
            ):
                store.requeue_upload(r["id"])
                if worker is not None:
                    worker.wake()
                st.rerun()
            if right.button("✕ Dismiss", key=f"dismiss-{r['id']}", help=r["filename"]):
                store.dismiss_upload(r["id"])
                st.rerun()
        if refresh and not any(r["state"] in ("queued", "processing") for r in rows):
            # The queue drained. Only a *full* app run takes the browser's
            # polling timer down, and the other tabs are stale anyway — they
            # were rendered before these runs existed.
            st.rerun()

    inbox_panel()

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
    if not results:
        # The old st.stop() lived at the top of the script and hid the whole
        # app in this state — including the upload button that fixes it.
        st.info(
            "No runs yet. Upload a file with 📤 Upload, or process the samples:\n\n"
            "```\nuv run python main.py --init-db\nuv run python main.py --all\n```"
        )
    counts: dict[FinalStatus, int] = {}
    running = 0
    for r in results:
        # An unfinished run is not a failed one, however its row reads.
        if in_flight(r):
            running += 1
        else:
            counts[r.final_status] = counts.get(r.final_status, 0) + 1
    cols = st.columns(len(STATUS_BADGE) + (1 if running else 0))
    for col, (status, badge) in zip(cols, STATUS_BADGE.items(), strict=False):
        col.metric(badge, counts.get(status, 0))
    if running:
        cols[-1].metric(IN_FLIGHT_BADGE, running)
    sent = store.money_sent()
    st.metric(
        "Money sent",
        " · ".join(f"{cur} {amt:,.2f}" for cur, amt in sorted(sent.items())) if sent else "—",
        help="Successful payments only. A declined payment records the sum it refused, "
        "which is not money that moved.",
    )
    options = {
        f"{IN_FLIGHT_BADGE if in_flight(r) else STATUS_BADGE[r.final_status]} · "
        f"{r.invoice.invoice_number if r.invoice else '?'} · {r.run_id}": r
        for r in results
    }
    if options:
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
