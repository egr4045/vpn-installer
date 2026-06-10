# Rendered by install.sh into ./docker-compose.yml (which is gitignored).
# Placeholders __LIKE_THIS__ are substituted by lib/60-render.sh from .env.
x-logging: &logging
  logging:
    driver: json-file
    options: { max-size: 10m, max-file: 3 }

services:
  xray:
    image: ghcr.io/xtls/xray-core:__XRAY_VER__
    container_name: xray
    network_mode: host
    restart: always
    user: "0:0"                      # bind :443/:8443 + read LE certs
    command: ["run", "-c", "/etc/xray/config.json"]
    <<: *logging
    volumes:
      - /etc/xray:/etc/xray:ro
      - /dev/shm:/dev/shm:rw
      - /etc/letsencrypt:/etc/letsencrypt:ro

  sing-box:
    image: ghcr.io/sagernet/sing-box:__SINGBOX_VER__
    container_name: sing-box
    network_mode: host
    restart: always
    command: ["run", "-c", "/etc/sing-box/config.json"]
    <<: *logging
    volumes:
      - /etc/sing-box:/etc/sing-box:rw
      - /etc/letsencrypt:/etc/letsencrypt:ro

  nginx:
    image: nginx:1.28
    container_name: nginx
    network_mode: host
    restart: always
    <<: *logging
    # Whole /etc/letsencrypt is mounted so live/ symlinks resolve to archive/
    # (per-file mounts are NOT needed when the entire tree is present).
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - /dev/shm:/dev/shm:rw
    command: sh -c 'rm -f /dev/shm/nginx.sock && exec nginx -g "daemon off;"'

  vpn-admin:
    build: { context: ., dockerfile: vpn-admin/Dockerfile }
    image: vpn-admin:local
    container_name: vpn-admin
    network_mode: host
    restart: always
    <<: *logging
    env_file: [ .env ]
    environment: [ VPN_DATA_DIR=/data ]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /etc/xray:/etc/xray:rw
      - /etc/sing-box:/etc/sing-box:rw
      - ./data:/data
    depends_on: [ xray, sing-box ]
