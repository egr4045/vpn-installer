# Rendered by install.sh into ./nginx.conf (gitignored). Placeholders __X__ from .env.
#
# Data path: xray Reality "steals" TLS on :443 and forwards undecryptable/owned
# SNIs to this nginx over a unix socket with PROXY protocol. nginx routes by SNI:
#   admin domain   -> the vpn-admin panel (sub pages, dashboard)
#   direct/cdn     -> xray XHTTP (/store) and httpupgrade (/probe) unix sockets
# Certs: whole /etc/letsencrypt is mounted, so live/ symlinks resolve.

server_names_hash_bucket_size 64;

map $http_upgrade $connection_upgrade { default upgrade; "" close; }

# admin panel + subscription pages
server {
    server_name __ADMIN_DOMAIN__;
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    http2 on;
    ssl_certificate     "/etc/letsencrypt/live/__CERT_DOMAIN__/fullchain.pem";
    ssl_certificate_key "/etc/letsencrypt/live/__CERT_DOMAIN__/privkey.pem";
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
    add_header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet, noimageindex" always;
}

# direct domain: XHTTP (/store) + httpupgrade (/probe) transports
server {
    server_name __DIRECT_DOMAIN__;
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    http2 on;
    ssl_certificate     "/etc/letsencrypt/live/__CERT_DOMAIN__/fullchain.pem";
    ssl_certificate_key "/etc/letsencrypt/live/__CERT_DOMAIN__/privkey.pem";
    ssl_protocols TLSv1.2 TLSv1.3;

    location ^~ /store { proxy_pass http://unix:/dev/shm/xhttp.sock;       proxy_http_version 1.1; proxy_set_header Host $host; }
    location ^~ /probe { proxy_pass http://unix:/dev/shm/httpupgrade.sock; proxy_http_version 1.1; proxy_set_header Host $host;
                         proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $connection_upgrade; }
    location / { return 404; }
    add_header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet, noimageindex" always;
}

# CDN/apex domain (Cloudflare-proxied): httpupgrade (/probe)
server {
    server_name __CDN_DOMAIN__ __APEX_DOMAIN__;
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    http2 on;
    ssl_certificate     "/etc/letsencrypt/live/__CERT_DOMAIN__/fullchain.pem";
    ssl_certificate_key "/etc/letsencrypt/live/__CERT_DOMAIN__/privkey.pem";
    ssl_protocols TLSv1.2 TLSv1.3;

    location ^~ /probe { proxy_pass http://unix:/dev/shm/httpupgrade.sock; proxy_http_version 1.1; proxy_set_header Host $host;
                         proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $connection_upgrade; }
    location / { return 404; }
    add_header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet, noimageindex" always;
}

# default: anything else that reaches the socket
server {
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol default_server;
    server_name _;
    ssl_certificate     "/etc/letsencrypt/live/__CERT_DOMAIN__/fullchain.pem";
    ssl_certificate_key "/etc/letsencrypt/live/__CERT_DOMAIN__/privkey.pem";
    return 444;
}
