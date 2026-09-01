"""
Hermes tasks — each task is one automation. Add yours here.

A task is a dict:
  desc      — one line, shown by /tasks
  schedule  — function taking datetime now, True if the task should run now
  run       — the function that does the work. It gets:
                llm(messages)      — your LLM
                web_search(query)  — free web search
                tg_send(text)      — message YOU on Telegram
                log(event)         — add to /status log

Start simple. Ship the first version, then add tasks one at a time.
"""

import os
from datetime import datetime


def _never(now):
    return False


def _daily(hour, minute=0):
    def sched(now):
        return now.hour == hour and now.minute == minute
    return sched


# ---------------------------------------------------------------------------
# Example task 1: daily research briefing
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
# Example task 2: watch a page for changes
# ---------------------------------------------------------------------------

def _watch_page(ctx):
    import hashlib

    url = ctx["URL"]
    ctx["log"](f"watching: {url}")
    import requests

    body = requests.get(url, timeout=15).text
    digest = hashlib.md5(body.encode()).hexdigest()

    # Simplest possible state: store the hash in the URL's own comment
    # field via env var is overkill — we compare against a file instead.
    # Render's disk is ephemeral, so a change only alerts once per deploy.
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
# Example task 3: remind yourself of anything, on a schedule
# ---------------------------------------------------------------------------

def _remind(ctx):
    ctx["tg_send"](f"⏰ Reminder: {ctx['MESSAGE']}")


TASKS = {
    "daily_briefing": {
        "desc": "Morning research summary on a topic of your choice",
        "schedule": _daily(9, 0),
        "run": _daily_briefing,
        "ctx": {"TOPIC": os.environ.get("BRIEFING_TOPIC", "AI and tech news")},
    },
    "watch_page": {
        "desc": "Watch a web page and message you when it changes",
        "schedule": _never,  # enable by giving it a schedule below
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
