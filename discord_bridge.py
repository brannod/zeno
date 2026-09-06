#!/usr/bin/env python3
"""Discord shared-chat bridge for Zeno.

This module owns Discord networking, Discord-side commands, shared-chat mirroring,
reply progress UI, and Discord attachment exchange.  It deliberately does not
start a client at import time.  The main orchestrator starts/stops DISCORD_BRIDGE.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from browser import LIVE_BROWSER, browser_status, playwright_available
from browser_agent import (
    browser_agent_latest, browser_agent_row, resume_browser_agent,
    start_browser_agent, stop_browser_agent,
)
from config import (
    APP_VERSION,
    BASE_DIR,
    DEFAULT_PERSONALITY,
    DISCORD_CONFIG_PATH,
    DISCORD_INFO_PATH,
    LM_LONG_GENERATION_TIMEOUT_SECONDS,
    MAX_RECENT_MESSAGES,
    MAX_UPLOAD_BYTES,
)
from context import (
    NO_CODE_REQUEST_RE,
    ZENO_FILE_BLOCK_RE,
    build_prompt,
    conversation_response_directives,
    estimate_context_usage,
    reset_chat_context,
)
from database import db_connect, now
from deepsearch import (
    deepsearch_goal_with_chat_context,
    deepsearch_state,
    natural_deepsearch_request,
    start_deepsearch,
)
from files import (
    search_uploaded_files,
    brand_proxy_scramble,
    clear_all_uploaded_files,
    compare_uploaded_lists,
    compare_text_lists,
    create_generated_file,
    extract_generated_file_blocks,
    extracted_email_lines,
    EMAIL_ADDRESS_RE,
    read_generated_file,
    stable_unique_lines,
    store_generated_file,
    store_uploaded_file,
    store_uploaded_file_record,
    uploaded_file_inventory,
    validate_file_transform,
)
from jobs import (
    interactive_request_finished,
    interactive_request_started,
    register_chat_operation,
    schedule_response_maintenance,
    stop_discord_chat_work,
    unregister_chat_operation,
)
from memory import manual_context_to_memory, memory_stats, optimize_memories
from model_api import lm_models, model_api_status, stream_completion, stream_completion_native_progress
from mcp_manager import maybe_mcp_context, mcp_context_message
from aycd_commands import (
    handle_aycd_command, aycd_reaction_action, aycd_job, aycd_job_reactions,
    aycd_dashboard_reaction,
)
from settings import bool_setting, get_setting, int_setting, set_setting
from reminders import (
    cancel_reminder, create_reminder, create_reminder_from_text, due_reminders,
    format_reminder_time, list_reminders, mark_reminder_delivered,
)
from task_router import route_task


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISCORD_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "token": "",
    "guild_id": "",
    "channel_id": "",
    "chat_id": 0,
}
DISCORD_INFO_PLACEHOLDERS = {
    "",
    "DISCORD_BOT_TOKEN_HERE",
    "DISCORD_SERVER_ID_HERE",
    "DISCORD_CHANNEL_ID_HERE",
}

_PROCESS_STARTED_MONOTONIC = time.monotonic()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def discord_info_file_values(chat_id: int | None = None) -> dict[str, Any] | None:
    """Parse DISCORD_TOKEN.txt without ever returning a placeholder as a token."""
    if not DISCORD_INFO_PATH.exists():
        return None
    try:
        raw = DISCORD_INFO_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Could not read {DISCORD_INFO_PATH.name}: {exc}") from exc

    parsed: dict[str, str] = {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip().upper()] = value.strip()

    token = parsed.get("TOKEN", "")
    guild_id = parsed.get("SERVER_ID", parsed.get("GUILD_ID", ""))
    channel_id = parsed.get("CHANNEL_ID", "")
    configured = any(value not in DISCORD_INFO_PLACEHOLDERS for value in (token, channel_id))
    if not configured:
        return None

    raw_chat_id = parsed.get("CHAT_ID", "CURRENT").strip().upper()
    resolved_chat_id = chat_id
    if raw_chat_id in {"", "CURRENT"}:
        if resolved_chat_id is None:
            try:
                resolved_chat_id = int(get_setting("active_chat_id", "0") or 0)
            except (TypeError, ValueError):
                resolved_chat_id = 0
            if not resolved_chat_id:
                with db_connect() as db:
                    row = db.execute(
                        "SELECT id FROM chats ORDER BY updated_at DESC,id DESC LIMIT 1"
                    ).fetchone()
                resolved_chat_id = int(row["id"]) if row else 0
    elif raw_chat_id.isdigit():
        resolved_chat_id = int(raw_chat_id)
    else:
        raise ValueError("CHAT_ID in DISCORD_TOKEN.txt must be CURRENT or a numeric Zeno chat ID.")

    return {
        "enabled": parsed.get("ENABLED", "false").casefold() in {"1", "true", "yes", "on"},
        "token": "" if token in DISCORD_INFO_PLACEHOLDERS else token,
        "guild_id": "" if guild_id in DISCORD_INFO_PLACEHOLDERS else guild_id,
        "channel_id": "" if channel_id in DISCORD_INFO_PLACEHOLDERS else channel_id,
        "chat_id": int(resolved_chat_id or 0),
    }


def load_discord_info_file(
    chat_id: int | None = None,
    required: bool = False,
) -> dict[str, Any] | None:
    values = discord_info_file_values(chat_id)
    if values is None:
        if required:
            raise ValueError("Fill in DISCORD_TOKEN.txt, save it, then reload the bridge.")
        return None
    return save_discord_bridge_config(values)


def discord_bridge_config() -> dict[str, Any]:
    config = dict(DISCORD_DEFAULT_CONFIG)
    try:
        loaded = json.loads(DISCORD_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config.update({key: loaded.get(key, config[key]) for key in config})
    except (OSError, json.JSONDecodeError):
        pass

    config["enabled"] = bool(config.get("enabled"))
    config["token"] = str(config.get("token") or "").strip()
    for key in ("guild_id", "channel_id"):
        config[key] = str(config.get(key) or "").strip()
    try:
        config["chat_id"] = int(config.get("chat_id") or 0)
    except (TypeError, ValueError):
        config["chat_id"] = 0
    return config


def save_discord_bridge_config(values: dict[str, Any]) -> dict[str, Any]:
    existing = discord_bridge_config()
    token = str(values.get("token") or "").strip() or str(existing.get("token") or "")
    if token and (len(token) < 30 or len(token) > 220 or any(ch.isspace() for ch in token)):
        raise ValueError("The Discord bot token format is invalid.")

    try:
        chat_id = int(values.get("chat_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a valid numeric Zeno chat ID.") from exc

    config = {
        "enabled": bool(values.get("enabled")),
        "token": token,
        "guild_id": str(values.get("guild_id") or "").strip(),
        "channel_id": str(values.get("channel_id") or "").strip(),
        "chat_id": chat_id,
    }
    for key, label in (("guild_id", "server"), ("channel_id", "channel")):
        if config[key] and (not config[key].isdigit() or len(config[key]) > 24):
            raise ValueError(f"Enter a valid Discord {label} ID.")

    if config["enabled"]:
        if not config["token"]:
            raise ValueError("Discord is enabled but the bot token is empty.")
        if not config["channel_id"]:
            raise ValueError("Discord is enabled but the Channel ID is empty.")
        if config["chat_id"] <= 0:
            raise ValueError("Discord is enabled but no Zeno chat is linked.")
        with db_connect() as db:
            if not db.execute("SELECT id FROM chats WHERE id=?", (config["chat_id"],)).fetchone():
                raise ValueError("The linked Zeno chat no longer exists.")

    _atomic_write_json(DISCORD_CONFIG_PATH, config)
    return dict(config)


def discord_public_config() -> dict[str, Any]:
    config = discord_bridge_config()
    return {
        "enabled": bool(config["enabled"]),
        "configured": bool(config["token"] and config["channel_id"] and config["chat_id"]),
        "has_token": bool(config["token"]),
        "guild_id": config["guild_id"],
        "channel_id": config["channel_id"],
        "chat_id": int(config["chat_id"]),
    }


# ---------------------------------------------------------------------------
# Shared message persistence
# ---------------------------------------------------------------------------


def append_chat_message(
    chat_id: int,
    role: str,
    content: str,
    *,
    source: str = "web_chat",
    source_label: str = "",
    attachments: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
    external_id: str = "",
) -> int:
    timestamp = now()
    with db_connect() as db:
        cursor = db.execute(
            "INSERT INTO messages(role,content,created_at,chat_id,attachments_json,citations_json,source,source_label,external_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                str(role),
                str(content).strip(),
                timestamp,
                int(chat_id),
                json.dumps(attachments or []),
                json.dumps(citations or []),
                str(source)[:40],
                str(source_label)[:80],
                str(external_id)[:160],
            ),
        )
        message_id = int(cursor.lastrowid)
        db.execute("UPDATE chats SET updated_at=? WHERE id=?", (timestamp, int(chat_id)))
    return message_id


def discord_web_updates(chat_id: int, after_id: int) -> list[dict[str, Any]]:
    with db_connect() as db:
        rows = db.execute(
            "SELECT id,role,content,source,source_label,attachments_json FROM messages "
            "WHERE chat_id=? AND id>? ORDER BY id LIMIT 100",
            (int(chat_id), max(0, int(after_id))),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Reply sanitization
# ---------------------------------------------------------------------------


def _normalized_repeat_key(text: str) -> str:
    value = re.sub(r"&#x0*20;|&#32;|&nbsp;", " ", str(text), flags=re.I)
    value = re.sub(r"[`*_>#\-]+", " ", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


def _collapse_repeated_paragraphs(text: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", str(text or "")) if part.strip()]
    kept: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        key = _normalized_repeat_key(paragraph)
        if key and len(key) >= 60 and key in seen:
            continue
        if key and len(key) >= 60:
            seen.add(key)
        kept.append(paragraph)
    return "\n\n".join(kept).strip()


def _looks_like_model_loop(text: str) -> bool:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()]
    if len(paragraphs) < 3:
        return False
    seen: set[str] = set()
    repeats = 0
    for paragraph in paragraphs:
        key = _normalized_repeat_key(paragraph)
        if len(key) < 35:
            continue
        if key in seen:
            repeats += 1
        else:
            seen.add(key)
    return repeats >= 1


def sanitize_discord_answer(answer: str, user_message: str, no_code: bool = False) -> str:
    value = str(answer or "").strip()
    had_file_block = bool(ZENO_FILE_BLOCK_RE.search(value))
    value = ZENO_FILE_BLOCK_RE.sub("", value)
    asks_about_files = bool(re.search(
        r"(?i)\b(file|attachment|download|upload|csv|txt|json|xlsx|spreadsheet)\b",
        str(user_message or ""),
    ))
    if not asks_about_files:
        value = re.sub(
            r"(?im)^.*(?:File creation is unavailable through the Discord chat-only bridge|"
            r"Downloadable-file capability|downloadable file will be generated once you confirm).*?$",
            "",
            value,
        )
    value = _collapse_repeated_paragraphs(value)

    asks_for_options = bool(re.search(
        r"(?i)\b(list|show|give|what are|help|examples?|options?)\b.{0,40}\b(commands?|prompts?|examples?|options?)\b",
        str(user_message or ""),
    ))
    if not asks_for_options:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", value) if p.strip()]
        trimmed: list[str] = []
        for paragraph in paragraphs:
            key = _normalized_repeat_key(paragraph)
            if trimmed and (
                "want me to" in key
                or "or if you want" in key
                or key.startswith("zeno tip")
                or key.startswith("tip ")
            ):
                break
            trimmed.append(paragraph)
        value = "\n\n".join(trimmed).strip()

    if _looks_like_model_loop(value):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", value) if p.strip()]
        kept: list[str] = []
        seen: set[str] = set()
        for paragraph in paragraphs:
            key = _normalized_repeat_key(paragraph)
            if len(key) >= 35 and key in seen:
                break
            if any(marker in key for marker in ("or if you want me to", "clean this entire message")) and kept:
                break
            if len(key) >= 35:
                seen.add(key)
            kept.append(paragraph)
        value = "\n\n".join(kept).strip()

    if no_code or NO_CODE_REQUEST_RE.search(str(user_message or "")):
        value = re.sub(r"```[A-Za-z0-9_+.#-]*\n.*?```", "", value, flags=re.S)
        value = re.sub(r"```.*?```", "", value, flags=re.S)
    value = re.sub(r"(?im)^\s*Downloadable-file capability:\s*$", "", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if had_file_block and asks_about_files and not value:
        value = "The requested file operation completed, but the generated-file payload is not rendered inline in Discord."
    return value


# ---------------------------------------------------------------------------
# Native reply progress state
# ---------------------------------------------------------------------------

_DISCORD_REPLY_PROGRESS_LOCK = threading.RLock()
_DISCORD_REPLY_PROGRESS: dict[str, dict[str, Any]] = {}


def discord_reply_progress_update(
    key: str,
    phase: str,
    percent: float | None = None,
    detail: str = "",
    output_chars: int = 0,
) -> None:
    key = str(key or "").strip()[:180]
    if not key:
        return
    payload = {
        "phase": str(phase or "working")[:40],
        "percent": None if percent is None else max(0.0, min(100.0, float(percent))),
        "detail": str(detail or "")[:180],
        "output_chars": max(0, int(output_chars or 0)),
        "updated_at": time.monotonic(),
    }
    with _DISCORD_REPLY_PROGRESS_LOCK:
        _DISCORD_REPLY_PROGRESS[key] = payload


def discord_reply_progress_get(key: str) -> dict[str, Any]:
    with _DISCORD_REPLY_PROGRESS_LOCK:
        return dict(_DISCORD_REPLY_PROGRESS.get(str(key or ""), {}))


def discord_reply_progress_clear(key: str) -> None:
    with _DISCORD_REPLY_PROGRESS_LOCK:
        _DISCORD_REPLY_PROGRESS.pop(str(key or ""), None)


def discord_reply_overall_percent(state: dict[str, Any]) -> int:
    """Map real backend stages into a stable user-facing overall percentage.

    Prompt/model stages use their real native progress.  During generation the
    model does not expose a truthful completion fraction, so the last segment is
    an explicitly stage-weighted estimate driven only by actual emitted output
    characters.  It never advances on a timer and never reaches 100 until the
    backend reports completion.
    """
    phase = str(state.get("phase") or "starting")
    native = state.get("percent")
    output_chars = max(0, int(state.get("output_chars") or 0))
    if phase in {"starting", "building_context"}:
        return 2
    if phase == "queued":
        return 5
    if phase == "connecting":
        return 10
    if phase == "loading_model":
        pct = max(0.0, min(100.0, float(native or 0.0)))
        return int(round(10 + pct * 0.12))
    if phase == "processing_prompt":
        pct = max(0.0, min(100.0, float(native or 0.0)))
        return int(round(22 + pct * 0.38))
    if phase in {"reasoning", "generating"}:
        # 64..96, asymptotic and driven by visible generated output only.
        fraction = 1.0 - math.exp(-output_chars / 2400.0) if output_chars else 0.0
        return min(96, max(64, int(round(64 + 32 * fraction))))
    if phase == "complete":
        return 100
    if phase == "error":
        return max(2, min(99, int(state.get("overall_percent") or 2)))
    return 2


def discord_reply_progress_text(state: dict[str, Any], label: str = "Zeno reply") -> tuple[str, str]:
    phase = str(state.get("phase") or "starting")
    overall = discord_reply_overall_percent(state)
    output_chars = max(0, int(state.get("output_chars") or 0))
    if phase == "building_context":
        return f"🧠 **Zeno is building context** · {overall}%", f"Context · {overall}%"
    if phase == "queued":
        return f"⏳ **Zeno reply queued** · {overall}%", f"Queued · {overall}%"
    if phase == "connecting":
        return f"📡 **Zeno is receiving the prompt** · {overall}%", f"Prompt · {overall}%"
    if phase == "loading_model":
        return f"📦 **Zeno is loading the selected model** · {overall}%", f"Loading · {overall}%"
    if phase == "processing_prompt":
        native = state.get("percent")
        native_note = f" · prompt {float(native):.0f}%" if native is not None else ""
        return f"🧠 **Zeno is processing the prompt** · {overall}%{native_note}", f"Prompt · {overall}%"
    if phase in {"reasoning", "generating"}:
        detail = f" · {output_chars:,} chars" if output_chars else ""
        return f"✍️ **Zeno is generating the reply** · ~{overall}%{detail}", f"Generating · ~{overall}%"
    if phase == "complete":
        return "✅ **Zeno reply** · 100%", "Reply · 100%"
    return f"⏳ **{label}** · {overall}%", f"Working · {overall}%"


# ---------------------------------------------------------------------------
# Discord text/file helpers
# ---------------------------------------------------------------------------


def discord_message_chunks(text: str, limit: int = 1900) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    limit = max(500, min(int(limit), 1950))
    chunks: list[str] = []
    while len(value) > limit:
        split_at = max(value.rfind("\n\n", 0, limit), value.rfind("\n", 0, limit), value.rfind(" ", 0, limit))
        if split_at < limit // 2:
            split_at = limit
        chunks.append(value[:split_at].rstrip())
        value = value[split_at:].lstrip()
    if value:
        chunks.append(value)
    return chunks


async def discord_reply_text_or_file(message: Any, text: str, *, filename: str = "zeno-result.txt") -> Any:
    """Send a complete reply, attaching UTF-8 text when Discord's limit is exceeded."""
    value = str(text or "").strip()
    chunks = discord_message_chunks(value)
    if len(value) <= 1900:
        sent = None
        for chunk in chunks:
            sent = await message.reply(
                chunk, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
        return sent
    return await message.reply(
        "📄 The complete result is attached as a text file.",
        file=discord.File(io.BytesIO(value.encode("utf-8")), filename=filename),
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def discord_channel_text_or_file(channel: Any, text: str, *, filename: str = "zeno-response.txt") -> Any:
    """Send a channel response without truncating long model output."""
    value = str(text or "").strip()
    chunks = discord_message_chunks(value)
    if len(value) <= 1900:
        sent = None
        for chunk in chunks:
            sent = await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        return sent
    return await channel.send(
        "📄 Zeno's complete response is attached as a text file.",
        file=discord.File(io.BytesIO(value.encode("utf-8")), filename=filename),
        allowed_mentions=discord.AllowedMentions.none(),
    )


def discord_error_text(exc: Exception, action: str = "request") -> str:
    detail = re.sub(r"\s+", " ", str(exc or "Unknown error")).strip()[:700]
    if isinstance(exc, InterruptedError):
        return f"⏹️ Zeno stopped the {action}."
    return f"⚠️ Zeno could not finish the {action}: {detail}"


def discord_decode_text_payload(raw: bytes) -> tuple[str, str, str, bool]:
    if not raw:
        raise ValueError("The attached file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Discord text files are limited to {MAX_UPLOAD_BYTES // 1_000_000} MB in Zeno.")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
        encoding = "utf-16"
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("That attachment is not UTF-8/UTF-16 text. Convert it to TXT/CSV text first.") from exc
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, encoding, newline, text.endswith(("\n", "\r"))


def discord_transform_payload(raw: bytes, filename: str, mode: str, options: dict[str, str] | None = None) -> tuple[bytes, str, dict[str, int]]:
    text, encoding, newline, trailing_newline = discord_decode_text_payload(raw)
    input_lines = text.splitlines()
    options = dict(options or {})
    converted = 0
    invalid_rows = 0
    missing_type_rows = 0
    if mode == "brand_proxy_scramble":
        if len(input_lines) < 2:
            raise ValueError("!scramble needs at least two complete lines.")
        output_lines = brand_proxy_scramble(input_lines)
        label = "scrambled"
    elif mode == "dedupe_lines":
        output_lines = stable_unique_lines(input_lines)
        label = "deduplicated"
    elif mode == "sort_lines":
        output_lines = sorted(input_lines, key=lambda value: (value.casefold(), value))
        label = "sorted"
    elif mode == "remove_blank_lines":
        output_lines = [line for line in input_lines if line.strip()]
        label = "without_blanks"
    elif mode == "extract_emails":
        output_lines = extracted_email_lines(input_lines)
        label = "emails"
    elif mode == "duplicate_emails":
        occurrences: list[tuple[str, str]] = []
        counts: dict[str, int] = {}
        first_spelling: dict[str, str] = {}
        for line in input_lines:
            for found in EMAIL_ADDRESS_RE.findall(line):
                key = found.casefold()
                counts[key] = counts.get(key, 0) + 1
                first_spelling.setdefault(key, found)
        output_lines = [first_spelling[key] for key, count in counts.items() if count > 1]
        label = "duplicate_emails"
    elif mode == "email_colon":
        output_lines = []
        for line in input_lines:
            # email:password -> email:::password. Preserve the exact email and
            # everything after the first colon; already-triple-colon rows stay unchanged.
            if ":::" in line:
                output_lines.append(line)
                continue
            if ":" not in line:
                output_lines.append(line)
                continue
            left, right = line.split(":", 1)
            if EMAIL_ADDRESS_RE.fullmatch(left.strip()):
                prefix_ws = left[:len(left) - len(left.lstrip())]
                suffix_ws = left[len(left.rstrip()):]
                email_value = left.strip()
                output_lines.append(prefix_ws + email_value + suffix_ws + ":::" + right)
                converted += 1
            else:
                output_lines.append(line)
        label = "emailcolon"
        if len(output_lines) != len(input_lines):
            raise RuntimeError("!emailcolon validation failed: line count changed.")
        for before, after in zip(input_lines, output_lines):
            if before == after:
                continue
            left_before, right_before = before.split(":", 1)
            left_after, right_after = after.split(":::", 1)
            if left_before.strip() != left_after.strip() or right_before != right_after:
                raise RuntimeError("!emailcolon validation failed: a credential value changed.")
    elif mode in {"card_colon_4", "card_colon_5"}:
        output_lines = []
        default_type = str(options.get("default_type") or "").strip()

        def clean_cell(value: str) -> str:
            return str(value or "").strip().strip("|\"'`")

        def normalized_parts(line: str) -> list[str]:
            raw_line = str(line or "").strip()
            if not raw_line or re.fullmatch(r"[-|+:,;\s]+", raw_line):
                return []
            if "|" in raw_line:
                parts = [clean_cell(x) for x in raw_line.strip("|").split("|")]
            elif "\t" in raw_line:
                parts = [clean_cell(x) for x in raw_line.split("\t")]
            elif "," in raw_line:
                parts = [clean_cell(x) for x in raw_line.split(",")]
            elif ":" in raw_line:
                parts = [clean_cell(x) for x in raw_line.split(":")]
            else:
                parts = [clean_cell(x) for x in re.split(r"\s+", raw_line)]
            return [x for x in parts if x != ""]

        def is_number(value: str) -> bool:
            return bool(re.fullmatch(r"\d{12,19}", value or ""))

        def is_month(value: str) -> bool:
            if not re.fullmatch(r"\d{1,2}", value or ""):
                return False
            return 1 <= int(value) <= 12

        def is_year(value: str) -> bool:
            return bool(re.fullmatch(r"(?:\d{2}|\d{4})", value or ""))

        def is_cvv(value: str) -> bool:
            return bool(re.fullmatch(r"\d{3,4}", value or ""))

        def parse_card_row(parts: list[str]) -> tuple[str, str, str, str, str] | None:
            if len(parts) >= 5 and is_number(parts[1]) and is_month(parts[2]) and is_year(parts[3]) and is_cvv(parts[4]):
                return parts[0], parts[1], parts[2].zfill(2), parts[3], parts[4]
            if len(parts) >= 4 and is_number(parts[0]) and is_month(parts[1]) and is_year(parts[2]) and is_cvv(parts[3]):
                return "", parts[0], parts[1].zfill(2), parts[2], parts[3]
            for index in range(0, min(max(len(parts) - 3, 0), 4)):
                if index + 3 >= len(parts):
                    break
                if is_number(parts[index]) and is_month(parts[index + 1]) and is_year(parts[index + 2]) and is_cvv(parts[index + 3]):
                    maybe_type = parts[index - 1] if index > 0 and not parts[index - 1].isdigit() else ""
                    return maybe_type, parts[index], parts[index + 1].zfill(2), parts[index + 2], parts[index + 3]
            return None

        for line in input_lines:
            parts = normalized_parts(line)
            if not parts:
                continue
            row = parse_card_row(parts)
            if row is None:
                invalid_rows += 1
                continue
            card_type, number, month, year, cvv = row
            if mode == "card_colon_5":
                card_type = clean_cell(card_type or default_type)
                if not card_type:
                    missing_type_rows += 1
                    continue
                output_lines.append(":".join((card_type, number, month, year, cvv)))
            else:
                output_lines.append(":".join((number, month, year, cvv)))
        if not output_lines:
            if mode == "card_colon_5" and missing_type_rows:
                raise ValueError("No rows were formatted because card type is missing. Use `!cardcolon5 TYPE` or include the type in each row.")
            raise ValueError("No valid card-info rows were found in that attachment.")
        label = "aycd_card_info_5field" if mode == "card_colon_5" else "aycd_card_info_4field"
    else:
        raise ValueError("Unsupported Discord file command.")

    if mode not in {"email_colon", "duplicate_emails", "card_colon_4", "card_colon_5"}:
        validation = validate_file_transform(input_lines, output_lines, mode, "Discord local command", {})
        if not validation.get("passed"):
            raise RuntimeError(
                "Zeno stopped the Discord file command because validation failed: "
                + "; ".join(validation.get("reasons") or [])
            )

    result = newline.join(output_lines) + (newline if trailing_newline else "")
    output_raw = result.encode(encoding)
    source = Path(re.sub(r"[^A-Za-z0-9._ -]", "_", filename or "discord-list.txt")).name
    source_path = Path(source)
    suffix = source_path.suffix or ".txt"
    output_name = f"{source_path.stem}_{label}{suffix}"
    stats = {
        "input_lines": len(input_lines),
        "output_lines": len(output_lines),
        "removed_duplicates": max(0, len(input_lines) - len(output_lines)) if mode == "dedupe_lines" else 0,
        "removed_blank_lines": max(0, len(input_lines) - len(output_lines)) if mode == "remove_blank_lines" else 0,
        "extracted_emails": len(output_lines) if mode == "extract_emails" else 0,
        "duplicate_emails": len(output_lines) if mode == "duplicate_emails" else 0,
        "converted_lines": converted if mode == "email_colon" else 0,
        "formatted_cards": len(output_lines) if mode in {"card_colon_4", "card_colon_5"} else 0,
        "invalid_card_rows": invalid_rows if mode in {"card_colon_4", "card_colon_5"} else 0,
        "missing_card_type_rows": missing_type_rows if mode == "card_colon_5" else 0,
    }
    return output_raw, output_name, stats


def discord_direct_file_transform(
    raw: bytes,
    filename: str,
    instruction: str,
) -> tuple[bytes, str, str] | None:
    request = re.sub(r"\s+", " ", str(instruction or "")).strip()
    lowered = request.casefold()
    wants_scramble = bool(re.search(r"\b(scramble|shuffle|randomi[sz]e)\b", lowered))
    wants_dedupe = bool(re.search(r"\b(remove|delete|drop)\b.{0,25}\b(exact )?duplicates?\b|\bdedupe\b", lowered))
    wants_sort = bool(re.search(r"\bsort\b.{0,20}\b(a.?z|alphabetical|ascending)\b|\bsort lines?\b", lowered))
    wants_remove_blank = bool(re.search(r"\b(remove|delete|drop)\b.{0,25}\b(blank|empty)\b.{0,15}\blines?\b", lowered))
    wants_extract_emails = bool(re.search(r"\bextract\b.{0,25}\bemails?\b|\bget\b.{0,20}\bemails?\b", lowered))
    wants_emailcolon = bool(
        "emailcolon" in lowered
        or "email:::password" in lowered
        or re.search(r"email\s*:\s*password.{0,30}email\s*:::\s*password", lowered)
        or re.search(r"convert.{0,40}\bcolon\b.{0,40}triple", lowered)
    )
    prepend_match = re.search(
        r"(?i)\bprepend\s+(?:the\s+text\s+)?(?:\"([^\"]+)\"|'([^']+)'|`([^`]+)`|([^,;]{1,80}?))\s+(?:to\s+)?(?:each|every)\s+line\b",
        request,
    )
    if not any((wants_scramble, wants_dedupe, wants_sort, wants_remove_blank, wants_extract_emails, wants_emailcolon, prepend_match)):
        return None

    text, encoding, newline, trailing_newline = discord_decode_text_payload(raw)
    input_lines = text.splitlines()
    output_lines = list(input_lines)
    actions: list[str] = []

    if prepend_match:
        prefix = next((part for part in prepend_match.groups() if part is not None), "").strip().strip('"\'`')
        if not prefix:
            raise ValueError("The text to prepend to each line is empty.")
        output_lines = [prefix + line for line in output_lines]
        actions.append(f"prepended {prefix!r} to each line")
    if wants_remove_blank:
        output_lines = [line for line in output_lines if line.strip()]
        actions.append("removed blank lines")
    if wants_dedupe:
        output_lines = stable_unique_lines(output_lines)
        actions.append("removed exact duplicates")
    if wants_sort:
        output_lines = sorted(output_lines, key=lambda value: (value.casefold(), value))
        actions.append("sorted lines A-Z")
    if wants_extract_emails:
        output_lines = extracted_email_lines(output_lines)
        actions.append("extracted email addresses")
    if wants_emailcolon:
        converted_lines: list[str] = []
        changed = 0
        for line in output_lines:
            if ":::" in line or ":" not in line:
                converted_lines.append(line)
                continue
            left, right = line.split(":", 1)
            if EMAIL_ADDRESS_RE.fullmatch(left.strip()):
                converted_lines.append(left + ":::" + right)
                changed += 1
            else:
                converted_lines.append(line)
        output_lines = converted_lines
        actions.append(f"converted {changed:,} email:password row(s) to email:::password")
    if wants_scramble:
        if len(output_lines) < 2:
            raise ValueError("Scrambling needs at least two complete lines.")
        before = sorted(output_lines)
        output_lines = brand_proxy_scramble(output_lines)
        if sorted(output_lines) != before:
            raise RuntimeError("Local scramble validation failed; Zeno refused to deliver a changed record set.")
        actions.append("scrambled complete lines")

    result = newline.join(output_lines) + (newline if trailing_newline else "")
    output_raw = result.encode(encoding)
    source = Path(re.sub(r"[^A-Za-z0-9._ -]", "_", filename or "discord-file.txt")).name
    source_path = Path(source)
    suffix = source_path.suffix or ".txt"
    output_name = f"{source_path.stem}_modified{suffix}"
    summary = (
        f"Finished locally: **{len(input_lines):,} → {len(output_lines):,}** line(s); "
        + ", ".join(actions)
        + "."
    )
    return output_raw, output_name, summary


def _stream_to_text(iterator: Any, stop_event: threading.Event) -> str:
    chunks: list[str] = []
    try:
        for chunk in iterator:
            if stop_event.is_set():
                raise InterruptedError("Generation was stopped.")
            chunks.append(str(chunk))
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    return "".join(chunks).strip()


def process_discord_chat(
    chat_id: int,
    content: str,
    author_id: str,
    author_name: str,
    external_id: str,
    stop_event: threading.Event | None = None,
    reply_context: str = "",
    *,
    file_ids: list[int] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    content = str(content or "").strip()
    author_id = str(author_id or "")[:40]
    author_name = re.sub(r"[\r\n]+", " ", str(author_name or "Discord user")).strip()[:80] or "Discord user"
    external_id = str(external_id or "")[:160]
    if not content or len(content) > 8_000:
        raise ValueError("Discord messages must contain 1 to 8,000 characters of text.")
    if not external_id:
        raise ValueError("Discord message ID is missing.")

    with db_connect() as db:
        if not db.execute("SELECT id FROM chats WHERE id=?", (int(chat_id),)).fetchone():
            raise ValueError("The linked Zeno chat no longer exists.")
        previous = db.execute(
            "SELECT status,response,updated_at,user_message_id FROM discord_events WHERE external_id=?",
            (external_id,),
        ).fetchone()
        if previous and str(previous["status"]) == "completed":
            return str(previous["response"])
        if previous and str(previous["status"]) == "processing" and now() - int(previous["updated_at"]) < 600:
            raise RuntimeError("That Discord message is already being processed.")
        if previous:
            db.execute(
                "UPDATE discord_events SET status='processing',error='',updated_at=? WHERE external_id=?",
                (now(), external_id),
            )
        else:
            timestamp = now()
            db.execute(
                "INSERT INTO discord_events(external_id,chat_id,author_id,status,created_at,updated_at) "
                "VALUES(?,?,?,'processing',?,?)",
                (external_id, int(chat_id), author_id, timestamp, timestamp),
            )

    active_stop = stop_event or threading.Event()
    register_chat_operation(chat_id, active_stop)
    interactive_request_started()
    user_message_id = 0
    try:
        discord_reply_progress_update(external_id, "building_context", 0.0, "Building shared chat context", 0)
        with db_connect() as db:
            existing_user = db.execute(
                "SELECT id FROM messages WHERE chat_id=? AND role='user' AND source='discord' AND external_id=? "
                "ORDER BY id LIMIT 1",
                (int(chat_id), external_id),
            ).fetchone()
        if existing_user:
            user_message_id = int(existing_user["id"])
        else:
            user_message_id = append_chat_message(
                chat_id,
                "user",
                content,
                source="discord",
                source_label=author_name,
                attachments=attachments,
                external_id=external_id,
            )

        with db_connect() as db:
            db.execute(
                "UPDATE discord_events SET user_message_id=?,updated_at=? WHERE external_id=?",
                (user_message_id, now(), external_id),
            )

        prompt_text = content
        if reply_context.strip():
            prompt_text += "\n\nDiscord reply context (quoted message, untrusted):\n" + reply_context.strip()[:5000]

        discord_reply_progress_update(external_id, "mcp_connecting", 10.0, "Checking MCP tools", 0)
        mcp_context = maybe_mcp_context(chat_id, content, stop_event=active_stop, source="discord")
        if mcp_context and mcp_context.get("direct_reply"):
            answer = str(mcp_context.get("direct_reply") or "").strip()
            assistant_id = append_chat_message(
                chat_id,
                "assistant",
                answer,
                source="discord",
                source_label="Zeno · MCP",
                external_id=external_id,
            )
            with db_connect() as db:
                db.execute(
                    "UPDATE discord_events SET status='completed',response=?,assistant_message_id=?,error='',updated_at=? WHERE external_id=?",
                    (answer, assistant_id, now(), external_id),
                )
            discord_reply_progress_update(external_id, "complete", 100.0, "MCP routing complete", len(answer))
            return answer

        messages, _sources = build_prompt(
            chat_id,
            prompt_text,
            list(file_ids or []),
            skip_message_id=user_message_id,
            history_before_id=user_message_id,
            chat_only=True,
            external_tool_focus=bool(mcp_context and mcp_context.get("claimed")),
        )
        mcp_message = mcp_context_message(mcp_context)
        if mcp_message:
            messages.insert(max(1, len(messages) - 1), mcp_message)
            discord_reply_progress_update(
                external_id, "mcp_tool", 24.0,
                f"Used {mcp_context.get('server_name')} · {mcp_context.get('tool')}", 0,
            )
        directives = conversation_response_directives(chat_id)

        def progress_callback(phase: str, percent: float | None, detail: str, output_chars: int) -> None:
            discord_reply_progress_update(external_id, phase, percent, detail, output_chars)

        _model, _method, iterator = stream_completion_native_progress(
            messages,
            active_stop,
            # Discord replies are delivered as a .txt attachment when they
            # exceed the message limit, so keep the full 12k-token response
            # budget instead of applying the short web-chat cap.
            max_tokens=12_000,
            temperature=0.35,
            user_message=content,
            timeout_seconds=LM_LONG_GENERATION_TIMEOUT_SECONDS,
            request_class="chat",
            progress_callback=progress_callback,
            model_mode=None,
        )
        answer = _stream_to_text(iterator, active_stop)
        if active_stop.is_set():
            raise InterruptedError("Discord generation was stopped.")
        answer = sanitize_discord_answer(answer, content, no_code=bool(directives.get("no_code")))
        if not answer:
            raise RuntimeError("Zeno returned an empty Discord reply.")

        assistant_id = append_chat_message(
            chat_id,
            "assistant",
            answer,
            source="discord",
            source_label=(f"Zeno · MCP · {mcp_context.get('server_name')}" if mcp_context and mcp_context.get("used") else "Zeno"),
            external_id=external_id,
        )
        with db_connect() as db:
            db.execute(
                "UPDATE discord_events SET status='completed',response=?,assistant_message_id=?,error='',updated_at=? "
                "WHERE external_id=?",
                (answer, assistant_id, now(), external_id),
            )
        discord_reply_progress_update(external_id, "complete", 100.0, "Reply complete", len(answer))
        schedule_response_maintenance(chat_id, content)
        return answer
    except Exception as exc:
        with db_connect() as db:
            db.execute(
                "UPDATE discord_events SET status='failed',error=?,updated_at=? WHERE external_id=?",
                (f"{type(exc).__name__}: {exc}"[:1000], now(), external_id),
            )
        raise
    finally:
        unregister_chat_operation(chat_id, active_stop)
        interactive_request_finished()


def discord_file_bridge(
    chat_id: int,
    instruction: str,
    raw: bytes,
    filename: str,
    author_name: str,
    external_id: str,
    stop_event: threading.Event | None = None,
) -> tuple[str, dict[str, Any]]:
    request_text = re.sub(r"\s+", " ", str(instruction or "")).strip()
    if len(request_text) < 2:
        raise ValueError("Add a file instruction after `!file`, for example `!file convert to email:::password`.")
    uploaded = store_uploaded_file_record(
        chat_id,
        filename,
        mimetypes.guess_type(filename)[0] or "text/plain",
        raw,
    )
    direct = discord_direct_file_transform(raw, filename, request_text)
    if direct is not None:
        output_raw, output_name, response_text = direct
        attachment = store_generated_file(
            chat_id,
            output_name,
            output_raw,
            source_file_id=int(uploaded["id"]),
            source_job_id="discord-file-local",
        )
        append_chat_message(
            chat_id,
            "user",
            f"[Discord file | {author_name}] {request_text}",
            source="discord",
            source_label=author_name,
            external_id=external_id,
        )
        assistant_id = append_chat_message(
            chat_id,
            "assistant",
            response_text,
            source="discord",
            source_label="Zeno",
            attachments=[attachment],
            external_id=external_id,
        )
        with db_connect() as db:
            db.execute(
                "UPDATE generated_files SET source_message_id=? WHERE id=?",
                (assistant_id, int(attachment["id"])),
            )
        return response_text, attachment

    file_text = str(uploaded.get("text") or "")
    if not file_text.strip():
        raise ValueError("Zeno could not extract readable text from that attachment.")

    model_stop = stop_event or threading.Event()
    register_chat_operation(chat_id, model_stop)
    interactive_request_started()
    try:
        system = get_setting("personality", DEFAULT_PERSONALITY).strip() + "\n\n" + (
            "Discord file-bridge rules:\n"
            "- You are transforming one uploaded file for a Discord command.\n"
            "- Follow the user's instruction exactly and preserve every unspecified value.\n"
            "- Return exactly one complete zeno-file block using the same extension when possible.\n"
            "- Put a short explanation before the zeno-file block.\n"
            "- Never truncate, omit, summarize, or replace sections with ellipses.\n"
        )
        user_message = (
            f"Instruction: {request_text}\n"
            f"Source filename: {uploaded['name']}\n"
            "Return the finished downloadable file in a zeno-file block.\n\n"
            f"FILE CONTENTS:\n```text\n{file_text[:120000]}\n```"
        )
        _model, _method, iterator = stream_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user_message}],
            model_stop,
            max_tokens=12000,
            temperature=0.15,
            model_mode=None,
            user_message=request_text,
            timeout_seconds=LM_LONG_GENERATION_TIMEOUT_SECONDS,
            request_class="file",
        )
        answer = _stream_to_text(iterator, model_stop)
    finally:
        unregister_chat_operation(chat_id, model_stop)
        interactive_request_finished()

    visible, generated = extract_generated_file_blocks(answer, str(uploaded["name"]) + " " + request_text)
    if not generated:
        raise RuntimeError("Zeno did not return a downloadable file block for that Discord file request.")
    output_name, output_content = generated[0]
    attachment = create_generated_file(
        chat_id,
        output_name,
        output_content,
        source_file_id=int(uploaded["id"]),
        source_job_id="discord-file-ai",
    )
    if len(generated) > 1:
        visible = (visible + "\n\nAdditional generated files were kept in the linked Zeno browser chat.").strip()
    if not visible:
        visible = "Finished the requested file transformation."
    append_chat_message(
        chat_id,
        "user",
        f"[Discord file | {author_name}] {request_text}",
        source="discord",
        source_label=author_name,
        external_id=external_id,
    )
    assistant_id = append_chat_message(
        chat_id,
        "assistant",
        visible,
        source="discord",
        source_label="Zeno",
        attachments=[attachment],
        external_id=external_id,
    )
    with db_connect() as db:
        db.execute(
            "UPDATE generated_files SET source_message_id=? WHERE id=?",
            (assistant_id, int(attachment["id"])),
        )
    schedule_response_maintenance(chat_id, request_text)
    return visible, attachment


