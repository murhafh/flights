from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..models import Deal
from .base import format_taxes, summarize


class ConsoleNotifier:
    name = "console"

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def send(self, deals: list[Deal]) -> None:
        if not deals:
            self._console.print("[dim]No deals under threshold.[/dim]")
            return

        table = Table(title=summarize(deals), title_justify="left", header_style="bold")
        table.add_column("Route", no_wrap=True)
        table.add_column("Depart", no_wrap=True)
        table.add_column("Cabin", no_wrap=True)
        table.add_column("Miles", justify="right", no_wrap=True)
        table.add_column("Taxes", justify="right", no_wrap=True)
        table.add_column("Program")
        table.add_column("Stops", no_wrap=True)
        table.add_column("Seats", justify="right", no_wrap=True)
        table.add_column("Airlines")
        # Only shown when at least one deal has a paired return, to keep the one-way
        # case as narrow as it was.
        show_returns = any(deal.returns for deal in deals)
        if show_returns:
            table.add_column("Return", no_wrap=True)
            table.add_column("Round trip", justify="right", no_wrap=True)

        for deal in deals:
            availability = deal.availability
            taxes = format_taxes(availability)
            # Highlight how far under the threshold it came in.
            margin = "green" if deal.savings >= 10_000 else "default"
            miles = f"[{margin}]{availability.miles:,}[/{margin}]"
            if deal.is_improvement:
                miles += f" [green]-{deal.previous_miles - availability.miles:,}[/green]"
            row = [
                f"{availability.origin}->{availability.destination}",
                f"{availability.depart:%Y-%m-%d}",
                availability.cabin.label,
                miles,
                taxes,
                availability.source,
                "nonstop" if availability.direct else "1+",
                str(availability.seats) if availability.seats is not None else "-",
                "/".join(availability.airlines) or "-",
            ]
            if show_returns:
                best = deal.best_return
                if best is None:
                    row += ["-", "-"]
                else:
                    suffix = f" +{deal.returns_total - 1}" if deal.returns_total > 1 else ""
                    row.append(f"{best.depart:%m-%d} {best.miles:,}{suffix}")
                    row.append(f"[bold]{deal.round_trip_miles:,}[/bold]")
            table.add_row(*row)

        self._console.print(table)
