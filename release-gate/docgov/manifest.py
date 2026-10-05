"""Candidate manifest model for a documentation release.

A candidate manifest describes, content-addressed, everything a release is
made of:

* pages      - rendered markdown/html source blobs
* assets     - images/demos/fonts with licence status
* redirects  - legacy path -> current path migration rules
* artifacts  - deterministic build outputs (path -> sha256 of content)
* index      - search index built over the pages
* routes     - public route table (path -> artifact path)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .canonical import encode, fingerprint, sha256

LINK_RE = re.compile(r"\]\((?!https?://)([^)#?]+)")
REF_RE = re.compile(r"""(?:src|href)=["'](?!https?://|/static/)([^"']+)["']""")


class ManifestError(ValueError):
    pass


def normalise_link(p: str) -> str:
    if p.startswith("/"):
        return p[1:]
    return p


def scan_links(page_blob: str) -> list[str]:
    links = set(re.findall(LINK_RE, page_blob))
    links.update(re.findall(REF_RE, page_blob))
    return sorted(normalise_link(l) for l in links if not l.startswith("mailto:"))


def _subset(m: dict[str, Any], key: str) -> Any:
    if key not in m:
        raise ManifestError(f"manifest missing section: {key}")
    return m[key]


# ---- subset fingerprints used as "dependency inputs" by individual checks --

def fp_pages(m: dict[str, Any]) -> str:
    pages = [
        {"path": p["path"], "title": p.get("title", ""), "blob": p["blob"]}
        for p in _subset(m, "pages")
    ]
    return fingerprint(sorted(pages, key=lambda p: p["path"]))


def fp_assets(m: dict[str, Any]) -> str:
    assets = [
        {"path": a["path"], "sha256": a["sha256"], "licence": a.get("licence", {})}
        for a in _subset(m, "assets")
    ]
    return fingerprint(sorted(assets, key=lambda a: a["path"]))


def fp_redirects(m: dict[str, Any]) -> str:
    reds = sorted(
        [{"from": r["from"], "to": r["to"]} for r in _subset(m, "redirects")],
        key=lambda r: r["from"],
    )
    return fingerprint(reds)


def fp_artifacts(m: dict[str, Any]) -> str:
    return fingerprint(sorted(dict(_subset(m, "artifacts")).items()))


def fp_index(m: dict[str, Any]) -> str:
    idx = _subset(m, "index")
    return fingerprint({"checksum": idx.get("checksum"), "entries": len(idx.get("doc_ids", []))})


def fp_index_full(m: dict[str, Any]) -> str:
    return fingerprint(_subset(m, "index"))


def fp_routes(m: dict[str, Any]) -> str:
    return fingerprint(sorted(dict(_subset(m, "routes")).items()))


def validate_manifest(m: dict[str, Any]) -> None:
    """Structural self-consistency validation."""
    required = {"manifest_version", "release_id", "version",
                "pages", "assets", "redirects", "artifacts", "index", "routes"}
    missing = required - set(m)
    if missing:
        raise ManifestError(f"missing fields: {sorted(missing)}")
    if m["manifest_version"] != 1:
        raise ManifestError("unsupported manifest_version")
    for section in ("pages", "assets", "redirects"):
        if not isinstance(m[section], list):
            raise ManifestError(f"{section} must be a list")
    for section in ("artifacts", "routes"):
        if not isinstance(m[section], dict):
            raise ManifestError(f"{section} must be an object")

    page_paths, seen = set(), set()
    for p in m["pages"]:
        for k in ("path", "blob"):
            if k not in p:
                raise ManifestError(f"page missing {k}")
        if p["path"] in seen:
            raise ManifestError(f"duplicate page path {p['path']}")
        seen.add(p["path"])
        page_paths.add(p["path"])
        if p.get("sha256") and p["sha256"] != sha256(p["blob"].encode("utf-8")):
            raise ManifestError(f"page checksum mismatch: {p['path']}")

    seen = set()
    for a in m["assets"]:
        for k in ("path", "sha256", "licence"):
            if k not in a:
                raise ManifestError(f"asset missing {k}: {a.get('path')}")
        if a["path"] in seen:
            raise ManifestError(f"duplicate asset path {a['path']}")
        seen.add(a["path"])

    for r in m["redirects"]:
        if "from" not in r or "to" not in r:
            raise ManifestError("redirect requires from/to")

    idx = m["index"]
    if "checksum" not in idx or "doc_ids" not in idx:
        raise ManifestError("index requires checksum and doc_ids")


# ---- deterministic "build" of a candidate ----------------------------------

def artifact_for_page(path: str) -> str:
    if path.endswith(".md"):
        return path[:-3] + ".html"
    return path + ".html"


def render_artifact(page: dict[str, Any]) -> bytes:
    """Deterministic pseudo-build: title + body bytes.

    Real systems run VitePress here; the gate only requires that builds are
    reproducible, so this stand-in is intentionally pure.
    """
    title = page.get("title", "")
    return encode({"title": title, "body": page["blob"]})


def build_artifacts(pages: list[dict[str, Any]]) -> dict[str, bytes]:
    return {artifact_for_page(p["path"]): render_artifact(p) for p in pages}


@dataclass
class PreparedCandidate:
    manifest: dict[str, Any]
    blobs: dict[str, bytes]  # content-addressed store: sha256 -> bytes
    mf: str                  # manifest fingerprint

    def fp(self) -> str:
        return self.mf


def prepare_candidate(m: dict[str, Any], asset_blobs: dict[str, dict[str, Any]] | None = None) -> PreparedCandidate:
    """Validate and content-address every blob, return sealed candidate."""
    validate_manifest(m)
    blobs: dict[str, bytes] = {}

    for p in m["pages"]:
        body = p["blob"].encode("utf-8")
        p["sha256"] = sha256(body)
        blobs[p["sha256"]] = body

    provided = asset_blobs or {}
    for a in m["assets"]:
        if a["path"] in provided:
            data = provided[a["path"]]
            body = data["blob"]
            declared_lic = data.get("licence", a.get("licence", {}))
        else:
            body = f"asset:{a['path']}".encode("utf-8")
            declared_lic = a.get("licence", {})
        a["sha256"] = sha256(body)
        a["licence"] = declared_lic
        blobs[a["sha256"]] = body

    # artifacts must be declared with the checksum of deterministic outputs
    for path, want in sorted(m["artifacts"].items()):
        page = next((p for p in m["pages"] if artifact_for_page(p["path"]) == path), None)
        if page is None:
            raise ManifestError(f"artifact has no source page: {path}")
        got = sha256(render_artifact(page))
        if want != got:
            raise ManifestError(f"artifact checksum mismatch: {path}")
        blobs[want] = render_artifact(page)

    # index checksum is over canonicalised doc_ids order
    idx = m["index"]
    ids = sorted(idx["doc_ids"])
    idx["doc_ids"] = ids
    idx["checksum"] = sha256(encode(ids))

    # routes must point at declared artifacts
    for route, art in m["routes"].items():
        if art not in m["artifacts"]:
            raise ManifestError(f"route {route} points to missing artifact {art}")

    return PreparedCandidate(manifest=m, blobs=blobs, mf=fingerprint(m))
