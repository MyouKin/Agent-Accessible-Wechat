from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Iterable
from hashlib import sha256
from json import dumps
import re
from typing import Literal, Protocol

AuthorKind = Literal["human", "llm", "unknown"]
WECHAT_QUOTE_LABEL = re.compile(r"\nQuote ([^\r\n]{1,256}?)'s message: ")


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Message:
    id: int | None = None
    chat: str = ""
    sender: str | None = None
    sender_id: str | None = None
    type: str = "unknown"
    text: str | None = None
    media_path: str | None = None
    url: str | None = None
    timestamp: datetime | None = None
    observed_at: datetime = field(default_factory=now)
    is_self: bool | None = None
    source: str = "accessibility"
    source_id: str | None = None
    metadata: dict = field(default_factory=dict)
    author_kind: AuthorKind = "unknown"
    author_source: str | None = None
    outbound_draft_id: int | None = None
    reply_text: str | None = None
    quote: dict | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    chat: str
    messages: tuple[Message, ...]
    at_tail: bool
    ready: bool = True


@dataclass(frozen=True, slots=True)
class QuoteTarget:
    """The stored target and the ordered messages that followed it."""

    text: str
    keys: tuple[str, ...]
    allow_own: bool = False


class MessageSource(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def get_status(self) -> dict: ...
    async def read_recent(self, limit: int) -> list[Message]: ...
    async def read_new(self) -> list[Message]: ...


class SnapshotSource(MessageSource, Protocol):
    async def read_snapshot(self, *, capture_media: bool = True) -> Snapshot: ...
    async def capture_messages(self, snapshot: Snapshot, indices: Iterable[int]) -> tuple[Message, ...]: ...
    async def wait_for_change(self, timeout: float | None = None) -> None: ...
    async def recover(self, previous: Snapshot, current: Snapshot) -> Snapshot | None: ...


def split_wechat_quote(text: str) -> tuple[str, dict] | None:
    """Read the native English quote label while preserving the displayed original."""
    normalized = text.replace("\r\n", "\n").replace("\u2029", "\n")
    label = WECHAT_QUOTE_LABEL.search(normalized)
    if label is None or not normalized[:label.start()].strip() or not normalized[label.end():].strip():
        return None
    return normalized[:label.start()], {
        "author_label": label.group(1),
        "text": normalized[label.end():],
    }


def message_key(message: Message) -> str:
    """Identify an observation for ordered matching. Equal rows remain separate."""
    if anchor := message.metadata.get("_anchor") or message.metadata.get("observation_key"):
        return anchor
    if message.source_id:
        identity = (message.source, message.source_id)
    else:
        identity = (
            message.sender_id, message.sender, message.type, message.text, message.url,
            message.timestamp.isoformat() if message.timestamp else None,
            message.is_self, message.metadata.get("raw_description"),
        )
    return sha256(dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
