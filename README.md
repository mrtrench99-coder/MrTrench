# MrTrench

An experimental assistant application for persistent thoughts, long-term memory, and proactive follow-ups. It uses a hosted LLM through an OpenAI-compatible API; it is not a newly trained or fine-tuned model.

## Project files and local data

- `thought_memory.py` implements the SQLite-backed conversation, thought, memory, and follow-up store.
- `chat_interface.py` provides interactive chat, updates cognitive state, and schedules up to 10 separate follow-ups from one message.
- `background_agent.py` evaluates due follow-ups and periodically reflects on recent conversation and thought state.
- `web_app.py` serves a local browser UI for chat, threads, memories, follow-ups, and background controls; `web/index.html` contains its responsive interface and color themes.
- `web/mrtrench-logo.png` is the current UI logo asset.
- `thought_memory.sqlite3` is created locally and contains conversation and memory data. It is excluded from Git; do not publish or share it.

Keep your API key in the `GROQ_API_KEY` environment variable. Do not paste a real key into source code, the README, or a committed `.env` file. The repository's `.gitignore` excludes the local database, environment files, and Python cache files.
## How the prototype works

The chat process sends each user turn and relevant saved context to the configured LLM. It stores the conversation, selected durable memories, thought threads, and timed triggers in SQLite. A separate background process checks due triggers every five seconds and asks the LLM to decide whether each follow-up is still relevant. It also runs a reflection cycle every 30 minutes by default.

The model is called when a chat turn, due trigger, or reflection cycle needs it; it is not continuously thinking between calls. The worker prints proactive messages in its terminal and stores them in the local outbox. No phone, email, or external chat notification is connected. Leave the worker and computer running for near-time follow-up delivery.

## Project status

This is an early research prototype for experimenting with stateful, cognition-inspired interaction. The current implementation provides persistent conversation history, resumable thought threads, durable memories, cross-thread links, time-based reminders, low-mood check-ins, and periodic background reflection. Its cognitive behavior comes from application logic and LLM prompts; it does not model or reproduce human cognition, and memory quality depends on the configured model and the context sent to it.

The default LLM endpoint is Groq's OpenAI-compatible API with `openai/gpt-oss-20b`. Hosted providers can impose rate limits; a rate-limited request may need to be retried after the wait period stated in the error.

## Cognition-inspired design

MrTrench is an application built around a hosted language model, not a newly trained model or a human brain simulation. The current design borrows software patterns from cognitive-agent research while keeping its claims modest:

- **Working set and persistent memory:** chat and background review each retrieve a small cue-relevant set of thought threads and memories. Memory ranking combines lexical relevance, recency, importance, and confidence. This first version uses lightweight word matching, not embeddings or neural signals.
- **Reactivation and reflection:** a new conversation cue can bring a suspended thread back into view. Background reflections must cite IDs of retrieved memories, and those evidence IDs are saved with the reflection. The background process is a scheduled model call, not continuous awareness.
- **Temporary affect cue:** the chat model can record a tentative affect label for up to eight hours. Neutral or low-confidence interpretations clear it. It is included only to help the next reply choose a considerate tone; it is not a diagnosis, stable personality profile, or reason by itself to initiate contact.
- **Separate attention and action gates:** the model can propose an unsolicited message, but application rules still enforce the user's pause, relevance and novelty thresholds, cooldown, and daily limit. Explicit reminders remain separate from discretionary check-ins.
- **Human-centered voice:** MrTrench aims to be warm, specific, honest, and useful. It must not claim human feelings or consciousness, manufacture a private life, flatter to retain attention, or pressure someone into continuing. The goal is trust and helpfulness, not emotional dependence.

