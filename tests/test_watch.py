"""End-to-end run tests using the mock provider and an in-memory store."""

from __future__ import annotations

from datetime import date

import pytest

from flightwatch.config import Config
from flightwatch.models import Availability, Cabin, Deal, SearchQuery
from flightwatch.notify import Channel
from flightwatch.notify.base import NotifierError, format_deals_text, summarize
from flightwatch.providers import MockProvider, ProviderError
from flightwatch.state import Store
from flightwatch.watch import run_once

TODAY = date(2026, 8, 21)


class RecordingNotifier:
    """Captures batches instead of sending them."""

    name = "recording"

    def __init__(self) -> None:
        self.batches: list[list[Deal]] = []

    def send(self, deals: list[Deal]) -> None:
        self.batches.append(list(deals))


class BrokenNotifier:
    name = "broken"

    def send(self, deals: list[Deal]) -> None:
        raise NotifierError("channel down")


class ExplodingProvider:
    name = "exploding"

    def search(self, query: SearchQuery) -> list[Availability]:
        raise ProviderError("upstream on fire")

    def close(self) -> None:
        return


def make_config(**overrides) -> Config:
    payload = {
        "provider": {"kind": "mock"},
        "notify": [{"kind": "console"}],
        "watch": [
            {
                "name": "Europe business",
                "origins": ["SFO"],
                "destinations": ["LHR", "CDG"],
                "depart_after": "+10d",
                "depart_before": "+120d",
                "thresholds": [{"cabin": "business", "max_miles": 70_000}],
            }
        ],
    }
    return Config.model_validate({**payload, **overrides})


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as store:
        yield store


def test_run_finds_and_alerts(store: Store) -> None:
    notifier = RecordingNotifier()
    result = run_once(
        make_config(),
        provider=MockProvider(),
        store=store,
        notifiers=[notifier],
        today=TODAY,
    )
    assert result.ok
    assert result.searched > 0
    assert result.alerted
    assert notifier.batches == [result.alerted]


def test_every_alerted_deal_is_under_its_threshold(store: Store) -> None:
    result = run_once(
        make_config(), provider=MockProvider(), store=store, notifiers=[], today=TODAY
    )
    # notifiers=[] records an error but matched deals are still computed.
    assert all(deal.availability.miles <= 70_000 for deal in result.matched)
    assert all(deal.availability.cabin is Cabin.BUSINESS for deal in result.matched)


def test_second_run_alerts_nothing_new(store: Store) -> None:
    notifier = RecordingNotifier()
    first = run_once(
        make_config(), provider=MockProvider(), store=store, notifiers=[notifier], today=TODAY
    )
    second = run_once(
        make_config(), provider=MockProvider(), store=store, notifiers=[notifier], today=TODAY
    )
    assert first.alerted
    assert second.alerted == []
    assert len(notifier.batches) == 1


def test_dry_run_does_not_persist_or_send(store: Store) -> None:
    notifier = RecordingNotifier()
    first = run_once(
        make_config(),
        provider=MockProvider(),
        store=store,
        notifiers=[notifier],
        dry_run=True,
        today=TODAY,
    )
    assert first.alerted
    assert notifier.batches == []

    # Because nothing was recorded, a real run still has everything to report.
    second = run_once(
        make_config(), provider=MockProvider(), store=store, notifiers=[notifier], today=TODAY
    )
    assert len(second.alerted) == len(first.alerted)


def test_failing_notifier_does_not_mark_deals_as_seen(store: Store) -> None:
    """If delivery fails outright, the deal must resurface on the next run."""
    config = make_config()
    first = run_once(
        config, provider=MockProvider(), store=store, notifiers=[BrokenNotifier()], today=TODAY
    )
    assert first.alerted == []
    assert first.errors

    working = RecordingNotifier()
    second = run_once(
        config, provider=MockProvider(), store=store, notifiers=[working], today=TODAY
    )
    assert second.alerted
    assert working.batches


def test_partial_delivery_still_records(store: Store) -> None:
    """One dead channel shouldn't cause endless repeat alerts on the healthy one."""
    working = RecordingNotifier()
    config = make_config()
    first = run_once(
        config,
        provider=MockProvider(),
        store=store,
        notifiers=[BrokenNotifier(), working],
        today=TODAY,
    )
    assert first.alerted
    assert first.errors  # the broken channel is still reported

    second = run_once(
        config, provider=MockProvider(), store=store, notifiers=[working], today=TODAY
    )
    assert second.alerted == []


