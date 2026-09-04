{
  "log": { "loglevel": "info", "dnsLog": true },
  "dns": { "servers": [ "1.1.1.1", "8.8.8.8", "localhost" ] },
  "inbounds": [
    {
      "tag": "xhttp-reality",
      "listen": "0.0.0.0", "port": 443, "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "xhttp", "security": "reality",
        "xhttpSettings": { "path": "/${XHTTP_PATH}", "mode": "auto" },
        "realitySettings": {
          "show": false, "target": "127.0.0.1:8444", "xver": 0,
          "serverNames": [ "${DOMAIN}" ],
          "privateKey": "${REALITY_PRIV}", "shortIds": [ "${SHORT_ID}" ]
        }
      },
      "sniffing": { "enabled": true, "destOverride": [ "http", "tls", "quic" ] }
    },
    {
      "tag": "tcp-reality",
      "listen": "0.0.0.0", "port": ${TCP_PORT}, "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "tcp", "security": "reality",
        "realitySettings": {
          "show": false, "target": "${FALLBACK_SNI}:443", "xver": 0,
          "serverNames": [ "${FALLBACK_SNI}" ],
          "privateKey": "${REALITY_PRIV}", "shortIds": [ "${SHORT_ID}" ]
        }
      },
      "sniffing": { "enabled": true, "destOverride": [ "http", "tls", "quic" ] }
    }
  ],
  "outbounds": [
    { "tag": "direct", "protocol": "freedom", "settings": { "domainStrategy": "${EGRESS_STRATEGY}" } },
    { "tag": "block",  "protocol": "blackhole" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      { "type": "field", "ip": [ "geoip:private" ], "outboundTag": "block" },
      { "type": "field", "protocol": [ "bittorrent" ], "outboundTag": "${TORRENT_OUT}" }
    ]
  }
}
