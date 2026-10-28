#!/usr/bin/env bash
# PostgreSQL dump + media files + env (secrets) of a netbox-docker deployment, with retention.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
DIR="${NETBOX_DOCKER_DIR:-/opt/netbox-docker}"; OUT="${NETBOX_BACKUP_DIR:-/var/backups/netbox}"; KEEP="${NETBOX_BACKUP_KEEP_DAYS:-14}"
if docker compose version >/dev/null 2>&1; then C=(docker compose); elif command -v docker-compose >/dev/null; then C=(docker-compose); else C=(podman compose); fi
umask 077; ts=$(date +%Y%m%d-%H%M%S); dest="$OUT/$ts"; mkdir -p "$dest"
"${C[@]}" --project-directory "$DIR" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$dest/netbox.pgdump"
"${C[@]}" --project-directory "$DIR" exec -T netbox tar czf - -C /opt/netbox/netbox/media . > "$dest/media.tar.gz"
tar czf "$dest/env.tar.gz" -C "$DIR" env docker-compose.override.yml
git -C "$DIR" describe --tags > "$dest/netbox-docker.version" 2>/dev/null || true
[ -s "$dest/netbox.pgdump" ] || { echo "empty dump" >&2; exit 1; }
find "$OUT" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP" -exec rm -rf {} +
echo "backup ok: $dest"
# restore: compose exec -T postgres pg_restore -U netbox -d netbox --clean < netbox.pgdump
