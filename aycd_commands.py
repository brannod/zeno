#!/usr/bin/env python3
"""AYCD Profile Builder workflow layer for Zeno.

Provides deterministic !aycd commands, synthetic/test profile previews,
MCP schema-aware Profile Builder actions, persistent jobs/recipes, and
Discord reaction controls. AYCD's live MCP schemas are always treated as the
source of truth; required values Zeno cannot infer are requested explicitly.
"""
from __future__ import annotations

import json
import random
import re
import string
import threading
from typing import Any

from database import db_connect, now
from mcp_manager import (
    MCP_DEFAULT_SERVER_ID,
    call_mcp_tool,
    mcp_public_server,
    mcp_tools,
)

AYCD_PROFILE_MAX_COUNT = 10000
AYCD_PREVIEW_COUNT = 12
AYCD_RESULT_LIMIT = 18000
AYCD_PROFILE_GENERATOR_VERSION = 3

# Synthetic/test-only profile components. These deliberately use reserved
# 555-01xx phone numbers and non-deliverable test addresses so Zeno never
# fabricates real identities, email accounts, or payment data.
_SYNTHETIC_CITIES = (
    "Exampleville", "Teston", "Sample City", "Demo Heights", "Mock Harbor",
    "Preview Point", "Sandbox Springs", "Fixture Falls",
)
_SYNTHETIC_STREETS = (
    "Example", "Test", "Sample", "Demo", "Sandbox", "Fixture", "Preview",
    "Mock", "Prototype", "Staging",
)
_STREET_SUFFIXES = ("St", "Ave", "Rd", "Blvd", "Ln", "Dr", "Way", "Ct")
_SYNTHETIC_STATES = ("CA", "NY", "TX", "FL", "IL", "WA", "MA", "CO")
_PAYMENT_FIELD_RE = re.compile(r"(?:^|_)(?:card|cc|cvv|cvc|expiry|expiration|payment|pan)(?:_|$)", re.I)

_LOCK = threading.RLock()
_ACTIVE_EVENTS: dict[str, threading.Event] = {}

_FIRST_NAMES = (
    "Avery", "Blake", "Cameron", "Casey", "Drew", "Elliot", "Emery", "Finley",
    "Harper", "Hayden", "Jamie", "Jordan", "Kai", "Logan", "Morgan", "Parker",
    "Quinn", "Reese", "Riley", "Rowan", "Sage", "Skyler", "Taylor", "Alex",
    "Maya", "Daniel", "Sofia", "Marcus", "Elena", "Noah", "Chloe", "Julian",
    "Nora", "Theo", "Mila", "Owen", "Iris", "Leo", "Lena", "Miles",
)
_LAST_NAMES = (
    "Bennett", "Brooks", "Carter", "Chen", "Collins", "Diaz", "Ellis", "Foster",
    "Garcia", "Gray", "Hayes", "Howard", "Kim", "Lee", "Martin", "Miller",
    "Morgan", "Nguyen", "Park", "Patel", "Reed", "Rivera", "Ross", "Scott",
    "Stone", "Turner", "Walker", "Ward", "Watson", "West", "White", "Young",
    "Hughes", "Price", "Bell", "Cooper", "Perry", "Murphy", "Bailey", "Kelly",
)

_RE_COUNT = re.compile(r"\b(\d{1,5})\b")
_RE_JOB = re.compile(r"\bA(\d{1,8})\b", re.I)
_RE_RECIPE_USING = re.compile(r"(?i)\busing\s+([a-z0-9_.-]{1,60})\b")

_ACTION_SPECS: dict[str, dict[str, Any]] = {
    "basic_jig": {
        "label": "Basic Jig", "emoji": "🧩",
        "aliases": ("jig", "basic jig", "basic-jig", "profiles jig", "profile jig"),
        "must": ("jig",), "boost": ("basic", "profile builder"),
        "avoid": ("address", "mass edit", "distribute", "spreadsheet", "google form", "duplicate"),
    },
    "address_jig": {
        "label": "AI Address Jig", "emoji": "🏠",
        "aliases": ("address jig", "address-jig", "ai address jig", "ai-address-jig"),
        "must": ("address", "jig"), "boost": ("ai", "profile builder"),
        "avoid": ("mass edit", "distribute", "spreadsheet", "google form", "duplicate"),
    },
    "mass_edit": {
        "label": "Mass Edit/Jig", "emoji": "✏️",
        "aliases": ("edit", "mass edit", "mass-edit", "mass edit jig", "mass-edit-jig"),
        "must": ("edit",), "boost": ("mass", "jig", "profile builder"),
        "avoid": ("address", "distribute", "spreadsheet", "google form", "duplicate"),
    },
    "distribute": {
        "label": "Mass Distribute", "emoji": "📦",
        "aliases": ("distribute", "mass distribute", "mass-distribute", "distribution"),
        "must": ("distribut",), "boost": ("mass", "profile builder"),
        "avoid": ("spreadsheet", "google form", "duplicate"),
    },
    "spreadsheet": {
        "label": "Send to Spreadsheets", "emoji": "📊",
        "aliases": ("spreadsheet", "spreadsheets", "send spreadsheet", "send to spreadsheet", "export spreadsheet"),
        "must": ("spreadsheet",), "boost": ("send", "profile builder", "google sheet", "sheet"),
        "avoid": ("google form", "duplicate"),
    },
    "forms": {
        "label": "Prefill Google Forms", "emoji": "📝",
        "aliases": ("forms", "form", "google forms", "google form", "prefill forms", "prefill google forms"),
        "must": ("form",), "boost": ("google", "prefill", "profile builder"),
        "avoid": ("spreadsheet", "duplicate"),
    },
}
_ACTION_EMOJI_TO_NAME = {str(spec["emoji"]): name for name, spec in _ACTION_SPECS.items()}


def _json_load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return str(value)


