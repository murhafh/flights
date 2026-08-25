from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from flightwatch.config import DedupeConfig
from flightwatch.models import Availability, Cabin, Deal
from flightwatch.state import Store

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as store:
        yield store


def make_deal(miles: int = 60_000, depart: date = date(2026, 10, 1)) -> Deal:
    return Deal(
        availability=Availability(
            source="aeroplan",
            origin="SFO",
            destination="LHR",
            depart=depart,
            cabin=Cabin.BUSINESS,
            miles=miles,
            direct=True,
        ),
        watch_name="test",
        max_miles=70_000,
    )


def test_unseen_deal_alerts(store: Store) -> None:
    assert store.should_alert(make_deal(), DedupeConfig(), now=NOW)


def test_same_deal_is_suppressed_within_cooldown(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=3, realert_on_improvement=5_000)
    store.record_alerted([make_deal()], now=NOW)
    assert not store.should_alert(make_deal(), dedupe, now=NOW + timedelta(days=2))


def test_same_deal_realerts_after_cooldown(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=3, realert_on_improvement=5_000)
    store.record_alerted([make_deal()], now=NOW)
    assert store.should_alert(make_deal(), dedupe, now=NOW + timedelta(days=3))


def test_zero_cooldown_never_realerts_unchanged_deals(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=0, realert_on_improvement=5_000)
    store.record_alerted([make_deal()], now=NOW)
    assert not store.should_alert(make_deal(), dedupe, now=NOW + timedelta(days=999))


def test_big_price_drop_realerts_immediately(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_deal(miles=60_000)], now=NOW)
    assert store.should_alert(make_deal(miles=55_000), dedupe, now=NOW + timedelta(hours=1))


def test_small_price_drop_does_not_realert(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_deal(miles=60_000)], now=NOW)
    assert not store.should_alert(make_deal(miles=58_000), dedupe, now=NOW + timedelta(hours=1))


def test_price_increase_does_not_realert(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_deal(miles=60_000)], now=NOW)
    assert not store.should_alert(make_deal(miles=68_000), dedupe, now=NOW + timedelta(hours=1))


def test_improvement_is_measured_against_last_alerted_price(store: Store) -> None:
    """Two 3k drops in a row shouldn't each be suppressed forever -- but neither
    should they add up to an alert until one crosses the bar from the alerted price."""
    dedupe = DedupeConfig(cooldown_days=0, realert_on_improvement=5_000)
    store.record_alerted([make_deal(miles=60_000)], now=NOW)

    assert not store.should_alert(make_deal(miles=57_000), dedupe, now=NOW)
    # Still measured from 60,000, so a cumulative 6k drop does trip it.
    assert store.should_alert(make_deal(miles=54_000), dedupe, now=NOW)


def test_select_alertable_annotates_previous_price(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=0, realert_on_improvement=5_000)
    store.record_alerted([make_deal(miles=60_000)], now=NOW)

    selected = store.select_alertable([make_deal(miles=50_000)], dedupe, now=NOW)
    assert len(selected) == 1
    assert selected[0].previous_miles == 60_000
    assert selected[0].is_improvement


def test_select_alertable_leaves_previous_price_unset_for_new_deals(store: Store) -> None:
    selected = store.select_alertable([make_deal()], DedupeConfig(), now=NOW)
    assert selected[0].previous_miles is None
    assert not selected[0].is_improvement


def test_different_dates_are_tracked_separately(store: Store) -> None:
    store.record_alerted([make_deal(depart=date(2026, 10, 1))], now=NOW)
    other = make_deal(depart=date(2026, 10, 2))
    assert store.should_alert(other, DedupeConfig(), now=NOW)


def test_recording_twice_updates_rather_than_duplicating(store: Store) -> None:
    store.record_alerted([make_deal(miles=60_000)], now=NOW)
    store.record_alerted([make_deal(miles=55_000)], now=NOW + timedelta(days=1))
    record = store.get(make_deal().availability.key)
    assert record is not None
    assert record.miles == 55_000


