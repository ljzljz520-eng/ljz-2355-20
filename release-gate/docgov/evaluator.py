"""The 12-item release checklist evaluator.

Every check produces per-item evidence (persisted), consumes named *dependency
inputs* (fingerprints), and is recorded with an evidence hash and a row
fingerprint.  Re-running the same snapshot must reproduce identical rows, and
the PREPARE phase re-derives every input to prove nothing relevant moved after
approval ("green lights from different times cannot be stitched together").
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import manifest as M
from .canonical import encode, fingerprint, sha256


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Snapshot: one consistent view of candidate + external world
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    m: dict[str, Any]
    baseline: dict[str, Any] | None
    open_blockers: list[dict[str, Any]]
    rights: dict[str, str]
    fps: dict[str, str] = field(default_factory=dict)
    ctx: dict[str, Any] = field(default_factory=dict)


def build_snapshot(
    conn: sqlite3.Connection,
    m: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> Snapshot:
    pages = m["pages"]
    page_paths = {p["path"] for p in pages}
    artifacts = dict(m["artifacts"])
    redirects = m["redirects"]
    red_from = {r["from"] for r in redirects}

    fb_rows = conn.execute(
        "SELECT id, page_path, severity, summary FROM feedback "
        "WHERE status='OPEN' AND severity='BLOCKER' ORDER BY id"
    ).fetchall()
    open_blockers = [dict(r) for r in fb_rows]

    rights: dict[str, str] = {}
    for a in m["assets"]:
        row = conn.execute(
            "SELECT licence FROM asset_registry WHERE path=?", (a["path"],)
        ).fetchone()
        rights[a["path"]] = row["licence"] if row else "UNKNOWN"

    rebuilt = {ap: sha256(M.render_artifact(p))
               for p in pages for ap in [M.artifact_for_page(p["path"])]}

    fps = {
        "mf": fingerprint(m),
        "pages": M.fp_pages(m),
        "assets": M.fp_assets(m),
        "redirects": M.fp_redirects(m),
        "artifacts": M.fp_artifacts(m),
        "index": M.fp_index(m),
        "index_full": M.fp_index_full(m),
        "routes": M.fp_routes(m),
        "baseline_mf": fingerprint(baseline) if baseline else "∅",
        "baseline_pages": fingerprint(sorted(p["path"] for p in baseline["pages"]))
        if baseline else "∅",
        "feedback": fingerprint(open_blockers),
        "asset_rights": fingerprint(sorted(rights.items())),
        "build": fingerprint(sorted(rebuilt.items())),
    }
    ctx = {
        "page_paths": page_paths, "artifacts": artifacts,
        "redirects": redirects, "red_from": red_from,
        "baseline_pages": {p["path"] for p in baseline["pages"]} if baseline else set(),
        "rebuilt": rebuilt,
    }
    return Snapshot(m=m, baseline=baseline, open_blockers=open_blockers,
                    rights=rights, fps=fps, ctx=ctx)


# ---------------------------------------------------------------------------
# Finding / evidence helpers
# ---------------------------------------------------------------------------

def finding(locator_type: str, locator: str, message: str,
            detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"locator_type": locator_type, "locator": locator,
            "message": message, "detail": detail or {}}


# ---------------------------------------------------------------------------
# Individual checks.  Each returns (passed, findings, consumes).
# ---------------------------------------------------------------------------

def chk_manifest_integrity(s: Snapshot):
    findings = []
    try:
        M.validate_manifest(s.m)
    except M.ManifestError as e:
        findings.append(finding("MANIFEST", "$", str(e)))
    return not findings, findings, ["mf", "pages", "assets", "artifacts",
                                    "index_full", "routes", "redirects"]


def chk_signature(s: Snapshot, verify_sig: Callable[[dict], bool]):
    ok = verify_sig(s.m)
    findings = [] if ok else [finding("MANIFEST", "$.signature",
                                      "manifest signature missing or invalid")]
    return ok, findings, ["mf"]


def chk_page_diff(s: Snapshot):
    """Preview page diff: additions/deletions.  Deletions need migration."""
    findings = []
    cur, base = s.ctx["page_paths"], s.ctx["baseline_pages"]
    if s.baseline is None:
        return True, [], ["pages", "baseline_pages", "redirects"]
    for added in sorted(cur - base):
        findings.append(finding("PAGE", added, "新增页面（预览差异）",
                                {"kind": "added"}))
    for removed in sorted(base - cur):
        migrated = removed in s.ctx["red_from"]
        findings.append(finding(
            "PAGE", removed,
            "页面删除但未配置重定向迁移" if not migrated
            else "页面删除，已配置重定向迁移",
            {"kind": "removed", "migrated": migrated,
             "blocking": not migrated}))
    blocking = [f for f in findings if f["detail"].get("blocking", True)
                and f["detail"].get("kind") == "removed"]
    return not blocking, findings, ["pages", "baseline_pages", "redirects"]


def chk_broken_links(s: Snapshot):
    """Broken links depend on page set AND redirects (rename changes result)."""
    findings = []
    targets = (s.ctx["page_paths"] | s.ctx["red_from"]
               | {a["path"] for a in s.m["assets"]})
    for p in sorted(s.m["pages"], key=lambda x: x["path"]):
        for link in M.scan_links(p["blob"]):
            if link not in targets:
                findings.append(finding("PAGE", p["path"],
                                        f"断链：{link}",
                                        {"target": link}))
    return not findings, findings, ["pages", "redirects"]


def chk_redirects(s: Snapshot):
    findings = []
    targets = s.ctx["page_paths"]
    for r in sorted(s.ctx["redirects"], key=lambda x: x["from"]):
        if r["to"] not in targets:
            findings.append(finding("REDIRECT", r["from"],
                                    f"重定向目标不存在：{r['to']}",
                                    {"to": r["to"]}))
    # self loop
    for r in s.ctx["redirects"]:
        if r["from"] == r["to"]:
            findings.append(finding("REDIRECT", r["from"], "重定向自循环"))
    return not findings, findings, ["redirects", "pages"]


def chk_feedback(s: Snapshot):
    findings = [finding("PAGE", f["page_path"],
                        f"未解决的阻断反馈：{f['summary']}",
                        {"feedback_id": f["id"], "severity": f["severity"]})
                for f in s.open_blockers
                if f["page_path"] in s.ctx["page_paths"]
                or f["page_path"] == "*"]
    return not findings, findings, ["feedback", "pages"]


def chk_asset_rights(s: Snapshot):
    findings = []
    for path, lic in sorted(s.rights.items()):
        if lic in ("REVOKED", "UNKNOWN"):
            findings.append(finding("ASSET", path,
                                    "素材已撤权/授权状态未知，禁止发布",
                                    {"licence": lic}))
    return not findings, findings, ["assets", "asset_rights"]


def chk_asset_refs(s: Snapshot):
    findings = []
    known = {a["path"] for a in s.m["assets"]}
    for p in sorted(s.m["pages"], key=lambda x: x["path"]):
        for link in M.scan_links(p["blob"]):
            if link.startswith("assets/") and link not in known:
                findings.append(finding("ASSET", link,
                                        f"页面引用了缺失素材（来自 {p['path']}）",
                                        {"page": p["path"]}))
    return not findings, findings, ["pages", "assets"]


def chk_build_eligible(s: Snapshot):
    """Build eligibility: deterministic outputs AND asset rights.

    素材撤权会改变构建资格 —— this check consumes both build and rights inputs.
    """
    findings = []
    for ap, want in sorted(s.ctx["artifacts"].items()):
        got = s.ctx["rebuilt"].get(ap)
        if got is None:
            findings.append(finding("ROUTE", ap, "产物无对应源页面，无法构建"))
        elif got != want:
            findings.append(finding("ROUTE", ap,
                                    "产物校验和与确定性构建不一致，构建不可复验"))
    for path, lic in sorted(s.rights.items()):
        if lic in ("REVOKED", "UNKNOWN"):
            findings.append(finding("ASSET", path,
                                    "素材授权失效，构建资格不成立",
                                    {"licence": lic}))
    return not findings, findings, ["build", "artifacts", "pages",
                                    "assets", "asset_rights"]


def chk_index_fresh(s: Snapshot):
    ids = set(s.m["index"]["doc_ids"])
    arts = set(s.ctx["artifacts"])
    findings = []
    if ids != arts:
        for missing in sorted(arts - ids):
            findings.append(finding("INDEX", missing, "搜索索引缺少该页面（索引落后）"))
        for stale in sorted(ids - arts):
            findings.append(finding("INDEX", stale, "搜索索引引用已删除页面（索引陈旧）"))
    if s.m["index"]["checksum"] != sha256(encode(sorted(s.m["index"]["doc_ids"]))):
        findings.append(finding("INDEX", "$.checksum", "索引校验和不匹配"))
    return not findings, findings, ["index", "index_full", "pages", "artifacts"]


def chk_routes(s: Snapshot):
    findings = []
    routes = s.m["routes"]
    arts = set(s.ctx["artifacts"])
    for path, art in sorted(routes.items()):
        if art not in arts:
            findings.append(finding("ROUTE", path,
                                    f"路由指向不存在的产物 {art}"))
    if set(routes.values()) != arts:
        for art in sorted(arts - set(routes.values())):
            findings.append(finding("ROUTE", art, "构建产物未挂接到路由表"))
    return not findings, findings, ["routes", "artifacts"]


EVALUATE_CHECKS: list[tuple[str, str, bool]] = [
    # code, title, waivable
    ("MANIFEST_INTEGRITY", "候选 manifest 结构完整性", False),
    ("MANIFEST_SIGNATURE", "候选 manifest 签名", False),
    ("PAGE_DIFF", "预览页面对比（增/删/迁移）", True),
    ("BROKEN_LINKS", "断链检查（依赖重命名与重定向）", True),
    ("REDIRECTS", "重定向有效性", True),
    ("FEEDBACK_OPEN", "阻断级反馈清零", False),
    ("ASSET_RIGHTS", "素材授权/撤权", False),
    ("ASSET_REFS", "页面素材引用完整性", True),
    ("BUILD_ELIGIBLE", "构建资格（可复验构建+授权）", False),
    ("INDEX_FRESH", "搜索索引新鲜度", False),
    ("ROUTES", "路由表一致性", True),
]


def row_fingerprint(code: str, status: str, phase: str, evidence_hash: str,
                    inputs: dict[str, str], waived: bool, waiver_id: str | None) -> str:
    return fingerprint({
        "code": code, "status": status, "phase": phase,
        "evidence_hash": evidence_hash, "inputs": inputs,
        "waived": waived, "waiver_id": waiver_id,
    })
