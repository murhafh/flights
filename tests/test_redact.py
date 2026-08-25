"""Credential redaction tests.

These guard a real leak: a verbose run printed a live Telegram bot token, because
Telegram embeds it in the URL path and httpx logs request URLs.
"""

from __future__ import annotations

import logging

from flightwatch.redact import RedactingFormatter, redact

# Synthetic, deliberately: a real token here would be published by any public repo,
# and secret scanners would (rightly) flag it.
TOKEN = "1234567890:ZZnotarealtokenZZnotarealtoken"


def test_telegram_token_in_a_url_is_masked() -> None:
    line = f'HTTP Request: POST https://api.telegram.org/bot{TOKEN}/sendMessage "200 OK"'
    out = redact(line)
    assert TOKEN not in out
    assert "AAEwyGc5" not in out
    assert "/bot<REDACTED>/sendMessage" in out


def test_telegram_getupdates_url_is_masked() -> None:
    assert TOKEN not in redact(f"GET https://api.telegram.org/bot{TOKEN}/getUpdates")


def test_surrounding_text_is_preserved() -> None:
    out = redact(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage returned 200")
    assert out.startswith("POST https://api.telegram.org/bot<REDACTED>")
    assert out.endswith("returned 200")


def test_seats_aero_key_is_masked() -> None:
    assert "notarealkey123" not in redact("key=pro_notarealkey123")


def test_resend_key_is_masked() -> None:
    assert redact("Bearer re_abcdef1234567890xyz").count("re_<REDACTED>") == 1


def test_slack_webhook_path_is_masked() -> None:
    out = redact("POST https://hooks.slack.com/services/T00/B11/XXXXsecretXXXX")
    assert "secret" not in out
    assert "hooks.slack.com/services/<REDACTED>" in out


def test_authorization_header_is_masked() -> None:
    for line in (
        "Authorization: Bearer abcdef1234567890",
        "'authorization': 'Bearer abcdef1234567890'",
        "Partner-Authorization: pro_abcdef1234567890",
        "x-api-key=abcdef1234567890",
    ):
        assert "abcdef1234567890" not in redact(line), line


def test_ordinary_text_is_untouched() -> None:
    line = "SFO -> Dublin, business: 4 records, 4 under threshold"
    assert redact(line) == line


def test_short_tokenlike_strings_are_not_mangled() -> None:
    """Avoid false positives that would make logs unreadable."""
    assert redact("robot:12") == "robot:12"


def test_formatter_redacts_the_rendered_message() -> None:
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="POST https://api.telegram.org/bot%s/sendMessage",
        args=(TOKEN,),
        exc_info=None,
    )
    out = formatter.format(record)
    assert TOKEN not in out
    assert "INFO" in out


def test_formatter_redacts_secrets_inside_tracebacks() -> None:
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"failed calling /bot{TOKEN}/sendMessage")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    assert TOKEN not in formatter.format(record)
