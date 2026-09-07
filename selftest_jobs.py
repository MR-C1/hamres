"""Check the job-list merge without touching the network.

state.reload_jobs() pulls the gist's copy of the queue and folds it into
the one in memory, and save_now() does the same fold just before every
write (_absorb_remote). Getting the merge wrong loses renders two ways:
the old rule rebuilt the list from the gist alone, so a job queued
seconds earlier — appended locally, still mid-write — disappeared; and
during a Render deploy overlap the new dyno's stale copy reverted a
claim the draining dyno had already written, so a second runner
re-rendered the same script and the finished render's report was eaten.

_merge_jobs is pure and state.py imports nothing heavy at module level,
so this runs in milliseconds:

    python selftest_jobs.py
"""
import sys
import time

import state
from state import _merge_jobs


def job(jid, status="pending", created=0.0, **extra):
    j = {"id": jid, "type": "render", "status": status, "created": created,
         "updated": created}
    j.update(extra)
    return j


def main():
    fails = []

    def check(label, got, want):
        ok = want(got) if callable(want) else got == want
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}")
        if not ok:
            fails.append(label)

    ids = lambda rows: [r["id"] for r in rows]

    # the bug that lost renders: add_job appended locally and the gist write
    # had not landed when a worker poll reloaded the queue
    check("a job the gist hasn't seen survives",
          ids(_merge_jobs([job("a", created=1), job("fresh", created=2)],
                          [job("a", created=1)])),
          ["a", "fresh"])

    # status is decided in this process, so the local copy is never the stale one
    check("local status wins over the gist's",
          [j["status"] for j in _merge_jobs([job("a", "claimed", 1)],
                                            [job("a", "pending", 1)])],
          ["claimed"])

    # a job queued by another process (or before a restart) has to appear
    check("a job only the gist has is picked up",
          ids(_merge_jobs([job("a", created=1)],
                          [job("a", created=1), job("b", created=2)])),
          ["a", "b"])

    # the gist is written by several callers; a duplicated id must not double
    check("duplicate ids collapse",
          ids(_merge_jobs([], [job("a", created=1), job("a", created=1)])),
          ["a"])

    # oldest first, whichever side each job came from — next_job() claims in
    # list order, so a scrambled order would serve the newest render first
    check("ordered by creation time",
          ids(_merge_jobs([job("late", created=90)],
                          [job("mid", created=50), job("early", created=10)])),
          ["early", "mid", "late"])

    # add_job keeps the list to 100; the merge must not undo that
    check("stays bounded at 100",
          len(_merge_jobs([job(f"l{i}", created=i) for i in range(80)],
                          [job(f"r{i}", created=100 + i) for i in range(80)])),
          100)
    check("bounding drops the oldest, not the newest",
          _merge_jobs([job(f"l{i}", created=i) for i in range(80)],
                      [job(f"r{i}", created=100 + i) for i in range(80)])[-1]["id"],
          "r79")

    # empty on either side is normal: a fresh deploy, or a gist with no jobs key
    check("nothing on either side", _merge_jobs([], []), [])
    check("empty gist keeps the local queue",
          ids(_merge_jobs([job("a", created=1)], [])), ["a"])

    # a job written before 'created' existed must not crash the sort
    check("a job with no created time is tolerated",
          ids(_merge_jobs([{"id": "old", "status": "pending"}],
                          [job("b", created=5)])),
          ["old", "b"])

    # ---- newest-stamp-wins: the deploy-overlap rules ----
    # every status transition re-stamps `updated`, so the newest stamp is
    # the latest truth whichever process wrote it

    # the incident: a new dyno booted mid-render holding the pre-claim
    # copy while the draining dyno had already written 'claimed' to the gist
    check("a claim made during a deploy overlap survives",
          _merge_jobs([job("a", "pending", created=1, updated=100)],
                      [job("a", "claimed", created=1, updated=200)])[0]["status"],
          "claimed")

    # the flip side: this process's own newer transition beats the gist's
    check("a newer local transition beats the gist's",
          _merge_jobs([job("a", "done", created=1, updated=300)],
                      [job("a", "claimed", created=1, updated=200)])[0]["status"],
          "done")

    # /retry requeues by writing a newer pending stamp — must survive too
    check("a requeued job stays pending over a stale claim",
          _merge_jobs([job("a", "pending", created=1, updated=400)],
                      [job("a", "claimed", created=1, updated=200)])[0]["status"],
          "pending")

    # ---- tombstones: deliberate deletions stay deleted ----

    # clear-queue/prune remove jobs the gist still holds copies of; without
    # the marker the pre-save merge would resurrect them on the next write
    check("a tombstoned job stays deleted on both sides",
          ids(_merge_jobs([job("b", created=2)],
                          [job("a", created=1), job("b", created=2)],
                          tombstones={"a": time.time()})),
          ["b"])
    check("tombstones can wipe the whole queue",
          _merge_jobs([job("a", created=1)], [job("a", created=1)],
                      tombstones={"a": time.time()}),
          [])

    # tombstone_jobs itself: expired markers prune, fresh ones stick
    now = time.time()
    state.STATE.clear()
    state.STATE["job_tombstones"] = {"old": now - 7200, "stale": now - 3660}
    state.tombstone_jobs(["fresh"])
    check("tombstone expiry prunes the stale and keeps the fresh",
          sorted(state.STATE["job_tombstones"]), ["fresh"])
    state.STATE.clear()

    # ---- pending approvals: the lost-approvals rules ----
    # pending_videos merges per-entry now; the incident was a deploy
    # overlap whose last-writer-wins write wiped three owner approvals
    from state import _merge_pending, _merge_stats, DECIDED_EXPIRY

    check("an approval only the gist knows survives the fold",
          sorted(_merge_pending({"local": {}}, {"local": {}, "remote": {}})),
          ["local", "remote"])
    check("an approval only this dyno knows survives the fold",
          sorted(_merge_pending({"mine": {}}, {})), ["mine"])
    check("nothing on either side", _merge_pending({}, {}), {})
    # a decided entry popped here must not resurrect from the gist's copy
    check("a decided approval stays decided",
          _merge_pending({"gone": {}}, {"gone": {}, "kept": {}},
                         decided={"gone"}),
          {"kept": {}})
    check("early decisions merge the same way",
          _merge_pending({"tapped": "approved"},
                         {"tapped": "approved", "wait": "rejected"},
                         decided={"tapped"}),
          {"wait": "rejected"})

    # tombstone_decided: expiry prunes, fresh stick
    state.STATE.clear()
    state.STATE["decided_videos"] = {"old": now - DECIDED_EXPIRY - 3600}
    state.tombstone_decided(["fresh"])
    check("decided tombstone expiry prunes and keeps",
          sorted(state.STATE["decided_videos"]), ["fresh"])
    state.STATE.clear()

    # stats_history unions by date — a deploy overlap blanked it once
    check("stats rows union by date",
          [r["date"] for r in _merge_stats(
              [{"date": "2026-09-05", "subs": 10}],
              [{"date": "2026-09-04", "subs": 8},
               {"date": "2026-09-05", "subs": 10}])],
          ["2026-09-04", "2026-09-05"])
    check("empty stats on either side",
          _merge_stats([], [{"date": "d"}]), [{"date": "d"}])

    print()
    if fails:
        print(f"{len(fails)} FAILED: {', '.join(fails)}")
        return 1
    print("all job-merge checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
