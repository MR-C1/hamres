"""The growth brain: Gemini-powered analysis, script generation, daily
reports, comment replies, and title optimization. What a real channel
operator would do — data-driven, honest, no fake engagement."""

import json
import re
from datetime import datetime, timedelta

import comms
import cloud
import config
import jobs
import llm
import state
import yt
import yt_analytics

# The channel's topic seed — the fallback direction whenever no learned
# guidance exists yet. PIVOT 2026-09-14: the owner moved FOOTNOTE off
# history/mystery facts (22 videos, 2 subs — the niche wasn't catching)
# onto mind tricks. This string is the single source of that seed.
MIND_TRICKS_SEED = ("mind tricks — dark psychology, persuasion tactics, "
                    "brain glitches, attention hijacking. Every video is "
                    "about the VIEWER: they should finish it feeling seen, "
                    "tricked, or smarter about their own head. Fun and "
                    "eye-catching, never a lecture.")

SYSTEM = ("You are the growth manager of FOOTNOTE — a faceless YouTube "
          "channel about mind tricks: dark psychology, persuasion "
          "tactics, brain glitches, the hidden mechanics of attention. "
          "The brand idea: every video is the footnote everyone skipped "
          "— the tiny detail that changes how you see your own mind. "
          "The target viewer must feel the video is ABOUT THEM. Voice: "
          "punchy, playful, a little unsettling in a fun way, never "
          "academic. Titles create an itch the viewer can't not scratch "
          "— but never lie. Every fact must be true, verifiable, and "
          "replication-aware: no busted myths (10% of the brain, "
          "learning styles, left/right-brained). Never suggest fake "
          "growth tactics (sub4sub, spam, bought views) — they get "
          "channels terminated.")

# Videos published before this date are the old (history/mystery)
# direction — legacy content. Permanent marker, unlike PIVOT_NOTE
# above: the title-swap loop skips them forever; re-titling old-niche
# videos can't help them and the optimization budget belongs to
# new-niche videos.
PIVOT_DATE = "2026-09-14"

# Shown to the strategist until the catalogue is mostly new-niche
# (roughly 10 mind-tricks videos, ~mid-Oct 2026 — then delete this).
# Without it, the back catalogue's history topics look like the
# channel's proven winners and the planner pulls the niche back.
PIVOT_NOTE = ("CHANNEL PIVOT (owner decision, 2026-09-14): this channel "
              "now makes MIND TRICKS videos — dark psychology, "
              "persuasion, brain glitches — not history/mystery. The "
              "existing back catalogue is OLD-direction evidence: do not "
              "use its topics to pull the direction back toward history, "
              "archives, or mysteries. Its FORMAT lessons (video length, "
              "pacing) may still carry over.")


def gemini(prompt, system=SYSTEM, gemini_models=None):
    """One LLM call through the provider chain (Gemini → Groq → OpenRouter).
    gemini_models restricts which Gemini models may answer — script
    generation passes SCRIPT_GEMINI_MODELS (see below)."""
    return llm.complete(prompt, system=system, gemini_models=gemini_models)


# ---------------------------------------------------------------------------
# script generation (same JSON schema as the pipeline queue)
# ---------------------------------------------------------------------------

# Pinned FULL-flash for script writing (probed live 2026-09-19 against the
# real key). The old value "gemini-flash-latest" was a dead alias (chronic
# 503), so scripts silently fell to groq's gpt-oss — the "academic essay"
# voice the QA scorer kept flagging. gemini-3.8/3.5-flash generate reliably
# and honor the 8-12 scene format; the lite geminis (which wrote 3-6 scene
# stubs) are deliberately kept OUT of this list, and stay in
# llm.GEMINI_MODELS only for the short calls (scoring, comments, summaries).
SCRIPT_GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash",
                        "gemini-3.6-flash", "gemini-3.5-flash"]

SCRIPT_PROMPT = """Write ONE video script for a faceless YouTube mind-tricks channel (dark psychology, persuasion tactics, brain glitches), as strict JSON only (no markdown, no commentary):

{{
  "id": "kebab-case-topic-slug",
  "format": ["short", "long"],
  "title": "<curiosity title under 70 chars>",
  "title_alternatives": ["exactly 2 alternatives, each under 70 chars"],
  "description": "Full YouTube description: 120-200 words. First 1-2 lines = a hook that sells the click (this text shows in search results). Then 2-3 short paragraphs of context that tease the trick WITHOUT spoiling the mechanism. End with an engaging question, then a line of 4-6 hashtags relevant to THIS topic (like #psychology #mindtricks #didyouknow).",
  "tags": ["8-14 specific tags: mix broad (psychology, mind tricks, human behavior) and topic-specific] ,
  "hook": "80-120 words. COLD-OPEN by putting the trick ON the viewer: make them count something, choose between options, watch a demonstration unfold on someone — they must PARTICIPATE before they understand. (If the topic truly can't involve the viewer, drop them into the single most striking moment instead.) No greeting, no channel intro, no context. End on the framing question the whole video answers.",
  "scenes": [
    {{"narration": "90-140 words, conversational, fast, surprising.",
      "short_narration": "ONLY on in_short scenes: 40-60 words — the scene's punchiest core, rewritten tight for the Short. End on a line that rewards a rewatch (the trick lands harder the second time)",
      "short_title": "ONLY on in_short scenes: a standalone Short title for just this scene — under 60 chars, curiosity gap, works with zero context",
      "archive_search": ["2-4 real-world archival photo searches"],
      "visual_keywords": ["2-3 stock-footage search phrases (fallback)"],
      "source": "short real citation anchoring this scene's central fact",
      "in_short": true}}
  ],
  "outro": "One-line call to action, max 12 words."
}}

STRUCTURE (this is a mini-documentary, not a list of facts):
- 10-14 scenes total, ~1,300-1,800 words of narration (8-11 minutes).
- Organize as 3-5 ACTS. Each act digs into one phase/angle of the story and
  ENDS unresolved or on a complication (open loop) — never on a conclusion.
- Transitions between scenes must be "but/therefore" (escalate or complicate),
  never "and then" (flat chronology). If a scene could be deleted without
  breaking the chain, cut it.
- The question from the hook must appear in the first 10% of the narration;
  the resolution/payoff appears ONLY in the final 20%. Everything between
  escalates.
- RETENTION MECHANICS (the video must HOLD, not just hook — write for the
  viewer's ear, never their eye):
  * Spoken register always: short sentences (average under 14 words),
    plain words, active voice. If a sentence sounds like an essay,
    rewrite it.
  * EVERY scene ends unresolved — a complication, a question, a twist.
    No scene closes flat.
  * Re-hook at every act break: one line that re-opens curiosity right
    where attention would dip ("And that's where the record stops
    making sense.").
  * Say numbers like a person, not a paper ("nearly two hundred men",
    not "approximately 187 individuals").
  * No literary filler: no throat-clearing, no mood-setting without
    facts, no adjective chains. Every sentence advances the story or
    opens a question — ideally both.
- MIND-TRICKS MECHANICS (what makes THIS niche work):
  * Second person, present tense. "You" language throughout — the
    viewer is the subject of the video, not a spectator of someone
    else's story.
  * Make the viewer DO something in the first 30 seconds: count the
    passes, remember this word, pick a card, watch the left hand. The
    classic attention experiments translate perfectly to video — use
    them as openers, not just as citations.
  * REVEAL-THEN-REPLAY: let the trick or glitch HAPPEN before any
    explanation, then dissect it. The viewer should want to rewatch
    the opening with new eyes — design for that rewatch.
  * End every video with the viewer able to SPOT the trick in the
    wild: a concrete "next time someone does X on you, you'll see it
    coming" moment. Practical payoff = shares and saves.
  * DEFENSIVE FRAMING, always: videos REVEAL what is done TO people
    and how to see through it — never a how-to for doing it to
    others. This is both the channel's ethical line and its
    policy-safe position (manipulation tutorials get channels
    demonetized; reveals get them shared).
- Mark the 4 most visual scenes "in_short": true. Each in_short scene
  becomes BOTH part of the main Short and, if unused there, its own
  standalone Short (that's what short_title is for). All facts true,
  verifiable, and specific (dates, numbers, names).
- ARCHIVAL (real material dominates WHEN IT EXISTS): when a scene names
  a real experiment, researcher, study, or artifact, its "archive_search"
  gives 2-4 exact Wikimedia Commons searches for that real thing
  ("Asch conformity experiment", "Stanford prison experiment", "Phineas
  Gage skull", "Ebbinghaus forgetting curve", "Milgram shock box").
  Many psychology scenes are conceptual (a trick working on you) — no
  real photo exists, so keep their archive_search empty or minimal and
  carry those scenes with strong visual_keywords instead. Never invent
  an archive subject that would not exist.
- SOURCES: at least half the scenes carry a "source" field — a SHORT real
  citation for that scene's central fact, like "Asch, 1951, Psychological
  Monographs", "Kahneman & Tversky, 1974, Science", "Simons & Chabris,
  1999, Perception". Never fabricate a citation: if unsure of the exact
  paper, cite the researcher and year ("Milgram, 1963"). These appear on
  screen — they are the channel's credibility device. A scene without a
  source is fine; a fake source is not.

CRITICAL — visual_keywords decide the stock footage shown during each scene. They are searched on stock-video sites (Pexels), so they must be phrased as searches that RETURN RESULTS there:
- Describe what the narration literally mentions, in filmable terms: a person,
  object, place, or action a camera can point at. NEVER abstract words
  ("mystery", "history", "time", "facts", "story", "secret").
- Use "concrete subject + common visual" phrasing that stock libraries stock:
  "crowd walking city street", "close up human eye", "slot machine
  spinning casino", "phone screen scrolling dark room", "handshake
  business meeting closeup". NOT hyper-specific proper nouns that
  return zero results ("Asch 1951 laboratory") — drop the proper noun,
  keep the visual ("psychology experiment cards").
- VARIETY IS MANDATORY: no keyword may repeat across scenes, and no two
  scenes may share more than one keyword. Each scene's footage must look
  different from the previous scene's. Aim for wide variety: people, objects,
  places, closeups, wide shots, day, night.
- If a concept is abstract, film its concrete consequence: for
  "attention hijacked" use "slot machine spinning closeup"; for "trust
  being built" use "handshake business meeting"; for "memory rewritten"
  use "old photographs scattered table".

Topic guidance from the channel's growth analysis: {direction}
Avoid these already-used topics: {used}

Return ONLY the JSON object."""


