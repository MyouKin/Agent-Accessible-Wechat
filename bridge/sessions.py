"""Observable WeChat session identities and the private desktop driver calls."""

from hashlib import sha256
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class Session(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    kind: Literal["group", "private", "unknown"]
    native_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("name", "native_id")
    @classmethod
    def valid_text(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\0" in value):
            raise ValueError("Expected a visible WeChat session field")
        return value

    @property
    def key(self) -> str:
        identity = ("native:" + self.native_id) if self.native_id else f"{self.kind}:{self.name}"
        return "session-" + sha256(identity.encode()).hexdigest()[:24]

    @property
    def partition(self) -> str:
        # A verified group keeps its legacy title-keyed archive. DMs with the
        # same visible name as a group have a separate, reserved storage key.
        return self.name if self.kind == "group" else f"\0private:{self.key}"

    def summary(self, active: bool = False) -> dict:
        return {**self.model_dump(), "key": self.key, "active": active}

    def matches_catalog(self, entry: "Session") -> bool:
        return (
            self.name == entry.name and self.native_id == entry.native_id
            and entry.kind in {"unknown", self.kind}
        )


class SessionCatalog(BaseModel):
    sessions: list[Session] = Field(max_length=1000)
    complete: bool


async def _request(client: httpx.AsyncClient | None, token: str, method: str, path: str, **kwargs) -> dict:
    if client is None:
        raise HTTPException(503, "The WeChat session driver is unavailable")
    try:
        response = await client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, timeout=40, **kwargs,
        )
        if response.status_code in {400, 404, 409, 422}:
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else None
            raise HTTPException(response.status_code, detail if isinstance(detail, str) else "Session could not be verified")
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, "The WeChat session driver is unavailable") from error
    if not isinstance(data, dict):
        raise HTTPException(502, "The WeChat session driver returned an invalid result")
    return data


async def list_sessions(client: httpx.AsyncClient | None, token: str) -> SessionCatalog:
    data = await _request(client, token, "GET", "/sessions")
    try:
        return SessionCatalog.model_validate(data)
    except ValidationError as error:
        raise HTTPException(502, "The WeChat session driver returned an invalid catalog") from error


async def open_session(client: httpx.AsyncClient | None, token: str, target: Session) -> Session:
    data = await _request(client, token, "POST", "/sessions/open", json={"name": target.name, "kind": target.kind})
    try:
        observed = Session.model_validate(data)
    except ValidationError as error:
        raise HTTPException(502, "The WeChat session driver returned an invalid selection") from error
    if (observed.kind == "unknown" or observed.name != target.name or observed.native_id != target.native_id
            or (target.kind != "unknown" and observed.kind != target.kind)):
        raise HTTPException(409, "The opened WeChat session did not match the selection")
    return observed
