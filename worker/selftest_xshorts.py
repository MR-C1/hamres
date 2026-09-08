"""Offline self-test for the multi-shorts build — no rendering, no
network, no keys. Proves the pure logic: which scenes become standalone
Shorts, that scenes inside the main Short are never reused, per-short
titles (brain's short_title, first-sentence fallback, length caps), the
upload batch (order, want_short flags, title dedupe), and that cleanup
globs cover the xshort finals AND their per-block files.

    python selftest_xshorts.py
"""
import sys
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import render_video  # noqa: E402
import worker as worker_mod  # noqa: E402
from common import REVIEW  # noqa: E402

FAILS = []


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)


def mkscript():
    """8 scenes; the first four are in_short with short narrations."""
    scenes = []
    for i in range(8):
        s = {"narration": f"Scene {i} full narration, told properly. " * 12,
             "visual_keywords": [f"keyword {i}"],
             "archive_search": [f"subject {i}"],
             "source": f"source {i}",
             "in_short": i < 4}
        if i < 4:
            s["short_narration"] = f"Scene {i} compressed. It is tight."
            s["short_title"] = f"Standalone Title {i}"
        scenes.append(s)
    return {"id": "xselftest", "title": "The Main Title",
            "hook": "Hook sentence one. Hook sentence two.",
            "scenes": scenes, "outro": "One line outro."}


DURS = {"hook": 30.0, "hookshort": 8.0, "outro": 3.0}
for i in range(8):
    DURS[f"scene{i}"] = 35.0
# main Short = trimmed hook (8s) + shortscene0 (12s) = 20s budget full
DURS["shortscene0"] = 12.0
DURS["shortscene1"] = 15.0
DURS["shortscene2"] = 18.0
DURS["shortscene3"] = 20.0


