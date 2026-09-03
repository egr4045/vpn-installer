#!/usr/bin/env python3
"""safechill-bot — Telegram admin bot for the exit node (stdlib only, long polling).

UX
  /users        people as buttons. Tapping a name turns the SAME message into that person's panel
                (key variants, ClashMi, Happ, delete, back) — menus edit themselves, the chat stays clean.
  panel → key   a clean, forwardable card: photo (QR + 3 steps) with URL buttons to download the app,
                then the key as a .vpn file with the key text under a spoiler (tap = reveal, tap = copy).
                No admin buttons on anything meant to be forwarded. Default key = the RU entry (survives
                mobile white lists; the RU node itself fails over to the standby exit), else Amsterdam.
  /status       rendered from /var/lib/safechill/health.json (written by vpn-health.sh every minute):
                instant, no probing. "🔄 Проверить сейчас" runs the health check and re-renders in place.
  /add /del /newip /dropip   ask first; the confirmation edits itself into progress and then the result.
Vocabulary shared with the health scripts: 🇳🇱 Амстердам · 🇷🇺 Москва · 🛟 Запасной; 🔴🟠🟡🟢🔵.
Admins only (TG_ADMIN_IDS in /etc/safechill/vpn.env); anyone else gets a toast, not a message in the chat.

Commands: /users  /add <name>  /del <name>  /status  /newip  /dropip <ip>  /qr <name> [ru|nl|awg|x2|happ|clash]  /help
CLI:      safechill-bot.py --card <name> <chat_id> [variant]     safechill-bot.py --status <chat_id>
"""
import json, re, subprocess, sys, time, urllib.request, urllib.error, uuid, html, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

ETC = pathlib.Path("/etc/safechill"); CLIENTS = pathlib.Path("/root/clients")
STATE = pathlib.Path("/var/lib/safechill"); STATE.mkdir(parents=True, exist_ok=True)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$"); IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
AMNEZIA_LATEST = "https://github.com/amnezia-vpn/amnezia-client/releases/latest"
AMNEZIA_IOS = "https://apps.apple.com/us/app/amneziavpn/id1600529900"
AMNEZIA_ANDROID = "https://play.google.com/store/apps/details?id=org.amnezia.vpn"
HAPP_IOS = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
HAPP_SITE = "https://www.happ.su/main/ru"
CLASHMI_IOS = "https://apps.apple.com/us/app/clash-mi/id6744321968"
CLASHMI_ALL = "https://github.com/KaringX/clashmi/releases/latest"
CLASHMI_SITE = "https://clashmi.app/download"
MSK = ZoneInfo("Europe/Moscow")
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()

def load_env(p):
    env = {}
    for line in pathlib.Path(p).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip("'\"")
    return env

ENV = load_env(ETC / "vpn.env")
TOKEN = ENV.get("TG_BOT_TOKEN", "")
BRAND = ENV.get("BRAND", "SafeChill")
ADMINS = {int(x) for x in re.split(r"[ ,]+", ENV.get("TG_ADMIN_IDS", "")) if x.strip().lstrip("-").isdigit()}
API = f"https://api.telegram.org/bot{TOKEN}/"
PENDING_ADD = {}   # chat -> (expires, prompt message id): the next plain message is a new person's name

def log(*a): print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)
def esc(s): return html.escape(str(s))
def env_now(): return load_env(ETC / "vpn.env")   # SERVER_IP changes after /newip

# ── small russian helpers ─────────────────────────────────────────────────────
def plural(n, one, few, many):
    n = int(n); m, h = n % 10, n % 100
    if m == 1 and h != 11: return f"{n} {one}"
    if 2 <= m <= 4 and not 12 <= h <= 14: return f"{n} {few}"
    return f"{n} {many}"
def hhmm(ts): return datetime.fromtimestamp(ts, MSK).strftime("%H:%M")
def rudate(ts): d = datetime.fromtimestamp(ts, MSK); return f"{d.day} {MONTHS[d.month - 1]}"
def dur(sec):
    sec = int(sec)
    if sec < 3600: return f"{max(1, (sec + 59) // 60)} мин"
    if sec < 86400: return f"{sec // 3600} ч {(sec % 3600) // 60} мин"
    return f"{sec // 86400} дн. {(sec % 86400) // 3600} ч"
