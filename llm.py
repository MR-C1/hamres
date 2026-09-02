"""LLM layer — one entry point, complete(), with a multi-provider
fallback chain (OpenRouter / Groq / Gemini) and per-provider daily quota.

All providers speak the OpenAI chat-completions format, so there is a
single code path. Raw requests (no SDK) so failures surface the
provider's own error text.
"""

import os
from datetime import datetime

import requests

import comms
import config
import state

DEFAULT_LIMIT = 50  # OpenRouter free tier


def _resolve(model):
    """'groq:x' | 'gemini:x' | bare → (provider, base, key, bare_model)."""
    if ":" in model:
        p, m = model.split(":", 1)
        if p in config.PROVIDERS:
            return p, config.PROVIDERS[p]["base"], config.PROVIDERS[p]["key"], m
    return "default", config.LLM_BASE_URL, config.LLM_API_KEY, model


def _url(base):
    base = base.rstrip("/")
    if base.endswith("/v1") or "/openai" in base:
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _chat(messages, max_tokens, model, base, key):
    resp = requests.post(
        _url(base),
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens},
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {base}: "
                           f"{resp.text[:400]}")
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"provider error: {str(data['error'])[:400]}")
    try:
        # content can be None (tool-call-only or empty responses)
        content = data["choices"][0]["message"]["content"]
        if not content or not content.strip():
            raise ValueError("empty content")
        return content.strip()
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError(f"unexpected response: {str(data)[:400]}")


def model_chain():
    """Primary model first, then fallbacks — deduplicated, and entries
    whose provider has no key configured are skipped."""
    seen, out = set(), []
    for m in [config.LLM_MODEL] + config.LLM_FALLBACK_MODELS:
        if not m or m in seen:
            continue
        seen.add(m)
        provider, _, key, _ = _resolve(m)
        if provider != "default" and not key:
            continue
        out.append(m)
    return out


# --- quota --------------------------------------------------------------------

def _today():
    return datetime.now().strftime("%Y-%m-%d")  # UTC day, matching OR's reset


def quota_state():
    q = state.STATE.setdefault("quota", {"day": _today(), "used": {}})
    if q["day"] != _today():
        q["day"] = _today()
        q["used"] = {}
    if isinstance(q["used"], int):  # migrate the old single-counter format
        q["used"] = {"default": q["used"]}
    return q


def count_request(provider="default"):
    q = quota_state()
    q["used"][provider] = q["used"].get(provider, 0) + 1
    state.save_soon()
    return q["used"][provider]


# --- the one entry point --------------------------------------------------------

LAST_MODEL = None  # which model answered the last complete() call


def complete(messages, max_tokens=800, skip=()):
    """Send a chat completion through the provider chain. `skip` names
    models to bypass (used by junk-reply retries to force a different
    model). Raises only if every model in the chain fails."""
    global LAST_MODEL
    errors = []
    for model in model_chain():
        if model in skip:
            continue
        provider, base, key, bare = _resolve(model)
        if provider != "default" and not key:
            continue
        try:
            text = _chat(messages, max_tokens, bare, base, key)
            count_request(provider)
            LAST_MODEL = model
            if model != config.LLM_MODEL:
                comms.log(f"fallback used: {model} ({provider})")
            return text
        except Exception as e:
            errors.append(f"{model} [{provider}]: {str(e)[:200]}")
    raise RuntimeError("all models failed — run /diag:\n"
                       + "\n".join(errors[:4]))


def diagnose():
    """Walk the whole chain; report per-model health with real errors."""
    lines = [
        f"default: {config.LLM_BASE_URL} "
        f"({'key set' if config.LLM_API_KEY else 'NO KEY'})",
        "providers: " + ", ".join(
            f"{p} {'✅' if d['key'] else '— no key'}"
            for p, d in config.PROVIDERS.items()),
        f"chain: {' → '.join(model_chain()) or '(empty)'}\n",
    ]
    test = [{"role": "user", "content": "Reply with the single word: ok"}]
    worked = []
    for model in model_chain():
        provider, base, key, bare = _resolve(model)
        try:
            text = _chat(test, 20, bare, base, key)
            lines.append(f"✅ {model} — replied: {text[:60]!r}")
            worked.append(model)
        except Exception as e:
            lines.append(f"❌ {model} — {str(e)[:300]}\n")
    lines.append(f"\n{len(worked)}/{len(model_chain())} models healthy."
                 if worked else "No model answered — check keys.")
    return "\n".join(lines)
