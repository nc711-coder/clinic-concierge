"""Conversation memory in Postgres: one row per session (with the booking state), one per message."""
from __future__ import annotations

import json
import uuid

HISTORY_LIMIT = 20


def parse_id(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def load_or_create(conn, session_id=None) -> tuple[uuid.UUID, dict, list[dict]]:
    """Return (session_id, state, recent history). Unknown or missing ids start a new session."""
    sid = parse_id(session_id)
    if sid:
        row = conn.execute("select state from sessions where id = %s", (sid,)).fetchone()
        if row:
            history = conn.execute(
                """select role, content from (select id, role, content from messages where session_id = %s
                                              order by id desc limit %s) m order by id""",
                (sid, HISTORY_LIMIT)).fetchall()
            return sid, dict(row["state"] or {}), [dict(h) for h in history]
    sid = uuid.uuid4()
    conn.execute("insert into sessions (id) values (%s)", (sid,))
    return sid, {}, []


def save_turn(conn, sid, user_message: str, reply: str, route: str, state: dict, meta: dict | None = None):
    with conn.transaction():
        conn.execute("insert into messages (session_id, role, content, route) values (%s,'user',%s,%s)",
                     (sid, user_message, route))
        conn.execute("insert into messages (session_id, role, content, route, meta) values (%s,'assistant',%s,%s,%s)",
                     (sid, reply, route, json.dumps(meta or {}, default=str)))
        conn.execute("update sessions set state = %s, updated_at = now() where id = %s",
                     (json.dumps(state, default=str), sid))
