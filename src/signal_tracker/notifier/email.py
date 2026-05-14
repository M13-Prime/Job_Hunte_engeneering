"""Email sender abstractions (SMTP + Resend).

The factory ``build_sender_from_settings`` picks the backend based on the
runtime config — Resend if ``RESEND_API_KEY`` is set, else SMTP.
"""

from __future__ import annotations

import abc
import asyncio
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

from signal_tracker.config import Settings, get_settings
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


class EmailSenderProtocol(Protocol):
    """Anything that can send an HTML+text email to one recipient."""

    async def send(
        self, *, to: str, subject: str, html: str, text: str, sender: str
    ) -> None:
        ...


class _EmailSenderBase(abc.ABC):
    @abc.abstractmethod
    async def send(
        self, *, to: str, subject: str, html: str, text: str, sender: str
    ) -> None:
        raise NotImplementedError


class SMTPSender(_EmailSenderBase):
    """STARTTLS SMTP sender. Uses ``smtplib`` via a thread executor."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        *,
        use_starttls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_starttls = use_starttls
        self.timeout = timeout

    def _send_sync(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            if self.use_starttls:
                client.starttls()
            if self.username and self.password:
                client.login(self.username, self.password)
            client.send_message(message)

    async def send(
        self, *, to: str, subject: str, html: str, text: str, sender: str
    ) -> None:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        await asyncio.to_thread(self._send_sync, message)
        logger.info("notifier.smtp_sent", extra={"to": to, "host": self.host})


class ResendSender(_EmailSenderBase):
    """https://resend.com/docs/api-reference/emails/send-email"""

    API_URL = "https://api.resend.com/emails"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    async def send(
        self, *, to: str, subject: str, html: str, text: str, sender: str
    ) -> None:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": sender,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
            )
            response.raise_for_status()
            logger.info(
                "notifier.resend_sent",
                extra={"to": to, "status": response.status_code},
            )
        finally:
            if self._owns_client:
                await client.aclose()


class DryRunSender(_EmailSenderBase):
    """Sender that captures messages instead of delivering them.

    Useful for ``--dry-run`` previews and for tests.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(
        self, *, to: str, subject: str, html: str, text: str, sender: str
    ) -> None:
        self.sent.append(
            {"to": to, "subject": subject, "html": html, "text": text, "from": sender}
        )
        logger.info(
            "notifier.dry_run_capture",
            extra={"to": to, "subject": subject, "size_html": len(html)},
        )


def build_sender_from_settings(
    settings: Settings | None = None,
) -> EmailSenderProtocol:
    """Pick a backend based on runtime config.

    Order of precedence:
        1. Resend if ``RESEND_API_KEY`` is set.
        2. SMTP if ``SMTP_HOST`` is set.
        3. Otherwise raises — the caller should fall back to dry-run.
    """
    settings = settings or get_settings()
    if settings.resend_api_key:
        return ResendSender(api_key=settings.resend_api_key)
    if settings.smtp_host:
        return SMTPSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
        )
    raise RuntimeError(
        "No email backend configured. Set RESEND_API_KEY or SMTP_HOST in .env "
        "(or run the digest in --dry-run mode)."
    )


__all__ = [
    "DryRunSender",
    "EmailSenderProtocol",
    "ResendSender",
    "SMTPSender",
    "build_sender_from_settings",
]
