"""In-memory stand-in for `telegram.Bot`.

`app/bot/telegram_bot.py` constructs `Bot(token=settings.telegram_bot_token)`
inside each send function rather than taking it as a dependency, so the only
way to intercept it is to replace the class in that module's namespace. That
is what `install()` does.

The trap this guards against
----------------------------
Every send function opens with:

    if not settings.telegram_bot_token:
        return

A test that forgets to set a token therefore exercises nothing, sends nothing,
asserts nothing — and passes, looking green. `install()` sets a dummy token so
the real formatting and sending path actually runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SentMessage:
    chat_id: Any
    text: str
    parse_mode: Optional[str] = None
    kwargs: dict = field(default_factory=dict)
    message_id: int = 0

    def __contains__(self, needle: str) -> bool:
        return needle in self.text


class _Result:
    """Mimics the `telegram.Message` the real API returns; the bot reads
    `.message_id` off it and stores it on the Alert row."""

    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    """Records outbound messages. One shared log across all instances, because
    the production code builds a fresh Bot per send call."""

    sent: list[SentMessage] = []
    #: Set to an Exception to make the next send raise — proves the caller's
    #: error handling runs (alerts must not abort the analyzer).
    fail_with: Optional[Exception] = None

    def __init__(self, token: str = "", **kwargs):
        self.token = token

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if FakeBot.fail_with is not None:
            raise FakeBot.fail_with
        msg = SentMessage(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            kwargs=kwargs,
            message_id=len(FakeBot.sent) + 1,
        )
        FakeBot.sent.append(msg)
        return _Result(msg.message_id)

    async def set_webhook(self, *a, **kw):
        return True

    async def delete_webhook(self, *a, **kw):
        return True

    async def get_me(self, *a, **kw):
        return _Result(0)

    # ── assertions ────────────────────────────────────────────────────────

    @classmethod
    def reset(cls) -> None:
        cls.sent = []
        cls.fail_with = None

    @classmethod
    def texts(cls) -> list[str]:
        return [m.text for m in cls.sent]

    @classmethod
    def only(cls) -> SentMessage:
        """The single message sent, or a clear failure if that is not true."""
        assert len(cls.sent) == 1, (
            f"expected exactly 1 message, got {len(cls.sent)}: {cls.texts()}"
        )
        return cls.sent[0]

    @classmethod
    def containing(cls, needle: str) -> list[SentMessage]:
        return [m for m in cls.sent if needle in m.text]


def install(monkeypatch, token: str = "test:token") -> type[FakeBot]:
    """Replace `Bot` wherever the app sends from, and supply a token.

    The token matters as much as the patch — see the module docstring.
    """
    FakeBot.reset()
    monkeypatch.setattr("app.bot.telegram_bot.Bot", FakeBot)
    monkeypatch.setattr("app.config.settings.telegram_bot_token", token)
    return FakeBot
