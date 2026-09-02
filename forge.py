"""
Skill forge — the bot writes its own task code.

Flow: user asks for an automation no template covers → the LLM writes a
small Python "skill" (a task(ctx) function) → the code is test-run in a
guarded subprocess (no secrets, timeout, no Telegram access) → if the
test passes, the skill is saved as a task recipe in gist state and
scheduled; restarts rebuild it like any other task.

Guardrails (best-effort, honestly): the sandbox subprocess runs with a
clean environment (no API keys/tokens reach skill code), a hard timeout,
blocked dangerous imports (os/subprocess/shutil/sys, eval/exec/open),
and skills can't message Telegram directly — they only RETURN a message
string that the parent process sends.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta



import config

BD_OFFSET = config.BD_OFFSET  # Bangladesh is UTC+6

SKILL_SYSTEM = """You write ONE Python function for a personal automation bot.

def task(ctx):
    ...

ctx is a dict:
- ctx["params"] — settings from the user's request
- ctx["now"] — ISO timestamp string, Dhaka local time (UTC+6)
- ctx["memory"] — dict that PERSISTS between runs. Store last-seen values,
  counters, flags here; next run reads them back. This is how you detect
  change: compare the fresh value to ctx["memory"]["last_x"], alert if
  different/interesting, then UPDATE ctx["memory"]["last_x"] = fresh.
  Keep memory small and JSON-serializable (numbers, strings, lists, dicts).

Rules — the sandbox enforces these, violations kill the skill:
- Allowed imports: requests, json, re, math, datetime, urllib.request,
  ddgs (for web search: from ddgs import DDGS; then DDGS().text(query)).
- BLOCKED: os, sys, subprocess, shutil, importlib, eval(), exec(), open().
  Do not import or call them.
- Must finish in under 60 seconds. Always pass timeout=15 (or similar)
  to requests calls.
- Network calls: wrap in try/except; on failure return a short error
  message instead of raising.
- Return a dict: {"message": "<text to send the user>"} — the parent
  sends it. Return {"message": "", "skip": "<reason>"} to stay silent
  (e.g. nothing changed). Keep messages under ~1500 characters.
- Only send a message when something worth telling happened.
- No files or databases — ctx["memory"] is your only persistence.

