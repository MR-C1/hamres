"""Offline self-test for ghassets.upload() — no network, no token needed.
Fakes `requests` and the env so every branch runs, and LOCKS the one bug
that silently starved the Instagram/TikTok drip for weeks: release-asset
bytes must POST to uploads.github.com (from the release's upload_url), NOT
api.github.com, which 404s.

    python selftest_ghassets.py
"""
import os
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)


class Resp:
    def __init__(self, code=200, js=None):
        self.status_code = code
        self.ok = code < 400
        self._js = js or {}

    def json(self):
        return self._js

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"{self.status_code} error")


def main():
    os.environ["GITHUB_REPOSITORY"] = "MR-C1/hamres"
    os.environ["GITHUB_TOKEN"] = "x-token"

    posts = []      # (url, has_bytes)
    deletes = []

    UPLOAD_TMPL = ("https://uploads.github.com/repos/MR-C1/hamres/releases/"
                   "42/assets{?name,label}")
    release = {"id": 42, "upload_url": UPLOAD_TMPL,
               "assets": [{"id": 9, "name": "20200101-clip_short.mp4"}]}

    def fake_get(url, **kw):
        # the release lookup by tag lives on the api host — that part is right
        check("release is fetched from api.github.com",
              url.startswith("https://api.github.com/repos/MR-C1/hamres/"
                             "releases/tags/media-cache"))
        return Resp(200, release)

    def fake_post(url, **kw):
        posts.append((url, bool(kw.get("data"))))
        return Resp(201, {"browser_download_url":
                          "https://example/dl/clip_short.mp4"})

    def fake_delete(url, **kw):
        deletes.append(url)
        return Resp(204)

    fake_requests = types.ModuleType("requests")
    fake_requests.get = fake_get
    fake_requests.post = fake_post
    fake_requests.delete = fake_delete
    sys.modules["requests"] = fake_requests

    import ghassets

    tmp = os.path.join(os.path.dirname(__file__) or ".", "_st_ghassets.bin")
    with open(tmp, "wb") as f:
        f.write(b"video-bytes")
    try:
        url = ghassets.upload(tmp, "clip_short.mp4")
    finally:
        os.remove(tmp)

    check("available() true with repo+token set", ghassets.available() is True)
    check("upload returned the browser_download_url",
          url == "https://example/dl/clip_short.mp4")
    check("exactly one asset POST was made", len(posts) == 1)
    up_url, had_bytes = posts[0] if posts else ("", False)
    # THE bug: the bytes must go to uploads.github.com, never api.github.com
    check("asset bytes POST to uploads.github.com (the fix)",
          up_url.startswith("https://uploads.github.com/repos/MR-C1/hamres/"
                            "releases/42/assets"))
    check("asset POST is NOT the api.github.com host that 404s",
          "api.github.com" not in up_url)
    check("the RFC-6570 {?name,label} template was stripped",
          "{" not in up_url and "?name=" in up_url)
    check("the name is date-stamped", "-clip_short.mp4" in up_url)
    check("the file bytes were sent", had_bytes)
    # the stale same-base asset from an earlier attempt is superseded
    check("a superseding delete hit the api host",
          len(deletes) == 1
          and deletes[0] == ("https://api.github.com/repos/MR-C1/hamres/"
                             "releases/assets/9"))

    # upload_url missing -> hand-built uploads URL, still not the api host
    release["upload_url"] = ""
    built = ghassets._upload_url(release)
    check("fallback builds an uploads.github.com URL",
          built == "https://uploads.github.com/repos/MR-C1/hamres/"
                   "releases/42/assets")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all ghassets checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
