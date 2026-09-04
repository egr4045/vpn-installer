#!/usr/bin/env python3
"""safechill-bot — Telegram admin bot for the exit node (stdlib only, long polling).

One command, one live message. /users (also /start) opens the home screen: people as buttons, health
summary in its first line, who is online, plus "Добавить" and "Подробно". Tapping a person replaces the
home message with a single card — photo (QR) whose caption carries the steps and the key/link itself,
download buttons, and buttons for the other profiles; switching a profile edits that same message in
place (editMessageMedia), so nothing piles up in the chat. "← Люди" brings the home screen back.

Default profile is ClashMi (one subscription: every server, auto-switching Россия → Амстердам →
запасной, Russian sites direct); AmneziaVPN keys are the alternatives. Money is never shown.
Vocabulary shared with the health scripts: 🇳🇱 Амстердам · 🇷🇺 Москва · 🛟 Запасной; 🔴🟠🟡🟢🔵.
Admins only (TG_ADMIN_IDS in /etc/safechill/vpn.env); anyone else gets a toast, not a chat message.

Commands: /users (the only advertised one). Hidden: /add /del /qr /status /newip /dropip /help
CLI:      safechill-bot.py --card <name> <chat_id> [clash|ru|nl|awg|x2]   safechill-bot.py --status <chat_id>
"""
import json, re, socket, subprocess, sys, time, urllib.request, urllib.error, uuid, html, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

# api.telegram.org resolves to IPv6 first and this host's IPv6 path to it stalls on roughly one request in
# five — a stalled long poll leaves the bot blind to taps until the socket times out, which is what made it
# feel frozen for minutes. This process only talks to Telegram and GitHub, so pin it to IPv4 wholesale.
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, port, family=0, *a, **kw: _getaddrinfo(host, port, socket.AF_INET, *a, **kw)

ETC = pathlib.Path("/etc/safechill"); CLIENTS = pathlib.Path("/root/clients")
STATE = pathlib.Path("/var/lib/safechill"); STATE.mkdir(parents=True, exist_ok=True)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$"); IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
TAG_RE = re.compile(r"<[^>]+>")
AMNEZIA_LATEST = "https://github.com/amnezia-vpn/amnezia-client/releases/latest"
AMNEZIA_IOS = "https://apps.apple.com/us/app/amneziavpn/id1600529900"
AMNEZIA_ANDROID = "https://play.google.com/store/apps/details?id=org.amnezia.vpn"
CLASHMI_IOS = "https://apps.apple.com/us/app/clash-mi/id6744321968"
CLASHMI_ALL = "https://github.com/KaringX/clashmi/releases/latest"
CLASHMI_SITE = "https://clashmi.app/download"
MSK = ZoneInfo("Europe/Moscow")
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()

def load_env(p):
    env = {}
    for line in pathlib.Path(p).read_text(encoding="utf-8").splitlines():
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
_ONLINE = (0.0, set())   # cheap cache: reading xray's journal takes seconds, the home screen must feel instant

def log(*a): print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)
def esc(s): return html.escape(str(s), quote=False)
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
    if sec < 86400: return f"{sec // 3600} ч" + (f" {(sec % 3600) // 60} мин" if sec % 3600 >= 60 else "")
    return f"{sec // 86400} дн." + (f" {(sec % 86400) // 3600} ч" if sec % 86400 >= 3600 else "")
def ago(sec):
    if sec is None: return ""
    if sec < 120: return "только что"
    if sec < 3600: return f"{sec // 60} мин назад"
    if sec < 86400: return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн. назад"
def tail(out, n=8): return "\n".join(out.strip().splitlines()[-n:])[-1500:]
def cap1(s): return s[:1].upper() + s[1:]   # .capitalize() would lowercase the rest ("вход через россию")
def vis(text):  # Telegram counts UTF-16 units, so an emoji costs 2 — measure what it measures
    return len(TAG_RE.sub("", html.unescape(text)).encode("utf-16-le")) // 2

# ── Telegram API ──────────────────────────────────────────────────────────────
POLL = 25          # long-poll seconds: the shorter it is, the smaller the blind window if a socket dies
SOCK = POLL + 15

