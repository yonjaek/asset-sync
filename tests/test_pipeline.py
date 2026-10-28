import copy
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from asset_sync import hostsparse, normalize  # noqa: E402
from asset_sync.common import is_placeholder, mask, redact  # noqa: E402
from asset_sync.sync import Syncer  # noqa: E402
from fakenetbox import FakeNetBox  # noqa: E402

FX = ROOT / "tests" / "fixtures"
SETTINGS = yaml.safe_load((FX / "settings.yml").read_text())
OVERRIDES = yaml.safe_load((FX / "overrides.yml").read_text())


@pytest.fixture(scope="module")
def entries():
    return hostsparse.parse_hosts(hostsparse.read_hosts_file(FX / "hosts.sample"), SETTINGS)


@pytest.fixture(scope="module")
def hosts(entries):
    return hostsparse.build_hosts(entries, OVERRIDES)


@pytest.fixture()
def assets(hosts):
    return {a["key"]: a for a in normalize.build_assets(copy.deepcopy(hosts), FX / "raw", SETTINGS)}


def by_line(entries, n):
    return next(e for e in entries if e.line_no == n)


# ------------------------------------------------------------------ hosts parsing
def test_loopback_excluded(entries):
    assert by_line(entries, 2).status == "excluded" and "loopback" in by_line(entries, 2).issues
    assert by_line(entries, 3).status == "excluded" and "dual_stack" not in by_line(entries, 2).issues


def test_fqdn_and_alias(entries):
    e = by_line(entries, 4)
    assert (e.hostname, e.fqdn) == ("web01", "web01.corp.example.com")
    assert e.aliases == ["www"] and e.prefix == "192.0.2.11/24"


def test_duplicate_ip_and_hostname(entries):
    assert any(i.startswith("duplicate_ip_conflict") for i in by_line(entries, 8).issues)
    assert by_line(entries, 9).status == "review"
    assert any(i.startswith("duplicate_hostname:dup01") for i in by_line(entries, 10).issues)


def test_invalid_rows(entries):
    assert by_line(entries, 13).status == "invalid"  # 192.0.2.300
    assert "no_hostname" in by_line(entries, 14).issues
    assert by_line(entries, 17).status == "invalid"  # bad_name!


def test_mask_and_ipv6(entries):
    assert any(i.startswith("mask_in_hosts_file") for i in by_line(entries, 15).issues)
    e = by_line(entries, 16)
    assert e.ip_version == 6 and "dual_stack" in e.issues and e.status == "ok"
    assert "mask_unknown" in by_line(entries, 12).issues  # 198.51.100.11 not in known subnets


def test_alias_clash(entries):
    assert "duplicate_alias:www" in by_line(entries, 18).issues
    assert by_line(entries, 4).status == "review"  # flagged, but not blocking collection


def test_bmc_link_and_inventory(hosts):
    assert hosts["web01"]["bmc"] == "web01-bmc" and hosts["web01-bmc"]["is_bmc"]
    inv = hostsparse.build_inventory(hosts, pilot=["web01", "app01"])["all"]["children"]
    assert set(inv["linux"]["hosts"]) >= {"web01", "app01", "db01"}
    assert "win01" in inv["windows"]["hosts"] and "web01-bmc" in inv["bmc"]["hosts"]
    assert "tabbed01" in inv["skipped"]["hosts"] and "dup01" in inv["skipped"]["hosts"]
    assert set(inv["pilot"]["hosts"]) == {"web01", "app01"}


# ------------------------------------------------------------------ normalization
def test_physical_fields_and_provenance(assets):
    a = assets["web01"]
    f = a["fields"]
    assert a["kind"] == "device"
    assert f["serial"]["value"] == "ABC1234" and f["serial"]["confidence"] == "high"
    assert "bmc_redfish" in f["serial"]["source"] and "linux_ssh" in f["serial"]["source"]  # confirmed by two sources
    assert "asset_tag" not in f  # placeholder never stored
    assert f["bmc_ip"]["value"] == "198.51.100.11" and f["platform"]["value"] == "Rocky 9"
    assert f["serial"]["collected_at"].startswith("2026-10-27")


def test_real_nic_names_and_mgmt(assets):
    ifs = {i["name"]: i for i in assets["web01"]["interfaces"]}
    assert set(ifs) == {"eno1", "eno2", "bond0"}  # lo/docker0 excluded, nothing renamed
    assert ifs["eno1"]["is_mgmt"] and not ifs["eno2"]["is_mgmt"]
    assert {x["address"] for x in ifs["eno1"]["addresses"]} == {"192.0.2.11/24", "fd00:10::11/64"}  # link-local dropped
    assert ifs["bond0"]["kind"] == "lag"


