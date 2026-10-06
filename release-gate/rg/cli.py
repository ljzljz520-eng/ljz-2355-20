"""Command-line driver (also used by demos).

Commands:
  init     create release from a laid-out candidate site
  evaluate run the long-transaction checklist
  waiver   record an exemption for a blocking item
  approve  dual-control sign-off (two distinct approvers)
  publish  same-generation cut-over (idempotent)
  rollback switch back to previous generation
  verify   public materials == signed manifest
  gate     print blockers / status as JSON
  serve    run the HTTP UI/API
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .publish import PublishError
from .server import serve
from .service import ConflictError, Service

DEFAULT_SECRET = b"docrelease-dev-secret"


def make_service(args) -> Service:
    return Service(args.db, args.store, args.secret.encode())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rg", description=__doc__)
    ap.add_argument("--db", default=os.environ.get("RG_DB", "./.rg/registry.db"))
    ap.add_argument("--store", default=os.environ.get("RG_STORE", "./.rg/store"))
    ap.add_argument("--secret", default=os.environ.get("RG_SECRET",
                                                       DEFAULT_SECRET.decode()))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("release_id")
    p.add_argument("site_dir")
    p = sub.add_parser("evaluate"); p.add_argument("release_id")
    p.add_argument("--actor", default="qa")
    p = sub.add_parser("waiver"); p.add_argument("release_id")
    p.add_argument("item"); p.add_argument("reason"); p.add_argument("--approver", default="qa-lead")
    p = sub.add_parser("approve"); p.add_argument("release_id")
    p.add_argument("approver")
    p = sub.add_parser("publish"); p.add_argument("release_id")
    p.add_argument("dist_dir", nargs="?"); p.add_argument("--key")
    p.add_argument("--actor", default="release-manager")
    p = sub.add_parser("rollback"); p.add_argument("--to", type=int)
    p = sub.add_parser("verify")
    p = sub.add_parser("gate"); p.add_argument("release_id")
    p = sub.add_parser("list")
    p = sub.add_parser("audit"); p.add_argument("release_id", nargs="?")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8080)

    args = ap.parse_args(argv)
    svc = make_service(args)

    try:
        if args.cmd == "init":
            m = svc.create_release(args.release_id, args.site_dir)
            print(f"候选 manifest 已建立: {args.release_id}（{len(m['pages'])} 页）")
        elif args.cmd == "evaluate":
            r = svc.evaluate(args.release_id, args.actor)
            print(f"run #{r['run_id']} 状态={r['state']} passed={r['passed']} "
                  f"agg={r['aggregate_fingerprint'][:16]}…")
            for it in r["items"]:
                print(f"  [{it['status']:7}] {it['name']:20} {it['detail']}")
        elif args.cmd == "waiver":
            print(svc.waiver(args.release_id, args.item, args.reason,
                             args.approver))
        elif args.cmd == "approve":
            print(svc.approve(args.release_id, args.approver))
        elif args.cmd == "publish":
            row = svc.conn().execute(
                "SELECT source_tree FROM releases WHERE id=?",
                (args.release_id,)).fetchone()
            dist = args.dist_dir or str(Path(row[0]).parent / "dist")
            r = svc.publish(args.release_id, dist, args.key, args.actor)
            print(json.dumps(r, ensure_ascii=False))
        elif args.cmd == "rollback":
            print(json.dumps(svc.rollback(args.to), ensure_ascii=False))
        elif args.cmd == "verify":
            print(json.dumps(svc.verify_public(), ensure_ascii=False, indent=2))
        elif args.cmd == "gate":
            print(json.dumps(svc.gate(args.release_id), ensure_ascii=False,
                             indent=2))
        elif args.cmd == "list":
            print(json.dumps(svc.list_releases(), ensure_ascii=False, indent=2))
        elif args.cmd == "audit":
            print(json.dumps(svc.audit(args.release_id), ensure_ascii=False,
                             indent=2))
        elif args.cmd == "serve":
            httpd = serve(svc, args.port)
            print(f"发布门禁 UI: http://127.0.0.1:{args.port}/")
            try:
                while True:
                    import time; time.sleep(1)
            except KeyboardInterrupt:
                httpd.shutdown()
        return 0
    except (PublishError, ConflictError, ValueError, KeyError) as e:
        print(f"错误: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
