"""Self-verification: the bot tests itself through the REAL pipeline —
real LLM calls, real services, real Telegram rendering — and reports.

Run /verify after every deploy. When something is red, paste the report
to whoever maintains the code: it contains the exact failing output.
This is what ended the deploy-and-pray cycle.
"""

import traceback

import comms
import config
import llm
import memory
import reminders
import research
import state
import tasks
from router import _extract_json, build_system_prompt

# (label, message, acceptable actions) — the router must classify each
# into one of the expected actions with parseable JSON. These mirror the
# exact phrases that broke in the field, so regressions get caught.
ROUTER_CASES = [
    ("chat", "hello, how are you?", {"chat"}),
    ("remember", "remember my verify key is abc123", {"remember"}),
    ("remember casual", "remember this my discord username is mr_c4",
     {"remember", "chat"}),  # chat ok — auto-memory stores it either way
    ("fact aside", "also my pet name is cocky",
     {"remember", "chat"}),  # same: the fact must land, wording is free
    ("forget all", "forget all the memories", {"forget"}),
    ("recall", "what is my verify key",
     {"recall", "chat"}),  # chat is valid if it answers from memory context
    ("reminder", "remind me in 1 hour to test the buzzer", {"reminder"}),
    ("create task", "alert me when bitcoin drops below 99000",
     {"task", "multi"}),
    ("stop task", "stop the bitcoin alert", {"stop"}),
    ("enable task", "enable the heartbeat", {"enable"}),
    ("edit task", "move the daily briefing to 8am", {"edit"}),
    ("expense", "spent 55 taka on tea", {"expense"}),
    ("spend query", "how much did i spend today",
     {"expense_query", "chat"}),
    ("summarize", "https://example.com read this", {"summarize"}),
    ("deep", "do deep research on why the sky is blue", {"deep"}),
    ("multi", "weather in 2 hours and also tell me ethereum price every 30m",
     {"multi", "task"}),
]


def _check(label, fn):
    """Run one check → (ok, detail). Never raises."""
    try:
        detail = fn()
        return (detail is None or detail is True), detail
    except Exception as e:
        traceback.print_exc()
        return False, f"{type(e).__name__}: {e}"


def _router_case(msg, acceptable):
    """One real classification through the exact production prompt."""

    def run():
        raw = llm.complete(
            [{"role": "system", "content": build_system_prompt(msg)},
             {"role": "user", "content": msg}],
            max_tokens=600,
        )
        data = _extract_json(raw)
        if not data:
            return f"no JSON — model said: {str(raw)[:120]!r}"
        action = data.get("action")
        if action not in acceptable:
            return f"got action '{action}' — {str(data)[:150]}"
        return None  # pass

    return run


def run(full=True):
    """The battery. Sends progress, then the report table."""
    results = []  # (ok, label, detail)

    comms.send("🧪 <b>Verifying</b> — testing the real pipeline…", html=True)

    # --- router classification (real LLM calls) ---
    for label, msg, acceptable in ROUTER_CASES:
        comms.typing()
        ok, detail = _check(label, _router_case(msg, acceptable))
        results.append((ok, label, detail))

    # --- services (free) ---
    ok, d = _check("web search", lambda: (
        None if research.web_search("test", max_results=1) else "empty"))
    results.append((ok, "web search", d))
    ok, d = _check("price feed",
                   lambda: None if tasks.fetch_price("bitcoin", "usd") > 0
                   else "no price")
    results.append((ok, "price feed", d))
    ok, d = _check("weather", lambda: None if tasks.fetch_weather("Dhaka")
                   else "empty")
    results.append((ok, "weather", d))
    results.append((bool(config.GROQ_API_KEY), "groq key (voice)",
                    None if config.GROQ_API_KEY else "not set — voice off"))
    results.append((bool(config.GEMINI_API_KEY), "gemini key (vision)",
                    None if config.GEMINI_API_KEY else "not set"))
    results.append((bool(state.GIST_TOKEN), "gist (persistence)",
                    None if state.GIST_TOKEN else "memory-only!"))

    # --- end-to-end with cleanup ---
    def _memory_roundtrip():
        memory.remember("verify roundtrip fact zq7")
        if "zq7" not in memory.recall("verify roundtrip"):
            return "recall missed the fact"
        memory.forget("verify roundtrip")
        return None

    ok, d = _check("memory roundtrip", _memory_roundtrip)
    results.append((ok, "memory roundtrip", d))

    def _reminder_roundtrip():
        if not reminders.add("in 1h verify roundtrip"):
            return "add failed"
        idx = next((i + 1 for i, r in enumerate(reminders.REMINDERS)
                    if r["text"] == "verify roundtrip"), None)
        if not idx:
            return "not stored"
        reminders.cancel(idx)
        return None

    ok, d = _check("reminder roundtrip", _reminder_roundtrip)
    results.append((ok, "reminder roundtrip", d))

    # --- Telegram HTML rendering (the entity bug class) ---
    ok, d = _check(
        "telegram html",
        lambda: None if comms.send(
            "🧪 <b>Bold</b> • <code>mono</code> • 'quotes' &amp; &lt;tags&gt; "
            "render test", html=True) else "Telegram rejected the message")
    results.append((ok, "telegram html", d))

    # --- report ---
    passed = sum(1 for ok, *_ in results if ok)
    lines = [f"🧪 <b>Verify</b> — {passed}/{len(results)} pass", ""]
    for ok, label, detail in results:
        mark = "✅" if ok else "❌"
        extra = f" — {comms.esc(str(detail)[:150])}" if not ok else ""
        lines.append(f"{mark} {label}{extra}")
    if passed == len(results):
        lines.append("\nAll green. Ship it. 🚀")
    else:
        lines.append("\n⚠️ Paste this report to get the red ones fixed.")
    comms.send("\n".join(lines), html=True)


def boot_check():
    """Mini check after every deploy: 1 LLM call + gist + telegram send.
    Messages the owner the result — no more silent broken deploys."""
    try:
        reply = llm.complete(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=150)
        llm_ok = bool(reply) and "ok" in reply.lower()
        detail = "" if llm_ok else f"LLM replied: {reply[:80]!r}"
    except Exception as e:
        llm_ok, detail = False, f"LLM error: {e}"

    html_ok = comms.send(
        "🚀 <b>Deployed &amp; restarted</b>\n"
        f"LLM: {'✅' if llm_ok else '❌ ' + comms.esc(str(detail)[:100])} • "
        f"state: {'gist' if state.GIST_TOKEN else 'memory-only'} • "
        f"memories: {len(state.STATE.get('memories', []))} • "
        f"automations: {len(state.STATE.get('dynamic_tasks', []))}\n"
        f"Full self-test: /verify", html=True)

    if not llm_ok or not html_ok:
        comms.log(f"boot check problem: llm={llm_ok} html={html_ok}")
