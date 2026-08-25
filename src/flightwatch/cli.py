"""Command-line interface."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import rules
from .config import DEFAULT_CONFIG_PATH, Config, Threshold, Watch, resolve_date
from .models import Availability, Cabin, Deal, SearchQuery
from .notify import NotifierError, build_notifiers
from .notify.console import ConsoleNotifier
from .providers import ProviderError, SeatsAeroProvider, build_provider
from .redact import RedactingFormatter
from .state import Store
from .watch import run_once, search_watch

console = Console()
err_console = Console(stderr=True)

STARTER_CONFIG = """\
# flightwatch configuration.
# Docs: see README.md. Validate changes with `flightwatch check`.

state_path = "~/.local/state/flightwatch/state.db"

[provider]
kind = "seats_aero"          # or "mock" to try things out with sample data
api_key_env = "SEATS_AERO_API_KEY"
requests_per_minute = 60     # keep at or below your plan's rate limit

[dedupe]
cooldown_days = 3            # re-alert an unchanged award after this long (0 = never)
realert_on_improvement = 5000  # ...or immediately if it drops this many miles

[[notify]]
kind = "console"

# [[notify]]
# kind = "telegram"
# bot_token_env = "TELEGRAM_BOT_TOKEN"
# chat_id_env = "TELEGRAM_CHAT_ID"

[[watch]]
name = "West coast -> Europe, business"
origins = ["SFO", "OAK", "SJC"]
destinations = ["LHR", "CDG", "AMS", "FRA", "MAD", "LIS"]
programs = ["aeroplan", "flyingblue", "virginatlantic"]
depart_after = "+21d"        # ISO date, or a relative offset like "+21d" / "6m"
depart_before = "+300d"

  [[watch.thresholds]]
  cabin = "business"
  max_miles = 70000
  max_taxes = 150
  min_seats = 2

[[watch]]
name = "Anywhere Asia, first class splurge"
origins = ["SFO", "LAX"]
destinations = ["HND", "NRT", "SIN", "BKK", "HKG"]
depart_after = "+30d"
depart_before = "+330d"

  [[watch.thresholds]]
  cabin = "first"
  max_miles = 120000
  min_seats = 1

  [[watch.thresholds]]
  cabin = "business"
  max_miles = 75000
"""


def _setup_logging(verbose: bool) -> None:
    """Configure stderr logging with credential redaction.

    httpx logs request URLs, and Telegram puts the bot token in the URL path, so raw
    logging would leak live credentials into any logfile. Every handler gets the
    redacting formatter rather than us trying to silence individual libraries.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)


def _load(config_path: Path) -> Config:
    try:
        return Config.load(config_path)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Invalid config at {config_path}:\n{exc}") from exc


config_option = click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to config.toml.",
)
verbose_option = click.option("-v", "--verbose", is_flag=True, help="Debug logging to stderr.")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="flightwatch")
def main() -> None:
    """Watch award availability and alert when redemptions drop below your thresholds."""


