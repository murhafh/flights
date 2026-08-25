from __future__ import annotations

import contextlib
import os

import httpx

from ..models import Deal
from .base import NotifierError, format_deals_text, summarize

TELEGRAM_LIMIT = 4096
"""Telegram rejects messages longer than this, so long batches are chunked."""


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot_token_env: str, chat_id_env: str, *, timeout: float = 15.0) -> None:
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

    def send(self, deals: list[Deal]) -> None:
        if not deals:
            return
        body = f"{summarize(deals)}\n\n{format_deals_text(deals)}"
        for chunk in _chunk(body, TELEGRAM_LIMIT):
            self._post(chunk)

    def _post(self, text: str) -> None:
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
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Telegram explains refusals in the body; a bare status code is useless here.
            detail = ""
            with contextlib.suppress(ValueError):
                detail = f": {exc.response.json().get('description', '')}"
            raise NotifierError(
                f"Telegram delivery failed ({exc.response.status_code}){detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise NotifierError(f"Telegram delivery failed: {exc}") from exc


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
