"""seats.aero Partner API client.

Schema note: the field names below match the Partner API's cached-search response as
of writing, but the parsing is deliberately tolerant -- unknown keys are ignored,
absent keys fall back to None, and numeric fields accept either strings or numbers
(the cached-search endpoint returns mileage costs as strings). Run
`flightwatch probe` to dump a real response and confirm the shape against your plan.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

import httpx

from ..models import Availability, Cabin, SearchQuery
from .base import ProviderError, RateLimiter

log = logging.getLogger(__name__)

PAGE_SIZE = 500
"""Records per request. The API caps `take`; 500 is a safe page size."""

MAX_PAGES = 40
"""Backstop so a broad watch can't spin forever on a paginating endpoint."""

_RETRY_STATUS = {429, 500, 502, 503, 504}


def _to_int(value: Any) -> int | None:
    """Coerce a mileage/seat field that may arrive as int, float, or numeric string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value or None
    if isinstance(value, float):
        return int(value) or None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(float(text)) or None
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _to_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value)
    # ParsedDate arrives as a full timestamp; Date as a bare YYYY-MM-DD.
    for candidate in (text, text.split("T")[0]):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _to_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _airlines(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, list):  # noqa: SIM108 - clearer than a wrapped ternary
        parts = [str(item) for item in value]
    else:
        parts = str(value).split(",")
    return tuple(part.strip().upper() for part in parts if part.strip())


class SeatsAeroProvider:
    name = "seats_aero"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://seats.aero/partnerapi",
        requests_per_minute: int = 60,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        taxes_in_cents: bool = False,
        booking_url_template: str = (
            "https://seats.aero/search?min_seats=1&applicable_cabin=any"
            "&additional_days=true&additional_days_num=7&max_fees=999999"
            "&disable_live_filtering=true&date={date}&origins={origin}"
            "&destinations={destination}&view=default"
        ),
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._taxes_divisor = 100.0 if taxes_in_cents else 1.0
        self._booking_url_template = booking_url_template
        self._limiter = RateLimiter(requests_per_minute)
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Partner-Authorization": api_key,
                "Accept": "application/json",
                "User-Agent": "flightwatch/0.1",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SeatsAeroProvider:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- HTTP -------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            self._limiter.wait()
            try:
                response = self._client.get(url, params=params or None)
            except httpx.ProxyError as exc:
                # A blocked CONNECT is not going to succeed on retry, and the proxy's
                # own status code looks deceptively like an upstream auth failure.
                raise ProviderError(
                    f"A network proxy refused the connection to {url} ({exc}). "
                    "This is the proxy talking, not seats.aero -- the host needs to be "
                    "allowed, or run flightwatch from an unproxied network."
                ) from exc
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("seats.aero request failed (%s), attempt %d", exc, attempt + 1)
            else:
                if response.status_code == 401:
                    raise ProviderError(
                        "seats.aero rejected the API key (401). Note that a seats.aero Pro "
                        "website subscription is separate from a Partner API key."
                    )
                if response.status_code == 403:
                    raise ProviderError(
                        "seats.aero returned 403. Your key may lack access to this endpoint "
                        "on the current plan."
                    )
                if response.status_code not in _RETRY_STATUS:
                    response.raise_for_status()
                    payload = response.json()
                    return payload if isinstance(payload, dict) else {"data": payload}

                last_error = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                retry_after = _to_float(response.headers.get("Retry-After"))
                backoff = retry_after if retry_after is not None else 2.0**attempt
                if attempt < self._max_retries:
                    log.warning(
                        "seats.aero returned %d; backing off %.1fs",
                        response.status_code,
                        backoff,
                    )
                    time.sleep(backoff)

        raise ProviderError(f"seats.aero request to {url} failed after retries: {last_error}")

    # --- Search -----------------------------------------------------------

    def _taxes(self, value: Any) -> float | None:
        amount = _to_float(value)
        return None if amount is None else amount / self._taxes_divisor

    def _booking_url(
        self, origin: str, destination: str, depart: date, source: str, rid: str
    ) -> str:
        """Render the configured link template. A template with an unknown placeholder
        falls back to the bare URL rather than raising -- a bad link in config should not
        take down the whole run."""
        try:
            return self._booking_url_template.format(
                origin=origin,
                destination=destination,
                date=depart.isoformat(),
                source=source,
                record_id=rid,
            )
        except (KeyError, IndexError, ValueError):
            log.warning(
                "booking_url_template has an unsupported placeholder: %r. "
                "Supported: {origin} {destination} {date} {source} {record_id}",
                self._booking_url_template,
            )
            return self._booking_url_template.split("{")[0]

    def _search_params(self, query: SearchQuery) -> dict[str, Any]:
        params: dict[str, Any] = {
            "origin_airport": ",".join(query.origins),
            "destination_airport": ",".join(query.destinations),
            "start_date": query.depart_after.isoformat(),
            "end_date": query.depart_before.isoformat(),
            "take": PAGE_SIZE,
        }
        if query.programs:
            params["sources"] = ",".join(query.programs)
        if len(query.cabins) == 1:
            # Only narrow server-side for a single cabin; the API takes one value.
            params["cabin"] = query.cabins[0].value
        return params

    def raw_search(self, query: SearchQuery) -> dict[str, Any]:
        """Single un-paginated request. Used by `flightwatch probe` for schema checks."""
        return self._get("search", self._search_params(query))

    def _pages(self, query: SearchQuery) -> Iterator[list[dict[str, Any]]]:
        params = self._search_params(query)
        skip = 0
        for page in range(MAX_PAGES):
            payload = self._get("search", {**params, "skip": skip})
            records = payload.get("data") or []
            if not isinstance(records, list):
                raise ProviderError(f"Unexpected `data` in seats.aero response: {type(records)}")
            yield records

            has_more = _to_bool(payload.get("hasMore", payload.get("has_more", False)))
            if not has_more or not records:
                return
            # Prefer a server-provided cursor when present; fall back to offset paging.
            cursor = payload.get("cursor")
            if cursor:
                params = {**params, "cursor": cursor}
            skip += len(records)
            if page == MAX_PAGES - 1:
                log.warning(
                    "Stopped paginating %s->%s at %d pages; narrow the date window "
                    "to see the rest.",
                    ",".join(query.origins),
                    ",".join(query.destinations),
                    MAX_PAGES,
                )
        return

    def search(self, query: SearchQuery) -> list[Availability]:
        results: list[Availability] = []
        for records in self._pages(query):
            for record in records:
                results.extend(self._expand(record, query.cabins))
        return results

    def _expand(self, record: dict[str, Any], cabins: tuple[Cabin, ...]) -> list[Availability]:
        """Fan one route-day record out into one Availability per cabin and routing type.

        Reads the plain fields (`JAvailable`, `JMileageCost`), never the `*Raw` variants.
        The Raw set includes dynamic-pricing junk that the plain set filters out -- a real
        SFO-DUB record showed `JAvailable: false` alongside `JMileageCostRaw: 200000`,
        i.e. business space existed only at 200k miles. Treating that as availability
        would bury genuine saver space under noise.
        """
        route = record.get("Route") or {}
        origin = route.get("OriginAirport") or record.get("OriginAirport")
        destination = route.get("DestinationAirport") or record.get("DestinationAirport")
        depart = _to_date(record.get("Date") or record.get("ParsedDate"))
        source = record.get("Source") or route.get("Source")

        if not (origin and destination and depart and source):
            log.debug("Skipping record with missing route/date/source: %s", record.get("ID"))
            return []

        common = {
            "source": str(source).lower(),
            "origin": str(origin).upper(),
            "destination": str(destination).upper(),
            "depart": depart,
            "record_id": str(record.get("ID") or ""),
            "taxes_currency": record.get("TaxesCurrency"),
            "distance": _to_int(route.get("Distance")),
            "updated_at": _to_datetime(record.get("UpdatedAt")),
            "booking_url": self._booking_url(
                str(origin).upper(),
                str(destination).upper(),
                depart,
                str(source).lower(),
                str(record.get("ID") or ""),
            ),
            "raw": record,
        }

        out: list[Availability] = []
        for cabin in cabins:
            code = cabin.code
            if not _to_bool(record.get(f"{code}Available")):
                continue

            taxes = self._taxes(record.get(f"{code}TotalTaxes"))
            airlines = _airlines(record.get(f"{code}Airlines"))
            seats = _to_int(record.get(f"{code}RemainingSeats"))

            # Cheapest award on any routing.
            if (miles := _to_int(record.get(f"{code}MileageCost"))) is not None:
                out.append(
                    Availability(
                        **common,
                        cabin=cabin,
                        miles=miles,
                        direct=False,
                        seats=seats,
                        taxes=taxes,
                        airlines=airlines,
                    )
                )

            # Cheapest nonstop, when one exists. Emitted separately so a nonstop_only
            # threshold can be evaluated honestly rather than assuming the cheap fare
            # was the nonstop one.
            if _to_bool(record.get(f"{code}Direct")):
                direct_miles = _to_int(record.get(f"{code}DirectMileageCost"))
                if direct_miles is not None:
                    out.append(
                        Availability(
                            **common,
                            cabin=cabin,
                            miles=direct_miles,
                            direct=True,
                            seats=_to_int(record.get(f"{code}DirectRemainingSeats")) or seats,
                            taxes=self._taxes(record.get(f"{code}DirectTotalTaxes")) or taxes,
                            airlines=_airlines(record.get(f"{code}DirectAirlines")) or airlines,
                        )
                    )
        return out
