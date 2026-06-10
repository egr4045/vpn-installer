"""FastAPI app: the single control-plane (users, subs, health, settings).

Data layer is the SQLite store + xray/sing-box CLIs (no Remnawave). UI ported
from the original admin; routes rewired to the store.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

import bcrypt as _bcrypt
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer
from starlette.responses import Response as StarResponse

from . import (alerts, config, enforcement, health, provider, render, singbox_ctl,
               store, subs, xray_ctl)
from .web import common
from .web.common import badge, fmt_bytes, lt

cfg = config.get_settings()
signer = URLSafeTimedSerializer(cfg.secret_key)
_PASS_HASH = _bcrypt.hashpw(cfg.admin_pass.encode(), _bcrypt.gensalt())

app = FastAPI()


def verify_pass(password: str) -> bool:
    return _bcrypt.checkpw(password.encode(), _PASS_HASH)


def get_session(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        return signer.loads(token, max_age=86400 * 7)
    except Exception:
        return None


def set_session(response: Response, username: str):
    response.set_cookie("session", signer.dumps(username), httponly=True, max_age=86400 * 7)


def page(title: str, body: str, nav_extra: str = "") -> str:
    return common.page(title, body, brand=cfg.brand,
                       banner=provider.provider_banner(), nav_extra=nav_extra)


def _auth(request: Request):
    return get_session(request) is not None


# ── auth ──────────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    err = f'<div class="alert alert-err">{error}</div>' if error else ""
    body = f"""
<div style="max-width:380px;margin:80px auto"><div class="card">
  <div class="card-title" style="text-align:center;margin-bottom:24px">{cfg.brand} Admin</div>
  {err}
  <form method="post" action="/login">
    <div class="form-group"><label>Логин</label><input name="username" autocomplete="username"></div>
    <div class="form-group"><label>Пароль</label><input type="password" name="password" autocomplete="current-password"></div>
    <button class="btn btn-primary" style="width:100%">Войти</button>
  </form>
</div></div>"""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход · {cfg.brand} Admin</title><style>{common.CSS}</style></head>
<body>{body}</body></html>""")


@app.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    if username == cfg.admin_user and verify_pass(password):
        r = RedirectResponse("/users", status_code=302)
        set_session(r, username)
        return r
    return RedirectResponse("/login?error=Неверный+логин+или+пароль", status_code=302)


@app.get("/logout")
async def logout():
    r = RedirectResponse("/login", status_code=302)
    r.delete_cookie("session")
    return r


@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/users")


# ── users ─────────────────────────────────────────────────────────────────────
_BADGE = {"ACTIVE": ("badge-ok", "Активен"), "DISABLED": ("badge-off", "Выкл"),
          "LIMITED": ("badge-exp", "Лимит"), "EXPIRED": ("badge-exp", "Истёк")}


