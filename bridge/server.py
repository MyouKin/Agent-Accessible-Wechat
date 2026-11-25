import asyncio
import base64
import json
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from hmac import compare_digest
from mimetypes import guess_type
from pathlib import Path
from typing import Annotated, Any, Callable, Literal
from unicodedata import category
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .desktop import MessageSender
from .models import AuthorKind, Snapshot, message_key
from .sessions import Session, list_sessions, open_session
from .storage import Store
from .sync import Synchronizer
from .tool_registry import ToolContent, ToolRegistry
from .wechat_tools import PUBLIC_ROUTES, TOOLS_ROUTES, forward_wechat_tools, read_attached_chat

logger = logging.getLogger(__name__)
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[int, Query(ge=0)]
Search = Annotated[str, Query(min_length=1, max_length=300)]
WaitSeconds = Annotated[float, Query(ge=0, le=30)]
ToolLimit = Annotated[int, Field(ge=1, le=200)]
ToolCursor = Annotated[int, Field(ge=0)]
ToolWaitSeconds = Annotated[float, Field(ge=0, le=30)]
ToolSearch = Annotated[str, Field(min_length=1, max_length=300)]
SessionKey = Annotated[str, Field(pattern=r"^session-[0-9a-f]{24}$")]
SessionRevision = Annotated[str, Field(min_length=1, max_length=128)]


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=8000)
    chat: str | None = Field(default=None, max_length=256)
    session_key: str | None = Field(default=None, max_length=64)
    session_revision: SessionRevision | None = None
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
    session_key: str | None = Field(default=None, max_length=64)


class AgentWechatToolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat: str = Field(min_length=1, max_length=256)
    arguments: dict = Field(default_factory=dict)


class SelectSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: SessionKey


class SessionSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: SessionRevision
    mention_nickname: str | None = Field(min_length=1, max_length=256)

    @field_validator("mention_nickname")
    @classmethod
    def validate_mention_nickname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        nickname = value.strip()
        if not nickname or any(category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in value):
            raise ValueError("Use a nonblank, single-line group nickname")
        return nickname


class SessionMentionBody(SessionSelection):
    session_key: SessionKey


