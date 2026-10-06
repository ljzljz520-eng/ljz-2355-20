"""Two-person approval (dual control). Approvals are bound to a specific run
and its signed manifest fingerprint; any relevant change after approval forces
re-evaluation and the old approvals no longer authorize publishing."""
from __future__ import annotations

import json

from .evaluate import current_run, gate_status, now
from .fingerprint import canonical, sha256_hex

REQUIRED_APPROVERS = 2


def approve(conn, release_id: str, approver: str, manifest: dict) -> dict:
    run = current_run(conn, release_id)
    if not run:
        raise ValueError("release has never been evaluated")
    status = gate_status(conn, release_id)
    if status["blockers"]:
        raise ValueError(
            "存在未豁免阻断项，无法签准: "
            + ", ".join(b["name"] for b in status["blockers"]))
    if run["protocol_state"] != "committed":
        raise ValueError("评估事务未正常提交（发生源变更漂移），请重新评估")

    manifest_sha = sha256_hex(canonical(manifest))
    conn.execute(
        "INSERT INTO approvals(release_id, run_id, approver, "
        "signed_manifest_sha, aggregate_fprint, created_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(release_id, approver) DO UPDATE SET "
        "run_id=excluded.run_id, signed_manifest_sha=excluded.signed_manifest_sha,"
        " aggregate_fprint=excluded.aggregate_fprint, created_at=excluded.created_at",
        (release_id, run["id"], approver, manifest_sha,
         run["aggregate_fprint"], now()))
    conn.execute("INSERT INTO audit(ts, actor, action, release_id, payload) "
                 "VALUES (?,?,?,?,?)",
                 (now(), approver, "approve", release_id,
                  json.dumps({"run_id": run["id"]})))
    return approval_state(conn, release_id)


def approval_state(conn, release_id: str) -> dict:
    run = current_run(conn, release_id)
    rows = conn.execute(
        "SELECT approver, run_id, signed_manifest_sha, aggregate_fprint "
        "FROM approvals WHERE release_id=?", (release_id,)).fetchall()
    current_fp = run["aggregate_fprint"] if run else None
    current_sha = None
    if run:
        rel = conn.execute("SELECT manifest_json FROM releases WHERE id=?",
                           (release_id,)).fetchone()
        from .fingerprint import canonical as _c
        current_sha = sha256_hex(_c(json.loads(rel["manifest_json"])))
    valid = [dict(r) for r in rows
             if r["run_id"] == run["id"]
             and r["aggregate_fprint"] == current_fp
             and r["signed_manifest_sha"] == current_sha] if run else []
    stale = [dict(r) for r in rows if dict(r) not in valid]
    return {
        "release_id": release_id,
        "required": REQUIRED_APPROVERS,
        "valid_approvals": valid,
        "stale_approvals": stale,
        "approved": len({v["approver"] for v in valid}) >= REQUIRED_APPROVERS,
    }
