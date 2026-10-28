"""Parse and validate a hosts(5) style file, then build normalized records and an Ansible inventory.

Nothing here touches the network: OS family, site and role come from inventory/host_overrides.yml,
never from probing the targets.
"""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


@dataclass
class Entry:
    line_no: int
    raw: str
    ip: str | None = None
    ip_version: int | None = None
    hostname: str | None = None  # short, lower-case
    fqdn: str | None = None
    aliases: list[str] = field(default_factory=list)
    status: str = "ok"  # ok | excluded | invalid | review
    issues: list[str] = field(default_factory=list)
    prefix_known: bool = False
    prefix: str | None = None
    bmc_candidate_of: str | None = None

    def flag(self, status: str, issue: str) -> None:
        order = {"ok": 0, "review": 1, "excluded": 2, "invalid": 3}
        if order[status] > order[self.status]:
            self.status = status
        if issue not in self.issues:
            self.issues.append(issue)


def valid_hostname(name: str) -> bool:
    if not name or len(name) > 253:
        return False
    return all(LABEL_RE.match(lbl) for lbl in name.rstrip(".").split("."))


def _classify_ip(ip: ipaddress._BaseAddress) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    if ip.is_link_local:
        return "link_local"
    if ip.version == 4 and str(ip) == "255.255.255.255":
        return "broadcast"
    if ip.version == 6 and str(ip).startswith("ff0"):
        return "ipv6_special"
    if ip.is_reserved:
        return "reserved"
    return None


def parse_hosts(text: str, settings: dict | None = None) -> list[Entry]:
    settings = settings or {}
    hp = settings.get("hosts_parse", {})
    suffixes = [s.lower().lstrip(".") for s in hp.get("domain_suffixes", [])]
    bmc_patterns = [re.compile(p, re.I) for p in hp.get("bmc_name_patterns", [])]
    known_nets = [ipaddress.ip_network(n, strict=False) for n in settings.get("subnets", [])]

    entries: list[Entry] = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip().replace("\t", " ")
        if not line:
            continue
        if "�" in raw or any(ord(c) > 127 for c in raw.split("#", 1)[0]):
            e = Entry(no, raw.rstrip())
            e.flag("invalid", "non_ascii_or_bad_encoding")
            entries.append(e)
            continue
        tokens = line.split()
        e = Entry(no, raw.rstrip())
        ip_tok = tokens[0]
        if "/" in ip_tok:
            # hosts(5) has no mask field; a CIDR here is a data error and the mask is not trusted
            e.flag("review", f"mask_in_hosts_file:{ip_tok}")
            ip_tok = ip_tok.split("/", 1)[0]
        if "%" in ip_tok:
            e.flag("review", "ipv6_zone_id")
            ip_tok = ip_tok.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_tok)
        except ValueError:
            e.flag("invalid", f"invalid_ip:{tokens[0]}")
            entries.append(e)
            continue
        e.ip, e.ip_version = str(ip), ip.version
        names = [t.lower().rstrip(".") for t in tokens[1:]]
        if not names:
            e.flag("invalid", "no_hostname")
            entries.append(e)
            continue
        bad = [n for n in names if not valid_hostname(n)]
        for n in bad:
            e.flag("invalid" if n == names[0] else "review", f"invalid_hostname:{n}")
        names = [n for n in names if n not in bad]
        if not names:
            entries.append(e)
            continue
        primary = names[0]
        if "." in primary:
            e.fqdn, e.hostname = primary, primary.split(".", 1)[0]
        else:
            e.hostname = primary
            fq = [n for n in names[1:] if n.split(".", 1)[0] == primary and "." in n]
            if fq:
                e.fqdn = fq[0]
            elif suffixes:
                e.fqdn = f"{primary}.{suffixes[0]}"
                e.issues.append("fqdn_from_default_suffix")
        e.aliases = [n for n in names if n not in (e.hostname, e.fqdn)]
        if len(set(names)) != len(names):
            e.flag("review", "duplicate_name_in_line")
        special = _classify_ip(ip)
        if special:
            e.flag("excluded", special)
        if ip.version == 6 and not special:
            e.issues.append("ipv6")
        for net in known_nets:
            if ip.version == net.version and ip in net:
                e.prefix_known, e.prefix = True, f"{ip}/{net.prefixlen}"
                break
        if not e.prefix_known and e.status == "ok":
            # hosts files carry no mask; prefix must come from collection or config/subnets
            e.issues.append("mask_unknown")
        for pat in bmc_patterns:
            m = pat.search(e.hostname)
            if m:
                base = pat.sub("", e.hostname).strip("-")
                if base:
                    e.bmc_candidate_of = base
                    e.issues.append("bmc_name_pattern")
                break
        entries.append(e)

    _cross_checks(entries)
    return entries


