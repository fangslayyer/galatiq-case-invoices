"""InvoiceFlow review dashboard, reading straight from invoiceflow.db.

Browse pipeline runs, inspect each agent's reasoning, and act on the ones a
person should see. Two queues, kept apart on purpose:

  Needs review — the agents could not decide; this needs a decision.
  Rejected     — the rules already decided; overturn it or confirm it.

A human action never edits what the agents wrote: it lands as a `human_reviews`
row (plus a payment and a registry update where money moves), and the effective
status is derived from it. "Unchecked" is simply the absence of any review.

Uploads land in a third list, the Inbox, which is intake rather than judgement:
a file is queued here, processed by a background worker one at a time, and only
then becomes a run the two queues above can act on.

Run with:  uv run streamlit run ui/app.py
"""

import sqlite3
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitAPIException

from invoiceflow import inbox
from invoiceflow.config import PROJECT_ROOT, Settings
from invoiceflow.db import Database
from invoiceflow.inbox import InboxWorker, UploadProbe
from invoiceflow.loaders import SUPPORTED_EXTENSIONS
from invoiceflow.models import (
    ApprovalStatus,
    CritiqueVerdict,
    FinalStatus,
    InvoiceRunResult,
    IssueCode,
    PaymentStatus,
    Severity,
)
from invoiceflow.review import apply_human_review
from invoiceflow.runstore import RunStore

st.set_page_config(page_title="ACME Corporation - InvoiceFlow", page_icon="🧾", layout="wide")

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

#: Colour per outcome, for the one-line verdicts in the detail pane.
DECISION_TONE = {
    ApprovalStatus.APPROVED: "green",
    ApprovalStatus.REJECTED: "red",
    ApprovalStatus.NEEDS_REVIEW: "orange",
}
#: The Critic judges the Approver's *decision*, never the invoice — affirming a
#: rejection means the rejection was right, not that the invoice is good. So
#: these read as verbs on the decision, and an affirm takes the colour of the
#: decision it upheld rather than a green of its own.
CRITIQUE_OUTCOME = {
    CritiqueVerdict.AFFIRM: "upheld the decision",
    CritiqueVerdict.REVISE: "sent back for revision",
    CritiqueVerdict.ESCALATE: "escalated to a human",
}
VERDICT_TONE = {
    CritiqueVerdict.REVISE: "orange",
    CritiqueVerdict.ESCALATE: "orange",  # both push toward a person, like needs review
}


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


def quarantined(result: InvoiceRunResult) -> bool:
    """Whether this run was stopped by the prompt-safety gate at ingestion.

    A quarantined document never reaches the Extractor — that is the point of
    the gate — so the run has no invoice at all. Every pane that would show
    invoice fields has to say *why* it cannot, or it reads as a broken run.
    """
    return (
        result.invoice is None
        and result.validation is not None
        and any(i.code == IssueCode.PROMPT_INJECTION_ATTEMPT for i in result.validation.issues)
    )


#: The learning walkthrough: two vendor histories, run one invoice at a time.
#: The full story, including the arithmetic, is in the directory's own README.
DEMO_DIR = PROJECT_ROOT / "data" / "demo" / "precedent"
DEMO_TRACKS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "Track A — Fabrikam GmbH bills in EUR",
        "A hard review rule today. Three human approvals settle it; the fourth "
        "invoice is paid without anyone being asked, and the fifth is far too "
        "large for what those approvals established.",
        [
            ("invoice_3001.txt", "INV-3001"),
            ("invoice_3002.json", "INV-3002"),
            ("invoice_3003.csv", "INV-3003"),
            ("invoice_3004.xml", "INV-3004"),
            ("invoice_3005.txt", "INV-3005"),
        ],
    ),
    (
        "Track B — Northwind Traders round each line",
        "Their totals sit two cents off. Almost nothing is at risk, so one "
        "human answer is enough — and still does not cover the $412 gap on the "
        "third, which the machine's own approval does nothing to excuse.",
        [
            ("invoice_4001.txt", "INV-4001"),
            ("invoice_4002.csv", "INV-4002"),
            ("invoice_4003.json", "INV-4003"),
        ],
    ),
]

