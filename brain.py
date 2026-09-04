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

SYSTEM = ("You are the growth manager of FOOTNOTE — a faceless YouTube "
          "facts/mystery channel. The brand idea: every video is the "
          "footnote everyone skipped — the tiny detail that changes the "
          "whole story. Voice: literate, precise, quietly unsettling. "
          "Titles hint at the impossible but never lie. Every fact must "
          "be true and verifiable. Never suggest fake growth tactics "
          "(sub4sub, spam, bought views) — they get channels terminated.")


def gemini(prompt, system=SYSTEM):
    """One LLM call through the provider chain (Gemini → Groq → OpenRouter)."""
    return llm.complete(prompt, system=system)


# ---------------------------------------------------------------------------
# script generation (same JSON schema as the pipeline queue)
# ---------------------------------------------------------------------------

SCRIPT_PROMPT = """Write ONE video script for a faceless YouTube facts/mystery channel, as strict JSON only (no markdown, no commentary):

{{
  "id": "kebab-case-topic-slug",
  "format": ["short", "long"],
  "title": "Curiosity-gap title under 70 chars",
  "description": "Full YouTube description: 120-200 words. First 1-2 lines = a hook that sells the click (this text shows in search results). Then 2-3 short paragraphs of context that tease the mystery WITHOUT spoiling the answer. End with an engaging question, then a line of 4-6 hashtags relevant to THIS topic (like #unsolvedmystery #truehistory #didyouknow).",
  "tags": ["8-14 specific tags: mix broad (facts, mystery) and topic-specific] ,
  "hook": "80-120 words. A cinematic COLD-OPEN vignette: drop the viewer INTO the single most striking moment of the story (a date, a place, a person mid-crisis). No greeting, no channel intro, no context. End on the framing question the whole video answers.",
  "scenes": [
    {{"narration": "90-140 words, conversational, fast, surprising.",
      "visual_keywords": ["2-3 stock-footage search phrases"],
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
- Mark the 3 most visual scenes "in_short": true. All facts true, verifiable,
  and specific (dates, numbers, names).

CRITICAL — visual_keywords decide the stock footage shown during each scene. They are searched on stock-video sites (Pexels), so they must be phrased as searches that RETURN RESULTS there:
- Describe what the narration literally mentions, in filmable terms: a person,
  object, place, or action a camera can point at. NEVER abstract words
  ("mystery", "history", "time", "facts", "story", "secret").
- Use "concrete subject + common visual" phrasing that stock libraries stock:
  "mans leather shoes closeup", "beach night waves", "old suitcase dark room",
  "vintage newspaper printing press". NOT hyper-specific proper nouns that
  return zero results ("Strasbourg 1518 street") — drop the proper noun,
  keep the visual ("medieval cobblestone street").
- VARIETY IS MANDATORY: no keyword may repeat across scenes, and no two
  scenes may share more than one keyword. Each scene's footage must look
  different from the previous scene's. Aim for wide variety: people, objects,
  places, closeups, wide shots, day, night.
- If a concept is abstract, film its concrete consequence: for "hysteria
  spreading" use "panicked crowd running"; for "no explanation" use
  "empty foggy road night".

Topic guidance from the channel's growth analysis: {direction}
Avoid these already-used topics: {used}

Return ONLY the JSON object."""


VIRALITY_PROMPT = """Score this video hook (the opening narration of a YouTube facts/mystery video) from 0-100 for its ability to stop a scroll and hold attention through the first 30 seconds.

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
    spent on it. Fails OPEN (75/unscorable) — the gate must never block
    production on its own API hiccup."""
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
        return 75, "unscorable"


def generate_script(direction=None):
    """Write one script, then QA the hook: below 70 → one regeneration,
    keep the better script. Scores are kept in the gist so the future
    analytics loop can correlate hook style with retention."""
    best = None
    best_score = -1
    for attempt in range(2):
        script = _write_script(direction)
        if not script:
            continue
        score, reason = _score_hook(script)
        state.STATE.setdefault("hook_scores", []).append(
            {"id": script.get("id"), "score": score, "reason": reason})
        del state.STATE["hook_scores"][:-200]
        state.save_soon()
        comms.log(f"hook score {score}: {reason}")
        if score > best_score:
            best, best_score = script, score
        if score >= 70:
            return script  # good enough — skip the second attempt
    return best


