"""Channel Agent — a 24/7 YouTube growth manager (Render brain + home-PC
worker). Telegram is the console.

This is the composition root: Flask worker endpoints, Telegram loop,
scheduler, slash commands, and approval buttons. Intelligence lives in
brain.py; YouTube access in yt.py; jobs in jobs.py.
"""

import re
import threading
import time
from datetime import datetime, timedelta

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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FOOTNOTE Production Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ---- tokens ---- */
:root{
  --paper:#faf6ee; --card:#fffdf6; --wash:#f3ecdc;
  --ink:#14141e; --muted:#5c5a4f; --dim:#8a8574; /* dim: decoration only */
  --rule:#e0d9c6; --rule-soft:#ebe5d4;
  --red:#b21818; --red-dark:#8f1414; --red-lift:#d95050;
  --green:#2e7d43; --amber:#96650a; --blue:#2b5fa8;
  --track:#efc9c9; /* lighter step of the red ramp, meter track */
  --serif:'Newsreader',Georgia,serif;
  --sans:'IBM Plex Sans',system-ui,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
  --rad:2px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--paper);color:var(--ink);font:15px/1.55 var(--sans);padding:0 20px 90px}
.wrap{max-width:1180px;margin:0 auto}

/* ---- focus ---- */
:focus-visible{outline:2px solid var(--red);outline-offset:2px;border-radius:var(--rad)}
::selection{background:var(--red);color:var(--paper)}

