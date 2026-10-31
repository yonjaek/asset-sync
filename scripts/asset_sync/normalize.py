"""Merge hosts-file records with raw collector output into assets with per-field provenance.

Every collected value is stored as {value, source, collected_at, confidence}. Values that could not be
confirmed are left out (never synthesized). Disagreeing sources produce confidence="conflict".
"""
from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any

from .common import clean, excel_ref, now_iso

HIGH, MEDIUM, CONFLICT = "high", "medium", "conflict"

VIRTUAL_VENDORS = re.compile(r"vmware|qemu|xen|innotek|virtualbox|parallels|amazon ec2|nutanix|ovirt|"
                             r"openstack|google|bochs|alibaba cloud|digitalocean|hetzner", re.I)
VIRTUAL_MODELS = re.compile(r"virtual machine|vmware|kvm|hvm domu|virtualbox|standard pc \(|openstack|"
                            r"google compute|bochs|cloud server|droplet|ahv|rhev hypervisor", re.I)
CONTAINER_TYPES = {"docker", "lxc", "podman", "openvz", "container", "systemd-nspawn", "wsl"}


def fv(value: Any, source: str, collected_at: str, confidence: str = HIGH, **extra) -> dict:
    return {"value": value, "source": source, "collected_at": collected_at, "confidence": confidence, **extra}


def merge_field(fields: dict, name: str, new: dict | None) -> None:
    """Add a field value; if a different value already exists from another source, mark conflict."""
    if not new or new.get("value") in (None, ""):
        return
    cur = fields.get(name)
    if cur is None:
        fields[name] = new
        return
    if cur["confidence"] == CONFLICT:
        cur["candidates"].append({k: new[k] for k in ("value", "source", "collected_at")})
        return
    same = str(cur["value"]).strip().lower() == str(new["value"]).strip().lower()
    if same:
        cur["source"] = f"{cur['source']}+{new['source']}"
        if cur["confidence"] != HIGH and new["confidence"] == HIGH:
            cur["confidence"] = HIGH
        return
    if cur["confidence"] == MEDIUM and new["confidence"] == HIGH:
        fields[name] = new  # direct observation beats a heuristic
        return
    if new["confidence"] == MEDIUM and cur["confidence"] == HIGH:
        return
    fields[name] = {"value": None, "confidence": CONFLICT, "source": "multiple",
                    "collected_at": new["collected_at"],
                    "candidates": [{k: x[k] for k in ("value", "source", "collected_at")} for x in (cur, new)]}


def _prefix(address: str, netmask_or_len: Any) -> str | None:
    try:
        return str(ipaddress.ip_interface(f"{address}/{netmask_or_len}"))
    except ValueError:
        return None


def _is_virtual(vendor: str | None, model: str | None) -> bool:
    return bool((vendor and VIRTUAL_VENDORS.search(vendor)) or (model and VIRTUAL_MODELS.search(model)))


def platform_name(os_name: str | None, version: str | None) -> str | None:
    if not os_name:
        return None
    if "windows" in os_name.lower():
        m = re.search(r"(Windows(?: Server)? \d{4}(?: R2)?|Windows \d+)", os_name)
        return m.group(1) if m else re.sub(r"^Microsoft\s+", "", os_name).strip()
    major = (version or "").split(".")[0]
    return f"{os_name} {major}".strip()


# ---------------------------------------------------------------- Linux

