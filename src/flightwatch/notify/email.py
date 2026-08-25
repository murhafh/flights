from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from ..models import Deal
from .base import NotifierError, format_deals_text, summarize


class SmtpNotifier:
    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        recipients: list[str],
        username_env: str,
        password_env: str,
        use_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._recipients = recipients
        self._use_tls = use_tls
        self._timeout = timeout
        self._username = os.environ.get(username_env)
        self._password = os.environ.get(password_env)
        if not self._password:
            raise NotifierError(
                f"Email notifier needs ${password_env} set "
                "(for Gmail, use an app password rather than your account password)."
            )

    def send(self, deals: list[Deal]) -> None:
        if not deals:
            return

        message = EmailMessage()
        message["Subject"] = f"[flightwatch] {summarize(deals)}"
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(format_deals_text(deals))

        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
                if self._use_tls:
                    server.starttls()
                if self._username:
                    server.login(self._username, self._password)
                server.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotifierError(f"Email delivery failed: {exc}") from exc
