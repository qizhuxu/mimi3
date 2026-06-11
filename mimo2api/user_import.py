from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_USERS_DIR = ROOT_DIR / "users"
UID_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
REQUIRED_FIELDS = ("userId", "serviceToken", "xiaomichatbot_ph")


def safe_uid(value: Any) -> str:
    uid = str(value or "").strip()
    if not uid or not UID_RE.fullmatch(uid) or ".." in uid:
        raise ValueError("invalid userId")
    return uid


def user_file_path(users_dir: Path | str, uid: str) -> Path:
    safe = safe_uid(uid)
    root = Path(users_dir).resolve()
    path = (root / f"user_{safe}.json").resolve()
    if not path.is_relative_to(root):
        raise ValueError("invalid userId")
    return path


def parse_raw_credentials(raw_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for match in re.finditer(r'([a-zA-Z0-9_]+)="?([^;"]+)"?', raw_text or ""):
        parsed[match.group(1)] = match.group(2)
    return parsed


def normalize_user_entry(entry: Any, *, uid_hint: str | None = None) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object")
    data = dict(entry)
    if uid_hint and not data.get("userId"):
        data["userId"] = uid_hint

    missing = [field for field in REQUIRED_FIELDS if not str(data.get(field) or "").strip()]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    uid = safe_uid(data["userId"])
    return {
        "userId": uid,
        "serviceToken": str(data["serviceToken"]),
        "xiaomichatbot_ph": str(data["xiaomichatbot_ph"]),
        "name": str(data.get("name") or f"Imported_{uid}"),
    }


def _entries_from_payload(payload: Any) -> tuple[list[tuple[str | None, Any]], bool]:
    overwrite = False
    source = payload

    if isinstance(payload, dict):
        overwrite = bool(payload.get("overwrite") is True)
        if "users" in payload:
            source = payload.get("users")

    if isinstance(source, list):
        return [(None, item) for item in source], overwrite

    if isinstance(source, dict):
        entries: list[tuple[str | None, Any]] = []
        for key, value in source.items():
            if key == "overwrite":
                continue
            entries.append((str(key), value))
        return entries, overwrite

    raise ValueError("batch payload must be an array or uid mapping object")


def import_user(
    entry: Any,
    *,
    users_dir: Path | str = DEFAULT_USERS_DIR,
    uid_hint: str | None = None,
    overwrite: bool = False,
) -> dict[str, str]:
    user_data = normalize_user_entry(entry, uid_hint=uid_hint)
    path = user_file_path(users_dir, user_data["userId"])
    if path.exists() and not overwrite:
        raise FileExistsError("user already exists")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(user_data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(path)
    return {"userId": user_data["userId"], "name": user_data["name"]}


def import_users_batch(
    payload: Any,
    *,
    users_dir: Path | str = DEFAULT_USERS_DIR,
) -> dict[str, Any]:
    entries, overwrite = _entries_from_payload(payload)
    imported: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []

    for index, (uid_hint, entry) in enumerate(entries):
        user_id = str(uid_hint or (entry.get("userId") if isinstance(entry, dict) else "") or f"#{index}")
        try:
            imported.append(import_user(entry, users_dir=users_dir, uid_hint=uid_hint, overwrite=overwrite))
        except Exception as exc:
            failures.append({"index": index, "userId": user_id, "reason": str(exc)})

    return {
        "ok": not failures,
        "success_count": len(imported),
        "failure_count": len(failures),
        "overwrite": overwrite,
        "imported": imported,
        "failures": failures,
    }
