"""Check the job-list merge without touching the network.

state.reload_jobs() pulls the gist's copy of the queue and folds it into
the one in memory. Getting that merge wrong loses renders: the old rule
rebuilt the list from the gist alone, so a job queued seconds earlier —
appended locally, still mid-write — disappeared, and the next save wrote
the shortened list back over it.

_merge_jobs is pure, and state.py imports only requests + stdlib at module
level, so this runs in milliseconds:

    python selftest_jobs.py
"""
import sys

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

    print()
    if fails:
        print(f"{len(fails)} FAILED: {', '.join(fails)}")
        return 1
    print("all job-merge checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