Write robust, simple, short code. No commentary outside the function —
your ENTIRE reply is the Python code for task(ctx)."""


# patterns that never appear in safe skill code (checked before running)
_BLOCKED = (
    "import os", "from os", "import sys", "from sys", "import subprocess",
    "from subprocess", "import shutil", "from shutil", "importlib",
    "__import__", "eval(", "exec(", "open(", "globals(", "locals(",
)


def guard(code):
    """Reject dangerous patterns before any skill code is run or saved."""
    for pat in _BLOCKED:
        if pat in code:
            raise ValueError(f"blocked pattern in skill code: '{pat}'")
    if "def task(" not in code:
        raise ValueError("skill code must define task(ctx)")


def _extract_code(raw):
    """Pull Python out of an LLM reply, tolerating ``` fences."""
    raw = (raw or "").strip()
    m = re.search(r"```(?:python)?\s*(.*?)```", raw, re.S)
    if m:
        return m.group(1).strip()
    # no fence: assume the whole reply is code if it looks like it
    if "def task(" in raw:
        return raw
    return None


def _sandbox_env():
    env = {"PYTHONIOENCODING": "utf-8"}
    if os.name == "nt":  # Windows Python needs SYSTEMROOT to start
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    return env


_WRAPPER = """

import json as _json, sys as _sys
_ctx = _json.loads(_sys.stdin.read())
try:
    _r = task(_ctx)
    if isinstance(_r, str):
        _r = {"message": _r}
    _sys.stdout.write(_json.dumps(
        {"ok": True, "result": _r, "memory": _ctx.get("memory", {})},
        default=str))
except Exception as _e:
    _sys.stdout.write(_json.dumps(
        {"ok": False, "error": f"{type(_e).__name__}: {_e}"}))
"""


def run_skill(code, user_params, memory=None, timeout=90):
    """Run skill code in a guarded subprocess. memory is the skill's
    persistent dict (loaded from state); whatever it leaves there comes
    back in out["memory"] for the parent to save.
    Returns (ok, dict): ok=True → dict has 'message'/'skip'/'memory',
    ok=False → dict has 'error'. Never raises."""
    try:
        guard(code)
    except ValueError as e:
        return False, {"error": str(e)}

    ctx = {
        "params": user_params or {},
        "memory": memory or {},
        "now": (datetime.now() + BD_OFFSET).isoformat(),
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code + _WRAPPER],
            input=json.dumps(ctx),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=_sandbox_env(),
            cwd=tempfile_dir(),
        )
    except subprocess.TimeoutExpired:
        return False, {"error": f"skill exceeded {timeout}s timeout"}
    except Exception as e:
        return False, {"error": f"could not run skill: {e}"}

    if proc.returncode != 0 and not proc.stdout.strip():
        err = (proc.stderr or "").strip()[-600:]
        return False, {"error": f"crashed: {err or 'no output'}"}

    # the wrapper writes one JSON line at the end — take the last line
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    if not lines:
        err = (proc.stderr or "").strip()[-600:]
        return False, {"error": f"no result: {err or 'empty output'}"}
    try:
        out = json.loads(lines[-1])
    except Exception:
        return False, {"error": f"unparseable output: {lines[-1][:300]}"}

    if out.get("ok"):
        result = out.get("result") or {}
        msg = str(result.get("message", ""))[:4000]
        try:
            mem = out.get("memory") or {}
            if len(json.dumps(mem, default=str)) > 8000:
                mem = {}  # runaway memory — reset rather than bloat state
        except Exception:
            mem = {}
        return True, {"message": msg, "skip": result.get("skip", ""),
                      "memory": mem}
    return False, {"error": str(out.get("error", "unknown error"))[:600]}


def tempfile_dir():
    import tempfile

    d = tempfile.gettempdir()
    return d if os.path.isdir(d) else None


def forge_skill(goal, user_params, schedule, llm, report):
    """Full creation flow. Returns a task spec dict for tasks.build,
    or None if the skill couldn't be written/tested (already reported)."""
    report(f"🔨 Writing a skill for: {goal}")

    raw = llm(
        [
            {"role": "system", "content": SKILL_SYSTEM},
            {"role": "user",
             "content": f"Automation request: {goal}\n"
                        f"Settings (ctx['params']): {json.dumps(user_params or {})}"},
        ],
        max_tokens=1500,
    )
    code = _extract_code(raw)
    if not code:
        report("⚠️ The model didn't produce code for that. Try rephrasing?")
        return None

    ok, out = run_skill(code, user_params, timeout=90)
    if not ok:
        # one repair attempt: show the model its own error
        report(f"🧪 First test failed ({out.get('error', '')[:150]}), fixing…")
        raw2 = llm(
            [
                {"role": "system", "content": SKILL_SYSTEM},
                {"role": "user",
                 "content": f"Automation request: {goal}\nSettings: "
                            f"{json.dumps(user_params or {})}"},
                {"role": "assistant", "content": code},
                {"role": "user",
                 "content": f"That code failed when run:\n"
                            f"{out.get('error', '')}\n"
                            f"Rewrite the whole function, fixed."},
            ],
            max_tokens=1500,
        )
        code2 = _extract_code(raw2)
        if not code2:
            report("⚠️ Repair didn't produce code either. Try rephrasing?")
            return None
        code = code2
        ok, out = run_skill(code, user_params, timeout=90)
        if not ok:
            report(f"⚠️ Still failing: {out.get('error', '')[:300]}\n"
                   f"Skill not saved.")
            return None

    note = out.get("message") or out.get("skip") or "(silent)"
    report(f"🧪 Test passed → {note[:200]}")

    return {
        "type": "skill",
        "params": {
            "goal": goal,
            "code": code,
            "schedule": schedule or {},
            "user_params": user_params or {},
        },
    }
