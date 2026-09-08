"""Self-test the panel login surface end to end, offline.

Runs a real Flask test client against app.py with env vars set for each
scenario, and checks every wall: anonymous redirect, wrong password, both
valid logins, the signed cookie, a forged admin cookie, visitor blocked
from /api/action but served /api/state, and the auth-off fallback (panel
open, as before).

    python selftest_panel_login.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, got, want):
    ok = want(got) if callable(want) else got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}")
    if not ok:
        FAILS.append(label)


def fresh(admin_pw="", visitor_pw="", worker_secret="wsecret",
           session_secret=None):
    """Import a clean app under the given env, return a test client."""
    for k in ("PANEL_ADMIN_PASSWORD", "PANEL_VISITOR_PASSWORD",
              "PANEL_SESSION_SECRET", "WORKER_SECRET"):
        os.environ.pop(k, None)
    os.environ["WORKER_SECRET"] = worker_secret
    if admin_pw:
        os.environ["PANEL_ADMIN_PASSWORD"] = admin_pw
    if visitor_pw:
        os.environ["PANEL_VISITOR_PASSWORD"] = visitor_pw
    if session_secret:
        os.environ["PANEL_SESSION_SECRET"] = session_secret
    # app import caches config at module load; force a clean one
    for m in [m for m in list(sys.modules) if m in
              ("app", "config", "state", "comms", "brain", "yt", "jobs",
               "llm", "cloud")]:
        del sys.modules[m]
    import app as appmod
    appmod.state.STATE.clear()
    appmod.state.LOADED = True  # memory-only: no gist writes during tests
    appmod.state.default_state()
    return appmod.app.test_client()


def main():
    # ---- auth ON ----
    c = fresh("326745", "12345")

    r = c.get("/panel")
    check("anonymous /panel redirects to login",
          (r.status_code, r.headers.get("Location", "")),
          (302, "/panel/login"))

    r = c.get("/panel/login")
    check("login page serves", r.status_code, 200)
    check("login page carries no passwords", r.get_data(as_text=True),
          lambda t: "326745" not in t and "12345" not in t)

    r = c.post("/panel/login", data={"user": "admin", "password": "wrong"})
    check("wrong password stays on login page", r.status_code, 200)
    check("wrong password shows the error",
          "didn't work" in r.get_data(as_text=True), True)

    r = c.post("/panel/login", data={"user": "admin", "password": "326745"},
               follow_redirects=False)
    check("admin login redirects to /panel", r.status_code, 302)
    check("admin cookie is a signed role, not the password",
          r.headers.get("Set-Cookie", ""),
          lambda s: "326745" not in s and "admin" in s)

    r = c.get("/panel")
    t = r.get_data(as_text=True)
    check("admin sees the desk", r.status_code, 200)
    check("desk carries admin role stamp",
          'name="panel-role" content="admin"' in t, True)

    # visitor
    c2 = fresh("326745", "12345")
    r = c2.post("/panel/login", follow_redirects=False,
                data={"user": "visitor", "password": "12345"})
    check("visitor login redirects to /panel", r.status_code, 302)
    r = c2.get("/panel")
    check("visitor sees the desk (full view)",
          'name="panel-role" content="visitor"'
          in r.get_data(as_text=True), True)

    r = c2.get("/api/state")
    check("visitor may read /api/state", r.status_code, 200)

    r = c2.post("/api/action", json={"action": "pause"})
    check("visitor action refused (403)",
          (r.status_code, r.get_json().get("error", "")),
          (403, "read-only sign-in — ask the admin"))

    # admin can act
    r = c.post("/api/action", json={"action": "resume"})
    check("admin action accepted", r.status_code, 200)

    # forged cookie: flip the role word, keep/forge signature
    c3 = fresh("326745", "12345")
    forged = "admin:" + ("0" * 16)
    c3.set_cookie("panel_role", forged)
    r = c3.get("/panel")
    check("forged admin cookie is rejected",
          (r.status_code, r.headers.get("Location", "")),
          (302, "/panel/login"))
    r = c3.post("/api/action", json={"action": "pause"})
    check("forged cookie cannot act", r.status_code, 403)

    # logout
    r = c.get("/panel/logout", follow_redirects=False)
    check("logout clears the cookie",
          r.headers.get("Set-Cookie", ""),
          lambda s: "panel_role=" in s and ("Max-Age=0" in s or "max-age=0" in s))

    # ---- auth OFF (fallback: open panel as before) ----
    c4 = fresh()
    r = c4.get("/panel")
    check("auth off: panel open, no redirect",
          (r.status_code, 'name="panel-role" content="open"'
           in r.get_data(as_text=True)), (200, True))
    r = c4.post("/api/action", json={"action": "resume"})
    check("auth off: actions work as before", r.status_code, 200)

    # ---- one password only ----
    c5 = fresh(admin_pw="", visitor_pw="12345")
    r = c5.post("/panel/login", follow_redirects=False,
                data={"user": "admin", "password": "12345"})
    check("admin name with the visitor password refused",
          r.status_code, 200)  # back on the login page
    r = c5.post("/panel/login", follow_redirects=False,
                data={"user": "visitor", "password": "12345"})
    check("visitor-only setup still signs visitors in", r.status_code, 302)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all panel-login checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