# ---------------------------------------------------------------------------
# Discord command/status helpers
# ---------------------------------------------------------------------------


def discord_format_uptime() -> str:
    seconds = max(0, int(time.monotonic() - _PROCESS_STARTED_MONOTONIC))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    pieces = []
    if days:
        pieces.append(f"{days}d")
    if days or hours:
        pieces.append(f"{hours}h")
    pieces.append(f"{minutes}m")
    pieces.append(f"{seconds}s")
    return " ".join(pieces)


def discord_health_text(discord_latency_ms: float | None = None) -> str:
    started = time.perf_counter()
    models: list[str] = []
    model_error = ""
    try:
        models = lm_models()
    except Exception as exc:
        model_error = str(exc)[:140]
    lm_ms = int((time.perf_counter() - started) * 1000)
    browser = browser_status()
    discord_part = (
        f"Discord: **{int(discord_latency_ms or 0)} ms**"
        if discord_latency_ms is not None
        else "Discord: **connected**"
    )
    lm_part = (
        f"Model backend: **online ({lm_ms} ms)** · {len(models)} model(s)"
        if models
        else f"Model backend: **offline/unreachable**{f' · {model_error}' if model_error else ''}"
    )
    browser_part = "Browser: **open**" if browser.get("ready") else "Browser: **idle**"
    return f"🏓 {discord_part}\n{lm_part}\n{browser_part}\nUptime: **{discord_format_uptime()}**"


