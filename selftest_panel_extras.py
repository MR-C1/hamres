"""Self-test the panel's new surfaces, offline.

Covers everything the "full potential" update added:

  * /api/state grows analytics (reason/rows/formats), health (booleans only),
    uploads_today, pending .urls, jobs .has_script
  * the script:<jobid> action serves a slim script, 404s a job without one,
    works for a visitor (read-only) while mutations stay refused
  * _analytics_compute merges rows with the video list, splits longs/shorts
    at 180s, converts CTR to a percent, sorts by retention
  * _analytics_snapshot never blocks: a cold call answers "pending" and a
    daemon thread fills the cache, which is then served without recompute
  * the panel HTML carries the new anchors, the [hidden] reset, and a JS
    block whose brackets balance and whose $("id") references all exist

    python selftest_panel_extras.py
"""
import os
import re
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, got, want):
    ok = want(got) if callable(want) else got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}")
    if not ok:
        FAILS.append(label)


def fresh(admin_pw="", visitor_pw=""):
    """Import a clean app under the given env, return (client, module)."""
    for k in ("PANEL_ADMIN_PASSWORD", "PANEL_VISITOR_PASSWORD",
              "PANEL_SESSION_SECRET", "WORKER_SECRET"):
        os.environ.pop(k, None)
    os.environ["WORKER_SECRET"] = "wsecret"
    if admin_pw:
        os.environ["PANEL_ADMIN_PASSWORD"] = admin_pw
    if visitor_pw:
        os.environ["PANEL_VISITOR_PASSWORD"] = visitor_pw
    for m in [m for m in list(sys.modules) if m in
              ("app", "config", "state", "comms", "brain", "yt", "jobs",
               "llm", "cloud", "yt_analytics")]:
        del sys.modules[m]
    import app as appmod
    appmod.state.STATE.clear()
    appmod.state.LOADED = True      # memory-only: no gist writes during tests
    appmod.state.default_state()
    return appmod.app.test_client(), appmod


