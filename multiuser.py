"""Authentication, tenant storage, and encrypted provider settings for hosted mode.

Hosted mode uses Supabase Auth for identity and one SQLite database per account.
The database directory must be on a persistent disk, and this layout is intended
for a single app instance while the project remains an experimental beta.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("MR_TRENCH_DATA_DIR", str(ROOT / "data" / "users"))).resolve()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
HOSTED_MODE = (os.getenv("MR_TRENCH_MODE", "").lower() == "hosted"
               or bool(SUPABASE_URL or SUPABASE_ANON_KEY))
_USER_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def require_config() -> None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError("Hosted multi-user mode needs SUPABASE_URL and SUPABASE_ANON_KEY.")
    if not os.getenv("MR_TRENCH_ENCRYPTION_KEY"):
        raise RuntimeError("Hosted mode needs MR_TRENCH_ENCRYPTION_KEY to protect saved provider keys.")
    _cipher()


def _auth_request(path: str, *, method: str = "GET", token: str = "",
                  payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise ValueError("Account sign-in is not configured on this server yet.")
    headers = {"apikey": SUPABASE_ANON_KEY, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1/{path.lstrip('/')}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            details = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = details.get("msg") or details.get("message") or details.get("error_description") or "Account request failed."
        except (json.JSONDecodeError, AttributeError):
            message = "Account request failed."
        raise ValueError(str(message)[:300]) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach the account service: {exc.reason}") from exc
    if not isinstance(result, dict):
        raise ValueError("Unexpected response from account service.")
    return result


def sign_up(email: str, password: str) -> dict[str, Any]:
    return _auth_request("signup", method="POST", payload={"email": email, "password": password})


def sign_in(email: str, password: str) -> dict[str, Any]:
    return _auth_request("token?grant_type=password", method="POST", payload={"email": email, "password": password})


def refresh_session(refresh_token: str) -> dict[str, Any]:
    return _auth_request("token?grant_type=refresh_token", method="POST",
                         payload={"refresh_token": refresh_token})


def user_for_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        user = _auth_request("user", token=token)
    except ValueError:
        return None
    user_id = str(user.get("id", ""))
    if not _USER_ID.fullmatch(user_id):
        return None
    return user


def tenant_dir(user_id: str) -> Path:
    if not _USER_ID.fullmatch(user_id):
        raise ValueError("Invalid account identifier.")
    path = (DATA_ROOT / user_id.lower()).resolve()
    if DATA_ROOT not in path.parents:
        raise ValueError("Invalid account storage path.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def tenant_database(user_id: str) -> Path:
    return tenant_dir(user_id) / "thought_memory.sqlite3"


def _cipher() -> Any:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("Hosted mode needs the Python dependencies from requirements.txt.") from exc
    key = os.getenv("MR_TRENCH_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("Provider key encryption is not configured on this server.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("MR_TRENCH_ENCRYPTION_KEY is invalid; generate a Fernet key.") from exc


def load_settings(user_id: str) -> dict[str, Any]:
    from cryptography.fernet import InvalidToken
    path = tenant_dir(user_id) / "settings.enc"
    defaults: dict[str, Any] = {
        "api_key": "", "model": os.getenv("OPENAI_MODEL", "openai/gpt-oss-20b"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
        "interval_seconds": max(60, int(os.getenv("THOUGHT_INTERVAL_SECONDS", "1800"))),
        "worker_enabled": False, "last_cycle_at": None, "last_worker_error": "",
    }
    if not path.exists():
        return defaults
    try:
        saved = json.loads(_cipher().decrypt(path.read_bytes()).decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Could not decrypt this account's settings. Check the server encryption key.") from exc
    defaults.update(saved if isinstance(saved, dict) else {})
    return defaults


def save_settings(user_id: str, settings: dict[str, Any]) -> None:
    safe = {key: settings[key] for key in (
        "api_key", "model", "base_url", "interval_seconds", "worker_enabled",
        "last_cycle_at", "last_worker_error",
    ) if key in settings}
    target = tenant_dir(user_id) / "settings.enc"
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(_cipher().encrypt(json.dumps(safe).encode("utf-8")))
    temporary.replace(target)


def registered_tenants() -> list[str]:
    if not DATA_ROOT.exists():
        return []
    return [path.name for path in DATA_ROOT.iterdir()
            if path.is_dir() and _USER_ID.fullmatch(path.name)
            and (path / "settings.enc").is_file()]
