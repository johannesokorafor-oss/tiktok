"""Local dashboard (FastAPI).

Shows watcher status, jobs, cover previews, provider + auth status and
offers retry / regenerate-cover actions.  Runs in the same process as the
watcher (`tta start`) or standalone read-only (`tta dashboard`).
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import db
from .config import Config
from .pipeline import Pipeline
from .providers import ProviderRegistry
from .tiktok_api import TokenStore

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>TikTok Auto-Poster</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;background:#101318;color:#e8eaf0;margin:0;padding:24px}
 h1{font-size:20px;margin:0 0 16px} h2{font-size:15px;margin:24px 0 8px;color:#9aa4b5}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
 .card{background:#1a1f27;border:1px solid #2a3140;border-radius:10px;padding:14px}
 .kv{display:flex;justify-content:space-between;margin:3px 0;font-size:13px}
 .kv span:first-child{color:#8b93a5}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #262d3a;vertical-align:middle}
 th{color:#8b93a5;font-weight:600}
 .state{padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600}
 .s-COMPLETED{background:#123b25;color:#4ade80}.s-UPLOADED{background:#123b25;color:#4ade80}
 .s-FAILED{background:#3b1212;color:#f87171}.s-DUPLICATE{background:#3b2f12;color:#fbbf24}
 .s-RETRY_PENDING{background:#3b2f12;color:#fbbf24}
 .s-default{background:#12293b;color:#60a5fa}
 img.cover{height:84px;border-radius:6px;border:1px solid #2a3140}
 button{background:#2563eb;border:0;color:#fff;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;margin-right:4px}
 button.warn{background:#7c3aed} button:disabled{opacity:.4}
 .ok{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}
 code{background:#0c0f14;padding:1px 5px;border-radius:4px;font-size:12px}
 .err{max-width:340px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#f87171;font-size:12px}
</style></head><body>
<h1>TikTok Auto-Poster <span id="dot"></span></h1>
<div class="grid">
 <div class="card"><h2 style="margin-top:0">Watcher</h2><div id="watcher"></div></div>
 <div class="card"><h2 style="margin-top:0">Image providers</h2><div id="providers"></div></div>
 <div class="card"><h2 style="margin-top:0">TikTok</h2><div id="tiktok"></div></div>
</div>
<h2>Jobs</h2>
<div class="card" style="overflow-x:auto"><table id="jobs"><thead>
<tr><th>Cover</th><th>File</th><th>State</th><th>Upload</th><th>Error</th><th>Actions</th></tr>
</thead><tbody></tbody></table></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function refresh(){
 try{
  const s=await (await fetch('api/status')).json();
  const j=await (await fetch('api/jobs')).json();
  document.getElementById('dot').textContent = s.watcher.running?'· watching':'· idle';
  document.getElementById('watcher').innerHTML =
   kv('Running', s.watcher.running?'<span class=ok>yes</span>':'<span class=bad>no (read-only dashboard?)</span>')+
   kv('Watched folder','<code>'+esc(s.watcher.watch_dir)+'</code>')+
   kv('Dry run', s.watcher.dry_run?'<span class=warn>yes (no uploads)</span>':'no')+
   kv('Current job', esc(s.watcher.current_job||'—'))+
   kv('Waiting for pair', esc(Object.keys(s.watcher.pending_pairs||{}).join(', ')||'—'));
  document.getElementById('providers').innerHTML = s.providers.map(p=>
   kv(p.name+' <small>['+p.cost+']</small>', (p.available?'<span class=ok>available</span>':'<span class=bad>unavailable</span>'))).join('')+
   kv('Selected','<b>'+esc(s.selected_provider)+'</b>');
  document.getElementById('tiktok').innerHTML =
   kv('Credentials', s.tiktok.configured?'<span class=ok>configured</span>':'<span class=bad>missing</span>')+
   kv('Authenticated', s.tiktok.authenticated?'<span class=ok>yes</span>':'<span class=bad>no</span>')+
   kv('Token valid', s.tiktok.access_valid?'<span class=ok>yes</span>':(s.tiktok.refresh_valid?'<span class=warn>refreshable</span>':'<span class=bad>no</span>'))+
   kv('Upload mode', esc(s.upload_mode));
  document.querySelector('#jobs tbody').innerHTML = j.jobs.map(job=>{
   const cls = ['COMPLETED','UPLOADED','FAILED','DUPLICATE','RETRY_PENDING'].includes(job.state)?'s-'+job.state:'s-default';
   const cover = job.has_cover?'<img class=cover src="covers/'+job.id+'.png?t='+Date.now()+'">':'—';
   const retry = ['FAILED','DUPLICATE','RETRY_PENDING'].includes(job.state)?
     '<button onclick="act(\\''+job.id+'\\',\\'retry\\')">Retry'+(job.state==='DUPLICATE'?' (force)':'')+'</button>':'';
   const regen = job.has_cover?'<button class=warn onclick="act(\\''+job.id+'\\',\\'regenerate-cover\\')">New cover</button>':'';
   return '<tr><td>'+cover+'</td><td><b>'+esc(job.base_name)+'</b><br><small>'+esc(job.id)+'</small></td>'+
    '<td><span class="state '+cls+'">'+esc(job.state)+'</span></td>'+
    '<td>'+esc(job.upload_status||'—')+(job.publish_id?'<br><small>'+esc(job.publish_id)+'</small>':'')+'</td>'+
    '<td class=err title="'+esc(job.error)+'">'+esc(job.error||'')+'</td><td>'+retry+regen+'</td></tr>';
  }).join('');
 }catch(e){ document.getElementById('dot').textContent='· connection lost'; }
}
function kv(k,v){return '<div class=kv><span>'+k+'</span><span>'+v+'</span></div>'}
async function act(id,action){
 const r=await fetch('api/jobs/'+id+'/'+action,{method:'POST'});
 if(!r.ok){alert((await r.json()).detail||'failed')}
 refresh();
}
refresh(); setInterval(refresh,4000);
</script></body></html>"""


