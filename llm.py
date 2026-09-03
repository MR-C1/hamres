"""LLM layer — one entry point, complete(), with a multi-provider
fallback chain: Gemini (native) → Groq → OpenRouter (OpenAI-compatible).

Gemini is primary (script quality on free tier). Groq and OpenRouter are
optional safety nets — add their free keys on Render and the chain uses
them automatically whenever Gemini is down or overloaded.
"""

import requests

import comms
import config

GEMINI_MODELS = ["gemini-flash-latest", "gemini-3.1-flash-lite",
                 "gemini-flash-lite-latest"]


def _gemini(prompt, system, max_tokens):
    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    for model in GEMINI_MODELS:
        try:
            r = client.models.generate_content(
                model=model, contents=prompt,
                config={"system_instruction": system} if system else None)
            text = (r.text or "").strip()
            if text:
                return text
        except Exception as e:
            comms.log(f"gemini {model} failed: {str(e)[:80]}")
    raise RuntimeError("all gemini models failed")


def _openai_compatible(base, key, model, prompt, system, max_tokens):
    url = base.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}]
    resp = requests.post(
        f"{url}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages,
              "max_tokens": max_tokens},
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("empty content")
    return content.strip()


def complete(prompt, system=None, max_tokens=2000):
    """Try Gemini, then Groq, then OpenRouter. Raises only if all fail."""
    errors = []

    if config.GEMINI_API_KEY:
        try:
            return _gemini(prompt, system, max_tokens)
        except Exception as e:
            errors.append(f"gemini: {str(e)[:150]}")

    if config.GROQ_API_KEY:
        try:
            text = _openai_compatible(
                "https://api.groq.com/openai", config.GROQ_API_KEY,
                config.GROQ_MODEL, prompt, system, max_tokens)
            comms.log(f"fallback used: groq ({config.GROQ_MODEL})")
            return text
        except Exception as e:
            errors.append(f"groq: {str(e)[:150]}")

    if config.OPENROUTER_API_KEY:
        try:
            text = _openai_compatible(
                "https://openrouter.ai/api", config.OPENROUTER_API_KEY,
                config.OPENROUTER_MODEL, prompt, system, max_tokens)
            comms.log(f"fallback used: openrouter ({config.OPENROUTER_MODEL})")
            return text
        except Exception as e:
            errors.append(f"openrouter: {str(e)[:150]}")

    raise RuntimeError("all providers failed:\n" + "\n".join(errors[:3])
                       if errors else "no LLM keys configured at all")


def diagnose():
    """Health-check every configured provider — used by /diag."""
    lines = []
    test = "Reply with the single word: ok"
    if config.GEMINI_API_KEY:
        try:
            lines.append(f"✅ gemini — replied: {_gemini(test, None, 20)[:40]!r}")
        except Exception as e:
            lines.append(f"❌ gemini — {str(e)[:200]}")
    else:
        lines.append("— gemini: no key")
    for name, base, key, model in [
        ("groq", "https://api.groq.com/openai", config.GROQ_API_KEY,
         config.GROQ_MODEL),
        ("openrouter", "https://openrouter.ai/api", config.OPENROUTER_API_KEY,
         config.OPENROUTER_MODEL),
    ]:
        if not key:
            lines.append(f"— {name}: no key")
            continue
        try:
            text = _openai_compatible(base, key, model, test, None, 20)
            lines.append(f"✅ {name} ({model}) — replied: {text[:40]!r}")
        except Exception as e:
            lines.append(f"❌ {name} — {str(e)[:200]}")
    return "\n".join(lines)
