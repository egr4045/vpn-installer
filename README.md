# SafeChill VPN

Семейный VPN на двух нодах Timeweb Cloud без панелей, докера и подписок. Один `install.sh`,
пять скриптов, systemd. Собран в сентябре 2026 под текущее поведение ТСПУ.

```
                    ┌──────────────── NL exit (Амстердам) ────────────────┐
  клиент ──443──►   │ xray  VLESS + XHTTP + REALITY  (SNI = свой домен,     │ ──► интернет
  клиент ──8443─►   │ xray  VLESS + TCP + REALITY + vision (SNI icloud)     │
  клиент ──udp──►   │ AmneziaWG 3.1 (kernel module)                         │
                    │ nginx 127.0.0.1:8444 — «настоящий» сайт для REALITY   │
                    │ vpn-health.timer — проверки раз в минуту + Telegram   │
                    └───────────────────────────▲───────────────────────────┘
                                                │ XHTTP+REALITY (один пул соединений)
  клиент ──443/8443─► RU entry (Москва): xray, REALITY под yandex.ru, всё → NL
```

- **Основной путь**: XHTTP + REALITY на 443. Одно долгоживущее HTTP/2-соединение с xmux, без бурста
  TLS-рукопожатий, которые ТСПУ с июня 2026 замораживает. SNI — свой домен, REALITY «крадёт» локальный
  nginx с настоящим сертификатом, поэтому IP, SNI и сертификат совпадают.
- **Резерв**: TCP + REALITY + xtls-rprx-vision на 8443 под `gateway.icloud.com`. Не зависит от домена.
- **Быстрая полоса**: AmneziaWG 3.1 (HeaderProtection, ContentPadding, RandomTrailers, DisableCookies)
  для домашнего провода. На мобильном и в режиме белых списков ненадёжен, поэтому не основной.
- **RU-вход**: для «белых списков» мобильного интернета. Клиент ходит на российский IP с SNI
  `yandex.ru`, нода пересылает всё в NL по XHTTP+REALITY. Пользователи одни и те же, конфиг RU-ноды
  рендерит и заливает NL-нода.
- Торренты через xray режутся (`BLOCK_TORRENT=1`), приватные сети заблокированы, BBR включён.

## Установка (NL-нода, Ubuntu 24.04, root)

```bash
git clone https://github.com/egr4045/vpn-installer safechill && cd safechill
cp vpn.env.example vpn.env      # DOMAIN обязателен, остальное по вкусу
nano vpn.env
./install.sh
```

Домен должен указывать на IP ноды **без прокси** (серая тучка в Cloudflare) — иначе Let's Encrypt не
выдаст сертификат. Если DNS ещё не готов, `install.sh` поставит самоподписанный сертификат, VPN
заработает, а health-check раз в минуту будет напоминать про DNS. Поправил DNS — просто перезапусти
`./install.sh`.

`install.sh` идемпотентен: секреты генерируются один раз в `/etc/safechill/secrets.env`, пользователи
живут в `/etc/safechill/users.json` и `/etc/safechill/peers/`, всё остальное перерисовывается.

## RU-вход

На чистой Ubuntu 24.04 в России ничего ставить не нужно, всё делает NL-нода:

```bash
setup-ru.sh 200.165.237.236 yandex.ru   # ssh root@<ru-ip> должен работать с NL-ноды
```

## Люди

```bash
add-client.sh egr        # печатает ссылки, кладёт всё в /root/clients/egr/
del-client.sh egr
vpn-status.sh            # сервисы, порты, сертификат, AWG-пиры, RU-нода, health
update-xray.sh           # обновить xray на обеих нодах
```

В `/root/clients/<имя>/`: `nl-xhttp.txt` (основной), `nl-tcp.txt` (резерв), `ru-xhttp.txt` и
`ru-tcp.txt` (для белых списков), `awg.conf`, а также `nl6-*`, `ru6-*` и `awg6.conf` с IPv6-адресами
нод для сетей, где IPv6 фильтруют слабее. QR-коды `*.png` ко всему. Выход в интернет с NL-ноды идёт
по IPv6, если у сайта он есть (`EGRESS_PREFER=ipv6`), иначе по IPv4.

