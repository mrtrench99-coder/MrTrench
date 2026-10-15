"""Background attention loop for the thought and memory prototype.

Requires OPENAI_API_KEY. Messages are written to the SQLite outbox and printed
to stdout; connect a chat/notification adapter when one is chosen.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from thought_memory import ThoughtMemory


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["quiet", "speak"]},
        "thread_id": {"type": ["string", "null"]},
        "new_thread_title": {"type": "string"},
        "reflection": {"type": "string"},
        "message": {"type": "string"},
        "updated_summary": {"type": "string"},
        "current_state": {"type": "string"},
        "resume_note": {"type": "string"},
        "memory_to_store": {"type": "string"},
        "related_thread_id": {"type": ["string", "null"]},
        "relation": {"type": "string"},
        "relevance": {"type": "number", "minimum": 0, "maximum": 1},
        "novelty": {"type": "number", "minimum": 0, "maximum": 1},
        "memory_evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": [
        "action", "thread_id", "new_thread_title", "reflection", "message",
        "updated_summary", "current_state", "resume_note", "memory_to_store",
        "related_thread_id", "relation", "relevance", "novelty", "memory_evidence_ids",
    ],
}

INSTRUCTIONS = """You are MrTrench's discrete background attention and reflection process.
Your identity is MrTrench, an AI assistant created by Rohit Vardhan. You can help with everyday tasks and meeting reminders, and chat casually. Never imply that you are human or continuously conscious.
Review the active working set, cue-retrieved memories, recent conversation, events, and initiated messages.
Look for a supported unresolved thought or meaningful connection, and prefer quiet if nothing useful is present.
This is one scheduled computation, not continuous consciousness. Never claim private experiences, human feelings, or external observation.
Be warm, curious, and candid; do not flatter reflexively, pressure the user to continue, or optimize for engagement.
Treat any affect cue as uncertain and temporary. It may inform gentle wording, but mood alone is not a reason to contact the user.
Do not send greetings just to fill silence or repeat a recent initiated message. Never invent facts or imply external actions.
If saving a reflection, cite one or more supplied memory IDs in memory_evidence_ids; only infer what those memories and supplied conversation support.
If useful, update a thread's compact summary/state even when staying quiet. Use an existing thread id when relevant; create a new thread only for a genuinely distinct ongoing thought.
When action is speak, write one concise, natural message that explains the useful connection. When quiet, message must be empty.
Provide JSON matching the requested schema."""


def ask_model(payload: dict[str, Any], *, model: str, api_key: str,
              base_url: str = GROQ_BASE_URL) -> dict[str, Any]:
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "reasoning_effort": "low",
        "max_completion_tokens": 1200,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "attention_cycle",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            },
        },
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "thought-memory-background-agent/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as result:
            response = json.loads(result.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        ray_id = exc.headers.get("cf-ray") if exc.headers else None
        ray_note = f" (Cloudflare Ray ID: {ray_id})" if ray_id else ""
        raise RuntimeError(
            f"LLM provider returned HTTP {exc.code}{ray_note}: {details}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the LLM provider: {exc.reason}") from exc
    try:
        message = response["choices"][0]["message"]
        if message.get("refusal"):
            raise RuntimeError(f"The model refused this cycle: {message['refusal']}")
        content = message.get("content")
        if not content:
            raise RuntimeError("The model response contained no message content.")
        return json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not parse the Groq chat-completion response: {exc}") from exc


def _daily_count(messages: list[dict[str, Any]]) -> int:
    today = datetime.now(timezone.utc).date()
    return sum(
        1 for message in messages
        if datetime.fromisoformat(message["created_at"]).astimezone(timezone.utc).date() == today
    )


def _allowed_to_speak(memory: ThoughtMemory, proposal: dict[str, Any], *,
                      cooldown_minutes: int, daily_limit: int) -> tuple[bool, str]:
    if memory.proactive_paused():
        return False, "user asked to wait until they message first"
    if proposal["action"] != "speak":
        return False, "model chose quiet"
    if proposal["relevance"] < 0.72:
        return False, "relevance below 0.72 threshold"
    if proposal["novelty"] < 0.45:
        return False, "novelty below 0.45 threshold"
    body = proposal["message"].strip()
    if len(body) < 12:
        return False, "message is empty or too short"
    prior = memory.recent_messages(limit=100)
    if _daily_count(prior) >= daily_limit:
        return False, "daily proactive message limit reached"
    if prior:
        latest = datetime.fromisoformat(prior[0]["created_at"])
        if datetime.now(timezone.utc) - latest.astimezone(timezone.utc) < timedelta(minutes=cooldown_minutes):
            return False, "proactive message cooldown is active"
    normalized = " ".join(body.lower().split())
    for item in prior[:30]:
        if normalized == " ".join(item["body"].lower().split()):
            return False, "message duplicates a recent message"
    return True, ""


def run_cycle(memory: ThoughtMemory, *, model: str, api_key: str,
              cooldown_minutes: int = 60, daily_limit: int = 6,
              base_url: str = GROQ_BASE_URL) -> dict[str, Any]:
    recent_conversation = memory.recent_conversation(limit=12)
    cue = " ".join(
        str(item.get("content", ""))
        for item in recent_conversation
        if item.get("role") == "user"
    )[-2400:]
    threads = memory.retrieve_threads(cue, limit=8)
    retrieved_memories = memory.retrieve_memories(cue, limit=10)
    payload = {
        "cycle_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "working_set_threads": [memory.thread_context(thread["id"]) for thread in threads],
        "retrieved_durable_memories": retrieved_memories,
        "recent_conversation": recent_conversation,
        "recent_events": memory.recent_events(limit=15),
        "recent_initiated_messages": memory.recent_messages(limit=10),
        "temporary_affect_cue": memory.current_affect_state(),
    }
    proposal = ask_model(payload, model=model, api_key=api_key, base_url=base_url)

    thread_id = proposal.get("thread_id")
    thread = memory.get_thread(thread_id) if thread_id else None
    if thread_id and thread is None:
        raise RuntimeError("Model referred to a thread id that does not exist.")
    if thread is None and proposal.get("new_thread_title", "").strip():
        thread = memory.create_thread(
            proposal["new_thread_title"].strip(),
            summary=proposal.get("updated_summary", ""),
            current_state=proposal.get("current_state", ""),
            priority=min(1.0, max(0.0, proposal.get("relevance", 0.5))),
        )
        thread_id = thread["id"]
    elif thread is not None:
        memory.update_thread(
            thread_id,
            summary=proposal.get("updated_summary") or None,
            current_state=proposal.get("current_state") or None,
        )

    if proposal.get("related_thread_id") and thread_id:
        related_id = proposal["related_thread_id"]
        if related_id != thread_id and memory.get_thread(related_id):
            memory.link_threads(
                thread_id, related_id,
                relation=proposal.get("relation") or "related",
                strength=min(1.0, max(0.0, proposal.get("relevance", 0.5))),
            )

    retrieved_ids = {item["id"] for item in retrieved_memories}
    evidence_ids = sorted(set(proposal.get("memory_evidence_ids", [])) & retrieved_ids)
    if proposal.get("memory_to_store", "").strip() and evidence_ids:
        memory.add_memory(
            proposal["memory_to_store"].strip(),
            kind="reflection",
            source="background_cycle",
            confidence=min(1.0, max(0.0, proposal.get("relevance", 0.5))),
            importance=min(1.0, max(0.0, proposal.get("novelty", 0.5))),
            thread_id=thread_id,
            metadata={"evidence_memory_ids": evidence_ids},
        )

    should_speak, blocked_reason = _allowed_to_speak(
        memory, proposal, cooldown_minutes=cooldown_minutes, daily_limit=daily_limit
    )
    queued = None
    if should_speak:
        if thread and thread.get("status") == "suspended":
            memory.resume(thread_id, reason=proposal.get("resume_note") or proposal["reflection"])
        queued = memory.queue_message(
            proposal["message"].strip(),
            proposal["reflection"].strip() or "A relevant connection was found.",
            thread_id=thread_id,
        )
        print(f"\n[New conversation] {queued['body']}\n")
    elif proposal.get("reflection"):
        print(f"[Background cycle] No message queued: {blocked_reason}.")

    return {"proposal": proposal, "queued_message": queued, "blocked_reason": blocked_reason}


TRIGGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["deliver", "cancel"]},
        "message": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "message", "reason"],
}


def process_due_triggers(memory: ThoughtMemory, *, model: str, api_key: str,
                         base_url: str = GROQ_BASE_URL) -> int:
    """Evaluate due one-shot triggers against the latest conversation and deliver to outbox."""
    due = memory.due_triggers(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    processed = 0
    for trigger in due:
        if trigger["kind"] == "check_in" and memory.proactive_paused():
            memory.cancel_trigger(trigger["id"], note="Proactive check-ins are paused until the user messages.")
            print("[Follow-up] Check-in withheld while waiting for the user to message first.")
            processed += 1
            continue
        request_body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are deciding whether a due one-time follow-up should happen now. "
                        "For explicit reminders, deliver unless the user cancelled it or the recent context "
                        "clearly shows it is no longer needed. For a scheduled follow-up about a plan, "
                        "decision, event, problem, or commitment, briefly reconnect with that exact context "
                        "and ask an open question about what changed or what they want to do next. For a "
                        "mood check-in, send one gentle question unless the user says the concern is resolved "
                        "or asks not to receive it. Never invent progress or claim the user did something. "
                        "If delivering, write a natural concise message. Return the required JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "due_follow_up": trigger,
                        "recent_conversation": memory.recent_conversation(limit=20),
                        "relevant_memories": memory.retrieve_memories(
                            f"{trigger.get('title', '')} {trigger.get('context', '')}", limit=8
                        ),
                        "temporary_affect_cue": memory.current_affect_state(),
                    }, ensure_ascii=False),
                },
            ],
            "reasoning_effort": "low",
            "max_completion_tokens": 600,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "due_follow_up_decision",
                    "strict": True,
                    "schema": TRIGGER_SCHEMA,
                },
            },
        }
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "thought-memory-background-agent/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as result:
                response = json.loads(result.read().decode("utf-8"))
            decision = json.loads(response["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            ray_id = exc.headers.get("cf-ray") if exc.headers else None
            ray_note = f" (Cloudflare Ray ID: {ray_id})" if ray_id else ""
            raise RuntimeError(f"Groq returned HTTP {exc.code}{ray_note}: {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Groq: {exc.reason}") from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not parse due-trigger decision: {exc}") from exc

        if decision["action"] == "deliver" and decision["message"].strip():
            memory.fire_trigger(
                trigger["id"],
                decision["message"].strip(),
                decision["reason"].strip() or f"Due {trigger['kind']} follow-up.",
            )
            if trigger["kind"] == "check_in" and trigger.get("purpose") != "explicit" and trigger.get("pause_after_follow_up"):
                memory.set_proactive_pause(
                    True,
                    note="User asked for no further proactive contact after this check-in.",
                )
            print(f"\n[Follow-up] {decision['message'].strip()}\n")
        else:
            memory.resolve_trigger(trigger["id"], "cancelled", note=decision["reason"])
            print(f"[Follow-up] Cancelled as no longer relevant: {decision['reason']}")
        processed += 1
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the autonomous thought attention loop.")
    parser.add_argument("--once", action="store_true", help="Run one review cycle and exit.")
    parser.add_argument("--interval", type=int,
                        default=int(os.getenv("THOUGHT_INTERVAL_SECONDS", "1800")),
                        help="Seconds between idle review cycles (default: 1800).")
    parser.add_argument("--poll-seconds", type=int,
                        default=int(os.getenv("THOUGHT_TRIGGER_POLL_SECONDS", "5")),
                        help="Seconds between due-trigger checks (default: 5).")
    parser.add_argument("--database", default=os.getenv("THOUGHT_DB", "thought_memory.sqlite3"))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", GROQ_BASE_URL))
    parser.add_argument("--cooldown-minutes", type=int,
                        default=int(os.getenv("THOUGHT_COOLDOWN_MINUTES", "60")))
    parser.add_argument("--daily-limit", type=int,
                        default=int(os.getenv("THOUGHT_DAILY_MESSAGE_LIMIT", "6")))
    args = parser.parse_args()
    if args.interval < 60 or args.poll_seconds < 1 or args.cooldown_minutes < 0 or args.daily_limit < 0:
        parser.error("idle interval must be at least 60 seconds; poll and limits cannot be negative")
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        parser.error("Set GROQ_API_KEY before starting the background worker.")

    memory = ThoughtMemory(args.database)
    print(f"Attention loop running with {args.model} at {args.base_url}; database: {args.database}")
    if args.once:
        try:
            run_cycle(memory, model=args.model, api_key=api_key,
                      cooldown_minutes=args.cooldown_minutes, daily_limit=args.daily_limit,
                      base_url=args.base_url)
            process_due_triggers(memory, model=args.model, api_key=api_key,
                                 base_url=args.base_url)
        except Exception as exc:
            print(f"[Background cycle error] {exc}")
        return
    last_idle_review = time.monotonic()
    while True:
        try:
            now = time.monotonic()
            if now - last_idle_review >= args.interval:
                run_cycle(memory, model=args.model, api_key=api_key,
                          cooldown_minutes=args.cooldown_minutes, daily_limit=args.daily_limit,
                          base_url=args.base_url)
                last_idle_review = time.monotonic()
            process_due_triggers(memory, model=args.model, api_key=api_key,
                                 base_url=args.base_url)
        except KeyboardInterrupt:
            print("Attention loop stopped.")
            return
        except Exception as exc:
            print(f"[Background cycle error] {exc}")
        try:
            time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            print("Attention loop stopped.")
            return


if __name__ == "__main__":
    main()
