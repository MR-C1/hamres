"""What the bot remembers about you: explicit facts, auto-extracted
facts, recent chat context, and the expense ledger. All gist-persisted.

Security note: memories are plain text in a secret GitHub gist — fine
for low-stakes facts, not a password vault.
"""

import re
from datetime import datetime

import config
import state

MAX_MEMORIES = 500   # cap so the gist stays small
MAX_TEXT = 500       # per-memory character cap

_STOPWORDS = {"what", "s", "is", "my", "the", "a", "do", "you",
              "remember", "about", "me", "i", "tell", "said", "say",
              "did", "where", "who", "live"}


def _now():
    return (datetime.now() + config.BD_OFFSET).isoformat()


def _tokens(s):
    return set(t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t)


def _memories():
    return state.STATE.setdefault("memories", [])


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --- facts ---------------------------------------------------------------------

def remember(text):
    """Store a fact. Near-duplicates update in place instead of piling up."""
    text = " ".join((text or "").split())[:MAX_TEXT]
    if not text:
        return "Remember what? 🙂 Say: remember <fact>"

    tok = _tokens(text)
    for m in _memories():
        if _jaccard(tok, _tokens(m["text"])) > 0.8:
            m["text"] = text
            m["at"] = _now()
            state.save_soon()
            return f"🧠 Updated: {text}"

    mems = _memories()
    mems.append({"text": text, "at": _now()})
    del mems[:-MAX_MEMORIES]
    state.save_soon()
    return f"🧠 Remembered: {text}"


def recall(query, n=3):
    """Top-n memories by keyword overlap — free, no LLM."""
    q = _tokens(query) - _STOPWORDS
    if not q:
        return "Search for what? 🙂 e.g. what do you remember about wifi"

    scored = []
    for i, m in enumerate(_memories()):
        score = len(q & _tokens(m["text"]))
        if score > 0:
            scored.append((score + i / 1000.0, m))  # tie → more recent
    if not scored:
        return "I don't have that stored. To save it, say: remember <fact>"

    scored.sort(reverse=True)
    return "\n".join(f"• {m['text']}  ({m['at'][:10]})"
                     for _, m in scored[:n])


def forget(query):
    """'everything' clears all; otherwise removes the best keyword match."""
    q = (query or "").strip().lower()
    if q in ("everything", "all"):
        n = len(_memories())
        state.STATE["memories"] = []
        state.save_soon()
        return f"🧹 Forgot {n} memor{'y' if n == 1 else 'ies'}."

    tok = _tokens(q)
    best, best_i, best_score = None, -1, 0
    for i, m in enumerate(_memories()):
        score = len(tok & _tokens(m["text"]))
        if score > best_score:
            best, best_i, best_score = m, i, score
    if best is None:
        return f"I don't remember anything like '{query}'."

    _memories().pop(best_i)
    state.save_soon()
    return f"🧹 Forgot: {best['text']}"


def recent(n=10):
    mems = _memories()
    if not mems:
        return "Nothing stored yet. Say: remember <fact>"
    lines = [f"🧠 {len(mems)} memories (newest last):"]
    lines += [f"• {m['text'][:120]}  ({m['at'][:10]})" for m in mems[-n:]]
    return "\n".join(lines)


def inject_for(text, budget=1500):
    """A 'Facts the user previously told you:' block from the top-5
    keyword-matched memories. '' if no match. Costs 0 extra requests."""
    q = _tokens(text)
    if not q:
        return ""
    scored = []
    for i, m in enumerate(_memories()):
        score = len(q & _tokens(m["text"]))
        if score > 0:
            scored.append((score + i / 1000.0, m))
    if not scored:
        return ""
    scored.sort(reverse=True)

    block = "Facts the user previously told you (use when relevant):\n"
    out = []
    for _, m in scored[:5]:
        line = f"- {m['text']}"
        if len(block) + sum(len(l) + 1 for l in out) + len(line) > budget:
            break
        out.append(line)
    return block + "\n".join(out) if out else ""


