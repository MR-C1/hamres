"""Offline self-test for the 2026-09 quality-first package — no network,
no keys. Fakes brain.gemini/_research so generate_script runs its full
gate loop end to end:

  * a clean draft (hook >= 75, retention >= 70, facts ok) passes on
    attempt 1 and carries its _qa + thumbnail concept
  * a weak hook regenerates — and the rewrite prompt NAMES the problem
    ("FAILED QUALITY REVIEW"), not just re-rolls
  * fact-check problems are fed verbatim into the rewrite
  * three failed attempts -> best of 3 ships WITH warnings attached
    (fail loud, never silent, never starves the queue)
  * every scorer fails OPEN: junk responses score 75 and pass
  * _pick_thumbnail picks the highest-scoring concept, fails open to None
  * next_publish_time: today's best hour if >= 45 min out, else
    tomorrow's
  * title_check ACTS on underperformers (auto-swap + loud report), and
    the one-swap-per-video ledger stops a second pass
  * queue_next_video's message carries the warnings and thumbnail text
  * the worker's thumbnail renderer overlays the chosen concept text and
    still renders offline (paper background) when PIL is available

    python selftest_quality.py
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {str(got)[:100]}")
    if not ok:
        FAILS.append(label)


def fresh():
    for k in ("PANEL_ADMIN_PASSWORD", "PANEL_VISITOR_PASSWORD",
              "PANEL_SESSION_SECRET", "WORKER_SECRET", "GIST_TOKEN"):
        os.environ.pop(k, None)
    os.environ["WORKER_SECRET"] = "wsecret"
    for m in [m for m in list(sys.modules) if m in
              ("app", "brain", "config", "state", "comms", "yt", "jobs",
               "llm", "cloud", "yt_analytics")]:
        del sys.modules[m]
    import brain
    brain.state.STATE.clear()
    brain.state.LOADED = True      # memory-only: no gist writes during tests
    brain.state.default_state()
    brain.state.save_soon = lambda *a, **k: None
    brain.state.save_now = lambda *a, **k: None
    brain.comms.send = lambda msg, html=False: None
    brain.comms.log = lambda *a, **k: None
    brain._research = lambda d, u: {"topic": "t", "angle": "a",
                                    "sources": [{"fact": "f", "citation": "c"}]}
    return brain


def script_json(sid, n=10):
    return json.dumps({
        "id": sid, "title": f"T {sid}", "hook": "h",
        "scenes": [{"narration": f"scene {i} text", "source": "s",
                    "archive_search": "q", "visual_keywords": "kw"}
                   for i in range(n)]})


def scorer(score):
    return json.dumps({"score": score, "reason": "test"})


FACTS_OK = json.dumps({"ok": True, "problems": []})
THUMB = json.dumps({"concepts": [
    {"text": "THE BURNING LEDGER", "concept": "a ledger on fire", "score": 40},
    {"text": "IMPOSSIBLE TOWN", "concept": "a town that isn't on maps", "score": 90},
    {"text": "CLERK KNEW", "concept": "a clerk's face lit by candle", "score": 60}]})


def rig(brain, responses):
    """Fake gemini with a scripted response queue; records prompts."""
    box = {"prompts": [], "n": 0}

    def fake(prompt, system=None, gemini_models=None):
        box["n"] += 1
        box["prompts"].append(prompt)
        r = responses.pop(0)
        return r() if callable(r) else r

    brain.gemini = fake
    return box


def writes(box):
    return [p for p in box["prompts"] if "Write ONE video script" in p]


def main():
    # ---- 1. clean pass: attempt 1 clears all three gates ----
    brain = fresh()
    box = rig(brain, [script_json("s1"), scorer(82), scorer(78),
                      FACTS_OK, THUMB])
    out = brain.generate_script("mysteries")
    check("clean draft passes on attempt 1", out is not None
          and out["id"] == "s1" and len(writes(box)) == 1)
    check("_qa rides along on the script",
          out and out["_qa"]["hook"] == 82 and out["_qa"]["retention"] == 78
          and out["_qa"]["facts"] == [])
    check("thumbnail concept attached (best of 3)",
          out and out["thumbnail"]["text"] == "IMPOSSIBLE TOWN")
    check("both gate ledgers recorded",
          (len(brain.state.STATE["hook_scores"]),
           len(brain.state.STATE["retention_scores"])), (1, 1))
    check("the script prompt teaches retention mechanics",
          "RETENTION MECHANICS" in writes(box)[0]
          and "average under 14 words" in writes(box)[0])

    # ---- 2. weak hook -> regenerate with the problem NAMED ----
    brain = fresh()
    box = rig(brain, [script_json("s1"), scorer(60), scorer(80), FACTS_OK,
                      script_json("s2"), scorer(85), scorer(76), FACTS_OK,
                      THUMB])
    out = brain.generate_script("mysteries")
    check("weak hook regenerates, second draft ships",
          out is not None and out["id"] == "s2")
    check("the rewrite prompt names the failed review",
          len(writes(box)) == 2
          and "FAILED QUALITY REVIEW" in writes(box)[1]
          and "hook weakest at" in writes(box)[1])

    # ---- 3. fact flags feed the rewrite verbatim ----
    brain = fresh()
    probs = json.dumps({"ok": False,
                        "problems": ["Scene 4: sources say 1935, not 1932"]})
    box = rig(brain, [script_json("s1"), scorer(85), scorer(80), probs,
                      script_json("s2"), scorer(86), scorer(79), FACTS_OK,
                      THUMB])
    out = brain.generate_script("mysteries")
    check("fact-flagged draft regenerates and recovers",
          out is not None and out["id"] == "s2")
    check("the fact problem is quoted to the rewriter",
          "1935, not 1932" in writes(box)[1])

    # ---- 4. three below-bar drafts -> best of 3 ships WITH warnings ----
    brain = fresh()
    box = rig(brain, [script_json("s1"), scorer(50), scorer(80), FACTS_OK,
                      script_json("s2"), scorer(60), scorer(80), FACTS_OK,
                      script_json("s3"), scorer(70), scorer(80), FACTS_OK,
                      THUMB])
    out = brain.generate_script("mysteries")
    check("all below bar -> 3 attempts, best (70) survives",
          out is not None and out["id"] == "s3"
          and len(writes(box)) == 3)
    check("the survivor still carries its honest _qa",
          out["_qa"]["hook"] == 70 and out["_qa"]["hook"] < brain.HOOK_MIN)

    # ---- 5. every scorer fails OPEN (junk response -> 75 -> passes) ----
    brain = fresh()
    box = rig(brain, [script_json("s1"), scorer(80), "not json at all",
                      FACTS_OK, THUMB])
    out = brain.generate_script("mysteries")
    check("junk retention scores 75 and passes",
          out is not None and out["_qa"]["retention"] == 75)

    brain = fresh()
    box = rig(brain, [script_json("s1"), "garbage", scorer(80),
                      FACTS_OK, THUMB])
    out = brain.generate_script("mysteries")
    check("junk hook scores 75 and passes",
          out is not None and out["_qa"]["hook"] == 75)

    brain = fresh()
    box = rig(brain, [script_json("s1"), scorer(85), scorer(80), "junk",
                      THUMB])
    out = brain.generate_script("mysteries")
    check("junk fact-check fails open (ok, no problems)",
          out is not None and out["_qa"]["facts"] == [])

    # ---- 6. thumbnail picker: fail-open leaves no key ----
    brain = fresh()
    box = rig(brain, [script_json("s1"), scorer(85), scorer(80),
                      FACTS_OK, "not json"])
    out = brain.generate_script("mysteries")
    check("junk thumbnail concepts -> no thumbnail key, script still ships",
          out is not None and "thumbnail" not in out)

    # ---- 7. next_publish_time clock math ----
    brain = fresh()   # best_hour defaults to 17
    check("10:00 -> today 17:00",
          brain.next_publish_time(datetime(2026, 9, 15, 10, 0)),
          datetime(2026, 9, 15, 17, 0))
    check("16:10 -> today 17:00 (50 min out)",
          brain.next_publish_time(datetime(2026, 9, 15, 16, 10)),
          datetime(2026, 9, 15, 17, 0))
    check("16:40 -> tomorrow (only 20 min out)",
          brain.next_publish_time(datetime(2026, 9, 15, 16, 40)),
          datetime(2026, 9, 16, 17, 0))
    check("18:00 -> tomorrow 17:00",
          brain.next_publish_time(datetime(2026, 9, 15, 18, 0)),
          datetime(2026, 9, 16, 17, 0))
    brain.state.STATE["best_hour"] = 9
    check("best_hour 9, 08:05 -> today 09:00",
          brain.next_publish_time(datetime(2026, 9, 15, 8, 5)),
          datetime(2026, 9, 15, 9, 0))

    # ---- 8. title_check ACTS, reports, and swaps once per video ----
    brain = fresh()
    sent = []
    brain.comms.send = lambda msg, html=False: sent.append(msg)
    swapped = []
    brain.yt.my_videos = lambda **k: [
        {"id": "ok1", "title": "A", "views": 300, "privacy": "public",
         "published": "2026-09-10"},
        {"id": "ok2", "title": "B", "views": 200, "privacy": "public",
         "published": "2026-09-10"},
        {"id": "ok3", "title": "C", "views": 150, "privacy": "public",
         "published": "2026-09-10"},
        {"id": "ok4", "title": "D", "views": 120, "privacy": "public",
         "published": "2026-09-10"},
        {"id": "weak1", "title": "Old Weak Title", "views": 20,
         "privacy": "public", "published": "2026-09-01"}]
    brain.yt.update_title = lambda vid, title: swapped.append((vid, title))
    box = rig(brain, ["\"A Far Better Title\""])
    brain.title_check()
    check("the underperformer's title was swapped",
          swapped, [("weak1", "A Far Better Title")])
    check("the swap was reported loudly",
          any("auto-swapped" in m and "Old Weak Title" in m for m in sent),
          True)
    check("the ledger remembers the swap",
          "weak1" in brain.state.STATE["title_swaps"], True)
    sent.clear()
    box2 = rig(brain, ["\"Another Title\""])
    brain.title_check()
    check("second pass: ledger blocks a re-swap",
          swapped == [("weak1", "A Far Better Title")]
          and box2["n"] == 0 and not sent, True)

    # ---- 9. queue message carries warnings + thumbnail text ----
    brain = fresh()
    sent = []
    brain.comms.send = lambda msg, html=False: sent.append(msg)
    brain.jobs.add_job = lambda t, payload: {"id": "jobX"}
    brain.cloud.wake_soon = lambda *a, **k: None
    brain.generate_script = lambda direction=None: {
        "id": "warned", "title": "Warned Video", "scenes": [0] * 10,
        "_qa": {"hook": 68, "retention": 72,
                "facts": ["Scene 2: date unsupported by sources"]},
        "thumbnail": {"text": "CLERK KNEW", "concept": "candlelit clerk"}}
    made = brain.queue_next_video(1)
    msg = "\n".join(sent)
    check("the warned video queued", made, 1)
    check("the message warns, loudly",
          "Passed with warnings" in msg and "hook 68" in msg
          and "Scene 2: date unsupported" in msg, True)
    check("the message names the thumbnail text",
          "CLERK KNEW" in msg, True)

    # ---- 10. worker thumbnail: overlay text + offline render ----
    sys.path.insert(0, str(Path(__file__).parent / "worker"))
    try:
        import make_thumbnails as mt
        have_pil = True
    except ImportError:
        # PIL lives in the render envs (Actions/PC venv), not necessarily
        # this python — stub it so the pure-text helpers still get
        # checked; only the actual pixel render is skipped
        import types
        fake_pil = types.ModuleType("PIL")
        for _n in ("Image", "ImageDraw", "ImageEnhance", "ImageFont"):
            setattr(fake_pil, _n, types.SimpleNamespace())
        sys.modules["PIL"] = fake_pil
        import make_thumbnails as mt
        have_pil = False
        print("skip pixel-render check (no PIL here) — text logic still runs")
    check("thumb_text prefers the QA-picked concept",
          mt.thumb_text({"title": "The Long Original Title",
                         "thumbnail": {"text": "IMPOSSIBLE TOWN",
                                       "concept": "x"}}),
          "IMPOSSIBLE TOWN")
    check("thumb_text falls back to the title",
          mt.thumb_text({"title": "The Long Original Title"}),
          "The Long Original Title")
    if have_pil:
        from PIL import Image
        mt._background = (lambda s, v:
                          (Image.new("RGB", (mt.W, mt.H), mt.PAPER), "paper"))
        out_png = Path("selftest_thumb.png")
        mt.make_thumbnail({"id": "t1", "title": "Some Long Title",
                           "thumbnail": {"text": "IMPOSSIBLE TOWN",
                                         "concept": "a burning ledger"}},
                          out_png)
        check("thumbnail renders offline (paper bg)",
              out_png.exists() and out_png.stat().st_size > 5000, True)
        out_png.unlink()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all quality-first checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
