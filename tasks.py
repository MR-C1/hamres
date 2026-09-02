"""Task definitions, the dynamic-task factory, and task management
operations (find / create / stop / resume / reschedule).

Built-in tasks live in TASKS below; anything the owner creates by
message is registered dynamically and rebuilt from gist state on boot.
"""

import hashlib
import re
from datetime import datetime, timedelta

import comms
import config
import forge
import reminders
import state


# ============================================================================
# Schedules (all in Dhaka time)
# ============================================================================

def never(now):
    return False


def daily(hour, minute=0):
    def sched(now):
        return now.hour == hour and now.minute == minute
    return sched


def every(hours):
    """Due at :00 and every N hours after it — every(0.5) = every 30 min."""
    period = max(1, int(round(hours * 60)))  # in minutes

    def sched(now):
        return (now.hour * 60 + now.minute) % period == 0
    return sched


def stable_id(s):
    """Hash that's stable across restarts (Python's hash() isn't)."""
    return int(hashlib.md5(str(s).encode()).hexdigest()[:8], 16) % 100000


# ============================================================================
# Free data fetchers (no LLM, no API keys)
# ============================================================================

_BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
    "ripple": "XRPUSDT", "dogecoin": "DOGEUSDT", "binancecoin": "BNBUSDT",
    "cardano": "ADAUSDT", "tron": "TRXUSDT", "litecoin": "LTCUSDT",
    "polkadot": "DOTUSDT", "chainlink": "LINKUSDT", "avalanche-2": "AVAXUSDT",
}


def fetch_price(coin, vs, symbol=None):
    """Price from the first free source that answers: CoinGecko → Binance
    → Coinbase. Raises if all fail."""
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

    raise RuntimeError(f"no free price source answered for '{coin}'")


def fetch_weather(location):
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


def yt_resolve(url_or_handle):
    """@handle or youtube.com/@handle → (channel_id, handle)."""
    import requests as rq

    s = str(url_or_handle or "").strip()
    m = re.search(r"youtube\.com/@([\w.-]+)", s)
    handle = m.group(1) if m else s.lstrip("@").strip()
    if handle.startswith("UC") and len(handle) == 24:
        return handle, handle
    r = rq.get(f"https://www.youtube.com/@{handle}",
               headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    m = re.search(r'"(UC[\w-]{22})"', r.text)
    if not m:
        raise ValueError(f"couldn't find a channel id for @{handle}")
    return m.group(1), handle


# ============================================================================
# Task run functions — each gets ctx with llm / web_search / tg_send /
# log / now (Dhaka) / name, plus its own params from the TASKS entry.
# ============================================================================

def _daily_briefing(ctx):
    """Compound topics get one search per topic, plus authoritative live
    data (weather/prices/rates) — one LLM request total."""
    import requests as rq
    topic = ctx["TOPIC"]
    ctx["log"](f"briefing: {topic}")

    facts = []
    t = topic.lower()
    try:
        if "weather" in t or "dhaka" in t:
            facts.append("Weather in Dhaka now: " + fetch_weather("Dhaka"))
    except Exception:
        pass
    for coin in ("bitcoin", "ethereum", "solana", "dogecoin"):
        if coin in t:
            try:
                facts.append(f"{coin}: ${fetch_price(coin, 'usd'):,.0f}")
            except Exception:
                pass
    if any(k in t for k in ("usd", "bdt", "exchange", "rate")):
        try:
            j = rq.get("https://open.er-api.com/v6/latest/USD", timeout=15).json()
            facts.append(f"USD/BDT: {j['rates']['BDT']}")
        except Exception:
            pass

    parts = [p.strip(" .:-") for p in re.split(r"[;,]|\(\d+\)|\d+\.", topic)
             if len(p.strip(" .:-")) > 3]
    queries = parts if 1 < len(parts) <= 6 else [topic]

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
            f"[{i+1}] {r['title']}\n{r['body']}" for i, r in enumerate(results[:12]))
    if not context:
        ctx["tg_send"](f"☀️ Briefing — {topic}\n"
                       f"(nothing usable came back today — retrying next time)")
        return

    summary = ctx["llm"]([
        {"role": "system",
         "content": "Summarize today's briefing in 5-8 short bullet points. "
                    "Use the live-data numbers as-is. Only include what the "
                    "data/results actually say — if a topic has no data, "
                    "skip it silently instead of announcing it's missing."},
        {"role": "user", "content": f"Topic: {topic}\n\n{context[:12000]}"},
    ])
    ctx["tg_send"](f"☀️ <b>Daily briefing</b>\n{comms.esc(topic)}\n\n{summary}")


