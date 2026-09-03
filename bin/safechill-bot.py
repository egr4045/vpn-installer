#!/usr/bin/env python3
"""safechill-bot — Telegram admin bot for the exit node (stdlib only, long polling).

UX: /users shows every person as a button. Tapping a person sends a ready-to-forward AmneziaVPN card:
one photo (QR) with download links + 3 steps, then the key as a .vpn file whose caption is the key
itself in monospace (tap = copy). Buttons under the card: AWG (speed), RU (white lists), standby exit,
HAPP (auto-select), delete. Admins only (TG_ADMIN_IDS in /etc/safechill/vpn.env).

Commands: /users  /add <name>  /status  /newip  /dropip <ip>  /help   (also /qr <name> [variant])
CLI: safechill-bot.py --card <name> <chat_id> [amnezia|awg|ru|x2|happ]
"""
import json, re, subprocess, sys, time, urllib.request, uuid, html, pathlib

ETC = pathlib.Path("/etc/safechill"); CLIENTS = pathlib.Path("/root/clients")
STATE = pathlib.Path("/var/lib/safechill"); STATE.mkdir(parents=True, exist_ok=True)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
AMNEZIA_LATEST = "https://github.com/amnezia-vpn/amnezia-client/releases/latest"
AMNEZIA_IOS = "https://apps.apple.com/us/app/amneziavpn/id1600529900"
AMNEZIA_ANDROID = "https://play.google.com/store/apps/details?id=org.amnezia.vpn"
HAPP_IOS = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
HAPP_SITE = "https://www.happ.su/main/ru"

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
PENDING_ADD = set()   # chats where the next plain message is a new person's name

def log(*a): print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)
def esc(s): return html.escape(str(s))

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

def send(chat, text, kb=None):
    p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb: p["reply_markup"] = {"inline_keyboard": kb}
    api("sendMessage", **p)

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
    """name -> connections accepted in the last N minutes (from xray's access log in journald)."""
    out = {}
    try:
        txt = subprocess.run(["journalctl", "-u", "xray", "--since", f"-{minutes}min", "--no-pager", "-o", "cat"], capture_output=True, text=True, timeout=15).stdout
        for m in re.finditer(r"accepted .* email: (\S+)", txt): out[m.group(1)] = out.get(m.group(1), 0) + 1
    except Exception: pass
    return out

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

# ── cards ─────────────────────────────────────────────────────────────────────
VARIANTS = {  # variant -> (file stem, short title, one-line hint)
    "amnezia": ("amnezia",     "основной",                        "Амстердам. Ставь этот."),
    "awg":     ("amnezia-awg", "AWG, максимальная скорость",       "Тот же сервер по протоколу AmneziaWG: быстрее дома по Wi-Fi и на ПК. На мобильном интернете может не работать, тогда вернись к основному."),
    "ru":      ("amnezia-ru",  "RU, белые списки",                 "Для мобильного интернета в дни, когда не открывается ничего иностранного. Выход всё равно Амстердам."),
    "x2":      ("amnezia-x2",  "запасной выход",                   "Если основной Амстердам вдруг недоступен."),
}
BTN = {"amnezia": "🟢 Основной", "awg": "⚡ AWG (макс. скорость)", "ru": "🇷🇺 RU (белые списки)", "x2": "🛟 Запасной (если основной упал)"}

def card_buttons(name, current):
    rows, row = [], []
    for v in ("amnezia", "awg", "ru", "x2"):
        if v != current and (CLIENTS / name / f"{VARIANTS[v][0]}.txt").exists():
            row.append({"text": BTN[v], "callback_data": f"k:{name}:{v}"})
            if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    last = [{"text": "🗑 Удалить", "callback_data": f"d:{name}"}]
    if current != "happ": last.insert(0, {"text": "🍏 HAPP (авто-выбор сервера)", "callback_data": f"k:{name}:happ"})
    rows.append(last)
    return rows

