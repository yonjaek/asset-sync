#!/usr/bin/env python3
"""데모용 가짜 데이터 생성기.

가상 회사 "데모클라우드"의 IDC 3곳(판교 메인 / 동탄 AI / 용인 DR)을 만들어 냅니다. 실제 조직, 장비, 주소와 무관합니다.
- IP: RFC 5737 문서용 대역만 사용 (OS 192.0.2.0/24 + 203.0.113.0/24, BMC 198.51.100.0/24), IPv6는 2001:db8::/32
- 시리얼: DEMO / DEMONS 접두어의 가짜 값
- 비밀번호 열: 의도적으로 넣은 가짜 값 (가져오기 단계가 이 열을 읽지 않는다는 것을 보여 주기 위함)

출력
- demo/data/demo_assets.xlsx       : 현업 엑셀과 같은 형식 (IDC별 시트, 병합 셀 = U 높이, A열 = 분류)
- demo/mock_bmc/scenario.json      : 가짜 Redfish BMC가 응답할 내용과 동작
- demo/data/netbox_seed.json       : 네트워크·설비팀이 이미 NetBox에 관리 중인 장비/IPAM (playbooks/netbox_demo_seed.yml 입력)
엑셀과 BMC 값은 일부러 몇 군데 어긋나게 만들어, 대조/검토 기능이 무엇을 잡아내는지 시연합니다.
"""
from __future__ import annotations

import ipaddress
import json
import random
from collections import Counter
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = Path(__file__).resolve().parent
SEED = 20261028
FAKE_PW = "demo / not-a-real-password"
HDR = ["", "서버 위치", "모델명", "S/N", "담당자\n(서버용도)", "사용 기간", "MGMT IP", "MGMT MAC", "SW MGMT\nport",
       "hostname", "OS IP", "OS MAC", "SW OS\nport", "Storage IP", "ID / PW", "PSU", "비고"]

# Excel model -> (BMC manufacturer, BMC model, U, BMC flavor)
MODELS = {
    "DL360 Gen10": ("HPE", "ProLiant DL360 Gen10", 1, "ilo"),
    "DL360 Gen11": ("HPE", "ProLiant DL360 Gen11", 1, "ilo"),
    "DL380 Gen10": ("HPE", "ProLiant DL380 Gen10", 2, "ilo"),
    "DL380 Gen10 Plus": ("HPE", "ProLiant DL380 Gen10 Plus", 2, "ilo"),
    "DL380 Gen11": ("HPE", "ProLiant DL380 Gen11", 2, "ilo"),
    "DL385 Gen11": ("HPE", "ProLiant DL385 Gen11", 2, "ilo"),
    "DL360 Gen9": ("HPE", "ProLiant DL360 Gen9", 1, "ilo"),
    "DX380 Gen10 Plus": ("HPE", "ProLiant DX380 Gen10 Plus 8SFF", 2, "ilo"),
    "PowerEdge R660": ("Dell Inc.", "PowerEdge R660", 1, "idrac"),
    "PowerEdge R760": ("Dell Inc.", "PowerEdge R760", 2, "idrac"),
    "PowerEdge R750": ("Dell Inc.", "PowerEdge R750", 2, "idrac"),
    "Apollo6500 Gen10+": ("HPE", "ProLiant XL675d Gen10 Plus", 6, "ilo"),   # model_alias: Excel name != BMC name
    "Cray XD670 Air": ("HPE", "HPE Cray XD670", 5, "ami"),                   # hgx: GPU baseboard exposed as a System
}

