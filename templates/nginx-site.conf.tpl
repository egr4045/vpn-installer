# :80 — ACME challenges + redirect to https.
# 127.0.0.1:8444 — the real HTTPS site that xray's REALITY inbound on :443 "steals":
# anyone probing :443 without the right key is transparently served this site.
server {
    listen 80; listen [::]:80;
    server_name ${DOMAIN};
    root /var/www/html;
    location /.well-known/acme-challenge/ { try_files $uri =404; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 127.0.0.1:8444 ssl http2;
    server_name ${DOMAIN};
    ssl_certificate     ${CERT_DIR}/fullchain.pem;
    ssl_certificate_key ${CERT_DIR}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m; ssl_session_timeout 1d;
    add_header Strict-Transport-Security "max-age=31536000" always;
    root /var/www/html; index index.html;
    location / { try_files $uri $uri/ =404; }
}
