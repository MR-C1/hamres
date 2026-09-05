"""Channel Agent — a 24/7 YouTube growth manager (Render brain + home-PC
worker). Telegram is the console.

This is the composition root: Flask worker endpoints, Telegram loop,
scheduler, slash commands, and approval buttons. Intelligence lives in
brain.py; YouTube access in yt.py; jobs in jobs.py.
"""

import re
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request

import brain
import comms
import config
import jobs
import state
import yt

app = Flask(__name__)
STARTED_AT = time.time()
UNAUTHORIZED = "This agent answers its owner only."

TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

HELP_HTML = """🎬 <b>Channel Agent</b> — your 24/7 YouTube growth manager

<b>Commands</b>
/status — system health + job queue
/stats — live channel numbers
/next — queue a new video right now
/clear — empty the job queue (/clear all also drops pending approvals)
/publish — publish every pending rendered video
/queue — every job in the queue with status and age
/pending — videos awaiting your ✅/❌, with watch links
/log — the brain's recent activity
/retry — requeue the last failed job (/retry all for every)
/reset — reset the approval counter to 0/10, auto-approve off
/report — today's growth report
/idea &lt;topic&gt; — draft a script on a topic
/settings — show / change settings
/pause • /resume — stop or resume auto work
/diag — test the AI providers

<b>Or just talk to me</b> — "why are views down?", "what should the next
video be about?", "how close am I to monetization?" ("publish" alone also
publishes.)

I check comments every few hours, report stats each morning, learn which
topics grow the channel, and keep videos queued. A cloud worker renders
them, uploads them privately, and sends previews here — tap ✅ to make a
video public or ❌ to delete it, whenever you like (no time limit).
"""


# ---------------------------------------------------------------------------
# worker endpoints (home PC)
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return "ok"


@app.route("/debug")
def debug():
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    import os
    return jsonify({
        "pid": os.getpid(),
        "jobs_in_memory": len(state.STATE.get("jobs", [])),
        "pending": jobs.pending_count(),
        "worker_seen": state.STATE.get("worker", {}).get("last_seen", ""),
        "uptime_s": int(time.time() - STARTED_AT),
    })


@app.route("/next-job")
def next_job():
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()  # idempotent — state keys always exist
    state.STATE["worker"]["last_seen"] = datetime.utcnow().isoformat()
    if state.STATE["worker"].get("warned_offline"):
        state.STATE["worker"]["warned_offline"] = False
        comms.send("✅ Worker back in contact.")
    state.save_soon()
    # cloud workers pass ?max_cost_minutes=N so jobs too big for their
    # remaining budget stay queued for the unlimited PC worker
    try:
        max_cost = float(request.args.get("max_cost_minutes"))
    except (TypeError, ValueError):
        max_cost = None
    job = jobs.next_job(max_cost_minutes=max_cost)
    return jsonify(job or {})


@app.route("/clear-queue", methods=["POST"])
def clear_queue():
    """Wipe the job queue (fresh start). Requires the worker secret so
    only the owner's tooling can call it. Optionally ?jobs=failed to
    only clear failed jobs, or ?all=1 to also drop pending approvals."""
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()
    state.reload_jobs()
    before = len(state.STATE["jobs"])
    only_failed = request.args.get("jobs") == "failed"
    if only_failed:
        state.STATE["jobs"] = [j for j in state.STATE["jobs"]
                               if j.get("status") != "failed"]
    else:
        state.STATE["jobs"] = []
    if request.args.get("all") == "1":
        state.STATE["pending_videos"] = {}
    state.save_now()
    comms.send(f"🧹 Queue cleared ({before} → "
               f"{len(state.STATE['jobs'])} jobs"
               + (", pending approvals dropped too" if request.args.get("all") == "1" else "")
               + ").", html=True)
    return jsonify({"ok": True, "before": before,
                    "after": len(state.STATE["jobs"])})


@app.route("/video-admin", methods=["POST"])
def video_admin():
    """Owner-tooling endpoint: YouTube cleanup on already-uploaded videos
    ({"action": "delete"|"unschedule", "video_ids": [...]}) and removal
    of stale pending-approval entries ({"action": "forget",
    "approval_ids": [...]}). The worker secret doubles as the admin
    credential — it's the owner's own tooling calling this, and every
    action is reported in Telegram."""
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "")

    if action == "forget":
        ids = data.get("approval_ids", [])
        removed = [i for i in ids
                   if state.STATE["pending_videos"].pop(i, None) is not None]
        if removed:
            state.save_soon()
        comms.send(f"🛠 <b>Video admin</b> forget: {len(removed)} stale "
                   f"entries removed.", html=True)
        return jsonify({"ok": True, "removed": removed})

    ids = data.get("video_ids", [])
    if action not in ("delete", "unschedule") or not ids:
        return jsonify({"error": "action must be delete|unschedule|forget"}), 400
    done, failed = [], []
    for vid in ids:
        url = vid if vid.startswith("http") else f"https://youtu.be/{vid}"
        try:
            if action == "delete":
                yt.delete_video(url)
                done.append(vid)
            else:  # strip schedule, keep private until the owner's ✅
                yt.make_private(url)
                done.append(vid)
        except Exception as e:
            failed.append({"id": vid, "error": str(e)[:100]})
    comms.send(f"🛠 <b>Video admin</b> {action}: {len(done)} ok, "
               f"{len(failed)} failed.", html=True)
    return jsonify({"ok": not failed, "done": done, "failed": failed})


