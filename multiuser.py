"""Supabase Auth, row-level tenant storage, and encrypted provider settings."""

from __future__ import annotations

import json
import base64
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
HOSTED_MODE = (os.getenv("MR_TRENCH_MODE", "").lower() == "hosted"
               or bool(SUPABASE_URL or SUPABASE_ANON_KEY))
_USER_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
_verified_sessions: dict[str, tuple[float, dict[str, Any]]] = {}


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
    cached = _verified_sessions.get(token)
    now = time.monotonic()
    if cached and now - cached[0] < 30:
        return cached[1]
    try:
        user = _auth_request("user", token=token)
    except ValueError:
        return None
    user_id = str(user.get("id", ""))
    if not _USER_ID.fullmatch(user_id):
        return None
    if len(_verified_sessions) > 1024:
        _verified_sessions.clear()
    _verified_sessions[token] = (now, user)
    return user


def _tenant_url(user_id: str) -> str:
    if not _USER_ID.fullmatch(user_id):
        raise ValueError("Invalid account identifier.")
    return f"{SUPABASE_URL}/rest/v1/mrtrench_user_state?user_id=eq.{urllib.parse.quote(user_id.lower())}&select=user_id,sqlite_state_base64,encrypted_settings"


def _data_request(user_id: str, access_token: str, *, payload: dict[str, Any] | None = None,
                  method: str = "GET") -> list[dict[str, Any]]:
    if not access_token:
        raise ValueError("Your session expired. Please sign in again.")
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    data = None
    url = _tenant_url(user_id)
    if payload is not None:
        if method == "POST":
            url = f"{SUPABASE_URL}/rest/v1/mrtrench_user_state?on_conflict=user_id"
        elif method == "PATCH":
            headers["Prefer"] = "return=minimal"
        else:
            raise ValueError("Unsupported account data operation.")
        headers["Content-Type"] = "application/json"
        if method == "POST":
            headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            if not raw:
                return []
            result = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Could not access your private account data (HTTP {exc.code}). Check the Supabase table setup.") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach Supabase storage: {exc.reason}") from exc
    return result if isinstance(result, list) else []


def load_memory_blob(user_id: str, access_token: str) -> bytes | None:
    rows = _data_request(user_id, access_token)
    if not rows or not rows[0].get("sqlite_state_base64"):
        return None
    try:
        return base64.b64decode(rows[0]["sqlite_state_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Saved account memory data could not be decoded.") from exc


def save_memory_blob(user_id: str, access_token: str, database_bytes: bytes) -> None:
    encoded = base64.b64encode(database_bytes).decode("ascii")
    # Saving a provider key creates the row before chat or background work starts.
    _data_request(user_id, access_token, method="PATCH", payload={
        "sqlite_state_base64": encoded,
    })


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


def load_settings(user_id: str, access_token: str) -> dict[str, Any]:
    from cryptography.fernet import InvalidToken
    defaults: dict[str, Any] = {
        "api_key": "", "model": os.getenv("OPENAI_MODEL", "openai/gpt-oss-20b"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
        "interval_seconds": max(60, int(os.getenv("THOUGHT_INTERVAL_SECONDS", "1800"))),
        "worker_enabled": False, "last_cycle_at": None, "last_worker_error": "",
    }
    rows = _data_request(user_id, access_token)
    if not rows or not rows[0].get("encrypted_settings"):
        return defaults
    try:
        saved = json.loads(_cipher().decrypt(rows[0]["encrypted_settings"].encode("ascii")).decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Could not decrypt this account's settings. Check the server encryption key.") from exc
    defaults.update(saved if isinstance(saved, dict) else {})
    return defaults


def save_settings(user_id: str, access_token: str, settings: dict[str, Any]) -> None:
    safe = {key: settings[key] for key in (
        "api_key", "model", "base_url", "interval_seconds", "worker_enabled",
        "last_cycle_at", "last_worker_error",
    ) if key in settings}
    rows = _data_request(user_id, access_token)
    state_blob = rows[0].get("sqlite_state_base64", "") if rows else ""
    encrypted = _cipher().encrypt(json.dumps(safe).encode("utf-8")).decode("ascii")
    _data_request(user_id, access_token, payload={
        "user_id": user_id.lower(), "sqlite_state_base64": state_blob,
        "encrypted_settings": encrypted,
    })