def discord_status_text(chat_id: int) -> str:
    with db_connect() as db:
        chat = db.execute("SELECT title FROM chats WHERE id=?", (int(chat_id),)).fetchone()
        file_count = int(db.execute("SELECT COUNT(*) FROM files WHERE chat_id=? AND active=1", (int(chat_id),)).fetchone()[0])
        page_count = int(db.execute("SELECT COUNT(*) FROM pages WHERE chat_id=? AND active=1", (int(chat_id),)).fetchone()[0])
    context = estimate_context_usage(chat_id)
    memories = memory_stats("current conversation")
    deep = deepsearch_state(chat_id)
    model_mode = get_setting("model_mode", "balanced").casefold()
    runtime = model_api_status()
    gate = dict(runtime.get("gate") or {})
    configured_model = get_setting("deep_model" if model_mode == "deep" else "fast_model", get_setting("model", ""))
    model_name = Path(str(configured_model).replace("\\", "/")).name or "not configured"
    model_name = re.sub(r"\.(?:gguf|bin|safetensors)$", "", model_name, flags=re.I)
    lane_state = "generating" if gate.get("busy") else "ready"
    return (
        f"**Zeno status · V{APP_VERSION}**\n"
        f"Linked chat: **{str(chat['title']) if chat else 'missing'}** (#{chat_id})\n"
        f"Mode: **{model_mode.title()}** · model: **{model_name}** · **{lane_state}** · queued: **{int(gate.get('queued') or 0)}**\n"
        f"Context: **{context['estimated_tokens']:,}/{context['window_tokens']:,} tokens** ({context['percent']}%)\n"
        f"Memory: **{int(memories['total']):,}** item(s) · active files: **{file_count}** · active pages: **{page_count}**\n"
        f"DeepSearch active: **{int(deep.get('active_count') or 0)}**\n"
        f"Auto-memory: **{'on' if bool_setting('auto_memory', False) else 'off'}** · Auto-summary: **{'on' if bool_setting('auto_summary', False) else 'off'}**\n"
        f"Completion mentions: **{'on' if bool_setting('discord_completion_mentions', False) else 'off'}**\n"
        "Everyone in this configured channel can chat with the same Zeno conversation."
    )