def _ensure_tables() -> None:
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS aycd_jobs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_code TEXT NOT NULL UNIQUE,
              chat_id INTEGER NOT NULL,
              kind TEXT NOT NULL DEFAULT 'profiles_generate',
              status TEXT NOT NULL DEFAULT 'preview_ready',
              request_text TEXT NOT NULL DEFAULT '',
              payload_json TEXT NOT NULL DEFAULT '{}',
              result_text TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              created_by_source TEXT NOT NULL DEFAULT '',
              created_by_user TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_aycd_jobs_chat_updated
              ON aycd_jobs(chat_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS aycd_recipes(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              config_json TEXT NOT NULL DEFAULT '{}',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              UNIQUE(chat_id,name)
            );
            CREATE INDEX IF NOT EXISTS idx_aycd_recipes_chat
              ON aycd_recipes(chat_id,updated_at DESC);
            """
        )


def init_aycd_commands() -> None:
    _ensure_tables()


def _job_code(job_id: int) -> str:
    return f"A{int(job_id):03d}"


def _row_to_job(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    item["payload"] = _json_load(item.pop("payload_json", "{}"), {})
    return item


def aycd_job(job_code: str, chat_id: int | None = None) -> dict[str, Any] | None:
    _ensure_tables()
    code = str(job_code or "").strip().upper()
    with db_connect() as db:
        if chat_id is None:
            row = db.execute("SELECT * FROM aycd_jobs WHERE job_code=?", (code,)).fetchone()
        else:
            row = db.execute("SELECT * FROM aycd_jobs WHERE job_code=? AND chat_id=?", (code, int(chat_id))).fetchone()
    return _row_to_job(row) if row else None


def aycd_jobs(chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
    _ensure_tables()
    with db_connect() as db:
        rows = db.execute(
            "SELECT * FROM aycd_jobs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (int(chat_id), max(1, min(int(limit), 100))),
        ).fetchall()
    return [_row_to_job(row) for row in rows]


def _update_job(code: str, *, status: str | None = None, payload: dict[str, Any] | None = None,
                result: str | None = None, error: str | None = None) -> dict[str, Any]:
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status=?"); values.append(str(status)[:40])
    if payload is not None:
        fields.append("payload_json=?"); values.append(json.dumps(payload, ensure_ascii=False, default=str))
    if result is not None:
        fields.append("result_text=?"); values.append(str(result)[:AYCD_RESULT_LIMIT])
    if error is not None:
        fields.append("error=?"); values.append(str(error)[:3000])
    fields.append("updated_at=?"); values.append(now())
    values.append(str(code).upper())
    with db_connect() as db:
        db.execute(f"UPDATE aycd_jobs SET {', '.join(fields)} WHERE job_code=?", tuple(values))
    return aycd_job(code) or {}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", str(value or "").casefold()).strip(".")


def _synthetic_profile(first: str, last: str, index: int, rng: random.SystemRandom) -> dict[str, str]:
    """Build one clearly synthetic/test profile. Email/payment fields are never generated."""
    token = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    street_no = rng.randint(10, 9998)
    street = f"{street_no} {rng.choice(_SYNTHETIC_STREETS)} {rng.choice(_STREET_SUFFIXES)}"
    address2 = f"Apt {rng.randint(1, 999)}" if rng.random() < 0.45 else ""
    # Keep phone values in the NANP reserved fictional 555-01xx range. The pool is
    # intentionally reused for very large synthetic batches rather than inventing
    # routable phone numbers.
    phone_tail = 100 + rng.randrange(100)
    full_name = f"{first} {last}"
    return {
        "first_name": first,
        "last_name": last,
        "name": full_name,
        "profile_name": f"{first}-{last}-{token}",
        "phone": f"+1-202-555-{phone_tail:04d}",
        "address1": street,
        "address2": address2,
        "city": rng.choice(_SYNTHETIC_CITIES),
        "state": rng.choice(_SYNTHETIC_STATES),
        "postal_code": "99999",
        "country": "US",
    }


def _unique_profiles(count: int) -> list[dict[str, str]]:
    """Generate up to AYCD_PROFILE_MAX_COUNT synthetic records without requiring unique names."""
    count = max(1, min(int(count), AYCD_PROFILE_MAX_COUNT))
    rng = random.SystemRandom()
    seen_profile_names: set[str] = set()
    profiles: list[dict[str, str]] = []
    while len(profiles) < count:
        first, last = rng.choice(_FIRST_NAMES), rng.choice(_LAST_NAMES)
        profile = _synthetic_profile(first, last, len(profiles), rng)
        key = str(profile.get("profile_name") or "").casefold()
        if not key or key in seen_profile_names:
            continue
        seen_profile_names.add(key)
        profiles.append(profile)
    return profiles


def _recipe(chat_id: int, name: str) -> dict[str, Any] | None:
    clean = str(name or "").strip().casefold()
    with db_connect() as db:
        row = db.execute("SELECT * FROM aycd_recipes WHERE chat_id=? AND name=?", (int(chat_id), clean)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["config"] = _json_load(item.pop("config_json", "{}"), {})
    return item


def list_aycd_recipes(chat_id: int) -> list[dict[str, Any]]:
    _ensure_tables()
    with db_connect() as db:
        rows = db.execute("SELECT * FROM aycd_recipes WHERE chat_id=? ORDER BY updated_at DESC", (int(chat_id),)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["config"] = _json_load(item.pop("config_json", "{}"), {})
        out.append(item)
    return out


def save_aycd_recipe(chat_id: int, name: str, config: dict[str, Any]) -> dict[str, Any]:
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(name or "").strip()).strip("-").casefold()[:60]
    if not clean:
        raise ValueError("Recipe name is required.")
    raw_actions = config.get("planned_actions") or config.get("recipe_actions") or []
    if not raw_actions and isinstance(config.get("action_history"), list):
        raw_actions = [x.get("action") for x in config["action_history"] if isinstance(x, dict) and x.get("status") == "completed"]
    actions: list[str] = []
    defaults: dict[str, dict[str, Any]] = {}
    for value in list(raw_actions)[:12] if isinstance(raw_actions, list) else []:
        key = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if key in _ACTION_SPECS and key not in actions:
            actions.append(key)
    for item in config.get("action_history") or []:
        if isinstance(item, dict) and item.get("action") in _ACTION_SPECS and isinstance(item.get("parameters"), dict):
            defaults[str(item["action"])] = dict(item["parameters"])
    defaults.update(config.get("action_defaults") if isinstance(config.get("action_defaults"), dict) else {})
    cfg = {
        "kind": "profiles_generate",
        "count": max(1, min(int(config.get("count") or 10), AYCD_PROFILE_MAX_COUNT)),
        "duplicate_check": bool(config.get("duplicate_check", True)),
        "preview_first": True,
        "synthetic_only": True,
        "generator_version": AYCD_PROFILE_GENERATOR_VERSION,
        "profile_fields": ["first_name","last_name","phone","address1","address2","city","state","postal_code","country"],
        "actions": actions,
        "action_defaults": defaults,
    }
    stamp = now()
    with db_connect() as db:
        db.execute(
            "INSERT INTO aycd_recipes(chat_id,name,config_json,created_at,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id,name) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at",
            (int(chat_id), clean, json.dumps(cfg, ensure_ascii=False), stamp, stamp),
        )
    return _recipe(chat_id, clean) or {"name": clean, "config": cfg}


def delete_aycd_recipe(chat_id: int, name: str) -> bool:
    with db_connect() as db:
        cur = db.execute("DELETE FROM aycd_recipes WHERE chat_id=? AND name=?", (int(chat_id), str(name).casefold()))
    return bool(cur.rowcount)


def _create_job(chat_id: int, kind: str, status: str, request_text: str, payload: dict[str, Any],
                source: str, user_id: str, error: str = "") -> dict[str, Any]:
    stamp = now()
    with db_connect() as db:
        cur = db.execute(
            "INSERT INTO aycd_jobs(job_code,chat_id,kind,status,request_text,payload_json,result_text,error,created_by_source,created_by_user,created_at,updated_at) "
            "VALUES('',?,?,?,?,?, '', ?,?,?,?,?)",
            (int(chat_id), kind, status, str(request_text)[:4000], json.dumps(payload, ensure_ascii=False, default=str),
             str(error)[:3000], str(source)[:40], str(user_id)[:80], stamp, stamp),
        )
        jid = int(cur.lastrowid); code = _job_code(jid)
        db.execute("UPDATE aycd_jobs SET job_code=? WHERE id=?", (code, jid))
    return aycd_job(code, chat_id) or {}


def _create_profile_job(chat_id: int, count: int, request_text: str, source: str, user_id: str, recipe_name: str = "") -> dict[str, Any]:
    count = max(1, min(int(count), AYCD_PROFILE_MAX_COUNT))
    recipe = _recipe(chat_id, recipe_name) if recipe_name else None
    cfg = dict(recipe.get("config") or {}) if recipe else {}
    payload = {
        "count": count,
        "profiles": _unique_profiles(count),
        "recipe": recipe_name,
        "duplicate_check": bool(cfg.get("duplicate_check", True)),
        "synthetic_only": True,
        "generator_version": AYCD_PROFILE_GENERATOR_VERSION,
        "profile_fields": ["first_name","last_name","phone","address1","address2","city","state","postal_code","country"],
        "planned_actions": list(cfg.get("actions") or []),
        "action_defaults": dict(cfg.get("action_defaults") or {}),
        "action_history": [],
    }
    return _create_job(chat_id, "profiles_generate", "preview_ready", request_text, payload, source, user_id)


def _required_args(tool: dict[str, Any]) -> list[str]:
    schema = tool.get("input_schema") or {}
    req = schema.get("required") or [] if isinstance(schema, dict) else []
    return [str(x) for x in req if str(x).strip()] if isinstance(req, list) else []


def _is_read_only(tool: dict[str, Any]) -> bool:
    ann = tool.get("annotations") or {}
    if isinstance(ann, dict):
        for key in ("readOnlyHint", "read_only_hint", "readOnly"):
            if key in ann:
                return bool(ann.get(key))
    name = str(tool.get("name") or "").casefold()
    if re.search(r"delete|remove|create|generate|add|update|edit|write|send|import|export|jig|distribute|save", name):
        return False
    return bool(re.search(r"list|search|find|get|read|view|check|status|lookup|inspect|preview|count|show", name))


def _schema_profile_signals(tool: dict[str, Any]) -> tuple[int, list[str]]:
    """Return a conservative score + reasons that a tool creates Profile Builder records.

    AYCD tool names are not guaranteed to contain the literal word ``profile``.
    We therefore inspect the public MCP description and JSON input schema too, while
    explicitly rejecting account/task/order style automation tools.
    """
    schema = tool.get("input_schema") or {}
    props = schema.get("properties") or {} if isinstance(schema, dict) else {}
    if not isinstance(props, dict):
        props = {}

    reasons: list[str] = []
    score = 0
    compact_props = {_norm_field(k) for k in props}

    # Strong container signals used by profile/shipping-profile APIs.
    for key in ("profiles", "profile", "profiledata", "shippingprofiles", "shippingprofile"):
        if key in compact_props:
            score += 9
            reasons.append(f"schema:{key}")

    # Direct identity/address shape. Require several independent fields before this
    # contributes enough to classify a strangely-named tool.
    identity = compact_props & {"firstname", "lastname", "fullname", "name", "phone", "phonenumber"}
    address = compact_props & {"address", "address1", "addressline1", "street", "city", "state", "province", "zip", "zipcode", "postal", "postalcode", "country"}
    if len(identity) >= 2:
        score += 5
        reasons.append("schema:identity")
    if len(address) >= 3:
        score += 7
        reasons.append("schema:address")
    if identity and address:
        score += 4
        reasons.append("schema:identity+address")

    # Array item schemas are common for bulk creation.
    for key, meta in props.items():
        if not isinstance(meta, dict) or str(meta.get("type") or "") != "array":
            continue
        item = meta.get("items") or {}
        item_props = item.get("properties") or {} if isinstance(item, dict) else {}
        if not isinstance(item_props, dict):
            continue
        item_keys = {_norm_field(k) for k in item_props}
        item_identity = item_keys & {"firstname", "lastname", "fullname", "name", "phone", "phonenumber"}
        item_address = item_keys & {"address", "address1", "addressline1", "street", "city", "state", "province", "zip", "zipcode", "postal", "postalcode", "country"}
        if _norm_field(key) in {"profiles", "profiledata", "shippingprofiles", "records", "items"} and (item_identity or item_address):
            score += 11
            reasons.append(f"array:{key}")
        if len(item_identity) >= 2 and len(item_address) >= 2:
            score += 8
            reasons.append(f"array-shape:{key}")

    return score, reasons


def _profile_write_tool(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find a Profile Builder create/generate tool using name, description AND schema.

    v3.6.7 intentionally made matching very strict after ``create_account_task`` was
    selected by mistake. AYCD can however expose a legitimate Profile Builder tool
    whose *name* does not literally contain ``profile``. This matcher keeps the hard
    exclusions, then accepts a tool only when its description/schema strongly proves
    that it creates profile/address records.
    """
    ranked: list[tuple[int, dict[str, Any]]] = []
    for tool in tools:
        name = str(tool.get("name") or "").casefold()
        desc = str(tool.get("description") or "").casefold()
        text = f"{name} {desc}"

        # Hard negative families. These must never be reinterpreted as Profile Builder.
        if re.search(r"(?:^|[_\- ])(?:account|task|checkout|order|cart|session|login|credential|password|payment|card)(?:$|[_\- ])", name):
            continue
        if re.search(r"duplicate|list|search|find|jig|distribute|spreadsheet|google.?forms|mass.?edit|delete|remove", name):
            continue
        if _is_read_only(tool):
            continue

        creation_name = bool(re.search(r"(?:^|[_\- ])(?:create|generate|add|build|import|insert|new|make)(?:$|[_\- ])", name))
        creation_desc = bool(re.search(r"\b(?:create|generate|add|build|import|insert|make)\b", desc))
        if not (creation_name or creation_desc):
            continue

        score = 0
        if creation_name:
            score += 8
        if creation_desc:
            score += 3
        if "profile" in name:
            score += 15
        if "profile builder" in text:
            score += 14
        elif "profile" in desc:
            score += 7
        if "shipping profile" in text or "billing profile" in text:
            score += 9
        if "address" in name and ("create" in name or "generate" in name):
            score += 3

        schema_score, _reasons = _schema_profile_signals(tool)
        score += schema_score

        # Required account-auth fields are a strong sign this is the wrong subsystem.
        required = {_norm_field(x) for x in _required_args(tool)}
        if required & {"username", "password", "login", "credential", "accountid", "taskid"}:
            continue

        # Safety threshold: either explicit profile semantics, or a strongly profile-
        # shaped schema plus a creation verb. This still rejects create_account_task.
        explicit_profile = ("profile" in name or "profile builder" in text or "shipping profile" in text or "billing profile" in text)
        if explicit_profile:
            if score < 18:
                continue
        else:
            if schema_score < 14 or score < 22:
                continue

        ranked.append((score, tool))

    ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("name") or "")))
    return ranked[0][1] if ranked else None


