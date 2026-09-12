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

    # once ever per comment: re-mining the same ids does nothing
    brain.gemini = fake_gemini
    replies.append('{"topic": "should not appear"}')
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
