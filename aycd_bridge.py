#!/usr/bin/env python3
"""AYCD -> Zeno shared-chat bridge.

This module owns the localhost-facing Zeno side of the integration. AYCD can
POST a text request to Zeno, and the request/reply are persisted in the same
messages table used by Web Chat and Discord.
"""
from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from config import LM_LONG_GENERATION_TIMEOUT_SECONDS
from context import adaptive_output_token_limit, build_prompt, sanitize_assistant_response
from database import db_connect, now
from jobs import (
    interactive_request_finished,
    interactive_request_started,
    register_chat_operation,
    schedule_response_maintenance,
    unregister_chat_operation,
)
from model_api import stream_completion_native_progress


def _positive_chat_id(chat_id: int) -> int:
    try:
        value = int(chat_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("AYCD chat_id must be an integer.") from exc
    if value <= 0:
        raise ValueError("AYCD chat_id must be greater than zero.")
    with db_connect() as db:
        if not db.execute("SELECT id FROM chats WHERE id=?", (value,)).fetchone():
            raise ValueError("The AYCD-linked Zeno chat does not exist.")
    return value


def _append(chat_id: int, role: str, content: str, *, source_label: str, external_id: str) -> int:
    stamp = now()
    with db_connect() as db:
        cursor = db.execute(
            "INSERT INTO messages(role,content,created_at,chat_id,attachments_json,citations_json,source,source_label,external_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                str(role),
                str(content).strip(),
                stamp,
                int(chat_id),
                "[]",
                "[]",
                "aycd",
                str(source_label)[:80],
                str(external_id)[:160],
            ),
        )
        message_id = int(cursor.lastrowid)
        db.execute("UPDATE chats SET updated_at=? WHERE id=?", (stamp, int(chat_id)))
    return message_id


def _stream_to_text(iterator: Any, stop_event: threading.Event) -> str:
    chunks: list[str] = []
    try:
        for chunk in iterator:
            if stop_event.is_set():
                raise InterruptedError("AYCD/Zeno generation was stopped.")
            chunks.append(str(chunk))
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    return "".join(chunks).strip()


def process_aycd_chat(
    chat_id: int,
    content: str,
    *,
    external_id: str = "",
    source_label: str = "AYCD",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Process one AYCD-originated text request through the standard Zeno model path."""
    chat_id = _positive_chat_id(chat_id)
    content = re.sub(r"\r\n?", "\n", str(content or "")).strip()
    if not content:
        raise ValueError("AYCD message cannot be empty.")
    if len(content) > 20_000:
        raise ValueError("AYCD messages are limited to 20,000 characters.")

    clean_external_id = re.sub(r"[\r\n]+", " ", str(external_id or "")).strip()[:150]
    if not clean_external_id:
        clean_external_id = "local-" + uuid.uuid4().hex
    event_id = "aycd:" + clean_external_id

    with db_connect() as db:
        prior = db.execute(
            "SELECT id,content FROM messages WHERE chat_id=? AND source='aycd' AND external_id=? "
            "AND role='assistant' ORDER BY id DESC LIMIT 1",
            (chat_id, event_id),
        ).fetchone()
    if prior:
        return {
            "ok": True,
            "duplicate": True,
            "chat_id": chat_id,
            "external_id": clean_external_id,
            "assistant_message_id": int(prior["id"]),
            "reply": str(prior["content"] or ""),
        }

    active_stop = stop_event or threading.Event()
    register_chat_operation(chat_id, active_stop)
    interactive_request_started()
    try:
        user_message_id = _append(
            chat_id, "user", content,
            source_label=str(source_label or "AYCD"),
            external_id=event_id,
        )
        messages, _sources = build_prompt(
            chat_id,
            content,
            [],
            skip_message_id=user_message_id,
            history_before_id=user_message_id,
            chat_only=False,
        )
        _model, method, iterator = stream_completion_native_progress(
            messages,
            active_stop,
            max_tokens=adaptive_output_token_limit(content),
            temperature=0.35,
            user_message=content,
            timeout_seconds=LM_LONG_GENERATION_TIMEOUT_SECONDS,
            request_class="chat",
            progress_callback=None,
            model_mode=None,
        )
        answer = _stream_to_text(iterator, active_stop)
        if active_stop.is_set():
            raise InterruptedError("AYCD/Zeno generation was stopped.")
        answer = sanitize_assistant_response(answer, content)
        if not answer:
            raise RuntimeError("Zeno returned an empty AYCD reply.")

        assistant_message_id = _append(
            chat_id, "assistant", answer,
            source_label="Zeno via AYCD",
            external_id=event_id,
        )
        schedule_response_maintenance(chat_id, content)
        return {
            "ok": True,
            "duplicate": False,
            "chat_id": chat_id,
            "external_id": clean_external_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "reply": answer,
            "transport": str(method or ""),
        }
    finally:
        unregister_chat_operation(chat_id, active_stop)
        interactive_request_finished()


def aycd_message_rows(chat_id: int, external_id: str) -> list[dict[str, Any]]:
    event_id = "aycd:" + re.sub(r"[\r\n]+", " ", str(external_id or "")).strip()[:150]
    with db_connect() as db:
        rows = db.execute(
            "SELECT id,role,content,source,source_label,external_id FROM messages "
            "WHERE chat_id=? AND source='aycd' AND external_id=? ORDER BY id",
            (int(chat_id), event_id),
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = ["process_aycd_chat", "aycd_message_rows"]
