#!/usr/bin/env python3
"""Zeno 3.0 local HTTP API and streaming server.

This module is intentionally a routing/orchestration layer. Subsystem business
logic remains in the dedicated Zeno modules. The server binds to localhost only.
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from console_runtime import console_log
from config import (
    APP_HOST,
    APP_PORT,
    APP_VERSION,
    BASE_DIR,
    HTML_PATH,
    ICON_PATH,
    WALLPAPER_PATH,
    MAX_REQUEST_BYTES,
    MAX_UPLOAD_BYTES,
    MEMORY_DIR,
)
from database import current_chat_id, db_connect, now
from settings import (
    bool_setting,
    clear_settings_cache,
    get_setting,
    int_setting,
    model_mode_setting,
    set_setting,
)
from model_api import (
    get_model_runtime,
    model_api_status,
    stream_completion,
    stream_completion_native_progress,
)
from provider_config import api_provider_state, save_provider, test_provider
from context import (
    adaptive_output_token_limit,
    build_prompt,
    estimate_context_usage,
    reset_chat_context,
    sanitize_assistant_response,
)
from memory import (
    add_memory,
    delete_memory,
    edit_memory,
    list_memories,
    manual_context_to_memory,
    memory_export_zip,
    memory_stats,
    analyze_memory_bank,
    optimize_memory_bank,
    save_memory_bundle,
    pin_memory,
    set_memory_temperature,
)
from performance import record_chat_timing, performance_snapshot
from jobs import (
    interactive_request_finished,
    interactive_request_started,
    jobs_status,
    register_chat_operation,
    register_request_stop,
    schedule_response_maintenance,
    stop_all_chat_work,
    stop_request,
    unregister_chat_operation,
    unregister_request_stop,
)
from files import (
    cancel_file_job,
    clear_all_uploaded_files,
    compare_uploaded_lists,
    create_generated_file,
    delete_file_preset,
    delete_uploaded_file,
    direct_file_action,
    extract_generated_file_blocks,
    file_subsystem_state,
    file_worker_preview,
    pypdf_available,
    purge_generated_file,
    queue_file_jobs,
    read_generated_file,
    read_uploaded_file,
    recycle_generated_file,
    reorder_file_job,
    restore_generated_file_version,
    retry_file_job,
    save_file_preset,
    set_uploaded_file_active,
    shuffle_uploaded_file,
    start_file_job,
    pause_file_job,
    resume_file_job,
    store_uploaded_file,
    undelete_generated_file,
)
from browser import (
    LIVE_BROWSER,
    browser_assist,
    browser_assist_history,
    browser_live_assist_settings,
    browser_page_state,
    browser_status,
    fetch_page,
    playwright_available,
    store_page,
)
from browser_agent import (
    browser_agent_row,
    browser_agent_state,
    resume_browser_agent,
    start_browser_agent,
    stop_browser_agent,
)
from screen_reader import (
    screen_reader_job_row,
    screen_reader_probe,
    screen_reader_state,
    start_screen_reader_job,
    stop_screen_reader_job,
)
from deepsearch import (
    deepsearch_history,
    deepsearch_row,
    deepsearch_state,
    pause_deepsearch,
    resume_deepsearch,
    start_deepsearch,
    stop_deepsearch,
)
from updater import (
    check_for_update,
    download_update,
    install_update,
    updater_status,
)
from discord_bridge import (
    append_chat_message,
    discord_bridge_state,
    discord_public_config,
    discord_web_updates,
    restart_discord_bridge,
    save_discord_bridge_config,
    start_discord_bridge,
    stop_discord_bridge,
)
from aycd_bridge import process_aycd_chat
from aycd_commands import handle_aycd_command, aycd_dashboard_data
from mcp_manager import (
    MCP_DEFAULT_SERVER_ID,
    call_mcp_tool,
    mcp_context_message,
    mcp_public_state,
    mcp_recent_calls,
    mcp_tools,
    maybe_mcp_context,
    refresh_mcp_server,
    save_mcp_server,
)


_SETTING_KEYS = (
    "personality",
    "model_mode",
    "model",
    "fast_model",
    "deep_model",
    "notetaker_model",
    "context_window_tokens",
    "recent_context_messages",
    "summary_trigger_messages",
    "summary_keep_messages",
    "auto_memory",
    "auto_summary",
    "memory_retrieval_enabled",
    "memory_retrieval_limit",
    "adaptive_context_enabled",
    "use_browser",
    "include_page_screenshot",
    "live_screen_enabled",
    "live_assist_interval_enabled",
    "live_assist_interval_seconds",
    "live_assist_focus",
    "compute_mode",
    "gpu_offload_requested",
    "update_repo",
    "auto_update_check",
    "api_provider",
    "api_base_url",
    "api_model",
)

_BOOL_SETTINGS = {
    "auto_memory",
    "auto_summary",
    "memory_retrieval_enabled",
    "adaptive_context_enabled",
    "use_browser",
    "include_page_screenshot",
    "live_screen_enabled",
    "live_assist_interval_enabled",
    "auto_update_check",
}
_INT_SETTINGS = {
    "context_window_tokens": (4096, 262144),
    "recent_context_messages": (6, 80),
    "summary_trigger_messages": (10, 100),
    "summary_keep_messages": (4, 40),
    "memory_retrieval_limit": (3, 30),
    "live_assist_interval_seconds": (5, 3600),
}


def _json_load(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value not in (None, "") else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _query(path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urllib.parse.urlsplit(path)
    route = parsed.path
    # 3.0 exposes /api/v1 aliases without breaking the V2.7 frontend routes.
    if route == "/api/v1":
        route = "/api"
    elif route.startswith("/api/v1/"):
        route = "/api/" + route[len("/api/v1/"):]
    return route, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)


def _one(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return str(values[-1]) if values else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_chat_id(value: Any = None) -> int:
    return current_chat_id(value)


def _aycd_target_chat_id(value: Any = None) -> int:
    """Use an explicit chat, otherwise prefer the Discord-linked shared chat."""
    if value not in (None, ""):
        return current_chat_id(value)
    try:
        linked = int(discord_public_config().get("chat_id") or 0)
    except (TypeError, ValueError):
        linked = 0
    return current_chat_id(linked if linked > 0 else None)


def _aycd_route_status(value: Any = None) -> dict[str, Any]:
    target = _aycd_target_chat_id(value)
    discord_cfg = discord_public_config()
    discord_state = discord_bridge_state()
    with db_connect() as db:
        row = db.execute("SELECT title FROM chats WHERE id=?", (target,)).fetchone()
    linked_chat = int(discord_cfg.get("chat_id") or 0)
    same_chat = linked_chat > 0 and linked_chat == target
    state_name = str(discord_state.get("status") or discord_state.get("state") or "").casefold()
    return {
        "ok": True,
        "route": "/api/aycd/chat",
        "aliases": ["/api/integrations/aycd/chat"],
        "method": "POST",
        "host_scope": "localhost",
        "target_chat_id": target,
        "target_chat_title": str(row["title"] if row else "New chat"),
        "discord_linked_chat_id": linked_chat,
        "same_chat_as_discord": same_chat,
        "discord_bridge_status": str(discord_state.get("status") or discord_state.get("state") or "unknown"),
        "mirror_to_browser": True,
        "mirror_to_discord": bool(same_chat and state_name in {"online", "connected", "ready", "running"}),
        "detail": (
            "AYCD requests use the same Zeno message table as Web Chat and Discord."
            if same_chat else
            "AYCD is ready, but Discord is linked to a different/no chat. Set the same Linked chat ID to mirror AYCD into Discord."
        ),
    }


def _message_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["attachments"] = _json_load(item.pop("attachments_json", "[]"), [])
    item["citations"] = _json_load(item.pop("citations_json", "[]"), [])
    return item


def _chat_title_from_message(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return "New chat"
    return value[:72] + ("…" if len(value) > 72 else "")


def _settings_state() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _SETTING_KEYS:
        raw = get_setting(key, "")
        if key in _BOOL_SETTINGS:
            result[key] = str(raw).casefold() in {"1", "true", "yes", "on"}
        elif key in _INT_SETTINGS:
            result[key] = _as_int(raw)
        else:
            result[key] = raw
    return result


def _runtime_state(refresh: bool = False) -> dict[str, Any]:
    manager = get_model_runtime()
    try:
        status = manager.status(refresh=refresh)
        if hasattr(status, "to_dict"):
            runtime = status.to_dict()
        elif isinstance(status, dict):
            runtime = status
        else:
            runtime = {"status": str(status)}
    except Exception as exc:
        runtime = {"state": "unavailable", "error": str(exc)[:500]}
    try:
        runtime["api"] = model_api_status()
    except Exception as exc:
        runtime["api"] = {"error": str(exc)[:500]}
    return runtime


def _model_inventory_state(refresh: bool = False) -> dict[str, Any]:
    manager = get_model_runtime()
    manager_error = ""
    try:
        rows = manager.available_models(refresh=refresh)
    except Exception as exc:
        manager_error = str(exc)[:500]
        rows = []
    if not rows:
        try:
            # Notetaker maintains a deliberately small direct inventory path.
            # Use it as a resilience fallback for the Settings model picker too.
            from desktop_notetaker import _lmstudio_models_direct
            rows = _lmstudio_models_direct()
        except Exception as exc:
            detail = str(exc)[:500]
            return {"ok": False, "models": [], "error": detail or manager_error or "LM Studio model detection failed."}
    models: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, str):
            key = row
            display = row
            model_type = ""
            max_context = None
            loaded = False
        elif isinstance(row, dict):
            key = str(row.get("key") or row.get("id") or row.get("model") or "")
            display = str(row.get("display_name") or key)
            model_type = str(row.get("type") or "")
            max_context = row.get("max_context_length")
            loaded_instances = row.get("loaded_instances")
            loaded = bool(loaded_instances) if isinstance(loaded_instances, list) else False
        else:
            continue
        if key:
            folded = f"{key} {display} {model_type}".casefold()
            vision_likely = any(token in folded for token in (
                "qwen2-vl", "qwen3-vl", "vision", "vlm", "llava", "pixtral", "gemma-3", "minicpm-v"
            ))
            models.append({
                "key": key, "display_name": display or key, "type": model_type,
                "max_context_length": max_context, "loaded": loaded, "vision_likely": vision_likely,
            })
    return {"ok": True, "models": models, "count": len(models), "refreshed": bool(refresh)}


def _sources_used_in_answer(answer: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach only citations the finished answer explicitly references."""
    text = str(answer or "")
    used: list[dict[str, Any]] = []
    for source in sources:
        label = str(source.get("label") or "").strip()
        if label and re.search(rf"\[{re.escape(label)}\]", text):
            used.append(source)
    return used


