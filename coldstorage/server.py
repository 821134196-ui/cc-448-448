"""HTTP 服务器入口（标准库 ThreadingHTTPServer）。"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import webapi
from . import config


class Handler(BaseHTTPRequestHandler):
    server_version = "ColdStorage/1.0"

    def _handle_static(self, path: str) -> bool:
        if path == "/" or path.startswith("/static/"):
            webapi.serve_file(self, path)
            return True
        if path.startswith("/photos/"):
            rel = path[len("/photos/"):]
            target = (config.PHOTO_DIR / rel).resolve()
            if not str(target).startswith(str(config.PHOTO_DIR.resolve())) \
                    or not target.is_file():
                self.send_json(404, {"error": "not found"})
                return True
            ctype = ("image/svg+xml" if target.suffix == ".svg"
                     else "application/octet-stream")
            self.send_bytes(200, target.read_bytes(), ctype)
            return True
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if self._handle_static(path):
            return
        webapi.dispatch(self)

    def do_POST(self):
        webapi.dispatch(self)

    def send_json(self, status: int, payload) -> None:
        import json
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def send_bytes(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        extra = getattr(self, "extra_cookie", None)
        if extra:
            self.send_header("Set-Cookie", extra)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 安静一点
        import sys
        sys.stderr.write("[http] " + fmt % args + "\n")


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    config.ensure_dirs()
    return ThreadingHTTPServer((host, port), Handler)