# 사이트별 엑셀 시트, 서버 그룹: (rack, hostname prefix, count, Excel model)
# 이름 뒤 번호는 001부터. 랙 안에서는 위(U38 또는 48U 랙의 U44)부터 아래로 빈틈없이 쌓습니다.
SITES = {
    "pangyo": {"sheet": "판교 IDC", "short": "pg", "ip_pool": "192.0.2.11", "vm_pool": "192.0.2.193", "racks_top": 38,
               "groups": [("A01", "pg-web", 12, "DL360 Gen11"), ("A02", "pg-app", 12, "DL360 Gen11"),
                          ("A03", "pg-k8s-m", 3, "DL360 Gen11"), ("A03", "pg-db", 6, "DL380 Gen11"),
                          ("A04", "pg-k8s-w", 8, "DL380 Gen11"), ("A05", "pg-k8s-w", 8, "DL380 Gen11"),
                          ("A06", "pg-k8s-w", 8, "DL380 Gen11"), ("A07", "pg-mon", 3, "DL360 Gen10"),
                          ("A07", "pg-bastion", 2, "DL360 Gen10"), ("A07", "pg-kafka", 6, "PowerEdge R660"),
                          ("A08", "pg-esxi", 6, "PowerEdge R760"), ("B01", "pg-ahv", 8, "DX380 Gen10 Plus"),
                          ("B02", "pg-ceph", 5, "DL380 Gen11"), ("B03", "pg-ceph", 4, "DL385 Gen11")],
               "vms": [("Nutanix", "pg-vm-web", 10), ("Nutanix", "pg-vm-api", 8), ("Nutanix", "pg-vm-batch", 6),
                       ("ESXi", "pg-vm-win", 6), ("ESXi", "pg-vm-erp", 6)]},
    "dongtan": {"sheet": "동탄 AI센터", "short": "dt", "ip_pool": "203.0.113.11", "vm_pool": "203.0.113.70", "racks_top": 44,
                "groups": [("G01", "dt-gpu-h100", 4, "Cray XD670 Air"), ("G02", "dt-gpu-h100", 4, "Cray XD670 Air"),
                           ("G03", "dt-gpu-h100", 4, "Cray XD670 Air"), ("G04", "dt-gpu-h100", 4, "Cray XD670 Air"),
                           ("G05", "dt-gpu-a100", 3, "Apollo6500 Gen10+"), ("G06", "dt-gpu-a100", 3, "Apollo6500 Gen10+"),
                           ("G07", "dt-gpu-a100", 2, "Apollo6500 Gen10+"), ("G08", "dt-login", 2, "DL360 Gen11"),
                           ("G08", "dt-mgmt", 2, "DL360 Gen11"), ("G08", "dt-data", 6, "DL380 Gen11")],
                "vms": [("Proxmox", "dt-vm-jupyter", 6), ("Proxmox", "dt-vm-mlflow", 2), ("Proxmox", "dt-vm-registry", 2)]},
    "yongin": {"sheet": "용인 DR센터", "short": "yi", "ip_pool": "192.0.2.130", "vm_pool": "203.0.113.130", "racks_top": 38,
               "groups": [("D01", "yi-web-dr", 6, "DL360 Gen10"), ("D02", "yi-db-dr", 4, "DL380 Gen10"),
                          ("D03", "yi-backup", 6, "DL380 Gen10 Plus"), ("D04", "yi-esxi", 4, "PowerEdge R750"),
                          ("D05", "yi-legacy", 2, "DL360 Gen9")],
               "vms": [("ESXi", "yi-vm-web-dr", 6), ("ESXi", "yi-vm-db-dr", 2)]},
}

