"""Offline self-test for script writing — no network, no keys. Fakes
brain.gemini so the corrective-retry path runs end to end:

  * a good script passes the parse on the first call
  * a 4-scene stub is rejected, and the SECOND call's prompt carries the
    format nudge — the retry that recovers a lazy/overloaded provider
    instead of losing the day
  * both attempts failing returns None (the queue just skips)
  * a fenced ```json block still parses (strip logic lives in _parse_script)

    python selftest_script.py
"""
import json
import os
import sys

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
    brain.state.LOADED = True
    brain.state.default_state()
    return brain


def good_script(n=10):
    return json.dumps({
        "id": f"test-{n}", "title": "T", "hook": "h",
        "scenes": [{"narration": f"scene {i} text", "source": "s",
                    "archive_search": "q", "visual_keywords": "kw"}
                   for i in range(n)]})


def main():
    brain = fresh()
    calls = {"n": 0, "prompts": []}

    def fake_gemini(prompt, system=None):
        calls["n"] += 1
        calls["prompts"].append(prompt)
        return calls["plan"].pop(0)

    brain.gemini = fake_gemini

    # 1. good script passes on the first call — no retry spent
    calls["plan"] = [good_script(10)]
    out = brain._write_script("mysteries")
    check("good script passes first try",
          (out is not None and calls["n"] == 1
           and len(out["scenes"]) == 10))

    # 2. stub -> rejected -> corrective retry whose prompt carries the nudge
    calls["plan"] = [good_script(4), good_script(11)]
    out = brain._write_script("mysteries")
    check("stub rejected, retry recovers", out is not None
          and len(out["scenes"]) == 11)
    check("the retry made exactly two calls", calls["n"], 3)
    check("the second prompt names the format fix",
          "8-12 full scenes" in calls["prompts"][-1], True)

    # 3. both attempts failing -> None (queue skips, nothing crashes)
    calls["plan"] = [good_script(3), good_script(2)]
    out = brain._write_script("mysteries")
    check("both attempts failing returns None", out, None)

    # 4. fenced ```json output still parses
    calls["plan"] = ["```json\n" + good_script(9) + "\n```"]
    out = brain._write_script("mysteries")
    check("fenced json parses", out is not None and len(out["scenes"]) == 9)

    # 5. junk (None / non-JSON) -> None, no exception. A silent chain
    # must not burn the retry nudge — the model never saw anything.
    before = calls["n"]
    calls["plan"] = [None]
    check("a silent provider returns None",
          brain._write_script("mysteries"), None)
    check("silent chain spends one call, not two", calls["n"] - before, 1)
    calls["plan"] = ["not json at all", "still not json"]
    check("non-JSON returns None", brain._write_script("mysteries"), None)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all script-writing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
