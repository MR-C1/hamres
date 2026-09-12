"""Offline self-test for the cross-post (Instagram Reels + TikTok
drafts) — no network, no keys.

Proves, end to end with faked HTTP:
  - ig.publish_reel: container → poll FINISHED → publish, the happy
    path returns a reel link; processing errors, status polling
    failures and HTTP exceptions all fail clean (False, reason)
  - ig.refresh_token: stores the refreshed token in state (env vars
    are read-only), silent False on a dead grant
  - tiktok.inbox_post: downloads the staged asset, chunks at TikTok's
    required 4 MB boundary (final chunk smaller), PUTs with correct
    Content-Range headers, reports the draft
  - tiktok.refresh: stores the ROTATED refresh token — losing it kills
    the grant forever — and _access() refreshes near expiry
  - ghassets.upload: date-stamped name, replaces an earlier attempt of
    the same render, returns the browser_download_url; prune drops
    30-day-old assets
  - the report endpoint stores asset_urls/description; the ✅ path
    (publish) cross-posts IG + TikTok and reports every outcome

    python selftest_crosspost.py
"""
import json
import sys
import types

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)


def fresh(module_name, fake_mods):
    """Reimport a module with a clean slate and fakes installed."""
    sys.modules.pop(module_name, None)
    for k in list(sys.modules):
        if k.startswith("_st_fake") or k in fake_mods:
            sys.modules.pop(k, None)
    for k, v in fake_mods.items():
        sys.modules[k] = v
    return __import__(module_name)