These choices are informed by [Generative Agents](https://arxiv.org/abs/2304.03442), which combines a memory stream, retrieval, reflection, and planning, and [MemGPT](https://arxiv.org/abs/2310.08560), which treats a limited working context and external memory as separate resources. The relevance/recency/importance blend and evidence-linked reflection are adapted as engineering ideas, not copied as a claim about how human memory works. Research on large-scale brain networks describes interacting systems for salience, internally directed cognition, and executive control ([Menon, 2011](https://pubmed.ncbi.nlm.nih.gov/21908230/)); neural rhythms coordinate activity across time, but their computational roles remain an active research question ([Wang, 2010](https://pmc.ncbi.nlm.nih.gov/articles/PMC2923921/)). These findings motivate separating cue selection, background reflection, and outward action in software. They do not justify labeling software components as brain networks or treating their outputs as neural signals.

Emotion support follows a similarly cautious approach: notice what the user actually says, acknowledge it, and let them choose between listening, practical help, or a change of subject. This is consistent with process-based accounts of emotion regulation, which distinguish attention, interpretation, and response ([Gross, 1998](https://journals.sagepub.com/doi/abs/10.1037/1089-2680.2.3.271)). The cue can be wrong, and users can correct or clear it by saying how they feel now.

### Current research limits

The memory retriever is lexical and can miss paraphrases; importance and affect are model estimates; a hosted model may still make mistakes; and a scheduled background cycle can only use information saved in this database. The inspiration is cognitive architecture research, not evidence that MrTrench has subjective experience. Proactive messages can feel intrusive or create unhealthy attachment, so silence, user controls, low message limits, transparent timing, and no engagement-maximizing objective are core behavior requirements.

## Start a conversation

### Browser interface

Run one local process from the project folder:

```powershell
python .\web_app.py
```

Open `http://127.0.0.1:8765` in your browser. Add the Groq API key under **Settings**; the web app keeps a key entered there in memory for the current run and does not save it to disk. **Test API key** checks provider authentication without making a model completion. It distinguishes `invalid_api_key` responses from provider 403 access/security errors and 429 rate/quota errors. The interface includes the conversation, saved thought threads, memories, follow-ups, a background worker toggle, a manual reflection button, an outreach pause, and selectable color themes. The default graphite theme uses a black background with soft grey panels. The app is bound to localhost; it is not yet deployed for public sharing. Stop it with Ctrl+C in that one terminal window.

### Public multi-user hosting status

Do not expose the current web app directly to the public internet. It has no accounts yet, and its SQLite database and in-memory provider settings belong to one local user. The requested public beta should give each person a separate account and memory store, and let each person use their own provider key. That requires authentication, per-user data isolation, protected per-user key handling, request limits, and a production web server before deployment.

One practical route is a Render web service for the Python app plus Supabase Auth/Postgres for accounts and isolated user data. Supabase recommends enabling Row Level Security on exposed tables and writing per-user policies. Render can keep deployment secrets in service environment variables; its service filesystem is ephemeral by default, and persistent disks are available for paid services. For a multi-user service, a managed database with access policies is preferable to sharing this prototype's SQLite file. See the [Supabase RLS guide](https://supabase.com/docs/guides/database/postgres/row-level-security), [Render environment variables and secrets](https://render.com/docs/configure-environment-variables), and [Render persistent disks](https://render.com/docs/disks).

Groq keys must stay server-side and should never be committed or embedded in the browser bundle. Groq's security guidance recommends environment variables or secret management and revoking exposed keys. The current local Settings form sends the key only to the localhost backend and does not save it; the public BYOK flow will need authenticated transport, per-user protection, and a clear delete/forget control. See [Groq's security onboarding](https://console.groq.com/docs/production-readiness/security-onboarding) and [OpenAI-compatible endpoint setup](https://console.groq.com/docs/openai).

The terminal interfaces below are still available if preferred.

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

The default branch is `main`. For this solo prototype, small changes can be committed directly to `main`; a separate branch is optional for larger experiments. A typical update is:

```powershell
git status
git add README.md chat_interface.py
git commit -m "Describe the change"
git push
```

GitHub's **Commits** view shows the project history. Update this README when setup steps or behavior change, and check that the local SQLite database and API keys are not staged before committing.
