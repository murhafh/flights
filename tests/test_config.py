from __future__ import annotations

from datetime import date

import pytest

from flightwatch.config import Config, Threshold, Watch, resolve_date
from flightwatch.models import Cabin

TODAY = date(2026, 8, 21)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("2026-12-25", date(2026, 12, 25)),
        ("today", TODAY),
        ("+21d", date(2026, 9, 11)),
        ("21d", date(2026, 9, 11)),
        ("-7d", date(2026, 8, 14)),
        ("2w", date(2026, 9, 4)),
        ("3m", date(2026, 11, 19)),
        ("1y", date(2027, 8, 21)),
    ],
)
def test_resolve_date(spec: str, expected: date) -> None:
    assert resolve_date(spec, today=TODAY) == expected


def test_resolve_date_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a date"):
        resolve_date("next tuesday", today=TODAY)


def test_airport_codes_are_normalized() -> None:
    watch = Watch(
        name="w",
        origins=["sfo", " oak "],
        destinations=["LHR"],
        thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000)],
    )
    assert watch.origins == ["SFO", "OAK"]


def test_bad_airport_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="IATA"):
        Watch(
            name="w",
            origins=["San Francisco"],
            destinations=["LHR"],
            thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000)],
        )


def test_inverted_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="later than"):
        Watch(
            name="backwards",
            origins=["SFO"],
            destinations=["LHR"],
            depart_after="+100d",
            depart_before="+10d",
            thresholds=[Threshold(cabin=Cabin.BUSINESS, max_miles=70_000)],
        )


def test_cabins_dedupes_but_keeps_order() -> None:
    watch = Watch(
        name="w",
        origins=["SFO"],
        destinations=["LHR"],
        thresholds=[
            Threshold(cabin=Cabin.FIRST, max_miles=120_000),
            Threshold(cabin=Cabin.BUSINESS, max_miles=70_000),
            Threshold(cabin=Cabin.FIRST, max_miles=130_000),
        ],
    )
    assert watch.cabins == (Cabin.FIRST, Cabin.BUSINESS)


def test_unknown_config_key_is_rejected() -> None:
    """Typos in a config file should fail loudly rather than being silently ignored."""
    with pytest.raises(ValueError, match="max_mile"):
        Config.model_validate(
            {
                "watch": [
                    {
                        "name": "typo",
                        "origins": ["SFO"],
                        "destinations": ["LHR"],
                        "thresholds": [{"cabin": "business", "max_mile": 70_000}],
                    }
                ]
            }
        )


def test_config_roundtrips_from_toml(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [provider]
        kind = "mock"

        [[notify]]
        kind = "console"

        [[watch]]
        name = "Europe business"
        origins = ["SFO"]
        destinations = ["LHR", "CDG"]
        depart_after = "+10d"
        depart_before = "+100d"

          [[watch.thresholds]]
          cabin = "business"
          max_miles = 70000
          max_taxes = 150.0
          min_seats = 2
        """
    )
    config = Config.load(path)
    assert config.provider.kind == "mock"
    assert config.watch[0].thresholds[0].min_seats == 2
    assert config.dedupe.cooldown_days == 3  # default applied


# --- Threshold ceilings -----------------------------------------------------


def test_max_miles_is_inclusive() -> None:
    threshold = Threshold(cabin=Cabin.BUSINESS, max_miles=80_000)
    assert threshold.allows(79_999)
    assert threshold.allows(80_000)
    assert not threshold.allows(80_001)
    assert not threshold.strict


def test_under_miles_is_strict() -> None:
    """Award prices cluster on round numbers, so excluding exactly-80,000 matters."""
    threshold = Threshold(cabin=Cabin.BUSINESS, under_miles=80_000)
    assert threshold.allows(79_999)
    assert not threshold.allows(80_000)
    assert not threshold.allows(80_001)
    assert threshold.strict


def test_both_ceilings_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly one of max_miles"):
        Threshold(cabin=Cabin.BUSINESS, max_miles=80_000, under_miles=80_000)


def test_neither_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly one of max_miles"):
        Threshold(cabin=Cabin.BUSINESS)


def test_limit_reports_the_configured_number() -> None:
    assert Threshold(cabin=Cabin.FIRST, max_miles=120_000).limit == 120_000
    assert Threshold(cabin=Cabin.FIRST, under_miles=120_000).limit == 120_000


def test_describe_shows_which_comparison_applies() -> None:
    assert Threshold(cabin=Cabin.BUSINESS, max_miles=80_000).describe() == "Business <= 80,000"
    assert Threshold(cabin=Cabin.BUSINESS, under_miles=80_000).describe() == "Business < 80,000"


def test_under_miles_parses_from_toml(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [provider]
        kind = "mock"

        [[watch]]
        name = "strict"
        origins = ["SFO"]
        destinations = ["DUB"]

          [[watch.thresholds]]
          cabin = "business"
          under_miles = 80000
        """
    )
    threshold = Config.load(path).watch[0].thresholds[0]
    assert threshold.strict
    assert threshold.limit == 80_000
