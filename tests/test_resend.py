"""Resend notifier tests."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from flightwatch.models import Availability, Cabin, Deal
from flightwatch.notify.base import NotifierError
from flightwatch.notify.resend import ENDPOINT, ResendNotifier


def make_deal(miles: int = 57_500, previous_miles: int | None = None, **kwargs) -> Deal:
    defaults = {
        "source": "american",
        "origin": "SFO",
        "destination": "DUB",
        "depart": date(2026, 10, 24),
        "cabin": Cabin.BUSINESS,
        "miles": miles,
        "direct": False,
        "seats": 2,
        "taxes": 5.6,
        "taxes_currency": "USD",
        "airlines": ("AA", "EI"),
        "booking_url": "https://example.test/s?origin=SFO&destination=DUB",
    }
    return Deal(
        availability=Availability(**{**defaults, **kwargs}),
        watch_name="SFO -> Dublin",
        max_miles=80_000,
        previous_miles=previous_miles,
    )


@pytest.fixture
def notifier(monkeypatch) -> ResendNotifier:
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    return ResendNotifier(sender="alerts@example.com", recipients=["me@example.com"])


def test_missing_api_key_is_rejected(monkeypatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(NotifierError, match="re_"):
        ResendNotifier(sender="a@b.com", recipients=["c@d.com"])


@respx.mock
def test_sends_with_bearer_auth(notifier: ResendNotifier) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "abc"}))
    notifier.send([make_deal()])

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer re_test_key"


@respx.mock
def test_payload_shape(notifier: ResendNotifier) -> None:
    import json

    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "abc"}))
    notifier.send([make_deal()])

    body = json.loads(route.calls[0].request.content)
    assert body["from"] == "alerts@example.com"
    assert body["to"] == ["me@example.com"]
    assert "57,500" in body["subject"]
    assert "html" in body and "text" in body
    assert "reply_to" not in body  # omitted when unset


@respx.mock
def test_reply_to_is_included_when_set(monkeypatch) -> None:
    import json

    monkeypatch.setenv("RESEND_API_KEY", "re_k")
    notifier = ResendNotifier(
        sender="a@b.com", recipients=["c@d.com"], reply_to="reply@example.com"
    )
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "x"}))
    notifier.send([make_deal()])
    assert json.loads(route.calls[0].request.content)["reply_to"] == "reply@example.com"


@respx.mock
def test_html_contains_the_deal_and_link(notifier: ResendNotifier) -> None:
    import json

    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "abc"}))
    notifier.send([make_deal()])

    html = json.loads(route.calls[0].request.content)["html"]
    assert "57,500" in html
    assert "5.60 USD" in html
    assert "example.test/s" in html
    assert "SFO" in html and "DUB" in html


@respx.mock
def test_html_shows_a_price_drop(notifier: ResendNotifier) -> None:
    import json

    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "abc"}))
    notifier.send([make_deal(miles=57_500, previous_miles=70_000)])
    assert "-12,500" in json.loads(route.calls[0].request.content)["html"]


@respx.mock
def test_watch_name_is_escaped(monkeypatch) -> None:
    """Watch names come from user config and land in HTML."""
    import json

    monkeypatch.setenv("RESEND_API_KEY", "re_k")
    notifier = ResendNotifier(sender="a@b.com", recipients=["c@d.com"])
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "x"}))

    deal = make_deal()
    object.__setattr__(deal, "watch_name", "<script>alert(1)</script>")
    notifier.send([deal])

    html = json.loads(route.calls[0].request.content)["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@respx.mock
def test_empty_batch_sends_nothing(notifier: ResendNotifier) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "x"}))
    notifier.send([])
    assert route.call_count == 0


@respx.mock
def test_unverified_sender_gets_an_actionable_error(notifier: ResendNotifier) -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            403,
            json={
                "name": "validation_error",
                "message": "The from address domain is not verified.",
            },
        )
    )
    with pytest.raises(NotifierError, match="onboarding@resend.dev"):
        notifier.send([make_deal()])


@respx.mock
def test_api_error_body_is_surfaced(notifier: ResendNotifier) -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            422, json={"name": "missing_field", "message": "to is required"}
        )
    )
    with pytest.raises(NotifierError, match="to is required"):
        notifier.send([make_deal()])


@respx.mock
def test_non_json_error_is_tolerated(notifier: ResendNotifier) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(NotifierError, match="bad gateway"):
        notifier.send([make_deal()])


@respx.mock
def test_network_failure_is_wrapped(notifier: ResendNotifier) -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(NotifierError, match="Resend delivery failed"):
        notifier.send([make_deal()])
