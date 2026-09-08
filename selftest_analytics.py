"""Offline self-test for the analytics learning loop — no network, no
keys. Fakes the whole google-api stack the way the live module uses it
(Credentials.refresh inside get_service, build("youtubeanalytics","v2")),
then proves: rows parse into the per-video dict, impressions/CTR are
feature-detected (a refused second query costs nothing), a missing
consent classifies as "scope", a disabled API as "disabled", empty data
as "empty" — and that the brain's planner rows / retention lines / one-
time nag behave with and without analytics.

    python selftest_analytics.py
"""
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)


# ---------------------------------------------------------------- fakes
REFRESH_ERROR = [None]          # raise this in Credentials.refresh
QUERY_RESPONSES = []            # popped per reports().query call
QUERY_ERRORS = []               # raise this on query N instead


class FakeRefreshRequest:
    pass


class FakeCreds:
    def __init__(self, **kw):
        self.kw = kw

    def refresh(self, req):
        if REFRESH_ERROR[0]:
            raise REFRESH_ERROR[0]


class FakeQuery:
    def __init__(self, n):
        self.n = n

    def execute(self):
        if self.n < len(QUERY_ERRORS) and QUERY_ERRORS[self.n]:
            raise QUERY_ERRORS[self.n]
        if self.n < len(QUERY_RESPONSES):
            return QUERY_RESPONSES[self.n]
        return {"columnHeaders": [], "rows": []}


class FakeReports:
    def __init__(self):
        self.calls = 0

    def query(self, **kw):
        q = FakeQuery(self.calls)
        self.calls += 1
        return q


class FakeService:
    reports_obj = FakeReports()

    def reports(self):
        return self.reports_obj


def fake_build(name, ver, credentials=None):
    assert name == "youtubeanalytics" and ver == "v2", (name, ver)
    return FakeService()


def install_fakes():
    g = types.ModuleType("google")
    auth = types.ModuleType("google.auth")
    transport = types.ModuleType("google.auth.transport")
    requests_mod = types.ModuleType("google.auth.transport.requests")
    requests_mod.Request = FakeRefreshRequest
    oauth2 = types.ModuleType("google.oauth2")
    creds_mod = types.ModuleType("google.oauth2.credentials")
    creds_mod.Credentials = FakeCreds
    gapi = types.ModuleType("googleapiclient")
    disc = types.ModuleType("googleapiclient.discovery")
    disc.build = fake_build
    for name, mod in [
            ("google", g), ("google.auth", auth),
            ("google.auth.transport", transport),
            ("google.auth.transport.requests", requests_mod),
            ("google.oauth2", oauth2),
            ("google.oauth2.credentials", creds_mod),
            ("googleapiclient", gapi),
            ("googleapiclient.discovery", disc)]:
        sys.modules[name] = mod
    parent, child = "google", "auth"
    setattr(g, "auth", auth)
    setattr(auth, "transport", transport)
    setattr(transport, "requests", requests_mod)
    setattr(g, "oauth2", oauth2)
    setattr(oauth2, "credentials", creds_mod)
    setattr(gapi, "discovery", disc)


V2_CORE = {
    "columnHeaders": [
        {"name": "video"}, {"name": "views"},
        {"name": "estimatedMinutesWatched"}, {"name": "averageViewDuration"},
        {"name": "averageViewPercentage"}, {"name": "subscribersGained"}],
    "rows": [
        ["abc", 1000, 50.5, 310.0, 34.2, 4],
        ["def", 300, 180.0, 620.0, 41.7, 12],
    ],
}
V2_IMPR = {
    "columnHeaders": [{"name": "video"}, {"name": "impressions"},
                      {"name": "clickThroughRate"}],
    "rows": [["abc", 20000, 0.051]],
}


def reset_module():
    import yt_analytics
    yt_analytics._service = None
    FakeService.reports_obj = FakeReports()
    return yt_analytics