# 시연용 예외 시나리오 (hostname -> scenario). 나머지는 모두 ok.
#   slow / auth_fail / unreachable : BMC 수집 실패 (사유 기록)
#   serial_typo   : 엑셀 시리얼 오타 -> 검토 (이름·랙을 신뢰할 근거 없음)
#   model_wrong   : 엑셀 모델명이 실제와 다름 -> 수집값으로 등록, 엑셀 수정 요청
#   label_only    : 엑셀 hostname 칸에 설명이 적힘 -> 검토 (사람이 이름 지정)
#   rack_conflict : 엑셀 U 위치가 네트워크 장비와 겹침 -> 장비는 등록, 랙 위치만 검토
SCENARIOS = {
    "pg-ceph-004": "slow", "pg-ceph-007": "slow", "dt-data-005": "slow",
    "pg-esxi-004": "auth_fail", "yi-esxi-003": "auth_fail",
    "yi-legacy-001": "unreachable", "yi-legacy-002": "unreachable",
    "pg-db-004": "serial_typo", "dt-gpu-h100-011": "serial_typo", "yi-backup-002": "serial_typo",
    "pg-k8s-w-013": "model_wrong", "pg-app-007": "model_wrong",
    "pg-ahv-005": "label_only", "pg-ahv-006": "label_only", "pg-ahv-007": "label_only", "pg-ahv-008": "label_only",
    "pg-mon-003": "rack_conflict", "dt-mgmt-002": "rack_conflict",
}
MODEL_WRONG_BMC = {"DL380 Gen11": ("HPE", "ProLiant DL385 Gen11"), "DL360 Gen11": ("HPE", "ProLiant DL360 Gen10 Plus")}
# 엑셀 hostname 칸에 일반 이름이 적힌 서버 (사람이 이름을 정해 줄 때까지 보류)
GENERIC = [("pangyo", "A07", "test", "DL360 Gen10"), ("yongin", "D05", "localhost", "DL360 Gen9")]

# --- 네트워크·설비 (seed) 장비 모델: (manufacturer, model, slug, U, full depth)
NET_TYPES = {
    "leaf": ("Arista", "DCS-7050SX3-48YC8", "arista-7050sx3-48yc8", 1, False),
    "oob": ("Juniper", "EX2300-48T", "juniper-ex2300-48t", 1, False),
    "ib": ("NVIDIA", "QM9700", "nvidia-qm9700", 1, False),
    "spine": ("Arista", "DCS-7280CR3-32P4", "arista-7280cr3-32p4", 1, False),
    "router": ("Juniper", "MX204", "juniper-mx204", 1, True),
    "firewall": ("Fortinet", "FortiGate 1800F", "fortinet-fg-1800f", 2, True),
    "lb": ("F5", "BIG-IP r5900", "f5-bigip-r5900", 1, True),
    "pdu": ("APC", "AP8886 Rack PDU", "apc-ap8886", 0, False),
    "ups": ("Eaton", "93PM 200kW", "eaton-93pm-200", 0, False),
    "crac": ("Vertiv", "Liebert PDX", "vertiv-liebert-pdx", 0, False),
}
# 네트워크실 랙 배치: (rack, U top, kind, name, role)
NETROOM = {
    "pangyo": [("N01", 41, "router", "pg-rtr-01", "router"), ("N01", 40, "router", "pg-rtr-02", "router"),
               ("N01", 37, "firewall", "pg-fw-01", "firewall"), ("N01", 34, "firewall", "pg-fw-02", "firewall"),
               ("N01", 31, "lb", "pg-lb-01", "load-balancer"), ("N01", 30, "lb", "pg-lb-02", "load-balancer"),
               ("N02", 41, "spine", "pg-spine-01", "spine-switch"), ("N02", 40, "spine", "pg-spine-02", "spine-switch"),
               ("N02", 39, "spine", "pg-spine-03", "spine-switch"), ("N02", 38, "spine", "pg-spine-04", "spine-switch"),
               ("N02", 35, "oob", "pg-oob-core-01", "oob-switch")],
    "dongtan": [("N01", 41, "router", "dt-rtr-01", "router"), ("N01", 40, "router", "dt-rtr-02", "router"),
                ("N01", 37, "firewall", "dt-fw-01", "firewall"), ("N01", 34, "firewall", "dt-fw-02", "firewall"),
                ("N01", 31, "spine", "dt-spine-01", "spine-switch"), ("N01", 30, "spine", "dt-spine-02", "spine-switch"),
                ("N01", 28, "ib", "dt-ib-spine-01", "spine-switch"), ("N01", 27, "ib", "dt-ib-spine-02", "spine-switch"),
                ("N01", 25, "oob", "dt-oob-core-01", "oob-switch")],
    "yongin": [("N01", 41, "router", "yi-rtr-01", "router"), ("N01", 37, "firewall", "yi-fw-01", "firewall"),
               ("N01", 34, "firewall", "yi-fw-02", "firewall"), ("N01", 31, "spine", "yi-spine-01", "spine-switch"),
               ("N01", 30, "spine", "yi-spine-02", "spine-switch"), ("N01", 28, "oob", "yi-oob-core-01", "oob-switch")],
}
# 서버 랙 수 (config/settings.yml 의 rack_groups 와 같아야 함): (site, prefix, count, U height, location)
SERVER_RACKS = [("pangyo", "A", 10, 42, "pg-hall-a"), ("pangyo", "B", 8, 42, "pg-hall-b"),
                ("dongtan", "G", 8, 48, "dt-ai-hall"), ("yongin", "D", 5, 42, "yi-dr-hall")]
