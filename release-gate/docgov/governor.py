"""Release governor: checklist evaluation + reproducible commit protocol.

Lifecycle:

    submit   -> DRAFT
    evaluate -> BLOCKED | SEALED     (long IMMEDIATE tx, per-item evidence)
    waiver   -> granted, bound to the row's dependency fingerprints
    approve  -> APPROVED             (signature over manifest+seal)
    prepare  -> PREPARED             (re-verify all dependency fingerprints in
                                      another long tx, materialise generation,
                                      read back & attest signed manifest)
    publish  -> PUBLISHED            (idempotent, CAS baseline switch:
                                      artifacts+index+routes as one generation)
    rollback -> previous generation  (old version stays fully servable)

After approval, ANY relevant change (page rename, asset revocation, feedback,
baseline drift...) changes dependency fingerprints: prepare refuses, forcing
re-evaluation and re-approval.  Green lights produced at different times can
never be stitched into one pass.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import evaluator as E
from . import manifest as M
from .canonical import b64id, encode, fingerprint, sha256, sign, verify
from .db import Database
from .errors import GovError

EVALUATE = "EVALUATE"
PREPARE = "PREPARE"


class Governor:
    def __init__(self, db_path: str, data_dir: str,
                 author_key: str = "demo-author-key",
                 approver_key: str = "demo-approver-key"):
        self.db = Database(db_path)
        self.root = Path(data_dir)
        (self.root / "generations").mkdir(parents=True, exist_ok=True)
        self.author_key = author_key
        self.approver_key = approver_key

    # ------------------------------------------------------------------ utils

    def _log(self, conn, kind: str, release_pk: str | None, detail: dict | None = None,
             actor: str = "system") -> None:
        conn.execute(
            "INSERT INTO events(at,actor,kind,release_pk,detail) VALUES(?,?,?,?,?)",
            (E.now(), actor, kind, release_pk,
             json.dumps(detail or {}, ensure_ascii=False)))

    def _get(self, conn, pk: str):
        row = conn.execute("SELECT * FROM releases WHERE id=?", (pk,)).fetchone()
        if not row:
            raise GovError("NOT_FOUND", f"release {pk} not found", 404)
        return row

    def _active(self, conn):
        return conn.execute("SELECT * FROM active WHERE singleton=1").fetchone()

    # ------------------------------------------------------------- submission

    def submit(self, manifest: dict[str, Any], signature: str,
               actor: str = "author") -> dict[str, Any]:
        try:
            prepared = M.prepare_candidate(json.loads(json.dumps(manifest)))
        except M.ManifestError as e:
            raise GovError("INVALID_MANIFEST", str(e), 422)
        m = prepared.manifest
        if not verify(m, signature, self.author_key):
            raise GovError("BAD_SIGNATURE", "manifest signature invalid", 401)

        with self.db.transaction(immediate=True) as conn:
            prev = conn.execute(
                "SELECT id, version, state FROM releases WHERE release_id=? "
                "ORDER BY version DESC LIMIT 1", (m["release_id"],)).fetchone()
            version = (prev["version"] + 1) if prev else 1
            supersedes = prev["id"] if prev else None
            active = self._active(conn)
            baseline_mf = active["mf"] if active else None
            pk = f"rel_{b64id(8)}"
            ts = E.now()
            conn.execute(
                "INSERT INTO releases(id,release_id,version,manifest,mf,state,"
                "baseline_mf,supersedes,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pk, m["release_id"], version,
                 json.dumps({"manifest": m, "signature": signature},
                            ensure_ascii=False),
                 prepared.mf, "DRAFT", baseline_mf, supersedes, ts, ts))
            self._log(conn, "SUBMIT", pk, {"version": version, "mf": prepared.mf}, actor)
            return self.release_view(conn, pk)

    @staticmethod
    def _row_manifest(row) -> dict[str, Any]:
        return json.loads(row["manifest"])["manifest"]

    def release_view(self, conn, pk: str) -> dict[str, Any]:
        row = self._get(conn, pk)
        env = json.loads(row["manifest"])
        checks = [dict(r) for r in conn.execute(
            "SELECT code,status,phase,waivable,waiver_id,evidence,evidence_hash,"
            "inputs,row_fp,evaluated_at FROM check_rows WHERE release_pk=? "
            "ORDER BY phase, code", (pk,)).fetchall()]
        for c in checks:
            c["evidence"] = json.loads(c["evidence"])
            c["inputs"] = json.loads(c["inputs"])
        active = self._active(conn)
        return {
            "id": row["id"], "release_id": row["release_id"],
            "version": row["version"], "state": row["state"],
            "mf": row["mf"], "baseline_mf": row["baseline_mf"],
            "seal": row["seal"], "approved": bool(row["approve_sig"]),
            "approve_by": row["approve_by"],
            "staged_gen": row["staged_gen"],
            "published_gen": row["published_gen"],
            "is_active": bool(active and active["gen"] == row["published_gen"]),
            "signature": env["signature"],
            "checks": checks,
            "blockers": [c for c in checks
                         if c["status"] == "BLOCKED" and not c.get("waiver_id")],
        }

    def get_release(self, pk: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            return self.release_view(conn, pk)

    def list_releases(self) -> dict[str, Any]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM releases ORDER BY created_at DESC").fetchall()
            active = self._active(conn)
            return {
                "releases": [self.release_view(conn, r["id"]) for r in rows],
                "active_gen": dict(active) if active else None,
            }

    # ------------------------------------------------------------- evaluation

    def _run_evaluate(self, s: E.Snapshot) -> list[dict[str, Any]]:
        checks = []
        runners: dict[str, Any] = {
            "MANIFEST_INTEGRITY": lambda: E.chk_manifest_integrity(s),
            # signature is verified directly below against the stored envelope
            "MANIFEST_SIGNATURE": lambda: E.chk_signature(s, lambda m: True),
            "PAGE_DIFF": lambda: E.chk_page_diff(s),
            "BROKEN_LINKS": lambda: E.chk_broken_links(s),
            "REDIRECTS": lambda: E.chk_redirects(s),
            "FEEDBACK_OPEN": lambda: E.chk_feedback(s),
            "ASSET_RIGHTS": lambda: E.chk_asset_rights(s),
            "ASSET_REFS": lambda: E.chk_asset_refs(s),
            "BUILD_ELIGIBLE": lambda: E.chk_build_eligible(s),
            "INDEX_FRESH": lambda: E.chk_index_fresh(s),
            "ROUTES": lambda: E.chk_routes(s),
        }
        for code, _title, waivable in E.EVALUATE_CHECKS:
            passed, findings, consume = runners[code]()
            evidence = {
                "title": dict((c, t) for c, t, _ in E.EVALUATE_CHECKS)[code],
                "passed": passed, "findings": findings,
                "consumes": consume,
            }
            inputs = {k: s.fps[k] for k in consume}
            checks.append({"code": code, "waivable": waivable,
                           "passed": passed, "evidence": evidence,
                           "inputs": inputs})
        return checks

    def evaluate(self, pk: str, actor: str = "ci") -> dict[str, Any]:
        """Run the whole checklist inside ONE long IMMEDIATE transaction."""
        with self.db.long_tx() as conn:
            row = self._get(conn, pk)
            if row["state"] == "PUBLISHED":
                raise GovError("RELEASE_FINAL", "已发布版本不可重新评估", 409)
            env = json.loads(row["manifest"])
            m, signature = env["manifest"], env["signature"]

            baseline = None
            if row["baseline_mf"]:
                a = self._active(conn)
                if not a or a["mf"] != row["baseline_mf"]:
                    raise GovError("BASELINE_DRIFT",
                                   "评估基线与当前线上代次不一致，请重新提交候选",
                                   409)
                baseline = self._row_manifest(
                    self._gen_release_row(conn, a["gen"]))

            snap = E.build_snapshot(conn, m, baseline)

            # signature is re-verified inside the frozen snapshot too
            sig_ok = verify(m, signature, self.author_key)
            checks = self._run_evaluate(snap)
            checks[1]["passed"] = sig_ok
            checks[1]["evidence"]["passed"] = sig_ok
            if not sig_ok:
                checks[1]["evidence"]["findings"] = [
                    E.finding("MANIFEST", "$.signature", "候选 manifest 签名无效")]

            ts = E.now()
            conn.execute("DELETE FROM check_rows WHERE release_pk=? AND phase=?",
                         (pk, EVALUATE))
            stored: list[dict[str, Any]] = []
            for c in checks:
                self._insert_row(conn, pk, c["code"], EVALUATE, c["waivable"],
                                 c["passed"], c["evidence"], c["inputs"], ts)
                stored.append(c)

            # apply currently-binding waivers, THEN seal over final row fps
            releasable = self._releasable(conn, pk)
            seal = self._seal(conn, pk)
            state = "SEALED" if releasable else "BLOCKED"

            # approval/preparation are invalidated by any post-approval change
            invalidate = row["state"] in ("APPROVED", "PREPARED")
            conn.execute(
                "UPDATE releases SET state=?, seal=?, approve_sig=NULL, "
                "approve_by=NULL, staged_gen=NULL, updated_at=? WHERE id=?",
                (state, seal, ts, pk))
            self._log(conn, "EVALUATE", pk,
                      {"state": state, "seal": seal, "invalidated": invalidate,
                       "blocked": [c["code"] for c in stored
                                   if not c["passed"]]}, actor)
            return self.release_view(conn, pk)

    def _gen_release_row(self, conn, gen: str):
        g = conn.execute("SELECT * FROM generations WHERE gen=?", (gen,)).fetchone()
        return conn.execute("SELECT * FROM releases WHERE id=?",
                            (g["release_pk"],)).fetchone()

    def _insert_row(self, conn, pk, code, phase, waivable, passed, evidence,
                    inputs, ts) -> str:
        ev_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        status = "PASS" if passed else "BLOCKED"
        row_fp = E.row_fingerprint(code, status, phase, sha256(ev_json.encode()),
                                   inputs, False, None)
        conn.execute(
            "INSERT INTO check_rows(release_pk,code,status,phase,waivable,"
            "evidence,evidence_hash,inputs,row_fp,evaluated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pk, code, status, phase, int(waivable), ev_json,
             sha256(ev_json.encode()),
             json.dumps(inputs, ensure_ascii=False, sort_keys=True),
             row_fp, ts))
        return row_fp

    def _seal(self, conn, pk: str, phase: str = EVALUATE) -> str:
        rows = conn.execute(
            "SELECT code,row_fp FROM check_rows WHERE release_pk=? AND phase=?",
            (pk, phase)).fetchall()
        return fingerprint([{"code": r["code"], "row_fp": r["row_fp"]}
                            for r in rows])

    def _commit_fp(self, conn, pk: str) -> str:
        rows = conn.execute(
            "SELECT code,phase,row_fp FROM check_rows WHERE release_pk=? "
            "ORDER BY phase,code", (pk,)).fetchall()
        return fingerprint([dict(r) for r in rows])

    def _releasable(self, conn, pk: str) -> bool:
        """Apply waivers: BLOCKED row passes only if waivable and a waiver whose
        binding still equals the row's current dependency fingerprints exists."""
        rows = conn.execute(
            "SELECT code,status,waivable,inputs,row_fp FROM check_rows "
            "WHERE release_pk=? AND phase=?", (pk, EVALUATE)).fetchall()
        conn.execute(
            "UPDATE check_rows SET waiver_id=NULL WHERE release_pk=? AND phase=?",
            (pk, EVALUATE))  # clear, re-derive below
        ok = True
        for r in rows:
            waiver_id = None
            if r["status"] == "BLOCKED":
                if not r["waivable"]:
                    ok = False
                else:
                    w = conn.execute(
                        "SELECT id,binding FROM waivers WHERE release_pk=? "
                        "AND code=? AND revoked=0 ORDER BY granted_at DESC LIMIT 1",
                        (pk, r["code"])).fetchone()
                    if w and w["binding"] == r["inputs"]:
                        waiver_id = w["id"]
                    else:
                        ok = False
            new_fp = E.row_fingerprint(
                r["code"], r["status"], EVALUATE,
                conn.execute(
                    "SELECT evidence_hash FROM check_rows WHERE release_pk=? "
                    "AND code=? AND phase=?",
                    (pk, r["code"], EVALUATE)).fetchone()["evidence_hash"],
                json.loads(r["inputs"]), bool(waiver_id), waiver_id)
            conn.execute(
                "UPDATE check_rows SET waiver_id=?, row_fp=? WHERE release_pk=? "
                "AND code=? AND phase=?",
                (waiver_id, new_fp, pk, r["code"], EVALUATE))
        return ok

    # ---------------------------------------------------------------- waivers

    def grant_waiver(self, pk: str, code: str, reason: str,
                     granted_by: str = "manager") -> dict[str, Any]:
        with self.db.long_tx() as conn:
            row = self._get(conn, pk)
            cr = conn.execute(
                "SELECT waivable,inputs,status FROM check_rows WHERE release_pk=? "
                "AND code=? AND phase=?", (pk, code, EVALUATE)).fetchone()
            if not cr:
                raise GovError("NO_CHECK", "检查项尚未运行", 409)
            if not cr["waivable"]:
                raise GovError("NOT_WAIVABLE",
                               f"检查项 {code} 为硬性阻断，不可豁免", 422)
            binding = json.loads(cr["inputs"])
            wid = f"wv_{b64id(6)}"
            ts = E.now()
            payload = {"waiver_id": wid, "release_pk": pk, "code": code,
                       "binding": binding, "reason": reason}
            sig = sign(payload, self.approver_key)
            conn.execute(
                "INSERT INTO waivers(id,release_pk,code,reason,binding,"
                "binding_hash,sig,granted_by,granted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (wid, pk, code, reason,
                 json.dumps(binding, ensure_ascii=False, sort_keys=True),
                 fingerprint(binding), sig, granted_by, ts))
            seal = None
            if self._releasable(conn, pk):
                seal = self._seal(conn, pk)
                conn.execute(
                    "UPDATE releases SET state='SEALED', seal=?, approve_sig=NULL,"
                    " updated_at=? WHERE id=?", (seal, ts, pk))
            self._log(conn, "WAIVER", pk, {"code": code, "waiver": wid}, granted_by)
            return {"id": wid, "code": code, "binding": binding, "seal": seal,
                    "state": self._get(conn, pk)["state"]}

    # --------------------------------------------------------------- approval

    def approve(self, pk: str, approver: str = "manager") -> dict[str, Any]:
        with self.db.long_tx() as conn:
            row = self._get(conn, pk)
            if row["state"] != "SEALED":
                raise GovError("NOT_SEALED",
                               f"当前状态 {row['state']}，清单未通过，无法批准", 409)
            if not self._releasable(conn, pk):
                raise GovError("NOT_SEALED", "仍有未豁免阻断项", 409)
            seal = self._seal(conn, pk)
            payload = {"release_pk": pk, "mf": row["mf"], "seal": seal}
            sig = sign(payload, self.approver_key)
            conn.execute(
                "UPDATE releases SET state='APPROVED', approve_sig=?, "
                "approve_by=?, seal=?, updated_at=? WHERE id=?",
                (sig, approver, seal, E.now(), pk))
            self._log(conn, "APPROVE", pk, {"seal": seal}, approver)
            return self.release_view(conn, pk)

    # ------------------------------------------------- reproducible 2-phase tx

    def _materialize(self, gen: str, m: dict[str, Any]) -> dict[str, Any]:
        gdir = self.root / "generations" / gen
        if gdir.exists():
            shutil.rmtree(gdir)
        (gdir / "artifacts").mkdir(parents=True)
        for p in m["pages"]:
            ap = M.artifact_for_page(p["path"])
            path = gdir / "artifacts" / ap
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(M.render_artifact(p))
        (gdir / "index.json").write_bytes(encode(m["index"]))
        (gdir / "routes.json").write_bytes(encode(m["routes"]))
        (gdir / "manifest.json").write_bytes(encode(m))
        return {"root": str(gdir)}

    def _attest_disk(self, gdir: Path, m: dict[str, Any]) -> dict[str, Any]:
        """Read everything back FROM DISK and attest against signed manifest."""
        files: dict[str, str] = {}
        for ap, want in sorted(m["artifacts"].items()):
            data = (gdir / "artifacts" / ap).read_bytes()
            files[f"artifacts/{ap}"] = sha256(data)
            if files[f"artifacts/{ap}"] != want:
                raise GovError("MATERIALIZE_FAILED",
                               f"落盘产物与签名 manifest 不一致：{ap}")
        disk_index = json.loads((gdir / "index.json").read_bytes())
        disk_routes = json.loads((gdir / "routes.json").read_bytes())
        disk_mf_obj = json.loads((gdir / "manifest.json").read_bytes())
        disk_mf = fingerprint(disk_mf_obj)
        expected_mf = fingerprint(m)
        return {
            "files": files,
            "index_match": disk_index == m["index"],
            "routes_match": disk_routes == m["routes"],
            "manifest_fp_disk": disk_mf,
            "manifest_fp_expected": expected_mf,
            "consistent": disk_mf == expected_mf
                          and disk_index == m["index"]
                          and disk_routes == m["routes"],
        }

    def prepare(self, pk: str, actor: str = "release-manager") -> dict[str, Any]:
        """Long-txn preflight: recompute every dependency fingerprint, verify
        check rows are byte-identical to those approved, then materialise."""
        with self.db.long_tx() as conn:
            row = self._get(conn, pk)
            if row["state"] != "APPROVED":
                raise GovError("NOT_APPROVED",
                               f"状态 {row['state']}，仅已批准清单可预检", 409)
            env = json.loads(row["manifest"])
            m, signature = env["manifest"], env["signature"]
            baseline = None
            if row["baseline_mf"]:
                a = self._active(conn)
                if not a or a["mf"] != row["baseline_mf"]:
                    raise GovError("BASELINE_DRIFT",
                                   "预检时发现线上代次已变化，必须重新评估", 409)
                baseline = self._row_manifest(
                    self._gen_release_row(conn, a["gen"]))

            snap = E.build_snapshot(conn, m, baseline)
            checks = self._run_evaluate(snap)
            checks[1]["passed"] = verify(m, signature, self.author_key)
            checks[1]["evidence"]["passed"] = checks[1]["passed"]

            # --- cross-check dependency fingerprints of every stored row ----
            mismatches = []
            for c in checks:
                stored = conn.execute(
                    "SELECT inputs,status,evidence_hash FROM check_rows "
                    "WHERE release_pk=? AND code=? AND phase=?",
                    (pk, c["code"], EVALUATE)).fetchone()
                ev_json = json.dumps(c["evidence"], ensure_ascii=False,
                                     sort_keys=True)
                ev_hash = sha256(ev_json.encode())
                cur_status = "PASS" if c["passed"] else "BLOCKED"
                # dependency fingerprints + status + evidence must be identical
                # (waiver only annotates the row; it cannot change inputs)
                if (json.loads(stored["inputs"]) != c["inputs"]
                        or stored["status"] != cur_status
                        or stored["evidence_hash"] != ev_hash):
                    mismatches.append({
                        "code": c["code"],
                        "input_changed": json.loads(stored["inputs"]) != c["inputs"],
                        "status_changed": stored["status"] != cur_status,
                        "evidence_changed": stored["evidence_hash"] != ev_hash,
                        "current_inputs": c["inputs"]})
            dep_evidence = {
                "title": "依赖指纹核对（长事务预检）",
                "passed": not mismatches,
                "findings": [E.finding("DEPENDENCY", x["code"],
                            "依赖指纹与评估时不一致：批准后发生相关变更", x)
                             for x in mismatches],
                "consumes": ["*"],
            }
            # approve signature must bind the exact same seal as on record
            approve_payload = {"release_pk": pk, "mf": row["mf"],
                               "seal": row["seal"]}
            approve_ok = bool(row["approve_sig"]) and verify(
                approve_payload, row["approve_sig"], self.approver_key)

            gen = f"gen_{b64id(8)}"
            mat_info = self._materialize(gen, m)
            attest = self._attest_disk(Path(mat_info["root"]), m)
            mat_evidence = {
                "title": "代次物化与签名 manifest 一致性",
                "passed": attest["consistent"] and approve_ok,
                "findings": [] if attest["consistent"] else [
                    E.finding("GENERATION", gen,
                              "回读的公开资料与已签 manifest 不一致", attest)],
                "consumes": ["*"],
                "attestation": attest,
                "gen": gen,
            }
            if not approve_ok:
                mat_evidence["findings"].append(E.finding(
                    "APPROVAL", pk, "批准签名与当前清单 seal 不匹配"))

            conn.execute("DELETE FROM check_rows WHERE release_pk=? AND phase=?",
                         (pk, PREPARE))
            ts = E.now()
            self._insert_row(conn, pk, "DEP_FINGERPRINTS", PREPARE, False,
                             not mismatches, dep_evidence, {"*": snap.fps["mf"]}, ts)
            self._insert_row(conn, pk, "SIGNED_CONSISTENCY", PREPARE, False,
                             mat_evidence["passed"], mat_evidence,
                             {"*": attest["manifest_fp_disk"]}, ts)

            if mismatches or not attest["consistent"] or not approve_ok:
                shutil.rmtree(mat_info["root"], ignore_errors=True)
                self._log(conn, "PREPARE_FAILED", pk,
                          {"mismatches": [x["code"] for x in mismatches]}, actor)
                raise GovError("CHECKLIST_STALE",
                               "预检失败：依赖指纹变更或资料不一致，需重新评估/批准",
                               409, {"mismatches": mismatches})

            conn.execute(
                "INSERT INTO generations(gen,release_pk,mf,root,state,"
                "manifest_path,created_at) VALUES(?,?,?,?,?,?,?)",
                (gen, pk, row["mf"], mat_info["root"], "STAGED",
                 str(Path(mat_info["root"]) / "manifest.json"), ts))
            commit_fp = self._commit_fp(conn, pk)
            conn.execute(
                "UPDATE releases SET state='PREPARED', staged_gen=?, "
                "updated_at=? WHERE id=?", (gen, ts, pk))
            self._log(conn, "PREPARE", pk, {"gen": gen, "commit_fp": commit_fp},
                      actor)
            return self.release_view(conn, pk)

    # ------------------------------------------------------------------ switch

    def publish(self, pk: str, idem_token: str | None = None,
                actor: str = "release-manager") -> dict[str, Any]:
        """Atomic same-generation switch with idempotency + CAS on baseline."""
        token = idem_token or f"tok_{b64id(8)}"
        with self.db.long_tx() as conn:
            prior = conn.execute(
                "SELECT result FROM idem_requests WHERE token=?", (token,)
            ).fetchone()
            if prior:
                return {"idempotent": True, **json.loads(prior["result"])}

            row = self._get(conn, pk)
            if row["state"] == "PUBLISHED":
                raise GovError("ALREADY_PUBLISHED",
                               "该候选已发布；重复请求请携带相同幂等令牌", 409)
            if row["state"] != "PREPARED":
                raise GovError("NOT_PREPARED",
                               f"状态 {row['state']}，未完成可复验预检", 409)

            # commit fingerprint must still cover exactly the prepared rows
            if not row["staged_gen"]:
                raise GovError("NOT_PREPARED", "缺少暂存代次", 409)
            if self._commit_fp(conn, pk) is None:
                raise GovError("NOT_PREPARED", "提交证据缺失", 409)

            active = self._active(conn)
            current_mf = active["mf"] if active else None
            if row["baseline_mf"] != current_mf:
                self._log(conn, "PUBLISH_CONFLICT", pk,
                          {"expected_baseline": row["baseline_mf"],
                           "current": current_mf}, actor)
                raise GovError("BASELINE_DRIFT",
                               "另一发布已切换代次：本次绿灯基于旧基线，"
                               "旧版保持完整服务，请重新评估后发布", 409)

            gen = row["staged_gen"]
            g = conn.execute("SELECT * FROM generations WHERE gen=? AND state='STAGED'",
                             (gen,)).fetchone()
            if not g:
                raise GovError("NOT_PREPARED", "暂存代次不存在", 409)
            # disk must still match the signed manifest at switch time
            gdir = Path(g["root"])
            m = self._row_manifest(row)
            attest = self._attest_disk(gdir, m)
            if not attest["consistent"]:
                raise GovError("MATERIALIZE_FAILED",
                               "切换时刻校验失败，旧版保持服务", 409)

            ts = E.now()
            if active:
                conn.execute("UPDATE generations SET state='RETIRED' WHERE gen=?",
                             (active["gen"],))
            conn.execute("UPDATE generations SET state='ACTIVE' WHERE gen=?", (gen,))
            conn.execute(
                "INSERT INTO active(singleton,gen,mf,baseline_mf,switched_at) "
                "VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "gen=excluded.gen,mf=excluded.mf,baseline_mf=excluded.baseline_mf,"
                "switched_at=excluded.switched_at",
                (gen, row["mf"], current_mf, ts))
            conn.execute(
                "UPDATE releases SET state='PUBLISHED', published_gen=?, "
                "updated_at=? WHERE id=?", (gen, ts, pk))
            result = {"release_pk": pk, "gen": gen, "mf": row["mf"],
                      "switched_at": ts}
            conn.execute(
                "INSERT INTO idem_requests(token,release_pk,result,at) "
                "VALUES(?,?,?,?)",
                (token, pk, json.dumps(result, ensure_ascii=False), ts))
            self._log(conn, "PUBLISH", pk, result, actor)
            return {"idempotent": False, **result}

    # ---------------------------------------------------------------- rollback

    def rollback(self, target_gen: str, actor: str = "release-manager") -> dict[str, Any]:
        with self.db.long_tx() as conn:
            t = conn.execute("SELECT * FROM generations WHERE gen=?",
                             (target_gen,)).fetchone()
            if not t:
                raise GovError("NOT_FOUND", "目标代次不存在", 404)
            if t["state"] not in ("RETIRED", "ACTIVE"):
                raise GovError("BAD_TARGET",
                               f"代次状态 {t['state']}，不可回滚", 409)
            active = self._active(conn)
            if active and active["gen"] == target_gen:
                raise GovError("BAD_TARGET", "该代次当前在线，无需回滚", 409)
            # old generation must still be intact and match its signed manifest
            m = self._row_manifest(self._gen_release_row(conn, target_gen))
            attest = self._attest_disk(Path(t["root"]), m)
            if not attest["consistent"]:
                raise GovError("MATERIALIZE_FAILED",
                               "旧代次资料损坏，无法回滚", 409)
            ts = E.now()
            if active:
                conn.execute("UPDATE generations SET state='RETIRED' WHERE gen=?",
                             (active["gen"],))
            conn.execute("UPDATE generations SET state='ACTIVE' WHERE gen=?",
                         (target_gen,))
            conn.execute(
                "UPDATE active SET gen=?, mf=?, switched_at=? WHERE singleton=1",
                (target_gen, t["mf"], ts))
            self._log(conn, "ROLLBACK", None, {"gen": target_gen}, actor)
            return {"rolled_back_to": target_gen, "mf": t["mf"],
                    "switched_at": ts}

    # ------------------------------------------------------------- externals

    def add_feedback(self, page_path: str, severity: str, summary: str) -> dict[str, Any]:
        if severity not in ("BLOCKER", "MAJOR", "MINOR"):
            raise GovError("BAD_INPUT", "severity invalid")
        fid = f"fb_{b64id(6)}"
        ts = E.now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO feedback(id,page_path,severity,status,summary,created_at)"
                " VALUES(?,?,?,'OPEN',?,?)", (fid, page_path, severity, summary, ts))
            self._log(conn, "FEEDBACK_ADD", None, {"id": fid, "page": page_path})
        return {"id": fid}

    def resolve_feedback(self, fid: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("UPDATE feedback SET status='RESOLVED' WHERE id=?",
                               (fid,))
            if cur.rowcount == 0:
                raise GovError("NOT_FOUND", "反馈不存在", 404)
        return {"id": fid, "status": "RESOLVED"}

    def set_asset_licence(self, path: str, licence: str) -> dict[str, Any]:
        if licence not in ("LICENSED", "REVOKED", "UNKNOWN"):
            raise GovError("BAD_INPUT", "licence invalid")
        with self.db.transaction(immediate=True) as conn:
            ts = E.now()
            conn.execute(
                "INSERT INTO asset_registry(path,licence,updated_at) VALUES(?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET licence=excluded.licence,"
                " updated_at=excluded.updated_at", (path, licence, ts))
            self._log(conn, "ASSET_LICENCE", None,
                      {"path": path, "licence": licence})
        return {"path": path, "licence": licence}

    def events(self) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT 200")]

    # --------------------------------------------------------------- serving

    def active_info(self) -> dict[str, Any]:
        with self.db.transaction() as conn:
            a = self._active(conn)
            if not a:
                return {"active": None}
            g = conn.execute("SELECT * FROM generations WHERE gen=?",
                             (a["gen"],)).fetchone()
            tgt = conn.execute("SELECT release_id,version FROM releases WHERE id=?",
                               (g["release_pk"],)).fetchone()
            return {"active": dict(a), "release": dict(tgt),
                    "root": g["root"]}

    def serve(self, route: str) -> bytes:
        info = self.active_info()
        if not info.get("active"):
            raise GovError("NOT_FOUND", "尚无在线代次", 404)
        gdir = Path(info["root"])
        routes = json.loads((gdir / "routes.json").read_bytes())
        if route not in routes:
            raise GovError("NOT_FOUND", f"路由 {route} 不存在", 404)
        p = gdir / "artifacts" / routes[route]
        if not p.exists():
            raise GovError("NOT_FOUND", "产物缺失（代次不完整）", 404)
        return p.read_bytes()