def main():
    c, appmod = fresh("326745", "12345")
    r = c.post("/panel/login", follow_redirects=False,
               data={"user": "admin", "password": "326745"})
    check("admin signs in", r.status_code, 302)

    # A fast fake from the very start: with the real module, the first
    # /api/state kicks a fill thread whose googleapiclient import is slow
    # in the venv, and that thread's late write raced this suite. Installing
    # this here makes every background fill instant and deterministic.
    class FakeFast:
        @staticmethod
        def video_report(days=28):
            return {}, "scope"
    sys.modules["yt_analytics"] = FakeFast

    # ---- the page carries the new anchors ----
    r = c.get("/panel")
    t = r.get_data(as_text=True)
    check("panel serves", r.status_code, 200)
    for anchor in ("retnote", "retbox", "retfmt", "retrows", "sysbox",
                   "op-uploads", "op-next", "filmq", "modal", "modal-body"):
        check(f"page carries #{anchor}", f'id="{anchor}"' in t, True)
    check("CSV buttons wired to LOCAL",
          'data-local="csvfilms"' in t and 'data-local="csvhistory"' in t, True)
    check("script buttons use data-script (survives visitor CSS)",
          "data-script" in t, True)
    check("hidden attribute reset present",
          "[hidden]{display:none!important}" in t, True)
    check("format + retained columns in the films table",
          'data-k="duration"' in t and 'data-k="avg_pct"' in t, True)

    # ---- the JS parses: brackets balance, every $("id") exists ----
    m = re.search(r"<script>(.*)</script>", t, re.S)
    check("script block found", bool(m), True)
    src = m.group(1)

    def balanced(src):
        """Bracket balance with strings, // and /* comments, and regex
        literals handled — a naive count false-alarms on /[&<>"]/g."""
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack = []
        i, n = 0, len(src)
        prev = ""                      # last significant char, for regex vs divide
        while i < n:
            ch = src[i]
            if ch in "\"'":
                q = ch
                i += 1
                while i < n and src[i] != q:
                    i += 2 if src[i] == "\\" else 1
                prev = q
            elif ch == "/" and src[i:i + 2] == "//":
                while i < n and src[i] != "\n":
                    i += 1
            elif ch == "/" and src[i:i + 2] == "/*":
                i += 2
                while i + 1 < n and src[i:i + 2] != "*/":
                    i += 1
                i += 1
            elif ch == "/" and (prev in "(,=:[!&|?;{}" or prev == ""):
                # a regex literal starts here: skip to its unescaped close
                i += 1
                in_class = False
                while i < n:
                    if src[i] == "\\":
                        i += 1
                    elif src[i] == "[":
                        in_class = True
                    elif src[i] == "]":
                        in_class = False
                    elif src[i] == "/" and not in_class:
                        break
                    elif src[i] == "\n":
                        return False, "regex ran to end of line"
                    i += 1
                prev = "/"
            elif ch in "([{":
                stack.append(ch)
                prev = ch
            elif ch in ")]}":
                if not stack or pairs[stack[-1]] != ch:
                    return False, f"stray {ch!r}"
                stack.pop()
                prev = ch
            elif not ch.isspace():
                prev = ch
            i += 1
        return (not stack), ("".join(stack) if stack else "ok")

    bal = balanced(src)
    check("JS brackets balance", bal, (True, "ok"))
    ids = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', src))
    missing = sorted(i for i in ids if f'id="{i}"' not in t)
    check("every $(\"id\") exists in the HTML", missing, [])

    # ---- /api/state grows the new fields ----
    appmod._channel_cache.update(t=time.time(), data={
        "ch": {"title": "T", "subs": 1, "views": 2, "videos": 2},
        "vids": [
            {"id": "today1", "title": "Out this morning", "privacy": "public",
             "published": f"{datetime.now() + appmod.config.BD_OFFSET:%Y-%m-%d}",
             "views": 5, "likes": 0, "comments": 0, "duration_s": 400},
            {"id": "old1", "title": "An older one", "privacy": "private",
             "published": "2026-01-01", "views": 9, "likes": 1,
             "comments": 0, "duration_s": 40},
        ]})
    appmod._analytics_cache.update(t=0, data=None, busy=False)
    appmod.state.STATE["pending_videos"] = {
        "u1": {"title": "Waiting film",
               "video_url": "https://youtu.be/abc",
               "video_urls": ["https://youtu.be/abc", "https://youtu.be/def"],
               "title_alternatives": ["Alt one", "Alt two"]}}
    r = c.get("/api/state")
    d = r.get_json()
    check("state serves", r.status_code, 200)
    check("uploads_today counts today's BD upload", d.get("uploads_today"), 1)
    check("pending carries every format url",
          d["pending"][0]["urls"], ["https://youtu.be/abc", "https://youtu.be/def"])
    check("analytics shape",
          sorted(d["analytics"].keys()),
          ["avg_pct", "formats", "minutes", "note", "reason", "rows", "when"])
    check("analytics reason is a known word",
          d["analytics"]["reason"] in ("ok", "pending", "empty", "scope",
                                       "disabled", "other"), True)
    h = d["health"]
    check("health carries the wiring keys",
          sorted(k for k in h if k != "lastdiag"),
          ["analytics_token", "cloudflare", "dispatch", "gemini_keys",
           "gist", "groq", "openrouter", "telegram", "youtube"])
    scalars = [v for k, v in h.items() if k != "lastdiag"]
    check("health values are booleans/counts only",
          all(isinstance(v, (bool, int)) for v in scalars), True)
    check("health lastdiag is a dict of strings",
          isinstance(h["lastdiag"], dict)
          and all(isinstance(v, str) for v in h["lastdiag"].values()), True)

    # ---- the script action ----
    appmod.state.STATE["jobs"] = [
        {"id": "job1", "type": "render", "status": "pending", "created": time.time(),
         "script": {"id": "t1", "title": "The test script", "hook": "A hook.",
                    "description": "desc", "tags": ["x", "y"], "outro": "bye",
                    "scenes": [
                        {"narration": "First scene.",
                         "short_narration": "Cut of the first scene.",
                         "short_title": "First cut", "in_short": True,
                         "source": "a book", "archive_search": "q",
                         "visual_keywords": "kw"},
                        {"narration": "Second scene."}]}},
        {"id": "job2", "type": "render", "status": "pending",
         "created": time.time()}]
    r = c.post("/api/action", json={"action": "script:job1"})
    sc = r.get_json().get("script") or {}
    check("script action serves the script", r.status_code, 200)
    check("script keeps its title and hook",
          (sc.get("title"), sc.get("hook")), ("The test script", "A hook."))
    check("scene one keeps narration + short fields",
          (sc["scenes"][0]["narration"], sc["scenes"][0]["short_title"],
           sc["scenes"][0]["in_short"]),
          ("First scene.", "First cut", True))
    check("scene two keeps a bare narration",
          sc["scenes"][1]["narration"], "Second scene.")
    check("job without a script is a 404",
          c.post("/api/action", json={"action": "script:job2"}).status_code, 404)
    check("unknown job is a 404 too",
          c.post("/api/action", json={"action": "script:nope"}).status_code, 404)

    # a visitor may read a script and still change nothing
    cv = appmod.app.test_client()
    r = cv.post("/panel/login", follow_redirects=False,
                data={"user": "visitor", "password": "12345"})
    check("visitor signs in", r.status_code, 302)
    r = cv.post("/api/action", json={"action": "script:job1"})
    check("visitor may read a script",
          (r.status_code, r.get_json().get("ok")), (200, True))
    r = cv.post("/api/action", json={"action": "pause"})
    check("visitor still cannot mutate",
          (r.status_code, r.get_json().get("error")),
          (403, "read-only sign-in — ask the admin"))

    # jobs list exposes has_script
    r = c.get("/api/state")
    d = r.get_json()
    got = {j["id"]: j.get("has_script") for j in d["jobs"]}
    check("jobs expose has_script", got, {"job1": True, "job2": False})

    # ---- _analytics_compute: the merge ----
    class FakeYA:
        @staticmethod
        def video_report(days=28):
            return ({"today1": {"views": 10, "minutes": 5.4, "avg_pct": 42.53,
                                "subs_gained": 1, "impressions": 100,
                                "ctr": 0.041},
                     "old1": {"views": 20, "minutes": 1.2, "avg_pct": 77.7,
                              "subs_gained": 0}},
                    "ok")
    sys.modules["yt_analytics"] = FakeYA
    vids = appmod._channel_cache["data"]["vids"]
    out = appmod._analytics_compute(vids)
    check("compute reason ok", out["reason"], "ok")
    check("rows sorted by retention",
          [r["id"] for r in out["rows"]], ["old1", "today1"])
    check("titles merged from the video list",
          out["rows"][1]["title"], "Out this morning")
    check("minutes rounded", out["rows"][1]["minutes"], 5)
    check("avg_pct rounded to one place", out["rows"][1]["avg_pct"], 42.5)
    check("ctr becomes a percent", out["rows"][1]["ctr"], 4.1)
    check("longs/shorts split at 180s",
          (out["formats"]["longs"]["n"], out["formats"]["shorts"]["n"]), (1, 1))
    check("format averages",
          (out["formats"]["longs"]["avg_pct"], out["formats"]["shorts"]["avg_pct"]),
          (42.5, 77.7))
    check("overall average", out["avg_pct"], 60.1)
    check("total minutes", out["minutes"], 6)   # 5 + 1: per-row rounded

    class FakeScope:
        @staticmethod
        def video_report(days=28):
            return {}, "scope"
    sys.modules["yt_analytics"] = FakeScope
    out = appmod._analytics_compute(vids)
    check("scope reason survives the merge", out["reason"], "scope")
    check("scope leaves rows empty", out["rows"], [])
    check("scope leaves no note", out["note"], "")

    class FakeOther:
        @staticmethod
        def video_report(days=28):
            return {}, "other"
        @staticmethod
        def last_error():
            return ("invalid_client: The OAuth client was not found. "
                    "way past one hundred and forty characters so the "
                    "panel has to cap it somewhere reasonable ok")
    sys.modules["yt_analytics"] = FakeOther
    out = appmod._analytics_compute(vids)
    check("other keeps its reason", out["reason"], "other")
    check("other quotes the provider's error",
          out["note"].startswith("invalid_client: The OAuth client was not found."),
          True)
    check("the quote is capped at 140", len(out["note"]), 140)

    # ---- _analytics_snapshot: never blocks, fills off-thread, caches ----
    calls = {"n": 0}

    class FakeSlow:
        @staticmethod
        def video_report(days=28):
            calls["n"] += 1
            time.sleep(0.25)
            return ({"today1": {"views": 10, "minutes": 5.4, "avg_pct": 42.5,
                                "subs_gained": 1}}, "ok")
    sys.modules["yt_analytics"] = FakeSlow
    # let any earlier fill thread land before we time the cold call
    for _ in range(200):
        if not appmod._analytics_cache.get("busy"):
            break
        time.sleep(0.05)
    appmod._analytics_cache.update(t=0, data=None, busy=False)
    t0 = time.time()
    first = appmod._analytics_snapshot(vids)
    took = time.time() - t0
    check("cold call answers without waiting", took < 0.15, True)
    check("cold call says pending", first.get("reason"), "pending")
    for _ in range(100):
        if appmod._analytics_cache["data"]:
            break
        time.sleep(0.05)
    second = appmod._analytics_snapshot(vids)
    check("filled snapshot serves merged data",
          (second["reason"], len(second["rows"])), ("ok", 1))
    third = appmod._analytics_snapshot(vids)
    check("cache serves without recompute", calls["n"], 1)
    check("cached object is the same one", third is second, True)

    class FakeBoom:
        @staticmethod
        def video_report(days=28):
            calls["n"] += 1
            return {}, "boom"
    sys.modules["yt_analytics"] = FakeBoom
    fourth = appmod._analytics_snapshot(vids)
    check("cache ignores a swapped provider inside its window",
          fourth["reason"], "ok")
    check("the swapped provider never runs", calls["n"], 1)

    # ---- uploads_today on a cold channel snapshot ----
    appmod._channel_cache.update(t=0, data=None)
    r = c.get("/api/state")
    d = r.get_json()
    check("state survives an empty channel", r.status_code, 200)
    check("videos degrade to a list", isinstance(d["videos"], list), True)

    # ---- a render that uploads nothing must say so — and retry must
    #      recover it (a dead OAuth token once hid a full day of these) ----
    sent = []
    appmod.comms.send = lambda msg, html=False: sent.append(msg)
    # the action handlers import cloud INSIDE the function, so the module
    # object itself is what must be neutralized (no dispatch in tests)
    import cloud as cloudmod
    cloudmod.wake_soon = lambda *a, **k: None
    appmod.state.STATE["pending_videos"] = {}
    appmod.state.STATE["jobs"] = [{
        "id": "j1", "type": "render", "status": "claimed",
        "created": 1.0, "updated": 2.0,
        "script": {"id": "s1", "title": "The Lost Upload"}}]
    r = c.post("/report",
               json={"job_id": "j1", "ok": True, "title": "The Lost Upload",
                     "msg": "rendered but upload FAILED: invalid_grant"},
               headers={"X-Worker-Secret": "wsecret"})
    check("upload-failure report accepted", r.status_code, 200)
    check("the owner is told the upload failed",
          any("upload failed" in m and "invalid_grant" in m for m in sent),
          True)
    check("the job is still recorded done",
          appmod.state.STATE["jobs"][0]["status"], "done")
    check("no approval entry for an unuploaded render",
          appmod.state.STATE["pending_videos"], {})

    # the contrast: a good report registers the approval, stays quiet
    sent.clear()
    appmod.state.STATE["jobs"] = [{
        "id": "j2", "type": "render", "status": "claimed",
        "created": 1.0, "updated": 2.0, "script": {"id": "s2", "title": "T2"}}]
    r = c.post("/report",
               json={"job_id": "j2", "ok": True, "title": "T2",
                     "video_url": "https://youtu.be/good",
                     "video_urls": ["https://youtu.be/good"]},
               headers={"X-Worker-Secret": "wsecret"})
    check("good report registers the pending approval",
          "j2" in appmod.state.STATE["pending_videos"], True)
    check("good report raises no alarm", sent, [])

    # /retry requeues failed jobs AND done renders that uploaded nothing
    appmod.state.STATE["jobs"] = [
        {"id": "f1", "type": "render", "status": "failed",
         "created": 1.0, "updated": 1.0},
        {"id": "d1", "type": "render", "status": "done", "created": 2.0,
         "updated": 2.0,
         "result": {"msg": "rendered but upload FAILED", "video_url": ""}},
        {"id": "d2", "type": "render", "status": "done", "created": 3.0,
         "updated": 3.0,
         "result": {"video_url": "https://youtu.be/ok"}},
        {"id": "c1", "type": "cleanup", "status": "done", "created": 4.0,
         "updated": 4.0},
    ]
    r = c.post("/api/action", json={"action": "retry"})
    check("retry counts the failed and the lost uploads",
          r.get_json().get("requeued"), 2)
    by = {j["id"]: j["status"] for j in appmod.state.STATE["jobs"]}
    check("failed job requeued", by.get("f1"), "pending")
    check("done-but-unuploaded render requeued", by.get("d1"), "pending")
    check("a render that uploaded stays done", by.get("d2"), "done")
    check("non-render jobs are never requeued", by.get("c1"), "done")

    del sys.modules["yt_analytics"]
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all panel-extras checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