## Клиент: AmneziaVPN 5.0.1.5+ (Android, iOS, Windows, macOS)

Один клиент на всё: в нём родной AmneziaWG 3.1 и с версии 5.0.0.5 XHTTP+REALITY с XMux.

1. `+` → «Импорт» → вставить `vless://…` из `nl-xhttp.txt` (или отсканировать `nl-xhttp.png`).
2. Так же добавить `nl-tcp.txt` и `ru-xhttp.txt`: между серверами переключаемся вручную, если основной
   вдруг не подключается.
3. AmneziaWG: `+` → «Импорт» → файл `awg.conf` или QR `awg.png`.

Если XHTTP в Amnezia капризничает: Happ, v2rayTun, Streisand (iOS), v2rayNG (Android) едят те же
`vless://` ссылки. Для AWG есть официальное приложение AmneziaWG. Часть клиентов удалена из
российского App Store, нужен зарубежный Apple ID.

## Telegram-бот

Тот же бот, что шлёт алерты, принимает команды от админов (`TG_ADMIN_IDS` в vpn.env), в личке или в группе:

| Команда | Что делает |
|---|---|
| `/users` | список людей и когда каждый последний раз поднимал AmneziaWG |
| `/add Имя` | `add-client.sh` на обеих нодах и сразу основной QR |
| `/qr Имя` | **один QR на всё**: личная подписка `https://домен/s/<token>` со всеми xray-серверами, для Happ и любого Xray-клиента с подписками; приложение само выбирает рабочий сервер |
| `/qr Имя amnezia` | **один QR для AmneziaVPN**: нативный ключ `vpn://` (формат самого клиента, см. `bin/amnezia-key.py`) с xray XHTTP+REALITY и AmneziaWG внутри; `amneziaru` — то же через RU-вход |
| `/qr Имя awg` \| `ru` \| `tcp` \| `nl6` \| `ru6` \| `all` | отдельные профили; для AWG приходит файл awg.conf и QR |
| `/del Имя yes` | удалить человека везде |
| `/status` | вывод `vpn-status.sh` |

Сервис `safechill-bot.service`, только стандартная библиотека Python, long polling.

## Мониторинг

`vpn-health.timer` на NL каждую минуту проверяет: REALITY на 443 отвечает как сайт, 8443 отдаёт
сертификат icloud, awg0 поднят, nginx жив, есть внешний интернет по IPv4 и IPv6, сертификат не
истекает, диск не полон, RU-нода отвечает. Упавшее перезапускает, в Telegram пишет один раз при
падении и один раз при восстановлении. Отдельно с NL-ноды прощупываются популярные сайты (YouTube,
Google, Instagram, Telegram, ChatGPT, X, Facebook, Discord, TikTok, WhatsApp, GitHub, Netflix):
если набор недоступных держится две проверки подряд, приходит одно сообщение со списком, при
восстановлении ещё одно. RU-нода в свою очередь следит за своим xray и за NL-нодой из России
(`vpn-health-ru.timer`). Чат для алертов: `TG_CHAT_ID` в vpn.env, иначе бот привязывается к первому,
кто ему напишет.

## Деньги (Timeweb, сентябрь 2026)

| Нода | Тариф | ₽/мес |
|---|---|---|
| NL exit | Cloud NL-15 (1 vCPU / 1 GB / 200 Мбит) + IPv4 | 710 |
| RU entry | Cloud MSK 40 (2 vCPU / 2 GB / 1 Гбит) + IPv4 | 1000 |

Трафик у Timeweb не тарифицируется. Серверы создаются только с IPv6, IPv4 добавляется отдельно
(`POST /servers/{id}/ips`). После удаления сервера IPv4 остаётся платным floating IP — удалять руками.

## Что не так и что дальше

- `.ru` домен могут отозвать: тогда ломаются только сертификат и «настоящий сайт», Reality-8443 и AWG
  работают дальше. Клиенты ходят по IP, DNS им не нужен.
- Планы: IPv6-ссылки для клиентов, взаимный мониторинг нод, проверка доступности популярных сервисов
  с NL-ноды одним сообщением, перебор IP RU-ноды под белые списки операторов.
