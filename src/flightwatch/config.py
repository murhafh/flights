"""TOML-backed configuration."""

from __future__ import annotations

import os
import re
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .airports import expand_airports
from .models import Cabin
from .programs import expand_programs

_RELATIVE = re.compile(r"^([+-]?\d+)\s*([dwmy])$", re.IGNORECASE)
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def resolve_date(spec: str | date, *, today: date | None = None) -> date:
    """Accept an absolute ISO date, a relative offset like "+21d" / "10m", or "today"."""
    if isinstance(spec, date):
        return spec
    base = today or date.today()
    text = str(spec).strip()
    if text.lower() == "today":
        return base
    if match := _RELATIVE.match(text):
        amount, unit = match.groups()
        return base + timedelta(days=int(amount) * _UNIT_DAYS[unit.lower()])
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"{spec!r} is not a date. Use an ISO date (2026-09-01), "
            'a relative offset ("+21d", "10m"), or "today".'
        ) from exc


class Threshold(BaseModel):
    """The bar a redemption must clear in one cabin before it's worth an alert.

    Set exactly one of `max_miles` (inclusive: 80000 qualifies) or `under_miles`
    (strict: 80000 does not). Award prices cluster on round numbers, so an award sitting
    exactly on your limit is a real case worth being able to exclude.
    """

    model_config = ConfigDict(extra="forbid")

    cabin: Cabin
    max_miles: int | None = Field(default=None, gt=0)
    """Inclusive ceiling: an award at exactly this price qualifies."""
    under_miles: int | None = Field(default=None, gt=0)
    """Strict ceiling: an award must come in below this price, not at it."""
    max_taxes: float | None = Field(default=None, ge=0)
    """Cap on cash surcharges, in the currency the provider reports (usually USD)."""
    min_seats: int = Field(default=1, ge=1)
    nonstop_only: bool = False

    @model_validator(mode="after")
    def _exactly_one_ceiling(self) -> Threshold:
        if (self.max_miles is None) == (self.under_miles is None):
            raise ValueError(
                f"threshold for {self.cabin.value} needs exactly one of max_miles "
                "(inclusive) or under_miles (strict), not both or neither"
            )
        return self

    @property
    def limit(self) -> int:
        """The numeric ceiling, whichever form was configured."""
        return self.max_miles if self.max_miles is not None else self.under_miles  # type: ignore[return-value]

    @property
    def strict(self) -> bool:
        return self.under_miles is not None

    def allows(self, miles: int) -> bool:
        return miles < self.limit if self.strict else miles <= self.limit

    def describe(self) -> str:
        return f"{self.cabin.label} {'<' if self.strict else '<='} {self.limit:,}"


