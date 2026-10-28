#!/usr/bin/env python3
"""가짜 Redfish BMC 서버 (데모 전용).

컨테이너 하나가 scenario.json의 BMC 여러 대를 흉내 냅니다.
- BMC IP마다 eth0에 /32 주소를 추가하고, 접속한 목적지 IP로 어떤 BMC인지 구분합니다.
- BMC마다 자체 서명 인증서를 따로 만들어 실제 장비처럼 인증서 고정(pinning)을 시연할 수 있습니다.
- 실제 운영에서 겪은 동작을 재현합니다:
  ilo         응답 후 예고 없이 연결을 끊음 (keep-alive 재사용 시 RemoteDisconnected)
  ami (hgx)   GPU 베이스보드가 별도 System으로 노출됨
  slow        /Systems 응답이 클라이언트 timeout보다 늦음
  auth_fail   이 BMC만 다른 계정을 사용 (401)
  unreachable 주소를 추가하지 않음 (도달 불가)
GET만 지원합니다. 다른 메서드는 405를 돌려줍니다.
"""
from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import os
import socketserver
import ssl
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

USER = os.environ.get("MOCK_BMC_USER", "demo")
PASSWORD = os.environ.get("MOCK_BMC_PASSWORD", "demo-password")
SLOW_SECONDS = float(os.environ.get("MOCK_BMC_SLOW_SECONDS", "20"))
SCENARIO = Path(os.environ.get("MOCK_BMC_SCENARIO", "/app/scenario.json"))
IFACE = os.environ.get("MOCK_BMC_IFACE", "eth0")


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def make_context(bmc: dict, workdir: Path) -> ssl.SSLContext:
    flavor = bmc.get("bmc_flavor", "ilo")
    sys0 = next((s for s in bmc["systems"] if s.get("SerialNumber")), {})
    cn = {"ilo": f"ILO{sys0.get('SerialNumber', 'DEMO')}", "idrac": "idrac-demo", "ami": "AMI MegaRAC (demo)"}[flavor]
    org = {"ilo": "Hewlett Packard Enterprise (demo)", "idrac": "Dell Inc. (demo)", "ami": "American Megatrends (demo)"}[flavor]
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.ORGANIZATION_NAME, org)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(bmc["ip"]))]), critical=False)
            .sign(key, hashes.SHA256()))
    crt, k = workdir / f"{bmc['ip']}.crt", workdir / f"{bmc['ip']}.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, k)
    return ctx


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # advertises keep-alive; the "ilo" flavor then drops the socket anyway
    server_version = "MockRedfish/1.0"

    def log_message(self, fmt, *args):  # quiet default logging; one line per request below
        pass

    @property
    def bmc(self) -> dict:
        return self.server.bmcs[self.connection.getsockname()[0]]

    def _send(self, code: int, body: dict | None = None) -> None:
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if code == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="Redfish"')
        self.end_headers()
        self.wfile.write(data)
        if self.bmc.get("bmc_flavor") == "ilo":
            self.close_connection = True  # like iLO: close without announcing it

    def _authorized(self) -> bool:
        if self.bmc["scenario"] == "auth_fail":
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except ValueError:
            return False
        return user == USER and pw == PASSWORD

    def do_GET(self):  # noqa: N802
        b, path = self.bmc, self.path.split("?")[0].rstrip("/")
        log(f"{b['ip']:<16} GET {path}")
        if path == "/redfish/v1":
            return self._send(200, {"@odata.id": "/redfish/v1", "RedfishVersion": "1.15.0", "Name": "Mock Redfish (demo)",
                                    "Systems": {"@odata.id": "/redfish/v1/Systems"},
                                    "Managers": {"@odata.id": "/redfish/v1/Managers"}})
        if not self._authorized():
            return self._send(401, {"error": {"message": "Authentication required"}})
        if path == "/redfish/v1/Systems":
            if b["scenario"] == "slow":
                time.sleep(SLOW_SECONDS)
            return self._send(200, {"Members": [{"@odata.id": f"/redfish/v1/Systems/{s['Id']}"} for s in b["systems"]]})
        for s in b["systems"]:
            if path == f"/redfish/v1/Systems/{s['Id']}":
                return self._send(200, {"@odata.id": path, "Id": s["Id"], "SystemType": "Physical",
                                        **{k: v for k, v in s.items() if k != "Id"}})
        mid = b["manager"]["Id"]
        if path == "/redfish/v1/Managers":
            return self._send(200, {"Members": [{"@odata.id": f"/redfish/v1/Managers/{mid}"}]})
        if path == f"/redfish/v1/Managers/{mid}":
            return self._send(200, {"Id": mid, "EthernetInterfaces": {"@odata.id": f"{path}/EthernetInterfaces"}})
        if path == f"/redfish/v1/Managers/{mid}/EthernetInterfaces":
            return self._send(200, {"Members": [{"@odata.id": f"{path}/1"}]})
        if path == f"/redfish/v1/Managers/{mid}/EthernetInterfaces/1":
            return self._send(200, {"Id": "1", "Name": "Manager Dedicated Network Interface", "InterfaceEnabled": True,
                                    "MACAddress": b["manager"]["MACAddress"],
                                    "IPv4Addresses": [{"Address": b["ip"], "SubnetMask": "255.255.255.0",
                                                       "AddressOrigin": "Static"}]})
        return self._send(404, {"error": {"message": f"not found: {path}"}})

    def _not_allowed(self):
        self._send(405, {"error": {"message": "read-only mock: only GET is supported"}})

    do_POST = do_PATCH = do_PUT = do_DELETE = _not_allowed  # noqa: N815


class TLSPerIPServer(socketserver.ThreadingTCPServer):
    """Accepts on 0.0.0.0:443 and picks the certificate by the destination IP of each connection."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, bmcs: dict, contexts: dict):
        self.bmcs, self.contexts = bmcs, contexts
        super().__init__(addr, Handler)

    def finish_request(self, request, client_address):
        ctx = self.contexts.get(request.getsockname()[0])
        if not ctx:
            request.close()
            return
        try:
            tls = ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # e.g. a bare TLS probe that only reads the certificate
        self.RequestHandlerClass(tls, client_address, self)


def main() -> int:
    bmcs = {b["ip"]: b for b in json.loads(SCENARIO.read_text())["bmcs"]}
    work = Path(tempfile.mkdtemp(prefix="mockbmc-"))
    contexts = {}
    for ip, b in bmcs.items():
        if b["scenario"] == "unreachable":
            log(f"{ip:<16} {b['host']:<14} unreachable (address not added)")
            continue
        subprocess.run(["ip", "addr", "add", f"{ip}/32", "dev", IFACE], check=False, capture_output=True)
        contexts[ip] = make_context(b, work)
        log(f"{ip:<16} {b['host']:<14} {b.get('bmc_flavor'):<6} {b['scenario']}")
    srv = TLSPerIPServer(("0.0.0.0", 443), bmcs, contexts)
    log(f"mock Redfish listening on :443 for {len(contexts)} BMC addresses (user={USER})")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
