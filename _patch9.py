# ---- brain.py: short_narration on in_short scenes ----
p = "brain.py"
s = open(p, encoding="utf-8").read()
old = '''  "scenes": [
    {{"narration": "90-140 words, conversational, fast, surprising.",
      "archive_search": ["2-4 real-world archival photo searches"],
      "visual_keywords": ["2-3 stock-footage search phrases (fallback)"],
      "source": "short real citation anchoring this scene's central fact",
      "in_short": true}}
  ],'''
new = '''  "scenes": [
    {{"narration": "90-140 words, conversational, fast, surprising.",
      "short_narration": "ONLY on in_short scenes: 40-60 words — the scene's punchiest core, rewritten tight for the Short",
      "archive_search": ["2-4 real-world archival photo searches"],
      "visual_keywords": ["2-3 stock-footage search phrases (fallback)"],
      "source": "short real citation anchoring this scene's central fact",
      "in_short": true}}
  ],'''
assert old in s
s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("brain.py: short_narration field")

# ---- render_video.py: short uses short_narration when present ----
p = "worker/render_video.py"
s = open(p, encoding="utf-8").read()
old2 = '''        chosen, total = [], audio_durations.get(
            "hookshort", audio_durations.get("hook", 0.0))
        # hard 20s scene budget after the hook: the trimmed hook runs
        # 5-10s, scenes are 8-12s each → 1-2 scenes → 18-25s total.
        # (A softer 25s cap let 4+ scenes stack to 45s+.)
        for idx, s_ in enumerate(scenes):
            d = audio_durations.get(f"scene{idx}", 0)
            if s_.get("in_short", True) and (total + d < 20 or not chosen):
                chosen.append((f"scene{idx}", s_))
                total += d'''
new2 = '''        chosen, total = [], audio_durations.get(
            "hookshort", audio_durations.get("hook", 0.0))
        # Shorts are built from the trimmed hook + the in_short scenes'
        # SHORT narrations (40-60 words each) when present — full scenes
        # run 30-40s and can never fit. Legacy scripts (no
        # short_narration) take at most ONE full scene, budget be damned.
        for idx, s_ in enumerate(scenes):
            if not s_.get("in_short", True):
                continue
            if s_.get("short_narration"):
                key = f"shortscene{idx}"
                if key not in audio:
                    audio[key] = tts_mod.tts(
                        f"{sid}_shortscene{idx}", s_["short_narration"],
                        vconf["long"], vconf.get("rate", "+0%"))
                    durations[key] = AudioFileClip(
                        str(audio[key][0])).duration
                d = durations.get(key, 0)
                if total + d < 20 or not chosen:
                    chosen.append((key, s_))
                    total += d
            else:
                d = audio_durations.get(f"scene{idx}", 0)
                if total + d < 20 or not chosen:
                    chosen.append((f"scene{idx}", s_))
                    total += d'''
assert old2 in s, "short budget block not found"
s = s.replace(old2, new2)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("render_video.py: short_narration support")