VIRALITY_PROMPT = """Score this video hook (the opening narration of a YouTube mind-tricks video) from 0-100 for its ability to stop a scroll and hold attention through the first 30 seconds.

Signals, in descending weight:
1. First sentence is a specific, arresting claim or image (dates, numbers, names) — not a general setup
2. A curiosity gap opens immediately — something unexplained that demands resolution
3. Stakes/consequence are clear — why this matters, what was lost or risked
4. Sensory immediacy — the viewer can SEE the moment being described
5. No throat-clearing (greetings, context, definitions before the hook lands)
6. Momentum — short sentences, active voice, present tense where possible
7. The framing question feels genuinely unanswered (not rhetorical fluff)
8. It promises something the video can actually pay off

Hook: "{hook}"

Title for context: "{title}"

Respond with strict JSON only: {{"score": <0-100 integer>, "reason": "<15 words max on the weakest signal>"}}"""


def _score_hook(script):
    """Second-opinion gate: score the hook before a 20+ min render is
    spent on it. On an API hiccup it scores 0/unscorable (NOT the old 75,
    which silently cleared the bar and could beat a real draft) — best-of-3
    still ships the least-bad script, but flagged, never passed as clean."""
    try:
        r = gemini(VIRALITY_PROMPT.format(
            hook=script.get("hook", "")[:600],
            title=script.get("title", "")[:100]))
        if r.strip().startswith("```"):
            r = r.split("```")[1]
            if r.strip().startswith("json"):
                r = r[4:]
        d = json.loads(r)
        return int(d.get("score", 0)), str(d.get("reason", ""))[:120]
    except Exception as e:
        comms.log(f"hook scoring failed (gate open): {str(e)[:60]}")
        return 0, "unscorable — scorer unavailable"


RETENTION_PROMPT = """Score this video SCRIPT (a mini-documentary) 0-100 for its ability to HOLD attention through the whole video — not the hook, the middle.

Signals, in descending weight:
1. Every scene ends unresolved — a complication, a question, a twist — no scene closes flat
2. Escalation: "but/therefore" momentum between scenes, never flat "and then" chronology
3. Re-hooks at act breaks — a line that re-opens curiosity right where attention would dip
4. Spoken register — short sentences (average under 14 words), plain words, active voice; an essay voice scores low
5. Tight scenes — nothing that could be cut without breaking the chain
6. Payoff placement — the resolution only lands in the final act

Scenes:
"{scenes}"

Title for context: "{title}"

Respond with strict JSON only: {{"score": <0-100 integer>, "reason": "<15 words max on the weakest signal>"}}"""


def _score_retention(script):
    """Gate 2: hold-ability of the full script — the hook scorer only
    sees the first 30 seconds, and the 2.5% long-form retention lived in
    the middle. Scores 0/unscorable on an API hiccup, same contract as _score_hook —
    flagged, never a silent pass."""
    try:
        scenes = "\n\n".join(
            f"[{i + 1}] {s.get('narration', '')}"
            for i, s in enumerate(script.get("scenes", [])))[:6000]
        r = gemini(RETENTION_PROMPT.format(
            scenes=scenes, title=script.get("title", "")[:100]))
        if r.strip().startswith("```"):
            r = r.split("```")[1]
            if r.strip().startswith("json"):
                r = r[4:]
        d = json.loads(r)
        return int(d.get("score", 0)), str(d.get("reason", ""))[:120]
    except Exception as e:
        comms.log(f"retention scoring failed (gate open): {str(e)[:60]}")
        return 0, "unscorable — scorer unavailable"


RESEARCH_PROMPT = """You are researching a topic for a mind-tricks YouTube channel (dark psychology, persuasion tactics, brain glitches). Channel direction: {direction}

Topics already used (pick something DIFFERENT): {used}

Pick ONE topic in this direction and RESEARCH IT using web search. Prefer effects with NAMED studies, experiments, or researchers behind them — and note in a source if an effect failed replication. Return strict JSON only:
{{
  "topic": "the chosen subject",
  "angle": "the fresh 'footnote' angle — the overlooked detail or the trick's mechanism that changes how the viewer sees themselves (1-2 sentences)",
  "sources": [{{"fact": "one specific verified fact", "citation": "source name + URL"}} ... 5 to 8 entries, from REAL search results],
  "archive_terms": ["3-5 Wikimedia Commons search terms for real photos of the actual experiments, researchers, or evidence"],
  "saturation_note": "one sentence: how covered is this topic already by big channels?"
}}"""


def _research(direction, used):
    """Grounded pre-script research pass (Doc-2 pattern): collect real
    sources BEFORE writing, so the script is written FROM evidence
    instead of model memory. Fails open — quota loss or a search hiccup
    just means writing from memory as before."""
    try:
        r = llm.search_complete(RESEARCH_PROMPT.format(
            direction=direction, used=used))
        if r.strip().startswith("```"):
            r = r.split("```")[1]
            if r.strip().startswith("json"):
                r = r[4:]
        d = json.loads(r)
        if d.get("sources"):
            comms.log(f"research pass: {len(d['sources'])} sources on "
                      f"'{str(d.get('topic'))[:40]}'")
            return d
    except Exception as e:
        comms.log(f"research pass failed (open): {str(e)[:60]}")
    return None


FACTCHECK_PROMPT = """You are a fact-checker for a psychology YouTube channel. Below are a script's scene narrations (each with its claimed source) and the RESEARCH SOURCES gathered for this topic before writing.

Scene narrations:
{scenes}

Research sources:
{sources}

Check each scene's central claims. Flag ONLY real problems:
- a fact that CONTRADICTS the research sources
- a specific claim (date, number, name) the sources don't support AND that sounds wrong or invented
- a citation that looks fabricated (not from the sources, not a plausible researcher + year)
- a famous psychology myth presented as fact (10% of the brain, learning styles, left/right-brained, subliminal advertising works) — UNLESS the script itself is debunking it
- an effect presented as solid when the sources say it failed replication

Do NOT flag style, wording, claims too general to check, or harmless paraphrases.

Respond with strict JSON only: {{"ok": true, "problems": []}} — or ok false with up to 4 problems, each under 25 words, like "Scene 4: the sources say 75% conformed at least once, not 75% every round"."""


def _fact_check(script, research):
    """Gate 3: script claims vs the grounded research it was written
    from. Returns (ok, problems). Fails OPEN — a checker outage must not
    stop production (and the research pass itself already failed open
    upstream, which just means this draft ships unchecked)."""
    try:
        scenes = "\n\n".join(
            f"[{i + 1}] {s.get('narration', '')}"
            + (f"  (claimed source: {s['source']})"
               if s.get("source") else "")
            for i, s in enumerate(script.get("scenes", [])))[:5000]
        if research and research.get("sources"):
            sources = json.dumps(research["sources"],
                                 ensure_ascii=False)[:3500]
        else:
            sources = ("(none — the research pass failed; flag only "
                       "internally impossible specifics and citations "
                       "that look invented)")
        r = gemini(FACTCHECK_PROMPT.format(scenes=scenes, sources=sources))
        if r.strip().startswith("```"):
            r = r.split("```")[1]
            if r.strip().startswith("json"):
                r = r[4:]
        d = json.loads(r)
        problems = [str(p)[:160] for p in (d.get("problems") or [])][:4]
        if d.get("ok", True) and not problems:
            return True, []
        return False, problems or ["checker flagged issues without specifics"]
    except Exception as e:
        comms.log(f"fact check failed (gate open): {str(e)[:60]}")
        return True, []


# Quality bars (2026-09 "quality-first" switch): a script must clear all
# three gates before a render is spent on it. Both scorers fail OPEN at
# 75 ("unscorable" on an API hiccup must never block production), so 75
# is also the hook bar: an unscoreable hook passes, a real 74 does not.
HOOK_MIN = 75
RETENTION_MIN = 70
QA_ATTEMPTS = 3


def generate_script(direction=None):
    """Quality-gated script factory: research → write → three gates
    (hook strength, retention structure, fact check). Up to 3 attempts,
    best script survives. Gates fail OPEN on API hiccups (a dead scorer
    can't veto the day's video) but fail LOUD on real problems — the
    warnings ride along on the script as _qa and surface in the queue
    message, so a below-bar video is the owner's informed call, never a
    silent one."""
    guidance = state.STATE.get("topic_direction", "")
    if direction:
        # Owner's topic override (/idea, panel box): the topic is FIXED,
        # but the growth analysis's style/format lessons still apply —
        # they were earned from retention data, not topic taste, and
        # dropping them would let a custom video repeat the channel's
        # old mistakes.
        d = direction
        if guidance:
            d += (f"\n(The topic above was requested by the channel owner "
                  f"— research and write THIS topic, never substitute. The "
                  f"channel's style and format lessons still apply: "
                  f"{guidance})")
    else:
        d = guidance or MIND_TRICKS_SEED
    used = ", ".join(state.STATE.get("used_topics", [])[-40:]) or "none yet"
    research = _research(d, used)
    feedback = None          # last attempt's problems, fed to the next draft
    best, best_q = None, -1
    for _ in range(QA_ATTEMPTS):
        script = _write_script(d, research, feedback=feedback)
        if not script:
            continue
        hook, hook_reason = _score_hook(script)
        ret, ret_reason = _score_retention(script)
        facts_ok, problems = _fact_check(script, research)
        state.STATE.setdefault("hook_scores", []).append(
            {"id": script.get("id"), "score": hook, "reason": hook_reason})
        state.STATE.setdefault("retention_scores", []).append(
            {"id": script.get("id"), "score": ret, "reason": ret_reason})
        del state.STATE["hook_scores"][:-200]
        del state.STATE["retention_scores"][:-200]
        state.save_soon()
        comms.log(f"QA: hook {hook} ({hook_reason}) · retention {ret} "
                  f"({ret_reason}) · facts {'ok' if facts_ok else 'flagged'}")
        script["_qa"] = {"hook": hook, "hook_reason": hook_reason,
                         "retention": ret, "retention_reason": ret_reason,
                         "facts": problems}
        # a script is as good as its weakest gate; fact flags cost extra
        q = min(hook, ret) - (15 if problems else 0)
        if q > best_q:
            best, best_q = script, q
        if hook >= HOOK_MIN and ret >= RETENTION_MIN and not problems:
            _pick_thumbnail(script)
            return script
        feedback = (problems[:4] if problems else
                    [f"hook weakest at: {hook_reason}",
                     f"retention weakest at: {ret_reason}"])
    if best:
        _pick_thumbnail(best)
    return best


