"""In-memory stand-in for the subset of the NetBox REST API used by asset_sync.sync."""
from __future__ import annotations

import copy
import ipaddress
import itertools

NESTED = {  # field -> endpoint it references (returned nested like the real API)
    "site": "dcim/sites", "role": "dcim/device-roles", "device_type": "dcim/device-types",
    "platform": "dcim/platforms", "manufacturer": "dcim/manufacturers", "cluster": "virtualization/clusters",
    "device": "dcim/devices", "virtual_machine": "virtualization/virtual-machines",
    "primary_ip4": "ipam/ip-addresses", "primary_ip6": "ipam/ip-addresses",
    "primary_mac_address": "dcim/mac-addresses",
}
OTYPE_EP = {"dcim.interface": "dcim/interfaces", "virtualization.vminterface": "virtualization/interfaces"}


class FakeNetBox:
    def __init__(self, version=(4, 7, 0)):
        self.version = version
        self.db: dict[str, dict[int, dict]] = {}
        self._ids = itertools.count(1)
        self.calls: list[tuple] = []

    # ---- seeding helpers
    def add(self, endpoint, **data):
        obj = {"id": next(self._ids), "custom_fields": {}, "tags": [], **data}
        self.db.setdefault(endpoint, {})[obj["id"]] = obj
        return obj

    # ---- API surface
    def _render(self, endpoint, obj):
        out = copy.deepcopy(obj)
        for k, ep in NESTED.items():
            if k in out and isinstance(out[k], int):
                ref = self.db.get(ep, {}).get(out[k], {"id": out[k]})
                out[k] = {x: ref.get(x) for x in ("id", "name", "slug", "address", "model", "site") if x in ref}
        if out.get("assigned_object_type") in OTYPE_EP and out.get("assigned_object_id"):
            ifc = self.db.get(OTYPE_EP[out["assigned_object_type"]], {}).get(out["assigned_object_id"], {})
            parent_key = "device" if "device" in ifc else "virtual_machine"
            parent_ep = NESTED[parent_key]
            par = self.db.get(parent_ep, {}).get(ifc.get(parent_key), {})
            out["assigned_object"] = {"id": ifc.get("id"), "name": ifc.get("name"),
                                      parent_key: {"id": par.get("id"), "name": par.get("name")}}
        out["tags"] = [{"slug": t["slug"] if isinstance(t, dict) else t} for t in out.get("tags", [])]
        return out

    def filter(self, endpoint, **flt):
        self.calls.append(("GET", endpoint, flt))
        flt.pop("limit", None)
        res = []
        for obj in self.db.get(endpoint, {}).values():
            if all(self._match(obj, k, v) for k, v in flt.items()):
                res.append(self._render(endpoint, obj))
        return res

    @staticmethod
    def _match(obj, k, v):
        if k == "address":
            return ipaddress.ip_interface(obj["address"]).ip == ipaddress.ip_address(v)
        if k == "tag":
            return v in [t["slug"] if isinstance(t, dict) else t for t in obj.get("tags", [])]
        if k.endswith("_id"):
            return obj.get(k[:-3]) == v
        if k in ("name", "serial", "mac_address", "model"):
            return str(obj.get(k) or "").lower() == str(v).lower()
        return obj.get(k) == v

    def create(self, endpoint, data):
        self.calls.append(("POST", endpoint, data))
        data = copy.deepcopy(data)
        if "tags" in data:
            data["tags"] = [t["slug"] for t in data["tags"]]
        obj = self.add(endpoint, **data)
        return self._render(endpoint, obj)

    def patch(self, endpoint, obj_id, data):
        self.calls.append(("PATCH", endpoint, obj_id, data))
        obj = self.db[endpoint.strip("/")][obj_id]
        data = copy.deepcopy(data)
        if "tags" in data:
            data["tags"] = [t["slug"] for t in data["tags"]]
        if "custom_fields" in data:
            obj["custom_fields"] = {**obj.get("custom_fields", {}), **data.pop("custom_fields")}
        obj.update(data)
        return self._render(endpoint, obj)

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]