def test_prune_drops_stale_records(store: Store) -> None:
    old = datetime.now(UTC) - timedelta(days=200)
    store.record_alerted([make_deal()], now=old)
    assert store.prune(older_than_days=120) == 1
    assert store.get(make_deal().availability.key) is None


def test_prune_keeps_fresh_records(store: Store) -> None:
    store.record_alerted([make_deal()], now=datetime.now(UTC))
    assert store.prune(older_than_days=120) == 0


def test_runs_are_recorded(store: Store) -> None:
    store.record_run(found=3, alerted=1)
    store.record_run(found=0, alerted=0, error="boom")
    runs = store.recent_runs()
    assert len(runs) == 2
    assert runs[0]["error"] == "boom"


def test_state_file_is_created_with_parents(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "state.db"
    with Store(path) as store:
        store.record_run(found=0, alerted=0)
    assert path.is_file()


# --- round-trip aware dedupe -------------------------------------------------


def make_paired_deal(miles: int = 57_500, return_miles: int | None = 60_000) -> Deal:
    deal = make_deal(miles=miles)
    if return_miles is None:
        return deal
    leg = Availability(
        source="american",
        origin="LHR",
        destination="SFO",
        depart=date(2026, 10, 10),
        cabin=Cabin.BUSINESS,
        miles=return_miles,
        direct=False,
    )
    return replace(deal, returns=(leg,), returns_total=1)


def test_return_appearing_later_triggers_a_realert(store: Store) -> None:
    """The point of pairing: you were told about a one-way, now it's a round trip."""
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_paired_deal(return_miles=None)], now=NOW)
    assert store.should_alert(make_paired_deal(), dedupe, now=NOW + timedelta(hours=1))


def test_unchanged_round_trip_is_still_suppressed(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_paired_deal()], now=NOW)
    assert not store.should_alert(make_paired_deal(), dedupe, now=NOW + timedelta(hours=1))


def test_cheaper_return_alone_triggers_a_realert(store: Store) -> None:
    """Outbound price unchanged, but the round trip got materially cheaper."""
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_paired_deal(return_miles=60_000)], now=NOW)
    assert store.should_alert(
        make_paired_deal(return_miles=52_000), dedupe, now=NOW + timedelta(hours=1)
    )


def test_marginally_cheaper_return_does_not_realert(store: Store) -> None:
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_paired_deal(return_miles=60_000)], now=NOW)
    assert not store.should_alert(
        make_paired_deal(return_miles=58_000), dedupe, now=NOW + timedelta(hours=1)
    )


def test_return_disappearing_does_not_realert(store: Store) -> None:
    """Losing a return is not news worth a second notification."""
    dedupe = DedupeConfig(cooldown_days=30, realert_on_improvement=5_000)
    store.record_alerted([make_paired_deal()], now=NOW)
    assert not store.should_alert(
        make_paired_deal(return_miles=None), dedupe, now=NOW + timedelta(hours=1)
    )


def test_return_miles_are_persisted(store: Store) -> None:
    store.record_alerted([make_paired_deal(return_miles=61_000)], now=NOW)
    assert store.get(make_deal().availability.key).return_miles == 61_000


def test_existing_state_file_is_migrated(tmp_path) -> None:
    """A db created before returns existed must keep working, not be rebuilt."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE alerts (
            key TEXT PRIMARY KEY, watch_name TEXT NOT NULL, miles INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL, last_alerted_at TEXT NOT NULL
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            found INTEGER NOT NULL, alerted INTEGER NOT NULL, error TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO alerts VALUES ('k', 'w', 57500, '2026-08-01T00:00:00+00:00', "
        "'2026-08-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    with Store(path) as store:
        record = store.get("k")
        assert record is not None
        assert record.miles == 57_500  # pre-existing history survives
        assert record.return_miles is None  # new column defaults cleanly
        store.record_alerted([make_paired_deal()], now=NOW)
