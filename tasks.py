"""
Hermes tasks — each task is one automation. Add yours here.

SCHEDULES ARE IN DHAKA TIME (app.py shifts Render's UTC clock for you):
  _daily(hour, minute)  — once a day, e.g. _daily(9, 0) = 9:00am Dhaka
  _every(hours)         — every N hours, e.g. _every(0.5) = every 30 min
  _never                — manual only, run with /run <name>

A task is a dict:
  desc      — one line, shown by /tasks
  schedule  — function taking datetime now, True if the task should run now
  run       — the function that does the work. It gets a ctx with:
                llm(messages)      — your LLM (costs 1 of ~50 free req/day)
                web_search(query)  — free web search (costs nothing)
                tg_send(text)      — message YOU on Telegram
                log(event)         — add to /status log

Budget rule: a task that calls llm() should not run more than ~24x/day
(the free-tier cap is ~50 requests/day across everything). web_search()
and plain requests.get() are free — run those as often as you like.
"""

import os
from datetime import datetime

import state


def _never(now):
    return False


def _daily(hour, minute=0):
    def sched(now):
        return now.hour == hour and now.minute == minute
    return sched


def _every(hours):
    """Due at :00 and every N hours after it — _every(0.5) = every 30 min."""
    period = max(1, int(round(hours * 60)))  # in minutes

    def sched(now):
        return (now.hour * 60 + now.minute) % period == 0
    return sched


# ---------------------------------------------------------------------------
# Task 1: daily research briefing (uses 1 LLM request per day)
# ---------------------------------------------------------------------------

def _daily_briefing(ctx):
    topic = ctx["TOPIC"] or "AI and tech news"
    ctx["log"](f"briefing: {topic}")
    results = ctx["web_search"](f"{topic} latest news today", max_results=8)
    context = "\n\n".join(f"[{i+1}] {r['title']}\n{r['body']}" for i, r in enumerate(results))
    summary = ctx["llm"](
        [
            {"role": "system", "content": "Summarize today's updates in 5-8 short "
             "bullet points. Only include what's actually in the results."},
            {"role": "user", "content": f"Topic: {topic}\n\n{context}"},
        ]
    )
    ctx["tg_send"](f"☀️ Daily briefing — {topic}\n\n{summary}")


# ---------------------------------------------------------------------------
# Task 2: crypto price watcher (FREE — no LLM, no API keys)
# Price comes from whichever source answers: CoinGecko → Binance → Coinbase.
# (CoinGecko throttles cloud IPs like Render's hard, hence the fallbacks.)
# Alert fires only when the price CROSSES your threshold, not every check.
# Note: fallback sources quote USD — keep vs="usd".
# ---------------------------------------------------------------------------

_BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
    "ripple": "XRPUSDT", "dogecoin": "DOGEUSDT", "binancecoin": "BNBUSDT",
    "cardano": "ADAUSDT", "tron": "TRXUSDT", "litecoin": "LTCUSDT",
    "polkadot": "DOTUSDT", "chainlink": "LINKUSDT", "avalanche-2": "AVAXUSDT",
}


def _fetch_price(coin, vs, symbol=None):
    """Price from the first free source that answers, or raises."""
    import requests as rq

    try:
        r = rq.get("https://api.coingecko.com/api/v3/simple/price",
                   params={"ids": coin, "vs_currencies": vs}, timeout=15)
        r.raise_for_status()
        return float(r.json()[coin][vs])
    except Exception:
        pass  # throttled or unknown coin — try Binance

    bin_sym = symbol or _BINANCE_SYMBOLS.get(coin)
    if bin_sym:
        try:
            r = rq.get("https://api.binance.com/api/v3/ticker/price",
                       params={"symbol": bin_sym}, timeout=15)
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            pass

    cb_sym = (bin_sym or "").replace("USDT", "-USD")  # BTCUSDT → BTC-USD
    if cb_sym.endswith("-USD"):
        try:
            r = rq.get(f"https://api.coinbase.com/v2/prices/{cb_sym}/spot",
                       timeout=15)
            r.raise_for_status()
            return float(r.json()["data"]["amount"])
        except Exception:
            pass

    raise RuntimeError(f"no free price source answered for '{coin}' — "
                       f"try again in a few minutes")


def _price_watch(ctx):
    coin = ctx["COIN"]          # CoinGecko id, e.g. "bitcoin", "ethereum"
    vs = ctx.get("VS", "usd")
    below = ctx.get("BELOW")    # alert when price drops below this
    above = ctx.get("ABOVE")    # alert when price rises above this

    price = _fetch_price(coin, vs, ctx.get("SYMBOL"))

    prices = state.STATE.setdefault("prices", {})
    prev = prices.get(coin)
    prices[coin] = price
    state.save_soon()  # baseline survives restarts via the gist

    if prev is None:  # first check just records the price
        ctx["log"](f"{coin} = {price} {vs.upper()} (recorded, watching)")
        return

    if below and prev >= below > price:
        ctx["tg_send"](f"📉 {coin} dropped below {below}! Now {price} {vs.upper()} (was {prev})")
    elif above and prev <= above < price:
        ctx["tg_send"](f"📈 {coin} rose above {above}! Now {price} {vs.upper()} (was {prev})")
    else:
        ctx["log"](f"{coin} = {price} {vs.upper()}")


