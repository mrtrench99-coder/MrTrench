"""Local browser interface for chatting with MrTrench and running its worker."""

from __future__ import annotations

import json
import os
import http.cookies
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from background_agent import DEFAULT_MODEL, GROQ_BASE_URL, process_due_triggers, run_cycle
from chat_interface import chat_turn
from thought_memory import ThoughtMemory
import multiuser


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = os.getenv("THOUGHT_DB", str(ROOT / "thought_memory.sqlite3"))
memory = ThoughtMemory(DB_PATH)
state_lock = threading.RLock()
settings_lock = threading.RLock()
local_settings: dict[str, object] = {
    "api_key": os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
    "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
    "base_url": os.getenv("OPENAI_BASE_URL", GROQ_BASE_URL),
    "interval_seconds": max(60, int(os.getenv("THOUGHT_INTERVAL_SECONDS", "1800"))),
    "worker_enabled": bool(os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")),
    "last_cycle_at": None,
    "last_worker_error": "",
}
tenant_locks: dict[str, threading.RLock] = {}
tenant_locks_guard = threading.Lock()
hosted_memories: dict[str, ThoughtMemory] = {}
active_tenant_tokens: dict[str, str] = {}
hosted_settings: dict[str, dict[str, object]] = {}


def _lock_for(user_id: str) -> threading.RLock:
    with tenant_locks_guard:
        return tenant_locks.setdefault(user_id, threading.RLock())


def _settings_for(user_id: str | None, access_token: str = "") -> dict[str, object]:
    if user_id is None:
        return local_settings
    with settings_lock:
        if user_id not in hosted_settings:
            hosted_settings[user_id] = multiuser.load_settings(user_id, access_token)
        return hosted_settings[user_id]


def _save_settings(user_id: str, access_token: str, user_settings: dict[str, object]) -> None:
    multiuser.save_settings(user_id, access_token, user_settings)
    with settings_lock:
        hosted_settings[user_id] = user_settings


def _memory_for(user_id: str | None, access_token: str = "") -> ThoughtMemory:
    if user_id is None:
        return memory
    with tenant_locks_guard:
        store = hosted_memories.get(user_id)
        if store is not None:
            return store
        database = Path(tempfile.gettempdir()) / f"mrtrench-{os.getpid()}-{user_id}.sqlite3"
        saved = multiuser.load_memory_blob(user_id, access_token)
        if saved:
            database.write_bytes(saved)
        store = ThoughtMemory(database)
        hosted_memories[user_id] = store
        return store


def _persist_memory(user_id: str | None, access_token: str, store: ThoughtMemory) -> None:
    if user_id is None:
        return
    database = Path(store.database)
    multiuser.save_memory_blob(user_id, access_token, database.read_bytes())


def _clean_base_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Enter a valid http or https API base URL.")
    if multiuser.HOSTED_MODE and url.lower() != GROQ_BASE_URL.lower():
        raise ValueError("Hosted beta currently supports the official Groq API endpoint only.")
    return url


def _probe_provider(api_key: str, base_url: str, model: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"{_clean_base_url(base_url)}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            # Cloudflare in front of Groq may reject urllib's default
            # Python-urllib user agent with Error 1010.
            "User-Agent": "thought-memory-web/0.1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        try:
            error = json.loads(details).get("error", {})
        except (json.JSONDecodeError, AttributeError):
            error = {}
        code = error.get("code", "")
        message = error.get("message", details[:500])
        ray = exc.headers.get("cf-ray") if exc.headers else None
        ray_note = f" Cloudflare Ray ID: {ray}." if ray else ""
        if code == "invalid_api_key" or exc.code == 401:
            raise ValueError("The provider rejected this API key (invalid_api_key). Verify it in the provider console, then create a fresh key if needed." + ray_note) from exc
        if exc.code == 403:
            raise ValueError(f"The provider returned HTTP 403: {message}.{ray_note} A 403 can be an account, region, or provider security restriction; it does not by itself prove the key is malformed.") from exc
        if exc.code == 429:
            raise ValueError(f"The provider returned HTTP 429: {message}. This is a rate or quota limit, not the usual invalid-key response.") from exc
        raise ValueError(f"The provider returned HTTP {exc.code}: {message}.{ray_note}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach the provider: {exc.reason}") from exc
    models = result.get("data", []) if isinstance(result, dict) else []
    ids = {item.get("id") for item in models if isinstance(item, dict)}
    if model and ids and model not in ids:
        return {"ok": True, "message": f"The key was accepted. The selected model '{model}' was not in the provider's model list; check the model ID and access."}
    return {"ok": True, "message": f"Connection successful. The provider accepted the key and returned {len(ids)} available model(s)."}


def _snapshot(user_id: str | None = None, access_token: str = "") -> dict[str, object]:
    store, user_settings = _memory_for(user_id, access_token), _settings_for(user_id, access_token)
    lock = state_lock if user_id is None else _lock_for(user_id)
    with lock, settings_lock:
        return {
            "messages": store.recent_conversation(limit=100),
            "proactive_messages": store.recent_messages(limit=30),
            "threads": store.list_threads(status="active", limit=30)
            + store.list_threads(status="suspended", limit=30),
            "memories": store.recent_memories(limit=50),
            # Emotion-based check-ins are internal attention cues, not user-facing tasks.
            "follow_ups": [item for item in store.pending_triggers(limit=100)
                           if item.get("purpose") != "mood"],
            "proactive_paused": store.proactive_paused(),
            "worker": {
                "enabled": bool(user_settings["worker_enabled"]),
                "configured": bool(user_settings["api_key"]),
                "last_cycle_at": user_settings["last_cycle_at"],
                "last_error": user_settings["last_worker_error"],
                "interval_seconds": user_settings["interval_seconds"],
                "model": user_settings["model"],
                "base_url": user_settings["base_url"],
            },
        }


def _background_loop() -> None:
    while True:
        time.sleep(5)
        if multiuser.HOSTED_MODE:
            with settings_lock:
                tenants = list(active_tenant_tokens.items())
        else:
            tenants = [(None, "")]
        for user_id, access_token in tenants:
            try:
                user_settings = _settings_for(user_id, access_token)
                if not (user_settings["worker_enabled"] and user_settings["api_key"]):
                    continue
                interval = int(user_settings["interval_seconds"])
                now = time.monotonic()
                next_review = getattr(_background_loop, "next_review", {})
                retry_map = getattr(_background_loop, "retry_after", {})
                next_review.setdefault(str(user_id), now + interval)
                retry_map.setdefault(str(user_id), 0.0)
                if now < retry_map[str(user_id)]:
                    continue
                store = _memory_for(user_id, access_token)
                lock = state_lock if user_id is None else _lock_for(user_id)
                with lock:
                    processed = process_due_triggers(
                        store, model=str(user_settings["model"]),
                        api_key=str(user_settings["api_key"]), base_url=str(user_settings["base_url"]))
                    if processed:
                        _persist_memory(user_id, access_token, store)
                    if now >= next_review[str(user_id)]:
                        run_cycle(store, model=str(user_settings["model"]),
                                  api_key=str(user_settings["api_key"]), base_url=str(user_settings["base_url"]))
                        next_review[str(user_id)] = now + interval
                        user_settings["last_cycle_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        user_settings["last_worker_error"] = ""
                        if user_id is not None:
                            _save_settings(user_id, access_token, user_settings)
                            _persist_memory(user_id, access_token, store)
                        with settings_lock:
                            local_settings["last_cycle_at"] = user_settings["last_cycle_at"]
                setattr(_background_loop, "next_review", next_review)
                setattr(_background_loop, "retry_after", retry_map)
            except Exception as exc:  # keep the UI alive when a provider is unavailable
                if user_id is not None:
                    try:
                        user_settings = _settings_for(user_id, access_token)
                        user_settings["last_worker_error"] = str(exc)[:1000]
                        _save_settings(user_id, access_token, user_settings)
                    except Exception:
                        pass
                with settings_lock:
                    local_settings["last_worker_error"] = str(exc)[:1000]
                retry_map = getattr(_background_loop, "retry_after", {})
                retry_map[str(user_id)] = time.monotonic() + 30
                setattr(_background_loop, "retry_after", retry_map)


class MrTrenchHandler(BaseHTTPRequestHandler):
    server_version = "MrTrench/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        # Avoid writing chat contents or local API activity to terminal logs.
        return

    def _json(self, value: object, status: int = 200, headers: dict[str, object] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, header_value in (headers or {}).items():
            for item in (header_value if isinstance(header_value, list) else [header_value]):
                self.send_header(name, str(item))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request size.") from exc
        if length < 0 or length > 1_000_000:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "").lower()
        return bool(host) and (not origin or urlparse(origin).netloc.lower() == host)

    def _trusted_host(self) -> bool:
        host = self.headers.get("Host", "").lower()
        allowed = {item.strip().lower() for item in os.getenv("MR_TRENCH_ALLOWED_HOSTS", "").split(",") if item.strip()}
        if not multiuser.HOSTED_MODE:
            allowed.update({f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"})
        return host in allowed if allowed else (not multiuser.HOSTED_MODE and bool(host))

    def _cookie(self, name: str) -> str:
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            return cookies[name].value if name in cookies else ""
        except http.cookies.CookieError:
            return ""

    def _session_headers(self, access: str = "", refresh: str = "", *, clear: bool = False) -> dict[str, object]:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        if clear:
            return {"Set-Cookie": [f"mrtrench_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}",
                                   f"mrtrench_refresh=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"]}
        if not access or not refresh:
            return {}
        return {"Set-Cookie": [f"mrtrench_session={access}; Path=/; HttpOnly; SameSite=Lax; Max-Age=3600{secure}",
                               f"mrtrench_refresh={refresh}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{secure}"]}

    def _current_user(self) -> tuple[str | None, dict[str, Any] | None]:
        if not multiuser.HOSTED_MODE:
            return None, None
        token = self._cookie("mrtrench_session")
        user = multiuser.user_for_token(token)
        if user:
            user_id = str(user["id"]).lower()
            self._access_token = token
            with settings_lock:
                active_tenant_tokens[user_id] = token
            return user_id, user
        refresh_token = self._cookie("mrtrench_refresh")
        if refresh_token:
            try:
                session = multiuser.refresh_session(refresh_token)
                token = str(session.get("access_token", ""))
                user = multiuser.user_for_token(token)
                if user:
                    self._auth_cookie_headers = self._session_headers(
                        token, str(session.get("refresh_token", refresh_token)))
                    user_id = str(user["id"]).lower()
                    self._access_token = token
                    with settings_lock:
                        active_tenant_tokens[user_id] = token
                    return user_id, user
            except ValueError:
                pass
        return None, None

    def _require_user(self) -> tuple[str | None, dict[str, Any] | None]:
        user_id, user = self._current_user()
        if multiuser.HOSTED_MODE and not user_id:
            raise PermissionError("Please sign in to continue.")
        return user_id, user

    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted_host():
            self.send_error(403)
            return
        if self.path == "/api/auth/session":
            user_id, user = self._current_user()
            headers = getattr(self, "_auth_cookie_headers", {})
            self._json({"hosted": multiuser.HOSTED_MODE, "authenticated": bool(user_id),
                        "user": {"email": user.get("email", "")} if user else None}, headers=headers)
            return
        if self.path == "/api/state":
            try:
                user_id, _ = self._require_user()
                self._json(_snapshot(user_id, getattr(self, "_access_token", "")),
                           headers=getattr(self, "_auth_cookie_headers", {}))
            except PermissionError as exc:
                self._json({"error": str(exc)}, 401)
            return
        relative = "index.html" if self.path in {"/", "/index.html"} else self.path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in target.parents or not target.is_file():
            self.send_error(404)
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".png": "image/png",
        }.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if not self._trusted_host() or not self._same_origin():
            self._json({"error": "Cross-origin requests are not allowed."}, 403)
            return
        try:
            body = self._body()
            if self.path == "/api/auth/signup":
                if not multiuser.HOSTED_MODE:
                    raise ValueError("Account sign-in is not enabled in local mode.")
                result = multiuser.sign_up(str(body.get("email", "")).strip(), str(body.get("password", "")))
                if result.get("access_token") and result.get("refresh_token"):
                    self._json({"ok": True, "authenticated": True, "message": "Account created."}, headers=self._session_headers(
                        str(result["access_token"]), str(result.get("refresh_token", ""))))
                else:
                    self._json({"ok": True, "message": "Check your email to confirm the account, then sign in."})
                return
            if self.path == "/api/auth/login":
                if not multiuser.HOSTED_MODE:
                    raise ValueError("Account sign-in is not enabled in local mode.")
                result = multiuser.sign_in(str(body.get("email", "")).strip(), str(body.get("password", "")))
                if not result.get("access_token") or not result.get("refresh_token"):
                    raise ValueError("The account service did not return a sign-in session. Confirm your email and try again.")
                self._json({"ok": True, "email": str(result.get("user", {}).get("email", ""))},
                           headers=self._session_headers(str(result["access_token"]), str(result["refresh_token"])))
                return
            if self.path == "/api/auth/logout":
                token = self._cookie("mrtrench_session")
                user = multiuser.user_for_token(token) if multiuser.HOSTED_MODE else None
                if user:
                    with settings_lock:
                        active_tenant_tokens.pop(str(user.get("id", "")).lower(), None)
                self._json({"ok": True}, headers=self._session_headers(clear=True))
                return
            user_id, _ = self._require_user()
            access_token = getattr(self, "_access_token", "")
            store, user_settings = _memory_for(user_id, access_token), _settings_for(user_id, access_token)
            lock = state_lock if user_id is None else _lock_for(user_id)
            if self.path == "/api/chat":
                text = str(body.get("message", "")).strip()
                if not text:
                    raise ValueError("Write a message first.")
                api_key = str(user_settings["api_key"])
                model = str(user_settings["model"])
                base_url = str(user_settings["base_url"])
                if not api_key:
                    raise ValueError("Add your API key in Settings before chatting.")
                with lock:
                    timezone_name = str(body.get("time_zone", "")).strip() or None
                    reply = chat_turn(store, text, model=model, api_key=api_key,
                                      base_url=base_url, user_timezone=timezone_name)
                    _persist_memory(user_id, access_token, store)
                self._json({"reply": reply}, headers=getattr(self, "_auth_cookie_headers", {}))
            elif self.path == "/api/test-connection":
                api_key = str(body.get("api_key", "")).strip() or str(user_settings["api_key"])
                base_url = _clean_base_url(body.get("base_url", user_settings["base_url"]))
                model = str(body.get("model", user_settings["model"])).strip()
                if not api_key:
                    raise ValueError("Paste an API key first, then test the connection.")
                self._json(_probe_provider(api_key, base_url, model), headers=getattr(self, "_auth_cookie_headers", {}))
            elif self.path == "/api/settings":
                model = str(body.get("model", user_settings["model"])).strip()
                if not model:
                    raise ValueError("Model name cannot be empty.")
                base_url = _clean_base_url(body.get("base_url", user_settings["base_url"]))
                if "api_key" in body and str(body["api_key"]).strip():
                    user_settings["api_key"] = str(body["api_key"]).strip()
                user_settings["model"] = model
                user_settings["base_url"] = base_url
                user_settings["interval_seconds"] = max(60, min(86400, int(body.get("interval_minutes", 30)) * 60))
                user_settings["worker_enabled"] = bool(body.get("worker_enabled", True)) and bool(user_settings["api_key"])
                user_settings["last_worker_error"] = ""
                if user_id is None:
                    local_settings.update(user_settings)
                else:
                    _save_settings(user_id, access_token, user_settings)
                self._json({"ok": True})
            elif self.path == "/api/worker":
                if not user_settings["api_key"]:
                    raise ValueError("Add an API key in Settings before starting the background worker.")
                user_settings["worker_enabled"] = bool(body.get("enabled", False))
                user_settings["last_worker_error"] = ""
                if user_id is None:
                    local_settings.update(user_settings)
                else:
                    _save_settings(user_id, access_token, user_settings)
                self._json({"ok": True})
            elif self.path == "/api/settings/forget-key":
                user_settings["api_key"] = ""
                user_settings["worker_enabled"] = False
                user_settings["last_worker_error"] = ""
                if user_id is None:
                    local_settings.update(user_settings)
                else:
                    _save_settings(user_id, access_token, user_settings)
                self._json({"ok": True})
            elif self.path == "/api/review":
                if not user_settings["api_key"]:
                    raise ValueError("Add an API key in Settings first.")
                api_key, model, base_url = str(user_settings["api_key"]), str(user_settings["model"]), str(user_settings["base_url"])
                with lock:
                    result = run_cycle(store, model=model, api_key=api_key, base_url=base_url)
                user_settings["last_cycle_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if user_id is None:
                    local_settings.update(user_settings)
                else:
                    _save_settings(user_id, access_token, user_settings)
                    _persist_memory(user_id, access_token, store)
                self._json({"ok": True, "queued": bool(result.get("queued_message"))})
            elif self.path == "/api/pause":
                paused = bool(body.get("paused", True))
                with lock:
                    store.set_proactive_pause(paused, note="Changed from the web interface.")
                    _persist_memory(user_id, access_token, store)
                self._json({"ok": True})
            elif self.path == "/api/followups/cancel":
                trigger_id = str(body.get("id", ""))
                if not trigger_id:
                    raise ValueError("Choose a follow-up to cancel.")
                with lock:
                    store.cancel_trigger(trigger_id, note="Cancelled from the web interface.")
                    _persist_memory(user_id, access_token, store)
                self._json({"ok": True})
            else:
                self._json({"error": "Not found."}, 404)
        except PermissionError as exc:
            self._json({"error": str(exc)}, 401)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 502)


def main() -> None:
    if multiuser.HOSTED_MODE:
        multiuser.require_config()
    threading.Thread(target=_background_loop, name="mrtrench-background", daemon=True).start()
    if multiuser.HOSTED_MODE:
        allowed = os.getenv("MR_TRENCH_ALLOWED_HOSTS") or os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
        if not allowed:
            raise RuntimeError("Set MR_TRENCH_ALLOWED_HOSTS to the public app hostname.")
        os.environ["MR_TRENCH_ALLOWED_HOSTS"] = allowed
    host = "0.0.0.0" if multiuser.HOSTED_MODE else "127.0.0.1"
    port = int(os.getenv("PORT", os.getenv("MR_TRENCH_PORT", "8765")))
    server = ThreadingHTTPServer((host, port), MrTrenchHandler)
    print(f"MrTrench is listening on {host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMrTrench web app stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
