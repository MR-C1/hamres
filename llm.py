"""LLM layer — one entry point, complete(), with a multi-provider
fallback chain: Gemini (native) → Groq → OpenRouter → Cloudflare
Workers AI (all OpenAI-compatible after Gemini).

Gemini is primary (script quality on free tier). The rest are optional
safety nets — add their free keys on Render and the chain uses them
automatically whenever the provider above is down or over quota.
Cloudflare sits last: its models are the weakest of the four and its
free 10K neurons/day go furthest when it only fires in a true
everything-else-is-down emergency. It needs both CF_API_TOKEN and
CF_ACCOUNT_ID (the endpoint is per-account) — with either missing the
chain skips it.
"""

import requests

import comms
import config

GEMINI_MODELS = ["gemini-flash-latest", "gemini-3.5-flash-lite",
                 "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]

# key rotation: {key: cooldown_until}. A 429 cools that KEY for 30 min
# (covers per-minute limits; per-day quotas revive on window rollover)
import time as _time
_key_cooldown = {}


def _gemini_keys():
    """Usable Gemini keys — live keys first, cooling ones last resort."""
    keys = list(config.GEMINI_API_KEYS)
    now = _time.time()
    live = [k for k in keys if _key_cooldown.get(k, 0) <= now]
    cooling = [k for k in keys if _key_cooldown.get(k, 0) > now]
    return live + cooling


def _gemini(prompt, system, max_tokens):
    from google import genai
    last_err = None
    for key in _gemini_keys():
        client = genai.Client(api_key=key)
        for model in GEMINI_MODELS:
            try:
                cfg = {"max_output_tokens": max_tokens}
                if system:
                    cfg["system_instruction"] = system
                r = client.models.generate_content(
                    model=model, contents=prompt, config=cfg)
                text = (r.text or "").strip()
                if text:
                    return text
            except Exception as e:
                msg = str(e)
                last_err = e
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    # THIS key's quota is hit — cool it and rotate to the
                    # next key (not the next model: same key = same quota)
                    _key_cooldown[key] = _time.time() + 1800
                    comms.log(f"gemini key ...{key[-6:]} quota-hit — "
                              f"rotating to next key")
                    break
                if "401" in msg or "UNAUTHENTICATED" in msg or "403" in msg:
                    # THIS key is invalid/dead (typo, revoked, wrong
                    # project) — trying more models with it is pointless;
                    # rotate to the next key immediately
                    comms.log(f"gemini key ...{key[-6:]} rejected (401/403) "
                              f"— rotating to next key")
                    break
                comms.log(f"gemini {model} failed: {msg[:80]}")
    raise RuntimeError(f"all gemini models failed: {last_err}")


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
    if "choices" not in data:
        # OpenRouter sometimes answers HTTP 200 with an error body
        # ({\"error\": {...}}) — surface it readably instead of a bare
        # KeyError('choices') that tells us nothing
        err = data.get("error")
        msg = ((err.get("message") if isinstance(err, dict) else None)
               or str(err))[:250]
        raise RuntimeError(f"no choices in response: {msg}")
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content.strip():
        # reasoning models (groq's gpt-oss) sometimes put the answer in
        # the reasoning field and leave content empty on tiny prompts
        content = msg.get("reasoning") or ""
    if not content.strip():
        raise RuntimeError("empty content")
    return content.strip()


def search_complete(prompt, system=None, max_tokens=4000):
    """Gemini WITH google_search grounding — the model searches the live
    web and answers with real sources. Used for the research pass before
    scripting: facts arrive grounded instead of from model memory.

    Grounding is the flakiest free-tier Gemini feature (429s constantly),
    so Groq's compound models — which run their own server-side web
    search and answer with citations — are the fallback. Research never
    dies just because every Gemini key is over quota."""
    from google import genai
    last_err = None
    if config.GEMINI_API_KEYS:
        for key in _gemini_keys():
            client = genai.Client(api_key=key)
            for model in GEMINI_MODELS:
                try:
                    cfg = {"max_output_tokens": max_tokens,
                           "tools": [{"google_search": {}}]}
                    if system:
                        cfg["system_instruction"] = system
                    r = client.models.generate_content(model=model,
                                                       contents=prompt, config=cfg)
                    text = (r.text or "").strip()
                    if text:
                        return text
                except Exception as e:
                    msg = str(e)
                    last_err = e
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        _key_cooldown[key] = _time.time() + 1800
                        comms.log(f"gemini-search key ...{key[-6:]} quota-hit — "
                                  f"rotating to next key")
                        break
                    comms.log(f"gemini-search {model} failed: {msg[:80]}")
    if config.GROQ_API_KEY:
        try:
            text = _openai_compatible(
                "https://api.groq.com/openai", config.GROQ_API_KEY,
                config.GROQ_SEARCH_MODEL, prompt, system, max_tokens)
            comms.log(f"search fallback used: groq "
                      f"({config.GROQ_SEARCH_MODEL}, web search)")
            return text
        except Exception as e:
            last_err = e
            comms.log(f"groq-search {config.GROQ_SEARCH_MODEL} failed: "
                      f"{str(e)[:80]}")
    if last_err is None:
        raise RuntimeError("no search provider configured "
                           "(need GEMINI_API_KEY or GROQ_API_KEY)")
    raise RuntimeError(f"search grounding failed: {last_err}")


def _cf_base():
    """Workers AI's OpenAI-compatible base — account-id is part of the
    path, so this is only valid once CF_ACCOUNT_ID is set."""
    return (f"https://api.cloudflare.com/client/v4/accounts/"
            f"{config.CF_ACCOUNT_ID}/ai")


def complete(prompt, system=None, max_tokens=8000):
    """Try Gemini, then Groq, then OpenRouter, then Cloudflare. Raises
    only if all fail.
    Default 8000 output tokens: full scripts (10-14 scenes, ~1,800
    words of narration as JSON) need ~3,000 tokens — the old 2,000
    default silently squeezed them down to 4-scene stubs on fallback
    providers."""
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

    if config.CF_API_TOKEN and config.CF_ACCOUNT_ID:
        try:
            text = _openai_compatible(
                _cf_base(), config.CF_API_TOKEN,
                config.CF_MODEL, prompt, system, max_tokens)
            comms.log(f"fallback used: cloudflare ({config.CF_MODEL})")
            return text
        except Exception as e:
            errors.append(f"cloudflare: {str(e)[:150]}")

    raise RuntimeError("all providers failed:\n" + "\n".join(errors[:3])
                       if errors else "no LLM keys configured at all")


def diagnose():
    """Health-check every configured provider — used by /diag."""
    lines = []
    test = "Reply with the single word: ok"
    if config.GEMINI_API_KEYS:
        now = _time.time()
        live = sum(1 for k in config.GEMINI_API_KEYS
                   if _key_cooldown.get(k, 0) <= now)
        try:
            lines.append(f"✅ gemini — replied: {_gemini(test, None, 20)[:40]!r} "
                         f"[{live}/{len(config.GEMINI_API_KEYS)} keys live]")
        except Exception as e:
            lines.append(f"❌ gemini — {str(e)[:200]} "
                         f"[{live}/{len(config.GEMINI_API_KEYS)} keys live]")
    else:
        lines.append("— gemini: no key")
    for name, base, key, model in [
        ("groq", "https://api.groq.com/openai", config.GROQ_API_KEY,
         config.GROQ_MODEL),
        ("openrouter", "https://openrouter.ai/api", config.OPENROUTER_API_KEY,
         config.OPENROUTER_MODEL),
        ("cloudflare", _cf_base() if config.CF_ACCOUNT_ID else "",
         config.CF_API_TOKEN if config.CF_ACCOUNT_ID else "",
         config.CF_MODEL),
    ]:
        if not key:
            lines.append(f"— {name}: no key"
                         + (" (CF_API_TOKEN set but CF_ACCOUNT_ID missing)"
                            if name == "cloudflare" and config.CF_API_TOKEN
                            else ""))
            continue
        try:
            text = _openai_compatible(base, key, model, test, None, 20)
            lines.append(f"✅ {name} ({model}) — replied: {text[:40]!r}")
        except Exception as e:
            lines.append(f"❌ {name} — {str(e)[:200]}")
    return "\n".join(lines)
