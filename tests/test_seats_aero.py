from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from flightwatch.models import Cabin, SearchQuery
from flightwatch.providers import ProviderError, SeatsAeroProvider

BASE = "https://seats.aero/partnerapi"

QUERY = SearchQuery(
    origins=("SFO", "OAK"),
    destinations=("LHR", "CDG"),
    depart_after=date(2026, 10, 1),
    depart_before=date(2026, 10, 31),
    cabins=(Cabin.BUSINESS, Cabin.ECONOMY),
    programs=("aeroplan", "flyingblue"),
)

RECORD = {
    "ID": "rec-1",
    "RouteID": "route-1",
    "Route": {
        "OriginAirport": "SFO",
        "DestinationAirport": "LHR",
        "Distance": 8616,
        "Source": "aeroplan",
    },
    "Date": "2026-10-01",
    "ParsedDate": "2026-10-01T00:00:00Z",
    "Source": "aeroplan",
    "TaxesCurrency": "USD",
    "UpdatedAt": "2026-08-21T09:00:00Z",
    # Mileage costs arrive as strings from the cached-search endpoint.
    "JAvailable": True,
    "JMileageCost": "60000",
    "JRemainingSeats": 2,
    "JAirlines": "AC, LH",
    "JTotalTaxes": "98",
    "JDirect": True,
    "JDirectMileageCost": "70000",
    "JDirectRemainingSeats": 1,
    "YAvailable": False,
    "YMileageCost": "0",
}


@pytest.fixture
def provider() -> SeatsAeroProvider:
    # A high rate limit keeps the throttle from slowing the suite down.
    return SeatsAeroProvider(api_key="test-key", requests_per_minute=100_000, max_retries=2)


@respx.mock
def test_parses_a_record_into_availabilities(provider: SeatsAeroProvider) -> None:
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [RECORD], "hasMore": False})
    )
    results = provider.search(QUERY)

    # Business only: economy is marked unavailable. One connecting + one nonstop.
    assert len(results) == 2
    connecting, nonstop = results

    assert connecting.miles == 60_000
    assert connecting.direct is False
    assert connecting.seats == 2
    assert connecting.taxes == 98.0
    assert connecting.airlines == ("AC", "LH")
    assert connecting.origin == "SFO"
    assert connecting.destination == "LHR"
    assert connecting.depart == date(2026, 10, 1)
    assert connecting.source == "aeroplan"
    assert connecting.distance == 8616
    assert connecting.cabin is Cabin.BUSINESS

    assert nonstop.miles == 70_000
    assert nonstop.direct is True
    assert nonstop.seats == 1


@respx.mock
def test_unavailable_cabin_is_skipped(provider: SeatsAeroProvider) -> None:
    record = {**RECORD, "JAvailable": False}
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [record], "hasMore": False})
    )
    assert provider.search(QUERY) == []


@respx.mock
def test_no_direct_flag_means_no_nonstop_row(provider: SeatsAeroProvider) -> None:
    record = {**RECORD, "JDirect": False}
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [record], "hasMore": False})
    )
    results = provider.search(QUERY)
    assert len(results) == 1
    assert results[0].direct is False


@respx.mock
def test_numeric_mileage_costs_also_parse(provider: SeatsAeroProvider) -> None:
    """The API is inconsistent about string vs number, so both must work."""
    record = {**RECORD, "JMileageCost": 60000, "JDirect": False}
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [record], "hasMore": False})
    )
    assert provider.search(QUERY)[0].miles == 60_000


@respx.mock
def test_taxes_in_cents_are_converted() -> None:
    provider = SeatsAeroProvider(api_key="k", requests_per_minute=100_000, taxes_in_cents=True)
    record = {**RECORD, "JTotalTaxes": "9840", "JDirect": False}
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [record], "hasMore": False})
    )
    assert provider.search(QUERY)[0].taxes == 98.4


@respx.mock
def test_record_missing_route_is_skipped(provider: SeatsAeroProvider) -> None:
    broken = {"ID": "x", "JAvailable": True, "JMileageCost": "50000"}
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [broken, RECORD], "hasMore": False})
    )
    # The valid record still comes through.
    assert len(provider.search(QUERY)) == 2