class Bridge:
    def __init__(self, settings: Settings, source, sender: MessageSender):
        self.settings = settings
        self.store: Store
        self.synchronizer: Synchronizer
        self.source = source
        self.sender = sender
        self.active_session: Session | None = None
        self.session_selection: SessionSelection | None = None
        self.restore_target: Session | None = None
        self.restore_failed = False
        self.activation_lock = asyncio.Lock()

    async def status(self) -> dict:
        generation = self.synchronizer.generation
        source = await self.source.get_status()
        if self.synchronizer.paused or generation != self.synchronizer.generation:
            source = {**source, "ready": False}
        return {
            "chat": self.store.chat,
            "send_mode": self.settings.send_mode,
            "source": source,
            "sync": self.store.sync_status(),
            "active_session": self.active_session.summary(active=True) if self.active_session else None,
            "session_selection": self.session_selection.model_dump()
            if self.active_session and self.session_selection else None,
            "session_restore": ("failed" if self.restore_failed else "waiting") if self.restore_target else None,
        }

    def check_key(self, session_key: str | None) -> None:
        expected = self.active_session.key if self.active_session else "legacy"
        if session_key is not None and session_key != expected:
            raise HTTPException(409, "The active WeChat session changed")

    def check_chat(self, chat: str | None = None, session_key: str | None = None,
                   session_revision: str | None = None) -> None:
        self.check_key(session_key)
        if self.synchronizer.paused or not self.store.chat or (chat is not None and chat != self.store.chat):
            raise HTTPException(409, "The request must target the active WeChat session")
        if session_revision is not None and (
            self.session_selection is None or self.session_selection.revision != session_revision
        ):
            raise HTTPException(409, "The active WeChat session selection changed")

    def save_session_selection(self, target: Session, selection: SessionSelection) -> None:
        self.store.select_session(
            target.model_dump(), revision=selection.revision, mention_nickname=selection.mention_nickname,
        )

    async def set_session_mention(self, payload: SessionMentionBody) -> SessionSelection:
        async with self.activation_lock:
            self.check_chat(session_key=payload.session_key, session_revision=payload.revision)
            target = self.active_session
            if target is None or target.kind != "group":
                raise HTTPException(409, "The active WeChat session must be a verified group")
            selection = SessionSelection(revision=payload.revision, mention_nickname=payload.mention_nickname)
            self.save_session_selection(target, selection)
            self.session_selection = selection
            return selection

    async def activate_session(self, target: Session, client: httpx.AsyncClient | None, token: str,
                               *, restoring: bool = False) -> tuple[Session, SessionSelection] | None:
        async with self.activation_lock:
            if restoring and self.restore_target != target:
                return None
            sync = self.synchronizer
            async with sync.operation_lock:
                sync.paused = True
            try:
                opened = await open_session(client, token, target)
                selection = self.session_selection if restoring else None
                if selection is None:
                    selection = SessionSelection(revision=uuid4().hex, mention_nickname=None)
                previous_chat = self.store.chat
                previous_kind = self.active_session.kind if self.active_session else None
                async with sync.operation_lock:
                    try:
                        await self.source.set_target(opened.name, opened.kind)
                        snapshot = await self.source.prepare_target()
                        if not snapshot.ready or not snapshot.at_tail or snapshot.chat != opened.name:
                            raise HTTPException(409, "Could not verify the selected session at the latest message")
                        self.save_session_selection(opened, selection)
                    except BaseException:
                        await self.source.set_target(previous_chat, previous_kind)
                        raise
                    self.sender.set_target(opened.name)
                    self.store.bind_target(opened.name, opened.partition)
                    sync.target_changed()
                    self.active_session = opened
                    self.session_selection = selection
                    self.restore_target = None
                    self.restore_failed = False
                return opened, selection
            finally:
                sync.paused = self.restore_target is not None

    async def restore_session(self, client: httpx.AsyncClient | None, token: str) -> None:
        while (target := self.restore_target) is not None:
            try:
                await list_sessions(client, token)
            except HTTPException as error:
                if client is None or error.status_code not in {409, 503}:
                    self.restore_failed = True
                    return
                await asyncio.sleep(5)
                continue
            try:
                await self.activate_session(target, client, token, restoring=True)
            except HTTPException as error:
                if self.restore_target == target and error.status_code == 503:
                    await asyncio.sleep(5)
                    continue
                if self.restore_target == target:
                    self.restore_failed = True
                    logger.warning("Saved WeChat session could not be verified")
            return

    async def read_current_chat(self, payload: AgentWechatToolBody,
                                attached: httpx.AsyncClient | None = None, tools_token: str = "") -> dict:
        limit = payload.arguments.get("limit", 20)
        if payload.arguments.keys() - {"limit"} or type(limit) is not int or not 1 <= limit <= 20:
            raise HTTPException(422, "The limit must be an integer from 1 to 20")
        async with self.synchronizer.operation_lock:
            self.check_chat(payload.chat)
            live = await self.source.read_snapshot(capture_media=False)
            if not live.ready or not live.at_tail or live.chat != self.store.chat:
                raise HTTPException(409, "The configured chat is not visible at the latest message")
            rows = [{"type": row.type, "text": (row.reply_text or row.text or "")[:360],
                     "sender": row.sender, "is_self": row.is_self} for row in live.messages[-limit:]]
            generation = self.synchronizer.generation
        current = {"chat": live.chat, "source": "accessibility", "messages": rows}
        if attached is not None and tools_token and (self.active_session is None or self.active_session.kind == "group"):
            current = await read_attached_chat(attached, tools_token, live.chat, limit) or current
        if generation != self.synchronizer.generation or self.synchronizer.paused:
            raise HTTPException(409, "The active WeChat session changed")
        result = json.dumps(current, ensure_ascii=False)
        return {"content": [{"type": "text", "text": result}]}

    async def draft(self, payload: DraftBody, *, origin: str) -> dict:
        self.check_chat(payload.chat, payload.session_key, payload.session_revision)
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
            return await self.confirm(row["id"], payload.session_key, session_revision=payload.session_revision)
        return row

    async def confirm(self, draft_id: int, session_key: str | None = None,
                      *, session_revision: str | None = None) -> dict:
        async with self.synchronizer.operation_lock:
            self.check_chat(session_key=session_key, session_revision=session_revision)
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


