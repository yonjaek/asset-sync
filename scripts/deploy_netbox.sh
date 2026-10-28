#!/usr/bin/env bash
# Deploy NetBox with the official netbox-community/netbox-docker (pinned release) behind an nginx TLS proxy.
# Default is a dry-run that only prints the plan. Pass --apply to execute.
# Refuses to run if a NetBox already answers on NETBOX_URL (use the existing one instead).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
APPLY=false; [ "${1:-}" = "--apply" ] && APPLY=true
VER="${NETBOX_DOCKER_VERSION:-5.1.1}"; DIR="${NETBOX_DOCKER_DIR:-/opt/netbox-docker}"
PUBLIC_IP="${NETBOX_PUBLIC_IP:?set NETBOX_PUBLIC_IP in .env}"
LOCAL_PORT="${NETBOX_PORT:-8000}"           # loopback-only plain HTTP for local health checks
WORKERS="${NETBOX_GRANIAN_WORKERS:-8}"
PG_SHARED_BUFFERS="${NETBOX_PG_SHARED_BUFFERS:-2GB}"; PG_CACHE="${NETBOX_PG_EFFECTIVE_CACHE:-6GB}"
NGINX_IMAGE="${NETBOX_NGINX_IMAGE:-docker.io/library/nginx:1.28-alpine}"
run() { if $APPLY; then echo "+ $*"; "$@"; else echo "[dry-run] $*"; fi; }

# any response carrying the API-Version header means NetBox is already there (403 without a token is normal)
if curl -sk -m 5 -o /dev/null -D - "${NETBOX_URL:-https://$PUBLIC_IP}/api/" 2>/dev/null | grep -qi '^api-version:'; then
  echo "Existing NetBox answers at ${NETBOX_URL:-https://$PUBLIC_IP}. Not deploying. Continue with: scripts/assetctl.py check"; exit 0
fi
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose)
else echo "Docker Compose v2 not found (install docker-ce + docker-compose-plugin)" >&2; exit 1; fi
# busy only if something listens on that IP (or on all addresses) and port
busy() { ss -Hltn | awk '{print $4}' | grep -qE "^(\[?($1|0\.0\.0\.0|::|\*)\]?):$2$"; }
for pair in "$PUBLIC_IP:80" "$PUBLIC_IP:443" "127.0.0.1:$LOCAL_PORT"; do
  if busy "${pair%:*}" "${pair##*:}"; then echo "address $pair already in use" >&2; exit 1; fi
done

echo "compose: ${COMPOSE[*]}  netbox-docker: $VER  dir: $DIR"
echo "public: https://$PUBLIC_IP (80 -> 443)  local: http://127.0.0.1:$LOCAL_PORT  granian workers: $WORKERS"
if [ ! -d "$DIR/.git" ]; then
  run git clone -q --depth 1 --branch "$VER" https://github.com/netbox-community/netbox-docker "$DIR"
else
  have=$(git -C "$DIR" describe --tags 2>/dev/null || echo unknown); echo "existing checkout: $have"
  [ "$have" = "$VER" ] || { echo "checkout is $have, expected $VER; upgrade deliberately (backup first)" >&2; exit 1; }
fi

