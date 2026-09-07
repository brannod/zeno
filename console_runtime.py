#!/usr/bin/env python3
"""Windows console hardening and non-blocking console logging for Zeno.

The local HTTP server must never wait on Console Host.  On Windows, QuickEdit
selection can pause console writes; request-thread print() calls can therefore
make the browser appear frozen until the console receives input again.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from typing import Any

_LOG_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=1200)
_LOG_STARTED = False
_LOG_LOCK = threading.Lock()
_DROPPED = 0


def disable_windows_quick_edit() -> bool:
    """Disable QuickEdit for this process' console input handle only."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_std_handle = kernel32.GetStdHandle
        get_std_handle.argtypes = [wintypes.DWORD]
        get_std_handle.restype = wintypes.HANDLE
        get_console_mode = kernel32.GetConsoleMode
        get_console_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_console_mode.restype = wintypes.BOOL
        set_console_mode = kernel32.SetConsoleMode
        set_console_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        set_console_mode.restype = wintypes.BOOL

        STD_INPUT_HANDLE = -10 & 0xFFFFFFFF
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080

        handle = get_std_handle(STD_INPUT_HANDLE)
        if not handle or handle == wintypes.HANDLE(-1).value:
            return False
        mode = wintypes.DWORD()
        if not get_console_mode(handle, ctypes.byref(mode)):
            return False
        new_mode = (int(mode.value) | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
        if not set_console_mode(handle, new_mode):
            return False
        return True
    except Exception:
        return False


def _writer() -> None:
    global _DROPPED
    while True:
        line = _LOG_QUEUE.get()
        try:
            # This worker is the only place routine server logs touch Console Host.
            # If Console Host stalls, HTTP/request threads keep running.
            print(line, flush=True)
            if _DROPPED:
                dropped = _DROPPED
                _DROPPED = 0
                print(f"[console] dropped {dropped} log line(s) while the queue was full", flush=True)
        except Exception:
            # Console output is diagnostic only. It must never become an app dependency.
            pass
        finally:
            _LOG_QUEUE.task_done()


def start_console_logger() -> None:
    global _LOG_STARTED
    if _LOG_STARTED:
        return
    with _LOG_LOCK:
        if _LOG_STARTED:
            return
        threading.Thread(target=_writer, daemon=True, name="ZenoConsoleLog").start()
        _LOG_STARTED = True


def configure_console_runtime() -> dict[str, Any]:
    """Harden the attached console without changing global Windows settings."""
    quick_edit_disabled = disable_windows_quick_edit()
    start_console_logger()
    return {
        "windows": os.name == "nt",
        "quick_edit_disabled": quick_edit_disabled,
        "async_logging": True,
    }


def console_log(message: Any) -> None:
    """Best-effort, non-blocking diagnostic output."""
    global _DROPPED
    start_console_logger()
    text = str(message)
    try:
        _LOG_QUEUE.put_nowait(text)
    except queue.Full:
        _DROPPED += 1


__all__ = [
    "configure_console_runtime",
    "console_log",
    "disable_windows_quick_edit",
    "start_console_logger",
]
