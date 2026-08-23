"""Command-line interface: single-invoice runs, batch mode, DB setup, exports."""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import PROJECT_ROOT, Settings, langsmith_project
from .db import Database
from .loaders import SUPPORTED_EXTENSIONS
from .models import FinalStatus, InvoiceRunResult, PaymentStatus, Severity

app = typer.Typer(add_completion=False, rich_markup_mode="rich")
console = Console()

STATUS_STYLE = {
    FinalStatus.PAID: "bold green",
    FinalStatus.REJECTED: "bold red",
    FinalStatus.NEEDS_REVIEW: "bold yellow",
    FinalStatus.DUPLICATE: "bold blue",
    FinalStatus.FAILED: "bold magenta",
}

# Keyed by str, not Severity: trace events carry the severity as plain text
# (Severity is a StrEnum, so the members below are valid str keys).
SEVERITY_STYLE: dict[str, str] = {
    Severity.CRITICAL: "red",
    Severity.WARNING: "yellow",
    Severity.INFO: "dim",
}


@app.command()
def main(
    invoice_path: Path | None = typer.Option(
        None, "--invoice_path", "--invoice-path", help="Process a single invoice file"
    ),
    process_all: bool = typer.Option(
        False, "--all", help="Process every invoice in data/invoices/"
    ),
    init_db: bool = typer.Option(
        False, "--init-db", help="Create and seed the inventory DB + the run store"
    ),
    reset_db: bool = typer.Option(
        False, "--reset-db", help="Drop and recreate both databases (inventory + run store)"
    ),
    export_json: str | None = typer.Option(
        None,
        "--export-json",
        help="Render a recorded run from the DB to results/<run_id>.json ('all' for every run)",
    ),
    export_graph: bool = typer.Option(
        False,
        "--export-graph",
        help="Re-render docs/graph.png from the compiled topology (fetches mermaid.ink)",
    ),
    model: str | None = typer.Option(None, "--model", help="Grok model name (default: grok-4.6)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show internal logs"),
) -> None:
    """Multi-agent invoice processing: ingestion -> validation -> approval -> payment."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = Settings(grok_model=model) if model else Settings()
    db = Database(settings.db_path)

    if export_graph:
        _export_graph(settings, db)
        if not (invoice_path or process_all):
            return

    if init_db or reset_db:
        from .runstore import RunStore

        db.init(reset=reset_db)
        RunStore(settings.runs_db_path).init(reset=reset_db)
        console.print(
            f"[green]✓[/green] Inventory database ready at [bold]{settings.db_path}[/bold]"
        )
        for rec in db.all_items():
            console.print(f"   {rec.item}: {rec.stock} in stock @ ${rec.unit_price:,.2f}")
        console.print(f"[green]✓[/green] Run store ready at [bold]{settings.runs_db_path}[/bold]")
        if not (invoice_path or process_all):
            return

    if export_json is not None:
        _export_json(settings, export_json)
        if not (invoice_path or process_all):
            return

    if not settings.db_path.exists():
        console.print(
            "[red]No inventory database found.[/red] Run with [bold]--init-db[/bold] first."
        )
        raise typer.Exit(1)

    if process_all:
        paths = sorted(
            p
            for p in (PROJECT_ROOT / "data" / "invoices").iterdir()
            if p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    elif invoice_path is not None:
        paths = [invoice_path]
    else:
        console.print("Nothing to do: pass [bold]--invoice_path FILE[/bold] or [bold]--all[/bold].")
        raise typer.Exit(1)

    # deferred: importing langchain is slow
    from .pipeline import MissingApiKeyError, Pipeline

    try:
        pipeline = Pipeline(settings)
    except MissingApiKeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[dim]Reasoning engine:[/dim] [bold]{pipeline.backend}[/bold]")
    if project := langsmith_project():
        console.print(
            f"[dim]LangSmith tracing:[/dim] [bold]{project}[/bold] "
            "[dim](development only — prompts and invoice text leave the machine)[/dim]"
        )

    results = []
    for path in paths:
        console.rule(f"[bold]{Path(path).name}[/bold]")
        result = pipeline.run(path)
        _render_run(result)
        _render_usage(pipeline, result)
        results.append(result)

    if len(results) > 1:
        _render_summary(results)
        calls, tokens, cost = _batch_usage(pipeline, results)
        console.print(
            f"[dim]Batch usage: {calls} LLM call(s) · {tokens:,} tokens"
            + (f" · ${cost:.4f}[/dim]" if cost is not None else "[/dim]")
        )


def _render_usage(pipeline, result: InvoiceRunResult) -> None:
    """One dim line of telemetry per run — the observability the DB now keeps.

    Cost appears only when model_pricing has a row for the backend; a missing
    price renders as nothing rather than as a made-up $0.
    """
    calls, tokens, cost = pipeline.store.run_usage(result.run_id)
    if not calls:
        return
    line = f"  [dim]{calls} LLM call(s) · {tokens:,} tokens"
    if cost is not None:
        line += f" · ${cost:.4f}"
    console.print(line + "[/dim]")


def _batch_usage(pipeline, results: list[InvoiceRunResult]) -> tuple[int, int, float | None]:
    calls = tokens = 0
    cost: float | None = None
    for r in results:
        c, t, usd = pipeline.store.run_usage(r.run_id)
        calls += c
        tokens += t
        if usd is not None:
            cost = (cost or 0.0) + usd
    return calls, tokens, cost


def _export_json(settings: Settings, which: str) -> None:
    """Render recorded runs from the DB to results/*.json — derived artifacts,
    never a second writer (docs/schema.md, phase 6)."""
    from .pipeline import export_result_json
    from .runstore import RunStore

    store = RunStore(settings.runs_db_path)
    run_ids = [r.run_id for r in store.load_results()] if which == "all" else [which]
    if not run_ids:
        console.print("[yellow]No recorded runs to export.[/yellow]")
        return
    for run_id in run_ids:
        try:
            out = export_result_json(store, run_id, settings.results_dir)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
        console.print(f"[green]✓[/green] {out}")


def _render_run(result: InvoiceRunResult) -> None:
    inv = result.invoice
    if inv is not None:
        console.print(
            f"  [bold]{inv.invoice_number}[/bold] · {inv.vendor or '[dim]<no vendor>[/dim]'} · "
            f"total: {_money(inv.total, inv.currency)} · "
            f"due: {inv.due_date or inv.due_date_raw or '—'}"
        )
    if result.document_run_no > 1:
        # Non-blocking by design: a prompt would break --all and scripting, and
        # double payment is already prevented downstream (check_duplicate).
        console.print(
            f"  [yellow]⟳ reprocessing:[/yellow] run #{result.document_run_no} "
            "for this exact document content"
        )
    for ev in result.trace:
        style = "dim"
        if ev.event.startswith("issue:"):
            style = SEVERITY_STYLE.get(ev.event.split(":")[1], "dim")
        console.print(f"  [cyan]{ev.stage:>10}[/cyan] [{style}]{ev.event}[/{style}] {ev.detail}")
    for i, round_ in enumerate(result.critique_rounds, 1):
        console.print(
            f"  [magenta]  critique[/magenta] round {i}: {round_.critique.verdict} — "
            f"{round_.critique.feedback}"
        )

    style = STATUS_STYLE[result.final_status]
    body = f"[{style}]{result.final_status.upper()}[/{style}]"
    if result.decision is not None:
        body += f"\n{result.decision.reasoning}"
    if result.payment is not None and result.payment.status == PaymentStatus.SUCCESS:
        body += (
            f"\n[green]Payment sent:[/green] "
            f"${result.payment.amount:,.2f} → {result.payment.vendor}"
        )
    if result.error:
        body += f"\n[red]{result.error}[/red]"
    console.print(Panel(body, title=f"run {result.run_id}", border_style=style.split()[-1]))


def _render_summary(results: list[InvoiceRunResult]) -> None:
    table = Table(title="Batch summary", show_lines=False)
    table.add_column("File")
    table.add_column("Invoice #")
    table.add_column("Vendor")
    table.add_column("Total", justify="right")
    table.add_column("Status")
    table.add_column("Key findings", max_width=60)
    counts: dict[FinalStatus, int] = {}
    for r in results:
        counts[r.final_status] = counts.get(r.final_status, 0) + 1
        inv = r.invoice
        findings = ""
        if r.validation and r.validation.issues:
            findings = "; ".join(str(i.code) for i in r.validation.issues[:3])
        elif r.error:
            findings = r.error
        style = STATUS_STYLE[r.final_status]
        table.add_row(
            Path(r.source_file_path).name,
            inv.invoice_number if inv else "—",
            (inv.vendor or "—") if inv else "—",
            _money(inv.total, inv.currency) if inv else "—",
            f"[{style}]{r.final_status}[/{style}]",
            findings,
        )
    console.print(table)
    tally = " · ".join(
        f"[{STATUS_STYLE[s]}]{s}: {n}[/{STATUS_STYLE[s]}]" for s, n in sorted(counts.items())
    )
    console.print(f"\n{len(results)} invoice(s) processed → {tally}")


def _export_graph(settings: Settings, db: Database) -> None:
    """Re-render docs/graph.png on demand — the only caller of the exporter.

    Deliberately not part of a run: rendering goes to mermaid.ink, and the case
    allows no external API but Grok. The topology does not depend on the model,
    so this needs no API key — any BaseChatModel compiles the same graph.
    """
    from langchain_core.language_models import FakeListChatModel

    from .graph import build_graph, export_graph_image
    from .runstore import RunStore

    try:
        graph = build_graph(
            settings, db, RunStore(settings.runs_db_path), FakeListChatModel(responses=[])
        )
        written = export_graph_image(graph)
    except Exception as exc:  # mermaid.ink unreachable, render error, unwritable docs/
        console.print(f"[red]Graph export failed:[/red] {exc}")
        raise typer.Exit(1) from None
    if written:
        console.print("[green]✓[/green] Graph diagram exported to [bold]docs/graph.png[/bold]")
    else:
        console.print("[dim]Graph diagram already matches the compiled topology.[/dim]")


def _money(total: float | None, currency: str = "USD") -> str:
    if total is None:
        return "—"
    symbol = "$" if currency.upper() == "USD" else f"{currency} "
    return f"{symbol}{total:,.2f}"


if __name__ == "__main__":
    app()
