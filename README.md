# Channel Agent — 24/7 YouTube growth manager

The brain half of a two-part system:

- **This repo** (deployed to Render's free tier): Telegram console, channel
  analytics watcher, growth brain (Gemini with Groq/OpenRouter fallback),
  and the job queue that hands render/upload work to the home PC.
- **The PC worker** (runs on the creator's computer): renders the videos
  and uploads them.

## Deploy

1. Push this repo to GitHub
2. Render → New Web Service → connect repo (render.yaml pre-fills settings,
   plan must be **Free**)
3. Set environment variables:

| Variable | Required | What it is |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | token from @BotFather |
| `OWNER_CHAT_ID` | ✅ | owner's Telegram chat id |
| `GEMINI_API_KEY` | ✅ | free key from aistudio.google.com |
| `YT_REFRESH_TOKEN` | ✅ | from the PC-side extract_refresh_token.py |
| `YT_CLIENT_ID` | ✅ | same script prints it |
| `YT_CLIENT_SECRET` | ✅ | same script prints it |
| `WORKER_SECRET` | ✅ | any long random string — shared with the PC worker |
| `GIST_TOKEN` | ✅ | GitHub token with gist permission (persistent state) |
| `GROQ_API_KEY` | optional | free fallback LLM (console.groq.com) |
| `OPENROUTER_API_KEY` | optional | second fallback LLM (openrouter.ai) |

4. After deploy the bot messages its owner — reply `/status` to confirm.

## What it does on schedule (owner's timezone)

| Time | Job |
|---|---|
| every 4h | comment sweep → drafts replies → owner approves via ✅ buttons |
| 08:00 | daily growth report |
| 08:30 | analyze winners/losers → write next script → queue render |
| 12:00 | underperformer check → better title suggestions |
| Sunday 09:00 | weekly strategy summary |

The home PC polls `GET /next-job` every 60s (this keeps the free dyno
awake) and reports results to `POST /report`. Both require the
`X-Worker-Secret` header.

No fake-growth tactics exist in this codebase — no sub4sub, comment spam,
or view automation. Channels grow from good topics, good titles, and
consistent posting.
