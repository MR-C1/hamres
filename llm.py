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

search_complete() is the grounded-research entry point: Gemini WITH
google_search, then a keyless DuckDuckGo scrape digested by the normal
chain, then Groq's compound system.
"""

import html
import re
from urllib.parse import parse_qs, quote, unquote, urlparse

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


def _gemini(prompt, system, max_tokens, models=None):
    """models restricts which Gemini models may answer (default: the
    full GEMINI_MODELS list). Long-form script generation passes only
    the big flash model — the lite geminis behind it quietly write
    3-6 scene stubs whenever flash-latest is down (its 503s are
    chronic), so for that work the chain should fall straight through
    to groq's big gpt-oss instead."""
    from google import genai
    last_err = None
    for key in _gemini_keys():
        client = genai.Client(api_key=key)
        for model in (models or GEMINI_MODELS):
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


# ---------------------------------------------------------------------------
# keyless web search — DuckDuckGo's HTML endpoints, parsed with regex
# (no bs4 dependency). This is the research fallback that needs NO api
# key and NO provider quota: gemini free-tier grounding is not included
# for these models (permanent 429s) and groq/compound's search pipeline
# inflates its internal request past the size cap (413 on any prompt
# that actually searches). Both were verified live 2026-09-11.
# ---------------------------------------------------------------------------

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# title anchors: class then href, href then class, and the lite endpoint's
# single-quoted result-link variant
_TITLE_PATTERNS = [
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    r'href="([^"]+)"[^>]*class="result__a"[^>]*>(.*?)</a>',
    r'href="([^"]+)"[^>]*class=[\x27"]result-link[\x27"][^>]*>(.*?)</a>',
]
_SNIPPET_PATTERNS = [
    r'class="result__snippet"[^>]*>(.*?)</a>',
    r'class=[\x27"]result-snippet[\x27"][^>]*>(.*?)</td>',
]


def _unwrap_ddg(href):
    """DDG wraps outbound links as //duckduckgo.com/l/?uddg=<urlencoded>
    — unwrap to the real destination."""
    if "uddg=" in href:
        got = parse_qs(urlparse(href).query).get("uddg", [""])
        if got[0]:
            return unquote(got[0])
    return href


def _strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(
        re.sub(r"<[^>]+>", " ", s))).strip()


def ddg_search(query, n=8):
    """Search DuckDuckGo, keyless. Returns [(title, url, snippet)] or
    raises — callers treat that as 'this fallback unavailable'."""
    last = "no endpoint tried"
    for base in ("https://html.duckduckgo.com/html/?q=",
                 "https://lite.duckduckgo.com/lite/?q="):
        host = base.split("/")[2]
        try:
            r = requests.get(base + quote(query),
                             headers={"User-Agent": _UA,
                                      "Accept-Language": "en-US,en;q=0.8"},
                             timeout=15)
            if r.status_code != 200:
                last = f"{host}: HTTP {r.status_code}"
                continue
            titles = []
            for pat in _TITLE_PATTERNS:
                titles = re.findall(pat, r.text, re.S)
                if titles:
                    break
            snippets = []
            for pat in _SNIPPET_PATTERNS:
                snippets = re.findall(pat, r.text, re.S)
                if snippets:
                    break
            out = []
            for (href, title), snip in zip(
                    titles, snippets + [""] * len(titles)):
                url = _unwrap_ddg(html.unescape(href))
                if not url.startswith("http"):
                    continue
                out.append((_strip_tags(title), url, _strip_tags(snip)))
            if out:
                return out[:n]
            last = f"{host}: 0 results parsed"
        except Exception as e:
            last = f"{host}: {str(e)[:60]}"
    raise RuntimeError(f"duckduckgo search failed: {last}")


_QUERY_INSTR = ("You are choosing a web-search query for a documentary "
                "channel. From the brief below, output ONLY the search "
                "query itself — 5 to 10 words, no quotes, no explanation, "
                "nothing else. Target a fresh topic in the brief's "
                "direction that is NOT among its already-used topics:"
                "\n\n")