def bridge_tools(bridge: Bridge, native_client: Callable[[], httpx.AsyncClient | None], native_token: str):
    tools = ToolRegistry(native_client, native_token)

    @tools.tool()
    async def get_status() -> dict[str, Any]:
        """Return current bridge, source and synchronization status."""
        return await bridge.status()

    @tools.tool(conversation=True)
    async def get_recent_messages(limit: int = 30, for_model: bool = False) -> list[dict[str, Any]]:
        """Return recent stored messages from the configured chat, oldest first."""
        return bridge.store.messages(limit=max(1, min(limit, 200)), for_model=for_model)

    @tools.tool(conversation=True)
    async def get_new_messages(after_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Return stored messages after a bridge message ID, oldest first."""
        return bridge.store.messages(limit=max(1, min(limit, 200)), after_id=max(0, after_id))

    @tools.tool()
    async def wait_for_messages(after_id: ToolCursor, timeout: ToolWaitSeconds = 15, limit: ToolLimit = 100) -> list[dict[str, Any]]:
        """Wait up to 30 seconds for new committed messages from the configured chat."""
        return await bridge.synchronizer.wait_for_messages(after_id, limit, timeout)

    @tools.tool(conversation=True)
    async def get_message(message_id: int) -> dict[str, Any]:
        """Look up one durable message ID in the configured chat."""
        if (message := bridge.store.get_message(message_id)) is None:
            raise ValueError("Message not found in the configured chat")
        return message

    @tools.tool(conversation=True)
    async def search_messages(q: str, limit: int = 30, for_model: bool = False) -> list[dict[str, Any]]:
        """Search message text, sender or URL within the configured chat."""
        if not 1 <= len(q) <= 300:
            raise ValueError("Search text must be 1–300 characters")
        return bridge.store.messages(limit=max(1, min(limit, 200)), q=q, for_model=for_model)

    @tools.tool()
    async def get_media(message_id: int, index: int = 0) -> ToolContent:
        """Return a locally captured image attached to a message in the configured chat."""
        path = bridge.store.media(message_id, index)
        if path is None:
            raise ValueError("No captured media for this message")
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as error:
            raise ValueError("The captured image could not be read") from error
        return ToolContent(content=[{
            "type": "image", "mimeType": guess_type(path)[0] or "image/png", "data": data,
        }])

    @tools.tool()
    async def get_sync_gaps(after_id: ToolCursor = 0, limit: ToolLimit = 30) -> list[dict[str, Any]]:
        """List recorded discontinuities in the configured chat's local history."""
        return bridge.store.sync_gaps(after_id, limit)

    @tools.tool(conversation=True)
    async def search_memories(q: ToolSearch | None = None, limit: ToolLimit = 30) -> list[dict[str, Any]]:
        """List or search the saved memories and summaries of the configured chat."""
        return bridge.store.memories(q, limit)

    def draft_status(draft: dict) -> dict[str, Any]:
        return {key: draft[key] for key in ("id", "origin", "status", "detail", "quote_message_id", "updated_at")}

    @tools.tool()
    async def list_drafts(limit: ToolLimit = 30) -> list[dict[str, Any]]:
        """List recent draft statuses in the configured chat."""
        return [draft_status(draft) for draft in bridge.store.list_drafts(limit)]

    @tools.tool()
    async def get_draft_status(draft_id: int) -> dict[str, Any]:
        """Check a draft's confirmation or delivery result."""
        if (draft := bridge.store.get_draft(draft_id)) is None:
            raise ValueError("Draft not found in the configured chat")
        return draft_status(draft)

    @tools.tool()
    async def draft_message(
        text: str, request_id: str | None = None, quote_message_id: int | None = None,
        author_kind: AuthorKind = "unknown",
    ) -> dict[str, Any]:
        """Create a pending draft for human review."""
        return await bridge.draft(
            DraftBody(text=text, request_id=request_id, quote_message_id=quote_message_id, author_kind=author_kind),
            origin="mcp",
        )

    @tools.tool(conversation=True)
    async def get_configured_chat_messages(limit: Annotated[int, Field(ge=1, le=20, strict=True)] = 20) -> ToolContent:
        """Read fresh text visible in the configured WeChat chat without changing chats."""
        return ToolContent.model_validate(await bridge.read_current_chat(
            AgentWechatToolBody(chat=bridge.store.chat, arguments={"limit": limit}),
            native_client(), native_token,
        ))

    return tools


def mcp_server(tools: ToolRegistry):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import CallToolResult, Tool

    class BridgeMCP(FastMCP):
        async def list_tools(self):
            return [Tool.model_validate(tool) for tool in await tools.list_tools()]

        async def call_tool(self, name: str, arguments: dict[str, Any]):
            return CallToolResult.model_validate(await tools.call_tool(name, arguments))

    return BridgeMCP(
        "Agent-Accessible-Wechat",
        stateless_http=True,
        json_response=True,
        max_request_body_size=32 * 1024 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )


def create_app(settings: Settings, token: str, mcp_token: str = "", *, source=None, sender=None,
               wechat_tools_socket: str | None = None, wechat_tools_token: str = ""):
    if len(token) < 24 or not token.isascii():
        raise ValueError("BRIDGE_TOKEN must contain at least 24 ASCII characters")
    if settings.mcp_enabled and (
        len(mcp_token) < 24 or not mcp_token.isascii() or token == mcp_token
    ):
        raise ValueError("MCP_TOKEN must be a distinct token of at least 24 ASCII characters")
    if wechat_tools_socket and (
        len(wechat_tools_token) < 24 or not wechat_tools_token.isascii()
        or wechat_tools_token in {token, mcp_token}
    ):
        raise ValueError("WECHAT_TOOLS_TOKEN must be a distinct token of at least 24 ASCII characters")

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
    wechat_tools_client: httpx.AsyncClient | None = None
    tools = bridge_tools(bridge, lambda: wechat_tools_client, wechat_tools_token)
    mcp = mcp_server(tools) if settings.mcp_enabled else None
    mcp_app = mcp.streamable_http_app() if mcp else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal store, wechat_tools_client
        async with AsyncExitStack() as stack:
            store = Store(data_dir, settings.chat)
            bridge.store = store
            stack.callback(store.close)
            bridge.active_session = None
            bridge.session_selection = None
            bridge.restore_target = None
            bridge.restore_failed = False
            if (selected := store.selected_session()) is not None:
                target, revision, nickname = selected
                bridge.restore_target = Session.model_validate(target)
                if revision is not None:
                    bridge.session_selection = SessionSelection(revision=revision, mention_nickname=nickname)
                store.bind_target("")
            synchronizer = Synchronizer(source, store, settings.poll_interval)
            synchronizer.paused = bridge.restore_target is not None
            bridge.synchronizer = synchronizer
            await source.start()
            stack.push_async_callback(source.stop)
            if wechat_tools_socket:
                wechat_tools_client = await stack.enter_async_context(httpx.AsyncClient(
                    base_url="http://127.0.0.1", transport=httpx.AsyncHTTPTransport(uds=wechat_tools_socket),
                    timeout=httpx.Timeout(600, connect=5, pool=5), trust_env=False,
                ))
            if mcp:
                await stack.enter_async_context(mcp.session_manager.run())
            tasks = [asyncio.create_task(synchronizer.run())]
            if bridge.restore_target is not None:
                tasks.append(asyncio.create_task(bridge.restore_session(wechat_tools_client, wechat_tools_token)))
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Agent-Accessible-Wechat", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        path = request.scope["path"]
        public_worker_route = (
            wechat_tools_socket and request.method == "GET" and path.removeprefix("/wechat/") in PUBLIC_ROUTES
            and path.startswith("/wechat/")
        )
        if path != "/health" and not public_worker_route:
            is_mcp = path == "/mcp" or path.startswith("/mcp/")
            expected = (mcp_token if settings.mcp_enabled else "") if is_mcp else token
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

    @app.get("/tools")
    async def tool_catalog(scope: Literal["conversation"] | None = None, chat: str | None = None,
                           session_key: str | None = None):
        if scope == "conversation":
            bridge.check_chat(chat, session_key)
        return {"tools": await tools.list_tools(conversation=scope == "conversation")}

    @app.post("/tools/{name}")
    async def execute_tool(name: str, arguments: dict, scope: Literal["conversation"] | None = None,
                           chat: str | None = None, session_key: str | None = None):
        if scope == "conversation":
            bridge.check_chat(chat, session_key)
        generation = bridge.synchronizer.generation
        result = await tools.call_tool(name, arguments, conversation=scope == "conversation")
        if scope == "conversation" and generation != bridge.synchronizer.generation:
            raise HTTPException(409, "The active WeChat session changed")
        return result

    @app.get("/status")
    async def status():
        return await bridge.status()

    @app.get("/sessions")
    async def sessions():
        catalog = await list_sessions(wechat_tools_client, wechat_tools_token)
        active = bridge.active_session
        active_matches = [entry for entry in catalog.sessions if active and active.matches_catalog(entry)]
        return {
            "sessions": [
                (active.summary(active=True) if active_matches == [entry] else entry.summary())
                for entry in catalog.sessions
            ],
            "active": active.summary(active=True) if active else None,
            "complete": catalog.complete,
        }

    @app.put("/sessions/active")
    async def activate_session(payload: SelectSessionBody):
        catalog = await list_sessions(wechat_tools_client, wechat_tools_token)
        current = bridge.active_session
        matches = [entry for entry in catalog.sessions if entry.key == payload.key or (
            current and payload.key == current.key and current.matches_catalog(entry)
        )]
        if not matches:
            raise HTTPException(404, "The selected session is no longer in the WeChat list")
        if len(matches) != 1:
            raise HTTPException(409, "The selected session has more than one match")
        active, selection = await bridge.activate_session(matches[0], wechat_tools_client, wechat_tools_token)
        return {"active": active.summary(active=True), "session_selection": selection.model_dump()}

    @app.put("/sessions/active/mention")
    async def set_session_mention(payload: SessionMentionBody):
        selection = await bridge.set_session_mention(payload)
        return {"session_selection": selection.model_dump()}

    @app.get("/messages")
    async def messages(limit: Limit = 30, for_model: bool = False, session_key: str | None = None):
        bridge.check_key(session_key)
        return {"messages": store.messages(limit=limit, for_model=for_model)}

    @app.get("/messages/new")
    async def new_messages(after_id: Cursor = 0, limit: Limit = 100, session_key: str | None = None):
        bridge.check_key(session_key)
        return {"messages": store.messages(limit=limit, after_id=after_id)}

    @app.get("/messages/search")
    async def search_messages(q: Search, limit: Limit = 30, for_model: bool = False, session_key: str | None = None):
        bridge.check_key(session_key)
        return {"messages": store.messages(limit=limit, q=q, for_model=for_model)}

    @app.get("/messages/wait")
    async def wait_messages(after_id: Cursor = 0, timeout: WaitSeconds = 15, limit: Limit = 100,
                            session_key: str | None = None):
        bridge.check_key(session_key)
        return {"messages": await bridge.synchronizer.wait_for_messages(after_id, limit, timeout)}

    @app.get("/messages/{message_id}")
    async def get_message(message_id: int, session_key: str | None = None):
        bridge.check_key(session_key)
        if (row := store.get_message(message_id)) is None:
            raise HTTPException(404, "Message not found")
        return row

    @app.get("/sync/gaps")
    async def sync_gaps(after_id: Cursor = 0, limit: Limit = 30):
        return {"gaps": store.sync_gaps(after_id, limit)}

    @app.get("/media/{message_id}")
    async def media(message_id: int, index: Annotated[int, Query(ge=0, le=1)] = 0, session_key: str | None = None):
        bridge.check_key(session_key)
        path = store.media(message_id, index)
        if path is None:
            raise HTTPException(404, "Captured media not found")
        return FileResponse(path)

    @app.post("/drafts")
    async def create_draft(payload: DraftBody):
        return await bridge.draft(payload, origin="http")

    @app.get("/drafts")
    async def list_drafts(limit: Limit = 30, session_key: str | None = None):
        bridge.check_key(session_key)
        return {"drafts": store.list_drafts(limit)}

    @app.get("/drafts/{draft_id}")
    async def get_draft(draft_id: int, session_key: str | None = None):
        bridge.check_key(session_key)
        if (row := store.get_draft(draft_id)) is None:
            raise HTTPException(404, "Draft not found")
        return row

    @app.post("/drafts/{draft_id}/confirm")
    async def confirm_draft(draft_id: int, session_key: str | None = None):
        return await bridge.confirm(draft_id, session_key)

    @app.get("/memories")
    async def memories(q: Search | None = None, limit: Limit = 30, session_key: str | None = None):
        bridge.check_key(session_key)
        return {"memories": store.memories(q, limit)}

    @app.post("/memories")
    async def create_memory(payload: MemoryBody):
        bridge.check_chat(payload.chat, payload.session_key)
        try:
            return store.create_memory(payload.kind, payload.text, payload.request_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/wechat/agent-tools/{name}")
    async def agent_wechat_tool(name: str, payload: AgentWechatToolBody):
        if name != "wechat_read_current_chat":
            raise HTTPException(404, "The tool is not available to the companion")
        return await bridge.read_current_chat(payload, wechat_tools_client, wechat_tools_token)

    if wechat_tools_socket:
        @app.api_route("/wechat/{path:path}", methods=["GET", "POST", "DELETE"])
        async def wechat_tools(request: Request, path: str):
            if path not in TOOLS_ROUTES or wechat_tools_client is None:
                raise HTTPException(404, "WeChat tools route not found")
            return await forward_wechat_tools(wechat_tools_client, request, path, wechat_tools_token)

    if mcp_app:
        app.mount("/", mcp_app)
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load()
    bridge_token, mcp_token = os.environ.get("BRIDGE_TOKEN", ""), os.getenv("MCP_TOKEN", "")
    tools_token = os.environ.pop("WECHAT_TOOLS_TOKEN", "")
    wechat_tools_socket = None
    if os.getenv("WECHAT_TOOLS_ENABLED", "true") == "true":
        wechat_tools_socket = os.getenv("AAW_WECHAT_TOOLS_SOCKET")
    app = create_app(settings, bridge_token, mcp_token, wechat_tools_socket=wechat_tools_socket,
                     wechat_tools_token=tools_token)
    uvicorn.run(app, host=os.getenv("BRIDGE_HOST", "0.0.0.0"), port=int(os.getenv("BRIDGE_PORT", "8765")),
                access_log=False)


if __name__ == "__main__":
    main()
