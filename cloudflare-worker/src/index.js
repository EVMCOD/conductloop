// Cloudflare Worker: hosts the ConductLoop status page.
// - Serves /static/* from the embedded HTML
// - Proxies /api/status.json to the ops-dashboard backend (with keepalive)
// - Adds CORS + cache headers

const STATUS_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>System Status — ConductLoop</title>
  <meta name="description" content="Real-time status of all public services." />
  <meta name="theme-color" content="#0f172a" />
  <style>
    :root{--bg:#0f172a;--bg-soft:#1e293b;--bg-card:#1a2332;--border:#334155;--ink:#f1f5f9;--muted:#94a3b8;--green:#22c55e;--amber:#f59e0b;--red:#ef4444;--blue:#3b82f6}
    *{box-sizing:border-box}
    body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5}
    .container{max-width:720px;margin:0 auto;padding:32px 20px 80px}
    h1{font-size:28px;margin:0 0 8px 0;font-weight:700;letter-spacing:-0.5px}
    h1 .dot{display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--green);margin-right:8px;vertical-align:middle;box-shadow:0 0 12px var(--green)}
    .subtitle{color:var(--muted);margin:0 0 32px 0;font-size:14px}
    .summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:32px}
    .summary-card{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:14px;text-align:center}
    .summary-card .num{font-size:28px;font-weight:700;line-height:1}
    .summary-card .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-top:4px}
    .summary-card.green .num{color:var(--green)}
    .summary-card.amber .num{color:var(--amber)}
    .summary-card.red .num{color:var(--red)}
    .section-title{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:24px 0 12px 0;font-weight:600}
    .service{display:flex;align-items:center;padding:14px 16px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;margin-bottom:8px;gap:14px}
    .status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
    .status-dot.ok{background:var(--green);box-shadow:0 0 8px var(--green)}
    .status-dot.fail{background:var(--red);box-shadow:0 0 8px var(--red)}
    .service-info{flex:1;min-width:0}
    .service-name{font-weight:600;font-size:14px}
    .service-url{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .service-meta{font-size:11px;color:var(--muted);margin-top:2px}
    .service a{color:var(--blue);text-decoration:none;font-size:12px}
    .service a:hover{text-decoration:underline}
    .footer{text-align:center;margin-top:40px;color:var(--muted);font-size:12px}
    .footer a{color:var(--blue);text-decoration:none}
    .empty{text-align:center;color:var(--muted);padding:32px 20px}
    .last-update{color:var(--muted);font-size:11px;text-align:center;margin-bottom:24px}
    @media (max-width:600px){.summary{grid-template-columns:1fr}.container{padding:20px 14px 60px}h1{font-size:22px}}
  </style>
</head>
<body>
  <div class="container">
    <h1><span class="dot" id="overall-dot"></span><span id="overall-title">All systems operational</span></h1>
    <p class="subtitle">Real-time status of ConductLoop public services</p>
    <p class="last-update" id="last-update">Loading…</p>
    <div class="summary">
      <div class="summary-card green"><div class="num" id="ok-count">—</div><div class="label">operational</div></div>
      <div class="summary-card amber"><div class="num" id="warn-count">—</div><div class="label">degraded</div></div>
      <div class="summary-card red"><div class="num" id="fail-count">—</div><div class="label">down</div></div>
    </div>
    <div class="section-title">Services</div>
    <div id="services">Loading…</div>
    <div class="footer">
      <p>Status updated every 5 minutes via ops-dashboard.</p>
      <p><a href="/api/status.json">JSON</a> · <a href="__BACKEND__">Dashboard</a></p>
    </div>
  </div>
  <script>
    const dot=document.getElementById('overall-dot'),title=document.getElementById('overall-title'),
      okCount=document.getElementById('ok-count'),warnCount=document.getElementById('warn-count'),
      failCount=document.getElementById('fail-count'),services=document.getElementById('services'),
      lastUpdate=document.getElementById('last-update');
    function renderStatus(data){
      let ok=0,warn=0,fail=0;
      const rows=[];
      (data.rows||[]).forEach(function(s){
        const healthy=s.ok===1||s.ok===true;
        const status=s.status_code||0;
        const isOk=healthy&&status>=200&&status<400;
        if(isOk) ok++; else if(status>=500||status===0) fail++; else warn++;
        const checkedAt=s.checked_at?new Date(s.checked_at*1000).toLocaleString():'never';
        const statusClass=isOk?'ok':'fail';
        const statusText=isOk?'operational':(status===0?'down':'degraded');
        rows.push('<div class="service"><div class="status-dot '+statusClass+'"></div><div class="service-info"><div class="service-name">'+escapeHtml(s.name)+'</div><div class="service-url"><a href="'+escapeHtml(s.url)+'" target="_blank" rel="noopener">'+escapeHtml(s.url)+'</a></div><div class="service-meta">HTTP '+(status||'—')+' · '+(s.latency_ms!=null?s.latency_ms+'ms · ':'')+'checked '+checkedAt+'</div></div><div style="font-size:11px;color:var(--muted);text-align:right"><div>'+statusText+'</div></div></div>');
      });
      okCount.textContent=ok; warnCount.textContent=warn; failCount.textContent=fail;
      services.innerHTML=rows.length?rows.join(''):'<div class="empty">No services configured yet.</div>';
      if(fail>0){dot.style.background='var(--red)';dot.style.boxShadow='0 0 12px var(--red)';title.textContent=fail+' service'+(fail>1?'s':'')+' down'}
      else if(warn>0){dot.style.background='var(--amber)';dot.style.boxShadow='0 0 12px var(--amber)';title.textContent=warn+' service'+(warn>1?'s':'')+' degraded'}
      else{dot.style.background='var(--green)';dot.style.boxShadow='0 0 12px var(--green)';title.textContent='All systems operational'}
      if(data.checked_at){lastUpdate.textContent='Last updated: '+new Date(data.checked_at*1000).toLocaleString()}
      else{lastUpdate.textContent='Status data from latest poll'}
    }
    function escapeHtml(s){if(s==null)return'';return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
    async function poll(){
      try{const r=await fetch('/api/status.json');const data=await r.json();renderStatus(data)}
      catch(e){services.innerHTML='<div class="empty">Could not load status.</div>'}
    }
    poll();
    setInterval(poll,60000);
  </script>
</body>
</html>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const BACKEND = env.OPS_DASHBOARD_URL || 'https://your-dashboard.example.com';

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: corsHeaders()
      });
    }

    // API proxy: /api/* → backend.
    //
    // Only the public, read-only endpoints go through. This worker is on the
    // public internet: proxying everything would expose /api/brain/memory,
    // /api/notes/* and /api/sessions/* to anyone, and caching them would
    // keep copies in the CDN. It also used to drop the method and the Origin
    // header, which laundered cross-site writes past the backend's own origin
    // guard. 2026-10-23.
    if (url.pathname.startsWith('/api/')) {
      if (!PUBLIC_API_PATHS.has(url.pathname)) {
        return new Response(JSON.stringify({ error: 'not_found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' }
        });
      }
      if (request.method !== 'GET') {
        return new Response(JSON.stringify({ error: 'method_not_allowed' }), {
          status: 405,
          headers: { 'Content-Type': 'application/json' }
        });
      }
      const target = BACKEND + url.pathname + url.search;
      try {
        const resp = await fetch(target, {
          method: 'GET',
          headers: { 'Accept': 'application/json' },
          // Public data, but never cached: the backend may tighten its rules
          // and a cached copy would outlive them.
          cf: { cacheTtl: 0, cacheEverything: false }
        });
        const body = await resp.text();
        return new Response(body, {
          status: resp.status,
          headers: {
            'Content-Type': resp.headers.get('Content-Type') || 'application/json',
            'Cache-Control': 'no-store',
            'Access-Control-Allow-Origin': '*',
          }
        });
      } catch (e) {
        return new Response(JSON.stringify({ error: 'backend unreachable', detail: String(e) }), {
          status: 502,
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' }
        });
      }
    }

    // Status HTML
    if (url.pathname === '/' || url.pathname === '/status' || url.pathname === '/index.html') {
      return new Response(STATUS_HTML.replace('__BACKEND__', BACKEND), {
        status: 200,
        headers: {
          'Content-Type': 'text/html; charset=utf-8',
          'Cache-Control': 'public, max-age=60',
        }
      });
    }

    // Favicon (avoid 404 noise)
    if (url.pathname === '/favicon.ico') {
      return new Response(null, { status: 204 });
    }

    return new Response('Not found', { status: 404 });
  }
};

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}

/**
 * The only backend endpoints this public worker may proxy: read-only and
 * public by the backend's own rules (see ops_dashboard/origin_guard.py,
 * PUBLIC_CORS_PATHS). Keep in sync with it.
 */
const PUBLIC_API_PATHS = new Set([
  '/api/health',
  '/api/pricing-tier',
]);
