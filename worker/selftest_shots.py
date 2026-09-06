"""Check the shot-cutting arithmetic without rendering anything.

A real render is 10-20 minutes and needs moviepy, ffmpeg, network footage
and TTS — far too slow a feedback loop for "how many shots does a Short
actually get?". shot_count/shot_order are deliberately pure, so this test
lifts just those two functions out of render_video.py (which imports
moviepy at module level) and exercises them in milliseconds.

    python selftest_shots.py

Regression under test: a 20s Short used to arrive as ONE film clip plus
ONE photograph, because the whole block was handed to a single subclip.
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).with_name("render_video.py")
WANT = ("shot_count", "shot_order")


def load_pure():
    """exec only the named functions, so moviepy is never imported."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    picked = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in WANT]
    missing = set(WANT) - {n.name for n in picked}
    if missing:
        raise SystemExit(f"render_video.py no longer defines {missing}")
    ns = {}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "<pure>", "exec"),
         ns)
    return ns


def main():
    ns = load_pure()
    shot_count, shot_order = ns["shot_count"], ns["shot_order"]
    fails = []

    def check(label, got, want):
        ok = got == want if not callable(want) else want(got)
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}")
        if not ok:
            fails.append(label)

    # the exact case the owner reported: one archival film + one photo
    # carrying a 20s Short. Old code: 2 shots for the whole video.
    check("20s short, 1 film + 1 photo",
          shot_count([("video", "a.mp4"), ("still", "b.jpg")], 20, 2.6),
          5)
    # the normal case now that Commons returns up to 8 stills per term
    check("20s short, 1 film + 6 photos",
          shot_count([("video", "a.mp4")] + [("still", f"{i}.jpg")
                                             for i in range(6)], 20, 2.6),
          8)
    # long-form block: slower cutting, still many shots
    check("35s long block, 4 stock clips",
          shot_count([("video", f"{i}.mp4") for i in range(4)], 35, 3.4),
          10)
    # a lone photograph must stay one continuous Ken Burns move
    check("12s block, single photo", shot_count([("still", "x.jpg")], 12, 3.0),
          1)
    # a lone film is still cut into different moments
    check("12s block, single film", shot_count([("video", "x.mp4")], 12, 3.0),
          3)
    # short blocks must not be diced into sub-second flashes
    check("3s hook, plenty of material",
          shot_count([("video", "a.mp4"), ("still", "b.jpg")], 3, 2.6), 1)
    check("no material at all", shot_count([], 20, 2.6), 0)

    # round-robin alternates sources and numbers each reuse, so the same
    # file is never shown the same way twice in a row
    order = shot_order([("video", "a.mp4"), ("still", "b.jpg")])
    seq = [next(order) for _ in range(5)]
    check("alternates sources", [k for k, _, _ in seq],
          ["video", "still", "video", "still", "video"])
    check("numbers reuses", [n for _, _, n in seq], [0, 0, 1, 1, 2])

    print()
    if fails:
        print(f"{len(fails)} FAILED: {', '.join(fails)}")
        return 1
    print("all shot-planning checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
