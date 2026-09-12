"""Host finished Shorts at public URLs for the Instagram/TikTok
cross-post.

Instagram's publishing API fetches the video from a public URL — it
accepts no file upload on the Instagram-Login path. The repo itself is
the free, reliable host: release assets on a PUBLIC repository are
world-readable, 2 GB per file, no card. The Actions runner's built-in
GITHUB_TOKEN writes them (the workflow grants contents:write); the PC
worker simply skips hosting when that env is absent.

Assets are pruned 30 days after upload — Instagram fetches within
minutes of publish and TikTok's draft lives in their cloud, so nothing
still needs the file a month later. (An approval sitting undecided for
over a month loses cross-posting, not the YouTube publish.)
"""

import os

import requests

API = "https://api.github.com"
TAG = "media-cache"


def available():
    """Only the Actions runner has the built-in GITHUB_TOKEN + repo env;
    anywhere else this is a clean no-op."""
    return bool(os.environ.get("GITHUB_REPOSITORY")
                and os.environ.get("GITHUB_TOKEN"))


def _headers(tok, upload=False):
    h = {"Authorization": f"Bearer {tok}",
         "Accept": "application/vnd.github+json"}
    if upload:
        h["Content-Type"] = "application/octet-stream"
    return h


def _get_release(repo, tok):
    r = requests.get(f"{API}/repos/{repo}/releases/tags/{TAG}",
                     headers=_headers(tok), timeout=20)
    if r.ok:
        return r.json()
    r = requests.post(f"{API}/repos/{repo}/releases",
                      headers=_headers(tok),
                      json={"tag_name": TAG,
                            "name": "media cache (auto-pruned)",
                            "body": "Rendered Shorts staged for Instagram/"
                                    "TikTok cross-posting; auto-pruned "
                                    "after 30 days. Nothing else lives "
                                    "here."},
                      timeout=20)
    r.raise_for_status()
    return r.json()


def upload(path, name):
    """Upload one file under a date-stamped name (a re-render retry must
    not 422 on an existing asset name). Returns the public
    browser_download_url, or None on any failure — hosting is a
    convenience, never a gate on the YouTube upload."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not (repo and tok):
        return None
    try:
        import time as _t
        rel = _get_release(repo, tok)
        for a in rel.get("assets", []):
            # same base name from an earlier attempt of THIS render: drop
            # it, the new file supersedes it
            if a["name"].endswith(f"-{name}"):
                requests.delete(
                    f"{API}/repos/{repo}/releases/assets/{a['id']}",
                    headers=_headers(tok), timeout=20)
        r = requests.post(
            f"{API}/repos/{repo}/releases/{rel['id']}/assets"
            f"?name={_t.strftime('%Y%m%d')}-{name}",
            headers=_headers(tok, upload=True),
            data=open(path, "rb").read(), timeout=300)
        r.raise_for_status()
        return r.json()["browser_download_url"]
    except Exception as e:
        print(f"[ghassets] upload failed for {name}: {e}")
        return None


def prune(days=30):
    """Drop assets older than `days` — keeps the release from growing
    forever. Best-effort: a failed prune just means the next run tries
    again."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not (repo and tok):
        return
    try:
        import datetime
        rel = _get_release(repo, tok)
        cutoff = (datetime.datetime.utcnow()
                  - datetime.timedelta(days=days)).isoformat()
        for a in rel.get("assets", []):
            if (a.get("updated_at") or "") < cutoff:
                requests.delete(
                    f"{API}/repos/{repo}/releases/assets/{a['id']}",
                    headers=_headers(tok), timeout=20)
    except Exception as e:
        print(f"[ghassets] prune failed: {e}")
