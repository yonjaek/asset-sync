#!/usr/bin/env python3
"""asset-sync CLI.

  parse   hosts file -> normalized CSV/JSON + Ansible inventory + parse report
  bmc     read-only Redfish/IPMI collection for BMC hosts
  build   merge raw collector output -> data/assets.json (per-field provenance)
  check   verify NetBox version / reachability / required custom fields & tag
  sync    plan (default, dry-run) or --apply the NetBox changes; writes review_report.csv
  report  summarize a run: success / failed / unverified / duplicate / review
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asset_sync import bmc as bmcmod  # noqa: E402
from asset_sync import hostsparse, normalize  # noqa: E402
from asset_sync.common import (PROJECT_ROOT, load_settings, load_yaml, mask, now_iso, setup_logging,  # noqa: E402
                               write_csv, write_json, write_yaml)
from asset_sync.nbclient import NetBox, NetBoxError  # noqa: E402
from asset_sync.sync import REVIEW_FIELDS, Syncer  # noqa: E402

DATA = PROJECT_ROOT / "data"
REPORTS = PROJECT_ROOT / "reports"
GEN_INV = PROJECT_ROOT / "inventory" / "hosts.generated.yml"  # next to inventory/group_vars so Ansible loads them
OVERRIDES = PROJECT_ROOT / "inventory" / "host_overrides.yml"
PINS = DATA / "bmc_cert_pins.json"


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _tls_verify(env_name: str) -> bool | str:
    ca = os.environ.get(env_name.replace("VERIFY_TLS", "CA_BUNDLE"))
    return ca if ca else _env_bool(env_name, True)


def _pilot(arg: str | None) -> list[str]:
    if not arg:
        return []
    p = Path(arg)
    if p.exists():
        return [ln.split("#")[0].strip().lower() for ln in p.read_text().splitlines() if ln.split("#")[0].strip()]
    return [x.strip().lower() for x in arg.split(",") if x.strip()]


# ------------------------------------------------------------------ parse
def cmd_parse(args, settings, log) -> int:
    text = hostsparse.read_hosts_file(args.hosts)
    entries = hostsparse.parse_hosts(text, settings)
    overrides = load_yaml(args.overrides or OVERRIDES, {})
    hosts = hostsparse.build_hosts(entries, overrides)
    pilot = _pilot(args.pilot)
    unknown = [p for p in pilot if p not in hosts]
    if unknown:
        log.warning("pilot hosts not in hosts file: %s", ", ".join(unknown))
    inv = hostsparse.build_inventory(hosts, pilot)

    rows = hostsparse.report_rows(entries)
    write_csv(REPORTS / "hosts_normalized.csv", rows, hostsparse.ENTRY_FIELDS)
    write_json(DATA / "hosts_normalized.json", {"source": str(args.hosts), "parsed_at": now_iso(),
                                                "entries": rows, "hosts": hosts})
    write_yaml(GEN_INV, inv)

    st = Counter(e.status for e in entries)
    issues = Counter(i.split(":")[0] for e in entries for i in e.issues)
    groups = {g: len(v["hosts"]) for g, v in inv["all"]["children"].items()}
    md = [f"# hosts parse report ({now_iso()})", "", f"source: `{args.hosts}`", "",
          "## Row status", *[f"- {k}: {v}" for k, v in sorted(st.items())], "",
          "## Issues", *[f"- {k}: {v}" for k, v in issues.most_common()], "",
          "## Inventory groups", *[f"- {k}: {v}" for k, v in groups.items()], "",
          "## Rows needing attention", "", "| line | ip | hostname | status | issues |", "|---|---|---|---|---|"]
    for e in entries:
        if e.status != "ok" or any(not i.startswith(("mask_unknown", "ipv6", "fqdn_from", "dual_stack")) for i in e.issues):
            md.append(f"| {e.line_no} | {e.ip or ''} | {e.hostname or ''} | {e.status} | {'; '.join(e.issues)} |")
    (REPORTS / "hosts_parse_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"rows={len(entries)} status={dict(st)} hosts={len(hosts)} groups={groups}")
    print(f"-> {REPORTS/'hosts_normalized.csv'}\n-> {DATA/'hosts_normalized.json'}\n-> {GEN_INV}\n-> {REPORTS/'hosts_parse_report.md'}")
    return 0


def cmd_import_excel(args, settings, log) -> int:
    from asset_sync import excelimport
    if args.site:
        settings.setdefault("excel", {})["site"] = args.site
    if args.sheet:
        settings.setdefault("excel", {})["sheet"] = args.sheet
    if args.site or args.sheet:  # explicit single sheet/site: ignore the per-IDC sheet map
        settings["excel"]["sheets"] = None
    overrides = load_yaml(args.overrides or OVERRIDES, {})
    hosts, rows = excelimport.build_hosts_from_excel(args.file, settings, overrides)
    pilot = _pilot(args.pilot)
    unknown = [p for p in pilot if p not in hosts]
    if unknown:
        log.warning("pilot hosts not in Excel import: %s", ", ".join(unknown))
    inv = hostsparse.build_inventory(hosts, pilot)
    fields = ["sheet", "row", "category", "model", "key", "status", "issues"]
    write_csv(REPORTS / "excel_import.csv", rows, fields)
    write_json(DATA / "hosts_normalized.json", {"source": str(args.file), "source_type": "excel", "parsed_at": now_iso(),
                                                "entries": rows, "hosts": hosts})
    write_yaml(GEN_INV, inv)
    st = Counter(r["status"] for r in rows)
    issues = Counter(i.split(":")[0] for r in rows if r["status"] != "excluded" for i in r["issues"])
    excl = Counter(i for r in rows if r["status"] == "excluded" for i in r["issues"])
    roles = Counter(h["role"] for h in hosts.values())
    groups = {g: len(v["hosts"]) for g, v in inv["all"]["children"].items()}
    md = [f"# Excel import report ({now_iso()})", "", f"source: `{args.file}` sheets `{', '.join(sorted({r['sheet'] for r in rows}))}`", "",
          "## Row status", *[f"- {k}: {v}" for k, v in sorted(st.items())], "",
          "## Excluded", *[f"- {k}: {v}" for k, v in excl.most_common()], "",
          "## Issues (imported rows)", *[f"- {k}: {v}" for k, v in issues.most_common()], "",
          "## Roles", *[f"- {k}: {v}" for k, v in roles.most_common()], "",
          "## Inventory groups", *[f"- {k}: {v}" for k, v in groups.items()], "",
          "## Rows needing attention", "", "| sheet | row | key | status | issues |", "|---|---|---|---|---|"]
    for r in rows:
        if r["status"] in ("review", "invalid") or any(not i.startswith(("hostname_not_usable",)) for i in r["issues"]) and r["status"] != "excluded":
            md.append(f"| {r['sheet']} | {r['row']} | {r.get('key') or ''} | {r['status']} | {'; '.join(r['issues'])} |")
    (REPORTS / "excel_import_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"rows={len(rows)} status={dict(st)} hosts={len(hosts)} roles={dict(roles)} groups={groups}")
    print(f"-> {REPORTS/'excel_import.csv'}\n-> {REPORTS/'excel_import_report.md'}\n-> {GEN_INV}")
    return 0


def _hosts() -> dict:
    return json.loads((DATA / "hosts_normalized.json").read_text())["hosts"]


# ------------------------------------------------------------------ bmc
def cmd_bmc(args, settings, log) -> int:
    hosts = _hosts()
    limit = set(_pilot(args.limit))
    targets = _bmc_targets(hosts, limit)
    user = os.environ.get("BMC_USERNAME")
    pw = os.environ.get("BMC_PASSWORD")
    if not (user and pw):
        log.warning("BMC_USERNAME/BMC_PASSWORD not set: all BMC targets skipped")
    mode = (os.environ.get("BMC_VERIFY_TLS") or "true").strip().lower()
    verify = _tls_verify("BMC_VERIFY_TLS") if mode != "pin" else True
    pins = json.loads(PINS.read_text()) if PINS.exists() else {}
    def one(t: str) -> dict:
        bh = hosts.get(t)
        ip = bh["ansible_host"] if bh else t
        btype = (bh or {}).get("bmc_type") or next((h.get("bmc_type") for h in hosts.values() if h.get("bmc") == t), None) or "redfish"
        if not (user and pw):
            res = {"host": t, "collector": f"bmc_{btype}", "status": "skipped", "reason": "no_credentials", "collected_at": now_iso()}
        elif btype == "ipmi":
            res = bmcmod.collect_ipmi(ip, user, pw)
        elif mode == "pin" and ip not in pins:
            res = {"host": t, "collector": "bmc_redfish", "status": "skipped", "reason": "no_pinned_cert", "collected_at": now_iso()}
        else:
            res = bmcmod.collect_redfish(ip, user, pw, verify, fingerprint=pins.get(ip, {}).get("sha256") if mode == "pin" else None,
                                         timeout=args.timeout)
        res["connected_ip"] = ip
        write_json(DATA / "raw" / "bmc" / f"{t}.json", res)
        log.info("bmc %s (%s): %s %s", t, btype, res["status"], res.get("reason", ""))
        return res

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(one, targets))
    c = Counter(r["status"] if r["status"] == "ok" else f"skipped:{r.get('reason')}" for r in results)
    print(f"bmc targets={len(targets)} " + " ".join(f"{k}={v}" for k, v in sorted(c.items())))
    return 0


def _bmc_targets(hosts: dict, limit: set) -> list[str]:
    """BMC addresses of hosts in scope; hosts marked skip are never contacted."""
    return sorted({h["bmc"] for n, h in hosts.items()
                   if h.get("bmc") and not h.get("skip") and (not limit or n in limit)})


def cmd_bmc_trust(args, settings, log) -> int:
    """Record the certificate each BMC presents (trust on first use). Review the output before relying on it."""
    hosts = _hosts()
    limit = set(_pilot(args.limit))
    targets = _bmc_targets(hosts, limit)
    pins = json.loads(PINS.read_text()) if PINS.exists() else {}

    def fetch(t: str):
        bh = hosts.get(t)
        ip = bh["ansible_host"] if bh else t
        try:
            return ip, bmcmod.fetch_cert(ip), None
        except OSError as exc:
            return ip, None, type(exc).__name__

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        fetched = list(ex.map(fetch, targets))
    stats = Counter()
    for ip, cert, err in sorted(fetched):
        if err:
            stats["no_tls"] += 1
            print(f"  {ip:<16} -- no TLS handshake ({err})")
            continue
        old = pins.get(ip, {}).get("sha256")
        if old and old != cert["sha256"] and not args.replace:
            stats["changed"] += 1
            print(f"  {ip:<16} !! certificate CHANGED (pinned {old[:16]}..., now {cert['sha256'][:16]}...). Use --replace if expected.")
            continue
        stats["kept" if old == cert["sha256"] else "pinned"] += 1
        pins[ip] = cert
        subj = next((d for d in cert["details"] if d.startswith("subject")), "")
        subj = re.sub(r"(CN\s*=\s*)([^,]+)", lambda m: m.group(1) + mask(m.group(2), 3), subj)  # iLO CN embeds the serial
        if args.verbose_list:
            exp = next((d for d in cert["details"] if d.startswith("notAfter")), "")
            print(f"  {ip:<16} {cert['sha256'][:32]}...  {subj[:60]}  {exp}")
    write_json(PINS, pins)
    print(f"targets={len(targets)} " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())) + f" total_pins={len(pins)} -> {PINS}")
    return 0


# ------------------------------------------------------------------ build
def cmd_build(args, settings, log) -> int:
    hosts = _hosts()
    limit = set(_pilot(args.limit))
    if limit:
        hosts = {k: v for k, v in hosts.items() if k in limit or any(hosts.get(l, {}).get("bmc") == k for l in limit)}
    assets = normalize.build_assets(hosts, DATA / "raw", settings)
    write_json(DATA / "assets.json", {"built_at": now_iso(), "assets": assets})
    rows = []
    for a in assets:
        f = a["fields"]
        g = lambda k: (f.get(k) or {}).get("value")  # noqa: E731
        rows.append({"asset": a["key"], "kind": a["kind"],
                     "collection": ";".join(f"{c.get('collector')}={c.get('status')}{'(' + str(c.get('reason')) + ')' if c.get('reason') else ''}"
                                            for c in a["collection"]),
                     "hostname": g("hostname"), "manufacturer": g("manufacturer"), "model": g("model"),
                     "serial": mask(g("serial")), "asset_tag": mask(g("asset_tag")), "os": g("platform"),
                     "is_virtual": g("is_virtual"), "bmc_ip": g("bmc_ip"),
                     "interfaces": " ".join(f"{i['name']}{'*' if i.get('is_mgmt') else ''}" for i in a["interfaces"]),
                     "notes": ";".join(a["notes"])})
    write_csv(REPORTS / "assets_summary.csv", rows, list(rows[0].keys()) if rows else ["asset"])
    print(f"assets={len(assets)} kinds={dict(Counter(a['kind'] for a in assets))} -> {DATA/'assets.json'}")
    return 0


# ------------------------------------------------------------------ netbox
def _nb(settings, write: bool = False) -> NetBox:
    """Reads use the read-only NETBOX_TOKEN (same as MCP); --apply requires NETBOX_WRITE_TOKEN."""
    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_WRITE_TOKEN") if write else (os.environ.get("NETBOX_TOKEN") or os.environ.get("NETBOX_WRITE_TOKEN"))
    if write and not token:
        raise NetBoxError("--apply requires NETBOX_WRITE_TOKEN")
    return NetBox(url, token, verify=_tls_verify("NETBOX_VERIFY_TLS"))


def _preflight(nb: NetBox, settings: dict) -> list[str]:
    problems = []
    v = nb.version
    minv = tuple(int(x) for x in str(settings.get("netbox", {}).get("min_version", "4.1")).split("."))
    if v < minv:
        problems.append(f"NetBox {'.'.join(map(str, v))} < required {'.'.join(map(str, minv))}")
    cfs = {c["name"]: c for c in nb.filter("extras/custom-fields")}
    for name, types in (("mgmt_nic", {"dcim.interface", "virtualization.vminterface"}), ("bmc_ip", {"dcim.device"})):
        c = cfs.get(name)
        if not c:
            problems.append(f"custom field '{name}' missing (run playbooks/netbox_bootstrap.yml)")
        elif not types <= set(c.get("object_types") or c.get("content_types") or []):
            problems.append(f"custom field '{name}' not enabled for {sorted(types)}")
    tag = settings.get("sync", {}).get("tag", "asset-sync")
    if not nb.filter("extras/tags", slug=tag):
        problems.append(f"tag '{tag}' missing (run playbooks/netbox_bootstrap.yml)")
    return problems


def cmd_check(args, settings, log) -> int:
    nb = _nb(settings, write=False)
    st = nb.status()
    print(json.dumps({k: st.get(k) for k in ("netbox-version", "python-version", "rq-workers-running", "django-version")}, indent=2))
    problems = _preflight(nb, settings)
    for p in problems:
        print("PROBLEM:", p)
    print("preflight OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_sync(args, settings, log) -> int:
    assets = json.loads((DATA / "assets.json").read_text())["assets"]
    limit = set(_pilot(args.limit))
    if limit:
        assets = [a for a in assets if a["key"] in limit]
    nb = _nb(settings, write=args.apply)
    problems = _preflight(nb, settings)
    if problems:
        for p in problems:
            log.error("preflight: %s", p)
        if args.apply:
            return 2
    max_hosts = args.max_hosts if args.max_hosts is not None else settings.get("sync", {}).get("max_hosts", 5)

    plan = Syncer(nb, settings, apply=False).run(assets)
    changing = [r for r in plan["results"] if r["status"] == "changed"]
    stamp = now_iso().replace(":", "")
    _write_run(plan, "plan", stamp, assets)
    print(_summary(plan, "DRY-RUN plan"))
    if not args.apply:
        print("\nNo changes written. Re-run with --apply after reviewing reports/sync_plan_*.md")
        return 0
    if len(changing) > max_hosts:
        log.error("%d assets would change, above --max-hosts=%d. Aborting before any write.", len(changing), max_hosts)
        return 3
    if not changing:
        print("Nothing to apply.")
        return 0
    if not args.yes:
        ans = input(f"Apply {len(plan['ops'])} write(s) to {len(changing)} asset(s) on {os.environ.get('NETBOX_URL')}? type 'apply': ")
        if ans.strip() != "apply":
            print("aborted")
            return 4
    allowed = {r["asset"] for r in changing}
    res = Syncer(nb, settings, apply=True).run([a for a in assets if a["key"] in allowed], context=assets)
    _write_run(res, "apply", stamp, assets)
    print(_summary(res, "APPLY result"))
    return 0 if not any(r["status"] == "failed" for r in res["results"]) else 5


def _summary(run: dict, title: str) -> str:
    c = Counter(r["status"] for r in run["results"])
    ops = Counter(f"{o['action']} {o['endpoint']}" for o in run["ops"])
    lines = [f"== {title} ==", "assets: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())),
             f"writes: {len(run['ops'])}", *[f"  {v:4d} x {k}" for k, v in ops.most_common()],
             f"review rows: {len(run['review'])}"]
    return "\n".join(lines)


def _write_run(run: dict, kind: str, stamp: str, assets: list[dict]) -> None:
    write_json(REPORTS / f"sync_{kind}_{stamp}.json", run)
    # the plan covers every asset in scope; an apply only re-runs the changing ones, so it must not shrink the main report
    name = "review_report.csv" if kind == "plan" else f"review_report_apply_{stamp}.csv"
    write_csv(REPORTS / name, run["review"], REVIEW_FIELDS)
    md = [f"# sync {kind} {stamp}", "", "| asset | kind | status | reason | writes |", "|---|---|---|---|---|"]
    for r in run["results"]:
        md.append(f"| {r['asset']} | {r.get('kind')} | {r['status']} | {r.get('reason', '')} | {r.get('changes', 0)} |")
    md += ["", "## Planned writes" if kind == "plan" else "## Writes", ""]
    md += [f"- {o['action']} `{o['endpoint']}` {o['object']} fields={','.join(o['fields'])}" for o in run["ops"]]
    (REPORTS / f"sync_{kind}_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ report
def cmd_report(args, settings, log) -> int:
    hosts = json.loads((DATA / "hosts_normalized.json").read_text())
    assets = json.loads((DATA / "assets.json").read_text())["assets"]
    # most recent run of either kind (an old pilot apply must not shadow a newer plan)
    plans = sorted(list(REPORTS.glob("sync_apply_*.json")) + list(REPORTS.glob("sync_plan_*.json")), key=lambda p: p.stat().st_mtime)
    run = json.loads(plans[-1].read_text()) if plans else {"results": []}
    by = {r["asset"]: r for r in run["results"]}
    rows = []
    for a in assets:
        r = by.get(a["key"], {})
        dup = any(i.startswith(("duplicate_ip", "duplicate_hostname", "duplicate_alias")) for i in a["hosts_file"]["issues"]) or \
            r.get("reason", "").startswith(("duplicate", "serial_name_mismatch", "exists_as"))
        if dup:
            cat = "duplicate"
        elif r.get("status") == "failed" or any(c.get("status") == "skipped" for c in a["collection"]) and \
                not any(c.get("status") == "ok" for c in a["collection"]):
            cat = "failed"
        elif not any(c.get("status") == "ok" for c in a["collection"]) or r.get("status") in (None, "review", "skipped"):
            cat = "unverified"  # not collected, held for review, or not synced yet
        else:
            cat = "success"
        rows.append({"asset": a["key"], "result": cat, "sync_status": r.get("status"), "reason": r.get("reason", ""),
                     "collection": ";".join(f"{c.get('collector')}={c.get('status')}:{c.get('reason') or ''}" for c in a["collection"])})
    invalid = [e for e in hosts["entries"] if e["status"] in ("invalid", "excluded")]
    write_csv(REPORTS / "run_report.csv", rows, ["asset", "result", "sync_status", "reason", "collection"])
    c = Counter(r["result"] for r in rows)
    print(f"source run: {plans[-1].name if plans else '(none)'}")
    print("result: " + ", ".join(f"{k}={c.get(k, 0)}" for k in ("success", "failed", "unverified", "duplicate")))
    print(f"hosts-file rows invalid/excluded: {len(invalid)}")
    for r in rows:
        print(f"  {r['asset']:<24} {r['result']:<10} {r['sync_status'] or '-':<10} {r['reason']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("parse"); p.add_argument("--hosts", required=True); p.add_argument("--overrides"); p.add_argument("--pilot", help="comma list or file")
    p = sp.add_parser("import-excel"); p.add_argument("--file", required=True); p.add_argument("--site"); p.add_argument("--sheet")
    p.add_argument("--overrides"); p.add_argument("--pilot", help="comma list or file")
    p = sp.add_parser("bmc"); p.add_argument("--limit"); p.add_argument("--workers", type=int, default=8)
    p.add_argument("--timeout", type=int, default=15, help="per-request read timeout (slow BMCs: 60-120)")
    p = sp.add_parser("bmc-trust", help="record BMC TLS certificate fingerprints (for BMC_VERIFY_TLS=pin)")
    p.add_argument("--limit"); p.add_argument("--replace", action="store_true"); p.add_argument("--workers", type=int, default=8)
    p.add_argument("--list", dest="verbose_list", action="store_true", help="print every pinned certificate")
    p = sp.add_parser("build"); p.add_argument("--limit")
    sp.add_parser("check")
    p = sp.add_parser("sync"); p.add_argument("--apply", action="store_true", help="write to NetBox (default: dry-run)")
    p.add_argument("--yes", action="store_true", help="skip interactive confirmation (still bound by --max-hosts)")
    p.add_argument("--max-hosts", type=int, default=None); p.add_argument("--limit")
    sp.add_parser("report")
    args = ap.parse_args()
    log = setup_logging(args.verbose)
    settings = load_settings(args.settings)
    try:
        return {"parse": cmd_parse, "import-excel": cmd_import_excel, "bmc": cmd_bmc, "bmc-trust": cmd_bmc_trust, "build": cmd_build, "check": cmd_check,
                "sync": cmd_sync, "report": cmd_report}[args.cmd](args, settings, log)
    except NetBoxError as exc:
        log.error("%s", exc)
        return 2
    except FileNotFoundError as exc:
        log.error("missing input: %s (run the previous step first)", exc.filename)
        return 2


if __name__ == "__main__":
    sys.exit(main())
