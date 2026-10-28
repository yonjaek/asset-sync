#!/usr/bin/env python3
"""데모용 가짜 데이터 생성기.

가상 회사 "데모클라우드"의 IDC-A 서버실을 만들어 냅니다. 실제 조직, 장비, 주소와 무관합니다.
- IP: RFC 5737 문서용 대역만 사용 (OS 192.0.2.0/24, GPU 203.0.113.0/24, BMC 198.51.100.0/24)
- 시리얼: DEMO 접두어의 가짜 값
- 비밀번호 열: 의도적으로 넣은 가짜 값 (가져오기 단계가 이 열을 읽지 않는다는 것을 보여 주기 위함)

출력
- demo/data/demo_assets.xlsx       : 실제 현업 엑셀과 같은 형식 (병합 셀 = U 높이, A열 = 분류)
- demo/mock_bmc/scenario.json      : 가짜 Redfish BMC가 응답할 내용과 동작
엑셀과 BMC 값은 일부러 몇 군데 어긋나게 만들어, 대조/검토 기능이 무엇을 잡아내는지 시연합니다.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = Path(__file__).resolve().parent
SEED = 20261028
FAKE_PW = "demo / not-a-real-password"
HDR = ["", "서버 위치", "모델명", "S/N", "담당자\n(서버용도)", "사용 기간", "MGMT IP", "MGMT MAC", "SW MGMT\nport",
       "hostname", "OS IP", "OS MAC", "SW OS\nport", "Storage IP", "ID / PW", "PSU", "비고"]

# (hostname, excel model | label_only: hostname-cell description, (bmc manufacturer, bmc model), U, rack, top U, scenario)
# scenario: ok | slow | auth_fail | unreachable | serial_typo | model_alias | model_wrong | label_only | generic_label | hgx
SERVERS = [
    ("web-01", "DL360 Gen10", ("HPE", "ProLiant DL360 Gen10"), 1, "A01", 40, "ok"),
    ("web-02", "DL360 Gen10", ("HPE", "ProLiant DL360 Gen10"), 1, "A01", 39, "ok"),
    ("web-03", "DL360 Gen11", ("HPE", "ProLiant DL360 Gen11"), 1, "A01", 38, "ok"),
    ("app-01", "DL380 Gen10", ("HPE", "ProLiant DL380 Gen10"), 2, "A01", 36, "ok"),
    ("app-02", "DL380 Gen10", ("HPE", "ProLiant DL380 Gen10"), 2, "A01", 34, "ok"),
    ("db-01", "DL380 Gen11", ("HPE", "ProLiant DL380 Gen11"), 2, "A02", 40, "ok"),
    ("db-02", "DL380 Gen11", ("HPE", "ProLiant DL380 Gen11"), 2, "A02", 38, "serial_typo"),
    ("k8s-node-01", "DL380 Gen10 Plus", ("HPE", "ProLiant DL385 Gen10 Plus"), 2, "A02", 36, "model_wrong"),
    ("k8s-node-02", "DL380 Gen10 Plus", ("HPE", "ProLiant DL380 Gen10 Plus"), 2, "A02", 34, "ok"),
    ("k8s-node-03", "DL380 Gen10 Plus", ("HPE", "ProLiant DL380 Gen10 Plus"), 2, "A02", 32, "ok"),
    ("storage-01", "DL380 Gen11", ("HPE", "ProLiant DL380 Gen11"), 2, "A03", 40, "slow"),
    ("storage-02", "DL380 Gen11", ("HPE", "ProLiant DL380 Gen11"), 2, "A03", 38, "slow"),
    ("vdi-01", "PowerEdge R750", ("Dell Inc.", "PowerEdge R750"), 2, "A03", 36, "ok"),
    ("vdi-02", "PowerEdge R750", ("Dell Inc.", "PowerEdge R750"), 2, "A03", 34, "auth_fail"),
    ("gpu-01", "Apollo6500 Gen10+", ("HPE", "ProLiant XL675d Gen10 Plus"), 6, "A04", 40, "model_alias"),
    ("gpu-02", "Apollo6500 Gen10+", ("HPE", "ProLiant XL675d Gen10 Plus"), 6, "A04", 34, "model_alias"),
    ("gpu-h100-01", "Cray XD670 Air", ("HPE", "HPE Cray XD670"), 5, "A04", 26, "hgx"),
    ("ahv-01", "AHV 클러스터 노드 1 (데모)", ("HPE", "ProLiant DX380 Gen10 8SFF"), 2, "A05", 40, "label_only"),
    ("ahv-02", "AHV 클러스터 노드 2 (데모)", ("HPE", "ProLiant DX380 Gen10 8SFF"), 2, "A05", 38, "label_only"),
    ("test", "DL360 Gen9", ("HPE", "ProLiant DL360 Gen9"), 1, "A05", 36, "generic_label"),
    ("legacy-01", "DL360 Gen9", ("HPE", "ProLiant DL360 Gen9"), 1, "A05", 35, "unreachable"),
]
NON_SERVERS = [  # imported rows that must be excluded by role rules
    ("A06", 42, 1, "HPE FlexFabric 5945 32QSFP28 Switch", "core-sw-01"),
    ("A06", 41, 1, "Aruba 6300M 48G Switch", "tor-sw-01"),
    ("A06", 30, 2, "Nimble HF20", "nimble-01"),
    ("A06", 10, 2, "SRT 10000 UPS", ""),
]
VMS = [("vm-web-01", "Nutanix"), ("vm-web-02", "Nutanix"), ("vm-ci-01", "Proxmox"), ("vm-mon-01", "ESXi"),
       ("젠킨스 서버 (데모)", "Proxmox"), ("vm-dup", "Nutanix"), ("vm-dup", "Proxmox")]


def serial(rng: random.Random, prefix: str = "DEMO") -> str:
    return prefix + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(6))


def mac(rng: random.Random) -> str:
    return "02:" + ":".join(f"{rng.randrange(256):02X}" for _ in range(5))  # locally administered


def main() -> None:
    rng = random.Random(SEED)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "장비 목록"
    for c, h in enumerate(HDR, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
        cell.alignment = Alignment(wrap_text=True, horizontal="center")
    for c, w in zip("ABCDEFGHIJKLMNOPQ", (8, 10, 26, 14, 22, 10, 16, 18, 8, 26, 14, 18, 8, 12, 24, 6, 28)):
        ws.column_dimensions[c].width = w

    r = 2
    bmcs = []

    def put(row_vals, height):
        nonlocal r
        for c, v in enumerate(row_vals, 1):
            ws.cell(r, c, v)
        if height > 1:  # one device = one merged block over its U rows (same as the real sheet)
            for c in range(3, len(HDR) + 1):
                ws.merge_cells(start_row=r, start_column=c, end_row=r + height - 1, end_column=c)
            for k in range(1, height):
                ws.cell(r + k, 1, row_vals[0])
                ws.cell(r + k, 2, f"{row_vals[1].split('-')[0]}-{int(row_vals[1].split('-')[1]) - k}")
        r += height

    for i, (name, xl_model, (vendor, bmc_model), u, rack, top, scen) in enumerate(SERVERS, start=11):
        os_ip = f"{'203.0.113' if scen in ('model_alias', 'hgx') else '192.0.2'}.{i}"
        bmc_ip = f"198.51.100.{i}"
        sn = serial(rng)
        xl_sn = sn + "X" if scen == "serial_typo" else sn
        # label_only: the Excel hostname cell holds a description (as in many real sheets); generic_label: "test"
        hostname_cell = {"label_only": xl_model, "generic_label": "test"}.get(scen, name)
        excel_model = "DX380 Gen10" if scen == "label_only" else xl_model
        put(["서버실", f"{rack}-{top}", excel_model, xl_sn, "데모 담당자 (데모 용도)", "영구", bmc_ip, mac(rng), "",
             hostname_cell, os_ip, mac(rng), "", "", FAKE_PW, "800", "BMC " + FAKE_PW], u)
        b = {"ip": bmc_ip, "host": name, "scenario": scen, "vendor": vendor,
             "systems": [{"Id": "1", "Manufacturer": vendor, "Model": bmc_model, "SerialNumber": sn,
                          "AssetTag": "", "HostName": {"label_only": "", "generic_label": "localhost"}.get(scen, name),
                          "SKU": f"SKU-{sn[-4:]}"}],
             "manager": {"Id": "1", "MACAddress": mac(rng)}}
        if scen == "hgx":
            b["systems"][0]["Id"] = "Self"
            b["systems"].insert(0, {"Id": "HGX_Baseboard_0", "Manufacturer": "NVIDIA", "Model": None, "SerialNumber": None})
            b["bmc_flavor"] = "ami"
        elif vendor.startswith("Dell"):
            b["systems"][0]["Id"] = "System.Embedded.1"
            b["manager"]["Id"] = "iDRAC.Embedded.1"
            b["bmc_flavor"] = "idrac"
        else:
            b["bmc_flavor"] = "ilo"
        bmcs.append(b)

    for rack, top, u, model, host in NON_SERVERS:
        put(["서버실", f"{rack}-{top}", model, serial(rng, "DEMONS"), "데모 담당자 (네트워크)", "영구", "", "", "",
             host, "", "", "", "", FAKE_PW, "", ""], u)
    put(["사무실", "", "DL360 Gen9", serial(rng), "데모 담당자 (사무실 테스트)", "", "", "", "", "office-test", "192.0.2.250",
         "", "", "", FAKE_PW, "", "서버실 밖 장비: 가져오기 대상 아님"], 1)
    put(["서버실", "A05-20", "DL360 Gen10", serial(rng), "데모 담당자", "", "", "", "", "-", "192.0.2.375", "", "", "",
         FAKE_PW, "", "IP 오타 예시"], 1)
    for j, (name, plat) in enumerate(VMS, start=101):
        put(["VM", plat, "", "", "데모 담당자 (VM)", "", "", "", "", name, f"192.0.2.{j}", "", "", "", FAKE_PW, "", ""], 1)

    out = ROOT / "data" / "demo_assets.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    scen = ROOT / "mock_bmc" / "scenario.json"
    scen.write_text(json.dumps({"bmcs": bmcs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"servers={len(SERVERS)} non-servers={len(NON_SERVERS)} vms={len(VMS)} -> {out.relative_to(ROOT.parent)}, "
          f"{scen.relative_to(ROOT.parent)}")


if __name__ == "__main__":
    main()
