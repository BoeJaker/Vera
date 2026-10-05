"""Intelligence - the installable mobile app (a PWA over the report pages).

/intel/app is a phone-first list of briefs and reports (the newest first, a
play badge when a report has narration); a report opens in the same report
page as everywhere else. The manifest makes it installable ("Add to home
screen"), the service worker (scope /intel/) keeps the app shell and every
report opened so far readable offline - network first, the cache when there is
no network. Pure: strings only; the routes live in intel_capabilities.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

NAME = "Vera Intelligence"
SHORT = "Intel"
THEME = "#0f1115"
ACCENT = "#7aa2f7"
SW_VERSION = "intel-app-v1"

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect width="512" height="512" rx="112" fill="#0f1115"/>'
            '<circle cx="256" cy="256" r="150" fill="none" stroke="#7aa2f7" stroke-width="28"/>'
            '<circle cx="256" cy="256" r="62" fill="#7aa2f7"/>'
            '<path d="M256 70v60M256 382v60M70 256h60M382 256h60" stroke="#7aa2f7" stroke-width="28" stroke-linecap="round"/>'
            '</svg>')


def manifest() -> str:
    return json.dumps({
        "name": NAME, "short_name": SHORT, "id": "/intel/app", "start_url": "/intel/app", "scope": "/intel/",
        "display": "standalone", "orientation": "portrait", "background_color": THEME, "theme_color": THEME,
        "description": "Briefs, reports and their narration from Vera's intelligence system.",
        "icons": [{"src": "/intel/app/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                  {"src": "/intel/app/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                  {"src": "/intel/app/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
    })


# Network first, cache as the fallback: a report is always the newest copy when
# online and still readable on a train. Audio is streamed (range requests), not cached.
SERVICE_WORKER = r"""const V='%s';const SHELL=['/intel/app','/intel/app/icon.svg','/intel/manifest.webmanifest'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(V).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()));});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;const u=new URL(r.url);
if(u.origin!==location.origin||!u.pathname.startsWith('/intel/')||u.pathname.startsWith('/intel/report/audio/'))return;
const keep=u.pathname==='/intel/app'||u.pathname.startsWith('/intel/app/')||u.pathname.startsWith('/intel/report/view/');
if(!keep)return;
e.respondWith(fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(V).then(x=>x.put(r,c));}return res;})
.catch(()=>caches.match(r).then(m=>m||caches.match('/intel/app'))));});
""" % SW_VERSION

# Every page in the app's scope: installable, themed, and it registers the worker.
HEAD = ('<link rel="manifest" href="/intel/manifest.webmanifest"><meta name="theme-color" content="%s">'
        '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        '<link rel="apple-touch-icon" href="/intel/app/icon-192.png"><link rel="icon" href="/intel/app/icon.svg">' % THEME)
REGISTER = ("<script>if('serviceWorker' in navigator&&(location.protocol==='https:'||location.hostname==='localhost'))"
            "navigator.serviceWorker.register('/intel/sw.js',{scope:'/intel/'}).catch(function(){});</script>")


def list_rows(reports: List[Dict[str, Any]], limit: int = 40) -> List[Dict[str, Any]]:
    """What the app's list needs from each report - nothing more."""
    out = []
    for r in reports[:limit]:
        heads = []
        for sec in r.get("sections") or []:
            if sec.get("kind") == "headline":
                heads = [{"topic": h.get("topic"), "text": h.get("text")} for h in (sec.get("items") or [])[:3]]
        if not heads and r.get("top_line"):
            heads = [{"topic": "", "text": x} for x in r["top_line"][:3]]
        audio = r.get("audio") or {}
        out.append({"id": r.get("id"), "title": r.get("title"), "ts": r.get("ts"), "counts": r.get("counts") or {},
                    "note": r.get("note") or "", "heads": heads,
                    "audio": float(audio.get("seconds") or 0) if audio.get("url") else 0.0})
    return out


APP_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Intel</title>%(head)s<style>
:root{--bg:#0f1115;--card:#171a21;--bd:#2a2f3a;--fg:#e6e8ee;--dim:#9aa3b2;--acc:#7aa2f7}
@media (prefers-color-scheme:light){:root{--bg:#f7f7f9;--card:#fff;--bd:#dde1e8;--fg:#1d2330;--dim:#5b6475;--acc:#3b63c9}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
header{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:10px;padding:14px 16px;background:var(--bg);border-bottom:1px solid var(--bd)}
header h1{font-size:1.2rem;margin:0;flex:1}header button{background:none;border:1px solid var(--bd);color:var(--fg);border-radius:10px;padding:6px 12px;font:inherit}
main{max-width:720px;margin:0 auto;padding:12px 12px 40px}
a.card{display:block;text-decoration:none;color:inherit;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px 14px 12px;margin:0 0 10px}
a.card:active{transform:scale(.99)}.t{font-weight:600;font-size:1rem;margin:0 0 4px}.m{color:var(--dim);font-size:.8rem;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pl{color:var(--acc);font-weight:600}.h{margin:8px 0 0;padding:0;list-style:none}.h li{font-size:.9rem;margin:4px 0;padding-left:12px;border-left:3px solid var(--bd)}
.h b{color:var(--dim);font-weight:500}.first{border-color:var(--acc)}.off{color:var(--dim);text-align:center;padding:30px 0}
.note{color:var(--dim);font-size:.8rem;margin-top:6px}
</style></head><body><header><h1>Intel</h1><button id="rf" aria-label="refresh">↻</button></header>
<main id="list"><div class="off">loading…</div></main>
<script>
function e(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function when(ts){var d=new Date(ts);return isNaN(d)?'':d.toLocaleString([], {weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});}
function mins(s){s=Math.round(s||0);return Math.floor(s/60)+':'+('0'+s%%60).slice(-2);}
function load(){fetch('/intel/app/list',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
var rows=j.rows||[];document.getElementById('list').innerHTML=rows.length?rows.map(function(r,i){
return '<a class="card'+(i===0?' first':'')+'" href="/intel/report/view/'+e(r.id)+'"><div class="t">'+e(r.title)+'</div>'
+'<div class="m"><span>'+e(when(r.ts))+'</span><span>'+e((r.counts||{}).signals||0)+' signals</span>'+(r.audio?'<span class="pl">▶ '+mins(r.audio)+'</span>':'')+'</div>'
+(r.heads&&r.heads.length?'<ul class="h">'+r.heads.map(function(h){return '<li>'+(h.topic?'<b>'+e(h.topic)+'</b> — ':'')+e(h.text)+'</li>';}).join('')+'</ul>':'')
+(r.note?'<div class="note">'+e(r.note)+'</div>':'')+'</a>';}).join(''):'<div class="off">No reports yet.</div>';})
.catch(function(){document.getElementById('list').innerHTML='<div class="off">Offline - open a report you have read before.</div>';});}
document.getElementById('rf').onclick=load;load();
</script>%(register)s</body></html>"""


def app_html() -> str:
    return APP_HTML % {"head": HEAD, "register": REGISTER}