#: st.session_state is new to this app and used only where a local cannot work.
#: @st.dialog wraps its body in a fragment, so a widget inside the modal reruns
#: only the modal — the `if st.button(...)` that opened it is never
#: re-evaluated, and any full rerun would otherwise close it mid-upload.
UPLOAD_OPEN, PROBES = "inbox.upload_open", "inbox.probes"
#: How many uploads had finished when the page was last drawn in full. The
#: Inbox polls inside a fragment, so a run landing mid-batch redraws that table
#: and nothing else — the two queues, All runs and the counts in the tab labels
#: all still show the database as it was when the batch was queued. Comparing
#: against this is how the fragment knows a run has landed and takes the whole
#: page with it.
INBOX_LANDED = "inbox.landed"

#: The tabs in order, by a name that never changes. Streamlit identifies a tab
#: by its *whole* label, and these labels carry live counts: when a count
#: changes, the label the browser has selected no longer exists and it drops
#: back to the first tab. Harmless when a rerun was a click the person just
#: made — but now that every landing run reruns the page, it would yank them
#: out of whatever they were reading every few seconds during a batch. So the
#: name is the identity: remembered on each switch, handed back to st.tabs as
#: `default` on every run, with only the count after it moving.
TAB_NAMES = (
    "📥 Inbox",
    "🟡 Needs review",
    "⛔ Rejected",
    "🎓 Learning",
    "📚 All runs",
    "🗄 Database",
)
TAB_KEY, ACTIVE_TAB = "tabs.main", "tabs.active"


def remember_tab() -> None:
    """Record which tab a person moved to, by name rather than by label."""
    label = st.session_state.get(TAB_KEY) or ""
    st.session_state[ACTIVE_TAB] = next(
        (name for name in TAB_NAMES if label.startswith(name)), TAB_NAMES[0]
    )


settings = Settings()
db = Database(settings.db_path)
store = RunStore(settings.runs_db_path)
results = store.load_results()

title_col, upload_col = st.columns([5, 1], vertical_alignment="bottom")
title_col.title("🧾 ACME Corporation - InvoiceFlow")
title_col.caption("Automated invoice processing — upload, validate, approve, pay.")
if upload_col.button("📤 Upload Invoices", type="primary", width="stretch", key="open-upload"):
    st.session_state[UPLOAD_OPEN] = True

if not settings.db_path.exists():
    st.warning("Inventory database not found. Run `python main.py --init-db` to set it up.")


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


def verdict(label: str, outcome: str, tone: str) -> None:
    """One line: who ruled, and how. Streamlit's :color[] markdown."""
    st.markdown(f"{label} — :{tone}[**{outcome}**]")


def render_verdicts(result: InvoiceRunResult) -> None:
    """The short version of every ruling on this invoice.

    Deliberately terse: the reasoning behind each of these is in the activity
    log, and a wall of prose here buries the one thing a reviewer needs, which
    is who decided what.
    """
    if result.validation is not None:
        issues = result.validation.issues
        critical = sum(1 for i in issues if i.severity == Severity.CRITICAL)
        warnings = sum(1 for i in issues if i.severity == Severity.WARNING)
        if critical:
            verdict("Validation", f"{critical} critical", "red")
        elif warnings:
            verdict("Validation", f"{warnings} warning{'s' if warnings > 1 else ''}", "orange")
        else:
            verdict("Validation", "passed", "green")

    if result.decision is not None:
        status = result.decision.status
        verdict("Approver", status.replace("_", " "), DECISION_TONE[status])

    for i, round_ in enumerate(result.critique_rounds, 1):
        label = "Critic" if len(result.critique_rounds) == 1 else f"Critic {i}"
        outcome = round_.critique.verdict
        # An affirm inherits the tone of the decision it upheld: "Approver —
        # rejected (red) / Critic — upheld the decision (green)" would read as
        # the Critic clearing an invoice it in fact refused.
        tone = VERDICT_TONE.get(outcome) or DECISION_TONE[round_.decision.status]
        verdict(label, CRITIQUE_OUTCOME[outcome], tone)

    for override in result.overrides:
        # The system replacing an agent's call is a ruling in its own right.
        verdict(
            "Override",
            f"{override.kind.replace('_', ' ')} → {override.to_status.replace('_', ' ')}",
            DECISION_TONE[override.to_status],
        )

    if result.payment is not None:
        # `amount` on a declined payment is what was claimed and refused, not
        # what moved — rendering both alike is how money that never left the
        # bank reads as a completed payment.
        pay = result.payment
        if pay.status == PaymentStatus.SUCCESS:
            verdict("Payment", f"paid ${pay.amount:,.2f} to {pay.vendor}", "green")
        else:
            verdict("Payment", f"declined, ${pay.amount:,.2f} not sent", "red")

    for cite in store.citations_for(result.run_id):
        # What history was asked, and what it answered. Rendered even when it
        # answered "not enough" — an escalation nobody can account for is the
        # thing this whole feature is supposed to stop producing.
        about = f"`{cite['code']}`" + (f" '{cite['subject']}'" if cite["subject"] else "")
        if cite["released"]:
            verdict(
                "Precedent",
                f"{cite['cases']} prior approval(s) settled {about} — "
                f"support {cite['support']:.2f} vs burden {cite['burden']:.2f}, "
                "review discharged",
                "green",
            )
        elif cite["blocked_by"]:
            verdict("Precedent", f"{about} — {cite['blocked_by']}", "orange")
        elif cite["cases"] or cite["rejections"]:
            verdict(
                "Precedent",
                f"{cite['cases']} prior approval(s) on {about}, not enough — "
                f"support {cite['support']:.2f} against a burden of {cite['burden']:.2f}"
                + (f"; {cite['rejections']} prior rejection(s)" if cite["rejections"] else ""),
                "orange",
            )

    for hr in result.human_reviews:
        verdict("Human review", hr.action.replace("_", " "), "blue")