class ReturnTrip(BaseModel):
    """Look for a return leg this many days after each qualifying outbound.

    Returns are evaluated against the same thresholds as the outbound, on the reversed
    route. Finding none never suppresses the outbound alert -- a one-way deal is still a
    deal; the return is extra information when it happens to exist.
    """

    model_config = ConfigDict(extra="forbid")

    min_days: int = Field(default=7, ge=1)
    max_days: int = Field(default=14, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> ReturnTrip:
        if self.min_days > self.max_days:
            raise ValueError(
                f"return_trip min_days ({self.min_days}) is greater than max_days ({self.max_days})"
            )
        return self


class Watch(BaseModel):
    """One standing search: some O&Ds, a departure window, and per-cabin thresholds."""

    model_config = ConfigDict(extra="forbid")

    name: str
    origins: list[str] = Field(min_length=1)
    destinations: list[str] = Field(min_length=1)
    thresholds: list[Threshold] = Field(min_length=1)
    enabled: bool = True
    """Set false to pause a watch without deleting it. Paused watches are not searched
    at all, so they cost no API calls, and their alert history is kept -- re-enabling
    won't replay deals you've already been told about."""
    programs: list[str] = Field(default_factory=list)
    depart_after: str = "+1d"
    depart_before: str = "+330d"
    return_trip: ReturnTrip | None = None
    """When set, pair each outbound with qualifying returns in this day range."""
    airlines_exclude: list[str] = Field(default_factory=list)
    """Marketing/operating carriers to drop, e.g. ["AC"] to avoid a specific airline."""

    @field_validator("origins", "destinations", mode="after")
    @classmethod
    def _airports_upper(cls, value: list[str]) -> list[str]:
        codes = [code.strip().upper() for code in value]
        for code in codes:
            if not re.fullmatch(r"[A-Z]{3}", code):
                raise ValueError(f"{code!r} is not a 3-letter IATA airport code")
        # Metro codes like LON must become real airports here: the API returns records
        # keyed by airport, and the route filter in `rules` compares against this list.
        return expand_airports(codes)

    @field_validator("programs", mode="after")
    @classmethod
    def _programs_lower(cls, value: list[str]) -> list[str]:
        # Resolves aliases like "@chase" and rejects unknown program names.
        return expand_programs(value)

    @field_validator("airlines_exclude", mode="after")
    @classmethod
    def _airlines_upper(cls, value: list[str]) -> list[str]:
        return [code.strip().upper() for code in value]

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Watch:
        after, before = self.window()
        if after > before:
            raise ValueError(
                f"watch {self.name!r}: depart_after ({after}) is later than "
                f"depart_before ({before})"
            )
        return self

    def window(self, *, today: date | None = None) -> tuple[date, date]:
        return (
            resolve_date(self.depart_after, today=today),
            resolve_date(self.depart_before, today=today),
        )

    def return_window(self, *, today: date | None = None) -> tuple[date, date] | None:
        """Date range wide enough to contain a return for any outbound in the window."""
        if self.return_trip is None:
            return None
        after, before = self.window(today=today)
        return (
            after + timedelta(days=self.return_trip.min_days),
            before + timedelta(days=self.return_trip.max_days),
        )

    @property
    def cabins(self) -> tuple[Cabin, ...]:
        # dict.fromkeys to dedupe while keeping config order stable.
        return tuple(dict.fromkeys(threshold.cabin for threshold in self.thresholds))


class DedupeConfig(BaseModel):
    """Controls repeat alerts for an award we've already told you about."""

    model_config = ConfigDict(extra="forbid")

    cooldown_days: int = Field(default=3, ge=0)
    """Re-alert an unchanged award after this long. 0 means never re-alert."""
    realert_on_improvement: int = Field(default=5_000, ge=0)
    """Re-alert immediately if the price drops by at least this many miles."""


class _NotifierBase(BaseModel):
    """Shared notifier settings."""

    model_config = ConfigDict(extra="forbid")

    watches: list[str] = Field(default_factory=list)
    """Restrict this channel to these watch names. Empty means every watch.

    Use it to keep a noisy exploratory watch on the console while only the watch you
    genuinely care about reaches your inbox. Names are validated against the configured
    watches at load time, so a rename can't silently disable a channel.
    """


class ConsoleNotifier(_NotifierBase):
    kind: Literal["console"] = "console"


class TelegramNotifier(_NotifierBase):
    kind: Literal["telegram"] = "telegram"
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"


class SlackNotifier(_NotifierBase):
    kind: Literal["slack"] = "slack"
    webhook_url_env: str = "SLACK_WEBHOOK_URL"


class ResendNotifier(_NotifierBase):
    kind: Literal["resend"] = "resend"
    api_key_env: str = "RESEND_API_KEY"
    sender: str
    """Must be on a domain verified in Resend, or `onboarding@resend.dev` for testing
    (which only delivers to your own Resend account address)."""
    recipients: list[str] = Field(min_length=1)
    reply_to: str | None = None


class EmailNotifier(_NotifierBase):
    kind: Literal["email"] = "email"
    host: str
    port: int = 587
    username_env: str = "SMTP_USERNAME"
    password_env: str = "SMTP_PASSWORD"
    sender: str
    recipients: list[str] = Field(min_length=1)
    use_tls: bool = True


NotifierConfig = Annotated[
    ConsoleNotifier | TelegramNotifier | SlackNotifier | EmailNotifier | ResendNotifier,
    Field(discriminator="kind"),
]


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["seats_aero", "mock"] = "seats_aero"
    api_key_env: str = "SEATS_AERO_API_KEY"
    base_url: str = "https://seats.aero/partnerapi"
    requests_per_minute: int = Field(default=60, gt=0)
    """Client-side throttle. Keep at or below your plan's documented rate limit."""
    taxes_in_cents: bool = False
    """Whether the API reports cash surcharges in minor units. Confirm with
    `flightwatch probe`: if taxes come back looking 100x too large, set this to true."""
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    booking_url_template: str = (
        "https://seats.aero/search?min_seats=1&applicable_cabin=any"
        "&additional_days=true&additional_days_num=7&max_fees=999999"
        "&disable_live_filtering=true&date={date}&origins={origin}"
        "&destinations={destination}&view=default"
    )
    """Link put in alerts. Supports {origin}, {destination}, {date} (ISO), {source} and
    {record_id}.

    This is a real seats.aero UI URL with only the route and date substituted, in the
    original parameter order. Dropping the params that look irrelevant (`additional_days`,
    `disable_live_filtering`, `max_fees`) produces a page that loads with the cities
    filled in but never runs the search -- the app appears to need the full set to treat
    the URL as a complete search state. Keep them all.

    The one deviation is `max_fees`, raised from the observed 40000 (i.e. $400) to
    999999, because it filters the site's own results: a link carrying a $400 cap would
    hide the very award being alerted whenever surcharges exceed it, which they do on
    Virgin Atlantic and British Airways transatlantic redemptions.

    Note there is no program parameter -- seats.aero cannot be deep-linked to one
    mileage program, so the alert text carries that instead."""

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watch: list[Watch] = Field(min_length=1)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    notify: list[NotifierConfig] = Field(default_factory=lambda: [ConsoleNotifier()])
    state_path: str = "~/.local/state/flightwatch/state.db"

    @property
    def resolved_state_path(self) -> Path:
        return Path(self.state_path).expanduser()

    @model_validator(mode="after")
    def _notifier_watches_exist(self) -> Config:
        """A notifier scoped to a watch name that no longer exists would silently stop
        delivering, so reject it at load time instead."""
        names = {watch.name for watch in self.watch}
        for entry in self.notify:
            unknown = [name for name in entry.watches if name not in names]
            if unknown:
                listed = ", ".join(sorted(names))
                raise ValueError(
                    f"notifier {entry.kind!r} is scoped to unknown watch(es) "
                    f"{unknown}. Configured watches: {listed}"
                )
        return self

    @classmethod
    def load(cls, path: str | Path) -> Config:
        target = Path(path).expanduser()
        if not target.is_file():
            raise FileNotFoundError(
                f"No config at {target}. Run `flightwatch init` to write a starter file."
            )
        with target.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))


DEFAULT_CONFIG_PATH = Path("~/.config/flightwatch/config.toml")