def api(method, _timeout=25, **params):
    req = urllib.request.Request(API + method, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_timeout) as r: return json.load(r)

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

def cap_clip(text):
    """Captions are limited to 1024 units of visible text; slicing the HTML instead would cut a tag in half
    and Telegram answers 'Can't find end tag'. Content is built to fit, so this only guards the odd case."""
    if vis(text) <= 1024: return text
    return TAG_RE.sub("", text)[:1020] + "…"

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

def drop(chat, mid):
    if not mid: return
    try: api("deleteMessage", chat_id=chat, message_id=mid)
    except Exception: pass

def toast(cid, text="", alert=False):
    try: api("answerCallbackQuery", callback_query_id=cid, text=text, show_alert=alert)
    except Exception: pass

# QR images never change unless a key is reissued, and Telegram hands back a file_id for anything uploaded
# once. Re-using it turns every later card into a plain JSON call instead of an upload.
PHOTOS = STATE / "photo_ids.json"
def _photo_key(p): s = p.stat(); return f"{p}:{s.st_mtime_ns}:{s.st_size}"
def _photo_ids():
    try: return json.loads(PHOTOS.read_text(encoding="utf-8"))
    except Exception: return {}
def _photo_remember(p, result):
    sizes = (result.get("result") or {}).get("photo") or []
    if not sizes: return
    ids = _photo_ids(); ids[_photo_key(p)] = sizes[-1]["file_id"]
    try: PHOTOS.write_text(json.dumps(ids), encoding="utf-8")
    except Exception: pass

def send_photo(chat, path, caption, kb=None):
    p = pathlib.Path(path); cap = cap_clip(caption); markup = {"inline_keyboard": kb} if kb else None
    fid = _photo_ids().get(_photo_key(p))
    if fid:
        try: return api("sendPhoto", chat_id=chat, photo=fid, caption=cap, parse_mode="HTML", reply_markup=markup)["result"]["message_id"]
        except urllib.error.HTTPError: pass          # file_id went stale — fall through and upload again
    f = {"chat_id": str(chat), "caption": cap, "parse_mode": "HTML"}
    if kb: f["reply_markup"] = json.dumps(markup)
    r = api_multipart("sendPhoto", f, {"photo": (p.name, p.read_bytes(), "image/png")})
    _photo_remember(p, r); return r["result"]["message_id"]

def edit_photo(chat, mid, path, caption, kb=None):
    """Replace the photo, caption and buttons of an existing card — one message morphs between profiles."""
    p = pathlib.Path(path); cap = cap_clip(caption); markup = {"inline_keyboard": kb} if kb else None
    fid = _photo_ids().get(_photo_key(p))
    if fid:
        try: return api("editMessageMedia", chat_id=chat, message_id=mid, reply_markup=markup,
                        media={"type": "photo", "media": fid, "caption": cap, "parse_mode": "HTML"})
        except urllib.error.HTTPError: pass
    f = {"chat_id": str(chat), "message_id": str(mid),
         "media": json.dumps({"type": "photo", "media": "attach://photo", "caption": cap, "parse_mode": "HTML"})}
    if kb: f["reply_markup"] = json.dumps(markup)
    r = api_multipart("editMessageMedia", f, {"photo": (p.name, p.read_bytes(), "image/png")})
    _photo_remember(p, r); return r

def send_doc(chat, blob, fname, caption):
    return api_multipart("sendDocument", {"chat_id": str(chat), "caption": cap_clip(caption), "parse_mode": "HTML"},
                         {"document": (fname, blob, "application/octet-stream")})

# ── data ──────────────────────────────────────────────────────────────────────
def users(): return json.loads((ETC / "users.json").read_text(encoding="utf-8"))

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
        for line in subprocess.run(["awg", "show", "awg0", "latest-handshakes"], capture_output=True, text=True, timeout=8).stdout.splitlines():
            pub, ts = line.split(); n = pub2name.get(pub); ts = int(ts)
            if n: out[n] = (int(time.time()) - ts) if ts else None
    except Exception: pass
    return out

