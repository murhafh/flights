"""Alert rendering tests."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from flightwatch.models import Availability, Cabin, Deal
from flightwatch.notify.base import (
    NotifierError,
    format_deal_line,
    format_deals_text,
    summarize,
)
from flightwatch.notify.telegram import (
    TELEGRAM_LIMIT,
    TelegramNotifier,
    _chunk,
    discover_chats,
    verify_token,
)


def make_deal(
    *,
    origin: str = "SFO",
    destination: str = "LHR",
    miles: int = 60_000,
    watch_name: str = "Europe business",
    previous_miles: int | None = None,
    source: str = "aeroplan",
    depart: date = date(2026, 10, 1),
    **availability_kwargs,
) -> Deal:
    defaults = {
        "source": source,
        "origin": origin,
        "destination": destination,
        "depart": depart,
        "cabin": Cabin.BUSINESS,
        "miles": miles,
        "direct": True,
        "seats": 2,
        "taxes": 98.0,
        "taxes_currency": "USD",
        "airlines": ("AC",),
        "booking_url": (
            f"https://example.test/s?origin={origin}&destination={destination}&source={source}"
        ),
    }
    return Deal(
        availability=Availability(**{**defaults, **availability_kwargs}),
        watch_name=watch_name,
        max_miles=70_000,
        previous_miles=previous_miles,
    )


def test_each_deal_gets_its_own_booking_link() -> None:
    """Regression: links were once emitted once per watch group, so every deal after
    the first pointed at the wrong route."""
    body = format_deals_text(
        [
            make_deal(destination="CDG", source="aeroplan"),
            make_deal(destination="LHR", source="flyingblue"),
        ]
    )
    assert "destination=CDG" in body
    assert "destination=LHR" in body
    assert "source=flyingblue" in body
    assert body.count("example.test/s") == 2


def test_link_matches_the_deal_it_follows() -> None:
    lines = format_deals_text(
        [make_deal(destination="CDG"), make_deal(destination="LHR")]
    ).splitlines()
    for index, line in enumerate(lines):
        if "SFO->CDG" in line:
            assert "destination=CDG" in lines[index + 1]
        if "SFO->LHR" in line:
            assert "destination=LHR" in lines[index + 1]


def test_deals_are_grouped_by_watch() -> None:
    body = format_deals_text(
        [make_deal(watch_name="Europe"), make_deal(watch_name="Asia", destination="HND")]
    )
    assert "Europe" in body
    assert "Asia" in body


def test_deal_line_includes_the_essentials() -> None:
    line = format_deal_line(make_deal())
    for fragment in (
        "SFO->LHR",
        "Business",
        "60,000 mi",
        "98 USD",
        "aeroplan",
        "nonstop",
        "2 seats",
    ):
        assert fragment in line


def test_deal_line_marks_connections() -> None:
    assert "1+ stop" in format_deal_line(make_deal(direct=False))


def test_deal_line_reports_a_price_drop() -> None:
    assert "down 10,000 from last alert" in format_deal_line(
        make_deal(miles=60_000, previous_miles=70_000)
    )


def test_deal_line_omits_drop_when_not_an_improvement() -> None:
    assert "down" not in format_deal_line(make_deal(miles=60_000))


def test_deal_line_handles_missing_optional_fields() -> None:
    line = format_deal_line(make_deal(taxes=None, seats=None, airlines=()))
    assert "SFO->LHR" in line
    assert "USD" not in line


def test_singular_seat_wording() -> None:
    assert "1 seat |" in format_deal_line(make_deal(seats=1))


def test_summarize_reports_the_cheapest() -> None:
    text = summarize([make_deal(miles=70_000), make_deal(miles=52_000, destination="CDG")])
    assert "52,000 miles" in text
    assert "SFO->CDG" in text
    assert "2 award deals" in text


def test_summarize_is_singular_for_one_deal() -> None:
    assert "1 award deal " in summarize([make_deal()])


def test_summarize_handles_empty() -> None:
    assert summarize([]) == "No award deals"


def test_body_carries_the_cache_caveat() -> None:
    assert "verify before counting on it" in format_deals_text([make_deal()])


def test_telegram_chunking_splits_on_line_boundaries() -> None:
    deals = [make_deal(depart=date(2026, 10, day)) for day in range(1, 29)] * 6
    body = format_deals_text(deals)
    assert len(body) > TELEGRAM_LIMIT  # otherwise this test proves nothing

    chunks = _chunk(body, TELEGRAM_LIMIT)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)
    assert "".join(chunks) == body


def test_telegram_short_body_is_one_chunk() -> None:
    body = format_deals_text([make_deal()])
    assert _chunk(body, TELEGRAM_LIMIT) == [body]


def test_small_surcharges_keep_their_cents() -> None:
    """$5.60 must not render as "6" -- on some routes the cash difference between
    otherwise identical awards is the entire decision."""
    assert "5.60 USD" in format_deal_line(make_deal(taxes=5.6))


def test_large_surcharges_drop_the_cents() -> None:
    assert "749 USD" in format_deal_line(make_deal(taxes=749.0))


def test_non_usd_currency_is_shown() -> None:
    assert "120 EUR" in format_deal_line(make_deal(taxes=120.0, taxes_currency="EUR"))


def test_link_line_is_omitted_when_there_is_no_url() -> None:
    """Better no link than a fabricated one that dead-ends."""
    body = format_deals_text([make_deal(booking_url="")])
    assert "http" not in body
    assert "SFO->LHR" in body


# --- Telegram setup helpers -------------------------------------------------

TG = "https://api.telegram.org"


@respx.mock
def test_verify_token_returns_the_bot_username() -> None:
    respx.get(f"{TG}/bottok/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "my_bot"}})
    )
    assert verify_token("tok") == "my_bot"


@respx.mock
def test_invalid_token_is_explained() -> None:
    respx.get(f"{TG}/botbad/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    with pytest.raises(NotifierError, match="@BotFather"):
        verify_token("bad")


@respx.mock
def test_discover_chats_extracts_id_and_label() -> None:
    respx.get(f"{TG}/bottok/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 12345, "username": "murhaf", "type": "private"}}}
                ],
            },
        )
    )
    assert discover_chats("tok") == [("12345", "murhaf")]


@respx.mock
def test_discover_chats_dedupes_repeat_messages() -> None:
    message = {"message": {"chat": {"id": 7, "first_name": "A", "type": "private"}}}
    respx.get(f"{TG}/bottok/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [message, message]})
    )
    assert len(discover_chats("tok")) == 1


@respx.mock
def test_discover_chats_falls_back_to_names_then_title() -> None:
    respx.get(f"{TG}/bottok/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 1, "first_name": "Ada", "last_name": "L"}}},
                    {"message": {"chat": {"id": 2, "title": "Deals group"}}},
                ],
            },
        )
    )
    assert dict(discover_chats("tok")) == {"1": "Ada L", "2": "Deals group"}


@respx.mock
def test_discover_chats_is_empty_before_you_message_the_bot() -> None:
    respx.get(f"{TG}/bottok/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    assert discover_chats("tok") == []


@respx.mock
def test_send_failure_surfaces_telegram_description() -> None:
    import os

    os.environ["_TG_TOK"] = "tok"
    os.environ["_TG_CHAT"] = "1"
    notifier = TelegramNotifier(bot_token_env="_TG_TOK", chat_id_env="_TG_CHAT")
    respx.post(f"{TG}/bottok/sendMessage").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
    )
    with pytest.raises(NotifierError, match="chat not found"):
        notifier.send([make_deal()])