def discord_profile_text(chat_id: int) -> str:
    personality = get_setting("personality", DEFAULT_PERSONALITY).strip()
    compact_personality = personality[:1200] + ("…" if len(personality) > 1200 else "")
    with db_connect() as db:
        chat = db.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
    return (
        f"**Zeno profile**\n"
        f"Linked chat: **{str(chat['title']) if chat else 'missing'}** (#{chat_id})\n"
        f"Browser tools: **{'enabled' if bool_setting('use_browser', True) else 'disabled'}** · "
        f"Screenshots: **{'enabled' if bool_setting('include_page_screenshot', True) else 'disabled'}**\n"
        f"Recent-context window: **{int_setting('recent_context_messages', MAX_RECENT_MESSAGES, 6, 80)} messages**\n"
        "**Personality snapshot**\n"
        f"```text\n{compact_personality}\n```"
    )


def discord_diagnostics_text(discord_latency_ms: float | None = None) -> str:
    checks: list[str] = []
    try:
        with db_connect() as db:
            count = int(db.execute("SELECT COUNT(*) FROM chats").fetchone()[0])
        checks.append(f"✅ Memory DB reachable · {count:,} chat(s)")
    except Exception as exc:
        checks.append(f"❌ Memory DB · {str(exc)[:180]}")
    try:
        models = lm_models()
        checks.append(f"✅ Model backend · {len(models)} model(s) visible" if models else "⚠️ Model backend reachable but no models listed")
    except Exception as exc:
        checks.append(f"❌ Model backend · {str(exc)[:180]}")
    try:
        state = browser_status()
        checks.append(f"✅ Live Browser · {'open' if state.get('ready') else 'idle'}")
    except Exception as exc:
        checks.append(f"❌ Live Browser · {str(exc)[:180]}")
    checks.append(f"{'✅' if playwright_available() else '⚠️'} Chromium/Playwright · {'available' if playwright_available() else 'not available'}")
    try:
        disk = shutil.disk_usage(BASE_DIR)
        checks.append(f"✅ Disk · {disk.free / (1024**3):.1f} GB free")
    except OSError as exc:
        checks.append(f"⚠️ Disk check · {str(exc)[:180]}")
    if discord_latency_ms is not None:
        checks.append(f"✅ Discord gateway · {int(discord_latency_ms)} ms")
    return "**Zeno Doctor**\n" + "\n".join(checks)


def discord_command_help() -> str:
    return (
        "**Zeno Discord Commands**\n"
        "`!help` — show this complete command list.\n"
        "`!reset` — drop the active topic/context without deleting visible history, files, pages, or long-term memory.\n"
        "`!context` — summarize the shared chat and promote durable facts into the Memory Bank.\n"
        "`!compact` — save durable context to memory, then start a fresh context window without deleting visible history.\n"
        "`!status` — show linked chat, context usage, model mode, active data, and job counts.\n"
        "`!fast` / `!balanced` — use the configured fast model.\n"
        "`!deep` — explicitly use the configured deep model until you switch back.\n"
        "`!profile` — show the linked chat and compact personality/config snapshot.\n"
        "`!diagnostics` — check model backend, memory DB, Discord, disk, and Chromium.\n"
        "`!ping` — check Discord latency, model backend, browser state, and uptime.\n"
        "`!uptime` — show Zeno process uptime and version.\n"
        "`!jobs` — list recent/active DeepSearch and File Worker jobs.\n"
        "`!last` — resend the latest completed file or DeepSearch result.\n"
        "`!retry` — retry the latest failed/stopped background task, or the latest normal Discord question.\n"
        "`!stop` — stop active generation and registered stoppable chat jobs.\n"
        "`!screenshot` — send the current Live Browser screenshot.\n"
        "`!clearfiles` — inspect stored input files; `!clearfiles confirm` deletes all uploaded/input files.\n"
        "`!notify on` / `!notify off` — toggle long-job completion mentions.\n"
        "`!file <instruction>` — transform an attached/replied-to supported file and return the result.\n"
        "`!comparelist` / `!listcompare` — attach two text files and list entries shared by both lists.\n"
        "`!listmissing` — attach two text files and list entries found only in List A or List B.\n"
        "`!scramble` — shuffle complete lines while preserving every record.\n"
        "`!removedupes` — remove exact duplicate lines while preserving first-seen order.\n"
        "`!emailcolon` — convert `email:password` rows to `email:::password` without changing either value.\n"
        "`!cardcolon` — locally format attached/replied card-info rows as `number:month:year:cvv`; result is saved under Zeno Files, not reposted to Discord.\n"
        "`!cardcolon5 TYPE` — locally format as `type:number:month:year:cvv`; TYPE is the explicit default for rows that omit it.\n"
        "`!extractemails` — extract email addresses from an attached/replied text file.\n"
        "`!dupeemails` — list email addresses that occur more than once in the attached/replied file.\n"
        "`!sortlines` — sort complete lines A-Z.\n"
        "`!removeblanks` — remove blank/empty lines.\n"
        "`!aycd` / `!aycd help` — AYCD Profile Builder commands, jobs, tools, recipes, and reaction legend.\n"
        "Normal messages chat with the same Zeno conversation used by the desktop/browser UI."
    )


def discord_jobs_text(chat_id: int, active_only: bool = False) -> str:
    with db_connect() as db:
        deep_rows = db.execute(
            "SELECT id,status,progress,pages_fetched,page_limit,stage,updated_at FROM deepsearch_jobs "
            "WHERE chat_id=? ORDER BY updated_at DESC LIMIT 8",
            (chat_id,),
        ).fetchall()
        file_rows = db.execute(
            "SELECT id,status,progress,processed_lines,input_lines,stage,updated_at FROM file_jobs "
            "WHERE chat_id=? ORDER BY updated_at DESC LIMIT 8",
            (chat_id,),
        ).fetchall()
    active = {"queued", "running", "paused", "stopping", "cancelling", "preview_ready", "interrupted"}
    if active_only:
        deep_rows = [row for row in deep_rows if str(row["status"]) in active]
        file_rows = [row for row in file_rows if str(row["status"]) in active]
    lines = ["**Zeno jobs**"]
    if deep_rows:
        lines.append("**DeepSearch**")
        for row in deep_rows:
            lines.append(
                f"`{str(row['id'])[:8]}` · **{row['status']}** · {int(row['progress'] or 0)}% · "
                f"{int(row['pages_fetched'] or 0):,}/{int(row['page_limit'] or 0):,} pages · {str(row['stage'] or '')[:90]}"
            )
    if file_rows:
        lines.append("**File Worker**")
        for row in file_rows:
            count = f" · {int(row['processed_lines'] or 0):,}/{int(row['input_lines'] or 0):,} lines"
            lines.append(
                f"`{str(row['id'])[:8]}` · **{row['status']}** · {int(row['progress'] or 0)}%{count} · "
                f"{str(row['stage'] or '')[:90]}"
            )
    has_jobs = bool(deep_rows or file_rows)
    if not has_jobs:
        lines.append("No active jobs." if active_only else "No DeepSearch or File Worker jobs yet.")
    if active_only and has_jobs:
        lines.append("React ⏹️ to stop · 🔁 to retry the latest failed task · 📄 for the latest result")
    return "\n".join(lines)


