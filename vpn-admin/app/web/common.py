"""Shared UI primitives (CSS, page shell, formatting helpers). Ported from the
original admin so the look stays identical."""
from __future__ import annotations

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; }
a { color: #60a5fa; text-decoration: none; }
a:hover { text-decoration: underline; }
.nav { background: #1e2130; padding: 12px 24px; display: flex; align-items: center; gap: 20px; border-bottom: 1px solid #2d3148; }
.nav-brand { font-weight: 700; font-size: 18px; color: #f1f5f9; letter-spacing: .5px; }
.nav a { color: #94a3b8; font-size: 14px; }
.nav a:hover { color: #e2e8f0; text-decoration: none; }
.nav-right { margin-left: auto; }
.container { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
.card { background: #1e2130; border: 1px solid #2d3148; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
.card-title { font-size: 14px; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: .8px; margin-bottom: 16px; }
.btn { display: inline-block; padding: 7px 14px; border-radius: 6px; font-size: 13px; font-weight: 500; border: none; cursor: pointer; transition: opacity .15s; }
.btn:hover { opacity: .85; text-decoration: none; }
.btn-primary { background: #3b82f6; color: #fff; }
.btn-danger  { background: #ef4444; color: #fff; }
.btn-warn    { background: #f59e0b; color: #000; }
.btn-ghost   { background: #2d3148; color: #e2e8f0; }
.btn-sm      { padding: 4px 10px; font-size: 12px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.badge-ok   { background: #14532d; color: #4ade80; }
.badge-off  { background: #7f1d1d; color: #fca5a5; }
.badge-exp  { background: #78350f; color: #fcd34d; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; padding: 10px 12px; color: #64748b; font-weight: 600; font-size: 12px; text-transform: uppercase; border-bottom: 1px solid #2d3148; }
td { padding: 12px; border-bottom: 1px solid #1a1f2e; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #252b3b; }
.progress { background: #1a1f2e; border-radius: 4px; height: 6px; overflow: hidden; }
.progress-bar { background: #3b82f6; height: 100%; border-radius: 4px; }
.progress-bar.warn { background: #f59e0b; }
.progress-bar.danger { background: #ef4444; }
.form-group { margin-bottom: 14px; }
label { display: block; font-size: 13px; color: #94a3b8; margin-bottom: 4px; }
input, select, textarea { width: 100%; padding: 9px 12px; background: #0f1117; border: 1px solid #2d3148; border-radius: 6px; color: #e2e8f0; font-size: 14px; }
input:focus, select:focus, textarea:focus { outline: none; border-color: #3b82f6; }
.alert { padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
.alert-err { background: #7f1d1d; color: #fca5a5; }
.alert-ok  { background: #14532d; color: #4ade80; }
.copy-btn { background: #2d3148; border: none; color: #94a3b8; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; }
.copy-btn:hover { color: #fff; }
.help { margin: 8px 0; background: #0f1117; border: 1px solid #2d3148; border-radius: 6px; }
.help > summary { cursor: pointer; padding: 8px 12px; color: #60a5fa; font-size: 13px; font-weight: 600; list-style: none; user-select: none; }
.help > summary::-webkit-details-marker { display: none; }
.help[open] > summary { border-bottom: 1px solid #2d3148; }
.help > ol, .help > div, .help > a { padding-left: 12px; padding-right: 12px; }
.help > a { display: inline-block; margin: 0 0 10px 12px; color: #60a5fa; font-size: 12px; }
.form-group select { width: 100%; padding: 8px 10px; background: #0f1117; border: 1px solid #2d3148; border-radius: 6px; color: #e2e8f0; }
"""

TOAST_HTML = ('<div id="toast" style="position:fixed;bottom:24px;left:50%;transform:translateX(-50%) '
              'translateY(12px);background:#16a34a;color:#fff;padding:10px 22px;border-radius:8px;'
              'font-size:14px;font-weight:500;opacity:0;transition:opacity .2s,transform .2s;'
              'pointer-events:none;z-index:9999;white-space:nowrap">Скопировано!</div>')

COPY_JS = """
function showToast(msg){
  var t=document.getElementById('toast');
  if(!t)return;
  t.textContent=msg;t.style.opacity='1';t.style.transform='translateX(-50%) translateY(0)';
  clearTimeout(t._tid);
  t._tid=setTimeout(function(){t.style.opacity='0';t.style.transform='translateX(-50%) translateY(12px)';},2000);
}
function copyText(btn,txt){
  function done(){
    showToast('Скопировано в буфер');
    var orig=btn.textContent;btn.textContent='✓';btn.style.color='#4ade80';
    setTimeout(function(){btn.textContent=orig;btn.style.color='';},1500);
  }
  function fallback(){
    var ta=document.createElement('textarea');ta.value=txt;
    Object.assign(ta.style,{position:'fixed',opacity:'0',top:'0',left:'0'});
    document.body.appendChild(ta);ta.focus();ta.select();
    try{document.execCommand('copy');done();}catch(e){}
    document.body.removeChild(ta);
  }
  if(navigator.clipboard&&window.isSecureContext){
    navigator.clipboard.writeText(txt).then(done).catch(fallback);
  }else{fallback();}
}
"""


def fmt_bytes(b: int) -> str:
    if not b:
        return "0 B"
    b = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def lt(iso: str, date_only: bool = False) -> str:
    """Render a UTC ISO timestamp as a span localized to the browser TZ by JS."""
    if not iso:
        return "—"
    cls = "lt-date" if date_only else "lt-time"
    fallback = iso[:10] if date_only else iso[:16].replace("T", " ")
    return f'<span class="{cls}" data-utc="{iso}">{fallback}</span>'


def badge(level: str, text: str) -> str:
    cls = {"crit": "badge-off", "warn": "badge-exp", "ok": "badge-ok", "info": "badge"}.get(level, "badge")
    return f'<span class="badge {cls}">{text}</span>'


def page(title: str, body: str, brand: str = "VPN", banner: str = "", nav_extra: str = "") -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {brand} Admin</title>
<style>{CSS}</style></head><body>
<nav class="nav">
  <span class="nav-brand">{brand} Admin</span>
  <a href="/users">Пользователи</a>
  <a href="/users/create">+ Создать</a>
  <a href="/health">Здоровье</a>
  <a href="/settings">Настройки</a>
  {nav_extra}
  <span class="nav-right"><a href="/logout">Выйти</a></span>
</nav>
<div class="container">{banner}{body}</div>
{TOAST_HTML}
<script>{COPY_JS}</script>
<script>
(function(){{
  document.querySelectorAll('.lt-time').forEach(function(e){{var d=new Date(e.dataset.utc); if(!isNaN(d)) e.textContent=d.toLocaleString();}});
  document.querySelectorAll('.lt-date').forEach(function(e){{var d=new Date(e.dataset.utc); if(!isNaN(d)) e.textContent=d.toLocaleDateString();}});
}})();
</script></body></html>"""