def online_set():
    """Who used xray or AmneziaWG in the last 5 minutes. Cached: the journal read is the slow part."""
    global _ONLINE
    if time.time() - _ONLINE[0] < 45: return _ONLINE[1]
    names = set()
    try:
        txt = subprocess.run(["journalctl", "-u", "xray", "--since", "-5min", "-n", "400", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=8).stdout
        names |= set(re.findall(r"email: (\S+)", txt))
    except Exception: pass
    names |= {n for n, s in awg_last_seen().items() if s is not None and s < 180}
    _ONLINE = (time.time(), names)
    return names

def amnezia_latest():
    cache = STATE / "amnezia.latest"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600: return cache.read_text(encoding="utf-8").strip()
        req = urllib.request.Request("https://api.github.com/repos/amnezia-vpn/amnezia-client/releases/latest", headers={"User-Agent": "safechill-bot"})
        with urllib.request.urlopen(req, timeout=10) as r: tag = json.load(r).get("tag_name", "")
        cache.write_text(tag, encoding="utf-8"); return tag
    except Exception:
        return cache.read_text(encoding="utf-8").strip() if cache.exists() else ""

def sh(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()

def health():
    try: return json.loads((STATE / "health.json").read_text(encoding="utf-8"))
    except Exception: return {}

# ── one card per person: photo (QR) + everything needed in its caption ────────
PROFILES = ["clash", "ru", "nl", "awg", "x2"]        # order = what people should try first
FILES = {"clash": "clash", "ru": "amnezia-ru", "nl": "amnezia", "awg": "amnezia-awg", "x2": "amnezia-x2"}
BTNS = {"clash": "🧩 ClashMi", "ru": "🔑 Amnezia · Россия", "nl": "🔑 Amnezia · Амстердам",
        "awg": "⚡ AmneziaWG", "x2": "🛟 Amnezia · запасной"}
LINE2 = {"ru":  "AmneziaVPN · через Россию, выход в Амстердаме.",   # every character here is one the ~860-char
         "nl":  "AmneziaVPN · Амстердам напрямую.",                 # key needs to stay inside the 1024 caption
         "awg": "AmneziaWG · быстро дома, на мобильном не всегда.",
         "x2":  "AmneziaVPN · запасной выход напрямую."}
DL = {"clash": [url("📲 iPhone", CLASHMI_IOS), url("🤖 Android · 💻 ПК", CLASHMI_ALL), url("🌐 Сайт", CLASHMI_SITE)],
      "amnezia": [url("📲 iPhone", AMNEZIA_IOS), url("🤖 Android", AMNEZIA_ANDROID), url("💻 Windows / Mac", AMNEZIA_LATEST)]}

def have(name, v): return (CLIENTS / name / f"{FILES[v]}.txt").exists()
def default_profile(name): return next((v for v in PROFILES if have(name, v)), "clash")

def card_content(name, v):
    """(png path, caption, keyboard, key-to-attach-or-None) for one profile."""
    d = CLIENTS / name; body = (d / f"{FILES[v]}.txt").read_text(encoding="utf-8").strip()
    if v == "clash":
        cap = (f"🧩 <b>{BRAND}</b> · {esc(name)}\n"
               f"ClashMi — всё в одном: все серверы, авто-переключение Россия → Амстердам → запасной, российские сайты напрямую.\n\n"
               f"1. Скачай ClashMi — кнопки ниже\n2. «+» → по ссылке: сканируй QR или вставь ссылку (кнопка ниже копирует)\n"
               f"3. Группа «⚡ Авто», включи. Готово.\n\n<code>{esc(body)}</code>")
        rows = [DL["clash"], [copy("📋 Скопировать ссылку", body)]]
        attach = None
    else:
        head = (f"🔑 <b>{BRAND}</b> · {esc(name)}\n{LINE2[v]}\n\n"
                f"1. Поставь AmneziaVPN — кнопки ниже\n"
                f"2. «+» → «Подключиться по ключу» → QR\n3. Включи. Готово.")
        if vis(head) + vis(body) + 2 <= 1024:   # the key must fit beside the steps, otherwise it goes into a file
            cap, attach = f"{head}\n\n<code>{esc(body)}</code>", None
        else:
            cap, attach = f"{head}\n\nКлюч длинный — он в файле следующим сообщением.", body
        rows = [DL["amnezia"]]
    alts = [btn(BTNS[p], f"k:{name}:{p}") for p in PROFILES if p != v and have(name, p)]
    rows += [alts[i:i + 2] for i in range(0, len(alts), 2)]
    rows.append([btn("🗑 Удалить", f"d:{name}"), btn("← Люди", "home")])
    return d / f"{FILES[v]}.png", cap, rows, attach

def card(chat, name, v=None, mid=None):
    """Send the card as one message, or morph an existing card into another profile."""
    v = v if (v in PROFILES and have(name, v)) else default_profile(name)
    if not have(name, v):
        return send(chat, f"У {esc(name)} нет профилей — запусти add-client.sh {esc(name)} на ноде", [[btn("← Люди", "home")]])
    png, cap, rows, attach = card_content(name, v)
    if mid: edit_photo(chat, mid, png, cap, rows)
    else: mid = send_photo(chat, png, cap, rows)
    if attach: send_doc(chat, attach.encode(), f"{BRAND}-{name}-{v}.vpn", "Ключ файлом — открой его в AmneziaVPN.")
    return mid

# ── home screen: people, health and adding, all in one message ───────────────
SEV = {"xhttp": "🔴", "egress4": "🔴", "cert3": "🔴", "disk95": "🔴",
       "tcp": "🟠", "awg": "🟠", "nginx": "🟠", "mem": "🟠", "ru": "🟠"}   # everything else 🟡
SITE = {"www.youtube.com": "YouTube", "www.google.com": "Google", "www.instagram.com": "Instagram", "web.telegram.org": "Telegram",
        "chatgpt.com": "ChatGPT", "x.com": "X", "www.facebook.com": "Facebook", "discord.com": "Discord", "www.tiktok.com": "TikTok",
        "web.whatsapp.com": "WhatsApp", "github.com": "GitHub", "www.netflix.com": "Netflix"}

def health_line(h):
    if not h: return "🟡 Проверок ещё не было"
    c = h.get("checks", {}); bad = [k for k, x in c.items() if not x.get("ok")]
    since = {f["key"]: f["since"] for f in h.get("failing", [])}
    if not bad: return f"🟢 Всё работает · проверено {hhmm(h['ts'])}"
    k = sorted(bad, key=lambda x: ("🔴🟠🟡".index(SEV.get(x, "🟡"))))[0]
    s = since.get(k); rest = f" (+{len(bad) - 1})" if len(bad) > 1 else ""
    return f"{SEV.get(k, '🟡')} {esc(cap1(c[k]['title']))}{rest}" + (f", с {hhmm(s)}" if s else "")

def home_view():
    us = sorted(users(), key=lambda x: x["name"].lower()); on = online_set(); h = health()
    L = [f"👥 <b>{BRAND}</b> · {plural(len(us), 'человек', 'человека', 'человек')}", health_line(h)]
    live = [u["name"] for u in us if u["name"] in on]
    L.append(f"🟢 В сети: {', '.join(esc(n) for n in live)}" if live else "Сейчас никто не подключён")
    rows = [[btn(f"{u['name']}{' 🟢' if u['name'] in on else ''}", f"u:{u['name']}") for u in us[i:i + 2]] for i in range(0, len(us), 2)]
    rows.append([btn("➕ Добавить", "add"), btn("📊 Подробно", "status")])
    return "\n".join(L), rows

def home(chat, mid=None):
    text, kb = home_view()
    if mid: edit(chat, mid, text, kb); return mid
    return send(chat, text, kb)

def status_view():
    h = health()
    if not h: return f"📊 <b>{BRAND}</b>\nПроверок ещё не было — нажми «Проверить сейчас».", [[btn("🔄 Проверить сейчас", "recheck"), btn("← Люди", "home")]]
    c = h.get("checks", {}); now = int(time.time())
    def ok(k): return c.get(k, {}).get("ok", True)
    def m(k): return "🟢" if ok(k) else SEV.get(k, "🟡")
    bad = [k for k, v in c.items() if not v.get("ok")]; since = {f["key"]: f["since"] for f in h.get("failing", [])}
    L = [f"📊 <b>{BRAND}</b> · " + ("всё в порядке" if not bad else plural(len(bad), "проблема", "проблемы", "проблем")),
         f"Проверено {hhmm(h['ts'])}" + (f" ({ago(now - h['ts'])})" if now - h["ts"] > 150 else "") + f" · инцидентов за сутки: {h.get('incidents_24h', 0)}", ""]
    for k in bad:
        s = since.get(k); L.append(f"{SEV.get(k, '🟡')} {esc(cap1(c[k]['title']))}: {esc(c[k]['detail'])}" + (f", с {hhmm(s)}" if s else ""))
    if bad: L.append("")
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
    on = sorted(online_set()); L.append(f"👥 В сети: {', '.join(esc(n) for n in on) if on else 'никого'}")
    ev = recent_incidents(3)
    if ev: L += [""] + ev
    return "\n".join(L), [[btn("🔄 Проверить сейчас", "recheck"), btn("🔄 Сменить IP", "newip")], [btn("← Люди", "home")]]

def recent_incidents(n):
    try: lines = (STATE / "incidents.log").read_text(encoding="utf-8").splitlines()[-n:]
    except Exception: return []
    out = []
    for ln in reversed(lines):
        p = ln.split("\t")
        if len(p) < 5: continue
        s, e, sev, _, title = int(p[0]), int(p[1]), p[2], p[3], p[4]
        out.append(f"🕓 {hhmm(s)} {'🔴' if sev == 'crit' else '🟠' if sev == 'warn' else '🟡'} {esc(cap1(title))}, {dur(e - s)}")
    return out

def status(chat, mid=None):
    text, kb = status_view()
    if mid: edit(chat, mid, text, kb); return mid
    return send(chat, text, kb)

# ── actions ───────────────────────────────────────────────────────────────────
ASK_NAME = "➕ Имя нового человека?\nЛатиницей, цифры, «-» и «_», до 32 символов."

def ask_name(chat, mid=None):
    kb = [[btn("← Люди", "home")]]
    mid = (edit(chat, mid, ASK_NAME, kb) or mid) if mid else send(chat, ASK_NAME, kb)
    PENDING_ADD[chat] = (time.time() + 300, mid)

def do_add(chat, name, mid=None):
    if not NAME_RE.match(name):
        return send(chat, "Имя латиницей: буквы, цифры, «-» и «_», до 32 символов.", [[btn("← Люди", "home")]])
    if find_user(name):
        drop(chat, mid); return card(chat, find_user(name))
    if mid: edit(chat, mid, f"⏳ Создаю {esc(name)} на всех нодах…")
    else: mid = send(chat, f"⏳ Создаю {esc(name)} на всех нодах…")
    rc, out = sh(["/usr/local/bin/add-client.sh", name])
    if rc != 0: return edit(chat, mid, f"❌ Не удалось создать {esc(name)}\n<pre>{esc(tail(out))}</pre>", [[btn("← Люди", "home")]])
    drop(chat, mid); card(chat, name)

def ask_del(chat, mid, name):
    edit(chat, mid, f"🗑 Удалить {esc(name)}?\nКлючи перестанут работать сразу, файлы удалятся.",
         [[btn("Да, удалить", f"dy:{name}"), btn("Отмена", f"u:{name}")]])

def do_del(chat, mid, name):
    edit(chat, mid, f"⏳ Удаляю {esc(name)}…")
    rc, out = sh(["/usr/local/bin/del-client.sh", name])
    if rc != 0: return edit(chat, mid, f"❌ Не удалось удалить {esc(name)}\n<pre>{esc(tail(out))}</pre>", [[btn("← Люди", "home")]])
    text, kb = home_view()
    edit(chat, mid, f"🗑 {esc(name)} удалён\n\n{text}", kb)

def ask_newip(chat, mid):
    if not ENV.get("TW_API_TOKEN") or not ENV.get("TW_SERVER_ID"):
        return edit(chat, mid, "🔄 Смена IP не настроена: добавь TW_API_TOKEN и TW_SERVER_ID в /etc/safechill/vpn.env", [[btn("← Люди", "home")]])
    ip = env_now().get("SERVER_IP", "")
    edit(chat, mid, f"🔄 Сменить IPv4 Амстердама?\nСейчас {esc(ip)}. Timeweb выдаст новый адрес.\n"
                    f"DNS, вход через Россию и подписки ClashMi обновятся сами.\nПрямые ключи Amnezia перестанут работать — нужно разослать новые.",
         [[btn("Да, менять", "newip:go"), btn("Отмена", "status")]])

def do_newip(chat, mid):
    old = env_now().get("SERVER_IP", "")
    edit(chat, mid, "⏳ Меняю IP… 1–2 минуты")
    rc, out = sh(["/usr/local/bin/rotate-ip.sh"], timeout=600)
    new = env_now().get("SERVER_IP", "")
    if rc != 0 or not new or new == old:
        return edit(chat, mid, f"❌ Timeweb не выдал новый IP\n<pre>{esc(tail(out, 10))}</pre>", [[btn("← Люди", "home")]])
    edit(chat, mid, f"✅ Новый IP Амстердама: {esc(new)} (был {esc(old)})\nПодписки ClashMi обновятся сами. Ключи Amnezia разошли заново из списка людей.\n"
                    f"Старый IP отпусти ночью: /dropip {esc(old)} — сервер перезагрузится на ~5 мин.", [[btn("← Люди", "home")]])

def ask_dropip(chat, ip):
    if not IP_RE.match(ip): return send(chat, "/dropip 1.2.3.4")
    if ip == env_now().get("SERVER_IP"): return send(chat, f"⛔ {esc(ip)} — адрес, к которому подключаются люди. Сначала смени IP.")
    send(chat, f"🗑 Отпустить IP {esc(ip)}?\nTimeweb перезагрузит сервер, VPN ляжет на ~5 минут.",
         [[btn("Да, отпустить", f"dropip:{ip}"), btn("← Люди", "home")]])

def do_dropip(chat, mid, ip):
    edit(chat, mid, f"⏳ Отпускаю {esc(ip)}…")
    rc, out = sh(["/usr/local/bin/drop-ip.sh", ip], timeout=120)
    edit(chat, mid, f"✅ IP {esc(ip)} отпущен. Сервер перезагружается, вернусь через ~5 мин." if rc == 0
                    else f"❌ Не удалось отпустить {esc(ip)}\n<pre>{esc(tail(out, 10))}</pre>", [[btn("← Люди", "home")]])

def do_recheck(chat, mid):
    edit(chat, mid, "⏳ Проверяю все ноды… ~20 секунд")
    try: sh(["/usr/local/bin/vpn-health.sh"], timeout=150)
    except subprocess.TimeoutExpired: pass
    global _ONLINE; _ONLINE = (0.0, set())
    status(chat, mid)

# ── dispatch ──────────────────────────────────────────────────────────────────
def on_message(msg):
    chat = msg["chat"]["id"]; frm = (msg.get("from") or {}).get("id"); text = (msg.get("text") or "").strip()
    if not text: return
    if frm not in ADMINS:
        if text.startswith("/"): send(chat, "⛔ Бот только для администратора.")
        return
    if not ENV.get("TG_CHAT_ID") and not (ETC / "tg_chat_id").exists():   # bind alerts to the first chat an admin uses
        (ETC / "tg_chat_id").write_text(str(chat), encoding="utf-8"); log("alerts bound to chat", chat)
    if not text.startswith("/"):
        p = PENDING_ADD.pop(chat, None)
        if p and p[0] > time.time(): do_add(chat, text.split()[0], p[1])
        return
    parts = text.split(); cmd = parts[0].split("@")[0].lower(); args = parts[1:]
    log("cmd", cmd, args, "from", frm)
    try:
        if cmd in ("/users", "/start", "/list", "/help", "/menu"): home(chat)
        elif cmd == "/add":
            if args: do_add(chat, args[0])
            else: ask_name(chat)
        elif cmd == "/qr":
            if not args: return send(chat, "/qr Имя [clash|ru|nl|awg|x2]")
            name = find_user(args[0])
            if not name: return send(chat, f"Нет такого человека: {esc(args[0])}", [[btn("← Люди", "home")]])
            card(chat, name, args[1].lower() if len(args) > 1 else None)
        elif cmd == "/del":
            name = find_user(args[0]) if args else None
            if not name: return send(chat, "/del Имя", [[btn("← Люди", "home")]])
            ask_del(chat, send(chat, "…"), name)
        elif cmd == "/status": status(chat)
        elif cmd == "/newip": ask_newip(chat, send(chat, "…"))
        elif cmd == "/dropip":
            if not args: return send(chat, "/dropip 1.2.3.4")
            ask_dropip(chat, args[0])
        else: home(chat)
    except Exception as e:  # noqa: BLE001
        log("error", repr(e)); send(chat, f"❌ Ошибка бота\n<pre>{esc(str(e)[:500])}</pre>", [[btn("← Люди", "home")]])

def on_callback(cq):
    cid = cq["id"]; m = cq.get("message") or {}; chat = m.get("chat", {}).get("id"); mid = m.get("message_id")
    frm = (cq.get("from") or {}).get("id"); data = cq.get("data", "")
    if frm not in ADMINS or chat is None: return toast(cid, "Только для администратора")
    log("cb", data, "from", frm)
    kind, _, rest = data.partition(":")
    is_photo = bool(m.get("photo"))
    try:
        if kind == "u":                                   # person tapped in the list → the list becomes the card
            toast(cid, "Открываю…")
            if is_photo: card(chat, rest, None, mid)
            else:                                         # the list stays on screen as a visible "wait" while the card loads
                edit(chat, mid, f"⏳ Открываю {esc(rest)}…"); card(chat, rest); drop(chat, mid)
        elif kind == "k":                                 # another profile → the same card morphs
            name, _, v = rest.partition(":"); toast(cid, "Готовлю…"); card(chat, name, v, mid if is_photo else None)
        elif kind == "home":
            toast(cid)
            if is_photo: drop(chat, mid); home(chat)
            else: home(chat, mid)
        elif kind == "add": toast(cid); ask_name(chat, None if is_photo else mid)
        elif kind == "status": toast(cid); (drop(chat, mid), status(chat)) if is_photo else status(chat, mid)
        elif kind == "recheck": toast(cid, "Проверяю…"); do_recheck(chat, mid)
        elif kind == "d":                                 # delete: confirm inside a plain message, the card goes away
            toast(cid)
            if is_photo: drop(chat, mid); ask_del(chat, send(chat, "…"), rest)
            else: ask_del(chat, mid, rest)
        elif kind == "dy": toast(cid); do_del(chat, mid, rest)
        elif kind == "newip":
            toast(cid)
            if rest == "go": do_newip(chat, mid)
            else: ask_newip(chat, mid)
        elif kind == "dropip": toast(cid); do_dropip(chat, mid, rest)
        else: toast(cid)
    except Exception as e:  # noqa: BLE001
        log("error", repr(e)); send(chat, f"❌ Ошибка бота\n<pre>{esc(str(e)[:500])}</pre>", [[btn("← Люди", "home")]])

def main():
    if not TOKEN: sys.exit("TG_BOT_TOKEN missing in /etc/safechill/vpn.env")
    if len(sys.argv) >= 4 and sys.argv[1] == "--card":
        name = find_user(sys.argv[2]) or sys.exit(f"unknown user {sys.argv[2]}")
        card(int(sys.argv[3]), name, sys.argv[4] if len(sys.argv) > 4 else None); return
    if len(sys.argv) >= 3 and sys.argv[1] == "--status": status(int(sys.argv[2])); return
    try: api("setMyCommands", commands=[{"command": "users", "description": "Люди, ключи и статус"}])
    except Exception: pass
    off_file = STATE / "bot.offset"; offset = int(off_file.read_text()) if off_file.exists() else 0
    log("bot started, admins", ADMINS)
    while True:
        try:
            res = api("getUpdates", _timeout=SOCK, offset=offset, timeout=POLL, allowed_updates=["message", "callback_query"])
        except KeyboardInterrupt: return
        except urllib.error.HTTPError as e:
            log("poll http", e.code); time.sleep(2 if e.code == 409 else 5); continue
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            log("poll retry:", e); continue          # a stalled poll socket is routine — re-poll at once
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1; off_file.write_text(str(offset))
            try:
                if "message" in upd: on_message(upd["message"])
                elif "callback_query" in upd: on_callback(upd["callback_query"])
            except KeyboardInterrupt: return
            except Exception as e:  # noqa: BLE001  — one bad update must not stop the bot
                log("handler error:", repr(e))

if __name__ == "__main__":
    main()
