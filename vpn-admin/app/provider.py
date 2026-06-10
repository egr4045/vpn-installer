"""Hosting-provider traffic widget (generic).

Configured via settings.provider:
  * HOSTKEY-style two-step (auth_url -> token, then data_url -> server_data),
    used when `auth_url` is set; api_key + server_id pre-filled for HOSTKEY.
  * otherwise a single authenticated GET of data_url returning JSON, reading
    `used_field` / `limit_field` by name.
Returns used/limit in GB so the banner + traffic alerts work unchanged.
"""
from __future__ import annotations

import json
import os
import time

import httpx

from . import config, store

PROVIDER_FILE = os.path.join(config.DATA_DIR, "provider.json")
PROVIDER_TTL = 600
PROVIDER = {"ok": False, "used_gb": 0.0, "limit_gb": 0, "alloc_gb": 0.0, "bands": 0, "ts": 0}

try:
    PROVIDER.update(json.load(open(PROVIDER_FILE)))
except Exception:
    pass


async def _fetch_hostkey(p: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(p["auth_url"], data={"action": "login", "key": p["api_key"],
                                              "base": "invapi.hostkey.ru"})
        tok = r.json()["result"]["token"]
        r2 = await c.post(p["data_url"], data={"action": "show", "key": p["api_key"],
                                               "token": tok, "id": p["server_id"]})
        sd = r2.json()["server_data"]
        return sd[0] if isinstance(sd, list) else sd


async def _fetch_generic(p: dict) -> dict:
    headers = {}
    if p.get("api_key"):
        headers["Authorization"] = f"Bearer {p['api_key']}"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(p["data_url"], headers=headers)
        return r.json()


async def refresh_provider(force: bool = False) -> None:
    cfg = config.get_settings()
    p = cfg.get("provider", {})
    if not p.get("enabled"):
        PROVIDER["ok"] = False
        return
    if not force and PROVIDER.get("ok") and (time.time() - PROVIDER.get("ts", 0) < PROVIDER_TTL):
        return
    try:
        sd = await (_fetch_hostkey(p) if p.get("auth_url") else _fetch_generic(p))
        used = float(sd.get(p["used_field"]) or 0)
        raw_limit = float(sd.get(p["limit_field"]) or 0)
        limit_gb = int(round(raw_limit * 1024)) if p.get("limit_unit_tb") else int(round(raw_limit))
        bands = int(float(sd.get(p.get("bands_field", ""), 0) or 0))
        reset = str(sd.get(p.get("reset_field", ""), ""))[:10]
        users = store.list_users()
        alloc = sum((u.get("traffic_limit") or 0) for u in users)
        PROVIDER.update({"ok": True, "used_gb": round(used, 1), "limit_gb": limit_gb,
                         "alloc_gb": round(alloc / 1024 ** 3, 1), "bands": bands,
                         "reset": reset, "ts": time.time(), "name": p.get("name", "")})
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            json.dump(PROVIDER, open(PROVIDER_FILE, "w"))
        except Exception:
            pass
        try:
            from . import alerts
            await alerts.eval_traffic_alerts(round(used, 1), limit_gb, users)
        except Exception:
            pass
    except Exception as e:
        PROVIDER["err"] = str(e)[:100]


def _fmt_tb(gb: float) -> str:
    return f"{gb / 1024:.2f} TB" if gb >= 1024 else f"{gb:.0f} GB"


def _ago(ts: float) -> str:
    if not ts:
        return "никогда"
    d = int(time.time() - ts)
    if d < 60:
        return f"{d} сек назад"
    if d < 3600:
        return f"{d // 60} мин назад"
    return f"{d // 3600} ч назад"


def provider_banner() -> str:
    p = PROVIDER
    if not p.get("ok"):
        if not config.get_settings().get("provider", {}).get("enabled"):
            return ""
        return ('<div style="background:#1e2130;border:1px solid #2d3148;border-radius:8px;'
                'padding:10px 14px;margin-bottom:16px;font-size:13px;color:#94a3b8">'
                'Трафик хостера: данные пока недоступны</div>')
    used, lim, alloc = p["used_gb"], p["limit_gb"], p["alloc_gb"]
    pct = round(used / lim * 100, 1) if lim else 0
    apct = round(alloc / lim * 100) if lim else 0
    color = "#ef4444" if pct >= 100 else ("#f59e0b" if pct >= 80 else "#3b82f6")
    warn = ""
    if pct >= 100:
        warn += '<span style="color:#fca5a5">⚠ превышен лимит хостера</span> '
    if alloc > lim:
        warn += f'<span style="color:#fcd34d">⚠ распределено больше лимита ({apct}%)</span>'
    warn_html = f'<div style="font-size:12px;margin-top:6px">{warn}</div>' if warn else ""
    label = p.get("name") or "хостера"
    return f"""
<div style="background:#1e2130;border:1px solid #2d3148;border-radius:8px;padding:12px 16px;margin-bottom:16px">
  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:13px;margin-bottom:6px">
    <span><b>Трафик {label}</b> · {_fmt_tb(used)} / {_fmt_tb(lim)} ({pct}%)</span>
    <span style="color:#64748b">распределено {_fmt_tb(alloc)} / {_fmt_tb(lim)} · сброс {p.get('reset','?')} · обновлено {_ago(p['ts'])} <a href="/provider/refresh" style="color:#60a5fa;text-decoration:none" title="обновить">↻</a></span>
  </div>
  <div style="background:#0f1117;border-radius:5px;height:10px;overflow:hidden">
    <div style="height:100%;width:{min(pct,100)}%;background:{color}"></div>
  </div>
  {warn_html}
</div>"""
