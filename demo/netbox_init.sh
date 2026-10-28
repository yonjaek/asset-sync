#!/usr/bin/env bash
# 새로 배포한 NetBox 에 관리자 계정과 API 토큰 2개(쓰기 / 읽기 전용)를 만들고 .env 에 기록합니다.
# - 관리자 비밀번호는 무작위로 만들어 demo/.netbox_admin (권한 600, Git 제외) 에만 저장합니다.
# - 토큰 값은 화면에 출력하지 않습니다.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env 가 없습니다. make env 를 먼저 실행하세요" >&2; exit 1; }
set -a; . ./.env; set +a
DIR="${NETBOX_DOCKER_DIR:-/opt/netbox-docker}"
C=(docker compose --project-directory "$DIR")
CA=(--cacert "$NETBOX_CA_BUNDLE")
ADMIN_USER="${NETBOX_ADMIN_USER:-admin}"
SECRET_FILE=demo/.netbox_admin

if [ -n "${NETBOX_WRITE_TOKEN:-}" ] && [ -n "${NETBOX_TOKEN:-}" ]; then
  echo ".env 에 토큰이 이미 있습니다. 새로 만들려면 두 값을 비우고 다시 실행하세요."; exit 0
fi

if [ ! -s "$SECRET_FILE" ]; then
  umask 077
  python3 -c "import secrets;print(secrets.token_urlsafe(18))" > "$SECRET_FILE"
fi
PW=$(cat "$SECRET_FILE")
if ! "${C[@]}" exec -T netbox /opt/netbox/netbox/manage.py shell -c \
     "from django.contrib.auth import get_user_model as g; import sys; sys.exit(0 if g().objects.filter(username='$ADMIN_USER').exists() else 1)" >/dev/null 2>&1; then
  "${C[@]}" exec -T -e DJANGO_SUPERUSER_PASSWORD="$PW" netbox /opt/netbox/netbox/manage.py createsuperuser \
    --noinput --username "$ADMIN_USER" --email "$ADMIN_USER@example.com" >/dev/null
  echo "관리자 계정 생성: $ADMIN_USER (비밀번호: $SECRET_FILE)"
fi

# v2 토큰(NetBox >= 4.5)은 응답에 key(식별자)와 token(비밀값)이 따로 오며, 인증 값은 nbt_<key>.<token> 입니다.
TOKEN_PY='import json,sys
d = json.load(sys.stdin)
print("nbt_" + d["key"] + "." + d["token"] if d.get("version") == 2 else (d.get("key") or ""))'

# 1) 쓰기 토큰: 사용자명/비밀번호로 발급 (provision)
resp=$(curl -fsS "${CA[@]}" -H "Content-Type: application/json" -H "Accept: application/json" \
       -d "{\"username\": \"$ADMIN_USER\", \"password\": \"$PW\", \"description\": \"asset-sync write (demo)\"}" \
       "$NETBOX_URL/api/users/tokens/provision/")
WRITE=$(printf '%s' "$resp" | python3 -c "$TOKEN_PY")
UID_=$(printf '%s' "$resp" | python3 -c 'import json,sys;print(json.load(sys.stdin)["user"]["id"])')
[ -n "$WRITE" ] || { echo "쓰기 토큰 발급 실패" >&2; exit 1; }
auth() { case "$1" in nbt_*) echo "Bearer $1";; *) echo "Token $1";; esac; }

# 2) 읽기 전용 토큰: 쓰기 토큰으로 write_enabled=false 토큰을 만듦
resp=$(curl -fsS "${CA[@]}" -H "Authorization: $(auth "$WRITE")" -H "Content-Type: application/json" -H "Accept: application/json" \
       -d "{\"user\": $UID_, \"write_enabled\": false, \"description\": \"asset-sync read-only (demo)\"}" \
       "$NETBOX_URL/api/users/tokens/")
READ=$(printf '%s' "$resp" | python3 -c "$TOKEN_PY")
[ -n "$READ" ] || { echo "읽기 토큰 발급 실패" >&2; exit 1; }

# 3) .env 에 기록 (값은 출력하지 않음)
python3 - "$WRITE" "$READ" <<'EOF'
import re, sys, pathlib
w, r = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); s = p.read_text()
s = re.sub(r"(?m)^NETBOX_WRITE_TOKEN=.*$", f"NETBOX_WRITE_TOKEN={w}", s)
s = re.sub(r"(?m)^NETBOX_TOKEN=.*$", f"NETBOX_TOKEN={r}", s)
p.write_text(s)
EOF
chmod 600 .env
# 4) 읽기 토큰이 정말 읽기 전용인지 확인: 빈 POST 가 403 이어야 함
code=$(curl -s -o /dev/null -w '%{http_code}' "${CA[@]}" -H "Authorization: $(auth "$READ")" -H "Content-Type: application/json" \
       -X POST -d '{}' "$NETBOX_URL/api/extras/tags/")
echo "토큰 2개를 .env 에 기록했습니다. 읽기 토큰으로 쓰기 시도: HTTP $code (403 이면 정상)"