def main():
    script = mkscript()

    # 1. scene selection: scenes inside the main Short are never reused
    main_blocks = [bid for bid, _ in
                   render_video.pick_scenes(script, "short", DURS)]
    check("main Short uses the trimmed hook + scene 0",
          main_blocks == ["hookshort", "shortscene0"])
    extra = render_video.pick_extra_shorts(script, DURS, 2)
    check("extras skip the scene already in the main Short",
          [i for i, _ in extra] == [1, 2])
    check("extras cap at the requested count",
          len(render_video.pick_extra_shorts(script, DURS, 99)) == 3)
    check("count 0 -> no extras",
          render_video.pick_extra_shorts(script, DURS, 0) == [])

    # 2. in_short=False scenes never become standalone Shorts
    s2 = dict(script)
    s2["scenes"] = [dict(x) for x in script["scenes"]]
    s2["scenes"][2]["in_short"] = False
    got = [i for i, _ in render_video.pick_extra_shorts(s2, DURS, 99)]
    check("in_short=False scene excluded from extras", got == [1, 3])

    # 3. legacy scripts (no short_narration) get no extras — graceful
    legacy = dict(script)
    legacy["scenes"] = [{k: v for k, v in s.items()
                         if k not in ("short_narration", "short_title")}
                        for s in script["scenes"]]
    check("legacy script -> no extras",
          render_video.pick_extra_shorts(legacy, DURS, 2) == [])

    # 4. the xshort format maps to exactly its one scene block
    blocks = render_video.pick_scenes(script, "xshort1", DURS)
    check("xshort1 renders one block: shortscene1",
          blocks == [("shortscene1", script["scenes"][1])])

    # 5. per-short titles: brain field, first-sentence fallback, caps
    check("brain short_title wins",
          worker_mod._short_title(script["scenes"][1], "T")
          == "Standalone Title 1")
    nofield = {"short_narration": "First sentence stands alone. "
                                  "Second sentence does not."}
    check("fallback takes the first sentence",
          worker_mod._short_title(nofield, "T") == "First sentence stands alone")
    longfirst = {"short_narration": ("extraordinarily mysterious "
                                     "circumstances surrounding unexplained "
                                     "disappearance remained thoroughly "
                                     "investigated despite contradictory "
                                     "evidence. Second sentence.")}
    t = worker_mod._short_title(longfirst, "T")
    check("over-long first sentence trimmed under 80 chars",
          len(t) <= 81 and t.endswith("…"))
    exact = {"short_narration": "Exactly four words here. More."}
    check("complete short sentence gets no ellipsis",
          worker_mod._short_title(exact, "T") == "Exactly four words here")
    check("empty scene falls back to the script title",
          worker_mod._short_title({}, "Fallback") == "Fallback")

    # 6. the upload batch: order, want_short flags, distinct titles
    REVIEW.mkdir(parents=True, exist_ok=True)
    sid = "xselftest"
    made = []
    for name in (f"{sid}_short.mp4", f"{sid}_xshort1.mp4",
                 f"{sid}_xshort2.mp4", f"{sid}_long.mp4"):
        p = REVIEW / name
        p.write_bytes(b"0" * 64)
        made.append(p)

    calls = []

    class FakeUpload:
        @staticmethod
        def parse_metadata(path):
            return {}

        @staticmethod
        def upload_video(f, meta, cfg, want_short=None, **kw):
            calls.append({"file": Path(f).name, "title": meta["title"],
                          "want_short": want_short})
            return f"https://youtu.fake/{Path(f).stem}"

    fake_mod = types.ModuleType("upload")
    fake_mod.parse_metadata = FakeUpload.parse_metadata
    fake_mod.upload_video = FakeUpload.upload_video
    fake_mod.set_thumbnail = lambda *a, **k: None
    real_upload = sys.modules.get("upload")
    sys.modules["upload"] = fake_mod
    worker_mod.CFG = {}
    try:
        up = worker_mod._upload_files(script, sid)
    finally:
        if real_upload is None:
            sys.modules.pop("upload", None)
        else:
            sys.modules["upload"] = real_upload
        for p in made:
            p.unlink(missing_ok=True)

    check("all four formats uploaded", len(calls) == 4)
    check("upload order: hook-short, scene-shorts, long",
          [c["file"] for c in calls]
          == [f"{sid}_short.mp4", f"{sid}_xshort1.mp4",
              f"{sid}_xshort2.mp4", f"{sid}_long.mp4"])
    check("want_short flags: every short short, long not",
          [c["want_short"] for c in calls] == [True, True, True, False])
    titles = [c["title"] for c in calls]
    check("hook-short and long share the script title, extras differ",
          titles == ["The Main Title", "Standalone Title 1",
                     "Standalone Title 2", "The Main Title"])
    check("titles map rides the result for previews",
          up["titles"].get(f"{sid}_xshort1.mp4") == "Standalone Title 1")

    # 7. duplicate short_titles get a part number, never a silent dup
    dup = dict(script)
    dup["scenes"] = [dict(x) for x in script["scenes"]]
    dup["scenes"][1]["short_title"] = "Same Title"
    dup["scenes"][2]["short_title"] = "Same Title"
    made = []
    for name in (f"{sid}_short.mp4", f"{sid}_xshort1.mp4",
                 f"{sid}_xshort2.mp4", f"{sid}_long.mp4"):
        p = REVIEW / name
        p.write_bytes(b"0" * 64)
        made.append(p)
    calls.clear()
    sys.modules["upload"] = fake_mod
    try:
        worker_mod._upload_files(dup, sid)
    finally:
        if real_upload is None:
            sys.modules.pop("upload", None)
        else:
            sys.modules["upload"] = real_upload
        for p in made:
            p.unlink(missing_ok=True)
    dup_titles = [c["title"] for c in calls]
    check("duplicate scene titles deduped with a part number",
          dup_titles == ["The Main Title", "Same Title",
                         "Same Title — Part 2", "The Main Title"])

    # 8. cleanup globs cover xshort finals AND their per-block files
    csid = "xcleantest"
    files = [REVIEW / f"{csid}_short.mp4", REVIEW / f"{csid}_long.mp4",
             REVIEW / f"{csid}_xshort2.mp4",
             REVIEW / f"{csid}_xshort2_00_shortscene2.mp4",
             REVIEW / f"{csid}_xshort2.blocks.txt"]
    REVIEW.mkdir(parents=True, exist_ok=True)
    for p in files:
        p.write_bytes(b"0" * 32)
    worker_mod._cleanup_files(csid)
    check("cleanup removes xshort finals and blocks",
          not any(p.exists() for p in files))

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all multi-shorts checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
