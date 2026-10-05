"""Run the release gate API + console:

    python -m docgov.server --db ./data/gate.db --data ./data --port 8080
"""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer

from .api import Handler
from .governor import Governor


def make_server(host: str, port: int, db: str, data: str,
                author_key: str = "demo-author-key",
                approver_key: str = "demo-approver-key") -> ThreadingHTTPServer:
    gov = Governor(db, data, author_key=author_key, approver_key=approver_key)

    class GovHandler(Handler):
        pass

    GovHandler.gov = gov
    httpd = ThreadingHTTPServer((host, port), GovHandler)
    httpd.gov = gov  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="./data/gate.db")
    ap.add_argument("--data", default="./data")
    args = ap.parse_args()
    httpd = make_server(args.host, args.port, args.db, args.data)
    print(f"release gate on http://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
