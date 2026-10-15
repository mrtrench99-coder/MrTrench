# Persistent thought prototype

This prototype has an interactive conversation plus an optional slower idle reflection loop.

## Project files and local data

- `thought_memory.py` implements the SQLite-backed conversation, thought, memory, and follow-up store.
- `chat_interface.py` provides the interactive chat and schedules up to 10 separate follow-ups from one message.
- `background_agent.py` evaluates due follow-ups and performs periodic reflection.
- `thought_memory.sqlite3` is created locally and contains conversation and memory data. It is excluded from Git; do not publish or share it.

Keep your API key in the `GROQ_API_KEY` environment variable. Do not paste a real key into source code, the README, or a committed `.env` file. The repository's `.gitignore` excludes the local database, environment files, and Python cache files.

- `thought_memory.py` stores conversation history, resumable thought threads, durable memories, links, events, timed triggers, and initiated messages in SQLite.
- `chat_interface.py` answers each user message immediately and lets the model update durable cognitive state when useful.
- `background_agent.py` watches pending reminders/check-ins and reviews recent conversation and thought state during quiet periods. It checks timers every five seconds and does a deeper idle review every 30 minutes.

## Start a conversation

In PowerShell, set your Groq key in the current window and start the chat:

```powershell
$env:GROQ_API_KEY = "your-groq-key"
python .\chat_interface.py
```

Type normally and press Enter. The assistant answers immediately. Commands:

- `/thoughts` shows active and suspended thought threads.
- `/memories` shows recent durable memories; `/memories words` searches memory text.
- `/followups` lists pending reminders and check-ins; `/cancel ID` cancels one.
- `/pause` makes the assistant wait for you to message first; `/resume` turns proactive contact back on.
- `/help` lists commands; `/quit` exits.

Every user and assistant turn is saved in the conversation log. Conversation turns are not automatically promoted to durable memory; the model is asked to preserve only useful stable facts, preferences, decisions, unresolved questions, insights, and ongoing lines of inquiry.

## Timed follow-ups and idle reflection

Open a second PowerShell window, set the same key there, go to the project folder, then run:

```powershell
$env:GROQ_API_KEY = "your-groq-key"
python .\background_agent.py
```

Keep this worker running to deliver follow-ups after you close the chat. It checks due triggers every five seconds, and reviews recent conversation, active/suspended threads, durable memories, and recent events every 30 minutes by default. Change the idle interval with `--interval` / `THOUGHT_INTERVAL_SECONDS`; change the trigger polling interval with `--poll-seconds` / `THOUGHT_TRIGGER_POLL_SECONDS`. A due message prints in the worker terminal and is saved in the SQLite outbox; no external chat or notification channel is connected. The computer and worker must remain on for near-time delivery. If the worker is stopped, due triggers remain saved and are handled when it starts again.

Explicit requests such as "remind me in two minutes" can create one-time reminders. The assistant can schedule up to **10 separate follow-ups in one message**, preserving each task and due time. There is no configured total limit on pending triggers in the database. For meaningful plans, decisions, events, ongoing problems, and commitments, the assistant can preserve the thought and schedule one follow-up when there is a clear reason to revisit it. It uses a stated time or event date when available; otherwise, it chooses a reasonable 1–3 day window. A significant low-mood disclosure gets a separate gentle check-in at a random time between 5 and 30 minutes. Casual mentions and positive moods do not automatically create check-ins. If you ask it to stop initiating contact after a follow-up, it waits until you message again. `/pause` and `/resume` control this directly. A pause suppresses unsolicited messages and check-ins, while explicitly requested reminders remain active. These follow-ups are visible and cancellable with `/followups` and `/cancel ID`.

## Model and storage configuration

The default endpoint is `https://api.groq.com/openai/v1` and the default model is `openai/gpt-oss-20b`. The default database is `thought_memory.sqlite3`. Override these with `--base-url`, `--model`, and `--database`, or the environment variables `OPENAI_BASE_URL`, `OPENAI_MODEL`, and `THOUGHT_DB`.

Each chat turn, due-trigger decision, and idle review makes a hosted model call. The model is not running continuously between calls: the worker wakes to check due times, then calls the model when a trigger is due or an idle review is scheduled. The system only knows the information saved in this database; it does not read the computer or the internet. Review what you store before sending personal information to a hosted model.

## GitHub development workflow

After connecting this folder to a GitHub repository, make a focused commit for each completed feature or fix. Update this README whenever commands, behavior, or setup steps change. Before pushing, check `git status` and confirm that `thought_memory.sqlite3`, API keys, and other private data are not staged. Use commit messages that describe the change, such as `Support multiple reminders per message`.