gen() { python3 -c "import secrets,string;a=string.ascii_letters+string.digits;print(''.join(secrets.choice(a) for _ in range($1)))"; }
if $APPLY && [ ! -f "$DIR/env/.asset-sync-secrets-generated" ]; then
  # upstream env files ship with public example secrets: replace them all
  db=$(gen 32); r1=$(gen 32); r2=$(gen 32)
  sed -i -E "s|^SECRET_KEY=.*|SECRET_KEY='$(gen 60)'|; s|^DB_PASSWORD=.*|DB_PASSWORD=$db|; s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$r1|; s|^REDIS_CACHE_PASSWORD=.*|REDIS_CACHE_PASSWORD=$r2|; s|^API_TOKEN_PEPPER_1=.*|API_TOKEN_PEPPER_1='$(gen 50)'|" "$DIR/env/netbox.env"
  sed -i -E "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$db|" "$DIR/env/postgres.env"
  sed -i -E "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$r1|" "$DIR/env/redis.env"
  sed -i -E "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$r2|" "$DIR/env/redis-cache.env"
  chmod 600 "$DIR"/env/*.env; touch "$DIR/env/.asset-sync-secrets-generated"
  echo "secrets regenerated in $DIR/env (chmod 600) - include in backups"
else
  echo "[dry-run] regenerate SECRET_KEY, DB/Redis passwords, API_TOKEN_PEPPER_1 in $DIR/env/*.env"
fi

# TLS: self-signed cert with the server IP in SAN unless a cert is already there (replace with a corporate cert anytime)
CERT_DIR="$DIR/proxy/certs"
if [ ! -f "$CERT_DIR/netbox.crt" ]; then
  run mkdir -p "$CERT_DIR"
  run openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes -subj "/CN=$PUBLIC_IP" \
      -addext "subjectAltName=IP:$PUBLIC_IP" -keyout "$CERT_DIR/netbox.key" -out "$CERT_DIR/netbox.crt"
  $APPLY && chmod 600 "$CERT_DIR/netbox.key"
fi
NGINX_CONF="server {
    listen 80;
    server_name _;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl;
    http2 on;
    server_name _;
    ssl_certificate     /etc/nginx/certs/netbox.crt;
    ssl_certificate_key /etc/nginx/certs/netbox.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 25m;
    add_header Strict-Transport-Security \"max-age=31536000\" always;
    location / {
        proxy_pass http://netbox:8080;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
}
"
OVR="$DIR/docker-compose.override.yml"
OVR_CONTENT="services:
  netbox:
    ports:
      - \"127.0.0.1:$LOCAL_PORT:8080\"
    environment:
      ALLOWED_HOSTS: \"$PUBLIC_IP localhost 127.0.0.1 netbox\"
      CSRF_TRUSTED_ORIGINS: \"https://$PUBLIC_IP\"
      GRANIAN_WORKERS: \"$WORKERS\"
    healthcheck:
      start_period: 600s   # first start runs all migrations (~4 min observed)
    restart: unless-stopped
  netbox-worker:
    restart: unless-stopped
  postgres:
    command: [\"postgres\", \"-c\", \"shared_buffers=$PG_SHARED_BUFFERS\", \"-c\", \"effective_cache_size=$PG_CACHE\",
              \"-c\", \"work_mem=16MB\", \"-c\", \"maintenance_work_mem=256MB\", \"-c\", \"random_page_cost=1.1\"]
    shm_size: 512m
    restart: unless-stopped
  redis:
    restart: unless-stopped
  redis-cache:
    restart: unless-stopped
  proxy:
    image: $NGINX_IMAGE
    depends_on:
      netbox:
        condition: service_healthy
    ports:
      - \"$PUBLIC_IP:80:80\"
      - \"$PUBLIC_IP:443:443\"
    volumes:
      - ./proxy/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./proxy/certs:/etc/nginx/certs:ro
    restart: unless-stopped
"
if $APPLY; then
  printf '%s' "$NGINX_CONF" > "$DIR/proxy/nginx.conf"; printf '%s' "$OVR_CONTENT" > "$OVR"
else
  echo "[dry-run] write $DIR/proxy/nginx.conf (80->443 redirect, TLS, proxy_pass netbox:8080)"
  echo "[dry-run] write $OVR:"; printf '%s' "$OVR_CONTENT" | sed 's/^/    /'
fi
run "${COMPOSE[@]}" --project-directory "$DIR" pull -q
run "${COMPOSE[@]}" --project-directory "$DIR" up -d
if $APPLY; then
  echo "waiting for NetBox (first start runs migrations, can take minutes)..."
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(${COMPOSE[@]} --project-directory "$DIR" ps -q netbox)")" = healthy ] && break; sleep 10
  done
  "${COMPOSE[@]}" --project-directory "$DIR" up -d   # start worker/proxy if they gave up waiting
  echo "API: $(curl -s --cacert "$CERT_DIR/netbox.crt" -o /dev/null -D - "https://$PUBLIC_IP/api/" | grep -i '^api-version' | tr -d '\r')"
  "${COMPOSE[@]}" --project-directory "$DIR" ps --format '{{.Service}}\t{{.Status}}'
  echo "volumes:"; "${COMPOSE[@]}" --project-directory "$DIR" config --volumes
  echo "CA for clients: $CERT_DIR/netbox.crt  (set NETBOX_CA_BUNDLE to it)"
  echo "next: ${COMPOSE[*]} --project-directory $DIR exec netbox /opt/netbox/netbox/manage.py createsuperuser"
fi
