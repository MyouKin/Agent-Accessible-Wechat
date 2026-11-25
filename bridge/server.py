import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .desktop import MessageSender
from .models import AuthorKind, Snapshot, message_key
from .storage import Store
from .sync import Synchronizer

logger = logging.getLogger(__name__)
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[int, Query(ge=0)]
Search = Annotated[str, Query(min_length=1, max_length=300)]


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=8000)
    chat: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    auto_send: bool = False
    quote_message_id: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)
    author_kind: AuthorKind = "human"

    @field_validator("text")
    @classmethod
    def validate_text(cls, text: str) -> str:
        if not text.strip() or any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
            raise ValueError("Use nonblank text with no embedded control characters")
        return text


class MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["memory", "summary"]
    text: str = Field(min_length=1, max_length=8000)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    chat: str | None = Field(default=None, max_length=256)


class Bridge:
    def __init__(self, settings: Settings, source, sender: MessageSender):
        self.settings = settings
        self.store: Store
        self.synchronizer: Synchronizer
        self.source = source
        self.sender = sender

    async def status(self) -> dict:
        return {
            "chat": self.settings.chat,
            "send_mode": self.settings.send_mode,
            "source": await self.source.get_status(),
            "sync": self.store.sync_status(),
        }

    def check_chat(self, chat: str | None = None) -> None:
        if not self.settings.chat or (chat is not None and chat != self.settings.chat):
            raise HTTPException(409, "The draft must target the configured chat")

    async def draft(self, payload: DraftBody, *, origin: str) -> dict:
        self.check_chat(payload.chat)
        if payload.auto_send and (
            self.settings.send_mode != "auto" or origin != "http" or not payload.request_id
        ):
            raise HTTPException(409, "Automatic sending requires auto mode and a request_id")
        try:
            row = self.store.create_draft(
                payload.text, payload.request_id, origin, payload.auto_send, payload.quote_message_id,
                payload.author_kind,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if payload.auto_send:
            return await self.confirm(row["id"])
        return row

    async def confirm(self, draft_id: int) -> dict:
        self.check_chat()
        async with self.synchronizer.operation_lock:
            return await self._confirm(draft_id)

    async def _confirm(self, draft_id: int) -> dict:
        row = self.store.get_draft(draft_id)
        if row is None:
            raise HTTPException(404, "Draft not found")
        if self.store.claim_draft(draft_id):
            async def checkpoint(snapshot: Snapshot) -> None:
                if any(
                    prior["text"] == row["text"] and prior["quote_message_id"] == row["quote_message_id"]
                    for prior in self.store.unverified_model_submissions()
                ):
                    raise ValueError("A previous identical model submission has no confirmed outcome")
                self.store.checkpoint_submission(draft_id, tuple(map(message_key, snapshot.messages)))

            try:
                quote_id = row["quote_message_id"]
                if quote_id is None:
                    result = await self.sender.send(row["text"], before_submit=checkpoint)
                elif (quote := self.store.quote_target(quote_id)) is not None:
                    result = await self.sender.send(row["text"], quote=quote, before_submit=checkpoint)
                else:
                    self.store.finish_draft(draft_id, "failed", "Unavailable or unsupported quote target")
                    return self.store.get_draft(draft_id)
                if result.witness is not None and result.state == "sent":
                    self.synchronizer.commit_send(row, result)
                else:
                    self.store.finish_draft(
                        draft_id, result.state, result.detail,
                        keep_submission=result.state == "uncertain" and result.submission_attempted,
                    )
            except asyncio.CancelledError:
                self.store.finish_draft(draft_id, "uncertain", "Send was interrupted", keep_submission=True)
                raise
            except Exception:
                logger.exception("Draft send failed")
                self.store.finish_draft(
                    draft_id, "uncertain", "Send could not be verified", keep_submission=True
                )
        return self.store.get_draft(draft_id)


def mcp_server(bridge: Bridge):
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "Agent-Accessible-Wechat",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )

    @mcp.tool()
    async def get_status() -> dict[str, Any]:
        """Return current bridge, source and synchronization status."""
        return await bridge.status()

    @mcp.tool()
    async def get_recent_messages(limit: int = 30, for_model: bool = False) -> list[dict[str, Any]]:
        """Return recent stored messages from the configured chat, oldest first."""
        return bridge.store.messages(limit=max(1, min(limit, 200)), for_model=for_model)

    @mcp.tool()
    async def get_new_messages(after_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Return stored messages after a bridge message ID, oldest first."""
        return bridge.store.messages(limit=max(1, min(limit, 200)), after_id=max(0, after_id))

    @mcp.tool()
    async def search_messages(q: str, limit: int = 30, for_model: bool = False) -> list[dict[str, Any]]:
        """Search message text, sender or URL within the configured chat."""
        if not 1 <= len(q) <= 300:
            raise ValueError("Search text must be 1–300 characters")
        return bridge.store.messages(limit=max(1, min(limit, 200)), q=q, for_model=for_model)

    @mcp.tool()
    async def get_media(message_id: int, index: int = 0) -> Image:
        """Return a locally captured image attached to a message in the configured chat."""
        path = bridge.store.media(message_id, index)
        if path is None:
            raise ValueError("No captured media for this message")
        return Image(path=str(path))

    @mcp.tool()
    async def draft_message(
        text: str, request_id: str | None = None, quote_message_id: int | None = None,
        author_kind: AuthorKind = "unknown",
    ) -> dict[str, Any]:
        """Create a pending draft for human review."""
        return await bridge.draft(
            DraftBody(text=text, request_id=request_id, quote_message_id=quote_message_id, author_kind=author_kind),
            origin="mcp",
        )

    return mcp


def create_app(settings: Settings, token: str, mcp_token: str = "", *, source=None, sender=None):
    if len(token) < 24 or not token.isascii():
        raise ValueError("BRIDGE_TOKEN must contain at least 24 ASCII characters")
    if settings.mcp_enabled and (
        len(mcp_token) < 24 or not mcp_token.isascii() or token == mcp_token
    ):
        raise ValueError("MCP_TOKEN must be a distinct token of at least 24 ASCII characters")

    lock = asyncio.Lock()
    data_dir = Path(settings.data_dir)
    store: Store
    if source is None:
        from .accessibility import AccessibilitySource

        source = AccessibilitySource(
            settings.chat, profile=settings.accessibility_profile,
            poll_interval=settings.poll_interval, media_dir=data_dir / "media", desktop_lock=lock,
            lookup_sender_names=settings.lookup_sender_names,
            native_image_viewer=settings.native_image_viewer,
        )
    sender = sender or MessageSender(settings.chat, source, lock, settings.send_key)
    bridge = Bridge(settings, source, sender)
    mcp = mcp_server(bridge) if settings.mcp_enabled else None
    mcp_app = mcp.streamable_http_app() if mcp else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal store
        async with AsyncExitStack() as stack:
            store = Store(data_dir, settings.chat)
            bridge.store = store
            stack.callback(store.close)
            synchronizer = Synchronizer(source, store, settings.poll_interval)
            bridge.synchronizer = synchronizer
            await source.start()
            stack.push_async_callback(source.stop)
            if mcp:
                await stack.enter_async_context(mcp.session_manager.run())
            task = asyncio.create_task(synchronizer.run())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Agent-Accessible-Wechat", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        path = request.scope["path"]
        if path != "/health":
            is_mcp = path == "/mcp" or path.startswith("/mcp/")
            expected = mcp_token if is_mcp else token
            actual = request.headers.get("authorization", "")
            if not expected or not compare_digest(actual.encode(), f"Bearer {expected}".encode()):
                return JSONResponse(
                    {"detail": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/status")
    async def status():
        return await bridge.status()

    @app.get("/messages")
    async def messages(limit: Limit = 30, for_model: bool = False):
        return {"messages": store.messages(limit=limit, for_model=for_model)}

    @app.get("/messages/new")
    async def new_messages(after_id: Cursor = 0, limit: Limit = 100):
        return {"messages": store.messages(limit=limit, after_id=after_id)}

    @app.get("/messages/search")
    async def search_messages(q: Search, limit: Limit = 30, for_model: bool = False):
        return {"messages": store.messages(limit=limit, q=q, for_model=for_model)}

    @app.get("/media/{message_id}")
    async def media(message_id: int, index: Annotated[int, Query(ge=0, le=1)] = 0):
        path = store.media(message_id, index)
        if path is None:
            raise HTTPException(404, "Captured media not found")
        return FileResponse(path)

    @app.post("/drafts")
    async def create_draft(payload: DraftBody):
        return await bridge.draft(payload, origin="http")

    @app.get("/drafts")
    async def list_drafts(limit: Limit = 30):
        return {"drafts": store.list_drafts(limit)}

    @app.get("/drafts/{draft_id}")
    async def get_draft(draft_id: int):
        if (row := store.get_draft(draft_id)) is None:
            raise HTTPException(404, "Draft not found")
        return row

    @app.post("/drafts/{draft_id}/confirm")
    async def confirm_draft(draft_id: int):
        return await bridge.confirm(draft_id)

    @app.get("/memories")
    async def memories(q: Search | None = None, limit: Limit = 30):
        return {"memories": store.memories(q, limit)}

    @app.post("/memories")
    async def create_memory(payload: MemoryBody):
        bridge.check_chat(payload.chat)
        try:
            return store.create_memory(payload.kind, payload.text, payload.request_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    if mcp_app:
        app.mount("/", mcp_app)
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load()
    app = create_app(settings, os.environ.get("BRIDGE_TOKEN", ""), os.getenv("MCP_TOKEN", ""))
    uvicorn.run(app, host=os.getenv("BRIDGE_HOST", "0.0.0.0"), port=int(os.getenv("BRIDGE_PORT", "8765")),
                access_log=False)


if __name__ == "__main__":
    main()
