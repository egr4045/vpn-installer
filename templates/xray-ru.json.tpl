{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "tag": "xhttp-reality",
      "listen": "0.0.0.0", "port": 443, "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "xhttp", "security": "reality",
        "xhttpSettings": { "path": "/${XHTTP_PATH}", "mode": "auto" },
        "realitySettings": {
          "show": false, "target": "${RU_SNI}:443", "xver": 0,
          "serverNames": [ "${RU_SNI}" ],
          "privateKey": "${REALITY_PRIV}", "shortIds": [ "${SHORT_ID}" ]
        }
      }
    },
    {
      "tag": "tcp-reality",
      "listen": "0.0.0.0", "port": ${TCP_PORT}, "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "tcp", "security": "reality",
        "realitySettings": {
          "show": false, "target": "${RU_SNI}:443", "xver": 0,
          "serverNames": [ "${RU_SNI}" ],
          "privateKey": "${REALITY_PRIV}", "shortIds": [ "${SHORT_ID}" ]
        }
      }
    }
  ],
  "outbounds": [
    {
      "tag": "to-exit", "protocol": "vless",
      "settings": { "vnext": [ { "address": "${SERVER_IP}", "port": 443,
        "users": [ { "id": "${RELAY_UUID}", "encryption": "none" } ] } ] },
      "streamSettings": {
        "network": "xhttp", "security": "reality",
        "xhttpSettings": { "path": "/${XHTTP_PATH}", "mode": "auto",
          "extra": { "xmux": { "maxConcurrency": "16-32", "maxConnections": 0, "cMaxReuseTimes": "64-128",
                               "hMaxRequestTimes": "800-900", "hMaxReusableSecs": "1800-3000" } } },
        "realitySettings": { "serverName": "${DOMAIN}", "fingerprint": "firefox",
          "publicKey": "${REALITY_PUB}", "shortId": "${SHORT_ID}" }
      }
    },
    { "tag": "block", "protocol": "blackhole" }
  ],
  "routing": {
    "rules": [
      { "type": "field", "inboundTag": [ "xhttp-reality", "tcp-reality" ], "outboundTag": "to-exit" }
    ]
  }
}
