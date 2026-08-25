"""Transfer-partner group and program validation tests."""

from __future__ import annotations

import pytest

from flightwatch.config import Threshold, Watch
from flightwatch.models import Cabin
from flightwatch.programs import CHASE, KNOWN_SOURCES, expand_programs


def make_watch(programs: list[str]) -> Watch:
    return Watch(
        name="w",
        origins=["SFO"],
        destinations=["DUB"],
        programs=programs,
        thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=80_000)],
    )


def test_chase_alias_expands() -> None:
    assert expand_programs(["@chase"]) == sorted(CHASE)


def test_chase_alias_works_in_a_watch() -> None:
    assert set(make_watch(["@chase"]).programs) == set(CHASE)


def test_chase_excludes_american_and_alaska() -> None:
    """The two programs with the cheapest SFO-DUB business space are not Chase
    partners; a Chase-restricted watch must not silently include them."""
    assert "american" not in CHASE
    assert "alaska" not in CHASE
    assert "delta" not in CHASE


def test_chase_members_are_all_real_sources() -> None:
    """An alias entry that isn't a real API source would match nothing forever."""
    assert CHASE <= KNOWN_SOURCES


def test_alias_and_explicit_programs_combine() -> None:
    expanded = expand_programs(["@chase", "alaska"])
    assert "alaska" in expanded
    assert "united" in expanded


def test_duplicates_are_removed() -> None:
    assert expand_programs(["united", "@chase", "united"]).count("united") == 1


def test_unknown_alias_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown program alias"):
        expand_programs(["@sapphire"])


def test_unknown_program_is_rejected() -> None:
    """A typo must fail loudly -- silently matching nothing is indistinguishable from
    'no award space', which is the worst failure mode here."""
    with pytest.raises(ValueError, match="Unknown mileage program"):
        expand_programs(["britishairways"])


def test_unknown_program_error_lists_valid_options() -> None:
    with pytest.raises(ValueError, match="virginatlantic"):
        expand_programs(["virgin"])


def test_case_and_whitespace_are_normalized() -> None:
    assert expand_programs(["  United  ", "@CHASE"]) == expand_programs(["united", "@chase"])


def test_empty_entries_are_skipped() -> None:
    assert expand_programs(["united", "", "  "]) == ["united"]


def test_empty_list_stays_empty() -> None:
    """No programs means 'whatever the provider covers', not 'nothing'."""
    assert expand_programs([]) == []
    assert make_watch([]).programs == []