def card(chat, name, variant="amnezia"):
    """Photo (QR + instructions), then the key as a .vpn file whose caption is the key in monospace."""
    if variant == "happ": return card_happ(chat, name)
    stem, title, hint = VARIANTS[variant]; d = CLIENTS / name; key_f = d / f"{stem}.txt"
    if not key_f.exists(): return send(chat, f"У {esc(name)} нет варианта «{title}» (такой ноды нет)")
    v = amnezia_latest()
    cap = (f"🔐 <b>{BRAND} · {esc(name)}</b> · {title}\n{hint}\n\n"
           f"📥 <b>Скачать AmneziaVPN</b>{' ' + esc(v) if v else ''}: <a href=\"{AMNEZIA_IOS}\">iPhone</a> · "
           f"<a href=\"{AMNEZIA_ANDROID}\">Android</a> · <a href=\"{AMNEZIA_LATEST}\">Windows / Mac</a>\n\n"
           f"1. Amnezia → «+» → «Подключиться по ключу»\n"
           f"2. Отсканируй этот QR, или открой файл ниже, или вставь ключ из него\n"
           f"3. Включи. Готово.")
    send_photo(chat, d / f"{stem}.png", cap, card_buttons(name, variant))
    key = key_f.read_text().strip()
    fname = f"{BRAND}-{name}{'' if variant == 'amnezia' else '-' + variant}.vpn"
    send_doc(chat, key.encode(), fname, f"<code>{esc(key)}</code>" if len(key) <= 1000 else "Ключ внутри файла: открой его в Amnezia.")

def card_happ(chat, name):
    d = CLIENTS / name; link = d / "sub.txt"
    if not link.exists(): return send(chat, f"У {esc(name)} нет подписки")
    url = link.read_text().strip()
    cap = (f"🍏 <b>{BRAND} · {esc(name)}</b> · Happ, авто-выбор сервера\n"
           f"Одна ссылка на все наши серверы (Амстердам, запасной, вход через Россию). Happ сам держится за живой и быстрый. AmneziaWG сюда не входит.\n\n"
           f"📥 <b>Скачать Happ</b>: <a href=\"{HAPP_IOS}\">iPhone</a> · <a href=\"{HAPP_ANDROID}\">Android</a> · <a href=\"{HAPP_SITE}\">Windows / Mac</a>\n\n"
           f"1. Happ → «+» → «Сканировать QR» или вставить ссылку\n2. Включи авто-выбор сервера. Готово.\n\n<code>{esc(url)}</code>")
    kb = [[{"text": "📋 Скопировать ссылку", "copy_text": {"text": url}}], *card_buttons(name, "happ")]
    send_photo(chat, d / "sub.png", cap, kb)

# ── screens ───────────────────────────────────────────────────────────────────
def mark(sec):
    if sec is None: return ""
    return " 🟢" if sec < 180 else ""

def users_screen(chat):
    us = sorted(users(), key=lambda x: x["name"].lower()); awg = awg_last_seen(); xr = xray_recent_users(5)
    rows, row = [], []
    for u in us:
        n = u["name"]; online = (n in xr) or (awg.get(n) is not None and awg[n] < 180)
        row.append({"text": f"{n}{' 🟢' if online else ''}", "callback_data": f"u:{n}"})
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{"text": "➕ Добавить человека", "callback_data": "add"}, {"text": "📊 Статус", "callback_data": "status"}])
    send(chat, f"👥 <b>{BRAND}</b> · {len(us)} чел. · 🟢 = в сети сейчас\nНажми на имя — пришлю готовый ключ, его можно сразу переслать человеку.", rows)

