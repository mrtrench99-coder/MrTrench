"""Persistent thought and memory store for an experimental assistant.

This module stores cognitive state; it does not generate thoughts or run a
background scheduler. A caller can use the returned thread and memory records
to decide what to reactivate when new context arrives.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id() -> str:
    return str(uuid.uuid4())


class ThoughtMemory:
    """SQLite-backed store for resumable thought threads and linked memories."""

    def __init__(self, database: str | Path = "thought_memory.sqlite3") -> None:
        self.database = str(database)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS thought_threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    current_state TEXT NOT NULL DEFAULT '',
                    resume_note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'suspended', 'resolved', 'archived')),
                    priority REAL NOT NULL DEFAULT 0.5 CHECK (priority >= 0 AND priority <= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_activated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'observation',
                    source TEXT NOT NULL DEFAULT 'assistant',
                    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
                    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thought_memory_links (
                    thread_id TEXT NOT NULL REFERENCES thought_threads(id) ON DELETE CASCADE,
                    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    relation TEXT NOT NULL DEFAULT 'context',
                    strength REAL NOT NULL DEFAULT 0.5 CHECK (strength >= 0 AND strength <= 1),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (thread_id, memory_id, relation)
                );

                CREATE TABLE IF NOT EXISTS thought_links (
                    source_thread_id TEXT NOT NULL REFERENCES thought_threads(id) ON DELETE CASCADE,
                    target_thread_id TEXT NOT NULL REFERENCES thought_threads(id) ON DELETE CASCADE,
                    relation TEXT NOT NULL DEFAULT 'related',
                    strength REAL NOT NULL DEFAULT 0.5 CHECK (strength >= 0 AND strength <= 1),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source_thread_id, target_thread_id, relation),
                    CHECK (source_thread_id <> target_thread_id)
                );

                CREATE TABLE IF NOT EXISTS thought_events (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT REFERENCES thought_threads(id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS proactive_messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT REFERENCES thought_threads(id) ON DELETE SET NULL,
                    body TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'delivered', 'dismissed')),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cognitive_triggers (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('reminder', 'check_in')),
                    title TEXT NOT NULL,
                    context TEXT NOT NULL DEFAULT '',
                    due_at TEXT NOT NULL,
                    thread_id TEXT REFERENCES thought_threads(id) ON DELETE SET NULL,
                    purpose TEXT NOT NULL DEFAULT '',
                    pause_after_follow_up INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'fired', 'cancelled')),
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS cognitive_controls (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_threads_status_priority
                    ON thought_threads(status, priority DESC, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_content ON memories(content);
                CREATE INDEX IF NOT EXISTS idx_events_thread_time
                    ON thought_events(thread_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_time
                    ON proactive_messages(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_conversation_time
                    ON conversation_messages(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cognitive_triggers_due
                    ON cognitive_triggers(status, due_at);
                """
            )
            trigger_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(cognitive_triggers)")
            }
            if "pause_after_follow_up" not in trigger_columns:
                db.execute(
                    "ALTER TABLE cognitive_triggers ADD COLUMN "
                    "pause_after_follow_up INTEGER NOT NULL DEFAULT 0"
                )
            if "purpose" not in trigger_columns:
                db.execute(
                    "ALTER TABLE cognitive_triggers ADD COLUMN purpose TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _event(self, db: sqlite3.Connection, thread_id: str | None,
               event_type: str, detail: dict[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO thought_events VALUES (?, ?, ?, ?, ?)",
            (_id(), thread_id, event_type, json.dumps(detail or {}), _now()),
        )

    def create_thread(self, title: str, *, summary: str = "",
                      current_state: str = "", priority: float = 0.5) -> dict[str, Any]:
        """Start a new line of thought, initially available for attention."""
        thread_id, now = _id(), _now()
        with self._connection() as db:
            db.execute(
                """INSERT INTO thought_threads
                   (id, title, summary, current_state, priority, created_at, updated_at,
                    last_activated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (thread_id, title, summary, current_state, priority, now, now, now),
            )
            self._event(db, thread_id, "created")
            row = db.execute("SELECT * FROM thought_threads WHERE id = ?", (thread_id,)).fetchone()
        return self._record(row) or {}

    def update_thread(self, thread_id: str, *, summary: str | None = None,
                      current_state: str | None = None, priority: float | None = None) -> dict[str, Any]:
        """Update the compact working state that should survive suspension."""
        fields = {"summary": summary, "current_state": current_state, "priority": priority}
        updates = {key: value for key, value in fields.items() if value is not None}
        if not updates:
            thread = self.get_thread(thread_id)
            if thread is None:
                raise KeyError(thread_id)
            return thread
        updates["updated_at"] = _now()
        clause = ", ".join(f"{key} = ?" for key in updates)
        with self._connection() as db:
            result = db.execute(
                f"UPDATE thought_threads SET {clause} WHERE id = ?",
                (*updates.values(), thread_id),
            )
            if result.rowcount == 0:
                raise KeyError(thread_id)
            self._event(db, thread_id, "updated", updates)
            row = db.execute("SELECT * FROM thought_threads WHERE id = ?", (thread_id,)).fetchone()
        return self._record(row) or {}

    def suspend(self, thread_id: str, resume_note: str) -> dict[str, Any]:
        """Save a handoff note and mark the thread as waiting for a trigger."""
        return self._set_status(thread_id, "suspended", "suspended", resume_note)

    def resume(self, thread_id: str, *, reason: str = "") -> dict[str, Any]:
        """Reactivate a thread and record what brought it back to attention."""
        return self._set_status(thread_id, "active", "resumed", reason, activate=True)

    def resolve(self, thread_id: str, *, note: str = "") -> dict[str, Any]:
        return self._set_status(thread_id, "resolved", "resolved", note)

    def _set_status(self, thread_id: str, status: str, event: str, note: str,
                    activate: bool = False) -> dict[str, Any]:
        now = _now()
        with self._connection() as db:
            result = db.execute(
                """UPDATE thought_threads SET status = ?, resume_note = ?, updated_at = ?,
                   last_activated_at = CASE WHEN ? THEN ? ELSE last_activated_at END
                   WHERE id = ?""",
                (status, note, now, int(activate), now, thread_id),
            )
            if result.rowcount == 0:
                raise KeyError(thread_id)
            self._event(db, thread_id, event, {"reason": note})
            row = db.execute("SELECT * FROM thought_threads WHERE id = ?", (thread_id,)).fetchone()
        return self._record(row) or {}

    def add_memory(self, content: str, *, kind: str = "observation",
                   source: str = "assistant", confidence: float = 0.5,
                   importance: float = 0.5, metadata: dict[str, Any] | None = None,
                   thread_id: str | None = None, relation: str = "context",
                   strength: float = 0.5) -> dict[str, Any]:
        """Store a memory with provenance and optionally attach it to a thread."""
        memory_id, now = _id(), _now()
        with self._connection() as db:
            db.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, content, kind, source, confidence, importance,
                 json.dumps(metadata or {}), now),
            )
            if thread_id is not None:
                self._require_thread(db, thread_id)
                db.execute(
                    "INSERT INTO thought_memory_links VALUES (?, ?, ?, ?, ?)",
                    (thread_id, memory_id, relation, strength, now),
                )
                self._event(db, thread_id, "memory_added", {"memory_id": memory_id})
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        result = self._record(row) or {}
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def link_threads(self, source_id: str, target_id: str, *,
                     relation: str = "related", strength: float = 0.5) -> None:
        """Record an association between two thought threads."""
        now = _now()
        with self._connection() as db:
            self._require_thread(db, source_id)
            self._require_thread(db, target_id)
            db.execute(
                "INSERT OR REPLACE INTO thought_links VALUES (?, ?, ?, ?, ?)",
                (source_id, target_id, relation, strength, now),
            )
            self._event(db, source_id, "thread_linked", {
                "target_thread_id": target_id, "relation": relation, "strength": strength
            })

    @staticmethod
    def _require_thread(db: sqlite3.Connection, thread_id: str) -> None:
        if db.execute("SELECT 1 FROM thought_threads WHERE id = ?", (thread_id,)).fetchone() is None:
            raise KeyError(thread_id)

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM thought_threads WHERE id = ?", (thread_id,)).fetchone()
        return self._record(row)

    def list_threads(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM thought_threads WHERE status = ? ORDER BY priority DESC, updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM thought_threads ORDER BY priority DESC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def thread_context(self, thread_id: str, *, limit: int = 20) -> dict[str, Any]:
        """Fetch a thread's attached memories and neighboring thought threads."""
        with self._connection() as db:
            thread = db.execute("SELECT * FROM thought_threads WHERE id = ?", (thread_id,)).fetchone()
            if thread is None:
                raise KeyError(thread_id)
            memories = db.execute(
                """SELECT m.*, l.relation, l.strength FROM memories m
                   JOIN thought_memory_links l ON l.memory_id = m.id
                   WHERE l.thread_id = ? ORDER BY m.importance DESC, m.created_at DESC LIMIT ?""",
                (thread_id, limit),
            ).fetchall()
            neighbors = db.execute(
                """SELECT t.*, l.relation, l.strength FROM thought_links l
                   JOIN thought_threads t ON t.id = l.target_thread_id
                   WHERE l.source_thread_id = ? ORDER BY l.strength DESC LIMIT ?""",
                (thread_id, limit),
            ).fetchall()
        memory_records = [dict(row) for row in memories]
        for record in memory_records:
            record["metadata"] = json.loads(record.pop("metadata_json"))
        return {"thread": dict(thread), "memories": memory_records,
                "related_threads": [dict(row) for row in neighbors]}

    def find_memories(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Basic text search for a prototype; replace with embeddings for semantic recall."""
        pattern = f"%{query}%"
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE content LIKE ? ORDER BY importance DESC, created_at DESC LIMIT ?",
                (pattern, limit),
            ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["metadata"] = json.loads(record.pop("metadata_json"))
        return records

    def recent_memories(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent durable memories, regardless of thread attachment."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["metadata"] = json.loads(record.pop("metadata_json"))
        return records

    def recent_events(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Return recent state changes for the background attention cycle."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM thought_events ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["detail"] = json.loads(record.pop("detail_json"))
        return records

    def queue_message(self, body: str, reason: str, *, thread_id: str | None = None) -> dict[str, Any]:
        """Persist an initiated message in the local outbox."""
        message_id, now = _id(), _now()
        with self._connection() as db:
            if thread_id is not None:
                self._require_thread(db, thread_id)
            db.execute(
                "INSERT INTO proactive_messages VALUES (?, ?, ?, ?, 'pending', ?)",
                (message_id, thread_id, body, reason, now),
            )
            self._event(db, thread_id, "message_queued", {"message_id": message_id, "reason": reason})
            row = db.execute("SELECT * FROM proactive_messages WHERE id = ?", (message_id,)).fetchone()
        return self._record(row) or {}

    def recent_messages(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM proactive_messages ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_conversation_message(self, role: str, content: str) -> dict[str, Any]:
        """Append a user or assistant turn to the durable conversation log."""
        if role not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        message_id, now = _id(), _now()
        with self._connection() as db:
            db.execute(
                "INSERT INTO conversation_messages VALUES (?, ?, ?, ?)",
                (message_id, role, content, now),
            )
            if role == "user":
                self._event(db, None, "conversation_message", {
                    "message_id": message_id, "content": content
                })
            row = db.execute(
                "SELECT * FROM conversation_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._record(row) or {}

    def recent_conversation(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent turns in chronological order for model context."""
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM (
                       SELECT rowid AS sequence, * FROM conversation_messages
                       ORDER BY created_at DESC, rowid DESC LIMIT ?
                   ) ORDER BY created_at ASC, sequence ASC""",
                (limit,),
            ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record.pop("sequence", None)
        return records

    def schedule_trigger(self, kind: str, title: str, due_at: str, *,
                         context: str = "", thread_id: str | None = None,
                         pause_after_follow_up: bool = False,
                         purpose: str = "") -> dict[str, Any]:
        """Persist a one-time reminder or personal check-in wake-up."""
        if kind not in {"reminder", "check_in"}:
            raise ValueError("kind must be 'reminder' or 'check_in'")
        trigger_id, now = _id(), _now()
        with self._connection() as db:
            if thread_id is not None:
                self._require_thread(db, thread_id)
            db.execute(
                """INSERT INTO cognitive_triggers
                   (id, kind, title, context, due_at, thread_id, purpose, pause_after_follow_up,
                    status, created_at, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)""",
                (trigger_id, kind, title, context, due_at, thread_id, purpose,
                 int(pause_after_follow_up), now),
            )
            self._event(db, thread_id, "trigger_scheduled", {
                "trigger_id": trigger_id, "kind": kind, "title": title, "due_at": due_at
            })
            row = db.execute(
                "SELECT * FROM cognitive_triggers WHERE id = ?", (trigger_id,)
            ).fetchone()
        return self._record(row) or {}

    def pending_triggers(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM cognitive_triggers WHERE status = 'pending' ORDER BY due_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def due_triggers(self, now: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM cognitive_triggers
                   WHERE status = 'pending' AND due_at <= ? ORDER BY due_at LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_trigger(self, trigger_id: str, status: str, *, note: str = "") -> dict[str, Any]:
        if status not in {"fired", "cancelled"}:
            raise ValueError("status must be 'fired' or 'cancelled'")
        now = _now()
        with self._connection() as db:
            result = db.execute(
                """UPDATE cognitive_triggers SET status = ?, resolved_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (status, now, trigger_id),
            )
            if result.rowcount == 0:
                raise KeyError(trigger_id)
            row = db.execute(
                "SELECT * FROM cognitive_triggers WHERE id = ?", (trigger_id,)
            ).fetchone()
            self._event(db, row["thread_id"], f"trigger_{status}", {
                "trigger_id": trigger_id, "note": note
            })
        return self._record(row) or {}

    def cancel_trigger(self, trigger_id: str, *, note: str = "Cancelled by user.") -> dict[str, Any]:
        return self.resolve_trigger(trigger_id, "cancelled", note=note)

    def set_proactive_pause(self, paused: bool, *, note: str = "") -> None:
        """Pause unsolicited outreach; explicitly requested reminders remain eligible."""
        value, now = ("true" if paused else "false"), _now()
        with self._connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO cognitive_controls VALUES ('user_initiated_only', ?, ?)",
                (value, now),
            )
            self._event(db, None, "proactive_pause_enabled" if paused else "proactive_pause_disabled",
                        {"note": note})
            if paused:
                pending = db.execute(
                    "SELECT id, thread_id FROM cognitive_triggers "
                    "WHERE status = 'pending' AND kind = 'check_in'"
                ).fetchall()
                db.execute(
                    "UPDATE cognitive_triggers SET status = 'cancelled', resolved_at = ? "
                    "WHERE status = 'pending' AND kind = 'check_in'",
                    (now,),
                )
                for item in pending:
                    self._event(db, item["thread_id"], "trigger_cancelled", {
                        "trigger_id": item["id"], "note": "Proactive check-ins paused by user."
                    })

    def proactive_paused(self) -> bool:
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM cognitive_controls WHERE key = 'user_initiated_only'"
            ).fetchone()
        return bool(row and row["value"] == "true")

    def fire_trigger(self, trigger_id: str, body: str, reason: str) -> dict[str, Any]:
        """Atomically queue a due follow-up and mark its trigger fired once."""
        message_id, now = _id(), _now()
        with self._connection() as db:
            trigger = db.execute(
                "SELECT * FROM cognitive_triggers WHERE id = ? AND status = 'pending'",
                (trigger_id,),
            ).fetchone()
            if trigger is None:
                raise KeyError(trigger_id)
            db.execute(
                "INSERT INTO proactive_messages VALUES (?, ?, ?, ?, 'pending', ?)",
                (message_id, trigger["thread_id"], body, reason, now),
            )
            db.execute(
                "UPDATE cognitive_triggers SET status = 'fired', resolved_at = ? WHERE id = ?",
                (now, trigger_id),
            )
            self._event(db, trigger["thread_id"], "trigger_fired", {
                "trigger_id": trigger_id, "message_id": message_id, "reason": reason
            })
            row = db.execute(
                "SELECT * FROM proactive_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._record(row) or {}
