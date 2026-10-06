"""Reproducible long-transaction evaluation protocol.

Phases (all audited):
  1. BEGIN IMMEDIATE             single serialization point
  2. preflight tree checksum
  3. run all checks on ONE snapshot / ONE candidate manifest
  4. verify dependency fingerprints
  5. post tree checksum -> must equal preflight, else ABORT(drift)
  6. apply in-scope waivers, COMMIT
Green results from different times can never be combined: a run is atomic,
and any relevant change after the run invalidates approvals downstream.
"""
from __future__ import annotations

import datetime as dt
import json

from . import db as dbmod
from .checks import run_all
from .fingerprint import fprint, tree_checksum
from .snapshot import EMPTY_BASELINE, Snapshot


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_live_manifest(conn) -> dict | None:
    row = conn.execute("SELECT g.manifest_signed FROM active a "
                       "JOIN generations g ON g.generation = a.generation "
                       "WHERE a.id = 1").fetchone()
    if not row:
        return None
    doc = json.loads(row[0])
    return doc["manifest"]


def evaluate(conn, release_id: str, candidate: dict, source_root: str | None,
             actor: str = "system") -> dict:
    dbmod.init_db(conn)
    conn.execute("INSERT OR IGNORE INTO releases(id, manifest_json, "
                 "source_tree, created_at, status) VALUES (?,?,?,?, 'pending')",
                 (release_id, json.dumps(candidate, ensure_ascii=False),
                  source_root, now()))
    conn.execute("UPDATE releases SET manifest_json=?, source_tree=? WHERE id=?",
                 (json.dumps(candidate, ensure_ascii=False), source_root,
                  release_id))

    tx = conn.execute("BEGIN IMMEDIATE")
    try:
        live = load_live_manifest(conn) or EMPTY_BASELINE
        snap = Snapshot(candidate=candidate, live=live,
                        source_root=source_root)

        pre = tree_checksum(source_root) if source_root else None
        results = run_all(snap)
        post = tree_checksum(source_root) if source_root else None
        drifted = pre != post

        agg = aggregate_fingerprint(results)
        state = "aborted_drift" if drifted else "committed"
        passed = (not drifted) and all(r["status"] == "pass"
                                       for r in results)

        cur = conn.execute(
            "INSERT INTO check_runs(release_id, kind, aggregate_fprint, "
            "pre_tree_checksum, post_tree_checksum, protocol_state, passed, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (release_id, "evaluate", agg, pre, post, state,
             1 if passed else 0, now()))
        run_id = cur.lastrowid

        waivers = {w["item"]: w for w in active_waivers(conn, release_id, agg)}
        waived_count = 0
        for r in results:
            status = r["status"]
            waiver_id = None
            if status == "fail" and r["name"] in waivers:
                status = "waived"
                waiver_id = waivers[r["name"]]["id"]
                waived_count += 1
            conn.execute(
                "INSERT INTO check_items(run_id, release_id, name, title, "
                "status, blocking, evidence, detail, depends_fprint, "
                "waiver_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, release_id, r["name"], r["title"], status,
                 1 if r["blocking"] else 0,
                 json.dumps(r["evidence"], ensure_ascii=False), r["detail"],
                 r["fprint"], waiver_id, now()))

        # invalidate waivers that no longer match current inputs
        conn.execute("UPDATE waivers SET active=0 WHERE release_id=? AND "
                     "active=1 AND scope_fprint<>?", (release_id, agg))

        effective = passed or all(
            r["status"] in ("pass", "waived") for r in results)
        conn.execute("UPDATE releases SET current_run_id=? WHERE id=?",
                     (run_id, release_id))
        conn.execute("INSERT INTO audit(ts, actor, action, release_id, "
                     "payload) VALUES (?,?,?,?,?)",
                     (now(), actor, "evaluate", release_id,
                      json.dumps({"run_id": run_id, "state": state,
                                  "passed": passed,
                                  "waived": waived_count})))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {"release_id": release_id, "run_id": run_id, "state": state,
            "passed": passed, "effective_pass": effective,
            "aggregate_fingerprint": agg,
            "drifted": drifted, "items": summarize_items(conn, run_id)}


def aggregate_fingerprint(results: list[dict]) -> str:
    return fprint("aggregate", [{"name": r["name"], "fprint": r["fprint"]}
                                 for r in results])


def active_waivers(conn, release_id: str, scope_fprint: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM waivers WHERE release_id=? AND active=1 AND "
        "scope_fprint=? AND (expires_at IS NULL OR expires_at > ?)",
        (release_id, scope_fprint, now())).fetchall()
    return [dict(r) for r in rows]


def add_waiver(conn, release_id: str, item: str, reason: str,
               approver: str, expires_at: str | None = None) -> dict:
    dbmod.init_db(conn)
    run = current_run(conn, release_id)
    scope = run["aggregate_fprint"] if run else ""
    cur = conn.execute(
        "INSERT INTO waivers(release_id, item, reason, scope_fprint, "
        "expires_at, approver, created_at) VALUES (?,?,?,?,?,?,?)",
        (release_id, item, reason, scope, expires_at, approver, now()))
    conn.execute("INSERT INTO audit(ts, actor, action, release_id, payload) "
                 "VALUES (?,?,?,?,?)",
                 (now(), approver, "waiver", release_id,
                  json.dumps({"item": item})))
    return {"id": cur.lastrowid, "item": item, "scope_fprint": scope}


def current_run(conn, release_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM check_runs WHERE release_id=? AND "
                       "kind='evaluate' ORDER BY id DESC LIMIT 1",
                       (release_id,)).fetchone()
    return dict(row) if row else None


def summarize_items(conn, run_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM check_items WHERE run_id=? ORDER BY id",
                        (run_id,)).fetchall()
    return [dict(r) for r in rows]


def gate_status(conn, release_id: str) -> dict:
    """Current gate with live recomputation of blocking items. The frontend
    uses this to locate blockers precisely."""
    run = current_run(conn, release_id)
    if not run:
        return {"release_id": release_id, "evaluated": False}
    items = summarize_items(conn, run["id"])
    blockers = [{"name": i["name"], "title": i["title"],
                 "detail": i["detail"], "evidence": json.loads(i["evidence"])}
                for i in items if i["status"] == "fail"]
    waived = [{"name": i["name"], "detail": i["detail"]}
              for i in items if i["status"] == "waived"]
    return {
        "release_id": release_id,
        "evaluated": True,
        "run_id": run["id"],
        "state": run["protocol_state"],
        "aggregate_fingerprint": run["aggregate_fprint"],
        "pre_tree_checksum": run["pre_tree_checksum"],
        "post_tree_checksum": run["post_tree_checksum"],
        "passed": bool(run["passed"]),
        "effective_pass": not blockers,
        "blockers": blockers,
        "waived": waived,
        "items": items,
    }