@main.command()
@config_option
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
def init(config_path: Path, force: bool) -> None:
    """Write a starter config file."""
    target = config_path.expanduser()
    if target.exists() and not force:
        raise click.ClickException(f"{target} already exists. Pass --force to overwrite it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STARTER_CONFIG)
    console.print(f"Wrote [bold]{target}[/bold]")
    console.print("Next: edit your routes and thresholds, then run [bold]flightwatch check[/bold].")


@main.command()
@config_option
def check(config_path: Path) -> None:
    """Validate config and credentials without calling the API."""
    config = _load(config_path)
    console.print(f"Config [green]OK[/green]: {len(config.watch)} watch(es)")

    for watch in config.watch:
        after, before = watch.window()
        cabins = ", ".join(f"{threshold.describe()}" for threshold in watch.thresholds)
        console.print(
            f"  [bold]{watch.name}[/bold]: "
            f"{len(watch.origins)}x{len(watch.destinations)} O&Ds, "
            f"{after} to {before} ({(before - after).days}d) | {cabins}"
        )

    problems: list[str] = []
    if config.provider.kind == "seats_aero" and not config.provider.api_key():
        problems.append(f"${config.provider.api_key_env} is not set")
    try:
        notifiers = build_notifiers(config)
        console.print(f"Notifiers: {', '.join(n.name for n in notifiers)}")
    except NotifierError as exc:
        problems.append(str(exc))

    if problems:
        for problem in problems:
            err_console.print(f"[red]![/red] {problem}")
        raise SystemExit(1)
    console.print("[green]Ready.[/green]")


@main.command(name="watch")
@config_option
@verbose_option
@click.option("--dry-run", is_flag=True, help="Find and print deals without sending or recording.")
@click.option("--quiet", is_flag=True, help="Print nothing when there are no new deals.")
def watch_cmd(config_path: Path, verbose: bool, dry_run: bool, quiet: bool) -> None:
    """Run one pass over every watch and alert on new deals. This is the cron entrypoint."""
    _setup_logging(verbose)
    config = _load(config_path)
    result = run_once(config, dry_run=dry_run)

    if dry_run and result.alerted:
        ConsoleNotifier(console).send(result.alerted)

    if not quiet or result.alerted or result.errors:
        console.print(
            f"[dim]{result.searched} records, {len(result.matched)} under threshold, "
            f"{len(result.alerted)} alerted"
            f"{' (dry run)' if dry_run else ''}[/dim]"
        )

    for error in result.errors:
        err_console.print(f"[red]![/red] {error}")
    if result.errors:
        raise SystemExit(1)


@main.command()
@config_option
@verbose_option
@click.argument("origins")
@click.argument("destinations")
@click.option(
    "--cabin",
    type=click.Choice([cabin.value for cabin in Cabin]),
    default="business",
    show_default=True,
)
@click.option("--max-miles", type=int, help="Only show awards at or below this price.")
@click.option("--under-miles", type=int, help="Only show awards strictly below this price.")
@click.option("--after", default="+1d", show_default=True, help='ISO date or offset like "+21d".')
@click.option("--before", default="+180d", show_default=True)
@click.option("--programs", help="Comma-separated mileage programs to restrict to.")
@click.option("--limit", type=int, default=40, show_default=True)
def search(
    config_path: Path,
    verbose: bool,
    origins: str,
    destinations: str,
    cabin: str,
    max_miles: int | None,
    under_miles: int | None,
    after: str,
    before: str,
    programs: str | None,
    limit: int,
) -> None:
    """Ad-hoc search. Touches no state and sends no alerts.

    ORIGINS and DESTINATIONS are comma-separated IATA codes, e.g. "SFO,OAK" "LHR,CDG".
    """
    _setup_logging(verbose)
    config = _load(config_path)

    if max_miles and under_miles:
        raise click.ClickException("Pass --max-miles or --under-miles, not both.")
    ceiling = (
        {"under_miles": under_miles} if under_miles else {"max_miles": max_miles or 10_000_000}
    )

    # Reuse Watch validation so an ad-hoc search rejects bad airport codes the same way.
    ad_hoc = Watch(
        name="ad-hoc",
        origins=origins.split(","),
        destinations=destinations.split(","),
        programs=programs.split(",") if programs else [],
        depart_after=after,
        depart_before=before,
        thresholds=[Threshold(cabin=Cabin(cabin), **ceiling)],
    )

    try:
        provider = build_provider(config.provider)
    except ProviderError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        availabilities = search_watch(provider, ad_hoc)
    except ProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        provider.close()

    deals = rules.evaluate(ad_hoc, availabilities)
    console.print(f"[dim]{len(availabilities)} records, {len(deals)} matching[/dim]")
    ConsoleNotifier(console).send(deals[:limit])
    if len(deals) > limit:
        console.print(f"[dim]... {len(deals) - limit} more (raise --limit to see them)[/dim]")


@main.command()
@config_option
@verbose_option
@click.argument("origin")
@click.argument("destination")
@click.option("--after", default="+30d", show_default=True)
@click.option("--before", default="+60d", show_default=True)
def probe(
    config_path: Path, verbose: bool, origin: str, destination: str, after: str, before: str
) -> None:
    """Dump one raw API response, to confirm the live schema matches the parser.

    Worth running once when you first set up a key, and again if results ever look
    wrong -- the field names in the response are what the parser keys off.
    """
    _setup_logging(verbose)
    config = _load(config_path)
    provider = build_provider(config.provider)
    if not isinstance(provider, SeatsAeroProvider):
        raise click.ClickException(
            f"probe only supports the seats_aero provider, not {provider.name}"
        )

    query = SearchQuery(
        origins=(origin.upper(),),
        destinations=(destination.upper(),),
        depart_after=resolve_date(after),
        depart_before=resolve_date(before),
        cabins=tuple(Cabin),
    )
    try:
        payload = provider.raw_search(query)
    except ProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        provider.close()

    records = payload.get("data") or []
    console.print(f"[dim]Top-level keys: {sorted(payload)}[/dim]")
    console.print(f"[dim]{len(records)} record(s)[/dim]")
    if records:
        console.print_json(json.dumps(records[0], default=str))
        parsed = provider._expand(records[0], tuple(Cabin))  # noqa: SLF001 - diagnostic
        console.print(f"[dim]Parser extracted {len(parsed)} availability row(s):[/dim]")
        for availability in parsed:
            console.print(f"  {availability}")
    else:
        console.print_json(json.dumps(payload, default=str)[:4000])


@main.command(name="test-notify")
@config_option
def test_notify(config_path: Path) -> None:
    """Send a fabricated deal through every configured channel."""
    config = _load(config_path)
    sample = Deal(
        availability=Availability(
            source="aeroplan",
            origin="SFO",
            destination="LHR",
            depart=resolve_date("+45d"),
            cabin=Cabin.BUSINESS,
            miles=60_000,
            direct=True,
            seats=2,
            taxes=98.4,
            taxes_currency="USD",
            airlines=("AC",),
            distance=8_616,
        ),
        watch_name="test-notify",
        max_miles=70_000,
    )

    try:
        notifiers = build_notifiers(config)
    except NotifierError as exc:
        raise click.ClickException(str(exc)) from exc

    failures = 0
    for channel in notifiers:
        # Bypass the watch scoping here on purpose: test-notify exists to prove every
        # channel's credentials work, not to replay routing rules.
        try:
            channel.notifier.send([sample])
            scope = f" (scoped to {len(channel.watches)} watch(es))" if channel.watches else ""
            console.print(f"[green]sent[/green] via {channel.name}{scope}")
        except NotifierError as exc:
            failures += 1
            err_console.print(f"[red]failed[/red] via {channel.name}: {exc}")
    if failures:
        raise SystemExit(1)


@main.command(name="telegram-setup")
@click.option(
    "--token-env",
    default="TELEGRAM_BOT_TOKEN",
    show_default=True,
    help="Env var holding the bot token from @BotFather.",
)
def telegram_setup(token_env: str) -> None:
    """Validate a Telegram bot token and find your chat id.

    Reads the token from the environment rather than an argument, so it never lands in
    your shell history. Steps:

    \b
      1. In Telegram, message @BotFather and send /newbot
      2. Put the token it gives you in .env as TELEGRAM_BOT_TOKEN=...
      3. Send your new bot any message (it can't find you until you do)
      4. Run this command
    """
    from .notify.telegram import discover_chats, verify_token

    token = os.environ.get(token_env)
    if not token:
        raise click.ClickException(
            f"${token_env} is not set. Create a bot with @BotFather, then add "
            f"{token_env}=<token> to your .env and re-run."
        )

    try:
        username = verify_token(token)
        console.print(f"Token is valid, bot is [bold]@{username}[/bold]")
        chats = discover_chats(token)
    except NotifierError as exc:
        raise click.ClickException(str(exc)) from exc

    if not chats:
        err_console.print(
            "[yellow]No chats found.[/yellow] Telegram only reveals your chat id after "
            f"you message the bot. Open Telegram, send @{username} any message, then "
            "run this again."
        )
        raise SystemExit(1)

    table = Table(title="Chats that have messaged this bot", title_justify="left")
    table.add_column("Chat ID")
    table.add_column("Who")
    for chat_id, label in chats:
        table.add_row(chat_id, label)
    console.print(table)

    chat_id = chats[0][0]
    console.print("\nAdd this to your .env:")
    console.print(f"  [bold]TELEGRAM_CHAT_ID={chat_id}[/bold]")
    console.print("\nThen make sure your config has:")
    # markup=False: rich would otherwise treat [[notify]] as escaped markup and eat it.
    console.print('  [[notify]]\n  kind = "telegram"', markup=False)
    console.print("\nAnd verify with: [dim]flightwatch test-notify[/dim]")


@main.command()
@config_option
@click.option("--limit", type=int, default=15, show_default=True)
def history(config_path: Path, limit: int) -> None:
    """Show recent runs and currently-tracked awards."""
    config = _load(config_path)
    with Store(config.resolved_state_path) as store:
        runs = store.recent_runs(limit)
        if not runs:
            console.print("[dim]No runs recorded yet.[/dim]")
            return
        table = Table(title="Recent runs", title_justify="left", header_style="bold")
        table.add_column("When")
        table.add_column("Found", justify="right")
        table.add_column("Alerted", justify="right")
        table.add_column("Error")
        for row in runs:
            table.add_row(
                row["started_at"][:19].replace("T", " "),
                str(row["found"]),
                str(row["alerted"]),
                (row["error"] or "")[:60],
            )
        console.print(table)


@main.command()
@config_option
@click.option("--older-than-days", type=int, default=120, show_default=True)
def prune(config_path: Path, older_than_days: int) -> None:
    """Forget alert history older than the given age."""
    config = _load(config_path)
    with Store(config.resolved_state_path) as store:
        removed = store.prune(older_than_days=older_than_days)
    console.print(f"Removed {removed} record(s).")


if __name__ == "__main__":  # pragma: no cover
    main()
