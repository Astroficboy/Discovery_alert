"""The email provider contract.

One interface, four implementations, and the application never learns which
one it has. Adding a provider means subclassing :class:`EmailProvider`,
registering it in :mod:`src.delivery.sender`, and adding its credentials to
``.env.example``.

``send`` returns a :class:`SendResult` rather than raising for ordinary
delivery failures: the pipeline wants to record the failure, mark the edition
as unsent and exit non-zero, not unwind through a traceback.
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field


class EmailConfigurationError(RuntimeError):
    """Missing or contradictory provider configuration. Raised at build time,
    not send time, so a misconfigured deployment fails before it does work."""


@dataclass
class EmailMessage:
    subject: str
    html: str
    text: str
    sender: str
    recipients: list[str]
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    provider: str
    message_id: str | None = None
    detail: str = ""
    recipients: list[str] = field(default_factory=list)


class EmailProvider(abc.ABC):
    name: str = "unnamed"

    #: True when the provider actually transmits. Used by ``--dry-run`` to
    #: assert that nothing left the building.
    transmits: bool = True

    @abc.abstractmethod
    async def send(self, message: EmailMessage) -> SendResult:
        """Deliver, or report why not."""

    async def verify(self) -> tuple[bool, str]:
        """Optional pre-flight check. Providers that can cheaply confirm their
        credentials should override this."""
        return True, "no verification implemented for this provider"

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not required
        """Release resources. Providers with nothing to close need not override."""


_ADDRESS = re.compile(
    r"^[^@\s<>]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_DISPLAY = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<addr>[^>]+)>\s*$")


def parse_address(value: str) -> tuple[str, str]:
    """Split ``"Name <a@b.c>"`` into ``("Name", "a@b.c")``."""
    match = _DISPLAY.match(value or "")
    if match:
        return match.group("name").strip().strip('"'), match.group("addr").strip()
    return "", (value or "").strip()


def validate_address(value: str) -> str:
    """Return the bare address, or raise. Never send to something unparsed."""
    _, address = parse_address(value)
    if not _ADDRESS.match(address):
        raise EmailConfigurationError(f"not a valid email address: {value!r}")
    return address


def validate_message(message: EmailMessage) -> None:
    if not message.recipients:
        raise EmailConfigurationError("no recipients configured (set EMAIL_TO)")
    if not message.sender:
        raise EmailConfigurationError("no sender configured (set EMAIL_FROM)")
    validate_address(message.sender)
    for recipient in message.recipients:
        validate_address(recipient)
    if not message.subject.strip():
        raise EmailConfigurationError("refusing to send an email with no subject")
    if not message.html.strip():
        raise EmailConfigurationError("refusing to send an email with no body")
    # Header injection: a newline in a header field lets an attacker append
    # arbitrary headers. Our subject is model-derived, so this is checked.
    for label, value in (("subject", message.subject), ("sender", message.sender),
                         *((f"recipient {r}", r) for r in message.recipients)):
        if "\n" in value or "\r" in value:
            raise EmailConfigurationError(f"newline in {label}; refusing to send")


__all__ = [
    "EmailConfigurationError",
    "EmailMessage",
    "EmailProvider",
    "SendResult",
    "parse_address",
    "validate_address",
    "validate_message",
]