class FakeResp:
    def __init__(self, ok=True, status=200, json_data=None, text=""):
        self.ok = ok
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def main():
    import state as st
    st.STATE.clear()
    st.LOADED = True

    # silence state saves (no gist writes from tests)
    st.save_soon = lambda: None
    st.save_now = lambda: None

    sent = []

    # ------------------------------------------------------------------
    # 1. ig.publish_reel — happy path
    # ------------------------------------------------------------------
    calls = []

    def ig_post(url, **kw):
        calls.append(("post", url))
        if url.endswith("/media"):
            return FakeResp(json_data={"id": "c123"})
        if url.endswith("/media_publish"):
            return FakeResp(json_data={"id": "r456"})
        return FakeResp(ok=False, json_data={"error": {"message": "?"}})

    def ig_get(url, **kw):
        calls.append(("get", url))
        if "/c123" in url:
            return FakeResp(json_data={"status_code": "FINISHED"})
        return FakeResp(json_data={})

    fake_req = types.SimpleNamespace(
        post=ig_post, get=ig_get,
        exceptions=types.SimpleNamespace(RequestException=RuntimeError))
    ig = fresh("ig", {"requests": fake_req})
    ig.state = st
    ig.config = types.SimpleNamespace(IG_ACCESS_TOKEN="tok", IG_USER_ID="u1")
    ig.sleep = lambda s: None            # no waiting in tests
    ok, detail = ig.publish_reel("https://example/v.mp4", "cap")
    check("ig: happy path publishes and returns the reel link",
          ok and detail == "https://www.instagram.com/reel/r456/")
    check("ig: sequence is container → poll → publish",
          calls[0][1].endswith("/media")
          and calls[1][1].endswith("/c123")
          and calls[2][1].endswith("/media_publish"))

    # processing error surfaces
    def ig_get_err(url, **kw):
        if "/c123" in url:
            return FakeResp(json_data={"status_code": "ERROR"})
        return FakeResp(json_data={})
    fake_req.get = ig_get_err
    ok, detail = ig.publish_reel("https://example/v.mp4", "cap")
    check("ig: ERROR status fails clean with a reason",
          not ok and "process" in detail.lower())
    fake_req.get = ig_get

    # container creation refused
    fake_req.post = lambda url, **kw: FakeResp(
        json_data={"error": {"message": "unsupported format"}})
    ok, detail = ig.publish_reel("https://example/v.mp4", "cap")
    check("ig: API error message comes back, never raises",
          not ok and "unsupported" in detail)

    # HTTP exception fails clean
    def boom(url, **kw):
        raise RuntimeError("connection reset")
    fake_req.post = boom
    ok, detail = ig.publish_reel("https://example/v.mp4", "cap")
    check("ig: transport exception -> (False, reason)",
          not ok and "connection reset" in detail)

    # ------------------------------------------------------------------
    # 2. ig.refresh_token — refreshed copy persists in state
    # ------------------------------------------------------------------
    def refresh_ok(url, **kw):
        return FakeResp(json_data={"access_token": "NEW",
                                   "expires_in": 5184000})
    fake_req.get = refresh_ok
    st.STATE["ig"] = {"token": "OLD", "user_id": "u1"}
    check("ig: refresh stores the new token in state",
          ig.refresh_token()
          and st.STATE["ig"]["token"] == "NEW")
    fake_req.get = lambda url, **kw: FakeResp(json_data={})
    st.STATE["ig"] = {"token": "DEAD", "user_id": "u1"}
    check("ig: dead grant -> silent False, no raise", not ig.refresh_token())

    # ------------------------------------------------------------------
    # 3. tiktok.inbox_post — chunked upload with correct ranges
    # ------------------------------------------------------------------
    ranges = []

    def tt_download(url, **kw):
        # 9 MB "video": 4 MB + 4 MB + 1 MB chunks
        class Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, n):
                yield b"x" * (4 * 1024 * 1024)
                yield b"x" * (4 * 1024 * 1024)
                yield b"x" * (1 * 1024 * 1024)
        return Ctx()

    def tt_init(url, **kw):
        return FakeResp(json_data={"data": {"upload_url":
                                            "https://up/123"}})

    def tt_put(url, data=None, headers=None, **kw):
        ranges.append((headers.get("Content-Range"), len(data)))
        return FakeResp(status=201, ok=True)

    fake_req2 = types.SimpleNamespace(
        get=tt_download, post=tt_init, put=tt_put,
        exceptions=types.SimpleNamespace(RequestException=RuntimeError))
    tk = fresh("tiktok", {"requests": fake_req2})
    tk.state = st
    tk.config = types.SimpleNamespace(
        TIKTOK_CLIENT_KEY="k", TIKTOK_CLIENT_SECRET="s",
        TIKTOK_ACCESS_TOKEN="", TIKTOK_REFRESH_TOKEN="",
        TIKTOK_OPEN_ID="oi")
    st.STATE["tiktok"] = {"access_token": "at", "refresh_token": "rt",
                          "open_id": "oi", "expires": 9999999999}
    ok, detail = tk.inbox_post("https://example/s.mp4")
    check("tt: inbox post uploads and reports the draft",
          ok and "draft" in detail.lower())
    check("tt: 4 MB chunks (final smaller) with exact Content-Range",
          len(ranges) == 3
          and ranges[0][0] == "bytes 0-4194303/9437184"
          and ranges[1][0] == "bytes 4194304-8388607/9437184"
          and ranges[2][0] == "bytes 8388608-9437183/9437184"
          and ranges[2][1] == 1 * 1024 * 1024)

    # init refused -> clean failure
    def tt_init_bad(url, **kw):
        return FakeResp(json_data={"error": {"message": "scope missing"}})
    fake_req2.post = tt_init_bad
    ok, detail = tk.inbox_post("https://example/s.mp4")
    check("tt: API refusal -> (False, reason)",
          not ok and "scope" in detail)
    fake_req2.post = tt_init

    # ------------------------------------------------------------------
    # 4. tiktok.refresh — the ROTATING refresh token is stored
    # ------------------------------------------------------------------
    def tt_refresh(url, **kw):
        # assert it was called with the CURRENT refresh token
        if kw.get("data", {}).get("refresh_token") != "rt":
            return FakeResp(json_data={})
        return FakeResp(json_data={"data": {
            "access_token": "at2",
            "refresh_token": "rt2",      # rotated!
            "open_id": "oi",
            "expires_in": 86400}})
    fake_req2.post = tt_refresh
    check("tt: refresh stores the ROTATED refresh token",
          tk.refresh()
          and st.STATE["tiktok"]["refresh_token"] == "rt2"
          and st.STATE["tiktok"]["access_token"] == "at2")

    # _access() refreshes when within an hour of expiry
    st.STATE["tiktok"]["expires"] = 1     # long past
    fake_req2.get = tt_download
    a = tk._access()
    check("tt: near-expiry access token auto-refreshes",
          a == "at2")

    # ------------------------------------------------------------------
    # 5. ghassets — date-stamped upload, replace same-render asset
    # ------------------------------------------------------------------
    uploaded = {}

    def gh_get(url, **kw):
        if "/releases/tags/" in url:
            return FakeResp(json_data={
                "id": 99, "assets": [
                    {"id": 1, "name": "20260901-abc_short.mp4",
                     "updated_at": "2026-09-01T00:00:00Z"}]})
        return FakeResp(ok=False)

    def gh_post(url, headers=None, data=None, json=None, **kw):
        if "/releases" in url and "assets" not in url:
            return FakeResp(json_data={"id": 88})
        uploaded[url] = data
        return FakeResp(json_data={"browser_download_url":
                                   "https://dl/xyz"})
    deleted = []

    def gh_delete(url, **kw):
        deleted.append(url)
        return FakeResp()

    fake_req3 = types.SimpleNamespace(
        get=gh_get, post=gh_post, delete=gh_delete)
    sys.path.insert(0, "worker")   # ghassets is a worker module (flat)
    gha = fresh("ghassets", {"requests": fake_req3})
    import os as _os
    gha.os.environ = {"GITHUB_REPOSITORY": "MR-C1/hamres",
                      "GITHUB_TOKEN": "gt"}
    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"video")
        tmpf = f.name
    url = gha.upload(tmpf, "abc_short.mp4")
    _os.unlink(tmpf)
    import time as _t
    stamp = _t.strftime("%Y%m%d")
    check("gh: upload returns the public download URL",
          url == "https://dl/xyz")
    check("gh: asset name is date-stamped",
          f"name={stamp}-abc_short.mp4" in next(iter(uploaded)))
    check("gh: an earlier attempt of the same render is replaced",
          any("/assets/1" in d for d in deleted))

    # prune drops 30-day-old assets only
    gh_get.assets_seen = True

    def gh_get2(url, **kw):
        if "/releases/tags/" in url:
            return FakeResp(json_data={
                "id": 99, "assets": [
                    {"id": 1, "name": "old.mp4",
                     "updated_at": "2026-08-01T00:00:00Z"},
                    {"id": 2, "name": "new.mp4",
                     "updated_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ")}]})
        return FakeResp(ok=False)
    fake_req3.get = gh_get2
    deleted.clear()
    gha.prune()
    check("gh: prune drops only 30-day-old assets",
          any("/assets/1" in d for d in deleted)
          and not any("/assets/2" in d for d in deleted))

    # ------------------------------------------------------------------
    # 6. report endpoint stores cross-post material; ✅ cross-posts
    # ------------------------------------------------------------------
    fake_ig = types.ModuleType("_st_fake_ig")
    fake_ig.configured = lambda: True
    fake_ig.publish_reel = lambda a, c: (True,
                                         "https://www.instagram.com/reel/x/")
    fake_ig.refresh_token = lambda: True
    fake_tk = types.ModuleType("_st_fake_tiktok")
    fake_tk.configured = lambda: True
    fake_tk.inbox_post = lambda a: (True, "draft created")
    fake_tk.refresh = lambda: True

    import config as real_config
    real_config.WORKER_SECRET = "s3cret"
    real_config.TELEGRAM_BOT_TOKEN = "bt"
    real_config.OWNER_CHAT_ID = "oc"
    real_config.PANEL_ADMIN_PASSWORD = ""
    real_config.PANEL_VISITOR_PASSWORD = ""

    fake_comms_mod = types.ModuleType("_st_fake_comms")
    fake_comms_mod.LOG = []
    fake_comms_mod.esc = lambda s: str(s)

    def fake_send(m, html=False):
        sent.append(m)

    fake_comms_mod.send = fake_send
    fake_comms_mod.send_md = fake_send
    fake_comms_mod.send_buttons = fake_send
    fake_comms_mod.log = lambda m: fake_comms_mod.LOG.append(m)
    fake_comms_mod.register_menu = lambda: None

    fake_yt = types.ModuleType("_st_fake_yt")

    def fake_sched(url, t):
        fake_yt.calls = getattr(fake_yt, "calls", []) + [url]
        return True
    fake_yt.schedule_public = fake_sched
    fake_yt.make_public = fake_sched

    import brain as _b
    fake_yt.my_videos = lambda *a, **k: []
    fake_yt.channel_stats = lambda: {"subs": 1, "views": 1, "videos": 1,
                                     "title": "t",
                                     "uploads_playlist": "up"}

    app = fresh("app", {"ig": fake_ig, "tiktok": fake_tk,
                        "comms": fake_comms_mod, "yt": fake_yt,
                        "config": real_config})
    app.state = st
    st.STATE["settings"]["paused"] = False

    client = app.app.test_client()
    r = client.post("/report",
                    headers={"X-Worker-Secret": "s3cret"},
                    json={"job_id": "jx", "ok": True, "title": "T",
                          "video_url": "https://youtu.be/aa",
                          "video_urls": ["https://youtu.be/aa",
                                         "https://youtu.be/bb"],
                          "asset_urls": {"short": "https://dl/s.mp4"},
                          "description": "the caption",
                          "timeline": [], "format_urls": {}})
    pend = st.STATE["pending_videos"].get("jx")
    check("report: asset_urls + description stored for the cross-post",
          pend and pend.get("asset_urls", {}).get("short")
          == "https://dl/s.mp4" and pend.get("description") == "the caption")

    # publish (✅) -> cross-post fires with the staged asset
    sent.clear()
    okp = app._publish_now("jx")
    check("publish: YouTube schedule succeeds",
          okp and "jx" not in st.STATE["pending_videos"])
    check("publish: Instagram Reel cross-posted from the staged URL",
          any("Instagram" in m and "Reel live" in m for m in sent))
    check("publish: TikTok draft cross-posted",
          any("TikTok" in m and "draft" in m.lower() for m in sent))

    # not-configured platforms are reported, not swallowed
    fake_ig.configured = lambda: False
    fake_tk.configured = lambda: False
    st.STATE["pending_videos"]["jy"] = {
        "title": "T2", "video_url": "https://youtu.be/cc",
        "video_urls": ["https://youtu.be/cc"], "asset_urls":
        {"short": "https://dl/s2.mp4"}, "description": "c2"}
    sent.clear()
    app._publish_now("jy")
    check("publish: unconfigured platforms say so in Telegram",
          any("Instagram" in m and "not configured" in m for m in sent)
          and any("TikTok" in m and "not configured" in m for m in sent))

    # a pending entry with NO staged asset (old render / PC worker)
    st.STATE["pending_videos"]["jz"] = {
        "title": "T3", "video_url": "https://youtu.be/dd",
        "video_urls": ["https://youtu.be/dd"], "asset_urls": {},
        "description": ""}
    sent.clear()
    app._publish_now("jz")
    check("publish: missing staged asset is explained, not silent",
          any("Cross-post" in m and "no staged Short" in m for m in sent))

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all cross-post checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
