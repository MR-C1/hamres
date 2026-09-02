"""Central configuration — everything the environment controls.

Secrets live in Render's Environment tab, never in code.
"""

import os
from datetime import timedelta

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")  # only this id is obeyed

BD_OFFSET = timedelta(hours=6)  # Bangladesh is UTC+6 (Render's clock is UTC)

# --- LLM providers ---------------------------------------------------------
LLM_BASE_URL = (os.environ.get("LLM_BASE_URL")
                or "https://openrouter.ai/api/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openrouter/auto")

# Fallback chain entries may name a provider with a prefix:
#   "groq:MODEL" / "gemini:MODEL" / bare (default provider above)
LLM_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("LLM_FALLBACK_MODELS", "").split(",")
    if m.strip()
]

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")      # console.groq.com
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # aistudio.google.com

PROVIDERS = {
    "groq": {
        "base": "https://api.groq.com/openai/v1",
        "key": GROQ_API_KEY,
        "limit": 1000,  # free plan, requests/day
    },
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key": GEMINI_API_KEY,
        "limit": 1500,  # approximate free-tier requests/day
    },
}

VISION_MODEL = os.environ.get("VISION_MODEL", "").strip()
BRIEFING_TOPIC = os.environ.get("BRIEFING_TOPIC", "AI and tech news")

# --- Email (Gmail app password) ---------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
