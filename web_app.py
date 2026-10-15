"""Local browser interface for chatting with MrTrench and running its worker."""

from __future__ import annotations

import json
import os
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


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = os.getenv("THOUGHT_DB", str(ROOT / "thought_memory.sqlite3"))
memory = ThoughtMemory(DB_PATH)
state_lock = threading.RLock()
settings_lock = threading.RLock()
settings: dict[str, object] = {
    "api_key": os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
    "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
    "base_url": os.getenv("OPENAI_BASE_URL", GROQ_BASE_URL),
    "interval_seconds": max(60, int(os.getenv("THOUGHT_INTERVAL_SECONDS", "1800"))),
    "worker_enabled": bool(os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")),
    "last_cycle_at": None,
    "last_worker_error": "",
}


def _clean_base_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Enter a valid http or https API base URL.")
    return url


def _probe_provider(api_key: str, base_url: str, model: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"{_clean_base_url(base_url)}/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
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


def _snapshot() -> dict[str, object]:
    with state_lock, settings_lock:
        return {
            "messages": memory.recent_conversation(limit=100),
            "proactive_messages": memory.recent_messages(limit=30),
            "threads": memory.list_threads(status="active", limit=30)
            + memory.list_threads(status="suspended", limit=30),
            "memories": memory.recent_memories(limit=50),
            "follow_ups": memory.pending_triggers(limit=100),
            "proactive_paused": memory.proactive_paused(),
            "worker": {
                "enabled": bool(settings["worker_enabled"]),
                "configured": bool(settings["api_key"]),
                "last_cycle_at": settings["last_cycle_at"],
                "last_error": settings["last_worker_error"],
                "interval_seconds": settings["interval_seconds"],
                "model": settings["model"],
                "base_url": settings["base_url"],
            },
        }


def _background_loop() -> None:
    current_interval = int(settings["interval_seconds"])
    next_review = time.monotonic() + current_interval
    retry_after = 0.0
    while True:
        time.sleep(2)
        with settings_lock:
            enabled = bool(settings["worker_enabled"] and settings["api_key"])
            api_key = str(settings["api_key"])
            model = str(settings["model"])
            base_url = str(settings["base_url"])
            interval = int(settings["interval_seconds"])
        if interval != current_interval:
            current_interval = interval
            next_review = time.monotonic() + interval
        if not enabled:
            next_review = time.monotonic() + interval
            continue
        if time.monotonic() < retry_after:
            continue
        try:
            with state_lock:
                process_due_triggers(memory, model=model, api_key=api_key, base_url=base_url)
                if time.monotonic() >= next_review:
                    run_cycle(memory, model=model, api_key=api_key, base_url=base_url)
                    next_review = time.monotonic() + interval
                    with settings_lock:
                        settings["last_cycle_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        settings["last_worker_error"] = ""
        except Exception as exc:  # keep the local UI alive when a provider is unavailable
            with settings_lock:
                settings["last_worker_error"] = str(exc)[:1000]
            retry_after = time.monotonic() + 30


class MrTrenchHandler(BaseHTTPRequestHandler):
    server_version = "MrTrenchLocal/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        # Avoid writing chat contents or local API activity to terminal logs.
        return

    def _json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
        allowed_hosts = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        return host in allowed_hosts and (not origin or urlparse(origin).netloc.lower() == host)

    def _trusted_host(self) -> bool:
        host = self.headers.get("Host", "").lower()
        return host in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted_host():
            self.send_error(403)
            return
        if self.path == "/api/state":
            self._json(_snapshot())
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
            if self.path == "/api/chat":
                text = str(body.get("message", "")).strip()
                if not text:
                    raise ValueError("Write a message first.")
                with settings_lock:
                    api_key = str(settings["api_key"])
                    model = str(settings["model"])
                    base_url = str(settings["base_url"])
                if not api_key:
                    raise ValueError("Add your API key in Settings before chatting.")
                with state_lock:
                    reply = chat_turn(memory, text, model=model, api_key=api_key, base_url=base_url)
                self._json({"reply": reply})
            elif self.path == "/api/test-connection":
                with settings_lock:
                    api_key = str(body.get("api_key", "")).strip() or str(settings["api_key"])
                    base_url = _clean_base_url(body.get("base_url", settings["base_url"]))
                    model = str(body.get("model", settings["model"])).strip()
                if not api_key:
                    raise ValueError("Paste an API key first, then test the connection.")
                self._json(_probe_provider(api_key, base_url, model))
            elif self.path == "/api/settings":
                model = str(body.get("model", settings["model"])).strip()
                if not model:
                    raise ValueError("Model name cannot be empty.")
                base_url = _clean_base_url(body.get("base_url", settings["base_url"]))
                with settings_lock:
                    if "api_key" in body and str(body["api_key"]).strip():
                        settings["api_key"] = str(body["api_key"]).strip()
                    settings["model"] = model
                    settings["base_url"] = base_url
                    settings["interval_seconds"] = max(60, min(86400, int(body.get("interval_minutes", 30)) * 60))
                    settings["worker_enabled"] = bool(body.get("worker_enabled", True)) and bool(settings["api_key"])
                    settings["last_worker_error"] = ""
                self._json({"ok": True})
            elif self.path == "/api/worker":
                with settings_lock:
                    if not settings["api_key"]:
                        raise ValueError("Add an API key in Settings before starting the background worker.")
                    settings["worker_enabled"] = bool(body.get("enabled", False))
                    settings["last_worker_error"] = ""
                self._json({"ok": True})
            elif self.path == "/api/review":
                with settings_lock:
                    if not settings["api_key"]:
                        raise ValueError("Add an API key in Settings first.")
                    api_key, model, base_url = str(settings["api_key"]), str(settings["model"]), str(settings["base_url"])
                with state_lock:
                    result = run_cycle(memory, model=model, api_key=api_key, base_url=base_url)
                with settings_lock:
                    settings["last_cycle_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._json({"ok": True, "queued": bool(result.get("queued_message"))})
            elif self.path == "/api/pause":
                paused = bool(body.get("paused", True))
                with state_lock:
                    memory.set_proactive_pause(paused, note="Changed from the local web interface.")
                self._json({"ok": True})
            elif self.path == "/api/followups/cancel":
                trigger_id = str(body.get("id", ""))
                if not trigger_id:
                    raise ValueError("Choose a follow-up to cancel.")
                with state_lock:
                    memory.cancel_trigger(trigger_id, note="Cancelled from the local web interface.")
                self._json({"ok": True})
            else:
                self._json({"error": "Not found."}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 502)


def main() -> None:
    threading.Thread(target=_background_loop, name="mrtrench-background", daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", int(os.getenv("MR_TRENCH_PORT", "8765"))), MrTrenchHandler)
    print(f"MrTrench is ready at http://127.0.0.1:{server.server_port}")
    print("This local server keeps your API key in memory only; use Ctrl+C to stop it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMrTrench web app stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
