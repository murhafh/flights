from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Deal


class NotifierError(RuntimeError):
    """A notifier could not be configured or could not deliver."""


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, deals: list[Deal]) -> None: ...


def summarize(deals: list[Deal]) -> str:
    """One-line subject/heading for a batch of deals."""
    if not deals:
        return "No award deals"
    best = min(deals, key=lambda deal: deal.availability.miles)
    noun = "deal" if len(deals) == 1 else "deals"
    return (
        f"{len(deals)} award {noun} - best {best.availability.miles:,} miles "
        f"{best.availability.origin}->{best.availability.destination} "
        f"({best.availability.cabin.label})"
    )


def format_taxes(deal_or_availability) -> str:  # noqa: ANN001 - accepts Availability
    """Render cash surcharges. Amounts under $10 keep their cents: those are the
    fee-only awards, and the difference between $5.60 and $749 on otherwise identical
    space is the whole decision."""
    availability = getattr(deal_or_availability, "availability", deal_or_availability)
    if availability.taxes is None:
        return "-"
    currency = availability.taxes_currency or "USD"
    amount = (
        f"{availability.taxes:,.2f}" if availability.taxes < 10 else f"{availability.taxes:,.0f}"
    )
    return f"{amount} {currency}"


def format_deal_line(deal: Deal) -> str:
    availability = deal.availability
    parts = [
        f"{availability.origin}->{availability.destination}",
        f"{availability.depart:%a %d %b %Y}",
        availability.cabin.label,
        f"{availability.miles:,} mi",
    ]
    if availability.taxes is not None:
        parts.append(f"+{format_taxes(availability)}")
    parts.append(availability.source)
    parts.append("nonstop" if availability.direct else "1+ stop")
    if availability.seats is not None:
        parts.append(f"{availability.seats} seat{'s' if availability.seats != 1 else ''}")
    if availability.airlines:
        parts.append("/".join(availability.airlines))
    if deal.is_improvement:
        parts.append(f"down {deal.previous_miles - availability.miles:,} from last alert")
    return " | ".join(parts)


def format_return_line(deal: Deal) -> str | None:
    """One line summarising the cheapest paired return and the round-trip total."""
    best = deal.best_return
    if best is None:
        return None

    parts = [
        f"return {best.origin}->{best.destination}",
        f"{best.depart:%a %d %b %Y}",
        f"{best.miles:,} mi",
    ]
    if best.taxes is not None:
        parts.append(f"+{format_taxes(best)}")
    parts.append(best.source)
    parts.append("nonstop" if best.direct else "1+ stop")

    total = f"round trip {deal.round_trip_miles:,} mi"
    if deal.round_trip_taxes is not None:
        currency = best.taxes_currency or "USD"
        total += f" + {deal.round_trip_taxes:,.2f} {currency}"
    extra = f", {deal.returns_total - 1} other date(s)" if deal.returns_total > 1 else ""
    return f"{' | '.join(parts)}  [{total}{extra}]"


def format_deals_text(deals: list[Deal]) -> str:
    """Plain-text body shared by the email and Telegram notifiers."""
    lines: list[str] = []
    by_watch: dict[str, list[Deal]] = {}
    for deal in deals:
        by_watch.setdefault(deal.watch_name, []).append(deal)

    for watch_name, group in by_watch.items():
        lines.append(f"{watch_name}")
        for deal in group:
            lines.append(f"  - {format_deal_line(deal)}")
            if (return_line := format_return_line(deal)) is not None:
                lines.append(f"    {return_line}")
            # Omitted entirely when the provider gave us no link, rather than printing
            # a fabricated URL that 404s.
            if deal.availability.booking_url:
                lines.append(f"    {deal.availability.booking_url}")
        lines.append("")

    lines.append("Availability is cached and moves fast - verify before counting on it.")
    return "\n".join(lines)
