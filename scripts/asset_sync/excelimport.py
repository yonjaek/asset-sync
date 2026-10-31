"""Import a data-center asset spreadsheet (one device per merged block) as the target list.

Excel values are human-entered: classification (site, rack, role, cluster) is taken as given, while factual
values (serial, model, BMC IP, MAC) are only used to cross-check what collectors observe (confidence=medium).
Credential / remark / personal columns are never read.
"""
from __future__ import annotations

import ipaddress
import re
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import clean
from .hostsparse import valid_hostname

# headers that must never be read (credentials, free-text remarks, personal data)
NEVER_READ = re.compile(r"ID\s*/\s*PW|password|passwd|account|계정|비고|remark|e-?mail|연락처|담당자|사용\s*기간", re.I)

COLUMNS = {  # logical field -> header regex
    "position": r"^서버\s*위치$|^position$|^location$",
    "model": r"^모델명?$|^model$",
    "serial": r"^S/?N$|^serial",
    "mgmt_ip": r"^MGMT\s*IP$|^(iLO|BMC|IPMI)\s*IP$",
    "mgmt_mac": r"^MGMT\s*MAC$",
    "hostname": r"^host\s*name",
    "os_ip": r"^OS\s*IP$",
    "os_mac": r"^OS\s*MAC$",
}
GENERIC_NAMES = {"localhost", "localhost4", "localhost6", "localdomain", "server", "host", "test", "unknown", "none"}
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
RACK_RE = re.compile(r"^([A-Z]{1,3}\d+)-(\d+)$", re.I)  # R01-42, A03-12 ...


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _ips(text: str) -> tuple[list[str], list[str]]:
    good, bad = [], []
    for tok in re.split(r"[\s,/;]+", text):
        if not tok or tok == "-":
            continue
        try:
            ip = ipaddress.ip_address(tok.split(":")[0])
            good.append(str(ip))
        except ValueError:
            if re.search(r"\d", tok):
                bad.append(tok)
    return good, bad


def read_sheet(path: Path | str, sheet: str) -> list[dict]:
    """Return one dict per device (top row of each merged block in the model column)."""
    import openpyxl  # optional dependency, only needed for Excel input

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    ws = wb[sheet]
    hdr_row, cols = None, {}
    for r in range(1, 6):
        heads = {c: _s(ws.cell(r, c).value).replace("\n", " ") for c in range(1, ws.max_column + 1)}
        found = {f: c for f, rx in COLUMNS.items() for c, h in heads.items()
                 if h and not NEVER_READ.search(h) and re.search(rx, h, re.I)}
        if {"model", "hostname"} <= set(found):
            hdr_row, cols = r, found
            break
    if not hdr_row:
        raise ValueError(f"header row with 모델명/hostname not found in sheet '{sheet}'")

    merged: dict[tuple[int, int], tuple[int, int]] = {}
    span: dict[int, int] = {}
    for rng in ws.merged_cells.ranges:
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merged[(r, c)] = (rng.min_row, rng.min_col)
        if rng.min_col <= cols["model"] <= rng.max_col:
            span[rng.min_row] = rng.max_row - rng.min_row + 1
            for r in range(rng.min_row + 1, rng.max_row + 1):
                span[r] = 0

    def val(r: int, c: int) -> str:
        tr, tc = merged.get((r, c), (r, c))
        return _s(ws.cell(tr, tc).value)

    rows = []
    for r in range(hdr_row + 1, ws.max_row + 1):
        if span.get(r) == 0:
            continue  # lower U of a multi-U device
        rec = {f: val(r, c) for f, c in cols.items()}
        rec["category"] = val(r, 1)  # unlabeled first column: 전산실 / VM / 사무실 ...
        if not (rec.get("model") or rec.get("hostname")):
            continue
        rec["row"], rec["u_height"] = r, span.get(r, 1)
        rows.append(rec)
    return rows


def classify_role(model: str, rules: list[list[str]]) -> str | None:
    for rx, role in rules:
        if re.search(rx, model or "", re.I):
            return None if role == "-" else role
    return None


