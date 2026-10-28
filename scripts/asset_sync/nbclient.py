"""Minimal NetBox REST client with a dry-run write gate.

Reads always hit the API. Writes go through `Writer`, which records the operation and only calls the API
when apply=True. DELETE is intentionally not implemented.
"""
from __future__ import annotations

import logging

import requests

from .common import redact

log = logging.getLogger("asset_sync.netbox")


class NetBoxError(RuntimeError):
    pass


class NetBox:
    def __init__(self, url: str, token: str, verify: bool | str = True, timeout: int = 30):
        if not url or not token:
            raise NetBoxError("NETBOX_URL and token are required (see .env.example)")
        self.api = url.rstrip("/") + "/api"
        self.s = requests.Session()
        token = token.strip()
        for prefix in ("Bearer ", "Token "):  # tolerate a pasted "Bearer nbt_..." value
            if token.startswith(prefix):
                token = token[len(prefix):].strip()
        scheme = "Bearer" if token.startswith("nbt_") else "Token"  # v2 tokens (NetBox >= 4.5) use Bearer
        self.s.headers.update({"Authorization": f"{scheme} {token}", "Accept": "application/json",
                               "Content-Type": "application/json"})
        self.s.verify = verify
        self.timeout = timeout
        self._version: tuple[int, ...] | None = None

    def _req(self, method: str, path: str, **kw) -> dict:
        url = path if path.startswith("http") else f"{self.api}/{path.strip('/')}/"
        try:
            r = self.s.request(method, url, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise NetBoxError(f"{method} {path}: cannot reach NetBox ({type(exc).__name__})") from None
        if r.status_code >= 400:
            raise NetBoxError(f"{method} {path} -> {r.status_code}: {redact(r.text[:500])}")
        return r.json() if r.content else {}

    @property
    def version(self) -> tuple[int, ...]:
        if self._version is None:
            v = self._req("GET", "status").get("netbox-version", "0.0")
            self._version = tuple(int(x) for x in v.split("-")[0].split(".")[:3] if x.isdigit())
        return self._version

    def status(self) -> dict:
        return self._req("GET", "status")

    def filter(self, endpoint: str, **params) -> list[dict]:
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("limit", 500)
        out, data = [], self._req("GET", endpoint, params=params)
        out += data.get("results", [])
        while data.get("next"):
            data = self._req("GET", data["next"])
            out += data.get("results", [])
        return out

    def create(self, endpoint: str, data: dict) -> dict:
        return self._req("POST", endpoint, json=data)

    def patch(self, endpoint: str, obj_id: int, data: dict) -> dict:
        return self._req("PATCH", f"{endpoint.strip('/')}/{obj_id}", json=data)


class Writer:
    """Records planned writes; performs them only when apply=True."""

    def __init__(self, nb, apply: bool = False):
        self.nb, self.apply = nb, apply
        self.ops: list[dict] = []

    def create(self, endpoint: str, data: dict, asset: str, label: str) -> dict:
        self.ops.append({"asset": asset, "action": "create", "endpoint": endpoint, "object": label,
                         "fields": sorted(data)})
        if not self.apply:
            return {"id": None, "_planned": True, **data}
        obj = self.nb.create(endpoint, data)
        log.info("created %s %s (id=%s)", endpoint, label, obj.get("id"))
        return obj

    def patch(self, endpoint: str, obj: dict, data: dict, asset: str, label: str) -> dict:
        self.ops.append({"asset": asset, "action": "update", "endpoint": endpoint, "object": label,
                         "id": obj.get("id"), "fields": sorted(data)})
        if not self.apply or obj.get("id") is None:
            return {**obj, **data}
        res = self.nb.patch(endpoint, obj["id"], data)
        log.info("updated %s %s (id=%s) fields=%s", endpoint, label, obj["id"], sorted(data))
        return res
