"""Threshold evaluation: which availabilities are actually worth an alert."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import timedelta

from .config import Threshold, Watch
from .models import Availability, Deal

MAX_RETURNS_SHOWN = 3
"""Cap on returns attached per outbound. A popular route can have dozens of qualifying
return dates; listing them all would bury the outbound the alert is actually about."""


def _passes(
    availability: Availability,
    threshold: Threshold,
    watch: Watch,
    *,
    origins: Sequence[str],
    destinations: Sequence[str],
) -> bool:
    """Whether one availability clears one threshold.

    `origins`/`destinations` are passed in rather than read off the watch so the same
    rules apply to a return leg, where the route is reversed.
    """
    if availability.cabin is not threshold.cabin:
        return False
    if not threshold.allows(availability.miles):
        return False
    if threshold.nonstop_only and not availability.direct:
        return False
    # A missing tax figure is treated as passing; the alert renders "-" so you can check.
    if (
        threshold.max_taxes is not None
        and availability.taxes is not None
        and availability.taxes > threshold.max_taxes
    ):
        return False
    if availability.seats is not None and availability.seats < threshold.min_seats:
        return False
    # Re-apply the config filters locally: a provider may ignore them server-side.
    if watch.programs and availability.source not in watch.programs:
        return False
    if watch.airlines_exclude and set(availability.airlines) & set(watch.airlines_exclude):
        return False
    if availability.origin not in origins:
        return False
    return availability.destination in destinations


def _qualifies(availability: Availability, watch: Watch, *, reverse: bool = False) -> bool:
    origins = watch.destinations if reverse else watch.origins
    destinations = watch.origins if reverse else watch.destinations
    return any(
        _passes(availability, threshold, watch, origins=origins, destinations=destinations)
        for threshold in watch.thresholds
    )


def _best_first(deal: Deal) -> tuple[int, float, int]:
    """Sort key: fewest miles, then lowest cash surcharge, then nonstop over connecting.

    Surcharges matter as a tiebreak because identical mileage prices routinely differ by
    hundreds of dollars -- the same 57,500-mile award costs $5.60 via one routing and
    $749 via London. A missing figure sorts as zero, matching how thresholds treat it.
    """
    availability = deal.availability
    return (
        availability.miles,
        availability.taxes if availability.taxes is not None else 0.0,
        0 if availability.direct else 1,
    )


def _cheapest_first(availability: Availability) -> tuple[int, float, int]:
    return (
        availability.miles,
        availability.taxes if availability.taxes is not None else 0.0,
        0 if availability.direct else 1,
    )


def evaluate(watch: Watch, availabilities: Iterable[Availability]) -> list[Deal]:
    """Return the best qualifying deal per route-day-cabin for this watch.

    A provider emits both a connecting and a nonstop variant for the same route-day,
    so matches are collapsed to one per `Availability.key` -- otherwise a single piece
    of award space would produce two near-identical alerts.
    """
    best: dict[str, Deal] = {}

    for availability in availabilities:
        for threshold in watch.thresholds:
            if not _passes(
                availability,
                threshold,
                watch,
                origins=watch.origins,
                destinations=watch.destinations,
            ):
                continue
            candidate = Deal(
                availability=availability,
                watch_name=watch.name,
                max_miles=threshold.limit,
            )
            incumbent = best.get(availability.key)
            if incumbent is None or _best_first(candidate) < _best_first(incumbent):
                best[availability.key] = candidate
            break  # first matching threshold for this cabin wins

    return sorted(best.values(), key=_best_first)


def attach_returns(
    watch: Watch,
    deals: list[Deal],
    return_availabilities: Iterable[Availability],
) -> list[Deal]:
    """Pair each outbound deal with qualifying returns in its configured day range.

    Returns are collapsed per route-day-cabin the same way outbounds are, then matched
    to each outbound by departure date. A deal with no qualifying return is returned
    unchanged: the one-way alert still stands.
    """
    if watch.return_trip is None:
        return deals

    # Collapse first so a connecting/nonstop pair doesn't occupy two of the shown slots.
    collapsed: dict[str, Availability] = {}
    for availability in return_availabilities:
        if not _qualifies(availability, watch, reverse=True):
            continue
        incumbent = collapsed.get(availability.key)
        if incumbent is None or _cheapest_first(availability) < _cheapest_first(incumbent):
            collapsed[availability.key] = availability
    candidates = sorted(collapsed.values(), key=_cheapest_first)

    if not candidates:
        return deals

    out: list[Deal] = []
    for deal in deals:
        earliest = deal.availability.depart + timedelta(days=watch.return_trip.min_days)
        latest = deal.availability.depart + timedelta(days=watch.return_trip.max_days)
        matches = [candidate for candidate in candidates if earliest <= candidate.depart <= latest]
        out.append(
            replace(
                deal,
                returns=tuple(matches[:MAX_RETURNS_SHOWN]),
                returns_total=len(matches),
            )
            if matches
            else deal
        )
    return out