# ---------------------------------------------------------------------------
# WEB CONTROL PANEL (same free Render service — owner password = WORKER_SECRET)
# ---------------------------------------------------------------------------

import hashlib as _hashlib

def _panel_ok():
    """Auth: session cookie OR the raw secret as a header (the login
    screen sends the header first; the cookie serves later visits)."""
    key = request.cookies.get("panel_key", "") or         request.headers.get("X-Panel-Key", "")
    if not config.WORKER_SECRET:
        return False
    # the header carries the raw secret; the cookie carries its hash
    if key == config.WORKER_SECRET:
        return True
    return key == _hashlib.sha256(config.WORKER_SECRET.encode()).hexdigest()


PANEL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FOOTNOTE Control Panel</title>
<style>
:root{--ink:#14141e;--card:#1d1d29;--cream:#faf6ee;--red:#b21818;--dim:#8b8b9e;--ok:#3fbf6f;--warn:#e0a030}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ink);color:var(--cream);font:14px/1.5 system-ui,sans-serif;padding:20px;max-width:1100px;margin:0 auto}
h1{font-size:22px;letter-spacing:.5px}h1 .star{color:var(--red)}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}
.pill{background:var(--card);border-radius:20px;padding:6px 14px;font-size:13px}
.pill b{color:var(--red)}
.card{background:var(--card);border-radius:12px;padding:16px;margin:14px 0}
.card h2{font-size:15px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 8px;text-align:left;border-bottom:1px solid #2a2a3a}
th{color:var(--dim);font-weight:600}
.st-pending{color:var(--warn)}.st-claimed{color:#5fa8e8}.st-done{color:var(--ok)}.st-failed{color:var(--red)}
.btn{background:var(--red);color:var(--cream);border:0;border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;margin:2px}
.btn:hover{filter:brightness(1.2)}
.btn.ghost{background:#2a2a3a}
.videorow{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #2a2a3a}
.videorow a{color:var(--cream);text-decoration:none;font-weight:600}
.videorow a:hover{color:var(--red)}
.grow{flex:1}
.log{font-family:ui-monospace,monospace;font-size:12px;color:var(--dim);max-height:260px;overflow-y:auto}
.log div{padding:2px 0;border-bottom:1px solid #232330}
.actions{display:flex;flex-wrap:wrap;gap:4px}
input{background:#2a2a3a;border:1px solid #3a3a4a;color:var(--cream);border-radius:8px;padding:10px;font-size:14px;width:100%}
.login{max-width:360px;margin:80px auto;text-align:center}
#refresh{color:var(--dim);font-size:12px;text-align:right}
</style></head><body>
<div id="app" class="login">
  <h1>FOOTNOTE<span class="star">*</span></h1>
  <p style="color:var(--dim);margin:8px 0 20px">Control Panel — the part they skipped</p>
  <input type="password" id="key" placeholder="Panel password (WORKER_SECRET)" onkeydown="if(event.key==='Enter')login()">
  <button class="btn" style="margin-top:10px;width:100%" onclick="login()">Enter</button>
  <p id="err" style="color:var(--red);margin-top:8px;font-size:12px"></p>
</div>
<div id="panel" style="display:none">
  <h1>FOOTNOTE<span class="star">*</span> <span style="font-size:13px;color:var(--dim)">Control Panel</span></h1>
  <div class="pills" id="pills"></div>
  <div class="card"><h2>Actions</h2><div class="actions">
    <button class="btn" onclick="act('next')">🎬 New video</button>
    <button class="btn" onclick="act('publish')">🚀 Publish all</button>
    <button class="btn ghost" onclick="act('retry')">🔁 Retry failed</button>
    <button class="btn ghost" onclick="act('pause')">⏸ Pause</button>
    <button class="btn ghost" onclick="act('resume')">▶️ Resume</button>
    <button class="btn ghost" onclick="act('clear_failed')">🧹 Clear failed</button>
    <button class="btn ghost" onclick="act('reset')">🔄 Reset approvals</button>
    <button class="btn ghost" style="background:#4a1a1a" onclick="if(confirm('Wipe the whole queue? Pending approvals are dropped (videos stay private).'))act('clear_all')">⛔ Clear everything</button>
  </div></div>
  <div class="card"><h2>Awaiting your decision</h2><div id="pending"></div></div>
  <div class="card"><h2>Job queue</h2><div id="queue"></div></div>
  <div class="card"><h2>Recent activity</h2><div class="log" id="log"></div></div>
  <div id="refresh"></div>
</div>
<script>
async function login(){
  const r=await fetch('/api/state',{headers:{'X-Panel-Key':document.getElementById('key').value}});
  if(!r.ok){document.getElementById('err').textContent='Wrong password';return}
  document.cookie='panel_key='+await sha(document.getElementById('key').value)+';path=/;max-age=2592000;secure';
  document.getElementById('app').style.display='none';
  document.getElementById('panel').style.display='block';
  load();
}
async function sha(t){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(t));
  return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('')}
let KEY=null;
async function load(){
  const r=await fetch('/api/state');
  if(r.status===403){location.reload();return}
  const d=await r.json();KEY=d._key;
  const P=(t,v)=>'<div class="pill">'+t+' <b>'+v+'</b></div>';
  document.getElementById('pills').innerHTML=
    P('📺',d.channel.title+' — '+d.channel.subs+' subs')+P('👁',d.channel.views+' views')+
    P('🎬',d.channel.videos+' videos')+P('⏳',d.queue.pending+' pending jobs')+
    P('✅',d.queue.awaiting+' awaiting decision')+
    P('🤖','auto-approve '+(d.settings.auto_approve?'ON':'off')+' ('+d.settings.approved+'/10)')+
    P('⚙️','auto work '+(d.settings.paused?'PAUSED':'running'))+
    P('🖥','worker seen '+d.worker.mins_ago+'m ago')+P('⏱','brain up '+d.uptime);
  let ph='';
  if(!d.pending.length)ph='<p style="color:var(--dim)">Nothing awaiting decision.</p>';
  for(const v of d.pending){
    ph+='<div class="videorow"><a href="'+v.url+'" target="_blank">'+esc(v.title)+'</a>'+
      '<span style="color:var(--dim);font-size:12px">'+v.alts+' alt titles</span>'+
      '<span class="grow"></span>'+
      '<button class="btn" onclick="act(\'publish:'+v.id+'\')">✅ Publish</button>'+
      '<button class="btn ghost" style="background:#4a1a1a" onclick="if(confirm(\'Delete this video from YouTube permanently?\'))act(\'reject:'+v.id+'\')">❌ Delete</button></div>';
  }
  document.getElementById('pending').innerHTML=ph;
  let qh='<table><tr><th></th><th>id</th><th>type</th><th>status</th><th>age</th><th>title</th></tr>';
  if(!d.jobs.length)qh+='<tr><td colspan="6" style="color:var(--dim)">Queue empty</td></tr>';
  for(const j of d.jobs.slice(-15).reverse()){
    qh+='<tr><td class="st-'+j.status+'">'+({pending:'⏳',claimed:'🔧',done:'✅',failed:'❌'}[j.status]||'❓')+'</td>'+
    '<td><code>'+j.id.slice(0,8)+'</code></td><td>'+j.type+'</td><td class="st-'+j.status+'">'+j.status+'</td><td>'+j.age+'m</td><td>'+esc(j.title)+'</td></tr>';
  }
  document.getElementById('queue').innerHTML=qh+'</table>';
  document.getElementById('log').innerHTML=d.log.map(l=>'<div>'+esc(l)+'</div>').join('');
  document.getElementById('refresh').textContent='auto-refresh 15s · '+new Date().toLocaleTimeString();
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function act(a){
  const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});
  const d=await r.json();
  if(d.ok!==false)load();else alert(d.error||'failed');
}
load();setInterval(load,15000);
</script></body></html>"""


@app.route("/panel")
def panel():
    if _panel_ok():
        return PANEL_HTML
    return PANEL_HTML  # login screen; JS gates the data behind auth


@app.route("/api/state")
def api_state():
    if not _panel_ok():
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()
    import time as _t
    w = state.STATE.get("worker", {})
    mins = ""
    if w.get("last_seen"):
        try:
            from datetime import datetime as _dt
            mins = int((_dt.utcnow() - _dt.fromisoformat(w["last_seen"]))
                       .total_seconds() // 60)
        except Exception:
            pass
    try:
        ch = yt.channel_stats() or {"title": "?", "subs": 0, "views": 0,
                                    "videos": 0}
    except Exception:
        ch = {"title": "unavailable", "subs": 0, "views": 0, "videos": 0}
    jobs = state.STATE.get("jobs", [])
    now = _t.time()
    s = state.STATE["settings"]
    return jsonify({
        "channel": ch,
        "queue": {"pending": sum(1 for j in jobs if j["status"] == "pending")},
        "awaiting": len(state.STATE["pending_videos"]),
        "settings": {"auto_approve": s.get("auto_approve"),
                     "approved": s.get("approved_count", 0),
                     "paused": s.get("paused")},
        "worker": {"mins_ago": mins},
        "uptime": f"{int(_t.time() - STARTED_AT) // 3600}h "
                  f"{(int(_t.time() - STARTED_AT) % 3600) // 60}m",
        "pending": [{"id": uid, "title": p.get("title", "?"),
                     "url": p.get("video_url", ""),
                     "alts": len(p.get("title_alternatives") or [])}
                    for uid, p in state.STATE["pending_videos"].items()],
        "jobs": [{"id": j["id"], "type": j["type"], "status": j["status"],
                  "age": int((now - j.get("created", now)) / 60),
                  "title": (j.get("script", {}).get("title") or
                            j.get("result", {}).get("msg", ""))[:60]}
                 for j in jobs],
        "log": comms.LOG[-20:],
    })


@app.route("/api/action", methods=["POST"])
def api_action():
    if not _panel_ok():
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()
    data = request.get_json(force=True, silent=True) or {}
    a = data.get("action", "")
    import cloud
    if a == "next":
        comms.send("Writing a script… (from panel)")
        made = brain.queue_next_video(1)
        return jsonify({"ok": bool(made)})
    if a == "publish":
        _publish_pending()
        return jsonify({"ok": True})
    if a.startswith("publish:"):
        uid = a.split(":", 1)[1]
        p = state.STATE["pending_videos"].get(uid)
        if p:
            s_ = state.STATE["settings"]
            s_["approved_count"] = s_.get("approved_count", 0) + 1
            _publish_now(uid)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "not pending"})
    if a.startswith("reject:"):
        _delete_pending(a.split(":", 1)[1])
        return jsonify({"ok": True})
    if a == "retry":
        state.reload_jobs()
        n = 0
        for j in state.STATE["jobs"]:
            if j.get("status") == "failed":
                j["status"] = "pending"
                j["updated"] = time.time()
                n += 1
        state.save_now()
        if n:
            cloud.wake_soon("render")
        return jsonify({"ok": True, "requeued": n})
    if a == "pause":
        state.STATE["settings"]["paused"] = True
        state.save_now()
        return jsonify({"ok": True})
    if a == "resume":
        state.STATE["settings"]["paused"] = False
        state.save_now()
        return jsonify({"ok": True})
    if a == "clear_failed":
        state.reload_jobs()
        state.STATE["jobs"] = [j for j in state.STATE["jobs"]
                               if j.get("status") != "failed"]
        state.save_now()
        return jsonify({"ok": True})
    if a == "clear_all":
        state.reload_jobs()
        state.STATE["jobs"] = []
        state.STATE["pending_videos"] = {}
        state.save_now()
        return jsonify({"ok": True})
    if a == "reset":
        s_ = state.STATE["settings"]
        s_["approved_count"] = 0
        s_["auto_approve"] = False
        state.save_now()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": f"unknown action {a!r}"}), 400


@app.route("/queue-render", methods=["POST"])
def queue_render():
    """Queue a hand-authored script for cloud rendering (owner tooling:
    trailers, custom topics). Worker-secret guarded."""
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()
    data = request.get_json(force=True, silent=True) or {}
    script = data.get("script") or {}
    if not all(script.get(k) for k in ("id", "title", "hook", "scenes")):
        return jsonify({"error": "script needs id/title/hook/scenes"}), 400
    import cloud
    payload = {"script": script}
    if data.get("approval_id"):
        payload["approval_id"] = data["approval_id"]
    job = jobs.add_job("render", payload)
    cloud.wake_soon("render")
    comms.send(f"🎬 <b>Custom render queued</b> — "
               f"{comms.esc(script['title'][:50])}", html=True)
    return jsonify({"ok": True, "job_id": job["id"]})


@app.route("/report", methods=["POST"])
def report():
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()  # idempotent — state keys always exist
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id", "")
    ok = bool(data.get("ok"))
    job = jobs.complete_job(job_id, data)

    is_render = (job and job["type"] == "render") or (
        job is None and data.get("video_url"))
    # job is None = the job was lost to a restart/race. A render report
    # with a video_url still matters (a real video sits on YouTube) —
    # register it for the owner's decision instead of dropping it.
    if is_render and ok:
        approval_id = (job or {}).get("approval_id") or job_id
        if data.get("video_url"):
            # every uploaded format (short + long) so ✅/❌ act on all
            urls = data.get("video_urls") or [data.get("video_url")]
            state.STATE["pending_videos"][approval_id] = {
                "title": data.get("title", ""),
                "title_alternatives": data.get("title_alternatives", []),
                "video_url": data.get("video_url", ""),
                "video_urls": urls,
                "job_id": job_id,
            }
            state.save_soon()
            if state.STATE["settings"].get("auto_approve"):
                _publish_now(approval_id, note="auto-approved")
        elif job is None:
            comms.send(f"⚠️ <b>Render reported for an unknown job</b> "
                       f"(<code>{comms.esc(job_id)}</code>) — state may have "
                       f"been lost to a restart.", html=True)
    elif job and job["type"] == "upload" and ok:
        comms.send(f"📤 <b>Uploaded</b> — {comms.esc(data.get('title', 'video'))}\n"
                   f"{comms.esc(data.get('video_url', ''))}", html=True)
    elif not ok and job is None:
        # job aged out of the bounded list — nothing to update, but the
        # failure is still worth surfacing
        comms.send(f"⚠️ <b>Job failed</b> (unknown/old job "
                   f"<code>{comms.esc(job_id)}</code>)\n"
                   f"{comms.esc(data.get('msg', ''))[:400]}", html=True)
    elif not ok:
        comms.send(f"⚠️ <b>Job failed</b> (<code>{comms.esc(job['type'])}</code>)\n"
                   f"{comms.esc(data.get('msg', ''))[:400]}", html=True)
    return jsonify({"ok": True})


def _publish_now(approval_id, note=""):
    """Flip a pending video's YouTube privacy to public — every format
    (short + long) uploaded for it."""
    p = state.STATE["pending_videos"].get(approval_id)
    if not p or not p.get("video_url"):
        return False
    urls = p.get("video_urls") or [p["video_url"]]
    try:
        for url in urls:
            yt.make_public(url)
    except Exception as e:
        comms.send(f"⚠️ Couldn't make it public — {comms.esc(str(e)[:150])}. "
                   f"Try again or flip it in YouTube Studio.", html=True)
        return False
    state.STATE["pending_videos"].pop(approval_id, None)
    state.save_soon()
    comms.send(f"🚀 <b>Published</b> — {comms.esc(p['title'][:60])}\n"
               f"{comms.esc(urls[0])}", html=True)
    return True


def _delete_pending(approval_id):
    """Delete a pending video from YouTube (owner's ❌) — every format."""
    p = state.STATE["pending_videos"].pop(approval_id, None)
    if not p or not p.get("video_url"):
        comms.send("🤷 That video isn't pending anymore.")
        return False
    state.save_soon()
    urls = p.get("video_urls") or [p["video_url"]]
    try:
        for url in urls:
            yt.delete_video(url)
        comms.send(f"🗑 Deleted from YouTube — {comms.esc(p['title'][:60])}.",
                   html=True)
    except Exception as e:
        # restore so the owner can retry
        state.STATE["pending_videos"][approval_id] = p
        state.save_soon()
        comms.send(f"⚠️ Delete failed — {comms.esc(str(e)[:150])}. You can "
                   f"delete it in YouTube Studio.", html=True)
        return False
    return True


def _record_decision(approval_id, decision):
    """Owner's ✅ = flip the already-uploaded video public; ❌ = delete
    it from YouTube. No time window — the video is safely private on
    YouTube until decided, so the decision can come hours or days later."""
    p = state.STATE["pending_videos"].get(approval_id)
    if not p:
        # entry is gone: either already decided (popped on publish) or
        # the state was lost. Honest reply either way.
        comms.send("🤷 That video isn't pending anymore — it was already "
                   "published or deleted.", html=True)
        return
    if decision == "approved":
        # trust counter: one increment per published video, idempotent
        # because the entry is popped on success
        s = state.STATE["settings"]
        s["approved_count"] = s.get("approved_count", 0) + 1
        count = s["approved_count"]
        after = s.get("auto_approve_after", 10)
        if not s.get("auto_approve") and count >= after:
            s["auto_approve"] = True
            comms.send(f"🤖 <b>Auto-approve enabled</b> — {count} approvals "
                       f"earned my trust. New videos will publish themselves; "
                       f"comment replies still need your ✅.", html=True)
        _publish_now(approval_id)
    elif decision == "rejected":
        _delete_pending(approval_id)
        return
    else:
        comms.send("✅ Already decided — one decision per video.")


# ---------------------------------------------------------------------------
# approval buttons (callback queries)
# ---------------------------------------------------------------------------

def handle_callback(cb):
    state.default_state()
    data = cb.get("data", "")
    cid = cb.get("id")
    comms.answer_callback(cid)
    action, _, uid = data.partition(":")

    if action == "v":      # approve video → record decision
        _record_decision(uid, "approved")
    elif action == "vx":   # reject video → record decision
        _record_decision(uid, "rejected")
    elif action == "r":
        comms.send(brain.post_reply(uid))
    elif action == "rx":
        p = state.STATE["pending_replies"].pop(uid, None)
        if p:
            state.STATE.setdefault("replied_comments", []).append(
                p["comment_id"])
            state.save_soon()
        comms.send("Skipped.")
    elif action == "t":
        comms.send(brain.apply_title(uid))
    elif action == "tx":
        state.STATE["pending_titles"].pop(uid, None)
        state.save_soon()
        comms.send("Keeping current title.")


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------

def cmd_help(_):
    comms.send(HELP_HTML, html=True)


def cmd_status(_):
    w = state.STATE.get("worker", {})
    last = w.get("last_seen", "")
    mins = ""
    if last:
        try:
            seen = datetime.fromisoformat(last)
            mins = f"{int((datetime.utcnow() - seen).total_seconds() // 60)} min ago"
        except Exception:
            pass
    s = state.STATE["settings"]
    up = int(time.time() - STARTED_AT)
    comms.send(
        f"🩺 <b>Status</b>\n"
        f"• Queue: {jobs.pending_count()} pending jobs\n"
        f"• Awaiting your ✅: {len(state.STATE['pending_videos'])} videos, "
        f"{len(state.STATE['pending_replies'])} replies\n"
        f"• Auto-approve: {'ON' if s.get('auto_approve') else 'off'} "
        f"({s.get('approved_count', 0)}/{s.get('auto_approve_after', 10)})\n"
        f"• Auto work: {'PAUSED' if s.get('paused') else 'running'}\n"
        f"• PC worker: {'online ' + mins if mins else 'never seen'}\n"
        f"• Brain uptime: {up // 3600}h {(up % 3600) // 60}m\n"
        + "\n".join(comms.LOG[-6:]), html=True)


def cmd_stats(_):
    try:
        ch = yt.channel_stats()
        comms.send(f"📺 <b>{comms.esc(ch['title'])}</b>\n"
                   f"Subs: <b>{ch['subs']:,}</b> • Views: <b>{ch['views']:,}</b> "
                   f"• Videos: {ch['videos']}\n"
                   f"Monetization progress: {ch['subs']:,}/1,000 subs",
                   html=True)
    except Exception as e:
        comms.send(f"⚠️ {comms.esc(e)}", html=True)


def cmd_next(_):
    comms.send("Writing a script…")
    made = brain.queue_next_video(1)
    if not made:
        comms.send("Script generation failed — Gemini may be busy. Try again.")


def cmd_report(_):
    brain.daily_report()


def cmd_idea(args):
    if not args:
        comms.send("Usage: /idea why cats purr")
        return
    comms.typing()
    script = brain.generate_script(direction=f"Topic requested by the "
                                             f"channel owner: {args}")
    if not script:
        comms.send("Couldn't draft that — try again.")
        return
    job = jobs.add_job("render", {"script": script,
                                  "approval_id": job_approval_id()})
    import cloud
    cloud.wake_soon("render")
    state.STATE.setdefault("used_topics", []).append(script.get("id", "?"))
    state.save_soon()
    comms.send(f"🎬 Queued: <b>{comms.esc(script['title'])}</b>\n"
               f"Hook: {comms.esc(script['hook'])}", html=True)


def job_approval_id():
    import uuid
    return uuid.uuid4().hex[:10]


def cmd_pause(_):
    state.STATE["settings"]["paused"] = True
    state.save_soon()
    comms.send("⏸ Paused — no new auto work. Queued jobs still finish. "
               "Use /resume to restart.")


def cmd_resume(_):
    state.STATE["settings"]["paused"] = False
    state.save_soon()
    comms.send("▶️ Resumed.")


def cmd_settings(_):
    s = state.STATE["settings"]
    comms.send(
        f"⚙️ <b>Settings</b>\n"
        f"• auto_approve: <code>{s.get('auto_approve')}</code> "
        f"(earned {s.get('approved_count', 0)}/"
        f"{s.get('auto_approve_after', 10)} approvals)\n"
        f"• publish_hour: <code>{s.get('publish_hour', 17)}:00</code>\n"
        f"• paused: <code>{s.get('paused')}</code>\n\n"
        f"Change by chatting: \"auto approve on\", \"publish at 7pm\"",
        html=True)


def cmd_diag(_):
    comms.typing()
    import llm
    comms.send(f"🔍 <b>LLM chain health</b>\n\n{comms.esc(llm.diagnose())}",
               html=True)


def cmd_queue(_):
    """Detailed queue view: every job with status, age, and type."""
    state.default_state()
    state.reload_jobs()
    jobs = state.STATE["jobs"]
    if not jobs:
        comms.send("📭 Queue is empty. /next queues a fresh video.", html=True)
        return
    now = time.time()
    lines = [f"📋 <b>Queue — {len(jobs)} jobs</b>"]
    for j in jobs[-15:]:
        age = int((now - j.get("created", now)) / 60)
        title = (j.get("script", {}).get("title")
                 or j.get("result", {}).get("msg", "") or j["type"])[:45]
        icon = {"pending": "⏳", "claimed": "🔧", "done": "✅",
                "failed": "❌"}.get(j["status"], "❓")
        lines.append(f"{icon} <code>{j['id'][:8]}</code> {j['type']:7} "
                     f"{j['status']:7} {age}m — {comms.esc(title)}")
    comms.send("\n".join(lines), html=True)


def cmd_pending(_):
    """Everything awaiting your decision, with watch links."""
    state.default_state()
    pending = state.STATE["pending_videos"]
    if not pending:
        comms.send("✅ Nothing awaiting your decision. New previews "
                   "arrive here automatically after each render.",
                   html=True)
        return
    lines = [f"🎬 <b>Awaiting your decision — {len(pending)} video(s)</b>",
             "Tap ✅/❌ on the preview message, or say 'publish' for all:"]
    for uid, p in pending.items():
        alts = p.get("title_alternatives") or []
        alt_note = f" (+{len(alts)} alt titles)" if alts else ""
        url = comms.esc(p.get("video_url", ""))
        lines.append(f"• <a href=\"{url}\">"
                     f"{comms.esc(p.get('title', '?')[:50])}</a>{alt_note}")
    comms.send("\n".join(lines), html=True)


def cmd_log(_):
    """The brain's recent activity log (last 15 events)."""
    state.default_state()
    entries = comms.LOG[-15:]
    body = ("\n".join(comms.esc(e) for e in entries)
            if entries else "(quiet — nothing logged yet)")
    comms.send("📜 <b>Recent activity</b>\n" + body, html=True)


def cmd_retry(args):
    """Requeue the last failed job: /retry (latest) or /retry all."""
    state.default_state()
    state.reload_jobs()
    failed = [j for j in state.STATE["jobs"] if j.get("status") == "failed"]
    if not failed:
        comms.send("No failed jobs to retry.", html=True)
        return
    targets = failed if args.strip().lower() == "all" else failed[-1:]
    import cloud
    for j in targets:
        j["status"] = "pending"
        j["updated"] = time.time()
    state.save_now()
    cloud.wake_soon("render")
    comms.send(f"🔁 Requeued {len(targets)} failed job(s) — cloud worker "
               f"waking.", html=True)


def cmd_publish(_):
    """Publish rendered-but-unapproved videos (same as saying 'publish')."""
    _publish_pending()


def cmd_clear(args):
    """Clear the job queue: /clear (jobs), /clear failed (keep the rest),
    /clear all (also drop pending approvals — videos stay private on
    YouTube, untouched)."""
    state.default_state()
    state.reload_jobs()
    arg = args.strip().lower()
    before = len(state.STATE["jobs"])
    if arg == "failed":
        state.STATE["jobs"] = [j for j in state.STATE["jobs"]
                               if j.get("status") != "failed"]
        what = "failed jobs"
    else:
        state.STATE["jobs"] = []
        what = "entire queue"
    note = ""
    if arg == "all":
        n = len(state.STATE["pending_videos"])
        state.STATE["pending_videos"] = {}
        note = (f" Dropped {n} pending approval{'s' if n != 1 else ''} "
                f"(videos stay private on YouTube — publish or delete "
                f"them in YouTube Studio).")
    state.save_now()
    comms.send(f"🧹 Cleared the {what} ({before} → "
               f"{len(state.STATE['jobs'])} jobs remain).{note} /next "
               f"queues a fresh video.", html=True)


def cmd_reset_approvals(_):
    """Reset the approval trust counter to 0 and turn auto-approve off."""
    state.default_state()
    s = state.STATE["settings"]
    was = s.get("approved_count", 0)
    s["approved_count"] = 0
    s["auto_approve"] = False
    state.save_now()
    comms.send(f"🔄 Approval counter reset ({was} → 0/10). Auto-approve is "
               f"OFF — every video needs your ✅ until 10 real approvals "
               f"accumulate.", html=True)


COMMANDS = {
    "help": cmd_help, "start": cmd_help,
    "status": cmd_status,
    "stats": cmd_stats,
    "next": cmd_next,
    "report": cmd_report,
    "idea": cmd_idea,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "settings": cmd_settings,
    "diag": cmd_diag,
    "publish": cmd_publish,
    "queue": cmd_queue,
    "pending": cmd_pending,
    "log": cmd_log,
    "retry": cmd_retry,
    "clear": cmd_clear,
    "reset": cmd_reset_approvals,
    "reset-approvals": cmd_reset_approvals,
}


PUBLISH_WORDS = {"publish", "upload", "release"}


def _is_publish_intent(text):
    """True for 'publish', 'ok publish now', 'please upload' etc. — any
    short chat message whose words are essentially a publish command.
    Longer sentences with real content still go to Gemini."""
    words = re.findall(r"[a-z]+", text.lower())
    return (bool(words) and len(words) <= 6
            and any(w in PUBLISH_WORDS for w in words)
            and all(w in PUBLISH_WORDS or w in {"ok", "now", "please",
                                                "the", "video", "it", "go",
                                                "yes", "do", "just"}
                    for w in words))


def _publish_pending():
    """Flip every pending video to public (owner said 'publish'). The
    videos are already private on YouTube — this just makes them live."""
    pending = state.STATE["pending_videos"]
    if not pending:
        comms.send("Nothing to publish — no uploaded videos awaiting "
                   "approval. Render one with /next, then tap ✅ on the "
                   "preview, or say 'publish' once it's rendered.")
        return
    for uid in list(pending):
        # trust counter per video, like the button path
        s = state.STATE["settings"]
        s["approved_count"] = s.get("approved_count", 0) + 1
        count = s["approved_count"]
        after = s.get("auto_approve_after", 10)
        if not s.get("auto_approve") and count >= after:
            s["auto_approve"] = True
            comms.send(f"🤖 <b>Auto-approve enabled</b> — {count} approvals "
                       f"earned my trust. New videos will publish themselves; "
                       f"comment replies still need your ✅.", html=True)
        _publish_now(uid, note="published via chat")


def chat_reply(text):
    """Free text → Gemini with channel context. Publish-intent phrases
    are handled as real commands instead (the LLM only talks, it can't
    actually publish anything)."""
    if _is_publish_intent(text):
        state.default_state()
        _publish_pending()
        return
    comms.typing()
    ctx = ""
    try:
        ch = yt.channel_stats()
        ctx = (f"Channel: {ch['title']}, {ch['subs']} subs, "
               f"{ch['views']} views, {ch['videos']} videos. ")
    except Exception:
        pass
    reply = ""
    try:
        reply = brain.gemini(
            f"{ctx}The owner asks: \"{text}\"\n"
            f"Answer as their channel growth manager. Be concise and "
            f"concrete (under 150 words).")
    except Exception as e:
        reply = f"⚠️ All AI providers failed: {comms.esc(str(e)[:150])}"
    comms.send_md(reply)


def handle_message(msg):
    cid = str(msg.get("chat", {}).get("id", ""))
    if cid != str(config.OWNER_CHAT_ID):
        if msg.get("text"):
            comms.send(UNAUTHORIZED, cid)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    comms.log(f"command: {text[:60]}")
    try:
        if text.startswith("/"):
            parts = text[1:].split(" ", 1)
            cmd = parts[0].split("@")[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""
            COMMANDS.get(cmd, cmd_help)(args)
        else:
            chat_reply(text)
    except Exception as e:
        import traceback
        traceback.print_exc()
        comms.send(f"⚠️ Command failed: {comms.esc(type(e).__name__)}: "
                   f"{comms.esc(e)}", html=True)


# ---------------------------------------------------------------------------
# background loops
# ---------------------------------------------------------------------------

def telegram_loop():
    if not config.TELEGRAM_BOT_TOKEN:
        print("[agent] TELEGRAM_BOT_TOKEN not set — console disabled")
        return
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{TG_API}/getUpdates", params=params, timeout=30)
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                if update.get("callback_query"):
                    handle_callback(update["callback_query"])
                elif update.get("message"):
                    handle_message(update["message"])
        except Exception as e:
            print("[telegram] loop error:", e)
            time.sleep(5)


def run_safely(name, fn):
    try:
        fn()
    except Exception as e:
        import traceback
        traceback.print_exc()
        comms.send(f"⚠️ <b>{comms.esc(name)}</b> failed: {comms.esc(e)[:200]}",
                   html=True)


def scheduler_loop():
    """Every minute, run whatever is due (Dhaka time). Daily jobs catch up
    if their slot was missed while the dyno was restarting — and the
    ran-today ledger lives in the persistent STATE, so a restart doesn't
    refire jobs that already ran (that bug queued a duplicate video on
    every single reboot)."""
    if not (config.TELEGRAM_BOT_TOKEN and config.OWNER_CHAT_ID):
        return

    def _ran(key):
        return key in state.STATE.setdefault("scheduler_ran", {})

    def _mark_ran(key):
        today = f"{now:%Y-%m-%d}"
        ran = state.STATE["scheduler_ran"]
        ran[key] = True
        # keep only today's keys — the ledger stays tiny
        state.STATE["scheduler_ran"] = {
            k: v for k, v in ran.items() if today in k}
        state.save_soon()

    def once_per_day(name, fn, hour, minute):
        key = f"{name}:{now:%Y-%m-%d}"
        if now >= now.replace(hour=hour, minute=minute) and not _ran(key):
            _mark_ran(key)
            comms.log(f"scheduled: {name}")
            threading.Thread(target=run_safely, args=(name, fn),
                             daemon=True).start()

    def every_hours(name, fn, hours):
        key = f"{name}:{now:%Y-%m-%d-%H}"
        if now.hour % hours == 0 and now.minute < 2 and not _ran(key):
            _mark_ran(key)
            comms.log(f"scheduled: {name}")
            threading.Thread(target=run_safely, args=(name, fn),
                             daemon=True).start()

    while True:
        now = datetime.now() + config.BD_OFFSET
        state.default_state()  # self-heal if a restart lost keys
        if not state.STATE["settings"].get("paused"):
            once_per_day("daily report", brain.daily_report, 8, 0)
            once_per_day("planning", brain.analyze_and_plan, 8, 30)
            once_per_day("title check", brain.title_check, 12, 0)
            if now.weekday() == 6:
                once_per_day("weekly summary", brain.weekly_summary, 9, 0)
            every_hours("comment sweep", brain.comment_sweep, 4)

            # keep the render queue fed: if nothing pending, plan one video
            if (now.hour == 9 and now.minute == 0
                    and jobs.pending_count() == 0):
                threading.Thread(target=run_safely,
                                 args=("queue top-up",
                                       lambda: brain.queue_next_video(1)),
                                 daemon=True).start()

        # worker offline watchdog — cloud-only mode: runners poll only
        # when jobs exist (wake-on-queue + 3 crons), so quiet gaps are
        # NORMAL. A real problem is no contact for over a day.
        w = state.STATE.get("worker", {})
        if w.get("last_seen") and not w.get("warned_offline"):
            try:
                seen = datetime.fromisoformat(w["last_seen"])
                if (datetime.utcnow() - seen).total_seconds() > 26 * 3600:
                    w["warned_offline"] = True
                    state.save_soon()
                    comms.send("⚠️ <b>No worker contact in 26h+</b> — check "
                               "the GitHub Actions workflow and repo "
                               "visibility.", html=True)
            except Exception:
                pass
        time.sleep(60)


# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------

def boot():
    # SYNCHRONOUS load: must complete before gunicorn serves requests,
    # otherwise a worker poll saves empty state over the real gist.
    state.default_state()
    state.load()          # sets LOADED — saves stay blocked until done
    state.default_state()  # fill anything the gist lacked
    comms.register_menu()
    threading.Thread(target=state.saver_loop, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    if config.TELEGRAM_BOT_TOKEN and config.OWNER_CHAT_ID:
        comms.send(f"🎬 <b>Channel Agent online</b> — brain rebooted. "
                   f"Use /status for a health check.", html=True)


boot()  # runs at import, before the first request can arrive


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", 5000)))
