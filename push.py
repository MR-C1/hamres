"""Web Push for the panel — the piece that lets the panel alert the owner
even when it is CLOSED, so it can fully stand in for Telegram.

Flow: the browser subscribes (service worker + VAPID public key) and POSTs
its subscription to /api/push/subscribe, which lands here in
state["push_subs"]. Whenever the brain files a notify_feed entry (comms.
_mirror_notify), it also calls push.send(), which delivers an encrypted
message to every subscribed browser through its push service (FCM, Mozilla,
Windows). A subscription that the service has forgotten (404/410) is pruned
so a stale phone never blocks the live ones.

Everything degrades gracefully: with no VAPID keys configured available()
is False and every call is a no-op — exactly like ghassets without a token.
"""

import json

import config


def available():
    """Web push is usable only when BOTH VAPID keys are configured (the
    private key signs, the public key is what the browser subscribed with)
    and pywebpush is importable."""
    if not (config.VAPID_PRIVATE_KEY and config.VAPID_PUBLIC_KEY):
        return False
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:
        return False


def public_key():
    """The applicationServerKey the panel's JS subscribes with."""
    return config.VAPID_PUBLIC_KEY


def _subs():
    import state
    return state.STATE.setdefault("push_subs", [])


def subscribe(sub):
    """Store a browser's PushSubscription (deduped by endpoint). Returns
    True when it was stored (or already present), False when the payload is
    not a usable subscription."""
    if not (isinstance(sub, dict) and sub.get("endpoint")):
        return False
    import state
    subs = _subs()
    ep = sub["endpoint"]
    subs[:] = [s for s in subs if s.get("endpoint") != ep]
    subs.append(sub)
    del subs[:-20]  # a handful of the owner's own devices, never a crowd
    state.save_soon()
    return True


def unsubscribe(endpoint):
    """Drop a subscription (browser revoked permission, or the push service
    told us it is gone)."""
    if not endpoint:
        return
    import state
    subs = _subs()
    before = len(subs)
    subs[:] = [s for s in subs if s.get("endpoint") != endpoint]
    if len(subs) != before:
        state.save_soon()


def send(title, body="", url=""):
    """Push one alert to every subscribed browser. Best-effort: a dead
    subscription is pruned, a transient error is swallowed, and the whole
    thing no-ops when web push is not configured. Never raises — a push
    failure must never break the notify path that triggered it."""
    if not available():
        return 0
    subs = list(_subs())
    if not subs:
        return 0
    from pywebpush import webpush, WebPushException
    payload = json.dumps({"title": str(title)[:80],
                          "body": str(body)[:200],
                          "url": url or f"{config.PANEL_URL.rstrip('/')}/panel"})
    claims = {"sub": config.VAPID_SUBJECT or config.PANEL_URL
              or "mailto:owner@localhost"}
    sent, dead = 0, []
    for s in subs:
        try:
            webpush(subscription_info=s, data=payload,
                    vapid_private_key=config.VAPID_PRIVATE_KEY,
                    vapid_claims=dict(claims), timeout=10)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):        # gone — the browser unsubscribed
                dead.append(s.get("endpoint"))
            # other codes (rate limit, transient) are left to retry next time
        except Exception:
            pass  # a malformed sub or a network blip must not raise
    for ep in dead:
        unsubscribe(ep)
    return sent
