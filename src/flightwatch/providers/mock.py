"""Deterministic fake provider.

Generates plausible award space so the full pipeline -- thresholds, dedupe, alert
formatting -- can be exercised without burning API quota. Output is a pure function
of the query, so tests and repeated runs see identical results.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from ..models import Availability, Cabin, SearchQuery

PROGRAMS = ("aeroplan", "flyingblue", "virginatlantic", "alaska", "united", "lifemiles")

# Rough "normal" price per cabin, in miles, for a long-haul redemption.
_TYPICAL_MILES = {
    Cabin.ECONOMY: 35_000,
    Cabin.PREMIUM: 55_000,
    Cabin.BUSINESS: 85_000,
    Cabin.FIRST: 140_000,
}


def _jitter(*parts: object) -> float:
    """Stable pseudo-random float in [0, 1) derived from the given parts."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


class MockProvider:
    name = "mock"

    def close(self) -> None:  # nothing to release
        return

    def _url(self, origin: str, destination: str, depart: date) -> str:
        return f"https://example.invalid/mock?{origin}-{destination}-{depart:%Y%m%d}"

    def search(self, query: SearchQuery) -> list[Availability]:
        programs = query.programs or PROGRAMS
        results: list[Availability] = []
        span = (query.depart_before - query.depart_after).days

        for origin in query.origins:
            for destination in query.destinations:
                for program in programs:
                    for offset in self._offsets(origin, destination, program, span):
                        depart = query.depart_after + timedelta(days=offset)
                        results.extend(self._for_day(origin, destination, program, depart, query))
        return results

    def _offsets(self, origin: str, destination: str, program: str, span: int) -> list[int]:
        """Pick a handful of dates in the window that have any space at all."""
        if span < 0:
            return []
        count = 1 + int(_jitter("days", origin, destination, program) * 3)
        return [
            int(_jitter("offset", origin, destination, program, index) * (span + 1))
            for index in range(count)
        ]

    def _for_day(
        self,
        origin: str,
        destination: str,
        program: str,
        depart: date,
        query: SearchQuery,
    ) -> list[Availability]:
        out: list[Availability] = []
        distance = 4_000 + int(_jitter("dist", origin, destination) * 8_000)

        for cabin in query.cabins:
            seed = ("fare", origin, destination, program, depart.isoformat(), cabin.value)
            if _jitter(*seed) < 0.45:  # most route-days have nothing in a given cabin
                continue

            # Price swings from a deep 45%-off sweet spot up to 30% above typical.
            multiplier = 0.55 + _jitter("mult", *seed) * 0.75
            miles = int(_TYPICAL_MILES[cabin] * multiplier / 500) * 500
            direct = _jitter("direct", *seed) > 0.55

            out.append(
                Availability(
                    source=program,
                    origin=origin,
                    destination=destination,
                    depart=depart,
                    cabin=cabin,
                    miles=miles,
                    direct=False,
                    seats=1 + int(_jitter("seats", *seed) * 4),
                    taxes=round(25 + _jitter("tax", *seed) * 300, 2),
                    taxes_currency="USD",
                    airlines=(self._airline(program, *seed),),
                    distance=distance,
                    record_id=f"mock-{abs(hash(seed)):x}",
                    booking_url=self._url(origin, destination, depart),
                )
            )
            if direct:
                out.append(
                    Availability(
                        source=program,
                        origin=origin,
                        destination=destination,
                        depart=depart,
                        cabin=cabin,
                        # Nonstop usually costs the same or a bit more.
                        miles=miles + int(_jitter("dpremium", *seed) * 4) * 2_500,
                        direct=True,
                        seats=1 + int(_jitter("dseats", *seed) * 3),
                        taxes=round(25 + _jitter("tax", *seed) * 300, 2),
                        taxes_currency="USD",
                        airlines=(self._airline(program, *seed),),
                        distance=distance,
                        record_id=f"mock-{abs(hash(seed)):x}-d",
                        booking_url=self._url(origin, destination, depart),
                    )
                )
        return out

    def _airline(self, program: str, *seed: object) -> str:
        carriers = {
            "aeroplan": ("AC", "LH", "NH", "SN"),
            "flyingblue": ("AF", "KL", "DL"),
            "virginatlantic": ("VS", "DL", "AF"),
            "alaska": ("AS", "AY", "BA", "QR"),
            "united": ("UA", "LH", "SQ"),
            "lifemiles": ("AV", "UA", "LH"),
        }.get(program, ("XX",))
        return carriers[int(_jitter("carrier", program, *seed) * len(carriers))]