def from_linux(raw: dict, settings: dict) -> dict:
    f = raw.get("facts", {})
    ts, src = raw["collected_at"], "linux_ssh"
    g = lambda k: f.get(f"ansible_{k}")  # noqa: E731
    fields: dict[str, dict] = {}
    notes: list[str] = []

    def put(name, key, value, conf=HIGH):
        v = clean(value)
        if v is not None:
            merge_field(fields, name, fv(v, f"{src}:{key}", ts, conf))

    put("hostname", "ansible_hostname", (g("hostname") or "").lower())
    put("fqdn", "ansible_fqdn", (g("fqdn") or "").lower() if "." in (g("fqdn") or "") else None)
    put("manufacturer", "ansible_system_vendor", g("system_vendor"))
    put("model", "ansible_product_name", g("product_name"))
    put("serial", "ansible_product_serial", g("product_serial"))
    put("asset_tag", "ansible_chassis_asset_tag", g("chassis_asset_tag"))
    if clean(g("product_serial")) is None:
        notes.append("serial_unavailable" + ("" if raw.get("privileged") else "_needs_become"))
    put("os_name", "ansible_distribution", g("distribution"))
    put("os_version", "ansible_distribution_version", g("distribution_version"))
    if fields.get("os_name"):
        put("platform", "derived", platform_name(fields["os_name"]["value"], g("distribution_version")))

    vrole, vtype = g("virtualization_role") or "NA", g("virtualization_type") or "NA"
    if vtype in CONTAINER_TYPES and vrole == "guest":
        notes.append(f"container_guest:{vtype}")
    elif vrole == "guest" or _is_virtual(g("system_vendor"), g("product_name")):
        merge_field(fields, "is_virtual", fv(True, f"{src}:ansible_virtualization_role", ts))
        if vtype != "NA":
            merge_field(fields, "virtualization_type", fv(vtype, f"{src}:ansible_virtualization_type", ts))
    elif clean(g("system_vendor")) and clean(g("product_name")):
        merge_field(fields, "is_virtual", fv(False, f"{src}:dmi+virtualization_role={vrole}", ts))

    exclude = re.compile(settings.get("interfaces", {}).get("exclude_regex", r"^lo$"))
    connected = raw.get("connected_ip")
    interfaces, excluded = [], []
    for name in g("interfaces") or []:
        key = "ansible_" + re.sub(r"[-.:]", "_", name)
        d = f.get(key) or {}
        if exclude.search(name) or d.get("type") == "loopback":
            excluded.append(name)
            continue
        addrs = []
        for a in [d.get("ipv4")] + list(d.get("ipv4_secondaries") or []):
            if a and a.get("address"):
                p = _prefix(a["address"], a.get("prefix") or a.get("netmask"))
                if p:
                    addrs.append({"address": p, "family": 4})
        for a in d.get("ipv6") or []:
            if a.get("scope") in ("link", "host") or not a.get("address"):
                continue
            p = _prefix(a["address"], a.get("prefix"))
            if p:
                addrs.append({"address": p, "family": 6})
        mac = clean(d.get("perm_macaddress") or d.get("macaddress"))
        iface = {"name": name, "mac": mac.upper() if mac else None, "kind": _linux_kind(d),
                 "driver": d.get("module"), "active": d.get("active"),
                 "addresses": addrs, "is_mgmt": False, "source": f"{src}:{key}", "collected_at": ts}
        interfaces.append(iface)
    _mark_mgmt(interfaces, connected, (g("default_ipv4") or {}).get("interface"), notes)

    if raw.get("ipmi_lan"):
        m = re.search(r"^IP Address\s*:\s*(\S+)", raw["ipmi_lan"], re.M)
        if m and clean(m.group(1)) and m.group(1) != "0.0.0.0":
            merge_field(fields, "bmc_ip", fv(m.group(1), f"{src}:ipmitool_lan_print", ts))
    return {"fields": fields, "interfaces": interfaces, "excluded_interfaces": excluded, "notes": notes}


def _linux_kind(d: dict) -> str:
    t = d.get("type")
    if t == "bonding":
        return "lag"
    if t == "bridge":
        return "bridge"
    if t == "ether" and d.get("module"):
        return "physical"
    return "virtual" if t in ("tunnel", "ether", "unknown") else "other"


# ---------------------------------------------------------------- Windows

