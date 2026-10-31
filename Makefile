# asset-sync 데모 실행용 Makefile
# 순서: make setup → make env → make netbox → make netbox-init → make bootstrap-apply
#       → make demo-data mock-bmc → make seed-apply → make demo → make demo-apply → make demo-rerun
SHELL := /bin/bash
PYTHON ?= $(shell command -v python3.12 || command -v python3.11 || command -v python3)
PY := .venv/bin/python
AP := .venv/bin/ansible-playbook
ENV := set -a; . ./.env; set +a;
# 데모 전용 쓰기 상한 (서버 약 150대). 실제 환경은 settings.yml 의 sync.max_hosts(기본 5)부터 단계적으로 올린다
DEMO_MAX_HOSTS ?= 200

.PHONY: help setup env test demo-data mock-bmc mock-bmc-down netbox netbox-init bootstrap bootstrap-apply \
        seed seed-apply demo demo-apply demo-rerun report mcp

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

setup: ## Python venv + 의존성 + Ansible collection 설치
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -r requirements.txt
	.venv/bin/ansible-galaxy collection install -r requirements.yml -p ./collections >/dev/null

env: ## .env.example → .env (이미 있으면 그대로 둠)
	@[ -f .env ] && echo ".env 이미 있음" || { cp .env.example .env; chmod 600 .env; echo ".env 생성"; }

test: ## 단위 테스트 (NetBox/서버 불필요)
	$(PY) -m pytest -q tests

demo-data: ## 가짜 자산 엑셀 + 가짜 BMC 시나리오 생성
	$(PY) demo/generate_demo_data.py

mock-bmc: ## 가짜 Redfish BMC 컨테이너 기동 (198.51.100.0/24)
	@[ -f demo/mock_bmc/scenario.json ] || $(MAKE) demo-data
	$(ENV) docker compose -f demo/mock_bmc/docker-compose.yml up -d --build

mock-bmc-down: ## 가짜 BMC 중지/삭제
	docker compose -f demo/mock_bmc/docker-compose.yml down

netbox: ## netbox-docker 배포 (dry-run 계획을 먼저 보고 싶으면: scripts/deploy_netbox.sh)
	scripts/deploy_netbox.sh --apply

netbox-init: ## 관리자 계정 + 쓰기/읽기 토큰 생성 후 .env 에 기록
	demo/netbox_init.sh

bootstrap: ## Site/Rack/Role/Cluster/커스텀 필드 계획 (check 모드, 쓰기 없음)
	$(ENV) $(AP) -i localhost, playbooks/netbox_bootstrap.yml

bootstrap-apply: ## 위 분류 체계를 실제로 생성
	$(ENV) $(AP) -i localhost, playbooks/netbox_bootstrap.yml -e apply=true

seed: ## 네트워크·설비 데모 장비/IPAM 계획 (check 모드, 쓰기 없음)
	@[ -f demo/data/netbox_seed.json ] || $(MAKE) demo-data
	$(ENV) $(AP) -i localhost, playbooks/netbox_demo_seed.yml

seed-apply: ## 위 데모 장비/IPAM 을 실제로 생성 (스위치·방화벽·PDU·UPS·항온항습기·VLAN·Prefix)
	@[ -f demo/data/netbox_seed.json ] || $(MAKE) demo-data
	$(ENV) $(AP) -i localhost, playbooks/netbox_demo_seed.yml -e apply=true

demo: ## 엑셀 가져오기 → BMC 인증서 고정 → Redfish 수집 → 대조 → dry-run 계획 → 보고
	$(ENV) $(PY) scripts/assetctl.py import-excel --file "$$ASSET_SYNC_EXCEL_FILE" --site "$$ASSET_SYNC_EXCEL_SITE"
	$(ENV) $(PY) scripts/assetctl.py bmc-trust
	$(ENV) $(PY) scripts/assetctl.py bmc
	$(ENV) $(PY) scripts/assetctl.py build
	$(ENV) $(PY) scripts/assetctl.py sync
	$(ENV) $(PY) scripts/assetctl.py report

demo-apply: ## dry-run 계획대로 NetBox 에 반영 (확인 프롬프트에 'apply' 입력)
	$(ENV) $(PY) scripts/assetctl.py sync --apply --max-hosts $(DEMO_MAX_HOSTS)

demo-rerun: ## 멱등성 확인: 다시 돌리면 쓰기 0건이어야 함
	$(ENV) $(PY) scripts/assetctl.py sync

report: ## 마지막 실행 결과 요약 (success / failed / unverified / duplicate)
	$(ENV) $(PY) scripts/assetctl.py report

mcp: ## 읽기 전용 netbox-mcp-server 설치 + .mcp.json 생성
	scripts/setup_mcp.sh