def test_no_notifiers_is_an_error(store: Store) -> None:
    result = run_once(
        make_config(), provider=MockProvider(), store=store, notifiers=[], today=TODAY
    )
    assert not result.ok
    assert any("not delivered" in error for error in result.errors)


def test_provider_failure_is_isolated_per_watch(store: Store) -> None:
    config = make_config(
        watch=[
            {
                "name": "one",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "thresholds": [{"cabin": "business", "max_miles": 70_000}],
            },
            {
                "name": "two",
                "origins": ["JFK"],
                "destinations": ["CDG"],
                "thresholds": [{"cabin": "business", "max_miles": 70_000}],
            },
        ]
    )
    result = run_once(
        config,
        provider=ExplodingProvider(),
        store=store,
        notifiers=[RecordingNotifier()],
        today=TODAY,
    )
    # Both watches failed, and both are named in the errors.
    assert len(result.errors) == 2
    assert any("one:" in error for error in result.errors)
    assert any("two:" in error for error in result.errors)


def test_missing_api_key_fails_before_searching(store: Store, monkeypatch) -> None:
    monkeypatch.delenv("SEATS_AERO_API_KEY", raising=False)
    config = make_config(provider={"kind": "seats_aero"})
    result = run_once(config, store=store, notifiers=[RecordingNotifier()], today=TODAY)
    assert not result.ok
    assert "SEATS_AERO_API_KEY" in result.errors[0]


def test_impossible_threshold_yields_nothing(store: Store) -> None:
    config = make_config(
        watch=[
            {
                "name": "unrealistic",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "thresholds": [{"cabin": "business", "max_miles": 1_000}],
            }
        ]
    )
    notifier = RecordingNotifier()
    result = run_once(
        config, provider=MockProvider(), store=store, notifiers=[notifier], today=TODAY
    )
    assert result.matched == []
    assert notifier.batches == []
    assert result.ok


def test_mock_provider_is_deterministic() -> None:
    query = SearchQuery(
        origins=("SFO",),
        destinations=("LHR",),
        depart_after=date(2026, 10, 1),
        depart_before=date(2026, 11, 1),
        cabins=(Cabin.BUSINESS,),
    )
    assert MockProvider().search(query) == MockProvider().search(query)


def test_run_is_recorded_in_history(store: Store) -> None:
    run_once(
        make_config(),
        provider=MockProvider(),
        store=store,
        notifiers=[RecordingNotifier()],
        today=TODAY,
    )
    runs = store.recent_runs()
    assert len(runs) == 1
    assert runs[0]["error"] is None


def test_alert_formatting_includes_the_essentials(store: Store) -> None:
    result = run_once(
        make_config(),
        provider=MockProvider(),
        store=store,
        notifiers=[RecordingNotifier()],
        today=TODAY,
    )
    body = format_deals_text(result.alerted)
    deal = result.alerted[0]
    assert "Europe business" in body
    assert f"{deal.availability.miles:,}" in body
    assert deal.availability.booking_url in body
    assert "miles" in summarize(result.alerted)