def _price_watch(ctx):
    """Alert when the price CROSSES a threshold (first run records only)."""
    coin = ctx["COIN"]
    vs = ctx.get("VS", "usd")
    price = fetch_price(coin, vs, ctx.get("SYMBOL"))

    prices = state.STATE.setdefault("prices", {})
    prev = prices.get(coin)
    prices[coin] = price
    state.save_soon()  # baseline survives restarts via the gist

    if prev is None:
        ctx["log"](f"{coin} = {price} {vs.upper()} (recorded, watching)")
        return
    if ctx.get("BELOW") and prev >= ctx["BELOW"] > price:
        ctx["tg_send"](f"📉 {coin} dropped below {ctx['BELOW']}! "
                       f"Now {price:,.2f} (was {prev:,.2f})")
    elif ctx.get("ABOVE") and prev <= ctx["ABOVE"] < price:
        ctx["tg_send"](f"📈 {coin} rose above {ctx['ABOVE']}! "
                       f"Now {price:,.2f} (was {prev:,.2f})")
    else:
        ctx["log"](f"{coin} = {price} {vs.upper()}")


def _price_report(ctx):
    price = fetch_price(ctx["COIN"], ctx.get("VS", "usd"), ctx.get("SYMBOL"))
    ctx["tg_send"](f"💰 {ctx['COIN']} is now {price:,.2f} USD")


def _weather_report(ctx):
    ctx["tg_send"](f"🌦 <b>Weather</b> — {comms.esc(ctx['LOCATION'].title())}\n"
                   + comms.esc(fetch_weather(ctx["LOCATION"])), html=True)


def _weather_once(ctx):
    """One-shot: runs, then removes itself forever."""
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


def _watch_page(ctx):
    import requests as rq

    url = ctx["URL"]
    ctx["log"](f"watching: {url}")
    body = rq.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).text
    digest = hashlib.md5(body.encode()).hexdigest()

    hashes = state.STATE.setdefault("page_hashes", {})
    old = hashes.get(url)
    hashes[url] = digest
    state.save_soon()

    if old and old != digest:
        ctx["tg_send"](f"👁 Page changed: {url}")


def _watch_feed(ctx):
    """Any RSS/Atom feed — alert when the newest entry changes."""
    import requests as rq

    url = ctx["URL"]
    r = rq.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    items = re.findall(r"<item>(.*?)</item>", r.text, re.S) or \
        re.findall(r"<entry>(.*?)</entry>", r.text, re.S)
    if not items:
        ctx["log"](f"feed empty: {url}")
        return
    block = items[0]
    lm = re.search(r"<link[^>]*>([^<]+)</link>", block) or \
        re.search(r'<link[^>]*href="([^"]+)"', block)
    tm = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?([^>\]]+)", block)
    latest = (lm.group(1).strip() if lm else "",
              tm.group(1).strip() if tm else "")

    feeds = state.STATE.setdefault("feeds", {})
    old = feeds.get(url)
    feeds[url] = latest
    state.save_soon()

    if old and old != latest:
        ctx["tg_send"](f"📰 New in feed: {latest[1] or latest[0]}\n{latest[0]}")
    elif old is None:
        ctx["log"](f"feed baseline set: {url}")