def create_app(config: Config, store: db.JobStore, watcher=None,
               pipeline: Pipeline | None = None,
               registry: ProviderRegistry | None = None) -> FastAPI:
    app = FastAPI(title="TikTok Auto-Poster", docs_url=None, redoc_url=None)
    registry = registry or (pipeline.registry if pipeline else None)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _PAGE

    @app.get("/api/status")
    def status():
        token_status = TokenStore(config.token_file).status()
        providers = registry.describe() if registry else []
        selected = ""
        if registry:
            try:
                selected = registry.select().name
            except Exception as exc:  # pragma: no cover
                selected = f"error: {exc}"
        watcher_status = watcher.status() if watcher else {
            "running": False, "watch_dir": str(config.input_dir),
            "current_job": "", "pending_pairs": {}, "dry_run": config.dry_run,
        }
        return {
            "watcher": watcher_status,
            "providers": providers,
            "selected_provider": selected,
            "upload_mode": config.upload_mode,
            "tiktok": {
                "configured": config.tiktok_configured,
                **token_status,
            },
        }

    @app.get("/api/jobs")
    def jobs(limit: int = 50):
        items = []
        for job in store.list_jobs(limit=limit):
            cover = config.covers_dir / f"{job.id}.png"
            items.append({
                "id": job.id,
                "base_name": job.base_name,
                "state": job.state,
                "error": job.error,
                "retries": job.retries,
                "upload_status": job.upload_status,
                "publish_id": job.publish_id,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "has_cover": cover.is_file(),
                "output_dir": job.output_dir,
            })
        return {"jobs": items}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        payload = job.as_dict()
        payload["events"] = store.events(job_id)
        return payload

    @app.get("/covers/{job_id}.png")
    def cover(job_id: str):
        path = config.covers_dir / f"{Path(job_id).name}.png"
        if not path.is_file():
            raise HTTPException(404, "no cover for this job")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if pipeline is None:
            raise HTTPException(409, "dashboard is read-only (start with `tta start`)")
        if job.state not in (db.FAILED, db.RETRY_PENDING, db.DUPLICATE):
            raise HTTPException(409, f"job is in state {job.state}, cannot retry")
        force = job.state == db.DUPLICATE
        threading.Thread(
            target=pipeline.run_job, args=(job,), kwargs={"force": force},
            daemon=True,
        ).start()
        return JSONResponse({"ok": True, "forced": force})

    @app.post("/api/jobs/{job_id}/regenerate-cover")
    def regenerate(job_id: str):
        if pipeline is None:
            raise HTTPException(409, "dashboard is read-only (start with `tta start`)")
        try:
            cover_path = pipeline.regenerate_cover(job_id)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "cover": str(cover_path)}

    return app
