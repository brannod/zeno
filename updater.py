#!/usr/bin/env python3
"""Safe GitHub Releases updater for Zeno.

The updater only replaces trusted application files at Zeno's root. Runtime data
(memory, uploads, outputs, browser profile, private Discord config) is never part
of the install allowlist and remains untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from config import APP_VERSION, BASE_DIR, DATA_DIR, DEFAULT_UPDATE_REPO

UPDATE_DIR = DATA_DIR / "updates"
MAX_UPDATE_ZIP_BYTES = 80 * 1024 * 1024
_REQUIRED_CORE = {"zeno.py", "app.html"}
_EXPLICIT_ROOT_FILES = {
    "app.html",
    "requirements.txt",
    "START_ZENO.bat",
    "INSTALL_ZENO.bat",
    "start_macos.sh",
    "install_macos.sh",
    "START_ZENO_MAC.command",
    "INSTALL_ZENO_MAC.command",
    "README.txt",
    "FIRST_TIME_USER_GUIDE.txt",
    "DISCORD_GUIDE.txt",
    "ZENO_3.0_INTEGRATION_AUDIT.txt",
    "zeno-icon.png",
    "zeno-wallpaper.svg",
    "zeno.ico",
    "VERSION",
}
_PROTECTED_NAMES = {
    "DISCORD_TOKEN.txt",
    "DISCORD_BOT_INFO_HERE.txt",
    "ZENO_LEGACY_MEMORY_IMPORT.md",
}
_LOCK = threading.RLock()
_STATE: dict[str, Any] = {
    "status": "idle",
    "detail": "Updater ready",
    "repo": DEFAULT_UPDATE_REPO,
    "current_version": APP_VERSION,
    "latest_version": "",
    "available": False,
    "downloaded_path": "",
    "backup_path": "",
    "restart_required": False,
    "checked_at": 0,
    "updated_at": int(time.time()),
}


def _set_state(**values: Any) -> dict[str, Any]:
    with _LOCK:
        _STATE.update(values)
        _STATE["updated_at"] = int(time.time())
        return dict(_STATE)


def updater_status() -> dict[str, Any]:
    with _LOCK:
        state = dict(_STATE)
    path = str(state.get("downloaded_path") or "")
    if path and not Path(path).exists():
        state["downloaded_path"] = ""
        path = ""
    # Keep a validated downloaded release discoverable after a Zeno restart.
    if not path and UPDATE_DIR.exists():
        candidates = sorted(
            (item for item in UPDATE_DIR.glob("*.zip") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                validate_release_zip(candidate)
            except Exception:
                continue
            values: dict[str, Any] = {"downloaded_path": str(candidate)}
            if state.get("status") == "idle":
                values.update(status="downloaded", detail=f"Validated downloaded update: {candidate.name}")
            state = _set_state(**values)
            break
    return state


def normalize_repo(value: str | None) -> str:
    text = str(value or DEFAULT_UPDATE_REPO).strip()
    if not text:
        text = DEFAULT_UPDATE_REPO
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urllib.parse.urlsplit(text)
        if parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
            raise ValueError("Update repository URL must point to github.com.")
        text = parsed.path.strip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError("GitHub repository must use owner/repository format.")
    return "/".join(parts)


def _version_tuple(value: str) -> tuple[int, ...]:
    text = str(value or "").strip().lstrip("vV")
    numbers = re.findall(r"\d+", text)
    if not numbers:
        return (0,)
    result = tuple(int(item) for item in numbers[:4])
    return result + (0,) * (4 - len(result))


def _request_json(url: str, timeout: float = 12.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Zeno/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("No public GitHub release was found for that repository.") from exc
        raise RuntimeError(f"GitHub update check failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub update check failed: {exc.reason}") from exc
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned an unexpected release response.")
    return value


def _release_zip_asset(release: dict[str, Any]) -> dict[str, Any] | None:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    candidates = [
        item for item in assets
        if isinstance(item, dict)
        and str(item.get("name") or "").casefold().endswith(".zip")
        and str(item.get("browser_download_url") or "").startswith("https://")
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            "zeno" not in str(item.get("name") or "").casefold(),
            "complete" not in str(item.get("name") or "").casefold(),
            str(item.get("name") or ""),
        )
    )
    return candidates[0]


def check_for_update(repo: str | None = None) -> dict[str, Any]:
    repo_name = normalize_repo(repo)
    _set_state(status="checking", detail=f"Checking {repo_name}…", repo=repo_name)
    try:
        release = _request_json(f"https://api.github.com/repos/{repo_name}/releases/latest")
        tag = str(release.get("tag_name") or release.get("name") or "").strip()
        if not tag:
            raise RuntimeError("The latest GitHub release has no version tag.")
        asset = _release_zip_asset(release)
        available = _version_tuple(tag) > _version_tuple(APP_VERSION)
        result = {
            "status": "update_available" if available else "current",
            "detail": (
                f"Zeno {tag.lstrip('vV')} is available."
                if available
                else f"Zeno {APP_VERSION} is current. Latest release: {tag}."
            ),
            "repo": repo_name,
            "current_version": APP_VERSION,
            "latest_version": tag.lstrip("vV"),
            "tag_name": tag,
            "available": available,
            "release_name": str(release.get("name") or tag),
            "release_url": str(release.get("html_url") or ""),
            "published_at": str(release.get("published_at") or ""),
            "asset": {
                "name": str(asset.get("name") or ""),
                "url": str(asset.get("browser_download_url") or ""),
                "size": int(asset.get("size") or 0),
            } if asset else None,
            "checked_at": int(time.time()),
        }
        _set_state(**result)
        return dict(result)
    except Exception as exc:
        _set_state(status="error", detail=str(exc), repo=repo_name, available=False)
        raise


def _safe_zip_members(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > 4000:
            raise ValueError("Update archive contains too many files.")
        total = 0
        for info in infos:
            member = PurePosixPath(info.filename.replace("\\", "/"))
            if member.is_absolute() or ".." in member.parts:
                raise ValueError("Update archive contains an unsafe path.")
            unix_mode = (int(info.external_attr) >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ValueError("Update archive contains a symbolic link, which Zeno does not install.")
            total += max(0, int(info.file_size))
            if total > 500 * 1024 * 1024:
                raise ValueError("Update archive expands beyond the allowed size.")
        return infos


def _find_release_root(path: Path) -> tuple[str, set[str]]:
    infos = _safe_zip_members(path)
    files = [PurePosixPath(info.filename.replace("\\", "/")) for info in infos if not info.is_dir()]
    roots: dict[str, set[str]] = {}
    for member in files:
        parent = str(member.parent)
        roots.setdefault(parent, set()).add(member.name)
    matches = [(parent, names) for parent, names in roots.items() if _REQUIRED_CORE.issubset(names)]
    if not matches:
        raise ValueError("Update ZIP is invalid: zeno.py and app.html must be together in the release root.")
    matches.sort(key=lambda item: (len(PurePosixPath(item[0]).parts), item[0]))
    return matches[0]


def validate_release_zip(path: str | Path) -> dict[str, Any]:
    candidate = Path(path).resolve()
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError("Downloaded update ZIP no longer exists.")
    if candidate.stat().st_size > MAX_UPDATE_ZIP_BYTES:
        raise ValueError("Update ZIP is larger than the 80 MB safety limit.")
    if not zipfile.is_zipfile(candidate):
        raise ValueError("Downloaded update is not a valid ZIP archive.")
    root, names = _find_release_root(candidate)
    return {
        "path": str(candidate),
        "root": root,
        "files": sorted(names),
        "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "size": candidate.stat().st_size,
    }


def download_update(repo: str | None = None) -> dict[str, Any]:
    info = check_for_update(repo)
    asset = info.get("asset")
    if not isinstance(asset, dict) or not asset.get("url"):
        raise RuntimeError("The latest release does not contain a downloadable ZIP asset.")
    size = int(asset.get("size") or 0)
    if size and size > MAX_UPDATE_ZIP_BYTES:
        raise ValueError("Release ZIP is larger than the 80 MB updater limit.")
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", str(asset.get("name") or "Zeno-update.zip"))
    destination = UPDATE_DIR / safe_name
    temp_path = destination.with_suffix(destination.suffix + ".part")
    _set_state(status="downloading", detail=f"Downloading {safe_name}…")
    request = urllib.request.Request(
        str(asset["url"]),
        headers={"Accept": "application/octet-stream", "User-Agent": f"Zeno/{APP_VERSION}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temp_path.open("wb") as handle:
            total = 0
            while True:
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPDATE_ZIP_BYTES:
                    raise ValueError("Downloaded update exceeded the 80 MB safety limit.")
                handle.write(chunk)
        os.replace(temp_path, destination)
        validation = validate_release_zip(destination)
        result = {
            "status": "downloaded",
            "detail": f"Downloaded and validated {destination.name}.",
            "downloaded_path": str(destination),
            "validation": validation,
            "restart_required": False,
        }
        _set_state(**result)
        return {**info, **result}
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        _set_state(status="error", detail=str(exc))
        raise


def _allowed_app_file(name: str) -> bool:
    clean = Path(name).name
    if clean in _PROTECTED_NAMES:
        return False
    if clean.casefold().endswith(".py"):
        return True
    return clean in _EXPLICIT_ROOT_FILES


def _extract_release(path: Path, destination: Path) -> Path:
    root, _ = _find_release_root(path)
    root_parts = tuple(PurePosixPath(root).parts) if root not in {"", "."} else ()
    with zipfile.ZipFile(path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member = PurePosixPath(info.filename.replace("\\", "/"))
            parts = member.parts
            if root_parts and parts[:len(root_parts)] != root_parts:
                continue
            relative_parts = parts[len(root_parts):]
            if len(relative_parts) != 1:
                continue
            name = relative_parts[0]
            if not _allowed_app_file(name):
                continue
            target = destination / name
            target.write_bytes(archive.read(info))
    if not all((destination / name).exists() for name in _REQUIRED_CORE):
        raise ValueError("Staged update is missing required core files.")
    return destination


def _current_app_files(base_dir: Path) -> list[Path]:
    result: list[Path] = []
    for path in base_dir.iterdir():
        if path.is_file() and _allowed_app_file(path.name):
            result.append(path)
    return sorted(result, key=lambda item: item.name.casefold())


def install_update(path: str | Path | None = None) -> dict[str, Any]:
    state = updater_status()
    source = Path(path or str(state.get("downloaded_path") or "")).resolve()
    validation = validate_release_zip(source)
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = UPDATE_DIR / f"backup-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    stage_dir = Path(tempfile.mkdtemp(prefix="zeno-update-stage-", dir=str(UPDATE_DIR)))
    _set_state(status="installing", detail="Creating backup and installing approved application files…")
    try:
        current_files = _current_app_files(BASE_DIR)
        for current in current_files:
            shutil.copy2(current, backup_dir / current.name)
        manifest = {
            "created_at": int(time.time()),
            "from_version": APP_VERSION,
            "source_zip": str(source),
            "source_sha256": validation["sha256"],
            "files": [item.name for item in current_files],
        }
        (backup_dir / "BACKUP_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        _extract_release(source, stage_dir)
        installed: list[str] = []
        for staged in sorted(stage_dir.iterdir(), key=lambda item: item.name.casefold()):
            if not staged.is_file() or not _allowed_app_file(staged.name):
                continue
            destination = BASE_DIR / staged.name
            temporary = BASE_DIR / (staged.name + ".update_tmp")
            shutil.copy2(staged, temporary)
            os.replace(temporary, destination)
            installed.append(staged.name)

        if not _REQUIRED_CORE.issubset(installed):
            raise RuntimeError("Update install did not replace both required core files.")
        result = {
            "status": "installed",
            "detail": "Update installed. Restart Zeno to load the new Python runtime.",
            "installed_files": installed,
            "backup_path": str(backup_dir),
            "downloaded_path": str(source),
            "restart_required": True,
        }
        _set_state(**result)
        return result
    except Exception as exc:
        # Best-effort rollback from the backup if installation fails midway.
        rollback_errors: list[str] = []
        try:
            for backup_file in backup_dir.iterdir():
                if not backup_file.is_file() or backup_file.name == "BACKUP_MANIFEST.json":
                    continue
                if not _allowed_app_file(backup_file.name):
                    continue
                try:
                    temporary = BASE_DIR / (backup_file.name + ".rollback_tmp")
                    shutil.copy2(backup_file, temporary)
                    os.replace(temporary, BASE_DIR / backup_file.name)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{backup_file.name}: {rollback_exc}")
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        suffix = "" if not rollback_errors else " Rollback warnings: " + "; ".join(rollback_errors[:4])
        _set_state(status="error", detail=f"Install failed: {exc}.{suffix}".strip(), backup_path=str(backup_dir))
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


__all__ = [
    "normalize_repo",
    "updater_status",
    "check_for_update",
    "download_update",
    "validate_release_zip",
    "install_update",
]
