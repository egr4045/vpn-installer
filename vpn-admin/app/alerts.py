"""Telegram alerting. Ported from the original admin; token/thresholds come
from settings. chat_id is auto-discovered via getUpdates and cached."""
from __future__ import annotations

import json
import os

import httpx

from . import config

TG_FILE = os.path.join(config.DATA_DIR, "tg.json")
ALERTS_FILE = os.path.join(config.DATA_DIR, "alerts.json")
HEALTH_FAIL_STREAK = 2
CLIENT_FAIL_STREAK = 1
HEALTH_SKIP = {"Reality"}   # RKN-flapping; not alerted on

_tg = {"chat_id": None}
try:
    _tg.update(json.load(open(TG_FILE)))
except Exception:
    pass
ALERTS: dict = {}
try:
    ALERTS.update(json.load(open(ALERTS_FILE)))
except Exception:
    pass


def _token() -> str:
    return config.get_settings().tg_bot_token


def _save_alerts():
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        json.dump(ALERTS, open(ALERTS_FILE, "w"))
    except Exception:
        pass


async def _tg_discover(c):
    try:
        r = await c.get(f"https://api.telegram.org/bot{_token()}/getUpdates")
        for u in reversed(r.json().get("result", [])):
            m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            ch = m.get("chat") or {}
            if ch.get("id"):
                _tg["chat_id"] = ch["id"]
                try:
                    json.dump(_tg, open(TG_FILE, "w"))
                except Exception:
                    pass
                return ch["id"]
    except Exception:
        pass
    return None


async def tg_send(text: str) -> bool:
    token = _token()
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            cid = _tg.get("chat_id") or await _tg_discover(c)
            if not cid:
                return False
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": cid, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
            return bool(r.json().get("ok"))
    except Exception:
        return False


def _crossed(key, pct, thresholds):
    prev = set(ALERTS.get(key, []))
    now = set(t for t in thresholds if pct >= t)
    if now != prev:
        ALERTS[key] = sorted(now)
        _save_alerts()
    return sorted(now - prev)


async def eval_traffic_alerts(used_gb, limit_gb, users):
    cfg = config.get_settings()
    if limit_gb:
        hp = used_gb / limit_gb * 100
        for t in _crossed("host", hp, cfg.host_thresholds):
            await tg_send(f"🟠 <b>Хостинг: достигнут {t}% лимита трафика</b>\n"
                          f"Использовано {used_gb:.0f} ГБ из {limit_gb} ГБ — {hp:.1f}%")
    for u in users or []:
        lim = u.get("traffic_limit") or 0
        if lim <= 0:
            continue
        used = u.get("used_total") or 0
        pct = used / lim * 100
        for t in _crossed(f"user:{u['id']}", pct, cfg.user_thresholds):
            await tg_send(f"⚠️ <b>{u.get('username')}: {t}% личного лимита</b>\n"
                          f"{used/1024**3:.1f} / {lim/1024**3:.0f} ГБ ({pct:.0f}%)")


async def eval_health_alerts(report: dict):
    host = report.get("host", "?")
    when = (report.get("_received") or "")[:19].replace("T", " ")
    for proto, info in (report.get("results") or {}).items():
        if proto in HEALTH_SKIP:
            continue
        key = f"health:{host}:{proto}"
        st = ALERTS.get(key)
        if not isinstance(st, dict):
            st = {"fails": 0, "alerted": False}
        if not info.get("ok"):
            st["fails"] = st.get("fails", 0) + 1
            if st["fails"] >= CLIENT_FAIL_STREAK and not st.get("alerted"):
                st["alerted"] = True
                await tg_send(f"🔴 <b>{proto} не отвечает</b>\nКлиент: {host}\nВремя: {when} UTC")
        else:
            if st.get("alerted"):
                await tg_send(f"🟢 <b>{proto} снова работает</b>\nКлиент: {host}")
            st = {"fails": 0, "alerted": False}
        ALERTS[key] = st
    _save_alerts()