@respx.mock
def test_unknown_fields_are_ignored(provider: SeatsAeroProvider) -> None:
    record = {**RECORD, "SomeNewFieldTheyAdded": {"nested": 1}, "JDirect": False}
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [record], "hasMore": False})
    )
    assert len(provider.search(QUERY)) == 1


@respx.mock
def test_search_params_are_built_correctly(provider: SeatsAeroProvider) -> None:
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    provider.search(QUERY)

    params = route.calls[0].request.url.params
    assert params["origin_airport"] == "SFO,OAK"
    assert params["destination_airport"] == "LHR,CDG"
    assert params["start_date"] == "2026-10-01"
    assert params["end_date"] == "2026-10-31"
    assert params["sources"] == "aeroplan,flyingblue"
    # Two cabins requested, so no server-side cabin narrowing.
    assert "cabin" not in params


@respx.mock
def test_single_cabin_narrows_server_side(provider: SeatsAeroProvider) -> None:
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    provider.search(
        SearchQuery(
            origins=("SFO",),
            destinations=("LHR",),
            depart_after=date(2026, 10, 1),
            depart_before=date(2026, 10, 2),
            cabins=(Cabin.BUSINESS,),
        )
    )
    assert route.calls[0].request.url.params["cabin"] == "business"


@respx.mock
def test_api_key_is_sent_as_partner_authorization(provider: SeatsAeroProvider) -> None:
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    provider.search(QUERY)
    assert route.calls[0].request.headers["Partner-Authorization"] == "test-key"


@respx.mock
def test_pagination_follows_has_more(provider: SeatsAeroProvider) -> None:
    respx.get(f"{BASE}/search").mock(
        side_effect=[
            httpx.Response(200, json={"data": [RECORD], "hasMore": True}),
            httpx.Response(
                200,
                json={"data": [{**RECORD, "ID": "rec-2", "Date": "2026-10-02"}], "hasMore": False},
            ),
        ]
    )
    results = provider.search(QUERY)
    assert {availability.depart for availability in results} == {
        date(2026, 10, 1),
        date(2026, 10, 2),
    }


@respx.mock
def test_pagination_advances_skip(provider: SeatsAeroProvider) -> None:
    route = respx.get(f"{BASE}/search").mock(
        side_effect=[
            httpx.Response(200, json={"data": [RECORD, RECORD], "hasMore": True}),
            httpx.Response(200, json={"data": [], "hasMore": False}),
        ]
    )
    provider.search(QUERY)
    assert route.calls[1].request.url.params["skip"] == "2"


@respx.mock
def test_empty_page_stops_pagination(provider: SeatsAeroProvider) -> None:
    """hasMore=true with no records must not loop forever."""
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": True})
    )
    assert provider.search(QUERY) == []
    assert route.call_count == 1


@respx.mock
def test_401_explains_the_partner_api_distinction(provider: SeatsAeroProvider) -> None:
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(ProviderError, match="separate from a Partner API key"):
        provider.search(QUERY)


@respx.mock
def test_403_is_reported_as_a_plan_problem(provider: SeatsAeroProvider) -> None:
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(ProviderError, match="403"):
        provider.search(QUERY)


@respx.mock
def test_rate_limit_is_retried(provider: SeatsAeroProvider, monkeypatch) -> None:
    monkeypatch.setattr("flightwatch.providers.seats_aero.time.sleep", lambda _: None)
    route = respx.get(f"{BASE}/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"data": [RECORD], "hasMore": False}),
        ]
    )
    assert len(provider.search(QUERY)) == 2
    assert route.call_count == 2


@respx.mock
def test_persistent_server_error_raises(provider: SeatsAeroProvider, monkeypatch) -> None:
    monkeypatch.setattr("flightwatch.providers.seats_aero.time.sleep", lambda _: None)
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError, match="failed after retries"):
        provider.search(QUERY)


@respx.mock
def test_network_error_is_retried_then_raises(provider: SeatsAeroProvider, monkeypatch) -> None:
    monkeypatch.setattr("flightwatch.providers.seats_aero.time.sleep", lambda _: None)
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="failed after retries"):
        provider.search(QUERY)