# ---------------------------------------------------------------------------
# Task 3: watch a web page for changes
# ---------------------------------------------------------------------------

def _watch_page(ctx):
    import hashlib

    url = ctx["URL"]
    ctx["log"](f"watching: {url}")
    import requests

    body = requests.get(url, timeout=15).text
    digest = hashlib.md5(body.encode()).hexdigest()

    # hashes live in the gist-backed state, so a restart doesn't forget
    # what the page looked like (no false "changed!" alert)
    hashes = state.STATE.setdefault("page_hashes", {})
    old = hashes.get(url)
    hashes[url] = digest
    state.save_soon()

    if old and old != digest:
        ctx["tg_send"](f"👁 Page changed: {url}")


# ---------------------------------------------------------------------------
# Task 4: reminder template
# ---------------------------------------------------------------------------

def _remind(ctx):
    ctx["tg_send"](f"⏰ Reminder: {ctx['MESSAGE']}")


# ---------------------------------------------------------------------------
# Dynamic tasks — created at runtime from parsed user messages.
# The LLM may only choose from these templates and fill in parameters;
# it can never inject code. Tasks appear in /tasks and /run immediately.
# NOTE: in-memory only — a restart/deploy resets them (code tasks survive).
# ---------------------------------------------------------------------------

def dyn_task_name(spec):
    """The deterministic name build_dynamic_task would give this spec,
    WITHOUT registering anything — safe for lookups/comparisons."""
    ttype = (spec or {}).get("type", "")
    params = spec.get("params") or {}
    if ttype == "price_alert":
        return f"alert_{str(params.get('coin') or 'bitcoin').lower().strip()}"
    if ttype == "briefing":
        hour = int(params.get("hour") if params.get("hour") is not None else 9)
        minute = int(params.get("minute") or 0)
        return f"briefing_{hour:02d}{minute:02d}"
    if ttype == "watch_page":
        return f"watch_{abs(hash(str(params.get('url')))) % 10000}"
    return None


def build_dynamic_task(spec):
    """spec example: {"type":"price_alert","params":{"coin":"bitcoin",
    "below":60000,"every_minutes":30}} → registers into TASKS, returns name."""
    ttype = (spec or {}).get("type", "")
    params = spec.get("params") or {}

    if ttype == "price_alert":
        coin = str(params.get("coin") or "bitcoin").lower().strip()
        below = params.get("below")
        above = params.get("above")
        every = max(5, int(params.get("every_minutes") or 30))
        if below is None and above is None:
            raise ValueError("need 'below' or 'above' (USD)")
        base = f"alert_{coin}"
        TASKS[base] = {
            "desc": f"Alert when {coin} crosses "
                    + (f"< ${below}" if below else f"> ${above}"),
            "schedule": _every(every / 60),
            "run": _price_watch,
            "ctx": {"COIN": coin, "BELOW": below, "ABOVE": above},
        }
        return base

    if ttype == "briefing":
        topic = str(params.get("topic") or "today's top news").strip()
        hour = int(params.get("hour") if params.get("hour") is not None else 9)
        minute = int(params.get("minute") or 0)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("hour/minute out of range")
        base = f"briefing_{hour:02d}{minute:02d}"
        TASKS[base] = {
            "desc": f"Daily briefing on '{topic}' at {hour:02d}:{minute:02d} Dhaka",
            "schedule": _daily(hour, minute),
            "run": _daily_briefing,
            "ctx": {"TOPIC": topic},
        }
        return base

    if ttype == "watch_page":
        url = str(params.get("url") or "").strip()
        every = max(5, int(params.get("every_minutes") or 60))
        if not url.startswith("http"):
            raise ValueError("url must start with http")
        base = f"watch_{abs(hash(url)) % 10000}"
        TASKS[base] = {
            "desc": f"Watch {url} every {every}m, ping on change",
            "schedule": _every(every / 60),
            "run": _watch_page,
            "ctx": {"URL": url},
        }
        return base

    raise ValueError(f"unknown task type '{ttype}' (use price_alert, "
                     "briefing, or watch_page)")


TASKS = {
    "daily_briefing": {
        "desc": "Morning research summary on a topic of your choice",
        "schedule": _daily(9, 0),  # 9:00am Dhaka
        "run": _daily_briefing,
        "ctx": {"TOPIC": os.environ.get("BRIEFING_TOPIC", "AI and tech news")},
    },
    "price_watch": {
        "desc": "Alert when a crypto price crosses your threshold (free, no LLM)",
        "schedule": _every(0.5),  # every 30 min
        "run": _price_watch,
        "ctx": {"COIN": "bitcoin", "BELOW": 60000, "ABOVE": 90000},
    },
    "watch_page": {
        "desc": "Watch a web page and message you when it changes",
        "schedule": _never,  # give it a schedule to enable, e.g. _every(1)
        "run": _watch_page,
        "ctx": {"URL": "https://example.com"},
    },
    "remind_me": {
        "desc": "A reminder template",
        "schedule": _never,
        "run": _remind,
        "ctx": {"MESSAGE": "write your reminder text here"},
    },
}