def _watch_youtube(ctx):
    """New-video alerts via the channel's public RSS feed. YouTube throws
    transient 500/404s at datacenter IPs, so one retry before giving up."""
    import time as _time
    import requests as rq

    chan = ctx["CHANNEL_ID"]
    last_exc = None
    for _attempt in (1, 2):
        try:
            r = rq.get("https://www.youtube.com/feeds/videos.xml",
                       params={"channel_id": chan},
                       headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            _time.sleep(4)
    if last_exc is not None:
        raise last_exc

    entries = re.findall(
        r"<entry>.*?<yt:videoId>([\w-]{11})</yt:videoId>.*?<title>([^<]+)</title>",
        r.text, re.S)
    if not entries:
        ctx["log"](f"yt feed parse empty for {chan}")
        return
    latest_id, latest_title = entries[0]

    yt = state.STATE.setdefault("youtube", {})
    prev = yt.get(chan)
    yt[chan] = latest_id
    state.save_soon()

    if prev and prev != latest_id:
        ctx["tg_send"](f"📺 New video: {latest_title}\nhttps://youtu.be/{latest_id}")
    elif prev is None:
        ctx["log"](f"yt baseline set for {chan}")


def _run_skill_task(ctx):
    """Skill whose code was written by the LLM (forge.py) — sandboxed."""
    name = ctx.get("name") or ""
    memory = state.STATE.get("skill_memory", {}).get(name, {})
    ok, out = forge.run_skill(ctx["CODE"], ctx.get("PARAMS") or {},
                              memory=memory, timeout=90)
    if ok:
        state.STATE.setdefault("skill_memory", {})[name] = out.get("memory") or {}
        state.save_soon()
        if out.get("message"):
            ctx["tg_send"](out["message"])
        else:
            ctx["log"](f"skill silent: {out.get('skip', '')}")
    else:
        ctx["tg_send"](
            f"⚠️ Skill failed: {comms.esc(out.get('error', '')[:300])}\n"
            f"(stop it with /kill {name or '?'})")


_PRAYERS = ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")


def _prayer_alert(ctx):
    """Dhaka prayer times (aladhan, method=1) + 10-minute warnings."""
    import requests as rq

    st = state.STATE.setdefault("prayer", {})
    today = f"{ctx['now']:%Y-%m-%d}"
    if st.get("date") != today:
        try:
            r = rq.get("https://api.aladhan.com/v1/timingsByCity",
                       params={"city": "Dhaka", "country": "Bangladesh",
                               "method": 1},
                       timeout=15)
            r.raise_for_status()
            t = r.json()["data"]["timings"]
            st.update({"date": today,
                       "times": {p: t[p] for p in _PRAYERS},
                       "alerted": []})
            state.save_soon()
            ctx["tg_send"]("🕌 <b>Today's prayers</b> (Dhaka)\n" + "\n".join(
                f"• {p}: {t[p]}" for p in _PRAYERS), html=True)
        except Exception as e:
            ctx["log"](f"prayer fetch failed: {e}")
            return

    alerted = st.setdefault("alerted", [])
    for p, hm in st.get("times", {}).items():
        try:
            h, mnt = map(int, str(hm).split(":")[:2])
        except Exception:
            continue
        warn_at = ctx["now"].replace(hour=h, minute=mnt, second=0,
                                     microsecond=0) - timedelta(minutes=10)
        if warn_at <= ctx["now"] < warn_at + timedelta(minutes=1) \
                and p not in alerted:
            alerted.append(p)
            state.save_soon()
            ctx["tg_send"](f"🕌 {p} in 10 minutes ({hm} Dhaka)")


def _daily_report(ctx):
    """Evening self-accounting: runs, failures, quota, money, suggestions."""
    from collections import Counter

    today = f"{ctx['now']:%Y-%m-%d}"
    runs = [r for r in state.STATE.get("runs", [])
            if str(r.get("at", "")).startswith(today)]

    lines = [f"📊 <b>Daily report</b> — {today}"]
    if runs:
        counts = Counter(r["task"] for r in runs)
        fails = [r for r in runs if not r.get("ok")]
        lines.append(f"Runs: {len(runs)} across {len(counts)} tasks")
        lines += [f"• {name} ×{n}" for name, n in counts.most_common(10)]
        if fails:
            lines.append(f"❌ Failures: {len(fails)}")
            lines += [f"  {r['task']}: {str(r.get('err', ''))[:80]}"
                      for r in fails[:4]]
        else:
            lines.append("✅ No failures")
    else:
        lines.append("No scheduled runs today (only manual /run's?)")

    q = state.STATE.get("quota", {})
    used = q.get("used", {})
    if isinstance(used, int):
        used = {"default": used}
    lines.append("LLM: " + " | ".join(
        f"{p} {used.get(k, 0)}/{d}"
        for k, d in [("default", 50), ("groq", 1000), ("gemini", 1500)]
        for p in [k] if k == "default" or used.get(k)))

    exps = [e for e in state.STATE.get("expenses", [])
            if str(e.get("at", "")).startswith(today)]
    if exps:
        tot = sum(e["amount"] for e in exps)
        lines.append(f"💸 Spent today: {tot:,.0f} tk ({len(exps)} items)")

    manual = Counter(r["task"] for r in runs if r.get("manual"))
    sugg = [f"you ran {t} manually {n}× — say 'make {t} every 30m'"
            for t, n in manual.most_common(3) if n >= 3]
    if sugg:
        lines.append("💡 " + " | ".join(sugg))

    if reminders.REMINDERS:
        lines.append("Pending reminders: " + "; ".join(
            f"{r['text'][:30]} ({r['due']:%H:%M})" for r in reminders.REMINDERS[:5]))

    lines.append("Active automations: /tasks • stop one: 'stop <name>'")
    ctx["tg_send"]("\n".join(lines), html=True)


def _heartbeat(ctx):
    try:
        price = fetch_price("bitcoin", "usd")
        extra = f" | BTC ${price:,.0f}"
    except Exception:
        extra = ""
    ctx["tg_send"](f"💓 Hermes is alive — {ctx['now']:%H:%M:%S} Dhaka{extra}")


def _selftest(ctx):
    import requests as rq

    checks = [("telegram send", True)]
    try:
        from ddgs import DDGS
        with DDGS() as d:
            list(d.text("test", max_results=1))
        checks.append(("web search", True))
    except Exception as e:
        checks.append((f"web search ({str(e)[:40]})", False))
    try:
        p = fetch_price("bitcoin", "usd")
        checks.append((f"price feed (BTC ${p:,.0f})", True))
    except Exception as e:
        checks.append((f"price feed ({str(e)[:40]})", False))
    try:
        rq.get("https://api.github.com", timeout=10)
        checks.append(("internet access", True))
    except Exception as e:
        checks.append((f"internet ({str(e)[:40]})", False))
    checks.append(("state " + ("gist-backed" if state.GIST_TOKEN
                               else "memory-only"),
                   bool(state.GIST_TOKEN)))
    checks.append((f"Dhaka time {ctx['now']:%H:%M:%S}", True))

    ok = sum(1 for _, p in checks if p)
    lines = [f"{'✅' if p else '❌'} {comms.esc(n)}" for n, p in checks]
    ctx["tg_send"](f"🧪 <b>Self-test</b>: {ok}/{len(checks)} pass\n" + "\n".join(lines)
                   + "\n(LLM not tested here — /say hi or /diag)", html=True)


# ============================================================================
# Built-in tasks
# ============================================================================

TASKS = {
    "daily_briefing": {
        "desc": f"Daily briefing on '{config.BRIEFING_TOPIC}' at 09:00",
        "schedule": daily(9, 0),
        "run": _daily_briefing,
        "ctx": {"TOPIC": config.BRIEFING_TOPIC},
    },
    "daily_report": {
        "desc": "Evening summary of everything the agent did (free)",
        "schedule": daily(21, 0),
        "run": _daily_report,
        "ctx": {},
    },
    "prayer_alert": {
        "desc": "Prayer times for Dhaka: morning list + 10-min warnings (free)",
        "schedule": every(1 / 60),
        "run": _prayer_alert,
        "ctx": {},
        "quiet": True,  # don't clutter the run history (1400+ runs/day)
    },
    "heartbeat": {
        "desc": "Hourly proof-of-life message (free, no LLM)",
        "schedule": every(1),
        "run": _heartbeat,
        "ctx": {},
    },
    "selftest": {
        "desc": "Check every subsystem in seconds (free) — /run selftest",
        "schedule": never,
        "run": _selftest,
        "ctx": {},
    },
}

CODE_TASKS = frozenset(TASKS)  # built-ins; message-created tasks aren't here


# ============================================================================
# Dynamic task factory — tasks created by message. The router LLM may only
# choose from these types and fill in parameters; it can never inject code
# (except via "skill", whose code runs guarded in the forge sandbox).
# ============================================================================

def dyn_task_name(spec):
    """The deterministic name build() would give this spec, WITHOUT
    registering anything — safe for lookups/comparisons."""
    ttype = (spec or {}).get("type", "")
    params = spec.get("params") or {}
    if ttype == "price_alert":
        return f"alert_{str(params.get('coin') or 'bitcoin').lower().strip()}"
    if ttype == "price_report":
        return f"report_{str(params.get('coin') or 'bitcoin').lower().strip()}"
    if ttype == "briefing":
        hour = int(params.get("hour") if params.get("hour") is not None else 9)
        return f"briefing_{hour:02d}{int(params.get('minute') or 0):02d}"
    if ttype == "weather_once":
        return "weather_once_" + str(
            stable_id((params.get("when_spec"), params.get("location"))))
    if ttype == "weather_daily":
        hour = int(params.get("hour") if params.get("hour") is not None else 8)
        return f"weather_{hour:02d}{int(params.get('minute') or 0):02d}"
    if ttype == "watch_page":
        return f"watch_{stable_id(params.get('url'))}"
    if ttype == "watch_rss":
        return f"rss_{stable_id(params.get('url'))}"
    if ttype == "watch_youtube":
        return f"yt_{stable_id(params.get('url_or_handle'))}"
    if ttype == "skill":
        return f"skill_{stable_id(params.get('goal'))}"
    return None


def _skill_schedule(params):
    sched = params.get("schedule") or {}
    if sched.get("daily"):
        return daily(int(sched["daily"].get("hour", 8)),
                     int(sched["daily"].get("minute", 0)))
    return every(max(1, int(sched.get("every_minutes") or 60)) / 60)


def build(spec):
    """Register a task from a spec dict; returns its name. May add derived
    keys (e.g. due_iso) into spec['params'] so a restart rebuilds the
    exact same task."""
    ttype = (spec or {}).get("type", "")
    params = spec.get("params") or {}

    if ttype == "price_alert":
        coin = str(params.get("coin") or "bitcoin").lower().strip()
        below, above = params.get("below"), params.get("above")
        every_m = max(5, int(params.get("every_minutes") or 30))
        if below is None and above is None:
            raise ValueError("need 'below' or 'above' (USD)")
        base = f"alert_{coin}"
        TASKS[base] = {
            "desc": f"Alert when {coin} crosses "
                    + (f"< ${below}" if below else f"> ${above}"),
            "schedule": every(every_m / 60),
            "run": _price_watch,
            "ctx": {"COIN": coin, "BELOW": below, "ABOVE": above},
        }
        return base

    if ttype == "price_report":
        coin = str(params.get("coin") or "bitcoin").lower().strip()
        every_m = max(1, int(params.get("every_minutes") or 60))
        base = f"report_{coin}"
        TASKS[base] = {
            "desc": f"Report {coin} price every {every_m}m (free)",
            "schedule": every(every_m / 60),
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
            "schedule": daily(hour, minute),
            "run": _daily_briefing,
            "ctx": {"TOPIC": topic},
        }
        return base

    if ttype == "weather_once":
        loc = str(params.get("location") or "Dhaka").strip()
        when = str(params.get("when_spec") or "").strip() or "in 1h"
        due = None
        if params.get("due_iso"):  # restored after a restart — original time
            try:
                due = datetime.fromisoformat(params["due_iso"])
            except Exception:
                due = None
        if due is None:
            parsed = reminders.parse(when)
            if not parsed:
                raise ValueError(f"couldn't understand when '{when}'")
            due = parsed[0]
            params["due_iso"] = due.isoformat()  # persist for restarts
        base = dyn_task_name({"type": ttype, "params": params})

        def once_sched(now, _due=due):
            return now >= _due

        TASKS[base] = {
            "desc": f"One-time weather for {loc} at {due:%H:%M} Dhaka",
            "schedule": once_sched,
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
            "schedule": daily(hour, minute),
            "run": _weather_report,
            "ctx": {"LOCATION": loc},
        }
        return base

    if ttype == "watch_page":
        url = str(params.get("url") or "").strip()
        every_m = max(5, int(params.get("every_minutes") or 60))
        if not url.startswith("http"):
            raise ValueError("url must start with http")
        base = f"watch_{stable_id(url)}"
        TASKS[base] = {
            "desc": f"Watch {url} every {every_m}m, ping on change",
            "schedule": every(every_m / 60),
            "run": _watch_page,
            "ctx": {"URL": url},
        }
        return base

    if ttype == "watch_rss":
        url = str(params.get("url") or "").strip()
        every_m = max(5, int(params.get("every_minutes") or 30))
        if not url.startswith("http"):
            raise ValueError("url must start with http")
        base = f"rss_{stable_id(url)}"
        TASKS[base] = {
            "desc": f"Watch feed {url} every {every_m}m",
            "schedule": every(every_m / 60),
            "run": _watch_feed,
            "ctx": {"URL": url},
        }
        return base

    if ttype == "watch_youtube":
        chan, handle = yt_resolve(params.get("url_or_handle"))
        every_m = max(5, int(params.get("every_minutes") or 15))
        base = f"yt_{stable_id(params.get('url_or_handle'))}"
        TASKS[base] = {
            "desc": f"Watch YouTube @{handle} for new videos every {every_m}m",
            "schedule": every(every_m / 60),
            "run": _watch_youtube,
            "ctx": {"CHANNEL_ID": chan},
        }
        return base

    if ttype == "skill":
        goal = str(params.get("goal") or "custom skill").strip()
        code = str(params.get("code") or "")
        if "def task(" not in code:
            raise ValueError("skill code must define task(ctx)")
        forge.guard(code)  # reject dangerous patterns at registration too
        base = f"skill_{stable_id(goal)}"
        TASKS[base] = {
            "desc": f"Skill: {goal[:70]}",
            "schedule": _skill_schedule(params),
            "run": _run_skill_task,
            "ctx": {"CODE": code, "PARAMS": params.get("user_params") or {}},
        }
        return base

    raise ValueError(
        f"unknown task type '{ttype}' (use price_alert, price_report, "
        "briefing, weather_once, weather_daily, watch_page, watch_rss, "
        "watch_youtube, skill)")


# ============================================================================
# Task management operations
# ============================================================================

def find(target):
    """Match a user's phrase ('the bitcoin alert', 'prayer reminder') to a
    task name — exact/partial name match first, then word overlap. On a
    tie, chat-created tasks beat built-ins (that's what users manage)."""
    t = (target or "").strip().lower().replace(" ", "_")
    if not t:
        return None
    for n in TASKS:
        if t == n.lower() or n.lower().startswith(t) or t in n.lower():
            # exact-name hits also prefer dynamic tasks on collision
            if n not in CODE_TASKS or not any(
                    t in m.lower() for m in TASKS if m not in CODE_TASKS):
                return n
    words = set((target or "").lower().split())
    best, best_score = None, 0
    for n, tk in TASKS.items():
        dw = set(tk["desc"].lower().split()) | set(n.lower().split("_"))
        score = len(words & dw)
        wins = score > best_score or (
            score == best_score and score > 0
            and best in CODE_TASKS and n not in CODE_TASKS)
        if wins:
            best, best_score = n, score
    return best if best_score >= 1 else None


def create_and_save(spec):
    """Build a task and remember its recipe (deduped by name). build() may
    add derived keys into spec['params'], so run it BEFORE saving."""
    name = build(spec)
    n = dyn_task_name(spec)
    lst = state.STATE.setdefault("dynamic_tasks", [])
    if n is not None:  # re-creating replaces the old recipe, no duplicates
        lst[:] = [s for s in lst if dyn_task_name(s) != n]
    lst.append({"type": spec.get("type"), "params": spec.get("params") or {}})
    state.save_soon()
    comms.log(f"dynamic task: {name}")
    return name


def stop(name):
    """Stop any task: dynamic ones are removed permanently; built-ins are
    PAUSED (their code lives here) and resumable via enable()."""
    if name in CODE_TASKS:
        paused = state.STATE.setdefault("paused_tasks", [])
        if name not in paused:
            paused.append(name)
            state.save_soon()
            comms.log(f"task paused: {name}")
        return (f"⏸ <b>Paused</b> — {comms.esc(TASKS[name]['desc'])}\n"
                f"Resume anytime: <code>enable {name}</code>")
    task = TASKS.pop(name)
    state.STATE["dynamic_tasks"] = [
        s for s in state.STATE.get("dynamic_tasks", [])
        if dyn_task_name(s) != name
    ]
    state.STATE.get("skill_memory", {}).pop(name, None)
    state.save_soon()
    comms.log(f"task killed: {name}")
    return f"🗑 <b>Stopped</b> — {comms.esc(task['desc'])}"


def stop_all():
    """Stop every dynamic task (built-ins stay, pause them by name)."""
    dyn = [n for n in list(TASKS) if n not in CODE_TASKS]
    for n in dyn:
        stop(n)
    return f"🗑 Stopped {len(dyn)} task(s). Built-ins stay."


def resume(name):
    paused = state.STATE.setdefault("paused_tasks", [])
    if name in paused:
        paused.remove(name)
        state.save_soon()
        comms.log(f"task resumed: {name}")
        return f"▶️ <b>Resumed</b> — {comms.esc(TASKS[name]['desc'])}"
    if name in TASKS:
        return f"'{name}' isn't paused."
    return f"No task '{name}'. Use /tasks."


def paused_names():
    return set(state.STATE.get("paused_tasks", []))


def apply_schedule(spec, sched):
    """Set a new schedule on a dynamic task spec (in place)."""
    params = spec.setdefault("params", {})
    ttype = spec.get("type")
    if ttype == "weather_once":
        raise ValueError("one-time tasks run at a fixed time — "
                         "stop it and create a new one")
    if sched.get("daily"):
        h = int(sched["daily"].get("hour", 8))
        m = int(sched["daily"].get("minute", 0))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("hour/minute out of range")
        if ttype == "skill":
            params["schedule"] = {"daily": {"hour": h, "minute": m}}
        else:
            params["hour"], params["minute"] = h, m
    elif sched.get("every_minutes"):
        n = max(1, int(sched["every_minutes"]))
        if ttype == "skill":
            params["schedule"] = {"every_minutes": n}
        else:
            params["every_minutes"] = n
    else:
        raise ValueError("schedule must be every_minutes or daily")