NET_RACKS = [("pangyo", "N", 2, 42, "pg-netroom"), ("dongtan", "N", 1, 42, "dt-netroom"), ("yongin", "N", 1, 42, "yi-netroom")]
FACILITY = {  # (location, kind, count)
    "pangyo": [("pg-utility", "ups", 2), ("pg-hall-a", "crac", 4), ("pg-hall-b", "crac", 3)],
    "dongtan": [("dt-utility", "ups", 2), ("dt-ai-hall", "crac", 4)],
    "yongin": [("yi-utility", "ups", 2), ("yi-dr-hall", "crac", 2)],
}
# IPAM: (site, prefix, VLAN id, VLAN name, description)
IPAM = [  # must contain the address pools above (SITES ip_pool / vm_pool)
    ("pangyo", "192.0.2.0/25", 110, "PG-SVC", "판교 서비스/k8s/스토리지"), ("pangyo", "192.0.2.192/26", 130, "PG-VM", "판교 VM"),
    ("yongin", "192.0.2.128/27", 310, "YI-SVC", "용인 DR 서비스"), ("yongin", "203.0.113.128/26", 330, "YI-VM", "용인 VM"),
    ("dongtan", "203.0.113.0/26", 210, "DT-GPU", "동탄 GPU 노드"), ("dongtan", "203.0.113.64/26", 230, "DT-VM", "동탄 VM"),
    ("dongtan", "203.0.113.192/26", 220, "DT-GPU-STOR", "동탄 GPU 스토리지 (Lustre)"),
    ("pangyo", "198.51.100.0/24", 900, "OOB", "BMC/OOB 관리망 (3개 센터 공용)"),
    ("pangyo", "2001:db8:10::/48", None, None, "판교 IPv6"), ("dongtan", "2001:db8:20::/48", None, None, "동탄 IPv6"),
    ("yongin", "2001:db8:30::/48", None, None, "용인 IPv6"),
]


def serial(rng: random.Random, prefix: str = "DEMO") -> str:
    return prefix + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(6))


def mac(rng: random.Random) -> str:
    return "02:" + ":".join(f"{rng.randrange(256):02X}" for _ in range(5))  # locally administered


class Pool:
    """Sequential addresses from a start IP; refuses to leave its /24."""

    def __init__(self, start: str):
        self.ip = ipaddress.ip_address(start)
        self.net = ipaddress.ip_network(f"{start}/24", strict=False)

    def next(self) -> str:
        ip, self.ip = self.ip, self.ip + 1
        if ip not in self.net or ip == self.net.broadcast_address:
            raise SystemExit(f"address pool {self.net} exhausted")
        return str(ip)


