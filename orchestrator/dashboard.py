"""Status views: a terminal table and a local auto-refreshing web dashboard (127.0.0.1 only)."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .quota import QuotaTracker


def latest_run(workdir, run_id=None):
    runs = Path(workdir) / "runs"
    if run_id:
        path = runs / run_id / "run.json"
    else:
        candidates = sorted(runs.glob("*/run.json"))
        if not candidates:
            return None
        path = candidates[-1]
    return json.loads(path.read_text()) if path.is_file() else None


def collect(workdir, agents, run_id=None):
    run = latest_run(workdir, run_id)
    dry = bool(run and run.get("dry_run"))
    tracker = QuotaTracker(Path(workdir) / ("state-dryrun.json" if dry else "state.json"))
    return {"agents": tracker.snapshot(agents), "run": run, "dry_run": dry,
            "reset_in": QuotaTracker.seconds_until_reset()}


def _fmt(used, limit):
    return f"{used}/{'∞' if limit is None else limit}"


def render_terminal(status):
    rows = status["agents"]
    out = ["AGENTS" + ("  (dry-run counters)" if status["dry_run"] else "")]
    header = f"{'agent':<16}{'requests':>12}{'tokens':>18}{'rpm':>6}{'err':>5}  state"
    out += [header, "-" * len(header)]
    for r in rows:
        rpm = "∞" if r["requests_per_minute"] is None else r["requests_per_minute"]
        out.append(f"{r['name']:<16}{_fmt(r['requests'], r['requests_per_day']):>12}"
                   f"{_fmt(r['tokens'], r['tokens_per_day']):>18}{rpm:>6}{r['errors']:>5}  {r['state']}")
    reset = status["reset_in"]
    out.append(f"daily counters reset in {reset // 3600}h{reset % 3600 // 60:02d}m")
    run = status["run"]
    if run and run.get("plan"):
        out += ["", f"RUN {run['run_id']}: {run['task']}"]
        for st in run["plan"]["subtasks"]:
            s = run["status"][st["id"]]
            agent = f" ← {s['agent']}" if s["agent"] else ""
            out.append(f"  {st['id']:<4}{s['state']:<9}[{st['capability']}] {st['title']}{agent}")
    return "\n".join(out)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Orchestrator Status</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--ok:#1a7f37;--warn:#b35900;--err:#c62828;--bar:#3b6fd8}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e8e8e8;--muted:#9aa0a6;--line:#2a2e33;--ok:#4ac26b;--warn:#f0a040;--err:#ff6b6b;--bar:#6f9cff}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;max-width:1100px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:20px 0 8px}.muted{color:var(--muted)}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.meter{background:var(--line);border-radius:3px;height:6px;width:110px;margin-top:3px}.meter>i{display:block;height:100%;background:var(--bar);border-radius:3px}
.done,.ok{color:var(--ok)}.paused,.blocked,.warn,.exhausted{color:var(--warn)}.error{color:var(--err)}
#log{font:12px ui-monospace,monospace;white-space:pre-wrap;border:1px solid var(--line);padding:8px;max-height:320px;overflow:auto}
</style></head><body>
<h1>Orchestrator Status</h1><div class="muted" id="sub">loading…</div>
<h2>Agents</h2><div class="wrap"><table id="agents"></table></div>
<h2>Subtasks</h2><div class="wrap"><table id="tasks"></table></div>
<h2>Log</h2><div id="log"></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const meter=(u,l)=>l==null?`${u} / ∞`:`${u} / ${l}<div class="meter"><i style="width:${Math.min(100,100*u/l)}%"></i></div>`;
async function tick(){
 try{const s=await (await fetch('status.json',{cache:'no-store'})).json();
 const r=s.run;document.getElementById('sub').textContent=(r?`Run ${r.run_id}: ${r.task}`:'No runs yet')+
  ` · counters reset in ${Math.floor(s.reset_in/3600)}h${String(Math.floor(s.reset_in%3600/60)).padStart(2,'0')}m`+(s.dry_run?' · dry run':'');
 document.getElementById('agents').innerHTML='<tr><th>Agent</th><th>Requests today</th><th>Tokens today</th><th>RPM</th><th>Errors</th><th>State</th></tr>'+
  s.agents.map(a=>`<tr><td>${esc(a.name)}<div class="muted">${esc(a.capabilities.join(', '))}</div></td><td>${meter(a.requests,a.requests_per_day)}</td><td>${meter(a.tokens,a.tokens_per_day)}</td><td>${a.requests_per_minute??'∞'}</td><td>${a.errors}</td><td class="${esc(a.state.split(' ')[0].replace(':',''))}">${esc(a.state)}</td></tr>`).join('');
 document.getElementById('tasks').innerHTML=r&&r.plan?'<tr><th>ID</th><th>State</th><th>Capability</th><th>Subtask</th><th>Agent</th><th>Tokens</th></tr>'+
  r.plan.subtasks.map(t=>{const x=r.status[t.id];return `<tr><td>${esc(t.id)}</td><td class="${esc(x.state)}">${esc(x.state)}</td><td>${esc(t.capability)}</td><td>${esc(t.title)}</td><td>${esc(x.agent||'')}</td><td>${x.tokens}</td></tr>`}).join(''):'';
 const log=document.getElementById('log');log.innerHTML=(r?r.events:[]).map(e=>`<span class="${esc(e.level)}">[${esc(e.t)}] ${esc(e.msg)}</span>`).join('\\n');log.scrollTop=log.scrollHeight;
 }catch(e){document.getElementById('sub').textContent='dashboard server not reachable';}
}
tick();setInterval(tick,2000);
</script></body></html>"""


def serve(workdir, agents, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/status.json"):
                body, ctype = json.dumps(collect(workdir, agents)).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
