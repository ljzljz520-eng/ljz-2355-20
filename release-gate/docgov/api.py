"""Stdlib HTTP API for the release gate.

The same handler can be invoked in-process (tests) or via ``make_server``.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from .canonical import sign
from .errors import GovError
from .governor import Governor


class Handler(BaseHTTPRequestHandler):
    gov: Governor = None  # injected by make_app

    def log_message(self, *args):  # quiet
        pass

    # ------------------------------------------------------------- plumbing
    def _send(self, obj: Any, status: int = 200,
              content_type: str = "application/json") -> None:
        if content_type == "application/json":
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            body = obj if isinstance(obj, bytes) else str(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",
                         content_type + ("; charset=utf-8"
                                        if content_type == "application/json"
                                        else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise GovError("BAD_JSON", "请求体不是合法 JSON", 400)

    def _handle(self, method: str):
        try:
            return self._route(method)
        except GovError as e:
            self._send(e.to_dict(), e.status)
        except FileNotFoundError as e:
            self._send({"error": "NOT_FOUND", "message": str(e)}, 404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    # ---------------------------------------------------------------- routes
    def _route(self, method: str):
        u = urlparse(self.path)
        p, q = u.path.strip("/"), parse_qs(u.query)
        gov = self.gov

        if method == "GET" and p in ("", "index.html"):
            return self._send(open(_web("index.html"), "rb").read(), 200,
                              "text/html")
        if method == "GET" and p == "app.js":
            return self._send(open(_web("app.js"), "rb").read(), 200,
                              "application/javascript")

        if method == "GET" and p == "api/releases":
            return self._send(gov.list_releases())
        if method == "GET" and p == "api/active":
            return self._send(gov.active_info())
        if method == "GET" and p == "api/events":
            return self._send({"events": gov.events()})

        parts = p.split("/")
        if len(parts) == 4 and parts[:2] == ["api", "releases"]:
            pk, action = parts[2], parts[3]
            if method == "GET" and action == "status":
                return self._send(gov.get_release(pk))
            if method == "POST" and action == "evaluate":
                b = self._body()
                return self._send(gov.evaluate(pk, b.get("actor", "ci")))
            if method == "POST" and action == "waiver":
                b = self._body()
                return self._send(gov.grant_waiver(
                    pk, b["code"], b.get("reason", ""),
                    b.get("granted_by", "manager")))
            if method == "POST" and action == "approve":
                b = self._body()
                return self._send(gov.approve(pk, b.get("approver", "manager")))
            if method == "POST" and action == "prepare":
                b = self._body()
                return self._send(gov.prepare(pk, b.get("actor", "release-manager")))
            if method == "POST" and action == "publish":
                b = self._body()
                return self._send(gov.publish(
                    pk, b.get("idem_token"), b.get("actor", "release-manager")))

        if method == "POST" and p == "api/releases/submit":
            b = self._body()
            return self._send(gov.submit(b["manifest"], b["signature"],
                                         b.get("actor", "author")))
        if method == "POST" and p == "api/dev/sign":
            # Demo only: sign a manifest with the author key.
            b = self._body()
            return self._send({"signature": sign(b["manifest"], gov.author_key)})
        if method == "POST" and p == "api/feedback":
            b = self._body()
            return self._send(gov.add_feedback(b["page_path"], b["severity"],
                                               b["summary"]))
        if method == "POST" and p == "api/asset-licence":
            b = self._body()
            return self._send(gov.set_asset_licence(b["path"], b["licence"]))
        if method == "POST" and p == "api/rollback":
            b = self._body()
            return self._send(gov.rollback(b["gen"], b.get("actor", "release-manager")))
        if method == "GET" and p.startswith("api/serve/"):
            route = p[len("api/serve/"):]
            return self._send(gov.serve(route), 200, "application/octet-stream")

        self._send({"error": "NOT_FOUND", "message": self.path}, 404)


def _web(name: str) -> str:
    import os
    here = os.path.dirname(__file__)
    return os.path.join(here, "web", name)
