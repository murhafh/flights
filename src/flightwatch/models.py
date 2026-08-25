"""Core domain types.

The normalized shape here follows seats.aero's availability schema fairly closely,
since anything else we plug in later can be mapped onto it more easily than the
reverse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Cabin(StrEnum):
    ECONOMY = "economy"
    PREMIUM = "premium"
    BUSINESS = "business"
    FIRST = "first"

    @property
    def code(self) -> str:
        """Single-letter fare code used as a field prefix in seats.aero responses."""
        return _CABIN_CODES[self]

    @property
    def label(self) -> str:
        return _CABIN_LABELS[self]


_CABIN_CODES = {
    Cabin.ECONOMY: "Y",
    Cabin.PREMIUM: "W",
    Cabin.BUSINESS: "J",
    Cabin.FIRST: "F",
}

_CABIN_LABELS = {
    Cabin.ECONOMY: "Economy",
    Cabin.PREMIUM: "Premium",
    Cabin.BUSINESS: "Business",
    Cabin.FIRST: "First",
}


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """One provider lookup: a set of O&Ds over a departure-date window."""

    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    depart_after: date
    depart_before: date
    cabins: tuple[Cabin, ...]
    programs: tuple[str, ...] = ()
    """Mileage programs to restrict to. Empty means whatever the provider covers."""


@dataclass(frozen=True, slots=True)
class Availability:
    """A single bookable-looking award: one route, one date, one cabin, one program.

    Providers return records covering all four cabins at once; we fan those out into
    one `Availability` per available cabin so threshold rules stay simple.
    """

    source: str
    """Mileage program, e.g. "aeroplan"."""
    origin: str
    destination: str
    depart: date
    cabin: Cabin
    miles: int
    direct: bool
    record_id: str = ""
    seats: int | None = None
    taxes: float | None = None
    taxes_currency: str | None = None
    airlines: tuple[str, ...] = ()
    distance: int | None = None
    updated_at: datetime | None = None
    booking_url: str = ""
    """Where to go to actually book this. Populated by the provider, which owns
    knowledge of its own site's URL format. Empty means "no link available" and
    notifiers omit it rather than emitting something broken."""
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        """Stable identity for dedupe. Deliberately excludes miles and seat count so a
        price change on the same award is recognized as the same award."""
        return (
            f"{self.source}:{self.origin}-{self.destination}"
            f":{self.depart:%Y-%m-%d}:{self.cabin.code}"
        )

    @property
    def miles_per_flown_mile(self) -> float | None:
        """Redemption rate per mile flown. `distance` is statute miles, not km."""
        if not self.distance:
            return None
        return self.miles / self.distance


@dataclass(frozen=True, slots=True)
class Deal:
    """An availability that cleared a threshold, plus why it's worth telling you about."""

    availability: Availability
    watch_name: str
    max_miles: int
    """The threshold it came in under, for context in the alert."""
    previous_miles: int | None = None
    """What we last alerted at for this key, if anything."""
    returns: tuple[Availability, ...] = ()
    """Qualifying return legs, cheapest first. Empty when none were found, or when the
    watch has no return_trip configured -- the outbound alert stands on its own either way."""
    returns_total: int = 0
    """How many returns qualified, which may exceed len(returns) since that is capped."""

    @property
    def savings(self) -> int:
        return self.max_miles - self.availability.miles

    @property
    def is_improvement(self) -> bool:
        return self.previous_miles is not None and self.availability.miles < self.previous_miles

    @property
    def best_return(self) -> Availability | None:
        return self.returns[0] if self.returns else None

    @property
    def round_trip_miles(self) -> int | None:
        best = self.best_return
        return None if best is None else self.availability.miles + best.miles

    @property
    def round_trip_taxes(self) -> float | None:
        """Combined surcharges. None only when neither leg reported a figure."""
        best = self.best_return
        if best is None:
            return None
        amounts = [t for t in (self.availability.taxes, best.taxes) if t is not None]
        return sum(amounts) if amounts else None
