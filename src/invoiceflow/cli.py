"""Command-line interface: single-invoice runs, batch mode, and DB setup."""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import PROJECT_ROOT, Settings
from .db import Database
from .loaders import SUPPORTED_EXTENSIONS
from .models import FinalStatus, InvoiceRunResult, Severity

app = typer.Typer(add_completion=False, rich_markup_mode="rich")
console = Console()

STATUS_STYLE = {
    FinalStatus.PAID: "bold green",
    FinalStatus.REJECTED: "bold red",
    FinalStatus.NEEDS_REVIEW: "bold yellow",
    FinalStatus.DUPLICATE: "bold blue",
    FinalStatus.FAILED: "bold magenta",
}

SEVERITY_STYLE = {Severity.CRITICAL: "red", Severity.WARNING: "yellow", Severity.INFO: "dim"}


@app.command()
def main(
    invoice_path: Path | None = typer.Option(
        None, "--invoice_path", "--invoice-path", help="Process a single invoice file"
    ),
    process_all: bool = typer.Option(
        False, "--all", help="Process every invoice in data/invoices/"
    ),
    init_db: bool = typer.Option(False, "--init-db", help="Create and seed the inventory DB"),
    reset_db: bool = typer.Option(
        False, "--reset-db", help="Drop and recreate the inventory DB and processed registry"
    ),
    model: str | None = typer.Option(None, "--model", help="Grok model name (default: grok-3)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show internal logs"),
) -> None:
    """Multi-agent invoice processing: ingestion -> validation -> approval -> payment."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = Settings(grok_model=model) if model else Settings()
    db = Database(settings.db_path)

    if init_db or reset_db:
        db.init(reset=reset_db)
        console.print(
            f"[green]✓[/green] Inventory database ready at [bold]{settings.db_path}[/bold]"
        )
        for rec in db.all_items():
            console.print(f"   {rec.item}: {rec.stock} in stock @ ${rec.unit_price:,.2f}")
        if not (invoice_path or process_all):
            return

    if not settings.db_path.exists():
        console.print(
            "[red]No inventory database found.[/red] Run with [bold]--init-db[/bold] first."
        )
        raise typer.Exit(1)

    if not (invoice_path or process_all):
        console.print("Nothing to do: pass [bold]--invoice_path FILE[/bold] or [bold]--all[/bold].")
        raise typer.Exit(1)

    from .llm import MissingApiKeyError
    from .pipeline import Pipeline  # deferred: importing langchain is slow

    try:
        pipeline = Pipeline(settings)
    except MissingApiKeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[dim]Reasoning engine:[/dim] [bold]{pipeline.backend}[/bold]")

    if process_all:
        paths = sorted(
            p
            for p in (PROJECT_ROOT / "data" / "invoices").iterdir()
            if p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    else:
        paths = [invoice_path]

    results = []
    for path in paths:
        console.rule(f"[bold]{Path(path).name}[/bold]")
        result = pipeline.run(path)
        _render_run(result)
        results.append(result)

    if len(results) > 1:
        _render_summary(results)


def _render_run(result: InvoiceRunResult) -> None:
    inv = result.invoice
    if inv is not None:
        console.print(
            f"  [bold]{inv.invoice_number}[/bold] · {inv.vendor or '[dim]<no vendor>[/dim]'} · "
            f"total: {_money(inv.total, inv.currency)} · "
            f"due: {inv.due_date or inv.due_date_raw or '—'}"
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
    if result.payment is not None and result.payment.status == "success":
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
            Path(r.source_file).name,
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


def _money(total: float | None, currency: str = "USD") -> str:
    if total is None:
        return "—"
    symbol = "$" if currency.upper() == "USD" else f"{currency} "
    return f"{symbol}{total:,.2f}"


if __name__ == "__main__":
    app()