def render_run(result: InvoiceRunResult, *, actionable: bool = False, scope: str = "runs") -> None:
    inv = result.invoice
    running = in_flight(result)
    if running:
        # Everything below is empty for a run that has not landed: finish_run
        # writes the invoice, validation, decision and trace in one transaction
        # at the end. Say so, rather than rendering a screen of blanks under
        # "run did not complete" — which reads as a failure and is not one.
        st.info(f"Still processing — started {result.started_at}. Progress is in the Inbox.")
    left, right = st.columns([3, 2])
    with left:
        if inv is None and quarantined(result):
            # The gate fires before the Extractor, so there is no invoice to
            # show and never was one — by design. Say that here, where the
            # table would be, rather than leaving the pane empty and letting it
            # read as a run that broke.
            st.warning(
                "**Quarantined at ingestion.** This document forged the pipeline's own "
                "prompt fences, so it was stopped before the Extractor and never shown to "
                "a language model. Nothing was extracted from it — read the original "
                "below and decide by hand."
            )
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
            st.markdown("**Checks**")
            if result.validation.issues:
                for issue in result.validation.issues:
                    st.markdown(f"{SEVERITY_ICON[issue.severity]} `{issue.code}` — {issue.detail}")
            else:
                st.markdown("🟢 All checks passed")
        render_verdicts(result)

    # Open by default when there is no invoice above it: for a quarantined run
    # the document itself is the only thing a reviewer can judge.
    with st.expander("Original document", expanded=inv is None and not running):
        render_source(result, scope)

    with st.expander("Activity log"):
        # Every agent's reasoning in full, in the order it happened. The pane
        # above says what was concluded; this says why.
        if result.validation is not None and result.validation.summary:
            st.markdown(f"**Validator** — {result.validation.summary}")
        if result.decision is not None:
            st.markdown(f"**Approver** — {result.decision.reasoning}")
        for i, r in enumerate(result.critique_rounds, 1):
            st.markdown(f"**Critic, round {i}** — {r.critique.verdict}: {r.critique.feedback}")
        for override in result.overrides:
            st.markdown(f"**Override** — {override.reasoning}")
        for hr in result.human_reviews:
            st.markdown(
                f"**Human review** — {hr.action.replace('_', ' ')} by {hr.reviewer} at "
                f"{hr.reviewed_at}" + (f": {hr.note}" if hr.note else "")
            )
        if result.trace:
            st.divider()
            for ev in result.trace:
                st.text(f"[{ev.stage:>10}] {ev.event}  {ev.detail}")

    if actionable:
        # A rejection is already decided, so the same two buttons mean different
        # things there: pay it after all, or put a person's name to the refusal.
        rejected = result.final_status == FinalStatus.REJECTED
        pay_label = "✅ Overturn & pay" if rejected else "✅ Approve & pay"
        reject_label = "⛔ Confirm rejection" if rejected else "⛔ Reject"
        if result.human_reviewed_at:
            st.caption(f"Reviewed {result.human_reviewed_at}")
        note = st.text_input("Note", key=f"note-{result.run_id}", placeholder="Optional")
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