@app.get("/users", response_class=HTMLResponse)
async def users_list(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    await provider.refresh_provider()
    users = store.list_users()
    if provider.PROVIDER.get("limit_gb"):
        provider.PROVIDER["alloc_gb"] = round(
            sum((u.get("traffic_limit") or 0) for u in users) / 1024 ** 3, 1)

    rows = ""
    for u in users:
        used, limit = u.get("used_total") or 0, u.get("traffic_limit") or 0
        pct = min(int(used / limit * 100), 100) if limit > 0 else 0
        bar_cls = "danger" if pct > 90 else ("warn" if pct > 70 else "")
        last = lt(u.get("last_seen")) if u.get("last_seen") else "—"
        _exp = u.get("expire_at") or ""
        expire = lt(_exp, date_only=True) if _exp and not _exp.startswith("2099") else "∞"
        bcls, btxt = _BADGE.get(u["status"], ("badge-off", u["status"]))
        sub_url = f"{cfg.sub_https_base}/{u['short_uuid']}"
        is_active = u["status"] == "ACTIVE"
        toggle_label = "Выкл" if is_active else "Вкл"
        toggle_cls = "btn-warn" if is_active else "btn-ghost"

        traffic_str = fmt_bytes(used)
        if limit > 0:
            traffic_str += f" / {fmt_bytes(limit)}"
            if provider.PROVIDER.get("ok") and provider.PROVIDER.get("limit_gb"):
                hp = round(limit / 1024 ** 3 / provider.PROVIDER["limit_gb"] * 100, 1)
                traffic_str += f' <span style="color:#64748b;font-size:11px">({hp}% хостинга)</span>'
            bar_html = f'<div class="progress" style="margin-top:4px"><div class="progress-bar {bar_cls}" style="width:{pct}%"></div></div>'
        else:
            traffic_str += " / ∞"
            bar_html = ""

        rows += f"""<tr>
<td><b>{u['username']}</b><br><span style="color:#64748b;font-size:11px">{u['vless_uuid'][:18]}…</span></td>
<td><span class="badge {bcls}">{btxt}</span></td>
<td>{traffic_str}{bar_html}</td>
<td>{last}</td>
<td>{expire}</td>
<td>
  <a class="btn btn-sm btn-primary" href="{sub_url}" target="_blank">Подписка</a>
  <button class="btn btn-sm copy-btn" onclick="copyText(this,'{sub_url}/raw')">Ссылка</button>
  <a class="btn btn-sm {toggle_cls}" href="/users/{u['id']}/toggle">{toggle_label}</a>
  <a class="btn btn-sm btn-ghost" href="/users/{u['id']}/edit">Изм.</a>
  <a class="btn btn-sm btn-danger" href="/users/{u['id']}/delete" onclick="return confirm('Удалить {u['username']}?')">✕</a>
</td></tr>"""

    body = f"""
<div class="card">
  <div class="card-title">Пользователи ({len(users)})</div>
  <table>
    <thead><tr><th>Имя / UUID</th><th>Статус</th><th>Трафик</th><th>Онлайн</th><th>До</th><th>Действия</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="6" style="color:#64748b;text-align:center;padding:30px">Нет пользователей</td></tr>'}</tbody>
  </table>
</div>"""
    return HTMLResponse(page("Пользователи", body))


@app.get("/users/create", response_class=HTMLResponse)
async def create_form(request: Request, error: str = ""):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    err = f'<div class="alert alert-err">{error}</div>' if error else ""
    body = f"""
<div style="max-width:480px"><div class="card">
  <div class="card-title">Новый пользователь</div>
  {err}
  <form method="post" action="/users/create">
    <div class="form-group"><label>Имя (латиница, без пробелов)</label><input name="username" required pattern="[A-Za-z0-9_-]+"></div>
    <div class="form-group"><label>Лимит трафика, ГБ (0 = безлимит)</label>
      <input type="number" name="traffic_gb" value="0" min="0" step="10" placeholder="например 800"></div>
    <div class="form-group"><label>Срок действия</label>
      <select name="expire_days">
        <option value="0">Без срока</option><option value="30">30 дней</option>
        <option value="90">3 месяца</option><option value="180">6 месяцев</option><option value="365">1 год</option>
      </select></div>
    <div style="display:flex;gap:10px">
      <button class="btn btn-primary">Создать</button>
      <a class="btn btn-ghost" href="/users">Отмена</a>
    </div>
  </form>
</div></div>"""
    return HTMLResponse(page("Создать", body))


@app.post("/users/create")
async def create_user(request: Request, username: str = Form(...),
                      traffic_gb: int = Form(0), expire_days: int = Form(0)):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    if store.get_by_username(username):
        return RedirectResponse(f"/users/create?error={quote('Имя уже занято')}", status_code=302)
    if expire_days > 0:
        expire_at = (datetime.now(timezone.utc) + timedelta(days=expire_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        expire_at = store.FAR_FUTURE
    traffic_bytes = traffic_gb * 1024 ** 3 if traffic_gb > 0 else 0
    user = store.create_user(username, traffic_limit=traffic_bytes, expire_at=expire_at)
    await asyncio.to_thread(xray_ctl.add_user, user)
    await asyncio.to_thread(render.write_singbox_config)
    await asyncio.to_thread(singbox_ctl.reload)
    return RedirectResponse("/users", status_code=302)


@app.get("/users/{uid}/toggle")
async def toggle_user(request: Request, uid: int):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    u = store.get_by_id(uid)
    if u:
        if u["status"] == "ACTIVE":
            store.update_user(uid, status="DISABLED")
            await asyncio.to_thread(xray_ctl.remove_user, u["username"])
        else:
            store.update_user(uid, status="ACTIVE")
            await asyncio.to_thread(xray_ctl.add_user, store.get_by_id(uid))
        await asyncio.to_thread(render.write_singbox_config)
        await asyncio.to_thread(singbox_ctl.reload)
    return RedirectResponse("/users", status_code=302)


@app.get("/users/{uid}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, uid: int, error: str = ""):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    u = store.get_by_id(uid)
    if not u:
        return RedirectResponse("/users", status_code=302)
    cur_gb = round((u.get("traffic_limit") or 0) / 1024 ** 3)
    cur_exp = (u.get("expire_at") or "")[:10]
    exp_val = cur_exp if cur_exp and cur_exp < "2099" else ""
    err = f'<div class="alert alert-err">{error}</div>' if error else ""
    body = f"""
<div style="max-width:480px"><div class="card">
  <div class="card-title">Изменить — {u['username']}</div>
  {err}
  <form method="post" action="/users/{uid}/edit">
    <div class="form-group"><label>Лимит трафика, ГБ (0 = безлимит)</label>
      <input type="number" name="traffic_gb" value="{cur_gb}" min="0" step="10"></div>
    <div class="form-group"><label>Действует до (пусто = бессрочно)</label>
      <input type="date" name="expire_date" value="{exp_val}"></div>
    <div style="display:flex;gap:10px">
      <button class="btn btn-primary">Сохранить</button>
      <a class="btn btn-ghost" href="/users">Отмена</a>
    </div>
  </form>
  <div style="margin-top:14px"><a class="btn btn-sm btn-ghost" href="/users/{uid}/reset" onclick="return confirm('Обнулить счётчик трафика?')">Обнулить трафик</a></div>
</div></div>"""
    return HTMLResponse(page("Изменить", body))


@app.post("/users/{uid}/edit")
async def edit_user(request: Request, uid: int,
                    traffic_gb: int = Form(0), expire_date: str = Form("")):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    store.update_user(
        uid,
        traffic_limit=traffic_gb * 1024 ** 3 if traffic_gb > 0 else 0,
        expire_at=(f"{expire_date}T23:59:59.000Z" if expire_date else store.FAR_FUTURE),
    )
    # reconcile status (may re-enable a LIMITED/EXPIRED user or disable an over-limit one)
    if await asyncio.to_thread(enforcement.enforce):
        await asyncio.to_thread(render.write_singbox_config)
        await asyncio.to_thread(singbox_ctl.reload)
    return RedirectResponse("/users", status_code=302)


@app.get("/users/{uid}/reset")
async def reset_user(request: Request, uid: int):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    store.reset_traffic(uid)
    if await asyncio.to_thread(enforcement.enforce):
        await asyncio.to_thread(render.write_singbox_config)
        await asyncio.to_thread(singbox_ctl.reload)
    return RedirectResponse("/users", status_code=302)


@app.get("/users/{uid}/delete")
async def delete_user(request: Request, uid: int):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    u = store.get_by_id(uid)
    if u:
        await asyncio.to_thread(xray_ctl.remove_user, u["username"])
        store.delete_user(uid)
        await asyncio.to_thread(render.write_singbox_config)
        await asyncio.to_thread(singbox_ctl.reload)
    return RedirectResponse("/users", status_code=302)


# ── subscriptions ─────────────────────────────────────────────────────────────
def _find(short_uuid: str):
    return store.get_by_short(short_uuid)


@app.get("/sub/{short_uuid}/raw")
async def sub_raw(short_uuid: str):
    u = _find(short_uuid)
    if not u:
        return PlainTextResponse("not found", status_code=404)
    b64 = subs.gen_sub_b64(u["vless_uuid"], u["trojan_pass"], u["username"], cfg)
    return StarResponse(content=b64, media_type="text/plain; charset=utf-8",
                        headers=subs.sub_headers(u, short_uuid, cfg))


@app.get("/sub/{short_uuid}/clash")
async def sub_clash(short_uuid: str):
    u = _find(short_uuid)
    if not u:
        return PlainTextResponse("not found", status_code=404)
    return StarResponse(content=subs.gen_clash_yaml(u["vless_uuid"], u["trojan_pass"], u["username"], cfg),
                        media_type="text/plain; charset=utf-8",
                        headers=subs.sub_headers(u, short_uuid, cfg))


@app.get("/sub/{short_uuid}/singbox")
async def sub_singbox(short_uuid: str):
    u = _find(short_uuid)
    if not u:
        return PlainTextResponse("not found", status_code=404)
    return StarResponse(content=subs.gen_singbox_json(u["vless_uuid"], u["trojan_pass"], u["username"], cfg),
                        media_type="application/json",
                        headers=subs.sub_headers(u, short_uuid, cfg))


@app.get("/sub/{short_uuid}", response_class=HTMLResponse)
async def sub_page(short_uuid: str):
    u = _find(short_uuid)
    if not u:
        return HTMLResponse("<h2>Подписка не найдена</h2>", status_code=404)
    used, limit = u.get("used_total") or 0, u.get("traffic_limit") or 0
    pct = min(int(used / limit * 100), 100) if limit > 0 else 0
    bar_cls = "danger" if pct > 90 else ("warn" if pct > 70 else "")
    expire = (u.get("expire_at") or "")[:10]
    if expire.startswith("2099"):
        expire = "бессрочно"
    active = u["status"] == "ACTIVE"

    base = cfg.sub_https_base
    su_raw, su_clash, su_sb = f"{base}/{short_uuid}/raw", f"{base}/{short_uuid}/clash", f"{base}/{short_uuid}/singbox"
    hiddify_link = f"hiddify://import/{su_sb}"

    mirror_rows = ""
    for _label, _base in cfg.sub_mirrors:
        _su = f"{_base}/{short_uuid}/singbox"
        _hl = f"hiddify://import/{_su}"
        mirror_rows += f"""
<div style="margin-bottom:8px;background:#0f1117;padding:10px 14px;border-radius:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
  <span style="font-size:12px;color:#60a5fa;min-width:96px">{_label}</span>
  <span style="font-family:monospace;font-size:11px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:120px">{_su}</span>
  <a class="btn btn-gray" href="{_hl}" style="font-size:12px;padding:5px 10px">В Hiddify</a>
  <button class="copy-btn" onclick="copyText(this,'{_su}')">Копировать</button>
</div>"""

    traffic_str = fmt_bytes(used)
    if limit > 0:
        traffic_str += f" / {fmt_bytes(limit)}"
        bar = f'<div class="progress" style="margin-top:8px"><div class="progress-bar {bar_cls}" style="width:{pct}%"></div></div>'
    else:
        traffic_str += " / ∞"
        bar = ""
    status_block = "" if active else '<div class="alert alert-err">⚠️ Подписка деактивирована. Обратитесь к администратору.</div>'

    links_html = ""
    for link in subs.gen_sub_links(u["vless_uuid"], u["trojan_pass"], u["username"], cfg):
        proto = link.split("://")[0].upper()
        name_part = unquote(link.split("#")[-1]) if "#" in link else proto
        safe_link = link.replace("'", "%27").replace("`", "%60")
        links_html += f"""
<div style="margin-bottom:8px;background:#0f1117;padding:10px 14px;border-radius:6px;display:flex;align-items:center;gap:8px">
  <span style="font-size:11px;color:#60a5fa;min-width:70px">{name_part}</span>
  <span style="font-family:monospace;font-size:11px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">{link[:80]}…</span>
  <button class="copy-btn" onclick="copyText(this,'{safe_link}')">Копировать</button>
</div>"""

    css_sub = common.CSS + """
.wrap { max-width: 640px; margin: 0 auto; padding: 24px 16px; }
.btn-blue { background: #3b82f6; color: #fff; }
.btn-gray { background: #2d3148; color: #e2e8f0; }
.step { display: flex; gap: 12px; margin-bottom: 12px; align-items: flex-start; }
.step-num { background: #3b82f6; color: #fff; border-radius: 50%; width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; flex-shrink: 0; margin-top: 2px; }
"""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VPN – {u['username']}</title><style>{css_sub}</style></head>
<body><div class="wrap">
<div class="card"><h1 style="font-size:22px">👤 {u['username']}</h1><p style="color:#64748b">{cfg.brand} VPN · подписка</p></div>
{status_block}
<div class="card">
  <div style="font-size:13px;color:#94a3b8;margin-bottom:6px">Использовано трафика</div>
  <div style="font-size:20px;font-weight:700">{traffic_str}</div>{bar}
  <div style="margin-top:10px;font-size:13px;color:#64748b">Действует до: {expire}</div>
</div>
<div class="card">
  <div style="font-size:15px;font-weight:600;margin-bottom:16px">Подключение через Hiddify</div>
  <div class="step"><div class="step-num">1</div><div><b>Скачай Hiddify</b><br>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">
      <a class="btn btn-gray" href="https://play.google.com/store/apps/details?id=app.hiddify.com">Android</a>
      <a class="btn btn-gray" href="https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532">iOS / Mac</a>
      <a class="btn btn-gray" href="https://github.com/hiddify/hiddify-app/releases/latest">Windows / Linux</a>
    </div></div></div>
  <div class="step"><div class="step-num">2</div><div><b>Добавь подписку</b><br>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">
      <a class="btn btn-blue" href="{hiddify_link}">Открыть в Hiddify</a>
      <button class="btn btn-gray" onclick="copyText(this,'{su_sb}')">Скопировать URL</button>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">
      <button class="btn btn-gray" onclick="copyText(this,'{su_clash}')" style="font-size:12px">Clash Meta</button>
      <button class="btn btn-gray" onclick="copyText(this,'{su_raw}')" style="font-size:12px">Raw (v2rayN)</button>
    </div></div></div>
  <div class="step"><div class="step-num">3</div><div><b>Включи VPN</b><br>
    <span style="font-size:13px;color:#94a3b8">Выбери профиль и нажми подключение — приложение само выберет лучший протокол.</span></div></div>
</div>
<div class="card"><div style="font-size:14px;font-weight:600;margin-bottom:6px;color:#94a3b8">Зеркала подписки</div>{mirror_rows}</div>
<div class="card"><div style="font-size:14px;font-weight:600;margin-bottom:12px;color:#94a3b8">Протоколы в подписке</div>{links_html}</div>
</div>{common.TOAST_HTML}<script>{common.COPY_JS}</script></body></html>""")


# ── health ────────────────────────────────────────────────────────────────────
@app.post("/api/health/report")
async def health_report(request: Request):
    if request.headers.get("Authorization", "") != f"Bearer {cfg.health_token}":
        return PlainTextResponse("unauthorized", status_code=401)
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)
    import datetime as _dt
    body["_received"] = _dt.datetime.utcnow().isoformat() + "Z"
    body["_from"] = request.client.host if request.client else "?"
    health.HEALTH_REPORTS.append(body)
    health.save_health()
    try:
        await alerts.eval_health_alerts(body)
    except Exception:
        pass
    return {"ok": True, "stored": len(health.HEALTH_REPORTS)}


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    sh = await asyncio.to_thread(health.server_health)
    cores = await asyncio.to_thread(health.core_status)

    core_line = (badge("ok", "xray: up") if cores["xray"] else badge("crit", "xray: DOWN")) + " " + \
                (badge("ok", "sing-box: up") if cores["singbox"] else badge("crit", "sing-box: DOWN"))

    if sh["alerts"]:
        items = "".join(f'<div style="margin-bottom:6px">{badge(l, l.upper())} {t}</div>' for l, _k, t in sh["alerts"])
    else:
        items = f'<div>{badge("ok", "OK")} Всё в норме, активных алертов нет</div>'
    alerts_card = f'<div class="card"><div class="card-title">Алерты</div>{items}</div>'

    rows = ""
    for p in sh["protocols"]:
        st = badge("ok", "UP") if p["up"] else badge("crit", "DOWN")
        lat = f'{p["lat_ms"]} ms' if p["lat_ms"] is not None else ("—" if p["proto"] == "udp" else badge("crit", "timeout"))
        rows += f'<tr><td>{p["label"]}</td><td>{p["port"]}/{p["proto"]}</td><td>{st}</td><td>{lat}</td></tr>'
    proto_card = (f'<div class="card"><div class="card-title">Протоколы на сервере</div>'
                  f'<table><tr><th>Протокол</th><th>Порт</th><th>Статус</th><th>TCP latency</th></tr>{rows}</table></div>')

    m = sh["mem"]
    swap_cls = "badge-off" if m["swap_used_pct"] >= 70 else ("badge-exp" if m["swap_used_pct"] >= 40 else "badge-ok")
    certs_str = " · ".join(f'{k.split(".")[0]}: {("%d дн." % v) if v is not None else "?"}' for k, v in sh["certs"].items())
    res_card = (f'<div class="card"><div class="card-title">Ресурсы и сертификаты</div>'
                f'<div style="margin-bottom:8px">RAM: {m["mem_used_pct"]}% занято · свободно {m["mem_avail_mb"]}МБ из {m["mem_total_mb"]}МБ</div>'
                f'<div style="margin-bottom:8px">Swap: <span class="badge {swap_cls}">{m["swap_used_pct"]}%</span> {m["swap_used_mb"]}/{m["swap_total_mb"]}МБ &nbsp; {core_line}</div>'
                f'<div style="font-size:13px;color:#94a3b8">Сертификаты: {certs_str}</div></div>')

    ob = health.last_outbound()
    if ob.get("ts"):
        ob_targets = " · ".join(
            f'{t["target"].split(":")[0]} ' +
            (badge("ok", f'{t["lat_ms"]}ms') if t["ok"] else badge("crit", "timeout"))
            for t in ob.get("targets", []))
        ob_state = badge("ok", "OK") if ob.get("reachable") else badge("crit", "НЕТ СВЯЗИ")
        ob_card = (f'<div class="card"><div class="card-title">Исходящая связь (аплинк)</div>'
                   f'<div style="margin-bottom:8px">{ob_state} доступно {ob.get("reachable")}/{ob.get("total")} '
                   f'<span style="color:#64748b;font-size:11px">· проверка раз в {cfg.get("outbound_probe_interval_sec", 30)}с · обновлено {lt(ob["ts"])}</span></div>'
                   f'<div style="font-size:13px">{ob_targets}</div></div>')
    else:
        ob_card = ""

    # SLA (month-to-date), provider/internet outages excluded
    _mtd = datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00.000Z")
    sla = await asyncio.to_thread(store.sla_stats, _mtd)
    if sla["total"]:
        pct = sla["sla"] * 100
        sla_cls = "ok" if pct >= 99.9 else ("warn" if pct >= 99 else "crit")
        secs = cfg.get("outbound_probe_interval_sec", 30)
        sla_card = (
            f'<div class="card"><div class="card-title">SLA (текущий месяц)</div>'
            f'<div style="font-size:22px;font-weight:600;margin-bottom:6px">{badge(sla_cls, f"{pct:.3f}%")}</div>'
            f'<div style="font-size:12px;color:#94a3b8">Наш простой: {sla["our_down"]} проб · '
            f'учтённое время: {sla["denom"]} проб (×{secs}с) · '
            f'исключено как провайдер/интернет: {sla["provider_down"]} проб</div>'
            f'<div style="font-size:11px;color:#64748b;margin-top:4px">Глобальный обрыв интернета в SLA не засчитывается (но шлётся в TG).</div></div>')
    else:
        sla_card = ""

    # disk usage + "clear space"
    disk = sh.get("disk", {})
    dpct = disk.get("used_pct", 0)
    disk_cls = "crit" if dpct >= 90 else ("warn" if dpct >= 80 else "ok")
    disk_card = (
        '<div class="card"><div class="card-title">Диск</div>'
        f'<div style="margin-bottom:10px">{badge(disk_cls, f"{dpct}%")} '
        f'занято {disk.get("used_gb")} / {disk.get("total_gb")} ГБ · свободно {disk.get("free_gb")} ГБ</div>'
        '<button class="btn btn-ghost btn-sm" onclick="cleanDisk(this)">Очистить место</button>'
        '<span id="diskmsg" style="font-size:12px;color:#94a3b8;margin-left:8px"></span>'
        '<div style="font-size:11px;color:#64748b;margin-top:6px">Удаляет docker build-cache, висящие образы и обрезает старые системные логи. '
        'Активные данные, конфиги и подписки не трогаются. Системные логи также авто-капятся (journald 100МБ).</div></div>')

    # server metrics history (server-side SVG line charts, no external JS)
    def _svg_line(series, color, unit="", w=260, h=44):
        vals = [v for _, v in series if v is not None]
        if len(vals) < 2:
            return '<span style="color:#64748b;font-size:12px">собираю данные…</span>'
        mn, mx = min(vals), max(vals); rng = (mx - mn) or 1; n = len(vals)
        pts = " ".join(f"{round(i/(n-1)*w,1)},{round(h-(v-mn)/rng*h,1)}" for i, v in enumerate(vals))
        return (f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="display:block">'
                f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5"/></svg>'
                f'<div style="font-size:11px;color:#94a3b8">сейчас {vals[-1]}{unit} · макс {mx}{unit}</div>')

    _since = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    series = {k: await asyncio.to_thread(store.metric_series, k, _since)
              for k in ("cpu_pct", "mem_pct", "disk_pct", "net_rx_mbps", "net_tx_mbps")}
    metrics_card = (
        '<div class="card"><div class="card-title">Метрики сервера · 3 часа</div>'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px">'
        f'<div><div style="font-size:11px;color:#64748b">CPU, %</div>{_svg_line(series["cpu_pct"], "#f59e0b", "%")}</div>'
        f'<div><div style="font-size:11px;color:#64748b">RAM, %</div>{_svg_line(series["mem_pct"], "#3b82f6", "%")}</div>'
        f'<div><div style="font-size:11px;color:#64748b">Диск, %</div>{_svg_line(series["disk_pct"], "#a855f7", "%")}</div>'
        f'<div><div style="font-size:11px;color:#64748b">Сеть ↓, Mbit/s</div>{_svg_line(series["net_rx_mbps"], "#4ade80")}</div>'
        f'<div><div style="font-size:11px;color:#64748b">Сеть ↑, Mbit/s</div>{_svg_line(series["net_tx_mbps"], "#22d3ee")}</div>'
        '</div></div>'
        '<script>function cleanDisk(b){if(!confirm("Очистить место? Удалятся docker build-cache, висящие образы и старые системные логи. Активные данные не затрагиваются."))return;'
        'b.disabled=true;var m=document.getElementById("diskmsg");m.textContent="Чищу…";'
        'fetch("/api/disk/clean",{method:"POST"}).then(r=>r.json()).then(d=>{m.textContent=d.ok?("Освобождено ~"+d.freed_mb+" МБ · свободно "+d.free_gb+" ГБ"):("Ошибка: "+(d.error||""));b.disabled=false;})'
        '.catch(e=>{m.textContent="Ошибка сети";b.disabled=false;});}</script>')

    cli = ""
    for r in list(health.HEALTH_REPORTS)[-5:][::-1]:
        host = r.get("host", r.get("_from", "?"))
        when = lt(r.get("_received", ""))
        cells = ""
        for name, info in r.get("results", {}).items():
            ok, ms = info.get("ok"), info.get("ms")
            b = badge("ok", f'{ms}ms' if ms is not None else "ok") if ok else badge("crit", "fail")
            cells += f'<td style="text-align:center"><div style="font-size:11px;color:#94a3b8">{name}</div>{b}</td>'
        yt = r.get("youtube", {})
        yt_str = f'{yt.get("mbps")} Mbps' if (yt and yt.get("mbps") is not None) else "—"
        cli += (f'<tr><td>{when}<br><span style="font-size:11px;color:#64748b">{host}</span></td>'
                f'{cells}<td style="text-align:center"><div style="font-size:11px;color:#94a3b8">YouTube</div>{yt_str}</td></tr>')
    if not cli:
        cli = '<tr><td colspan="9" style="color:#64748b">Отчётов от клиентов ещё нет.</td></tr>'
    cli_card = f'<div class="card"><div class="card-title">Отчёты клиентов (healthcheck)</div><table>{cli}</table></div>'

    # ── protocol uptime bars (from recent client reports) ──
    recent = list(health.HEALTH_REPORTS)[-50:]
    order = list((recent[-1].get("results") or {}).keys()) if recent else []
    up_rows = ""
    for p in order:
        tot = sum(1 for r in recent if p in (r.get("results") or {}))
        okc = sum(1 for r in recent if (r.get("results") or {}).get(p, {}).get("ok"))
        pct = round(okc / tot * 100) if tot else 0
        col = "#4ade80" if pct >= 99 else ("#f59e0b" if pct >= 80 else "#ef4444")
        up_rows += (f'<tr><td style="width:90px">{p}</td>'
                    f'<td><div style="background:#0f1117;border-radius:4px;height:8px"><div style="height:100%;width:{pct}%;background:{col};border-radius:4px"></div></div></td>'
                    f'<td style="text-align:right;color:{col};white-space:nowrap">{pct}% <span style="color:#64748b;font-size:11px">({okc}/{tot})</span></td></tr>')
    uptime_card = (f'<div class="card"><div class="card-title">Аптайм протоколов · последние {len(recent)} проверок</div>'
                   f'<table style="table-layout:fixed">{up_rows or "<tr><td>нет данных от клиентов</td></tr>"}</table></div>')

    def _spark(vals, color, h=30):
        present = [v for v in vals if v is not None]
        if not present:
            return '<span style="color:#64748b;font-size:12px">нет данных</span>'
        mx = max(present) or 1
        bars = ""
        for v in vals:
            if v is None:
                bars += '<div style="width:4px;height:1px;background:#2d3148"></div>'
            else:
                bars += f'<div style="width:4px;height:{max(2, round(v / mx * h))}px;background:{color};border-radius:1px" title="{v}"></div>'
        return f'<div style="display:flex;align-items:flex-end;gap:2px;height:{h}px">{bars}</div>'

    pings = [(r.get("youtube") or {}).get("ping_ms") for r in recent]
    speeds = [(r.get("youtube") or {}).get("mbps") for r in recent]
    latest = (recent[-1].get("youtube") or {}) if recent else {}
    last_speed = next((s for s in reversed(speeds) if s is not None), None)
    _ping = latest.get("ping_ms")
    _loss = latest.get("loss_pct")
    net_card = (
        '<div class="card"><div class="card-title">Сеть клиента</div>'
        '<div style="display:flex;gap:24px;flex-wrap:wrap;font-size:13px;margin-bottom:12px">'
        f'<div>Ping: <b>{_ping if _ping is not None else "—"} ms</b></div>'
        f'<div>Потери: <b>{_loss if _loss is not None else "—"}%</b></div>'
        f'<div>Скорость: <b>{last_speed if last_speed is not None else "—"} Mbps</b> '
        '<span style="color:#64748b;font-size:11px">(замер раз в час)</span></div></div>'
        f'<div style="font-size:11px;color:#64748b;margin-bottom:4px">Ping, мс</div>{_spark(pings, "#3b82f6")}'
        f'<div style="font-size:11px;color:#64748b;margin:10px 0 4px">Скорость, Mbps</div>{_spark(speeds, "#4ade80")}'
        '</div>'
    )

    install_cmd = (f"iwr http://{cfg.server_ip}:8080/health/install -OutFile install.ps1; "
                   "powershell -ExecutionPolicy Bypass -File install.ps1")
    hc_card = (
        '<div class="card"><div class="card-title">Healthcheck для Windows</div>'
        f'<div style="background:#0f1117;padding:10px 14px;border-radius:6px;font-family:monospace;font-size:12px;color:#cbd5e1;word-break:break-all;margin-bottom:10px">{install_cmd}</div>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap">'
        f'<button class="btn btn-primary btn-sm" onclick="copyText(this,\'{install_cmd}\')">Скопировать команду</button>'
        '<a class="btn btn-ghost btn-sm" href="/health/install">Установщик</a>'
        '<a class="btn btn-ghost btn-sm" href="/health/script">Скрипт</a>'
        '<button class="btn btn-ghost btn-sm" onclick="fetch(\'/api/tg/test\').then(r=>r.json()).then(d=>showToast(d.sent?\'Алерт отправлен\':\'Нет chat_id — напиши боту\'))">Тест Telegram</button>'
        '</div></div>'
    )
    body = (f'<h2 style="margin-bottom:16px">Здоровье VPN</h2>'
            f'<div style="font-size:12px;color:#64748b;margin-bottom:16px">обновлено {lt(sh["ts"])} · <a href="/health">обновить</a></div>'
            f'{alerts_card}{proto_card}{res_card}{disk_card}{ob_card}{sla_card}{metrics_card}{uptime_card}{net_card}{cli_card}{hc_card}')
    return HTMLResponse(page("Здоровье", body))


@app.post("/api/disk/clean")
async def disk_clean(request: Request):
    if not _auth(request):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    import shutil as _sh
    import subprocess
    before = _sh.disk_usage("/").free

    def _prune():
        for args in (["docker", "image", "prune", "-f"],
                     ["docker", "builder", "prune", "-f"]):
            try:
                subprocess.run(args, capture_output=True, text=True, timeout=120)
            except Exception:
                pass
    await asyncio.to_thread(_prune)
    after = _sh.disk_usage("/")
    return JSONResponse({"ok": True, "freed_mb": max(0, round((after.free - before) / 1024**2)),
                         "free_gb": round(after.free / 1024**3, 1)})


def _serve_ps1(path: str, filename: str):
    try:
        txt = open(path, encoding="utf-8").read()
    except Exception:
        return PlainTextResponse("script not found", status_code=404)
    # scripts ship with placeholders (no secrets in repo); fill from live config
    for token, val in (("__SERVER_IP__", cfg.server_ip), ("__HEALTH_TOKEN__", cfg.health_token),
                       ("__DIRECT_DOMAIN__", cfg.direct_domain), ("__CDN_DOMAIN__", cfg.cdn_domain),
                       ("__BRAND__", cfg.brand)):
        txt = txt.replace(token, str(val or ""))
    return PlainTextResponse("﻿" + txt, media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/health/script")
async def health_script():
    return _serve_ps1("/app/scripts/vpn-healthcheck.ps1", "vpn-healthcheck.ps1")


@app.get("/health/install")
async def health_install():
    return _serve_ps1("/app/scripts/install-healthcheck.ps1", "install.ps1")


# ── provider / telegram ───────────────────────────────────────────────────────
@app.get("/provider/refresh")
async def provider_refresh_ui(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    await provider.refresh_provider(force=True)
    return RedirectResponse("/users", status_code=302)


@app.get("/api/tg/test")
async def tg_test(request: Request):
    if not _auth(request):
        return PlainTextResponse("unauthorized", status_code=401)
    ok = await alerts.tg_send(f"✅ Тест: алерты {cfg.brand} VPN подключены.")
    return {"sent": ok, "chat_id": alerts._tg.get("chat_id")}


# ── settings (Phase A: read-only overview; Phase B: full editing) ─────────────
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", status_code=302)
    d = cfg.as_dict()
    def mask(s):
        s = str(s or "")
        return (s[:6] + "…" + s[-4:]) if len(s) > 12 else "—"
    dom_rows = "".join(
        f'<tr><td>{x["domain"]}</td><td>{x["role"]}</td><td>{"CF proxy" if x.get("proxied") else "—"}</td></tr>'
        for x in d.get("domains", []))
    prov = d.get("provider", {})
    body = f"""
<div class="card"><div class="card-title">Настройки (просмотр)</div>
<div style="font-size:13px;color:#94a3b8;margin-bottom:12px">Полное редактирование через веб появится в Phase B; пока правится через env/.env и перезапуск.</div>
<table>
  <tr><th>Параметр</th><th>Значение</th></tr>
  <tr><td>Бренд</td><td>{d.get('brand')}</td></tr>
  <tr><td>IP сервера</td><td>{d.get('server_ip')}</td></tr>
  <tr><td>База подписки</td><td>{d.get('sub_https_base')}</td></tr>
  <tr><td>Reality SNI / порт</td><td>{d.get('reality_sni')} : {d.get('reality_port')}</td></tr>
  <tr><td>Reality pubkey</td><td><code>{d.get('reality_public_key')}</code></td></tr>
  <tr><td>CDN домен</td><td>{d.get('cdn_domain')} ({d.get('hu_path')})</td></tr>
  <tr><td>Hy2 / TUIC / TCP порты</td><td>{d.get('hy2_port')} · {d.get('tuic_port')} · {d.get('tcp_port')}</td></tr>
  <tr><td>TG bot token</td><td>{mask(d.get('tg_bot_token'))}</td></tr>
  <tr><td>Провайдер-виджет</td><td>{prov.get('name')} ({'вкл' if prov.get('enabled') else 'выкл'}) · key {mask(prov.get('api_key'))}</td></tr>
</table></div>
<div class="card"><div class="card-title">Домены</div>
<table><tr><th>Домен</th><th>Роль</th><th>CF</th></tr>{dom_rows}</table></div>"""
    return HTMLResponse(page("Настройки", body))


# ── background loop ───────────────────────────────────────────────────────────
@app.on_event("startup")
async def _start_bg():
    store.init_db()

    async def loop():
        await asyncio.sleep(20)
        while True:
            try:
                await asyncio.to_thread(enforcement.tick)
                await provider.refresh_provider(force=True)
                await alerts.eval_server_alerts()
            except Exception:
                pass
            await asyncio.sleep(cfg.get("poll_interval_sec", 120))

    async def outbound_loop():
        # tight cadence to catch short uplink/packet-loss blips; also drives
        # SLA sampling and core self-heal.
        await asyncio.sleep(25)
        fails = {"xray": 0, "singbox": 0}
        ctl = {"xray": xray_ctl, "singbox": singbox_ctl}
        while True:
            try:
                probe = await asyncio.to_thread(health.outbound_probe)
                await alerts.eval_outbound_alert(probe)
                cores = await asyncio.to_thread(health.core_status)

                # classify this tick for SLA (provider outages excluded)
                if probe.get("total", 0) > 0 and probe.get("reachable", 0) == 0:
                    kind = "provider_down"
                elif not (cores.get("xray") and cores.get("singbox")):
                    kind = "our_down"
                else:
                    kind = "up"
                await asyncio.to_thread(store.record_sla, kind)

                # time-series metrics (CPU/mem/disk/net/udp-drops) for the dashboard
                await asyncio.to_thread(lambda: store.record_metrics(health.collect_metrics()))

                # self-heal: restart a wedged core, but never during a provider
                # outage (restarting can't fix a dead uplink). One restart per
                # outage episode; counter resets when the core recovers.
                if kind != "provider_down":
                    for name in ("xray", "singbox"):
                        if cores.get(name):
                            fails[name] = 0
                        else:
                            fails[name] += 1
                            if fails[name] == 3:  # ~90s unresponsive
                                await alerts.tg_send(f"🔧 <b>{name} не отвечает — перезапускаю</b>")
                                ok, out = await asyncio.to_thread(ctl[name].restart)
                                await alerts.tg_send(("✅" if ok else "❌") +
                                                     f" перезапуск {name}: {(out or '').strip()[:120]}")
            except Exception:
                pass
            await asyncio.sleep(cfg.get("outbound_probe_interval_sec", 30))

    asyncio.create_task(loop())
    asyncio.create_task(outbound_loop())