def from_windows(raw: dict, settings: dict) -> dict:
    d, ts, src = raw.get("data", {}), raw["collected_at"], "windows_winrm"
    fields: dict[str, dict] = {}
    notes: list[str] = []

    def put(name, key, value, conf=HIGH):
        v = clean(value)
        if v is not None:
            merge_field(fields, name, fv(v, f"{src}:{key}", ts, conf))

    put("hostname", "COMPUTERNAME", (d.get("hostname") or "").lower())
    if d.get("hostname") and d.get("dns_domain") and "." in d["dns_domain"]:
        put("fqdn", "Win32_ComputerSystem.Domain", f"{d['hostname']}.{d['dns_domain']}".lower())
    put("manufacturer", "Win32_ComputerSystem.Manufacturer", d.get("manufacturer"))
    put("model", "Win32_ComputerSystem.Model", d.get("model"))
    put("serial", "Win32_BIOS.SerialNumber", d.get("serial"))
    put("asset_tag", "Win32_SystemEnclosure.SMBIOSAssetTag", d.get("asset_tag"))
    put("os_name", "Win32_OperatingSystem.Caption", d.get("os_caption"))
    put("os_version", "Win32_OperatingSystem.Version", d.get("os_version"))
    if fields.get("os_name"):
        put("platform", "derived", platform_name(fields["os_name"]["value"], d.get("os_version")))
    if clean(d.get("manufacturer")) and clean(d.get("model")):
        merge_field(fields, "is_virtual", fv(_is_virtual(d.get("manufacturer"), d.get("model")),
                                             f"{src}:manufacturer/model", ts))

    exclude = re.compile(settings.get("interfaces", {}).get("exclude_regex", r"^lo$"))
    ips_by_if: dict[Any, list] = {}
    for ip in d.get("ip_addresses") or []:
        if str(ip.get("address", "")).lower().startswith(("fe80", "127.", "::1", "169.254")):
            continue
        p = _prefix(ip["address"], ip.get("prefix"))
        if p:
            ips_by_if.setdefault(ip.get("ifindex"), []).append({"address": p, "family": 6 if ":" in p else 4})
    interfaces, excluded = [], []
    for a in d.get("adapters") or []:
        if exclude.search(a.get("name", "")):
            excluded.append(a.get("name"))
            continue
        mac = clean((a.get("mac") or "").replace("-", ":"))
        interfaces.append({"name": a["name"], "mac": mac.upper() if mac else None,
                           "kind": "virtual" if a.get("virtual") else "physical", "driver": a.get("description"),
                           "active": a.get("status") == "Up", "addresses": ips_by_if.get(a.get("ifindex"), []),
                           "is_mgmt": False, "source": f"{src}:Get-NetAdapter", "collected_at": ts})
    default_if = next((r.get("ifindex") for r in d.get("default_routes") or []), None)
    default_name = next((a["name"] for a in d.get("adapters") or [] if a.get("ifindex") == default_if), None)
    _mark_mgmt(interfaces, raw.get("connected_ip"), default_name, notes)
    return {"fields": fields, "interfaces": interfaces, "excluded_interfaces": excluded, "notes": notes}


def _mark_mgmt(interfaces: list[dict], connected_ip: str | None, default_if: str | None, notes: list[str]) -> None:
    """The management NIC is the one carrying the IP we connected to. Never renamed."""
    for i in interfaces:
        for a in i["addresses"]:
            if connected_ip and a["address"].split("/")[0] == connected_ip:
                i["is_mgmt"], i["mgmt_source"] = True, "connected_ip_match"
                a["is_primary"] = True
                return
    notes.append("mgmt_nic_unidentified")
    for i in interfaces:
        if i["name"] == default_if:
            i["mgmt_candidate"] = True  # default route only: medium confidence, review before marking
            return


# ---------------------------------------------------------------- BMC

def from_bmc(raw: dict) -> dict:
    ts, src = raw["collected_at"], raw["collector"]
    fields: dict[str, dict] = {}
    systems = raw.get("systems") or []
    # some platforms expose extra Systems (e.g. HGX GPU baseboard next to the host "Self"): use the one
    # that carries a serial and model, preferring non-baseboard entries
    real = [x for x in systems if clean(x.get("serial")) and clean(x.get("model"))]
    if len(real) > 1:
        real = [x for x in real if not re.search(r"baseboard|hgx|gpu", str(x.get("id")), re.I)] or real
    if len(real) == 1 or (len(systems) == 1 and not real):
        s = (real or systems)[0]
        for name, key in (("manufacturer", "Manufacturer"), ("model", "Model"), ("serial", "SerialNumber"),
                          ("asset_tag", "AssetTag")):
            v = clean(s.get(name))
            if v is not None:
                merge_field(fields, name, fv(v, f"{src}:System.{key}", ts))
        # a reachable BMC means physical hardware
        merge_field(fields, "is_virtual", fv(False, f"{src}:has_bmc", ts))
    bmc_ips = {a["address"] for i in raw.get("manager_interfaces") or [] for a in i.get("ipv4", [])
               if a.get("address") and a["address"] != "0.0.0.0"}
    target = raw.get("connected_ip")
    if target and (not bmc_ips or target in bmc_ips):
        merge_field(fields, "bmc_ip", fv(target, f"{src}:connected_ip", ts))
    elif len(bmc_ips) == 1:
        merge_field(fields, "bmc_ip", fv(next(iter(bmc_ips)), f"{src}:Manager.EthernetInterfaces", ts))
    bmc_iface = None
    for i in raw.get("manager_interfaces") or []:
        addr = next((a for a in i.get("ipv4", []) if fields.get("bmc_ip") and a["address"] == fields["bmc_ip"]["value"]), None)
        if addr:
            bmc_iface = {"name": i.get("id"), "mac": (clean(i.get("mac")) or "").upper() or None,
                         "address": _prefix(addr["address"], addr.get("netmask")), "source": f"{src}:Manager.EthernetInterfaces",
                         "collected_at": ts}
    return {"fields": fields, "bmc_interface": bmc_iface}