@respx.mock
def test_list_payload_is_tolerated(provider: SeatsAeroProvider) -> None:
    """Defensive: a bare array instead of {"data": [...]} still parses."""
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=[RECORD]))
    assert len(provider.search(QUERY)) == 2


@respx.mock
def test_proxy_block_is_not_retried_and_says_so(provider: SeatsAeroProvider, monkeypatch) -> None:
    """A blocked CONNECT reports the proxy, not a seats.aero auth failure, and gives up
    immediately rather than retrying something that cannot succeed."""
    monkeypatch.setattr("flightwatch.providers.seats_aero.time.sleep", lambda _: None)
    route = respx.get(f"{BASE}/search").mock(side_effect=httpx.ProxyError("403 Forbidden"))
    with pytest.raises(ProviderError, match="proxy refused"):
        provider.search(QUERY)
    assert route.call_count == 1


@respx.mock
def test_default_booking_url_carries_the_full_param_set(provider: SeatsAeroProvider) -> None:
    """Regression: a trimmed query string loads the page with cities filled in but never
    runs the search. seats.aero needs the whole set to treat the URL as a search state,
    so every param stays even when it looks irrelevant."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [RECORD], "hasMore": False})
    )
    url = provider.search(QUERY)[0].booking_url
    for required in (
        "origins=SFO",  # plural, not origin=
        "destinations=LHR",  # plural, not destination=
        "date=2026-10-01",
        "min_seats=1",
        "applicable_cabin=any",
        "additional_days=true",
        "additional_days_num=7",
        "disable_live_filtering=true",
        "view=default",
    ):
        assert required in url, required


@respx.mock
def test_default_max_fees_does_not_hide_high_surcharge_awards(
    provider: SeatsAeroProvider,
) -> None:
    """max_fees filters seats.aero's own results. At the observed $400 it would hide the
    very award being linked whenever surcharges exceed that -- routine on VS/BA."""
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [RECORD], "hasMore": False})
    )
    url = provider.search(QUERY)[0].booking_url
    assert "max_fees=40000" not in url
    fees = int(url.split("max_fees=")[1].split("&")[0])
    assert fees > 71_030, "must exceed the ~$710.30 Virgin Atlantic surcharge, in cents"


@respx.mock
def test_booking_url_template_is_rendered() -> None:
    provider = SeatsAeroProvider(
        api_key="k",
        requests_per_minute=100_000,
        booking_url_template=(
            "https://seats.aero/search?o={origin}&d={destination}&on={date}&src={source}"
        ),
    )
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [RECORD], "hasMore": False})
    )
    url = provider.search(QUERY)[0].booking_url
    assert url == ("https://seats.aero/search?o=SFO&d=LHR&on=2026-10-01&src=aeroplan")


@respx.mock
def test_booking_url_template_supports_record_id() -> None:
    provider = SeatsAeroProvider(
        api_key="k",
        requests_per_minute=100_000,
        booking_url_template="https://seats.aero/availability/{record_id}",
    )
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [RECORD], "hasMore": False})
    )
    assert provider.search(QUERY)[0].booking_url.endswith("/rec-1")


@respx.mock
def test_bad_template_placeholder_degrades_instead_of_crashing() -> None:
    """A typo in the template must not take down the run."""
    provider = SeatsAeroProvider(
        api_key="k",
        requests_per_minute=100_000,
        booking_url_template="https://seats.aero/search?x={nonsense}",
    )
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [RECORD], "hasMore": False})
    )
    results = provider.search(QUERY)
    assert results
    assert results[0].booking_url == "https://seats.aero/search?x="


@respx.mock
def test_empty_template_disables_links_entirely() -> None:
    """Setting booking_url_template = "" is how links get turned off. It must yield an
    empty string, not a stray "https://" fragment, so notifiers omit the link."""
    provider = SeatsAeroProvider(api_key="k", requests_per_minute=100_000, booking_url_template="")
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json={"data": [RECORD], "hasMore": False})
    )
    assert all(a.booking_url == "" for a in provider.search(QUERY))