def _profile_tool_diagnostics(tools: list[dict[str, Any]], limit: int = 5) -> str:
    """Compact user-facing hints when no safe creation tool can be selected."""
    rows: list[tuple[int, str]] = []
    for tool in tools:
        name = str(tool.get("name") or "")
        low = name.casefold()
        if re.search(r"(?:account|task|checkout|order|cart|login|payment|card)", low):
            continue
        desc = str(tool.get("description") or "").casefold()
        schema_score, _ = _schema_profile_signals(tool)
        score = schema_score + (8 if "profile" in low else 0) + (5 if "profile" in desc else 0) + (4 if re.search(r"create|generate|build|add|make", low) else 0)
        if score > 0:
            rows.append((score, name))
    rows.sort(key=lambda x: (-x[0], x[1]))
    if not rows:
        return ""
    return " Closest non-blocked MCP tool names: " + ", ".join(name for _, name in rows[:limit]) + "."

def _duplicate_tool(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = []
    for tool in tools:
        text = (str(tool.get("name") or "") + " " + str(tool.get("description") or "")).casefold()
        score = (8 if "duplicate" in text else 0) + (4 if "profile" in text else 0) + (3 if _is_read_only(tool) else -4)
        if score >= 8: ranked.append((score, tool))
    ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("name") or "")))
    return ranked[0][1] if ranked else None


def _norm_field(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _profile_value_for_key(profile: dict[str, Any], key: str) -> tuple[bool, Any]:
    """Map common AYCD/profile-builder field spellings to our synthetic record."""
    compact = _norm_field(key)
    aliases = {
        "firstname": "first_name", "givenname": "first_name", "fname": "first_name",
        "lastname": "last_name", "surname": "last_name", "familyname": "last_name", "lname": "last_name",
        "name": "name", "fullname": "name", "profilename": "profile_name", "label": "profile_name",
        "phone": "phone", "phonenumber": "phone", "telephone": "phone",
        "address": "address1", "address1": "address1", "addressline1": "address1", "street": "address1", "street1": "address1",
        "address2": "address2", "addressline2": "address2", "street2": "address2", "apt": "address2", "apartment": "address2",
        "city": "city", "locality": "city",
        "state": "state", "province": "state", "region": "state",
        "zip": "postal_code", "zipcode": "postal_code", "postal": "postal_code", "postalcode": "postal_code",
        "country": "country", "countrycode": "country",
    }
    field = aliases.get(compact)
    if field and field in profile:
        return True, profile.get(field)
    return False, None


def _public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    # Defense in depth: never pass payment-like fields even if a future recipe adds them.
    return {k: v for k, v in profile.items() if not _PAYMENT_FIELD_RE.search(str(k).replace("-", "_"))}


def _profile_arguments(tool: dict[str, Any], job: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    payload = dict(job.get("payload") or {})
    profiles = [_public_profile(dict(p)) for p in list(payload.get("profiles") or [])]
    count = int(payload.get("count") or len(profiles))
    schema = tool.get("input_schema") or {}; props = schema.get("properties") or {} if isinstance(schema, dict) else {}
    if not isinstance(props, dict): props = {}
    args: dict[str, Any] = {}
    names = [str(p.get("name") or "") for p in profiles]
    first = [str(p.get("first_name") or "") for p in profiles]; last = [str(p.get("last_name") or "") for p in profiles]
    phones = [str(p.get("phone") or "") for p in profiles]
    addresses = [{k: p.get(k, "") for k in ("address1","address2","city","state","postal_code","country")} for p in profiles]
    for key, meta in props.items():
        low = str(key).casefold().replace("-", "_"); compact = _norm_field(key)
        typ = str((meta or {}).get("type") or "") if isinstance(meta, dict) else ""
        if _PAYMENT_FIELD_RE.search(low):
            continue
        if compact in {"count","amount","quantity","number","total","profilecount"} and typ in {"","integer","number"}: args[key] = count
        elif compact in {"profiles","items","records","profiledata","data","shippingprofiles"} and typ in {"","array"}: args[key] = profiles
        elif compact in {"names","profilenames","fullnames"} and typ in {"","array"}: args[key] = names
        elif compact in {"firstnames","givenNames".casefold()} and typ in {"","array"}: args[key] = first
        elif compact in {"lastnames","surnames","familynames"} and typ in {"","array"}: args[key] = last
        elif compact in {"phones","phonenumbers","telephones"} and typ in {"","array"}: args[key] = phones
        elif compact in {"addresses","shippingaddresses"} and typ in {"","array"}: args[key] = addresses
        elif compact in {"synthetic","syntheticonly","test","testdata","istest"} and typ in {"","boolean"}: args[key] = True
        elif compact in {"preview","dryrun"} and typ in {"","boolean"}: args[key] = False
    missing = [key for key in _required_args(tool) if key not in args]
    return args, missing


def _single_profile_arguments(tool: dict[str, Any], profile: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    schema = tool.get("input_schema") or {}; props = schema.get("properties") or {} if isinstance(schema, dict) else {}
    if not isinstance(props, dict): props = {}
    safe = _public_profile(dict(profile))
    args: dict[str, Any] = {}
    for key, meta in props.items():
        low = str(key).casefold().replace("-", "_"); compact = _norm_field(key)
        typ = str((meta or {}).get("type") or "") if isinstance(meta, dict) else ""
        if _PAYMENT_FIELD_RE.search(low):
            continue
        if compact in {"profile","record","item","profiledata","data"} and typ in {"","object"}:
            args[key] = safe; continue
        if compact in {"address","shippingaddress","billingaddress","shipping","billing"} and typ == "object":
            args[key] = {
                "address1": safe.get("address1", ""), "address2": safe.get("address2", ""),
                "city": safe.get("city", ""), "state": safe.get("state", ""),
                "postal_code": safe.get("postal_code", ""), "country": safe.get("country", ""),
            }; continue
        found, value = _profile_value_for_key(safe, key)
        if found:
            args[key] = value
        elif compact in {"synthetic","syntheticonly","test","testdata","istest"} and typ in {"","boolean"}:
            args[key] = True
    missing = [key for key in _required_args(tool) if key not in args]
    return args, missing


def _profile_call_plan(tool: dict[str, Any], job: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    """Return (mode, bulk_args, missing). mode is bulk or per_profile."""
    bulk_args, bulk_missing = _profile_arguments(tool, job)
    props = (tool.get("input_schema") or {}).get("properties") or {}
    if not isinstance(props, dict): props = {}
    # Any explicit array of profile-ish objects means the tool can receive the whole batch.
    for key, meta in props.items():
        compact = _norm_field(key); typ = str((meta or {}).get("type") or "") if isinstance(meta, dict) else ""
        if typ == "array" and compact in {"profiles","items","records","profiledata","data","shippingprofiles"}:
            return "bulk", bulk_args, bulk_missing
    # If all required args were satisfied by a count/names/phones/etc bulk shape, use one call.
    if not bulk_missing and any(_norm_field(k) in {"count","amount","quantity","number","total","profilecount","names","profilenames","fullnames","phones","addresses"} for k in props):
        return "bulk", bulk_args, []
    profiles = list((job.get("payload") or {}).get("profiles") or [])
    if profiles:
        _one_args, one_missing = _single_profile_arguments(tool, profiles[0])
        if not one_missing:
            return "per_profile", {}, []
    return "bulk", bulk_args, bulk_missing


def _normalize_action_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().casefold()).replace("_", "-")
    for name, spec in _ACTION_SPECS.items():
        if text == name.replace("_", "-") or text in {str(x).casefold() for x in spec.get("aliases") or ()}:
            return name
    return ""


def _find_action_tool(tools: list[dict[str, Any]], action: str) -> dict[str, Any] | None:
    spec = _ACTION_SPECS.get(action) or {}; ranked = []
    for tool in tools:
        text = re.sub(r"[_\-.]+", " ", str(tool.get("name") or "") + " " + str(tool.get("description") or "")).casefold()
        score = 0; label = str(spec.get("label") or "").casefold()
        if label and label in text: score += 30
        must = list(spec.get("must") or ())
        if must and not all(str(x).casefold() in text for x in must):
            continue
        if must: score += 18 + len(must) * 2
        score += sum(5 for x in spec.get("boost") or () if str(x).casefold() in text)
        score -= sum(12 for x in spec.get("avoid") or () if str(x).casefold() in text)
        if "profile" in text: score += 4
        if _is_read_only(tool): score -= 15
        if score > 0: ranked.append((score, tool))
    ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("name") or "")))
    return ranked[0][1] if ranked else None


