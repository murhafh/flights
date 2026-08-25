"""Resend HTTP API notifier.

Resend is an HTTP transactional-email API, not SMTP, so this is separate from the
`email` (SMTP) notifier rather than a variant of it.
"""

from __future__ import annotations

import os
from html import escape

import httpx

from ..models import Deal
from .base import NotifierError, format_return_line, format_taxes, summarize

ENDPOINT = "https://api.resend.com/emails"


class ResendNotifier:
    name = "resend"

    def __init__(
        self,
        *,
        sender: str,
        recipients: list[str],
        api_key_env: str = "RESEND_API_KEY",
        reply_to: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._api_key = os.environ.get(api_key_env)
        if not self._api_key:
            raise NotifierError(
                f"Resend notifier needs ${api_key_env} set to an API key (starts with 're_')."
            )
        self._sender = sender
        self._recipients = recipients
        self._reply_to = reply_to
        self._timeout = timeout

    def send(self, deals: list[Deal]) -> None:
        if not deals:
            return

        payload: dict[str, object] = {
            "from": self._sender,
            "to": self._recipients,
            "subject": f"[flightwatch] {summarize(deals)}",
            "html": _html_body(deals),
            "text": _text_body(deals),
        }
        if self._reply_to:
            payload["reply_to"] = self._reply_to

        try:
            response = httpx.post(
                ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise NotifierError(f"Resend delivery failed: {exc}") from exc

        if response.status_code >= 400:
            # Resend returns a JSON body explaining the rejection; surfacing it is far
            # more useful than a bare status code, especially for unverified senders.
            detail = _error_detail(response)
            raise NotifierError(
                f"Resend rejected the message (HTTP {response.status_code}): {detail}"
            )


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or "no response body"
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or body
        name = body.get("name")
        hint = ""
        if name == "validation_error" and "from" in str(message).lower():
            hint = (
                " -- the sending domain must be verified in Resend, or use "
                "onboarding@resend.dev (which only delivers to your own account address)."
            )
        return f"{message}{hint}"
    return str(body)[:300]


def _text_body(deals: list[Deal]) -> str:
    from .base import format_deals_text

    return format_deals_text(deals)


def _html_body(deals: list[Deal]) -> str:
    by_watch: dict[str, list[Deal]] = {}
    for deal in deals:
        by_watch.setdefault(deal.watch_name, []).append(deal)

    parts = [
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        'max-width:760px;color:#1a1a1a">',
        f'<h2 style="margin:0 0 4px;font-size:18px">{escape(summarize(deals))}</h2>',
    ]

    for watch_name, group in by_watch.items():
        parts.append(
            f'<h3 style="margin:20px 0 6px;font-size:14px;text-transform:uppercase;'
            f'letter-spacing:.04em;color:#666">{escape(watch_name)}</h3>'
        )
        parts.append(
            '<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;'
            'font-size:13px">'
            "<tr>"
            + "".join(
                f'<th style="text-align:{align};padding:6px 8px;border-bottom:2px solid #ddd;'
                f'font-weight:600">{label}</th>'
                for label, align in (
                    ("Route", "left"),
                    ("Depart", "left"),
                    ("Cabin", "left"),
                    ("Miles", "right"),
                    ("Cash", "right"),
                    ("Program", "left"),
                    ("Stops", "left"),
                    ("Airlines", "left"),
                    ("", "left"),
                )
            )
            + "</tr>"
        )

        for deal in group:
            availability = deal.availability
            drop = (
                f' <span style="color:#0a7d28">-{deal.previous_miles - availability.miles:,}</span>'
                if deal.is_improvement
                else ""
            )
            cells = [
                (f"{availability.origin}&rarr;{availability.destination}", "left"),
                (f"{availability.depart:%a %d %b %Y}", "left"),
                (availability.cabin.label, "left"),
                (f"<strong>{availability.miles:,}</strong>{drop}", "right"),
                (escape(format_taxes(availability)), "right"),
                (escape(availability.source), "left"),
                ("nonstop" if availability.direct else "1+", "left"),
                (escape("/".join(availability.airlines)) or "&ndash;", "left"),
                (
                    f'<a href="{escape(availability.booking_url)}" '
                    'style="color:#0b62d0;text-decoration:none">open&nbsp;&rsaquo;</a>'
                    if availability.booking_url
                    else "",
                    "left",
                ),
            ]
            parts.append(
                "<tr>"
                + "".join(
                    f'<td style="text-align:{align};padding:6px 8px;'
                    f'border-bottom:1px solid #eee">{value}</td>'
                    for value, align in cells
                )
                + "</tr>"
            )
            # Paired return gets its own indented sub-row so the outbound stays scannable.
            if (return_line := format_return_line(deal)) is not None:
                parts.append(
                    '<tr><td colspan="9" style="padding:2px 8px 8px 24px;font-size:12px;'
                    'color:#555;border-bottom:1px solid #eee">'
                    f"&#8618; {escape(return_line)}</td></tr>"
                )
        parts.append("</table>")

    parts.append(
        '<p style="margin:20px 0 0;font-size:12px;color:#888">'
        "Availability is cached and moves fast &mdash; verify before counting on it.</p></div>"
    )
    return "".join(parts)