THUMB_PROMPT = """Design the YouTube thumbnail for this video.

Title: "{title}"
Hook (opening narration): "{hook}"

A thumbnail is NOT the title repeated — it is one image plus a few words that make a scrolling stranger NEED to know. Draft 3 DIFFERENT concepts. Each:
- "text": 2-4 WORDS, all-caps, punchy — opens a curiosity gap TOGETHER WITH the title (never repeats the title, never a sentence)
- "concept": one line describing the picture — for this channel (mind tricks), a HUMAN ELEMENT wins: a face mid-reaction, a pair of eyes, hands mid-trick, someone choosing between options. One focal point, high contrast, filmable, readable at phone size
- "score": 0-100, on curiosity gap with the title, readability at small size, and image concreteness

Respond with strict JSON only: {{"concepts": [{{"text": "...", "concept": "...", "score": 0}}, exactly 3]}}"""


def _pick_thumbnail(script):
    """Pick the thumbnail's overlay text + background concept (best of 3
    LLM drafts, scored against the title). Stored on the script; the
    worker's make_thumbnail renders it instead of the raw title. Fails
    open — no key means the worker falls back to the title exactly as
    before."""
    try:
        r = gemini(THUMB_PROMPT.format(
            title=script.get("title", "")[:100],
            hook=script.get("hook", "")[:400]))
        if r.strip().startswith("```"):
            r = r.split("```")[1]
            if r.strip().startswith("json"):
                r = r[4:]
        d = json.loads(r)
        ranked = sorted(d.get("concepts", []),
                        key=lambda c: int(c.get("score", 0)), reverse=True)
        best = ranked[0] if ranked else None
        if best and str(best.get("text", "")).strip():
            script["thumbnail"] = {
                "text": str(best["text"]).strip()[:40],
                "concept": str(best.get("concept", "")).strip()[:200],
                # the losing concepts ride along: the thumbnail A/B loop
                # (see _thumb_check) swaps one in when this one underperforms
                "alternates": [
                    {"text": str(c.get("text", "")).strip()[:40],
                     "concept": str(c.get("concept", "")).strip()[:200]}
                    for c in ranked[1:]
                    if str(c.get("text", "")).strip()][:2]}
            comms.log(f"thumbnail concept: '{script['thumbnail']['text']}'")
            return script["thumbnail"]
    except Exception as e:
        comms.log(f"thumbnail concepts failed (fallback to title): "
                  f"{str(e)[:60]}")
    return None


