"""One-time Instagram token extraction (run on the PC, python
extract_ig_token.py).

Prerequisites (about 15 minutes, all free):
  1. Convert the channel's Instagram account to Professional
     (Settings → Account type → Creator). Takes effect immediately.
  2. Create an app at developers.facebook.com → My Apps → Create App →
     type "Business". Add the product "Instagram Graph API" (the
     "Instagram API with Instagram Login" surface). No App Review is
     needed — we only ever post to YOUR OWN account.
  3. In the app: App settings → Basic → note the App ID and App secret
     (App settings → Basic). Scroll down and Add Platform → Web, with
     your site URL (anything valid, e.g. the channel's Instagram page).
  4. Generate a LONG-LIVED token (60 days):
     a. In the app dashboard → API setup with Instagram login → your
        app has an "Instagram User ID" shown there. Note it.
     b. Visit (browser):
        https://www.instagram.com/oauth/authorize
          ?client_id=APP_ID&redirect_uri=REDIRECT&scope=instagram_business_basic,instagram_business_content_publish&response_type=code
        (REDIRECT is any https URL you listed as a valid redirect in
        App settings → Basic → Instagram API settings — e.g.
        https://localhost/ works as a placeholder).
     c. The browser lands on a URL like REDIRECT/?code=SHORT_CODE...
        Copy the code value.
     d. Run: python extract_ig_token.py SHORT_CODE REDIRECT
     e. The script exchanges it (short-lived → long-lived) and prints
        the 60-day token + your user id, ready for Render env vars.

The brain refreshes the token daily from then on (ig.refresh_token), so
it never actually expires.
"""

import sys

import requests

# from step 2 — fill these in before running, or export them
APP_ID = ""            # App settings → Basic
APP_SECRET = ""        # App settings → Basic → show secret


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print("usage: python extract_ig_token.py <code> <redirect_uri>")
        return 1
    code, redirect = sys.argv[1], sys.argv[2]
    if not (APP_ID and APP_SECRET):
        print("Fill in APP_ID / APP_SECRET at the top of this file first.")
        return 1
    # short-lived token
    r = requests.post(
        "https://api.instagram.com/oauth/access_token",
        data={"client_id": APP_ID, "client_secret": APP_SECRET,
              "grant_type": "authorization_code",
              "redirect_uri": redirect, "code": code}, timeout=30).json()
    short = r.get("access_token")
    if not short:
        print("exchange failed:", r)
        return 1
    print("short-lived token OK — exchanging for long-lived...")
    # long-lived (60 days)
    r = requests.get(
        "https://graph.instagram.com/access_token",
        params={"grant_type": "ig_exchange_token",
                "client_secret": APP_SECRET,
                "access_token": short}, timeout=30).json()
    long_tok = r.get("access_token")
    if not long_tok:
        print("long-lived exchange failed:", r)
        return 1
    # who am I
    me = requests.get(
        "https://graph.instagram.com/v21.0/me",
        params={"fields": "user_id,username",
                "access_token": long_tok}, timeout=30).json()
    print()
    print("Add these two to Render env vars:")
    print(f"  IG_USER_ID = {me.get('user_id', '?')}")
    print(f"  IG_ACCESS_TOKEN = {long_tok}")
    print(f"  (account: @{me.get('username', '?')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
