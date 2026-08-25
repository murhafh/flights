"""Metro-area code expansion tests.

Regression guard: `destinations = ["LON"]` used to search correctly (seats.aero accepts
city codes) and then discard every result, because the API returns LHR/LGW and the route
filter compared those against the literal string "LON". A silent zero indistinguishable
from "no award space".
"""

from __future__ import annotations

from flightwatch.airports import METRO_AREAS, expand_airports, is_metro
from flightwatch.config import Threshold, Watch
from flightwatch.models import Cabin


def make_watch(destinations: list[str]) -> Watch:
    return Watch(
        name="w",
        origins=["SFO"],
        destinations=destinations,
        thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=80_000)],
    )


def test_london_expands_to_its_airports() -> None:
    expanded = expand_airports(["LON"])
    assert "LHR" in expanded
    assert "LGW" in expanded
    assert "LON" not in expanded


def test_watch_expands_metro_destinations() -> None:
    watch = make_watch(["LON"])
    assert "LHR" in watch.destinations
    assert "LON" not in watch.destinations


def test_watch_expands_metro_origins() -> None:
    watch = Watch(
        name="w",
        origins=["NYC"],
        destinations=["LHR"],
        thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=80_000)],
    )
    assert set(watch.origins) == {"JFK", "EWR", "LGA"}


def test_plain_airports_pass_through() -> None:
    assert expand_airports(["LHR", "CDG"]) == ["LHR", "CDG"]


def test_unknown_three_letter_code_is_treated_as_an_airport() -> None:
    """Not every airport is in the table; unknown codes must still work."""
    assert expand_airports(["DUB"]) == ["DUB"]


def test_mixing_metro_and_airport_codes_dedupes() -> None:
    expanded = expand_airports(["LON", "LHR"])
    assert expanded.count("LHR") == 1


def test_expansion_is_order_stable() -> None:
    assert expand_airports(["CDG", "LON"])[0] == "CDG"


def test_lowercase_metro_code_still_expands() -> None:
    """The uppercasing validator runs before expansion."""
    assert "LHR" in make_watch(["lon"]).destinations


def test_shared_code_does_not_duplicate() -> None:
    """BKK and SHA are both a metro code and one of their own airports."""
    for code in ("BKK", "SHA"):
        expanded = expand_airports([code])
        assert len(expanded) == len(set(expanded))
        assert code in expanded


def test_is_metro_identifies_city_codes() -> None:
    assert is_metro("LON")
    assert not is_metro("LHR")


def test_every_metro_maps_to_valid_looking_codes() -> None:
    for metro, members in METRO_AREAS.items():
        assert len(metro) == 3 and metro.isupper()
        assert members, f"{metro} maps to nothing"
        for member in members:
            assert len(member) == 3 and member.isupper(), f"{metro} -> {member}"


def test_metro_expansion_survives_a_full_route_filter() -> None:
    """The actual bug: results keyed by airport must survive a watch written as LON."""
    from datetime import date

    from flightwatch import rules
    from flightwatch.models import Availability

    watch = make_watch(["LON"])
    availability = Availability(
        source="aeroplan",
        origin="SFO",
        destination="LHR",
        depart=date(2026, 10, 1),
        cabin=Cabin.BUSINESS,
        miles=70_000,
        direct=True,
    )
    assert len(rules.evaluate(watch, [availability])) == 1