def _write_script(direction=None):
    direction = direction or state.STATE.get("topic_direction") or (
        "unsolved mysteries, strange science, history they never taught you")
    used = ", ".join(state.STATE.get("used_topics", [])[-40:]) or "none yet"
    text = gemini(SCRIPT_PROMPT.format(direction=direction, used=used))
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        script = json.loads(text)
        if "id" not in script or "scenes" not in script:
            raise ValueError("missing keys")
        # a mini-doc needs its acts — a 4-scene stub means the provider
        # squeezed the script (token cap or lazy compliance). Reject and
        # retry rather than render a 90-second "long-form"
        if len(script["scenes"]) < 8:
            raise ValueError(f"too few scenes ({len(script['scenes'])}) "
                             f"for the 8-12 min format")
        return script
    except Exception as e:
        comms.log(f"script parse failed: {e}")
        return None


def queue_next_video(n=1, direction=None):
    """Generate n scripts and queue render jobs for the PC worker."""
    made = 0
    for _ in range(n):
        script = generate_script(direction)
        if not script:
            continue
        job = jobs.add_job("render", {"script": script})
        cloud.wake_soon("render")  # cloud runner starts within seconds
        state.STATE.setdefault("used_topics", []).append(script.get("id", "?"))
        state.save_soon()
        comms.send(f"🎬 <b>New video queued</b> — {comms.esc(script['title'])}\n"
                   f"({len(script['scenes'])} scenes, job <code>{job['id']}</code>)",
                   html=True)
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
    yesterday = next((h for h in history if h.get("date") != today),
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
    comms.send("\n".join(lines), html=True)


def analyze_and_plan():
    """The core growth loop: what worked → what to make next."""
    try:
        videos = [v for v in yt.my_videos() if v["privacy"] != "private"]
    except Exception as e:
        comms.log(f"plan: yt failed {e}")
        return
    if len(videos) < 3:
        state.STATE["topic_direction"] = (
            "Channel is new — focus on high-curiosity evergreen topics: "
            "unsolved mysteries, space anomalies, human body oddities, "
            "history they never taught you.")
        state.save_soon()
        queue_next_video(1)
        return

    summary = "\n".join(f"- {v['title']} | {v['views']} views | "
                        f"{v['likes']} likes | {v['comments']} comments | "
                        f"published {v['published']}"
                        for v in videos[:20])
    guidance = gemini(f"""Channel stats for our facts/mystery channel:

{summary}

Analyze like a professional YouTube strategist:
1. Which topics/styles CLEARLY outperform? Which underperform?
2. In one short paragraph, give the topic direction for the next videos.
3. Keep it concise — under 150 words total.
Respond with just the analysis and direction, no preamble.""")
    if guidance:
        state.STATE["topic_direction"] = guidance
        state.save_soon()
        comms.send_md(f"🧠 **Growth analysis**\n\n{guidance}")
    queue_next_video(1)


def learn_best_hour():
    """From stats history, when do videos posted at hour H get the most
    next-day views? Fallback: keep 17:00. (Data accumulates over weeks.)"""
    # simple version for now — history-based refinement can be added once
    # the channel has 2+ weeks of data
    return state.STATE.get("best_hour", 17)


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
# underperformer title optimization
# ---------------------------------------------------------------------------

def title_check():
    if state.STATE["settings"].get("paused"):
        return
    try:
        videos = [v for v in yt.my_videos() if v["privacy"] != "private"]
    except Exception:
        return
    if len(videos) < 4:
        return
    views = sorted(v["views"] for v in videos)
    median = views[len(views) // 2]
    cutoff = (datetime.now() + config.BD_OFFSET
              - timedelta(hours=48)).strftime("%Y-%m-%d")
    weak = [v for v in videos
            if v["published"] <= cutoff and v["views"] < median * 0.5
            and v["views"] < 100]
    for v in weak[:2]:
        alt = gemini(
            f"This video is underperforming. Current title: "
            f'"{v["title"]}" ({v["views"]} views).\n'
            f"Give me ONE better title — curiosity-gap, under 70 chars, "
            f"honest (no clickbait lies). Respond with the title only.")
        if not alt:
            continue
        alt = alt.strip().strip('"').split("\n")[0][:100]
        uid = v["id"][:12]
        state.STATE["pending_titles"][uid] = {
            "video_id": v["id"], "title": alt}
        comms.send_buttons(
            f"📉 <b>Underperforming video</b>\n"
            f"{comms.esc(v['title'])} — only {v['views']} views\n\n"
            f"✏️ <b>Suggested new title:</b>\n{comms.esc(alt)}",
            [[("✅ Update title", f"t:{uid}")],
             [("❌ Keep", f"tx:{uid}")]])


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