class Sheet:
    def __init__(self, wb: openpyxl.Workbook, title: str, first: bool):
        self.ws = wb.active if first else wb.create_sheet()
        self.ws.title = title
        for c, h in enumerate(HDR, 1):
            cell = self.ws.cell(1, c, h)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="DDEBF7")
            cell.alignment = Alignment(wrap_text=True, horizontal="center")
        for c, w in zip("ABCDEFGHIJKLMNOPQ", (8, 10, 26, 14, 22, 10, 16, 18, 8, 26, 14, 18, 8, 12, 24, 6, 28)):
            self.ws.column_dimensions[c].width = w
        self.r = 2

    def put(self, row_vals: list, height: int) -> None:
        for c, v in enumerate(row_vals, 1):
            self.ws.cell(self.r, c, v)
        if height > 1:  # one device = one merged block over its U rows (same as the real sheet)
            for c in range(3, len(HDR) + 1):
                self.ws.merge_cells(start_row=self.r, start_column=c, end_row=self.r + height - 1, end_column=c)
            rack, top = row_vals[1].split("-")
            for k in range(1, height):
                self.ws.cell(self.r + k, 1, row_vals[0])
                self.ws.cell(self.r + k, 2, f"{rack}-{int(top) - k}")
        self.r += height


def rack_names(prefix: str, count: int) -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(1, count + 1)]


