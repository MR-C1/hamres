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
    """Open panel (owner choice: no password). OBSERVE: this makes the
    /api/state and /api/action routes publicly reachable by anyone who
    knows the URL — publish/delete are exposed. The URL is unguessable
    enough for the owner's risk tolerance; revert this function to the
    cookie check if that changes."""
    return True


PANEL_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FOOTNOTE Control Panel</title>
<style>
:root{--ink:#14141e;--card:#1d1d29;--card2:#232331;--cream:#faf6ee;--red:#b21818;--dim:#8b8b9e;--ok:#3fbf6f;--warn:#e0a030;--blue:#5fa8e8}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ink);color:var(--cream);font:14px/1.5 system-ui,sans-serif;padding:16px;max-width:1280px;margin:0 auto}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:22px;letter-spacing:.5px}h1 .star{color:var(--red)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.dot.ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--red)}
#live{font-size:12px;color:var(--dim);margin-left:auto}
nav{display:flex;gap:6px;margin:10px 0;flex-wrap:wrap}
nav button{background:var(--card);border:0;color:var(--dim);border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer}
nav button.on{background:var(--red);color:var(--cream)}
.tab{display:none}.tab.on{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{background:var(--card);border-radius:12px;padding:16px;margin:12px 0}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:10px}
.stat{font-size:26px;font-weight:700}.stat small{font-size:13px;color:var(--dim);font-weight:400}
.delta{font-size:12px}.up{color:var(--ok)}.down{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:7px 8px;text-align:left;border-bottom:1px solid #2a2a3a}
th{color:var(--dim);font-weight:600;cursor:pointer;user-select:none}
th:hover{color:var(--cream)}
.st-pending{color:var(--warn)}.st-claimed{color:var(--blue)}.st-done{color:var(--ok)}.st-failed{color:var(--red)}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;background:#2a2a3a;color:var(--dim)}
.badge.pub{background:#1d3a2a;color:var(--ok)}.badge.priv{background:#3a2a1d;color:var(--warn)}
.btn{background:var(--red);color:var(--cream);border:0;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;margin:2px}
.btn:hover{filter:brightness(1.25)}
.btn.ghost{background:var(--card2)}.btn.sm{padding:4px 10px;font-size:12px}
.actions{display:flex;flex-wrap:wrap;gap:4px}
.videorow{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #2a2a3a;flex-wrap:wrap}
.videorow a{color:var(--cream);text-decoration:none;font-weight:600}
.videorow a:hover{color:var(--red)}
.grow{flex:1}
.log{font-family:ui-monospace,monospace;font-size:12px;color:var(--dim);max-height:300px;overflow-y:auto;background:var(--card2);border-radius:8px;padding:10px}
.log div{padding:2px 0;border-bottom:1px solid #232330;white-space:pre-wrap}
.log .t{color:var(--blue)}
canvas{width:100%;height:120px;display:block}
.bar{height:8px;border-radius:4px;background:var(--card2);overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;background:var(--red)}
textarea,input[type=text]{background:var(--card2);border:1px solid #3a3a4a;color:var(--cream);border-radius:8px;padding:10px;font-size:14px;width:100%;font-family:inherit}
textarea{min-height:70px;resize:vertical}
#toast{position:fixed;bottom:20px;right:20px;background:var(--red);color:var(--cream);padding:12px 20px;border-radius:10px;opacity:0;transition:opacity .3s;z-index:9;font-size:13px}
.toast-ok{background:#1d3a2a !important;color:var(--ok) !important}
.muted{color:var(--dim);font-size:12px}
.score{font-weight:700}.score.hi{color:var(--ok)}.score.mid{color:var(--warn)}.score.lo{color:var(--red)}
.toggle{cursor:pointer;user-select:none}
kbd{background:var(--card2);border-radius:4px;padding:1px 6px;font-size:11px}
details{margin:8px 0}summary{cursor:pointer;color:var(--dim);font-size:13px}
</style></head><body>
<header><h1>FOOTNOTE<span class="star">*</span> <span class="muted">Control Center</span></h1>
<span id="health"></span><span id="live"></span></header>
<nav>
<button class="on" data-t="dash" onclick="tab('dash')">📊 Dashboard</button>
<button data-t="videos" onclick="tab('videos')">📺 Videos</button>
<button data-t="pending" onclick="tab('pending')">🎬 Decisions</button>
<button data-t="tools" onclick="tab('tools')">🛠 Tools</button>
<button data-t="logs" onclick="tab('logs')">📜 Logs</button>
</nav>

<div class="tab on" id="t-dash">
 <div class="grid" id="statcards"></div>
 <div class="grid">
  <div class="card"><h2>Subscribers</h2><canvas id="c-subs"></canvas><p class="muted" id="subs-note"></p></div>
  <div class="card"><h2>Views</h2><canvas id="c-views"></canvas><p class="muted" id="views-note"></p></div>
 </div>
 <div class="grid">
  <div class="card"><h2>Hook virality scores</h2><div id="hooks" class="log" style="max-height:160px"></div></div>
  <div class="card"><h2>Auto-approve trust</h2><div id="trust"></div>
   <button class="btn ghost sm" style="margin-top:10px" onclick="act('toggle_auto')">Toggle auto-approve</button>
   <button class="btn ghost sm" onclick="act('reset')">Reset counter</button></div>
 </div>
 <div class="card"><h2>Job queue</h2><div id="queue"></div></div>
</div>

<div class="tab" id="t-videos">
 <div class="card"><h2>Channel videos <span class="muted">(click headers to sort)</span></h2>
 <div style="overflow-x:auto"><table id="vidtable"><thead><tr>
  <th onclick="sortV('title')">Title</th><th onclick="sortV('privacy')">Status</th>
  <th onclick="sortV('views')">Views</th><th onclick="sortV('likes')">Likes</th>
  <th onclick="sortV('comments')">Comments</th><th onclick="sortV('published')">Published</th><th></th>
 </tr></thead><tbody id="vidbody"></tbody></table></div></div>
</div>

<div class="tab" id="t-pending">
 <div class="card"><h2>Awaiting your decision</h2><div id="pending"></div></div>
</div>

<div class="tab" id="t-tools">
 <div class="grid">
  <div class="card"><h2>Quick actions</h2><div class="actions">
   <button class="btn" onclick="act('next')">🎬 New video</button>
   <button class="btn" onclick="act('publish')">🚀 Publish all pending</button>
   <button class="btn ghost" onclick="act('retry')">🔁 Retry failed jobs</button>
   <button class="btn ghost" onclick="act('pause')">⏸ Pause auto work</button>
   <button class="btn ghost" onclick="act('resume')">▶️ Resume</button>
   <button class="btn ghost" onclick="act('clear_failed')">🧹 Clear failed</button>
   <button class="btn ghost" onclick="act('refresh_channel')">🔄 Refresh channel data</button>
   <button class="btn ghost" style="background:#4a1a1a" onclick="if(confirm('Wipe the queue and drop pending approvals? Videos stay private.'))act('clear_all')">⛔ Clear everything</button>
  </div></div>
  <div class="card"><h2>Custom video</h2>
   <textarea id="idea" placeholder="Topic or angle… e.g. 'the 1989 memo that created Area 51'"></textarea>
   <button class="btn" style="margin-top:8px" onclick="idea()">✍️ Write & queue script</button>
   <p class="muted" style="margin-top:6px">Grounded research → virality gate → cloud render. Preview lands in Telegram + the Decisions tab.</p></div>
 </div>
 <div class="card"><h2>Topic guidance (from growth analysis)</h2><div id="direction" class="log" style="max-height:180px"></div></div>
 <div class="card"><h2>Used topics</h2><div id="topics" class="muted"></div></div>
</div>

<div class="tab" id="t-logs">
 <div class="card"><h2>Brain activity</h2><div id="log" class="log"></div></div>
</div>
<div id="toast"></div>
<script>
let DATA=null,PAUSED=false,VIDSORT={k:'views',asc:false};
function tab(t){document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x.id=='t-'+t))}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function toast(m,ok){const t=document.getElementById('toast');t.textContent=m;t.className=ok?'toast-ok':'';t.style.opacity=1;
 setTimeout(()=>t.style.opacity=0,2600)}
function fmt(n){return (n||0).toLocaleString()}

async function load(){
 const r=await fetch('/api/state');if(!r.ok)return;
 DATA=await r.json();render();
}
function render(){
 const d=DATA;
 // health
 const h=d.worker.mins_ago<10?'ok':d.worker.mins_ago<70?'warn':'bad';
 document.getElementById('health').innerHTML=
  '<span class="dot '+h+'"></span><span class="muted">worker '+d.worker.mins_ago+'m · brain '+d.uptime+'</span>';
 document.getElementById('live').textContent=(PAUSED?'⏸ ':'')+'auto-refresh '+(PAUSED?'paused':'15s')+' · '+new Date().toLocaleTimeString();
 // stat cards
 const C=(t,v,s)=>'<div class="card"><h2>'+t+'</h2><div class="stat">'+v+'</div><div class="muted">'+(s||'')+'</div></div>';
 const trust=d.settings.approved+' / 10';
 document.getElementById('statcards').innerHTML=
  C('Subscribers',fmt(d.channel.subs),d.delta.subs!=null?'<span class="delta '+(d.delta.subs>=0?'up':'down')+'">'+(d.delta.subs>=0?'+':'')+d.delta.subs+' today</span>':'first reading')+
  C('Total views',fmt(d.channel.views),d.delta.views!=null?'<span class="delta '+(d.delta.views>=0?'up':'down')+'">'+(d.delta.views>=0?'+':'')+fmt(d.delta.views)+' today</span>':'first reading')+
  C('Videos',fmt(d.channel.videos),d.channel.public+' public · '+d.channel.private+' private')+
  C('Queue',d.queue.pending+' <small>pending</small>',d.queue.claimed+' claimed · '+d.queue.failed+' failed')+
  C('Decisions',d.queue.awaiting+' <small>awaiting</small>','tap Decisions tab')+
  C('Auto-approve',d.settings.auto_approve?'<span style="color:var(--ok)">ON</span>':'<span style="color:var(--dim)">off</span>','trust '+trust+' · '+(d.settings.paused?'PAUSED':'running'));
 // sparklines
 spark('c-subs',d.history.map(x=>x.subs));spark('c-views',d.history.map(x=>x.views));
 document.getElementById('subs-note').textContent=d.history.length+' days of history';
 document.getElementById('views-note').textContent=d.history.length+' days of history';
 // hooks
 const hs=d.hooks.slice(-12).reverse().map(x=>{
  const c=x.score>=80?'hi':x.score>=60?'mid':'lo';
  return '<div><span class="score '+c+'">'+x.score+'</span> — '+esc(x.reason||x.id)+'</div>'}).join('');
 document.getElementById('hooks').innerHTML=hs||'<div>(no scores yet)</div>';
 // trust bar
 document.getElementById('trust').innerHTML='Approvals: <b>'+d.settings.approved+'</b>/10'+
  '<div class="bar"><i style="width:'+(d.settings.approved*10)+'%"></i></div>'+
  '<p class="muted" style="margin-top:6px">'+(d.settings.auto_approve?'Auto-approve is ON — new videos publish themselves after render.':'10 manual ✅s enable auto-approve.')+'</p>';
 // queue
 let q='<table><tr><th></th><th>id</th><th>type</th><th>status</th><th>age</th><th>title</th></tr>';
 if(!d.jobs.length)q+='<tr><td colspan="6" class="muted">Queue empty — make a video from the Tools tab</td></tr>';
 for(const j of d.jobs.slice(-15).reverse()){
  q+='<tr><td class="st-'+j.status+'">'+({pending:'⏳',claimed:'🔧',done:'✅',failed:'❌'}[j.status]||'❓')+'</td><td><code>'+j.id.slice(0,8)+'</code></td><td>'+j.type+'</td><td class="st-'+j.status+'">'+j.status+'</td><td>'+j.age+'m</td><td>'+esc(j.title)+'</td></tr>'}
 document.getElementById('queue').innerHTML=q+'</table>';
 // videos table
 let v='';
 if(!d.videos.length)v='<tr><td colspan="7" class="muted">No videos yet</td></tr>';
 for(const x of d.videos){
  v+='<tr><td><a href="https://youtu.be/'+x.id+'" target="_blank">'+esc(x.title.slice(0,52))+'</a></td>'+
  '<td><span class="badge '+(x.privacy==='public'?'pub':'priv')+'">'+x.privacy+'</span></td>'+
  '<td><b>'+fmt(x.views)+'</b></td><td>'+fmt(x.likes)+'</td><td>'+fmt(x.comments)+'</td><td class="muted">'+x.published+'</td>'+
  '<td>'+(x.privacy!=='public'?'<button class="btn sm" onclick="act(\'vpub:'+x.id+'\')">Make public</button>':'')+
  ' <button class="btn ghost sm" onclick="act(\'vpriv:'+x.id+'\')">Private</button></td></tr>'}
 document.getElementById('vidbody').innerHTML=v;
 // pending
 let ph='';
 if(!d.pending.length)ph='<p class="muted">Nothing awaiting decision. Renders land here automatically.</p>';
 for(const x of d.pending){
  ph+='<div class="videorow"><a href="'+x.url+'" target="_blank">🎬 '+esc(x.title)+'</a>'+
   '<span class="badge">'+x.formats+' formats</span><span class="muted">'+x.alts+' alt titles</span><span class="grow"></span>'+
   '<button class="btn" onclick="act(\'publish:'+x.id+'\')">✅ Publish</button>'+
   '<button class="btn ghost" style="background:#4a1a1a" onclick="if(confirm(\'Delete from YouTube permanently?\'))act(\'reject:'+x.id+'\')">❌ Delete</button></div>';
  if(x.alts)for(let i=0;i<x.alts.length;i++)
   ph+='<div class="videorow" style="padding-left:24px"><span class="muted">alt '+(i+2)+':</span> <span>'+esc(x.alts[i])+'</span><span class="grow"></span><button class="btn ghost sm" onclick="act(\'retitle:'+x.id+':'+i+'\')">Use this title</button></div>';
 }
 document.getElementById('pending').innerHTML=ph;
 // tools
 document.getElementById('direction').textContent=d.direction||'(no growth analysis yet — accumulates after ~3 public videos)';
 document.getElementById('topics').textContent=d.used_topics.join(' · ')||'(none)';
 // log
 document.getElementById('log').innerHTML=d.log.map(l=>{
  const m=l.match(/^(\d\d:\d\d:\d\d)(.*)$/);return m?'<div><span class="t">'+m[1]+'</span>'+esc(m[2])+'</div>':'<div>'+esc(l)+'</div>'}).join('');
}
function spark(id,arr){
 const c=document.getElementById(id);if(!c||arr.length<2){if(c)c.getContext('2d').clearRect(0,0,c.width,c.height);return}
 c.width=c.offsetWidth*2;c.height=240;const g=c.getContext('2d');
 const min=Math.min(...arr),max=Math.max(...arr),rg=(max-min)||1;
 g.strokeStyle='#b21818';g.lineWidth=4;g.beginPath();
 arr.forEach((v,i)=>{const x=i/(arr.length-1)*(c.width-20)+10,y=220-(v-min)/rg*190;i?g.lineTo(x,y):g.moveTo(x,y)});
 g.stroke();
 g.fillStyle='#b21818';arr.forEach((v,i)=>{const x=i/(arr.length-1)*(c.width-20)+10,y=220-(v-min)/rg*190;
  g.beginPath();g.arc(x,y,5,0,7);g.fill()});
}
function sortV(k){VIDSORT.asc=VIDSORT.k===k?!VIDSORT.asc:false;VIDSORT.k=k;
 DATA.videos.sort((a,b)=>{const x=a[VIDSORT.k],y=b[VIDSORT.k];
  return (typeof x=='number'?x-y:String(x).localeCompare(String(y)))*(VIDSORT.asc?1:-1)});render()}
async function idea(){
 const t=document.getElementById('idea').value.trim();if(!t)return toast('Type a topic first');
 toast('Writing script…');
 const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'idea',topic:t})});
 const d=await r.json();toast(d.ok?'Script queued — rendering soon':'Failed: '+(d.error||''),d.ok);
 if(d.ok)document.getElementById('idea').value='';
 load();
}
async function act(a){
 const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});
 const d=await r.json();
 if(d.ok===false){toast('❌ '+(d.error||'failed'));return}
 const nice={next:'New video queued',publish:'Published',retry:(d.requeued||0)+' requeued',pause:'Paused',resume:'Resumed',
  clear_failed:'Failed jobs cleared',clear_all:'Everything cleared',reset:'Counter reset',toggle_auto:'Auto-approve toggled',
  refresh_channel:'Channel data refreshed',idea:'Script queued'};
 if(a.startsWith('publish:'))toast('Published ✅',1);
 else if(a.startsWith('reject:'))toast('Deleted 🗑');
 else if(a.startsWith('vpub:'))toast('Video public',1);
 else if(a.startsWith('vpriv:'))toast('Video private');
 else if(a.startsWith('retitle:'))toast('Title updated ✏️',1);
 else toast(nice[a]||'Done',1);
 load();
}
load();setInterval(()=>{if(!PAUSED)load()},15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!PAUSED)load()});
</script></body></html>"""


_channel_cache = {"t": 0, "data": None}


def _channel_snapshot():
    """Channel + video data with a 5-minute server cache — the panel
    auto-refreshes every 15s and must not burn YouTube quota."""
    import time as _t
    if (_t.time() - _channel_cache["t"]) < 300 and _channel_cache["data"]:
        return _channel_cache["data"]
    try:
        ch = yt.channel_stats() or {}
        vids = yt.my_videos(30)
        data = {"ch": ch, "vids": vids}
        _channel_cache.update(t=_t.time(), data=data)
        return data
    except Exception:
        return _channel_cache["data"] or {"ch": {}, "vids": []}


@app.route("/panel")
def panel():
    return PANEL_HTML


@app.route("/api/state")
def api_state():
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
    snap = _channel_snapshot()
    ch = snap["ch"]
    vids = snap["vids"]
    hist = state.STATE.get("stats_history", [])
    today = None
    delta = {}
    if len(hist) >= 2:
        delta = {"subs": hist[-1]["subs"] - hist[-2]["subs"],
                 "views": hist[-1]["views"] - hist[-2]["views"]}
    jobs_list = state.STATE.get("jobs", [])
    now = _t.time()
    s = state.STATE["settings"]
    pending_detail = []
    for uid, p in state.STATE["pending_videos"].items():
        pending_detail.append({
            "id": uid, "title": p.get("title", "?"),
            "url": p.get("video_url", ""),
            "alts": (p.get("title_alternatives") or [])[:2],
            "formats": len(p.get("video_urls") or [1]),
        })
    return jsonify({
        "channel": {"title": ch.get("title", "?"),
                    "subs": ch.get("subs", 0), "views": ch.get("views", 0),
                    "videos": ch.get("videos", len(vids)),
                    "public": sum(1 for v in vids if v["privacy"] == "public"),
                    "private": sum(1 for v in vids if v["privacy"] != "public")},
        "delta": delta,
        "videos": vids,
        "history": hist[-30:],
        "hooks": state.STATE.get("hook_scores", [])[-30:],
        "queue": {"pending": sum(1 for j in jobs_list if j["status"] == "pending"),
                  "claimed": sum(1 for j in jobs_list if j["status"] == "claimed"),
                  "failed": sum(1 for j in jobs_list if j["status"] == "failed"),
                  "awaiting": len(state.STATE["pending_videos"])},
        "settings": {"auto_approve": s.get("auto_approve"),
                     "approved": s.get("approved_count", 0),
                     "paused": s.get("paused")},
        "worker": {"mins_ago": mins},
        "uptime": f"{int(_t.time() - STARTED_AT) // 3600}h "
                  f"{(int(_t.time() - STARTED_AT) % 3600) // 60}m",
        "pending": pending_detail,
        "jobs": [{"id": j["id"], "type": j["type"], "status": j["status"],
                  "age": int((now - j.get("created", now)) / 60),
                  "title": (j.get("script", {}).get("title") or
                            j.get("result", {}).get("msg", ""))[:60]}
                 for j in jobs_list],
        "direction": state.STATE.get("topic_direction", ""),
        "used_topics": state.STATE.get("used_topics", [])[-25:],
        "log": comms.LOG[-25:],
    })


@app.route("/api/action", methods=["POST"])
def api_action():
    state.default_state()
    data = request.get_json(force=True, silent=True) or {}
    a = data.get("action", "")
    import cloud
    if a == "next":
        comms.send("Writing a script… (from panel)")
        made = brain.queue_next_video(1)
        return jsonify({"ok": bool(made),
                        "error": "" if made else "script generation failed"})
    if a == "idea":
        topic = (data.get("topic") or "").strip()
        if not topic:
            return jsonify({"ok": False, "error": "empty topic"}), 400
        comms.typing()
        script = brain.generate_script(
            direction=f"Topic requested by the channel owner: {topic}")
        if not script:
            return jsonify({"ok": False, "error": "generation failed"})
        import uuid as _uuid
        job = jobs.add_job("render", {"script": script,
                                      "approval_id": _uuid.uuid4().hex[:10]})
        cloud.wake_soon("render")
        state.STATE.setdefault("used_topics", []).append(script.get("id", "?"))
        state.save_soon()
        comms.send(f"🎬 <b>Custom video queued</b> — "
                   f"{comms.esc(script['title'])}", html=True)
        return jsonify({"ok": True, "job_id": job["id"]})
    if a == "publish":
        _publish_pending()
        return jsonify({"ok": True})
    if a.startswith("publish:"):
        uid = a.split(":", 1)[1]
        if uid in state.STATE["pending_videos"]:
            s_ = state.STATE["settings"]
            s_["approved_count"] = s_.get("approved_count", 0) + 1
            _publish_now(uid)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "not pending"})
    if a.startswith("reject:"):
        _delete_pending(a.split(":", 1)[1])
        return jsonify({"ok": True})
    if a.startswith("retitle:"):
        _, vid, idx = a.split(":", 2)
        p = state.STATE["pending_videos"].get(vid)
        if p:
            alts = p.get("title_alternatives") or []
            i = int(idx)
            if 0 <= i < len(alts):
                try:
                    for url in (p.get("video_urls")
                                or [p.get("video_url")]):
                        if url:
                            yt.update_title(
                                url.rstrip("/").split("/")[-1], alts[i])
                    p["title"] = alts[i]
                    state.save_soon()
                    return jsonify({"ok": True})
                except Exception as e:
                    return jsonify({"ok": False, "error": str(e)[:120]})
        return jsonify({"ok": False, "error": "not found"})
    if a.startswith("vpub:"):
        try:
            yt.make_public(f"https://youtu.be/{a.split(':', 1)[1]}")
            _channel_cache.update(t=0)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:120]})
    if a.startswith("vpriv:"):
        try:
            yt.make_private(f"https://youtu.be/{a.split(':', 1)[1]}")
            _channel_cache.update(t=0)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:120]})
    if a == "toggle_auto":
        s_ = state.STATE["settings"]
        s_["auto_approve"] = not s_.get("auto_approve")
        state.save_soon()
        return jsonify({"ok": True, "now": s_["auto_approve"]})
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
    if a == "refresh_channel":
        _channel_cache.update(t=0)
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
