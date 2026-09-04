from __future__ import annotations

import contextlib
import logging
import os
import time

import httpx

from ..models import Deal
from .base import NotifierError, format_deals_text, summarize

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
"""Telegram rejects messages longer than this, so long batches are chunked."""


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self,
        bot_token_env: str,
        chat_id_env: str,
        *,
        timeout: float = 15.0,
        max_retries: int = 3,
    ) -> None:
        self._token = os.environ.get(bot_token_env)
        self._chat_id = os.environ.get(chat_id_env)
        missing = [
            env
            for env, value in ((bot_token_env, self._token), (chat_id_env, self._chat_id))
            if not value
        ]
        if missing:
            raise NotifierError(
                f"Telegram notifier needs {' and '.join('$' + name for name in missing)} set."
            )
        self._timeout = timeout
        self._max_retries = max_retries

    def send(self, deals: list[Deal]) -> None:
        if not deals:
            return
        body = f"{summarize(deals)}\n\n{format_deals_text(deals)}"
        for chunk in _chunk(body, TELEGRAM_LIMIT):
            self._post(chunk)

    def _post(self, text: str) -> None:
        """Send one message, retrying transient failures.

        A single read timeout used to lose an alert outright: the send failed, another
        channel had already succeeded, and the deal was recorded as seen -- so it stayed
        suppressed for the whole cooldown. Transient network faults are common enough
        (about 1 run in 60) to be worth retrying rather than dropping.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = httpx.post(
                    f"{API_BASE}/bot{self._token}/sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=self._timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                log.warning("Telegram send failed (%s), attempt %d", exc, attempt + 1)
            except httpx.HTTPError as exc:
                # Anything else at the transport layer is unlikely to fix itself.
                raise NotifierError(f"Telegram delivery failed: {exc}") from exc
            else:
                if response.status_code < 400:
                    return

                detail = ""
                with contextlib.suppress(ValueError):
                    detail = f": {response.json().get('description', '')}"

                # 429 carries a server-specified wait; 5xx is worth another try. A 4xx
                # like "chat not found" is a config error that retrying cannot fix.
                if response.status_code != 429 and response.status_code < 500:
                    raise NotifierError(
                        f"Telegram delivery failed ({response.status_code}){detail}"
                    )
                last_error = NotifierError(
                    f"Telegram delivery failed ({response.status_code}){detail}"
                )
                if response.status_code == 429:
                    with contextlib.suppress(ValueError, AttributeError):
                        retry_after = response.json()["parameters"]["retry_after"]
                        log.warning("Telegram rate limited; waiting %ss", retry_after)
                        time.sleep(min(float(retry_after), 30.0))
                        continue

            if attempt < self._max_retries:
                time.sleep(2.0**attempt)

        raise NotifierError(f"Telegram delivery failed after retries: {last_error}")


def _chunk(text: str, limit: int) -> list[str]:
    """Split on line boundaries so a deal is never cut in half."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


API_BASE = "https://api.telegram.org"


def _call(token: str, method: str, *, timeout: float = 15.0) -> dict:
    try:
        response = httpx.get(f"{API_BASE}/bot{token}/{method}", timeout=timeout)
    except httpx.HTTPError as exc:
        raise NotifierError(f"Could not reach the Telegram API: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        raise NotifierError(
            f"Telegram returned a non-JSON response (HTTP {response.status_code})"
        ) from None

    if not payload.get("ok"):
        description = payload.get("description", "unknown error")
        if response.status_code == 401:
            raise NotifierError(
                f"Telegram rejected the bot token: {description}. "
                "Check you copied the whole token from @BotFather."
            )
        raise NotifierError(f"Telegram API error: {description}")
    return payload


def verify_token(token: str) -> str:
    """Return the bot's @username, raising if the token is invalid."""
    result = _call(token, "getMe").get("result", {})
    return result.get("username", "unknown")


def discover_chats(token: str) -> list[tuple[str, str]]:
    """Find (chat_id, label) pairs from the bot's pending updates.

    Telegram gives no way to look up "the chat with my own account" -- the chat only
    exists once you message the bot, and then only appears in getUpdates. So this
    requires you to have sent the bot a message first.
    """
    updates = _call(token, "getUpdates").get("result", [])
    found: dict[str, str] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        label = (
            chat.get("username")
            or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
            or chat.get("title")
            or chat.get("type", "chat")
        )
        found[str(chat_id)] = label
    return sorted(found.items())