def build_hosts_from_excel(path: Path | str, settings: dict, overrides: dict | None = None) -> tuple[dict, list[dict]]:
    """Return (hosts, rows_report). hosts has the same shape as hostsparse.build_hosts output."""
    xc = settings.get("excel", {}) or {}
    cats = set(xc.get("include_categories", ["서버실"]))
    vm_cat = xc.get("vm_category", "VM") if xc.get("include_vms", True) else None
    rules = xc.get("role_rules", [])
    ov = (overrides or {}).get("hosts", {}) or {}
    ts = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()
    sheets = xc.get("sheets") or {}  # one workbook, one sheet per IDC: {sheet name: {site, cluster_map}}
    multi = bool(sheets)
    plan = list(sheets.items()) if multi else [(xc.get("sheet", "장비 목록"), {})]

    report, hosts = [], {}
    records = [(sheet, sc, rec) for sheet, sc in plan for rec in read_sheet(path, sheet)]
    for sheet, sc, rec in records:
        site = (sc or {}).get("site") or xc.get("site")
        cmap = (sc or {}).get("cluster_map") or xc.get("cluster_map") or {}
        cat = rec["category"]
        out = {"sheet": sheet, "row": rec["row"], "category": cat, "model": rec.get("model"), "label": rec.get("hostname"),
               "status": "ok", "issues": [], "key": None}
        report.append(out)
        is_vm = vm_cat is not None and cat == vm_cat
        if cat not in cats and not is_vm:
            out.update(status="excluded", issues=[f"category:{cat or '(blank)'}"])
            continue
        role = xc.get("vm_role", "vm") if is_vm else classify_role(rec.get("model", ""), rules)
        if not role:
            out.update(status="excluded", issues=["not_a_server_model"])
            continue
        ips, bad = _ips(rec.get("os_ip", ""))
        out["issues"] += [f"invalid_ip:{b}" for b in bad]
        if len(ips) > 1:
            out["issues"].append("multiple_os_ips")
        label = rec.get("hostname", "").split("\n")[0].strip().lower()
        trusted = bool(label) and label != "-" and valid_hostname(label) and label.split(".")[0] not in GENERIC_NAMES
        if not trusted:
            out["issues"].append("hostname_not_usable" if label and label != "-" else "hostname_missing")
        if not ips and not trusted:
            out.update(status="invalid", issues=out["issues"] + ["no_os_ip"])
            continue
        key = label.split(".")[0] if trusted else "ip-" + ips[0].replace(".", "-")
        named = (ov.get(key) or {}).get("name")  # human-assigned name for a row whose Excel label is unusable
        if named and not trusted:
            if not valid_hostname(str(named)):
                out["issues"].append(f"override_name_invalid:{named}")
            else:
                key, trusted = str(named).lower(), True
                out["issues"].append("name_from_override")
        rack = pos = None
        m = RACK_RE.match(rec.get("position", ""))
        if m and not is_vm:
            rack, top = m.group(1).upper(), int(m.group(2))
            pos = top - rec["u_height"] + 1  # NetBox position = lowest U occupied
        mgmt, _ = _ips(rec.get("mgmt_ip", ""))
        out["key"] = key
        h = {"hostname": key, "fqdn": label if trusted and "." in label else None, "aliases": [],
             "ipv4": ips, "ipv6": [], "source_lines": [rec["row"]], "status": "ok", "issues": list(out["issues"]),
             "name_trusted": trusted, "bmc_candidate_of": None,
             "excel": {"sheet": sheet, "multi_sheet": multi, "row": rec["row"], "collected_at": ts, "label": rec.get("hostname"),
                       "model": clean(rec.get("model")), "serial": clean(rec.get("serial")),
                       "bmc_ip": mgmt[0] if mgmt else None, "os_mac": _mac(rec.get("os_mac")),
                       "mgmt_mac": _mac(rec.get("mgmt_mac")), "rack": rack, "position": pos,
                       "u_height": rec["u_height"], "kind": "vm" if is_vm else "device"}}
        o = ov.get(key, {}) or ov.get("ip-" + ips[0].replace(".", "-") if ips else "", {}) or {}
        h.update(os_family=o.get("os_family") or xc.get("default_os_family"), site=o.get("site") or site,
                 role=o.get("role") or role, rack=None if is_vm else (o.get("rack") or rack),
                 cluster=o.get("cluster") or (cmap.get(rec.get("position")) if is_vm else None),
                 bmc=o.get("bmc") or (mgmt[0] if mgmt and not is_vm else None),
                 bmc_type=o.get("bmc_type") or "redfish", skip=bool(o.get("skip", False)),
                 skip_reason=o.get("reason"), ansible_host=o.get("ansible_host") or (ips[0] if ips else None))
        if is_vm and not h["cluster"]:
            h["issues"].append(f"cluster_unmapped:{rec.get('position')}")
        if key in hosts:
            key2 = f"{key}~{site}-row{rec['row']}" if multi else f"{key}~row{rec['row']}"
            h["hostname"] = key2
            for x in (hosts[key], h):
                x["status"] = "review"
                x["issues"].append(f"duplicate_hostname:{key}")
            out["key"] = key = key2
        hosts[key] = h

    by_ip = defaultdict(list)
    for k, h in hosts.items():
        if h["ansible_host"]:
            by_ip[h["ansible_host"]].append(k)
    for ip, ks in by_ip.items():
        if len(ks) > 1:
            for k in ks:
                hosts[k]["status"] = "review"
                hosts[k]["issues"].append("duplicate_ip_conflict:" + ",".join(sorted(ks)))
    keyed = {r["key"]: r for r in report if r.get("key")}
    for k, h in hosts.items():
        if k in keyed:
            keyed[k].update(status=h["status"], issues=h["issues"])
    return hosts, report


def _mac(v: str | None) -> str | None:
    s = re.sub(r"[^0-9A-Fa-f]", "", v or "")
    return ":".join(s[i:i + 2] for i in range(0, 12, 2)).upper() if len(s) == 12 else None
