"""Run orchestration: search every watch, apply thresholds, dedupe, notify."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from . import rules
from .config import Config, Watch
from .models import Availability, Deal, SearchQuery
from .notify import Channel, Notifier, NotifierError, build_notifiers
from .providers import Provider, ProviderError, build_provider
from .state import Store

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    matched: list[Deal] = field(default_factory=list)
    """Everything under threshold, before dedupe."""
    alerted: list[Deal] = field(default_factory=list)
    """What was actually delivered (or would be, on a dry run)."""
    searched: int = 0
    """Total availability records returned by the provider."""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def search_watch(
    provider: Provider, watch: Watch, *, today: date | None = None
) -> list[Availability]:
    depart_after, depart_before = watch.window(today=today)
    query = SearchQuery(
        origins=tuple(watch.origins),
        destinations=tuple(watch.destinations),
        depart_after=depart_after,
        depart_before=depart_before,
        cabins=watch.cabins,
        programs=tuple(watch.programs),
    )
    return provider.search(query)


def search_returns(
    provider: Provider, watch: Watch, *, today: date | None = None
) -> list[Availability]:
    """Search the reversed route over a window wide enough to hold any valid return."""
    window = watch.return_window(today=today)
    if window is None:
        return []
    depart_after, depart_before = window
    query = SearchQuery(
        origins=tuple(watch.destinations),
        destinations=tuple(watch.origins),
        depart_after=depart_after,
        depart_before=depart_before,
        cabins=watch.cabins,
        programs=tuple(watch.programs),
    )
    return provider.search(query)


def _deliver(channels: list[Channel], deals: list[Deal], errors: list[str]) -> set[str]:
    """Send each deal to the channels scoped to its watch.

    Returns the keys of deals that at least one channel accepted. Tracking delivery
    per deal rather than per run matters once channels are scoped: a deal that no
    channel wanted, or whose only channel failed, must not be recorded as seen --
    otherwise it is silently swallowed and never alerted again.
    """
    delivered: set[str] = set()
    if not channels:
        errors.append("No notifiers configured; deals were found but not delivered.")
        return delivered

    for channel in channels:
        batch = [deal for deal in deals if channel.accepts(deal)]
        if not batch:
            continue
        try:
            channel.notifier.send(batch)
        except NotifierError as exc:
            log.error("Notifier %s failed: %s", channel.name, exc)
            errors.append(f"{channel.name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a broken channel must not kill the run
            log.exception("Notifier %s raised unexpectedly", channel.name)
            errors.append(f"{channel.name}: unexpected error: {exc}")
        else:
            # A channel nobody reads must not mark a deal as delivered, or its success
            # masks a real failure elsewhere and suppresses the alert for the cooldown.
            if channel.counts_as_delivery:
                delivered.update(deal.availability.key for deal in batch)

    undelivered = [deal for deal in deals if deal.availability.key not in delivered]
    if undelivered:
        by_watch = sorted({deal.watch_name for deal in undelivered})
        errors.append(
            f"{len(undelivered)} deal(s) reached no channel and were not recorded "
            f"(watches: {', '.join(by_watch)}). Add a notifier covering these watches."
        )
    return delivered


def run_once(
    config: Config,
    *,
    provider: Provider | None = None,
    store: Store | None = None,
    notifiers: list[Notifier | Channel] | None = None,
    dry_run: bool = False,
    today: date | None = None,
) -> RunResult:
    """Execute one full pass over all watches.

    Injectable collaborators keep this testable; when omitted they're built from
    config and closed on the way out. Bare notifiers passed in are treated as
    unscoped channels.
    """
    result = RunResult()

    owns_provider = provider is None
    owns_store = store is None
    try:
        provider = provider or build_provider(config.provider)
    except ProviderError as exc:
        result.errors.append(str(exc))
        return result

    store = store or Store(config.resolved_state_path)
    channels: list[Channel]
    if notifiers is None:
        try:
            channels = build_notifiers(config)
        except NotifierError as exc:
            # Fail before searching: alerting is the whole point, so a misconfigured
            # channel is worth surfacing immediately rather than after burning quota.
            result.errors.append(str(exc))
            if owns_store:
                store.close()
            if owns_provider:
                provider.close()
            return result
    else:
        channels = [n if isinstance(n, Channel) else Channel(notifier=n) for n in notifiers]

    try:
        for watch in config.watch:
            if not watch.enabled:
                log.info("%s: paused (enabled = false), skipping", watch.name)
                continue
            try:
                availabilities = search_watch(provider, watch, today=today)
            except ProviderError as exc:
                log.error("Watch %r failed: %s", watch.name, exc)
                result.errors.append(f"{watch.name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - isolate one bad watch from the rest
                log.exception("Watch %r raised unexpectedly", watch.name)
                result.errors.append(f"{watch.name}: unexpected error: {exc}")
                continue

            result.searched += len(availabilities)
            matched = rules.evaluate(watch, availabilities)

            # Only search returns when something qualified outbound -- no point spending
            # a second API call to pair returns against nothing.
            if matched and watch.return_trip is not None:
                try:
                    returns = search_returns(provider, watch, today=today)
                except ProviderError as exc:
                    # A failed return search must not lose the outbound deals.
                    log.error("Return search for %r failed: %s", watch.name, exc)
                    result.errors.append(f"{watch.name} (returns): {exc}")
                except Exception as exc:  # noqa: BLE001
                    log.exception("Return search for %r raised unexpectedly", watch.name)
                    result.errors.append(f"{watch.name} (returns): unexpected error: {exc}")
                else:
                    result.searched += len(returns)
                    matched = rules.attach_returns(watch, matched, returns)
                    paired = sum(1 for deal in matched if deal.returns)
                    log.info(
                        "%s: %d of %d outbounds paired with a return",
                        watch.name,
                        paired,
                        len(matched),
                    )

            log.info(
                "%s: %d records, %d under threshold",
                watch.name,
                len(availabilities),
                len(matched),
            )
            result.matched.extend(matched)

        # Watches can overlap on purpose -- e.g. a program-restricted watch alongside a
        # broad one. The same award then matches twice, and since dedupe is keyed on the
        # award rather than the watch, both copies would land in one alert. Keep the
        # first match in config order, so an award is attributed to the watch you listed
        # first (put your most specific watch first).
        seen_keys: set[str] = set()
        unique: list[Deal] = []
        for deal in result.matched:
            if deal.availability.key in seen_keys:
                continue
            seen_keys.add(deal.availability.key)
            unique.append(deal)
        result.matched = unique

        # Group by watch in config order, cheapest first within each.
        watch_order = {watch.name: index for index, watch in enumerate(config.watch)}
        result.matched.sort(
            key=lambda deal: (
                watch_order.get(deal.watch_name, len(watch_order)),
                deal.availability.miles,
                deal.availability.taxes if deal.availability.taxes is not None else 0.0,
            )
        )

        to_alert = store.select_alertable(result.matched, config.dedupe)
        if to_alert:
            if dry_run:
                result.alerted = to_alert
            else:
                delivered = _deliver(channels, to_alert, result.errors)
                result.alerted = [deal for deal in to_alert if deal.availability.key in delivered]
                if result.alerted:
                    store.record_alerted(result.alerted)

        if not dry_run:
            store.record_run(
                found=len(result.matched),
                alerted=len(result.alerted),
                error="; ".join(result.errors) or None,
            )
        return result
    finally:
        if owns_store:
            store.close()
        if owns_provider:
            provider.close()
