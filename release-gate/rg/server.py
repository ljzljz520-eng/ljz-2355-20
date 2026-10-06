"""Zero-dependency stdlib HTTP API + static frontend."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .publish import PublishError
from .service import ConflictError, Service

FRONTEND = Path(__file__).parent / "frontend"


class Handler(BaseHTTPRequestHandler):
    service: Service = None  # type: ignore

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _error(self, code, msg):
        self._send(code, {"error": msg})

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._static("index.html")
        if p.startswith("/static/"):
            return self._static(p.split("/static/", 1)[1])
        if p == "/api/releases":
            return self._send(200, self.service.list_releases())
        if p.startswith("/api/gate/"):
            rid = p.rsplit("/", 1)[-1]
            try:
                return self._send(200, self.service.gate(rid))
            except KeyError:
                return self._error(404, "release not found")
        if p == "/api/audit":
            rid = parse_qs(u.query).get("release")
            return self._send(200, self.service.audit(rid[0] if rid else None))
        if p == "/api/public":
            return self._send(200, self.service.verify_public())
        return self._error(404, "not found")

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            b = self._body()
            if p == "/api/releases":
                m = self.service.create_release(b["release_id"], b["site_dir"])
                return self._send(201, {"release_id": b["release_id"],
                                        "pages": len(m["pages"])})
            if p.startswith("/api/evaluate/"):
                rid = p.rsplit("/", 1)[-1]
                return self._send(200, self.service.evaluate(
                    rid, b.get("actor", "system")))
            if p.startswith("/api/waiver/"):
                rid = p.rsplit("/", 1)[-1]
                return self._send(201, self.service.waiver(
                    rid, b["item"], b["reason"], b.get("approver", "qa")))
            if p.startswith("/api/approve/"):
                rid = p.rsplit("/", 1)[-1]
                return self._send(200, self.service.approve(
                    rid, b["approver"]))
            if p.startswith("/api/publish/"):
                rid = p.rsplit("/", 1)[-1]
                return self._send(200, self.service.publish(
                    rid, b["dist_dir"], b.get("idempotency_key"),
                    b.get("actor", "system")))
            if p == "/api/rollback":
                return self._send(200, self.service.rollback(
                    b.get("target_generation"), b.get("actor", "ops")))
            return self._error(404, "not found")
        except ConflictError as e:
            self._error(409, str(e))
        except PublishError as e:
            self._error(412, str(e))
        except KeyError:
            self._error(404, "release not found")
        except ValueError as e:
            self._error(400, str(e))
        except Exception as e:  # noqa
            self._error(500, f"{type(e).__name__}: {e}")

    def _static(self, name: str):
        fp = (FRONTEND / name).resolve()
        if not str(fp).startswith(str(FRONTEND.resolve())) or not fp.exists():
            return self._error(404, "not found")
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else \
                "application/javascript; charset=utf-8"
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(service: Service, port: int = 8080) -> ThreadingHTTPServer:
    Handler.service = service
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