def global_activity_state(chat_id: int | None = None) -> dict[str, Any]:
    """Return lightweight user-visible work state for the global bottom rail."""
    chat_id = _safe_chat_id(chat_id)
    tasks: list[dict[str, Any]] = []

    try:
        registry = jobs_status().get("chat_operation_registry") or {}
        active_events = int((registry.get("counts_by_chat") or {}).get(str(chat_id), 0) or 0)
    except Exception:
        active_events = 0
    if active_events:
        tasks.append({
            "kind": "chat", "label": "Zeno reply", "status": "running",
            "detail": "Generating a response", "percent": 0, "indeterminate": True, "priority": 100,
        })

    try:
        latest = deepsearch_state(chat_id).get("latest") or {}
        if str(latest.get("status") or "") in {"queued", "running", "paused", "stopping"}:
            tasks.append({
                "kind": "deepsearch", "label": "DeepSearch", "status": str(latest.get("status") or "running"),
                "detail": str(latest.get("detail") or latest.get("stage") or "Searching"),
                "percent": max(0, min(100, int(latest.get("progress") or 0))), "indeterminate": False, "priority": 70,
            })
    except Exception:
        pass

    try:
        latest = browser_agent_state(chat_id).get("latest") or {}
        if str(latest.get("status") or "") in {"queued", "running", "stopping"}:
            step = max(0, int(latest.get("step") or 0)); max_steps = max(1, int(latest.get("max_steps") or 20))
            percent = min(95, 8 + round((step / max_steps) * 84))
            tasks.append({
                "kind": "browser_agent", "label": "Browser Agent", "status": str(latest.get("status") or "running"),
                "detail": str(latest.get("detail") or latest.get("current_title") or "Working in browser"),
                "percent": percent, "indeterminate": False, "priority": 80,
            })
    except Exception:
        pass

    try:
        latest = screen_reader_state(chat_id).get("latest") or {}
        if str(latest.get("status") or "") in {"queued", "running", "fetching", "analyzing", "stopping"}:
            tasks.append({
                "kind": "screen_reader", "label": "Screen Reader", "status": str(latest.get("status") or "running"),
                "detail": str(latest.get("detail") or "Reading screen/channel content"),
                "percent": max(0, min(100, int(latest.get("progress") or 0))), "indeterminate": False, "priority": 60,
            })
    except Exception:
        pass

    try:
        with db_connect() as db:
            row = db.execute(
                "SELECT status,stage,detail,progress FROM file_jobs WHERE chat_id=? "
                "AND status IN ('preview_ready','queued','running','cancelling','pausing','paused','interrupted') "
                "ORDER BY CASE WHEN status='running' THEN 0 WHEN status='queued' THEN 1 ELSE 2 END,updated_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        if row:
            item = dict(row)
            tasks.append({
                "kind": "file", "label": "File Worker", "status": str(item.get("status") or "running"),
                "detail": str(item.get("detail") or item.get("stage") or "Processing file"),
                "percent": max(0, min(100, int(item.get("progress") or 0))), "indeterminate": False, "priority": 65,
            })
    except Exception:
        pass

    try:
        from desktop_notetaker import notetaker_activity_state
        nt = notetaker_activity_state()
        if nt.get("analyzing"):
            phase = str(nt.get("phase") or "analyzing").casefold()
            phase_percent = {"queued": 8, "capturing": 20, "preparing": 36, "analyzing": 68, "saving": 90, "posting": 94}.get(phase, 55)
            tasks.append({
                "kind": "notetaker", "label": "Desktop Notetaker", "status": phase,
                "detail": str(nt.get("detail") or "Analyzing the current desktop"),
                "percent": phase_percent, "indeterminate": phase not in {"queued", "capturing", "preparing", "analyzing", "saving", "posting"}, "priority": 90,
            })
    except Exception:
        pass

    tasks.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    primary = tasks[0] if tasks else None
    return {"active": bool(tasks), "primary": primary, "tasks": tasks, "count": len(tasks), "time": now()}


def snapshot_state(chat_id: int | None = None, message_limit: int = 300) -> dict[str, Any]:
    chat_id = _safe_chat_id(chat_id)
    try:
        requested_limit = int(message_limit or 300)
    except (TypeError, ValueError):
        requested_limit = 300
    message_limit = max(100, min(requested_limit, 3000))
    with db_connect() as db:
        chat_row = db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        chats = [dict(row) for row in db.execute(
            "SELECT id,title,archived,created_at,updated_at FROM chats ORDER BY archived,updated_at DESC,id DESC LIMIT 500"
        ).fetchall()]
        total_messages = int(db.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
        ).fetchone()[0])
        messages = [_message_dict(row) for row in db.execute(
            "SELECT id,role,content,created_at,attachments_json,citations_json,source,source_label,external_id "
            "FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, message_limit)
        ).fetchall()][::-1]
    files_state = file_subsystem_state(chat_id)
    oldest_loaded_id = int(messages[0].get("id") or 0) if messages else 0
    return {
        "app": {"name": "Zeno", "version": APP_VERSION, "host": APP_HOST, "port": APP_PORT},
        "chat": dict(chat_row) if chat_row else {"id": chat_id, "title": "New chat", "summary": ""},
        "chats": chats,
        "messages": messages,
        "message_history": {
            "loaded": len(messages),
            "total": total_messages,
            "limit": message_limit,
            "has_older": total_messages > len(messages),
            "oldest_loaded_id": oldest_loaded_id,
        },
        "settings": _settings_state(),
        "context": estimate_context_usage(chat_id),
        "memory": list_memories(limit=250),
        "memory_stats": memory_stats("current conversation"),
        "memory_folder": str(MEMORY_DIR),
        **files_state,
        "pages": browser_page_state(chat_id),
        "browser": browser_status(),
        "browser_assist": browser_assist_history(chat_id),
        "browser_agent": browser_agent_state(chat_id),
        "screen_reader": screen_reader_state(chat_id),
        "deepsearch": deepsearch_state(chat_id),
        "discord": discord_public_config(),
        "discord_state": discord_bridge_state(),
        "aycd": _aycd_route_status(chat_id),
        "aycd_dashboard": aycd_dashboard_data(chat_id),
        "mcp": mcp_public_state(),
        "mcp_calls": mcp_recent_calls(chat_id, 20),
        "jobs": jobs_status(),
        "runtime": _runtime_state(False),
        "model_inventory": _model_inventory_state(False),
        "performance": performance_snapshot(),
        "update": updater_status(),
        "capabilities": {
            "playwright": playwright_available(),
            "pypdf": pypdf_available(),
            "discord": bool(discord_bridge_state().get("available", True)),
            "mcp": bool(mcp_public_state().get("available")),
        },
    }


