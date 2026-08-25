from __future__ import annotations

from datetime import date

from flightwatch import rules
from flightwatch.config import Threshold, Watch
from flightwatch.models import Availability, Cabin

DEPART = date(2026, 10, 1)


def make_watch(**overrides) -> Watch:
    defaults = {
        "name": "test",
        "origins": ["SFO"],
        "destinations": ["LHR"],
        "thresholds": [Threshold(cabin=Cabin.BUSINESS, max_miles=70_000)],
    }
    return Watch(**{**defaults, **overrides})


def make_availability(**overrides) -> Availability:
    defaults = {
        "source": "aeroplan",
        "origin": "SFO",
        "destination": "LHR",
        "depart": DEPART,
        "cabin": Cabin.BUSINESS,
        "miles": 60_000,
        "direct": True,
        "seats": 2,
        "taxes": 100.0,
        "taxes_currency": "USD",
        "airlines": ("AC",),
    }
    return Availability(**{**defaults, **overrides})


def test_under_threshold_matches() -> None:
    deals = rules.evaluate(make_watch(), [make_availability()])
    assert len(deals) == 1
    assert deals[0].max_miles == 70_000
    assert deals[0].savings == 10_000


def test_over_threshold_is_dropped() -> None:
    assert rules.evaluate(make_watch(), [make_availability(miles=70_001)]) == []


def test_exactly_at_threshold_matches() -> None:
    assert len(rules.evaluate(make_watch(), [make_availability(miles=70_000)])) == 1


def test_wrong_cabin_is_dropped() -> None:
    assert rules.evaluate(make_watch(), [make_availability(cabin=Cabin.ECONOMY)]) == []


def test_taxes_over_cap_is_dropped() -> None:
    watch = make_watch(
        thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000, max_taxes=150)]
    )
    assert rules.evaluate(watch, [make_availability(taxes=400.0)]) == []
    assert len(rules.evaluate(watch, [make_availability(taxes=120.0)])) == 1


def test_unknown_taxes_pass_the_cap() -> None:
    """We'd rather surface a deal with unknown surcharges than hide it."""
    watch = make_watch(
        thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000, max_taxes=150)]
    )
    assert len(rules.evaluate(watch, [make_availability(taxes=None)])) == 1


def test_too_few_seats_is_dropped() -> None:
    watch = make_watch(thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000, min_seats=2)])
    assert rules.evaluate(watch, [make_availability(seats=1)]) == []


def test_unknown_seat_count_passes() -> None:
    watch = make_watch(thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000, min_seats=2)])
    assert len(rules.evaluate(watch, [make_availability(seats=None)])) == 1


def test_nonstop_only_drops_connections() -> None:
    watch = make_watch(
        thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000, nonstop_only=True)]
    )
    assert rules.evaluate(watch, [make_availability(direct=False)]) == []
    assert len(rules.evaluate(watch, [make_availability(direct=True)])) == 1


def test_program_filter_is_reapplied_locally() -> None:
    watch = make_watch(programs=["flyingblue"])
    assert rules.evaluate(watch, [make_availability(source="aeroplan")]) == []


def test_excluded_airline_is_dropped() -> None:
    watch = make_watch(airlines_exclude=["AC"])
    assert rules.evaluate(watch, [make_availability(airlines=("AC", "LH"))]) == []


def test_offroute_availability_is_dropped() -> None:
    """Comma-separated O&D searches can return pairs we didn't ask for."""
    assert rules.evaluate(make_watch(), [make_availability(destination="CDG")]) == []


def test_duplicate_route_day_collapses_to_cheapest() -> None:
    """A connecting and a nonstop variant of the same award is one alert, not two."""
    connecting = make_availability(miles=55_000, direct=False)
    nonstop = make_availability(miles=65_000, direct=True)
    deals = rules.evaluate(make_watch(), [connecting, nonstop])
    assert len(deals) == 1
    assert deals[0].availability.miles == 55_000


