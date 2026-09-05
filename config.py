"""Central configuration — everything comes from Render env vars."""

import os
from datetime import timedelta

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")  # only this chat is obeyed

# shared secret between this brain and the home-PC worker (any long string)
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")

# Gemini (script writing + growth analysis) — free keys from
# aistudio.google.com. MULTIPLE KEYS rotate automatically when one hits
# its daily quota (429): set GEMINI_API_KEY as key1,key2,key3 (comma-
# separated) or GEMINI_API_KEY_2 / _3 / _4 as extra env vars. Each
# Google account gets its own free key with its own quota.
_raw_keys = [k.strip() for k in
             os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()]
for _i in (2, 3, 4):
    _extra = os.environ.get(f"GEMINI_API_KEY_{_i}", "").strip()
    if _extra and _extra not in _raw_keys:
        _raw_keys.append(_extra)
GEMINI_API_KEYS = _raw_keys
GEMINI_API_KEY = _raw_keys[0] if _raw_keys else ""  # legacy single-key name

# Optional fallbacks (both free tiers) — used automatically when Gemini is down
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")  # console.groq.com
# gpt-oss-120b: the free-plan workhorse (old Llama chat models
# went enterprise-only on Groq's free tier)
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")  # openrouter.ai
# glm-5.2:free — frontier-class, 256K ctx, 50 req/day free (verified
# Sept 2026; free models rotate — check openrouter.ai/models when stale)
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "z-ai/glm-5.2:free")

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
