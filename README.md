# Hermes — personal 24/7 agent (free, no credit card)

Your own agent running 24/7 on Render's free plan. You command it from
Telegram; it researches, watches things, runs tasks on schedules, and
reports back. Owner-only — nobody else can command it.

## Commands (send them to your bot on Telegram)
```
/help    list commands
/tasks   list installed tasks
/run <name>     run a task immediately
/ask <question> research: free web search + your LLM, with sources
/status uptime, memory, recent activity
/say <text>     plain chat with the LLM
```

## Setup (~30 min, ৳0, no card)

### 1. Create the Telegram bot (2 min)
1. Telegram → **@BotFather** → `/newbot` → name + username (ends in `bot`).
2. Copy the **token**.

### 3. Find your chat id (1 min)
Message **@userinfobot** on Telegram — it replies with your numeric id.
That's `OWNER_CHAT_ID`. The agent ignores everyone who isn't this id.

### 3. Push to GitHub
New repo (private is fine) → upload all files.

### 4. Deploy on Render (free, GitHub login)
New → Web Service → connect repo. Verify **Plan: Free**, start command
`gunicorn app:app --timeout 120`, health check `/health`.
Environment tab:
- `TELEGRAM_BOT_TOKEN` = bot token
- `OWNER_CHAT_ID` = your numeric id from step 2
- `LLM_API_KEY` = your OpenRouter key
- `LLM_BASE_URL` = `https://openrouter.ai/api/v1`
- `LLM_MODEL` = primary model, e.g. `z-ai/glm-5.2:free`
- `LLM_FALLBACK_MODELS` = comma-separated backups, e.g.
  `minimax/minimax-m3:free,thinkingmachines/inkling:free,openrouter/free`
- `LLM_API_FORMAT` = `openai` (skips the format probe)
- `BRIEFING_TOPIC` = whatever you want the daily briefing about
- `GIST_TOKEN` = GitHub token with gist read/write — see Persistence

## Memory (free — 0 LLM requests)
"remember my wifi password is xyz" → stored in the gist, survives
restarts. "what's my wifi password?" / "what do you remember about X" /
"what was the X I talked about" → recalled, free. `/memories` lists,
`/forget <what>` deletes (or "forget everything"). Relevant memories
are also injected into normal chat replies automatically — still 1
request per message. The bot also keeps your last 6 chat exchanges for
context, so follow-up questions work.
⚠️ Memories are plain text in a secret gist on GitHub — fine for
low-stakes facts, NOT a password vault. Real passwords belong in a
password manager.

## Link reader (1 request per link)
Paste any URL (or `/summarize <url>`) → the server fetches and reads it,
you get a 5-10 bullet summary. Saves you mobile data on heavy pages.
A URL inside a task request ("watch https://… every 10m") still creates
the task — it won't be hijacked into a summary.

## Persistence (survives restarts, ~5 min, no card)
Without it, every restart/deploy wipes reminders, tasks created by
message, and price baselines. With it, all of that is saved to a secret
gist on your GitHub account and restored on boot.
1. GitHub → profile → Settings → Developer settings → Personal access
   tokens → fine-grained → Generate: permission **Account → Gists →
   Read and write**. (Or classic token with the `gist` scope.)
2. Render → Environment → add `GIST_TOKEN` = that token.
3. Done — the gist is created automatically on first save.
Missed reminders fire on next boot; `/status` shows `state: gist-backed`.
Note: fine-grained tokens expire — set the bot a reminder to rotate it.

### 5. Keep awake — UptimeRobot (free, no card)
HTTP(s) monitor, every 5 min, `https://your-service.onrender.com/health`.

Then message your bot `/status` — you should get uptime and a log back.

## Adding your own automations
Open `tasks.py` — each task is a dict with `desc`, `schedule`, `run`.
Your `run` function gets a ctx with `llm()`, `web_search()`, `tg_send()`,
`log()`. Write the work, message yourself the result. That's it.
Schedule helpers: `_daily(hour, minute)` or write any function of `now`.

Test a task instantly with `/run <name>` before trusting its schedule.

## Limits (honest)
- Free Render: 750 instance-hours/mo ≈ 24/7 for ONE service. Fits, barely.
- Disk is ephemeral (wiped on restart/deploy) — keep state in Google
  Sheets, a Telegram message to yourself, or a GitHub repo.
- `/ask` searches the live web via ddgs; quality varies, always cites.
- Render restarts the service now and then; loops resume automatically.