async def eval_outbound_alert(probe: dict):
    """Fire/clear a TG alert on total loss of outbound connectivity (uplink blip).
    Kept separate from eval_server_alerts so it can run on a tight cadence and
    catch short (<5 min) provider blips that the port-listening check misses."""
    key = "outbound"
    state = ALERTS.get(key) if isinstance(ALERTS.get(key), dict) else {"alerted": False}
    down = probe.get("total", 0) > 0 and probe.get("reachable", 0) == 0
    if down and not state.get("alerted"):
        tgt = ", ".join(t["target"] for t in probe.get("targets", []))
        await tg_send("🔴 <b>Сервер: пропала исходящая связь</b>\n"
                      f"Недоступны все контрольные адреса ({tgt}).\n"
                      "Похоже на потерю пакетов на аплинке — протоколы могут лагать/рваться у всех.")
        ALERTS[key] = {"alerted": True}
        _save_alerts()
    elif not down and state.get("alerted"):
        await tg_send("🟢 <b>Сервер: исходящая связь восстановлена</b>\n"
                      f"Доступно {probe.get('reachable')}/{probe.get('total')} адресов.")
        ALERTS[key] = {"alerted": False}
        _save_alerts()


async def eval_reachability_alert(reach: dict, anycast_up: bool):
    """Fire/clear a TG alert on a PARTIAL outage: several real, network-diverse
    services unreachable at once while anycast (CF/Google) is still up — the
    signature of a provider transit/peering fault that blackholes part of the
    internet (AWS/Twitch, game servers) but looks green on the basic watchdog.

    Skip entirely when anycast is down too: that's a TOTAL outage already covered
    by eval_outbound_alert (avoid double-paging). Streak of 2 to debounce."""
    cfg = config.get_settings()
    need = cfg.get("reachability_alert_fails", 2)
    total = reach.get("total", 0)
    if not total:
        return
    fails = total - (reach.get("reachable") or 0)
    partial = fails >= need
    key = "reach"
    state = ALERTS.get(key) if isinstance(ALERTS.get(key), dict) else {"streak": 0, "alerted": False}

    if not anycast_up:
        # Total uplink outage — owned by eval_outbound_alert. Hold our state and
        # stay silent so we don't emit a false "restored" while things are worse.
        return

    if partial:
        state["streak"] = state.get("streak", 0) + 1
        if state["streak"] >= 2 and not state.get("alerted"):
            state["alerted"] = True
            down = ", ".join(t["target"] for t in reach.get("targets", []) if not t["ok"])
            await tg_send(
                "🟠 <b>Частичная потеря связи (похоже на транзит провайдера)</b>\n"
                f"Сервер не достучался до {fails}/{total} контрольных сервисов, "
                "при этом Cloudflare/Google отвечают.\n"
                f"Недоступны: {down}\n"
                "Симптом: часть сайтов/игр/стримов не грузится у всех. "
                "Лечится на стороне хостера (можно завести тикет).")
        ALERTS[key] = state
        _save_alerts()
    else:
        if state.get("alerted"):
            await tg_send("🟢 <b>Связь восстановлена</b>\n"
                          f"Доступно {reach.get('reachable')}/{total} контрольных сервисов.")
        ALERTS[key] = {"streak": 0, "alerted": False}
        _save_alerts()


async def eval_server_alerts():
    import asyncio

    from . import health
    sh = await asyncio.to_thread(health.server_health)
    cur = {k: t for _lvl, k, t in sh.get("alerts", [])}
    state = ALERTS.get("srv")
    if not isinstance(state, dict):
        state = {}
    newstate = {}
    for k, text in cur.items():
        prev = state.get(k) if isinstance(state.get(k), dict) else {}
        cnt = prev.get("fails", 0) + 1
        alerted = prev.get("alerted", False)
        if cnt >= HEALTH_FAIL_STREAK and not alerted:
            alerted = True
            await tg_send(f"🔴 <b>Сервер</b>\n{text}")
        newstate[k] = {"fails": cnt, "alerted": alerted, "text": text}
    for k, prev in state.items():
        if isinstance(prev, dict) and k not in cur and prev.get("alerted"):
            await tg_send(f"🟢 <b>Сервер: восстановлено</b>\n{prev.get('text', k)}")
    ALERTS["srv"] = newstate
    _save_alerts()
