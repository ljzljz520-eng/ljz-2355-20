"""Content fingerprints: canonical JSON, tree checksums, HMAC-signed manifests."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


def canonical(obj: Any) -> bytes:
    """Deterministic JSON serialization (sorted keys, compact, stable separators)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fprint(namespace: str, payload: Any) -> str:
    """A namespaced fingerprint over an arbitrary JSON-able payload."""
    return sha256_hex(namespace.encode() + b"|" + canonical(payload))


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_checksum(root: str | Path) -> str:
    """Checksum of an entire directory tree (relative path + content)."""
    root = Path(root)
    entries: list[tuple[str, str]] = []
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                entries.append((rel, file_sha256(p)))
    return fprint("tree", entries)


def sign_manifest(manifest: dict, secret: bytes) -> str:
    return hmac.new(secret, canonical(manifest), hashlib.sha256).hexdigest()


def verify_manifest(manifest: dict, signature: str, secret: bytes) -> bool:
    expect = sign_manifest(manifest, secret)
    return hmac.compare_digest(expect, signature or "")
