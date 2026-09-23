"""Offline self-test for web push (push.py) — no network, no keys, no VAPID
needed. Fakes the `state` module and the `pywebpush` library so every branch
runs: availability gating, subscription store (dedupe, cap, bad payloads),
and send() fan-out with dead-subscription pruning.

    python selftest_push.py
"""
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILS = []


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)


def fake_state():
    st = types.ModuleType("state")
    st.STATE = {}
    st.saved = {"n": 0}
    st.save_soon = lambda: st.saved.__setitem__("n", st.saved["n"] + 1)
    sys.modules["state"] = st
    return st


def main():
    st = fake_state()
    import config
    import push

    # 1. availability gating — no keys => everything is a graceful no-op
    config.VAPID_PUBLIC_KEY = ""
    config.VAPID_PRIVATE_KEY = ""
    check("no keys -> unavailable", push.available() is False)
    check("no keys -> public_key empty", push.public_key() == "")
    check("send() no-ops when unavailable", push.send("hi") == 0)

    # 2. subscribe: stores, dedupes by endpoint, rejects junk, caps devices
    check("subscribe rejects a non-dict", push.subscribe("nope") is False)
    check("subscribe rejects a dict with no endpoint",
          push.subscribe({"keys": {}}) is False)
    check("a bad subscribe wrote nothing", st.STATE.get("push_subs") is None
          or st.STATE["push_subs"] == [])
    check("subscribe stores a real subscription",
          push.subscribe({"endpoint": "https://push/a", "keys": {"p": 1}})
          is True and len(st.STATE["push_subs"]) == 1)
    push.subscribe({"endpoint": "https://push/a", "keys": {"p": 2}})
    check("same endpoint replaces, never duplicates",
          len(st.STATE["push_subs"]) == 1
          and st.STATE["push_subs"][0]["keys"]["p"] == 2)
    for i in range(25):
        push.subscribe({"endpoint": f"https://push/{i}"})
    check("device list is capped at 20", len(st.STATE["push_subs"]) == 20)
    push.unsubscribe("https://push/24")
    check("unsubscribe drops exactly one",
          len(st.STATE["push_subs"]) == 19
          and all(s["endpoint"] != "https://push/24"
                  for s in st.STATE["push_subs"]))

    # 3. send() fan-out with keys set + a faked pywebpush. One subscription
    #    is "gone" (410) and must be pruned; a transient 500 is kept; the
    #    rest deliver.
    config.VAPID_PUBLIC_KEY = "pub"
    config.VAPID_PRIVATE_KEY = "priv"
    config.VAPID_SUBJECT = "mailto:x@y.z"
    st.STATE["push_subs"] = [
        {"endpoint": "https://push/live1"},
        {"endpoint": "https://push/gone"},
        {"endpoint": "https://push/flaky"},
        {"endpoint": "https://push/live2"},
    ]

    sent_to = []

    class WebPushException(Exception):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    class Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_webpush(subscription_info, data, vapid_private_key,
                     vapid_claims, timeout=None):
        ep = subscription_info["endpoint"]
        if ep.endswith("gone"):
            raise WebPushException("gone", response=Resp(410))
        if ep.endswith("flaky"):
            raise WebPushException("boom", response=Resp(500))
        sent_to.append(ep)
        return True

    fake_mod = types.ModuleType("pywebpush")
    fake_mod.webpush = fake_webpush
    fake_mod.WebPushException = WebPushException
    real = sys.modules.get("pywebpush")
    sys.modules["pywebpush"] = fake_mod
    try:
        n = push.send("Title line", "body text")
    finally:
        if real is None:
            sys.modules.pop("pywebpush", None)
        else:
            sys.modules["pywebpush"] = real

    check("available() true once keys are set", push.available() is True)
    check("send delivered to both live subs", n == 2
          and sorted(sent_to) == ["https://push/live1", "https://push/live2"])
    eps = [s["endpoint"] for s in st.STATE["push_subs"]]
    check("a 410 subscription was pruned", "https://push/gone" not in eps)
    check("a transient 500 subscription was KEPT", "https://push/flaky" in eps)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all web-push checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
