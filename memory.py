"""
Persistent memory — facts the owner tells the bot, stored in the gist.

Costs nothing: storing, recalling, forgetting are all plain Python.
Security note: memories are plain text in a secret GitHub gist — fine
for low-stakes facts, not a password vault.
"""

import re
from datetime import datetime, timedelta

import state

BD_OFFSET = timedelta(hours=6)  # Bangladesh is UTC+6

MAX_MEMORIES = 500   # cap so the gist stays small
MAX_TEXT = 500       # per-memory character cap


def _now():
    return (datetime.now() + BD_OFFSET).isoformat()


def _tokens(s):
    return set(t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t)


def _memories():
    return state.STATE.setdefault("memories", [])


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def remember(text):
    """Store a fact. Near-duplicate of an existing memory updates it in
    place (fresh text + timestamp) instead of piling up entries.
    Returns a confirmation string."""
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
    del mems[:-MAX_MEMORIES]  # oldest fall off at the cap
    state.save_soon()
    return f"🧠 Remembered: {text}"


def recall(query, n=3):
    """Top-n memories by keyword overlap (plus a small recency bonus).
    Returns a reply string; never calls the LLM."""
    q = _tokens(query) - {"what", "s", "is", "my", "the", "a", "do", "you",
                          "remember", "about", "me", "i", "tell", "said",
                          "say", "did"}
    if not q:
        return "Search for what? 🙂 e.g. what do you remember about wifi"

    scored = []
    for i, m in enumerate(_memories()):
        score = len(q & _tokens(m["text"]))
        if score > 0:
            scored.append((score + i / 1000.0, m))  # tie → more recent
    if not scored:
        return ("I don't have that stored. To save it, say: "
                "remember <fact>")

    scored.sort(reverse=True)
    lines = []
    for _, m in scored[:n]:
        when = m["at"][:10]
        lines.append(f"• {m['text']}  ({when})")
    return "\n".join(lines)


def forget(query):
    """'everything'/'all' clears all memories; otherwise removes the best
    keyword match. Returns a message; never raises."""
    q = query.strip().lower()
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
    for m in mems[-n:]:
        lines.append(f"• {m['text'][:120]}  ({m['at'][:10]})")
    return "\n".join(lines)


def inject_for(text, budget=1500):
    """A 'Facts the user previously told you:' block from the top-5
    keyword-matched memories, capped to budget chars. '' if no match.
    Appended to chat system prompts — costs 0 extra requests."""
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
    if not out:
        return ""
    return block + "\n".join(out)


# Light auto-memory: catch fact-shaped phrases in normal chat and store
# them silently. Regex-only, so it costs nothing and never hallucinates.
_AUTO_PATTERNS = [
    re.compile(r"\bmy name is ([\w .'-]{2,30})", re.I),
    # lazy match + lookahead: stop the fact at , / and / . / end of sentence
    re.compile(r"\bmy ([a-z][a-z ]{2,18}?) is ([\w .@#/+%-]{2,50}?)"
               r"(?=$|[.!?]|,|\s+and\b|\s+but\b)", re.I),
    re.compile(r"\bi live in ([\w ,]{2,30}?)(?=$|[.!?]|,|\s+and\b)", re.I),
    re.compile(r"\bi (?:like|love|hate|prefer) ([\w ,]{2,35}?)(?=$|[.!?]|,|\s+and\b)", re.I),
    re.compile(r"\bi(?:'m| am) (?:a|an) ([\w ]{2,35}?)(?=$|[.!?]|,|\s+and\b)", re.I),
]


def auto_extract(text):
    """Pull fact-shaped phrases out of a chat message and remember them
    (deduped). Returns the list of NEW facts stored — silent by design."""
    out = []
    for pat in _AUTO_PATTERNS:
        for m in pat.finditer(text or ""):
            fact = " ".join(m.group(0).split())
            if len(fact) < 8 or len(fact) > 90:
                continue
            if "remember" in fact.lower():
                continue  # that's an explicit remember, handled elsewhere
            r = remember(fact)
            if "Remembered" in r:
                out.append(fact)
    return out
