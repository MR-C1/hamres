"""Central configuration — everything comes from Render env vars."""

import os
from datetime import timedelta

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")  # only this chat is obeyed

# shared secret between this brain and the home-PC worker (any long string)
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")

# Gemini (script writing + growth analysis) — free key from aistudio.google.com
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Optional fallbacks (both free tiers) — used automatically when Gemini is down
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")  # console.groq.com
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")  # openrouter.ai
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

# YouTube: refresh token + client from the one-time extract_refresh_token.py run
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl",
             "https://www.googleapis.com/auth/youtube.readonly"]

# gist-backed state (same pattern as hermes-agent)
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")

# GitHub repo that runs the cloud render worker (agent_repo) — the brain
# pokes it with repository_dispatch so renders start instantly instead of
# waiting for the next cron slot. Needs a PAT with repo access.
GITHUB_REPO = os.environ.get("GITHUB_REPO", "MR-C1/hamres")
GITHUB_DISPATCH_TOKEN = os.environ.get("GITHUB_DISPATCH_TOKEN", "")

BD_OFFSET = timedelta(hours=6)  # Render's clock is UTC; Bangladesh is UTC+6

CHANNEL_NAME = os.environ.get("CHANNEL_NAME", "Mind Unfold")