# ---------------------------------------------------------------- assemble

def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_assets(hosts: dict[str, dict], raw_dir: Path, settings: dict) -> list[dict]:
    assets = []
    defaults = settings.get("defaults", {}) or {}
    for name, h in sorted(hosts.items()):
        if h.get("is_bmc"):
            continue
        a: dict[str, Any] = {"key": name, "hosts_file": {k: h.get(k) for k in
                             ("hostname", "fqdn", "aliases", "ipv4", "ipv6", "source_lines", "status", "issues")},
                             "name_trusted": h.get("name_trusted", True), "source": "excel" if h.get("excel") else "hosts_file",
                             "fields": {}, "interfaces": [], "excluded_interfaces": [], "notes": [],
                             "collection": [], "built_at": now_iso()}
        a["classification"] = {"site": h.get("site") or defaults.get("site"),
                               "role": h.get("role"), "cluster": h.get("cluster") or defaults.get("cluster"),
                               "rack": h.get("rack")}
        if h.get("skip"):
            a["collection"].append({"collector": "-", "status": "skipped", "reason": h.get("skip_reason") or "override"})
        fam = h.get("os_family")
        if fam in ("linux", "windows") and not h.get("skip"):
            raw = _load(raw_dir / fam / f"{name}.json")
            st = _load(raw_dir / fam / f"{name}.status.json")
            if raw and raw.get("status") == "ok" and (not st or st.get("collected_at", "") < raw["collected_at"]):
                part = (from_linux if fam == "linux" else from_windows)(raw, settings)
                for k, v in part["fields"].items():
                    merge_field(a["fields"], k, v)
                a["interfaces"], a["excluded_interfaces"] = part["interfaces"], part["excluded_interfaces"]
                a["notes"] += part["notes"]
                a["collection"].append({"collector": raw["collector"], "status": "ok", "collected_at": raw["collected_at"]})
            elif st:
                a["collection"].append({k: st.get(k) for k in ("collector", "status", "reason", "detail", "collected_at")})
            else:
                a["collection"].append({"collector": fam, "status": "not_collected", "reason": "no_raw_data"})
        elif not fam and not h.get("skip"):
            a["collection"].append({"collector": "-", "status": "not_collected", "reason": "os_family_unknown"})
        bmc = None if h.get("skip") else h.get("bmc")  # skip means: no collection, no registration
        if bmc:
            braw = _load(raw_dir / "bmc" / f"{bmc}.json")
            if braw and braw.get("status") == "ok":
                part = from_bmc(braw)
                for k, v in part["fields"].items():
                    merge_field(a["fields"], k, v)
                a["bmc_interface"] = part["bmc_interface"]
                a["_bmc_hostname"] = ((braw.get("systems") or [{}])[0]).get("hostname")
                a["collection"].append({"collector": braw["collector"], "status": "ok", "collected_at": braw["collected_at"]})
            else:
                if braw:
                    a["collection"].append({k: braw.get(k) for k in ("collector", "status", "reason", "collected_at")})
                bh = hosts.get(bmc)
                ip = bh["ansible_host"] if bh else (bmc if _looks_ip(bmc) else None)
                if ip and not (h.get("excel") or {}).get("bmc_ip") == ip:  # excel value is added by excel_crosscheck
                    merge_field(a["fields"], "bmc_ip", fv(ip, f"hosts_file:{h.get('bmc_from', 'override')}", h.get("built_at", now_iso()), MEDIUM))
        if h.get("excel"):
            excel_crosscheck(a, h["excel"])
            _confirm_name_by_serial(a, h["excel"], name)
        hn = a["fields"].get("hostname")
        if hn and hn["value"] and a["name_trusted"] and hn["value"].split(".")[0] != name:
            a["notes"].append(f"hostname_mismatch:hosts={name},collected={hn['value']}")
        a["kind"] = _kind(a["fields"].get("is_virtual"))
        assets.append(a)
    return assets


