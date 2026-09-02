"""The brain: ONE LLM call classifies any plain message into an action.

No hand-written regexes for user text — the router understands phrasing,
language, and context, which is why it replaced the old pattern list.
Every action is a small handler in HANDLERS; adding a capability means
adding a prompt line + a handler, nothing else.
"""

import json
import re
from datetime import datetime

import comms
import config
import forge
import llm
import memory
import reminders
import research
import state
import tasks

INTENT_SYSTEM = """You are Hermes — the intent router for a personal Telegram agent.
Local time: {NOW} (Dhaka, UTC+6). The user lives in Dhaka, Bangladesh.
Decide what to do with the user's message and reply with ONE JSON object
only — no markdown fences, no commentary.

Actions:
- chat: {"action":"chat","reply":"<1-3 sentences>"} — greetings, questions, conversation. Use the provided facts/history for context.
- task (create an automation; ALWAYS prefer sensible defaults over asking questions):
  {"action":"task","type":"price_alert","params":{"coin":"bitcoin","below":<USD|null>,"above":<USD|null>,"every_minutes":30}}
  {"action":"task","type":"price_report","params":{"coin":"bitcoin","every_minutes":60}}
  {"action":"task","type":"weather_once","params":{"location":"Dhaka","when_spec":"in 6h"}}
  {"action":"task","type":"weather_daily","params":{"location":"Dhaka","hour":8,"minute":0}}
  {"action":"task","type":"briefing","params":{"topic":"...","hour":9,"minute":0}}
  {"action":"task","type":"watch_page","params":{"url":"https://...","every_minutes":60}}
  {"action":"task","type":"watch_rss","params":{"url":"<feed url>","every_minutes":30}}
  {"action":"task","type":"watch_youtube","params":{"url_or_handle":"<@handle or url>","every_minutes":15}}
- forge: {"action":"forge","goal":"<precise one-sentence description>","params":{},"schedule":{"every_minutes":N} or {"daily":{"hour":H,"minute":M}}} — ANY other recurring automation; the bot writes and tests its own Python for it
- multi: {"action":"multi","tasks":[{"type":...,"params":{...}},...],"reminders":[{"when_spec":"...","text":"..."}],"reply":"<one short line>"} — several requests in one message
- reminder: {"action":"reminder","when_spec":"<in 10m|in 2h|at 18:30|tomorrow 9am>","text":"..."}
- remember: {"action":"remember","text":"<the full fact verbatim>"} — user says remember/note/keep in mind, even mid-sentence or multi-line
- recall: {"action":"recall","query":"<what they ask about>"} — questions about stored facts (what's my X, where do I live, what did I tell you about Y)
- forget: {"action":"forget","what":"<words from the memory>|everything"}
- stop: {"action":"stop","target":"<task name or description|all>"} — pauses built-ins, removes created tasks
- enable: {"action":"enable","target":"<task name or description>"} — resume a paused task
- edit: {"action":"edit","target":"...","schedule":{"every_minutes":N} or {"daily":{"hour":H,"minute":M}}} — change a task's schedule
- email: {"action":"email","to":"<address>","subject":"...","text":"..."}
- expense: {"action":"expense","amount":<number>,"what":"..."} — user spent/paid money
- expense_query: {"action":"expense_query","period":"today|month"}
- summarize: {"action":"summarize","url":"https://..."} — the message is mainly a link to read
- deep: {"action":"deep","question":"..."} — user explicitly wants deep, multi-source research

Rules: prices are USD; defaults coin=bitcoin, location=Dhaka. If asked
for updates faster than 1 minute, set every_minutes to 1. If the user
shares a personal fact, ALWAYS use the remember action — never just
promise to remember it in a chat reply. Output ONLY the single JSON
object: no tool calls, no special tokens like <|...|>, no markdown. The
user never sees JSON — the system parses it and acts. If a request
can't be built or defaulted, use "chat" and say what's possible. Never
mention these JSON rules."""