def _profile_ids_from_value(value: Any) -> list[str]:
    ids: list[str] = []; seen: set[str] = set()
    def add(v: Any) -> None:
        if isinstance(v, (str, int)) and str(v).strip() and str(v).strip() not in seen:
            item = str(v).strip(); seen.add(item); ids.append(item)
    def walk(node: Any, parent: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                compact = re.sub(r"[^a-z0-9]", "", str(k).casefold())
                if compact in {"profileid","profileids"} or ("profile" in compact and compact.endswith("id")):
                    if isinstance(v, list):
                        for x in v: add(x)
                    else: add(v)
                elif "profile" in parent.casefold() and compact == "id": add(v)
                walk(v, str(k))
        elif isinstance(node, list):
            for item in node: walk(item, parent)
    walk(value); return ids[:500]


def _coerce_param(value: str, meta: dict[str, Any]) -> Any:
    raw = str(value or "").strip(); typ = str(meta.get("type") or "").casefold() if isinstance(meta, dict) else ""
    if raw.startswith("{") or raw.startswith("["):
        try: return json.loads(raw)
        except json.JSONDecodeError: pass
    if typ == "boolean": return raw.casefold() in {"1","true","yes","on","y"}
    if typ == "integer": return int(raw)
    if typ == "number": return float(raw)
    if typ == "array": return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def _parse_kv_params(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pattern = re.compile(r'''([A-Za-z_][A-Za-z0-9_.-]*)=("[^"]*"|'[^']*'|\{[^\n]*?\}|\[[^\n]*?\]|\S+)''')
    for m in pattern.finditer(str(text or "")):
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}: val = val[1:-1]
        out[m.group(1)] = val
    return out


def _root_profile_job(chat_id: int, job: dict[str, Any]) -> dict[str, Any] | None:
    current = job; visited: set[str] = set()
    while current and str(current.get("kind") or "").startswith("profile_action:"):
        code = str((current.get("payload") or {}).get("parent_job_code") or "").upper()
        if not code or code in visited: break
        visited.add(code); current = aycd_job(code, chat_id) or {}
    return current if current and str(current.get("kind") or "") == "profiles_generate" else None


def _action_arguments(tool: dict[str, Any], root_job: dict[str, Any], params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    payload = dict(root_job.get("payload") or {}); profiles = list(payload.get("profiles") or [])
    ids = _profile_ids_from_value(payload.get("last_structured")); count = int(payload.get("count") or len(profiles))
    schema = tool.get("input_schema") or {}; props = schema.get("properties") or {} if isinstance(schema, dict) else {}
    if not isinstance(props, dict): props = {}
    args: dict[str, Any] = {}; lookup = {str(k).casefold(): k for k in props}
    for key, value in dict(params or {}).items():
        actual = lookup.get(str(key).casefold(), key if key in props else None)
        if actual is not None: args[actual] = _coerce_param(str(value), props.get(actual) or {})
    names = [str(p.get("name") or "") for p in profiles]
    for key, meta in props.items():
        if key in args: continue
        low = re.sub(r"[^a-z0-9]", "", str(key).casefold()); typ = str((meta or {}).get("type") or "") if isinstance(meta, dict) else ""
        if low in {"profileids","ids","selectedprofileids"} and ids and typ in {"","array"}: args[key] = ids
        elif low in {"profileid","selectedprofileid"} and len(ids) == 1 and typ in {"","string","integer"}: args[key] = ids[0]
        elif low in {"profiles","items","records","profiledata","data"} and typ in {"","array"}: args[key] = profiles
        elif low in {"names","profilenames","fullnames"} and typ in {"","array"}: args[key] = names
        elif low in {"count","amount","quantity","number","total","profilecount"} and typ in {"","integer","number"}: args[key] = count
        elif low in {"jobid","jobcode","sourcejob","sourcejobid"} and typ in {"","string"}: args[key] = str(root_job.get("job_code") or "")
        elif low in {"synthetic","syntheticonly","test","testdata","istest"} and typ in {"","boolean"}: args[key] = True
        elif low in {"preview","dryrun"} and typ in {"","boolean"}: args[key] = False
    return args, [key for key in _required_args(tool) if key not in args]


def _create_action_job(chat_id: int, root: dict[str, Any], action: str, request_text: str, source: str,
                       user_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    action = _normalize_action_name(action)
    if not action: raise ValueError("Unknown AYCD Profile Builder action.")
    tool = _find_action_tool(mcp_tools(MCP_DEFAULT_SERVER_ID), action); params = dict(params or {}); spec = _ACTION_SPECS[action]
    payload = {
        "action": action, "action_label": spec["label"], "parent_job_code": str(root.get("job_code") or ""),
        "count": int((root.get("payload") or {}).get("count") or 0), "profiles": list((root.get("payload") or {}).get("profiles") or []),
        "parameters": params, "tool_name": str(tool.get("name") or "") if tool else "", "arguments": {}, "missing": [],
    }
    if tool:
        args, missing = _action_arguments(tool, root, params); payload["arguments"] = args; payload["missing"] = missing
    status = "waiting_tool" if not tool else ("waiting_input" if payload["missing"] else "preview_ready")
    error = f"AYCD did not expose an obvious {spec['label']} MCP tool. Run !aycd tools to inspect the live names." if not tool else ("Required AYCD fields still needed: " + ", ".join(payload["missing"]) if payload["missing"] else "")
    return _create_job(chat_id, f"profile_action:{action}", status, request_text, payload, source, user_id, error)


def _refresh_action_job(chat_id: int, job: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job.get("payload") or {}); action = _normalize_action_name(str(payload.get("action") or "")); root = _root_profile_job(chat_id, job)
    if not action or not root: return _update_job(str(job.get("job_code") or ""), status="failed", error="The source profile job could not be resolved.")
    tool = _find_action_tool(mcp_tools(MCP_DEFAULT_SERVER_ID, refresh=True), action)
    if not tool: return _update_job(str(job.get("job_code") or ""), status="waiting_tool", error=f"AYCD did not expose an obvious {_ACTION_SPECS[action]['label']} tool.")
    args, missing = _action_arguments(tool, root, dict(payload.get("parameters") or {}))
    payload.update({"tool_name": str(tool.get("name") or ""), "arguments": args, "missing": missing})
    return _update_job(str(job.get("job_code") or ""), status="waiting_input" if missing else "preview_ready", payload=payload,
                       error=("Required AYCD fields still needed: " + ", ".join(missing)) if missing else "")


def set_aycd_job_parameters(chat_id: int, job_code: str, values: dict[str, Any], actor_id: str = "") -> dict[str, Any]:
    job = aycd_job(job_code, chat_id)
    if not job: raise ValueError("AYCD job not found.")
    creator = str(job.get("created_by_user") or "")
    if creator and actor_id and creator != str(actor_id): raise PermissionError("Only the Discord user who created this AYCD job can change its parameters.")
    if not str(job.get("kind") or "").startswith("profile_action:"): raise ValueError("!aycd set is for Profile Builder action jobs.")
    payload = dict(job.get("payload") or {}); params = dict(payload.get("parameters") or {}); params.update(values); payload["parameters"] = params
    _update_job(job_code, payload=payload)
    return _refresh_action_job(chat_id, aycd_job(job_code, chat_id) or job)


def _preview_lines(job: dict[str, Any], full: bool = False) -> str:
    profiles = list((job.get("payload") or {}).get("profiles") or []); limit = len(profiles) if full else min(len(profiles), AYCD_PREVIEW_COUNT)
    lines: list[str] = []
    for i in range(limit):
        p = profiles[i]
        addr = ", ".join(x for x in [str(p.get("address1") or ""), str(p.get("city") or ""), str(p.get("state") or ""), str(p.get("postal_code") or "")] if x)
        lines.append(f"{i+1}. {p.get('name','')} · {p.get('phone','')} · {addr}")
    if len(profiles) > limit: lines.append(f"… +{len(profiles)-limit} more")
    return "\n".join(lines)


def aycd_job_reactions(job: dict[str, Any] | None) -> list[str]:
    if not job: return []
    status = str(job.get("status") or ""); kind = str(job.get("kind") or "")
    if status == "preview_ready": return ["✅","👀","🎲","🔍","❌"] if kind == "profiles_generate" else ["✅","👀","🔄","❌","📋"]
    if status == "waiting_input": return ["📋","🔄","❌"]
    if status == "running": return ["📋","🛑"]
    if status == "completed": return ["🧩","🏠","✏️","📦","📊","📝","🔍","📋","💾"] if kind == "profiles_generate" else ["📋","💾"]
    return ["📋"]


def aycd_job_text(job: dict[str, Any], *, full_preview: bool = False) -> str:
    if not job: return "AYCD job not found."
    payload = dict(job.get("payload") or {}); status = str(job.get("status") or "unknown"); kind = str(job.get("kind") or "")
    status_label = {"preview_ready":"Waiting for approval","waiting_input":"Needs required AYCD fields","running":"Running","completed":"Completed","cancelled":"Cancelled","failed":"Failed","stopped":"Stopped","waiting_tool":"Needs tool/schema review"}.get(status, status.replace("_"," ").title())
    if kind.startswith("profile_action:"):
        action = _normalize_action_name(str(payload.get("action") or "")); spec = _ACTION_SPECS.get(action) or {"label": action or "Profile action", "emoji":"⚙️"}
        text = f"{spec['emoji']} **AYCD PROFILE ACTION {job.get('job_code')}**\nAction: **{spec['label']}**\nSource batch: **{payload.get('parent_job_code') or '?'}**\nStatus: **{status_label}**\n"
        if payload.get("tool_name"): text += f"Tool: `{payload['tool_name']}`\n"
        if payload.get("arguments"): text += f"Prepared arguments: `{json.dumps(payload['arguments'],ensure_ascii=False,default=str)[:1800]}`\n"
        missing = list(payload.get("missing") or [])
        if missing: text += "\n**Still required by AYCD**\n" + "\n".join(f"• `{x}`" for x in missing) + f"\n\nSet values with: `!aycd set {job.get('job_code')} field=value`"
        if job.get("result_text"): text += "\n\n**Result**\n" + str(job["result_text"])[:6000]
        if job.get("error") and not missing: text += "\n\n⚠️ " + str(job["error"])[:1800]
        if status == "preview_ready": text += f"\n\n✅ Approve · 🔄 Re-check schema · ❌ Cancel\n`!aycd approve {job.get('job_code')}`"
        return text
    count = int(payload.get("count") or 0)
    text = f"🧩 **AYCD PROFILE JOB {job.get('job_code')}**\nRequested: **{count}** synthetic/test profile(s)\nFields: **name · phone · address** (no email/payment/card data)\nStatus: **{status_label}**\n"
    progress = dict(payload.get("progress") or {})
    if status == "running" and progress:
        text += f"Progress: **{int(progress.get('processed') or 0)}/{int(progress.get('total') or count)}** · succeeded {int(progress.get('succeeded') or 0)} · failed {int(progress.get('failed') or 0)}\n"
    if status in {"preview_ready","running","waiting_tool"}: text += "\n**Preview**\n" + (_preview_lines(job, full_preview) or "(empty)")
    if job.get("result_text"): text += "\n\n**Result**\n" + str(job["result_text"])[:5000]
    if job.get("error"): text += "\n\n⚠️ " + str(job["error"])[:1500]
    planned = [x for x in list(payload.get("planned_actions") or []) if x in _ACTION_SPECS]
    if planned: text += "\n\nRecipe workflow: **" + " → ".join(_ACTION_SPECS[x]["label"] for x in planned) + "**"
    if status == "preview_ready": text += f"\n\n✅ Create · 👀 Full preview · 🎲 Regenerate · 🔍 Duplicate check · ❌ Cancel\n`!aycd approve {job.get('job_code')}`"
    elif status == "completed": text += f"\n\n**Next Profile Builder actions**\n🧩 `!aycd jig {job.get('job_code')}` · 🏠 `!aycd address-jig {job.get('job_code')}` · ✏️ `!aycd edit {job.get('job_code')}` · 📦 `!aycd distribute {job.get('job_code')}`\n📊 `!aycd spreadsheet {job.get('job_code')}` · 📝 `!aycd forms {job.get('job_code')}` · 🔍 duplicate check"
    return text


def aycd_help_text() -> str:
    return (
        "**AYCD · Zeno Profile Builder v3 / Control Center**\n"
        "`!aycd` → Control Center · `!aycd status` · `!aycd tools` · `!aycd profiles`\n"
        "`!aycd generate 100` → generate a synthetic profile batch (name/phone/address, never email/card data; max 10,000)\n`!aycd approve A001` → create the approved batch in AYCD using its live MCP schema\n"
        "`!aycd workflow A001` → inspect live Profile Builder tool matches\n"
        "`!aycd jig A001` · `!aycd address-jig A001` · `!aycd edit A001`\n"
        "`!aycd distribute A001` · `!aycd spreadsheet A001` · `!aycd forms A001`\n"
        "`!aycd set A002 field=value` → provide a required AYCD field\n"
        "`!aycd duplicates [A001]` · `!aycd jobs active|waiting|approval|completed|failed|recent`\n"
        "`!aycd job A001` · `!aycd job latest` · `!aycd stop latest` · `!aycd approve latest`\n"
        "`!aycd ongoing` · `!aycd available`\n"
        "`!aycd recipe list` · `!aycd recipe show <name>` · `!aycd recipe save testing A001`\n\n"
        "**Reactions**\n✅ approve · ❌ cancel · 👀 preview · 🎲 regenerate · 🔄 refresh schema · 🔍 validate · 📋 details\n"
        "🧩 Basic Jig · 🏠 AI Address Jig · ✏️ Mass Edit/Jig · 📦 Mass Distribute · 📊 Spreadsheets · 📝 Google Forms · 💾 save recipe · 🛑 stop\n\n"
        "Write actions always preview first. Zeno never invents required AYCD IDs/settings."
    )


def aycd_status_text() -> str:
    try:
        server = mcp_public_server(MCP_DEFAULT_SERVER_ID); status = dict(server.get("status") or {})
        return f"**AYCD MCP status**\nConnection: **{'Connected' if status.get('connected') else 'Not connected'}**\nEnabled: **{'yes' if server.get('enabled') else 'no'}**\nEndpoint: `{server.get('url')}`\nTools: **{int(server.get('tools_count') or 0)}**\nLatency: **{int(status.get('latency_ms') or 0)} ms**" + (f"\nError: `{status.get('error')}`" if status.get('error') else "")
    except Exception as exc:
        return f"⚠️ AYCD MCP status failed: {exc}"


def aycd_tools_text(refresh: bool = False) -> str:
    try: tools = mcp_tools(MCP_DEFAULT_SERVER_ID, refresh=refresh)
    except Exception as exc: return f"⚠️ Could not discover AYCD tools: {exc}"
    if not tools: return "AYCD MCP is reachable, but no tools were discovered."
    lines = [f"**AYCD MCP tools · {len(tools)}**"]
    for tool in tools[:60]:
        name = str(tool.get("name") or "unnamed"); desc = re.sub(r"\s+"," ",str(tool.get("description") or "")).strip(); mode = "read" if _is_read_only(tool) else "write/unknown"
        lines.append(f"`{name}` · {mode}" + (f" — {desc[:180]}" if desc else ""))
    return "\n".join(lines + ([f"… +{len(tools)-60} more"] if len(tools) > 60 else []))


def _run_duplicate_check(chat_id: int, source: str = "chat", job: dict[str, Any] | None = None) -> str:
    tools = mcp_tools(MCP_DEFAULT_SERVER_ID); tool = _duplicate_tool(tools)
    if not tool:
        local = ""
        if job:
            names = [str(x.get("name") or "").casefold() for x in list((job.get("payload") or {}).get("profiles") or [])]
            local = f"Local preview duplicates: **{len(names)-len(set(names))}**. "
        return local + "AYCD did not expose an obvious read-only profile duplicate tool. Use `!aycd tools` to inspect the live names."
    req = _required_args(tool)
    if req: return f"AYCD duplicate tool `{tool.get('name')}` requires arguments ({', '.join(req)}), so Zeno will not invent them."
    result = call_mcp_tool(chat_id, MCP_DEFAULT_SERVER_ID, str(tool.get("name") or ""), {}, source=source)
    return f"🔍 **AYCD duplicate check · `{tool.get('name')}`**\n{str(result.get('result') or '')[:9000]}"


def execute_aycd_job(chat_id: int, job_code: str, *, source: str = "chat", actor_id: str = "") -> dict[str, Any]:
    job = aycd_job(job_code, chat_id)
    if not job: raise ValueError("AYCD job not found.")
    if str(job.get("status")) in {"completed","cancelled","stopped"}: return job
    creator = str(job.get("created_by_user") or "")
    if creator and actor_id and creator != str(actor_id): raise PermissionError("Only the Discord user who created this AYCD job can approve it.")
    kind = str(job.get("kind") or "")
    if kind.startswith("profile_action:"):
        job = _refresh_action_job(chat_id, job); payload = dict(job.get("payload") or {})
        if str(job.get("status")) in {"waiting_input","waiting_tool","failed"}: return job
        tool_name = str(payload.get("tool_name") or ""); args = dict(payload.get("arguments") or {})
        stop_event = threading.Event(); _ACTIVE_EVENTS[str(job_code).upper()] = stop_event; _update_job(job_code,status="running",error="")
        try:
            result = call_mcp_tool(chat_id,MCP_DEFAULT_SERVER_ID,tool_name,args,stop_event=stop_event,source=source)
            payload.update({"last_structured":_json_safe(result.get("structured")),"last_call_id":result.get("call_id"),"last_tool":tool_name})
            updated = _update_job(job_code,status="completed",payload=payload,result=f"Tool: {tool_name}\n{str(result.get('result') or '')[:AYCD_RESULT_LIMIT]}",error="")
            root = _root_profile_job(chat_id, updated)
            if root:
                rp = dict(root.get("payload") or {}); hist = list(rp.get("action_history") or []); hist.append({"action":payload.get("action"),"job_code":job_code,"status":"completed","tool":tool_name,"parameters":dict(payload.get("parameters") or {})}); rp["action_history"] = hist[-30:]; _update_job(str(root.get("job_code") or ""),payload=rp)
            return updated
        except InterruptedError as exc: return _update_job(job_code,status="stopped",error=str(exc))
        except Exception as exc: return _update_job(job_code,status="failed",error=f"{type(exc).__name__}: {exc}")
        finally: _ACTIVE_EVENTS.pop(str(job_code).upper(),None)
    tool = _profile_write_tool(mcp_tools(MCP_DEFAULT_SERVER_ID))
    if not tool:
        live_tools = mcp_tools(MCP_DEFAULT_SERVER_ID)
        hint = _profile_tool_diagnostics(live_tools)
        return _update_job(job_code,status="waiting_tool",error="AYCD MCP is connected, but Zeno could not safely identify a Profile Builder create/generate tool." + hint + " Run !aycd tools if you want to inspect the full live tool list.")
    mode, args, missing = _profile_call_plan(tool, job)
    if missing:
        cardish = [x for x in missing if _PAYMENT_FIELD_RE.search(str(x).replace("-", "_"))]
        emailish = [x for x in missing if "email" in _norm_field(str(x))]
        if cardish:
            return _update_job(job_code,status="waiting_tool",error=f"Selected AYCD tool `{tool.get('name')}` requires payment/card fields ({', '.join(cardish)}). Zeno's synthetic generator intentionally never creates card data.")
        if emailish:
            return _update_job(job_code,status="waiting_tool",error=f"Selected AYCD Profile Builder tool `{tool.get('name')}` requires email field(s) ({', '.join(emailish)}). Email generation is disabled by design; choose/configure a profile tool where email is optional.")
        return _update_job(job_code,status="waiting_tool",error=f"Selected AYCD tool `{tool.get('name')}` requires fields Zeno cannot safely infer: {', '.join(missing)}.")
    stop_event = threading.Event(); _ACTIVE_EVENTS[str(job_code).upper()] = stop_event
    payload = dict(job.get("payload") or {}); payload["execution_mode"] = mode; payload["progress"] = {"processed":0,"total":int(payload.get("count") or 0),"succeeded":0,"failed":0}
    _update_job(job_code,status="running",payload=payload,error="")
    try:
        tool_name = str(tool.get("name") or "")
        if mode == "bulk":
            result = call_mcp_tool(chat_id,MCP_DEFAULT_SERVER_ID,tool_name,args,stop_event=stop_event,source=source)
            payload.update({"last_structured":_json_safe(result.get("structured")),"last_call_id":result.get("call_id"),"last_tool":tool_name})
            payload["progress"] = {"processed":int(payload.get("count") or 0),"total":int(payload.get("count") or 0),"succeeded":int(payload.get("count") or 0),"failed":0}
            return _update_job(job_code,status="completed",payload=payload,result=f"Tool: {tool_name}\nMode: bulk\n{str(result.get('result') or '')[:AYCD_RESULT_LIMIT]}",error="")

        profiles = [_public_profile(dict(p)) for p in list(payload.get("profiles") or [])]
        outputs: list[str] = []; structured: list[Any] = []; call_ids: list[Any] = []; succeeded = failed = 0
        for idx, profile in enumerate(profiles, start=1):
            if stop_event.is_set(): raise InterruptedError("AYCD profile batch was stopped.")
            one_args, one_missing = _single_profile_arguments(tool, profile)
            if one_missing:
                failed += 1; outputs.append(f"#{idx} skipped: missing {', '.join(one_missing)}")
            else:
                try:
                    result = call_mcp_tool(chat_id,MCP_DEFAULT_SERVER_ID,tool_name,one_args,stop_event=stop_event,source=source)
                    succeeded += 1; call_ids.append(result.get("call_id")); structured.append(_json_safe(result.get("structured")))
                    text = re.sub(r"\s+", " ", str(result.get("result") or "")).strip()
                    if text: outputs.append(f"#{idx}: {text[:350]}")
                except Exception as exc:
                    failed += 1; outputs.append(f"#{idx} failed: {type(exc).__name__}: {exc}")
            payload["progress"] = {"processed":idx,"total":len(profiles),"succeeded":succeeded,"failed":failed}
            if idx == len(profiles) or idx % 5 == 0:
                _update_job(job_code,status="running",payload=payload,error="")
        payload.update({"last_structured":structured[-100:],"last_call_id":call_ids[-1] if call_ids else None,"last_call_ids":call_ids[-100:],"last_tool":tool_name})
        status = "completed" if succeeded else "failed"
        summary = f"Tool: {tool_name}\nMode: per-profile\nCreated: {succeeded}/{len(profiles)}\nFailed: {failed}"
        if outputs: summary += "\n\n" + "\n".join(outputs[-40:])
        return _update_job(job_code,status=status,payload=payload,result=summary,error="" if succeeded else "No profiles were created.")
    except InterruptedError as exc: return _update_job(job_code,status="stopped",payload=payload,error=str(exc))
    except Exception as exc: return _update_job(job_code,status="failed",payload=payload,error=f"{type(exc).__name__}: {exc}")
    finally: _ACTIVE_EVENTS.pop(str(job_code).upper(),None)


def stop_aycd_job(chat_id: int, job_code: str) -> dict[str, Any]:
    job = aycd_job(job_code, chat_id)
    if not job: raise ValueError("AYCD job not found.")
    event = _ACTIVE_EVENTS.get(str(job_code).upper())
    if event: event.set(); return _update_job(job_code,status="stopped",error="Stop requested by user.")
    if str(job.get("status")) in {"preview_ready","waiting_input","waiting_tool"}: return _update_job(job_code,status="cancelled",error="")
    return job


def reroll_aycd_job(chat_id: int, job_code: str, actor_id: str = "") -> dict[str, Any]:
    job = aycd_job(job_code, chat_id)
    if not job: raise ValueError("AYCD job not found.")
    if str(job.get("kind") or "") != "profiles_generate": return _refresh_action_job(chat_id, job)
    creator = str(job.get("created_by_user") or "")
    if creator and actor_id and creator != str(actor_id): raise PermissionError("Only the Discord user who created this AYCD job can regenerate it.")
    payload = dict(job.get("payload") or {}); payload["profiles"] = _unique_profiles(int(payload.get("count") or 10))
    return _update_job(job_code,status="preview_ready",payload=payload,result="",error="")


def cancel_aycd_job(chat_id: int, job_code: str, actor_id: str = "") -> dict[str, Any]:
    job = aycd_job(job_code, chat_id)
    if not job: raise ValueError("AYCD job not found.")
    creator = str(job.get("created_by_user") or "")
    if creator and actor_id and creator != str(actor_id): raise PermissionError("Only the Discord user who created this AYCD job can cancel it.")
    return stop_aycd_job(chat_id, job_code)


def _latest_completed_profile_job(chat_id: int) -> dict[str, Any] | None:
    return next((j for j in aycd_jobs(chat_id,50) if j.get("kind") == "profiles_generate" and j.get("status") == "completed"), None)


def _action_plan_text(chat_id: int, root: dict[str, Any]) -> str:
    tools = mcp_tools(MCP_DEFAULT_SERVER_ID); lines = [f"**AYCD Profile Builder workflow · {root.get('job_code')}**"]
    for name, spec in _ACTION_SPECS.items():
        tool = _find_action_tool(tools,name)
        if not tool: lines.append(f"{spec['emoji']} **{spec['label']}** · no obvious live MCP tool found"); continue
        _args, missing = _action_arguments(tool,root,dict((root.get("payload") or {}).get("action_defaults") or {}).get(name,{}) if isinstance((root.get("payload") or {}).get("action_defaults"),dict) else {})
        lines.append(f"{spec['emoji']} **{spec['label']}** → `{tool.get('name')}` · " + (f"needs: {', '.join(missing)}" if missing else "ready for preview/approval"))
    lines.append("\nUse `!aycd <action> A001 key=value`. Zeno never invents required IDs/settings.")
    return "\n".join(lines)


def aycd_reaction_action(chat_id: int, job_code: str, emoji: str, *, actor_id: str = "", source: str = "discord") -> dict[str, Any]:
    job = aycd_job(job_code, chat_id)
    if not job: return {"ok":False,"text":"That AYCD job no longer exists.","job":None}
    try:
        creator = str(job.get("created_by_user") or "")
        if creator and actor_id and creator != str(actor_id): raise PermissionError("Only the Discord user who created this AYCD job can control it.")
        if emoji == "✅": updated = execute_aycd_job(chat_id,job_code,source=source,actor_id=actor_id); return {"ok":True,"text":aycd_job_text(updated),"job":updated}
        if emoji == "❌": updated = cancel_aycd_job(chat_id,job_code,actor_id); return {"ok":True,"text":aycd_job_text(updated),"job":updated}
        if emoji == "🎲": updated = reroll_aycd_job(chat_id,job_code,actor_id); return {"ok":True,"text":aycd_job_text(updated),"job":updated}
        if emoji == "🔄": updated = reroll_aycd_job(chat_id,job_code,actor_id); return {"ok":True,"text":aycd_job_text(updated),"job":updated}
        if emoji == "👀": return {"ok":True,"text":aycd_job_text(job,full_preview=True),"job":job}
        if emoji == "🔍": return {"ok":True,"text":_run_duplicate_check(chat_id,source,_root_profile_job(chat_id,job) or job),"job":job}
        if emoji == "📋": return {"ok":True,"text":aycd_job_text(job),"job":job}
        if emoji == "🛑": updated=stop_aycd_job(chat_id,job_code); return {"ok":True,"text":aycd_job_text(updated),"job":updated}
        if emoji == "💾":
            root = _root_profile_job(chat_id,job) or job; recipe=save_aycd_recipe(chat_id,f"job-{str(root.get('job_code') or job_code).casefold()}",dict(root.get("payload") or {})); return {"ok":True,"text":f"💾 Saved recipe `{recipe.get('name')}`.","job":job}
        action = _ACTION_EMOJI_TO_NAME.get(emoji)
        if action:
            root = _root_profile_job(chat_id,job) or (job if job.get("kind") == "profiles_generate" else None)
            if not root or root.get("status") != "completed": return {"ok":False,"text":"Create the profile batch first, then run Profile Builder actions.","job":job}
            defaults = dict((root.get("payload") or {}).get("action_defaults") or {}).get(action,{})
            child = _create_action_job(chat_id,root,action,f"reaction {emoji} from {job_code}",source,actor_id,defaults if isinstance(defaults,dict) else {})
            return {"ok":True,"text":aycd_job_text(child),"job":child}
        return {"ok":False,"text":"That reaction is not active for this AYCD job.","job":job}
    except Exception as exc:
        return {"ok":False,"text":f"⚠️ AYCD reaction failed: {exc}","job":job}


def stop_aycd_jobs_for_chat(chat_id: int, reason: str = "Stop All Chat Work") -> dict[str, int]:
    affected = 0
    for job in aycd_jobs(chat_id,100):
        if str(job.get("status") or "") not in {"preview_ready","waiting_input","running","waiting_tool"}: continue
        event = _ACTIVE_EVENTS.get(str(job.get("job_code") or "").upper())
        if event: event.set()
        _update_job(str(job.get("job_code") or ""),status="stopped",error=str(reason)[:500]); affected += 1
    return {"aycd_job_count":affected}


def _count_from_text(text: str, default: int = 10) -> int:
    m=_RE_COUNT.search(text); return max(1,min(int(m.group(1)),AYCD_PROFILE_MAX_COUNT)) if m else default


def _job_code_from_text(text: str) -> str:
    m=_RE_JOB.search(text); return f"A{int(m.group(1)):03d}" if m else ""


def _action_from_command(rest: str) -> tuple[str,str]:
    low = re.sub(r"\s+"," ",str(rest).strip().casefold()); candidates=[]
    for action,spec in _ACTION_SPECS.items():
        for alias in spec.get("aliases") or ():
            a=str(alias).casefold()
            if low == a or low.startswith(a+" "): candidates.append((len(a),action,a))
    if not candidates: return "",""
    _,action,alias=sorted(candidates,reverse=True)[0]; return action,str(rest).strip()[len(alias):].strip()


def _job_label(job: dict[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    action = str(payload.get("action") or "")
    if action in _ACTION_SPECS:
        return str(_ACTION_SPECS[action]["label"])
    count = int(payload.get("count") or 0)
    if str(job.get("kind") or "") == "profiles_generate":
        return f"Generate {count} profile{'s' if count != 1 else ''}"
    return str(job.get("kind") or "AYCD job").replace("_", " ").replace(":", " · ").title()


def _status_emoji(status: str) -> str:
    return {
        "running": "🟢",
        "preview_ready": "🟡",
        "waiting_input": "🟠",
        "waiting_tool": "🟣",
        "completed": "✅",
        "failed": "🔴",
        "stopped": "🛑",
        "cancelled": "⚫",
    }.get(str(status or ""), "⚪")


def _status_bucket(status: str) -> str:
    value = str(status or "").casefold()
    if value == "running": return "active"
    if value == "preview_ready": return "approval"
    if value in {"waiting_input", "waiting_tool"}: return "waiting"
    if value == "completed": return "completed"
    if value in {"failed", "stopped", "cancelled"}: return "failed"
    return "other"


def _latest_job(chat_id: int, *, buckets: set[str] | None = None, kind: str = "") -> dict[str, Any] | None:
    for job in aycd_jobs(chat_id, 100):
        if buckets and _status_bucket(str(job.get("status") or "")) not in buckets:
            continue
        if kind and str(job.get("kind") or "") != kind:
            continue
        return job
    return None


def _resolve_job_reference(chat_id: int, text: str, *, buckets: set[str] | None = None, kind: str = "") -> dict[str, Any] | None:
    code = _job_code_from_text(text)
    if code:
        return aycd_job(code, chat_id)
    if "latest" in str(text or "").casefold() or not str(text or "").strip():
        return _latest_job(chat_id, buckets=buckets, kind=kind)
    return None


def _job_line(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "unknown")
    code = str(job.get("job_code") or "?")
    label = _job_label(job)
    extra = ""
    payload = dict(job.get("payload") or {})
    if status == "waiting_input" and payload.get("missing"):
        extra = " · needs " + ", ".join(str(x) for x in list(payload.get("missing") or [])[:3])
    elif status == "preview_ready":
        extra = " · awaiting approval"
    return f"{_status_emoji(status)} `{code}` · **{label}** · {status.replace('_',' ')}{extra}"


def _jobs_text(chat_id: int, filter_name: str = "recent") -> str:
    filter_key = str(filter_name or "recent").strip().casefold()
    aliases = {"all": "recent", "running": "active", "pending": "waiting", "approve": "approval", "approvals": "approval", "done": "completed", "errors": "failed"}
    filter_key = aliases.get(filter_key, filter_key)
    valid = {"recent", "active", "waiting", "approval", "completed", "failed"}
    if filter_key not in valid:
        return "Unknown AYCD jobs filter. Use `active`, `waiting`, `approval`, `completed`, `failed`, or `recent`."
    jobs = aycd_jobs(chat_id, 60)
    if filter_key != "recent":
        jobs = [job for job in jobs if _status_bucket(str(job.get("status") or "")) == filter_key]
    jobs = jobs[:15]
    title = "Recent" if filter_key == "recent" else filter_key.title()
    if not jobs:
        return f"**AYCD jobs · {title}**\nNo matching AYCD jobs."
    return "\n".join([f"**AYCD jobs · {title}**"] + [_job_line(job) for job in jobs] + ["", "`!aycd job A001` · `!aycd job latest`"]) 


def _dashboard_text(chat_id: int) -> str:
    jobs = aycd_jobs(chat_id, 80)
    counts = {key: 0 for key in ("active", "waiting", "approval", "completed", "failed")}
    for job in jobs:
        bucket = _status_bucket(str(job.get("status") or ""))
        if bucket in counts: counts[bucket] += 1
    try:
        server = mcp_public_server(MCP_DEFAULT_SERVER_ID)
        st = dict(server.get("status") or {})
        connected = bool(st.get("connected"))
        tools = int(server.get("tools_count") or 0)
        latency = int(st.get("latency_ms") or 0)
        health = f"{'🟢 Connected' if connected else '🔴 Not connected'} · {tools} tools" + (f" · {latency} ms" if connected and latency else "")
    except Exception as exc:
        health = f"🔴 Status error · {str(exc)[:120]}"
    recipes = list_aycd_recipes(chat_id)
    lines = [
        "**🔌 AYCD CONTROL CENTER**",
        f"MCP: **{health}**",
        f"Jobs: 🟢 **{counts['active']} active** · 🟠 **{counts['waiting']} waiting** · 🟡 **{counts['approval']} approval** · ✅ **{counts['completed']} completed** · 🔴 **{counts['failed']} failed**",
        f"Recipes: **{len(recipes)}**",
    ]
    groups = [
        ("ACTIVE", "active", 3),
        ("WAITING FOR INPUT", "waiting", 3),
        ("WAITING FOR APPROVAL", "approval", 3),
    ]
    for title, bucket, limit in groups:
        subset = [j for j in jobs if _status_bucket(str(j.get("status") or "")) == bucket][:limit]
        if subset:
            lines += ["", f"**{title}**"] + [_job_line(job) for job in subset]
    recent = [j for j in jobs if _status_bucket(str(j.get("status") or "")) in {"completed", "failed"}][:4]
    if recent:
        lines += ["", "**RECENT**"] + [_job_line(job) for job in recent]
    root = _latest_completed_profile_job(chat_id)
    if root:
        lines += ["", f"Latest completed profile batch: **`{root.get('job_code')}`** · `{_job_label(root)}`", "Use `!aycd workflow latest` to see Profile Builder actions available for it."]
    lines += [
        "",
        "**QUICK COMMANDS**",
        "`!aycd jobs active` · `waiting` · `approval` · `completed` · `failed`",
        "`!aycd job latest` · `!aycd workflow latest` · `!aycd stop latest`",
        "`!aycd generate 10` · `!aycd recipes` · `!aycd tools` · `!aycd help`",
        "",
        "React: 🔄 refresh dashboard · 📋 recent jobs · 👀 latest workflow",
    ]
    return "\n".join(lines)



def aycd_dashboard_data(chat_id: int) -> dict[str, Any]:
    jobs = aycd_jobs(chat_id, 80)
    counts = {key: 0 for key in ("active", "waiting", "approval", "completed", "failed")}
    for job in jobs:
        bucket = _status_bucket(str(job.get("status") or ""))
        if bucket in counts:
            counts[bucket] += 1
    def compact(job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(job.get("payload") or {})
        return {
            "job_code": str(job.get("job_code") or ""),
            "status": str(job.get("status") or ""),
            "bucket": _status_bucket(str(job.get("status") or "")),
            "label": _job_label(job),
            "kind": str(job.get("kind") or ""),
            "missing": [str(x) for x in list(payload.get("missing") or [])[:8]],
            "updated_at": int(job.get("updated_at") or 0),
        }
    try:
        server = mcp_public_server(MCP_DEFAULT_SERVER_ID)
        st = dict(server.get("status") or {})
        mcp = {
            "connected": bool(st.get("connected")),
            "enabled": bool(server.get("enabled")),
            "tools_count": int(server.get("tools_count") or 0),
            "latency_ms": int(st.get("latency_ms") or 0),
            "error": str(st.get("error") or "")[:500],
        }
    except Exception as exc:
        mcp = {"connected": False, "enabled": False, "tools_count": 0, "latency_ms": 0, "error": str(exc)[:500]}
    root = _latest_completed_profile_job(chat_id)
    return {
        "ok": True,
        "chat_id": int(chat_id),
        "mcp": mcp,
        "counts": counts,
        "active": [compact(j) for j in jobs if _status_bucket(str(j.get("status") or "")) == "active"][:8],
        "waiting": [compact(j) for j in jobs if _status_bucket(str(j.get("status") or "")) == "waiting"][:8],
        "approval": [compact(j) for j in jobs if _status_bucket(str(j.get("status") or "")) == "approval"][:8],
        "recent": [compact(j) for j in jobs if _status_bucket(str(j.get("status") or "")) in {"completed", "failed"}][:10],
        "recipes_count": len(list_aycd_recipes(chat_id)),
        "latest_profile_job": compact(root) if root else None,
    }

def aycd_dashboard_reaction(chat_id: int, emoji: str) -> dict[str, Any]:
    mark = str(emoji or "")
    if mark == "🔄":
        return {"ok": True, "text": _dashboard_text(chat_id), "reactions": ["🔄", "📋", "👀"]}
    if mark == "📋":
        return {"ok": True, "text": _jobs_text(chat_id, "recent"), "reactions": ["🔄", "📋", "👀"]}
    if mark == "👀":
        root = _latest_completed_profile_job(chat_id)
        text = _action_plan_text(chat_id, root) if root else "No completed AYCD profile batch yet. Create one with `!aycd generate 10`."
        return {"ok": True, "text": text, "reactions": ["🔄", "📋", "👀"]}
    return {"ok": False, "text": "That dashboard reaction is not available.", "reactions": ["🔄", "📋", "👀"]}


def _profiles_help() -> str:
    return aycd_help_text()


def handle_aycd_command(chat_id: int, text: str, *, source: str = "chat", user_id: str = "") -> dict[str, Any] | None:
    raw=str(text or "").strip()
    if not raw.casefold().startswith("!aycd"): return None
    _ensure_tables(); rest=raw[5:].strip(); lower=rest.casefold()
    if not rest or lower in {"dashboard","home","control","control center"}:
        return {"handled":True,"text":_dashboard_text(chat_id),"job_code":"","reactions":["🔄","📋","👀"],"control_kind":"aycd_dashboard"}
    if lower in {"help","commands","?"}: return {"handled":True,"text":aycd_help_text(),"job_code":"","reactions":[]}
    if lower=="status": return {"handled":True,"text":aycd_status_text(),"job_code":"","reactions":[]}
    if lower in {"tools","tools refresh","refresh tools"}: return {"handled":True,"text":aycd_tools_text(refresh="refresh" in lower),"job_code":"","reactions":[]}
    if lower in {"profiles","profile","profile builder","profiles help"}: return {"handled":True,"text":_profiles_help(),"job_code":"","reactions":[]}
    if lower in {"ongoing","active"}: return {"handled":True,"text":_jobs_text(chat_id,"active"),"job_code":"","reactions":[]}
    if lower in {"available","actions","available actions"}:
        root=_latest_completed_profile_job(chat_id)
        return {"handled":True,"text":(_action_plan_text(chat_id,root) if root else "No completed AYCD profile batch yet. Create one with `!aycd generate 10`."),"job_code":str(root.get("job_code") or "") if root else "","reactions":aycd_job_reactions(root)}
    if lower in {"jobs","job"}: return {"handled":True,"text":_jobs_text(chat_id,"recent"),"job_code":"","reactions":[]}
    if lower.startswith("jobs "):
        filt=rest.split(None,1)[1].strip(); return {"handled":True,"text":_jobs_text(chat_id,filt),"job_code":"","reactions":[]}
    if lower.startswith("job "):
        job=_resolve_job_reference(chat_id,rest); code=str(job.get("job_code") or "") if job else ""; return {"handled":True,"text":aycd_job_text(job or {}),"job_code":code,"reactions":aycd_job_reactions(job)}
    if lower.startswith(("workflow","plan")):
        code=_job_code_from_text(rest); root=aycd_job(code,chat_id) if code else _latest_completed_profile_job(chat_id)
        if root and str(root.get("kind") or "").startswith("profile_action:"): root=_root_profile_job(chat_id,root)
        if not root: return {"handled":True,"text":"Create and approve a profile batch first, then use `!aycd workflow A001`.","job_code":"","reactions":[]}
        return {"handled":True,"text":_action_plan_text(chat_id,root),"job_code":str(root.get("job_code") or ""),"reactions":aycd_job_reactions(root)}
    if lower.startswith("set "):
        code=_job_code_from_text(rest); params=_parse_kv_params(rest)
        if not code or not params: return {"handled":True,"text":"Use `!aycd set A002 field=value`.","job_code":"","reactions":[]}
        job=set_aycd_job_parameters(chat_id,code,params,user_id); return {"handled":True,"text":aycd_job_text(job),"job_code":code,"reactions":aycd_job_reactions(job)}
    action,tail=_action_from_command(rest)
    if action:
        code=_job_code_from_text(tail); root=aycd_job(code,chat_id) if code else _latest_completed_profile_job(chat_id)
        if root and str(root.get("kind") or "").startswith("profile_action:"): root=_root_profile_job(chat_id,root)
        if not root: return {"handled":True,"text":"Create and approve a profile batch first. Example: `!aycd generate 10` → ✅ → `!aycd jig A001`.","job_code":"","reactions":[]}
        if root.get("status") != "completed": return {"handled":True,"text":f"Profile batch `{root.get('job_code')}` is not completed yet.","job_code":str(root.get("job_code") or ""),"reactions":aycd_job_reactions(root)}
        params=_parse_kv_params(tail); defaults=dict((root.get("payload") or {}).get("action_defaults") or {}).get(action,{})
        merged=dict(defaults) if isinstance(defaults,dict) else {}; merged.update(params)
        child=_create_action_job(chat_id,root,action,raw,source,user_id,merged); return {"handled":True,"text":aycd_job_text(child),"job_code":str(child.get("job_code") or ""),"reactions":aycd_job_reactions(child)}
    if lower.startswith(("approve ","confirm ","run ")):
        target=_resolve_job_reference(chat_id,rest,buckets={"approval"})
        if not target: return {"handled":True,"text":"No AYCD job is waiting for approval. Use `!aycd jobs approval`.","job_code":"","reactions":[]}
        code=str(target.get("job_code") or ""); job=execute_aycd_job(chat_id,code,source=source,actor_id=user_id); return {"handled":True,"text":aycd_job_text(job),"job_code":code,"reactions":aycd_job_reactions(job)}
    if lower.startswith(("cancel ","stop ")):
        target=_resolve_job_reference(chat_id,rest,buckets={"active","approval","waiting"})
        if not target: return {"handled":True,"text":"No active/waiting AYCD job was found. Use `!aycd jobs active` or `!aycd jobs waiting`.","job_code":"","reactions":[]}
        code=str(target.get("job_code") or ""); job=cancel_aycd_job(chat_id,code,user_id); return {"handled":True,"text":aycd_job_text(job),"job_code":code,"reactions":aycd_job_reactions(job)}
    if lower.startswith(("reroll ","regenerate ")):
        target=_resolve_job_reference(chat_id,rest,kind="profiles_generate"); code=str(target.get("job_code") or "") if target else ""; job=reroll_aycd_job(chat_id,code,user_id) if code else None; return {"handled":True,"text":aycd_job_text(job or {}),"job_code":code if job else "","reactions":aycd_job_reactions(job)}
    if lower.startswith("recipe list") or lower=="recipes":
        recipes=list_aycd_recipes(chat_id); text_out="**AYCD profile recipes**\n"+("\n".join(f"`{x['name']}` · default {int(x['config'].get('count') or 10)}" for x in recipes) if recipes else "No saved recipes yet."); return {"handled":True,"text":text_out,"job_code":"","reactions":[]}
    if lower.startswith("recipe show "):
        name=rest.split(None,2)[2].strip(); recipe=_recipe(chat_id,name)
        if not recipe: return {"handled":True,"text":f"AYCD recipe `{name}` was not found.","job_code":"","reactions":[]}
        cfg=dict(recipe.get("config") or {}); labels=[_ACTION_SPECS[x]["label"] for x in cfg.get("actions") or [] if x in _ACTION_SPECS]
        return {"handled":True,"text":f"**AYCD recipe `{name}`**\nDefault count: **{int(cfg.get('count') or 10)}**\nWorkflow: **{' → '.join(labels) if labels else 'none saved'}**","job_code":"","reactions":[]}
    if lower.startswith("recipe delete "):
        name=rest.split(None,2)[2].strip(); ok=delete_aycd_recipe(chat_id,name); return {"handled":True,"text":f"{'Deleted' if ok else 'Could not find'} AYCD recipe `{name}`.","job_code":"","reactions":[]}
    if lower.startswith("recipe save "):
        parts=rest.split(); name=parts[2] if len(parts)>2 else ""; code=_job_code_from_text(rest); job=aycd_job(code,chat_id) if code else _latest_completed_profile_job(chat_id)
        if not job: return {"handled":True,"text":"No AYCD profile job is available to save as a recipe.","job_code":"","reactions":[]}
        root=_root_profile_job(chat_id,job) or job; recipe=save_aycd_recipe(chat_id,name,dict(root.get("payload") or {})); return {"handled":True,"text":f"💾 Saved AYCD recipe `{recipe.get('name')}`.","job_code":str(root.get("job_code") or ""),"reactions":aycd_job_reactions(root)}
    if lower in {"duplicates","profiles duplicates","profile duplicates"} or lower.startswith("duplicates ") or "check duplicates" in lower:
        code=_job_code_from_text(rest); job=aycd_job(code,chat_id) if code else _latest_completed_profile_job(chat_id); root=_root_profile_job(chat_id,job) if job and str(job.get("kind") or "").startswith("profile_action:") else job
        return {"handled":True,"text":_run_duplicate_check(chat_id,source,root),"job_code":str(root.get("job_code") or "") if root else "","reactions":aycd_job_reactions(root)}
    if lower.startswith(("generate","preview","profiles generate","profiles preview","profile generate")):
        count=_count_from_text(rest,10); rm=_RE_RECIPE_USING.search(rest); recipe_name=rm.group(1).casefold() if rm else ""
        if recipe_name and not _recipe(chat_id,recipe_name): return {"handled":True,"text":f"AYCD recipe `{recipe_name}` was not found.","job_code":"","reactions":[]}
        job=_create_profile_job(chat_id,count,raw,source,user_id,recipe_name); return {"handled":True,"text":aycd_job_text(job),"job_code":str(job.get("job_code") or ""),"reactions":aycd_job_reactions(job)}
    return {"handled":True,"text":"I recognize `!aycd`, but that subcommand is not defined. Use `!aycd help`, or ask the AYCD request as a normal sentence for the MCP natural-language router.","job_code":"","reactions":[]}


__all__ = [
    "init_aycd_commands", "handle_aycd_command", "aycd_help_text", "aycd_status_text", "aycd_tools_text",
    "aycd_job", "aycd_jobs", "aycd_job_text", "aycd_job_reactions", "aycd_reaction_action",
    "execute_aycd_job", "stop_aycd_job", "reroll_aycd_job", "set_aycd_job_parameters",
    "list_aycd_recipes", "save_aycd_recipe", "stop_aycd_jobs_for_chat", "aycd_dashboard_reaction", "aycd_dashboard_data",
]