def test_vm_detection(assets):
    assert assets["app01"]["kind"] == "vm"
    assert "serial" not in assets["app01"]["fields"]  # "NA" is a placeholder
    assert "serial_unavailable_needs_become" in assets["app01"]["notes"]
    w = assets["win01"]
    assert w["kind"] == "vm" and w["fields"]["platform"]["value"] == "Windows Server 2022"
    assert w["interfaces"][0]["name"] == "Ethernet0" and w["interfaces"][0]["mac"] == "00:50:56:AA:BB:01"


def test_unreachable_recorded(assets):
    assert assets["db01"]["collection"][0]["reason"] == "unreachable"


def test_serial_conflict(hosts, tmp_path):
    raw = tmp_path / "raw"
    shutil.copytree(FX / "raw", raw)
    b = json.loads((raw / "bmc" / "web01-bmc.json").read_text())
    b["systems"][0]["serial"] = "ZZZ9999"
    (raw / "bmc" / "web01-bmc.json").write_text(json.dumps(b))
    a = {x["key"]: x for x in normalize.build_assets(copy.deepcopy(hosts), raw, SETTINGS)}["web01"]
    assert a["fields"]["serial"]["confidence"] == "conflict" and a["fields"]["serial"]["value"] is None


# ------------------------------------------------------------------ NetBox sync
def seeded():
    nb = FakeNetBox()
    nb.add("dcim/sites", name="DC1", slug="dc1")
    nb.add("dcim/device-roles", name="Server", slug="server")
    nb.add("extras/tags", name="asset-sync", slug="asset-sync")
    nb.add("virtualization/clusters", name="kvm-01", site=None)
    return nb


def test_dry_run_writes_nothing(assets):
    nb = seeded()
    run = Syncer(nb, SETTINGS, apply=False).run(list(assets.values()))
    assert nb.writes() == []
    st = {r["asset"]: r["status"] for r in run["results"]}
    assert st["web01"] == "changed" and st["app01"] == "changed"
    assert st["db01"] == "skipped"                       # unreachable
    assert st["db02"] == "skipped"                       # never collected
    assert st["win01"] == "review"                       # cluster vmw-01 missing in NetBox
    assert st["dup01"] == "skipped"
    assert any(o["endpoint"] == "dcim/devices" and o["action"] == "create" for o in run["ops"])


def test_apply_then_idempotent(assets):
    nb = seeded()
    Syncer(nb, SETTINGS, apply=True).run(list(assets.values()))
    dev = next(iter(nb.db["dcim/devices"].values()))
    assert dev["name"] == "web01" and dev["serial"] == "ABC1234" and dev["custom_fields"]["bmc_ip"] == "198.51.100.11"
    mfr = nb.db["dcim/manufacturers"]
    assert [m["name"] for m in mfr.values()] == ["Dell"]  # alias applied
    ifs = {i["name"]: i for i in nb.db["dcim/interfaces"].values() if i["device"] == dev["id"]}
    assert ifs["eno1"]["custom_fields"]["mgmt_nic"] is True and ifs["eno2"]["custom_fields"]["mgmt_nic"] is False
    assert ifs["eno1"]["type"] == "other" and ifs["bond0"]["type"] == "lag"
    assert dev["primary_ip4"] and "primary_ip6" not in dev  # only the IP we connected to is confirmed primary
    vm = next(iter(nb.db["virtualization/virtual-machines"].values()))
    assert vm["name"] == "app01" and vm["primary_ip4"]
    n = len(nb.writes())
    run2 = Syncer(nb, SETTINGS, apply=True).run(list(assets.values()))
    assert len(nb.writes()) == n, "second run must not write anything"
    assert {r["status"] for r in run2["results"] if r["asset"] in ("web01", "app01")} == {"unchanged"}


def test_existing_serial_mismatch_not_overwritten(assets):
    nb = seeded()
    nb.add("dcim/devices", name="web01", serial="OLD0001", site=1, role=2, device_type=None)
    run = Syncer(nb, SETTINGS, apply=True).run([assets["web01"]])
    assert run["results"][0]["reason"] == "serial_mismatch"
    assert nb.db["dcim/devices"][5]["serial"] == "OLD0001"
    row = next(r for r in run["review"] if r["field"] == "serial")
    assert "ABC1234" not in str(row) and row["collected_value"] == mask("ABC1234")  # masked in report


