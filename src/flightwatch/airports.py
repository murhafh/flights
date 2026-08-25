"""Airport and metropolitan-area codes.

seats.aero accepts IATA *city* codes in a search (`destinations=LON` returns the same
1805 rows as listing the London airports individually), but the availability records it
returns are keyed by real airport -- LHR, LGW. The route guard in `rules` compares those
against the configured list, so a watch written as `destinations = ["LON"]` would search
correctly and then discard every result: a silent zero that looks identical to "no award
space".

Expanding city codes to their airports at config load keeps the query and the guard
talking about the same thing.
"""

from __future__ import annotations

METRO_AREAS: dict[str, tuple[str, ...]] = {
    "LON": ("LHR", "LGW", "LCY", "STN", "LTN", "SEN"),
    "NYC": ("JFK", "EWR", "LGA"),
    "PAR": ("CDG", "ORY", "BVA"),
    "TYO": ("HND", "NRT"),
    "OSA": ("KIX", "ITM", "UKB"),
    "MIL": ("MXP", "LIN", "BGY"),
    "ROM": ("FCO", "CIA"),
    "STO": ("ARN", "BMA", "NYO"),
    "MOW": ("SVO", "DME", "VKO"),
    "WAS": ("IAD", "DCA", "BWI"),
    "CHI": ("ORD", "MDW"),
    "YTO": ("YYZ", "YTZ", "YHM"),
    "YMQ": ("YUL", "YMX"),
    "SAO": ("GRU", "CGH", "VCP"),
    "RIO": ("GIG", "SDU"),
    "BUE": ("EZE", "AEP"),
    "BJS": ("PEK", "PKX"),
    "SHA": ("PVG", "SHA"),
    "SEL": ("ICN", "GMP"),
    "BKK": ("BKK", "DMK"),
    "IST": ("IST", "SAW"),
    "BER": ("BER",),
}
"""IATA metropolitan-area codes mapped to the airports they cover.

Codes not listed here are passed through unchanged and treated as airports. This is not
exhaustive -- it covers the metro codes most likely to appear in a long-haul award search.
"""


def expand_airports(codes: list[str]) -> list[str]:
    """Replace any metropolitan-area code with its airports, order-stable and deduped."""
    out: list[str] = []
    for code in codes:
        out.extend(METRO_AREAS.get(code, (code,)))
    return list(dict.fromkeys(out))


def is_metro(code: str) -> bool:
    return code in METRO_AREAS
