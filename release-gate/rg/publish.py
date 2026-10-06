"""Same-generation cut-over protocol for build artifacts + search index +
routes table, plus rollback.

Guarantees
----------
* The three artifacts switch as ONE generation (atomic symlink exchange while
  holding the write lock). Partial failure before the switch leaves the old
  generation fully serving; failure at/after the switch rolls the link back.
* The published material is byte-identical to the signed, dual-approved
  candidate manifest (staged hashes re-verified, HMAC checked).
* Duplicate idempotency keys return the same outcome; two different releases
  published concurrently serialize (one wins, the other gets 'conflict').
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import db as dbmod
from .approve import approval_state
from .evaluate import current_run, gate_status, load_live_manifest, now
from .fingerprint import (canonical, file_sha256, sign_manifest,
                           tree_checksum, verify_manifest)

# test hooks: e.g. {"after_switch": callable(conn, gen)}
FAULT_HOOK: dict = {}


class PublishError(RuntimeError):
    pass


def _root(store: str | Path) -> Path:
    root = Path(store)
    (root / "generations").mkdir(parents=True, exist_ok=True)
    return root


def _active_link(store) -> Path:
    return _root(store) / "active"


def current_generation(conn) -> int:
    row = conn.execute("SELECT generation FROM active WHERE id=1").fetchone()
    return row[0] if row else 0


def _verify_staged(staged: Path, manifest: dict) -> list[str]:
    errs = []
    by_path = {a["path"]: a["sha256"]
               for a in manifest.get("build", {}).get("artifacts", [])}
    for rel, want in by_path.items():
        # artifact paths in the manifest are site-relative (dist/...);
        # the immutable generation directory holds the dist contents directly
        local = rel[5:] if rel.startswith("dist/") else rel
        fp = staged / local
        if not fp.exists():
            errs.append(f"暂存缺少产物 {rel}")
        elif file_sha256(fp) != want:
            errs.append(f"暂存产物校验和不符 {rel}")
    return errs


def publish(conn, release_id: str, dist_dir: str | Path, secret: bytes,
            idempotency_key: str | None, store: str | Path,
            actor: str = "system") -> dict:
    """Run the reproducible commit protocol inside BEGIN IMMEDIATE."""
    dbmod.init_db(conn)
    idem = idempotency_key or f"release:{release_id}"

    conn.execute("BEGIN IMMEDIATE")
    try:
        dup = conn.execute("SELECT * FROM publish_intents WHERE idempotency_key=?",
                           (idem,)).fetchone()
        if dup:
            outcome = {"release_id": dup["release_id"],
                       "generation": dup["generation"],
                       "outcome": dup["outcome"],
                       "idempotency_key": dup["idempotency_key"],
                       "duplicate": True}
            conn.execute("INSERT INTO audit(ts, actor, action, release_id, "
                         "payload) VALUES (?,?,?,?,?)",
                         (now(), actor, "publish.duplicate", release_id,
                          json.dumps({"key": idem})))
            conn.execute("COMMIT")
            return outcome

        # idempotency by content: if THIS release already owns the active
        # generation, any further publish request is a repeat and returns it
        # instead of cutting a new generation.
        active_row = conn.execute(
            "SELECT g.generation, g.release_id FROM active a "
            "JOIN generations g ON g.generation = a.generation WHERE a.id=1"
        ).fetchone()
        if active_row and active_row["release_id"] == release_id:
            conn.execute("INSERT INTO audit(ts, actor, action, release_id, "
                         "payload) VALUES (?,?,?,?,?)",
                         (now(), actor, "publish.duplicate", release_id,
                          json.dumps({"key": idem, "already_active": True})))
            conn.execute("COMMIT")
            return {"release_id": release_id,
                    "generation": active_row["generation"],
                    "outcome": "published", "idempotency_key": idem,
                    "duplicate": True}

        gate = gate_status(conn, release_id)
        if not gate.get("evaluated"):
            raise PublishError("发布前必须先评估")
        if gate["blockers"]:
            raise PublishError("仍有阻断项: "
                               + ", ".join(b["name"] for b in gate["blockers"]))
        if gate["state"] != "committed":
            raise PublishError("评估发生漂移，必须重新评估后签准")

        appr = approval_state(conn, release_id)
        if not appr["approved"]:
            raise PublishError(
                f"需要两人签准（当前有效签准 "
                f"{len({v['approver'] for v in appr['valid_approvals']})}/2）"
                + ("；存在因变更失效的旧签准" if appr["stale_approvals"] else ""))

        rel = conn.execute("SELECT * FROM releases WHERE id=?",
                           (release_id,)).fetchone()
        manifest = json.loads(rel["manifest_json"])
        run = current_run(conn, release_id)

        # --- phase 1: stage (copy dist into immutable generation dir) ------
        gen_no = current_generation(conn) + 1
        staged = _root(store) / "generations" / f"gen-{gen_no}"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(dist_dir, staged)

        # --- phase 2: verify staged artifacts vs manifest ------------------
        errs = _verify_staged(staged, manifest)
        if errs:
            shutil.rmtree(staged, ignore_errors=True)
            raise PublishError("暂存校验失败: " + "; ".join(errs))

        # --- phase 3: sign manifest and store the signed envelope ----------
        signature = sign_manifest(manifest, secret)
        envelope = {"manifest": manifest, "signature": signature,
                    "release_id": release_id,
                    "aggregate_fingerprint": run["aggregate_fprint"],
                    "gen": gen_no}
        (staged / "manifest.signed.json").write_bytes(canonical(envelope))

        # --- phase 4: same-generation atomic cut-over ----------------------
        link = _active_link(store)
        prev_link = link.with_name(link.name + ".prev")
        if prev_link.is_symlink() or prev_link.exists():
            prev_link.unlink()
        if link.is_symlink():
            # atomically move the current link aside so it can be restored
            os.rename(link, prev_link)
            prev_target = os.readlink(prev_link)
        else:
            prev_target = None

        def restore_link():
            if prev_target is not None:
                rb = link.with_name(link.name + ".rb")
                if rb.is_symlink() or rb.exists():
                    rb.unlink()
                os.symlink(prev_target, rb)
                os.replace(rb, link)
            elif link.is_symlink() or link.exists():
                link.unlink()
            if prev_link.is_symlink() or prev_link.exists():
                prev_link.unlink()

        os.symlink(str(staged), link.with_name(link.name + ".new"))
        os.replace(link.with_name(link.name + ".new"), link)

        try:
            hook = FAULT_HOOK.get("after_switch")
            if hook:
                hook(conn, gen_no, staged)  # may raise -> rollback below

            # --- phase 5: post-switch verification --------------------------
            post_errs = _post_switch_verify(link, manifest, signature, secret)
            if post_errs:
                raise PublishError(
                    "切换后校验失败，已回滚: " + "; ".join(post_errs))
        except Exception:
            # ANY failure at/after the switch restores the previous intact
            # generation; the new staged dir is discarded, nothing is committed
            restore_link()
            raise
        if prev_link.is_symlink() or prev_link.exists():
            prev_link.unlink()

        cur = conn.execute(
            "INSERT INTO generations(release_id, generation, manifest_signed, "
            "manifest_sha, artifact_dir, switched_at, state, publish_run_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (release_id, gen_no,
             canonical(envelope).decode(),
             __import__("hashlib").sha256(canonical(manifest)).hexdigest(),
             str(staged), now(), "active", run["id"]))
        gen_id = cur.lastrowid
        conn.execute("UPDATE generations SET state='superseded' WHERE state='active' AND id<>?",
                     (gen_id,))
        conn.execute("INSERT INTO active(id, generation, release_id, switched_at) "
                     "VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "generation=excluded.generation, release_id=excluded.release_id, "
                     "switched_at=excluded.switched_at",
                     (gen_no, release_id, now()))
        conn.execute("UPDATE releases SET status='published' WHERE id=?",
                     (release_id,))
        conn.execute(
            "INSERT INTO publish_intents(idempotency_key, release_id, "
            "generation, outcome, created_at) VALUES (?,?,?,?,?)",
            (idem, release_id, gen_no, "published", now()))
        conn.execute("INSERT INTO audit(ts, actor, action, release_id, payload) "
                     "VALUES (?,?,?,?,?)",
                     (now(), actor, "publish", release_id,
                      json.dumps({"generation": gen_no})))
        conn.execute("COMMIT")
        return {"release_id": release_id, "generation": gen_no,
                "outcome": "published", "duplicate": False,
                "idempotency_key": idem}
    except Exception:
        # A rejected/aborted attempt is fully rolled back and never reserves
        # the idempotency key: the client may fix the gate and retry with the
        # same key. Only a *successful* cut-over dedupes.
        conn.execute("ROLLBACK")
        raise


def _post_switch_verify(link: Path, manifest: dict, signature: str,
                        secret: bytes) -> list[str]:
    errs = []
    env_path = link / "manifest.signed.json"
    if not env_path.exists():
        return ["活动代次缺少签名清单"]
    env = json.loads(env_path.read_text())
    if not verify_manifest(env["manifest"], env.get("signature", ""), secret):
        errs.append("签名校验失败")
    if canonical(env["manifest"]) != canonical(manifest):
        errs.append("公开产物清单与签准 manifest 不一致")
    errs += _verify_staged(link, manifest)
    return errs


def rollback(conn, store: str | Path, secret: bytes, actor: str = "ops",
             target_generation: int | None = None) -> dict:
    """Roll back to a previous intact generation. Old/current generation
    content is never deleted, so service continuity is preserved."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if target_generation is None:
            row = conn.execute(
                "SELECT generation FROM generations WHERE state='superseded' "
                "ORDER BY generation DESC LIMIT 1").fetchone()
            if not row:
                raise PublishError("没有可回滚的上一代")
            target_generation = row[0]
        gen = conn.execute("SELECT * FROM generations WHERE generation=?",
                           (target_generation,)).fetchone()
        if not gen:
            raise PublishError(f"代次 {target_generation} 不存在")

        target_dir = Path(gen["artifact_dir"])
        env = json.loads((target_dir / "manifest.signed.json").read_text())
        if not verify_manifest(env["manifest"], env.get("signature", ""), secret):
            raise PublishError("回滚目标签名校验失败")
        errs = _verify_staged(target_dir, env["manifest"])
        if errs:
            raise PublishError("回滚目标不完整: " + "; ".join(errs))

        cur_gen = current_generation(conn)
        link = _active_link(store)
        prev = os.readlink(link) if link.is_symlink() else None
        tmp = link.with_name(link.name + ".rb")
        os.symlink(str(target_dir), tmp)
        os.replace(tmp, link)

        post = _post_switch_verify(link, env["manifest"],
                                   env.get("signature", ""), secret)
        if post:
            if prev:
                t2 = link.with_name(link.name + ".rb2")
                os.symlink(prev, t2)
                os.replace(t2, link)
            raise PublishError("回滚切换后校验失败，已恢复: " + "; ".join(post))

        conn.execute("UPDATE generations SET state='rolled_back' WHERE generation=?",
                     (cur_gen,))
        conn.execute("UPDATE generations SET state='active' WHERE generation=?",
                     (target_generation,))
        conn.execute("INSERT INTO active(id, generation, release_id, switched_at) "
                     "VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "generation=excluded.generation, release_id=excluded.release_id, "
                     "switched_at=excluded.switched_at",
                     (target_generation, gen["release_id"], now()))
        conn.execute("INSERT INTO audit(ts, actor, action, release_id, payload) "
                     "VALUES (?,?,?,?,?)",
                     (now(), actor, "rollback", gen["release_id"],
                      json.dumps({"from": cur_gen, "to": target_generation})))
        conn.execute("COMMIT")
        return {"rolled_back_to": target_generation,
                "release_id": gen["release_id"], "from": cur_gen}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def public_consistency(conn, store: str | Path, secret: bytes) -> dict:
    """Final acceptance: what the public sees == signed manifest."""
    link = _active_link(store)
    if not link.exists():
        return {"consistent": False, "reason": "尚无活动代次"}
    env = json.loads((link / "manifest.signed.json").read_text())
    ok_sig = verify_manifest(env["manifest"], env.get("signature", ""), secret)
    errs = _verify_staged(link, env["manifest"])
    gen = current_generation(conn)
    return {
        "consistent": ok_sig and not errs and env.get("gen") == gen,
        "active_generation": gen,
        "signed_gen": env.get("gen"),
        "signature_ok": ok_sig,
        "artifact_errors": errs,
        "release_id": env.get("release_id"),
    }