def ago(sec):
    if sec is None: return ""
    if sec < 120: return "только что"
    if sec < 3600: return f"{sec // 60} мин назад"
    if sec < 86400: return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн. назад"
def tail(out, n=8): return "\n".join(out.strip().splitlines()[-n:])[-1500:]

# ── Telegram API ──────────────────────────────────────────────────────────────
def api(method, **params):
    req = urllib.request.Request(API + method, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r: return json.load(r)

def api_multipart(method, fields, files):
    boundary = uuid.uuid4().hex; body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, blob, ctype) in files.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + blob + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(API + method, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=70) as r: return json.load(r)

def btn(text, data): return {"text": text, "callback_data": data}
def url(text, link): return {"text": text, "url": link}
def copy(text, s): return {"text": text, "copy_text": {"text": s}}
def clip(text, n=4000):  # never cut inside a tag
    return text if len(text) <= n else re.sub(r"<[^>]*$", "", text[:n - 1]) + "…"

def send(chat, text, kb=None, silent=False):
    p = {"chat_id": chat, "text": clip(text), "parse_mode": "HTML", "disable_web_page_preview": True, "disable_notification": silent}
    if kb: p["reply_markup"] = {"inline_keyboard": kb}
    return api("sendMessage", **p)["result"]["message_id"]

def edit(chat, mid, text, kb=None):
    p = {"chat_id": chat, "message_id": mid, "text": clip(text), "parse_mode": "HTML", "disable_web_page_preview": True,
         "reply_markup": {"inline_keyboard": kb or []}}
    try: api("editMessageText", **p)
    except urllib.error.HTTPError as e:
        if e.code == 400 and b"not modified" in e.read(): return
        raise

def toast(cid, text="", alert=False):
    try: api("answerCallbackQuery", callback_query_id=cid, text=text, show_alert=alert)
    except Exception: pass

def send_photo(chat, path, caption, kb=None):
    f = {"chat_id": str(chat), "caption": caption[:1024], "parse_mode": "HTML"}
    if kb: f["reply_markup"] = json.dumps({"inline_keyboard": kb})
    return api_multipart("sendPhoto", f, {"photo": (pathlib.Path(path).name, pathlib.Path(path).read_bytes(), "image/png")})

def send_doc(chat, blob, fname, caption, kb=None):
    f = {"chat_id": str(chat), "caption": caption[:1024], "parse_mode": "HTML"}
    if kb: f["reply_markup"] = json.dumps({"inline_keyboard": kb})
    return api_multipart("sendDocument", f, {"document": (fname, blob, "application/octet-stream")})

# ── data ──────────────────────────────────────────────────────────────────────
def users(): return json.loads((ETC / "users.json").read_text())

def find_user(name):
    for u in users():
        if u["name"].lower() == name.lower(): return u["name"]
    return None

def awg_last_seen():
    pub2name = {}
    for f in (ETC / "peers").glob("*.env"):
        e = load_env(f); pub2name[e.get("PEER_PUB", "")] = e.get("PEER_NAME", f.stem)
    out = {}
    try:
        for line in subprocess.run(["awg", "show", "awg0", "latest-handshakes"], capture_output=True, text=True, timeout=10).stdout.splitlines():
            pub, ts = line.split(); n = pub2name.get(pub); ts = int(ts)
            if n: out[n] = (int(time.time()) - ts) if ts else None
    except Exception: pass
    return out

def xray_recent_users(minutes=5):
    """name -> connections accepted in the last N minutes (xray access log in journald)."""
    out = {}
    try:
        txt = subprocess.run(["journalctl", "-u", "xray", "--since", f"-{minutes}min", "--no-pager", "-o", "cat"], capture_output=True, text=True, timeout=15).stdout
        for m in re.finditer(r"accepted .* email: (\S+)", txt): out[m.group(1)] = out.get(m.group(1), 0) + 1
    except Exception: pass
    return out

def online_set():
    awg = awg_last_seen()
    return set(xray_recent_users(5)) | {n for n, s in awg.items() if s is not None and s < 180}

