"""Idempotent NetBox sync.

Rules
- Only fields with confidence=high are written. Medium/conflict values go to review_report.csv.
- New objects are created; for existing objects only *empty* NetBox fields are filled. A non-empty NetBox
  value that differs from the collected one is never overwritten: it becomes a review row.
- Identity is checked before any write: serial and name must point to the same object, no duplicates,
  the object must not already exist with the other kind (device vs VM), IP/MAC must not belong to another object.
- Sites, roles and clusters are never created here (classification is a human decision; see
  playbooks/netbox_bootstrap.yml). Manufacturer / device type / platform are created from high-confidence data
  when sync.auto_create_reference is true.
- Nothing is ever deleted. Stale objects are only reported.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

from .common import mask, now_iso, slugify
from .nbclient import NetBoxError, Writer

log = logging.getLogger("asset_sync.sync")

SENSITIVE = {"serial", "asset_tag"}
IDENTITY_FIELDS = ("hostname", "serial", "is_virtual")
IFACE_TYPE = {"physical": "other", "virtual": "virtual", "lag": "lag", "bridge": "bridge", "other": "other"}

REVIEW_FIELDS = ["asset", "object", "field", "collected_value", "netbox_value", "source", "collected_at",
                 "confidence", "reason", "action_required"]


def _id(v: Any) -> int | None:
    return v.get("id") if isinstance(v, dict) else v


def _empty(v: Any) -> bool:
    return v in (None, "", [], {})


class Syncer:
    def __init__(self, nb, settings: dict, apply: bool = False):
        self.nb = nb
        self.cfg = settings
        self.sync_cfg = settings.get("sync", {}) or {}
        self.w = Writer(nb, apply=apply)
        self.apply = apply
        self.review: list[dict] = []
        self.results: list[dict] = []
        self._cache: dict[tuple, Any] = {}
        self.aliases = {k.lower(): v for k, v in (settings.get("manufacturer_aliases") or {}).items()}
        self.mac_model = nb.version >= (4, 2)
        self.vm_serial = nb.version >= (4, 1)
        self.tag = self.sync_cfg.get("tag", "asset-sync")

    # ------------------------------------------------------------ helpers
    def _review(self, asset: str, obj: str, field: str, reason: str, fvd: dict | None = None,
                netbox_value: Any = None, action: str = "verify and update manually") -> None:
        fvd = fvd or {}
        val = fvd.get("value") if fvd.get("confidence") != "conflict" else \
            " | ".join(f"{c['source']}={mask(c['value']) if field in SENSITIVE else c['value']}" for c in fvd.get("candidates", []))
        if field in SENSITIVE and fvd.get("confidence") != "conflict":
            val, netbox_value = mask(val), mask(netbox_value)
        self.review.append({"asset": asset, "object": obj, "field": field, "collected_value": val,
                            "netbox_value": netbox_value, "source": fvd.get("source"),
                            "collected_at": fvd.get("collected_at"), "confidence": fvd.get("confidence"),
                            "reason": reason, "action_required": action})

    def _one(self, endpoint: str, **flt) -> list[dict]:
        if any(v is None for v in flt.values()):
            return []  # parent planned but not created (dry-run): nothing can exist yet
        return self.nb.filter(endpoint, **flt)

    def _lookup_ref(self, endpoint: str, slug: str) -> dict | None:
        key = (endpoint, slug)
        if key not in self._cache:
            found = self.nb.filter(endpoint, slug=slug)
            self._cache[key] = found[0] if found else None
        return self._cache[key]

    def _tag_ref(self) -> list[dict]:
        return [{"slug": self.tag}] if self._lookup_ref("extras/tags", self.tag) else []

    def _ensure_ref(self, endpoint: str, name: str, extra: dict, asset: str, slug: str | None = None) -> dict | None:
        slug = slug or slugify(name)
        obj = self._lookup_ref(endpoint, slug)
        if obj:
            return obj
        by_name = [o for o in self.nb.filter(endpoint, name=name) if o.get("name", "").lower() == name.lower()]
        if by_name:
            self._cache[(endpoint, slug)] = by_name[0]
            return by_name[0]
        if not self.sync_cfg.get("auto_create_reference", True):
            self._review(asset, endpoint, "name", f"{endpoint} '{name}' missing and auto_create_reference=false")
            return None
        obj = self.w.create(endpoint, {"name": name, "slug": slug, **extra}, asset, name)
        self._cache[(endpoint, slug)] = obj
        return obj

    def _high(self, a: dict, name: str) -> Any:
        f = a["fields"].get(name)
        return f["value"] if f and f["confidence"] == "high" else None

    def _journal(self, obj_type: str, obj: dict, asset: dict, fields: list[str]) -> None:
        if not self.sync_cfg.get("journal", True) or not fields:
            return
        srcs = sorted({f"{k}<-{asset['fields'][k]['source']}@{asset['fields'][k]['collected_at']}"
                       for k in fields if k in asset["fields"]})
        self.w.create("extras/journal-entries", {
            "assigned_object_type": obj_type, "assigned_object_id": obj.get("id"), "kind": "info",
            "comments": f"asset-sync {now_iso()}: set {', '.join(sorted(fields))}\n" + "\n".join(srcs)},
            asset["key"], f"journal:{asset['key']}")

    # ------------------------------------------------------------ gates
    def _gate(self, a: dict) -> str | None:
        """Return a reason string if the asset must not be written at all."""
        name = a["key"]
        if not any(c.get("status") == "ok" for c in a["collection"]):
            reasons = ",".join(f"{c.get('collector')}:{c.get('reason')}" for c in a["collection"]) or "no_collection"
            return f"not_collected({reasons})"
        for f in IDENTITY_FIELDS:
            fvd = a["fields"].get(f)
            if fvd and fvd["confidence"] == "conflict":
                self._review(name, "asset", f, "conflicting sources for identity field", fvd)
                return f"identity_conflict:{f}"
        hn = self._high(a, "hostname")
        if not hn:
            self._review(name, "asset", "hostname", "hostname not confirmed by a collector")
            return "hostname_unverified"
        src_list = a["hosts_file"].get("source") or a.get("source") or "hosts_file"
        if not a.get("name_trusted", True):
            # the list entry had no usable hostname (e.g. a description in the Excel hostname cell):
            # the NetBox name comes from the host itself
            a["nb_name"] = hn.split(".")[0]
        elif hn.split(".")[0] != name:
            self._review(name, "asset", "hostname", f"collected hostname differs from {src_list}", a["fields"]["hostname"],
                         netbox_value=None, action=f"fix {src_list} or host; {src_list} says '{name}'")
            return "hostname_mismatch"
        if a["kind"] == "unknown":
            self._review(name, "asset", "is_virtual", "cannot tell physical vs virtual", a["fields"].get("is_virtual"))
            return "kind_unknown"
        if any(n.startswith("container_guest") for n in a["notes"]):
            return "container_guest"
        if a["hosts_file"]["status"] == "review" and any(
                i.startswith(("duplicate_ip_conflict", "duplicate_hostname")) for i in a["hosts_file"]["issues"]):
            self._review(name, "hosts_file", "ip", "hosts file conflict: " + ";".join(a["hosts_file"]["issues"]))
            return "hosts_file_conflict"
        return None

    # ------------------------------------------------------------ entry point
    def run(self, assets: list[dict]) -> dict:
        for a in assets:
            before = len(self.w.ops)
            try:
                reason = self._gate(a)
                if reason:
                    status = "review" if not reason.startswith("not_collected") else "skipped"
                    self.results.append({"asset": a["key"], "kind": a.get("kind"), "status": status, "reason": reason})
                    continue
                held = self._sync_vm(a) if a["kind"] == "vm" else self._sync_device(a)
                changed = len(self.w.ops) > before
                status = "review" if held else ("changed" if changed else "unchanged")
                self.results.append({"asset": a["key"], "kind": a["kind"], "status": status,
                                     "reason": held or "", "changes": len(self.w.ops) - before})
            except NetBoxError as exc:
                log.error("asset %s failed: %s", a["key"], exc)
                self.results.append({"asset": a["key"], "kind": a.get("kind"), "status": "failed", "reason": str(exc)[:300]})
        for a in assets:  # field-level review rows for medium / conflicting non-identity values
            for k, f in a["fields"].items():
                if f["confidence"] == "medium":
                    self._review(a["key"], "asset", k, "low-confidence source; not applied automatically", f)
                elif f["confidence"] == "conflict" and k not in IDENTITY_FIELDS:
                    self._review(a["key"], "asset", k, "sources disagree; not applied", f)
            for d in a.get("excel_diffs", []):
                sens = d["field"] == "serial"
                self.review.append({"asset": a["key"], "object": "excel", "field": d["field"],
                                    "collected_value": mask(d["excel"]) if sens else d["excel"],
                                    "netbox_value": "", "source": d["source"], "collected_at": d["collected_at"],
                                    "confidence": "medium",
                                    "reason": f"Excel differs from collected value ({mask(d['collected']) if sens else d['collected']} "
                                              f"from {d['collected_source']}); collected value used",
                                    "action_required": "correct the Excel row"})
            if "mgmt_nic_unidentified" in a["notes"]:
                cand = next((i["name"] for i in a["interfaces"] if i.get("mgmt_candidate")), None)
                self._review(a["key"], "interface", "mgmt_nic", "connected IP not found on any NIC (NAT/proxy?)",
                             {"value": cand, "source": "default_route", "confidence": "medium"},
                             action="confirm management NIC and set mgmt_nic manually")
        return {"results": self.results, "ops": self.w.ops, "review": self.review}

    # ------------------------------------------------------------ device
    def _classify(self, a: dict, endpoint: str, slug: str | None, field: str) -> dict | None:
        if not slug:
            self._review(a["key"], "classification", field, f"{field} not set (inventory/host_overrides.yml or defaults)",
                         action=f"assign {field} in host_overrides.yml")
            return None
        obj = self._lookup_ref(endpoint, slug)
        if not obj:
            self._review(a["key"], "classification", field, f"{field} '{slug}' does not exist in NetBox",
                         action="create it via playbooks/netbox_bootstrap.yml")
        return obj

    def _sync_device(self, a: dict) -> str | None:
        name = a.get("nb_name") or a["key"]
        cls = a["classification"]
        role_slug = cls.get("role") or self.cfg.get("defaults", {}).get("device_role")
        site = self._classify(a, "dcim/sites", cls.get("site"), "site")
        role = self._classify(a, "dcim/device-roles", role_slug, "role")
        mfr_raw, model = self._high(a, "manufacturer"), self._high(a, "model")
        if not (mfr_raw and model):
            self._review(name, "device", "device_type", "manufacturer/model not confirmed")
        if not (site and role and mfr_raw and model):
            return "missing_classification"
        rack = self._rack(a, site)

        serial = self._high(a, "serial")
        by_serial = [d for d in self.nb.filter("dcim/devices", serial=serial)
                     if (d.get("serial") or "").lower() == serial.lower()] if serial else []
        by_name = [d for d in self.nb.filter("dcim/devices", name=name) if d.get("name", "").lower() == name.lower()]
        if self.nb.filter("virtualization/virtual-machines", name=name):
            self._review(name, "device", "name", "a VirtualMachine with this name exists; collected data says physical",
                         a["fields"].get("is_virtual"))
            return "exists_as_vm"
        if len(by_serial) > 1:
            self._review(name, "device", "serial", f"{len(by_serial)} devices share this serial", a["fields"]["serial"])
            return "duplicate_serial_in_netbox"
        if len(by_name) > 1:
            self._review(name, "device", "name", f"{len(by_name)} devices share this name (different sites?)")
            return "duplicate_name_in_netbox"
        s_obj, n_obj = (by_serial or [None])[0], (by_name or [None])[0]
        if s_obj and n_obj and s_obj["id"] != n_obj["id"]:
            self._review(name, "device", "serial", f"serial belongs to device '{s_obj['name']}' (id {s_obj['id']})",
                         a["fields"]["serial"], netbox_value=n_obj.get("serial"))
            return "serial_name_mismatch"
        obj = s_obj or n_obj
        if obj and s_obj and obj.get("name", "").lower() != name.lower():
            self._review(name, "device", "name", "serial matches a device with another name (renamed host?)",
                         a["fields"]["hostname"], netbox_value=obj.get("name"), action="rename in NetBox or fix hostname")
            return "name_differs_for_serial"
        if obj and serial and obj.get("serial") and obj["serial"].lower() != serial.lower():
            self._review(name, "device", "serial", "NetBox device with this name has a different serial (hardware swap?)",
                         a["fields"]["serial"], netbox_value=obj["serial"])
            return "serial_mismatch"

        mfr_name = self.aliases.get(mfr_raw.lower(), mfr_raw)
        for prefix in {mfr_raw, mfr_name}:  # "HPE Cray XD670" under manufacturer HPE -> "Cray XD670"
            if model.lower().startswith(prefix.lower() + " "):
                model = model[len(prefix) + 1:].strip()
        mfr = self._ensure_ref("dcim/manufacturers", mfr_name, {}, name)
        dtype = None
        if mfr:
            dkey = ("dcim/device-types", mfr_name.lower(), model.lower())
            if dkey not in self._cache:  # cache planned objects too, so a dry-run counts each type once
                found = self._one("dcim/device-types", manufacturer_id=mfr.get("id"), model=model)
                self._cache[dkey] = found[0] if found else self.w.create(
                    "dcim/device-types", {"manufacturer": mfr.get("id"), "model": model,
                                          "slug": slugify(f"{mfr_name}-{model}")}, name, f"{mfr_name} {model}")
            dtype = self._cache[dkey]
        platform = self._platform(a)
        tag = self._tag_ref()
        asset_tag = self._high(a, "asset_tag")
        if asset_tag:
            clash = [d for d in self.nb.filter("dcim/devices", asset_tag=asset_tag) if not obj or d["id"] != obj["id"]]
            if clash:
                self._review(name, "device", "asset_tag", f"asset tag already used by '{clash[0]['name']}'", a["fields"]["asset_tag"])
                asset_tag = None
        bmc_ip = self._high(a, "bmc_ip")
        cf_bmc = {"bmc_ip": bmc_ip} if bmc_ip and self.sync_cfg.get("bmc_custom_field", True) else {}

        if not obj:
            data = {"name": name, "site": site["id"], "role": role["id"], "device_type": dtype and dtype.get("id"),
                    "status": self.sync_cfg.get("default_status", "active")}
            if serial:
                data["serial"] = serial
            if asset_tag:
                data["asset_tag"] = asset_tag
            if platform:
                data["platform"] = platform.get("id")
            if tag:
                data["tags"] = tag
            if cf_bmc:
                data["custom_fields"] = cf_bmc
            if rack:
                data["rack"] = rack["id"]
                if _id(rack.get("location")):
                    data["location"] = _id(rack["location"])
            obj = self.w.create("dcim/devices", data, name, name)
            self._journal("dcim.device", obj, a, [k for k in ("serial", "asset_tag", "manufacturer", "model", "platform", "bmc_ip")
                                                  if self._high(a, k)])
        else:
            patch: dict[str, Any] = {}
            for fld, want, have, src in (
                    ("site", site["id"], _id(obj.get("site")), None),
                    ("role", role["id"], _id(obj.get("role") or obj.get("device_role")), None),
                    ("device_type", dtype and dtype.get("id"), _id(obj.get("device_type")), "model"),
                    ("platform", platform and platform.get("id"), _id(obj.get("platform")), "platform"),
                    ("serial", serial, obj.get("serial"), "serial"),
                    ("asset_tag", asset_tag, obj.get("asset_tag"), "asset_tag"),
                    ("rack", rack and rack.get("id"), _id(obj.get("rack")), None),
                    ("location", rack and _id(rack.get("location")), _id(obj.get("location")), None)):
                if want is None:
                    continue
                if _empty(have):
                    patch[fld] = want
                elif have != want and not (isinstance(have, str) and str(want).lower() == have.lower()):
                    self._review(name, "device", fld, "NetBox value differs; not overwritten",
                                 a["fields"].get(src) if src else {"value": want, "source": "classification"},
                                 netbox_value=have)
            cfs = obj.get("custom_fields") or {}
            if cf_bmc and _empty(cfs.get("bmc_ip")):
                patch["custom_fields"] = cf_bmc
            elif cf_bmc and cfs.get("bmc_ip") != bmc_ip:
                self._review(name, "device", "bmc_ip", "NetBox bmc_ip differs", a["fields"]["bmc_ip"], cfs.get("bmc_ip"))
            if tag and self.tag not in {t.get("slug") for t in obj.get("tags", [])}:
                patch["tags"] = [{"slug": t["slug"]} for t in obj.get("tags", [])] + tag
            if patch:
                obj = self.w.patch("dcim/devices", obj, patch, name, name)
                self._journal("dcim.device", obj, a, [k for k in patch if k in a["fields"]])
        pos = (a.get("excel") or {}).get("position")
        if rack and pos and _empty(obj.get("position")):
            self._review(a["key"], "device", "position", "rack position from Excel is not applied automatically",
                         {"value": f"{rack.get('name')} U{pos} ({a['excel'].get('u_height')}U)", "source": f"excel:row{a['excel'].get('row')}",
                          "confidence": "medium"}, action="set position/face in NetBox after checking the rack")
        self._sync_interfaces(a, obj, vm=False)
        return None

    def _rack(self, a: dict, site: dict) -> dict | None:
        """Rack from the asset list. Not applied if the list row may describe another machine (serial differs)."""
        rname = a["classification"].get("rack")
        if not rname:
            return None
        if any(d["field"] in ("serial", "kind") for d in a.get("excel_diffs", [])):
            self._review(a["key"], "device", "rack", "Excel row serial/kind differs from the host: row may describe another device; rack not applied",
                         {"value": rname, "source": f"excel:row{(a.get('excel') or {}).get('row')}", "confidence": "medium"})
            return None
        found = self._one("dcim/racks", site_id=site.get("id"), name=rname)
        if not found:
            self._review(a["key"], "classification", "rack", f"rack '{rname}' does not exist in site '{site.get('slug')}'",
                         action="create it via playbooks/netbox_bootstrap.yml")
            return None
        return found[0]

    def _platform(self, a: dict) -> dict | None:
        pname = self._high(a, "platform")
        return self._ensure_ref("dcim/platforms", pname, {}, a["key"]) if pname else None

    # ------------------------------------------------------------ VM
    def _sync_vm(self, a: dict) -> str | None:
        name = a.get("nb_name") or a["key"]
        cls = a["classification"]
        cluster = self._classify(a, "virtualization/clusters", None, "cluster") if not cls.get("cluster") else None
        if cls.get("cluster"):
            found = self.nb.filter("virtualization/clusters", name=cls["cluster"])
            cluster = found[0] if found else None
            if not cluster:
                self._review(name, "classification", "cluster", f"cluster '{cls['cluster']}' does not exist in NetBox",
                             action="create it via playbooks/netbox_bootstrap.yml")
        role_slug = cls.get("role") or self.cfg.get("defaults", {}).get("vm_role")
        role = self._classify(a, "dcim/device-roles", role_slug, "role") if role_slug or self.sync_cfg.get("vm_role_required", True) else None
        if not cluster or (self.sync_cfg.get("vm_role_required", True) and not role):
            return "missing_classification"
        if self.nb.filter("dcim/devices", name=name):
            self._review(name, "vm", "name", "a physical Device with this name exists; collected data says virtual",
                         a["fields"].get("is_virtual"))
            return "exists_as_device"
        found = [v for v in self.nb.filter("virtualization/virtual-machines", name=name)
                 if v.get("name", "").lower() == name.lower()]
        if len(found) > 1:
            self._review(name, "vm", "name", f"{len(found)} VMs share this name")
            return "duplicate_name_in_netbox"
        obj = found[0] if found else None
        if obj and _id(obj.get("cluster")) not in (None, cluster.get("id")):
            self._review(name, "vm", "cluster", "VM exists in another cluster; not moved",
                         {"value": cls.get("cluster"), "source": "classification"}, netbox_value=(obj.get("cluster") or {}).get("name"))
            return "cluster_mismatch"
        platform = self._platform(a)
        serial = self._high(a, "serial") if self.vm_serial and self.sync_cfg.get("vm_serial", False) else None
        tag = self._tag_ref()
        if not obj:
            data = {"name": name, "cluster": cluster["id"], "status": self.sync_cfg.get("default_status", "active")}
            if (cluster.get("site") or {}).get("id"):
                data["site"] = cluster["site"]["id"]
            if role:
                data["role"] = role["id"]
            if platform:
                data["platform"] = platform.get("id")
            if serial:
                data["serial"] = serial
            if tag:
                data["tags"] = tag
            obj = self.w.create("virtualization/virtual-machines", data, name, name)
            self._journal("virtualization.virtualmachine", obj, a, [k for k in ("platform", "serial") if self._high(a, k)])
        else:
            patch = {}
            for fld, want, have, src in (("role", role and role["id"], _id(obj.get("role")), None),
                                         ("platform", platform and platform.get("id"), _id(obj.get("platform")), "platform"),
                                         ("serial", serial, obj.get("serial"), "serial")):
                if want is None:
                    continue
                if _empty(have):
                    patch[fld] = want
                elif have != want:
                    self._review(name, "vm", fld, "NetBox value differs; not overwritten",
                                 a["fields"].get(src) if src else {"value": want, "source": "classification"}, netbox_value=have)
            if tag and self.tag not in {t.get("slug") for t in obj.get("tags", [])}:
                patch["tags"] = [{"slug": t["slug"]} for t in obj.get("tags", [])] + tag
            if patch:
                obj = self.w.patch("virtualization/virtual-machines", obj, patch, name, name)
                self._journal("virtualization.virtualmachine", obj, a, [k for k in patch if k in a["fields"]])
        self._sync_interfaces(a, obj, vm=True)
        return None

    # ------------------------------------------------------------ interfaces / MAC / IP
    def _sync_interfaces(self, a: dict, parent: dict, vm: bool) -> None:
        name = a["key"]
        ep = "virtualization/interfaces" if vm else "dcim/interfaces"
        otype = "virtualization.vminterface" if vm else "dcim.interface"
        parent_key = "virtual_machine" if vm else "device"
        existing = {i["name"]: i for i in self._one(ep, **{f"{parent_key}_id": parent.get("id")})}
        primary: dict[int, dict] = {}
        for ci in a["interfaces"]:
            nb_if = existing.get(ci["name"])
            mgmt = bool(ci.get("is_mgmt"))
            if not nb_if:
                data = {parent_key: parent.get("id"), "name": ci["name"], "custom_fields": {"mgmt_nic": mgmt}}
                if not vm:
                    data["type"] = IFACE_TYPE.get(ci.get("kind"), "other")
                if not self.mac_model and ci.get("mac"):
                    data["mac_address"] = ci["mac"]
                nb_if = self.w.create(ep, data, name, f"{name}:{ci['name']}")
            else:
                patch = {}
                cur = (nb_if.get("custom_fields") or {}).get("mgmt_nic")
                if cur is None:
                    patch["custom_fields"] = {"mgmt_nic": mgmt}
                elif bool(cur) != mgmt:
                    self._review(name, "interface", f"{ci['name']}.mgmt_nic", "mgmt flag differs; not overwritten",
                                 {"value": mgmt, "source": ci.get("mgmt_source", ci["source"]), "collected_at": ci["collected_at"],
                                  "confidence": "high"}, netbox_value=cur)
                if not self.mac_model and ci.get("mac"):
                    have = nb_if.get("mac_address")
                    if _empty(have):
                        patch["mac_address"] = ci["mac"]
                    elif have.upper() != ci["mac"]:
                        self._review(name, "interface", f"{ci['name']}.mac", "MAC differs", {"value": ci["mac"], "source": ci["source"],
                                     "collected_at": ci["collected_at"], "confidence": "high"}, netbox_value=have)
                if patch:
                    nb_if = self.w.patch(ep, nb_if, patch, name, f"{name}:{ci['name']}")
            if self.mac_model and ci.get("mac"):
                self._sync_mac(a, ci, nb_if, ep, otype, parent)
            for addr in ci["addresses"]:
                ip = self._sync_ip(a, ci, addr, nb_if, otype)
                if addr.get("is_primary") and ip is not None:
                    primary[addr["family"]] = ip
        extra = sorted(set(existing) - {i["name"] for i in a["interfaces"]} - set(a.get("excluded_interfaces", [])))
        if extra:
            self._review(name, "interface", "name", "interfaces in NetBox not seen on host (not deleted): " + ", ".join(extra),
                         action="verify and remove manually if obsolete")
        self._set_primary(a, parent, primary, vm)

    def _sync_mac(self, a: dict, ci: dict, nb_if: dict, ep: str, otype: str, parent: dict) -> None:
        name, mac = a["key"], ci["mac"]
        found = self.nb.filter("dcim/mac-addresses", mac_address=mac)
        mine = [m for m in found if m.get("assigned_object_type") == otype and m.get("assigned_object_id") == nb_if.get("id")
                and nb_if.get("id") is not None]
        def same_parent(m: dict) -> bool:
            owner = m.get("assigned_object") or {}
            p = owner.get("device") or owner.get("virtual_machine") or {}
            return parent.get("id") is not None and p.get("id") == parent.get("id")
        # a MAC shared by VLAN sub-interfaces / bridges of the same host is normal; another host is a conflict
        others = [m for m in found if m not in mine and m.get("assigned_object_id") is not None and not same_parent(m)]
        if others and not mine:
            self._review(name, "interface", f"{ci['name']}.mac", "MAC already assigned to another interface",
                         {"value": mac, "source": ci["source"], "collected_at": ci["collected_at"], "confidence": "high"},
                         netbox_value=f"{others[0].get('assigned_object_type')}#{others[0].get('assigned_object_id')}")
            return
        mobj = mine[0] if mine else self.w.create("dcim/mac-addresses", {
            "mac_address": mac, "assigned_object_type": otype, "assigned_object_id": nb_if.get("id")}, name, f"{name}:{ci['name']}:{mac}")
        if _empty(nb_if.get("primary_mac_address")):
            self.w.patch(ep, nb_if, {"primary_mac_address": mobj.get("id")}, name, f"{name}:{ci['name']}")

    def _sync_ip(self, a: dict, ci: dict, addr: dict, nb_if: dict, otype: str) -> dict | None:
        name = a["key"]
        iface = ipaddress.ip_interface(addr["address"])
        fvd = {"value": addr["address"], "source": ci["source"], "collected_at": ci["collected_at"], "confidence": "high"}
        found = [x for x in self.nb.filter("ipam/ip-addresses", address=str(iface.ip))
                 if ipaddress.ip_interface(x["address"]).ip == iface.ip]
        if len(found) > 1:
            self._review(name, "ip", ci["name"], f"{len(found)} IP objects for this address (VRFs?)", fvd)
            return None
        if not found:
            data = {"address": addr["address"], "status": "active",
                    "assigned_object_type": otype, "assigned_object_id": nb_if.get("id")}
            tag = self._tag_ref()
            if tag:
                data["tags"] = tag
            return self.w.create("ipam/ip-addresses", data, name, f"{name}:{ci['name']}:{addr['address']}")
        ip = found[0]
        if ipaddress.ip_interface(ip["address"]).network.prefixlen != iface.network.prefixlen:
            self._review(name, "ip", ci["name"], "prefix length differs from NetBox (mask uncertain)", fvd, ip["address"])
            return None
        if ip.get("assigned_object_id") is None:
            return self.w.patch("ipam/ip-addresses", ip, {"assigned_object_type": otype, "assigned_object_id": nb_if.get("id")},
                                name, f"{name}:{addr['address']}")
        if ip.get("assigned_object_type") == otype and ip.get("assigned_object_id") == nb_if.get("id"):
            return ip
        owner = (ip.get("assigned_object") or {})
        self._review(name, "ip", ci["name"], "IP is assigned to another object; not moved", fvd,
                     netbox_value=f"{(owner.get('device') or owner.get('virtual_machine') or {}).get('name', '?')}:{owner.get('name', '?')}")
        return None

    def _set_primary(self, a: dict, parent: dict, primary: dict[int, dict], vm: bool) -> None:
        ep = "virtualization/virtual-machines" if vm else "dcim/devices"
        patch = {}
        for fam, ip in primary.items():
            key = f"primary_ip{fam}"
            have = _id(parent.get(key))
            if have is None:
                patch[key] = ip.get("id")
            elif have != ip.get("id"):
                self._review(a["key"], "primary_ip", key, "primary IP differs; not overwritten",
                             {"value": ip.get("address"), "source": "connected_ip_match", "confidence": "high"},
                             netbox_value=(parent.get(key) or {}).get("address"))
        if patch:
            self.w.patch(ep, parent, patch, a["key"], a["key"])

    # ------------------------------------------------------------ stale report (no delete)
    def stale(self, assets: list[dict]) -> list[dict]:
        seen = {a["key"] for a in assets}
        out: list[dict] = []
        if not self._lookup_ref("extras/tags", self.tag):
            return out
        for ep, kind in (("dcim/devices", "device"), ("virtualization/virtual-machines", "vm")):
            for o in self.nb.filter(ep, tag=self.tag):
                if o["name"] not in seen:
                    out.append({"name": o["name"], "kind": kind, "id": o["id"], "action": "report_only"})
        return out
