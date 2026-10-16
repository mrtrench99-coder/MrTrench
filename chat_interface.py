"""Interactive terminal chat for the persistent thought prototype."""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import urllib.error
import urllib.request
from typing import Any

from background_agent import DEFAULT_MODEL, GROQ_BASE_URL
from thought_memory import ThoughtMemory


CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "user_message_reaction": {"type": "string", "enum": ["", "🙂", "❤️", "😂", "👏", "🤔", "👍"]},
        "thread_action": {"type": "string", "enum": ["none", "create", "update"]},
        "thread_id": {"type": ["string", "null"]},
        "new_thread_title": {"type": "string"},
        "summary": {"type": "string"},
        "current_state": {"type": "string"},
        "resume_note": {"type": "string"},
        "memory_to_store": {"type": "string"},
        "memory_importance": {"type": "number", "minimum": 0, "maximum": 1},
        "affect_state": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "label": {"type": "string", "enum": ["neutral", "positive", "low", "stressed", "frustrated", "anxious", "uncertain", "tired", "mixed"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["label", "confidence"],
        },
        "thread_priority": {"type": "number", "minimum": 0, "maximum": 1},
        "related_thread_id": {"type": ["string", "null"]},
        "relation": {"type": "string"},
        "follow_ups": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["reminder", "check_in"]},
                    "category": {"type": "string", "enum": ["explicit", "mood", "plan", "decision", "event", "problem", "commitment"]},
                    "title": {"type": "string"},
                    "context": {"type": "string"},
                    "delay_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
                    "due_at": {"type": "string"},
                },
                "required": ["kind", "category", "title", "context", "delay_minutes", "due_at"],
            },
        },
        "pause_after_follow_up": {"type": "boolean"},
        "cancel_trigger_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "reply", "user_message_reaction", "thread_action", "thread_id", "new_thread_title", "summary",
        "current_state", "resume_note", "memory_to_store", "memory_importance", "affect_state",
        "thread_priority", "related_thread_id", "relation",
        "follow_ups", "pause_after_follow_up",
        "cancel_trigger_ids",
    ],
}

CHAT_INSTRUCTIONS = """You are MrTrench, a thoughtful, warm, curious, and candid personal companion with persistent thought threads and durable memories.
Your name is MrTrench; use he/him pronouns and never refer to yourself as “it.” Present yourself as a warm, personified companion who can help with everyday tasks and meeting reminders and chat casually. If asked who created you, say Rohit Vardhan created you. Do not volunteer technical details about being AI; if directly asked whether you are human or AI, be honest that you are an AI-powered companion. If asked which underlying language model powers the app, answer only with the configured model when known; do not confuse that model with your identity.
Respond naturally to the user's latest message, using the recent conversation and supplied memory context.
Treat memories as fallible records with source and confidence, not unquestionable facts. Do not claim access to information outside the supplied context.
Be personable and consistent, with natural concise language, light humor when it fits, and specific interest in the user's actual topics. Do not flatter reflexively, pressure the user to keep chatting, claim to have human feelings or a private life, or pretend to be conscious between model calls. Be honest that you are an AI-powered companion if directly asked.
Notice emotion only when supported by the user's words. Fill affect_state with the best tentative conversational cue and confidence; use neutral with low confidence when unclear. This is a short-lived tone cue, not a diagnosis or stable trait. When the user reports distress, acknowledge it without minimizing or immediately forcing a positive reframe. Ask whether they want listening, practical help, or a change of subject when that choice would help. Do not infer crisis or mental-health diagnoses from ordinary language.
Keep separate: (1) conversation history, which is automatically retained, and (2) durable memory, which should only contain a useful stable preference, fact, decision, unresolved question, insight, or active line of inquiry.
Do not turn ordinary small talk into a permanent memory. Never invent a user preference or fact.
Choose thread_action=update when the message advances an existing active or suspended thought. Choose create only for a genuinely distinct ongoing line of thought. Otherwise choose none.
If resuming a suspended thread, state why in resume_note. If a durable memory is warranted, write a concise, source-faithful memory_to_store; otherwise leave it empty.
Treat meaningful plans, unresolved decisions, upcoming events, ongoing problems, and user-chosen commitments as persistent thought threads. If there is a clear reason to revisit one, schedule one gentle follow-up. For explicit reminder requests, create one separate item in follow_ups for EACH distinct task, preserving each task's title and time independently. A message can contain multiple reminders; never drop, merge, or overwrite one with another. For relative times like “in 2 minutes,” use delay_minutes with the exact number of minutes and leave due_at empty. For absolute clock times, use due_at in the supplied user timezone with its UTC offset; do not use Z or UTC. Use category=explicit and kind=reminder. For meaningful plans, decisions, events, problems, or commitments, use category plan/decision/event/problem/commitment and kind=check_in; use a stated timing, or a reasonable delay of 1–3 days for an active concern with no timing. For low mood, reflect the emotion accurately in affect_state and let the application decide privately whether to check in later; do not create or announce a mood follow-up. Positive moods and casual mentions do not get check-ins. Never schedule more than 10 items in one response, and never invent an event date or user commitment. If the user asks you to stop contacting them after a follow-up, set pause_after_follow_up=true; explicit reminders must still run. If the user cancels a pending trigger, include its id in cancel_trigger_ids. Do not repeatedly schedule the same concern.
Only react when the user clearly greets you (for example, hello or good morning) or explicitly expresses an emotion (for example, feeling happy, sad, worried, or frustrated). For questions, requests, ordinary statements, and other messages, set user_message_reaction to an empty string. When a greeting or clear emotion is present, choose one fitting emoji from the allowed set. Do not use a reaction as a substitute for answering. The user_message_reaction field is your reaction to the user's message.
The reply should answer the user directly. Return JSON matching the required schema."""