def test_collapse_prefers_nonstop_on_a_tie() -> None:
    deals = rules.evaluate(
        make_watch(),
        [
            make_availability(miles=60_000, direct=False),
            make_availability(miles=60_000, direct=True),
        ],
    )
    assert len(deals) == 1
    assert deals[0].availability.direct is True


def test_distinct_dates_do_not_collapse() -> None:
    deals = rules.evaluate(
        make_watch(),
        [make_availability(), make_availability(depart=date(2026, 10, 2))],
    )
    assert len(deals) == 2


def test_results_are_sorted_cheapest_first() -> None:
    deals = rules.evaluate(
        make_watch(),
        [
            make_availability(depart=date(2026, 10, 3), miles=68_000),
            make_availability(depart=date(2026, 10, 1), miles=52_000),
            make_availability(depart=date(2026, 10, 2), miles=60_000),
        ],
    )
    assert [deal.availability.miles for deal in deals] == [52_000, 60_000, 68_000]


def test_multiple_cabins_each_use_their_own_threshold() -> None:
    watch = make_watch(
        thresholds=[
            Threshold(cabin=Cabin.BUSINESS, max_miles=70_000),
            Threshold(cabin=Cabin.FIRST, max_miles=120_000),
        ]
    )
    deals = rules.evaluate(
        watch,
        [
            make_availability(cabin=Cabin.BUSINESS, miles=65_000),
            make_availability(cabin=Cabin.FIRST, miles=110_000),
            make_availability(cabin=Cabin.FIRST, miles=130_000, depart=date(2026, 10, 5)),
        ],
    )
    assert {(deal.availability.cabin, deal.availability.miles) for deal in deals} == {
        (Cabin.BUSINESS, 65_000),
        (Cabin.FIRST, 110_000),
    }


def test_equal_mileage_sorts_cheaper_surcharge_first() -> None:
    """Identical mileage prices routinely differ by hundreds in cash."""
    deals = rules.evaluate(
        make_watch(),
        [
            make_availability(depart=date(2026, 10, 2), miles=57_500, taxes=749.0),
            make_availability(depart=date(2026, 10, 1), miles=57_500, taxes=5.6),
        ],
    )
    assert [deal.availability.taxes for deal in deals] == [5.6, 749.0]


def test_miles_still_outrank_surcharges() -> None:
    deals = rules.evaluate(
        make_watch(),
        [
            make_availability(depart=date(2026, 10, 1), miles=57_500, taxes=749.0),
            make_availability(depart=date(2026, 10, 2), miles=60_000, taxes=5.6),
        ],
    )
    assert [deal.availability.miles for deal in deals] == [57_500, 60_000]


def test_collapse_prefers_lower_surcharge_at_equal_miles() -> None:
    deals = rules.evaluate(
        make_watch(),
        [
            make_availability(miles=57_500, taxes=749.0, direct=False),
            make_availability(miles=57_500, taxes=5.6, direct=False),
        ],
    )
    assert len(deals) == 1
    assert deals[0].availability.taxes == 5.6


def test_strict_ceiling_excludes_an_award_at_the_limit() -> None:
    watch = make_watch(thresholds=[Threshold(cabin=Cabin.BUSINESS, under_miles=80_000)])
    assert rules.evaluate(watch, [make_availability(miles=80_000)]) == []
    assert len(rules.evaluate(watch, [make_availability(miles=79_500)])) == 1


def test_inclusive_ceiling_still_includes_the_limit() -> None:
    watch = make_watch(thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=80_000)])
    assert len(rules.evaluate(watch, [make_availability(miles=80_000)])) == 1


def test_deal_records_the_ceiling_it_cleared() -> None:
    watch = make_watch(thresholds=[Threshold(cabin=Cabin.BUSINESS, under_miles=80_000)])
    deal = rules.evaluate(watch, [make_availability(miles=70_000)])[0]
    assert deal.max_miles == 80_000
    assert deal.savings == 10_000