def main() -> None:
    rng = random.Random(SEED)
    wb = openpyxl.Workbook()
    bmc_pool = Pool("198.51.100.11")
    bmcs, seed_devices, counts = [], [], Counter()

    def seed(name, kind, role, site, location, rack=None, position=None):
        mfr, model, slug, u, _ = NET_TYPES[kind]
        d = {"name": name, "device_type": slug, "role": role, "site": site, "location": location, "serial": serial(rng, "DEMONS")}
        if rack:
            d["rack"] = rack
        if position is not None:
            d.update(position=position, face="front")
        seed_devices.append(d)
        return d

    for idx, (site, cfg) in enumerate(SITES.items()):
        sh = Sheet(wb, cfg["sheet"], idx == 0)
        ip_pool, vm_pool = Pool(cfg["ip_pool"]), Pool(cfg["vm_pool"])
        short = cfg["short"]
        # rack layout: top U holds 2 leaf switches + OOB switch (+ InfiniBand leaf in GPU racks); servers below
        cursor = {}
        for s, prefix, n, height, loc in SERVER_RACKS:
            if s != site:
                continue
            for rack in rack_names(prefix, n):
                top = height
                net = [("leaf", f"{short}-leaf-{rack.lower()}-1", "leaf-switch"), ("leaf", f"{short}-leaf-{rack.lower()}-2", "leaf-switch"),
                       ("oob", f"{short}-oob-{rack.lower()}", "oob-switch")]
                if prefix == "G":
                    net.append(("ib", f"{short}-ib-leaf-{rack.lower()}", "leaf-switch"))
                for kind, name, role in net:
                    d = seed(name, kind, role, site, loc, rack, top)
                    mfr, model, *_ = NET_TYPES[kind]
                    sh.put(["서버실", f"{rack}-{top}", f"{mfr} {model}", d["serial"], "데모 담당자 (네트워크)", "영구",
                            "", "", "", name, "", "", "", "", FAKE_PW, "", ""], 1)
                    top -= 1
                for side in ("a", "b"):
                    seed(f"{short}-pdu-{rack.lower()}-{side}", "pdu", "pdu", site, loc, rack)
                cursor[rack] = cfg["racks_top"]
        for s, prefix, n, height, loc in NET_RACKS:
            if s == site:
                for rack in rack_names(prefix, n):
                    for side in ("a", "b"):
                        seed(f"{short}-pdu-{rack.lower()}-{side}", "pdu", "pdu", site, loc, rack)
        for rack, top, kind, name, role in NETROOM[site]:
            d = seed(name, kind, role, site, f"{short}-netroom", rack, top - NET_TYPES[kind][3] + 1)
            mfr, model, *_ = NET_TYPES[kind]
            sh.put(["네트워크실", f"{rack}-{top}", f"{mfr} {model}", d["serial"], "데모 담당자 (네트워크)", "영구", "", "", "",
                    name, "", "", "", "", FAKE_PW, "", "네트워크실 장비: 가져오기 대상 아님"], NET_TYPES[kind][3])
        fac = Counter()
        for loc, kind, n in FACILITY[site]:  # numbered per site: pg-crac-01..04 (hall A), pg-crac-05..07 (hall B)
            for _ in range(n):
                fac[kind] += 1
                seed(f"{short}-{kind}-{fac[kind]:02d}", kind, kind, site, loc)

        # servers
        seq, servers = Counter(), []
        for rack, pfx, n, xl_model in cfg["groups"]:  # numbering continues across racks: pg-k8s-w-001..024
            for _ in range(n):
                seq[pfx] += 1
                servers.append((rack, f"{pfx}-{seq[pfx]:03d}", xl_model))
        servers += [(rack, None, xl_model) for s, rack, label, xl_model in GENERIC if s == site]
        generic = iter(label for s, rack, label, xl_model in GENERIC if s == site)
        for rack, name, xl_model in servers:
            scen = SCENARIOS.get(name, "ok") if name else "generic_label"
            label = name or next(generic)
            vendor, bmc_model, u, flavor = MODELS[xl_model]
            if scen == "model_wrong":
                vendor, bmc_model = MODEL_WRONG_BMC[xl_model]
            top = cursor[rack]
            cursor[rack] = top - u
            xl_top = rack_top(site) if scen == "rack_conflict" else top  # claims the OOB switch's U
            os_ip, bmc_ip = ip_pool.next(), bmc_pool.next()
            sn = serial(rng)
            xl_sn = sn + "X" if scen == "serial_typo" else sn
            hostname_cell = {"label_only": f"AHV 클러스터 노드 {label[-1]} (판교)", "generic_label": label}.get(scen, label)
            sh.put(["서버실", f"{rack}-{xl_top}", xl_model, xl_sn, "데모 담당자 (데모 용도)", "영구", bmc_ip, mac(rng), "",
                    hostname_cell, os_ip, mac(rng), "", "", FAKE_PW, "800", "BMC " + FAKE_PW], u)
            b = {"ip": bmc_ip, "host": label, "scenario": scen, "vendor": vendor, "bmc_flavor": flavor,
                 "systems": [{"Id": "1", "Manufacturer": vendor, "Model": bmc_model, "SerialNumber": sn, "AssetTag": "",
                              "HostName": {"label_only": "", "generic_label": "localhost"}.get(scen, label),
                              "SKU": f"SKU-{sn[-4:]}"}],
                 "manager": {"Id": "1", "MACAddress": mac(rng)}}
            if flavor == "ami":
                b["systems"][0]["Id"] = "Self"
                b["systems"].insert(0, {"Id": "HGX_Baseboard_0", "Manufacturer": "NVIDIA", "Model": None, "SerialNumber": None})
            elif flavor == "idrac":
                b["systems"][0]["Id"] = "System.Embedded.1"
                b["manager"]["Id"] = "iDRAC.Embedded.1"
            bmcs.append(b)
            counts["servers"] += 1
            counts[f"scenario:{scen}"] += 1

        if site == "dongtan":  # GPU storage enclosures: excluded by role rule '-' (Cray ClusterStor)
            for i, top in enumerate((20, 16), 1):
                sh.put(["서버실", f"G07-{top}", "Cray ClusterStor E1000 SSU", serial(rng, "DEMONS"), "데모 담당자 (스토리지)", "영구",
                        "", "", "", f"dt-lustre-ssu-{i:02d}", "", "", "", "", FAKE_PW, "", ""], 4)
        # rows that must not be imported: out of scope / typo
        sh.put(["사무실", "", "DL360 Gen9", serial(rng), "데모 담당자 (사무실 테스트)", "", "", "", "", f"{short}-office-test",
                "192.0.2.250" if site != "dongtan" else "203.0.113.250", "", "", "", FAKE_PW, "", "서버실 밖 장비: 가져오기 대상 아님"], 1)
        if site == "pangyo":
            sh.put(["창고", "", "DL380 Gen10", serial(rng), "데모 담당자", "", "", "", "", "pg-spare-01", "", "", "", "", FAKE_PW, "",
                    "창고 보관 예비 장비"], 1)
            sh.put(["서버실", "A09-20", "DL360 Gen10", serial(rng), "데모 담당자", "", "", "", "", "-", "192.0.2.375", "", "", "",
                    FAKE_PW, "", "IP 오타 예시"], 1)
        # VMs (hypervisor collector is the next step: imported but not collected)
        for plat, pfx, n in cfg["vms"]:
            for i in range(1, n + 1):
                sh.put(["VM", plat, "", "", "데모 담당자 (VM)", "", "", "", "", f"{pfx}-{i:02d}", vm_pool.next(), "", "", "", FAKE_PW,
                        "", ""], 1)
                counts["vms"] += 1
        if site == "pangyo":
            sh.put(["VM", "Nutanix", "", "", "데모 담당자 (VM)", "", "", "", "", "젠킨스 서버 (데모)", vm_pool.next(), "", "", "", FAKE_PW,
                    "", ""], 1)
            counts["vms"] += 1
        # same VM name registered at two sites (copied ledger rows) -> duplicate_hostname review
        if site in ("pangyo", "yongin"):
            plat = "Nutanix" if site == "pangyo" else "ESXi"
            sh.put(["VM", plat, "", "", "데모 담당자 (VM)", "", "", "", "", "vm-jenkins", vm_pool.next(), "", "", "", FAKE_PW, "", ""], 1)
            counts["vms"] += 1

    dup = [n for n, c in Counter(d["name"] for d in seed_devices).items() if c > 1]
    if dup:
        raise SystemExit(f"duplicate seed device names: {dup}")
    # device types / manufacturers for the seed (servers' types are created by asset-sync from BMC data)
    used = sorted({d["device_type"] for d in seed_devices})
    dtypes = [{"manufacturer": m, "model": mo, "slug": sl, "u_height": u, "is_full_depth": fd}
              for m, mo, sl, u, fd in NET_TYPES.values() if sl in used]
    seed_doc = {"_comment": "데모클라우드 가짜 데이터: 네트워크·설비팀이 이미 NetBox에 관리 중인 장비와 IPAM (asset-sync 대상 아님)",
                "tag": "demo-seed",
                "manufacturers": sorted({d["manufacturer"] for d in dtypes}),
                "device_types": dtypes, "devices": seed_devices,
                "vlans": [{"site": s, "vid": v, "name": n, "description": d} for s, p, v, n, d in IPAM if v],
                "prefixes": [{"site": s, "prefix": p, "vlan": {"vid": v, "site": s} if v else None, "description": d}
                             for s, p, v, n, d in IPAM]}

    out = ROOT / "data" / "demo_assets.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    (ROOT / "data" / "netbox_seed.json").write_text(json.dumps(seed_doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    scen = ROOT / "mock_bmc" / "scenario.json"
    scen.write_text(json.dumps({"bmcs": bmcs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    kinds = Counter(d["role"] for d in seed_devices)
    print(f"servers={counts['servers']} vms={counts['vms']} seed_devices={len(seed_devices)} {dict(kinds)}")
    print("scenarios: " + ", ".join(f"{k.split(':')[1]}={v}" for k, v in sorted(counts.items()) if k.startswith("scenario:")))
    print(f"-> {out.relative_to(ROOT.parent)}, demo/data/netbox_seed.json, {scen.relative_to(ROOT.parent)}")


def rack_top(site: str) -> int:
    """U of the OOB switch in a server rack (rack height - 2): a server row claiming it collides on purpose."""
    return 46 if site == "dongtan" else 40


if __name__ == "__main__":
    main()