def amnezia_latest():
    cache = STATE / "amnezia.latest"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600: return cache.read_text().strip()
        req = urllib.request.Request("https://api.github.com/repos/amnezia-vpn/amnezia-client/releases/latest", headers={"User-Agent": "safechill-bot"})
        with urllib.request.urlopen(req, timeout=10) as r: tag = json.load(r).get("tag_name", "")
        cache.write_text(tag); return tag
    except Exception:
        return cache.read_text().strip() if cache.exists() else ""

def sh(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()

# ── cards (forwardable: no admin buttons, URL buttons only) ───────────────────
VARIANTS = {  # variant -> (file stem, second line of the card)
    "ru":  ("amnezia-ru",  "Основной ключ · вход через Россию, выход в Амстердаме. Работает и на мобильном в дни белых списков; если Амстердам упал, сам уходит на запасной выход. Ставь этот."),
    "nl":  ("amnezia",     "Амстердам напрямую · запасной вариант, если вход через Россию не подключается."),
    "awg": ("amnezia-awg", "AmneziaWG · Амстердам напрямую. Быстрее на домашнем Wi-Fi и ПК. На мобильном может не работать — тогда вернись к основному."),
    "x2":  ("amnezia-x2",  "Запасной выход напрямую · если и Россия, и Амстердам недоступны."),
}
PANEL_BTN = {"ru": "🇷🇺 Основной", "nl": "🇳🇱 Амстердам", "awg": "⚡ AmneziaWG", "x2": "🛟 Запасной", "clash": "🧩 ClashMi", "happ": "🔗 Happ"}
NO_NODE = {"ru": "🇷🇺 Вход через Россию не настроен (RU_HOST пуст) — setup-ru.sh на ноде.",
           "x2": "🛟 Запасной выход не настроен (EXIT2_HOST пуст) — setup-exit2.sh на ноде."}
AMNEZIA_DL = [[url("📲 iPhone", AMNEZIA_IOS), url("🤖 Android", AMNEZIA_ANDROID), url("💻 Windows / Mac", AMNEZIA_LATEST)]]
HAPP_DL = [url("📲 iPhone", HAPP_IOS), url("🤖 Android", HAPP_ANDROID), url("💻 Windows / Mac", HAPP_SITE)]
CLASH_DL = [url("📲 iPhone", CLASHMI_IOS), url("🤖 Android · 💻 ПК", CLASHMI_ALL), url("🌐 Сайт", CLASHMI_SITE)]

def default_variant(name):
    return "ru" if (CLIENTS / name / "amnezia-ru.txt").exists() else "nl"

def variant_file(name, v):
    return CLIENTS / name / ("sub.txt" if v == "happ" else "clash.txt" if v == "clash" else VARIANTS[v][0] + ".txt")

def card(chat, name, variant=None):
    """Photo (QR + steps + download buttons), then the .vpn file with the key under a spoiler."""
    variant = variant or default_variant(name)
    if variant == "happ": return card_happ(chat, name)
    if variant == "clash": return card_clash(chat, name)
    stem, line2 = VARIANTS[variant]; d = CLIENTS / name; key_f = d / f"{stem}.txt"
    if not key_f.exists(): return send(chat, NO_NODE.get(variant, f"У {esc(name)} нет ключа «{variant}» — запусти add-client.sh {esc(name)} на ноде"))
    v = amnezia_latest()
    cap = (f"🔑 <b>{BRAND}</b> · {esc(name)}\n{line2}\n\n"
           f"1. Скачай AmneziaVPN{' ' + esc(v) if v else ''} — кнопки ниже\n"
           f"2. В приложении: «+» → «Подключиться по ключу» → сканируй QR или открой файл\n"
           f"3. Включи. Готово.")
    send_photo(chat, d / f"{stem}.png", cap, AMNEZIA_DL)
    key = key_f.read_text().strip(); fname = f"{BRAND}-{name}-{variant}.vpn"
    intro = "Ключ файлом — открой его в Amnezia. Или текстом, нажми чтобы раскрыть и скопировать:\n"
    if len(intro) + len(key) + 40 > 1024: return send_doc(chat, key.encode(), fname, "Ключ внутри файла: открой его в Amnezia.")
    try: send_doc(chat, key.encode(), fname, f"{intro}<tg-spoiler><code>{esc(key)}</code></tg-spoiler>")
    except urllib.error.HTTPError:  # Telegram refused the nesting → plain monospace (tap = copy)
        send_doc(chat, key.encode(), fname, f"Ключ файлом — открой его в Amnezia. Или текстом (тап = копировать):\n<code>{esc(key)}</code>")

def card_clash(chat, name):
    d = CLIENTS / name; f = d / "clash.txt"
    if not f.exists(): return send(chat, f"У {esc(name)} нет Clash-профиля — запусти add-client.sh {esc(name)} на ноде")
    link = f.read_text().strip()
    cap = (f"🧩 <b>{BRAND}</b> · {esc(name)}\n"
           f"ClashMi, всё в одном · одна подписка: все серверы, обычный протокол и AmneziaWG, авто-переключение Россия → Амстердам → запасной, российские сайты напрямую.\n\n"
           f"1. Скачай ClashMi — кнопки ниже\n2. ClashMi → «+» → профиль по ссылке: вставь ссылку (кнопка копирует) или сканируй QR\n"
           f"3. Выбери профиль {BRAND}, оставь группу «⚡ Авто», включи. Готово.\n\n<code>{esc(link)}</code>")
    send_photo(chat, d / "clash.png", cap, [CLASH_DL, [copy("📋 Скопировать ссылку", link)]])

def card_happ(chat, name):
    d = CLIENTS / name; f = d / "sub.txt"
    if not f.exists(): return send(chat, f"У {esc(name)} нет подписки — запусти add-client.sh {esc(name)} на ноде")
    link = f.read_text().strip()
    cap = (f"🔗 <b>{BRAND}</b> · {esc(name)}\n"
           f"Подписка Happ · все наши серверы одной ссылкой: вход через Россию, Амстердам, запасной. Happ сам держится за живой. AmneziaWG сюда не входит.\n\n"
           f"1. Скачай Happ — кнопки ниже\n2. Happ → «+» → «Сканировать QR» или вставь ссылку\n3. Включи авто-выбор сервера. Готово.\n\n<code>{esc(link)}</code>")
    send_photo(chat, d / "sub.png", cap, [HAPP_DL, [copy("📋 Скопировать ссылку", link)]])

# ── screens (menus that edit themselves) ──────────────────────────────────────
def users_view():
    us = sorted(users(), key=lambda x: x["name"].lower()); on = online_set()
    rows = [[btn(f"{u['name']}{' 🟢' if u['name'] in on else ''}", f"u:{u['name']}") for u in us[i:i + 2]] for i in range(0, len(us), 2)]
    rows.append([btn("➕ Добавить", "add"), btn("📊 Статус", "status")])
    return f"👥 <b>{BRAND}</b> · {plural(len(us), 'человек', 'человека', 'человек')}\nНажми на имя.", rows

def users_screen(chat, mid=None):
    text, kb = users_view()
    return edit(chat, mid, text, kb) if mid else send(chat, text, kb)

def panel(chat, mid, name):
    d = CLIENTS / name; on = online_set(); awg = awg_last_seen().get(name)
    bits = ["в сети сейчас" if name in on else "не в сети"]
    if awg is not None: bits.append(f"AmneziaWG {ago(awg)}")
    stamps = [p.stat().st_mtime for p in d.glob("*.txt")] if d.exists() else []
    if stamps: bits.append(f"ключи от {rudate(max(stamps))}")
    rows = [[]]
    for v in ("ru", "nl", "awg", "x2", "clash", "happ"):
        if not variant_file(name, v).exists(): continue
        if len(rows[-1]) == 2: rows.append([])
        rows[-1].append(btn(PANEL_BTN[v], f"k:{name}:{v}"))
    if len(rows[-1]) == 2: rows.append([])
    rows[-1].append(btn("🗑 Удалить", f"d:{name}")); rows.append([btn("← Люди", "users")])
    edit(chat, mid, f"👤 <b>{esc(name)}</b>\n{' · '.join(bits)}", rows)

SEV = {"xhttp": "🔴", "egress4": "🔴", "cert3": "🔴", "disk95": "🔴", "bal3": "🔴",
       "tcp": "🟠", "awg": "🟠", "nginx": "🟠", "mem": "🟠", "ru": "🟠"}   # everything else 🟡
SITE = {"www.youtube.com": "YouTube", "www.google.com": "Google", "www.instagram.com": "Instagram", "web.telegram.org": "Telegram",
        "chatgpt.com": "ChatGPT", "x.com": "X", "www.facebook.com": "Facebook", "discord.com": "Discord", "www.tiktok.com": "TikTok",
        "web.whatsapp.com": "WhatsApp", "github.com": "GitHub", "www.netflix.com": "Netflix"}

def status_view():
    """Text + keyboard from health.json (vpn-health.sh, every minute) + who is online right now."""
    try: h = json.loads((STATE / "health.json").read_text())
    except Exception:
        return f"📊 <b>{BRAND}</b>\nНет данных: vpn-health.sh ещё не отработал. Нажми «Проверить сейчас».", [[btn("🔄 Проверить сейчас", "recheck")]]
    c = h.get("checks", {}); now = int(time.time())
    def ok(k): return c.get(k, {}).get("ok", True)
    def m(k): return "🟢" if ok(k) else SEV.get(k, "🟡")
    failing = [k for k, v in c.items() if not v.get("ok")]
    since = {f["key"]: f["since"] for f in h.get("failing", [])}
    L = [f"📊 <b>{BRAND}</b> · " + ("всё в порядке" if not failing else plural(len(failing), "проблема", "проблемы", "проблем")),
         f"Проверено {hhmm(h['ts'])}" + (f" ({ago(now - h['ts'])})" if now - h["ts"] > 150 else "") + f" · инцидентов за сутки: {h.get('incidents_24h', 0)}", ""]
    for k in failing:
        s = since.get(k); L.append(f"{SEV.get(k, '🟡')} {esc(c[k]['title'].capitalize())}: {esc(c[k]['detail'])}" + (f", с {hhmm(s)}" if s else ""))
    if failing: L.append("")
    L += [f"{h.get('flag', '🇳🇱')} <b>{esc(h.get('node', 'Амстердам'))}</b> · {esc(h.get('ip', ''))}",
          f"{m('xhttp')} Основной :443  {m('tcp')} Резерв :{h.get('tcp_port', 8443)}  {m('awg')} AmneziaWG  {m('nginx')} nginx",
          f"{m('egress4')} Интернет IPv4" + (f" · {m('egress6')} IPv6{'' if ok('egress6') else ' не выходит'}" if "egress6" in c else "")]
    cert = h.get("cert", {})
    L.append(f"{'🔒' if ok('cert14') and ok('selfsigned') else '🟡'} Сертификат {esc(cert.get('issuer', '?'))} · ещё {plural(cert.get('days', 0), 'день', 'дня', 'дней')}")
    L.append(f"💾 Диск {h.get('disk_pct', '?')}% · 🧠 Память {h.get('mem_pct', '?')}% · ⏱ {dur(h.get('uptime_s', 0))} без перезагрузки")
    if h.get("ru_host"): L += ["", f"🇷🇺 <b>Москва</b> · {esc(h['ru_host'])}", f"{m('ru')} Вход :443 → {esc(h.get('node', 'Амстердам'))}"]
    if h.get("exit2_host"): L += ["", f"🛟 <b>Запасной</b> · {esc(h.get('exit2_domain') or h['exit2_host'])}", f"{m('exit2')} :443"]
    s = h.get("sites", {"total": 0, "down": []}); down = s.get("down", [])
    L += ["", f"🌐 Сайты: {s['total'] - len(down)} из {s['total']} открываются" + (f" · не отвечают: {esc(', '.join(SITE.get(x, x) for x in down))}" if down else "")]
    bal = h.get("balance", {})
    if bal.get("rub"): L.append(f"💳 Timeweb: {esc(bal['rub'])} ₽" + (f", хватит на {plural(bal['days'], 'день', 'дня', 'дней')}" if bal.get("days") else ""))
    on = sorted(online_set()); L.append(f"👥 В сети: {', '.join(esc(n) for n in on) if on else 'никого'}")
    ev = recent_incidents(3)
    if ev: L += [""] + ev
    return "\n".join(L), [[btn("🔄 Проверить сейчас", "recheck"), btn("👥 Люди", "users")]]

def recent_incidents(n):
    try: lines = (STATE / "incidents.log").read_text().splitlines()[-n:]
    except Exception: return []
    out = []
    for ln in reversed(lines):
        p = ln.split("\t")
        if len(p) < 5: continue
        s, e, sev, _, title = int(p[0]), int(p[1]), p[2], p[3], p[4]
        out.append(f"🕓 {hhmm(s)} {'🔴' if sev == 'crit' else '🟠' if sev == 'warn' else '🟡'} {esc(title)}, {dur(e - s)}")
    return out

def status_screen(chat, mid=None):
    text, kb = status_view()
    return edit(chat, mid, text, kb) if mid else send(chat, text, kb)

# ── actions ───────────────────────────────────────────────────────────────────
ASK_NAME = "➕ Имя нового человека?\nЛатиницей, цифры, «-» и «_», до 32 символов."

def ask_name(chat, mid=None):
    kb = [[btn("Отмена", "cancel")]]
    if mid: edit(chat, mid, ASK_NAME, kb)
    else: mid = send(chat, ASK_NAME, kb)
    PENDING_ADD[chat] = (time.time() + 300, mid)

def do_add(chat, name):
    if not NAME_RE.match(name): return send(chat, "Имя латиницей: буквы, цифры, «-» и «_», до 32 символов. Например: /add Egor")
    if find_user(name):
        mid = send(chat, f"{esc(name)} уже есть — вот панель"); return panel(chat, mid, find_user(name))
    mid = send(chat, f"⏳ Создаю {esc(name)} на всех нодах…")
    rc, out = sh(["/usr/local/bin/add-client.sh", name])
    if rc != 0: return edit(chat, mid, f"❌ Не удалось создать {esc(name)}\n<pre>{esc(tail(out))}</pre>")
    edit(chat, mid, f"✅ {esc(name)} создан · {plural(len(users()), 'человек', 'человека', 'человек')}")
    card(chat, name)

def ask_del(chat, name, mid=None):
    text = f"🗑 Удалить {esc(name)}?\nКлючи перестанут работать сразу, файлы удалятся."
    kb = [[btn("Да, удалить", f"dy:{name}"), btn("Отмена", "cancel")]]
    return edit(chat, mid, text, kb) if mid else send(chat, text, kb)

def do_del(chat, mid, name):
    edit(chat, mid, f"⏳ Удаляю {esc(name)}…")
    rc, out = sh(["/usr/local/bin/del-client.sh", name])
    if rc != 0: return edit(chat, mid, f"❌ Не удалось удалить {esc(name)}\n<pre>{esc(tail(out))}</pre>")
    edit(chat, mid, f"🗑 {esc(name)} удалён · {plural(len(users()), 'человек', 'человека', 'человек')}", [[btn("← Люди", "users")]])

def ask_newip(chat):
    if not ENV.get("TW_API_TOKEN") or not ENV.get("TW_SERVER_ID"):
        return send(chat, "🔄 Смена IP не настроена: добавь TW_API_TOKEN и TW_SERVER_ID в /etc/safechill/vpn.env")
    ip = env_now().get("SERVER_IP", "")
    send(chat, f"🔄 Сменить IPv4 Амстердама?\nСейчас {esc(ip)}. Timeweb выдаст новый адрес (+200 ₽/мес, пока старый не отпущен).\n"
               f"DNS, вход через Россию и подписки обновятся сами.\nПрямые ключи Amnezia («Амстердам», AmneziaWG) у всех перестанут работать — нужно разослать новые.",
         [[btn("Да, менять", "newip:go"), btn("Отмена", "cancel")]])

def do_newip(chat, mid):
    old = env_now().get("SERVER_IP", "")
    edit(chat, mid, "⏳ Меняю IP… 1–2 минуты")
    rc, out = sh(["/usr/local/bin/rotate-ip.sh"], timeout=600)
    new = env_now().get("SERVER_IP", "")
    if rc != 0 or not new or new == old:
        return edit(chat, mid, f"❌ Timeweb не выдал новый IP\n<pre>{esc(tail(out, 10))}</pre>\nЧаще всего: баланс меньше месячной стоимости всех ресурсов + 200 ₽.")
    warn = [l for l in out.splitlines() if l.startswith("!")]
    edit(chat, mid, f"✅ Новый IP Амстердама: {esc(new)} (был {esc(old)})\nПодписки Happ и ClashMi обновятся сами. Прямые ключи Amnezia: разошли новые из /users.\n"
                    f"Старый IP отпусти ночью: /dropip {esc(old)} — сервер перезагрузится на ~5 мин."
                    + (f"\n⚠️ <pre>{esc(chr(10).join(warn)[-600:])}</pre>" if warn else ""), [[btn("👥 Люди", "users")]])

def ask_dropip(chat, ip):
    if not IP_RE.match(ip): return send(chat, "/dropip 1.2.3.4")
    if ip == env_now().get("SERVER_IP"): return send(chat, f"⛔ {esc(ip)} — адрес, к которому подключаются люди. Сначала /newip.")
    send(chat, f"🗑 Отпустить IP {esc(ip)}?\nTimeweb перезагрузит сервер, VPN ляжет на ~5 минут. Экономия 200 ₽/мес.",
         [[btn("Да, отпустить", f"dropip:{ip}"), btn("Отмена", "cancel")]])

def do_dropip(chat, mid, ip):
    edit(chat, mid, f"⏳ Отпускаю {esc(ip)}…")
    rc, out = sh(["/usr/local/bin/drop-ip.sh", ip], timeout=120)
    edit(chat, mid, f"✅ IP {esc(ip)} отпущен. Сервер перезагружается, вернусь через ~5 мин." if rc == 0
                    else f"❌ Не удалось отпустить {esc(ip)}\n<pre>{esc(tail(out, 10))}</pre>")

def do_recheck(chat, mid):
    edit(chat, mid, "⏳ Проверяю все ноды… ~20 секунд")
    try: sh(["/usr/local/bin/vpn-health.sh"], timeout=150)
    except subprocess.TimeoutExpired: pass
    status_screen(chat, mid)

HELP = (f"<b>{BRAND}</b> · команды\n"
        "👥 /users — люди и их ключи\n➕ /add Имя — новый человек\n📊 /status — ноды, сайты, сертификат\n"
        "🔄 /newip — новый IPv4 Амстердама, если заблокировали\n🗑 /del Имя — удалить человека\n\n"
        "Редко:\n/qr Имя [ru|nl|awg|x2|happ|clash] — карточка напрямую\n/dropip 1.2.3.4 — отпустить лишний IP (перезагрузка сервера)")

# ── dispatch ──────────────────────────────────────────────────────────────────
def on_message(msg):
    chat = msg["chat"]["id"]; frm = (msg.get("from") or {}).get("id"); text = (msg.get("text") or "").strip()
    if not text: return
    if frm not in ADMINS:
        if text.startswith("/"): send(chat, "⛔ Команды бота доступны только администратору.")
        return
    if not ENV.get("TG_CHAT_ID") and not (ETC / "tg_chat_id").exists():   # bind alerts to the first chat an admin uses
        (ETC / "tg_chat_id").write_text(str(chat)); log("alerts bound to chat", chat)
    if not text.startswith("/"):
        p = PENDING_ADD.pop(chat, None)
        if p and p[0] > time.time():
            edit(chat, p[1], f"➕ {esc(text.split()[0])}"); do_add(chat, text.split()[0])
        return
    parts = text.split(); cmd = parts[0].split("@")[0].lower(); args = parts[1:]
    log("cmd", cmd, args, "from", frm)
    try:
        if cmd in ("/users", "/list", "/start"): users_screen(chat)
        elif cmd == "/add":
            if args: do_add(chat, args[0])
            else: ask_name(chat)
        elif cmd == "/qr":
            if not args: return send(chat, "/qr Имя [ru|nl|awg|x2|happ|clash]")
            name = find_user(args[0]); v = args[1].lower() if len(args) > 1 else None
            if not name: return send(chat, f"Нет такого человека: {esc(args[0])}. Список — /users")
            card(chat, name, v if (v in VARIANTS or v in ("happ", "clash")) else None)
        elif cmd == "/del":
            name = find_user(args[0]) if args else None
            if not name: return send(chat, f"Нет такого человека: {esc(args[0])}. Список — /users" if args else "/del Имя")
            ask_del(chat, name)
        elif cmd == "/status": status_screen(chat)
        elif cmd == "/newip": ask_newip(chat)
        elif cmd == "/dropip":
            if not args: return send(chat, "/dropip 1.2.3.4")
            ask_dropip(chat, args[0])
        elif cmd == "/help": send(chat, HELP)
        else: send(chat, "Не знаю такой команды. Список — /help")
    except Exception as e:  # noqa: BLE001
        log("error", repr(e)); send(chat, f"❌ Ошибка бота\n<pre>{esc(str(e)[:500])}</pre>")

def on_callback(cq):
    cid = cq["id"]; m = cq.get("message") or {}; chat = m.get("chat", {}).get("id"); mid = m.get("message_id")
    frm = (cq.get("from") or {}).get("id"); data = cq.get("data", "")
    if frm not in ADMINS or chat is None: return toast(cid, "Только для администратора")
    log("cb", data, "from", frm)
    kind, _, rest = data.partition(":")
    try:
        if kind == "u": toast(cid); panel(chat, mid, rest)
        elif kind == "k":
            name, _, v = rest.partition(":"); toast(cid, "Готовлю карточку…"); card(chat, name, v if (v in VARIANTS or v in ("happ", "clash")) else None)
        elif kind == "d": toast(cid); ask_del(chat, rest, mid)
        elif kind == "dy": toast(cid); do_del(chat, mid, rest)
        elif kind == "add": toast(cid); ask_name(chat, mid)
        elif kind == "cancel": toast(cid); PENDING_ADD.pop(chat, None); edit(chat, mid, "Отменено")
        elif kind == "users": toast(cid); users_screen(chat, mid)
        elif kind == "status": toast(cid); status_screen(chat, mid)
        elif kind == "recheck": toast(cid, "Проверяю…"); do_recheck(chat, mid)
        elif kind == "newip" and rest == "go": toast(cid); do_newip(chat, mid)
        elif kind == "dropip": toast(cid); do_dropip(chat, mid, rest)
        else: toast(cid)
    except Exception as e:  # noqa: BLE001
        log("error", repr(e)); send(chat, f"❌ Ошибка бота\n<pre>{esc(str(e)[:500])}</pre>")

def main():
    if not TOKEN: sys.exit("TG_BOT_TOKEN missing in /etc/safechill/vpn.env")
    if len(sys.argv) >= 4 and sys.argv[1] == "--card":
        name = find_user(sys.argv[2]) or sys.exit(f"unknown user {sys.argv[2]}")
        card(int(sys.argv[3]), name, sys.argv[4] if len(sys.argv) > 4 else None); return
    if len(sys.argv) >= 3 and sys.argv[1] == "--status": status_screen(int(sys.argv[2])); return
    try:
        api("setMyCommands", commands=[{"command": "users", "description": "Люди и их ключи"}, {"command": "add", "description": "Добавить человека"},
                                       {"command": "status", "description": "Ноды, сайты, сертификат"}, {"command": "newip", "description": "Новый IPv4 Амстердама"},
                                       {"command": "del", "description": "Удалить человека"}, {"command": "help", "description": "Справка"}])
    except Exception: pass
    off_file = STATE / "bot.offset"; offset = int(off_file.read_text()) if off_file.exists() else 0
    log("bot started, admins", ADMINS)
    while True:
        try:
            res = api("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1; off_file.write_text(str(offset))
                if "message" in upd: on_message(upd["message"])
                elif "callback_query" in upd: on_callback(upd["callback_query"])
        except KeyboardInterrupt: return
        except Exception as e:  # noqa: BLE001
            log("loop error:", e); time.sleep(5)

if __name__ == "__main__":
    main()
