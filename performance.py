#!/usr/bin/env python3
"""Lightweight in-process performance telemetry for Zeno.

The tracker stores only timing/counter metadata. It never stores prompt text,
responses, files, or memory contents.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_LOCK = threading.RLock()
_HISTORY: deque[dict[str, Any]] = deque(maxlen=30)
_LAST: dict[str, Any] = {}


def _ms(value: float | None) -> float:
    return round(max(0.0, float(value or 0.0)) * 1000.0, 1)


def record_chat_timing(
    *,
    request_id: str,
    mode: str,
    model: str,
    context_seconds: float,
    model_setup_seconds: float,
    first_token_seconds: float | None,
    generation_seconds: float,
    total_seconds: float,
    output_chars: int,
    stopped: bool = False,
    direct_action: bool = False,
) -> dict[str, Any]:
    """Store one completed chat-turn timing snapshot."""
    item = {
        "request_id": str(request_id or "")[:180],
        "mode": str(mode or "balanced"),
        "model": str(model or ""),
        "context_ms": _ms(context_seconds),
        "model_setup_ms": _ms(model_setup_seconds),
        "first_token_ms": None if first_token_seconds is None else _ms(first_token_seconds),
        "generation_ms": _ms(generation_seconds),
        "total_ms": _ms(total_seconds),
        "output_chars": max(0, int(output_chars or 0)),
        "chars_per_second": round(max(0, int(output_chars or 0)) / generation_seconds, 1) if generation_seconds > 0 else 0.0,
        "stopped": bool(stopped),
        "direct_action": bool(direct_action),
        "recorded_at": int(time.time()),
    }
    with _LOCK:
        global _LAST
        _LAST = dict(item)
        _HISTORY.append(dict(item))
    return item


def performance_snapshot() -> dict[str, Any]:
    """Return last-turn and recent-average timing data."""
    with _LOCK:
        rows = list(_HISTORY)
        last = dict(_LAST)
    completed = [row for row in rows if not row.get("direct_action")]
    if not completed:
        return {"last": last, "average": {}, "samples": len(rows)}

    def avg(key: str) -> float | None:
        values = [float(row[key]) for row in completed if row.get(key) is not None]
        return round(sum(values) / len(values), 1) if values else None

    average = {
        "context_ms": avg("context_ms"),
        "model_setup_ms": avg("model_setup_ms"),
        "first_token_ms": avg("first_token_ms"),
        "generation_ms": avg("generation_ms"),
        "total_ms": avg("total_ms"),
        "chars_per_second": avg("chars_per_second"),
    }
    return {"last": last, "average": average, "samples": len(rows)}


__all__ = ["record_chat_timing", "performance_snapshot"]
