#!/usr/bin/env python3
"""SQLite persistence, migration, and bootstrap compatibility for Zeno."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from config import (
    AUTO_SENSITIVE_RE,
    BASE_DIR,
    CHAT_MEMORY_DIR,
    CONTEXT_MEMORY_DIR,
    DATA_DIR,
    DB_PATH,
    DEFAULT_COMPUTE_MODE,
    DEFAULT_PERSONALITY,
    DISCORD_INFO_PATH,
    DISCORD_INFO_TEMPLATE,
    FILE_JOB_DIR,
    LEGACY_MEMORY_IMPORT_PATH,
    LONG_TERM_MEMORY_DIR,
    MAX_RECENT_MESSAGES,
    MEMORY_DIR,
    MEMORY_RETRIEVAL_LIMIT,
    OLD_DEFAULT_PERSONALITY,
    OUTPUT_DIR,
    PREFERRED_DEEP_MODEL,
    PREFERRED_MODEL,
    PREVIOUS_DEFAULT_PERSONALITY_V273,
    PREVIOUS_DEFAULT_PERSONALITY_V342,
    PREVIOUS_DEFAULT_PERSONALITY_V343,
    PRIVATE_DIR,
    SCREENSHOT_DIR,
    SELFDEV_BACKUP_DIR,
    SENSITIVE_RE,
    SUMMARY_KEEP_MESSAGES,
    SUMMARY_TRIGGER_MESSAGES,
    UPLOAD_DIR,
)


def now() -> int:
    return int(time.time())


def db_connect() -> sqlite3.Connection:
    """Open a Zeno SQLite connection with per-connection safety settings.

    WAL mode is configured once during init_db() instead of being requested on
    every connection. foreign_keys remains a per-connection PRAGMA.
    """
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def configure_database_journal() -> None:
    """Configure persistent database journal mode during bootstrap."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA journal_mode=WAL")


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}


def ensure_column(db: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    if name not in table_columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def load_legacy_memory_seeds() -> list[str]:
    """Read the reviewed, non-sensitive memories bundled with the Zeno upgrade."""
    try:
        raw = LEGACY_MEMORY_IMPORT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    memories: list[str] = []
    for line in raw.splitlines():
        content = line[2:].strip() if line.startswith("- ") else ""
        if not content or len(content) > 1_200:
            continue
        if SENSITIVE_RE.search(content) or AUTO_SENSITIVE_RE.search(content):
            continue
        memories.append(content)
    return memories


def database_message_count(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return -1
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0]) if row else 0
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return -1


def database_content_score(path: Path) -> int:
    """Rank prior databases without depending on an older product filename."""
    if not path.exists() or not path.is_file():
        return -1
    weights = {"messages": 100, "chats": 25, "memories": 20, "files": 10, "pages": 5}
    try:
        score = 0
        with sqlite3.connect(path) as connection:
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table, weight in weights.items():
                if table in tables:
                    score += int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) * weight
        return score
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return -1


