FROM python:3.12-slim
WORKDIR /app

# docker CLI (static) so the admin can `docker exec/restart/kill` the cores
ARG DOCKER_VER=27.3.1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VER}.tgz" \
      | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY vpn-admin/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vpn-admin/app /app/app
COPY templates /app/templates
COPY scripts /app/scripts

ENV VPN_TEMPLATE_DIR=/app/templates
# Honour X-Forwarded-Proto/For from the local nginx (loopback only) so the app
# sees the real https scheme + client IP behind the TLS terminator.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