def wiki_search(query, n=5):
    """Keyless Wikipedia search + intro extracts. DuckDuckGo blocks
    datacenter IPs (verified live on Render 2026-09-11), but Wikipedia's
    API is built for programmatic use — and it is exactly the source
    this channel cites anyway. Returns [(title, url, extract)] or
    raises."""
    api = "https://en.wikipedia.org/w/api.php"
    r = requests.get(api, params={
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": n, "format": "json"},
        headers={"User-Agent": _UA}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"wikipedia search: HTTP {r.status_code}")
    hits = r.json().get("query", {}).get("search", [])
    if not hits:
        raise RuntimeError("wikipedia: 0 hits")
    titles = [h["title"] for h in hits]
    r2 = requests.get(api, params={
        "action": "query", "prop": "extracts", "exintro": 1,
        "explaintext": 1, "titles": "|".join(titles), "format": "json"},
        headers={"User-Agent": _UA}, timeout=15)
    if r2.status_code != 200:
        raise RuntimeError(f"wikipedia extracts: HTTP {r2.status_code}")
    out = []
    for p in r2.json().get("query", {}).get("pages", {}).values():
        ex = (p.get("extract") or "").strip()
        if not ex:
            continue  # disambiguation stubs etc.
        t = p.get("title", "?")
        out.append((t, "https://en.wikipedia.org/wiki/"
                   + quote(t.replace(" ", "_")), ex))
    if not out:
        raise RuntimeError("wikipedia: no extracts")
    return out[:n]


def _keyless_research(prompt, system, max_tokens):
    """Grounded research with NO api key and NO search quota: turn the
    brief into a query, fetch REAL results (wikipedia's API from
    datacenter IPs, the DDG scrape for the broader web where it isn't
    blocked), then let the normal complete() chain extract the facts.
    Raises if no source answers or the chain is dead."""
    query = complete(_QUERY_INSTR + prompt, None, 60).strip()
    query = query.strip("'\"").splitlines()[0][:120].strip()
    results, src = [], ""
    for fetch, name in ((wiki_search, "wikipedia"),
                        (ddg_search, "duckduckgo")):
        try:
            results = fetch(query)
            if results:
                src = name
                break
        except Exception as e:
            comms.log(f"{name} search failed: {str(e)[:80]}")
    if not results:
        raise RuntimeError("no keyless search source answered")
    block = "\n".join(f"- {t} — {s} ({u})" for t, u, s in results)
    text = complete(
        prompt + "\n\nREAL web-search results for the query "
        f"'{query}' (from {src}). Every fact in 'sources' MUST come "
        "from these results — cite each as 'title — URL'. If they are "
        "too thin for 5-8 facts, pick a topic they support better and "
        "say so in the JSON:\n" + block, system, max_tokens)
    comms.log(f"research fallback used: {src} "
              f"({len(results)} results for '{query}')")
    return text


def search_complete(prompt, system=None, max_tokens=4000):
    """Gemini WITH google_search grounding — the model searches the live
    web and answers with real sources. Used for the research pass before
    scripting: facts arrive grounded instead of from model memory.

    Grounding is not included on gemini's free tier for these models
    (permanent 429s), so the first fallback is KEYLESS: fetch real
    results from wikipedia's API (datacenter-IP-safe) or the DuckDuckGo
    scrape and let the normal complete() chain extract the facts from
    them — no api key, no search quota, works whenever any generation
    provider is alive. Groq's compound system (its own server-side web
    search) stays as the last resort for the day its search pipeline
    stops 413ing. Research never dies just because every Gemini key is
    over quota."""
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

    # keyless fallback: real search results in, plain generation out
    try:
        return _keyless_research(prompt, system, max_tokens)
    except Exception as e:
        comms.log(f"keyless research fallback failed: {str(e)[:80]}")

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


def complete(prompt, system=None, max_tokens=8000, gemini_models=None):
    """Try Gemini, then Groq, then OpenRouter, then Cloudflare. Raises
    only if all fail.
    Default 8000 output tokens: full scripts (10-14 scenes, ~1,800
    words of narration as JSON) need ~3,000 tokens — the old 2,000
    default silently squeezed them down to 4-scene stubs on fallback
    providers.
    gemini_models: restrict which Gemini models may answer (see
    _gemini) — long-form script generation passes only the big flash
    model so a 503 falls to groq's gpt-oss instead of a lite gemini."""
    errors = []

    if config.GEMINI_API_KEY:
        try:
            return _gemini(prompt, system, max_tokens, gemini_models)
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
    # each keyless research source gets its own health line so /diag can
    # show which ones answer from THIS machine (datacenter IPs differ)
    for name, fn in (("duckduckgo", ddg_search),
                     ("wikipedia", wiki_search)):
        try:
            hits = fn("unexplained historical mystery", n=3)
            lines.append(f"✅ {name} search — {len(hits)} results "
                         f"(keyless research fallback)")
        except Exception as e:
            lines.append(f"❌ {name} search — {str(e)[:120]}")
    return "\n".join(lines)
