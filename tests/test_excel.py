import copy
import json
import sys
from pathlib import Path

import openpyxl
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from asset_sync import excelimport, hostsparse, normalize  # noqa: E402
from asset_sync.sync import Syncer  # noqa: E402
from fakenetbox import FakeNetBox  # noqa: E402

SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yml").read_text())
SECRET = "SuperSecret!!42"
HDR = ["", "서버 위치", "모델명", "S/N", "담당자\n(서버용도)", "사용 기간", "MGMT IP", "MGMT MAC", "SW MGMT\nport",
       "hostname", "OS IP", "OS MAC", "SW OS\nport", "Storage IP", "ID / PW", "PSU", "비고"]


def _row(ws, r, cat, pos, model="", sn="", mgmt="", host="", osip="", osmac=""):
    vals = [cat, pos, model, sn, "홍길동 (용도)", "영구", mgmt, "", "", host, osip, osmac, "", "", f"admin / {SECRET}", "800",
            f"BMC admin / {SECRET}"]
    for c, v in enumerate(vals, 1):
        ws.cell(r, c, v)


@pytest.fixture(scope="module")
def xlsx(tmp_path_factory):
    p = tmp_path_factory.mktemp("xl") / "dt.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "장비 목록"
    for c, h in enumerate(HDR, 1):
        ws.cell(1, c, h)
    _row(ws, 2, "서버실", "A01-42", "HPE5520 Switch", "CN0001", "198.51.100.233", "sw01", "")      # network -> excluded
    _row(ws, 3, "서버실", "A01-41", "DL380 Gen10", "SGH111", "198.51.100.11", "gpu-web01", "192.0.2.11",
         "aa-bb-cc-00-00-01")                                                                          # 2U server
    _row(ws, 4, "서버실", "A01-40")
    for c in range(3, 18):
        ws.merge_cells(start_row=3, start_column=c, end_row=4, end_column=c)
    _row(ws, 5, "서버실", "A02-10", "DGX B200", "1660925", "198.51.100.12", "DGX B200 (임대)", "203.0.113.12")  # bad label
    _row(ws, 6, "VM", "Nutanix", "", "", "", "app01", "192.0.2.30")                              # VM
    _row(ws, 7, "사무실", "", "DL360 Gen9", "SGX9", "", "office01", "192.0.2.205")                    # out of scope
    _row(ws, 8, "서버실", "A02-09", "DL380 Gen11", "SGH222", "", "dup01", "192.0.2.40")
    _row(ws, 9, "서버실", "A02-08", "DL380 Gen11", "SGH333", "", "dup01", "192.0.2.41")           # duplicate host
    _row(ws, 10, "서버실", "A02-07", "DL380 Gen11", "SGH444", "", "-", "192.0.2.375")             # typo IP, no name
    wb.save(p)
    return p


@pytest.fixture(scope="module")
def imported(xlsx):
    return excelimport.build_hosts_from_excel(xlsx, SETTINGS, {"hosts": {"gpu-web01": {"os_family": "linux"}}})


def test_credentials_never_read(xlsx, imported):
    hosts, rows = imported
    dump = json.dumps([hosts, rows], ensure_ascii=False, default=str)
    assert SECRET not in dump and "홍길동" not in dump and "영구" not in dump


def test_scope_and_roles(imported):
    hosts, rows = imported
    st = {r["row"]: (r["status"], r["issues"]) for r in rows}
    assert st[2] == ("excluded", ["not_a_server_model"])
    assert st[7] == ("excluded", ["category:사무실"])
    assert hosts["gpu-web01"]["role"] == "server" and hosts["app01"]["role"] == "vm"
    assert hosts["app01"]["cluster"] == "demo-nutanix" and hosts["app01"]["rack"] is None


def test_merged_u_height_and_rack_position(imported):
    xl = imported[0]["gpu-web01"]["excel"]
    assert (xl["rack"], xl["u_height"], xl["position"]) == ("A01", 2, 40)  # rows 41..40 -> lowest U 40
    assert xl["bmc_ip"] == "198.51.100.11" and xl["os_mac"] == "AA:BB:CC:00:00:01"
    assert imported[0]["gpu-web01"]["site"] == "idc-a" and imported[0]["gpu-web01"]["os_family"] == "linux"


def test_role_rules():
    rules = SETTINGS["excel"]["role_rules"]
    r = lambda m: excelimport.classify_role(m, rules)  # noqa: E731
    assert r("Cray CS 2U24 SSU v2") is None and r("HPE Cray SC 20TB SAS") is None
    assert r("Cray XD670 Air") == r("QuantaGrid S74G-2U 1S7GZ9Z0000") == r("DGX B300") == "gpu-server"
    assert r("HPE DL380 Gen11") == r("DX380 Gen10") == "server"
    assert r("Nimble HF20") is r("QM9790 NDR Switch") is r("SRT 10000") is None


def test_unusable_label_keyed_by_ip(imported):
    h = imported[0]["ip-203-0-113-12"]
    assert h["name_trusted"] is False and h["role"] == "gpu-server" and h["ansible_host"] == "203.0.113.12"