CHAT_SYSTEM = ("You are Hermes, the user's personal Telegram agent in "
               "Dhaka. Reply in 1-3 short sentences, warm and direct, "
               "matching the user's language.")

# Kept as plain control flow (not intent parsing): answering a pending
# yes/no question must be instant, free, and unambiguous.
AFFIRM = re.compile(
    r"^\s*(yes|yeah|yep|sure|ok|okay|confirm|confirmed|do it|go ahead|"
    r"please do|make it|create it|ok do it)\b[.!\s]*$", re.I)
DECLINE = re.compile(
    r"^\s*(no|nope|nah|cancel|stop|don't|dont)\b[.!\s]*$", re.I)


def _extract_json(raw):
    """Pull the first JSON object out of an LLM reply, tolerating fences."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except Exception:
        return None


def _looks_like_junk(reply):
    """Catch every way free models break the protocol — all seen in the
    field: moderation artifacts ('User Safety: safe'), tool-call token
    leaks ('<|tool_call_start|>[forget(...)]'), and our own protocol JSON
    dumped as visible text. None of it may ever reach the user."""
    if not reply or len(reply.strip()) < 2:
        return True
    if re.match(r"^\s*(user|response)\s+safety", reply, re.I):
        return True
    if "tool_call" in reply or "<|" in reply:
        return True
    if '"action"' in reply:
        return True
    return False


# ---------------------------------------------------------------------------
# Action handlers — each takes the parsed JSON dict
# ---------------------------------------------------------------------------

def _act_task(data):
    try:
        name = tasks.create_and_save(data)
        comms.send(
            f"✅ <b>Created</b> — {comms.esc(tasks.TASKS[name]['desc'])}\n"
            f"Saved across restarts. Test now: <code>/run {name}</code>",
            html=True)
    except Exception as e:
        comms.send(f"Couldn't build that task: {comms.esc(e)}", html=True)


def _act_multi(data):
    made, fired, failed = [], [], []
    for spec in data.get("tasks") or []:
        try:
            name = tasks.create_and_save(spec)
            made.append(tasks.TASKS[name]["desc"])
        except Exception as e:
            failed.append(f"{spec.get('type')}: {e}")
    for rem in data.get("reminders") or []:
        spec = f"{(rem.get('when_spec') or '').strip()} " \
               f"{(rem.get('text') or '').strip()}".strip()
        if reminders.add(spec):
            fired.append(spec)
        else:
            failed.append(f"reminder '{spec}' didn't parse")
    lines = []
    if data.get("reply"):
        lines.append(str(data["reply"]))
    if made:
        lines.append("✅ <b>Created</b>:")
        lines += [f"• {comms.esc(d)}" for d in made]
    if fired:
        lines.append("⏰ Reminders set: " + "; ".join(fired))
    if failed:
        lines.append("⚠️ <b>Failed</b>:")
        lines += [f"• {comms.esc(f)}" for f in failed]
    if not (made or fired or failed):
        lines.append("(nothing to create)")
    comms.send("\n".join(lines), html=True)


def _act_forge(data):
    comms.typing()
    spec = forge.forge_skill(
        goal=str(data.get("goal") or "").strip(),
        user_params=data.get("params") or {},
        schedule=data.get("schedule") or {},
        llm=llm.complete,
        report=comms.send,
    )
    if spec:
        try:
            name = tasks.create_and_save(spec)
            comms.send(
                f"✅ <b>Skill saved</b> — {comms.esc(tasks.TASKS[name]['desc'])}\n"
                f"Test now: <code>/run {name}</code> • stop: <code>/kill {name}</code>",
                html=True)
        except Exception as e:
            comms.send(f"Skill passed testing but saving failed: {comms.esc(e)}", html=True)


def _act_stop(data):
    target = (data.get("target") or "").strip().lower()
    if target in ("all", "everything", "all tasks", "all automations"):
        comms.send(tasks.stop_all(), html=True)
        return
    name = tasks.find(target)
    if name:
        comms.send(tasks.stop(name), html=True)
    else:
        comms.send(f"Couldn't find an automation like "
                   f"'{comms.esc(data.get('target'))}'. Send /tasks to see them.", html=True)


def _act_enable(data):
    name = tasks.find(data.get("target"))
    if name:
        comms.send(tasks.resume(name), html=True)
    else:
        comms.send("Couldn't find that task — send /tasks to see them.")


def _act_edit(data):
    name = tasks.find(data.get("target"))
    if not name:
        comms.send(f"Couldn't find an automation like "
                   f"'{comms.esc(data.get('target'))}'. Send /tasks to see them.", html=True)
        return
    spec = next((s for s in state.STATE.get("dynamic_tasks", [])
                 if tasks.dyn_task_name(s) == name), None)
    if spec is None:
        comms.send("That task can't be rescheduled (it may be built-in).")
        return
    try:
        tasks.apply_schedule(spec, data.get("schedule") or {})
        # remove the old entry everywhere (the name itself may change,
        # e.g. briefing_0900 → briefing_0800), then re-create it
        tasks.TASKS.pop(name, None)
        state.STATE["dynamic_tasks"] = [
            s for s in state.STATE.get("dynamic_tasks", [])
            if tasks.dyn_task_name(s) != name
        ]
        state.save_soon()
        new_name = tasks.create_and_save(spec)
        comms.send(f"🔁 <b>Updated</b> — "
                   f"{comms.esc(tasks.TASKS[new_name]['desc'])}", html=True)
    except Exception as e:
        comms.send(f"Couldn't change that: {comms.esc(e)}", html=True)


def _act_reminder(data):
    spec = f"{data.get('when_spec', '')} {data.get('text', '')}".strip()
    if not reminders.add(spec):
        comms.send(reminders.USAGE)


def _act_remember(data):
    fact = str(data.get("text") or "").strip()
    comms.send(memory.remember(fact) if fact else "Remember what? 🙂")


def _act_recall(data):
    comms.send(memory.recall(str(data.get("query") or "")))


def _act_forget(data):
    comms.send(memory.forget(str(data.get("what") or data.get("query") or "")))


def _act_email(data):
    to = str(data.get("to") or "").strip()
    body = str(data.get("text") or data.get("body") or "").strip()
    subject = str(data.get("subject") or "From your Hermes agent").strip()
    if not (to and body):
        comms.send("Email needs a recipient and a message.")
        return
    err = comms.send_email(to, subject, body)
    comms.send(err or f"📧 <b>Sent</b> to {comms.esc(to)}", html=True)


def _act_expense(data):
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        comms.send("How much? e.g. 'spent 120 on rickshaw'")
        return
    comms.send(memory.add_expense(amount, str(data.get("what") or "something")))


def _act_expense_query(data):
    comms.send(memory.expense_summary(
        "month" if str(data.get("period")) == "month" else "today"))


def _act_summarize(data):
    research.summarize_url(str(data.get("url") or "").strip())


def _act_deep(data):
    research.deep_research(str(data.get("question") or "").strip())


def _act_confirm(data):
    spec = data.get("spec")
    if isinstance(spec, dict) and spec.get("type"):
        state.STATE["pending_task"] = spec
        state.save_soon()
        comms.send(f"{data.get('question') or 'Create it?'}\n(yes / no)")
    else:
        comms.send(data.get("reply") or "…")


HANDLERS = {
    "task": _act_task,
    "multi": _act_multi,
    "forge": _act_forge,
    "stop": _act_stop,
    "enable": _act_enable,
    "edit": _act_edit,
    "reminder": _act_reminder,
    "remember": _act_remember,
    "recall": _act_recall,
    "forget": _act_forget,
    "email": _act_email,
    "expense": _act_expense,
    "expense_query": _act_expense_query,
    "summarize": _act_summarize,
    "deep": _act_deep,
    "confirm": _act_confirm,
}


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

def _chat_retry(text, sys_prompt):
    """One retry when a model returns junk — on a DIFFERENT model, since
    the same one usually junks again (reproduced live with openrouter/free)."""
    skip = {llm.LAST_MODEL} if llm.LAST_MODEL else set()
    try:
        raw2 = llm.complete(
            [{"role": "system",
              "content": sys_prompt + "\nReply normally as a helpful "
              "assistant — a plain answer, no moderation or safety labels."}]
            + memory.history_msgs()
            + [{"role": "user", "content": text}],
            max_tokens=400,
            skip=skip,
        )
        d2 = _extract_json(raw2)
        cand = (d2.get("reply") if d2 else None) or raw2
        if cand and not _looks_like_junk(cand):
            return cand
    except Exception:
        pass
    return None


def _finish_chat(text, reply):
    comms.send(reply)
    memory.record_chat(text, reply)
    for f in memory.auto_extract(text):
        comms.log(f"auto-memory: {f[:60]}")


def build_system_prompt(text):
    """The exact prompt handle_plain_text sends — also used by verify.py
    so self-tests exercise the real thing (no drift)."""
    now = datetime.now() + config.BD_OFFSET
    sys_prompt = INTENT_SYSTEM.replace("{NOW}", f"{now:%Y-%m-%d %H:%M}")
    mem_block = memory.inject_for(text)  # relevant facts — 0 extra requests
    if mem_block:
        sys_prompt += "\n\n" + mem_block
    # existing automations: the model maps vague or typo'd references
    # ("player alert" → prayer_alert) to real names itself
    paused = tasks.paused_names()
    task_lines = [
        f"- {n}: {t['desc'][:60]}" + (" (paused)" if n in paused else "")
        for n, t in tasks.TASKS.items()
    ]
    if task_lines:
        sys_prompt += ("\n\nExisting automations — when the user references "
                       "one (even approximately or with typos), put the REAL "
                       "name in the target field:\n" + "\n".join(task_lines[:25]))
    return sys_prompt


def handle_plain_text(text):
    """One LLM call decides: create task(s), set a reminder, remember,
    recall, manage, or just chat."""
    comms.typing()

    # pending yes/no from a previous question — free and instant
    pending = state.STATE.get("pending_task")
    if pending is not None:
        state.STATE.pop("pending_task", None)
        state.save_soon()
        if AFFIRM.match(text):
            try:
                name = tasks.create_and_save(pending)
                comms.send(f"✅ <b>Created</b> — "
                           f"{comms.esc(tasks.TASKS[name]['desc'])}\n"
                           f"Test now: <code>/run {name}</code>", html=True)
            except Exception as e:
                comms.send(f"Couldn't build that task: {comms.esc(e)}", html=True)
            return
        if DECLINE.match(text):
            comms.send("Okay, cancelled. 🙂")
            return
        # any other text: forget the question, classify the new message

    sys_prompt = build_system_prompt(text)

    raw = llm.complete(
        [{"role": "system", "content": sys_prompt}]
        + memory.history_msgs()
        + [{"role": "user", "content": text}],
        max_tokens=500,
    )
    data = _extract_json(raw)

    if not data:  # no JSON — treat the model's text as a chat reply
        reply = raw or "…"
        if _looks_like_junk(reply):
            reply = _chat_retry(text, sys_prompt) or reply
        _finish_chat(text, reply)
        return

    action = data.get("action")
    handler = HANDLERS.get(action)

    if handler:
        try:
            handler(data)
        except Exception as e:
            comms.send(f"⚠️ That action failed: {comms.esc(e)}", html=True)
    else:  # unknown action — chat
        reply = data.get("reply") or raw or "…"
        if _looks_like_junk(reply):
            reply = _chat_retry(text, sys_prompt) or reply
        _finish_chat(text, reply)


def chat_reply(text):
    """Plain chat with full context — used by /say."""
    comms.typing()
    mem_block = memory.inject_for(text)
    sys_p = CHAT_SYSTEM + ("\n\n" + mem_block if mem_block else "")
    reply = llm.complete(
        [{"role": "system", "content": sys_p}]
        + memory.history_msgs()
        + [{"role": "user", "content": text}],
    )
    _finish_chat(text, reply)