/* ---- masthead ---- */
.mast{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:26px 0 14px;border-bottom:2px solid var(--ink)}
.mast-brand{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.mast-star{width:30px;height:30px;align-self:center;flex:none}
.mast-star .spokes{stroke:var(--red);stroke-width:2.4;stroke-linecap:round}
.mast-star.live .spokes{animation:spin 40s linear infinite;transform-origin:12px 12px}
@keyframes spin{to{transform:rotate(360deg)}}
.mast h1{font:600 34px/1 var(--serif);letter-spacing:.01em}
/* the tagline breaks as a whole line, never mid-phrase */
.mast .tag{font:italic 400 16px/1 var(--serif);color:var(--muted);white-space:nowrap}
.mast-meta{display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:13.5px;color:var(--muted)}
.beat{display:inline-flex;align-items:center;gap:7px;min-height:24px}
.beat .led{width:8px;height:8px;border-radius:50%;flex:none;background:var(--dim)}
.led.ok{background:var(--green)} .led.warn{background:var(--amber)} .led.bad{background:var(--red-lift)}
#refresh{background:none;border:0;padding:6px 8px;min-height:32px;color:var(--muted);font:inherit;cursor:pointer;border-radius:var(--rad)}
#refresh:hover{color:var(--ink)}
/* muted is right for a panel that is keeping itself current; a panel that has stopped
   is showing a photograph, and that is worth the ink */
#refresh.off{color:var(--ink)}
.pausedflag{background:var(--red);color:#fff;border-radius:999px;padding:3px 9px;font-size:12.5px;letter-spacing:.02em}
.loadfail{border:1px solid var(--red);border-left-width:4px;background:#fdf1f1;color:var(--ink);padding:12px 14px;border-radius:var(--rad);margin:14px 0;font-size:14.5px}

/* ---- nav ---- */
nav{display:flex;gap:2px;border-bottom:1px solid var(--rule);overflow-x:auto;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{background:none;border:0;border-bottom:2px solid transparent;margin-bottom:-1px;padding:13px 16px 11px;font:500 15px var(--sans);color:var(--muted);cursor:pointer;white-space:nowrap;min-height:44px}
nav button:hover{color:var(--ink)}
nav button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--red)}
nav .count{display:inline-block;min-width:20px;margin-left:7px;padding:1px 6px;border-radius:9px;background:var(--red);color:var(--paper);font:600 11.5px/1.4 var(--sans);text-align:center}
nav .count.zero{display:none}
/* phones: wrap the strip onto two rows rather than hide a tab behind a swipe */
@media (max-width:560px){
  nav{flex-wrap:wrap;overflow-x:visible}
  nav button{padding:12px 12px 10px;font-size:14.5px}
}

/* ---- sections & headers ---- */
section.tab{display:none;padding-top:26px}
section.tab.on{display:block}
.sec{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:30px 0 12px}
.sec:first-child{margin-top:4px}
.sec h2{font:600 24px/1.2 var(--serif)}
.sec .note{font-size:13px;color:var(--muted)}

/* ---- stat tiles ---- */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:14px 16px 12px;min-height:104px;display:flex;flex-direction:column;gap:2px}
.tile .label{font-size:13.5px;color:var(--muted)}
.tile .value{font:600 27px/1.15 var(--sans);letter-spacing:-.01em;font-variant-numeric:proportional-nums}
.tile .sub{font-size:13px;color:var(--muted)}
.tile .spark{margin:4px 0 2px}
.delta{font-size:13px;font-weight:600}
.delta.up{color:var(--green)} .delta.down{color:var(--red)}
.delta.flat{color:var(--muted)}
/* the tile's footer line sits on the floor, so every tile in the row aligns */
.tile > .sub:last-child, .tile > .delta:last-child{margin-top:auto}

/* ---- charts ---- */
.charts{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:760px){.charts{grid-template-columns:1fr}}
/* two-up row where each card keeps its natural height — a stretched card leaves a void */
.duo{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-items:start;margin-top:10px}
@media (max-width:760px){.duo{grid-template-columns:1fr}}
.duo > .card{margin:0}
.chart{background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:16px 18px 12px}
.chart h3{font:600 17px/1.2 var(--serif);margin-bottom:2px}
.chart .note{font-size:12.5px;color:var(--muted);margin-bottom:8px}
.plot{position:relative;touch-action:none}
.plot svg{display:block;width:100%;height:190px;cursor:crosshair}
.plot .grid{stroke:var(--rule-soft);stroke-width:1}
.plot .axis{fill:var(--muted);font:12px var(--sans)}
.plot .line{fill:none;stroke:var(--red);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.plot .area{fill:var(--red);opacity:.07}
.plot .enddot{fill:var(--red);stroke:var(--card);stroke-width:2}
.plot .vline{stroke:var(--dim);stroke-width:1;opacity:0}
.plot .hitrect{fill:none;pointer-events:all}
.plot .tipline{stroke:var(--red);stroke-width:1;opacity:0}
.tip{position:absolute;pointer-events:none;background:var(--ink);color:var(--paper);border-radius:var(--rad);padding:7px 11px;font:500 13px var(--sans);display:none;z-index:5;box-shadow:0 3px 14px rgba(20,20,30,.25)}
.tip .tv{font-weight:700}
.tip .td{color:#c9c4b4;font-size:12px}
details.tbl{margin-top:6px;border-top:1px solid var(--rule-soft)}
details.tbl summary{cursor:pointer;font-size:13px;color:var(--muted);padding:7px 0 5px;min-height:32px;display:flex;align-items:center;gap:7px;list-style:none}
details.tbl summary::-webkit-details-marker{display:none}
details.tbl summary::before{content:"";width:0;height:0;border:4px solid transparent;border-left-color:var(--dim);transition:transform .15s}
details.tbl[open] summary::before{transform:rotate(90deg) translateX(1px)}
details.tbl summary:hover{color:var(--ink)}
details.tbl summary:hover::before{border-left-color:var(--ink)}
details.tbl table{margin:2px 0 8px}
details.tbl tbody tr:hover{background:var(--wash)}

/* ---- trust meter ---- */
.meter{display:flex;gap:3px;margin:10px 0 8px}
.meter i{flex:1;height:14px;background:var(--track);border-radius:1px}
.meter i.f{background:var(--red)}

/* ---- cards / lists ---- */
.card{background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:16px 18px;margin:10px 0}
.list{background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:6px 18px}
.rowline{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--rule-soft);flex-wrap:wrap}
.rowline:last-child{border-bottom:0}
.rowline .grow{flex:1;min-width:180px}
.ttl{font-weight:600;color:var(--ink)}
a.ttl{color:var(--ink);text-decoration:none;background:linear-gradient(var(--red),var(--red)) left bottom/0 1px no-repeat;transition:background-size .15s;padding-bottom:1px}
a.ttl:hover{background-size:100% 1px}
.scorebar{width:56px;height:9px;border-radius:2px;background:var(--wash);overflow:hidden;flex:none}
.scorebar i{display:block;height:100%;border-radius:2px 0 0 2px}
.sc{font:600 13.5px var(--mono)}

/* ---- status vocabulary ---- */
.st{display:inline-flex;align-items:center;gap:7px;font-size:13.5px;min-height:24px}
.st .dot{width:7px;height:7px;border-radius:50%;flex:none}
.st-wait .dot{background:var(--amber)} .st-wait{color:var(--amber)}
.st-render .dot{background:var(--blue);animation:pulse 2.2s ease-in-out infinite} .st-render{color:var(--blue)}
@keyframes pulse{50%{opacity:.35}}
.st-done .dot{background:var(--green)} .st-done{color:var(--green)}
.st-fail .dot{background:var(--red-lift)} .st-fail{color:var(--red-lift)}
.st-off .dot{background:var(--dim)} .st-off{color:var(--muted)}

/* ---- tables ---- */
.tblwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--rule-soft)}
thead th{border-bottom:1px solid var(--rule);font:500 13px var(--sans);color:var(--muted)}
th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
th.sortable:hover{color:var(--ink)}
th .arr{font-size:10px;margin-left:3px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.mono{font-family:var(--mono);font-size:13px;color:var(--muted)}
tbody tr:hover{background:var(--wash)}
/* an empty state is the only row in its card, so the card edge closes it — a rule
   underneath just hangs there. border:0 also has to beat the td rule above. */
.empty{padding:26px 12px;color:var(--muted);font-size:14.5px;border:0}
/* set by headIf(): column names have nothing to name, and an empty state sizes the
   columns to itself, which slides the names off the top of them */
table.nohead thead{display:none}
th button{background:none;border:0;padding:0;margin:0;font:inherit;color:inherit;cursor:pointer}
th button:hover{color:var(--ink)}
.btn[aria-pressed=true]{background:var(--ink);color:var(--paper)}
.cl{display:none}
.sortbar{display:none;align-items:center;gap:8px;padding:10px 0 4px;font-size:13px;color:var(--muted)}
.sortbar select{font:14px var(--sans);color:var(--ink);background:var(--paper);border:1px solid var(--rule);border-radius:var(--rad);padding:7px 9px;min-height:38px}

/* below 900 the seven columns cannot hold their width — the title crushes to six
   lines and the trailing columns fall off the card. So each row becomes a block
   and every value carries its own label. Sorting moves to a native select,
   which a thumb handles better than a header cell anyway. */
@media (max-width:900px){
  .rtable thead{display:none}
  .rtable, .rtable tbody, .rtable tr, .rtable td{display:block;width:auto;min-width:0}
  .rtable tr{padding:11px 0;border-bottom:1px solid var(--rule)}
  .rtable tr:last-child{border-bottom:0}
  .rtable td{border:0;padding:2px 0;display:flex;align-items:baseline;gap:10px;text-align:left}
  .rtable td.num{text-align:left}
  /* the title leads its block: no label, its own full-width line */
  .rtable td.lead{display:block;padding:0 0 5px}
  .rtable td.empty{padding:24px 0}
  .rtable .cl{display:block;flex:0 0 88px;font-size:12.5px;color:var(--muted)}
  /* the row's action is not a labelled value: it sits flush left, set off by a gap */
  .rtable td.act{padding-top:7px}
  .rtable td.act .linkbtn{padding-left:0}
  .sortbar{display:flex}
}
/* tablets have width to spare: the same blocks, but the values run along one
   line instead of stacking, so a film is four lines tall rather than seven */
@media (min-width:621px) and (max-width:900px){
  .rtable tr{display:flex;flex-wrap:wrap;align-items:baseline;column-gap:22px;row-gap:3px}
  .rtable td{padding:0}
  .rtable td.lead{flex:1 1 100%;padding:0 0 5px}
  .rtable .cl{flex:0 0 auto}
  .rtable td.act{margin-left:auto;padding-top:0}
}

/* ---- buttons ---- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--card);border:1px solid var(--ink);color:var(--ink);border-radius:var(--rad);padding:8px 16px;min-height:38px;font:500 14px var(--sans);cursor:pointer;transition:background .15s,color .15s}
.btn:hover{background:var(--ink);color:var(--paper)}
.btn:disabled{opacity:.5;cursor:default}
.btn-primary{background:var(--red);border-color:var(--red-dark);color:var(--paper)}
.btn-primary:hover{background:var(--red-dark)}
.btn-danger{border-color:var(--red);color:var(--red)}
.btn-danger:hover{background:var(--red);color:var(--paper)}
/* an armed button that is not destructive still has to look armed — the label carries
   it, but ink makes the pending click unmistakable. Danger keeps the red, after. */
.btn.confirm{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.btn-danger.confirm{background:var(--red);color:var(--paper);border-color:var(--red)}
.btn-sm{padding:5px 12px;min-height:32px;font-size:13px}
.linkbtn{background:none;border:0;padding:6px 8px;min-height:32px;font:500 13.5px var(--sans);color:var(--muted);cursor:pointer;border-radius:var(--rad);text-decoration:underline;text-underline-offset:3px}
.linkbtn:hover{color:var(--red)}
/* a row action never wraps: "Make private" on two lines reads like two links */
td.act .linkbtn{white-space:nowrap}
.actions{display:flex;flex-wrap:wrap;gap:8px}
/* grouped action bank: routine work first, clearing set apart behind a rule */
.agroup + .agroup{margin-top:14px;padding-top:14px;border-top:1px solid var(--rule-soft)}
.agroup.danger{border-top-color:var(--rule)}
.alabel{font-size:12.5px;color:var(--muted);margin-bottom:8px}

/* ---- chips / tags ---- */
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;padding:2px 9px;border-radius:8px;border:1px solid var(--rule);color:var(--muted);background:var(--paper)}
.chip .dot{width:6px;height:6px;border-radius:50%}
.chip.pub .dot{background:var(--green)} .chip.pub{color:var(--green);border-color:#cfe3d3}
.chip.priv .dot{background:var(--amber)} .chip.priv{color:var(--amber);border-color:#e8d8ae}
.tags{display:flex;flex-wrap:wrap;gap:6px}
.tags .chip{font-size:12.5px}

/* ---- forms ---- */
textarea{width:100%;min-height:74px;resize:vertical;background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:11px 13px;font:15px var(--sans);color:var(--ink)}
textarea:focus{border-color:var(--ink);outline:none}
textarea:focus-visible{outline:2px solid var(--red);outline-offset:0}
.help{font-size:13px;color:var(--muted);margin-top:8px}

/* ---- guidance / log ---- */
.prose{background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:16px 18px;font-size:14px;color:var(--ink);white-space:pre-wrap;line-height:1.6;max-height:220px;overflow-y:auto}
.log{font:13px/1.7 var(--mono);background:var(--ink);color:#d8d3c3;border-radius:var(--rad);padding:14px 16px;max-height:340px;overflow-y:auto}
.log .lt{color:#8a8574}
.log div{border-bottom:1px solid #232331;padding:2px 0}
.log div:last-child{border-bottom:0}
/* the Ledger is the one place worth scrolling at length */
.log.big{max-height:min(72vh,780px)}
.log .day{position:sticky;top:-14px;margin:10px -16px 4px;padding:5px 16px;background:#232331;color:#faf6ee;border:0;font:600 12px/1.5 var(--sans);letter-spacing:.07em}
.log .day:first-child{margin-top:-8px}
.log .k{display:inline-block;min-width:56px;color:#7d7a6c;font-size:11px;letter-spacing:.05em;text-transform:uppercase}
.log .b{color:#f0ebde}
/* where the line came from: volatile stream, saved state, or this browser */
.log .src{color:#57553f;font-size:11px;margin-left:6px}
.log .row-panel .b{color:#e8c9a0}
.log .row-film .b{color:#a8d8b4}
.log .row-fail .b{color:#f0a3a3}

/* ---- filter chips / small controls ---- */
.lbar{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:10px 0}
.chipbtn{background:var(--paper);border:1px solid var(--rule);color:var(--muted);border-radius:999px;padding:5px 12px;min-height:32px;font:500 13px var(--sans);cursor:pointer}
.chipbtn:hover{color:var(--ink);border-color:var(--ink)}
.chipbtn[aria-pressed=true]{background:var(--ink);border-color:var(--ink);color:var(--paper)}
/* a chip that removes something: the × is the button, the label stays text */
.chip .x{background:none;border:0;padding:0 0 0 2px;margin:0;color:var(--dim);font:600 14px/1 var(--sans);cursor:pointer;min-height:0}
.chip .x:hover{color:var(--red)}

/* ---- number / text fields ---- */
input[type=number],input[type=text]{background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:8px 10px;font:15px var(--sans);color:var(--ink);min-height:38px}
input[type=number]{width:96px;font-variant-numeric:tabular-nums}
input:focus{border-color:var(--ink);outline:none}
input:focus-visible{outline:2px solid var(--red);outline-offset:0}
.field{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 12px}
.field:last-child{margin-bottom:0}
.field > label{font-size:13.5px;color:var(--muted);flex:0 0 190px}
.field .hint{font-size:12.5px;color:var(--muted);flex:1 1 100%}
@media (max-width:560px){.field > label{flex:1 1 100%}}

/* ---- answers mailbox ---- */
.ans{background:var(--card);border:1px solid var(--rule);border-radius:var(--rad);padding:14px 16px;margin:10px 0}
.ans .meta{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.ans .kind{font:600 11.5px var(--sans);letter-spacing:.07em;text-transform:uppercase;color:var(--red)}
.ans .when{font-size:12.5px;color:var(--muted)}
.ans .body{white-space:pre-wrap;font-size:14px;line-height:1.6;max-height:320px;overflow-y:auto}

/* ---- work in flight ---- */
/* A background action answers in milliseconds and then keeps working for a
   minute. Nothing in the panel used to say so, which is why queueing a video
   looked like it did nothing at all. */
.working{border:1px solid var(--rule);border-left:4px solid var(--blue);background:#f1f5fc;padding:10px 14px;border-radius:var(--rad);margin:14px 0;font-size:14px}
.working .wrow{display:flex;align-items:center;gap:9px;padding:3px 0}
.working .el{margin-left:auto;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
@keyframes spin360{to{transform:rotate(360deg)}}
.spin{display:inline-block;width:12px;height:12px;flex:none;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin360 .75s linear infinite}
.working .spin{color:var(--blue)}
/* :disabled dims to .5, which would grey out the spinner mid-click */
.btn.busy:disabled{opacity:.8}

/* ---- toast ---- */
#toast{position:fixed;bottom:22px;right:22px;z-index:50;background:var(--ink);color:var(--paper);padding:13px 18px;border-radius:var(--rad);font:500 14px var(--sans);box-shadow:0 4px 18px rgba(20,20,30,.35);opacity:0;transform:translateY(8px);transition:opacity .25s,transform .25s;max-width:320px}
#toast.show{opacity:1;transform:none}
/* a 4px accent was the only thing separating "Published" from "Network error" —
   the failure now carries the red itself, and the wording still says so too */
#toast.err{background:var(--red);border-left:4px solid #7d0f0f}

/* ---- flash on data change ---- */
@keyframes flash{0%{background:var(--wash)}100%{background:var(--card)}}
.flash{animation:flash 1s ease-out}
@media (prefers-reduced-motion:reduce){
  .mast-star.live .spokes,.st-render .dot{animation:none}
  /* the ring stays as a static mark; the "Working…" label is what carries it */
  .spin{animation:none}
  .flash{animation:none}
  *{transition:none!important}
}
</style>
</head>
<body>
<div class="wrap">

<header class="mast">
  <div class="mast-brand">
    <svg class="mast-star" id="star" viewBox="0 0 24 24" aria-hidden="true">
      <g class="spokes"><path d="M12 2v20M3.3 7l17.4 10M20.7 7L3.3 17"/></g>
    </svg>
    <h1>FOOTNOTE</h1>
    <span class="tag">production desk</span>
  </div>
  <div class="mast-meta">
    <span class="beat" id="beat"><span class="led" id="beatled"></span><span id="beattxt">connecting</span></span>
    <span id="uptime"></span>
    <span id="clock"></span>
    <!-- a paused agent makes nothing at all; that cannot live only in a tile subtitle
         about auto-approve and a button label at the foot of the page -->
    <span class="pausedflag" id="pausedflag" hidden>agent paused</span>
    <button id="refresh"></button>
  </div>
</header>

<nav role="tablist" aria-label="Panel sections">
  <button role="tab" aria-selected="true" data-t="desk" id="tab-desk">Desk</button>
  <button role="tab" aria-selected="false" data-t="films" id="tab-films">Films</button>
  <button role="tab" aria-selected="false" data-t="dec" id="tab-dec">Decisions<span class="count zero" id="navdec"></span></button>
  <button role="tab" aria-selected="false" data-t="studio" id="tab-studio">Studio</button>
  <button role="tab" aria-selected="false" data-t="ledger" id="tab-ledger">Ledger</button>
</nav>

<!-- load() keeps the last render through a network drop, but the first load has no
     last render to keep, and a free-tier dyno waking up fails exactly there: without
     this the panel would just sit empty with nothing to explain it -->
<div class="loadfail" id="loadfail" hidden role="status">Couldn't reach the agent — it may be waking up. Retrying every 15 seconds.</div>

<!-- Background work the panel has started and not yet seen finish. Actions that
     think (a script, a report, a provider test) answer instantly and then run for
     up to a minute; without this the click was the last thing you ever heard. -->
<div class="working" id="working" hidden role="status" aria-live="polite"></div>

<!-- ============ DESK ============ -->
<section class="tab on" id="t-desk" role="tabpanel" aria-labelledby="tab-desk">
  <div class="kpis" id="kpis"></div>

  <div class="sec"><h2>Growth</h2><span class="note" id="chartnote"></span></div>
  <div class="charts">
    <div class="chart">
      <h3>Subscribers</h3>
      <div class="plot" id="plot-subs"></div>
      <!-- lineChart() decides this: hidden until there are two points to tabulate,
           which also keeps it shut before the first load arrives -->
      <details class="tbl" hidden><summary>Show the numbers</summary>
        <div class="tblwrap"><table id="tbl-subs"><thead><tr><th>Date</th><th class="num">Subscribers</th></tr></thead><tbody></tbody></table></div>
      </details>
    </div>
    <div class="chart">
      <h3>Views</h3>
      <div class="plot" id="plot-views"></div>
      <details class="tbl" hidden><summary>Show the numbers</summary>
        <div class="tblwrap"><table id="tbl-views"><thead><tr><th>Date</th><th class="num">Views</th></tr></thead><tbody></tbody></table></div>
      </details>
    </div>
  </div>

  <div class="duo">
    <div class="card">
      <h3 style="font:600 17px/1.2 var(--serif)">Hook virality</h3>
      <div class="note" style="font-size:12.5px;color:var(--muted);margin-bottom:6px">Scores from the script gate, recent first.</div>
      <div id="hooks"></div>
    </div>
    <div class="card">
      <h3 style="font:600 17px/1.2 var(--serif)">Auto-approve trust</h3>
      <div class="note" style="font-size:12.5px;color:var(--muted);margin-bottom:2px" id="trustnote"></div>
      <div class="meter" id="meter" role="img" aria-label=""></div>
      <div class="actions">
        <button class="btn btn-sm" data-act="toggle_auto" id="btn-auto">Toggle auto-approve</button>
        <button class="btn btn-sm" data-act="reset" data-confirm="Reset trust to zero and switch auto-approve off?">Reset counter</button>
      </div>
    </div>
  </div>

  <div class="sec"><h2>Render queue</h2><span class="note" id="queuenote"></span></div>
  <div class="card" style="padding:4px 18px">
    <div class="tblwrap"><table class="rtable" aria-label="Render queue">
      <thead><tr><th>Script</th><th>Status</th><th class="num">Age</th><th>Job</th><th><span class="visually-hidden">Actions</span></th></tr></thead>
      <tbody id="jobs"></tbody>
    </table></div>
  </div>

  <!-- The renderer lives in GitHub Actions, not in this app: it wakes on a cron
       and on a nudge. When a queue sits still, the question is always whether the
       runner ever came — so what it last did, and what the agent's own schedule
       has already run today, belong on the front page. -->
  <div class="sec"><h2>Operations</h2><span class="note">The renderer runs elsewhere; this is what it and the schedule last did.</span></div>
  <div class="card">
    <div class="rowline"><span style="flex:0 0 150px;color:var(--muted);font-size:13.5px">Renderer</span><span class="grow" id="op-worker"></span></div>
    <div class="rowline"><span style="flex:0 0 150px;color:var(--muted);font-size:13.5px">Ran today</span><span class="grow"><span class="tags" id="op-sched"></span></span></div>
    <div class="rowline"><span style="flex:0 0 150px;color:var(--muted);font-size:13.5px">Publishing</span><span class="grow" id="op-hour"></span></div>
    <div class="actions" style="margin-top:12px">
      <button class="btn btn-sm" data-act="wake">Wake the renderer</button>
      <button class="btn btn-sm" data-act="refresh_channel">Refresh channel data</button>
    </div>
  </div>
</section>

<!-- ============ FILMS ============ -->
<section class="tab" id="t-films" role="tabpanel" aria-labelledby="tab-films">
  <div class="sec"><h2>Films</h2><span class="note">Every upload, public and private.</span></div>
  <div class="sortbar">
    <label for="sortk">Sort by</label>
    <select id="sortk">
      <option value="views">Views</option>
      <option value="likes">Likes</option>
      <option value="comments">Comments</option>
      <option value="published">Published</option>
      <option value="title">Title</option>
      <option value="privacy">Status</option>
    </select>
    <button class="btn btn-sm" id="sortdir" aria-pressed="false">Reverse order</button>
  </div>
  <div class="card" style="padding:4px 18px">
    <div class="tblwrap"><table class="rtable" aria-label="Channel videos">
      <thead><tr>
        <th class="sortable" data-k="title" scope="col"><button type="button">Title</button></th>
        <th class="sortable" data-k="privacy" scope="col"><button type="button">Status</button></th>
        <th class="sortable num" data-k="views" scope="col"><button type="button">Views</button></th>
        <th class="sortable num" data-k="likes" scope="col"><button type="button">Likes</button></th>
        <th class="sortable num" data-k="comments" scope="col"><button type="button">Comments</button></th>
        <th class="sortable" data-k="published" scope="col"><button type="button">Published</button></th>
        <th scope="col"><span class="visually-hidden">Actions</span></th>
      </tr></thead>
      <tbody id="vids"></tbody>
    </table></div>
  </div>
</section>

<!-- ============ DECISIONS ============ -->
<section class="tab" id="t-dec" role="tabpanel" aria-labelledby="tab-dec">
  <div class="sec"><h2>Decisions</h2><span class="note">Finished renders, private on YouTube until you call them.</span></div>
  <div id="decisions"></div>

  <!-- Comment replies and title renames were approvable only from Telegram. The
       panel can do everything the phone can now; the headings hide themselves
       when there is nothing in either queue. -->
  <div class="sec" id="sec-replies" hidden><h2>Comment replies</h2><span class="note">Drafted by the agent, posted only on your word.</span></div>
  <div id="replies"></div>

  <div class="sec" id="sec-titles" hidden><h2>Title changes</h2><span class="note">Renames proposed for films already published.</span></div>
  <div id="titles"></div>
</section>

<!-- ============ STUDIO ============ -->
<section class="tab" id="t-studio" role="tabpanel" aria-labelledby="tab-studio">
  <div class="sec"><h2>Commission</h2><span class="note">Grounded research, virality gate, cloud render. The preview lands here and in Telegram.</span></div>
  <div class="card">
    <label for="idea" style="display:block;font-size:13.5px;color:var(--muted);margin-bottom:8px">Topic or angle</label>
    <textarea id="idea" maxlength="300" placeholder="For example: the 1989 memo that created Area 51"></textarea>
    <div class="actions" style="margin-top:12px">
      <button class="btn btn-primary" data-act="idea" data-from="idea" data-key="topic" data-clear="1">Write the script</button>
    </div>
  </div>

  <div class="sec"><h2>Ask the manager</h2><span class="note">The same brain that plans the channel, with its numbers in front of it.</span></div>
  <div class="card">
    <label for="ask" style="display:block;font-size:13.5px;color:var(--muted);margin-bottom:8px">Question</label>
    <textarea id="ask" maxlength="600" placeholder="For example: which of the last four films deserves a sequel?"></textarea>
    <div class="actions" style="margin-top:12px">
      <button class="btn" data-act="chat" data-from="ask" data-key="text" data-clear="1">Ask</button>
    </div>
    <div class="help">The answer arrives under Answers below, and in Telegram.</div>
  </div>

  <div class="sec"><h2>Actions</h2><span class="note">Destructive and bulk ones ask twice.</span></div>
  <div class="card">
    <div class="agroup">
      <div class="alabel">Production</div>
      <div class="actions">
        <button class="btn btn-primary" data-act="next">Queue a new video</button>
        <!-- one click away from making every waiting film public at once, and an audience
             sees it the moment it lands: bulk gets the same two steps as clearing -->
        <button class="btn" data-confirm="Publish every waiting film now?" data-act="publish">Publish everything waiting</button>
        <button class="btn" data-act="refresh_channel">Refresh channel data</button>
      </div>
    </div>
    <div class="agroup">
      <div class="alabel">Automatic work</div>
      <div class="actions">
        <button class="btn" id="btn-pauseresume" data-act="pause">Pause auto work</button>
        <button class="btn" data-act="retry">Retry failed jobs</button>
        <button class="btn" data-act="wake">Wake the renderer</button>
      </div>
    </div>
    <!-- these five used to exist only as Telegram commands, so the panel could show
         the channel but never ask the agent anything about it -->
    <div class="agroup">
      <div class="alabel">Thinking — each answer lands under Answers below</div>
      <div class="actions">
        <button class="btn" data-act="report">Today's report</button>
        <button class="btn" data-act="plan">Re-plan topics</button>
        <button class="btn" data-act="titlecheck">Check published titles</button>
        <button class="btn" data-act="comments">Read new comments</button>
        <button class="btn" data-act="diag">Test AI providers</button>
      </div>
    </div>
    <div class="agroup danger">
      <div class="alabel">Clearing</div>
      <div class="actions">
        <button class="btn btn-danger" data-confirm="Clear the failed jobs from the queue?" data-act="clear_failed">Clear failed jobs</button>
        <button class="btn btn-danger" data-confirm="Wipe the queue and drop pending approvals?" data-act="clear_all">Clear everything</button>
      </div>
      <div class="note" style="font-size:12.5px;color:var(--muted);margin-top:8px">Neither one deletes a video from YouTube; uploads waiting on you stay private.</div>
    </div>
  </div>

  <div class="sec"><h2>Answers</h2><span class="note" id="ansnote"></span></div>
  <div id="out"></div>

  <div class="sec"><h2>Settings</h2><span class="note">Saved to the agent the moment you press Save.</span></div>
  <div class="card">
    <div class="field">
      <label for="set-hour">Publish hour (Dhaka)</label>
      <input type="number" id="set-hour" min="0" max="23" step="1" inputmode="numeric">
      <button class="btn btn-sm" data-act="set_hour" data-num="set-hour">Save</button>
      <span class="hint" id="hint-hour"></span>
    </div>
    <div class="field">
      <label for="set-after">Auto-approve after</label>
      <input type="number" id="set-after" min="1" max="500" step="1" inputmode="numeric">
      <button class="btn btn-sm" data-act="set_after" data-num="set-after">Save</button>
      <span class="hint">Manual approvals before the agent starts publishing on its own.</span>
    </div>
  </div>

  <div class="sec"><h2>Topic guidance</h2><span class="note">What the growth analysis is steering toward — and yours to overrule.</span></div>
  <div class="card">
    <textarea id="direction" maxlength="1200" style="min-height:160px" aria-describedby="dirhelp"></textarea>
    <div class="actions" style="margin-top:12px">
      <button class="btn" data-act="direction" data-from="direction" data-key="text">Save guidance</button>
      <button class="btn btn-sm" data-local="revertdir" id="dir-revert" hidden>Discard my edits</button>
    </div>
    <div class="help" id="dirhelp">Every new script is written against this text. Re-planning rewrites it.</div>
  </div>

  <div class="sec"><h2>Used topics</h2><span class="note">Already filmed; kept off future scripts. Remove one to allow it again.</span></div>
  <div class="tags" id="topics"></div>
</section>

<!-- ============ LEDGER ============ -->
<section class="tab" id="t-ledger" role="tabpanel" aria-labelledby="tab-ledger">
  <div class="sec"><h2>Ledger</h2><span class="note" id="lednote">The agent's activity, Dhaka time.</span></div>
  <div class="lbar" role="group" aria-label="Filter the ledger">
    <button class="chipbtn" data-f="all" aria-pressed="true">Everything</button>
    <button class="chipbtn" data-f="agent" aria-pressed="false">Agent</button>
    <button class="chipbtn" data-f="panel" aria-pressed="false">My clicks</button>
    <button class="chipbtn" data-f="render" aria-pressed="false">Renders</button>
    <button class="chipbtn" data-f="film" aria-pressed="false">Films</button>
    <button class="chipbtn" data-f="score" aria-pressed="false">Scores</button>
    <button class="chipbtn" data-f="fail" aria-pressed="false">Trouble</button>
  </div>
  <div class="lbar">
    <button class="btn btn-sm" data-local="copylog">Copy the ledger</button>
    <button class="btn btn-sm btn-danger" data-local="clearjournal" data-confirm="Clear the journal this browser keeps?">Clear this browser's journal</button>
  </div>
  <div class="log big" id="log" tabindex="0" aria-label="Activity log"></div>
  <!-- three sources with three different lifetimes, and saying which is which is the
       difference between a log and a guess: the agent's live stream dies with every
       free-tier restart, the saved copy survives in the gist, and this browser writes
       down what it saw itself — including the live lines, so they outlive the restart. -->
  <div class="help" id="ledhelp"></div>
</section>

</div>
<div id="toast" role="status" aria-live="polite"></div>

<style>.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}</style>
<script>
let DATA=null, PAUSED=false, LAST={};
const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
/* the render pipeline supplies these urls; only http(s) may become a live href, and
   an empty one is not a link at all rather than an href="" that reloads the panel */
const safeUrl = u => /^https?:\/\//i.test(String(u||"")) ? String(u) : "";
/* A four-column header above a one-sentence empty state is a broken-looking table: the
   sentence is the only cell sizing anything, so the names slide out from over their
   columns. With no rows there is nothing for them to name, so they go. */
function headIf(tbodyId){
  const tb = $(tbodyId), t = tb.closest("table");
  if (t) t.classList.toggle("nohead", !!tb.querySelector(".empty"));
}
const fmt = n => (n||0).toLocaleString("en-US");
/* The live agent reported "worker 512m ago" over job ages of 535m — past a couple of
   hours a minute count stops being a quantity anyone can feel. Under two hours it is
   left alone, so the heartbeat's 10m and 70m bands still read as the minutes they are. */
function dur(m){
  const n = Math.max(0, Math.round(Number(m) || 0));
  if (n < 120) return n + "m";
  if (n < 2880) { const h = Math.floor(n/60), r = n%60; return h + "h" + (r ? " " + r + "m" : ""); }
  const dd = Math.floor(n/1440), hh = Math.floor((n%1440)/60);
  return dd + "d" + (hh ? " " + hh + "h" : "");
}
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
/* the API sends 2026-09-02; a table column reads better as Sep 2 */
const shortDate = s => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s||""));
  return m ? MON[+m[2]-1] + " " + (+m[3]) : String(s||"");
};

/* ---- Dhaka time -------------------------------------------------------------
   Dhaka is the channel's clock: everything the agent writes is already in it.
   The panel can be read from anywhere, so a browser-side moment is converted
   rather than assumed. en-CA gives YYYY-MM-DD, which sorts as plain text.
   The formatter is built on first use and cached on the function itself — the
   mock harness calls this before the script's own consts exist. */
function dhakaStamp(d){
  const f = dhakaStamp.f || (dhakaStamp.f = new Intl.DateTimeFormat("en-CA", {
    timeZone:"Asia/Dhaka", year:"numeric", month:"2-digit", day:"2-digit",
    hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false}));
  const p = {};
  f.formatToParts(d || new Date()).forEach(x => { p[x.type] = x.value });
  if (p.hour === "24") p.hour = "00";   /* some engines still say 24 for midnight */
  return p.year+"-"+p.month+"-"+p.day+" "+p.hour+":"+p.minute+":"+p.second;
}
const hmsSec = s => {
  const m = /^(\d\d):(\d\d):(\d\d)/.exec(String(s||""));
  return m ? (+m[1])*3600 + (+m[2])*60 + (+m[3]) : -1;
};
/* comms.LOG carries HH:MM:SS and no date. It holds the last 30 lines, so just
   after midnight those lines are yesterday's — a time later than now cannot
   have happened today. */
function stampFor(hms){
  const now = dhakaStamp();
  if (hmsSec(hms) > hmsSec(now.slice(11)) + 300)
    return dhakaStamp(new Date(Date.now() - 86400000)).slice(0,10) + " " + hms;
  return now.slice(0,10) + " " + hms;
}
function dayLabel(ymd){
  if (!ymd) return "No timestamp";
  if (ymd === dhakaStamp().slice(0,10)) return "Today";
  if (ymd === dhakaStamp(new Date(Date.now() - 86400000)).slice(0,10)) return "Yesterday";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd);
  return m ? MON[+m[2]-1] + " " + (+m[3]) + ", " + m[1] : ymd;
};

/* ---- tabs (hash-routed, so every view is a shareable link) ---- */
const TABS = [...document.querySelectorAll("nav [role=tab]")];
function showTab(t, push){
  const b = TABS.find(x => x.dataset.t === t) || TABS[0];
  TABS.forEach(x => x.setAttribute("aria-selected", String(x === b)));
  document.querySelectorAll("section.tab").forEach(s => s.classList.toggle("on", s.id === "t-"+b.dataset.t));
  if (push && location.hash.slice(1) !== b.dataset.t) history.replaceState(null, "", "#"+b.dataset.t);
  /* a chart measured while its tab was hidden has no width — remeasure now */
  if (b.dataset.t === "desk") drawCharts();
  /* the Ledger and the Studio mailbox are the only two views that need the heavy
     fields, so they are fetched when one of them opens rather than every 15s.
     Nothing to do before the first load answers — this runs during boot too. */
  if (DATA && (b.dataset.t === "ledger" || b.dataset.t === "studio")) {
    if (!DATA.log_tail) load();
    else if (b.dataset.t === "ledger") paintLedger();
  }
}
TABS.forEach(b => b.addEventListener("click", () => showTab(b.dataset.t, true)));
addEventListener("hashchange", () => showTab(location.hash.slice(1), false));
showTab(location.hash.slice(1), false);

/* ---- toast ---- */
let toastTimer;
function toast(msg, err, sticky){
  const t = $("toast");
  t.textContent = msg; t.className = err ? "err show" : "show";
  clearTimeout(toastTimer);
  /* something that went wrong is worth reading twice; something the reader has to
     act on waits to be dismissed rather than vanishing after three seconds */
  if (!sticky) toastTimer = setTimeout(() => t.classList.remove("show"), err ? 6000 : 3000);
}
$("toast").addEventListener("click", () => $("toast").classList.remove("show"));

/* ---- action buttons ---------------------------------------------------------
   Every button on every tab goes through act(). It answers three questions the
   old version left the reader guessing at: did the click register (the button
   goes busy), did the agent accept it (the toast), and is it still happening
   (the in-flight strip). Actions the agent runs on a background thread answer
   in milliseconds with {started:true} and finish up to a minute later. */
const NICE = {next:"New video queued", idea:"Script queued", chat:"Asked the manager",
  publish:"Published", reject:"Deleted from YouTube", vpub:"Video is public",
  vpriv:"Video is private", retitle:"Title updated", retry:"Jobs requeued",
  pause:"Paused", resume:"Resumed", clear_failed:"Failed jobs cleared",
  clear_all:"Everything cleared", reset:"Counter reset",
  toggle_auto:"Auto-approve switched", refresh_channel:"Channel data refreshed",
  wake:"Renderer woken", direction:"Guidance saved", set_hour:"Publish hour saved",
  set_after:"Threshold saved", killjob:"Job cancelled", forget:"Topic freed up",
  clear_out:"Answers cleared", reply:"Reply posting", skipreply:"Reply dropped",
  title:"Renaming", keeptitle:"Title kept", report:"Report on its way",
  plan:"Planning", titlecheck:"Checking titles", comments:"Reading comments",
  diag:"Testing the providers"};
const BUSY = {next:"Queueing…", idea:"Writing…", chat:"Asking…", publish:"Publishing…",
  reject:"Deleting…", retry:"Requeueing…", pause:"Pausing…", resume:"Resuming…",
  clear_failed:"Clearing…", clear_all:"Clearing…", clear_out:"Clearing…",
  reset:"Resetting…", toggle_auto:"Switching…", refresh_channel:"Refreshing…",
  wake:"Waking…", direction:"Saving…", report:"Writing…", plan:"Planning…",
  titlecheck:"Checking…", comments:"Reading…", diag:"Testing…"};
const HEAD = a => String(a).split(":")[0];

function busyOn(btn, label){
  if (!btn) return "";
  const was = btn.dataset.label || btn.textContent;
  btn.dataset.busy = "1"; btn.disabled = true; btn.classList.add("busy");
  btn.dataset.was = btn.innerHTML;
  btn.innerHTML = '<span class="spin"></span>' + esc(label);
  return was;
}
function busyOff(btn){
  if (!btn || !btn.dataset.busy) return;
  delete btn.dataset.busy; btn.disabled = false; btn.classList.remove("busy");
  if (btn.dataset.was != null) { btn.innerHTML = btn.dataset.was; delete btn.dataset.was; }
}

async function act(a, btn, extra){
  const head = HEAD(a);
  if (btn && btn.dataset.busy) return false;            /* no double-fire */
  const label = busyOn(btn, BUSY[head] || "Working…") || head;
  jlog("panel", "I clicked: " + label);
  let ctl = null, timer = null;
  try {
    ctl = ("AbortController" in window) ? new AbortController() : null;
    /* every action answers in milliseconds now, so half a minute of silence means
       the dyno is cold or the proxy dropped us — saying so beats a button that
       spins for ever */
    if (ctl) timer = setTimeout(() => ctl.abort(), 30000);
    const r = await fetch("/api/action", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.assign({action: a}, extra || {})),
      signal: ctl ? ctl.signal : undefined});
    clearTimeout(timer);
    let d = {};
    try { d = await r.json(); }
    catch(e) { d = {ok:false, error:"the agent answered with something that isn't JSON"}; }
    if (d.ok === false || !r.ok) {
      const why = d.error || ("the agent said " + r.status);
      toast(why, true);
      jlog("panel", "refused — " + label + ": " + why);
      load(); return false;
    }
    const said = d.msg || NICE[head] || "Done";
    toast(said);
    await load();                       /* so the strip below knows what "before" was */
    if (d.started) { startedWork(said); [3000, 10000, 30000, 60000].forEach(ms => setTimeout(load, ms)); }
    else jlog("panel", "done — " + said);
    return true;
  } catch(e) {
    clearTimeout(timer);
    const gone = e && e.name === "AbortError";
    toast(gone ? "No answer in 30 seconds. It may still be happening — watch the Ledger."
               : "Network error — the agent didn't answer.", true, gone);
    jlog("panel", (gone ? "no answer in 30s — " : "network error — ") + label);
    return false;
  } finally {
    /* the two-state buttons (pause/resume, auto-approve) carry a label render()
       owns; restoring the pre-click text would leave it describing the old state,
       so re-render once the button is free again */
    busyOff(btn);
    if (DATA) render();
  }
}

/* What a button carries with it: nothing, the text of a field (data-from +
   data-key), or a number appended to the action (data-num → set_hour:17). */
function payload(b){
  const a = b.dataset.act;
  if (b.dataset.from) {
    const el = $(b.dataset.from), v = String(el.value || "").trim();
    if (!v) return {ok:false, err:"Type something in first", focus:b.dataset.from};
    const extra = {}; extra[b.dataset.key || "text"] = v;
    return {ok:true, action:a, extra:extra, clear: b.dataset.clear ? b.dataset.from : ""};
  }
  if (b.dataset.num) {
    const el = $(b.dataset.num), n = parseInt(el.value, 10);
    const lo = Number(el.min), hi = Number(el.max);
    if (!isFinite(n) || n < lo || n > hi)
      return {ok:false, err:"Enter a whole number from " + lo + " to " + hi, focus:b.dataset.num};
    return {ok:true, action: a + ":" + n, extra:null};
  }
  return {ok:true, action:a, extra:null};
}

/* Buttons that render() generates carry their action in data-act and are handled
   here. An id interpolated into an inline onclick would be HTML-decoded and then
   parsed as code, and one listener outlives every re-render of these containers.
   data-local is the same plumbing for the few controls the browser answers by
   itself, so arming and confirming work identically for both. */
document.addEventListener("click", e => {
  const b = e.target.closest("[data-act],[data-local]");
  if (!b || b.disabled) return;
  if (b.dataset.local) {
    armOr(b, () => { const f = LOCAL[b.dataset.local]; if (f) f(b); });
    return;
  }
  const got = payload(b);
  if (!got.ok) { toast(got.err, true); const f = $(got.focus); if (f) f.focus(); return; }
  armOr(b, async () => {
    const ok = await act(got.action, b, got.extra);
    if (!ok) return;
    if (got.clear) $(got.clear).value = "";
    if (b.dataset.from) { DIRTY[b.dataset.from] = false; syncDirty(); }
  });
});

/* two-step inline confirm for destructive actions */
function armOr(btn, run){
  if (!btn.dataset.confirm) { run(); return; }
  /* the Clearing buttons are static markup — render() never rewrites them, so the
     armed state has to be undone here or the label stays "Confirm: …" for good */
  if (btn.dataset.armed) { disarm(btn); run(); return; }
  const msg = btn.dataset.confirm;
  btn.dataset.armed = "1"; btn.classList.add("confirm"); btn.dataset.label = btn.textContent;
  btn.textContent = "Confirm: " + msg.replace(/\?$/, "");
  setTimeout(() => { if (btn.dataset.armed) disarm(btn); }, 4000);
}
function disarm(btn){
  delete btn.dataset.armed; btn.classList.remove("confirm");
  if (btn.dataset.label) btn.textContent = btn.dataset.label;
}

/* ---- work in flight ---------------------------------------------------------
   A backgrounded action returns at once and then runs for up to a minute. The
   panel keeps its own list of what it started so the reader is never left with a
   click as the last thing that happened. An entry clears when the agent writes a
   new log line or drops an answer in the mailbox — its own proof of life — and
   after three minutes it says nothing came back instead of spinning for ever. */
const WORK = [];
const logTip = () => { const l = (DATA && DATA.log) || []; return l.length ? String(l[l.length-1]) : ""; };
const outTip = () => (DATA && DATA.out && DATA.out.t) ? String(DATA.out.t) : "";
function startedWork(label){
  WORK.push({label:label, t0:Date.now(), tip:logTip(), out:outTip()});
  drawWorking();
}
function pruneWork(){
  const tip = logTip(), out = outTip();
  for (let i = WORK.length - 1; i >= 0; i--) {
    const w = WORK[i], age = Date.now() - w.t0;
    const moved = tip !== w.tip || out !== w.out;
    if (moved) { WORK.splice(i, 1); continue; }
    if (age > 180000) {
      WORK.splice(i, 1);
      jlog("panel", "nothing came back from: " + w.label);
      toast("No word back on “" + w.label + "”. Check the Ledger.", true);
    }
  }
}
function drawWorking(){
  const box = $("working");
  box.hidden = !WORK.length;
  box.innerHTML = WORK.map(w =>
    '<div class="wrow"><span class="spin"></span><span>' + esc(w.label) +
    '</span><span class="el">' + Math.round((Date.now() - w.t0)/1000) + 's</span></div>').join("");
}
setInterval(() => { if (WORK.length) { pruneWork(); drawWorking(); } }, 1000);

/* ---- the journal this browser keeps ------------------------------------------
   comms.LOG lives in the dyno's memory and dies with every free-tier restart; the
   gist copy survives but only carries what the agent wrote about itself. Neither
   records what the panel witnessed: a click, a refusal, a dropped connection, a
   film going public, the agent's clock jumping backwards. So the panel writes
   that down where it is standing, and copies the agent's volatile lines while
   they still exist. localStorage is per-browser and per-origin, which is the
   honest scope for it — the Ledger labels these rows as seen here. */
const JKEY = "footnote.journal.v1", JCAP = 500;
let JOURNAL = [];
try { JOURNAL = JSON.parse(localStorage.getItem(JKEY) || "[]") || []; } catch(e) { JOURNAL = []; }
if (!Array.isArray(JOURNAL)) JOURNAL = [];
let JSEEN = new Set(JOURNAL.map(e => e.t + "|" + e.m));
let jsaveTimer;
function jlogAt(stamp, kind, msg){
  if (!msg) return;
  const e = {t: stamp, k: kind, m: String(msg).slice(0, 300)};
  const key = e.t + "|" + e.m;
  if (JSEEN.has(key)) return;           /* same second, same words: one event */
  JSEEN.add(key);
  if (JSEEN.size > 4000) JSEEN = new Set(JOURNAL.map(x => x.t + "|" + x.m));
  JOURNAL.push(e);
  if (JOURNAL.length > JCAP) JOURNAL.splice(0, JOURNAL.length - JCAP);
  clearTimeout(jsaveTimer); jsaveTimer = setTimeout(jsave, 400);
  if (ledgerOn()) paintLedger();
}
const jlog = (kind, msg) => jlogAt(dhakaStamp(), kind, msg);
function jsave(){
  try { localStorage.setItem(JKEY, JSON.stringify(JOURNAL)); }
  catch(e) {
    /* a full quota or a locked-down private window: the journal is a convenience
       and never a dependency, so drop the oldest half and try once more */
    try { JOURNAL.splice(0, Math.ceil(JOURNAL.length/2));
          localStorage.setItem(JKEY, JSON.stringify(JOURNAL)); } catch(e2) {}
  }
}

/* ---- data cycle ---- */
let FAILS = 0;
const ledgerOn = () => !!($("t-ledger") && $("t-ledger").classList.contains("on"));
async function load(){
  /* log_tail and outs are the two heavy fields; they are only asked for on the
     tabs that show them */
  const full = ledgerOn() || ($("t-studio") && $("t-studio").classList.contains("on"));
  try {
    const r = await fetch("/api/state" + (full ? "?full=1" : ""));
    if (!r.ok) throw new Error(r.status);
    const fresh = await r.json();
    /* carry the heavy fields across a light poll so switching tabs never blanks
       the Ledger */
    if (DATA) {
      if (!fresh.log_tail && DATA.log_tail) fresh.log_tail = DATA.log_tail;
      if (!fresh.outs && DATA.outs) fresh.outs = DATA.outs;
    }
    const prev = DATA;
    DATA = fresh;
    $("loadfail").hidden = true;
    if (FAILS) { jlog("agent", "the agent answered again after " + FAILS + " missed poll" + (FAILS > 1 ? "s" : "")); FAILS = 0; }
    watch(prev, fresh);
    render();
    if (!COUNTED) { COUNTED = true; countUp(); }
  } catch(e) {
    FAILS++;
    if (FAILS === 1) jlog("agent", "couldn't reach the agent — " + (e && e.message ? e.message : "no answer"));
    /* a cold start has nothing to keep; two misses in a row means what is on
       screen is stale and the panel has to admit it rather than look alive */
    if (!DATA || FAILS >= 2) {
      const b = $("loadfail");
      b.textContent = DATA
        ? "Can't reach the agent (" + FAILS + " tries). Everything below is the last thing it said."
        : "Can't reach the agent. It may be waking up — this page retries every 15 seconds.";
      b.hidden = false;
    }
  }
}

/* ---- what changed since the last poll --------------------------------------
   The agent only logs what it decides to log, and only while it is running. Every
   difference between two polls is evidence of something happening, so the panel
   turns the diff into journal lines: this is where "all the logging possible"
   actually comes from. */
const STWORD = s => ({pending:"waiting", claimed:"rendering", done:"done", failed:"failed"}[s] || String(s||"?"));
const trim = (s, n) => { s = String(s == null ? "" : s); return s.length > (n||60) ? s.slice(0, (n||60)-1) + "…" : s; };
function watch(prev, d){
  /* copy the agent's own stream into the journal: these lines die with the next
     restart, and they keep their own time so they collapse onto the saved copy */
  ((d && d.log) || []).forEach(l => {
    const s = String(l), m = /^(\d\d:\d\d:\d\d)\s*(.*)$/.exec(s);
    if (m) jlogAt(stampFor(m[1]), kindOf(m[2]), m[2]);
    else jlog(kindOf(s), s);
  });
  if (!prev) { jlog("panel", "I opened the panel"); return; }
  if (prev.started && d.started && Number(d.started) !== Number(prev.started))
    jlog("agent", "the agent restarted — anything it had only in memory is gone");
  /* the render queue */
  const was = {};
  (prev.jobs || []).forEach(j => { was[j.id] = j.status; });
  (d.jobs || []).forEach(j => {
    const lbl = trim(j.title || j.type);
    if (!(j.id in was)) jlog("render", "queued: " + lbl);
    else if (was[j.id] !== j.status) jlog("render", STWORD(was[j.id]) + " → " + STWORD(j.status) + ": " + lbl);
  });
  (prev.jobs || []).forEach(j => {
    if (!(d.jobs || []).some(x => x.id === j.id)) jlog("render", "left the queue: " + trim(j.title || j.type));
  });
  /* decisions waiting on the owner */
  const pw = {};
  (prev.pending || []).forEach(p => { pw[p.id] = p.title; });
  (d.pending || []).forEach(p => { if (!(p.id in pw)) jlog("film", "uploaded, waiting on my decision: " + trim(p.title, 70)); });
  Object.keys(pw).forEach(id => { if (!(d.pending || []).some(p => p.id === id)) jlog("film", "decided: " + trim(pw[id], 70)); });
  /* films changing status on YouTube */
  const vw = {};
  (prev.videos || []).forEach(v => { vw[v.id] = v.privacy; });
  (d.videos || []).forEach(v => {
    if (!(v.id in vw)) jlog("film", "new on the channel: " + trim(v.title, 70));
    else if (vw[v.id] !== v.privacy) jlog("film", (v.privacy === "public" ? "went public: " : "made private: ") + trim(v.title, 70));
  });
  /* hook scoring */
  const hw = new Set((prev.hooks || []).map(h => h.id + ":" + h.score));
  (d.hooks || []).forEach(h => { if (!hw.has(h.id + ":" + h.score)) jlog("score", "hook scored " + h.score + " — " + trim(h.id, 40)); });
  /* the mailbox */
  if (d.out && d.out.t && (!prev.out || prev.out.t !== d.out.t)) jlog("agent", "an answer arrived: " + (d.out.kind || "note"));
  /* work waiting on the owner elsewhere */
  const rw = new Set((prev.replies || []).map(r => r.id));
  (d.replies || []).forEach(r => { if (!rw.has(r.id)) jlog("agent", "a reply is drafted and waiting: " + trim(r.comment, 50)); });
  const tw = new Set((prev.titles || []).map(t => t.id));
  (d.titles || []).forEach(t => { if (!tw.has(t.id)) jlog("agent", "a better title is proposed: " + trim(t.title, 60)); });
  /* settings, whoever changed them */
  const a = prev.settings || {}, b = d.settings || {};
  if (a.auto_approve !== b.auto_approve) jlog("panel", "auto-approve is now " + (b.auto_approve ? "on" : "off"));
  if (a.paused !== b.paused) jlog("panel", b.paused ? "automatic work paused" : "automatic work resumed");
  if (a.hour !== b.hour && b.hour != null) jlog("panel", "publish hour → " + pad2(b.hour) + ":00");
  if (a.after !== b.after && b.after != null) jlog("panel", "auto-approve threshold → " + b.after);
  if (a.approved !== b.approved && b.approved != null) jlog("panel", "approvals counted: " + b.approved);
  if ((prev.direction || "") !== (d.direction || "")) jlog("panel", "topic guidance changed");
  /* the statistics day rolling over */
  const ph = prev.history || [], nh = d.history || [];
  if (ph.length && nh.length && nh[nh.length-1].date !== ph[ph.length-1].date)
    jlog("agent", "a new day of statistics opened: " + nh[nh.length-1].date);
  /* the worker checking in */
  if ((prev.worker || {}).last_seen !== (d.worker || {}).last_seen && (d.worker || {}).last_seen)
    jlog("render", "the renderer checked in" + ((d.worker || {}).last_job ? " — " + trim(d.worker.last_job) : ""));
}
const pad2 = n => String(n).padStart(2, "0");
/* A tile that moved gets the one-second wash. With a 15-second poll the whole
   difference between "nothing happened" and "a subscriber arrived" is one digit
   in one of six tiles, which is easy to sit right in front of and miss. The
   tiles are rebuilt from scratch each render, so the class always re-triggers
   and never needs clearing. Nothing flashes on the first paint — arriving is
   not changing, and countUp() is already doing that job. */
function flashChanged(){
  const first = !Object.keys(LAST).length;
  document.querySelectorAll("#kpis .tile").forEach(t => {
    const k = t.dataset.key, val = t.querySelector(".value");
    const v = val ? val.textContent : "";
    if (!first && LAST[k] !== undefined && LAST[k] !== v) t.classList.add("flash");
    LAST[k] = v;
  });
}
let COUNTED = false;
/* the one load moment: KPI numbers count up on first arrival */
function countUp(){
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  document.querySelectorAll("#kpis .value").forEach(el => {
    const txt = el.textContent, m = txt.replace(/[^0-9]/g,"");
    if (!m) return;
    const target = parseInt(m, 10), t0 = performance.now(), dur = 650;
    const prefix = txt.slice(0, txt.indexOf(m)), suffix = txt.slice(txt.indexOf(m)+m.length);
    function frame(t){
      const p = Math.min(1, (t-t0)/dur), e = 1-Math.pow(1-p,3);
      el.textContent = prefix + fmt(Math.round(target*e)) + suffix;
      if (p < 1) requestAnimationFrame(frame); else el.textContent = txt;
    }
    requestAnimationFrame(frame);
  });
}

/* ---- render ---- */
function render(){
  const d = DATA;
  /* heartbeat */
  const led = $("beatled"), m = d.worker.mins_ago;
  let cls = "bad", txt = "worker never seen";
  if (m !== "" && m != null) {
    const mins = Number(m);
    /* the bands stay in minutes (10, 70) because that is how they are reasoned about;
       only the reading is rolled up, and dur() leaves anything under two hours alone */
    if (mins < 10) cls = "ok";
    else if (mins < 70) cls = "warn";
    txt = "worker " + dur(mins) + " ago";
  }
  led.className = "led " + cls;
  $("beattxt").textContent = txt;
  $("star").classList.toggle("live", cls === "ok");
  $("uptime").textContent = "up " + String(d.uptime || "").replace(/^0h\s*/, "");
  /* only the client flag belongs here: the 15s interval consults PAUSED and never the
     server's auto-work pause, so folding settings.paused in made this claim refreshing
     had stopped while it carried on. The old static aria-label also overrode the visible
     text, hiding the state — the name now says what the next click will do. */
  $("refresh").textContent = (PAUSED ? "auto-refresh off" : "auto-refresh 15s") + ", updated " + new Date().toLocaleTimeString("en-US",{hour12:false});
  $("refresh").setAttribute("aria-label", PAUSED ? "Resume auto-refresh" : "Pause auto-refresh");
  $("refresh").classList.toggle("off", PAUSED);
  $("pausedflag").hidden = !d.settings.paused;

  /* nav badge — everything that is actually waiting on the owner, not just films:
     a drafted reply and a proposed title are decisions too */
  const held = (d.queue.awaiting || 0) + ((d.replies || []).length) + ((d.titles || []).length);
  const nb = $("navdec");
  nb.textContent = held;
  nb.classList.toggle("zero", !held);

  /* work the panel started and has not seen finish */
  pruneWork(); drawWorking();

  /* KPI tiles */
  const tiles = [];
  tiles.push(tile("Subscribers", fmt(d.channel.subs), deltaSub(d.delta.subs), sparkHTML(d.history.map(x=>x.subs))));
  tiles.push(tile("Views", fmt(d.channel.views), deltaSub(d.delta.views), sparkHTML(d.history.map(x=>x.views))));
  /* NOT channel.videos: that is YouTube's videoCount, which counts public uploads only,
     so the live channel showed "6" over a subtitle reading "6 public, 4 private". The
     uploads list is what the Films tab enumerates, so the tile counts the same thing. */
  tiles.push(tile("Films", fmt(d.channel.public + d.channel.private), null, null, d.channel.public + " public, " + d.channel.private + " private"));
  tiles.push(tile("Queue", d.queue.pending, null, null, d.queue.claimed + " rendering, " + d.queue.failed + " failed"));
  tiles.push(tile("Decisions", d.queue.awaiting, null, null, d.queue.awaiting ? "waiting on you" : "nothing waiting"));
  tiles.push(tile("Auto-approve", d.settings.auto_approve ? "on" : "off", null, null,
    "trust " + d.settings.approved + " of 10" + (d.settings.paused ? ", paused" : "")));
  $("kpis").innerHTML = tiles.join("");
  flashChanged();

  /* charts */
  $("chartnote").textContent = d.history.length + (d.history.length === 1 ? " day of history" : " days of history");
  drawCharts();
  fillTable("tbl-subs", d.history.map(h=>[h.date,fmt(h.subs)]));
  fillTable("tbl-views", d.history.map(h=>[h.date,fmt(h.views)]));

  /* hooks — the bar carries the ramp, the number stays ink */
  $("hooks").innerHTML = d.hooks.slice(-10).reverse().map(h => {
    const col = hookColor(h.score);
    return '<div class="rowline"><span class="sc">'+h.score+'</span>'+
      '<span class="scorebar" role="img" aria-label="score '+h.score+' of 100"><i style="width:'+h.score+'%;background:'+col+'"></i></span>'+
      '<span class="grow" style="font-size:14px;color:var(--muted)">'+esc(h.reason||h.id)+'</span></div>';
  }).join("") || '<div class="empty">No scores yet — they arrive with each script.</div>';

  /* trust meter */
  const n = Math.min(10, Math.max(0, d.settings.approved));
  $("meter").innerHTML = Array.from({length:10},(_,i)=>"<i"+(i<n?' class="f"':"")+"></i>").join("");
  $("meter").setAttribute("aria-label", d.settings.approved + " of 10 approvals");
  $("trustnote").textContent = d.settings.auto_approve
    ? "On. New videos publish themselves after render."
    : "10 manual approvals turn it on. Currently " + d.settings.approved + ".";
  $("btn-auto").textContent = d.settings.auto_approve ? "Turn auto-approve off" : "Turn auto-approve on";

  /* one button for a two-state thing: it names what it will do next. This wrote
     data-a for a listener that reads data-act, so the button labelled "Resume auto
     work" was still sending pause — resuming was impossible from the panel. */
  const pr = $("btn-pauseresume");
  pr.dataset.act = d.settings.paused ? "resume" : "pause";
  pr.textContent = d.settings.paused ? "Resume auto work" : "Pause auto work";

  /* operations — the renderer runs in someone else's cloud, so its last words are
     the only evidence it exists */
  const w = d.worker || {};
  $("op-worker").innerHTML = (w.mins_ago === "" || w.mins_ago == null)
    ? '<span style="color:var(--muted)">never checked in — nothing has rendered yet</span>'
    : esc(dur(w.mins_ago) + " ago") + (w.last_job ? ' · <span style="color:var(--muted)">' + esc(trim(w.last_job, 70)) + '</span>' : '');
  const SCHED = {comments:"comments", daily_report:"daily report", planning:"planning",
                 title_check:"title check", weekly_review:"weekly review"};
  $("op-sched").innerHTML = (d.sched || []).length
    ? (d.sched || []).map(s => '<span class="tag">' + esc(SCHED[s] || s) + '</span>').join("")
    : '<span style="color:var(--muted);font-size:13.5px">nothing yet today</span>';
  const hr = d.settings.hour;
  $("op-hour").innerHTML = esc(pad2(hr == null ? 17 : hr) + ":00 Dhaka") +
    (d.best_hour != null && d.best_hour !== "" && Number(d.best_hour) !== Number(hr)
      ? ' · <span style="color:var(--muted)">the numbers prefer ' + esc(pad2(d.best_hour)) + ':00</span>'
      : ' · <span style="color:var(--muted)">matches the best hour so far</span>');

  /* jobs */
  $("queuenote").textContent = d.queue.pending + " waiting, " + d.queue.claimed + " rendering, " + d.queue.failed + " failed";
  const STMAP = {pending:["wait","waiting"],claimed:["render","rendering"],done:["done","done"],failed:["fail","failed"]};
  $("jobs").innerHTML = d.jobs.slice(-12).reverse().map(j => {
    const st = STMAP[j.status] || ["off","unknown"];
    /* .cl labels are hidden until the table reflows into blocks on phones */
    return '<tr><td class="ttl lead">'+esc(j.title||j.type)+'</td>'+
      '<td><span class="cl">Status</span><span class="st st-'+st[0]+'"><span class="dot"></span>'+st[1]+'</span></td>'+
      '<td class="num"><span class="cl">Age</span>'+dur(j.age)+'</td>'+
      '<td class="mono"><span class="cl">Job</span>'+esc(String(j.id).slice(0,8))+'</td>'+
      /* a job already claimed by the renderer cannot be called back, so it is not
         offered — the agent would refuse it anyway */
      '<td class="act">'+(j.status === "pending" || j.status === "failed"
        ? '<button class="linkbtn" data-act="killjob:'+esc(j.id)+'" data-confirm="Drop this job?">Cancel</button>'
        : '')+'</td></tr>';
  }).join("") || '<tr><td colspan="5" class="empty">Queue is empty. Commission a video from the Studio tab.</td></tr>';
  headIf("jobs");

  /* films */
  document.querySelectorAll("#t-films th.sortable").forEach(th => {
    const k = th.dataset.k;
    let dir = null;
    if (VID.k === k) dir = VID.asc ? "ascending" : "descending";
    th.setAttribute("aria-sort", dir || "none");
    th.firstElementChild.innerHTML = esc(THLABEL[k]) + (dir ? ' <span class="arr">' + (VID.asc ? "▲" : "▼") + '</span>' : "");
  });
  $("sortk").value = VID.k;
  $("sortdir").setAttribute("aria-pressed", String(VID.asc));
  /* sort on every render, so the order always matches what the controls claim */
  $("vids").innerHTML = d.videos.slice().sort(cmpVid).map(v =>
    '<tr><td class="lead"><a class="ttl" href="https://youtu.be/'+esc(v.id)+'" target="_blank" rel="noopener">'+esc(v.title)+'</a></td>'+
    '<td><span class="cl">Status</span><span class="chip '+(v.privacy==="public"?"pub":"priv")+'"><span class="dot"></span>'+esc(v.privacy)+'</span></td>'+
    '<td class="num"><span class="cl">Views</span><b>'+fmt(v.views)+'</b></td>'+
    '<td class="num"><span class="cl">Likes</span>'+fmt(v.likes)+'</td>'+
    '<td class="num"><span class="cl">Comments</span>'+fmt(v.comments)+'</td>'+
    '<td style="color:var(--muted)"><span class="cl">Published</span>'+esc(shortDate(v.published))+'</td>'+
    '<td class="act">'+(v.privacy !== "public"
      ? '<button class="linkbtn" data-act="vpub:'+esc(v.id)+'">Make public</button>'
      : '<button class="linkbtn" data-act="vpriv:'+esc(v.id)+'">Make private</button>')+'</td></tr>'
  ).join("") || '<tr><td colspan="7" class="empty">No films yet — the first one appears here once a render finishes.</td></tr>';
  headIf("vids");

  /* decisions */
  $("decisions").innerHTML = d.pending.map(p =>
    '<div class="card">'+
    '<div class="rowline" style="border:0;padding:0 0 6px">'+
      (safeUrl(p.url)
        ? '<a class="ttl" style="font-size:16px" href="'+esc(safeUrl(p.url))+'" target="_blank" rel="noopener">'+esc(p.title)+'</a>'
        : '<span class="ttl" style="font-size:16px">'+esc(p.title)+'</span>')+
      '<span class="grow"></span>'+
      '<span class="chip">'+p.formats+(p.formats === 1 ? ' format' : ' formats')+'</span></div>'+
    '<div class="actions">'+
      '<button class="btn btn-primary" data-act="publish:'+esc(p.id)+'">Publish</button>'+
      '<button class="btn btn-danger" data-confirm="Delete from YouTube permanently?" data-act="reject:'+esc(p.id)+'">Delete</button>'+
    '</div>'+
    (p.alts && p.alts.length ? '<div style="margin-top:12px">'+
      '<div style="font-size:13.5px;color:var(--muted);margin-bottom:2px">Alternative titles</div>'+
      p.alts.map((a,i) => '<div class="rowline" style="padding:8px 0"><span class="grow">'+esc(a)+'</span>'+
        '<button class="btn btn-sm" data-act="retitle:'+esc(p.id)+':'+i+'">Use this title</button></div>').join("")+
    '</div>' : '')+
    '</div>'
  ).join("") || '<div class="card empty" style="border-style:dashed">Nothing waiting. Finished renders land here for your call, and in Telegram.</div>';

  /* drafted comment replies — until now these could only be answered from Telegram */
  const reps = d.replies || [];
  $("sec-replies").hidden = !reps.length;
  $("replies").innerHTML = reps.map(r =>
    '<div class="card">'+
    '<div style="font-size:13.5px;color:var(--muted);margin-bottom:4px">Someone wrote'+
      (r.video ? ' under <span style="color:var(--ink)">'+esc(trim(r.video, 60))+'</span>' : '')+'</div>'+
    '<div style="font-size:15px;margin-bottom:10px">“'+esc(r.comment || "")+'”</div>'+
    '<div style="font-size:13.5px;color:var(--muted);margin-bottom:4px">The agent would answer</div>'+
    '<div style="font-size:15px;border-left:3px solid var(--rule);padding-left:10px;margin-bottom:12px">'+esc(r.draft || "")+'</div>'+
    '<div class="actions">'+
      '<button class="btn btn-primary" data-act="reply:'+esc(r.id)+'">Post this reply</button>'+
      '<button class="btn" data-act="skipreply:'+esc(r.id)+'">Don’t answer</button>'+
    '</div></div>').join("");

  /* proposed renames for films already public */
  const tts = d.titles || [];
  $("sec-titles").hidden = !tts.length;
  $("titles").innerHTML = tts.map(t =>
    '<div class="card">'+
    '<div class="rowline" style="border:0;padding:0 0 6px">'+
      (t.vid ? '<a class="ttl" href="https://youtu.be/'+esc(t.vid)+'" target="_blank" rel="noopener">'+esc(t.current || "")+'</a>'
             : '<span class="ttl">'+esc(t.current || "")+'</span>')+
      '<span class="grow"></span><span class="chip">now</span></div>'+
    '<div class="rowline" style="border:0;padding:0 0 10px"><span class="ttl" style="color:var(--blue)">'+esc(t.title || "")+'</span>'+
      '<span class="grow"></span><span class="chip">proposed</span></div>'+
    '<div class="actions">'+
      '<button class="btn btn-primary" data-act="title:'+esc(t.id)+'">Use the new title</button>'+
      '<button class="btn" data-act="keeptitle:'+esc(t.id)+'">Keep the old one</button>'+
    '</div></div>').join("");

  /* studio — the guidance is editable now, so it must not be overwritten under a
     hand that is typing into it */
  const dir = $("direction");
  if (!DIRTY.direction && document.activeElement !== dir) dir.value = d.direction || "";
  dir.placeholder = "No growth analysis yet. It starts writing itself after about three public films — or say what you want covered and the agent follows it.";
  syncDirty();
  $("topics").innerHTML = d.used_topics.length
    ? d.used_topics.map(t => '<span class="chip">'+esc(t)+
        '<button class="x" data-act="forget:'+esc(t)+'" title="Allow this topic again" aria-label="Allow '+esc(t)+' again">×</button></span>').join("")
    : '<span class="note" style="color:var(--muted);font-size:13px">None yet</span>';

  /* the answers mailbox: /report, /plan, /diag and the manager's replies all land
     in one place, newest first, instead of only in Telegram */
  const outs = (d.outs || []).slice().reverse();
  const one = d.out && d.out.text ? d.out : null;
  const seenAns = [];
  if (one && !outs.some(o => o.t === one.t && o.kind === one.kind)) seenAns.push(one);
  outs.forEach(o => seenAns.push(o));
  $("ansnote").textContent = seenAns.length
    ? (seenAns.length === 1 ? "One answer waiting." : seenAns.length + " answers, newest first.")
    : "Nothing yet. Ask a question above, or press one of the thinking buttons.";
  $("out").innerHTML = seenAns.length
    ? seenAns.map((o, i) => '<div class="ans">'+
        '<div class="meta"><span class="kind">'+esc(o.kind || "note")+'</span>'+
        '<span class="when">'+esc(o.t || "")+'</span>'+
        (i === 0 ? '<span class="grow"></span><button class="linkbtn" data-act="clear_out">Clear</button>' : '')+
        '</div><div class="body">'+esc(o.text || "")+'</div></div>').join("")
    : '';

  /* settings — a half-typed number must survive a poll landing mid-edit */
  const hh = $("set-hour"), aa = $("set-after");
  if (!DIRTY["set-hour"] && document.activeElement !== hh) hh.value = (hr == null ? 17 : hr);
  if (!DIRTY["set-after"] && document.activeElement !== aa) aa.value = (d.settings.after == null ? 10 : d.settings.after);
  $("hint-hour").textContent = "Saved: " + pad2(hr == null ? 17 : hr) + ":00 Dhaka." +
    (d.best_hour != null && d.best_hour !== "" ? " Best hour by the numbers so far: " + pad2(d.best_hour) + ":00." : "") +
    " Threshold saved: " + (d.settings.after == null ? 10 : d.settings.after) +
    " approval" + ((d.settings.after) === 1 ? "" : "s") + " before it trusts itself.";

  /* ledger */
  if (ledgerOn()) paintLedger();
}

/* ---- fields the reader is editing -------------------------------------------
   Three controls hold values the agent also sends. A poll landing mid-keystroke
   could replace what was being typed, so an edit raises a flag and render() keeps
   its hands off until the value is saved or discarded. */
const DIRTY = {};
["direction", "set-hour", "set-after"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("input", () => { DIRTY[id] = true; syncDirty(); });
});
function syncDirty(){ $("dir-revert").hidden = !DIRTY.direction; }

/* ---- what the browser answers by itself ---- */
const LOCAL = {
  revertdir: () => {
    DIRTY.direction = false;
    $("direction").value = (DATA && DATA.direction) || "";
    syncDirty();
    toast("Back to the agent's own words");
  },
  clearjournal: () => {
    const n = JOURNAL.length;
    JOURNAL = []; JSEEN = new Set();
    try { localStorage.removeItem(JKEY); } catch(e) {}
    jlog("panel", "I cleared this browser's journal (" + n + " entries)");
    paintLedger();
    toast("Cleared. The agent's own log is untouched.");
  },
  copylog: async () => {
    const txt = ledgerText();
    const lines = txt ? txt.split("\n").length : 0;
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("no clipboard");
      await navigator.clipboard.writeText(txt);
      toast("Copied " + lines + " lines");
    } catch(e) {
      /* the async clipboard needs a secure context; a panel opened from a file, or
         an older browser, still has execCommand */
      const ta = document.createElement("textarea");
      ta.value = txt; ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta);
      ta.select(); ta.setSelectionRange(0, txt.length);
      let done = false;
      try { done = document.execCommand("copy"); } catch(e2) {}
      ta.remove();
      toast(done ? "Copied " + lines + " lines"
                 : "Couldn't reach the clipboard — select the ledger and copy it by hand", !done);
    }
  }
};

/* ---- the ledger -------------------------------------------------------------
   Four sources, because no single one of them is complete:
     the live stream   — comms.LOG, everything the agent has said since it last
                         restarted, which on a free plan is measured in hours;
     its saved copy    — log_tail in the gist, the same lines but dated and
                         surviving restarts;
     reconstruction    — what the saved state implies happened (a job's age is a
                         timestamp, a history row is a day that closed);
     this browser      — the journal, which is the only record of what the panel
                         itself witnessed: clicks, refusals, dropped polls.
   Identical text at an identical second is one event, so a line that exists in
   two sources collapses to the earliest-known source rather than repeating. */
const KINDS = ["agent", "panel", "render", "film", "score"];
function kindOf(s){
  const t = String(s).toLowerCase();
  if (/\(panel\)|^command:|i clicked|i opened/.test(t)) return "panel";
  if (/hook|scored/.test(t)) return "score";
  if (/render|worker|runner|ffmpeg|queue|job |clip|footage|voice|script|scene/.test(t)) return "render";
  if (/publish|public|private|upload|deleted|youtube|went live/.test(t)) return "film";
  return "agent";
}
const isBad = s => /fail|error|refus|couldn'?t|could not|⚠|exited|timed out|no answer|no word|didn'?t answer|missed|abort/i.test(String(s));
const KMAP = {job:"render", net:"agent", decide:"film", report:"agent", topic:"agent",
              comment:"agent", fail:"agent", note:"agent"};
const normKind = k => KINDS.indexOf(String(k)) >= 0 ? String(k) : (KMAP[k] || "agent");

/* Two records of one event. The agent logs "hook score 86 — dyatlov-reopened";
   this browser writes "hook scored 86 — dyatlov-reopened" when it sees the same
   change arrive, up to one poll later. Both are worth keeping — the browser's
   copy is what survives the next free-tier restart — but printing both is just
   noise, so a browser row is HIDDEN while an agent row close in time says the
   same thing. Nothing is dropped from the journal: when the agent's stream is
   wiped, its row goes with it and this one takes its place. */
const sigOf = s => new Set(String(s).toLowerCase().match(/[a-z]{4,}|\d+/g) || []);
const secsOf = t => {
  const m = /^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)/.exec(String(t || ""));
  return m ? Date.UTC(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]) / 1000 : NaN;
};
function dropEchoes(rows){
  const agent = rows.filter(r => r.s === "agent" && r.t).map(r => ({at: secsOf(r.t), g: sigOf(r.m)}));
  if (!agent.length) return rows;
  return rows.filter(r => {
    if (r.s !== "here" || !r.t) return true;
    const at = secsOf(r.t), g = sigOf(r.m);
    if (isNaN(at) || !g.size) return true;
    return !agent.some(a => {
      if (isNaN(a.at) || Math.abs(a.at - at) > 120) return false;
      let hit = 0;
      g.forEach(w => { if (a.g.has(w)) hit++; });
      return hit / g.size >= 0.8;
    });
  });
}

function ledgerRows(){
  const d = DATA || {}, rows = [], seen = new Set();
  const add = (t, kind, text, src) => {
    text = String(text == null ? "" : text).trim();
    if (!text) return;
    const key = String(t || "") + "|" + text;
    if (seen.has(key)) return;
    seen.add(key);
    rows.push({t: String(t || ""), k: kind, m: text, s: src, bad: isBad(text)});
  };
  (d.log_tail || []).forEach(e => add(e.t, kindOf(e.m), e.m, "agent"));
  (d.log || []).forEach(l => {
    const m = /^(\d\d:\d\d:\d\d)\s*(.*)$/.exec(String(l));
    if (m) add(stampFor(m[1]), kindOf(m[2]), m[2], "agent");
    else add(dhakaStamp(), kindOf(l), l, "agent");
  });
  JOURNAL.forEach(e => add(e.t, normKind(e.k), e.m, "here"));
  derived(d, add);
  rows.sort((a, b) => a.t < b.t ? 1 : a.t > b.t ? -1 : 0);
  return dropEchoes(rows);
}

/* Reconstructed from the saved state, for a panel opened after a restart wiped
   the stream. Labelled as such: worked out, not witnessed. */
function derived(d, add){
  const now = Date.now();
  (d.jobs || []).forEach(j => add(dhakaStamp(new Date(now - (Number(j.age) || 0)*60000)), "render",
    "job " + String(j.id).slice(0,8) + " created — " + trim(j.title || j.type, 70) + " (" + STWORD(j.status) + ")", "state"));
  const h = d.history || [];
  /* Only days that have actually ended. Stamping the current day 23:59 put a row
     reading "day closed on <today>" hours into the future, at the very top of the
     Ledger — and today has not closed. Today's movement is the delta on the Desk
     and the "as it stands" row below. */
  const today = dhakaStamp().slice(0, 10);
  for (let i = 1; i < h.length; i++) {
    if (String(h[i].date) >= today) continue;
    const ds = (h[i].subs || 0) - (h[i-1].subs || 0), dv = (h[i].views || 0) - (h[i-1].views || 0);
    add(h[i].date + " 23:59:00", "agent", "day closed on " + h[i].date + ": " +
      (ds >= 0 ? "+" : "") + ds + " subscriber" + (Math.abs(ds) === 1 ? "" : "s") + ", " +
      (dv >= 0 ? "+" : "") + fmt(dv) + " views", "state");
  }
  (d.videos || []).forEach(v => { if (v.published)
    add(String(v.published).slice(0,10) + " 00:00:00", "film",
      "on the channel: " + trim(v.title, 70) + " — " + v.privacy + ", " + fmt(v.views) + " views", "state"); });
  const lw = d.worker && d.worker.last_seen ? String(d.worker.last_seen) : "";
  if (lw) {
    /* the worker stamps UTC without saying so */
    const iso = lw.replace(" ", "T") + (/[zZ]|[+\-]\d\d:?\d\d$/.test(lw) ? "" : "Z");
    const at = new Date(iso);
    if (!isNaN(at.getTime())) add(dhakaStamp(at), "render",
      "the renderer last checked in" + (d.worker.last_job ? " — " + trim(d.worker.last_job, 70) : ""), "state");
  }
  if (d.started) {
    const st = new Date(Number(d.started) * 1000);
    if (!isNaN(st.getTime())) add(dhakaStamp(st), "agent", "this run of the agent started here", "state");
  }
  if ((d.sched || []).length) add(dhakaStamp().slice(0,10) + " 00:00:00", "agent",
    "scheduled work already done today: " + d.sched.join(", "), "state");
  /* no time exists for these at all, so they group at the end instead of
     pretending to one */
  (d.pending || []).forEach(p => add("", "film", "waiting on my decision: " + trim(p.title, 70), "state"));
  (d.hooks || []).slice(-10).forEach(k => add("", "score",
    "hook " + k.score + " of 100 — " + trim(k.reason || k.id, 70), "state"));
  if (d.channel) {
    const s = d.settings || {}, c = d.channel, q = d.queue || {};
    add(dhakaStamp(), "agent", "as it stands: " + fmt(c.subs) + " subscribers, " + fmt(c.views) +
      " views, " + (c.public || 0) + " public and " + (c.private || 0) + " private, " +
      (q.pending || 0) + " queued, " + (q.awaiting || 0) + " awaiting a decision, auto-approve " +
      (s.auto_approve ? "on" : "off") + ", trust " + (s.approved || 0) + " of " +
      (s.after == null ? 10 : s.after) + ", up " + (d.uptime || "?"), "state");
  }
}

let LFILTER = "all";
document.addEventListener("click", e => {
  const c = e.target.closest(".lbar [data-f]");
  if (!c) return;
  LFILTER = c.dataset.f;
  document.querySelectorAll(".lbar [data-f]").forEach(x => x.setAttribute("aria-pressed", String(x === c)));
  paintLedger();
});
const SRCLBL = {agent: "", state: "reconstructed", here: "seen by this browser"};
function lfilter(rows){
  if (LFILTER === "all") return rows;
  if (LFILTER === "fail") return rows.filter(r => r.bad);
  return rows.filter(r => r.k === LFILTER);
}
function ledgerText(){
  return lfilter(ledgerRows()).map(r =>
    (r.t || "                   ") + "  " + (r.k + "      ").slice(0,7) + "  " + r.m +
    (SRCLBL[r.s] ? "   [" + SRCLBL[r.s] + "]" : "")).join("\n");
}
function paintLedger(){
  const all = ledgerRows(), rows = lfilter(all);
  const groups = {}, order = [];
  rows.forEach(r => {
    const k = r.t.slice(0, 10);
    if (!groups[k]) { groups[k] = []; order.push(k); }
    groups[k].push(r);
  });
  /* "" (undated) sorts after every real date, which is where it belongs */
  order.sort((a, b) => a < b ? 1 : a > b ? -1 : 0);
  $("log").innerHTML = order.map(k =>
    '<div class="day">' + esc(dayLabel(k)) + ' · ' + groups[k].length +
      (groups[k].length === 1 ? ' entry' : ' entries') + '</div>' +
    groups[k].map(r =>
      '<div class="row-' + r.k + (r.bad ? ' row-fail' : '') + '">' +
      '<span class="lt">' + esc(r.t.slice(11) || "  --:--  ") + '</span> ' +
      '<span class="k">' + r.k + '</span>' +
      '<span class="b">' + esc(r.m) + '</span>' +
      (SRCLBL[r.s] ? '<span class="src">' + SRCLBL[r.s] + '</span>' : '') + '</div>').join("")
  ).join("") || '<div>Nothing under this filter yet.</div>';

  const cnt = {agent:0, state:0, here:0};
  all.forEach(r => { cnt[r.s] = (cnt[r.s] || 0) + 1; });
  $("lednote").textContent = all.length + (all.length === 1 ? " entry" : " entries") +
    " — " + cnt.agent + " from the agent, " + cnt.state + " reconstructed, " +
    cnt.here + " seen here" + (LFILTER === "all" ? "" : " · " + rows.length + " shown");
  const saved = DATA && DATA.log_tail ? DATA.log_tail.length : 0;
  $("ledhelp").textContent =
    "The agent's own log lives in memory and dies every time the free plan restarts it; " +
    (saved ? saved + " lines survive in the copy it saves." : "its saved copy hasn't loaded yet.") +
    " Rows marked reconstructed are worked out from that saved state, not witnessed. " +
    "Rows marked seen by this browser were recorded here and exist nowhere else — " +
    "they are what remains when the agent forgets.";
}


function tile(label, value, delta, spark, sub){
  return '<div class="tile" data-key="'+esc(label)+'"><div class="label">'+esc(label)+'</div>'+
    '<div class="value">'+value+'</div>'+
    (spark || '') +
    (delta || '') + (sub ? '<div class="sub">'+esc(sub)+'</div>' : '') + '</div>';
}
function deltaSub(n){
  if (n == null) return '';
  const cls = n > 0 ? "up" : n < 0 ? "down" : "flat";
  const sign = n > 0 ? "+" : "";
  return '<div class="delta '+cls+'">'+sign+fmt(n)+' today</div>';
}

/* ---- sparklines (stat-tile spec: de-emphasis line, accent current point) ---- */
function sparkHTML(arr){
  /* nothing to trace with one day, and an all-zero series gets no line here either,
     so the tile and its chart tell the same story */
  if (!arr || arr.length < 2 || Math.max(...arr) === 0) return '';
  const w=96,h=26,pad=3;
  const min=Math.min(...arr),max=Math.max(...arr),rg=(max-min)||1;
  /* a flat series has nothing to slope: sat on the floor of the box it reads as a
     stray rule, so run it through the middle instead */
  const flat = max === min;
  const pts=arr.map((v,i)=>[pad+i/(arr.length-1)*(w-2*pad), flat ? h/2 : h-pad-(v-min)/rg*(h-2*pad)]);
  const d=pts.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1)).join("");
  const last=pts[pts.length-1];
  return '<svg class="spark" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" aria-hidden="true">'+
    '<path d="'+d+'" fill="none" stroke="#8a8574" stroke-width="1.5" stroke-linecap="round"/>'+
    '<circle cx="'+last[0].toFixed(1)+'" cy="'+last[1].toFixed(1)+'" r="3" fill="#b21818" stroke="#fffdf6" stroke-width="1.5"/></svg>';
}

/* ---- hook bars: one red ramp, light -> dark with score ---- */
function hookColor(s){
  const t=Math.max(0,Math.min(100,s))/100;
  const a=[236,199,199],b=[143,20,20]; /* #ecc7c7 -> #8f1414 */
  const c=a.map((x,i)=>Math.round(x+(b[i]-x)*t));
  return "rgb("+c.join(",")+")";
}

/* ---- line chart: single series, crosshair + tooltip ---- */
function drawCharts(){
  if (!DATA) return;
  lineChart("plot-subs", "subs", "Subscribers", DATA.history);
  lineChart("plot-views", "views", "Views", DATA.history);
}
function lineChart(elId, key, label, history){
  const host = $(elId);
  const arr = history.map(h => ({x:h.date, y:h[key]})).filter(p => p.y != null);
  /* with one point there is no line to draw and nothing for the table to say */
  const det = host.parentElement.querySelector("details.tbl");
  if (det) det.hidden = arr.length < 2;
  if (arr.length < 2) { host.innerHTML = '<div class="empty">Not enough history yet — the chart draws after two daily reports.</div>'; return; }

  const W=Math.max(280, host.clientWidth || 560), H=190, PL=8, PR=58, PT=12, PB=24;
  const min=Math.min(...arr.map(p=>p.y)), max=Math.max(...arr.map(p=>p.y));
  /* a series that is all zeros would draw a line along its own baseline under 180px
     of void: a sentence carries the same fact and does not look broken */
  if (min === 0 && max === 0) {
    host.innerHTML = '<div class="empty">No '+esc(label.toLowerCase())+' yet — the line starts on the first day that registers.</div>';
    return;
  }
  /* a flat series (same count two days running) has no span to pad — give it a
     window of its own, or every y would divide by zero and the path go NaN */
  const span = max - min;
  let lo = span ? min - span*0.08 : min - Math.max(2, Math.abs(min)*0.1);
  let hi = span ? max + span*0.08 : max + Math.max(2, Math.abs(max)*0.1);
  if (min >= 0 && lo < 0) lo = 0;   /* counts are never negative */
  const X = i => PL + i/(arr.length-1)*(W-PL-PR);
  const Y = v => PT + (1-(v-lo)/(hi-lo))*(H-PT-PB);
  const pts = arr.map((p,i)=>[X(i),Y(p.y)]);

  /* clean y ticks, then drop any that would sit on the end-value label */
  const ticks = niceTicks(lo, hi, 4, 1);
  const dPath = pts.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1)).join(" ");
  const area = dPath + "L"+pts[pts.length-1][0].toFixed(1)+" "+(H-PB)+"L"+pts[0][0].toFixed(1)+" "+(H-PB)+'Z';
  const last=pts[pts.length-1], first=pts[0];
  const endLbl = fmt(arr[arr.length-1].y);
  const shown = ticks.filter(v => Math.abs(Y(v) - last[1]) > 18);

  host.innerHTML =
   '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" role="img" aria-label="'+label+' line chart, latest '+endLbl+'">'+
    ticks.map(v=>'<line class="grid" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+Y(v).toFixed(1)+'" y2="'+Y(v).toFixed(1)+'"/>').join("")+
    '<path class="area" d="'+area+'"/>'+
    '<path class="line" d="'+dPath+'"/>'+
    shown.map(v=>'<text class="axis" x="'+(W-PR+9)+'" y="'+(Y(v)+4).toFixed(1)+'" text-anchor="start">'+compactTick(v)+'</text>').join("")+
    '<text class="axis" x="'+PL+'" y="'+(H-6)+'">'+esc(shortDate(arr[0].x))+'</text>'+
    '<text class="axis" x="'+(W-PR-4)+'" y="'+(H-6)+'" text-anchor="end">'+esc(shortDate(arr[arr.length-1].x))+'</text>'+
    '<line class="vline" id="'+elId+'-v" y1="'+PT+'" y2="'+(H-PB)+'"/>'+
    '<circle class="enddot" cx="'+last[0].toFixed(1)+'" cy="'+last[1].toFixed(1)+'" r="4"/>'+
    '<text class="axis" x="'+(W-PR+9)+'" y="'+(last[1]+4).toFixed(1)+'" style="font-weight:600;fill:#14141e">'+endLbl+'</text>'+
    '<rect class="hitrect" x="0" y="0" width="'+W+'" height="'+H+'" id="'+elId+'-hit"/>'+
   '</svg><div class="tip" id="'+elId+'-tip"></div>';

  /* crosshair */
  const svg = host.querySelector("svg"), tip = host.querySelector(".tip"), vl = host.querySelector(".vline");
  function readout(frac, snap){
    const i = Math.max(0, Math.min(arr.length-1, Math.round(frac*(arr.length-1))));
    const p = arr[i];
    tip.style.display="block";
    tip.innerHTML = '<span class="tv">'+fmt(p.y)+'</span> <span class="td">'+esc(p.x)+'</span>';
    const px = frac*(svg.getBoundingClientRect().width);
    tip.style.left = Math.min(Math.max(px+12,8), svg.getBoundingClientRect().width-110)+"px";
    tip.style.top = "6px";
    if (snap) {
      vl.setAttribute("x1",X(i)); vl.setAttribute("x2",X(i)); vl.style.opacity=1;
      tip.style.left = Math.min(Math.max(X(i)/W*svg.getBoundingClientRect().width+12,8), svg.getBoundingClientRect().width-110)+"px";
    }
  }
  svg.addEventListener("pointermove", e => {
    const r = svg.getBoundingClientRect();
    readout((e.clientX-r.left)/r.width, true);
  });
  svg.addEventListener("pointerleave", () => { tip.style.display="none"; vl.style.opacity=0; });
  /* keyboard: arrow through days */
  svg.tabIndex = 0;
  svg.setAttribute("aria-label", label+" chart. Use arrow keys to read daily values.");
  let ki = arr.length-1;
  svg.addEventListener("keydown", e => {
    if (e.key==="ArrowLeft"||e.key==="ArrowRight"){
      e.preventDefault();
      ki = Math.max(0, Math.min(arr.length-1, ki + (e.key==="ArrowRight"?1:-1)));
      readout(ki/(arr.length-1), true);
    }
  });
}
function niceTicks(lo, hi, target, minStep){
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / target;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / pow;
  let step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * pow;
  /* subscribers and views are whole numbers: a 0.5 step would label 0, 0, 1, 1 */
  if (minStep && step < minStep) step = minStep;
  const out = [];
  for (let v = Math.ceil(lo/step)*step; v <= hi + step*1e-9; v += step) out.push(v);
  return out;
}
function compactTick(v){
  if (v>=1e6) return (v/1e6).toFixed(v%1e6?1:0)+"M";
  if (v>=1e4) return Math.round(v/1e3)+"k";
  return fmt(Math.round(v));
}
function fillTable(id, rows){
  $(id).querySelector("tbody").innerHTML = rows.map(r =>
    "<tr><td>"+esc(r[0])+"</td><td class=\"num\">"+esc(r[1])+"</td></tr>").join("");
}

/* ---- films sorting ---- */
const THLABEL = {title:"Title",privacy:"Status",views:"Views",likes:"Likes",comments:"Comments",published:"Published"};
let VID = {k:"views", asc:false};
function cmpVid(a,b){
  const x = a[VID.k], y = b[VID.k];
  return (typeof x === "number" ? x-y : String(x).localeCompare(String(y))) * (VID.asc?1:-1);
}
/* headers on wide screens, a select on phones — both drive the same sort */
document.querySelector("#t-films thead").addEventListener("click", e => {
  const th = e.target.closest("th.sortable");
  if (!th) return;
  if (VID.k === th.dataset.k) VID.asc = !VID.asc; else { VID.k = th.dataset.k; VID.asc = false; }
  render();
});
$("sortk").addEventListener("change", e => { VID.k = e.target.value; VID.asc = false; render(); });
$("sortdir").addEventListener("click", () => { VID.asc = !VID.asc; render(); });

/* ---- clock (Dhaka) + auto-refresh ---- */
function tick(){
  $("clock").textContent = "Dhaka " + new Intl.DateTimeFormat("en-GB",{hour:"2-digit",minute:"2-digit",timeZone:"Asia/Dhaka"}).format(new Date());
}
$("refresh").addEventListener("click", () => { PAUSED = !PAUSED; render(); if(!PAUSED) load(); });
setInterval(tick, 30000); tick();
load();
/* a refresh rewrites the tables under the cursor. Hold it while a destructive
   button is armed — its 4s window overlaps this 15s one — and while the keyboard
   is inside a region render() replaces, which would drop focus to the body. */
function refreshHeld(){
  if (document.querySelector("[data-armed]")) return true;
  const a = document.activeElement;
  if (!a || !a.closest) return false;
  /* #decisions is what the old code called #pending — the id moved and this check
     silently stopped covering the cards it was written for. The interactive
     containers render() rewrites now include replies, titles, topic chips and the
     mailbox. A field being typed into does NOT hold the poll: render() leaves a
     dirty or focused field alone, so the rest of the panel can keep updating. */
  return !!a.closest("#vids,#decisions,#jobs,#hooks,#replies,#titles,#topics,#out,#kpis");
}
setInterval(() => { if (!PAUSED && !document.hidden && !refreshHeld()) load(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && !PAUSED) load(); });

/* redraw charts to the new width (debounced) */
let rsz;
window.addEventListener("resize", () => {
  clearTimeout(rsz);
  rsz = setTimeout(drawCharts, 200);
});

/* arrow keys walk the tab list */
document.querySelector("nav").addEventListener("keydown", e => {
  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
  const tabs = [...document.querySelectorAll("nav [role=tab]")];
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  const n = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  n.focus(); n.click();
});
</script>
</body>
</html>
"""


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
    # "+N today" on the Desk tiles has to actually mean today. stats_history
    # gets one row per day, written when daily_report() runs, so diffing the
    # last two rows measured YESTERDAY's growth for the whole stretch before
    # that report landed — under a subscriber count read live from YouTube,
    # so the tile contradicted itself most of the day. Today's movement is
    # the live figure minus yesterday's close, and it has to be yesterday
    # exactly: if the report missed a day the newest close is older than
    # that and the difference spans several days, so there is no honest
    # figure and the tile shows none (deltaSub() skips a missing value).
    yday_bd = f"{datetime.now() + config.BD_OFFSET - timedelta(days=1):%Y-%m-%d}"
    base = next((h for h in reversed(hist)
                 if str(h.get("date", "")) == yday_bd), None)
    delta = {}
    if base and ch:
        delta = {"subs": ch.get("subs", 0) - base.get("subs", 0),
                 "views": ch.get("views", 0) - base.get("views", 0)}
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
    replies = [{"id": uid, "draft": (r.get("draft") or "")[:400],
                "video": (r.get("video_title") or "")[:70],
                "comment": (r.get("comment") or r.get("text") or "")[:300]}
               for uid, r in state.STATE.get("pending_replies", {}).items()]
    titles = [{"id": uid, "vid": t.get("video_id", ""),
               "current": (t.get("current") or "")[:90],
               "title": (t.get("title") or "")[:90]}
              for uid, t in state.STATE.get("pending_titles", {}).items()]
    out = {
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
                     "paused": s.get("paused"),
                     "after": s.get("auto_approve_after", 10),
                     "hour": s.get("publish_hour", 17)},
        "worker": {"mins_ago": mins, "last_job": w.get("last_job") or "",
                   "last_seen": w.get("last_seen") or ""},
        "uptime": f"{int(_t.time() - STARTED_AT) // 3600}h "
                  f"{(int(_t.time() - STARTED_AT) % 3600) // 60}m",
        "started": int(STARTED_AT),
        "pending": pending_detail,
        "replies": replies,
        "titles": titles,
        "jobs": [{"id": j["id"], "type": j["type"], "status": j["status"],
                  "age": int((now - j.get("created", now)) / 60),
                  "title": (j.get("script", {}).get("title") or
                            j.get("result", {}).get("msg", ""))[:60]}
                 for j in jobs_list],
        "direction": state.STATE.get("topic_direction", ""),
        "used_topics": state.STATE.get("used_topics", [])[-25:],
        "best_hour": state.STATE.get("best_hour", 17),
        "sched": sorted(state.STATE.get("scheduler_ran", {}).keys()),
        "log": comms.LOG[-30:],
        # newest text answer a panel button asked for (diag/chat/report)
        "out": (state.STATE.get("panel_out") or [None])[-1],
    }
    # The durable log and the whole output mailbox are only worth shipping
    # when the Ledger asks for them — the 15s poll stays small.
    if request.args.get("full"):
        out["log_tail"] = state.STATE.get("log_tail", [])[-250:]
        out["outs"] = state.STATE.get("panel_out", [])
    return jsonify(out)


def _bg(name, fn):
    """Run a slow action on a daemon thread.

    Gunicorn runs ONE sync worker on the free plan, so a synchronous
    /api/action that takes 40s freezes the whole app — including the
    panel's own /api/state polls, which is exactly why clicking "queue a
    video" looked like it did nothing. Answer instantly, work in the
    background, report through the log + Telegram + the output mailbox.
    """
    threading.Thread(target=run_safely, args=(name, fn), daemon=True).start()


def _panel_out(kind, text):
    """Mailbox for text a panel button asked for (diag, chat, report).

    Those answers arrive after the HTTP response has already gone, so the
    panel picks them up from /api/state on its next poll.
    """
    if not text:
        return
    box = state.STATE.setdefault("panel_out", [])
    box.append({"t": f"{datetime.now() + config.BD_OFFSET:%Y-%m-%d %H:%M:%S}",
                "kind": kind, "text": str(text)[:4000]})
    del box[:-6]
    state.save_soon()


@app.route("/api/action", methods=["POST"])
def api_action():
    state.default_state()
    data = request.get_json(force=True, silent=True) or {}
    a = data.get("action", "")
    import cloud
    if a == "next":
        comms.log("command: queue a video (panel)")
        comms.send("Writing a script… (from panel)")
        _bg("panel: queue a video", lambda: brain.queue_next_video(1))
        return jsonify({"ok": True, "started": True,
                        "msg": "Writing a script — watch the queue."})
    if a == "idea":
        topic = (data.get("topic") or "").strip()
        if not topic:
            return jsonify({"ok": False, "error": "empty topic"}), 400

        def _make_idea():
            script = brain.generate_script(
                direction=f"Topic requested by the channel owner: {topic}")
            if not script:
                comms.log(f"idea failed: {topic[:40]}")
                comms.send("⚠️ Couldn't draft that one — every AI provider "
                           "refused. Try a different wording.", html=True)
                return
            import uuid as _uuid
            jobs.add_job("render", {"script": script,
                                    "approval_id": _uuid.uuid4().hex[:10]})
            cloud.wake_soon("render")
            state.STATE.setdefault("used_topics", []).append(
                script.get("id", "?"))
            state.save_soon()
            comms.log(f"queued custom video: {script['title'][:50]}")
            comms.send(f"🎬 <b>Custom video queued</b> — "
                       f"{comms.esc(script['title'])}", html=True)

        comms.log(f"command: idea \"{topic[:40]}\" (panel)")
        _bg("panel: custom idea", _make_idea)
        return jsonify({"ok": True, "started": True,
                        "msg": "Drafting your topic — watch the queue."})
    if a == "publish":
        # bulk publish reports per-video truth: "Published 2" is a lie if
        # YouTube refused one of them
        good, bad = _publish_pending()
        if not good and not bad:
            return jsonify({"ok": True, "msg": "Nothing was waiting."})
        return jsonify({
            "ok": bad == 0,
            "msg": f"Published {good}." if not bad else "",
            "error": (f"{bad} of {good + bad} wouldn't publish — see "
                      f"Telegram for the reason.") if bad else ""})
    if a.startswith("publish:"):
        uid = a.split(":", 1)[1]
        if uid in state.STATE["pending_videos"]:
            # through _record_decision so one-at-a-time approvals count
            # toward auto-approve exactly like a bulk publish does
            ok = _record_decision(uid, "approved")
            return jsonify({"ok": ok,
                            "error": "" if ok else "YouTube refused — check "
                                                   "Telegram for the reason"})
        return jsonify({"ok": False, "error": "not pending"})
    if a.startswith("reject:"):
        ok = _delete_pending(a.split(":", 1)[1])
        return jsonify({"ok": ok,
                        "error": "" if ok else "delete failed — see Telegram"})
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

    # ---- background thinking: answers arrive in the output mailbox -------
    if a == "diag":
        def _diag():
            import llm
            _panel_out("diag", llm.diagnose())
            comms.log("provider test finished (panel)")
        comms.log("command: test providers (panel)")
        _bg("panel: provider test", _diag)
        return jsonify({"ok": True, "started": True,
                        "msg": "Testing every AI provider…"})
    if a == "chat":
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "empty message"}), 400

        def _chat():
            ctx = ""
            try:
                ch_ = yt.channel_stats()
                ctx = (f"Channel: {ch_['title']}, {ch_['subs']} subs, "
                       f"{ch_['views']} views, {ch_['videos']} videos. ")
            except Exception:
                pass
            try:
                reply = brain.gemini(
                    f"{ctx}The owner asks: \"{text}\"\n"
                    f"Answer as their channel growth manager. Be concise "
                    f"and concrete (under 150 words).")
            except Exception as e:
                reply = f"⚠️ All AI providers failed: {str(e)[:150]}"
            _panel_out("chat", f"You: {text}\n\n{reply}")
            comms.send_md(reply)

        comms.log(f"asked: {text[:60]}")
        _bg("panel: chat", _chat)
        return jsonify({"ok": True, "started": True, "msg": "Thinking…"})
    if a == "report":
        comms.log("command: today's report (panel)")
        _bg("panel: daily report",
            lambda: _panel_out("report", brain.daily_report()
                               or "Report sent to Telegram."))
        return jsonify({"ok": True, "started": True,
                        "msg": "Writing today's report…"})
    if a == "plan":
        comms.log("command: re-plan topics (panel)")
        _bg("panel: planning",
            lambda: _panel_out("plan", brain.analyze_and_plan()
                               or "Planning done — see Topic guidance."))
        return jsonify({"ok": True, "started": True,
                        "msg": "Re-reading the channel and re-planning…"})
    if a == "titlecheck":
        comms.log("command: title check (panel)")
        _bg("panel: title check",
            lambda: _panel_out("titles", brain.title_check()
                               or "Title check done."))
        return jsonify({"ok": True, "started": True,
                        "msg": "Checking titles on published films…"})
    if a == "comments":
        comms.log("command: comment sweep (panel)")
        _bg("panel: comment sweep",
            lambda: _panel_out("comments", brain.comment_sweep()
                               or "Comment sweep done."))
        return jsonify({"ok": True, "started": True,
                        "msg": "Reading new comments…"})
    if a == "clear_out":
        state.STATE["panel_out"] = []
        state.save_soon()
        return jsonify({"ok": True})

    # ---- comment replies + title suggestions (was Telegram-only) ---------
    if a.startswith("reply:"):
        uid = a.split(":", 1)[1]
        if uid not in state.STATE.get("pending_replies", {}):
            return jsonify({"ok": False, "error": "not pending"})
        _bg("panel: post reply",
            lambda: _panel_out("reply", brain.post_reply(uid)))
        return jsonify({"ok": True, "started": True, "msg": "Posting…"})
    if a.startswith("skipreply:"):
        p = state.STATE.get("pending_replies", {}).pop(
            a.split(":", 1)[1], None)
        if p:
            state.STATE.setdefault("replied_comments", []).append(
                p.get("comment_id"))
            state.save_soon()
        return jsonify({"ok": bool(p),
                        "error": "" if p else "not pending"})
    if a.startswith("title:"):
        uid = a.split(":", 1)[1]
        if uid not in state.STATE.get("pending_titles", {}):
            return jsonify({"ok": False, "error": "not pending"})
        _bg("panel: apply title",
            lambda: _panel_out("title", brain.apply_title(uid)))
        return jsonify({"ok": True, "started": True, "msg": "Renaming…"})
    if a.startswith("keeptitle:"):
        gone = state.STATE.get("pending_titles", {}).pop(
            a.split(":", 1)[1], None)
        state.save_soon()
        return jsonify({"ok": bool(gone),
                        "error": "" if gone else "not pending"})

    # ---- settings + queue surgery ---------------------------------------
    if a.startswith("set_hour:"):
        try:
            h = int(a.split(":", 1)[1])
        except ValueError:
            return jsonify({"ok": False, "error": "not a number"}), 400
        if not 0 <= h <= 23:
            return jsonify({"ok": False, "error": "hour must be 0–23"}), 400
        state.STATE["settings"]["publish_hour"] = h
        state.save_now()
        comms.log(f"publish hour set to {h}:00 (panel)")
        return jsonify({"ok": True, "now": h})
    if a.startswith("set_after:"):
        try:
            n = int(a.split(":", 1)[1])
        except ValueError:
            return jsonify({"ok": False, "error": "not a number"}), 400
        if not 1 <= n <= 500:
            return jsonify({"ok": False, "error": "must be 1–500"}), 400
        state.STATE["settings"]["auto_approve_after"] = n
        state.save_now()
        comms.log(f"auto-approve threshold set to {n} (panel)")
        return jsonify({"ok": True, "now": n})
    if a == "direction":
        text = (data.get("text") or "").strip()[:1200]
        state.STATE["topic_direction"] = text
        state.save_now()
        comms.log("topic guidance rewritten (panel)")
        return jsonify({"ok": True})
    if a == "wake":
        cloud.wake_soon("render")
        comms.log("render worker woken by hand (panel)")
        return jsonify({"ok": True})
    if a.startswith("killjob:"):
        jid = a.split(":", 1)[1]
        state.reload_jobs()
        keep, killed = [], None
        for j in state.STATE["jobs"]:
            if killed is None and (j["id"] == jid
                                   or j["id"].startswith(jid)) \
                    and j.get("status") in ("pending", "failed"):
                killed = j
                continue
            keep.append(j)
        if not killed:
            return jsonify({"ok": False,
                            "error": "no waiting or failed job with that id"})
        state.STATE["jobs"] = keep
        state.save_now()
        comms.log(f"job {jid[:8]} cancelled (panel)")
        return jsonify({"ok": True})
    if a.startswith("forget:"):
        t = a.split(":", 1)[1]
        used = state.STATE.get("used_topics", [])
        if t not in used:
            return jsonify({"ok": False, "error": "not in the list"})
        state.STATE["used_topics"] = [u for u in used if u != t]
        state.save_now()
        comms.log(f"topic freed up again: {t[:50]}")
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
            # a ✅/❌ tapped on the preview before this report arrived
            early = state.STATE.setdefault("early_decisions", {}).pop(
                approval_id, None)
            if early:
                state.save_soon()
                _record_decision(approval_id, early)
            elif state.STATE["settings"].get("auto_approve"):
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
    if not (is_render and ok and data.get("video_url")):
        # No approval entry was registered — the job failed, or it rendered
        # but the upload didn't land. Either way a decision tapped ahead of
        # it is waiting for a video that will never arrive.
        stale = (job or {}).get("approval_id") or job_id
        if state.STATE.setdefault("early_decisions", {}).pop(stale, None):
            state.save_soon()
            comms.send("↩️ The video you decided on early never made it to "
                       "YouTube, so that decision is dropped.", html=True)
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


def _job_in_flight(approval_id):
    """Is a render that will claim this approval id still running?

    The approval id is the job's own id unless the job carried one (see
    the worker's `job.get("approval_id", job["id"])`), so match either.
    """
    for j in state.STATE.get("jobs", []):
        if (j.get("approval_id") or j.get("id")) == approval_id:
            return j.get("status") in ("pending", "claimed")
    return False


def _record_decision(approval_id, decision):
    """Owner's ✅ = flip the already-uploaded video public; ❌ = delete
    it from YouTube. No time window — the video is safely private on
    YouTube until decided, so the decision can come hours or days later.

    Returns whether the decision actually took effect, so callers (panel
    and Telegram alike) can report the truth instead of a blanket success.
    """
    p = state.STATE["pending_videos"].get(approval_id)
    if not p:
        # The worker attaches the ✅/❌ buttons to the Telegram preview and
        # only reports to /report afterwards — and sending the long-form
        # preview in between takes tens of seconds. A tap inside that window
        # used to be answered "isn't pending anymore", which was false and
        # threw the decision away. While the render is still in flight,
        # remember what was asked and let /report carry it out on arrival.
        if _job_in_flight(approval_id):
            ed = state.STATE.setdefault("early_decisions", {})
            ed[approval_id] = decision
            for old in list(ed)[:-20]:  # bounded, like every other list here
                del ed[old]
            state.save_soon()
            comms.send("⏳ Noted — the render is still finishing. I'll "
                       + ("publish" if decision == "approved" else "delete")
                       + " it the moment it lands.", html=True)
            return False
        # entry is gone: either already decided (popped on publish) or
        # the state was lost. Honest reply either way.
        comms.send("🤷 That video isn't pending anymore — it was already "
                   "published or deleted.", html=True)
        return False
    if decision == "approved":
        # publish FIRST: a YouTube call that fails must not earn trust,
        # or ten failed publishes would silently switch auto-approve on
        if not _publish_now(approval_id):
            return False
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
        state.save_soon()
        return True
    elif decision == "rejected":
        return _delete_pending(approval_id)
    else:
        comms.send("✅ Already decided — one decision per video.")
        return False


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
    videos are already private on YouTube — this just makes them live.

    Returns (published, failed) so the caller can report the truth: a
    YouTube call that refuses must not be announced as a publish. Trust
    and the auto-approve threshold go through _record_decision, so the
    bulk path counts exactly like the one-video button — and, like it,
    only counts a video that actually went public.
    """
    pending = state.STATE["pending_videos"]
    if not pending:
        comms.send("Nothing to publish — no uploaded videos awaiting "
                   "approval. Render one with /next, then tap ✅ on the "
                   "preview, or say 'publish' once it's rendered.")
        return 0, 0
    ok = bad = 0
    for uid in list(pending):
        if _record_decision(uid, "approved"):
            ok += 1
        else:
            bad += 1
    return ok, bad


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

    def top_up_queue():
        """Queue a video for the day, but only if the pipeline is empty.

        Anything already moving counts as fed: a job still pending, one a
        runner has claimed, and a finished video waiting in the approval
        mailbox. Counting only 'pending' meant a render stuck in 'claimed',
        or a stack of videos waiting for approval, still read as an empty
        queue — so the top-up piled another video on top every morning.
        """
        jl = state.STATE.get("jobs", [])
        busy = (sum(1 for j in jl if j.get("status") in ("pending", "claimed"))
                + len(state.STATE.get("pending_videos", {})))
        if busy:
            comms.log(f"queue top-up skipped — {busy} already in the pipeline")
            return
        brain.queue_next_video(1)

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
            # Through once_per_day, not a bare "is it 09:00?": the free dyno
            # restarts often, and a restart across that one minute used to
            # skip the day's video silently. Now a late boot still catches up.
            once_per_day("queue top-up", top_up_queue, 9, 0)

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