def test_duplicates_and_bad_ip(imported):
    hosts, rows = imported
    dups = [k for k in hosts if k.startswith("dup01")]
    assert len(dups) == 2 and all(hosts[k]["status"] == "review" for k in dups)
    r10 = next(r for r in rows if r["row"] == 10)
    assert r10["status"] == "invalid" and "invalid_ip:192.0.2.375" in r10["issues"]
    inv = hostsparse.build_inventory(hosts)["all"]["children"]
    assert all(k in inv["skipped"]["hosts"] for k in dups)
    assert "gpu-web01" in inv["linux"]["hosts"] and "app01" in inv["unclassified"]["hosts"]


# ------------------------------------------------------------------ cross-check + sync
def _raw(tmp_path, serial="SGH111", hostname="gpu-web01", model="ProLiant DL380 Gen10"):
    raw = tmp_path / "raw"
    (raw / "linux").mkdir(parents=True)
    (raw / "linux" / "gpu-web01.json").write_text(json.dumps({
        "host": "gpu-web01", "collector": "linux_ssh", "status": "ok", "connected_ip": "192.0.2.11", "privileged": True,
        "collected_at": "2026-10-28T01:00:00+00:00",
        "facts": {"ansible_hostname": hostname, "ansible_system_vendor": "HPE", "ansible_product_name": model,
                  "ansible_product_serial": serial, "ansible_distribution": "Rocky", "ansible_distribution_version": "9.4",
                  "ansible_virtualization_role": "NA", "ansible_virtualization_type": "NA", "ansible_interfaces": ["ens1f0"],
                  "ansible_ens1f0": {"type": "ether", "module": "ice", "macaddress": "aa:bb:cc:00:00:01",
                                     "ipv4": {"address": "192.0.2.11", "netmask": "255.255.255.0"}}}}))
    return raw


def _nb():
    nb = FakeNetBox()
    nb.add("dcim/sites", name="데모 IDC-A", slug="idc-a")
    nb.add("dcim/device-roles", name="Server", slug="server")
    nb.add("extras/tags", name="asset-sync", slug="asset-sync")
    loc = nb.add("dcim/locations", name="서버실", slug="idc-a-sr", site=1)
    nb.add("dcim/racks", name="A01", site=1, location=loc["id"])
    return nb


def test_excel_confirmed_then_rack_applied(imported, tmp_path):
    hosts = {"gpu-web01": copy.deepcopy(imported[0]["gpu-web01"])}
    a = normalize.build_assets(hosts, _raw(tmp_path), SETTINGS)[0]
    assert a["excel_diffs"] == [] and "+excel" in a["fields"]["serial"]["source"] and "+excel" in a["fields"]["model"]["source"]
    assert a["fields"]["bmc_ip"]["confidence"] == "medium"  # BMC not queried: Excel value only -> review
    nb = _nb()
    run = Syncer(nb, SETTINGS, apply=True).run([a])
    dev = next(iter(nb.db["dcim/devices"].values()))
    assert dev["rack"] == 5 and dev["location"] == 4 and "position" not in dev
    assert "bmc_ip" not in dev.get("custom_fields", {})
    assert any(r["field"] == "position" and "U40" in str(r["collected_value"]) for r in run["review"])
    assert any(r["field"] == "bmc_ip" and r["confidence"] == "medium" for r in run["review"])


def test_excel_serial_mismatch_blocks_rack(imported, tmp_path):
    hosts = {"gpu-web01": copy.deepcopy(imported[0]["gpu-web01"])}
    a = normalize.build_assets(hosts, _raw(tmp_path, serial="ZZZ999"), SETTINGS)[0]
    assert a["fields"]["serial"]["value"] == "ZZZ999" and a["excel_diffs"][0]["field"] == "serial"
    nb = _nb()
    run = Syncer(nb, SETTINGS, apply=True).run([a])
    dev = next(iter(nb.db["dcim/devices"].values()))
    assert dev["serial"] == "ZZZ999" and "rack" not in dev            # collected wins, Excel rack not trusted
    rows = [r for r in run["review"] if r["object"] == "excel" or r["field"] == "rack"]
    assert len(rows) == 2 and "SGH111" not in json.dumps(rows) and "ZZZ999" not in json.dumps(rows)  # masked


def test_untrusted_label_named_from_host(imported, tmp_path):
    h = copy.deepcopy(imported[0]["ip-203-0-113-12"])
    h["os_family"] = "linux"
    raw = _raw(tmp_path, serial="1660925", hostname="dgx-b200-01", model="DGX B200")
    (raw / "linux" / "gpu-web01.json").rename(raw / "linux" / "ip-203-0-113-12.json")
    a = normalize.build_assets({"ip-203-0-113-12": h}, raw, SETTINGS)[0]
    nb = _nb()
    nb.add("dcim/device-roles", name="GPU Server", slug="gpu-server")
    nb.add("dcim/racks", name="A02", site=1, location=4)
    Syncer(nb, SETTINGS, apply=True).run([a])
    dev = next(iter(nb.db["dcim/devices"].values()))
    assert dev["name"] == "dgx-b200-01" and dev["rack"] == 7