# Light auto-memory: catch fact-shaped phrases in normal chat, silently.
_AUTO_PATTERNS = [
    re.compile(r"\bmy name is ([\w .'-]{2,30})", re.I),
    re.compile(r"\bmy ([a-z][a-z ]{2,24}?) is ([\w .@#/+%-]{2,50}?)"
               r"(?=$|[.!?]|,|\s+and\b|\s+but\b)", re.I),
    re.compile(r"\bi live in ([\w ,]{2,30}?)(?=$|[.!?]|,|\s+and\b)", re.I),
    re.compile(r"\bi (?:like|love|hate|prefer) ([\w ,]{2,35}?)"
               r"(?=$|[.!?]|,|\s+and\b)", re.I),
    re.compile(r"\bi(?:'m| am) (?:a|an) ([\w ]{2,35}?)(?=$|[.!?]|,|\s+and\b)", re.I),
]


def auto_extract(text):
    """Pull fact-shaped phrases out of a chat message and remember them.
    Regex-only: costs nothing, never hallucinates. Returns new facts."""
    out = []
    for pat in _AUTO_PATTERNS:
        for m in pat.finditer(text or ""):
            fact = " ".join(m.group(0).split())
            if not 8 <= len(fact) <= 90 or "remember" in fact.lower():
                continue
            if "Remembered" in remember(fact):
                out.append(fact)
    return out


# --- chat context (last few exchanges) --------------------------------------------

def history_msgs(limit=6):
    """Recent turns as LLM messages, so follow-ups have context."""
    h = state.STATE.get("chat_history", [])[-limit:]
    return [{"role": m["role"], "content": str(m["content"])[:400]} for m in h]


def record_chat(user_text, bot_reply):
    h = state.STATE.setdefault("chat_history", [])
    h.append({"role": "user", "content": (user_text or "")[:800], "at": _now()})
    h.append({"role": "assistant", "content": (bot_reply or "")[:800]})
    del h[:-12]  # keep the last 6 exchanges
    state.save_soon()


# --- expense ledger (free, rule-driven via the router) -------------------------------

def add_expense(amount, what):
    state.STATE.setdefault("expenses", []).append({
        "amount": float(amount), "what": what, "at": _now(),
    })
    state.save_soon()
    now = datetime.now() + config.BD_OFFSET
    day = sum(e["amount"] for e in state.STATE["expenses"]
              if str(e["at"])[:10] == f"{now:%Y-%m-%d}")
    return f"💸 Logged {amount:,.0f} tk — {what}. Today: {day:,.0f} tk"


def expense_summary(period="today"):
    now = datetime.now() + config.BD_OFFSET
    exps = state.STATE.get("expenses", [])
    if period == "month":
        tot = sum(e["amount"] for e in exps if str(e["at"])[:7] == f"{now:%Y-%m}")
        return f"💸 This month: {tot:,.0f} tk"
    tot = sum(e["amount"] for e in exps if str(e["at"])[:10] == f"{now:%Y-%m-%d}")
    return f"💸 Today: {tot:,.0f} tk (/expenses for the list)"


def expense_list():
    exps = state.STATE.get("expenses", [])
    if not exps:
        return "No expenses logged. Say: spent <amount> on <thing>"
    now = datetime.now() + config.BD_OFFSET
    d = sum(e["amount"] for e in exps if str(e["at"])[:10] == f"{now:%Y-%m-%d}")
    m = sum(e["amount"] for e in exps if str(e["at"])[:7] == f"{now:%Y-%m}")
    lines = [f"💸 Today: {d:,.0f} tk | this month: {m:,.0f} tk", "Recent:"]
    lines += [f"• {e['amount']:,.0f} tk — {e['what']} ({str(e['at'])[5:10]})"
              for e in exps[-10:]]
    return "\n".join(lines)
