"""Round-trip pairing tests."""

from __future__ import annotations

from datetime import date

import pytest

from flightwatch import rules
from flightwatch.config import ReturnTrip, Threshold, Watch
from flightwatch.models import Availability, Cabin, Deal
from flightwatch.notify.base import format_deals_text, format_return_line

OUT = date(2026, 10, 1)


def make_watch(**overrides) -> Watch:
    defaults = {
        "name": "test",
        "origins": ["SFO"],
        "destinations": ["DUB"],
        "thresholds": [Threshold(cabin=Cabin.BUSINESS, max_miles=80_000)],
        "return_trip": ReturnTrip(min_days=7, max_days=14),
    }
    return Watch(**{**defaults, **overrides})


def outbound(**overrides) -> Availability:
    defaults = {
        "source": "american",
        "origin": "SFO",
        "destination": "DUB",
        "depart": OUT,
        "cabin": Cabin.BUSINESS,
        "miles": 57_500,
        "direct": False,
        "taxes": 5.6,
        "taxes_currency": "USD",
        "airlines": ("AA",),
    }
    return Availability(**{**defaults, **overrides})


def inbound(depart: date, miles: int = 60_000, **overrides) -> Availability:
    defaults = {
        "source": "american",
        "origin": "DUB",
        "destination": "SFO",
        "depart": depart,
        "cabin": Cabin.BUSINESS,
        "miles": miles,
        "direct": False,
        "taxes": 90.0,
        "taxes_currency": "USD",
        "airlines": ("AA",),
    }
    return Availability(**{**defaults, **overrides})


def paired(watch: Watch, returns: list[Availability], out: Availability | None = None) -> Deal:
    deals = rules.evaluate(watch, [out or outbound()])
    return rules.attach_returns(watch, deals, returns)[0]


# --- window ------------------------------------------------------------------


def test_return_inside_the_window_is_attached() -> None:
    deal = paired(make_watch(), [inbound(date(2026, 10, 10))])
    assert deal.best_return is not None
    assert deal.best_return.depart == date(2026, 10, 10)


def test_return_too_soon_is_ignored() -> None:
    """6 days out, window starts at 7."""
    assert paired(make_watch(), [inbound(date(2026, 10, 7))]).returns == ()


def test_return_too_late_is_ignored() -> None:
    """15 days out, window ends at 14."""
    assert paired(make_watch(), [inbound(date(2026, 10, 16))]).returns == ()


def test_window_boundaries_are_inclusive() -> None:
    for day in (date(2026, 10, 8), date(2026, 10, 15)):
        assert paired(make_watch(), [inbound(day)]).returns, day


def test_return_before_the_outbound_is_ignored() -> None:
    assert paired(make_watch(), [inbound(date(2026, 9, 20))]).returns == ()


# --- thresholds --------------------------------------------------------------


def test_return_over_the_mileage_ceiling_is_ignored() -> None:
    assert paired(make_watch(), [inbound(date(2026, 10, 10), miles=90_000)]).returns == ()


def test_return_must_clear_the_tax_cap_too() -> None:
    watch = make_watch(thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=80_000, max_taxes=50)])
    assert paired(watch, [inbound(date(2026, 10, 10), taxes=700.0)]).returns == ()


def test_return_must_be_on_an_allowed_program() -> None:
    watch = make_watch(programs=["@chase"])
    out = outbound(source="united")
    assert paired(watch, [inbound(date(2026, 10, 10), source="american")], out).returns == ()
    assert paired(watch, [inbound(date(2026, 10, 10), source="united")], out).returns


def test_return_on_the_wrong_route_is_ignored() -> None:
    """A return leg must actually come back to an origin we asked about."""
    wrong = inbound(date(2026, 10, 10), destination="JFK")
    assert paired(make_watch(), [wrong]).returns == ()


def test_return_must_be_the_reverse_direction() -> None:
    """An outbound-direction record inside the return window is not a return."""
    not_a_return = outbound(depart=date(2026, 10, 10))
    assert paired(make_watch(), [not_a_return]).returns == ()


