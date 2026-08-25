from __future__ import annotations

import os

import httpx

from ..models import Deal
from .base import NotifierError, format_deal_line, summarize


class SlackNotifier:
    name = "slack"

    def __init__(self, webhook_url_env: str, *, timeout: float = 15.0) -> None:
        self._webhook = os.environ.get(webhook_url_env)
        if not self._webhook:
            raise NotifierError(f"Slack notifier needs ${webhook_url_env} set to a webhook URL.")
        self._timeout = timeout

    def send(self, deals: list[Deal]) -> None:
        if not deals:
            return

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": summarize(deals)},
            }
        ]
        for deal in deals:
            section: dict = {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"`{format_deal_line(deal)}`"},
            }
            if deal.availability.booking_url:
                section["accessory"] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open"},
                    "url": deal.availability.booking_url,
                }
            blocks.append(section)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_Cached availability - verify before counting on it._",
                    }
                ],
            }
        )

        try:
            response = httpx.post(
                self._webhook,
                json={"text": summarize(deals), "blocks": blocks},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifierError(f"Slack delivery failed: {exc}") from exc