def discord_retry_last_task(chat_id: int) -> str:
    from files import retry_file_job

    with db_connect() as db:
        file_row = db.execute(
            "SELECT id,status,updated_at FROM file_jobs WHERE chat_id=? "
            "AND status IN ('failed','validation_failed','cancelled','interrupted') ORDER BY updated_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        deep_row = db.execute(
            "SELECT id,start_url,goal,page_limit,max_depth,status,updated_at FROM deepsearch_jobs WHERE chat_id=? "
            "AND status IN ('failed','stopped','interrupted') ORDER BY updated_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    candidates: list[tuple[int, str, Any]] = []
    if file_row:
        candidates.append((int(file_row["updated_at"]), "file", file_row))
    if deep_row:
        candidates.append((int(deep_row["updated_at"]), "deep", deep_row))
    if not candidates:
        return "There is no failed/stopped DeepSearch or File Worker task to retry."
    _timestamp, kind, row = max(candidates, key=lambda item: item[0])
    if kind == "file":
        job = retry_file_job(str(row["id"]), chat_id)
        return f"🔁 Retrying File Worker `{str(job['id'])[:8]}` from its last safe step."
    job_id = start_deepsearch(
        chat_id,
        str(row["start_url"]),
        str(row["goal"]),
        int(row["page_limit"]),
        int(row["max_depth"]),
    )
    return f"🔁 Restarted DeepSearch as `{job_id[:8]}` with the same goal and limits."


def discord_last_result(chat_id: int) -> dict[str, Any]:
    with db_connect() as db:
        generated = db.execute(
            "SELECT id,name,mime,size_bytes,created_at,source_job_id FROM generated_files "
            "WHERE chat_id=? AND deleted_at=0 ORDER BY created_at DESC,id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        deep = db.execute(
            "SELECT id,goal,report,pages_fetched,errors,updated_at FROM deepsearch_jobs "
            "WHERE chat_id=? AND status='completed' ORDER BY updated_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    gen_time = int(generated["created_at"]) if generated else -1
    deep_time = int(deep["updated_at"]) if deep else -1
    newest = max(gen_time, deep_time)
    if generated and gen_time == newest:
        row = dict(generated)
        return {
            "kind": "file",
            "text": f"**Latest result** · file `{row['name']}` · {int(row['size_bytes']):,} bytes",
            "name": str(row["name"]),
            "id": int(row["id"]),
        }
    if deep and deep_time == newest:
        report = str(deep["report"] or "").strip()
        return {
            "kind": "text",
            "text": (
                f"**Latest DeepSearch result** · `{str(deep['id'])[:8]}` · "
                f"{int(deep['pages_fetched'] or 0):,} page(s) read · {int(deep['errors'] or 0):,} error(s)\n\n"
                + (report or "The completed DeepSearch has no saved report text.")
            ),
        }
    return {"kind": "text", "text": "There is no completed file or DeepSearch result yet."}



# ZENO_RECENT_UPGRADE_2026_08_28: Discord slash-command helpers

def discord_file_search_text(chat_id: int, query: str) -> str:
    result = search_uploaded_files(chat_id, query, active_only=True, limit_files=1000, limit_matches=30)
    lines = [
        f"🔎 **Zeno file search** · scanned **{int(result['scanned_files']):,}** active file(s) · "
        f"matched **{int(result['matched_files']):,}** file(s)"
    ]
    if result.get("exact_targets"):
        found = set(result.get("found_targets") or [])
        for target in result.get("exact_targets") or []:
            lines.append(f"{'✅' if target in found else '❌'} `{target}`")
    for item in (result.get("matches") or [])[:18]:
        hits = item.get("hits") or []
        preview = str(hits[0].get("snippet") or "Match found") if hits else "Match found"
        line_no = int(hits[0].get("line") or 0) if hits else 0
        location = f" · line {line_no:,}" if line_no else ""
        lines.append(f"• **{item['name']}**{location} · {preview}")
    if not result.get("matches"):
        lines.append("No matching active uploaded files were found.")
    if result.get("truncated"):
        lines.append("More matches exist; refine the query to narrow them down.")
    return "\n".join(lines)[:7900]

# ---------------------------------------------------------------------------
# Bridge implementation
# ---------------------------------------------------------------------------


class DiscordBridge:
    """One restartable discord.py client bridged to one persistent Zeno chat."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._status = "stopped"
        self._detail = "Discord bridge is stopped."
        self._user = ""
        self._updated_at = now()

    def _set_status(self, status: str, detail: str, user: str = "") -> None:
        with self._lock:
            self._status = str(status)[:40]
            self._detail = str(detail)[:500]
            if user:
                self._user = str(user)[:160]
            self._updated_at = now()

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            status = {
                "status": self._status,
                "detail": self._detail,
                "user": self._user,
                "updated_at": self._updated_at,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "connected": bool(self._client and not getattr(self._client, "is_closed", lambda: True)()),
            }
        status["config"] = discord_public_config()
        return status

    def start(self) -> None:
        try:
            load_discord_info_file(required=False)
        except (ValueError, OSError) as exc:
            self._set_status("error", f"Discord bot-info file error: {exc}")
            return
        config = discord_bridge_config()
        if not config["enabled"]:
            self._set_status("disabled", "Discord bridge is disabled in Setup.")
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._set_status("starting", "Connecting Zeno to Discord…")
            self._thread = threading.Thread(
                target=self._run,
                args=(config,),
                daemon=True,
                name="ZenoDiscordBridge",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            loop, client, thread = self._loop, self._client, self._thread
        if loop and client and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout=8)
            except Exception:
                pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self._lock:
            self._loop = None
            self._client = None
            self._thread = None
        self._set_status("stopped", "Discord bridge stopped.")

    def restart_async(self) -> None:
        def restart() -> None:
            self.stop()
            time.sleep(0.2)
            self.start()
        threading.Thread(target=restart, daemon=True, name="ZenoDiscordRestart").start()

    def send_message(self, channel_id: int, message: str) -> bool:
        with self._lock:
            loop, client = self._loop, self._client
        if not loop or not client or not loop.is_running():
            raise RuntimeError("Discord bridge is not connected.")

        async def send() -> bool:
            channel = client.get_channel(int(channel_id))
            if channel is None:
                channel = await client.fetch_channel(int(channel_id))
            for chunk in discord_message_chunks(message):
                await channel.send(chunk)
            return True

        return bool(asyncio.run_coroutine_threadsafe(send(), loop).result(timeout=20))

    def receive_commands(self) -> str:
        """Compatibility/status API for the future HTTP layer."""
        return discord_command_help()

    def _run(self, config: dict[str, Any]) -> None:
        try:
            import discord  # type: ignore
        except Exception as exc:
            self._set_status("error", f"discord.py is unavailable: {exc}")
            return

        bridge = self
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        chat_id = int(config["chat_id"])
        channel_id = int(config["channel_id"])
        configured_guild_id = int(config["guild_id"]) if str(config.get("guild_id") or "").isdigit() else 0

        class ZenoDiscordClient(discord.Client):
            def __init__(self) -> None:
                super().__init__(intents=intents)
                self.tree = discord.app_commands.CommandTree(self)
                self._zeno_slash_registered = False
                self.processing_lock = asyncio.Lock()
                self.sync_started = False
                self.last_message_id = 0
                self.control_messages: dict[int, dict[str, Any]] = {}
                self.last_discord_author_id = 0
                self.activity_override = ""
                self._last_presence_text = ""


            # ZENO_RECENT_UPGRADE_2026_08_28: slash commands, no Discord threads
            def _register_recent_slash_commands(self) -> None:
                if getattr(self, "_zeno_slash_registered", False):
                    return
                self._zeno_slash_registered = True

                async def run_prompt(interaction: Any, prompt: str, label: str) -> None:
                    if int(getattr(interaction.channel, "id", 0) or 0) != channel_id:
                        await interaction.response.send_message("This Zeno bot is linked to a different channel.", ephemeral=True)
                        return
                    author = getattr(interaction, "user", None)
                    author_name = str(getattr(author, "display_name", "") or getattr(author, "name", "") or "Discord user")[:80]
                    external_id = f"slash:{int(getattr(interaction, 'id', 0) or 0)}"
                    await interaction.response.send_message(f"🧠 **{label}** · 2%")
                    original = await interaction.original_response()
                    task = asyncio.create_task(asyncio.to_thread(
                        process_discord_chat,
                        chat_id,
                        prompt,
                        str(getattr(author, "id", "")),
                        author_name,
                        external_id,
                        threading.Event(),
                        "",
                    ))
                    last = ""
                    last_edit = 0.0
                    try:
                        while not task.done():
                            state = discord_reply_progress_get(external_id)
                            text_value, presence = discord_reply_progress_text(state, label)
                            self.activity_override = presence
                            now_mono = time.monotonic()
                            if text_value != last and now_mono - last_edit >= 0.9:
                                try:
                                    await original.edit(content=text_value[:1900])
                                    last, last_edit = text_value, now_mono
                                except Exception:
                                    pass
                            try:
                                answer = await asyncio.wait_for(asyncio.shield(task), timeout=0.8)
                                break
                            except asyncio.TimeoutError:
                                continue
                        else:
                            answer = await task
                        chunks = discord_message_chunks(answer)
                        if chunks:
                            await original.edit(content=chunks[0][:1900])
                            for chunk in chunks[1:]:
                                await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                        else:
                            await original.edit(content="✅ Zeno finished, but the reply was empty.")
                    except Exception as exc:
                        await original.edit(content=discord_error_text(exc, "slash command")[:1900])
                    finally:
                        self.activity_override = ""
                        discord_reply_progress_clear(external_id)

                @self.tree.command(name="commands", description="Show Zeno's Discord command center")
                async def zeno_commands(interaction: Any) -> None:
                    text = (
                        "**🤖 Zeno Command Center**\n"
                        "`/ask` Ask Zeno using the shared chat + memory\n"
                        "`/search` Search every active uploaded file locally\n"
                        "`/summarize` Summarize text using the shared Zeno chat\n"
                        "`/commands` Show this command center\n\n"
                        "**Remote browser + automation**\n"
                        "`/browser` Run Browser Agent from Discord\n"
                        "`/screen` Send the current Live Browser screenshot\n"
                        "`/browserstatus` `/browserstop` `/browserresume` Control the agent\n"
                        "`/remind` `/reminders` `/cancelreminder` Persistent reminders\n"
                        "`/memoryoptimize` Preview/apply safe memory deduplication\n\n"
                        "Normal messages are routed only when the tool intent is explicit. Legacy `!` commands still work. Discord threads remain disabled."
                    )
                    await interaction.response.send_message(text, ephemeral=True)

                @self.tree.command(name="ask", description="Ask Zeno in the shared chat")
                async def zeno_ask(interaction: Any, prompt: str) -> None:
                    await run_prompt(interaction, prompt[:8000], "Zeno is processing")

                @self.tree.command(name="search", description="Search all active Zeno uploaded files")
                async def zeno_search(interaction: Any, query: str) -> None:
                    if int(getattr(interaction.channel, "id", 0) or 0) != channel_id:
                        await interaction.response.send_message("This Zeno bot is linked to a different channel.", ephemeral=True)
                        return
                    await interaction.response.defer(thinking=True)
                    try:
                        text = await asyncio.to_thread(discord_file_search_text, chat_id, query[:4000])
                        chunks = discord_message_chunks(text)
                        await interaction.followup.send(chunks[0] if chunks else "No matches.", ephemeral=True)
                        for chunk in chunks[1:]:
                            await interaction.followup.send(chunk, ephemeral=True)
                    except Exception as exc:
                        await interaction.followup.send(discord_error_text(exc, "file search")[:1900], ephemeral=True)

                @self.tree.command(name="summarize", description="Summarize text with Zeno")
                async def zeno_summarize(interaction: Any, text: str) -> None:
                    prompt = "Summarize the following text clearly and compactly. Preserve important constraints, numbers, names, links, and unresolved issues.\n\n" + text[:7000]
                    await run_prompt(interaction, prompt, "Zeno is summarizing")


            # ZENO_AGENT_AUTOMATION_PACK_2026_08_28
            async def _browser_agent_progress_interaction(self, interaction: Any, goal: str, max_steps: int = 20) -> None:
                if int(getattr(interaction.channel, "id", 0) or 0) != channel_id:
                    await interaction.response.send_message("This Zeno bot is linked to a different channel.", ephemeral=True)
                    return
                await interaction.response.send_message("🌐 **Browser Agent** · 2% · Starting…")
                original = await interaction.original_response()
                try:
                    job = await asyncio.to_thread(start_browser_agent, chat_id, goal[:5000], max_steps)
                    job_id = str(job.get("id") or "")
                    last = ""
                    while job_id:
                        row = await asyncio.to_thread(browser_agent_row, job_id)
                        if not row:
                            await original.edit(content="❌ Browser Agent job disappeared.")
                            return
                        status = str(row.get("status") or "queued")
                        step = max(0, int(row.get("step") or 0))
                        maximum = max(1, int(row.get("max_steps") or max_steps or 20))
                        if status == "queued":
                            percent = 4
                        elif status in {"running", "stopping"}:
                            percent = min(96, 8 + int(step / maximum * 86))
                        elif status == "completed":
                            percent = 100
                        else:
                            percent = 98
                        detail = re.sub(r"\s+", " ", str(row.get("detail") or status)).strip()[:700]
                        text = f"🌐 **Browser Agent** · {percent}% · {status.title()}\n{detail}"
                        if text != last:
                            try:
                                await original.edit(content=text[:1900])
                                last = text
                            except Exception:
                                pass
                        self.activity_override = f"Browser Agent · {percent}%"
                        if status not in {"queued", "running", "stopping"}:
                            break
                        await asyncio.sleep(1.0)
                except Exception as exc:
                    await original.edit(content=discord_error_text(exc, "Browser Agent")[:1900])
                finally:
                    self.activity_override = ""

            async def _browser_agent_progress_message(self, message: Any, goal: str, max_steps: int = 20) -> None:
                card = await message.reply(
                    "🌐 **Browser Agent** · 2% · Starting…",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                try:
                    job = await asyncio.to_thread(start_browser_agent, chat_id, goal[:5000], max_steps)
                    job_id = str(job.get("id") or "")
                    last = ""
                    while job_id:
                        row = await asyncio.to_thread(browser_agent_row, job_id)
                        if not row:
                            await card.edit(content="❌ Browser Agent job disappeared.")
                            return
                        status = str(row.get("status") or "queued")
                        step = max(0, int(row.get("step") or 0))
                        maximum = max(1, int(row.get("max_steps") or max_steps or 20))
                        percent = 4 if status == "queued" else (min(96, 8 + int(step / maximum * 86)) if status in {"running", "stopping"} else (100 if status == "completed" else 98))
                        detail = re.sub(r"\s+", " ", str(row.get("detail") or status)).strip()[:700]
                        text = f"🌐 **Browser Agent** · {percent}% · {status.title()}\n{detail}"
                        if text != last:
                            await card.edit(content=text[:1900])
                            last = text
                        self.activity_override = f"Browser Agent · {percent}%"
                        if status not in {"queued", "running", "stopping"}:
                            break
                        await asyncio.sleep(1.0)
                except Exception as exc:
                    await card.edit(content=discord_error_text(exc, "Browser Agent")[:1900])
                finally:
                    self.activity_override = ""

            async def _handle_routed_task(self, message: Any, content: str, author_name: str, external_id: str) -> bool:
                route = route_task(content)
                kind = str(route.get("kind") or "chat")
                if kind == "chat":
                    return False
                owner_id = int(getattr(message.author, "id", 0) or 0)
                if kind == "browser_agent":
                    append_chat_message(chat_id, "user", content, source="discord", source_label=author_name, external_id=external_id)
                    await self._browser_agent_progress_message(message, str(route.get("goal") or content), 20)
                    return True
                if kind == "reminder":
                    try:
                        row = await asyncio.to_thread(create_reminder_from_text, chat_id, channel_id, owner_id, content)
                        await message.reply(
                            f"⏰ Reminder **{row['id']}** set for **{format_reminder_time(int(row['due_at']))}**\n{row['text']}",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except Exception as exc:
                        await message.reply(discord_error_text(exc, "reminder")[:1900], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if kind == "file_search":
                    try:
                        result = await asyncio.to_thread(discord_file_search_text, chat_id, str(route.get("query") or content))
                        await discord_reply_text_or_file(message, result, filename="zeno-file-search.txt")
                    except Exception as exc:
                        await message.reply(discord_error_text(exc, "file search")[:1900], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if kind == "memory_optimize":
                    try:
                        result = await asyncio.to_thread(optimize_memories, chat_id, dry_run=True)
                        await message.reply(
                            f"🧠 **Memory Optimizer preview** · scanned {int(result['scanned']):,} · "
                            f"duplicate groups {int(result['duplicate_groups']):,} · removable duplicates {int(result['duplicates']):,} · "
                            f"metadata repairs {int(result['repairs']):,}.\nUse the GUI Memory Manager to apply it.",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except Exception as exc:
                        await message.reply(discord_error_text(exc, "Memory Optimizer")[:1900], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                return False

            async def sync_reminders(self, channel: Any) -> None:
                while not self.is_closed():
                    try:
                        rows = await asyncio.to_thread(due_reminders, channel_id, limit=20)
                        for row in rows:
                            owner = int(row.get("owner_id") or 0)
                            prefix = f"<@{owner}> " if owner else ""
                            await channel.send(
                                f"{prefix}⏰ **Zeno reminder**\n{str(row.get('text') or '')[:1500]}",
                                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
                            )
                            await asyncio.to_thread(mark_reminder_delivered, str(row.get("id") or ""))
                    except Exception as exc:
                        bridge._set_status("online", f"Discord connected; reminder warning: {str(exc)[:220]}", str(self.user or ""))
                    await asyncio.sleep(12.0)

            def _register_agent_slash_commands(self) -> None:
                if getattr(self, "_zeno_agent_slash_registered", False):
                    return
                self._zeno_agent_slash_registered = True

                @self.tree.command(name="screen", description="Send the current Zeno Live Browser screenshot")
                async def zeno_screen(interaction: Any) -> None:
                    if int(getattr(interaction.channel, "id", 0) or 0) != channel_id:
                        await interaction.response.send_message("This Zeno bot is linked to a different channel.", ephemeral=True)
                        return
                    await interaction.response.defer(thinking=True)
                    raw = await asyncio.to_thread(LIVE_BROWSER.screenshot)
                    state = await asyncio.to_thread(browser_status)
                    if not raw:
                        await interaction.followup.send("Live Browser does not have a screenshot yet.", ephemeral=True)
                        return
                    caption = f"📸 **Zeno Live Browser** · {str(state.get('title') or 'Browser')[:160]}\n{str(state.get('url') or '')[:1500]}"
                    await interaction.followup.send(caption, file=discord.File(io.BytesIO(raw), filename="zeno-browser.jpg"))

                @self.tree.command(name="browser", description="Give Zeno Browser Agent a remote browser task")
                async def zeno_browser(interaction: Any, task: str, max_steps: int = 20) -> None:
                    await self._browser_agent_progress_interaction(interaction, task, max(4, min(int(max_steps or 20), 60)))

                @self.tree.command(name="browserstatus", description="Show the latest Browser Agent job")
                async def zeno_browser_status(interaction: Any) -> None:
                    row = await asyncio.to_thread(browser_agent_latest, chat_id)
                    if not row:
                        await interaction.response.send_message("No Browser Agent jobs yet.", ephemeral=True)
                        return
                    await interaction.response.send_message(
                        f"🌐 **Browser Agent** · {str(row.get('status') or 'unknown').title()} · "
                        f"step {int(row.get('step') or 0)}/{int(row.get('max_steps') or 0)}\n{str(row.get('detail') or '')[:1500]}",
                        ephemeral=True,
                    )

                @self.tree.command(name="browserstop", description="Stop the active Browser Agent job")
                async def zeno_browser_stop(interaction: Any) -> None:
                    row = await asyncio.to_thread(browser_agent_latest, chat_id)
                    if not row or str(row.get("status") or "") not in {"queued", "running", "stopping"}:
                        await interaction.response.send_message("No active Browser Agent job to stop.", ephemeral=True)
                        return
                    stopped = await asyncio.to_thread(stop_browser_agent, str(row.get("id") or ""), chat_id)
                    await interaction.response.send_message(f"⏹️ Browser Agent is **{str(stopped.get('status') or 'stopping')}**.", ephemeral=True)

                @self.tree.command(name="browserresume", description="Resume the latest waiting/stopped Browser Agent job")
                async def zeno_browser_resume(interaction: Any, instruction: str = "") -> None:
                    row = await asyncio.to_thread(browser_agent_latest, chat_id)
                    if not row:
                        await interaction.response.send_message("No Browser Agent job to resume.", ephemeral=True)
                        return
                    goal = instruction.strip() or str(row.get("goal") or "")
                    await interaction.response.send_message("🌐 Resuming Browser Agent…", ephemeral=True)
                    try:
                        resumed = await asyncio.to_thread(resume_browser_agent, str(row.get("id") or ""), chat_id, goal=goal)
                        await interaction.edit_original_response(content=f"🌐 Browser Agent resumed · `{str(resumed.get('id') or '')[:10]}`")
                    except Exception as exc:
                        await interaction.edit_original_response(content=discord_error_text(exc, "Browser Agent resume")[:1900])

                @self.tree.command(name="remind", description="Create a persistent Zeno reminder")
                async def zeno_remind(interaction: Any, when: str, task: str) -> None:
                    try:
                        row = await asyncio.to_thread(
                            create_reminder, chat_id, channel_id, int(getattr(interaction.user, "id", 0) or 0), when, task
                        )
                        await interaction.response.send_message(
                            f"⏰ Reminder **{row['id']}** set for **{format_reminder_time(int(row['due_at']))}**\n{row['text']}",
                            ephemeral=True,
                        )
                    except Exception as exc:
                        await interaction.response.send_message(discord_error_text(exc, "reminder")[:1900], ephemeral=True)

                @self.tree.command(name="reminders", description="List your pending Zeno reminders")
                async def zeno_reminders(interaction: Any) -> None:
                    rows = await asyncio.to_thread(
                        list_reminders, chat_id=chat_id, owner_id=int(getattr(interaction.user, "id", 0) or 0), include_done=False
                    )
                    if not rows:
                        await interaction.response.send_message("You have no pending Zeno reminders.", ephemeral=True)
                        return
                    lines = ["⏰ **Your pending reminders**"]
                    for row in rows[:20]:
                        lines.append(f"`{row['id']}` · **{format_reminder_time(int(row['due_at']))}** · {str(row['text'])[:300]}")
                    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

                @self.tree.command(name="cancelreminder", description="Cancel one of your pending reminders")
                async def zeno_cancel_reminder(interaction: Any, reminder_id: str) -> None:
                    ok = await asyncio.to_thread(cancel_reminder, reminder_id, owner_id=int(getattr(interaction.user, "id", 0) or 0))
                    await interaction.response.send_message("✅ Reminder cancelled." if ok else "I couldn't find that pending reminder.", ephemeral=True)

                @self.tree.command(name="memoryoptimize", description="Preview or apply conservative memory deduplication")
                async def zeno_memory_optimize(interaction: Any, mode: str = "preview") -> None:
                    apply_changes = str(mode or "preview").casefold() in {"apply", "run", "yes"}
                    await interaction.response.defer(thinking=True, ephemeral=True)
                    try:
                        result = await asyncio.to_thread(optimize_memories, chat_id, dry_run=not apply_changes)
                        verb = "applied" if apply_changes else "preview"
                        await interaction.followup.send(
                            f"🧠 **Memory Optimizer {verb}** · scanned **{int(result['scanned']):,}** · "
                            f"duplicate groups **{int(result['duplicate_groups']):,}** · duplicates **{int(result['duplicates']):,}** · "
                            f"metadata repairs **{int(result['repairs']):,}**."
                            + (" A before/after memory checkpoint was saved." if apply_changes else " Use `mode:apply` to perform the merge."),
                            ephemeral=True,
                        )
                    except Exception as exc:
                        await interaction.followup.send(discord_error_text(exc, "Memory Optimizer")[:1900], ephemeral=True)

            async def on_ready(self) -> None:
                # Zeno intentionally uses prefix commands only.  Keep the
                # legacy app-command helpers available for compatibility, but
                # do not register or sync slash commands with Discord.
                channel = self.get_channel(channel_id)
                channel_guild = getattr(channel, "guild", None) if channel is not None else None
                if channel is None or channel_guild is None:
                    bridge._set_status(
                        "error",
                        "The configured Discord channel was not found. Check the Channel ID and bot access.",
                        str(self.user or ""),
                    )
                    return
                if configured_guild_id and int(channel_guild.id) != configured_guild_id:
                    bridge._set_status(
                        "error",
                        "The optional Server ID does not match the configured Discord channel.",
                        str(self.user or ""),
                    )
                    return
                bridge._set_status(
                    "online",
                    f"Shared chat connected to #{getattr(channel, 'name', channel_id)} · commands ready.",
                    str(self.user or ""),
                )
                if not self.sync_started:
                    with db_connect() as db:
                        self.last_message_id = int(db.execute(
                            "SELECT COALESCE(MAX(id),0) FROM messages WHERE chat_id=?",
                            (chat_id,),
                        ).fetchone()[0])
                    self.sync_started = True
                    asyncio.create_task(self.sync_web_chat(channel))
                    asyncio.create_task(self.sync_presence())
                    asyncio.create_task(self.sync_reminders(channel))

            async def on_disconnect(self) -> None:
                return

            async def on_resumed(self) -> None:
                bridge._set_status("online", "Discord connection resumed.", str(self.user or ""))

            async def _command_attachment(self, message: Any) -> Any | None:
                attachments = list(getattr(message, "attachments", []) or [])
                if attachments:
                    return attachments[0]
                reference = getattr(message, "reference", None)
                resolved = getattr(reference, "resolved", None) if reference else None
                referenced_attachments = list(getattr(resolved, "attachments", []) or [])
                if referenced_attachments:
                    return referenced_attachments[0]
                message_id = int(getattr(reference, "message_id", 0) or 0) if reference else 0
                if message_id:
                    try:
                        referenced = await message.channel.fetch_message(message_id)
                        attachments = list(getattr(referenced, "attachments", []) or [])
                        return attachments[0] if attachments else None
                    except Exception:
                        return None
                return None

            async def _reply_context(self, message: Any) -> str:
                reference = getattr(message, "reference", None)
                if not reference:
                    return ""
                resolved = getattr(reference, "resolved", None)
                referenced = resolved
                if referenced is None:
                    message_id = int(getattr(reference, "message_id", 0) or 0)
                    if message_id:
                        try:
                            referenced = await message.channel.fetch_message(message_id)
                        except Exception:
                            referenced = None
                if referenced is None:
                    return ""
                author = getattr(referenced, "author", None)
                author_name = str(getattr(author, "display_name", "") or getattr(author, "name", "") or "Discord user")
                body = str(getattr(referenced, "content", "") or "").strip()
                attachments = list(getattr(referenced, "attachments", []) or [])
                attachment_note = ""
                if attachments:
                    names = ", ".join(str(getattr(item, "filename", "file"))[:120] for item in attachments[:5])
                    attachment_note = f" [attachments: {names}]"
                if not body and not attachment_note:
                    return ""
                return f"{author_name}: {body[:4200]}{attachment_note}".strip()

            async def _safe_reactions(self, sent: Any) -> None:
                if sent is None:
                    return
                try:
                    for emoji in ("⏹️", "🔁", "📄"):
                        await sent.add_reaction(emoji)
                    self.control_messages[int(sent.id)] = {"kind": "task", "chat_id": chat_id}
                    if len(self.control_messages) > 100:
                        for old_id in list(self.control_messages)[:-80]:
                            self.control_messages.pop(old_id, None)
                except Exception:
                    pass

            async def _aycd_reactions(self, sent: Any, job_code: str, allowed_user_id: str, reactions: list[str] | None = None) -> None:
                if sent is None or not job_code:
                    return
                wanted = list(reactions or ["✅", "👀", "🎲", "🔍", "❌"])[:12]
                try:
                    try:
                        await sent.clear_reactions()
                    except Exception:
                        pass
                    for emoji in wanted:
                        await sent.add_reaction(emoji)
                    self.control_messages[int(sent.id)] = {
                        "kind": "aycd_job",
                        "chat_id": chat_id,
                        "job_code": str(job_code).upper(),
                        "allowed_user_id": str(allowed_user_id or ""),
                        "reactions": wanted,
                    }
                    if len(self.control_messages) > 140:
                        for old_id in list(self.control_messages)[:-100]:
                            self.control_messages.pop(old_id, None)
                except Exception:
                    pass

            async def _aycd_dashboard_reactions(self, sent: Any, allowed_user_id: str, reactions: list[str] | None = None) -> None:
                if sent is None:
                    return
                wanted = list(reactions or ["🔄", "📋", "👀"])[:8]
                try:
                    try:
                        await sent.clear_reactions()
                    except Exception:
                        pass
                    for emoji in wanted:
                        await sent.add_reaction(emoji)
                    self.control_messages[int(sent.id)] = {
                        "kind": "aycd_dashboard",
                        "chat_id": chat_id,
                        "allowed_user_id": str(allowed_user_id or ""),
                        "reactions": wanted,
                    }
                    if len(self.control_messages) > 140:
                        for old_id in list(self.control_messages)[:-100]:
                            self.control_messages.pop(old_id, None)
                except Exception:
                    pass

            async def _send_last_result(self, target: Any) -> Any:
                result = await asyncio.to_thread(discord_last_result, chat_id)
                if result.get("kind") == "file":
                    try:
                        raw, metadata = await asyncio.to_thread(read_generated_file, int(result["id"]), chat_id=chat_id)
                        file_obj = discord.File(io.BytesIO(raw), filename=str(metadata["name"]))
                        return await target.send(
                            result["text"],
                            file=file_obj,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except Exception as exc:
                        return await target.send(
                            result["text"] + "\n" + discord_error_text(exc, "file upload"),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                return await discord_channel_text_or_file(target, str(result.get("text") or ""), filename="zeno-result.txt")

            async def _progress_card(self, message: Any, label: str) -> Any:
                try:
                    sent = await message.reply(
                        f"⏳ **{label}**",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await self._safe_reactions(sent)
                    return sent
                except Exception:
                    return None

            async def _finish_progress_card(self, card: Any, text_value: str) -> None:
                if card is None:
                    return
                try:
                    await card.edit(content=text_value[:1900])
                except Exception:
                    pass

            async def _run_with_reply_progress(
                self,
                message: Any,
                label: str,
                func: Any,
                *args: Any,
                progress_key: str = "",
            ) -> Any:
                """Run reply work while rendering throttled stage-based percentages."""
                key = str(progress_key or "")
                if key:
                    discord_reply_progress_clear(key)
                    discord_reply_progress_update(key, "building_context", 0.0, "Starting reply")
                task = asyncio.create_task(asyncio.to_thread(func, *args))
                card = None
                last_text = ""
                last_edit_at = 0.0
                last_overall = -1
                last_phase = ""
                try:
                    while not task.done():
                        state = discord_reply_progress_get(key) if key else {"phase": "starting"}
                        text, presence = discord_reply_progress_text(state, label)
                        phase = str(state.get("phase") or "")
                        overall = discord_reply_overall_percent(state)
                        self.activity_override = presence
                        current = time.monotonic()
                        materially_changed = phase != last_phase or overall >= last_overall + 3 or overall == 100
                        if text != last_text and materially_changed and current - last_edit_at >= 0.9:
                            try:
                                if card is None:
                                    card = await message.reply(
                                        text,
                                        mention_author=False,
                                        allowed_mentions=discord.AllowedMentions.none(),
                                    )
                                    await self._safe_reactions(card)
                                else:
                                    await card.edit(content=text)
                                last_text = text
                                last_edit_at = current
                                last_overall = overall
                                last_phase = phase
                            except Exception:
                                pass
                        try:
                            return await asyncio.wait_for(asyncio.shield(task), timeout=0.8)
                        except asyncio.TimeoutError:
                            continue
                    return await task
                finally:
                    self.activity_override = ""
                    if key:
                        state = discord_reply_progress_get(key)
                        if card is not None:
                            try:
                                if str(state.get("phase") or "") == "complete":
                                    await card.edit(content="✅ **Zeno reply** · 100%")
                                elif task.cancelled():
                                    await card.edit(content="⏹️ **Zeno reply stopped**")
                                else:
                                    await card.edit(content="✅ **Zeno reply finished** · 100%")
                                await asyncio.sleep(0.8)
                                await card.delete()
                            except Exception:
                                pass
                        discord_reply_progress_clear(key)

            async def _read_attachment(self, attachment: Any) -> tuple[bytes, str]:
                size = int(getattr(attachment, "size", 0) or 0)
                if size and size > MAX_UPLOAD_BYTES:
                    raise ValueError(f"Attachments are limited to {MAX_UPLOAD_BYTES // 1_000_000} MB in Zeno.")
                raw = await attachment.read()
                if len(raw) > MAX_UPLOAD_BYTES:
                    raise ValueError(f"Attachments are limited to {MAX_UPLOAD_BYTES // 1_000_000} MB in Zeno.")
                return raw, str(getattr(attachment, "filename", "discord-upload.bin"))

            async def _store_normal_attachments(self, message: Any) -> tuple[list[int], list[dict[str, Any]]]:
                file_ids: list[int] = []
                refs: list[dict[str, Any]] = []
                for attachment in list(getattr(message, "attachments", []) or [])[:4]:
                    raw, filename = await self._read_attachment(attachment)
                    mime = str(getattr(attachment, "content_type", "") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
                    stored = await asyncio.to_thread(
                        store_uploaded_file,
                        chat_id,
                        filename,
                        mime,
                        raw,
                        allow_images=True,
                    )
                    file_ids.append(int(stored["id"]))
                    refs.append({
                        "kind": "uploaded_file",
                        "id": int(stored["id"]),
                        "name": str(stored["name"]),
                        "mime": str(stored["mime"]),
                    })
                return file_ids, refs

            async def _handle_list_compare(self, message: Any, command: str, content: str, author_name: str, external_id: str) -> bool:
                attachments = list(getattr(message, "attachments", []) or [])
                if len(attachments) != 2:
                    await message.reply(
                        "Attach exactly two text files to the same message, then use `!comparelist` (or `!listcompare`) for shared duplicates, or `!listmissing` for entries present in only one list.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return True
                stored: list[dict[str, Any]] = []
                for attachment in attachments:
                    raw, filename = await self._read_attachment(attachment)
                    mime = str(getattr(attachment, "content_type", "") or mimetypes.guess_type(filename)[0] or "text/plain")
                    stored.append(await asyncio.to_thread(store_uploaded_file_record, chat_id, filename, mime, raw))
                comparison = await asyncio.to_thread(
                    compare_uploaded_lists, chat_id, int(stored[0]["id"]), int(stored[1]["id"])
                )
                counts = comparison.get("counts") or {}
                lines = [
                    "Zeno list comparison",
                    f"List A: {stored[0]['name']} · {int(counts.get('lines_a', 0)):,} lines / {int(counts.get('unique_a', 0)):,} unique",
                    f"List B: {stored[1]['name']} · {int(counts.get('lines_b', 0)):,} lines / {int(counts.get('unique_b', 0)):,} unique",
                ]
                if command == "!listmissing":
                    lines += [
                        "",
                        f"Only in List A ({int(counts.get('only_a', 0)):,}):",
                        *(comparison.get("only_a") or ["(none)"]),
                        "",
                        f"Only in List B ({int(counts.get('only_b', 0)):,}):",
                        *(comparison.get("only_b") or ["(none)"]),
                    ]
                else:
                    lines += [
                        "",
                        f"Shared entries / duplicates across both lists ({int(counts.get('shared', 0)):,}):",
                        *(comparison.get("duplicates") or ["(none)"]),
                    ]
                if int(counts.get("internal_duplicates_a", 0)) or int(counts.get("internal_duplicates_b", 0)):
                    lines += ["", f"Internal duplicate rows: List A {int(counts.get('internal_duplicates_a', 0)):,} · List B {int(counts.get('internal_duplicates_b', 0)):,}."]
                answer = "\n".join(lines)
                append_chat_message(chat_id, "user", content, source="discord", source_label=author_name, external_id=external_id)
                append_chat_message(chat_id, "assistant", answer, source="discord", source_label="Zeno", external_id=external_id)
                await discord_reply_text_or_file(message, answer, filename="zeno-list-comparison.txt")
                return True

            async def _handle_file_command(self, message: Any, command: str, content: str, author_name: str, external_id: str) -> bool:
                attachment = await self._command_attachment(message)
                if attachment is None:
                    await message.reply(
                        "Attach a file, or reply to a message that has one.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return True
                raw, filename = await self._read_attachment(attachment)
                direct_modes = {
                    "!scramble": ("brand_proxy_scramble", "discord-scramble"),
                    "!removedupes": ("dedupe_lines", "discord-dedupe"),
                    "!emailcolon": ("email_colon", "discord-emailcolon"),
                    "!cardcolon": ("card_colon_4", "discord-cardcolon"),
                    "!cardcolon5": ("card_colon_5", "discord-cardcolon5"),
                    "!extractemails": ("extract_emails", "discord-extractemails"),
                    "!dupeemails": ("duplicate_emails", "discord-dupeemails"),
                    "!sortlines": ("sort_lines", "discord-sortlines"),
                    "!removeblanks": ("remove_blank_lines", "discord-removeblanks"),
                }
                if command in direct_modes:
                    mode, source_job_id = direct_modes[command]
                    transform_options: dict[str, str] = {}
                    if command == "!cardcolon5":
                        parts = content.split(None, 1)
                        if len(parts) > 1:
                            transform_options["default_type"] = parts[1].strip()[:40]
                    output_raw, output_name, stats = await asyncio.to_thread(
                        discord_transform_payload, raw, filename, mode, transform_options
                    )
                    stored = await asyncio.to_thread(
                        store_generated_file, chat_id, output_name, output_raw, source_job_id=source_job_id
                    )
                    if command == "!scramble":
                        answer = f"Scrambled **{stats['input_lines']:,}** complete line(s); every record was preserved."
                    elif command == "!removedupes":
                        answer = f"Removed **{stats['removed_duplicates']:,}** exact duplicate line(s) · **{stats['output_lines']:,}** line(s) remain."
                    elif command == "!emailcolon":
                        answer = f"Converted **{stats['converted_lines']:,}** `email:password` row(s) to `email:::password` · **{stats['output_lines']:,}** line(s) preserved."
                    elif command in {"!cardcolon", "!cardcolon5"}:
                        shape = "type:number:month:year:cvv" if command == "!cardcolon5" else "number:month:year:cvv"
                        answer = (f"Formatted **{stats['formatted_cards']:,}** AYCD Card Info row(s) locally as `{shape}`. "
                                  f"Saved as **{output_name}** under **Files → Generated outputs**. Full values were not reposted into Discord.")
                        if stats.get("invalid_card_rows"):
                            answer += f" · Skipped **{stats['invalid_card_rows']:,}** invalid/header row(s)."
                        if stats.get("missing_card_type_rows"):
                            answer += f" · Skipped **{stats['missing_card_type_rows']:,}** row(s) missing card type."
                    elif command == "!extractemails":
                        answer = f"Extracted **{stats['extracted_emails']:,}** unique email address(es)."
                    elif command == "!dupeemails":
                        answer = f"Found **{stats['duplicate_emails']:,}** email address(es) that occur more than once."
                    elif command == "!sortlines":
                        answer = f"Sorted **{stats['output_lines']:,}** complete line(s) A-Z."
                    else:
                        answer = f"Removed **{stats['removed_blank_lines']:,}** blank line(s) · **{stats['output_lines']:,}** line(s) remain."
                    append_chat_message(chat_id, "user", content, source="discord", source_label=author_name, external_id=external_id)
                    assistant_id = append_chat_message(chat_id, "assistant", answer, source="discord", source_label="Zeno", attachments=[stored], external_id=external_id)
                    with db_connect() as db:
                        db.execute("UPDATE generated_files SET source_message_id=? WHERE id=?", (assistant_id, int(stored["id"])))
                else:
                    instruction = content.split(None, 1)[1].strip() if len(content.split(None, 1)) > 1 else ""
                    stop_event = threading.Event()
                    card = await self._progress_card(message, "Processing attached file…")
                    try:
                        answer, stored = await asyncio.to_thread(
                            discord_file_bridge,
                            chat_id,
                            instruction,
                            raw,
                            filename,
                            author_name,
                            external_id,
                            stop_event,
                        )
                        await self._finish_progress_card(card, "✅ File finished")
                    except Exception:
                        await self._finish_progress_card(card, "⚠️ File task did not finish")
                        raise
                if command in {"!cardcolon", "!cardcolon5"}:
                    await message.reply(
                        answer,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    generated_raw, metadata = await asyncio.to_thread(read_generated_file, int(stored["id"]), chat_id=chat_id)
                    file_obj = discord.File(io.BytesIO(generated_raw), filename=str(metadata["name"]))
                    await message.reply(
                        answer,
                        file=file_obj,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return True

            async def _handle_command(self, message: Any, content: str, author_name: str, external_id: str) -> bool:
                command = content.split(None, 1)[0].casefold()
                if command == "!aycd":
                    result = await asyncio.to_thread(
                        handle_aycd_command,
                        chat_id,
                        content,
                        source="discord",
                        user_id=str(message.author.id),
                    )
                    if result is None:
                        return False
                    # Keep AYCD command traffic in the same shared chat used by the browser.
                    append_chat_message(
                        chat_id, "user", content, source="discord", source_label=author_name, external_id=external_id
                    )
                    answer = str(result.get("text") or "").strip()
                    job_code = str(result.get("job_code") or "").strip().upper()
                    append_chat_message(
                        chat_id, "assistant", answer, source="discord",
                        source_label=(f"AYCD Job {job_code}" if job_code else "Zeno · AYCD"),
                        external_id=external_id + ":aycd",
                    )
                    sent = await discord_reply_text_or_file(message, answer, filename="zeno-aycd-result.txt")
                    if sent is not None and job_code:
                        await self._aycd_reactions(
                            sent, job_code, str(message.author.id), list(result.get("reactions") or [])
                        )
                    elif sent is not None and str(result.get("control_kind") or "") == "aycd_dashboard":
                        await self._aycd_dashboard_reactions(
                            sent, str(message.author.id), list(result.get("reactions") or [])
                        )
                    return True
                if command == "!help":
                    for chunk in discord_message_chunks(discord_command_help()):
                        await message.reply(chunk, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command in {"!fast", "!balanced", "!deep"}:
                    mode = command[1:]
                    set_setting("model_mode", mode)
                    text = (
                        "⚡ Model mode set to **Fast**."
                        if mode == "fast"
                        else "⚖️ Model mode set to **Balanced** (fast model, balanced defaults)."
                        if mode == "balanced"
                        else "🧠 Model mode set to **Deep**. The configured deep model is now explicitly selected."
                    )
                    await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command == "!reset":
                    await asyncio.to_thread(stop_discord_chat_work, chat_id)
                    result = await asyncio.to_thread(reset_chat_context, chat_id, "discord", "Zeno")
                    await message.reply(
                        f"🧹 Context reset at message #{int(result['boundary_id'])}. Visible history, files, pages, and long-term memory were kept.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return True
                if command == "!context":
                    stop_event = threading.Event()
                    register_chat_operation(chat_id, stop_event)
                    self.activity_override = "Consolidating context"
                    card = await self._progress_card(message, "🧠 Summarizing shared chat into Memory Bank…")
                    try:
                        async with message.channel.typing():
                            result = await asyncio.to_thread(manual_context_to_memory, chat_id, stop_event)
                        text = (
                            f"🧠 **Context saved to Memory Bank.**\n"
                            f"Summarized **{int(result['messages']):,}** message(s) across **{int(result.get('chunks') or 0):,}** chunk(s).\n"
                            f"Added **{int(result['added']):,}** new memories · refreshed **{int(result['refreshed']):,}** existing memories."
                        )
                        await self._finish_progress_card(card, "✅ Context summarized and saved")
                        await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    except Exception:
                        await self._finish_progress_card(card, "⚠️ Context consolidation did not finish")
                        raise
                    finally:
                        unregister_chat_operation(chat_id, stop_event)
                        self.activity_override = ""
                    return True
                if command == "!compact":
                    stop_event = threading.Event()
                    register_chat_operation(chat_id, stop_event)
                    self.activity_override = "Compacting context"
                    card = await self._progress_card(message, "🧠 Compacting shared chat context…")
                    try:
                        async with message.channel.typing():
                            result = await asyncio.to_thread(manual_context_to_memory, chat_id, stop_event)
                            boundary = await asyncio.to_thread(reset_chat_context, chat_id, "discord", "Zeno")
                        text = (
                            f"🧠 **Context compacted.**\n"
                            f"Reviewed **{int(result['messages']):,}** message(s), added **{int(result['added']):,}** memories, "
                            f"refreshed **{int(result['refreshed']):,}**, then opened a fresh context window at message "
                            f"#{int(boundary['boundary_id'])}. Visible chat history was kept."
                        )
                        await self._finish_progress_card(card, "✅ Context compacted")
                        await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    except Exception:
                        await self._finish_progress_card(card, "⚠️ Context compact did not finish")
                        raise
                    finally:
                        unregister_chat_operation(chat_id, stop_event)
                        self.activity_override = ""
                    return True
                if command == "!status":
                    await message.reply(await asyncio.to_thread(discord_status_text, chat_id), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command == "!profile":
                    await message.reply(discord_profile_text(chat_id), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command == "!ping":
                    health = await asyncio.to_thread(discord_health_text, float(self.latency) * 1000.0)
                    await message.reply(health, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command == "!diagnostics":
                    report = await asyncio.to_thread(discord_diagnostics_text, float(self.latency) * 1000.0)
                    await message.reply(report, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command == "!uptime":
                    await message.reply(f"⏱️ Zeno uptime: **{discord_format_uptime()}** · V{APP_VERSION}", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command == "!jobs":
                    sent = await message.reply(await asyncio.to_thread(discord_jobs_text, chat_id, False), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    await self._safe_reactions(sent)
                    return True
                if command == "!last":
                    await self._send_last_result(message.channel)
                    return True
                if command == "!retry":
                    result = await asyncio.to_thread(discord_retry_last_task, chat_id)
                    if result.startswith("There is no failed"):
                        with db_connect() as db:
                            row = db.execute(
                                "SELECT content,source_label FROM messages WHERE chat_id=? AND role='user' AND source='discord' "
                                "ORDER BY id DESC LIMIT 1",
                                (chat_id,),
                            ).fetchone()
                        if row and str(row["content"]).strip() and not str(row["content"]).lstrip().startswith("!"):
                            retry_text = str(row["content"]).strip()
                            retry_external = external_id + ":retry:" + uuid.uuid4().hex[:8]
                            async with message.channel.typing():
                                answer = await self._run_with_reply_progress(
                                    message,
                                    "Retrying Zeno reply",
                                    process_discord_chat,
                                    chat_id,
                                    retry_text,
                                    str(message.author.id),
                                    author_name,
                                    retry_external,
                                    threading.Event(),
                                    "",
                                    progress_key=retry_external,
                                )
                            await discord_channel_text_or_file(message.channel, answer, filename="zeno-retry.txt")
                            return True
                    sent = await message.reply(result, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    await self._safe_reactions(sent)
                    return True
                if command == "!stop":
                    stopped = await asyncio.to_thread(stop_discord_chat_work, chat_id)
                    await message.reply(
                        f"⏹️ Stop sent · active operations **{stopped['generation_count']}** · "
                        f"DeepSearch **{stopped['deepsearch_count']}** · File Worker **{stopped['file_job_count']}**.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return True
                if command == "!clearfiles":
                    parts = content.split(None, 1)
                    inventory = await asyncio.to_thread(uploaded_file_inventory)
                    if len(parts) < 2 or parts[1].strip().casefold() != "confirm":
                        await message.reply(
                            f"🧹 Zeno currently has **{inventory['file_count']:,} uploaded/input file(s)** in the database "
                            f"({inventory['text_chars']:,} extracted text characters). Generated output files are not included. "
                            "Use `!clearfiles confirm` to permanently remove all uploaded/input file records and local upload copies.",
                            mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        result = await asyncio.to_thread(clear_all_uploaded_files)
                        await message.reply(
                            f"🧹 Removed **{int(result['files']):,}** uploaded/input file record(s) and **{int(result['disk_files']):,}** local file(s). Generated outputs were kept.",
                            mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    return True
                if command == "!notify":
                    parts = content.split(None, 1)
                    if len(parts) < 2 or parts[1].strip().casefold() not in {"on", "off"}:
                        await message.reply("Use `!notify on` or `!notify off`.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    else:
                        enabled = parts[1].strip().casefold() == "on"
                        set_setting("discord_completion_mentions", "true" if enabled else "false")
                        await message.reply(f"Completion mentions are now **{'on' if enabled else 'off'}**.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command == "!screenshot":
                    raw = await asyncio.to_thread(LIVE_BROWSER.screenshot)
                    if not raw:
                        await message.reply("Live Browser does not have a screenshot yet.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    else:
                        await message.reply(file=discord.File(io.BytesIO(raw), filename="zeno-browser.jpg"), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    return True
                if command in {"!comparelist", "!listcompare", "!listmissing"}:
                    if command == "!listcompare":
                        command = "!comparelist"
                    return await self._handle_list_compare(message, command, content, author_name, external_id)
                if command in {"!file", "!scramble", "!removedupes", "!emailcolon", "!cardcolon", "!cardcolon5", "!extractemails", "!dupeemails", "!sortlines", "!removeblanks"}:
                    return await self._handle_file_command(message, command, content, author_name, external_id)
                return False

            async def on_message(self, message: Any) -> None:
                if self.user is None or getattr(message.author, "bot", False) or int(getattr(message.author, "id", 0) or 0) == int(self.user.id):
                    return
                if int(getattr(message.channel, "id", 0) or 0) != channel_id:
                    return
                guild = getattr(message, "guild", None)
                if configured_guild_id and (guild is None or int(guild.id) != configured_guild_id):
                    return
                content = str(getattr(message, "content", "") or "").strip()
                if not content and getattr(message, "attachments", None):
                    content = "Please analyze the attached file."
                if not content:
                    return
                author_name = str(getattr(message.author, "display_name", "") or getattr(message.author, "name", "") or "Discord user")[:80]
                self.last_discord_author_id = int(getattr(message.author, "id", 0) or 0)
                external_id = f"{int(getattr(guild, 'id', 0) or 0)}:{channel_id}:{int(message.id)}"

                try:
                    if content.startswith("!") and await self._handle_command(message, content, author_name, external_id):
                        return
                    if await self._handle_routed_task(message, content, author_name, external_id):
                        return
                    async with self.processing_lock:
                        request = natural_deepsearch_request(content)
                        if request is not None:
                            append_chat_message(chat_id, "user", content, source="discord", source_label=author_name, external_id=external_id)
                            goal = await asyncio.to_thread(deepsearch_goal_with_chat_context, chat_id, content)
                            job_id = await asyncio.to_thread(
                                start_deepsearch,
                                chat_id,
                                str(request["url"]),
                                goal,
                                int(request["page_limit"]),
                                int(request["max_depth"]),
                            )
                            await message.reply(
                                f"🔎 DeepSearch started · `{job_id[:8]}` · up to **{int(request['page_limit']):,}** pages. Progress will mirror into this shared chat.",
                                mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                            return

                        file_ids: list[int] = []
                        attachment_refs: list[dict[str, Any]] = []
                        if getattr(message, "attachments", None):
                            file_ids, attachment_refs = await self._store_normal_attachments(message)
                        reply_context = await self._reply_context(message)
                        stop_event = threading.Event()
                        async with message.channel.typing():
                            # Keyword arguments are wrapped so the legacy positional compatibility signature remains intact.
                            def run_chat() -> str:
                                return process_discord_chat(
                                    chat_id,
                                    content,
                                    str(message.author.id),
                                    author_name,
                                    external_id,
                                    stop_event,
                                    reply_context,
                                    file_ids=file_ids,
                                    attachments=attachment_refs,
                                )
                            answer = await self._run_with_reply_progress(
                                message,
                                "Zeno is processing",
                                run_chat,
                                progress_key=external_id,
                            )
                        await discord_channel_text_or_file(message.channel, answer, filename="zeno-response.txt")
                except Exception as exc:
                    await message.reply(discord_error_text(exc), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

            async def on_raw_reaction_add(self, payload: Any) -> None:
                if self.user is not None and int(getattr(payload, "user_id", 0) or 0) == int(self.user.id):
                    return
                if int(getattr(payload, "channel_id", 0) or 0) != channel_id:
                    return
                message_id = int(getattr(payload, "message_id", 0) or 0)
                control = dict(self.control_messages.get(message_id) or {})
                if not control:
                    return
                emoji = str(getattr(payload, "emoji", ""))
                actor_id = str(int(getattr(payload, "user_id", 0) or 0))
                try:
                    channel = self.get_channel(channel_id)
                    if channel is None:
                        return
                    if control.get("kind") == "aycd_job":
                        allowed = str(control.get("allowed_user_id") or "")
                        if allowed and actor_id != allowed:
                            await channel.send(
                                "🔒 That AYCD approval belongs to the Discord user who created the job.",
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                            return
                        if emoji not in set(control.get("reactions") or []):
                            return
                        job_code = str(control.get("job_code") or "").upper()
                        result = await asyncio.to_thread(
                            aycd_reaction_action, chat_id, job_code, emoji, actor_id=actor_id, source="discord"
                        )
                        text = str(result.get("text") or "").strip()
                        if text:
                            chunks = discord_message_chunks(text)
                            original = None
                            try:
                                original = await channel.fetch_message(message_id)
                                if chunks:
                                    await original.edit(content=chunks[0][:1900])
                                    for extra in chunks[1:]:
                                        await channel.send(extra, allowed_mentions=discord.AllowedMentions.none())
                            except Exception:
                                original = None
                            if original is None and chunks:
                                original = await channel.send(chunks[0][:1900], allowed_mentions=discord.AllowedMentions.none())
                            try:
                                append_chat_message(
                                    chat_id, "assistant", text, source="discord",
                                    source_label=f"AYCD Job {job_code}",
                                    external_id=f"discord:aycd-reaction:{message_id}:{emoji}:{int(time.time()*1000)}",
                                )
                            except Exception:
                                pass
                            job = dict(result.get("job") or {})
                            if original is not None and job_code:
                                reactions = aycd_job_reactions(job)
                                await self._aycd_reactions(original, job_code, allowed, reactions)
                        return
                    if control.get("kind") == "aycd_dashboard":
                        allowed = str(control.get("allowed_user_id") or "")
                        if allowed and actor_id != allowed:
                            return
                        if emoji not in set(control.get("reactions") or []):
                            return
                        result = await asyncio.to_thread(aycd_dashboard_reaction, chat_id, emoji)
                        text = str(result.get("text") or "").strip()
                        if text:
                            chunks = discord_message_chunks(text)
                            original = None
                            try:
                                original = await channel.fetch_message(message_id)
                                if chunks:
                                    await original.edit(content=chunks[0][:1900])
                                    for extra in chunks[1:]:
                                        await channel.send(extra, allowed_mentions=discord.AllowedMentions.none())
                            except Exception:
                                original = None
                            if original is None and chunks:
                                original = await channel.send(chunks[0][:1900], allowed_mentions=discord.AllowedMentions.none())
                            if original is not None:
                                await self._aycd_dashboard_reactions(
                                    original, allowed, list(result.get("reactions") or ["🔄", "📋", "👀"])
                                )
                        return
                    if emoji == "⏹️":
                        stopped = await asyncio.to_thread(stop_discord_chat_work, chat_id)
                        await channel.send(
                            f"⏹️ Stop sent · generation **{stopped['generation_count']}** · DeepSearch **{stopped['deepsearch_count']}** · File Worker **{stopped['file_job_count']}**.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    elif emoji == "🔁":
                        await channel.send(await asyncio.to_thread(discord_retry_last_task, chat_id), allowed_mentions=discord.AllowedMentions.none())
                    elif emoji == "📄":
                        await self._send_last_result(channel)
                except Exception:
                    return

            async def _send_synced_message(self, channel: Any, row: dict[str, Any]) -> None:
                source = str(row.get("source") or "")
                if source == "discord":
                    return
                role = str(row.get("role") or "assistant")
                content = str(row.get("content") or "").strip()
                label = str(row.get("source_label") or "").strip()
                if source == "aycd":
                    if role == "user":
                        content = ("**AYCD → Zeno**\n" + content).strip()
                    else:
                        content = ("**Zeno via AYCD**\n" + content).strip()
                elif role == "user" and source not in {"discord"}:
                    content = (f"**Web chat · {label or 'User'}**\n" + content).strip()
                elif label and label not in {"Zeno"}:
                    content = (f"**{label}**\n" + content).strip()

                attachments: list[Any] = []
                try:
                    parsed = json.loads(str(row.get("attachments_json") or "[]"))
                    if isinstance(parsed, list):
                        attachments = parsed
                except json.JSONDecodeError:
                    attachments = []

                file_sent = False
                last_sent = None
                for item in attachments[:3]:
                    if not isinstance(item, dict) or item.get("kind") != "generated_file" or not item.get("id"):
                        continue
                    try:
                        raw, metadata = await asyncio.to_thread(read_generated_file, int(item["id"]), chat_id=chat_id)
                        file_obj = discord.File(io.BytesIO(raw), filename=str(metadata["name"]))
                        first = discord_message_chunks(content)[0] if content else "Zeno generated a file."
                        last_sent = await channel.send(first, file=file_obj, allowed_mentions=discord.AllowedMentions.none())
                        file_sent = True
                        remaining = discord_message_chunks(content)[1:] if content else []
                        for chunk in remaining:
                            last_sent = await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                        break
                    except Exception as exc:
                        content += "\n" + discord_error_text(exc, "synced file upload")
                if not file_sent:
                    for chunk in discord_message_chunks(content):
                        last_sent = await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

                # Browser-created AYCD jobs are mirrored into Discord with the
                # same reaction controls, so either interface can continue them.
                job_match = re.search(r"(?i)^AYCD Job\s+(A\d+)$", label)
                if last_sent is not None and job_match:
                    job_code = job_match.group(1).upper()
                    job = await asyncio.to_thread(aycd_job, job_code, chat_id)
                    if job:
                        status = str(job.get("status") or "")
                        reactions = aycd_job_reactions(job)
                        await self._aycd_reactions(
                            last_sent, job_code, str(job.get("created_by_user") or ""), reactions
                        )

            async def sync_web_chat(self, channel: Any) -> None:
                while not self.is_closed():
                    try:
                        rows = await asyncio.to_thread(discord_web_updates, chat_id, self.last_message_id)
                        for row in rows:
                            self.last_message_id = max(self.last_message_id, int(row["id"]))
                            await self._send_synced_message(channel, row)
                    except Exception as exc:
                        bridge._set_status("online", f"Discord connected; shared-chat sync warning: {str(exc)[:220]}", str(self.user or ""))
                    await asyncio.sleep(1.2)

            async def sync_presence(self) -> None:
                while not self.is_closed():
                    try:
                        mode = get_setting("model_mode", "balanced").title()
                        text = self.activity_override or f"Ready · {mode}"
                        if text != self._last_presence_text:
                            await self.change_presence(activity=discord.Game(name=text[:128]))
                            self._last_presence_text = text
                    except Exception:
                        pass
                    await asyncio.sleep(3.0)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = ZenoDiscordClient()
        with self._lock:
            self._loop = loop
            self._client = client
        try:
            loop.run_until_complete(client.start(str(config["token"]), reconnect=True))
        except Exception as exc:
            self._set_status("error", f"Discord bridge stopped with an error: {str(exc)[:350]}")
        finally:
            try:
                if not client.is_closed():
                    loop.run_until_complete(client.close())
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            with self._lock:
                self._loop = None
                self._client = None
            if self._status not in {"error", "disabled"}:
                self._set_status("stopped", "Discord bridge stopped.")


DiscordHandler = DiscordBridge
DISCORD_BRIDGE = DiscordBridge()


def start_discord_bridge() -> None:
    DISCORD_BRIDGE.start()


def stop_discord_bridge() -> None:
    DISCORD_BRIDGE.stop()


def restart_discord_bridge() -> None:
    DISCORD_BRIDGE.restart_async()


def discord_bridge_state() -> dict[str, Any]:
    return DISCORD_BRIDGE.public_status()


def send_message(channel_id: int, message: str) -> bool:
    return DISCORD_BRIDGE.send_message(channel_id, message)


def receive_commands() -> str:
    return DISCORD_BRIDGE.receive_commands()


__all__ = [
    "DISCORD_DEFAULT_CONFIG",
    "discord_info_file_values",
    "load_discord_info_file",
    "discord_bridge_config",
    "save_discord_bridge_config",
    "discord_public_config",
    "append_chat_message",
    "discord_web_updates",
    "sanitize_discord_answer",
    "discord_reply_progress_update",
    "discord_reply_progress_get",
    "discord_reply_progress_clear",
    "discord_reply_overall_percent",
    "discord_reply_progress_text",
    "discord_message_chunks",
    "discord_error_text",
    "discord_decode_text_payload",
    "discord_transform_payload",
    "discord_direct_file_transform",
    "process_discord_chat",
    "discord_file_bridge",
    "discord_format_uptime",
    "discord_health_text",
    "discord_status_text",
    "discord_profile_text",
    "discord_diagnostics_text",
    "discord_command_help",
    "discord_jobs_text",
    "discord_retry_last_task",
    "discord_last_result",
    "DiscordBridge",
    "DiscordHandler",
    "DISCORD_BRIDGE",
    "start_discord_bridge",
    "stop_discord_bridge",
    "restart_discord_bridge",
    "discord_bridge_state",
    "send_message",
    "receive_commands",
]