def _workspace_for(chat_id: int, page_id: int | None = None) -> dict[str, Any]:
    """Return the editable public-frontend workspace for compatibility."""
    with db_connect() as db:
        row = db.execute("SELECT * FROM workspaces WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            return dict(row)
        source = None
        if page_id:
            source = db.execute("SELECT * FROM pages WHERE id=? AND chat_id=?", (int(page_id), chat_id)).fetchone()
    if source:
        return {
            "chat_id": chat_id,
            "html": str(source["raw_html"] or ""),
            "css": str(source["css_code"] or ""),
            "js": str(source["js_code"] or ""),
            "source_page_id": int(source["id"]),
            "updated_at": 0,
        }
    return {"chat_id": chat_id, "html": "", "css": "", "js": "", "source_page_id": None, "updated_at": 0}


def _workspace_index(html: str, css: str, js: str) -> str:
    document = str(html or "").strip() or "<!doctype html><html><head></head><body></body></html>"
    style_tag = "<style>\n" + str(css or "") + "\n</style>"
    script_tag = "<script>\n" + str(js or "").replace("</script", "<\\/script") + "\n</script>"
    if re.search(r"</head\s*>", document, re.I):
        document = re.sub(r"</head\s*>", style_tag + "</head>", document, count=1, flags=re.I)
    else:
        document = style_tag + document
    if re.search(r"</body\s*>", document, re.I):
        document = re.sub(r"</body\s*>", script_tag + "</body>", document, count=1, flags=re.I)
    else:
        document += script_tag
    return document


def _decode_data_url(value: str) -> bytes:
    text = str(value or "")
    if "," in text and text.casefold().startswith("data:"):
        _, text = text.split(",", 1)
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise ValueError("Upload data is not valid base64.") from exc


def _progress_overall(phase: str, percent: float | None, output_chars: int = 0) -> int:
    phase = str(phase or "").casefold()
    local = 0.0 if percent is None else max(0.0, min(100.0, float(percent)))
    if phase in {"starting", "building_context"}:
        return 2
    if phase == "queued":
        return 5
    if phase in {"mcp_connecting", "mcp_planning"}:
        return min(18, 8 + round(local * 0.10))
    if phase in {"mcp_tool", "tool"}:
        return min(28, 16 + round(local * 0.12))
    if phase == "connecting":
        return 10
    if phase == "loading_model":
        return min(22, 10 + round(local * 0.12))
    if phase == "processing_prompt":
        return min(60, 22 + round(local * 0.38))
    if phase in {"reasoning", "generating"}:
        # Output length only advances a conservative heuristic. 100 remains EOS-only.
        return min(96, 64 + int(32 * min(1.0, max(0, output_chars) / 3600.0)))
    if phase == "complete":
        return 100
    return 8


def _chat_update_title(chat_id: int, user_text: str) -> None:
    with db_connect() as db:
        row = db.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
        if row and str(row["title"] or "").strip().casefold() in {"", "new chat", "imported conversation"}:
            db.execute("UPDATE chats SET title=?,updated_at=? WHERE id=?", (_chat_title_from_message(user_text), now(), chat_id))


def _prepare_chat_turn(chat_id: int, payload: dict[str, Any]) -> tuple[str, int, int, list[int]]:
    """Return user text, user message id, history-before id, file ids."""
    content = str(payload.get("content") or "").strip()
    file_ids = [_as_int(item) for item in list(payload.get("file_ids") or []) if _as_int(item) > 0]
    edit_id = _as_int(payload.get("edit_message_id"))
    regenerate = bool(payload.get("regenerate"))
    history_before_id = 0

    if edit_id:
        if not content:
            raise ValueError("Edited message cannot be empty.")
        with db_connect() as db:
            row = db.execute("SELECT id,role FROM messages WHERE id=? AND chat_id=?", (edit_id, chat_id)).fetchone()
            if not row or str(row["role"]) != "user":
                raise ValueError("The message being edited was not found.")
            db.execute("UPDATE messages SET content=?,created_at=? WHERE id=?", (content, now(), edit_id))
            db.execute("DELETE FROM messages WHERE chat_id=? AND id>?", (chat_id, edit_id))
            db.execute("UPDATE chats SET updated_at=? WHERE id=?", (now(), chat_id))
        user_message_id = edit_id
        history_before_id = edit_id
    elif regenerate:
        with db_connect() as db:
            row = db.execute(
                "SELECT id,content FROM messages WHERE chat_id=? AND role='user' ORDER BY id DESC LIMIT 1", (chat_id,)
            ).fetchone()
            if not row:
                raise ValueError("There is no user message to regenerate.")
            user_message_id = int(row["id"])
            content = str(row["content"])
            db.execute("DELETE FROM messages WHERE chat_id=? AND id>?", (chat_id, user_message_id))
            db.execute("UPDATE chats SET updated_at=? WHERE id=?", (now(), chat_id))
        history_before_id = user_message_id
    else:
        if not content:
            raise ValueError("Message cannot be empty.")
        user_message_id = append_chat_message(chat_id, "user", content, source="web_chat", attachments=file_ids)
        history_before_id = user_message_id
        _chat_update_title(chat_id, content)
    return content, user_message_id, history_before_id, file_ids


class ZenoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Browsers routinely abandon keep-alive sockets during refresh/navigation.
        # socketserver's default handler prints a full synchronous traceback to stderr,
        # which can block on Windows Console Host and make unrelated requests appear hung.
        _exc_type, exc, _tb = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in {10038, 10053, 10054}:
            return
        console_log(f"HTTP worker error from {client_address}: {exc or _exc_type}")


class AppHandler(BaseHTTPRequestHandler):
    server_version = f"Zeno/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        route, _ = _query(self.path)
        if route in {
            "/api/chat/check", "/api/browser/screenshot", "/api/browser/status",
            "/api/deepsearch/status", "/api/activity", "/api/notetaker/status",
        }:
            return
        console_log(f"[{self.log_date_time_string()}] {fmt % args}")

    @staticmethod
    def _client_disconnected(exc: BaseException) -> bool:
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return True
        return isinstance(exc, OSError) and getattr(exc, "winerror", None) in {10038, 10053, 10054}

    def _headers(self, status: int, content_type: str, length: int | None = None, *, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, *, cache: str = "no-store", disposition: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception as exc:
            if not self._client_disconnected(exc):
                raise

    def send_json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_bytes(data, "application/json; charset=utf-8", status)

    def send_error_json(self, exc: Exception, status: int = 400) -> None:
        error_type = "error"
        text = str(exc) or exc.__class__.__name__
        if "context" in text.casefold() and ("length" in text.casefold() or "token" in text.casefold()):
            error_type = "context_size"
        self.send_json({"ok": False, "error": text[:3000], "error_type": error_type}, status)

    def read_json(self) -> dict[str, Any]:
        length = _as_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BYTES:
            raise ValueError(f"Request is larger than {MAX_REQUEST_BYTES // 1_000_000} MB.")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Request JSON must be an object.")
        return value

    def _ndjson_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _ndjson(self, value: Any) -> None:
        data = (json.dumps(value, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        route, query = _query(self.path)
        try:
            if route in {"/", "/index.html", "/app.html"}:
                if not HTML_PATH.exists():
                    raise FileNotFoundError("app.html is missing beside Zeno.")
                self.send_bytes(HTML_PATH.read_bytes(), "text/html; charset=utf-8", cache="no-cache")
                return
            if route in {"/zeno-icon.png", "/favicon.ico"}:
                if ICON_PATH.exists():
                    self.send_bytes(ICON_PATH.read_bytes(), "image/png", cache="public, max-age=3600")
                else:
                    self.send_bytes(b"", "image/png", status=404)
                return
            if route == "/zeno-wallpaper.svg":
                if WALLPAPER_PATH.exists():
                    self.send_bytes(WALLPAPER_PATH.read_bytes(), "image/svg+xml; charset=utf-8", cache="public, max-age=3600")
                else:
                    self.send_bytes(b"", "image/svg+xml", status=404)
                return
            if route == "/api/health":
                self.send_json({"ok": True, "app": "Zeno", "version": APP_VERSION, "time": now()})
                return
            if route == "/api/state":
                self.send_json(snapshot_state(
                    _as_int(_one(query, "chat_id")),
                    _as_int(_one(query, "message_limit"), 300),
                ))
                return
            if route == "/api/activity":
                self.send_json(global_activity_state(_as_int(_one(query, "chat_id"))))
                return
            if route == "/api/chat/check":
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                after_id = _as_int(_one(query, "after_id"))
                self.send_json({"messages": discord_web_updates(chat_id, after_id)})
                return
            if route == "/api/runtime/status":
                self.send_json({"runtime": _runtime_state(_one(query, "refresh") == "1")})
                return
            if route in {"/api/providers", "/api/providers/status"}:
                self.send_json(api_provider_state())
                return
            if route == "/api/browser/status":
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                self.send_json({
                    "browser": browser_status(),
                    "messages": browser_assist_history(chat_id),
                    "live_assist": browser_live_assist_settings(),
                    "agent": browser_agent_state(chat_id).get("latest"),
                    "agent_history": browser_agent_state(chat_id).get("history", []),
                    "screen_reader_job": screen_reader_state(chat_id).get("latest"),
                    "screen_reader_history": screen_reader_state(chat_id).get("history", []),
                })
                return
            if route == "/api/browser/screenshot":
                raw = LIVE_BROWSER.screenshot()
                if not raw:
                    self.send_bytes(b"", "image/jpeg", status=404)
                else:
                    self.send_bytes(raw, "image/jpeg", cache="no-store")
                return
            if route == "/api/deepsearch/status":
                job_id = _one(query, "job_id")
                if job_id:
                    job = deepsearch_row(job_id)
                    if not job:
                        raise ValueError("DeepSearch job not found.")
                    self.send_json({"job": job})
                else:
                    chat_id = _safe_chat_id(_one(query, "chat_id"))
                    self.send_json(deepsearch_state(chat_id))
                return
            if route == "/api/discord/status":
                self.send_json({"bridge": discord_bridge_state(), "config": discord_public_config()})
                return
            if route in {"/api/screen-reader/probe", "/api/browser/reader/probe"}:
                self.send_json(screen_reader_probe())
                return
            if route in {"/api/aycd/status", "/api/integrations/aycd/status"}:
                self.send_json(_aycd_route_status(_one(query, "chat_id") or None))
                return
            if route in {"/api/aycd/dashboard", "/api/integrations/aycd/dashboard"}:
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                self.send_json(aycd_dashboard_data(chat_id))
                return
            if route in {"/api/mcp/status", "/api/integrations/mcp/status"}:
                self.send_json(mcp_public_state())
                return
            if route in {"/api/mcp/tools", "/api/integrations/mcp/tools"}:
                server_id = _one(query, "server") or MCP_DEFAULT_SERVER_ID
                self.send_json({"ok": True, "server_id": server_id, "tools": mcp_tools(server_id, refresh=_one(query, "refresh") == "1")})
                return
            if route in {"/api/mcp/calls", "/api/integrations/mcp/calls"}:
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                self.send_json({"ok": True, "calls": mcp_recent_calls(chat_id, _as_int(_one(query, "limit"), 30))})
                return
            if route == "/api/browser/agent/status":
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                job_id = _one(query, "job_id")[:80]
                agent = browser_agent_row(job_id) if job_id else browser_agent_state(chat_id).get("latest")
                if agent and int(agent.get("chat_id") or 0) != chat_id:
                    agent = None
                self.send_json({"agent": agent})
                return
            if route in {"/api/browser/reader/status", "/api/discord/channel/status"}:
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                job_id = _one(query, "job_id")[:80]
                job = screen_reader_job_row(job_id) if job_id else screen_reader_state(chat_id).get("latest")
                if job and int(job.get("chat_id") or 0) != chat_id:
                    job = None
                self.send_json({"job": job})
                return
            if route == "/api/browser/reader/history":
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                self.send_json({"history": screen_reader_state(chat_id).get("history", [])})
                return
            if route == "/api/diagnostics":
                self.send_json({
                    "app": {"version": APP_VERSION},
                    "runtime": _runtime_state(False),
                    "jobs": jobs_status(),
                    "browser": browser_status(),
                    "discord": discord_bridge_state(),
                    "aycd": _aycd_route_status(_one(query, "chat_id") or None),
                    "mcp": mcp_public_state(),
                    "update": updater_status(),
                    "capabilities": {"playwright": playwright_available(), "pypdf": pypdf_available()},
                })
                return
            if route == "/api/selfdev/status":
                self.send_json({"enabled": False, "status": "removed", "detail": "Self-Dev is not part of the Zeno 3.0 GUI/runtime."})
                return
            if route == "/api/update/check":
                repo = _one(query, "repo") or get_setting("update_repo", "brannod/zeno")
                self.send_json(check_for_update(repo))
                return
            if route == "/api/update/status":
                self.send_json(updater_status())
                return
            if route == "/api/workspace":
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                page_id = _as_int(_one(query, "page_id")) or None
                self.send_json({"workspace": _workspace_for(chat_id, page_id)})
                return
            if route == "/api/export":
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                ws = _workspace_for(chat_id)
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("index.html", _workspace_index(ws["html"], ws["css"], ws["js"]))
                    archive.writestr("styles.css", ws["css"])
                    archive.writestr("script.js", ws["js"])
                    archive.writestr("README.txt", "Exported from Zeno 3.0. Contains public frontend code only.\n")
                self.send_bytes(output.getvalue(), "application/zip", disposition='attachment; filename="Zeno-code-workspace.zip"')
                return
            if route == "/api/page/screenshot":
                page_id = _as_int(_one(query, "id"))
                with db_connect() as db:
                    row = db.execute("SELECT screenshot_path FROM pages WHERE id=?", (page_id,)).fetchone()
                if not row or not str(row["screenshot_path"] or "").strip():
                    raise ValueError("Screenshot not found.")
                candidate = (BASE_DIR / str(row["screenshot_path"])).resolve()
                if BASE_DIR.resolve() not in candidate.parents or not candidate.is_file():
                    raise ValueError("Screenshot not found.")
                self.send_bytes(candidate.read_bytes(), "image/jpeg", cache="no-store")
                return
            if route == "/api/file":
                file_id = _as_int(_one(query, "id"))
                raw, meta = read_uploaded_file(file_id)
                mime = str(meta.get("mime") or "application/octet-stream")
                disposition = ""
                if _one(query, "download") == "1":
                    disposition = f'attachment; filename="{Path(str(meta.get("name") or "file")).name}"'
                self.send_bytes(raw, mime, disposition=disposition)
                return
            if route == "/api/generated-file":
                output_id = _as_int(_one(query, "id"))
                raw, meta = read_generated_file(output_id)
                disposition = f'attachment; filename="{Path(str(meta.get("name") or "zeno-output")).name}"'
                self.send_bytes(raw, str(meta.get("mime") or "application/octet-stream"), disposition=disposition)
                return
            if route == "/api/memory/export":
                chat_id = _safe_chat_id(_one(query, "chat_id"))
                raw = memory_export_zip(chat_id)
                self.send_bytes(raw, "application/zip", disposition='attachment; filename="zeno-memory.zip"')
                return
            if route == "/api/models":
                refresh_models = str(_one(query, "refresh") or "").casefold() in {"1", "true", "yes", "on"}
                self.send_json(_model_inventory_state(refresh_models))
                return
            # ZENO_DESKTOP_NOTETAKER_2026_08_28
            if route == "/api/notetaker/status":
                from desktop_notetaker import notetaker_status
                self.send_json(notetaker_status(start_if_enabled=False))
                return
            self.send_json({"error": "Route not found", "route": route}, 404)
        except FileNotFoundError as exc:
            self.send_error_json(exc, 404)
        except ValueError as exc:
            self.send_error_json(exc, 400)
        except Exception as exc:
            self.send_error_json(exc, 500)

    def do_POST(self) -> None:  # noqa: N802
        route, _query_values = _query(self.path)
        if route == "/api/chat/stream":
            self._handle_chat_stream()
            return
        try:
            payload = self.read_json()
            result = self._dispatch_post(route, payload)
            self.send_json(result)
        except ValueError as exc:
            self.send_error_json(exc, 400)
        except PermissionError as exc:
            self.send_error_json(exc, 403)
        except Exception as exc:
            self.send_error_json(exc, 500)

    def _dispatch_post(self, route: str, payload: dict[str, Any]) -> Any:
        chat_id = _safe_chat_id(payload.get("chat_id"))
        route = {
            "/api/browser/agent/start": "/api/browser-agent/start",
            "/api/browser/agent/stop": "/api/browser-agent/stop",
            "/api/browser/agent/resume": "/api/browser-agent/resume",
            "/api/browser/reader/start": "/api/screen-reader/start",
            "/api/browser/reader/stop": "/api/screen-reader/stop",
        }.get(route, route)

        # MCP client hub --------------------------------------------------------
        if route in {"/api/mcp/config", "/api/integrations/mcp/config"}:
            values = payload.get("server") if isinstance(payload.get("server"), dict) else payload
            return {"ok": True, "server": save_mcp_server(dict(values or {}))}
        if route in {"/api/mcp/refresh", "/api/mcp/test", "/api/integrations/mcp/refresh"}:
            server_id = str(payload.get("server_id") or MCP_DEFAULT_SERVER_ID)
            return {"ok": True, "server": refresh_mcp_server(server_id)}
        if route in {"/api/mcp/call", "/api/integrations/mcp/call"}:
            target_chat = _safe_chat_id(payload.get("chat_id"))
            server_id = str(payload.get("server_id") or MCP_DEFAULT_SERVER_ID)
            tool_name = str(payload.get("tool") or "").strip()
            arguments = payload.get("arguments") or {}
            if not tool_name:
                raise ValueError("MCP tool name is required.")
            if not isinstance(arguments, dict):
                raise ValueError("MCP tool arguments must be a JSON object.")
            return call_mcp_tool(target_chat, server_id, tool_name, arguments, source="manual")

        # AYCD shared-chat bridge ------------------------------------------------
        if route in {"/api/aycd/chat", "/api/integrations/aycd/chat"}:
            target_chat = _aycd_target_chat_id(payload.get("chat_id"))
            content = str(
                payload.get("message")
                or payload.get("content")
                or payload.get("prompt")
                or ""
            ).strip()
            result = process_aycd_chat(
                target_chat,
                content,
                external_id=str(payload.get("external_id") or payload.get("request_id") or ""),
                source_label=str(payload.get("source_label") or "AYCD"),
            )
            result["route_status"] = _aycd_route_status(target_chat)
            return result

        # ZENO_DESKTOP_NOTETAKER_2026_08_28
        if route == "/api/notetaker/settings":
            from desktop_notetaker import update_notetaker_settings
            return update_notetaker_settings(
                enabled=payload.get("enabled") if "enabled" in payload else None,
                interval_seconds=payload.get("interval_seconds") if "interval_seconds" in payload else None,
                focus=payload.get("focus") if "focus" in payload else None,
                monitor=payload.get("monitor") if "monitor" in payload else None,
                post_to_chat=payload.get("post_to_chat") if "post_to_chat" in payload else None,
                skip_unchanged=payload.get("skip_unchanged") if "skip_unchanged" in payload else None,
                model=payload.get("model") if "model" in payload else None,
                detail_level=payload.get("detail_level") if "detail_level" in payload else None,
            )
        if route == "/api/notetaker/analyze":
            from desktop_notetaker import request_notetaker_analysis
            return request_notetaker_analysis(payload.get("chat_id"))
        if route == "/api/notetaker/session/end":
            from desktop_notetaker import end_notetaker_session
            return end_notetaker_session(payload.get("chat_id"))

        # Chat lifecycle -------------------------------------------------
        if route == "/api/chat/new":
            timestamp = now()
            with db_connect() as db:
                cursor = db.execute("INSERT INTO chats(title,created_at,updated_at) VALUES('New chat',?,?)", (timestamp, timestamp))
                new_id = int(cursor.lastrowid)
            set_setting("active_chat_id", str(new_id))
            return {"ok": True, "chat_id": new_id, "state": snapshot_state(new_id)}
        if route == "/api/chat/switch":
            target = _safe_chat_id(payload.get("chat_id"))
            set_setting("active_chat_id", str(target))
            return {"ok": True, "state": snapshot_state(target)}
        if route == "/api/chat/rename":
            title = re.sub(r"\s+", " ", str(payload.get("title") or "")).strip()[:120]
            if not title:
                raise ValueError("Chat title cannot be empty.")
            with db_connect() as db:
                db.execute("UPDATE chats SET title=?,updated_at=? WHERE id=?", (title, now(), chat_id))
            return {"ok": True, "title": title}
        if route == "/api/chat/archive":
            archived = 1 if bool(payload.get("archived", True)) else 0
            with db_connect() as db:
                db.execute("UPDATE chats SET archived=?,updated_at=? WHERE id=?", (archived, now(), chat_id))
            return {"ok": True, "archived": bool(archived)}
        if route == "/api/chat/reset":
            stop_all_chat_work(chat_id)
            return {"ok": True, **reset_chat_context(chat_id, "web_chat", "Zeno")}
        if route == "/api/stop":
            request_id = str(payload.get("request_id") or "")
            stopped = stop_request(request_id) if request_id else False
            stopped_work = stop_all_chat_work(chat_id) if bool(payload.get("all")) else {}
            return {"ok": True, "request_stopped": stopped, "work": stopped_work}
        if route == "/api/context/save":
            return {"ok": True, "result": manual_context_to_memory(chat_id)}

        if route == "/api/model-mode":
            mode = str(payload.get("model_mode") or "balanced").casefold()
            if mode not in {"fast", "balanced", "deep"}:
                raise ValueError("Model mode must be Fast, Balanced, or Deep.")
            set_setting("model_mode", mode)
            return {"ok": True, "model_mode": mode}
        if route == "/api/memory/save":
            return {"ok": True, "saved": save_memory_bundle(chat_id, "manual save")}
        if route == "/api/workspace/save":
            html = str(payload.get("html") or "")[:400_000]
            css = str(payload.get("css") or "")[:240_000]
            js = str(payload.get("js") or "")[:240_000]
            source_page_id = _as_int(payload.get("source_page_id")) or None
            with db_connect() as db:
                db.execute(
                    "INSERT INTO workspaces(chat_id,html,css,js,source_page_id,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET html=excluded.html,css=excluded.css,js=excluded.js,"
                    "source_page_id=excluded.source_page_id,updated_at=excluded.updated_at",
                    (chat_id, html, css, js, source_page_id, now()),
                )
            return {"ok": True}
        if route == "/api/shutdown":
            saved = save_memory_bundle(None, "exit checkpoint") if bool(payload.get("save_context", True)) else None
            threading.Thread(target=lambda: (time.sleep(0.25), self.server.shutdown()), daemon=True, name="ZenoShutdown").start()
            return {"ok": True, "saved": saved}

        # Settings/runtime -----------------------------------------------
        if route == "/api/settings":
            updates = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
            saved: dict[str, Any] = {}
            for key, value in updates.items():
                if key not in _SETTING_KEYS:
                    continue
                if key == "model_mode":
                    value = str(value).casefold()
                    if value not in {"fast", "balanced", "deep"}:
                        raise ValueError("Model mode must be Fast, Balanced, or Deep.")
                elif key == "compute_mode":
                    value = str(value).casefold()
                    if value not in {"auto", "max_gpu", "partial_gpu", "cpu_only"}:
                        raise ValueError("Compute mode must be Auto, Max GPU, Partial GPU, or CPU Only.")
                elif key == "gpu_offload_requested":
                    value = str(value).strip().casefold()
                    if value != "auto":
                        try:
                            ratio = float(value)
                        except ValueError as exc:
                            raise ValueError("GPU offload must be auto or a ratio from 0.0 through 1.0.") from exc
                        if not 0.0 <= ratio <= 1.0:
                            raise ValueError("GPU offload ratio must be from 0.0 through 1.0.")
                        value = str(ratio)
                elif key in _BOOL_SETTINGS:
                    value = "true" if bool(value) else "false"
                elif key in _INT_SETTINGS:
                    low, high = _INT_SETTINGS[key]
                    ivalue = _as_int(value, -1)
                    if not low <= ivalue <= high:
                        raise ValueError(f"{key} must be between {low} and {high}.")
                    value = str(ivalue)
                else:
                    value = str(value)
                set_setting(key, value)
                saved[key] = value
            return {"ok": True, "saved": saved, "settings": _settings_state()}
        if route in {"/api/providers/config", "/api/providers/save"}:
            values = payload.get("provider") if isinstance(payload.get("provider"), dict) else payload
            result = save_provider(values if isinstance(values, dict) else {})
            return result
        if route == "/api/providers/test":
            provider_id = str(payload.get("id") or payload.get("provider") or "").strip() or None
            return test_provider(provider_id)
        if route in {"/api/runtime/load", "/api/runtime/reload"}:
            manager = get_model_runtime()
            requested = manager.requested_config(
                model_mode=str(payload.get("model_mode") or model_mode_setting()),
                context_length=_as_int(payload.get("context_length")) or None,
                compute_mode=str(payload.get("compute_mode") or get_setting("compute_mode", "auto")),
                gpu_offload_requested=str(payload.get("gpu_offload_requested") or get_setting("gpu_offload_requested", "auto")),
                parallel=max(1, min(8, _as_int(payload.get("parallel"), 1))),
            )
            status = manager.reload(requested) if route.endswith("reload") else manager.load(requested)
            return {"ok": True, "runtime": status.to_dict() if hasattr(status, "to_dict") else status}
        if route == "/api/runtime/unload":
            status = get_model_runtime().unload(payload.get("instance_id") or None, allow_unmanaged=False)
            return {"ok": True, "runtime": status.to_dict() if hasattr(status, "to_dict") else status}

        # Memory ---------------------------------------------------------
        if route == "/api/memory":
            return {"ok": True, "memory": add_memory(str(payload.get("content") or ""), chat_id=chat_id)}
        if route == "/api/memory/edit":
            return {"ok": True, "memory": edit_memory(_as_int(payload.get("id")), str(payload.get("content") or ""), chat_id=chat_id)}
        if route == "/api/memory/pin":
            return {"ok": True, "memory": pin_memory(_as_int(payload.get("id")), bool(payload.get("pinned", True)))}
        if route == "/api/memory/temperature":
            return {"ok": True, "memory": set_memory_temperature(_as_int(payload.get("id")), str(payload.get("temperature") or "warm"))}
        if route == "/api/memory/delete":
            return {"ok": True, "result": delete_memory(_as_int(payload.get("id")), chat_id=chat_id)}
        if route == "/api/memory/analyze":
            return {"ok": True, "analysis": analyze_memory_bank(_as_int(payload.get("limit"), 1200))}
        if route == "/api/memory/optimize":
            return {"ok": True, "result": optimize_memory_bank(
                dry_run=bool(payload.get("dry_run", False)),
                limit=_as_int(payload.get("limit"), 2000),
            )}

        # Files ----------------------------------------------------------
        if route == "/api/upload":
            raw = _decode_data_url(str(payload.get("data") or ""))
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError(f"Files are limited to {MAX_UPLOAD_BYTES // 1_000_000} MB.")
            name = str(payload.get("name") or "upload.bin")
            mime = str(payload.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream")
            return {"ok": True, "file": store_uploaded_file(chat_id, name, mime, raw)}
        if route in {"/api/files/clear-all", "/api/file/clear-all"}:
            return {"ok": True, "cleared": clear_all_uploaded_files()}
        if route == "/api/file/toggle":
            return {"ok": True, "file": set_uploaded_file_active(chat_id, _as_int(payload.get("id")), bool(payload.get("active", True)))}
        if route == "/api/file/delete":
            return {"ok": True, "result": delete_uploaded_file(chat_id, _as_int(payload.get("id")))}
        if route == "/api/file/shuffle":
            return {"ok": True, "attachment": shuffle_uploaded_file(chat_id, _as_int(payload.get("id")))}
        if route in {"/api/file/list-compare", "/api/file/compare-list"}:
            file_a = _as_int(payload.get("file_a"))
            file_b = _as_int(payload.get("file_b"))
            text_a = payload.get("text_a")
            text_b = payload.get("text_b")
            if isinstance(text_a, str) and text_a.strip():
                stored_a = store_uploaded_file(
                    chat_id, str(payload.get("name_a") or "list_a_pasted.txt")[:180],
                    "text/plain", text_a.encode("utf-8"),
                )
                file_a = int(stored_a["id"])
            if isinstance(text_b, str) and text_b.strip():
                stored_b = store_uploaded_file(
                    chat_id, str(payload.get("name_b") or "list_b_pasted.txt")[:180],
                    "text/plain", text_b.encode("utf-8"),
                )
                file_b = int(stored_b["id"])
            if file_a <= 0 or file_b <= 0:
                raise ValueError("Paste both lists or choose two uploaded text files.")
            result = compare_uploaded_lists(chat_id, file_a, file_b)
            mode = str(payload.get("mode") or "duplicates").casefold()
            if mode not in {"duplicates", "missing"}:
                mode = "duplicates"
            result["mode"] = mode
            counts = result.get("counts") or {}
            output_lines = [
                "Zeno list comparison",
                f"List A: {result.get('files', {}).get('a', 'pasted text')} · {int(counts.get('lines_a', 0)):,} lines / {int(counts.get('unique_a', 0)):,} unique",
                f"List B: {result.get('files', {}).get('b', 'pasted text')} · {int(counts.get('lines_b', 0)):,} lines / {int(counts.get('unique_b', 0)):,} unique",
                "",
            ]
            if mode == "missing":
                output_lines += [f"Only in List A ({int(counts.get('only_a', 0)):,}):", *(result.get("only_a") or ["(none)"]), "", f"Only in List B ({int(counts.get('only_b', 0)):,}):", *(result.get("only_b") or ["(none)"])]
            else:
                output_lines += [f"Shared entries / duplicates across both lists ({int(counts.get('shared', 0)):,}):", *(result.get("duplicates") or ["(none)"])]
            result["output"] = create_generated_file(
                chat_id, f"list_{'missing' if mode == 'missing' else 'duplicates'}.txt",
                "\n".join(output_lines), source_job_id="list-compare",
            )
            return {"ok": True, "result": result}
        if route == "/api/generated-file/delete":
            return {"ok": True, "file": recycle_generated_file(chat_id, _as_int(payload.get("id")))}
        if route == "/api/generated-file/undelete":
            return {"ok": True, "file": undelete_generated_file(chat_id, _as_int(payload.get("id")))}
        if route == "/api/generated-file/purge":
            return {"ok": True, "file": purge_generated_file(chat_id, _as_int(payload.get("id")))}
        if route == "/api/generated-file/version-restore":
            return {"ok": True, "file": restore_generated_file_version(chat_id, _as_int(payload.get("id")))}
        if route == "/api/file-preset/save":
            values = payload.get("preset") if isinstance(payload.get("preset"), dict) else payload
            return {"ok": True, "preset": save_file_preset(
                preset_id=_as_int(values.get("id")), name=str(values.get("name") or ""),
                mode=str(values.get("mode") or ""), instruction=str(values.get("instruction") or ""),
                preserve_structure=bool(values.get("preserve_structure", True)),
                exact_multiset=values.get("exact_multiset"), required_delimiter=str(values.get("required_delimiter") or ""),
                preserve_aycd_values=values.get("preserve_aycd_values"),
            )}
        if route == "/api/file-preset/delete":
            return {"ok": True, "result": delete_file_preset(_as_int(payload.get("id")))}
        if route == "/api/file-worker/preview":
            source_text = payload.get("source_text")
            file_id = _as_int(payload.get("file_id"))
            # Pasted text uses the same validated File Worker pipeline as an
            # uploaded text file. Store it locally so the approved job can be
            # resumed/cancelled safely and can produce a normal output file.
            if isinstance(source_text, str) and source_text.strip():
                source_name = str(payload.get("source_name") or "pasted_input.txt")[:180]
                pasted = store_uploaded_file(
                    chat_id, source_name, "text/plain", source_text.encode("utf-8")
                )
                file_id = int(pasted["id"])
            if file_id <= 0:
                raise ValueError("Paste text or choose a text file first.")
            return {"ok": True, "job": file_worker_preview(
                chat_id, file_id, _as_int(payload.get("preset_id")),
                str(payload.get("instruction") or ""), str(payload.get("batch_id") or ""),
            )}
        if route == "/api/file-worker/queue":
            return {"ok": True, "jobs": queue_file_jobs([str(x) for x in payload.get("job_ids") or []], chat_id)}
        if route.startswith("/api/file-worker/"):
            job_id = str(payload.get("job_id") or "")
            action = route.rsplit("/", 1)[-1]
            actions: dict[str, Callable[..., Any]] = {
                "start": start_file_job,
                "pause": pause_file_job,
                "resume": resume_file_job,
                "cancel": cancel_file_job,
                "retry": retry_file_job,
            }
            if action == "reorder":
                return {"ok": True, "jobs": reorder_file_job(job_id, chat_id, str(payload.get("direction") or "up"))}
            if action in actions:
                return {"ok": True, "job": actions[action](job_id, chat_id)}

        # Saved web pages ------------------------------------------------
        if route == "/api/page/add":
            page = fetch_page(str(payload.get("url") or ""), prefer_browser=bool(payload.get("prefer_browser", True)))
            page_id = store_page(chat_id, page)
            return {"ok": True, "page_id": page_id, "page": page}
        if route in {"/api/page/toggle", "/api/page/pin", "/api/page/delete"}:
            page_id = _as_int(payload.get("id"))
            with db_connect() as db:
                row = db.execute("SELECT id FROM pages WHERE id=? AND chat_id=?", (page_id, chat_id)).fetchone()
                if not row:
                    raise ValueError("Saved page not found.")
                if route.endswith("toggle"):
                    db.execute("UPDATE pages SET active=? WHERE id=?", (1 if bool(payload.get("active", True)) else 0, page_id))
                elif route.endswith("pin"):
                    db.execute("UPDATE pages SET context_pinned=? WHERE id=?", (1 if bool(payload.get("pinned", True)) else 0, page_id))
                else:
                    db.execute("DELETE FROM pages WHERE id=?", (page_id,))
            return {"ok": True}

        # Live browser ---------------------------------------------------
        if route == "/api/browser/action":
            action = str(payload.get("action") or "snapshot")
            values = {k: v for k, v in payload.items() if k not in {"action", "chat_id"}}
            if action == "open":
                state = LIVE_BROWSER.call("start")
                url = str(values.get("url") or "").strip()
                if url:
                    state = LIVE_BROWSER.call("navigate", url=url)
            elif action == "close":
                state = LIVE_BROWSER.call("close") if browser_status().get("running") else browser_status()
            else:
                state = LIVE_BROWSER.call(action, **values)
            return {"ok": True, "browser": state}
        if route == "/api/browser/assist":
            answer, browser_state_value = browser_assist(
                chat_id, str(payload.get("question") or ""), auto=bool(payload.get("auto", False)),
                focus=str(payload.get("focus") or ""), force_report=bool(payload.get("force_report", False)),
            )
            return {"ok": True, "answer": answer, "browser": browser_state_value, "messages": browser_assist_history(chat_id)}

        # Browser Agent --------------------------------------------------
        if route == "/api/browser-agent/start":
            return {"ok": True, "job": start_browser_agent(chat_id, str(payload.get("goal") or ""), _as_int(payload.get("max_steps"), 20))}
        if route == "/api/browser-agent/stop":
            return {"ok": True, "job": stop_browser_agent(str(payload.get("job_id") or ""), chat_id)}
        if route == "/api/browser-agent/resume":
            return {"ok": True, "job": resume_browser_agent(
                str(payload.get("job_id") or ""), chat_id, goal=str(payload.get("goal") or ""),
                max_steps=_as_int(payload.get("max_steps")) or None,
            )}

        # Screen Reader --------------------------------------------------
        if route == "/api/screen-reader/start":
            return {"ok": True, "job": start_screen_reader_job(chat_id, str(payload.get("question") or ""), _as_int(payload.get("message_limit"), 500))}
        if route == "/api/screen-reader/stop":
            return {"ok": True, "job": stop_screen_reader_job(str(payload.get("job_id") or ""), chat_id)}

        # DeepSearch -----------------------------------------------------
        if route == "/api/deepsearch/start":
            job_id = start_deepsearch(
                chat_id, str(payload.get("url") or payload.get("start_url") or ""), str(payload.get("goal") or ""),
                _as_int(payload.get("page_limit"), 100), _as_int(payload.get("max_depth"), 3),
            )
            return {"ok": True, "job_id": job_id, "job": deepsearch_row(job_id)}
        if route == "/api/deepsearch/control":
            job_id = str(payload.get("job_id") or "")
            action = str(payload.get("action") or "").casefold()
            if action == "pause":
                job = pause_deepsearch(job_id, chat_id)
            elif action == "resume":
                job = resume_deepsearch(job_id, chat_id)
            elif action == "stop":
                job = stop_deepsearch(job_id, chat_id)
            else:
                raise ValueError("DeepSearch action must be pause, resume, or stop.")
            return {"ok": True, "job": job}

        # Discord --------------------------------------------------------
        if route == "/api/discord/config":
            values = payload.get("config") if isinstance(payload.get("config"), dict) else payload
            return {"ok": True, "config": save_discord_bridge_config(values)}
        if route == "/api/discord/reload":
            restart_discord_bridge()
            return {"ok": True, "state": discord_bridge_state(), "config": discord_public_config()}
        if route == "/api/discord/start":
            start_discord_bridge()
            return {"ok": True, "state": discord_bridge_state()}
        if route == "/api/discord/stop":
            stop_discord_bridge()
            return {"ok": True, "state": discord_bridge_state()}

        # GitHub Releases updater ----------------------------------------
        if route == "/api/update/check":
            repo = str(payload.get("repo") or get_setting("update_repo", "brannod/zeno"))
            return check_for_update(repo)
        if route == "/api/update/download":
            repo = str(payload.get("repo") or get_setting("update_repo", "brannod/zeno"))
            return download_update(repo)
        if route == "/api/update/install":
            return install_update(str(payload.get("path") or "") or None)

        raise ValueError(f"Unknown API route: {route}")

    def _handle_chat_stream(self) -> None:
        request_id = ""
        chat_id = 0
        stop_event: threading.Event | None = None
        turn_started = time.monotonic()
        context_seconds = 0.0
        model_setup_seconds = 0.0
        first_token_seconds: float | None = None
        generation_started: float | None = None
        active_model = ""
        registered_chat = False
        started_interactive = False
        stream_started = False
        try:
            payload = self.read_json()
            chat_id = _safe_chat_id(payload.get("chat_id"))
            request_id = str(payload.get("request_id") or f"web-{time.time_ns()}")[:180]
            stop_event = register_request_stop(request_id)
            register_chat_operation(chat_id, stop_event)
            registered_chat = True
            interactive_request_started()
            started_interactive = True
            self._ndjson_start()
            stream_started = True

            user_text, user_message_id, history_before_id, file_ids = _prepare_chat_turn(chat_id, payload)
            self._ndjson({"type": "progress", "phase": "building_context", "percent": 2, "detail": "Building context"})

            # Deterministic AYCD command family. These commands write into the same
            # shared chat, so Discord sync and the browser see the same job/result.
            aycd_command = handle_aycd_command(chat_id, user_text, source="web", user_id="")
            if aycd_command is not None:
                answer = str(aycd_command.get("text") or "").strip()
                job_code = str(aycd_command.get("job_code") or "").strip()
                assistant_id = append_chat_message(
                    chat_id,
                    "assistant",
                    answer,
                    source="web_chat",
                    source_label=(f"AYCD Job {job_code}" if job_code else "AYCD"),
                )
                self._ndjson({"type": "meta", "sources": [], "model": "aycd-command", "aycd": aycd_command})
                self._ndjson({"type": "delta", "text": answer})
                self._ndjson({"type": "progress", "phase": "complete", "percent": 100, "detail": "AYCD command complete"})
                self._ndjson({"type": "done", "message_id": assistant_id, "attachments": [], "content": answer, "aycd": aycd_command})
                return

            # Deterministic file actions stay local and avoid a model call.
            direct = direct_file_action(chat_id, user_text)
            if direct is not None:
                answer, attachments = direct
                assistant_id = append_chat_message(
                    chat_id, "assistant", answer, source="web_chat", attachments=attachments,
                )
                self._ndjson({"type": "meta", "sources": [], "model": "local-action"})
                self._ndjson({"type": "delta", "text": answer})
                self._ndjson({"type": "progress", "phase": "complete", "percent": 100, "detail": "Complete"})
                self._ndjson({"type": "done", "message_id": assistant_id, "attachments": attachments})
                record_chat_timing(
                    request_id=request_id, mode=model_mode_setting(), model="local-action",
                    context_seconds=0.0, model_setup_seconds=0.0, first_token_seconds=0.0,
                    generation_seconds=0.0, total_seconds=time.monotonic()-turn_started,
                    output_chars=len(answer), direct_action=True,
                )
                schedule_response_maintenance(chat_id, user_text)
                return

            self._ndjson({"type": "progress", "phase": "mcp_connecting", "percent": 10, "detail": "Checking MCP tools"})
            mcp_context = maybe_mcp_context(chat_id, user_text, stop_event=stop_event, source="web")
            if mcp_context and mcp_context.get("direct_reply"):
                answer = str(mcp_context.get("direct_reply") or "").strip()
                assistant_id = append_chat_message(chat_id, "assistant", answer, source="web_chat", source_label="MCP")
                self._ndjson({"type": "meta", "sources": [], "model": "mcp-router", "mcp": mcp_context})
                self._ndjson({"type": "delta", "text": answer})
                self._ndjson({"type": "progress", "phase": "complete", "percent": 100, "detail": "Complete"})
                self._ndjson({"type": "done", "message_id": assistant_id, "attachments": []})
                schedule_response_maintenance(chat_id, user_text)
                return

            context_started = time.monotonic()
            messages, sources = build_prompt(
                chat_id, user_text, file_ids, skip_message_id=user_message_id,
                history_before_id=history_before_id,
                external_tool_focus=bool(mcp_context and mcp_context.get("claimed")),
            )
            mcp_message = mcp_context_message(mcp_context)
            if mcp_message:
                insert_at = max(1, len(messages) - 1)
                messages.insert(insert_at, mcp_message)
                self._ndjson({"type": "progress", "phase": "mcp_tool", "percent": 24, "detail": f"Used {mcp_context.get('server_name')} · {mcp_context.get('tool')}"})
            context_seconds = time.monotonic() - context_started
            max_tokens = adaptive_output_token_limit(
                user_text,
                downloadable_file=bool(re.search(r"(?i)\b(file|download|txt|csv|json|xlsx|code file)\b", user_text)),
            )
            self._ndjson({"type": "meta", "sources": sources, "mode": model_mode_setting()})

            progress_lock = threading.Lock()
            last_progress = {"overall": -1, "phase": "", "at": 0.0}

            def progress_callback(phase: str, percent: float | None = None, detail: str = "", output_chars: int = 0) -> None:
                overall = _progress_overall(phase, percent, output_chars)
                moment = time.monotonic()
                with progress_lock:
                    material = phase != last_progress["phase"] or overall >= int(last_progress["overall"]) + 2 or overall == 100
                    if not material or (moment - float(last_progress["at"]) < 0.25 and overall != 100):
                        return
                    last_progress.update({"overall": overall, "phase": phase, "at": moment})
                try:
                    self._ndjson({"type": "progress", "phase": phase, "percent": overall, "native_percent": percent, "detail": detail})
                except Exception:
                    # The main streaming loop will detect a disconnected client and cancel.
                    if stop_event is not None:
                        stop_event.set()

            model_setup_started = time.monotonic()
            try:
                model, transport, chunks = stream_completion_native_progress(
                    messages, stop_event, max_tokens=max_tokens, temperature=0.35,
                    user_message=user_text, request_class="chat", progress_callback=progress_callback,
                    model_mode=None,
                )
            except RuntimeError as exc:
                # Older LM Studio builds may not expose /api/v1/chat. Keep the
                # OpenAI-compatible stream as a compatibility fallback.
                if "404" not in str(exc) and "native" not in str(exc).casefold():
                    raise
                progress_callback("processing_prompt", 10.0, "Using compatible model stream", 0)
                model, transport, chunks = stream_completion(
                    messages, stop_event, max_tokens=max_tokens, temperature=0.35,
                    model_mode=None, user_message=user_text, request_class="chat",
                )

            model_setup_seconds = time.monotonic() - model_setup_started
            active_model = str(model or "")
            self._ndjson({"type": "model", "model": model, "transport": transport})
            answer_parts: list[str] = []
            for chunk in chunks:
                if stop_event.is_set():
                    break
                if first_token_seconds is None:
                    first_token_seconds = time.monotonic() - turn_started
                    generation_started = time.monotonic()
                answer_parts.append(chunk)
                self._ndjson({"type": "delta", "text": chunk})

            if stop_event.is_set():
                partial = "".join(answer_parts).strip()
                if partial:
                    append_chat_message(chat_id, "assistant", partial, source="web_chat", source_label="Stopped response")
                finished = time.monotonic()
                record_chat_timing(
                    request_id=request_id, mode=model_mode_setting(), model=active_model,
                    context_seconds=context_seconds, model_setup_seconds=model_setup_seconds,
                    first_token_seconds=first_token_seconds,
                    generation_seconds=(finished-generation_started) if generation_started else 0.0,
                    total_seconds=finished-turn_started, output_chars=len(partial), stopped=True,
                )
                self._ndjson({"type": "stopped", "partial": bool(partial)})
                return

            raw_answer = "".join(answer_parts).strip()
            visible_answer, generated = extract_generated_file_blocks(raw_answer, user_text)
            visible_answer = sanitize_assistant_response(visible_answer, user_text)
            used_sources = _sources_used_in_answer(visible_answer, sources)
            assistant_id = append_chat_message(
                chat_id, "assistant", visible_answer, source="web_chat", citations=used_sources,
                source_label=(f"MCP · {mcp_context.get('server_name')}" if mcp_context and mcp_context.get("used") else ""),
            )
            attachments: list[dict[str, Any]] = []
            for name, content in generated:
                attachments.append(create_generated_file(
                    chat_id, name, content, source_message_id=assistant_id,
                ))
            if attachments:
                with db_connect() as db:
                    db.execute(
                        "UPDATE messages SET attachments_json=? WHERE id=?",
                        (json.dumps(attachments), assistant_id),
                    )
            progress_callback("complete", 100.0, "Reply complete", len(visible_answer))
            self._ndjson({"type": "done", "message_id": assistant_id, "attachments": attachments, "citations": used_sources, "content": visible_answer})
            finished = time.monotonic()
            record_chat_timing(
                request_id=request_id, mode=model_mode_setting(), model=active_model,
                context_seconds=context_seconds, model_setup_seconds=model_setup_seconds,
                first_token_seconds=first_token_seconds,
                generation_seconds=(finished-generation_started) if generation_started else 0.0,
                total_seconds=finished-turn_started, output_chars=len(visible_answer),
            )
            schedule_response_maintenance(chat_id, user_text)
        except Exception as exc:
            if stop_event is not None and stop_event.is_set():
                try:
                    self._ndjson({"type": "stopped"})
                except Exception:
                    pass
            else:
                try:
                    # If streaming started, HTTP headers are already committed.
                    if stream_started:
                        self._ndjson({"type": "error", "error": str(exc)[:3000]})
                    elif not self.wfile.closed:
                        self.send_error_json(exc, 500)
                except Exception as write_exc:
                    if not self._client_disconnected(write_exc):
                        console_log(f"Chat stream error after client disconnect: {exc}")
        finally:
            if registered_chat and stop_event is not None:
                unregister_chat_operation(chat_id, stop_event)
            if request_id:
                unregister_request_stop(request_id)
            if started_interactive:
                interactive_request_finished()
            self.close_connection = True


def create_server(host: str = APP_HOST, port: int = APP_PORT) -> ZenoHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Zeno's local server may only bind to localhost.")
    return ZenoHTTPServer((host, int(port)), AppHandler)


def serve_forever(host: str = APP_HOST, port: int = APP_PORT) -> None:
    server = create_server(host, port)
    console_log(f"Zeno {APP_VERSION} listening on http://{host}:{server.server_address[1]}")
    try:
        server.serve_forever(poll_interval=0.35)
    finally:
        server.server_close()


__all__ = ["AppHandler", "ZenoHTTPServer", "snapshot_state", "create_server", "serve_forever"]
