# Hermes — a personal 24/7 agent

Free stack, no credit card: **Render** (free web service) + **Telegram**
+ **OpenRouter / Groq / Gemini** free tiers + a **secret GitHub gist**
for persistent state. Kept awake by a free UptimeRobot ping on `/health`.

## Architecture

```
app.py       composition root: web server, Telegram loop, scheduler,
             slash commands (table-driven), photo/voice handling, boot
router.py    THE BRAIN — one LLM call classifies any plain message
             into an action (18 actions, no hand-written regexes)
tasks.py     built-in tasks, dynamic-task factory, management (find/
             stop/resume/reschedule) — all in Dhaka time
runner.py    executes a task with the shared toolkit injected into ctx
forge.py     skill forge: the LLM writes its own task code, test-runs
             it in a guarded sandbox, only saves what passes
memory.py    persistent facts, auto-extracted facts, chat context,
             expense ledger
reminders.py natural-language reminder engine (10-second precision)
research.py  free web search, link reader, /ask, deep research
llm.py       multi-provider chain (groq:/gemini: prefixes), quota
comms.py     Telegram sending (HTML-aware), typing indicator, email
config.py    every environment variable in one place
state.py     gist-backed persistence (survives restarts & redeploys)
```

## Talking to it — just talk

```
"remember my wifi password is xyz"     → "what's my wifi password?"
"remind me in 10m …"                   → buzz, 10s precision
"alert me when bitcoin drops below 60000"
"tell me ethereum's price every 30m"
"weather in 6 hours" / "watch @channel for new videos"
"spent 120 on rickshaw"                → "how much did I spend?"
"email x@gmail.com saying I'll be late"
"check the USD→BDT rate hourly, tell me if it moves"  → bot writes
                                                       its own code
"stop the bitcoin alert" / "make it every 10m" / "enable prayer"
📷 photo + question • 🎙 voice note • 🔗 paste a link → summary
```

Several requests in one message work. Slash commands: /help /tasks /run
/ask /deep /say /remind /reminders /memories /forget /expenses /quota
/report /status /kill /enable /summarize /diag — also in Telegram's
"/" menu.

## Environment (Render → Environment)

| Variable | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `OWNER_CHAT_ID` | your numeric id (@userinfobot) — the only obeyed user |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | primary provider (OpenRouter) |
| `LLM_FALLBACK_MODELS` | e.g. `gemini:gemini-3.6-flash,minimax/minimax-m3:free,openrouter/free` |
| `GROQ_API_KEY` | console.groq.com — 1,000 req/day + free Whisper voice |
| `GEMINI_API_KEY` | aistudio.google.com — ~1,500 req/day + vision |
| `GIST_TOKEN` | GitHub token, gists read/write — persistence across restarts |
| `BRIEFING_TOPIC` | topic for the built-in 9am briefing |
| `SMTP_USER` / `SMTP_PASS` | Gmail + app password — email sending |
| `VISION_MODEL` | optional override (default: gemini flash, else inkling) |

## Budgets (honest)

Free-tier request caps, counted per provider: OpenRouter 50/day,
Groq 1,000/day, Gemini ~1,500/day — plus 2,000 voice transcriptions/day
on Groq. Every plain message costs 1 request (the router); scheduled
tasks with free data sources (prices, weather, prayer, page/RSS/YouTube
watches) cost 0. `/quota` shows the live numbers.

## Built-in tasks

| Task | Schedule (Dhaka) | Cost |
|---|---|---|
| daily_briefing | 09:00 | 1 req |
| daily_report | 21:00 | 0 |
| prayer_alert | every minute (Dhaka, aladhan) | 0 |
| heartbeat | hourly | 0 |
| selftest | manual: /run selftest | 0 |

Built-ins can be paused ("stop the prayer reminders" → `enable` to
resume); tasks you create by message are stopped permanently by `/kill`.

## Safety model

- Owner-locked: only `OWNER_CHAT_ID` is obeyed
- Skill forge code runs in a guarded subprocess: clean environment (no
  secrets), 90s timeout, blocked dangerous imports, no direct Telegram
  access — and must pass a test run before it's ever saved
- Memories are plain text in a secret gist: fine for low-stakes facts,
  **not a password vault**

## Deploy

1. Push this folder to a private GitHub repo
2. render.com → New → Web Service → connect the repo → **Free** plan
   (build `pip install -r requirements.txt`, start
   `gunicorn app:app --timeout 120`, health check `/health`)
3. Add the environment variables above
4. UptimeRobot → HTTP monitor every 5 min on
   `https://<service>.onrender.com/health`
5. Message the bot `/status`