def _cross_checks(entries: list[Entry]) -> None:
    by_ip: dict[str, list[Entry]] = defaultdict(list)
    by_name: dict[str, list[Entry]] = defaultdict(list)
    by_alias: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.ip and e.hostname and e.status not in ("invalid", "excluded"):
            by_ip[e.ip].append(e)
            by_name[e.hostname].append(e)
            for a in e.aliases:
                if a.split(".", 1)[0] != e.hostname:
                    by_alias[a].append(e)
    for alias, es in by_alias.items():
        clash = {x.ip for x in es} | {x.ip for x in by_name.get(alias, [])}
        if len(clash) > 1:
            for x in es:
                x.flag("review", f"duplicate_alias:{alias}")
    for ip, es in by_ip.items():
        if len(es) > 1:
            hosts = {x.hostname for x in es}
            issue = "duplicate_ip_same_host" if len(hosts) == 1 else "duplicate_ip_conflict:" + ",".join(sorted(hosts))
            for x in es:
                x.flag("review", issue)
    for name, es in by_name.items():
        uniq = {x.ip for x in es}
        if len(uniq) <= 1:
            continue
        v4 = {x.ip for x in es if x.ip_version == 4}
        if len(v4) > 1:
            for x in es:
                x.flag("review", f"duplicate_hostname:{name}:{','.join(sorted(v4))}")
        else:
            for x in es:
                if "dual_stack" not in x.issues:
                    x.issues.append("dual_stack")


def build_hosts(entries: list[Entry], overrides: dict | None = None) -> dict[str, dict]:
    """Group usable entries into one record per host (keyed by short hostname)."""
    overrides = (overrides or {}).get("hosts", {}) or {}
    hosts: dict[str, dict] = {}
    for e in entries:
        if e.status in ("invalid", "excluded") or not e.hostname:
            continue
        h = hosts.setdefault(e.hostname, {
            "hostname": e.hostname, "fqdn": e.fqdn, "aliases": [], "ipv4": [], "ipv6": [],
            "source_lines": [], "status": "ok", "issues": [], "bmc_candidate_of": e.bmc_candidate_of,
        })
        h["fqdn"] = h["fqdn"] or e.fqdn
        (h["ipv4"] if e.ip_version == 4 else h["ipv6"]).append(e.ip)
        h["aliases"] = sorted(set(h["aliases"]) | set(e.aliases))
        h["source_lines"].append(e.line_no)
        if e.status == "review":
            h["status"] = "review"
        h["issues"] = sorted(set(h["issues"]) | set(e.issues))
    for name, h in hosts.items():
        ov = overrides.get(name, {}) or {}
        h["os_family"] = ov.get("os_family")
        h["site"] = ov.get("site")
        h["role"] = ov.get("role")
        h["cluster"] = ov.get("cluster")
        h["bmc"] = ov.get("bmc")
        h["bmc_type"] = ov.get("bmc_type")
        h["skip"] = bool(ov.get("skip", False))
        h["skip_reason"] = ov.get("reason")
        h["ansible_host"] = ov.get("ansible_host") or (h["ipv4"][0] if h["ipv4"] else (h["ipv6"][0] if h["ipv6"] else None))
    # link BMC-looking names to their base host when an override did not do it
    for name, h in hosts.items():
        base = h.get("bmc_candidate_of")
        if base and base in hosts and not hosts[base].get("bmc"):
            hosts[base]["bmc"] = name
            hosts[base]["bmc_from"] = "hosts_name_pattern"
            h["is_bmc"] = True
        elif any(v.get("bmc") == name for v in hosts.values()):
            h["is_bmc"] = True
    return hosts


def build_inventory(hosts: dict[str, dict], pilot: list[str] | None = None) -> dict:
    groups: dict[str, dict] = {g: {"hosts": {}} for g in ("linux", "windows", "bmc", "unclassified", "skipped", "pilot")}
    pilot = set(pilot or [])
    for name, h in sorted(hosts.items()):
        hv = {"ansible_host": h.get("ansible_host")}
        if h.get("is_bmc"):
            groups["bmc"]["hosts"][name] = {**hv, "bmc_type": h.get("bmc_type") or "redfish"}
            continue
        if h["skip"]:
            groups["skipped"]["hosts"][name] = {**hv, "skip_reason": h.get("skip_reason") or "override"}
            continue
        fam = h.get("os_family")
        if h["status"] == "review" and any(i.startswith(("duplicate_ip_conflict", "duplicate_hostname")) for i in h["issues"]):
            groups["skipped"]["hosts"][name] = {**hv, "skip_reason": "hosts_file_conflict"}
            continue
        if not h.get("ansible_host"):
            groups["skipped"]["hosts"][name] = {"skip_reason": "no_ip"}
            continue
        target = fam if fam in ("linux", "windows") else "unclassified"
        groups[target]["hosts"][name] = hv
        if h.get("bmc"):
            groups[target]["hosts"][name]["bmc_host"] = h["bmc"]
        if name in pilot:
            groups["pilot"]["hosts"][name] = None
    return {"all": {"children": {g: v for g, v in groups.items() if v["hosts"]}}}


def report_rows(entries: list[Entry]) -> list[dict]:
    rows = []
    for e in entries:
        d = asdict(e)
        d["aliases"] = " ".join(e.aliases)
        d["issues"] = ";".join(e.issues)
        rows.append(d)
    return rows


ENTRY_FIELDS = ["line_no", "ip", "ip_version", "hostname", "fqdn", "aliases", "status", "issues",
                "prefix_known", "prefix", "bmc_candidate_of", "raw"]


def read_hosts_file(path: Path | str) -> str:
    data = Path(path).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")