# --- selection and capping ---------------------------------------------------


def test_cheapest_return_is_chosen() -> None:
    deal = paired(
        make_watch(),
        [
            inbound(date(2026, 10, 10), miles=75_000),
            inbound(date(2026, 10, 11), miles=60_000),
        ],
    )
    assert deal.best_return.miles == 60_000


def test_returns_are_capped_but_total_is_reported() -> None:
    many = [inbound(date(2026, 10, 8 + i), miles=60_000 + i * 500) for i in range(6)]
    deal = paired(make_watch(), many)
    assert len(deal.returns) == rules.MAX_RETURNS_SHOWN
    assert deal.returns_total == 6


def test_connecting_and_nonstop_variants_collapse() -> None:
    """Both variants of one return route-day must not occupy two shown slots."""
    day = date(2026, 10, 10)
    deal = paired(
        make_watch(),
        [inbound(day, miles=60_000, direct=False), inbound(day, miles=62_500, direct=True)],
    )
    assert deal.returns_total == 1
    assert deal.best_return.miles == 60_000


# --- totals ------------------------------------------------------------------


def test_round_trip_totals_add_up() -> None:
    deal = paired(make_watch(), [inbound(date(2026, 10, 10), miles=60_000)])
    assert deal.round_trip_miles == 117_500
    assert deal.round_trip_taxes == pytest.approx(95.6)


def test_round_trip_is_none_without_a_return() -> None:
    deal = paired(make_watch(), [])
    assert deal.round_trip_miles is None
    assert deal.round_trip_taxes is None


def test_round_trip_taxes_tolerate_a_missing_leg_figure() -> None:
    deal = paired(make_watch(), [inbound(date(2026, 10, 10), taxes=None)])
    assert deal.round_trip_taxes == pytest.approx(5.6)


# --- one-way behaviour is preserved -----------------------------------------


def test_no_return_still_yields_the_outbound_deal() -> None:
    """The headline requirement: one-way alerts keep working."""
    deals = rules.evaluate(make_watch(), [outbound()])
    out = rules.attach_returns(make_watch(), deals, [])
    assert len(out) == 1
    assert out[0].returns == ()


def test_watch_without_return_trip_is_untouched() -> None:
    watch = make_watch(return_trip=None)
    deals = rules.evaluate(watch, [outbound()])
    assert rules.attach_returns(watch, deals, [inbound(date(2026, 10, 10))]) == deals


def test_return_window_spans_the_whole_outbound_range() -> None:
    watch = make_watch(depart_after="2026-10-01", depart_before="2026-10-31")
    assert watch.return_window() == (date(2026, 10, 8), date(2026, 11, 14))


def test_return_window_is_none_when_unconfigured() -> None:
    assert make_watch(return_trip=None).return_window() is None


def test_inverted_return_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="min_days .* greater than"):
        ReturnTrip(min_days=14, max_days=7)


# --- rendering ---------------------------------------------------------------


def test_return_line_shows_leg_and_round_trip_total() -> None:
    line = format_return_line(paired(make_watch(), [inbound(date(2026, 10, 10))]))
    assert "return DUB->SFO" in line
    assert "60,000 mi" in line
    assert "round trip 117,500 mi" in line


def test_return_line_mentions_other_dates() -> None:
    many = [inbound(date(2026, 10, 8 + i)) for i in range(4)]
    assert "3 other date(s)" in format_return_line(paired(make_watch(), many))


def test_return_line_is_none_without_a_return() -> None:
    assert format_return_line(paired(make_watch(), [])) is None


def test_text_body_includes_the_return_under_the_outbound() -> None:
    body = format_deals_text([paired(make_watch(), [inbound(date(2026, 10, 10))])])
    lines = body.splitlines()
    outbound_index = next(i for i, line in enumerate(lines) if "SFO->DUB" in line)
    assert "return DUB->SFO" in lines[outbound_index + 1]


def test_text_body_unchanged_for_one_way_deals() -> None:
    body = format_deals_text([paired(make_watch(), [])])
    assert "return" not in body
    assert "SFO->DUB" in body
