"""Shared helpers: config loading, timestamps, secret masking, safe file output."""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Values that firmware/OS report when the real value is unknown. They must never be
# stored as if they were real identifiers.
PLACEHOLDER_VALUES = {
    "", "none", "null", "n/a", "na", "unknown", "not specified", "not available",
    "not applicable", "to be filled by o.e.m.", "to be filled by oem", "default string",
    "system serial number", "chassis serial number", "system manufacturer",
    "system product name", "o.e.m.", "oem", "0", "00000000", "0000000000", "0123456789",
    "123456789", "1234567890", "xxxxxxxxxx", "invalid", "empty", "none specified",
    "base board serial number", "serial number", "asset tag", "no asset tag",
    "asset-1234567890", "default", "-",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    s = str(value).strip().lower()
    if s in PLACEHOLDER_VALUES:
        return True
    # strings made only of one repeated char ("0000", "FFFF", "....")
    return len(s) > 0 and len(set(s.replace(" ", ""))) <= 1


def clean(value: Any) -> str | None:
    """Return a trimmed value, or None if it is a known placeholder."""
    if is_placeholder(value):
        return None
    return str(value).strip()


def mask(value: Any, keep: int = 2) -> str:
    """Mask identifiers (serial, asset tag, tokens) for logs and reports."""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep * 2) + s[-keep:]


_SECRET_PATTERNS = [
    re.compile(r"(?i)(token|password|passwd|secret|authorization)(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',]+)"),
    re.compile(r"\bnbt_[A-Za-z0-9._-]+"),
    re.compile(r"(?i)\b(Token|Bearer)\s+[A-Za-z0-9._-]{16,}"),
]


def redact(text: str) -> str:
    out = text
    out = _SECRET_PATTERNS[0].sub(lambda m: m.group(1) + m.group(2) + "***", out)
    out = _SECRET_PATTERNS[1].sub("nbt_***", out)
    out = _SECRET_PATTERNS[2].sub(lambda m: m.group(1) + " ***", out)
    return out


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbose: bool = False) -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # urllib3 debug logs can contain headers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger("asset_sync")


def load_yaml(path: Path | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return default if data is None else data


def load_settings(path: Path | str | None = None) -> dict:
    p = Path(path) if path else PROJECT_ROOT / "config" / "settings.yml"
    data = load_yaml(p, {})
    if not data:
        raise SystemExit(f"settings file not found or empty: {p}")
    return data


def _private_open(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline="")


def write_json(path: Path | str, data: Any) -> Path:
    p = Path(path)
    with _private_open(p) as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False, default=str)
        fh.write("\n")
    return p


def write_csv(path: Path | str, rows: Iterable[dict], fields: list[str]) -> Path:
    p = Path(path)
    with _private_open(p) as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("; ".join(map(str, v)) if isinstance(v, (list, tuple)) else v)
                        for k, v in r.items()})
    return p


def write_yaml(path: Path | str, data: Any) -> Path:
    p = Path(path)
    with _private_open(p) as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    return p


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", str(text).strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")[:100] or "unknown"
