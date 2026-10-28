# CLAUDE.md — asset-sync

hosts 파일 또는 자산 엑셀에서 출발해 IT 자산을 수집·검증하고 NetBox에 등록/동기화하는 프로젝트.
이 저장소는 포트폴리오용 데모이며 모든 데이터는 가짜다 (`demo/`). 실제 조직 데이터를 추가하지 않는다.

## 반드시 지킬 규칙
- **NetBox 쓰기는 사용자 승인 후에만**: `assetctl.py sync`는 기본 dry-run이다. `--apply`, `netbox_bootstrap.yml -e apply=true`,
  `deploy_netbox.sh --apply`는 사용자가 이번 대화에서 명시적으로 승인했을 때만 실행한다. 이전 승인은 다음 작업으로 이어지지 않는다.
- **삭제 금지**: 삭제 코드는 없다. 추가하지 말 것. 오래된(stale) 객체는 보고만 한다.
- **대량 등록 금지**: `sync.max_hosts`(기본 5)를 사용자 승인 없이 올리지 않는다.
- **무단 스캔·대상 변경 금지**: nmap/ping sweep/포트 스캔 금지. 수집은 host_overrides.yml에 OS가 지정된 호스트만,
  읽기 전용(facts, Get-CimInstance, Redfish GET, ipmitool print)으로 한다.
- **값을 지어내지 않는다**: placeholder(`To Be Filled By O.E.M.` 등)는 버린다. NIC 이름은 수집된 그대로 쓴다(eth1 등으로 바꾸지 말 것).
- **덮어쓰기 금지**: NetBox에 값이 있고 다르면 review_report.csv로 보낸다.
- **비밀 정보**: 토큰/비밀번호는 `.env`(gitignore) 또는 Ansible Vault에만 둔다. 시리얼·토큰을 대화, 커밋, 로그에 출력하지 않는다
  (`common.mask()` 사용). `data/`, `reports/`, `inventory/hosts.generated.yml`은 커밋하지 않는다.

## MCP
- `netbox` MCP(netboxlabs/netbox-mcp-server)는 **읽기 전용**이며 조회·검증에만 쓴다. 쓰기는 `scripts/assetctl.py sync --apply`
  또는 `playbooks/netbox_bootstrap.yml`로 한다.

## 주요 명령
```bash
set -a; . ./.env; set +a
.venv/bin/python -m pytest -q tests                     # 단위 테스트 (NetBox/서버 불필요)
.venv/bin/python scripts/assetctl.py parse --hosts inventory/hosts_input/hosts --pilot inventory/pilot.txt
.venv/bin/ansible-playbook playbooks/collect_linux.yml -l pilot
.venv/bin/ansible-playbook playbooks/collect_windows.yml -l pilot
.venv/bin/python scripts/assetctl.py bmc --limit inventory/pilot.txt
.venv/bin/python scripts/assetctl.py build --limit inventory/pilot.txt
.venv/bin/python scripts/assetctl.py check
.venv/bin/python scripts/assetctl.py sync --limit inventory/pilot.txt     # dry-run
.venv/bin/python scripts/assetctl.py report
```

## 데모
```bash
make setup env netbox netbox-init bootstrap-apply   # 최초 1회
make demo-data mock-bmc demo                         # 가짜 엑셀 + 가짜 BMC → dry-run 계획
make demo-apply demo-rerun                           # 반영(확인 프롬프트) → 쓰기 0건 확인
```
- 가짜 BMC는 198.51.100.0/24(RFC 5737)에서 동작한다. 실제 장비 주소로 바꾸지 않는다.
- 데모 시나리오를 바꾸려면 `demo/generate_demo_data.py`의 `SERVERS`를 고치고 `make demo-data`를 다시 실행한다.

## 구조
- `scripts/asset_sync/hostsparse.py`: hosts 파싱/검증/inventory 생성
- `scripts/asset_sync/normalize.py`: 수집 원본 → 필드별 {value, source, collected_at, confidence}
- `scripts/asset_sync/sync.py`: 식별자 검증 → 생성 / 빈 값 채우기 / review. dry-run과 apply는 같은 코드 경로를 쓴다(`Writer`)
- `scripts/asset_sync/excelimport.py`: 자산 엑셀 가져오기 (비밀번호/비고/담당자 열은 읽지 않음)
- `scripts/asset_sync/bmc.py`: Redfish GET / ipmitool 읽기, 인증서 지문 고정
- `playbooks/collect_*.yml`: 읽기 전용 수집, 실패 사유는 `data/raw/<os>/<host>.status.json`
- 테스트는 `tests/fakenetbox.py`(인메모리 NetBox)를 사용한다. sync 규칙을 바꾸면 테스트도 같이 바꾼다.
