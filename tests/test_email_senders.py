"""Tests for the SMTP / Resend / DryRun senders."""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from signal_tracker.notifier.email import (
    DryRunSender,
    ResendSender,
    SMTPSender,
    build_sender_from_settings,
)


async def test_dry_run_captures_message() -> None:
    sender = DryRunSender()
    await sender.send(
        to="me@x.com",
        subject="hello",
        html="<b>hi</b>",
        text="hi",
        sender="bot@x.com",
    )
    assert len(sender.sent) == 1
    assert sender.sent[0]["subject"] == "hello"


async def test_smtp_sender_uses_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.host = host
            self.port = port

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def starttls(self) -> None:
            return None

        def login(self, user: str, pw: str) -> None:
            return None

        def send_message(self, message: EmailMessage) -> None:
            captured.append(message)

    monkeypatch.setattr("signal_tracker.notifier.email.smtplib.SMTP", FakeSMTP)

    sender = SMTPSender(
        host="smtp.example.com",
        port=587,
        username="u",
        password="p",
    )
    await sender.send(
        to="me@x.com",
        subject="hi",
        html="<b>hi</b>",
        text="hi",
        sender="bot@x.com",
    )
    assert len(captured) == 1
    message = captured[0]
    assert message["Subject"] == "hi"
    assert message["To"] == "me@x.com"
    assert message["From"] == "bot@x.com"
    # text/plain part is the first alternative; assert its content.
    parts = list(message.walk())
    text_part = next(p for p in parts if p.get_content_type() == "text/plain")
    assert text_part.get_content().strip() == "hi"
    html_part = next(p for p in parts if p.get_content_type() == "text/html")
    assert "<b>hi</b>" in html_part.get_content()


async def test_resend_sender_posts_to_api() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "abc123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ResendSender(api_key="re_test", client=client)
    try:
        await sender.send(
            to="me@x.com",
            subject="hi",
            html="<b>hi</b>",
            text="hi",
            sender="bot@x.com",
        )
    finally:
        await client.aclose()

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["authorization"] == "Bearer re_test"
    assert b"me@x.com" in captured["body"]


async def test_resend_sender_raises_on_http_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ResendSender(api_key="re_test", client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await sender.send(
                to="me@x.com",
                subject="hi",
                html="<b>hi</b>",
                text="hi",
                sender="bot@x.com",
            )
    finally:
        await client.aclose()


def test_factory_picks_resend_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_xxx")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    from signal_tracker.config import get_settings

    get_settings.cache_clear()
    sender = build_sender_from_settings()
    assert isinstance(sender, ResendSender)


def test_factory_falls_back_to_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    from signal_tracker.config import get_settings

    get_settings.cache_clear()
    sender = build_sender_from_settings()
    assert isinstance(sender, SMTPSender)
    assert sender.host == "smtp.example.com"


def test_factory_raises_when_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    from signal_tracker.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        build_sender_from_settings()