def copy_database_snapshot(source: Path, destination: Path) -> None:
    """Copy a consistent SQLite snapshot, including committed WAL contents."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".zeno-import-{uuid.uuid4().hex}.db"
    source_connection = sqlite3.connect(source, timeout=30)
    target_connection = sqlite3.connect(temporary, timeout=30)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    os.replace(temporary, destination)


def previous_database_candidates() -> list[Path]:
    candidates: set[Path] = set()
    for folder in (BASE_DIR, MEMORY_DIR):
        if folder.exists():
            candidates.update(path for path in folder.glob("*.db") if path.is_file())
    return sorted(
        (
            path for path in candidates
            if path != DB_PATH and not path.name.startswith((".", "pre-"))
        ),
        key=lambda path: (database_content_score(path), path.stat().st_mtime),
        reverse=True,
    )


def directory_size(path: Path) -> int:
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def previous_data_directories() -> list[Path]:
    if not BASE_DIR.exists():
        return []
    recognizable = {"uploads", "outputs", "screenshots", "browser_profile", "file_jobs", "private"}
    candidates = [
        path for path in BASE_DIR.iterdir()
        if path.is_dir() and path != DATA_DIR and path.name.casefold().endswith("_data")
        and any((path / name).exists() for name in recognizable)
    ]
    return sorted(candidates, key=directory_size, reverse=True)


def merge_data_directory(source: Path, destination: Path) -> None:
    """Copy missing persisted files without overwriting newer Zeno data."""
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if item.is_symlink():
            continue
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


MEMORY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "do", "does", "for",
    "from", "had", "has", "have", "he", "her", "his", "i", "if", "in", "is", "it", "its", "me",
    "my", "of", "on", "or", "our", "she", "so", "that", "the", "their", "them", "they", "this", "to",
    "user", "was", "we", "were", "what", "when", "where", "which", "who", "will", "with", "you", "your",
}

def memory_normalized_key(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold())
    tokens = [token for token in cleaned.split() if token not in MEMORY_STOPWORDS]
    return " ".join(tokens[:80])[:500]

def memory_category(text: str) -> str:
    value = str(text or "").casefold()
    buckets = (
        ("Zeno", ("zeno", "lm studio", "deepsearch", "discord", "browser agent", "file worker")),
        ("Files", ("csv", "xlsx", "spreadsheet", "file", "txt", "json", "proxy", "aycd")),
        ("Code", ("python", "javascript", "html", "css", "code", "api", "script")),
        ("Trading", ("trading", "futures", "stock", "topstep", "gold", "market")),
        ("Preferences", ("prefers", "preference", "likes", "wants", "doesn't want", "does not want")),
        ("People", ("friend", "mother", "partner", "coworker", "person")),
        ("Projects", ("project", "roadmap", "build", "workflow", "setup")),
    )
    for category, terms in buckets:
        if any(term in value for term in terms):
            return category
    return "General"

def json_load(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def init_db() -> None:
    prior_data_dirs = previous_data_directories()
    migrated_data_dirs = prior_data_dirs[:1]
    if migrated_data_dirs:
        merge_data_directory(migrated_data_dirs[0], DATA_DIR)
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    FILE_JOB_DIR.mkdir(parents=True, exist_ok=True)
    SELFDEV_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    if not DISCORD_INFO_PATH.exists():
        DISCORD_INFO_PATH.write_text(DISCORD_INFO_TEMPLATE, encoding="utf-8")
        try:
            os.chmod(DISCORD_INFO_PATH, 0o600)
        except OSError:
            pass
    MEMORY_DIR.mkdir(exist_ok=True)
    CHAT_MEMORY_DIR.mkdir(exist_ok=True)
    CONTEXT_MEMORY_DIR.mkdir(exist_ok=True)
    LONG_TERM_MEMORY_DIR.mkdir(exist_ok=True)
    current_score = database_content_score(DB_PATH)
    prior_databases = previous_database_candidates()
    best_prior_path = prior_databases[0] if prior_databases else None
    best_prior_score = database_content_score(best_prior_path) if best_prior_path else -1
    if best_prior_path and best_prior_score > current_score:
        if DB_PATH.exists():
            backup_path = MEMORY_DIR / "pre-v2.7.1-empty-history.db"
            if not backup_path.exists():
                copy_database_snapshot(DB_PATH, backup_path)
        copy_database_snapshot(best_prior_path, DB_PATH)
        recovered_messages = max(0, database_message_count(best_prior_path))
        print(f"Recovered {recovered_messages:,} history message(s) from an earlier database into Zeno.")
    configure_database_journal()
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                summary_until_id INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_context_rollups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                through_message_id INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                chat_id INTEGER,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                citations_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'legacy',
                source_label TEXT NOT NULL DEFAULT '',
                external_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'manual'
            );
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                page_text TEXT NOT NULL,
                page_code TEXT NOT NULL DEFAULT '',
                raw_html TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                chat_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                sections_json TEXT NOT NULL DEFAULT '[]',
                links_json TEXT NOT NULL DEFAULT '[]',
                screenshot_path TEXT NOT NULL DEFAULT '',
                engine TEXT NOT NULL DEFAULT 'basic',
                css_code TEXT NOT NULL DEFAULT '',
                js_code TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                mime TEXT NOT NULL,
                kind TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                extracted_text TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generated_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                source_message_id INTEGER,
                source_file_id INTEGER,
                name TEXT NOT NULL,
                mime TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                version_group TEXT NOT NULL DEFAULT '',
                version_number INTEGER NOT NULL DEFAULT 1,
                is_current INTEGER NOT NULL DEFAULT 1,
                restored_from_id INTEGER,
                deleted_at INTEGER NOT NULL DEFAULT 0,
                source_job_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL,
                instruction TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                builtin INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_jobs (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                preset_id INTEGER,
                mode TEXT NOT NULL,
                instruction TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                detail TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                input_lines INTEGER NOT NULL DEFAULT 0,
                processed_lines INTEGER NOT NULL DEFAULT 0,
                output_lines INTEGER NOT NULL DEFAULT 0,
                output_file_id INTEGER,
                output_name TEXT NOT NULL DEFAULT '',
                preview_json TEXT NOT NULL DEFAULT '{}',
                validation_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL DEFAULT '',
                queue_position INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                log_json TEXT NOT NULL DEFAULT '[]',
                failure_type TEXT NOT NULL DEFAULT '',
                failure_hint TEXT NOT NULL DEFAULT '',
                last_successful_step TEXT NOT NULL DEFAULT '',
                resume_step TEXT NOT NULL DEFAULT '',
                partial_path TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                chat_id INTEGER PRIMARY KEY,
                html TEXT NOT NULL DEFAULT '',
                css TEXT NOT NULL DEFAULT '',
                js TEXT NOT NULL DEFAULT '',
                source_page_id INTEGER,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deepsearch_jobs (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                start_url TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                detail TEXT NOT NULL,
                pages_fetched INTEGER NOT NULL DEFAULT 0,
                page_limit INTEGER NOT NULL,
                max_depth INTEGER NOT NULL,
                queued_links INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                current_url TEXT NOT NULL DEFAULT '',
                progress INTEGER NOT NULL DEFAULT 0,
                log_json TEXT NOT NULL DEFAULT '[]',
                report TEXT NOT NULL DEFAULT '',
                citations_json TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS selfdev_jobs (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL DEFAULT 0,
                request TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                patch_json TEXT NOT NULL DEFAULT '[]',
                validation_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                touched_files_json TEXT NOT NULL DEFAULT '[]',
                backup_path TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                applied_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discord_events (
                external_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                author_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                response TEXT NOT NULL DEFAULT '',
                user_message_id INTEGER NOT NULL DEFAULT 0,
                assistant_message_id INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mcp_tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                server_id TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL DEFAULT '',
                arguments_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                result_text TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'chat',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS browser_assist_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                page_url TEXT NOT NULL DEFAULT '',
                page_title TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'manual',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS browser_agent_jobs (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                step INTEGER NOT NULL DEFAULT 0,
                max_steps INTEGER NOT NULL DEFAULT 20,
                detail TEXT NOT NULL DEFAULT '',
                current_url TEXT NOT NULL DEFAULT '',
                current_title TEXT NOT NULL DEFAULT '',
                log_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discord_channel_jobs (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                guild_id TEXT NOT NULL DEFAULT '',
                channel_id TEXT NOT NULL DEFAULT '',
                channel_name TEXT NOT NULL DEFAULT '',
                guild_name TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                detail TEXT NOT NULL DEFAULT '',
                messages_fetched INTEGER NOT NULL DEFAULT 0,
                message_limit INTEGER NOT NULL DEFAULT 500,
                progress INTEGER NOT NULL DEFAULT 0,
                report TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS screen_reader_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL,
                page_url TEXT NOT NULL DEFAULT '',
                page_title TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'page',
                source_label TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL DEFAULT '',
                items_read INTEGER NOT NULL DEFAULT 0,
                report TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'completed',
                created_at INTEGER NOT NULL,
                completed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notetaker_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT 'observation',
                title TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT 'observation',
                importance TEXT NOT NULL DEFAULT 'medium',
                content TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                structured_json TEXT NOT NULL DEFAULT '{}',
                model TEXT NOT NULL DEFAULT '',
                transport TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notetaker_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'Desktop session',
                summary TEXT NOT NULL DEFAULT '',
                started_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                ended_at INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0,
                important_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # Upgrade compatible earlier databases in place.
        ensure_column(db, "messages", "chat_id", "INTEGER")
        ensure_column(db, "messages", "attachments_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(db, "messages", "citations_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(db, "messages", "source", "TEXT NOT NULL DEFAULT 'legacy'")
        ensure_column(db, "messages", "source_label", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "messages", "external_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "memories", "updated_at", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "memories", "source", "TEXT NOT NULL DEFAULT 'manual'")
        ensure_column(db, "memories", "category", "TEXT NOT NULL DEFAULT 'general'")
        ensure_column(db, "memories", "pinned", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "memories", "access_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "memories", "last_used_at", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "memories", "normalized_key", "TEXT NOT NULL DEFAULT ''")
        for name, declaration in {
            "version_group": "TEXT NOT NULL DEFAULT ''", "version_number": "INTEGER NOT NULL DEFAULT 1",
            "is_current": "INTEGER NOT NULL DEFAULT 1", "restored_from_id": "INTEGER",
            "deleted_at": "INTEGER NOT NULL DEFAULT 0", "source_job_id": "TEXT NOT NULL DEFAULT ''",
        }.items():
            ensure_column(db, "generated_files", name, declaration)
        for name, declaration in {
            "batch_id": "TEXT NOT NULL DEFAULT ''", "queue_position": "INTEGER NOT NULL DEFAULT 0",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0", "log_json": "TEXT NOT NULL DEFAULT '[]'",
            "failure_type": "TEXT NOT NULL DEFAULT ''", "failure_hint": "TEXT NOT NULL DEFAULT ''",
            "last_successful_step": "TEXT NOT NULL DEFAULT ''", "resume_step": "TEXT NOT NULL DEFAULT ''",
            "partial_path": "TEXT NOT NULL DEFAULT ''",
        }.items():
            ensure_column(db, "file_jobs", name, declaration)
        for name, declaration in {
            "chat_id": "INTEGER", "active": "INTEGER NOT NULL DEFAULT 1",
            "sections_json": "TEXT NOT NULL DEFAULT '[]'", "links_json": "TEXT NOT NULL DEFAULT '[]'",
            "screenshot_path": "TEXT NOT NULL DEFAULT ''", "engine": "TEXT NOT NULL DEFAULT 'basic'",
            "css_code": "TEXT NOT NULL DEFAULT ''", "js_code": "TEXT NOT NULL DEFAULT ''",
            "deepsearch_job_id": "TEXT NOT NULL DEFAULT ''", "context_pinned": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            ensure_column(db, "pages", name, declaration)
        ensure_column(db, "files", "context_pinned", "INTEGER NOT NULL DEFAULT 0")
        for name, declaration in {
            "session_id": "INTEGER NOT NULL DEFAULT 0",
            "title": "TEXT NOT NULL DEFAULT ''",
            "event_type": "TEXT NOT NULL DEFAULT 'observation'",
            "importance": "TEXT NOT NULL DEFAULT 'medium'",
            "structured_json": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            ensure_column(db, "notetaker_notes", name, declaration)

        # Create indexes only after legacy schemas have been upgraded.
        # Older Zeno databases can already contain these tables without newer
        # columns such as notetaker_notes.session_id or messages.chat_id.
        # Creating migration-sensitive indexes before ensure_column() would
        # abort startup before the migration could run.
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_mcp_tool_calls_chat ON mcp_tool_calls(chat_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_screen_reader_history_chat ON screen_reader_history(chat_id, completed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notetaker_notes_chat ON notetaker_notes(chat_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_notetaker_notes_session ON notetaker_notes(session_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_notetaker_sessions_chat ON notetaker_sessions(chat_id, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id);
            CREATE INDEX IF NOT EXISTS idx_pages_chat_active_id ON pages(chat_id, active, id);
            CREATE INDEX IF NOT EXISTS idx_files_chat_active_id ON files(chat_id, active, id);
            CREATE INDEX IF NOT EXISTS idx_generated_files_chat_deleted ON generated_files(chat_id, deleted_at, created_at);
            CREATE INDEX IF NOT EXISTS idx_file_jobs_chat_status ON file_jobs(chat_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_browser_assist_chat_id ON browser_assist_messages(chat_id, id);
            CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);
            """
        )
        for prior_data_dir in migrated_data_dirs:
            old_prefix = prior_data_dir.name.replace("\\", "/") + "/"
            old_windows_prefix = prior_data_dir.name + "\\"
            db.execute(
                "UPDATE files SET stored_path=REPLACE(stored_path,?,?)",
                (old_prefix, DATA_DIR.name + "/"),
            )
            db.execute(
                "UPDATE files SET stored_path=REPLACE(stored_path,?,?)",
                (old_windows_prefix, DATA_DIR.name + "\\"),
            )
            db.execute(
                "UPDATE pages SET screenshot_path=REPLACE(screenshot_path,?,?)",
                (old_prefix, DATA_DIR.name + "/"),
            )
            db.execute(
                "UPDATE pages SET screenshot_path=REPLACE(screenshot_path,?,?)",
                (old_windows_prefix, DATA_DIR.name + "\\"),
            )
            db.execute(
                "UPDATE generated_files SET stored_path=REPLACE(stored_path,?,?)",
                (old_prefix, DATA_DIR.name + "/"),
            )
            db.execute(
                "UPDATE generated_files SET stored_path=REPLACE(stored_path,?,?)",
                (old_windows_prefix, DATA_DIR.name + "\\"),
            )
            db.execute(
                "UPDATE file_jobs SET partial_path=REPLACE(partial_path,?,?)",
                (old_prefix, DATA_DIR.name + "/"),
            )
            db.execute(
                "UPDATE file_jobs SET partial_path=REPLACE(partial_path,?,?)",
                (old_windows_prefix, DATA_DIR.name + "\\"),
            )
            db.execute(
                "UPDATE selfdev_jobs SET backup_path=REPLACE(backup_path,?,?)",
                (old_prefix, DATA_DIR.name + "/"),
            )
            db.execute(
                "UPDATE selfdev_jobs SET backup_path=REPLACE(backup_path,?,?)",
                (old_windows_prefix, DATA_DIR.name + "\\"),
            )

        # Earlier ready Self-Dev plans may name a superseded main Python file.
        # Point only missing single-Python targets at the sole current core module.
        for row in db.execute("SELECT id,patch_json,touched_files_json FROM selfdev_jobs"):
            patch = json_load(str(row["patch_json"]), [])
            touched = json_load(str(row["touched_files_json"]), [])
            changed_plan = False
            for operation in patch if isinstance(patch, list) else []:
                relative = str(operation.get("path", "")) if isinstance(operation, dict) else ""
                if relative.casefold().endswith(".py") and relative != "zeno.py" and not (BASE_DIR / relative).exists():
                    operation["path"] = "zeno.py"
                    changed_plan = True
            if isinstance(touched, list):
                migrated_touched = [
                    "zeno.py" if str(item).casefold().endswith(".py")
                    and str(item) != "zeno.py" and not (BASE_DIR / str(item)).exists()
                    else item for item in touched
                ]
                changed_plan = changed_plan or migrated_touched != touched
            else:
                migrated_touched = touched
            if changed_plan:
                db.execute(
                    "UPDATE selfdev_jobs SET patch_json=?,touched_files_json=? WHERE id=?",
                    (json.dumps(patch), json.dumps(migrated_touched), str(row["id"])),
                )

        defaults = {
            "personality": DEFAULT_PERSONALITY,
            "model": PREFERRED_MODEL,
            "model_mode": "balanced",
            "fast_model": PREFERRED_MODEL,
            "deep_model": PREFERRED_DEEP_MODEL,
            "context_window_tokens": "32768",
            "active_chat_id": "",
            "auto_memory": "true",
            "auto_summary": "true",
            "long_context_retrieval_enabled": "true",
            "long_context_retrieval_limit": "8",
            "recent_context_messages": str(MAX_RECENT_MESSAGES),
            "summary_trigger_messages": str(SUMMARY_TRIGGER_MESSAGES),
            "summary_keep_messages": str(SUMMARY_KEEP_MESSAGES),
            "autosave_turn_interval": "1",
            "use_browser": "true",
            "include_page_screenshot": "true",
            "selfdev_enabled": "true",
            "selfdev_auto_apply": "false",
            "discord_browser_activity_mode": "visual",
            "discord_completion_mentions": "false",
            "memory_retrieval_enabled": "true",
            "memory_retrieval_limit": str(MEMORY_RETRIEVAL_LIMIT),
            "notetaker_model": PREFERRED_MODEL,
            "notetaker_enabled": "false",
            "notetaker_interval_seconds": "20",
            "notetaker_monitor": "0",
            "notetaker_post_to_chat": "true",
            "notetaker_skip_unchanged": "true",
            "adaptive_context_enabled": "true",
            "live_screen_enabled": "true",
            "live_assist_interval_enabled": "false",
            "live_assist_interval_seconds": "30",
            "live_assist_focus": "Watch the current screen for meaningful changes, errors, warnings, important values, or useful next steps.",
            "compute_mode": DEFAULT_COMPUTE_MODE,
            "gpu_offload_requested": "auto",
            "update_repo": "brannod/zeno",
            "auto_update_check": "true",
        }
        for key, value in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))

        # Existing installations historically defaulted automatic memory and
        # summaries off. Upgrade them once to the new unlimited-archive mode;
        # later user changes are preserved because the marker prevents repeats.
        continuity = db.execute(
            "SELECT value FROM settings WHERE key='unlimited_context_migration_v1'"
        ).fetchone()
        if not continuity or str(continuity["value"]).casefold() != "complete":
            db.execute("UPDATE settings SET value='true' WHERE key IN ('auto_memory','auto_summary')")
            db.execute("UPDATE settings SET value='1' WHERE key='autosave_turn_interval'")
            db.execute(
                "INSERT INTO settings(key,value) VALUES('unlimited_context_migration_v1','complete') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

        current_personality = db.execute("SELECT value FROM settings WHERE key='personality'").fetchone()
        if current_personality and str(current_personality["value"]).strip() in {
            OLD_DEFAULT_PERSONALITY.strip(), PREVIOUS_DEFAULT_PERSONALITY_V273.strip(), PREVIOUS_DEFAULT_PERSONALITY_V342.strip(), PREVIOUS_DEFAULT_PERSONALITY_V343.strip()
        }:
            db.execute("UPDATE settings SET value=? WHERE key='personality'", (DEFAULT_PERSONALITY,))

        legacy_import = db.execute(
            "SELECT value FROM settings WHERE key='legacy_zeno_memory_import_v27'"
        ).fetchone()
        if not legacy_import or str(legacy_import["value"]).casefold() != "complete":
            existing_memories = {
                str(row["content"]).strip().casefold()
                for row in db.execute("SELECT content FROM memories")
            }
            imported_count = 0
            for content in load_legacy_memory_seeds():
                normalized = content.casefold()
                if normalized in existing_memories:
                    continue
                db.execute(
                    "INSERT INTO memories(content,created_at,updated_at,source) VALUES(?,?,?,?)",
                    (content, now(), now(), "legacy_zeno_v27"),
                )
                existing_memories.add(normalized)
                imported_count += 1
            db.execute(
                "INSERT INTO settings(key,value) VALUES('legacy_zeno_memory_import_v27','complete') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            db.execute(
                "INSERT INTO settings(key,value) VALUES('legacy_zeno_memory_import_v27_count',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(imported_count),),
            )
        personality_row = db.execute("SELECT value FROM settings WHERE key='personality'").fetchone()
        if personality_row:
            current_personality = str(personality_row["value"]).lstrip()
            if current_personality.startswith(
                "You are Zeno, a sharp and friendly local AI assistant created by Pluru."
            ):
                db.execute("UPDATE settings SET value=? WHERE key='personality'", (DEFAULT_PERSONALITY,))
            elif "Do not use anime roleplay, forced affection, fake enthusiasm, or corporate filler." in current_personality:
                updated_personality = current_personality.replace(
                    "Do not use anime roleplay, forced affection, fake enthusiasm, or corporate filler. ", ""
                ).replace(
                    "Do not use anime roleplay, forced affection, fake enthusiasm, or corporate filler.", ""
                )
                db.execute("UPDATE settings SET value=? WHERE key='personality'", (updated_personality.strip(),))

        timestamp = now()
        db.execute(
            "DELETE FROM file_presets WHERE builtin=1 AND name='Brand Proxy Scramble' "
            "AND EXISTS(SELECT 1 FROM file_presets WHERE builtin=1 AND name='Scramble Lines / Proxies')"
        )
        db.execute(
            "UPDATE file_presets SET name='Scramble Lines / Proxies',instruction=?,updated_at=? "
            "WHERE builtin=1 AND (name='Brand Proxy Scramble' OR mode='brand_proxy_scramble')",
            ("Randomize complete lines and interleave proxy providers when detected. Preserve every complete record "
             "exactly once and character-for-character; add no numbering, headers, or commentary.", timestamp),
        )
        db.execute(
            "DELETE FROM file_presets WHERE builtin=1 AND name='AYCD Triple-Colon Format' "
            "AND EXISTS(SELECT 1 FROM file_presets WHERE builtin=1 AND name='AYCD Email:::Password Format')"
        )
        db.execute(
            "UPDATE file_presets SET name='AYCD Email:::Password Format',updated_at=? "
            "WHERE builtin=1 AND name='AYCD Triple-Colon Format'", (timestamp,)
        )
        builtin_presets = (
            (
                "Scramble Lines / Proxies", "brand_proxy_scramble",
                "Randomize complete lines and interleave proxy providers when detected. Preserve every complete record "
                "exactly once and character-for-character; add no numbering, headers, or commentary.",
                {"exact_multiset": True, "preserve_structure": True},
            ),
            (
                "Shuffle Complete Lines", "shuffle_lines",
                "Randomize complete-line order only. Never alter the contents of any line.",
                {"exact_multiset": True, "preserve_structure": True},
            ),
            (
                "Remove Exact Duplicates", "dedupe_lines",
                "Remove repeated complete lines while preserving the first occurrence and every unique value.",
                {"preserve_structure": True},
            ),
            (
                "Sort Lines A-Z", "sort_lines",
                "Sort complete lines case-insensitively from A to Z without changing any line content.",
                {"preserve_structure": True},
            ),
            (
                "Remove Blank Lines", "remove_blank_lines",
                "Remove only empty or whitespace-only lines. Preserve every nonblank line character-for-character.",
                {"preserve_structure": True},
            ),
            (
                "Extract Email Addresses", "extract_emails",
                "Extract email addresses in first-seen order, one per line, and remove exact duplicate addresses.",
                {"preserve_structure": False},
            ),
            (
                "AI Transform Each Line", "ai_line_transform",
                "Apply the user's exact instruction independently to every complete line. Preserve all unspecified values.",
                {"preserve_structure": True},
            ),
            (
                "AYCD Email:::Password Format", "ai_line_transform",
                "Return AYCD records using the literal ::: delimiter. Preserve every identifier and value; never treat :, ::, "
                "and ::: as interchangeable. Add no header, numbering, commentary, or dummy rows.",
                {"preserve_structure": False, "required_delimiter": ":::", "preserve_aycd_values": True},
            ),
            (
                "Email:Password Cleanup", "ai_line_transform",
                "Return one email:password record per input line. Remove proxy or metadata fields only when present, "
                "and preserve the email and password values exactly. Add no header, numbering, or commentary.",
                {"preserve_structure": False},
            ),
        )
        for name, mode, instruction, config in builtin_presets:
            db.execute(
                "INSERT OR IGNORE INTO file_presets(name,mode,instruction,config_json,builtin,created_at,updated_at) "
                "VALUES(?,?,?,?,1,?,?)",
                (name, mode, instruction, json.dumps(config), timestamp, timestamp),
            )

        chat = db.execute("SELECT id FROM chats ORDER BY id LIMIT 1").fetchone()
        if chat is None:
            old_count = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            title = "Imported conversation" if old_count else "New chat"
            cursor = db.execute(
                "INSERT INTO chats(title,created_at,updated_at) VALUES(?,?,?)", (title, now(), now())
            )
            chat_id = int(cursor.lastrowid)
        else:
            chat_id = int(chat["id"])
        db.execute("UPDATE messages SET chat_id=? WHERE chat_id IS NULL", (chat_id,))
        db.execute("UPDATE pages SET chat_id=? WHERE chat_id IS NULL", (chat_id,))
        legacy_page = db.execute("SELECT value FROM settings WHERE key='active_page_id'").fetchone()
        if legacy_page and str(legacy_page["value"]).isdigit():
            db.execute("UPDATE pages SET active=CASE WHEN id=? THEN 1 ELSE 0 END WHERE chat_id=?",
                       (int(legacy_page["value"]), chat_id))
        active = db.execute("SELECT value FROM settings WHERE key='active_chat_id'").fetchone()
        if not active or not str(active["value"]).isdigit():
            db.execute("UPDATE settings SET value=? WHERE key='active_chat_id'", (str(chat_id),))
        db.execute("UPDATE memories SET updated_at=created_at WHERE updated_at=0")
        for memory_row in db.execute("SELECT id,content,category,normalized_key FROM memories").fetchall():
            content = str(memory_row["content"] or "").strip()
            category = str(memory_row["category"] or "general")
            normalized = str(memory_row["normalized_key"] or "")
            if content and (not normalized or category == "general"):
                db.execute(
                    "UPDATE memories SET normalized_key=?,category=? WHERE id=?",
                    (memory_normalized_key(content), memory_category(content) if category == "general" else category, int(memory_row["id"])),
                )
        db.execute("UPDATE generated_files SET version_group='legacy:' || id WHERE version_group='' OR version_group IS NULL")
        db.execute("UPDATE file_jobs SET queue_position=created_at WHERE queue_position=0")
        db.execute(
            "UPDATE deepsearch_jobs SET status='interrupted',stage='Interrupted',"
            "detail='Zeno restarted before this DeepSearch finished.',updated_at=? "
            "WHERE status IN ('queued','running','paused')", (now(),)
        )
        db.execute(
            "UPDATE file_jobs SET status='paused',stage='Paused after restart',"
            "detail='Zeno restarted safely. Resume this job from its last completed chunk.',"
            "resume_step=CASE WHEN resume_step='' THEN 'Resume after restart' ELSE resume_step END,updated_at=? "
            "WHERE status IN ('running','cancelling')", (now(),)
        )
        db.execute(
            "UPDATE selfdev_jobs SET status='interrupted',error='Zeno restarted before this Self-Dev plan finished.',updated_at=? "
            "WHERE status IN ('queued','planning','validating','applying')", (now(),)
        )
        db.execute(
            "UPDATE browser_agent_jobs SET status='interrupted',detail='Zeno restarted before this browser task finished.',updated_at=? "
            "WHERE status IN ('queued','running','stopping')", (now(),)
        )
        db.execute(
            "UPDATE discord_channel_jobs SET status='interrupted',detail='Zeno restarted before this Screen Reader job finished.',updated_at=? "
            "WHERE status IN ('queued','fetching','analyzing','stopping')", (now(),)
        )


def get_setting(key: str, fallback: str = "") -> str:
    with db_connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else fallback


def bool_setting(key: str, fallback: bool = False) -> bool:
    return get_setting(key, "true" if fallback else "false").casefold() == "true"


def int_setting(key: str, fallback: int, minimum: int, maximum: int) -> int:
    try:
        value = int(get_setting(key, str(fallback)))
    except ValueError:
        value = fallback
    return max(minimum, min(value, maximum))


def set_setting(key: str, value: str) -> None:
    with db_connect() as db:
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )


def current_chat_id(candidate: Any = None) -> int:
    raw = str(candidate if candidate not in (None, "") else get_setting("active_chat_id", ""))
    with db_connect() as db:
        row = db.execute("SELECT id FROM chats WHERE id=?", (int(raw) if raw.isdigit() else -1,)).fetchone()
        if row:
            return int(row["id"])
        row = db.execute("SELECT id FROM chats ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row:
            set_setting("active_chat_id", str(row["id"]))
            return int(row["id"])
        cursor = db.execute("INSERT INTO chats(title,created_at,updated_at) VALUES('New chat',?,?)", (now(), now()))
        chat_id = int(cursor.lastrowid)
    set_setting("active_chat_id", str(chat_id))
    return chat_id