def _salvage_json(t):
    """Recover a script object from a chatty/loose/truncated model reply.
    groq (the fallback when the flash geminis 503) frequently emits raw
    newlines inside string values or gets cut off at the token cap, which
    made json.loads die with 'Unterminated string' and FAIL the whole
    render. Three passes: strict, control-char-tolerant, then a truncation
    repair that trims to the last complete element and closes open
    brackets. Returns a dict or None."""
    if "{" in t and "}" in t:
        t = t[t.index("{"): t.rindex("}") + 1]
    for kw in ({}, {"strict": False}):
        try:
            d = json.loads(t, **kw)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    # truncation repair: walk the text tracking string state + bracket
    # depth, remember the last point a bracket closed OUTSIDE a string
    # (a safe cut), then re-close whatever was still open there
    stack, safe_len, safe_stack = [], 0, []
    in_str = esc = False
    for i, c in enumerate(t):
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif in_str:
            pass
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]":
            if stack:
                stack.pop()
            safe_len, safe_stack = i + 1, list(stack)
    head = t[:safe_len].rstrip().rstrip(",")
    if not head:
        return None
    try:
        d = json.loads(head + "".join(reversed(safe_stack)), strict=False)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _parse_script(text, min_scenes=8):
    """JSON text -> script dict, or None (logged). A mini-doc needs its
    acts — a 4-scene stub means the provider squeezed the script (token
    cap or lazy compliance), so reject rather than render a 90-second
    "long-form". Answer-Shorts pass their own floor (4 scenes)."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    script = _salvage_json(t)
    if not isinstance(script, dict):
        comms.log(f"script parse failed: unrecoverable JSON ({text[:50]!r})")
        return None
    try:
        if "id" not in script or "scenes" not in script:
            raise ValueError("missing keys")
        if len(script.get("scenes") or []) < min_scenes:
            raise ValueError(f"too few scenes ({len(script.get('scenes') or [])})"
                             f" for the {min_scenes}+ scene format")
        # every scene must carry what the renderer's validate_script demands
        # (narration + visual_keywords). Catching it HERE routes a malformed
        # scene into the corrective retry below; letting it through queues a
        # job that dies on the runner mid-render (ValueError: "scene N needs
        # 'narration' and 'visual_keywords'") — a whole wasted render slot.
        for i, sc in enumerate(script["scenes"]):
            if not (isinstance(sc, dict) and sc.get("narration")
                    and sc.get("visual_keywords")):
                raise ValueError(f"scene {i} missing narration/visual_keywords")
        return script
    except Exception as e:
        comms.log(f"script parse failed: {e}")
        return None


# "Mind Trick #7: …" prefixes are RETIRED (owner's call — plain titles).
# The regex stays because old titles and provider habits still produce the
# variant; everything downstream strips it instead of stamping it.
SERIES_PREFIX = re.compile(r"(?i)^mind\s*tricks?\s*#?(\d+)\s*[:.\-—–]?\s*")


def _number_title(script, n=None):
    """Strip any 'Mind Trick #N:' series prefix off the title (and the
    alternatives). Kept under the old name so every call site reads the
    same; the number argument is ignored — numbering is gone."""
    del n

    def fix(t):
        return SERIES_PREFIX.sub("", str(t or "").strip())[:100] \
            .rstrip(" :-—–").strip()

    if script.get("title"):
        script["title"] = fix(script["title"])
    if script.get("title_alternatives"):
        script["title_alternatives"] = [
            fix(a) for a in script["title_alternatives"]][:2]
    return script


def _craft_lessons(n=10):
    """Self-improvement: the QA gate flags the SAME craft weaknesses
    (essay tone, flat transitions, weak hooks) script after script, but
    that feedback only lived inside one best-of-3 loop. Read the recent
    SCORED reasons and hand the recurring low-score ones back as STANDING
    guidance, so every new script preemptively fixes the mistakes this
    channel actually makes — the bot learns from itself. Fail-safe: no
    usable history -> empty string (prompt unchanged)."""
    items = _craft_lesson_items(n)
    if not items:
        return ""
    return ("\n\nRECURRING WEAKNESSES the QA gate keeps flagging on this "
            "channel's scripts — fix every one preemptively so it does NOT "
            "happen again:\n- " + "\n- ".join(items))


def _craft_lesson_items(n=10):
    """The distinct recurring low-score QA weaknesses, most recent first
    (also surfaced on the panel so the owner can watch the bot self-correct
    — not just the prompt injection above)."""
    reasons = []
    for key in ("hook_scores", "retention_scores"):
        for r in state.STATE.get(key, [])[-n:]:
            txt = str(r.get("reason", "")).strip()
            try:
                low = int(r.get("score", 100)) < 80
            except (TypeError, ValueError):
                low = False
            if txt and low and "unscorable" not in txt.lower():
                reasons.append(txt)
    seen, distinct = set(), []
    for t in reversed(reasons):          # most recent weaknesses first
        k = t.lower()[:40]
        if k not in seen:
            seen.add(k)
            distinct.append(t)
        if len(distinct) >= 5:
            break
    return distinct


def _write_script(direction=None, research=None, feedback=None):
    direction = (direction or state.STATE.get("topic_direction")
                 or MIND_TRICKS_SEED)
    used = ", ".join(state.STATE.get("used_topics", [])[-40:]) or "none yet"
    prompt = SCRIPT_PROMPT.format(direction=direction, used=used)
    prompt += _craft_lessons()   # standing self-learning from past QA flags
    if feedback:
        # the previous draft failed a quality gate — name the problems so
        # the rewrite fixes them instead of re-rolling the same weaknesses
        prompt += ("\n\nYOUR PREVIOUS DRAFT FAILED QUALITY REVIEW for these "
                   "exact reasons — fix every one in this rewrite:\n- "
                   + "\n- ".join(str(f) for f in feedback[:4]))
    if research:
        # the script is written FROM grounded research, not memory:
        # scene 'source' fields must cite these; facts must not contradict
        prompt += ("\n\nGROUNDED RESEARCH on the topic (write the script "
                   "FROM these facts; scene 'source' fields cite them; do "
                   "not contradict them):\n"
                   + json.dumps(research.get("sources", []),
                                ensure_ascii=False)[:3500]
                   + "\nFresh angle: " + str(research.get("angle", ""))[:300]
                   + "\nSaturation: "
                   + str(research.get("saturation_note", ""))[:200])
    text = gemini(prompt, gemini_models=SCRIPT_GEMINI_MODELS)
    if not text:
        return None          # the whole chain is silent — no nudge will help
    script = _parse_script(text)
    if script:
        return _number_title(script)

    # a stub script (the usual parse failure) gets ONE corrective retry
    # that names the shortfall — a blind re-roll rolls the same dice on
    # the same overloaded provider, but "you wrote 4 scenes, the format
    # needs 8-12" snaps a lazy or squeezed model out of it.
    text = gemini(prompt + ("\n\nIMPORTANT: your previous attempt failed the "
                            "format check. It must be a COMPLETE script with "
                            "8-12 full scenes (each with narration, source, "
                            "archive_search, visual_keywords) — not a summary "
                            "or an outline. Never cut the scene list short."),
                  gemini_models=SCRIPT_GEMINI_MODELS)
    script = _parse_script(text)
    return _number_title(script) if script else None


# YouTube's API budget: 10,000 units/day, every videos.insert costs 1,600,
# so ~6 uploads a day is the hard ceiling. The window resets 07:00 UTC
# (13:00 Dhaka). Blowing it means every upload 403s until the reset —
# with no signal until a whole render has been wasted.
QUOTA_MAX_UPLOADS = 6


def _quota_window_start():
    """Epoch moment the current YouTube quota window opened (13:00 Dhaka
    = 07:00 UTC; before 13:00 we are still in yesterday's window)."""
    bd = datetime.now() + config.BD_OFFSET
    start = bd.replace(hour=13, minute=0, second=0, microsecond=0)
    if bd < start:
        start -= timedelta(days=1)
    return (start - config.BD_OFFSET).timestamp()


def _quota_used():
    """Uploads made inside the current quota window. Finished jobs carry
    their uploaded URLs in result.video_urls, and `updated` is the epoch
    the report landed — a done job stamped inside the window spent its
    inserts inside the window."""
    start = _quota_window_start()
    used = 0
    for j in state.STATE.get("jobs", []):
        if j.get("status") != "done" or (j.get("updated") or 0) < start:
            continue
        r = j.get("result") or {}
        urls = (r.get("video_urls")
                or ([r["video_url"]] if r.get("video_url") else []))
        used += len(urls)
    return used


def quota_full():
    return _quota_used() >= QUOTA_MAX_UPLOADS


def queue_script(script, approval_id=None):
    """Queue a finished script for rendering. Every queue path — the
    scheduler's top-up, the panel, Telegram's /idea, the answer-Shorts —
    goes through here, so the bookkeeping below can never be skipped by
    one of them."""
    payload = {"script": script}
    if approval_id:
        payload["approval_id"] = approval_id
    job = jobs.add_job("render", payload)
    cloud.wake_soon("render", script.get("title", ""))  # names the run too
    state.STATE.setdefault("used_topics", []).append(script.get("id", "?"))
    del state.STATE["used_topics"][:-200]   # only [-40:] is ever read; cap the gist
    state.save_soon()
    return job


def queue_next_video(n=1, direction=None):
    """Generate n scripts and queue render jobs for the cloud worker.
    Quota-aware: if this window's upload budget is spent, the batch is
    deferred (not dropped) and the 13:30 scheduler slot retries it after
    the reset."""
    if quota_full():
        state.STATE["quota_deferred"] = (
            int(state.STATE.get("quota_deferred") or 0) + n)
        state.save_soon()
        comms.send(f"⏸️ <b>Upload quota full</b> — {n} video"
                   f"{'s' if n != 1 else ''} deferred to after the 13:00 "
                   f"reset (YouTube's daily API budget). The retry is "
                   f"automatic.", html=True)
        return 0
    made = 0
    for _ in range(n):
        script = generate_script(direction)
        if not script:
            continue
        job = queue_script(script)
        qa = script.get("_qa") or {}
        warns = []
        if qa.get("hook", 100) < HOOK_MIN:
            warns.append(f"hook {qa['hook']}")
        if qa.get("retention", 100) < RETENTION_MIN:
            warns.append(f"retention {qa['retention']}")
        if qa.get("facts"):
            warns.append(f"{len(qa['facts'])} fact flag"
                         f"{'s' if len(qa['facts']) != 1 else ''}")
        msg = (f"🎬 <b>New video queued</b> — {comms.esc(script['title'])}\n"
               f"({len(script['scenes'])} scenes, job <code>{job['id']}</code>)")
        if warns:
            # below the quality bar after 3 drafts — shipped anyway (the
            # queue must never starve), but never silently
            msg += (f"\n⚠️ <b>Passed with warnings</b> — {', '.join(warns)}"
                    + (f". Top flag: {comms.esc(qa['facts'][0])}"
                       if qa.get("facts") else ""))
        if script.get("thumbnail"):
            msg += (f"\n🖼 Thumbnail text: "
                    f"<b>{comms.esc(script['thumbnail']['text'])}</b>")
        msg += (f'\n🔗 <a href="{comms.panel_link("#script-" + job["id"])}">'
                f"Read the script</a> · "
                f'<a href="{comms.panel_link("#dec")}">decide it</a>')
        comms.send(msg, html=True)
        made += 1
    return made


# ---------------------------------------------------------------------------
# daily stats + analysis
# ---------------------------------------------------------------------------

def daily_report():
    try:
        ch = yt.channel_stats()
        videos = yt.my_videos()
    except Exception as e:
        comms.send(f"⚠️ <b>Stats check failed</b>\n{comms.esc(e)}", html=True)
        return

    today = (datetime.now() + config.BD_OFFSET).strftime("%Y-%m-%d")
    history = state.STATE.setdefault("stats_history", [])
    # newest close BEFORE today — iterating history forwards picked its
    # oldest row instead, so the report's "+N" was growth since tracking
    # began, not since yesterday. Same family as the panel's Desk delta.
    yesterday = next((h for h in reversed(history)
                      if h.get("date") != today),
                     history[-1] if history else None)
    d_subs = ch["subs"] - yesterday["subs"] if yesterday else 0
    d_views = ch["views"] - yesterday["views"] if yesterday else 0

    # record today
    history[:] = [h for h in history if h.get("date") != today]
    history.append({"date": today, "subs": ch["subs"], "views": ch["views"]})
    del history[:-120]
    state.save_soon()

    pub = [v for v in videos if v["privacy"] != "private"] or videos
    best = max(pub, key=lambda v: v["views"], default=None)

    lines = [f"📊 <b>Daily report — {comms.esc(ch['title'])}</b>",
             f"Subs: <b>{ch['subs']:,}</b> ({d_subs:+d}) • "
             f"Total views: <b>{ch['views']:,}</b> ({d_views:+d}) • "
             f"Videos: {ch['videos']}"]
    if best:
        lines.append(f"Top video: {comms.esc(best['title'][:60])} "
                     f"— {best['views']:,} views")
    lines += _retention_lines(pub)
    wants = [f"{r['topic']} ({r['count']})"
             for r in state.STATE.get("audience_requests", [])
             if r.get("count", 0) >= 2][:3]
    if wants:
        lines.append("💬 Asked for: " + comms.esc(", ".join(wants)))
    comms.send("\n".join(lines), html=True)
    # the learning loops' fuel: per-video daily views (best-hour learner)
    record_video_history(pub)
    learn_best_hour(pub)
    # scene-aware retention: harvest timelines from any newly published
    # long-forms' chapters, then map curves onto them (own message only
    # when a finding exists — never raises, never costs the report)
    backfill_timelines(pub)
    scene_retention_pass(pub)


def _retention_lines(pub):
    """Analytics lines for the daily report: average retention split by
    format, plus the best-retaining long-form. Never raises — a missing
    analytics consent costs the report nothing (the one-time setup hint
    goes out through _maybe_nag_analytics)."""
    try:
        report, reason = yt_analytics.video_report(days=28)
    except Exception as e:
        comms.log(f"analytics snapshot failed: {e}")
        return []
    _maybe_nag_analytics(reason)
    merged = [(v, report[v["id"]]) for v in pub if v.get("id") in report]
    if not merged:
        return []
    lines = []
    longs = [(v, a) for v, a in merged if v.get("duration_s", 0) >= 180]
    shorts = [(v, a) for v, a in merged if v.get("duration_s", 0) < 180]
    if longs:
        avg = sum(a["avg_pct"] for _, a in longs) / len(longs)
        mins = sum(a["minutes"] for _, a in longs)
        best = max(longs, key=lambda x: x[1]["avg_pct"])
        lines.append(f"Retention (longs): <b>{avg:.0f}%</b> avg • "
                     f"{mins:,.0f} min watched • best: "
                     f"{comms.esc(best[0]['title'][:40])} "
                     f"({best[1]['avg_pct']:.0f}%)")
    if shorts:
        avg = sum(a["avg_pct"] for _, a in shorts) / len(shorts)
        mins = sum(a["minutes"] for _, a in shorts)
        lines.append(f"Retention (shorts): <b>{avg:.0f}%</b> avg • "
                     f"{mins:,.0f} min watched over {len(shorts)} shorts")
    return lines


def _maybe_nag_analytics(reason):
    """One Telegram hint per distinct blocker — never daily spam, and
    never a repeat once the owner has fixed it (the reason stops
    appearing). Clearing the flag on ok/empty makes a REGRESSION (token
    swapped back) visible again."""
    if reason in ("ok", "empty"):
        state.STATE.setdefault("analytics_nag", {}).clear()
        return
    nags = state.STATE.setdefault("analytics_nag", {})
    if nags.get(reason):
        return
    nags[reason] = True
    state.save_soon()
    if reason == "scope":
        comms.send(
            "📉 <b>Analytics loop needs a one-time re-consent</b>\n"
            "Retention &amp; watch-time data needs its own YouTube "
            "permission. On the PC, run extract_refresh_token.py "
            "(updated — it now asks for analytics), then replace "
            "YT_REFRESH_TOKEN on Render. Reports keep working without "
            "it in the meantime.", html=True)
    elif reason == "disabled":
        comms.send(
            "📉 <b>Analytics: enable the API</b>\n"
            "console.cloud.google.com → APIs &amp; Services → Library → "
            "enable <b>YouTube Analytics API</b> (same project as the "
            "upload credentials).", html=True)


def _plan_rows(videos, report):
    """One line per video for the strategist prompt. With analytics,
    each line carries retention and watched-minutes — the numbers that
    say which FORMAT works, not just which video got lucky with views.
    Pure function: testable without the API."""
    rows = []
    for v in videos[:20]:
        base = (f"- {v['title']} | {v['views']} views | {v['likes']} likes "
                f"| {v['comments']} comments | published {v['published']}"
                f" | {max(v.get('duration_s', 0) // 60, 1)} min")
        a = report.get(v["id"])
        if a:
            extra = (f" | {a['avg_pct']:.0f}% retained"
                     f" | {a['minutes']:.0f} min watched"
                     f" | +{a['subs_gained']} subs")
            if "ctr" in a:
                extra += f" | CTR {a['ctr'] * 100:.1f}%"
            base += extra
        rows.append(base)
    return rows


def analyze_and_plan():
    """The core growth loop: what worked → what to make next."""
    try:
        videos = [v for v in yt.my_videos() if v["privacy"] != "private"]
    except Exception as e:
        comms.log(f"plan: yt failed {e}")
        return
    if len(videos) < 3:
        state.STATE["topic_direction"] = (
            "Channel is new — " + MIND_TRICKS_SEED)
        state.save_soon()
        queue_next_video(1)
        return

    # the learning loop's fuel: retention > views. A missing analytics
    # consent just means the plain rows (degrades to the old behavior).
    try:
        report, reason = yt_analytics.video_report(days=28)
    except Exception as e:
        comms.log(f"plan: analytics failed {e}")
        report, reason = {}, "other"
    _maybe_nag_analytics(reason)
    summary = "\n".join(_plan_rows(videos, report))
    has_retention = bool(report)
    backfill_timelines(videos)   # idempotent — in case the report missed
    scene_lessons = _scene_lesson_lines()
    audience = _audience_lines()
    guidance = gemini(f"""Channel stats for our mind-tricks channel
(dark psychology, persuasion tactics, brain glitches):

{PIVOT_NOTE}

{summary}

{("SCENE-LEVEL RETENTION FINDINGS — mapped from YouTube's retention curve onto each film's scenes. This is the strongest evidence you have; make the pacing/structure guidance concrete and cite these findings:" + chr(10) + chr(10).join(scene_lessons)) if scene_lessons else ""}

{("AUDIENCE REQUESTS — topics viewers explicitly asked for in the comments (count = distinct askers). These are strong candidates when they fit the channel's voice:" + chr(10) + chr(10).join(audience)) if audience else ""}

Analyze like a professional YouTube strategist:
1. Which topics/styles CLEARLY outperform? Which underperform?
2. In one short paragraph, give the topic direction for the next videos.
3. Keep it concise — under 150 words total.
{"RETENTION AND WATCHED-MINUTES ARE INCLUDED — they matter MORE than raw views: a video with fewer views but higher retention is the format to double down on. Call out the ideal video length if the data shows one." if has_retention else "No retention data yet (analytics not connected) — judge by views and engagement."}
Respond with just the analysis and direction, no preamble.""")
    if guidance:
        state.STATE["topic_direction"] = guidance
        state.save_soon()
        comms.send_md(f"🧠 **Growth analysis**\n\n{guidance}")
    queue_next_video(1)


def record_video_history(videos):
    """One views-snapshot row per public video per day — the fuel the
    best-hour learner runs on. Rows are [date, views]; same-day calls
    update in place instead of stacking. Kept bounded: 30 rows per
    video, 60 videos."""
    hist = state.STATE.setdefault("video_history", {})
    today = f"{datetime.now() + config.BD_OFFSET:%Y-%m-%d}"
    changed = False
    for v in videos:
        if v.get("privacy") == "private":
            continue
        rows = hist.setdefault(v["id"], [])
        if not rows or rows[-1][0] != today or rows[-1][1] != v["views"]:
            if rows and rows[-1][0] == today:
                rows[-1][1] = v["views"]
            else:
                rows.append([today, v["views"]])
            changed = True
        del rows[:-30]
    if len(hist) > 60:
        for k in sorted(hist, key=lambda k: hist[k][-1][0])[:-60]:
            del hist[k]
    if changed:
        state.save_soon()


def _hour_buckets(videos, hist, now_bd=None):
    """Group each public video's early velocity (views at the first
    snapshot 3+ days after it went public) by its publish HOUR (BD
    clock). Pure — the learner's math must be checkable offline.
    Returns {hour: [velocities]}."""
    now_bd = now_bd or (datetime.now() + config.BD_OFFSET)
    buckets = {}
    for v in videos:
        ts = v.get("published_ts") or ""
        rows = hist.get(v["id"]) or []
        if not ts or not rows or v.get("privacy") != "public":
            continue
        try:
            # drop the UTC offset before adding BD_OFFSET — mixing an
            # aware pub time with a naive now() raises TypeError
            pub = datetime.fromisoformat(
                ts.replace("Z", "+00:00")).replace(tzinfo=None) \
                + config.BD_OFFSET
        except ValueError:
            continue
        if (now_bd - pub).days < 4:
            continue          # too young for a 3-day reading
        vel = None
        for d, views in rows:
            if (datetime.strptime(d, "%Y-%m-%d") - pub).days >= 3:
                vel = views
                break
        if vel is None:
            continue
        buckets.setdefault(pub.hour, []).append(vel)
    return buckets


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def learn_best_hour(videos=None):
    """The real best-hour learner (was a stub returning the stored 17).
    Median early velocity per publish hour, from the daily per-video
    snapshots; an hour needs 3+ videos before it can win, and it must
    beat the current hour by 20% before the channel moves — one lucky
    video at 2am shouldn't reschedule everything. A move is stored AND
    announced, because every future ✅ silently changes hour with it."""
    cur = int(state.STATE.get("best_hour", 17))
    if videos is None:
        return cur           # nothing to learn from (e.g. tests, offline)
    buckets = _hour_buckets(videos, state.STATE.get("video_history", {}))
    ranked = {h: _median(vs) for h, vs in buckets.items() if len(vs) >= 3}
    if not ranked:
        return cur
    best = max(ranked, key=ranked.get)
    if best == cur:
        return cur
    cur_med = ranked.get(cur)
    if cur_med is None or ranked[best] >= cur_med * 1.2:
        state.STATE["best_hour"] = best
        state.save_soon()
        comms.send(
            f"🕒 <b>Best hour moved: {cur}:00 → {best}:00 BD</b>\n"
            f"Videos published at {best}:00 average "
            f"{ranked[best]:.0f} views in their first days vs "
            f"{cur_med:.0f} at {cur}:00. New approvals schedule there "
            f"from now on.", html=True)
        return best
    return cur


# ---------------------------------------------------------------------------
# scene-aware retention — WHICH scene loses the viewers
# ---------------------------------------------------------------------------

# A curve from a video with a handful of views is noise shaped like a
# curve. Below this many views the mapping is skipped (and retried once
# the count grows).
SCENE_VIEWS_MIN = 50


def parse_chapters(description):
    """Rebuild a scene timeline from a published video's TIMESTAMPS
    block — the renderer writes one into every long-form description,
    so the existing catalogue gets scene analysis without re-rendering.
    Returns [{'id', 'label', 'start', 'end'}] (last end filled by the
    caller from the real duration), or [] when there is nothing usable."""
    scenes = []
    in_block = False
    for line in (description or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.upper() == "TIMESTAMPS":
            in_block = True
            continue
        if not in_block:
            continue
        m = re.match(r"^(\d+):(\d{2})\s+(.+)$", text)
        if not m:
            # a non-chapter line ends the block (hashtags, subscribe
            # lines ride after it in the description)
            if scenes:
                break
            continue
        start = int(m.group(1)) * 60 + int(m.group(2))
        if scenes and start <= scenes[-1]["start"]:
            continue          # YouTube-style junk or a duplicate stamp
        scenes.append({"id": f"chapter{len(scenes)}",
                       "label": m.group(3).strip()[:60],
                       "start": float(start), "end": None})
    return scenes if len(scenes) >= 4 else []


def backfill_timelines(videos):
    """Harvest scene timelines from the descriptions of already-published
    long-forms. Runs alongside the render-time capture ({sid}_timeline.json
    rides the worker report), so both the back catalogue and every new
    video end up in STATE['scene_timelines'] keyed by video id."""
    tls = state.STATE.setdefault("scene_timelines", {})
    made = 0
    for v in videos:
        if v.get("id") in tls or v.get("duration_s", 0) < 180:
            continue
        scenes = parse_chapters(v.get("description", ""))
        if not scenes:
            continue
        scenes[-1]["end"] = float(v["duration_s"])
        tls[v["id"]] = {"title": v["title"], "scenes": scenes,
                        "source": "chapters",
                        "when": datetime.now().strftime("%Y-%m-%d")}
        made += 1
    if len(tls) > 80:      # oldest entries (insertion order) fall off
        for k in list(tls)[:-80]:
            del tls[k]
    if made:
        state.save_soon()
        comms.log(f"scene timelines backfilled from chapters: {made}")
    return made


def classify_scene(drop, step, peak, n_points):
    """One scene's verdict from its curve slice. drop = percentage points
    of the audience lost inside the scene; step = the sharpest single
    point-to-point loss; peak = highest watching share seen inside it
    (>100 means people rewound to rewatch). Thresholds deliberately
    coarse — the numbers are small, the signal is what matters."""
    if n_points < 2:
        return "thin"       # not enough curve inside this scene to judge
    if drop >= 8 or step >= 10:
        return "drop_off"
    if peak >= 105:
        return "rewatch"
    if drop <= 4:
        return "strong_hold"
    return "steady"


def map_scene_retention(scenes, points, duration_s):
    """Project YouTube's retention curve onto a scene timeline. Pure
    function — the math must be checkable offline. The timeline's
    block-math durations get rescaled to the video's REAL duration (the
    render adds an end card and encoder slack), exactly the way the
    chapters in the description already are. Returns
    [{id, label, start, end, drop, step, peak, signal}] for scenes that
    contain at least one curve point."""
    if not scenes or len(points) < 10 or not duration_s:
        return []
    last_end = scenes[-1].get("end") or 0.0
    scale = (duration_s / last_end) if last_end > 0 else 1.0
    out = []
    for i, sc in enumerate(scenes):
        s0 = sc.get("start", 0.0) * scale
        s1 = (sc.get("end") or duration_s) * scale
        inside = [p for p in points
                  if p["elapsed"] * duration_s > s0
                  and (p["elapsed"] * duration_s <= s1
                       or i == len(scenes) - 1)]
        if not inside:
            continue
        drop = 100.0 * (inside[0]["watching"] - inside[-1]["watching"])
        step = max((100.0 * (a["watching"] - b["watching"])
                    for a, b in zip(inside, inside[1:])), default=0.0)
        peak = 100.0 * max(p["watching"] for p in inside)
        out.append({
            "id": sc.get("id", ""), "label": sc.get("label", ""),
            "start": sc.get("start", 0.0), "end": sc.get("end"),
            "drop": round(drop, 1), "step": round(step, 1),
            "peak": round(peak, 1),
            "signal": classify_scene(drop, step, peak, len(inside))})
    return out


def _scene_summary(mapped):
    """The one-line verdicts worth reporting: worst drop-off and strongest
    hold, ignoring 'thin' scenes."""
    scored = [s for s in mapped if s["signal"] != "thin"]
    drop = max((s for s in scored if s["signal"] == "drop_off"),
               key=lambda s: s["drop"], default=None)
    rewatch = max((s for s in scored if s["signal"] == "rewatch"),
                  key=lambda s: s["peak"], default=None)
    hold = max((s for s in scored if s["signal"] == "strong_hold"),
               key=lambda s: -s["drop"], default=None)
    return drop, (rewatch or hold)


def scene_retention_pass(videos):
    """Daily scene-watch: for every long-form with a stored timeline,
    fetch its retention curve, map it onto the scenes, store the
    snapshot. First snapshots with a real finding are reported; refreshes
    (views doubled since last snapshot, at most every 3 days) update
    quietly. Never raises — a refused curve just skips that video."""
    tls = state.STATE.get("scene_timelines", {})
    if not tls:
        return
    snaps = state.STATE.setdefault("scene_retention", {})
    today = datetime.now().strftime("%Y-%m-%d")
    for v in videos:
        vid = v.get("id")
        tl = tls.get(vid)
        if not tl or v.get("duration_s", 0) < 180:
            continue
        views = v.get("views", 0)
        if views < SCENE_VIEWS_MIN:
            continue          # too few viewers for the curve to mean anything
        old = snaps.get(vid)
        if old:
            # refresh only when the audience has meaningfully grown —
            # the curve's shape stabilizes, the refetches must not spam
            days = (datetime.now() - datetime.strptime(
                old.get("when") or "2000-01-01", "%Y-%m-%d")).days
            if not (views >= 2 * old.get("views", 0) and days >= 3):
                continue
        try:
            points, reason = yt_analytics.retention_curve(vid)
        except Exception as e:
            comms.log(f"scene curve failed on {vid}: {str(e)[:60]}")
            continue
        if reason != "ok":
            continue
        mapped = map_scene_retention(tl["scenes"], points,
                                     v["duration_s"])
        if not mapped:
            continue
        snaps[vid] = {"title": v["title"], "scenes": mapped,
                      "views": views, "when": today}
        if len(snaps) > 40:
            for k in list(snaps)[:-40]:
                del snaps[k]
        state.save_soon()
        drop, strong = _scene_summary(mapped)
        if not old and drop:
            # first snapshot with a real drop-off — this is the finding
            # the whole feature exists for
            comms.send(
                f"🔍 <b>Scene watch</b> — {comms.esc(v['title'][:50])}\n"
                f"Viewers left at <b>{comms.esc(drop['label'])}</b> "
                f"(−{drop['drop']:.0f}% of the audience inside that scene"
                + (f"; strongest: {comms.esc(strong['label'])}"
                   if strong else "") + ")",
                html=True)
        elif not old:
            comms.log(f"scene snapshot stored for {vid} (no drop-off "
                      f"above threshold)")


def _scene_lesson_lines():
    """Scene-level evidence for the strategist prompt: concrete findings
    from the stored snapshots, plus one aggregate pacing lesson (how
    long the scenes that LOSE viewers run vs the ones that hold)."""
    snaps = state.STATE.get("scene_retention", {})
    lines = []
    for vid, s in list(snaps.items())[-8:]:
        drop, strong = _scene_summary(s.get("scenes", []))
        if drop:
            lines.append(
                f'- "{s.get("title", "?")[:40]}": viewers left at '
                f'"{drop["label"]}" (lost {drop["drop"]:.0f}% inside '
                f'that scene)'
                + (f'; held best at "{strong["label"]}"'
                   if strong else ""))
    if len(lines) < 2:
        return []
    # aggregate pacing: drop-off scenes vs strong-hold scenes, by length
    def _dur(sc):
        return (sc.get("end") or 0) - (sc.get("start") or 0)
    drops, holds = [], []
    for s in snaps.values():
        for sc in s.get("scenes", []):
            d = _dur(sc)
            if d <= 0:
                continue
            if sc["signal"] == "drop_off":
                drops.append(d)
            elif sc["signal"] == "strong_hold":
                holds.append(d)
    if drops and holds:
        lines.append(
            f"(pattern across films: scenes that lose viewers average "
            f"{sum(drops) / len(drops):.0f}s; scenes that hold average "
            f"{sum(holds) / len(holds):.0f}s — pace future scenes "
            f"accordingly)")
    return lines


def next_publish_time(now_bd=None):
    """The owner's ✅ lands the video in the channel's best hour rather
    than the moment of the tap — YouTube sizes a video's reach from its
    first hours, so those hours should be good ones. Today's slot if
    it's still 45+ minutes out (YouTube rejects a publishAt in the
    past), otherwise tomorrow's. Pure clock math, so it stays testable."""
    now_bd = now_bd or (datetime.now() + config.BD_OFFSET)
    hour = int(state.STATE.get("best_hour", 17))
    slot = now_bd.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot - now_bd < timedelta(minutes=45):
        slot += timedelta(days=1)
    return slot


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------

def comment_sweep():
    if state.STATE["settings"].get("paused"):
        return
    try:
        videos = yt.my_videos()
    except Exception as e:
        comms.log(f"comments: yt failed {e}")
        return
    recent = [v for v in videos if v["privacy"] != "private"][:5]
    handled = state.STATE.setdefault("replied_comments", [])
    try:
        comments = yt.new_comments([v["id"] for v in recent], handled)
    except Exception as e:
        comms.log(f"comment sweep failed: {e}")
        return
    if not comments:
        return
    # audience-demand mining rides the sweep: every NEW comment gets one
    # tiny "is this a topic request?" pass, and repeated asks become
    # planner candidates (see _mine_requests)
    _mine_requests(comments)

    titles = {v["id"]: v["title"] for v in recent}
    shown = 0
    for c in comments:
        if shown >= 5:
            break
        if c["published"] < (datetime.now() + config.BD_OFFSET
                             - timedelta(days=3)).strftime("%Y-%m-%d"):
            handled.append(c["comment_id"])  # old, skip silently
            continue
        draft = gemini(
            f"Draft a short YouTube comment reply (1-2 sentences, warm, "
            f"grateful, natural — no emojis spam, no salesy tone) to this "
            f"comment on our facts video:\n\n"
            f'"{c["text_plain"]}"\n\nReply only with the reply text.')
        if not draft:
            continue
        draft = draft.strip().strip('"')
        uid = c["comment_id"][:12]
        state.STATE["pending_replies"][uid] = {
            "comment_id": c["comment_id"], "draft": draft[:900]}
        comms.send_buttons(
            f"💬 <b>{comms.esc(c['author'])}</b> commented:\n"
            f"{comms.esc(c['text'][:200])}\n\n"
            f"💬 <b>Suggested reply:</b>\n{comms.esc(draft)}",
            [[("✅ Post reply", f"r:{uid}")],
             [("❌ Skip", f"rx:{uid}")]])
        shown += 1
    del handled[:-400]
    state.save_soon()


def post_reply(uid):
    p = state.STATE["pending_replies"].pop(uid, None)
    if not p:
        return "Already handled."
    try:
        yt.reply_to_comment(p["comment_id"], p["draft"])
        state.STATE.setdefault("replied_comments", []).append(p["comment_id"])
        state.save_soon()
        return "Reply posted ✅"
    except Exception as e:
        state.STATE["pending_replies"][uid] = p  # restore for retry
        return f"Failed: {e}"


# ---------------------------------------------------------------------------
# audience-demand mining — the comments are free topic research
# ---------------------------------------------------------------------------

MINE_PROMPT = """A viewer comment on a mind-tricks YouTube channel (dark psychology, persuasion tactics, brain glitches).

Comment: "{text}"

Does the viewer explicitly ask for a specific topic or video ("do a video on X", "please cover X", "more about X")? Reply with strict JSON only: {{"topic": "<the topic in 2-5 words>"}}, or {{"topic": null}} if it is not a request."""


def _mine_requests(comments):
    """A repeated ask in the comments is the cheapest topic research
    there is. One tiny LLM call per NEW comment (the mined ledger keeps
    it to once ever per comment); each request bumps its counter, and
    the planner sees the whole list every planning run. The second ask
    for the same topic tells the owner — one ask is an anecdote, two is
    a queue. Fails open; a mining hiccup just skips that comment."""
    mined = state.STATE.setdefault("mined_comments", [])
    reqs = state.STATE.setdefault("audience_requests", [])
    today = f"{datetime.now() + config.BD_OFFSET:%Y-%m-%d}"
    for c in comments:
        if c["comment_id"] in mined:
            continue
        mined.append(c["comment_id"])
        topic = None
        try:
            r = gemini(MINE_PROMPT.format(text=c["text_plain"][:300]))
            if r.strip().startswith("```"):
                r = r.split("```")[1]
                if r.strip().startswith("json"):
                    r = r[4:]
            topic = str(json.loads(r).get("topic") or "").strip() or None
        except Exception:
            topic = None       # fail open — not a request as far as we know
        if not topic:
            continue
        key = " ".join(re.findall(r"[a-z0-9]+", topic.lower()))
        if not key:
            continue
        entry = next((e for e in reqs if e.get("key") == key), None)
        if entry:
            entry["count"] += 1
            if c["author"] not in entry.get("askers", []):
                entry.setdefault("askers", []).append(c["author"])
                del entry["askers"][:-8]
            entry["last"] = today
            entry["topic"] = topic[:60]
        else:
            entry = {"topic": topic[:60], "key": key, "count": 1,
                     "askers": [c["author"]], "last": today}
            reqs.append(entry)
            del reqs[:-40]
        if entry["count"] == 2:
            comms.send(f"💬 <b>Viewers keep asking for:</b> "
                       f"{comms.esc(entry['topic'])} — it's on the "
                       f"planner's candidate list now.\n"
                       f'🔗 <a href="{comms.panel_link("#studio")}">Audience '
                       f"requests in the panel</a>", html=True)
        # two asks = a queue: draft the answer Short in the background.
        # Marked answered BEFORE spawning so the third ask can't spawn a
        # second draft; a paused agent or a full quota skips it entirely
        # (the topic stays a planner candidate either way).
        if (entry["count"] >= 2 and not entry.get("answered")
                and not state.STATE["settings"].get("paused")
                and not quota_full()):
            entry["answered"] = True
            import threading
            threading.Thread(target=_queue_answer_short,
                             args=(dict(entry),), daemon=True).start()
        state.save_soon()
    del mined[:-400]


def _audience_lines():
    """The planner's view of audience demand: every request with its
    ask-count. Two-plus asks is a strong candidate; one is a hint."""
    reqs = state.STATE.get("audience_requests", [])
    return [f'- {r.get("topic", "?")} ({r.get("count", 0)} ask'
            f'{"s" if r.get("count", 0) != 1 else ""})'
            for r in reqs if r.get("count")][:8]


# ---------------------------------------------------------------------------
# answer-Shorts — the repeated ask becomes the video
# ---------------------------------------------------------------------------

SHORT_ANSWER_PROMPT = """Write ONE YouTube Short script (45-60 seconds) answering a viewer's request, for a faceless mind-tricks channel (dark psychology, persuasion tactics, brain glitches). Strict JSON only:

{{
  "id": "kebab-case-slug",
  "format": ["short"],
  "title": "<curiosity title under 60 chars>",
  "description": "60-100 words: hook first line, 2-3 sentences of context, one engaging question, then 3-4 hashtags.",
  "tags": ["6-10 tags mixing psychology with the topic"],
  "hook": "20-30 words. Cold open ON the viewer — they DO the trick in the first 5 seconds. No greeting.",
  "scenes": [
    {{"narration": "30-60 words, conversational, second person, present tense.",
      "short_narration": "the same beat as narration, 30-50 words, cut to the bone",
      "short_title": "standalone scene title under 60 chars",
      "archive_search": ["1-3 real archival photo searches"],
      "visual_keywords": ["2-3 stock-footage searches"],
      "source": "short real citation for the scene's central fact",
      "in_short": true}}
  ],
  "outro": "One line, max 10 words — invite the rewatch."
}}

Rules:
- 4-6 scenes, ALL with "in_short": true. Total narration 150-300 words.
- The trick HAPPENS on the viewer (reveal-then-replay: let it land, then explain).
- DEFENSIVE FRAMING: reveal what is done TO people and how to spot it — never a how-to for doing it.
- Facts must be real and named (experiment, researcher, year) when they exist; note replication failures.
- End with a "spot it in the wild" line.

The viewer request to answer: "{topic}"
Avoid these already-used topics: {used}

Return ONLY the JSON object."""


def _write_short(topic):
    """Draft the answer-Short for a repeated viewer request. Lighter than
    the long-form factory: one grounded research pass, one draft, hook +
    fact gates only (retention structure matters less at 45 seconds), and
    a 4-scene floor instead of 8. Returns the script or None."""
    used = ", ".join(state.STATE.get("used_topics", [])[-40:]) or "none yet"
    research = _research(f"Answer this viewer request with one concrete "
                         f"mind trick or brain glitch: {topic}", used)
    prompt = SHORT_ANSWER_PROMPT.format(topic=topic[:200], used=used)
    if research and research.get("sources"):
        prompt += ("\n\nGROUNDED RESEARCH (write from these facts; cite "
                   "them in 'source'):\n"
                   + json.dumps(research["sources"],
                                ensure_ascii=False)[:2500])
    for attempt in range(2):
        text = gemini(prompt, gemini_models=SCRIPT_GEMINI_MODELS)
        script = _parse_script(text, min_scenes=4) if text else None
        if script:
            break
        # one corrective retry — same contract as the long-form writer
        prompt += ("\n\nIMPORTANT: the previous attempt failed the format "
                   "check. It must be a COMPLETE 4-6 scene script, not a "
                   "summary or outline.")
    if not script:
        return None
    script = _number_title(script)
    script["format"] = ["short"]      # the worker renders ONLY the Short
    hook, hook_reason = _score_hook(script)
    facts_ok, problems = _fact_check(script, research)
    script["_qa"] = {"hook": hook, "hook_reason": hook_reason,
                     "retention": None, "retention_reason": "short — skipped",
                     "facts": problems}
    if hook < HOOK_MIN or problems:
        comms.log(f"answer-short QA weak (hook {hook}, "
                  f"{len(problems)} fact flags) — shipping with warnings")
    _pick_thumbnail(script)
    return script


def _queue_answer_short(entry):
    """Background thread: draft + queue the answer-Short for a twice-asked
    viewer request. Called from _mine_requests with a snapshot of the
    entry; failures log quietly (the request stays marked answered so a
    hiccup can't spawn a queue of retries)."""
    topic = str(entry.get("topic") or "").strip()
    if not topic:
        return
    try:
        if quota_full():
            comms.log("answer-short deferred — upload quota full")
            return
        script = _write_short(topic)
        if not script:
            comms.log(f"answer-short for '{topic[:40]}' failed to draft")
            return
        job = queue_script(script)
        comms.send(
            f"🩳 <b>Answer Short queued</b> — {comms.esc(script['title'])}\n"
            f"Viewers asked for {comms.esc(topic)} twice, so it becomes a "
            f"Short ({len(script['scenes'])} scenes, 1 upload).\n"
            f'🔗 <a href="{comms.panel_link("#script-" + job["id"])}">'
            f"Read it</a>", html=True)
    except Exception as e:
        comms.log(f"answer-short thread failed: {str(e)[:80]}")


# ---------------------------------------------------------------------------
# underperformer title optimization
# ---------------------------------------------------------------------------

def title_check():
    """Underperformer retitle — now ACTS instead of asking. A video that
    is 48h+ old, under half the channel median AND under 100 views is
    invisible either way, so the swap is near-zero-stakes: apply it,
    then report loudly with the old title included (one Studio edit to
    undo). One swap per video, ever — the ledger prevents daily
    flip-flopping. The ledger also records the pre-swap CTR/views, and
    _swap_verdicts (below) comes back a week later to say whether the
    swap actually worked."""
    if state.STATE["settings"].get("paused"):
        return
    try:
        videos = [v for v in yt.my_videos() if v["privacy"] != "private"]
    except Exception:
        return
    if len(videos) < 4:
        return
    # one analytics fetch for both the pre-swap snapshot and the verdict
    # pass — a refused fetch just means no CTR context, never a skip
    try:
        report, _reason = yt_analytics.video_report(days=35)
    except Exception:
        report = {}
    views = sorted(v["views"] for v in videos)
    median = views[len(views) // 2]
    cutoff = (datetime.now() + config.BD_OFFSET
              - timedelta(hours=48)).strftime("%Y-%m-%d")
    swaps = state.STATE.setdefault("title_swaps", {})
    for v in videos:
        if v["id"] in swaps:
            continue
        if v["published"] < PIVOT_DATE:
            continue   # pre-pivot back catalogue — legacy, leave it be
        if not (v["published"] <= cutoff and v["views"] < median * 0.5
                and v["views"] < 100):
            continue
        alt = gemini(
            f"This video is underperforming. Current title: "
            f'"{v["title"]}" ({v["views"]} views).\n'
            f"Give me ONE better title — curiosity-gap, under 70 chars, "
            f"honest (no clickbait lies). Respond with the title only.")
        if not alt:
            continue
        alt = alt.strip().strip('"').split("\n")[0][:100]
        if alt.strip().lower() == v["title"].strip().lower():
            continue  # nothing to apply
        # numbered "Mind Trick #N:" prefixes are retired — strip one from
        # the rewrite (and from an already-prefixed title) either way
        alt = _number_title({"title": alt, "title_alternatives": []})["title"]
        if not alt:
            continue
        try:
            yt.update_title(v["id"], alt)
        except Exception as e:
            comms.log(f"title swap failed on {v['id']}: {str(e)[:60]}")
            continue
        a = report.get(v["id"]) or {}
        swaps[v["id"]] = {"from": v["title"], "to": alt,
                          "when": datetime.now().strftime("%Y-%m-%d"),
                          # the A/B baseline: what the old title had
                          "pre_ctr": (round(a["ctr"], 4)
                                      if "ctr" in a else None),
                          "pre_views": v["views"],
                          "verdict": None}
        state.save_soon()
        comms.send(
            f"✏️ <b>Title auto-swapped</b> (underperformer)\n"
            f"from: {comms.esc(v['title'])}\n"
            f"to: {comms.esc(alt)}\n"
            f"{v['views']} views after 48h+ — the new title gets a fresh "
            f"shot at impressions. Undo: YouTube Studio → title.\n"
            f'🔗 <a href="{comms.panel_link("#desk")}">A/B verdicts in the '
            f"panel</a>",
            html=True)
    _swap_verdicts(report)
    _thumb_check(videos, report)
    _thumb_verdicts(report)


def _swap_verdicts(report):
    """A/B-lite for title swaps: a week after a swap, compare CTR (when
    the channel has impressions data) and views, and tell the owner
    whether it worked. Once per swap ever — the verdict marks the
    ledger entry. Views alone can't prove the title caused anything
    (time passes, impressions decay), so without CTR the verdict says
    so instead of pretending."""
    today = datetime.now() + config.BD_OFFSET
    swaps = state.STATE.get("title_swaps", {})
    for vid, sw in swaps.items():
        if sw.get("verdict") is not None:
            continue
        try:
            when = datetime.strptime(sw.get("when") or "", "%Y-%m-%d")
        except ValueError:
            continue
        if (today - when).days < 7:
            continue
        a = report.get(vid) or {}
        post_ctr = a.get("ctr")
        post_views = a.get("views")
        pre_ctr = sw.get("pre_ctr")
        pre_views = sw.get("pre_views", 0)
        parts, verdict = [], None
        if pre_ctr is not None and post_ctr is not None:
            parts.append(f"CTR {pre_ctr * 100:.1f}% → "
                         f"{post_ctr * 100:.1f}%")
            gain = (post_ctr - pre_ctr) / pre_ctr if pre_ctr else 0
            verdict = ("worked — keep it" if gain >= 0.15
                       else "no clear CTR gain — consider reverting")
        if post_views is not None:
            parts.append(f"views {pre_views} → {post_views}")
        if not parts:
            sw["verdict"] = "no data"
            state.save_soon()
            comms.log(f"swap verdict skipped (no analytics) for {vid}")
            continue
        if verdict is None:
            verdict = (f"inconclusive — no CTR data; views moved "
                       f"{pre_views} → {post_views}")
        sw["verdict"] = verdict
        state.save_soon()
        comms.send(f"✏️ <b>Title swap verdict</b> — "
                   f"{comms.esc(sw.get('to', '')[:60])}\n"
                   f"{' · '.join(parts)}\n{comms.esc(verdict)}\n"
                   f'🔗 <a href="{comms.panel_link("#desk")}">All A/B '
                   f"verdicts</a>",
                   html=True)


# ---------------------------------------------------------------------------
# thumbnail A/B — the second experiment surface
# ---------------------------------------------------------------------------

def queue_thumb_test(video, alt, ctr=None):
    """Queue ONE thumbnail swap for a video with a banked alternate — the
    shared engine of the automatic _thumb_check and the panel's manual
    A/B control. Writes the thumb_swaps ledger entry (pre-swap CTR/views,
    the original concept for a revert) so _thumb_verdicts judges it a
    week later. One swap per video ever; a second attempt returns False."""
    bank = state.STATE.setdefault("thumb_bank", {})
    swaps = state.STATE.setdefault("thumb_swaps", {})
    vid = video["id"]
    if vid in swaps:
        return False
    entry = bank.get(vid) or {}
    jobs.add_job("thumb", {
        "video_url": f"https://youtu.be/{vid}",
        "title": video.get("title", ""),
        "thumbnail": alt})
    cloud.wake_soon("render")
    swaps[vid] = {
        "from": entry.get("text", ""), "to": alt.get("text", ""),
        "concept": alt.get("concept", ""),
        "when": datetime.now().strftime("%Y-%m-%d"),
        "pre_ctr": (round(ctr, 4) if ctr is not None else None),
        "pre_views": video.get("views", 0), "verdict": None,
        "video_url": f"https://youtu.be/{vid}",
        "orig": {"text": entry.get("text", ""),
                 "concept": entry.get("concept", "")}}
    state.save_soon()
    return True


def _thumb_check(videos, report):
    """A weak thumbnail on a 7-day-old video is a free swap: the runner
    renders the banked alternate concept and set_thumbnail replaces the
    image (no re-render, ~50 quota units). One swap per video ever, the
    ledger records the pre-swap CTR/views, and _thumb_verdicts comes back
    a week later — a clear loss reverts to the original concept."""
    bank = state.STATE.setdefault("thumb_bank", {})
    swaps = state.STATE.setdefault("thumb_swaps", {})
    views_all = sorted(v["views"] for v in videos)
    median = views_all[len(views_all) // 2] if views_all else 0
    for v in videos:
        if v["id"] in swaps or v["id"] not in bank:
            continue
        if v["published"] < PIVOT_DATE or v["privacy"] == "private":
            continue   # legacy back catalogue / not live
        if (v.get("duration_s") or 0) < 180:
            continue   # Shorts ignore custom thumbnails
        age_days = ((datetime.now() + config.BD_OFFSET
                     - timedelta(days=7)).strftime("%Y-%m-%d"))
        if v["published"] > age_days:
            continue   # too young to judge
        a = report.get(v["id"]) or {}
        ctr = a.get("ctr")
        weak = (v["views"] < median * 0.5
                or (ctr is not None and ctr < 0.04))
        if not weak:
            continue
        entry = bank[v["id"]]
        alts = [x for x in (entry.get("alternates") or [])
                if x.get("text")]
        if not alts:
            continue
        new = alts[0]   # ranked by score at concept time
        if not queue_thumb_test(v, new, ctr):
            continue
        comms.send(
            f"🖼 <b>Thumbnail auto-swapped</b> (underperformer)\n"
            f"{comms.esc(v['title'][:60])}\n"
            f"'{comms.esc(entry.get('text', ''))}' → "
            f"'{comms.esc(new.get('text', ''))}'\n"
            f"A week of data will say whether it worked. "
            f"Undo: YouTube Studio → thumbnail.", html=True)


def _thumb_verdicts(report):
    """The A/B verdict for thumbnail swaps, a week after each one. With
    CTR: a >15% relative gain keeps it, a >15% loss reverts automatically
    (the runner re-sets the original concept — cheaper than a re-render).
    Without CTR the verdict says so instead of guessing."""
    today = datetime.now() + config.BD_OFFSET
    swaps = state.STATE.get("thumb_swaps", {})
    for vid, sw in swaps.items():
        if sw.get("verdict") is not None:
            continue
        try:
            when = datetime.strptime(sw.get("when") or "", "%Y-%m-%d")
        except ValueError:
            continue
        if (today - when).days < 7:
            continue
        a = report.get(vid) or {}
        post_ctr, post_views = a.get("ctr"), a.get("views")
        pre_ctr, pre_views = sw.get("pre_ctr"), sw.get("pre_views", 0)
        parts, verdict = [], None
        if pre_ctr is not None and post_ctr is not None:
            parts.append(f"CTR {pre_ctr * 100:.1f}% → "
                         f"{post_ctr * 100:.1f}%")
            gain = (post_ctr - pre_ctr) / pre_ctr if pre_ctr else 0
            verdict = ("worked — keeping it" if gain >= 0.15
                       else "clear loss — reverting" if gain <= -0.15
                       else "no clear CTR change — keeping it")
        if post_views is not None:
            parts.append(f"views {pre_views} → {post_views}")
        if not parts:
            sw["verdict"] = "no data"
            state.save_soon()
            continue
        if verdict is None:
            verdict = (f"inconclusive — no CTR data; views moved "
                       f"{pre_views} → {post_views}")
        sw["verdict"] = verdict
        if verdict == "clear loss — reverting" and sw.get("orig"):
            jobs.add_job("thumb", {
                "video_url": sw.get("video_url") or "",
                "title": f"revert: {sw.get('to', '')[:60]}",
                "thumbnail": sw["orig"]})
            cloud.wake_soon("render")
        state.save_soon()
        comms.send(f"🖼 <b>Thumbnail swap verdict</b> — "
                   f"'{comms.esc(sw.get('to', '')[:40])}'\n"
                   f"{' · '.join(parts)}\n{comms.esc(verdict)}\n"
                   f'🔗 <a href="{comms.panel_link("#desk")}">All A/B '
                   f"verdicts</a>",
                   html=True)


def apply_title(uid):
    p = state.STATE["pending_titles"].pop(uid, None)
    if not p:
        return "Already handled."
    try:
        yt.update_title(p["video_id"], p["title"])
        state.save_soon()
        return "Title updated ✅"
    except Exception as e:
        state.STATE["pending_titles"][uid] = p
        return f"Failed: {e}"


# ---------------------------------------------------------------------------
# weekly summary
# ---------------------------------------------------------------------------

def weekly_summary():
    history = state.STATE.get("stats_history", [])
    if len(history) < 7:
        comms.send("📈 Weekly summary starts once I have a week of data.")
        return
    week_ago = history[-8] if len(history) >= 8 else history[0]
    latest = history[-1]
    gained = latest["subs"] - week_ago["subs"]
    views_gained = latest["views"] - week_ago["views"]
    summary = gemini(
        f"Our facts channel gained {gained} subscribers and {views_gained} "
        f"views in the last 7 days (now {latest['subs']} subs). Milestone: "
        f"1000 subs for monetization. Give 3 concise strategic priorities "
        f"for next week. Under 120 words.")
    comms.send_md(
        f"📈 **Weekly summary**\n"
        f"Subs: +{gained} (now {latest['subs']:,}) • Views: +{views_gained:,}\n"
        f"Monetization progress: {latest['subs']}/1000\n\n{summary or ''}")
