"""Canonical JSON, fingerprints, HMAC signatures.

All fingerprints are SHA-256 over a canonical, deterministic, UTF-8 JSON
encoding (sorted keys, no insignificant whitespace).  Signatures are
HMAC-SHA-256 and are hex-encoded, prefixed with the algorithm tag.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

ALG = "HS256"


def encode(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(obj: Any) -> str:
    return sha256(encode(obj))


def sign(obj: Any, key: str) -> str:
    """Deterministic signature over the canonical encoding of obj."""
    mac = hmac.new(key.encode("utf-8"), encode(obj), hashlib.sha256)
    return f"{ALG}.{mac.hexdigest()}"


def verify(obj: Any, signature: str, key: str) -> bool:
    if not signature or "." not in signature:
        return False
    alg, _, _ = signature.partition(".")
    if alg != ALG:
        return False
    return hmac.compare_digest(signature, sign(obj, key))


def b64id(n: int = 16) -> str:
    import secrets

    return secrets.token_hex(n)
