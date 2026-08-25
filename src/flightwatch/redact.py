"""Credential redaction for log output.

Some APIs put secrets in places that get logged incidentally -- Telegram embeds the bot
token directly in the URL path, and httpx logs request URLs at INFO. Under `-v`, or a
launchd job with StandardOutPath, that writes live credentials to disk. Rather than
silencing the useful logging, the formatter below masks the secrets inside it.
"""

from __future__ import annotations

import logging
import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram bot token, as it appears in the URL path: /bot<id>:<secret>/method
    (re.compile(r"/bot\d+:[A-Za-z0-9_\-]{10,}"), "/bot<REDACTED>"),
    # seats.aero Partner API keys.
    (re.compile(r"\bpro_[A-Za-z0-9]{8,}"), "pro_<REDACTED>"),
    # Resend API keys.
    (re.compile(r"\bre_[A-Za-z0-9]{8,}"), "re_<REDACTED>"),
    # Slack and Telegram webhook-style URLs.
    (re.compile(r"(hooks\.slack\.com/services/)[A-Za-z0-9/_\-]+"), r"\1<REDACTED>"),
    # Generic Authorization / api-key headers, however they're spelled.
    (
        re.compile(
            r"(?i)\b(authorization|partner-authorization|x-api-key|api[_-]?key)"
            r"(['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?[A-Za-z0-9._\-]{8,}"
        ),
        r"\1\2\3<REDACTED>",
    ),
)


def redact(text: str) -> str:
    """Mask any credential-looking substring."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts secrets from the fully-rendered message.

    Redacting after formatting catches secrets wherever they came from -- the message,
    its args, or an exception traceback -- rather than only the ones we anticipated.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
