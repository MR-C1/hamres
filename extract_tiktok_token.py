"""One-time TikTok token extraction (run on the PC, python
extract_tiktok_token.py).

Prerequisites (about 10 minutes, all free):
  1. developers.tiktok.com → Manage apps → Connect an app (or create
     one). Add the product "Content Posting API".
  2. The app stays unaudited — that is fine. Unaudited apps can only
     post privately, and we deliberately use the INBOX (draft) flow,
     which needs no audit: the finished Short lands in your TikTok
     drafts and you tap publish.
  3. In the app settings: add Login kit scopes "user.info.basic" and
     "video.upload"; set the Redirect URI to something you control
     (e.g. https://localhost/ — TikTok requires an https URL, the page
     itself never loads).
  4. Note the Client key and Client secret.
  5. Visit (browser), replacing CLIENT_KEY and REDIRECT:
        https://www.tiktok.com/v2/auth/authorize/
          ?client_key=CLIENT_KEY&scope=user.info.basic,video.upload
          &response_type=code&redirect_uri=REDIRECT&state=x
     Log in as the CHANNEL's TikTok account and approve.
  6. The browser lands on REDIRECT?code=LONG_CODE&state=x — copy the
     code (it is long; take everything between code= and &state).
  7. Run: python extract_tiktok_token.py CODE REDIRECT
     The script prints the four values for Render env vars.

CRITICAL: the refresh token ROTATES every time the brain refreshes —
after the brain has run once, the printed values are dead. Never
re-run this unless the brain's state was wiped; check Telegram —
a "TikTok refresh failed" streak means the grant is gone and only
then re-extract and re-paste.
"""

import sys

import requests

# from step 4 — fill these in before running, or export them
CLIENT_KEY = ""
CLIENT_SECRET = ""


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print("usage: python extract_tiktok_token.py <code> <redirect_uri>")
        return 1
    code, redirect = sys.argv[1], sys.argv[2]
    if not (CLIENT_KEY and CLIENT_SECRET):
        print("Fill in CLIENT_KEY / CLIENT_SECRET at the top of this "
              "file first.")
        return 1
    r = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={"client_key": CLIENT_KEY, "client_secret": CLIENT_SECRET,
              "grant_type": "authorization_code",
              "redirect_uri": redirect, "code": code}, timeout=30).json()
    d = r.get("data") or {}
    if not d.get("access_token"):
        print("exchange failed:", r)
        return 1
    print()
    print("Add these four to Render env vars:")
    print(f"  TIKTOK_CLIENT_KEY   = {CLIENT_KEY}")
    print(f"  TIKTOK_CLIENT_SECRET = {CLIENT_SECRET}")
    print(f"  TIKTOK_ACCESS_TOKEN = {d['access_token']}")
    print(f"  TIKTOK_REFRESH_TOKEN = {d.get('refresh_token', '')}")
    print(f"  TIKTOK_OPEN_ID      = {d.get('open_id', '')}")
    print()
    print("Remember: never re-run this after the brain has refreshed "
          "even once — the refresh token rotates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