# ------------------------------------------------------------------ BMC-only (no OS account)
def _bmc_raw(tmp_path, serial, host_ip="198.51.100.11", hostname=""):
    raw = tmp_path / "raw"
    (raw / "bmc").mkdir(parents=True)
    (raw / "bmc" / f"{host_ip}.json").write_text(json.dumps({
        "host": host_ip, "collector": "bmc_redfish", "status": "ok", "connected_ip": host_ip, "tls": "pinned",
        "collected_at": "2026-10-28T02:00:00+00:00",
        "systems": [{"manufacturer": "HPE", "model": "ProLiant DL380 Gen10", "serial": serial, "hostname": hostname}],
        "manager_interfaces": [{"id": "1", "mac": "aa:bb:cc:99:00:01", "ipv4": [{"address": host_ip, "netmask": "255.255.255.0"}]}]}))
    return raw


def _bmc_only_host(imported):
    h = copy.deepcopy(imported[0]["gpu-web01"])
    h["os_family"] = None
    return {"gpu-web01": h}


def test_bmc_serial_confirms_excel_name(imported, tmp_path):
    a = normalize.build_assets(_bmc_only_host(imported), _bmc_raw(tmp_path, "SGH111", hostname="other-name"), SETTINGS)[0]
    assert a["fields"]["hostname"]["confidence"] == "high" and "confirmed_by_bmc_serial" in a["fields"]["hostname"]["source"]
    assert a["fields"]["bmc_ip"]["confidence"] == "high" and a["kind"] == "device"
    assert "bmc_reported_hostname_differs:other-name" in a["notes"]
    nb = _nb()
    run = Syncer(nb, SETTINGS, apply=True).run([a])
    dev = next(iter(nb.db["dcim/devices"].values()))
    assert (dev["name"], dev["serial"], dev["rack"], dev["custom_fields"]["bmc_ip"]) == ("gpu-web01", "SGH111", 5, "198.51.100.11")
    assert run["results"][0]["status"] == "changed"
    n = len(nb.writes())
    Syncer(nb, SETTINGS, apply=True).run([a])
    assert len(nb.writes()) == n  # idempotent


def test_bmc_serial_mismatch_holds_asset(imported, tmp_path):
    a = normalize.build_assets(_bmc_only_host(imported), _bmc_raw(tmp_path, "OTHER99"), SETTINGS)[0]
    assert "hostname" not in a["fields"]
    nb = _nb()
    run = Syncer(nb, SETTINGS, apply=True).run([a])
    assert run["results"][0]["reason"] == "hostname_unverified" and nb.writes() == []


def test_bmc_multiple_systems_picks_host(tmp_path):
    raw = {"collector": "bmc_redfish", "collected_at": "2026-10-28T00:00:00+00:00", "connected_ip": "198.51.100.202",
           "systems": [{"id": "HGX_Baseboard_0", "manufacturer": "NVIDIA", "model": None, "serial": None},
                       {"id": "Self", "manufacturer": "HPE", "model": "HPE Cray XD670", "serial": "SGX123"}],
           "manager_interfaces": []}
    f = normalize.from_bmc(raw)["fields"]
    assert (f["manufacturer"]["value"], f["model"]["value"], f["serial"]["value"]) == ("HPE", "HPE Cray XD670", "SGX123")
    raw["systems"].append({"id": "Node2", "manufacturer": "HPE", "model": "Cray XD670 Node", "serial": "SGX999"})
    assert "serial" not in normalize.from_bmc(raw)["fields"]  # two real hosts: ambiguous, take nothing


def test_skip_blocks_bmc_registration(imported, tmp_path):
    hosts = _bmc_only_host(imported)
    hosts["gpu-web01"].update(skip=True, skip_reason="test")
    a = normalize.build_assets(hosts, _bmc_raw(tmp_path, "SGH111"), SETTINGS)[0]
    assert not any(c.get("status") == "ok" for c in a["collection"])
    nb = _nb()
    run = Syncer(nb, SETTINGS, apply=True).run([a])
    assert run["results"][0]["status"] == "skipped" and nb.writes() == []


def test_generic_and_override_names(xlsx):
    wb = openpyxl.load_workbook(xlsx)
    ws = wb.active
    _row(ws, 11, "서버실", "A03-01", "DL360 Gen10", "SGL001", "198.51.100.21", "localhost", "192.0.2.21")
    _row(ws, 12, "서버실", "A03-02", "DL360 Gen10", "SGL002", "198.51.100.22", "AHV 노드 (설명)", "192.0.2.22")
    p = xlsx.parent / "dt2.xlsx"
    wb.save(p)
    hosts, rows = excelimport.build_hosts_from_excel(p, SETTINGS, {"hosts": {"ip-192-0-2-22": {"name": "ahv01"}}})
    assert hosts["ip-192-0-2-21"]["name_trusted"] is False            # "localhost" never used as a name
    assert "ahv01" in hosts and hosts["ahv01"]["name_trusted"] is True    # human-assigned via host_overrides
    assert hosts["ahv01"]["rack"] == "A03" and hosts["ahv01"]["ansible_host"] == "192.0.2.22"
