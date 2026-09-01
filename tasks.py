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
# Task 2: crypto price watcher (FREE — no LLM, no API key; CoinGecko)
# Alert fires only when the price CROSSES your threshold, not every check.
# ---------------------------------------------------------------------------

_PRICE_STATE = {}  # last seen price per coin, in memory while service runs


def _price_watch(ctx):
    import requests as rq

    coin = ctx["COIN"]          # CoinGecko id, e.g. "bitcoin", "ethereum"
    vs = ctx.get("VS", "usd")
    below = ctx.get("BELOW")    # alert when price drops below this
    above = ctx.get("ABOVE")    # alert when price rises above this

    r = rq.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": coin, "vs_currencies": vs},
        timeout=15,
    )
    r.raise_for_status()
    price = r.json()[coin][vs]

    prev = _PRICE_STATE.get(coin)
    _PRICE_STATE[coin] = price

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

    # Render's disk is ephemeral, so state only survives while the service
    # runs — a change alerts once per deploy. Good enough to start.
    state_file = "/tmp/" + hashlib.md5(url.encode()).hexdigest()[:8]
    try:
        old = open(state_file).read().strip()
    except FileNotFoundError:
        old = None
    with open(state_file, "w") as f:
        f.write(digest)

    if old and old != digest:
        ctx["tg_send"](f"👁 Page changed: {url}")


# ---------------------------------------------------------------------------
# Task 4: reminder template
# ---------------------------------------------------------------------------

def _remind(ctx):
    ctx["tg_send"](f"⏰ Reminder: {ctx['MESSAGE']}")


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