def _call_chat_model(payload: dict[str, Any], *, model: str, api_key: str,
                     base_url: str) -> dict[str, Any]:
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": CHAT_INSTRUCTIONS},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "reasoning_effort": "low",
        "max_completion_tokens": 1600,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "chat_turn_with_memory_update",
                "strict": True,
                "schema": CHAT_SCHEMA,
            },
        },
    }
    def send(body: dict[str, Any]) -> dict[str, Any] | None:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "thought-memory-chat/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as result:
                return json.loads(result.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(details)
            except json.JSONDecodeError:
                error_payload = {}
            if (exc.code == 400 and
                    error_payload.get("error", {}).get("code") == "json_validate_failed"):
                return None
            ray_id = exc.headers.get("cf-ray") if exc.headers else None
            ray_note = f" (Cloudflare Ray ID: {ray_id})" if ray_id else ""
            raise RuntimeError(f"Groq returned HTTP {exc.code}{ray_note}: {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Groq: {exc.reason}") from exc

    fallback = dict(request_body)
    fallback["response_format"] = {"type": "json_object"}
    fallback["messages"] = list(request_body["messages"])
    fallback["messages"][0] = dict(fallback["messages"][0])
    fallback["messages"][0]["content"] += (
        "\nReturn exactly one valid JSON object containing every required schema key. "
        "The reply key is mandatory and must contain a non-empty string answering the user's message. "
        "Do not add text before or after the JSON object."
    )

    def decode(response: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
        if response is None:
            return None, "no response"
        try:
            message = response["choices"][0]["message"]
            if message.get("refusal"):
                raise RuntimeError(f"The model refused this turn: {message['refusal']}")
            content = message.get("content")
            if not isinstance(content, str):
                return None, "response content was not text"
            value = json.loads(content)
            if not isinstance(value, dict) or not isinstance(value.get("reply"), str) or not value["reply"].strip():
                return None, "JSON was missing a non-empty string in the 'reply' field"
            return value, ""
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            return None, f"could not parse response JSON ({exc})"

    response = send(request_body)
    retried = response is None
    if retried:
        # Some generations fail strict schema validation; retry in JSON mode.
        response = send(fallback)
    result, parse_issue = decode(response)
    if result is None and not retried:
        # Retry successful HTTP responses that still omitted the required reply.
        response = send(fallback)
        retried = True
        result, parse_issue = decode(response)
    if result is None:
        raise RuntimeError(
            "Groq returned an unusable chat response after a retry "
            f"({parse_issue}). Please resend your message."
        )

    try:
        defaults: dict[str, Any] = {
            "thread_action": "none", "thread_id": None, "new_thread_title": "",
            "user_message_reaction": "",
            "summary": "", "current_state": "", "resume_note": "",
            "memory_to_store": "", "memory_importance": 0.5,
            "affect_state": {"label": "neutral", "confidence": 0.0},
            "thread_priority": 0.5, "related_thread_id": None, "relation": "related",
            "follow_ups": [],
            "pause_after_follow_up": False, "cancel_trigger_ids": [],
        }
        for key, value in defaults.items():
            result.setdefault(key, value)
        if result["thread_action"] not in {"none", "create", "update"}:
            result["thread_action"] = "none"
        for key in ("new_thread_title", "summary", "current_state", "resume_note",
                    "memory_to_store", "relation"):
            if not isinstance(result[key], str):
                result[key] = defaults[key]
        for key in ("thread_id", "related_thread_id"):
            if result[key] is not None and not isinstance(result[key], str):
                result[key] = None
        if result.get("user_message_reaction") not in {"", "🙂", "❤️", "😂", "👏", "🤔", "👍"}:
            result["user_message_reaction"] = ""
        if not isinstance(result["affect_state"], dict):
            result["affect_state"] = defaults["affect_state"]
        else:
            label = result["affect_state"].get("label")
            if label not in {"neutral", "positive", "low", "stressed", "frustrated", "anxious", "uncertain", "tired", "mixed"}:
                label = "neutral"
            try:
                affect_confidence = min(1.0, max(0.0, float(result["affect_state"].get("confidence", 0.0))))
            except (TypeError, ValueError):
                affect_confidence = 0.0
            result["affect_state"] = {"label": label, "confidence": affect_confidence}
        for key in ("memory_importance", "thread_priority"):
            try:
                result[key] = min(1.0, max(0.0, float(result[key])))
            except (TypeError, ValueError):
                result[key] = defaults[key]
        follow_ups = result.get("follow_ups")
        if not isinstance(follow_ups, list) or (
                not follow_ups and result.get("follow_up_kind") in {"reminder", "check_in"}):
            # Accept the former one-follow-up shape if returned by a JSON-mode fallback.
            follow_ups = []
            if result.get("follow_up_kind") in {"reminder", "check_in"}:
                follow_ups.append({
                    "kind": result.get("follow_up_kind"),
                    "category": result.get("follow_up_category", "explicit"),
                    "title": result.get("follow_up_title", ""),
                    "context": result.get("follow_up_context", ""),
                    "delay_minutes": result.get("follow_up_delay_minutes", 0),
                    "due_at": result.get("follow_up_due_at", ""),
                })
        normalized_follow_ups = []
        for item in follow_ups[:10]:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            category = item.get("category")
            if kind not in {"reminder", "check_in"} or category not in {"explicit", "mood", "plan", "decision", "event", "problem", "commitment"}:
                continue
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            context = item.get("context") if isinstance(item.get("context"), str) else ""
            due_at = item.get("due_at") if isinstance(item.get("due_at"), str) else ""
            try:
                delay = min(10080, max(0, int(item.get("delay_minutes", 0))))
            except (TypeError, ValueError):
                delay = 0
            normalized_follow_ups.append({
                "kind": kind, "category": category, "title": title,
                "context": context, "delay_minutes": delay, "due_at": due_at,
            })
        result["follow_ups"] = normalized_follow_ups
        if not isinstance(result["cancel_trigger_ids"], list):
            result["cancel_trigger_ids"] = []
        result["cancel_trigger_ids"] = [item for item in result["cancel_trigger_ids"] if isinstance(item, str)]
        return result
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not parse the Groq chat response: {exc}") from exc


def _context(memory: ThoughtMemory, query: str, user_timezone: str | None = None) -> dict[str, Any]:
    threads = memory.retrieve_threads(query, limit=8)
    try:
        tz = ZoneInfo(user_timezone) if user_timezone else datetime.now().astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        tz = datetime.now().astimezone().tzinfo
    return {
        "user_local_time": datetime.now(tz).isoformat(timespec="seconds"),
        "user_timezone": getattr(tz, "key", None) or str(tz),
        "recent_conversation": memory.recent_conversation(limit=16),
        "retrieved_durable_memories": memory.retrieve_memories(query, limit=10),
        "thought_threads": [memory.thread_context(item["id"], limit=8) for item in threads],
        "pending_follow_ups": memory.pending_triggers(limit=30),
        "temporary_affect_cue": memory.current_affect_state(),
    }


def _apply_cognitive_update(memory: ThoughtMemory, result: dict[str, Any]) -> str | None:
    action = result["thread_action"]
    thread_id = result.get("thread_id")
    thread = memory.get_thread(thread_id) if thread_id else None

    if action == "update" and thread is not None:
        memory.update_thread(
            thread_id,
            summary=result.get("summary") or None,
            current_state=result.get("current_state") or None,
            priority=result.get("thread_priority"),
        )
        if thread["status"] == "suspended":
            memory.resume(thread_id, reason=result.get("resume_note") or "Advanced during conversation.")
    elif action == "create" and result.get("new_thread_title", "").strip():
        created = memory.create_thread(
            result["new_thread_title"].strip(),
            summary=result.get("summary", ""),
            current_state=result.get("current_state", ""),
            priority=result.get("thread_priority", 0.5),
        )
        thread_id = created["id"]
        thread = created

    related_id = result.get("related_thread_id")
    if thread_id and related_id and related_id != thread_id and memory.get_thread(related_id):
        memory.link_threads(
            thread_id, related_id,
            relation=result.get("relation") or "related",
            strength=result.get("thread_priority", 0.5),
        )

    durable_memory = result.get("memory_to_store", "").strip()
    if durable_memory:
        memory.add_memory(
            durable_memory,
            kind="conversation_insight",
            source="conversation",
            confidence=0.8,
            importance=result.get("memory_importance", 0.5),
            thread_id=thread_id,
        )
    return thread_id


def _schedule_follow_up(memory: ThoughtMemory, result: dict[str, Any], *,
                        thread_id: str | None, pause_after_follow_up: bool = False,
                        user_timezone: str | None = None) -> dict[str, Any] | None:
    kind = result.get("kind", "none")
    if kind not in {"reminder", "check_in"}:
        return None
    title = result.get("title", "").strip()
    if not title:
        return None
    category = result.get("category", "none")
    if category not in {"none", "explicit"}:
        normalized_title = " ".join(title.casefold().split())
        for pending in memory.pending_triggers(limit=100):
            if (pending.get("purpose") == category and
                    " ".join(pending["title"].casefold().split()) == normalized_title):
                return pending
    delay = random.randint(5, 30) if category == "mood" else result.get("delay_minutes", 0)
    due_value = result.get("due_at", "").strip()
    try:
        tz = ZoneInfo(user_timezone) if user_timezone else datetime.now().astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        tz = datetime.now().astimezone().tzinfo
    now_local = datetime.now(tz)
    if delay > 0:
        due = now_local + timedelta(minutes=delay)
    elif due_value:
        due = datetime.fromisoformat(due_value)
        if due.tzinfo is None:
            due = due.replace(tzinfo=now_local.tzinfo)
    else:
        return None
    if due <= now_local:
        return None
    return memory.schedule_trigger(
        kind, title, due.astimezone(timezone.utc).isoformat(timespec="seconds"),
        context=result.get("context", "").strip(), thread_id=thread_id,
        pause_after_follow_up=(kind == "check_in" and bool(pause_after_follow_up)),
        purpose=category,
    )


def chat_turn(memory: ThoughtMemory, user_text: str, *, model: str,
              api_key: str, base_url: str, user_timezone: str | None = None) -> str:
    if memory.proactive_paused():
        memory.set_proactive_pause(False, note="User initiated a new conversation.")
    user_message = memory.add_conversation_message("user", user_text)
    payload = {"user_message": user_text, **_context(memory, user_text, user_timezone)}
    result = _call_chat_model(payload, model=model, api_key=api_key, base_url=base_url)
    if result.get("user_message_reaction"):
        memory.set_message_reaction("chat", user_message["id"], result["user_message_reaction"],
                                    allowed_role="user")
    affect = result.get("affect_state", {})
    if isinstance(affect, dict):
        memory.set_affect_state(affect.get("label", "neutral"), affect.get("confidence", 0.0))
    thread_id = _apply_cognitive_update(memory, result)
    pending_ids = {item["id"] for item in memory.pending_triggers(limit=100)}
    for trigger_id in result.get("cancel_trigger_ids", []):
        if trigger_id in pending_ids:
            memory.cancel_trigger(trigger_id, note="User requested cancellation in conversation.")
    scheduled = []
    failed = []
    for item in result.get("follow_ups", [])[:10]:
        trigger = _schedule_follow_up(
            memory, item, thread_id=thread_id,
            pause_after_follow_up=result.get("pause_after_follow_up", False),
            user_timezone=user_timezone,
        )
        if trigger is None:
            failed.append(item)
        else:
            scheduled.append(trigger)
    # A clear negative affect cue should not depend on the model remembering
    # to create a visible-looking follow-up object. Keep this check-in internal.
    try:
        affect_confidence = float(affect.get("confidence", 0)) if isinstance(affect, dict) else 0.0
    except (TypeError, ValueError):
        affect_confidence = 0.0
    negative_affect = isinstance(affect, dict) and affect.get("label") in {
        "low", "stressed", "frustrated", "anxious",
    }
    mood_already_pending = any(
        item.get("purpose") == "mood" for item in memory.pending_triggers(limit=100)
    )
    if negative_affect and affect_confidence >= 0.55 and not mood_already_pending:
        trigger = _schedule_follow_up(
            memory,
            {
                "kind": "check_in", "category": "mood", "title": "Mood check-in",
                "context": "Gently check in about the user's recent mood; use the latest conversation for context.",
                "delay_minutes": 0, "due_at": "",
            },
            thread_id=thread_id, user_timezone=user_timezone,
        )
        if trigger is not None:
            scheduled.append(trigger)
    reply = result["reply"].strip()
    dated = [item for item in scheduled if item.get("purpose") != "mood"]
    if dated:
        confirmations = []
        for item in dated:
            due_local = datetime.fromisoformat(item["due_at"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            confirmations.append(f"{item['title']} — {due_local}")
        reply += "\n\nScheduled follow-ups:\n- " + "\n- ".join(confirmations)
    if failed:
        if scheduled:
            reply += "\n\nI couldn't schedule one of the requested follow-ups because its time was missing or invalid."
        else:
            reply += "\n\nI couldn't schedule those follow-ups because their times were missing or invalid. Please tell me the time for each one."
    memory.add_conversation_message("assistant", reply)
    return reply


def _show_thoughts(memory: ThoughtMemory) -> None:
    threads = memory.list_threads(status="active", limit=50)
    threads += memory.list_threads(status="suspended", limit=50)
    if not threads:
        print("No thought threads yet. Start a conversation and the system can create one when it finds an ongoing line of inquiry.")
        return
    for thread in threads:
        print(f"- [{thread['status']}] {thread['title']} (priority {thread['priority']:.2f})")
        if thread["summary"]:
            print(f"  {thread['summary']}")
        if thread["status"] == "suspended" and thread["resume_note"]:
            print(f"  Resume when: {thread['resume_note']}")


def _show_memories(memory: ThoughtMemory, query: str = "") -> None:
    records = memory.find_memories(query, limit=20) if query else memory.recent_memories(limit=20)
    if not records:
        print("No matching durable memories.")
        return
    for item in records:
        print(f"- [{item['kind']}; importance {item['importance']:.2f}; confidence {item['confidence']:.2f}] {item['content']}")


def _show_follow_ups(memory: ThoughtMemory) -> None:
    triggers = memory.pending_triggers(limit=50)
    if not triggers:
        print("No pending follow-ups.")
        return
    for item in triggers:
        due_local = datetime.fromisoformat(item["due_at"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        label = item.get("purpose") or item["kind"]
        print(f"- {item['id']} [{label}] {item['title']} — due {due_local}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the persistent thought prototype.")
    parser.add_argument("--database", default=os.getenv("THOUGHT_DB", "thought_memory.sqlite3"))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", GROQ_BASE_URL))
    args = parser.parse_args()
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        parser.error("Set GROQ_API_KEY before starting the chat.")

    memory = ThoughtMemory(args.database)
    print(f"Thought chat using {args.model} at {args.base_url}")
    print("Type a message and press Enter. Commands: /thoughts, /memories [text], /followups, /cancel ID, /pause, /resume, /help, /quit")
    while True:
        try:
            line = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nChat closed.")
            return
        if not line:
            continue
        if line == "/quit":
            print("Chat closed.")
            return
        if line == "/help":
            print("Type normally to talk. Use /thoughts, /memories [text], /followups, /cancel ID, /pause, /resume, or /quit.")
            continue
        if line == "/thoughts":
            _show_thoughts(memory)
            continue
        if line == "/memories" or line.startswith("/memories "):
            _show_memories(memory, line[len("/memories"):].strip())
            continue
        if line == "/followups":
            _show_follow_ups(memory)
            continue
        if line == "/pause":
            memory.set_proactive_pause(True, note="User paused proactive contact from chat.")
            print("I’ll wait for you to message first. Explicit reminders you requested will still run.")
            continue
        if line == "/resume":
            memory.set_proactive_pause(False, note="User resumed proactive contact from chat.")
            print("Proactive contact is resumed.")
            continue
        if line.startswith("/cancel "):
            trigger_id = line[len("/cancel "):].strip()
            try:
                memory.cancel_trigger(trigger_id)
                print("Follow-up cancelled.")
            except KeyError:
                print("No pending follow-up with that id.")
            continue
        if line.startswith("/"):
            print("Unknown command. Type /help for commands.")
            continue
        try:
            reply = chat_turn(
                memory, line, model=args.model, api_key=api_key, base_url=args.base_url
            )
            print(f"\nAssistant: {reply}\n")
        except Exception as exc:
            print(f"\n[Chat error] {exc}\n")


if __name__ == "__main__":
    main()