def test_serial_on_other_device(assets):
    nb = seeded()
    nb.add("dcim/devices", name="old-web", serial="ABC1234", site=1, role=2)
    run = Syncer(nb, SETTINGS, apply=True).run([assets["web01"]])
    assert run["results"][0]["reason"] == "name_differs_for_serial"
    assert nb.writes() == []


def test_ip_owned_elsewhere_goes_to_review(assets):
    nb = seeded()
    other = nb.add("dcim/devices", name="other", site=1, role=2)
    oif = nb.add("dcim/interfaces", device=other["id"], name="eth0", type="other")
    nb.add("ipam/ip-addresses", address="192.0.2.11/24", assigned_object_type="dcim.interface", assigned_object_id=oif["id"])
    run = Syncer(nb, SETTINGS, apply=True).run([assets["web01"]])
    assert any(r["object"] == "ip" and "another object" in r["reason"] for r in run["review"])
    ip = next(i for i in nb.db["ipam/ip-addresses"].values() if i["address"] == "192.0.2.11/24")
    assert ip["assigned_object_id"] == oif["id"]  # not moved


def test_fill_empty_only(assets):
    nb = seeded()
    nb.add("dcim/devices", name="web01", serial="", site=1, role=2, platform=None, custom_fields={"bmc_ip": "203.0.113.99"})
    run = Syncer(nb, SETTINGS, apply=True).run([assets["web01"]])
    dev = nb.db["dcim/devices"][5]
    assert dev["serial"] == "ABC1234"                       # empty -> filled
    assert dev["custom_fields"]["bmc_ip"] == "203.0.113.99" # non-empty -> kept
    assert any(r["field"] == "bmc_ip" for r in run["review"])


def test_kind_mismatch(assets):
    nb = seeded()
    nb.add("virtualization/virtual-machines", name="web01", cluster=4)
    run = Syncer(nb, SETTINGS, apply=True).run([assets["web01"]])
    assert run["results"][0]["reason"] == "exists_as_vm" and nb.writes() == []


def test_missing_role_is_review(assets):
    a = copy.deepcopy(assets["web01"])
    a["classification"]["role"] = None
    nb = seeded()
    run = Syncer(nb, SETTINGS, apply=True).run([a])
    assert run["results"][0]["status"] == "review" and nb.writes() == []


def test_hostname_mismatch_held(assets):
    a = copy.deepcopy(assets["web01"])
    a["fields"]["hostname"]["value"] = "web99"
    nb = seeded()
    run = Syncer(nb, SETTINGS, apply=True).run([a])
    assert run["results"][0]["reason"] == "hostname_mismatch" and nb.writes() == []


def test_old_netbox_mac_field(assets):
    nb = seeded()
    nb.version = (4, 1, 0)
    Syncer(nb, SETTINGS, apply=True).run([assets["web01"]])
    assert "dcim/mac-addresses" not in nb.db
    assert any(i.get("mac_address") == "AA:BB:CC:00:00:01" for i in nb.db["dcim/interfaces"].values())


def test_no_delete_capability():
    from asset_sync import nbclient
    assert not hasattr(nbclient.NetBox, "delete") and not hasattr(nbclient.Writer, "delete")


# ------------------------------------------------------------------ secrets
def test_redaction_and_placeholders():
    assert "s3cret" not in redact("password=s3cret token: abcdef")
    assert "nbt_abc" not in redact("Authorization: Bearer nbt_abc.def123456789012345")
    assert is_placeholder("To Be Filled By O.E.M.") and is_placeholder("0000000") and not is_placeholder("ABC1234")


def test_dry_run_counts_shared_device_type_once(assets):
    a2 = copy.deepcopy(assets["web01"])
    a2["key"] = "web02"
    for k in ("serial",):
        a2["fields"][k] = dict(a2["fields"][k], value="XYZ7890")
    a2["fields"]["hostname"] = dict(a2["fields"]["hostname"], value="web02")
    a2["interfaces"] = []
    a2["fields"].pop("bmc_ip")
    run = Syncer(seeded(), SETTINGS, apply=False).run([assets["web01"], a2])
    assert sum(1 for o in run["ops"] if o["endpoint"] == "dcim/device-types") == 1
    assert sum(1 for o in run["ops"] if o["endpoint"] == "dcim/devices" and o["action"] == "create") == 2
