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

import forge
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

def _briefing_facts(topic):
    """Concrete data for common briefing needs — fetched from free, no-key
    sources. Much more reliable than web search for prices/weather/rates."""
    facts = []
    t = str(topic).lower()
    try:
        if "weather" in t or "dhaka" in t:
            facts.append("Weather in Dhaka now: " + _fetch_weather("Dhaka"))
    except Exception:
        pass
    for coin in ("bitcoin", "ethereum", "solana", "dogecoin"):
        if coin in t:
            try:
                facts.append(f"{coin}: ${_fetch_price(coin, 'usd'):,.0f}")
            except Exception:
                pass
    if "usd" in t or "bdt" in t or "exchange" in t or "rate" in t:
        try:
            import requests as rq

            j = rq.get("https://open.er-api.com/v6/latest/USD", timeout=15).json()
            facts.append(f"USD/BDT: {j['rates']['BDT']}")
        except Exception:
            pass
    return facts


def _daily_briefing(ctx):
    import re as _re

    topic = ctx["TOPIC"] or "AI and tech news"
    ctx["log"](f"briefing: {topic}")

    # compound topics ("(1) weather, (2) crypto, (3) …") get one search
    # PER topic — one giant query returns junk
    parts = [p.strip(" .:-") for p in _re.split(r"[;,]|\(\d+\)|\d+\.", topic)
             if len(p.strip(" .:-")) > 3]
    queries = parts if 1 < len(parts) <= 6 else [topic]

    facts = _briefing_facts(topic)

    results = []
    for q in queries[:6]:
        try:
            results += ctx["web_search"](f"{q} latest news today", max_results=3)
        except Exception:
            pass

    context = ""
    if facts:
        context += "Live data (authoritative — use these numbers):\n" \
                   + "\n".join(facts) + "\n\n"
    if results:
        context += "Web results:\n" + "\n\n".join(
            f"[{i+1}] {r['title']}\n{r['body']}" for i, r in enumerate(results[:12])
        )
    if not context:
        ctx["tg_send"](f"☀️ Briefing — {topic}\n"
                       f"(nothing usable came back today — retrying next time)")
        return

    summary = ctx["llm"](
        [
            {"role": "system", "content": "Summarize today's briefing in 5-8 "
             "short bullet points. Use the live-data numbers as-is. Only "
             "include what the data/results actually say — if a topic has "
             "no data, skip it silently instead of announcing it's missing."},
            {"role": "user", "content": f"Topic: {topic}\n\n{context[:12000]}"},
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
# More free task types (no LLM, no API keys)
# ---------------------------------------------------------------------------

def _price_report(ctx):
    coin = ctx["COIN"]
    vs = ctx.get("VS", "usd")
    price = _fetch_price(coin, vs, ctx.get("SYMBOL"))
    ctx["tg_send"](f"💰 {coin} is now {price:,.2f} {vs.upper()}")


def _fetch_weather(location):
    """Current conditions from wttr.in — free, no key."""
    import requests as rq

    r = rq.get(
        f"https://wttr.in/{location}",
        params={"format": "%c %t (feels %f), humidity %h, wind %w"},
        headers={"User-Agent": "curl/8"},
        timeout=20,
    )
    r.raise_for_status()
    return r.text.strip()


def _weather_report(ctx):
    loc = ctx["LOCATION"]
    ctx["tg_send"](f"🌦 Weather — {loc.title()}\n{_fetch_weather(loc)}")


def _weather_once(ctx):
    """One-shot weather report: runs, then removes itself forever."""
    try:
        _weather_report(ctx)
    finally:
        name = ctx.get("name")
        if name and name in TASKS:
            TASKS.pop(name)
            state.STATE["dynamic_tasks"] = [
                s for s in state.STATE.get("dynamic_tasks", [])
                if dyn_task_name(s) != name
            ]
            state.save_soon()


def _yt_resolve(url_or_handle):
    """@handle or youtube.com/@handle → (channel_id, handle)."""
    import re as _re
    import requests as rq

    s = str(url_or_handle or "").strip()
    m = _re.search(r"youtube\.com/@([\w.-]+)", s)
    handle = m.group(1) if m else s.lstrip("@").strip()
    if handle.startswith("UC") and len(handle) == 24:
        return handle, handle
    r = rq.get(
        f"https://www.youtube.com/@{handle}",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    m = _re.search(r'"(UC[\w-]{22})"', r.text)
    if not m:
        raise ValueError(f"couldn't find a channel id for @{handle}")
    return m.group(1), handle


def _watch_youtube(ctx):
    """New-video alert via the channel's public RSS feed — free, no key.
    YouTube's feed throws transient 500/404s at datacenter IPs, so one
    retry before giving up."""
    import re as _re
    import time as _time
    import requests as rq

    chan = ctx["CHANNEL_ID"]
    last_exc = None
    for attempt in (1, 2):
        try:
            r = rq.get(
                "https://www.youtube.com/feeds/videos.xml",
                params={"channel_id": chan},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            r.raise_for_status()
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            _time.sleep(4)
    if last_exc is not None:
        raise last_exc
    entries = _re.findall(
        r"<entry>.*?<yt:videoId>([\w-]{11})</yt:videoId>.*?<title>([^<]+)</title>",
        r.text, _re.S,
    )
    if not entries:
        ctx["log"](f"yt feed empty for {chan}")
        return
    latest_id, latest_title = entries[0]

    yt = state.STATE.setdefault("youtube", {})
    prev = yt.get(chan)
    yt[chan] = latest_id
    state.save_soon()

    if prev and prev != latest_id:
        ctx["tg_send"](
            f"📺 New video: {latest_title}\nhttps://youtu.be/{latest_id}"
        )
    elif prev is None:
        ctx["log"](f"yt baseline set for {chan}")


# ---------------------------------------------------------------------------
# Skills — tasks whose code was WRITTEN BY THE LLM (see forge.py).
# A skill runs in a guarded sandbox and only returns a message string;
# it can't touch Telegram, secrets, or files directly.
# ---------------------------------------------------------------------------

def _run_skill_task(ctx):
    name = ctx.get("name") or ""
    memory = state.STATE.get("skill_memory", {}).get(name, {})
    ok, out = forge.run_skill(
        ctx["CODE"], ctx.get("PARAMS") or {}, memory=memory, timeout=90
    )
    if ok:
        # persist whatever the skill left in memory (its only state)
        state.STATE.setdefault("skill_memory", {})[name] = out.get("memory") or {}
        state.save_soon()
        if out.get("message"):
            ctx["tg_send"](out["message"])
        else:
            ctx["log"](f"skill silent: {out.get('skip', '')}")
    else:
        ctx["tg_send"](
            f"⚠️ Skill failed: {out.get('error', '')[:300]}\n"
            f"(stop it with /kill {name or '?'})"
        )


def _skill_schedule(params):
    sched = params.get("schedule") or {}
    if sched.get("daily"):
        h = int(sched["daily"].get("hour", 8))
        m = int(sched["daily"].get("minute", 0))
        return _daily(h, m)
    every = max(1, int(sched.get("every_minutes") or 60))
    return _every(every / 60)


# ---------------------------------------------------------------------------
# Dynamic tasks — created at runtime from parsed user messages.
# The LLM may only choose from these templates and fill in parameters;
# it can never inject code — EXCEPT via "skill", whose code is written
# by the LLM but runs guarded in a sandbox (see forge.py).
# ---------------------------------------------------------------------------

def _stable_id(s):
    """Hash that's stable across restarts (Python's hash() isn't)."""
    import hashlib

    return int(hashlib.md5(str(s).encode()).hexdigest()[:8], 16) % 100000


def dyn_task_name(spec):
    """The deterministic name build_dynamic_task would give this spec,
    WITHOUT registering anything — safe for lookups/comparisons."""
    ttype = (spec or {}).get("type", "")
    params = spec.get("params") or {}
    if ttype == "price_alert":
        return f"alert_{str(params.get('coin') or 'bitcoin').lower().strip()}"
    if ttype == "price_report":
        return f"report_{str(params.get('coin') or 'bitcoin').lower().strip()}"
    if ttype == "briefing":
        hour = int(params.get("hour") if params.get("hour") is not None else 9)
        minute = int(params.get("minute") or 0)
        return f"briefing_{hour:02d}{minute:02d}"
    if ttype == "weather_once":
        return "weather_once_" + str(
            _stable_id((params.get("when_spec"), params.get("location")))
        )
    if ttype == "weather_daily":
        hour = int(params.get("hour") if params.get("hour") is not None else 8)
        minute = int(params.get("minute") or 0)
        return f"weather_{hour:02d}{minute:02d}"
    if ttype == "watch_page":
        return f"watch_{_stable_id(params.get('url'))}"
    if ttype == "watch_youtube":
        return f"yt_{_stable_id(params.get('url_or_handle'))}"
    if ttype == "skill":
        return f"skill_{_stable_id(params.get('goal'))}"
    return None


def build_dynamic_task(spec):
    """spec example: {"type":"price_alert","params":{"coin":"bitcoin",
    "below":60000,"every_minutes":30}} → registers into TASKS, returns name.
    May add derived keys (e.g. due_iso) into spec["params"] so a restart
    can rebuild the exact same task."""
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

    if ttype == "price_report":
        coin = str(params.get("coin") or "bitcoin").lower().strip()
        every = max(1, int(params.get("every_minutes") or 60))
        base = f"report_{coin}"
        TASKS[base] = {
            "desc": f"Report {coin} price every {every}m (free)",
            "schedule": _every(every / 60),
            "run": _price_report,
            "ctx": {"COIN": coin},
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

    if ttype == "weather_once":
        # deferred import: app imports tasks, so importing app here at
        # module level would be circular
        from app import _parse_reminder

        loc = str(params.get("location") or "Dhaka").strip()
        when = str(params.get("when_spec") or "").strip() or "in 1h"
        due = None
        if params.get("due_iso"):  # restored after a restart — original time
            try:
                from datetime import datetime as _dt

                due = _dt.fromisoformat(params["due_iso"])
            except Exception:
                due = None
        if due is None:
            parsed = _parse_reminder(when)
            if not parsed:
                raise ValueError(f"couldn't understand when '{when}'")
            due = parsed[0]
            params["due_iso"] = due.isoformat()  # persist for restarts
        base = dyn_task_name({"type": ttype, "params": params})

        def _once_sched(now, _due=due):
            return now >= _due

        TASKS[base] = {
            "desc": f"One-time weather for {loc} at {due:%H:%M} Dhaka",
            "schedule": _once_sched,
            "run": _weather_once,
            "ctx": {"LOCATION": loc},
        }
        return base

    if ttype == "weather_daily":
        loc = str(params.get("location") or "Dhaka").strip()
        hour = int(params.get("hour") if params.get("hour") is not None else 8)
        minute = int(params.get("minute") or 0)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("hour/minute out of range")
        base = f"weather_{hour:02d}{minute:02d}"
        TASKS[base] = {
            "desc": f"Daily weather for {loc} at {hour:02d}:{minute:02d} Dhaka",
            "schedule": _daily(hour, minute),
            "run": _weather_report,
            "ctx": {"LOCATION": loc},
        }
        return base

    if ttype == "watch_page":
        url = str(params.get("url") or "").strip()
        every = max(5, int(params.get("every_minutes") or 60))
        if not url.startswith("http"):
            raise ValueError("url must start with http")
        base = f"watch_{_stable_id(url)}"
        TASKS[base] = {
            "desc": f"Watch {url} every {every}m, ping on change",
            "schedule": _every(every / 60),
            "run": _watch_page,
            "ctx": {"URL": url},
        }
        return base

    if ttype == "watch_youtube":
        chan, handle = _yt_resolve(params.get("url_or_handle"))
        every = max(5, int(params.get("every_minutes") or 15))
        base = f"yt_{_stable_id(params.get('url_or_handle'))}"
        TASKS[base] = {
            "desc": f"Watch YouTube @{handle} for new videos every {every}m",
            "schedule": _every(every / 60),
            "run": _watch_youtube,
            "ctx": {"CHANNEL_ID": chan},
        }
        return base

    if ttype == "skill":
        goal = str(params.get("goal") or "custom skill").strip()
        code = str(params.get("code") or "")
        if "def task(" not in code:
            raise ValueError("skill code must define task(ctx)")
        forge._guard(code)  # reject dangerous patterns at registration too
        base = f"skill_{_stable_id(goal)}"
        TASKS[base] = {
            "desc": f"Skill: {goal[:70]}",
            "schedule": _skill_schedule(params),
            "run": _run_skill_task,
            "ctx": {"CODE": code, "PARAMS": params.get("user_params") or {}},
        }
        return base

    raise ValueError(
        f"unknown task type '{ttype}' (use price_alert, price_report, "
        "briefing, weather_once, weather_daily, watch_page, watch_youtube, "
        "skill)"
    )


# ---------------------------------------------------------------------------
# Task: heartbeat — the smoke test. Proves scheduler → task → Telegram all
# work, using zero LLM requests. "I'm alive" + live Dhaka clock + uptime.
# ---------------------------------------------------------------------------


def _heartbeat(ctx):
    import requests as rq

    try:
        price = _fetch_price("bitcoin", "usd")
        extra = f" | BTC ${price:,.0f}"
    except Exception:
        extra = " | (price sources all busy — still fine)"

    ctx["tg_send"](
        f"💓 Hermes is alive — {ctx['now']:%H:%M:%S} Dhaka"
        f"{extra}"
        f"\n(i.e. the scheduler, tasks, and Telegram all work)"
    )


# ---------------------------------------------------------------------------
# Task: selftest — THE quick verification task. Run /run selftest and get a
# reply in seconds. Checks every subsystem the bot depends on and reports
# pass/fail per item. Zero LLM requests.
# ---------------------------------------------------------------------------


def _selftest(ctx):
    import requests as rq

    checks = []

    # 1. Telegram send (if you got this message, it obviously works)
    checks.append(("telegram send", True))

    # 2. Web search (free, no key)
    try:
        from ddgs import DDGS
        with DDGS() as d:
            list(d.text("test", max_results=1))
        checks.append(("web search", True))
    except Exception as e:
        checks.append((f"web search ({str(e)[:40]})", False))

    # 3. Free price sources
    try:
        p = _fetch_price("bitcoin", "usd")
        checks.append((f"price feed (BTC ${p:,.0f})", True))
    except Exception as e:
        checks.append((f"price feed ({str(e)[:40]})", False))

    # 4. Outbound internet (a plain fetch)
    try:
        rq.get("https://api.github.com", timeout=10)
        checks.append(("internet access", True))
    except Exception as e:
        checks.append((f"internet ({str(e)[:40]})", False))

    # 5. Persistent state (gist)
    checks.append((
        "state " + ("gist-backed" if state.GIST_TOKEN else "memory-only"),
        bool(state.GIST_TOKEN),
    ))

    # 6. Clock
    checks.append((f"Dhaka time {ctx['now']:%H:%M:%S}", True))

    ok = sum(1 for _, p in checks if p)
    lines = [f"{'✅' if p else '❌'} {name}" for name, p in checks]
    ctx["tg_send"](
        f"🧪 Self-test: {ok}/{len(checks)} pass\n" + "\n".join(lines)
        + "\n(LLM not tested here — use /say hi or /diag for that)"
    )


# ---------------------------------------------------------------------------
# Daily self-report — the agent accounts for its own day. Free, no LLM.
# ---------------------------------------------------------------------------

def _daily_report(ctx):
    from collections import Counter

    today = f"{ctx['now']:%Y-%m-%d}"
    runs = [r for r in state.STATE.get("runs", [])
            if str(r.get("at", "")).startswith(today)]

    lines = [f"📊 Daily report — {today}"]
    if runs:
        counts = Counter(r["task"] for r in runs)
        fails = [r for r in runs if not r.get("ok")]
        lines.append(f"Runs today: {len(runs)} across {len(counts)} tasks")
        for name, n in counts.most_common(12):
            lines.append(f"• {name} ×{n}")
        if fails:
            lines.append(f"❌ Failures: {len(fails)}")
            for r in fails[:4]:
                lines.append(f"  {r['task']}: {str(r.get('err', ''))[:80]}")
        else:
            lines.append("✅ No failures")
    else:
        lines.append("No scheduled runs today (only manual /run's?)")

    quota = state.STATE.get("quota", {})
    lines.append(f"LLM requests: {quota.get('used', 0)}/50 today")

    try:  # app imports tasks, so import app lazily here
        from app import REMINDERS

        if REMINDERS:
            lines.append("Pending reminders: " + "; ".join(
                f"{r['text'][:30]} ({r['due']:%H:%M}) " for r in REMINDERS[:5]
            ))
    except Exception:
        pass

    lines.append("Active automations: /tasks • stop one: 'stop <name>'")
    ctx["tg_send"]("\n".join(lines))


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
    "heartbeat": {
        "desc": "Hourly proof-of-life message with Dhaka time (free, no LLM)",
        "schedule": _every(1),
        "run": _heartbeat,
        "ctx": {},
    },
    "selftest": {
        "desc": "Check every subsystem in seconds (free, no LLM) — /run selftest",
        "schedule": _never,
        "run": _selftest,
        "ctx": {},
    },
    "daily_report": {
        "desc": "Evening summary of everything the agent did today (free)",
        "schedule": _daily(21, 0),  # 9pm Dhaka
        "run": _daily_report,
        "ctx": {},
    },
    "remind_me": {
        "desc": "A reminder template",
        "schedule": _never,
        "run": _remind,
        "ctx": {"MESSAGE": "write your reminder text here"},
    },
}
