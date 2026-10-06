"""Generate documentation site fixtures under temp directories.

Layout produced:
  src/**/*.md  assets/**  licenses.json  feedback.json  redirects.json
  migrations.json  dist/{index.html,search-index.json,routes.json,manifest.json}
"""
from __future__ import annotations

import json
from pathlib import Path

# Default candidate = a clean revision of the live site: /old-guide is
# RENAMED to /quickstart (redirect + migration), and every internal link
# already points at an existing page. Tests that need a broken/deleted page
# override pages / redirects / migrations explicitly.
PAGES = {
    "index.md": ("首页", "欢迎\n\n- [安装](/installation)\n- [按钮](/button)\n"),
    "installation.md": ("安装", "安装指南\n\n[快速开始](/quickstart)\n"),
    "quickstart.md": ("快速开始", "快速开始\n\n[按钮](/button)\n"),
    "button.md": ("按钮", "按钮组件\n\n[快速开始](/quickstart)\n"),
}

DEFAULT_REDIRECTS = {"/old-guide": "/quickstart"}
DEFAULT_MIGRATIONS = {"deleted_pages": [
    {"route": "/old-guide", "redirect_to": "/quickstart", "migrated": True}]}

ASSETS = {
    "assets/logo.svg": b"<svg/>",
    "assets/button-demo.png": b"\x89PNG-fake",
}


def make_site(root: str | Path, pages=None, dist_pages=None,
              redirects=None, migrations=None, feedback=None,
              licenses=None, assets=None, page_links=None,
              extra_dist: dict | None = None, first: bool | None = None) -> Path:
    root = Path(root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    pages = PAGES if pages is None else pages
    page_links = page_links or {}
    for name, (title, _default) in pages.items():
        body = page_links.get(name)
        if body is None:
            body = _default
        (root / "src" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "src" / name).write_text(f"# {title}\n\n{body}", encoding="utf-8")

    (root / "assets").mkdir(parents=True, exist_ok=True)
    for path, data in (assets or ASSETS).items():
        fp = root / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(data)

    licenses = licenses if licenses is not None else {
        "assets/logo.svg": {"license": "MIT", "used_by": ["/"]},
        "assets/button-demo.png": {"license": "CC-BY-4.0",
                                    "used_by": ["/button"]},
    }
    (root / "licenses.json").write_text(json.dumps(licenses, ensure_ascii=False))
    (root / "feedback.json").write_text(
        json.dumps(feedback or [], ensure_ascii=False))
    # The default rename redirect/migration applies to revisions of the live
    # site (old-guide GONE). A brand-new deployment has no prior pages.
    if first is None:
        first = set(pages) == set(baseline_live_pages())
    if redirects is None:
        redirects = {} if first else dict(DEFAULT_REDIRECTS)
    if migrations is None:
        migrations = {"deleted_pages": []} if first else DEFAULT_MIGRATIONS
    (root / "redirects.json").write_text(
        json.dumps(redirects, ensure_ascii=False))
    (root / "migrations.json").write_text(
        json.dumps(migrations, ensure_ascii=False))

    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    if dist_pages is None:
        dist_pages = sorted("/" + n[:-3].replace("index", "").strip("/")
                            for n in pages)
        dist_pages = ["/" if r in ("/", "") else r for r in dist_pages]
        # routes table also serves redirect source paths
        dist_pages = sorted(set(dist_pages) | set(redirects.keys()))
    (dist / "routes.json").write_text(json.dumps(
        {r: f"assets/{r.strip('/') or 'index'}.html" for r in dist_pages},
        ensure_ascii=False))
    (dist / "search-index.json").write_text(json.dumps(
        {"routes": dist_pages}, ensure_ascii=False))
    (dist / "index.html").write_text("<html>site</html>")
    for name, data in (extra_dist or {}).items():
        (dist / name).write_bytes(data)
    return root


def baseline_live_pages():
    """Page set for an earlier, already-live generation."""
    return {
        "index.md": ("首页", "欢迎\n"),
        "installation.md": ("安装", "安装\n"),
        "old-guide.md": ("旧指南", "旧的上手指南\n"),
    }