def main():
    real_modules = {k: v for k, v in sys.modules.items()
                    if k.startswith(("google", "googleapiclient"))}
    install_fakes()
    import config as cfg
    cfg.YT_REFRESH_TOKEN = "rtok"
    cfg.YT_CLIENT_ID = "cid"
    cfg.YT_CLIENT_SECRET = "csec"
    try:
        # 1. rows parse into the per-video dict
        yta = reset_module()
        REFRESH_ERROR[0] = None
        QUERY_RESPONSES[:] = [V2_CORE, V2_IMPR]
        QUERY_ERRORS[:] = []
        report, reason = yta.video_report(days=28)
        check("report returns ok with data", reason == "ok")
        check("both videos keyed by id", set(report) == {"abc", "def"})
        check("core metrics parsed",
              report["abc"]["views"] == 1000
              and abs(report["abc"]["minutes"] - 50.5) < 0.01
              and abs(report["def"]["avg_pct"] - 41.7) < 0.01
              and report["def"]["subs_gained"] == 12)
        check("impressions feature-detected onto matching video",
              report["abc"].get("impressions") == 20000
              and abs(report["abc"].get("ctr", 0) - 0.051) < 1e-6
              and "impressions" not in report["def"])

        # 2. refused impressions query costs nothing
        yta = reset_module()
        QUERY_RESPONSES[:] = [V2_CORE]
        QUERY_ERRORS[:] = [None, RuntimeError("HTTP 400: metric not "
                                              "supported for channel")]
        report, reason = yta.video_report(days=28)
        check("refused impressions query ignored, core kept",
              reason == "ok" and report["abc"]["views"] == 1000
              and "impressions" not in report["abc"])

        # 3. missing consent -> reason "scope", empty report, no raise
        yta = reset_module()
        REFRESH_ERROR[0] = RuntimeError(
            "invalid_scope: https://oauth2.googleapis.com/token")
        report, reason = yta.video_report(days=28)
        check("invalid_scope classifies as scope",
              reason == "scope" and report == {})

        # 4. API not enabled -> reason "disabled"
        yta = reset_module()
        REFRESH_ERROR[0] = None
        QUERY_RESPONSES[:] = []
        QUERY_ERRORS[:] = [RuntimeError(
            "403 YouTube Analytics API has not been used in project 123 "
            "before or it is disabled")]
        report, reason = yta.video_report(days=28)
        check("service-disabled classifies as disabled",
              reason == "disabled" and report == {})

        # 5. works but no rows -> "empty"
        yta = reset_module()
        QUERY_RESPONSES[:] = [{"columnHeaders": [{"name": "video"}],
                               "rows": []}]
        QUERY_ERRORS[:] = []
        report, reason = yta.video_report(days=28)
        check("no rows classifies as empty", reason == "empty")

        # 6. brain: planner rows with and without analytics
        import brain
        videos = [
            {"id": "abc", "title": "The Oath", "views": 1000, "likes": 10,
             "comments": 2, "published": "2026-09-01", "duration_s": 620},
            {"id": "def", "title": "Hook Short", "views": 300, "likes": 5,
             "comments": 1, "published": "2026-09-02", "duration_s": 40},
        ]
        plain = brain._plan_rows(videos, {})
        check("without analytics rows stay plain",
              plain[0].startswith("- The Oath | 1000 views")
              and "retained" not in plain[0])
        rep = {"abc": {"views": 1000, "minutes": 50.5, "avg_duration": 310,
                       "avg_pct": 34.2, "subs_gained": 4,
                       "impressions": 20000, "ctr": 0.051}}
        rich = brain._plan_rows(videos, rep)
        check("with analytics rows carry retention/subs/ctr",
              "34% retained" in rich[0] and "+4 subs" in rich[0]
              and "CTR 5.1%" in rich[0])
        check("videos without analytics rows degrade per-video",
              "retained" not in rich[1])

        # 7. brain: retention lines split by duration
        pub = videos
        lines = brain._retention_lines(pub)
        # analytics unavailable offline (refresh error is None but the
        # fake service has no rows configured) — must be [] and not raise
        check("retention lines tolerate unavailable analytics",
              isinstance(lines, list))

        # 8. brain: the nag fires once per reason, clears on recovery
        state_calls = []
        brain.state.STATE.clear()
        brain._maybe_nag_analytics("scope")
        n1 = dict(brain.state.STATE.get("analytics_nag", {}))
        brain._maybe_nag_analytics("scope")
        n2 = dict(brain.state.STATE.get("analytics_nag", {}))
        check("scope nag arms once, no repeats", n1 == {"scope": True}
              and n2 == n1)
        brain._maybe_nag_analytics("ok")
        check("recovery clears the nag flags",
              brain.state.STATE.get("analytics_nag") == {})
    finally:
        for k in list(sys.modules):
            if k.startswith(("google", "googleapiclient")) \
                    and k not in real_modules:
                sys.modules.pop(k, None)
        for k, v in real_modules.items():
            sys.modules[k] = v

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all analytics-loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
