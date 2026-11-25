import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .models import AuthorKind, Message, QuoteTarget, message_key, now, split_wechat_quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat_id ON messages(chat, id);
CREATE TABLE IF NOT EXISTS sync_state (
    chat TEXT PRIMARY KEY,
    anchors TEXT,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_gaps (
    id INTEGER PRIMARY KEY,
    chat TEXT NOT NULL,
    reason TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sync_gaps_chat_id ON sync_gaps(chat, id);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    chat TEXT NOT NULL,
    text TEXT NOT NULL,
    origin TEXT NOT NULL,
    request_id TEXT,
    auto_send INTEGER NOT NULL,
    quote_message_id INTEGER,
    author_kind TEXT NOT NULL DEFAULT 'unknown',
    submission_anchors TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat, origin, request_id)
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    chat TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chat, request_id)
);
CREATE INDEX IF NOT EXISTS memories_chat_id ON memories(chat, id);
CREATE TABLE IF NOT EXISTS active_session (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    selection TEXT NOT NULL,
    revision TEXT,
    mention_nickname TEXT
);
"""


class Store:
    def __init__(self, data_dir: Path, chat: str):
        self.bind_target(chat)
        self.data_dir = data_dir
        self.media_dir = data_dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(data_dir / "messages.db")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        with self.transaction() as db:
            columns = {column["name"] for column in db.execute("PRAGMA table_info(drafts)")}
            for name, declaration in (
                ("quote_message_id", "INTEGER"),
                ("author_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("submission_anchors", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {declaration}")
            session_columns = {column["name"] for column in db.execute("PRAGMA table_info(active_session)")}
            for name in ("revision", "mention_nickname"):
                if name not in session_columns:
                    db.execute(f"ALTER TABLE active_session ADD COLUMN {name} TEXT")
            db.execute(
                """UPDATE drafts SET status='uncertain', detail=?, updated_at=?
                   WHERE status='sending'""",
                ("Bridge stopped during the send attempt", now().isoformat()),
            )

    def bind_target(self, chat: str, partition: str | None = None) -> None:
        self.chat = chat
        self.partition = chat if partition is None else partition

    def selected_session(self) -> tuple[dict, str | None, str | None] | None:
        row = self.connection.execute(
            "SELECT selection, revision, mention_nickname FROM active_session WHERE singleton=1",
        ).fetchone()
        return (json.loads(row["selection"]), row["revision"], row["mention_nickname"]) if row else None

    def select_session(
        self, selection: dict, *, revision: str | None = None, mention_nickname: str | None = None,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO active_session(singleton, selection, revision, mention_nickname)
                   VALUES (1, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET selection=excluded.selection,
                   revision=excluded.revision, mention_nickname=excluded.mention_nickname""",
                (json.dumps(selection, ensure_ascii=False), revision, mention_nickname),
            )

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection

    def anchors(self) -> tuple[str, ...] | None:
        row = self.connection.execute(
            "SELECT anchors FROM sync_state WHERE chat=?", (self.partition,)
        ).fetchone()
        if row is None or row["anchors"] is None:
            return None
        return tuple(json.loads(row["anchors"]))

    @staticmethod
    def _message_record(message: Message) -> dict:
        if message.source == "accessibility" and message.type == "text" and message.text and message.quote is None:
            if parts := split_wechat_quote(message.text):
                message = replace(message, reply_text=parts[0], quote=parts[1])
        return asdict(message)

    @staticmethod
    def _quote_bodies(record: dict) -> set[str]:
        text = record.get("text")
        if not isinstance(text, str):
            return set()
        body = text.replace("\r\n", "\n").replace("\u2029", "\n")
        reply = record.get("reply_text")
        if isinstance(reply, str):
            return {body, reply.replace("\r\n", "\n").replace("\u2029", "\n")}
        if record.get("source") == "accessibility" and (parts := split_wechat_quote(body)):
            return {body, parts[0]}
        return {body}

    def _quote_author_kind(self, candidate: dict) -> AuthorKind:
        source = candidate.get("author_source")
        kind = candidate.get("author_kind")
        if kind == "human" and source != "unverified_bridge_submission":
            return "human"
        if kind != "llm":
            return "unknown"
        if source == "verified_live_test_audit":
            return "llm"
        draft_id = candidate.get("outbound_draft_id")
        if source == "confirmed_bridge_submission" and type(draft_id) is int:
            draft = self.get_draft(draft_id)
            if draft and draft["status"] == "sent" and draft["author_kind"] == "llm":
                sent_body = draft["text"].replace("\r\n", "\n").replace("\u2029", "\n")
                if sent_body in self._quote_bodies(candidate):
                    return "llm"
        return "unknown"

    def _resolve_quote(self, record: dict) -> None:
        quote = record.get("quote")
        if not isinstance(quote, dict):
            return
        quote.pop("author_kind", None)
        quote.pop("message_id", None)
        text = record.get("text")
        if record.get("source") != "accessibility" or record.get("type") != "text" or not isinstance(text, str):
            return
        # Check the stored raw label, not attribution supplied in the structured quote.
        parts = split_wechat_quote(text)
        if parts is None or quote.get("text") != parts[1]["text"] or quote.get("author_label") != parts[1]["author_label"]:
            return
        body = parts[1]["text"]
        if body.rstrip().endswith(("…", "...")):
            return
        rows = self.connection.execute(
            """WITH prior AS (
                   SELECT id, record,
                          replace(replace(json_extract(record, '$.text'), char(13)||char(10), char(10)),
                                  char(8233), char(10)) AS body,
                          replace(replace(json_extract(record, '$.reply_text'), char(13)||char(10), char(10)),
                                  char(8233), char(10)) AS reply
                   FROM messages WHERE chat=:chat AND id<:before AND json_extract(record, '$.type')='text'
               )
               SELECT id, record FROM prior
               WHERE body=:body OR reply=:body OR instr(body, :legacy_prefix)=1""",
            {"chat": self.partition, "before": record["id"], "body": body, "legacy_prefix": body + "\nQuote "},
        )
        match = None
        for row in rows:
            candidate = json.loads(row["record"])
            if body not in self._quote_bodies(candidate):
                continue
            if match is not None:
                return
            match = row["id"], candidate
        if match is not None and (kind := self._quote_author_kind(match[1])) != "unknown":
            quote.update(author_kind=kind, message_id=match[0])

    def save_sync(
        self, state: str, *, anchors: tuple[str, ...] | None = None,
        messages: tuple[Message, ...] = (), gap: str | None = None,
        finished_draft: tuple[int, str | None] | None = None,
        resolved_attempts: tuple[int, ...] = (),
    ) -> None:
        stamp = now().isoformat()
        with self.transaction() as db:
            db.executemany(
                "INSERT INTO messages(chat,record) VALUES (?,?)",
                [(self.partition, json.dumps(self._message_record(message), default=lambda dt: dt.isoformat()))
                 for message in messages],
            )
            db.execute(
                """INSERT INTO sync_state(chat,anchors,state,updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(chat) DO UPDATE SET
                       anchors=coalesce(excluded.anchors,sync_state.anchors),
                       state=excluded.state, updated_at=excluded.updated_at""",
                (self.partition, None if anchors is None else json.dumps(anchors), state, stamp),
            )
            if gap:
                db.execute(
                    "INSERT INTO sync_gaps(chat,reason,observed_at) VALUES (?,?,?)",
                    (self.partition, gap, stamp),
                )
            if finished_draft:
                draft_id, detail = finished_draft
                if not self._finish_draft(db, draft_id, "sent", detail, keep_submission=False):
                    raise ValueError("The confirmed draft was no longer claimed")
            db.executemany(
                "UPDATE drafts SET submission_anchors=NULL WHERE chat=? AND id=? AND status='uncertain'",
                [(self.partition, draft_id) for draft_id in resolved_attempts],
            )

    def sync_status(self) -> dict:
        row = self.connection.execute(
            "SELECT state, updated_at FROM sync_state WHERE chat=?", (self.partition,)
        ).fetchone()
        last_gap = self.connection.execute(
            "SELECT id,reason,observed_at FROM sync_gaps WHERE chat=? ORDER BY id DESC LIMIT 1",
            (self.partition,),
        ).fetchone()
        return {
            **(dict(row) if row else {"state": "starting", "updated_at": None}),
            "last_gap": dict(last_gap) if last_gap else None,
        }

    def sync_gaps(self, after_id: int, limit: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id,reason,observed_at FROM sync_gaps WHERE chat=? AND id>? ORDER BY id LIMIT ?",
            (self.partition, after_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def messages(
        self, *, limit: int, after_id: int | None = None, q: str | None = None,
        for_model: bool = False,
    ) -> list[dict]:
        conditions = ["chat=?"]
        parameters: list = [self.partition]
        if after_id is not None:
            conditions.append("id>?")
            parameters.append(after_id)
        if for_model:
            conditions.append("""(
                coalesce(json_extract(record, '$.author_kind'), 'unknown') <> 'llm'
                AND coalesce(json_extract(record, '$.author_source'), '') <> 'unverified_bridge_submission'
                AND (coalesce(json_extract(record, '$.is_self'), 0) <> 1
                     OR json_extract(record, '$.author_kind') = 'human')
            )""")
        if q is not None:
            conditions.append("""(
                instr(lower(json_extract(record, '$.text')), lower(?))>0
                OR instr(lower(json_extract(record, '$.sender')), lower(?))>0
                OR instr(lower(json_extract(record, '$.url')), lower(?))>0
            )""")
            parameters.extend((q, q, q))
        order = "ASC" if after_id is not None else "DESC"
        rows = self.connection.execute(
            f"SELECT id,record FROM messages WHERE {' AND '.join(conditions)} ORDER BY id {order} LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        if after_id is None:
            rows.reverse()
        return [self._format_message(row) for row in rows]

    def get_message(self, message_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT id,record FROM messages WHERE chat=? AND id=?", (self.partition, message_id),
        ).fetchone()
        return self._format_message(row) if row is not None else None

    def _format_message(self, row: sqlite3.Row) -> dict:
        record = {
            "author_kind": "unknown", "author_source": None, "outbound_draft_id": None,
            "reply_text": None, "quote": None, **json.loads(row["record"]), "id": row["id"],
        }
        if (record["type"] == "text" and record["source"] == "accessibility"
                and record["text"] and record["quote"] is None):
            if parts := split_wechat_quote(record["text"]):
                record["reply_text"], record["quote"] = parts
        self._resolve_quote(record)
        return record

    def media(self, message_id: int, index: int = 0) -> Path | None:
        if type(index) is not int or index < 0:
            return None
        row = self.connection.execute(
            "SELECT record FROM messages WHERE chat=? AND id=?", (self.partition, message_id)
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record"])
        filename = record.get("media_path")
        if index:
            metadata = record.get("metadata")
            paths = metadata.get("media_paths") if isinstance(metadata, dict) else None
            if not isinstance(paths, list) or index >= len(paths):
                return None
            filename = paths[index]
        if not isinstance(filename, str) or not filename:
            return None
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        try:
            root = self.media_dir.resolve(strict=True)
            path = (root / relative).resolve(strict=True)
            return path if path.is_relative_to(root) and path.is_file() else None
        except (OSError, RuntimeError, ValueError):
            return None

    def quote_target(self, message_id: int) -> QuoteTarget | None:
        recent = self.messages(limit=100)
        index = next((i for i, row in enumerate(recent) if row["id"] == message_id), None)
        if index is None:
            return None
        target = recent[index]
        if (target["type"] != "text" or not target["text"] or target["quote"] is not None
                or target["author_kind"] == "llm"
                or target["author_source"] == "unverified_bridge_submission"
                or (target["is_self"] is True and target["author_kind"] != "human")):
            return None

        keys = []
        for row in recent[index:]:
            row["observed_at"] = datetime.fromisoformat(row["observed_at"])
            if row["timestamp"] is not None:
                row["timestamp"] = datetime.fromisoformat(row["timestamp"])
            keys.append(message_key(Message(**row)))
        return QuoteTarget(
            target["text"], tuple(keys),
            allow_own=target["author_kind"] == "human" and target["is_self"] is True,
        )

    def create_draft(
        self, text: str, request_id: str | None, origin: str, auto_send: bool,
        quote_message_id: int | None = None, author_kind: AuthorKind = "human",
    ) -> dict:
        stamp = now().isoformat()
        with self.transaction() as db:
            row = db.execute(
                """INSERT INTO drafts(chat,text,origin,request_id,auto_send,quote_message_id,author_kind,status,
                                      created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,'pending',?,?)
                   ON CONFLICT(chat,origin,request_id) DO NOTHING RETURNING *""",
                (self.partition, text, origin, request_id, auto_send, quote_message_id, author_kind, stamp, stamp),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT * FROM drafts WHERE chat=? AND origin=? AND request_id=?",
                    (self.partition, origin, request_id),
                ).fetchone()
                if (row["text"] != text or bool(row["auto_send"]) != auto_send
                        or row["quote_message_id"] != quote_message_id
                        or row["author_kind"] not in ("unknown", author_kind)):
                    raise ValueError("request_id already belongs to a different draft")
                if row["author_kind"] == "unknown" and row["status"] == "pending":
                    row = db.execute(
                        "UPDATE drafts SET author_kind=? WHERE id=? AND chat=? RETURNING *",
                        (author_kind, row["id"], self.partition),
                    ).fetchone()
        return {**dict(row), "chat": self.chat}

    def get_draft(self, draft_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM drafts WHERE chat=? AND id=?", (self.partition, draft_id)
        ).fetchone()
        return {**dict(row), "chat": self.chat} if row else None

    def list_drafts(self, limit: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM drafts WHERE chat=? ORDER BY id DESC LIMIT ?", (self.partition, limit)
        ).fetchall()
        return [{**dict(row), "chat": self.chat} for row in rows]

    def claim_draft(self, draft_id: int) -> bool:
        with self.transaction() as db:
            result = db.execute(
                """UPDATE drafts SET status='sending',updated_at=?
                   WHERE chat=? AND id=? AND status='pending'""",
                (now().isoformat(), self.partition, draft_id),
            )
            return result.rowcount == 1

    def checkpoint_submission(self, draft_id: int, anchors: tuple[str, ...]) -> None:
        with self.transaction() as db:
            changed = db.execute(
                """UPDATE drafts SET submission_anchors=?,updated_at=?
                   WHERE chat=? AND id=? AND status='sending' AND submission_anchors IS NULL""",
                (json.dumps(anchors), now().isoformat(), self.partition, draft_id),
            )
            if changed.rowcount != 1:
                raise ValueError("The draft could not record its pre-send snapshot")

    def unverified_model_submissions(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            """SELECT id,text,quote_message_id,submission_anchors FROM drafts
               WHERE chat=? AND author_kind IN ('llm','unknown') AND status='uncertain'
               AND submission_anchors IS NOT NULL ORDER BY id""",
            (self.partition,),
        )]

    def _finish_draft(self, db, draft_id: int, status: str, detail: str | None, *, keep_submission: bool) -> bool:
        updated = db.execute(
            """UPDATE drafts SET status=?,detail=?,updated_at=?,
                                 submission_anchors=CASE WHEN ? THEN submission_anchors END
               WHERE chat=? AND id=? AND status='sending'""",
            (status, detail, now().isoformat(), keep_submission, self.partition, draft_id),
        )
        return updated.rowcount == 1

    def finish_draft(
        self, draft_id: int, status: str, detail: str | None = None, *, keep_submission: bool = False,
    ) -> None:
        with self.connection:
            self._finish_draft(
                self.connection, draft_id, status, detail, keep_submission=keep_submission
            )

    def memories(self, q: str | None, limit: int) -> list[dict]:
        rows = self.connection.execute(
            """SELECT id,kind,text,created_at FROM memories
               WHERE chat=? AND (? IS NULL OR instr(lower(text),lower(?))>0)
               ORDER BY id DESC LIMIT ?""",
            (self.partition, q, q, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_memory(self, kind: str, text: str, request_id: str | None) -> dict:
        with self.transaction() as db:
            row = db.execute(
                """INSERT INTO memories(chat,kind,text,request_id,created_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(chat,request_id) DO NOTHING RETURNING *""",
                (self.partition, kind, text, request_id, now().isoformat()),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT * FROM memories WHERE chat=? AND request_id=?", (self.partition, request_id)
                ).fetchone()
                if (row["kind"], row["text"]) != (kind, text):
                    raise ValueError("request_id already belongs to a different memory")
        return {**dict(row), "chat": self.chat}
