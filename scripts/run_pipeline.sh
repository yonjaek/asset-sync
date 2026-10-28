#!/usr/bin/env bash
# parse -> collect (linux/windows/bmc) -> build -> sync plan [-> apply]
# Usage: scripts/run_pipeline.sh [--limit pilot|host1,host2] [--apply] [--yes]
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
PY=.venv/bin/python; AP=.venv/bin/ansible-playbook
LIMIT=""; APPLY=(); 
while [ $# -gt 0 ]; do case "$1" in
  --limit) LIMIT="$2"; shift 2;; --apply) APPLY+=(--apply); shift;; --yes) APPLY+=(--yes); shift;;
  *) echo "unknown arg $1" >&2; exit 1;; esac; done
HOSTS="${ASSET_SYNC_HOSTS_FILE:-inventory/hosts_input/hosts}"
PILOT_ARG=(); [ -f inventory/pilot.txt ] && PILOT_ARG=(--pilot inventory/pilot.txt)
if [ -n "${ASSET_SYNC_EXCEL_FILE:-}" ]; then
  $PY scripts/assetctl.py import-excel --file "$ASSET_SYNC_EXCEL_FILE" ${ASSET_SYNC_EXCEL_SITE:+--site "$ASSET_SYNC_EXCEL_SITE"} "${PILOT_ARG[@]}"
else
  $PY scripts/assetctl.py parse --hosts "$HOSTS" "${PILOT_ARG[@]}"
fi
VAULT=(); [ -f inventory/group_vars/all/vault.yml ] && [ -n "${ANSIBLE_VAULT_PASSWORD_FILE:-}" ] && VAULT=(--vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE")
LIM=(); HOSTLIST=()
if [ -n "$LIMIT" ]; then LIM=(--limit "$LIMIT"); if [ "$LIMIT" = pilot ]; then HOSTLIST=(--limit inventory/pilot.txt); else HOSTLIST=(--limit "$LIMIT"); fi; fi
$AP playbooks/collect_linux.yml "${LIM[@]}" "${VAULT[@]}" || true
$AP playbooks/collect_windows.yml "${LIM[@]}" "${VAULT[@]}" || true
$PY scripts/assetctl.py bmc "${HOSTLIST[@]}"
$PY scripts/assetctl.py build "${HOSTLIST[@]}"
MAX=(); [ -n "${ASSET_SYNC_MAX_HOSTS:-}" ] && MAX=(--max-hosts "$ASSET_SYNC_MAX_HOSTS")
$PY scripts/assetctl.py sync "${HOSTLIST[@]}" "${MAX[@]}" "${APPLY[@]}"
$PY scripts/assetctl.py report
