"""Orchestration layer: ties DB + evaluation + approval + publish together and
adds in-process coordination for concurrent publish requests."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import db as dbmod, evaluate as evmod, approve as apmod, publish as pb
from .manifest import build_manifest


class ConflictError(RuntimeError):
    pass


class Service:
    def __init__(self, db_path: str, store_dir: str, secret: bytes):
        self.db_path = db_path
        self.store_dir = store_dir
        self.secret = secret
        self._switch = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_guard = threading.Lock()
        dbmod.init_db(self.conn())

    def conn(self):
        return dbmod.connect(self.db_path)

    # ----------------------------------------------------------- planning --
    def create_release(self, release_id: str, site_dir: str) -> dict:
        manifest = build_manifest(site_dir, release_id)
        conn = self.conn()
        conn.execute(
            "INSERT INTO releases(id, manifest_json, source_tree, created_at, "
            "status) VALUES (?,?,?,?, 'pending') ON CONFLICT(id) DO UPDATE SET "
            "manifest_json=excluded.manifest_json, source_tree=excluded.source_tree",
            (release_id, json.dumps(manifest, ensure_ascii=False),
             str(Path(site_dir) / "src"), now()))
        conn.execute("INSERT INTO audit(ts, actor, action, release_id, payload) "
                     "VALUES (?,?,?,?,?)",
                     (now(), "system", "create", release_id,
                      json.dumps({"site": site_dir})))
        return manifest

    def get_manifest(self, release_id: str) -> dict:
        conn = self.conn()
        row = conn.execute("SELECT manifest_json FROM releases WHERE id=?",
                           (release_id,)).fetchone()
        if not row:
            raise KeyError(release_id)
        return json.loads(row["manifest_json"])

    def evaluate(self, release_id: str, actor="system") -> dict:
        conn = self.conn()
        row = conn.execute("SELECT * FROM releases WHERE id=?",
                           (release_id,)).fetchone()
        if not row:
            raise KeyError(release_id)
        manifest = json.loads(row["manifest_json"])
        return evmod.evaluate(conn, release_id, manifest, row["source_tree"],
                              actor)

    def gate(self, release_id: str) -> dict:
        conn = self.conn()
        gate = evmod.gate_status(conn, release_id)
        gate["approvals"] = apmod.approval_state(conn, release_id)
        gate["active_generation"] = pb.current_generation(conn)
        gate["public"] = pb.public_consistency(conn, self.store_dir, self.secret)
        return gate

    def waiver(self, release_id: str, item: str, reason: str,
               approver: str) -> dict:
        conn = self.conn()
        return evmod.add_waiver(conn, release_id, item, reason, approver)

    def approve(self, release_id: str, approver: str) -> dict:
        conn = self.conn()
        manifest = self.get_manifest(release_id)
        return apmod.approve(conn, release_id, approver, manifest)

    # ---------------------------------------------------------- switching --
    def publish(self, release_id: str, dist_dir: str,
                idempotency_key: str | None, actor="system") -> dict:
        idem = idempotency_key or f"release:{release_id}"

        # fast path: request already finished
        conn = self.conn()
        prior = conn.execute("SELECT * FROM publish_intents WHERE idempotency_key=?",
                             (idem,)).fetchone()
        if prior and prior["outcome"] == "published":
            return {"release_id": prior["release_id"],
                    "generation": prior["generation"],
                    "outcome": "published",
                    "idempotency_key": prior["idempotency_key"],
                    "duplicate": True}

        # coalesce same-key in-flight requests (two people same release)
        with self._inflight_guard:
            done = self._inflight.get(idem)
            if done is None:
                done = threading.Event()
                self._inflight[idem] = done
                leader = True
            else:
                leader = False

        if not leader:
            done.wait(60)
            conn = self.conn()
            row = conn.execute("SELECT * FROM publish_intents WHERE idempotency_key=?",
                               (idem,)).fetchone()
            if row:
                out = dict(row)
            else:
                act = conn.execute("SELECT generation, release_id FROM active "
                                   "WHERE id=1").fetchone()
                out = {"generation": act[0] if act else None,
                       "release_id": act[1] if act else release_id,
                       "outcome": "published"}
            out["duplicate"] = True
            return out

        try:
            # different releases switching at the same instant -> conflict
            if not self._switch.acquire(blocking=False):
                raise ConflictError("另一个发布正在切换代次，请稍后重试")
            try:
                conn = self.conn()
                race = conn.execute(
                    "SELECT * FROM publish_intents WHERE idempotency_key=?",
                    (idem,)).fetchone()
                if race:
                    return {"release_id": race["release_id"],
                            "generation": race["generation"],
                            "outcome": race["outcome"],
                            "idempotency_key": idem, "duplicate": True}
                return pb.publish(conn, release_id, dist_dir, self.secret,
                                  idem, self.store_dir, actor)
            finally:
                self._switch.release()
        finally:
            with self._inflight_guard:
                self._inflight.pop(idem, None)
            done.set()

    def rollback(self, target_generation=None, actor="ops") -> dict:
        conn = self.conn()
        return pb.rollback(conn, self.store_dir, self.secret, actor,
                           target_generation)

    def verify_public(self) -> dict:
        conn = self.conn()
        return pb.public_consistency(conn, self.store_dir, self.secret)

    def list_releases(self) -> list[dict]:
        conn = self.conn()
        rows = conn.execute("SELECT id, status, created_at, current_run_id "
                            "FROM releases ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def audit(self, release_id=None) -> list[dict]:
        conn = self.conn()
        if release_id:
            rows = conn.execute("SELECT * FROM audit WHERE release_id=? "
                                "ORDER BY id", (release_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()
