"""BMC collectors. Redfish uses HTTP GET only with basic auth (no session POST, no PATCH).
IPMI uses read-only 'ipmitool fru print' / 'lan print' when ipmitool is installed.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

import requests
import requests.adapters
import urllib3
from urllib3.util.retry import Retry

from .common import now_iso

log = logging.getLogger("asset_sync.bmc")


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    """Accept exactly one certificate (SHA-256 of the DER), e.g. a self-signed iLO cert recorded with `bmc-trust`.
    Chain/hostname checks are replaced by the fingerprint check, which still fails on any other certificate."""

    def __init__(self, fingerprint: str, **kw):
        self.fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kw):
        kw["assert_fingerprint"] = self.fingerprint
        super().init_poolmanager(*args, **kw)


def fetch_cert(host: str, port: int = 443, timeout: int = 10) -> dict:
    """One TLS handshake to read the presented certificate (no HTTP request, no credentials)."""
    import hashlib
    import ssl
    import subprocess as sp
    pem = ssl.get_server_certificate((host, port), timeout=timeout)
    der = ssl.PEM_cert_to_DER_cert(pem)
    info = sp.run(["openssl", "x509", "-noout", "-subject", "-issuer", "-enddate"], input=pem, capture_output=True,
                  text=True, timeout=10).stdout.strip().splitlines()
    return {"sha256": hashlib.sha256(der).hexdigest(), "details": info, "fetched_at": now_iso()}


class RedfishReadOnly:
    def __init__(self, host: str, user: str, password: str, verify: bool | str = True, timeout: int = 15,
                 fingerprint: str | None = None):
        self.base = f"https://{host}"
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.verify = verify
        # HPE iLO closes the socket after each response; reusing it fails with RemoteDisconnected
        self.s.headers.update({"Accept": "application/json", "Connection": "close"})
        self.timeout = timeout
        retry = Retry(total=2, connect=2, read=1, backoff_factor=0.5, allowed_methods=frozenset({"GET"}),
                      status_forcelist=(502, 503, 504))
        if fingerprint:
            self.s.verify = False  # CA/hostname check replaced by the exact fingerprint match below
            self.s.mount(self.base, _PinnedAdapter(fingerprint, max_retries=retry))
        else:
            self.s.mount(self.base, requests.adapters.HTTPAdapter(max_retries=retry))
        if self.s.verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def get(self, path: str) -> dict:
        r = self.s.get(self.base + path, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def members(self, path: str) -> list[dict]:
        return [self.get(m["@odata.id"]) for m in self.get(path).get("Members", [])]


def collect_redfish(host: str, user: str, password: str, verify: bool | str = True,
                    fingerprint: str | None = None, timeout: int = 15) -> dict[str, Any]:
    out: dict[str, Any] = {"host": host, "collector": "bmc_redfish", "collected_at": now_iso(),
                           "tls": "pinned" if fingerprint else ("ca" if verify else "insecure")}
    try:
        rf = RedfishReadOnly(host, user, password, verify, timeout=timeout, fingerprint=fingerprint)
        root = rf.get("/redfish/v1/")
        systems = rf.members(root.get("Systems", {}).get("@odata.id", "/redfish/v1/Systems"))
        managers = rf.members(root.get("Managers", {}).get("@odata.id", "/redfish/v1/Managers"))
    except requests.exceptions.SSLError as exc:
        pinned_mismatch = fingerprint and "fingerprint" in str(exc).lower()
        return {**out, "status": "skipped", "reason": "tls_pin_mismatch" if pinned_mismatch else "tls_verify_failed"}
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        reason = {401: "auth_failed", 403: "forbidden", 404: "redfish_not_supported"}.get(code, f"http_{code}")
        return {**out, "status": "skipped", "reason": reason}
    except requests.exceptions.Timeout:
        return {**out, "status": "skipped", "reason": "timeout"}
    except requests.exceptions.ConnectionError as exc:
        txt = str(exc)
        reason = ("timeout" if "timed out" in txt.lower()  # read timeouts surface as ConnectionError after retries
                  else "connection_dropped" if "RemoteDisconnected" in txt or "aborted" in txt
                  else "refused" if "refused" in txt.lower() else "unreachable")
        return {**out, "status": "skipped", "reason": reason}
    except ValueError:
        return {**out, "status": "skipped", "reason": "invalid_json"}

    out["status"] = "ok"
    out["systems"] = [{
        "id": s.get("Id"), "manufacturer": s.get("Manufacturer"), "model": s.get("Model"),
        "serial": s.get("SerialNumber"), "asset_tag": s.get("AssetTag"), "sku": s.get("SKU"),
        "uuid": s.get("UUID"), "hostname": s.get("HostName"), "system_type": s.get("SystemType"),
    } for s in systems]
    mgr_ifaces = []
    for m in managers:
        eth = m.get("EthernetInterfaces", {}).get("@odata.id")
        if not eth:
            continue
        try:
            for i in rf.members(eth):
                mgr_ifaces.append({
                    "manager": m.get("Id"), "id": i.get("Id"), "name": i.get("Name"),
                    "mac": i.get("MACAddress") or i.get("PermanentMACAddress"),
                    "enabled": i.get("InterfaceEnabled"),
                    "ipv4": [{"address": a.get("Address"), "netmask": a.get("SubnetMask"), "origin": a.get("AddressOrigin")}
                             for a in i.get("IPv4Addresses", []) if a.get("Address")],
                })
        except requests.RequestException as exc:
            log.warning("redfish %s: manager ethernet read failed: %s", host, type(exc).__name__)
    out["manager_interfaces"] = mgr_ifaces
    return out


def _ipmi(args: list[str], host: str, user: str, password: str) -> str:
    env = {"IPMI_PASSWORD": password, "PATH": "/usr/bin:/usr/sbin:/bin"}
    cmd = ["ipmitool", "-I", "lanplus", "-H", host, "-U", user, "-E", *args]  # -E: password from env, not argv
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30, check=True).stdout


def _kv(text: str) -> dict[str, str]:
    d = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d.setdefault(k.strip(), v.strip())
    return d


def collect_ipmi(host: str, user: str, password: str) -> dict[str, Any]:
    out: dict[str, Any] = {"host": host, "collector": "bmc_ipmi", "collected_at": now_iso()}
    if not shutil.which("ipmitool"):
        return {**out, "status": "skipped", "reason": "ipmitool_not_installed"}
    try:
        fru = _kv(_ipmi(["fru", "print", "0"], host, user, password))
        lan = _kv(_ipmi(["lan", "print"], host, user, password))
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "").lower()
        return {**out, "status": "skipped", "reason": "auth_failed" if "unauthorized" in err or "password" in err else "ipmi_error"}
    except subprocess.TimeoutExpired:
        return {**out, "status": "skipped", "reason": "unreachable"}
    out.update(status="ok", systems=[{
        "manufacturer": fru.get("Product Manufacturer") or fru.get("Board Mfg"),
        "model": fru.get("Product Name"), "serial": fru.get("Product Serial") or fru.get("Chassis Serial"),
        "asset_tag": fru.get("Product Asset Tag"),
    }], manager_interfaces=[{
        "id": "lan", "name": None, "mac": lan.get("MAC Address"),
        "ipv4": [{"address": lan.get("IP Address"), "netmask": lan.get("Subnet Mask"), "origin": lan.get("IP Address Source")}]
        if lan.get("IP Address") else [],
    }])
    return out
