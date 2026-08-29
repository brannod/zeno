#!/usr/bin/env python3
"""Zeno 3.0 application bootstrap.

The large V2.x monolith is now a thin orchestrator. Subsystems live in their
own modules and are wired together here through explicit hooks.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import webbrowser

from config import APP_HOST, APP_PORT, APP_VERSION
from database import init_db
from settings import clear_settings_cache, preload_settings
from context import set_context_stop_hook
from jobs import (
    register_chat_stop_hook,
    start_maintenance_worker,
    stop_all_chat_work,
    stop_maintenance_worker,
)
from files import resume_pending_file_jobs, stop_file_jobs_for_chat
from browser import set_browser_chat_append_hook, shutdown_live_browser
from browser_agent import set_browser_agent_chat_append_hook, stop_browser_agents_for_chat
from screen_reader import set_screen_reader_chat_append_hook, stop_screen_reader_jobs_for_chat
from desktop_notetaker import (
    ensure_notetaker_worker,
    notetaker_settings,
    stop_notetaker_for_chat,
    stop_notetaker_worker,
)
from deepsearch import (
    resume_pending_deepsearch_jobs,
    set_deepsearch_chat_append_hook,
    stop_deepsearch_for_chat,
)
from discord_bridge import (
    append_chat_message,
    discord_bridge_config,
    start_discord_bridge,
    stop_discord_bridge,
)
from model_api import get_model_runtime
from mcp_manager import start_mcp_manager
from http_server import create_server, snapshot_state


def wire_subsystems() -> None:
    """Connect cross-module hooks without creating circular imports."""
    set_browser_chat_append_hook(append_chat_message)
    set_browser_agent_chat_append_hook(append_chat_message)
    set_screen_reader_chat_append_hook(append_chat_message)
    set_deepsearch_chat_append_hook(append_chat_message)

    register_chat_stop_hook("file_jobs", stop_file_jobs_for_chat, discord_visible=True)
    register_chat_stop_hook("deepsearch", stop_deepsearch_for_chat, discord_visible=True)
    register_chat_stop_hook("browser_agent", stop_browser_agents_for_chat, discord_visible=True)
    register_chat_stop_hook("screen_reader", stop_screen_reader_jobs_for_chat, discord_visible=True)
    register_chat_stop_hook("notetaker", stop_notetaker_for_chat, discord_visible=True)
    set_context_stop_hook(stop_all_chat_work)


def initialize() -> None:
    init_db()
    clear_settings_cache()
    preload_settings()
    wire_subsystems()
    # Start Auto-Watch only from the persisted setting during application bootstrap.
    # Merely polling /api/notetaker/status must never start or re-enable the watcher.
    try:
        if notetaker_settings().get("enabled"):
            ensure_notetaker_worker()
    except Exception as exc:
        print(f"Notetaker auto-watch not started: {exc}")
    start_maintenance_worker()
    resume_pending_file_jobs()
    # V2.7 marked unfinished DeepSearch jobs interrupted during DB bootstrap.
    # This helper only resumes rows that are explicitly eligible to resume.
    try:
        resume_pending_deepsearch_jobs()
    except Exception as exc:
        print(f"DeepSearch recovery skipped: {exc}")
    try:
        config = discord_bridge_config()
        if bool(config.get("enabled")):
            start_discord_bridge()
    except Exception as exc:
        # Discord is optional and must never block local Zeno startup.
        print(f"Discord bridge not started: {exc}")
    try:
        start_mcp_manager()
    except Exception as exc:
        # MCP is optional. Missing SDK or an offline local server must not block Zeno startup.
        print(f"MCP manager not started: {exc}")


def shutdown() -> None:
    try:
        stop_discord_bridge()
    except Exception as exc:
        print(f"Discord shutdown warning: {exc}")
    try:
        stop_maintenance_worker()
    except Exception as exc:
        print(f"Maintenance shutdown warning: {exc}")
    try:
        stop_notetaker_worker()
    except Exception as exc:
        print(f"Notetaker shutdown warning: {exc}")
    try:
        shutdown_live_browser()
    except Exception as exc:
        print(f"Browser shutdown warning: {exc}")
    try:
        # Do not unload a model that the user loaded outside Zeno.
        get_model_runtime().close(unload_owned=False)
    except Exception as exc:
        print(f"Model runtime shutdown warning: {exc}")


def check_installation() -> int:
    initialize()
    try:
        state = snapshot_state()
        print(f"Zeno {APP_VERSION} integration check: OK")
        print(f"Active chat: {state['chat']['id']} · modules/state assembled successfully")
        print(f"Playwright: {'yes' if state['capabilities']['playwright'] else 'optional/not installed'}")
        print(f"PDF reader: {'yes' if state['capabilities']['pypdf'] else 'optional/not installed'}")
        return 0
    finally:
        shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Zeno {APP_VERSION} local assistant")
    parser.add_argument("--host", default=APP_HOST)
    parser.add_argument("--port", type=int, default=APP_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the GUI in the default browser.")
    parser.add_argument("--check", action="store_true", help="Initialize and run an integration check, then exit.")
    args = parser.parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Zeno only supports localhost binding.")

    if args.check:
        return check_installation()

    initialize()
    server = create_server(args.host, args.port)
    stopping = threading.Event()

    def request_shutdown(*_args: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True, name="ZenoHTTPShutdown").start()

    try:
        signal.signal(signal.SIGINT, request_shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_shutdown)
    except (ValueError, OSError):
        pass

    actual_port = int(server.server_address[1])
    url = f"http://{args.host}:{actual_port}"
    print("=" * 62)
    print(f" Zeno {APP_VERSION}")
    print(f" {url}")
    print(" Fast/Balanced use the configured fast model; Deep is opt-in.")
    print(" Press Ctrl+C to stop.")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.35)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
