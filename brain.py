"""The growth brain: Gemini-powered analysis, script generation, daily
reports, comment replies, and title optimization. What a real channel
operator would do — data-driven, honest, no fake engagement."""

import json
import re
from datetime import datetime, timedelta

import comms
import config
import jobs
import llm
import state
import yt

SYSTEM = ("You are the growth manager of ALLEGEDLY — a faceless YouTube "
          "facts/mystery channel with a declassified-newspaper brand voice: "
          "confident, wry, precise. Titles hint at the impossible but never "
          "lie. Every fact must be true and verifiable. Never suggest fake "
          "growth tactics (sub4sub, spam, bought views) — they get channels "
          "terminated.")


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
  "description": "2-3 sentence YouTube description ending with a question",
  "tags": ["facts", ...8-12 tags...],
  "hook": "First 8 seconds of narration. Shocking claim or question. Max 25 words.",
  "scenes": [
    {{"narration": "35-60 words, conversational, fast, surprising.",
      "visual_keywords": ["2-3 stock-footage search phrases like 'ocean aerial drone'"],
      "in_short": true}}
  ],
  "outro": "One-line call to action, max 12 words."
}}

Rules: 7-9 scenes. Mark the 3 most visual scenes "in_short": true. All facts true and verifiable.

Topic guidance from the channel's growth analysis: {direction}
Avoid these already-used topics: {used}

Return ONLY the JSON object."""


def generate_script(direction=None):
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
        comms.send_md(f"🧠 <b>Growth analysis</b>\n\n{guidance}", html=True)
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
        f"📈 <b>Weekly summary</b>\n"
        f"Subs: +{gained} (now {latest['subs']:,}) • Views: +{views_gained:,}\n"
        f"Monetization progress: {latest['subs']}/1000\n\n{summary or ''}",
        html=True)
