"""Candidate manifest: build, validate and normalize the single candidate
manifest that every check in a run reads (no per-check divergent inputs)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .fingerprint import canonical, file_sha256, sha256_hex

REQUIRED = ["release_id", "schema", "pages", "redirects", "assets",
            "feedback", "build", "migrations"]


def load_manifest(path: str | Path) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    m = json.loads(raw.decode("utf-8"))
    return validate(m)


def save_manifest(manifest: dict, path: str | Path) -> None:
    Path(path).write_bytes(canonical(manifest))


def build_manifest(site_dir: str | Path, release_id: str) -> dict:
    """Assemble a candidate manifest from a laid-out site fixture directory:
      <site>/src/**/*.md
      <site>/licenses.json            -> {asset_path: {license, revoked}}
      <site>/feedback.json
      <site>/dist/**                  build artifacts (hash recorded)
      <site>/dist/routes.json         {"/route":"file", ...}
      <site>/dist/search-index.json   {"routes": [...]}
      <site>/redirects.json           {old: new}
      <site>/migrations.json          {deleted_pages: [{route,redirect_to,migrated}]}
    """
    site = Path(site_dir)
    pages = []
    for md in sorted((site / "src").rglob("*.md")):
        rel = md.relative_to(site / "src").as_posix()
        route = "/" + rel[:-3].replace("index", "").rstrip("/")
        if not route.startswith("/"):
            route = "/" + route
        route = route if route else "/"
        pages.append({
            "route": _norm(route),
            "source": rel,
            "title": md.stem.replace("-", " ").title(),
            "content_sha": file_sha256(md),
        })

    lic = json.loads((site / "licenses.json").read_text()) if (site / "licenses.json").exists() else {}
    assets = []
    for ap in sorted((site / "assets").rglob("*")) if (site / "assets").exists() else []:
        if ap.is_file():
            rel = ap.relative_to(site).as_posix()
            meta = lic.get(rel, {})
            assets.append({
                "path": rel,
                "sha256": file_sha256(ap),
                "license": meta.get("license", "unknown"),
                "revoked": bool(meta.get("revoked", False)),
                "used_by": sorted(meta.get("used_by", [])),
            })

    feedback = json.loads((site / "feedback.json").read_text()) if (site / "feedback.json").exists() else []

    dist = site / "dist"
    artifacts = []
    if dist.exists():
        for p in sorted(dist.rglob("*")):
            if p.is_file():
                artifacts.append({"path": p.relative_to(site).as_posix(),
                                  "sha256": file_sha256(p)})

    def read(name, default):
        fp = site / name
        return json.loads(fp.read_text()) if fp.exists() else default

    routes_table = read("dist/routes.json", {})
    search = read("dist/search-index.json", {"routes": []})
    redirects = read("redirects.json", {})
    migrations = read("migrations.json", {"deleted_pages": []})

    manifest = {
        "schema": "docrelease/manifest.v1",
        "release_id": release_id,
        "pages": pages,
        "redirects": {_norm(k): _norm(v) for k, v in redirects.items()},
        "assets": assets,
        "feedback": feedback,
        "build": {
            "artifacts": artifacts,
            "search_index": {
                "path": "dist/search-index.json",
                "sha256": _artifact_sha(site, "dist/search-index.json"),
                "routes": sorted(search.get("routes", [])),
            },
            "routes_table": {
                "path": "dist/routes.json",
                "sha256": _artifact_sha(site, "dist/routes.json"),
                "routes": sorted(routes_table.keys()),
            },
        },
        "migrations": migrations,
    }
    return validate(manifest)


def _artifact_sha(site: Path, rel: str) -> str | None:
    p = site / rel
    return file_sha256(p) if p.exists() else None


def _norm(route: str) -> str:
    if not route.startswith("/"):
        route = "/" + route
    if len(route) > 1 and route.endswith("/"):
        route = route.rstrip("/")
    return route


def validate(m: dict) -> dict:
    for key in REQUIRED:
        if key not in m:
            raise ValueError(f"manifest missing key: {key}")
    if m["schema"] != "docrelease/manifest.v1":
        raise ValueError("unsupported manifest schema")
    routes = [p["route"] for p in m["pages"]]
    if len(routes) != len(set(routes)):
        raise ValueError("duplicate page routes in manifest")
    for p in m["pages"]:
        for k in ("route", "source", "content_sha"):
            if not p.get(k):
                raise ValueError(f"page missing {k}")
    m["release_id"] = str(m["release_id"])
    return m


def manifest_sha(manifest: dict) -> str:
    return sha256_hex(canonical(manifest))