def _same(field: str, a: Any, b: Any) -> bool:
    x, y = str(a).strip().lower(), str(b).strip().lower()
    if field == "model":  # "ProLiant DL380 Gen10" vs "DL380 Gen10", "HPE DL380 Gen11" vs "ProLiant DL380 Gen11"
        strip = lambda s: re.sub(r"\b(hpe|hp|proliant|dell|poweredge|nvidia|inc\.?)\b|[^a-z0-9]", "", s)  # noqa: E731
        x, y = strip(x), strip(y)
        return bool(x and y) and (x in y or y in x)
    return x == y


def _confirm_name_by_serial(a: dict, xl: dict, name: str) -> None:
    """BMC-only collection gives no OS hostname. When the BMC serial equals the Excel row's serial, that row is
    proven to describe this machine, so its (valid) hostname is accepted as confirmed."""
    if "hostname" in a["fields"] or not a.get("name_trusted"):
        return
    s = a["fields"].get("serial")
    xs = clean(xl.get("serial"))
    if s and s["confidence"] == HIGH and "bmc_" in s["source"] and xs and _same("serial", s["value"], xs):
        a["fields"]["hostname"] = fv(name, f"excel:{excel_ref(xl)}(confirmed_by_bmc_serial)", s["collected_at"], HIGH)
    bmc_hn = clean(a.pop("_bmc_hostname", None))
    if bmc_hn and bmc_hn.lower().split(".")[0] != name:
        a["notes"].append(f"bmc_reported_hostname_differs:{bmc_hn.lower()}")


def excel_crosscheck(a: dict, xl: dict) -> None:
    """Compare spreadsheet values with collected ones. Collected (high) wins; differences are recorded.
    Without a collected value the spreadsheet value is kept as medium confidence (review, never auto-applied)."""
    src, ts = f"excel:{excel_ref(xl)}", xl.get("collected_at") or now_iso()
    a["excel"] = {k: xl.get(k) for k in ("sheet", "multi_sheet", "row", "rack", "position", "u_height", "kind")}
    a["excel_diffs"] = []
    for fname in ("serial", "model", "bmc_ip"):
        xv = clean(xl.get(fname))
        if not xv:
            continue
        cur = a["fields"].get(fname)
        if cur and cur["confidence"] == HIGH:
            if _same(fname, cur["value"], xv):
                cur["source"] += "+excel"
            else:
                a["excel_diffs"].append({"field": fname, "excel": xv, "collected": cur["value"],
                                         "source": src, "collected_at": ts, "collected_source": cur["source"]})
        elif not cur:
            a["fields"][fname] = fv(xv, src, ts, MEDIUM)
    macs = {i.get("mac") for i in a["interfaces"] if i.get("mac")}
    if xl.get("os_mac") and macs and xl["os_mac"] not in macs:
        a["excel_diffs"].append({"field": "os_mac", "excel": xl["os_mac"], "collected": ",".join(sorted(macs)),
                                 "source": src, "collected_at": ts, "collected_source": "interfaces"})
    xk = xl.get("kind")
    if xk and a["fields"].get("is_virtual", {}).get("confidence") == HIGH:
        if (xk == "vm") != bool(a["fields"]["is_virtual"]["value"]):
            a["excel_diffs"].append({"field": "kind", "excel": xk, "collected": "vm" if a["fields"]["is_virtual"]["value"] else "device",
                                     "source": src, "collected_at": ts, "collected_source": a["fields"]["is_virtual"]["source"]})


def _looks_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _kind(v: dict | None) -> str:
    if not v or v["confidence"] == CONFLICT:
        return "unknown"
    return "vm" if v["value"] is True else "device"