#: All-runs list: relative widths for status, invoice, vendor, amount, started, action.
RUN_COLUMNS = [1.6, 1.2, 3.0, 1.6, 1.6, 0.9]
#: run_ids whose detail is currently expanded.
OPEN_RUNS = "runs.open"


def run_fields(result: InvoiceRunResult) -> tuple[str, str, str, str, str]:
    """The five cells of one row in All runs."""
    inv = result.invoice
    badge = IN_FLIGHT_BADGE if in_flight(result) else STATUS_BADGE[result.final_status]
    number = inv.invoice_number if inv else "—"
    who = (inv.vendor or "unknown vendor") if inv else Path(result.source_file_path).name
    amount = f"{inv.currency} {inv.total:,.2f}" if inv and inv.total else "—"
    return badge, number, who, amount, result.started_at[:16].replace("T", " ")


SOURCE_LANGUAGE = {"json": "json", "xml": "xml", "csv": "csv", "txt": None, "pdf": None}
SOURCE_MIME = {
    "json": "application/json",
    "xml": "application/xml",
    "csv": "text/csv",
    "txt": "text/plain",
    "pdf": "application/pdf",
}


def shown_path(path: Path) -> str:
    """A source path as a reader can place it: relative to the project when it
    lives there, absolute when it does not."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def render_source(result: InvoiceRunResult, scope: str) -> None:
    """The document as it arrived, and as the Extractor read it.

    Two different things for a PDF, so both are offered: the page itself, and
    the text pdfplumber pulled out of it. When a total on screen disagrees with
    the invoice, that gap is usually the answer.
    """
    # `scope` disambiguates the download key: one run can be on screen twice —
    # a rejected invoice appears in its own tab and in All runs — and a widget
    # key has to be unique across the whole page, not just its section.
    doc = store.document_for_run(result.run_id)
    path = Path(result.source_file_path)

    # Where the file came from, for anyone reconciling a run against the disk.
    # Here rather than in the heading above: it is a server-side detail, and
    # for a run with no invoice it used to be the *entire* title.
    st.caption(f"Source: `{shown_path(path)}`")
    if path.exists():
        suffix = path.suffix.lstrip(".").lower()
        st.download_button(
            f"Download {path.name}",
            path.read_bytes(),
            file_name=path.name,
            mime=SOURCE_MIME.get(suffix, "application/octet-stream"),
            key=f"download-{scope}-{result.run_id}",
        )
        if suffix == "pdf":
            try:
                st.pdf(path.read_bytes(), height=600)
            except StreamlitAPIException:
                # st.pdf needs the optional streamlit[pdf] extra. Missing it
                # should cost the preview, not the whole detail pane — the
                # download and the extracted text below still work.
                st.caption("Install `streamlit[pdf]` to preview the page inline.")
    else:
        st.caption("The original file is no longer on disk.")

    if doc is None:
        st.caption("Nothing was recorded — this run failed before it could read the file.")
        return
    if doc.file_format == "pdf":
        st.caption(f"Text extracted from the PDF · {doc.char_count:,} characters")
    else:
        st.caption(f"{doc.file_format.upper()} · {doc.char_count:,} characters")
    st.code(doc.raw_text, language=SOURCE_LANGUAGE.get(doc.file_format), height=400)


def toggle_run(run_id: str) -> None:
    """Open or close one row's detail.

    An on_click callback rather than the button's return value: Streamlit runs
    callbacks *before* the rerun, so the label and the pane below it both see
    the new state on the same pass. Reading the return value instead would
    leave the button saying "View" over an already-open detail.
    """
    st.session_state.setdefault(OPEN_RUNS, set()).symmetric_difference_update({run_id})


@st.fragment
def run_row(result: InvoiceRunResult) -> None:
    """One row of All runs, plus its detail when open.

    A fragment so that opening a row reruns only this row. Without it every
    click re-executes the whole script, which reloads all runs from SQLite and
    redraws four other tabs to reveal one pane.
    """
    is_open = result.run_id in st.session_state.setdefault(OPEN_RUNS, set())
    row = st.columns(RUN_COLUMNS, vertical_alignment="center")
    for col, value in zip(row, run_fields(result), strict=False):
        col.markdown(value)
    row[-1].button(
        "Hide" if is_open else "View",
        key=f"toggle-{result.run_id}",
        on_click=toggle_run,
        args=(result.run_id,),
        type="secondary",  # bordered; tertiary renders as a bare link
        width="stretch",
    )
    if is_open:
        with st.container(border=True):
            render_run(result)


def run_header(result: InvoiceRunResult) -> str:
    """The title of one run's panel.

    Without an invoice there is no number and no vendor to name it by — a
    quarantined document was never extracted, and a failed one never got that
    far. The file it arrived as is all that is left, so use its name and say
    why it is standing in. The full path is a server-side detail: it belongs in
    the source pane below, not in a heading.
    """
    if result.invoice:
        return f"{result.invoice.invoice_number} · {result.invoice.vendor or 'unknown vendor'}"
    name = Path(result.source_file_path).name or "unnamed document"
    return f"{name} · {'quarantined, never extracted' if quarantined(result) else 'not extracted'}"


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
        st.warning("Processing is unavailable — no API key configured. Files will queue.")
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
        st.caption(f"Accepted: {', '.join(sorted(e.lstrip('.') for e in SUPPORTED_EXTENSIONS))}")
        if files and st.button("Continue", type="primary", key="inbox-check"):
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
        if st.button("Load sample invoices", key="inbox-samples"):
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
            st.error(f"**{probe.filename}** — cannot be read. {probe.error}")
        elif probe.is_rerun:
            st.warning(
                f"**{probe.filename}** — already processed {probe.prior_runs} time(s) before. "
                "Processing it again will reach the same result."
            )
        else:
            st.success(f"**{probe.filename}** — {probe.byte_size:,} bytes")

    fresh = [p for p in readable if not p.is_rerun]
    a, b, c = st.columns(3)
    if fresh and a.button(f"Upload {len(fresh)} new", type="primary", key="inbox-queue-new"):
        queue_files(fresh, discard=[p for p in probes if p not in fresh])
    if readable and b.button(f"Upload all {len(readable)}", key="inbox-queue-all"):
        queue_files(readable, discard=[p for p in probes if not p.readable])
    if c.button("Cancel", key="inbox-cancel"):
        queue_files([], discard=probes)


if st.session_state.get(UPLOAD_OPEN):
    upload_dialog()

queue = store.review_queue()
rejected = store.rejected_runs()
unchecked = sum(1 for r in rejected if not r.human_reviewed_at)
inbox_counts = store.inbox_counts()
pending = inbox_counts.get("queued", 0) + inbox_counts.get("processing", 0)
# Everything below this line is drawn from the database as it is right now, so
# this is the mark the Inbox fragment polls against.
st.session_state[INBOX_LANDED] = inbox_counts.get("processed", 0) + inbox_counts.get("failed", 0)
# run_every is fixed when the fragment is *declared*, and the browser rebuilds
# its auto-rerun timers from scratch on every full app run — nothing the server
# sends later can cancel one. So recomputing it here is how the polling both
# starts and, via the st.rerun() at the end of the fragment, stops.
refresh = 2.0 if pending else None

learned = store.learned_precedent()
#: Latest run per invoice number. `load_results` is newest-first, so the first
#: sighting of a number is its current run — later ones are superseded.
latest_by_number: dict[str, InvoiceRunResult] = {}
for r in results:
    if r.invoice is not None:
        latest_by_number.setdefault(r.invoice.invoice_number, r)

tab_counts = (
    f"({pending} in flight)" if pending else "",
    f"({len(queue)})" if queue else "",
    f"({unchecked} unchecked)" if unchecked else "",
    f"({len(learned)})" if learned else "",
    "",  # All runs
    "",  # Database
)
tab_labels = [f"{name} {count}".rstrip() for name, count in zip(TAB_NAMES, tab_counts, strict=True)]
active_tab = st.session_state.get(ACTIVE_TAB) or TAB_NAMES[0]
tab_inbox, tab_queue, tab_rejected, tab_learning, tab_runs, tab_db = st.tabs(
    tab_labels,
    key=TAB_KEY,
    # Stateful, which costs a rerun per tab click: it is the only way the
    # server learns which tab is open, and therefore which one to hold when a
    # background run relabels them all.
    on_change=remember_tab,
    default=next((label for label in tab_labels if label.startswith(active_tab)), tab_labels[0]),
)

with tab_inbox:
    st.caption("Uploaded files, processed in the background one at a time.")

    @st.fragment(run_every=refresh)
    def inbox_panel() -> None:
        rows = store.inbox_rows()
        if not rows:
            st.info("No uploads yet. Use 📤 Upload, top right.")
            return
        finished = [r for r in rows if r["state"] in ("processed", "failed")]
        if finished and st.button("Clear all", key="inbox-clear-all"):
            for r in finished:
                store.dismiss_upload(r["id"])
            st.rerun()
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
                    # A string, like every other cell: a float here and "—"
                    # there makes one column two types, which Arrow refuses to
                    # serialise the moment a queued row sits beside a finished
                    # one.
                    "seconds": f"{r['duration_ms'] / 1000:.1f}" if r["duration_ms"] else "—",
                    "cost": f"${r['cost_usd']:.4f}" if r["cost_usd"] else "—",
                    "note": r["error"] or ("Duplicate submission" if r["prior_runs"] else ""),
                }
                for r in rows
            ],
            width="stretch",
            hide_index=True,
        )
        # Counts rather than `rows`, which is capped: this has to compare like
        # with like against the snapshot the full run took.
        by_state = store.inbox_counts()
        landed = by_state.get("processed", 0) + by_state.get("failed", 0)
        still_arriving = by_state.get("queued", 0) + by_state.get("processing", 0)
        if refresh and (landed != st.session_state.get(INBOX_LANDED, landed) or not still_arriving):
            # A run landed, so the rest of the page is out of date: a finished
            # invoice belongs in Needs review and All runs *now*, not when its
            # batch is done. Only a full app run can put it there — everything
            # outside this fragment was rendered before that run existed.
            # It is also the only thing that takes the browser's polling timer
            # down, which is what the drained case is for.
            st.rerun()

    inbox_panel()

with tab_queue:
    if not queue:
        st.success("Nothing needs review.")
    else:
        st.caption("These need a decision before any money moves.")
    for result in queue:
        with st.container(border=True):
            st.subheader(run_header(result))
            render_run(result, actionable=True, scope="queue")

with tab_rejected:
    # Kept out of the escalation queue on purpose: the queue means "this needs a
    # decision from you", these are decided and merely open to being overturned.
    st.caption("Rejected automatically. Overturn one that is wrong, or confirm it.")
    if not rejected:
        st.info("No rejected runs.")
    for result in rejected:
        mark = "" if result.human_reviewed_at else " · 🔎 unchecked"
        with st.container(border=True):
            st.subheader(run_header(result) + mark)
            render_run(result, actionable=True, scope="rejected")

with tab_learning:
    st.caption(
        "Findings a person has settled often enough that the pipeline stops asking. "
        "Only human decisions count — an automatic approval is never evidence for the next one."
    )
    if not learned:
        st.info(
            "Nothing learned yet. Every finding somebody settles in the two queues shows up "
            "here, and the walkthrough below is the short version of it happening."
        )
    else:
        st.dataframe(
            [
                {
                    "finding": row["code"],
                    "about": row["subject"] or "the vendor's practice",
                    "vendor": row["vendor"],
                    "approved by a person": row["approvals"],
                    "rejected by a person": row["rejections"],
                    "largest approved": f"{row['largest_approved']:,.2f}"
                    if row["largest_approved"]
                    else "—",
                    "last reviewed": (row["last_reviewed_at"] or "")[:16].replace("T", " "),
                    "invoices": row["invoices"] or "",
                }
                for row in learned
            ],
            width="stretch",
            hide_index=True,
        )

    st.divider()
    st.markdown("#### Walkthrough")
    st.caption(
        "Eight invoices, all legitimate, none clean. One runs at a time and the next unlocks "
        "when you have dealt with the last — that step is the demo. Full arithmetic in "
        "`data/demo/precedent/README.md`."
    )
    if not DEMO_DIR.exists():
        st.warning(f"The demo invoices are missing from {DEMO_DIR}.")

    def demo_row(number: str) -> tuple[str, str, bool]:
        """(badge, note, resolved) for one demo invoice."""
        result = latest_by_number.get(number)
        if result is None:
            return "⚪ not run", "", False
        if in_flight(result):
            return IN_FLIGHT_BADGE, "", False
        badge = STATUS_BADGE[result.final_status]
        cite = next(iter(store.citations_for(result.run_id)), None)
        if cite is None:
            note = ""
        elif cite["released"]:
            note = (
                f"decided by the pipeline on {cite['cases']} prior human approval(s) — "
                f"support {cite['support']:.2f} vs burden {cite['burden']:.2f}"
            )
        elif cite["blocked_by"]:
            note = cite["blocked_by"]
        else:
            note = (
                f"history fell short: support {cite['support']:.2f} against a burden of "
                f"{cite['burden']:.2f}"
            )
        if result.human_reviews:
            note = f"a person {result.human_reviews[-1].action.replace('_', ' ')}d it. " + note
        # Resolved means "you have done your part", not "it went well": a
        # rejection unlocks the next invoice exactly as an approval does.
        resolved = result.final_status in (FinalStatus.PAID, FinalStatus.REJECTED)
        return badge, note, resolved

    for title, blurb, files in DEMO_TRACKS:
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(blurb)
            next_up: tuple[str, str] | None = None
            blocked_on: str | None = None
            for filename, number in files:
                badge, note, resolved = demo_row(number)
                started = number in latest_by_number
                cols = st.columns([1.4, 1.2, 4.4], vertical_alignment="center")
                cols[0].markdown(badge)
                cols[1].markdown(f"`{number}`")
                cols[2].caption(note or filename)
                if not started and next_up is None and blocked_on is None:
                    next_up = (filename, number)
                if started and not resolved and blocked_on is None:
                    blocked_on = number
            if blocked_on:
                st.info(
                    f"{blocked_on} is waiting on you — decide it in 🟡 Needs review, then come "
                    "back. Its answer is what the next invoice is weighed against."
                )
            elif next_up is None:
                st.success("Track complete.")
            elif pending:
                st.caption("Something is already processing — the Inbox tab has it.")
            else:
                filename, number = next_up
                if st.button(f"▶ Process {number}", key=f"demo-{number}", type="primary"):
                    probe = inbox.probe_upload(store, DEMO_DIR / filename)
                    if not probe.readable:
                        st.error(f"{filename} cannot be read. {probe.error}")
                    else:
                        try:
                            inbox.enqueue(store, probe, source="samples")
                        except sqlite3.IntegrityError:
                            # stored_path is UNIQUE, and dismissing an inbox row
                            # keeps it. Re-running the walkthrough is a database
                            # reset, which is the honest answer for a demo whose
                            # whole subject is accumulated history.
                            st.error(
                                f"{filename} has been queued before. Start the walkthrough "
                                "over with `python main.py --reset-db --init-db`."
                            )
                        else:
                            if worker is not None:
                                worker.wake()
                            st.rerun()

with tab_runs:
    if not results:
        # The old st.stop() lived at the top of the script and hid the whole
        # app in this state — including the upload button that fixes it.
        st.info("No runs yet. Use 📤 Upload to add invoices, or load the samples.")
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
        help="Successful payments only. A declined payment is not money that moved.",
    )
    if results:
        # st.columns rather than one padded label per row: a column layout
        # aligns in the page's own font, where padding a string only lines up
        # inside a monospace code span.
        head = st.columns(RUN_COLUMNS, vertical_alignment="bottom")
        names = ("Status", "Invoice", "Vendor", "Amount", "Started")
        for col, name in zip(head, names, strict=False):
            col.caption(name)
        st.divider()

    for r in results:
        run_row(r)

with tab_db:
    st.markdown("**Inventory**")
    st.dataframe(
        [vars(rec) for rec in db.all_items()] if settings.db_path.exists() else [],
        width="stretch",
        hide_index=True,
    )
    st.markdown("**Processed invoices**")
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM invoice_registry").fetchall()]
    st.dataframe(rows, width="stretch", hide_index=True)

    st.markdown("**Cost by agent**")
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM v_cost_by_agent").fetchall()]
    st.dataframe(rows, width="stretch", hide_index=True)

    st.markdown("**Most common issues**")
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM v_issue_frequency").fetchall()]
    st.dataframe(rows, width="stretch", hide_index=True)

    reprocessed = []
    with store.connect() as conn:
        reprocessed = [
            dict(r) for r in conn.execute("SELECT * FROM v_reprocessed_documents").fetchall()
        ]
    if reprocessed:
        st.markdown("**Duplicate submissions**")
        st.dataframe(reprocessed, width="stretch", hide_index=True)
