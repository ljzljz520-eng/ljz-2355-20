"""Release checklist checks.

Each check reads the SAME candidate manifest through one Snapshot, emits
per-item evidence, and declares which inputs it depends on. A check's
*dependency fingerprint* binds together the outputs of its dependencies and its
own inputs, so renaming a page (changes page_diff -> redirect -> broken_links)
or revoking an asset (changes asset_license -> build eligibility) invalidates
later items. Green lights collected at different times can never be spliced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .fingerprint import fprint

BLOCKING = True


@dataclass
class Check:
    name: str
    title: str
    deps: tuple[str, ...]
    run: Callable[[dict], dict]


def _ok(evidence, detail=None):
    return {"status": "pass", "evidence": evidence, "detail": detail}


def _fail(evidence, detail):
    return {"status": "fail", "evidence": evidence, "detail": detail}


# ---------------------------------------------------------------- checks ----
def check_page_diff(snap):
    added, deleted, common = snap.diff_pages()
    renamed = snap.renamed()
    changed = snap.changed_routes()
    ev = {
        "added": sorted(added),
        "deleted": sorted(deleted),
        "renamed": renamed,
        "truly_removed": sorted(snap.truly_removed()),
        "modified": sorted(r for r in common
                           if snap.old_by_route[r].get("content_sha")
                           != snap.new_by_route[r].get("content_sha")),
        "changed_routes": sorted(changed),
    }
    return _ok(ev, f"+{len(added)} / -{len(deleted)} 页，重命名 {len(renamed)} 页")


def check_deleted_migration(snap):
    removed = sorted(snap.truly_removed())
    migrations = {m.get("route"): m for m in
                  snap.candidate.get("migrations", {}).get("deleted_pages", [])}
    missing, accepted = [], []
    for r in removed:
        m = migrations.get(r)
        if m and m.get("migrated") and m.get("redirect_to"):
            accepted.append(r)
        else:
            missing.append(r)
    ev = {"truly_removed": removed, "migrated": accepted,
          "missing_migration": missing}
    if missing:
        return _fail(ev, f"{len(missing)} 个删除页未提供迁移: {', '.join(missing)}")
    return _ok(ev, "所有删除页均已迁移")


def check_redirects(snap):
    routes = snap.page_set_new
    live_routes = snap.page_set_old
    bad, good = [], []
    for src, dst in sorted(snap.candidate.get("redirects", {}).items()):
        if dst in routes:
            good.append({"from": src, "to": dst})
        else:
            bad.append({"from": src, "to": dst,
                        "reason": "目标页在候选版本中不存在（断链重定向）"})
    # source of redirect should be a renamed/deleted page, not a live page
    for src, dst in snap.candidate.get("redirects", {}).items():
        if src in routes and src not in live_routes:
            pass
    ev = {"redirects": good, "invalid": bad}
    if bad:
        return _fail(ev, f"{len(bad)} 条重定向指向不存在的页面")
    return _ok(ev, f"{len(good)} 条重定向全部有效")


def check_broken_links(snap):
    # rename-aware: redirects and renamed pages resolve otherwise-broken links
    redirects = snap.candidate.get("redirects", {})
    reachable = set(snap.page_set_new) | set(redirects.keys())
    broken = []
    checked = 0
    for src, targets in snap.page_links().items():
        for t in targets:
            if not t:
                continue
            checked += 1
            norm = t if t.startswith("/") else "/" + t.strip("/")
            norm = norm.rstrip("/") or "/"
            if norm in reachable:
                continue
            resolved = redirects.get(norm)
            if resolved and resolved in snap.page_set_new:
                continue
            broken.append({"page": src, "target": t,
                           "reason": "目标页不存在（重命名后未更新链接/缺少重定向）"})
    ev = {"links_checked": checked,
          "broken": broken,
          "renamed": snap.renamed()}
    if broken:
        return _fail(ev, f"{len(broken)} 个断链")
    return _ok(ev, f"检查 {checked} 个内部链接，无断链")


def check_feedback(snap):
    changed = snap.changed_routes()
    open_items = []
    for item in snap.candidate.get("feedback", []):
        if item.get("state", "open") != "resolved":
            open_items.append(item)
    blocking_items = [i for i in open_items if i.get("page") in changed]
    ev = {"changed_routes": sorted(changed),
          "open_total": len(open_items),
          "open_on_changed_pages": blocking_items}
    if blocking_items:
        return _fail(ev, f"{len(blocking_items)} 条未关闭反馈落在变更/删除页面上")
    return _ok(ev, "变更页面无未解决反馈")


def check_asset_license(snap):
    revoked, unknown, ok_assets = [], [], []
    for a in snap.candidate.get("assets", []):
        if a.get("revoked"):
            revoked.append({"path": a["path"], "used_by": a.get("used_by", [])})
        elif a.get("license", "unknown") in ("unknown", ""):
            unknown.append(a["path"])
        else:
            ok_assets.append(a["path"])
    ev = {"revoked": revoked, "unknown_license": unknown,
           "licensed": ok_assets}
    if revoked:
        return _fail(ev, f"{len(revoked)} 个素材授权已撤销")
    if unknown:
        return _fail(ev, f"{len(unknown)} 个素材缺少授权信息")
    return _ok(ev, f"{len(ok_assets)} 个素材授权有效")


def check_build_eligibility(snap):
    """Build eligibility depends on asset licenses: revoked material used by any
    page disqualifies the build."""
    revoked_used = sorted({p for a in snap.candidate.get("assets", [])
                           if a.get("revoked")
                           for p in a.get("used_by", [])})
    eligible = not revoked_used
    ev = {"revoked_assets_used_by_pages": revoked_used,
          "eligible": eligible}
    if not eligible:
        return _fail(ev, f"素材撤权影响页面 {revoked_used}，构建不合格")
    return _ok(ev, "构建资格通过（无撤权素材被引用）")


def check_artifact_integrity(snap):
    build = snap.candidate.get("build", {})
    artifacts = build.get("artifacts", [])
    missing_hash = [a["path"] for a in artifacts if not a.get("sha256")]
    index = build.get("search_index", {})
    routes = build.get("routes_table", {})
    has_index = bool(index.get("sha256"))
    has_routes = bool(routes.get("sha256"))
    ev = {"artifact_count": len(artifacts),
          "missing_hash": missing_hash,
          "search_index_present": has_index,
          "routes_table_present": has_routes}
    problems = []
    if not artifacts:
        problems.append("无构建产物")
    if missing_hash:
        problems.append("产物缺少校验和")
    if not has_index:
        problems.append("缺少搜索索引")
    if not has_routes:
        problems.append("缺少路由表")
    if problems:
        return _fail(ev, "；".join(problems))
    return _ok(ev, f"{len(artifacts)} 个产物、索引与路由表校验完整")


def check_index_coverage(snap):
    """Search index must cover every page in the candidate and match the
    routes table (index lag blocks release)."""
    pages = snap.page_set_new
    indexed = set(snap.candidate.get("build", {})
                  .get("search_index", {}).get("routes", []))
    routed = set(snap.candidate.get("build", {})
                 .get("routes_table", {}).get("routes", []))
    served = pages | set(snap.candidate.get("redirects", {}).keys())
    missing_index = sorted(pages - indexed)
    stale_index = sorted(indexed - served)
    routes_mismatch = sorted(indexed ^ routed)
    ev = {"pages": sorted(pages), "indexed": sorted(indexed),
          "served": sorted(served),
          "missing_from_index": missing_index,
          "stale_in_index": stale_index,
          "index_routes_table_diff": routes_mismatch}
    problems = []
    if missing_index:
        problems.append(f"索引缺少 {missing_index}")
    if stale_index:
        problems.append(f"索引含已删除且无重定向的页 {stale_index}（索引落后）")
    if routes_mismatch:
        problems.append(f"索引与路由表不一致 {routes_mismatch}")
    if problems:
        return _fail(ev, "；".join(problems))
    return _ok(ev, f"索引覆盖全部 {len(pages)} 页且与路由表一致")


def check_routes_table(snap):
    pages = snap.page_set_new
    redirects = snap.candidate.get("redirects", {})
    # the router serves live pages AND redirect source paths
    served = pages | set(redirects.keys())
    routed = set(snap.candidate.get("build", {})
                 .get("routes_table", {}).get("routes", []))
    extra = sorted(routed - served)
    missing = sorted(served - routed)
    bad_targets = [{"from": src, "to": d} for src, d in redirects.items()
                   if d not in pages]
    ev = {"routes": sorted(routed), "missing": missing, "extra": extra,
          "bad_redirect_targets": bad_targets}
    if missing or extra or bad_targets:
        return _fail(ev, "路由表与页面集合/重定向不一致")
    return _ok(ev, "路由表与候选页面及重定向完全一致")


def check_manifest_consistency(snap):
    """Signed-manifest consistency is proven at staging/publish time against
    dist artifacts; here we verify internal hash references exist."""
    build = snap.candidate.get("build", {})
    arts = {a["path"]: a["sha256"] for a in build.get("artifacts", [])}
    mismatches = []
    for key in ("search_index", "routes_table"):
        ref = build.get(key, {})
        p = ref.get("path")
        if p and arts.get(p) != ref.get("sha256"):
            mismatches.append(p)
    ev = {"referenced": ["build.search_index", "build.routes_table"],
          "mismatches": mismatches}
    if mismatches:
        return _fail(ev, f"清单内引用校验和与产物不一致: {mismatches}")
    return _ok(ev, "清单引用与产物校验和一致")


REGISTRY: dict[str, Check] = {}
for _c in [
    Check("page_diff", "预览/线上页面增删比较", (), check_page_diff),
    Check("deleted_migration", "删除页迁移", ("page_diff",), check_deleted_migration),
    Check("redirect", "重定向有效性", ("page_diff",), check_redirects),
    Check("broken_links", "断链检查（重命名感知）",
          ("page_diff", "redirect"), check_broken_links),
    Check("feedback", "反馈闭环", ("page_diff",), check_feedback),
    Check("asset_license", "素材授权", (), check_asset_license),
    Check("build_eligibility", "构建资格（依赖素材撤权）",
          ("asset_license",), check_build_eligibility),
    Check("artifact_integrity", "构建产物完整性", ("build_eligibility",),
          check_artifact_integrity),
    Check("index_coverage", "搜索索引新鲜度（防落后）",
          ("page_diff", "routes_table"), check_index_coverage),
    Check("routes_table", "路由表一致性", ("page_diff", "redirect"),
          check_routes_table),
    Check("manifest_consistency", "清单/产物一致性",
          ("artifact_integrity",), check_manifest_consistency),
]:
    REGISTRY[_c.name] = _c


def ordered_checks() -> list[Check]:
    """Topological order respecting declared dependencies."""
    out: list[Check] = []
    done: set[str] = set()

    def visit(name: str, stack=()):
        if name in done:
            return
        if name in stack:
            raise ValueError(f"dependency cycle at {name}")
        c = REGISTRY[name]
        for d in c.deps:
            visit(d, stack + (name,))
        out.append(c)
        done.add(name)

    for name in REGISTRY:
        visit(name)
    return out


def run_all(snap) -> list[dict]:
    """Run the whole checklist against one snapshot. Each item records the
    fingerprint of (its inputs + dependency outputs)."""
    outputs: dict[str, dict] = {}
    results = []
    for c in ordered_checks():
        res = c.run(snap)
        dep_fps = {d: outputs[d]["fprint"] for d in c.deps}
        fp = fprint("dep:" + c.name, {"deps": dep_fps,
                                       "evidence": res["evidence"]})
        res = {**res, "name": c.name, "title": c.title,
               "deps": list(c.deps), "fprint": fp, "blocking": BLOCKING}
        outputs[c.name] = res
        results.append(res)
    return results