def test_overlapping_watches_alert_an_award_once(store: Store) -> None:
    """A Chase-restricted watch plus a broad watch both match the same award. It must
    appear once per run, not twice in the same email."""
    config = make_config(
        watch=[
            {
                "name": "chase only",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "programs": ["@chase"],
                "depart_after": "+10d",
                "depart_before": "+120d",
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
            {
                "name": "any program",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "depart_after": "+10d",
                "depart_before": "+120d",
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
        ]
    )
    notifier = RecordingNotifier()
    result = run_once(
        config, provider=MockProvider(), store=store, notifiers=[notifier], today=TODAY
    )
    keys = [deal.availability.key for deal in result.alerted]
    assert len(keys) == len(set(keys)), "the same award was alerted twice in one run"


def test_overlap_is_attributed_to_the_first_watch(store: Store) -> None:
    config = make_config(
        watch=[
            {
                "name": "first",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "programs": ["@chase"],
                "depart_after": "+10d",
                "depart_before": "+120d",
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
            {
                "name": "second",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "depart_after": "+10d",
                "depart_before": "+120d",
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
        ]
    )
    result = run_once(
        config, provider=MockProvider(), store=store, notifiers=[RecordingNotifier()], today=TODAY
    )
    chase_sources = {"aeroplan", "flyingblue", "united", "virginatlantic", "british", "jetblue"}
    for deal in result.alerted:
        if deal.availability.source in chase_sources:
            assert deal.watch_name == "first"


def test_watches_are_grouped_in_config_order(store: Store) -> None:
    config = make_config(
        watch=[
            {
                "name": "zebra",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
            {
                "name": "apple",
                "origins": ["JFK"],
                "destinations": ["CDG"],
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
        ]
    )
    result = run_once(
        config, provider=MockProvider(), store=store, notifiers=[RecordingNotifier()], today=TODAY
    )
    names = [deal.watch_name for deal in result.alerted]
    # "zebra" is configured first, so it leads despite sorting later alphabetically.
    assert names[0] == "zebra"
    assert names == sorted(names, key=lambda n: 0 if n == "zebra" else 1)


def two_watch_config(**overrides) -> Config:
    return make_config(
        watch=[
            {
                "name": "chase only",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "programs": ["@chase"],
                "depart_after": "+10d",
                "depart_before": "+120d",
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
            {
                "name": "any program",
                "origins": ["JFK"],
                "destinations": ["CDG"],
                "depart_after": "+10d",
                "depart_before": "+120d",
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
        ],
        **overrides,
    )


def test_scoped_channel_only_receives_its_watch(store: Store) -> None:
    email, console = RecordingNotifier(), RecordingNotifier()
    result = run_once(
        two_watch_config(),
        provider=MockProvider(),
        store=store,
        notifiers=[
            Channel(notifier=email, watches=frozenset({"chase only"})),
            Channel(notifier=console),
        ],
        today=TODAY,
    )
    assert result.ok
    assert email.batches, "scoped channel got nothing"
    assert {deal.watch_name for deal in email.batches[0]} == {"chase only"}
    # The unscoped channel still sees everything.
    assert {deal.watch_name for deal in console.batches[0]} == {"chase only", "any program"}
    assert len(console.batches[0]) == len(result.alerted)


def test_scoped_channel_alone_does_not_swallow_other_watches(store: Store) -> None:
    """With only a scoped channel, deals from other watches reach nobody. They must NOT
    be recorded as seen, or they'd be lost permanently."""
    email = RecordingNotifier()
    config = two_watch_config()
    first = run_once(
        config,
        provider=MockProvider(),
        store=store,
        notifiers=[Channel(notifier=email, watches=frozenset({"chase only"}))],
        today=TODAY,
    )
    assert {deal.watch_name for deal in first.alerted} == {"chase only"}
    assert any("reached no channel" in error for error in first.errors)

    # Adding a channel that covers the other watch surfaces them, still unalerted.
    console = RecordingNotifier()
    second = run_once(
        config, provider=MockProvider(), store=store, notifiers=[console], today=TODAY
    )
    assert "any program" in {deal.watch_name for deal in second.alerted}


def test_scoped_channel_failure_does_not_record_its_deals(store: Store) -> None:
    config = two_watch_config()
    first = run_once(
        config,
        provider=MockProvider(),
        store=store,
        notifiers=[
            Channel(notifier=BrokenNotifier(), watches=frozenset({"chase only"})),
            Channel(notifier=RecordingNotifier(), watches=frozenset({"any program"})),
        ],
        today=TODAY,
    )
    # Only the healthy channel's watch was delivered and recorded.
    assert {deal.watch_name for deal in first.alerted} == {"any program"}

    working = RecordingNotifier()
    second = run_once(
        config,
        provider=MockProvider(),
        store=store,
        notifiers=[Channel(notifier=working, watches=frozenset({"chase only"}))],
        today=TODAY,
    )
    assert {deal.watch_name for deal in second.alerted} == {"chase only"}


def test_notifier_scoped_to_unknown_watch_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown watch"):
        two_watch_config(notify=[{"kind": "console", "watches": ["typo"]}])


def test_notifier_scoped_to_a_real_watch_is_accepted() -> None:
    config = two_watch_config(notify=[{"kind": "console", "watches": ["chase only"]}])
    assert config.notify[0].watches == ["chase only"]


def test_unscoped_notifier_needs_no_watch_names() -> None:
    assert two_watch_config().notify[0].watches == []


def test_paused_watch_is_not_searched(store: Store) -> None:
    """A paused watch must cost nothing: no API call, no records, no deals."""
    config = make_config(
        watch=[
            {
                "name": "paused",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "enabled": False,
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            }
        ]
    )
    result = run_once(
        config,
        provider=ExplodingProvider(),
        store=store,
        notifiers=[RecordingNotifier()],
        today=TODAY,
    )
    # ExplodingProvider would raise if it were called at all.
    assert result.ok
    assert result.searched == 0
    assert result.matched == []


def test_paused_watch_does_not_block_active_ones(store: Store) -> None:
    notifier = RecordingNotifier()
    config = make_config(
        watch=[
            {
                "name": "paused",
                "origins": ["JFK"],
                "destinations": ["CDG"],
                "enabled": False,
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
            {
                "name": "active",
                "origins": ["SFO"],
                "destinations": ["LHR"],
                "thresholds": [{"cabin": "business", "max_miles": 200_000}],
            },
        ]
    )
    result = run_once(
        config, provider=MockProvider(), store=store, notifiers=[notifier], today=TODAY
    )
    assert result.alerted
    assert {deal.watch_name for deal in result.alerted} == {"active"}


def test_watches_are_enabled_by_default(store: Store) -> None:
    assert all(watch.enabled for watch in make_config().watch)


def test_pausing_preserves_alert_history(store: Store) -> None:
    """Re-enabling a watch must not replay deals already alerted, so history is kept."""
    active = make_config()
    first = run_once(
        active,
        provider=MockProvider(),
        store=store,
        notifiers=[RecordingNotifier()],
        today=TODAY,
    )
    assert first.alerted

    paused = make_config()
    paused.watch[0].enabled = False
    run_once(
        paused, provider=MockProvider(), store=store, notifiers=[RecordingNotifier()], today=TODAY
    )

    resumed = run_once(
        make_config(),
        provider=MockProvider(),
        store=store,
        notifiers=[RecordingNotifier()],
        today=TODAY,
    )
    assert resumed.alerted == []


def test_notifier_may_reference_a_paused_watch() -> None:
    """Pausing a watch must not invalidate a notifier scoped to it."""
    config = make_config(
        notify=[{"kind": "console", "watches": ["Europe business"]}],
    )
    config.watch[0].enabled = False
    assert config.notify[0].watches == ["Europe business"]


def test_unread_channel_does_not_mask_a_real_failure(store: Store) -> None:
    """Production regression: Telegram timed out, console 'succeeded', and the deal was
    recorded as sent -- suppressing it for the full 3-day cooldown. A channel marked
    counts_as_delivery=False must not absorb the failure."""
    console = RecordingNotifier()
    config = make_config()
    first = run_once(
        config,
        provider=MockProvider(),
        store=store,
        notifiers=[
            Channel(notifier=BrokenNotifier()),  # stands in for Telegram
            Channel(notifier=console, counts_as_delivery=False),  # the unread log
        ],
        today=TODAY,
    )
    assert first.alerted == [], "an unread channel marked the deal as delivered"
    assert first.errors
    assert console.batches, "the unread channel should still print"

    # Next run, with a working channel, the deal is still available to send.
    working = RecordingNotifier()
    second = run_once(
        config, provider=MockProvider(), store=store, notifiers=[working], today=TODAY
    )
    assert second.alerted
    assert working.batches


def test_counting_channel_still_records(store: Store) -> None:
    working = RecordingNotifier()
    result = run_once(
        make_config(),
        provider=MockProvider(),
        store=store,
        notifiers=[
            Channel(notifier=working),
            Channel(notifier=RecordingNotifier(), counts_as_delivery=False),
        ],
        today=TODAY,
    )
    assert result.alerted


def test_counts_as_delivery_defaults_true() -> None:
    config = make_config(notify=[{"kind": "console"}])
    assert config.notify[0].counts_as_delivery is True


def test_counts_as_delivery_parses_from_config() -> None:
    config = make_config(notify=[{"kind": "console", "counts_as_delivery": False}])
    assert config.notify[0].counts_as_delivery is False
