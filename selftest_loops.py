"""Offline self-test for the remaining learning loops — no network, no
keys. Three loops, one suite:

  - audience-demand mining: comment -> topic request, dedupe by normalized
    key, count per asker, the second ask announces, the planner sees the
    list, non-requests and API failures mine nothing, one pass per comment
  - best-hour learner: daily per-video view snapshots record correctly,
    early velocity buckets by publish hour (BD), 3+ videos per hour to
    qualify, 20% margin before a move, announcement on move, fallback
  - title-swap verdicts: pre-swap CTR captured in the ledger, verdict at
    7 days (worked / no gain / inconclusive / no data), once ever
  - quota-aware planning: the 13:00 BD window, done-upload counting, the
    defer when the window is full
  - answer-Shorts: the second ask drafts the Short once ever, full quota
    defers, _write_short's 4-scene floor and short-only format
  - thumbnail A/B: the weak-video swap with banked alternates, the
    ledger, and the 7-day verdict (win keeps, clear loss reverts)

    python selftest_loops.py
"""
import sys

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)


def main():
    import state as st
    st.STATE.clear()
    st.LOADED = True
    st.default_state()

    import brain
    brain.state = st
    sent = []
    orig_send = brain.comms.send
    brain.comms.send = lambda m, html=False: sent.append(m)

    # ------------------------------------------------------------------
    # 1. audience-demand mining
    # ------------------------------------------------------------------
    replies = []          # scripted gemini answers, popped per call

    def fake_gemini(prompt, system=None, gemini_models=None):
        return replies.pop(0) if replies else ""

    orig_gemini = brain.gemini

    # the spawn is live in _mine_requests — pin it to a no-op so section
    # 1's second ask can't fire a REAL background draft that would race
    # the scripted gemini replies (section 5 fakes it properly)
    real_qas = brain._queue_answer_short
    brain._queue_answer_short = lambda entry: None

    def c(cid, text, author="A"):
        return {"comment_id": cid, "text_plain": text, "text": text,
                "author": author, "published": "2026-09-12"}

    brain.gemini = fake_gemini
    replies.append('{"topic": "lost gold shipwrecks"}')
    brain._mine_requests([c("c1", "please do a video on lost gold ships")])
    reqs = st.STATE["audience_requests"]
    check("mine: request stored with count 1",
          len(reqs) == 1 and reqs[0]["count"] == 1
          and reqs[0]["topic"] == "lost gold shipwrecks")
    check("mine: second ask announces once",
          not sent)             # one ask = no announcement yet

    replies.append('{"topic": "Lost Gold Shipwrecks"}')  # case-insensitive
    brain._mine_requests([c("c2", "more lost gold shipwreck content please",
                            author="B")])
    check("mine: same topic dedupes and counts",
          len(reqs) == 1 and reqs[0]["count"] == 2
          and set(reqs[0]["askers"]) == {"A", "B"})
    check("mine: the second ask announced",
          any("keep asking" in m for m in sent))

    sent.clear()
    replies.append('{"topic": null}')
    brain._mine_requests([c("c3", "great video, loved it")])
    check("mine: non-request ignored", len(reqs) == 1)

    brain.gemini = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("503"))      # provider down
    brain._mine_requests([c("c4", "do one on the Voynich manuscript")])
    check("mine: provider failure fails open (no entry, no raise)",
          len(reqs) == 1)

    # once ever per comment: re-mining the same ids does nothing (the
    # ledger check skips the comment before gemini is ever called — no
    # reply is appended, and none would be consumed)
    brain.gemini = fake_gemini
    brain._mine_requests([c("c1", "please do a video on lost gold ships")])
    check("mine: mined ledger prevents double counting",
          len(reqs) == 1 and reqs[0]["count"] == 2)

    lines = brain._audience_lines()
    check("planner lines carry topic and ask-count",
          lines == ["- Lost Gold Shipwrecks (2 asks)"])

    # daily report line (indirect: the wants formatting)
    wants = [f"{r['topic']} ({r['count']})" for r in reqs
             if r.get("count", 0) >= 2][:3]
    check("daily-report 'asked for' line formats", wants
          == ["Lost Gold Shipwrecks (2)"])

    # ------------------------------------------------------------------
    # 2. best-hour learner
    # ------------------------------------------------------------------
    st.STATE["video_history"] = {}
    from datetime import datetime, timedelta
    import config

    # Pin the clock: every date-dependent check below (age gates, the
    # 7-day verdict wait) is judged against 2026-09-12 20:00 BD, no
    # matter when the suite runs.
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 12, 14, 0)  # UTC == 20:00 BD

    orig_dt = brain.datetime
    brain.datetime = FixedDT

    def vid(vid_, hour_bd, base_pub, views_rows):
        # published_ts is UTC; BD = UTC+6
        pub = (datetime.strptime(base_pub, "%Y-%m-%d")
               + timedelta(hours=hour_bd) - config.BD_OFFSET)
        v = {"id": vid_, "title": vid_, "privacy": "public",
             "views": views_rows[-1][1],
             "published_ts": pub.strftime("%Y-%m-%dT%H:%M:%SZ")}
        st.STATE["video_history"][vid_] = views_rows
        return v

    # rows are [date, views]; the first snapshot 3+ days after publish is
    # the early-velocity reading, so seed the eligible one on 08-30
    # (08-23 is only 2.3 days after an Aug-20 17:00 publish — too early)
    now_bd = datetime(2026, 9, 12, 20, 0)
    # four videos at 17:00 BD with modest velocity
    vids_17 = [vid(f"a{i}", 17, "2026-08-20",
                   [["2026-08-23", 30 + i], ["2026-08-30", 40 + i]])
               for i in range(4)]
    # four at 20:00 BD with clearly better velocity
    vids_20 = [vid(f"b{i}", 20, "2026-08-20",
                   [["2026-08-23", 60 + i], ["2026-08-30", 80 + i]])
               for i in range(4)]
    # one lonely 8:00 video with huge views — buckets keep it, but 3+
    # videos are required before an hour can win
    vids_08 = [vid("c0", 8, "2026-08-20",
                   [["2026-08-23", 500], ["2026-08-30", 900]])]
    # too young to judge (published yesterday)
    vids_young = [vid("d0", 10, "2026-09-11",
                      [["2026-09-12", 5]])]

    buckets = brain._hour_buckets(vids_17 + vids_20 + vids_08 + vids_young,
                                  st.STATE["video_history"], now_bd=now_bd)
    check("hours: every aged video buckets, young excluded",
          set(buckets) == {8, 17, 20} and 10 not in buckets)
    check("hours: velocity is the 3-day snapshot, not the latest",
          sorted(buckets[17]) == [40, 41, 42, 43])

    sent.clear()
    st.STATE["best_hour"] = 17
    hour = brain.learn_best_hour(vids_17 + vids_20 + vids_08 + vids_young)
    check("best hour: moves to the stronger hour", hour == 20)
    check("best hour: the 900-view loner can't win without 3+ videos",
          st.STATE["best_hour"] == 20)
    check("best hour: move is announced with numbers",
          any("Best hour moved" in m and "20:00" in m for m in sent))

    # a 20% margin is required: 17:00 at ~40 vs 20:00 at 45 stays put
    # (08-24 is the first snapshot date 3+ days after an 08-20 publish)
    st.STATE["video_history"] = {}
    st.STATE["best_hour"] = 17
    vids_17b = [vid(f"e{i}", 17, "2026-08-20",
                    [["2026-08-24", 38 + i]]) for i in range(4)]
    vids_20b = [vid(f"f{i}", 20, "2026-08-20",
                    [["2026-08-24", 45 + i]]) for i in range(4)]
    sent.clear()
    hour = brain.learn_best_hour(vids_17b + vids_20b)
    check("best hour: under 20% margin keeps the current hour",
          hour == 17 and st.STATE["best_hour"] == 17 and not sent)

    # no qualifying buckets at all -> fallback
    check("best hour: no data keeps stored hour",
          brain.learn_best_hour([]) == 17)

    # record_video_history: appends per day, updates same-day, skips
    # private, caps rows
    st.STATE["video_history"] = {}
    brain.record_video_history([
        {"id": "p1", "privacy": "public", "views": 10},
        {"id": "priv", "privacy": "private", "views": 99}])
    brain.record_video_history([
        {"id": "p1", "privacy": "public", "views": 15}])
    rows = st.STATE["video_history"]["p1"]
    check("history: one row per day, same-day updates in place",
          len(rows) == 1 and rows[0][1] == 15)
    check("history: private videos never recorded",
          "priv" not in st.STATE["video_history"])
    # 35 days of backfilled rows + today's append -> trimmed to 30
    st.STATE["video_history"]["p1"] = [
        [f"2026-08-{d:02d}", d] for d in range(1, 36)]
    brain.record_video_history([{"id": "p1", "privacy": "public",
                                 "views": 99}])
    check("history: rows capped at 30",
          len(st.STATE["video_history"]["p1"]) == 30
          and st.STATE["video_history"]["p1"][-1][1] == 99)

    # ------------------------------------------------------------------
    # 3. title-swap verdicts (clock still pinned to 2026-09-12 BD)
    # ------------------------------------------------------------------
    st.STATE["title_swaps"] = {
        # a week old, CTR available pre and post
        "v1": {"from": "Old", "to": "New One", "when": "2026-09-01",
               "pre_ctr": 0.020, "pre_views": 40, "verdict": None},
        # only 3 days old — must wait
        "v2": {"from": "Old", "to": "New Two", "when": "2026-09-09",
               "pre_ctr": 0.020, "pre_views": 40, "verdict": None},
        # no CTR anywhere
        "v3": {"from": "Old", "to": "New Three", "when": "2026-09-01",
               "pre_ctr": None, "pre_views": 40, "verdict": None},
        # already judged — never again
        "v4": {"from": "Old", "to": "New Four", "when": "2026-08-01",
               "pre_ctr": 0.02, "pre_views": 40,
               "verdict": "worked — keep it"},
    }
    report = {
        "v1": {"ctr": 0.031, "views": 130},
        "v2": {"ctr": 0.05, "views": 90},
        "v3": {"views": 75},
    }
    sent.clear()
    brain._swap_verdicts(report)
    sw = st.STATE["title_swaps"]
    check("verdict: CTR gain judged worked",
          sw["v1"]["verdict"] == "worked — keep it")
    check("verdict: worked verdict announced",
          any("Title swap verdict" in m and "New One" in m
              and "2.0% → 3.1%" in m for m in sent))
    check("verdict: under 7 days waits",
          sw["v2"]["verdict"] is None)
    check("verdict: no CTR is honest, not pretend",
          "inconclusive" in (sw["v3"]["verdict"] or ""))
    check("verdict: judged entries never re-judged",
          sw["v4"]["verdict"] == "worked — keep it")

    # a flat CTR verdict says revert
    st.STATE["title_swaps"] = {
        "v5": {"from": "Old", "to": "New Five", "when": "2026-09-01",
               "pre_ctr": 0.030, "pre_views": 40, "verdict": None}}
    sent.clear()
    brain._swap_verdicts({"v5": {"ctr": 0.031, "views": 55}})
    check("verdict: flat CTR suggests reverting",
          "no clear CTR gain" in st.STATE["title_swaps"]["v5"]["verdict"])

    # empty analytics for an old swap -> "no data", once
    st.STATE["title_swaps"] = {
        "v6": {"from": "Old", "to": "New Six", "when": "2026-09-01",
               "pre_ctr": None, "pre_views": 40, "verdict": None}}
    brain._swap_verdicts({})
    check("verdict: nothing at all -> no data, marked done",
          st.STATE["title_swaps"]["v6"]["verdict"] == "no data")

    # ------------------------------------------------------------------
    # 4. quota-aware planning (clock still pinned: 2026-09-12 20:00 BD)
    # ------------------------------------------------------------------
    # the window opened at 13:00 BD today = 07:00 UTC; the expected epoch
    # is computed with the same naive-datetime conversion the brain uses,
    # so the check holds on any machine's timezone
    win_start = datetime(2026, 9, 12, 7, 0).timestamp()

    def dj(updated, urls):
        return {"id": f"j{int(updated)}", "type": "render",
                "status": "done", "updated": updated,
                "result": {"video_urls": urls}}

    st.STATE["jobs"] = [
        dj(win_start + 60, ["u1", "u2", "u3"]),   # 3 uploads this window
        dj(win_start - 60, ["u4"]),               # last window: ignored
        {"id": "jx", "type": "render", "status": "failed",
         "updated": win_start + 60, "result": {}},   # failed: ignored
    ]
    check("quota: window opens at 13:00 BD (07:00 UTC)",
          abs(brain._quota_window_start() - win_start) < 1)
    check("quota: counts done uploads inside the window only",
          brain._quota_used() == 3)
    check("quota: 3 of 6 is not full", not brain.quota_full())
    st.STATE["jobs"].append(dj(win_start + 120, ["u5", "u6", "u7"]))
    check("quota: 6 uploads fills the window", brain.quota_full())

    # a full window defers the batch instead of generating into a wall
    st.STATE["quota_deferred"] = 0
    sent.clear()
    made = brain.queue_next_video(1)
    check("quota: full window defers, nothing generated",
          made == 0 and st.STATE["quota_deferred"] == 1
          and any("quota full" in m for m in sent))
    st.STATE["jobs"] = []
    st.STATE["quota_deferred"] = 0

    # ------------------------------------------------------------------
    # 5. answer-Shorts: the second ask spawns the draft
    # ------------------------------------------------------------------
    import threading as _th
    got, ev = {}, _th.Event()

    def fake_qas(entry):
        got.update(entry)
        ev.set()
    orig_qas, orig_qf = brain._queue_answer_short, brain.quota_full
    brain._queue_answer_short = fake_qas
    brain.quota_full = lambda: False
    st.STATE["settings"]["paused"] = False

    replies.append('{"topic": "mirror touch illusion"}')
    brain._mine_requests([c("c9", "please cover the mirror touch illusion")])
    check("answer-short: one ask spawns nothing", not ev.is_set())
    replies.append('{"topic": "Mirror Touch Illusion"}')   # same dedupe key
    brain._mine_requests([c("c10", "mirror touch again please", author="Z")])
    check("answer-short: the second ask drafts the Short",
          ev.wait(2) and got.get("topic") == "Mirror Touch Illusion")
    entry = next(e for e in st.STATE["audience_requests"]
                 if e["key"] == "mirror touch illusion")
    check("answer-short: the request is marked answered (once ever)",
          entry.get("answered") is True)
    ev.clear()
    replies.append('{"topic": "mirror touch illusion"}')
    brain._mine_requests([c("c11", "a third mirror touch ask", author="Y")])
    check("answer-short: the third ask never re-spawns",
          not ev.wait(0.3) and entry["count"] == 3)

    # a full quota skips the spawn and leaves the request unanswered
    brain.quota_full = lambda: True
    replies.append('{"topic": "priming numbers"}')
    brain._mine_requests([c("c12", "do one on priming numbers")])
    replies.append('{"topic": "Priming Numbers"}')
    brain._mine_requests([c("c13", "priming numbers please", author="W")])
    entry2 = next(e for e in st.STATE["audience_requests"]
                  if e["key"] == "priming numbers")
    check("answer-short: full quota defers the spawn (no answered mark)",
          not ev.is_set() and entry2["count"] == 2
          and not entry2.get("answered"))
    brain.quota_full = orig_qf

    # _write_short: 4 scenes parse, short-only, prefix-free, QA attached
    orig_research, orig_hook = brain._research, brain._score_hook
    orig_fact, orig_pick = brain._fact_check, brain._pick_thumbnail
    brain._research = lambda d, u: None
    brain._score_hook = lambda s: (80, "fine")
    brain._fact_check = lambda s, r: (True, [])
    brain._pick_thumbnail = lambda s: None
    short_json = ('{"id": "mirror-touch", "title": "mind trick #5: You Feel '
                  'What You See", "format": ["short"], "hook": "Touch your '
                  'own cheek. Now watch my hand.", "scenes": ['
                  + ",".join('{"narration": "Scene %d words here.", '
                             '"short_narration": "Scene %d.", '
                             '"short_title": "T%d", "archive_search": ["a"], '
                             '"visual_keywords": ["k"], "source": "s", '
                             '"in_short": true}' % (i, i, i)
                             for i in range(4))
                  + '], "outro": "Watch it again."}')
    replies.append(short_json)
    sc = brain._write_short("mirror touch illusion")
    check("write-short: 4 scenes parse with the short floor",
          sc is not None and len(sc["scenes"]) == 4)
    check("write-short: short-only format, series prefix stripped",
          sc["format"] == ["short"]
          and sc["title"] == "You Feel What You See")
    check("write-short: hook + fact gates ran, retention skipped",
          sc["_qa"]["hook"] == 80 and sc["_qa"]["facts"] == []
          and sc["_qa"]["retention"] is None)
    # a 2-scene stub is rejected (min_scenes=4), retried, rejected -> None
    replies.append('{"id": "stub", "title": "T", "hook": "h", '
                   '"scenes": [{"narration": "a", "visual_keywords": ["k"]},'
                   '{"narration": "b", "visual_keywords": ["k"]}]}')
    replies.append("")     # the retry gets nothing back
    check("write-short: under-floor stubs never ship",
          brain._write_short("stub topic") is None)
    brain._research, brain._score_hook = orig_research, orig_hook
    brain._fact_check, brain._pick_thumbnail = orig_fact, orig_pick

    # ------------------------------------------------------------------
    # 6. thumbnail A/B (pivot floor dropped for mechanics, as in the
    #    title-swap suite; clock still pinned)
    # ------------------------------------------------------------------
    orig_pivot = brain.PIVOT_DATE
    brain.PIVOT_DATE = "2000-01-01"
    st.STATE["thumb_bank"] = {
        "t1": {"text": "OLD", "concept": "old concept",
               "alternates": [{"text": "NEW", "concept": "new concept"}]},
        "t3": {"text": "NOALT", "concept": "x", "alternates": []},
        "t5": {"text": "T5", "concept": "y",
               "alternates": [{"text": "T5NEW", "concept": "y2"}]},
        "t6": {"text": "T6", "concept": "z",
               "alternates": [{"text": "T6NEW", "concept": "z2"}]},
    }
    st.STATE["thumb_swaps"] = {}
    orig_add_job, orig_wake = brain.jobs.add_job, brain.cloud.wake_soon
    added = []
    brain.jobs.add_job = lambda t, p: (added.append((t, p)),
                                       {"id": "thumbjob"})[1]
    brain.cloud.wake_soon = lambda *a, **k: None
    old = (datetime(2026, 9, 12) - timedelta(days=10)).strftime("%Y-%m-%d")
    fresh = (datetime(2026, 9, 12) - timedelta(days=2)).strftime("%Y-%m-%d")
    vids = [
        # long, 10 days old, weak by CTR -> swap
        {"id": "t1", "title": "Mind Trick #3: X", "views": 5,
         "privacy": "public", "published": old, "duration_s": 400},
        {"id": "t2", "title": "Big", "views": 100, "privacy": "public",
         "published": old, "duration_s": 400},
        {"id": "t4", "title": "Mid", "views": 70, "privacy": "public",
         "published": old, "duration_s": 400},
        # weak by CTR but no alternates banked -> skip
        {"id": "t3", "title": "NoAlt", "views": 5, "privacy": "public",
         "published": old, "duration_s": 400},
        # weak + alternates but only 2 days old -> too young
        {"id": "t5", "title": "Young", "views": 5, "privacy": "public",
         "published": fresh, "duration_s": 400},
        # weak + old but a Short (under 3 min) -> Shorts ignore thumbnails
        {"id": "t6", "title": "Shorty", "views": 5, "privacy": "public",
         "published": old, "duration_s": 60},
    ]
    # weakness comes from CTR (< 4%): four videos sit at 5 views, so the
    # views-vs-median route could never fire (the median IS 5)
    ctr_report = {"t1": {"ctr": 0.02}, "t3": {"ctr": 0.02},
                  "t5": {"ctr": 0.02}, "t6": {"ctr": 0.02}}
    sent.clear()
    brain._thumb_check(vids, ctr_report)
    check("thumb: the underperformer with alternates gets one swap job",
          len(added) == 1 and added[0][0] == "thumb"
          and added[0][1]["thumbnail"]["text"] == "NEW"
          and added[0][1]["video_url"] == "https://youtu.be/t1")
    sw = st.STATE["thumb_swaps"]["t1"]
    check("thumb: the ledger records from/to and the original for revert",
          sw["from"] == "OLD" and sw["to"] == "NEW"
          and sw["orig"] == {"text": "OLD", "concept": "old concept"}
          and sw["verdict"] is None)
    check("thumb: the swap is announced", any("Thumbnail auto-swapped" in m
                                              for m in sent))
    # second pass: the ledger blocks a re-swap
    added.clear()
    brain._thumb_check(vids, ctr_report)
    check("thumb: one swap per video ever", not added)

    # the manual path (the panel's A/B button) shares the same engine:
    # one job, same ledger shape, and a second attempt is refused
    added.clear()
    ok = brain.queue_thumb_test(
        {"id": "t7", "title": "Manual Pick", "views": 42},
        {"text": "PICKED", "concept": "picked concept"}, ctr=0.033)
    check("thumb manual: queues exactly one job with the chosen concept",
          ok and len(added) == 1 and added[0][0] == "thumb"
          and added[0][1]["thumbnail"]["text"] == "PICKED")
    sw7 = st.STATE["thumb_swaps"]["t7"]
    check("thumb manual: same ledger shape with the CTR baseline",
          sw7["to"] == "PICKED" and sw7["pre_ctr"] == 0.033
          and sw7["pre_views"] == 42 and sw7["verdict"] is None)
    check("thumb manual: one swap per video ever here too",
          brain.queue_thumb_test(
              {"id": "t7", "title": "Manual Pick", "views": 42},
              {"text": "OTHER", "concept": "x"}) is False)

    # verdicts: a clear CTR loss reverts via a second thumb job
    sw["when"] = "2026-09-01"
    sw["pre_ctr"] = 0.040
    added.clear()
    sent.clear()
    brain._thumb_verdicts({"t1": {"ctr": 0.010, "views": 60}})
    check("thumb verdict: clear loss reverts to the original concept",
          any(t == "thumb" and p["thumbnail"]["text"] == "OLD"
              for t, p in added)
          and "revert" in sw["verdict"])
    check("thumb verdict: the revert is announced",
          any("Thumbnail swap verdict" in m for m in sent))
    # a win keeps it: no second job
    sw["verdict"] = None
    sw["pre_ctr"] = 0.020
    added.clear()
    brain._thumb_verdicts({"t1": {"ctr": 0.035, "views": 90}})
    check("thumb verdict: a win keeps the new concept (no job)",
          not added and "worked" in sw["verdict"])
    brain.PIVOT_DATE = orig_pivot
    brain.jobs.add_job = orig_add_job
    brain.cloud.wake_soon = orig_wake

    brain._queue_answer_short = real_qas
    brain.comms.send = orig_send
    brain.gemini = orig_gemini
    brain.datetime = orig_dt

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all learning-loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
