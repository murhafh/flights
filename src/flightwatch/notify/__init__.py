"""Alert delivery channels."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..models import Deal
from .base import (
    Notifier,
    NotifierError,
    format_deal_line,
    format_deals_text,
    summarize,
)
from .console import ConsoleNotifier
from .email import SmtpNotifier
from .resend import ResendNotifier
from .slack import SlackNotifier
from .telegram import TelegramNotifier

__all__ = [
    "Channel",
    "ConsoleNotifier",
    "Notifier",
    "NotifierError",
    "ResendNotifier",
    "SlackNotifier",
    "SmtpNotifier",
    "TelegramNotifier",
    "build_notifiers",
    "format_deal_line",
    "format_deals_text",
    "summarize",
]


@dataclass(frozen=True)
class Channel:
    """A notifier plus the watches it's scoped to."""

    notifier: Notifier
    watches: frozenset[str] = field(default_factory=frozenset)
    """Empty means every watch."""

    @property
    def name(self) -> str:
        return self.notifier.name

    def accepts(self, deal: Deal) -> bool:
        return not self.watches or deal.watch_name in self.watches


def build_notifiers(config: Config) -> list[Channel]:
    """Construct every configured channel, failing fast on missing credentials."""
    channels: list[Channel] = []
    for entry in config.notify:
        notifier: Notifier
        match entry.kind:
            case "console":
                notifier = ConsoleNotifier()
            case "telegram":
                notifier = TelegramNotifier(
                    bot_token_env=entry.bot_token_env,
                    chat_id_env=entry.chat_id_env,
                )
            case "slack":
                notifier = SlackNotifier(webhook_url_env=entry.webhook_url_env)
            case "resend":
                notifier = ResendNotifier(
                    sender=entry.sender,
                    recipients=entry.recipients,
                    api_key_env=entry.api_key_env,
                    reply_to=entry.reply_to,
                )
            case "email":
                notifier = SmtpNotifier(
                    host=entry.host,
                    port=entry.port,
                    sender=entry.sender,
                    recipients=entry.recipients,
                    username_env=entry.username_env,
                    password_env=entry.password_env,
                    use_tls=entry.use_tls,
                )
            case _:  # pragma: no cover - pydantic's discriminator rejects this first
                raise NotifierError(f"Unknown notifier kind {entry.kind!r}")
        channels.append(Channel(notifier=notifier, watches=frozenset(entry.watches)))
    return channels