def node_probe(host, sni):
    try:
        rc, out = sh(["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "-m", "6", "--resolve", f"{sni}:443:{host}", f"https://{sni}/"], timeout=12)
        return out.strip() not in ("", "000")
    except Exception: return False

def status_screen(chat):
    E = load_env(ETC / "vpn.env")
    def svc(u): return subprocess.run(["systemctl", "is-active", "--quiet", u]).returncode == 0
    lines = [f"📊 <b>{BRAND}</b> · {time.strftime('%d.%m %H:%M')}"]
    nl_ok = svc("xray") and node_probe("127.0.0.1", E.get("DOMAIN", "")); awg_ok = svc("awg-quick@awg0")
    lines.append(f"{'🟢' if nl_ok else '🔴'} Амстердам, основной · xray {'ok' if svc('xray') else 'DOWN'} · AWG {'ok' if awg_ok else 'DOWN'} · {E.get('SERVER_IP','')}")
    if E.get("EXIT2_HOST"): lines.append(f"{'🟢' if node_probe(E['EXIT2_HOST'], E.get('EXIT2_DOMAIN','')) else '🔴'} Запасной выход · {E['EXIT2_HOST']}")
    if E.get("RU_HOST"): lines.append(f"{'🟢' if node_probe(E['RU_HOST'], E.get('RU_SNI','yandex.ru')) else '🔴'} Вход через Россию · {E['RU_HOST']}")
    cert = E.get("CERT_DIR", ""); lines.append(f"{'🟢' if 'letsencrypt' in cert else '🟡'} Сертификат: {'Let’s Encrypt' if 'letsencrypt' in cert else 'самоподписанный, DNS ещё едет'}")
    sites = (STATE / "services.last").read_text().strip() if (STATE / "services.last").exists() else ""
    lines.append(f"{'🟠 Недоступны с сервера: ' + esc(sites) if sites else '🟢 YouTube, Google, Instagram, Telegram, ChatGPT и остальные открываются'}")
    xr = xray_recent_users(5); awg = awg_last_seen()
    online = sorted({n for n, c in xr.items()} | {n for n, s in awg.items() if s is not None and s < 180})
    lines.append(f"👥 В сети сейчас: {', '.join(esc(n) for n in online) if online else 'никого'}")
    failing = (STATE / "health.state").read_text().split() if (STATE / "health.state").exists() else []
    failing = [f for f in failing if f != "selfsigned"]
    if failing: lines.append(f"⚠️ Открытые проблемы: {esc(', '.join(failing))}")
    try:
        last = [l for l in (pathlib.Path("/var/log/vpn-health.log").read_text().splitlines()) if "FAIL" in l or "OK " in l][-3:]
        if last: lines.append("🕓 Последние события:\n" + "\n".join("  " + esc(l[:100]) for l in last))
    except Exception: pass
    send(chat, "\n".join(lines))

def do_add(chat, name):
    if not NAME_RE.match(name): return send(chat, "Имя латиницей, цифры, - и _, до 32 символов. Попробуй ещё раз: /add Имя")
    if find_user(name): return send(chat, f"{esc(name)} уже есть.")
    send(chat, f"⏳ Создаю {esc(name)} на всех нодах…")
    rc, out = sh(["/usr/local/bin/add-client.sh", name])
    if rc != 0: return send(chat, f"❌ не получилось:\n<pre>{esc(out[-1500:])}</pre>")
    card(chat, name, "amnezia")

def do_del(chat, name):
    rc, out = sh(["/usr/local/bin/del-client.sh", name])
    send(chat, f"🗑 {esc(name)} удалён везде" if rc == 0 else f"❌ ошибка:\n<pre>{esc(out[-1500:])}</pre>")

def do_newip(chat):
    if not ENV.get("TW_API_TOKEN") or not ENV.get("TW_SERVER_ID"):
        return send(chat, "Нет TW_API_TOKEN / TW_SERVER_ID в /etc/safechill/vpn.env — без ключа Timeweb IP менять нечем.")
    send(chat, "⏳ Беру новый IPv4 у Timeweb, переписываю DNS, конфиги нод и все ключи. 1–2 минуты…")
    rc, out = sh(["/usr/local/bin/rotate-ip.sh"], timeout=600)
    send(chat, ("✅ " if rc == 0 else "❌ ") + f"<pre>{esc(out[-2500:])}</pre>")

HELP = (f"🔐 <b>{BRAND}</b>\n/users — люди кнопками: нажал на имя → готовый ключ для пересылки\n"
        "/add Имя — новый человек\n/status — состояние нод по-человечески\n/newip — новый IPv4 основной ноды, если старый заблокировали\n"
        "/dropip 1.2.3.4 — отпустить лишний IP (сервер перезагрузится)")

# ── dispatch ──────────────────────────────────────────────────────────────────
def on_message(msg):
    chat = msg["chat"]["id"]; frm = (msg.get("from") or {}).get("id"); text = (msg.get("text") or "").strip()
    if not text: return
    if frm not in ADMINS:
        if text.startswith("/"): send(chat, "⛔ Команды бота доступны только администратору.")
        return
    if not text.startswith("/"):
        if chat in PENDING_ADD: PENDING_ADD.discard(chat); do_add(chat, text.split()[0])
        return
    parts = text.split(); cmd = parts[0].split("@")[0].lower(); args = parts[1:]
    log("cmd", cmd, args, "from", frm)
    try:
        if cmd in ("/users", "/list", "/start"): users_screen(chat)
        elif cmd == "/add":
            if args: do_add(chat, args[0])
            else: PENDING_ADD.add(chat); send(chat, "Напиши имя нового человека (латиницей):")
        elif cmd == "/qr":
            if not args: return send(chat, "/qr Имя [amnezia|awg|ru|x2|happ]")
            name = find_user(args[0]); v = (args[1].lower() if len(args) > 1 else "amnezia")
            if not name: return send(chat, f"Не знаю «{esc(args[0])}»")
            card(chat, name, v if v in VARIANTS or v == "happ" else "amnezia")
        elif cmd == "/del":
            name = find_user(args[0]) if args else None
            if not name: return send(chat, "/del Имя")
            send(chat, f"Удалить {esc(name)} везде?", [[{"text": f"Да, удалить {name}", "callback_data": f"dy:{name}"}, {"text": "Отмена", "callback_data": "noop"}]])
        elif cmd == "/status": status_screen(chat)
        elif cmd == "/newip": do_newip(chat)
        elif cmd == "/dropip":
            if not args: return send(chat, "/dropip 1.2.3.4")
            rc, out = sh(["/usr/local/bin/drop-ip.sh", args[0]], timeout=120); send(chat, ("✅ " if rc == 0 else "❌ ") + f"<pre>{esc(out[-1500:])}</pre>")
        elif cmd == "/help": send(chat, HELP)
    except Exception as e:  # noqa: BLE001
        log("error", repr(e)); send(chat, f"❌ {esc(str(e)[:500])}")

def on_callback(cq):
    cid = cq["id"]; chat = cq["message"]["chat"]["id"]; frm = (cq.get("from") or {}).get("id"); data = cq.get("data", "")
    try: api("answerCallbackQuery", callback_query_id=cid)
    except Exception: pass
    if frm not in ADMINS: return send(chat, "⛔ Только для администратора.")
    log("cb", data, "from", frm)
    try:
        kind, _, rest = data.partition(":")
        if kind == "u": card(chat, rest, "amnezia")
        elif kind == "k":
            name, _, v = rest.partition(":"); card(chat, name, v)
        elif kind == "d": send(chat, f"Удалить {esc(rest)} везде? Ключи перестанут работать сразу.", [[{"text": f"Да, удалить {rest}", "callback_data": f"dy:{rest}"}, {"text": "Отмена", "callback_data": "noop"}]])
        elif kind == "dy": do_del(chat, rest)
        elif kind == "add": PENDING_ADD.add(chat); send(chat, "Напиши имя нового человека (латиницей):")
        elif kind == "status": status_screen(chat)
    except Exception as e:  # noqa: BLE001
        log("error", repr(e)); send(chat, f"❌ {esc(str(e)[:500])}")

def main():
    if not TOKEN: sys.exit("TG_BOT_TOKEN missing in /etc/safechill/vpn.env")
    if len(sys.argv) >= 4 and sys.argv[1] == "--card":
        name = find_user(sys.argv[2]) or sys.exit(f"unknown user {sys.argv[2]}")
        card(int(sys.argv[3]), name, sys.argv[4] if len(sys.argv) > 4 else "amnezia"); return
    if len(sys.argv) >= 3 and sys.argv[1] == "--status": status_screen(int(sys.argv[2])); return
    try:
        api("setMyCommands", commands=[{"command": "users", "description": "люди и их ключи"}, {"command": "add", "description": "добавить человека"},
                                       {"command": "status", "description": "состояние нод"}, {"command": "newip", "description": "новый IPv4 основной ноды"},
                                       {"command": "help", "description": "справка"}])
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
