"""A local HTTP server that stands in for WhatsApp, Telegram or a speech-to-text API in tests.

Routes map (method, path regex) to a handler that gets the recorded request and returns
(status, body); a dict body is sent as JSON, bytes as they are. Every request is recorded.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes

    @property
    def json(self):
        return json.loads(self.body or b"{}")

    def form_field(self, name: str) -> bytes | None:
        """A field of a multipart/form-data body (the file part's content for file fields)."""
        boundary = re.search(r"boundary=([^;]+)", self.headers.get("Content-Type", ""))
        if not boundary:
            return None
        for part in self.body.split(b"--" + boundary.group(1).strip('"').encode()):
            head, _, content = part.partition(b"\r\n\r\n")
            if f'name="{name}"'.encode() in head:
                return content.rsplit(b"\r\n", 1)[0]
        return None


@dataclass
class FakeHTTP:
    routes: list = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)

    def route(self, method: str, pattern: str, handler) -> FakeHTTP:
        self.routes.append((method, re.compile(pattern), handler))
        return self

    def start(self) -> FakeHTTP:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                url = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                rec = Recorded(self.command, url.path, parse_qs(url.query), dict(self.headers), body)
                fake.requests.append(rec)
                for method, pattern, handler in fake.routes:
                    if method == self.command and pattern.fullmatch(url.path):
                        status, out = handler(rec)
                        break
                else:
                    status, out = 404, {"error": {"message": f"no route for {self.command} {url.path}"}}
                data = json.dumps(out).encode() if isinstance(out, (dict, list)) else out
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if isinstance(out, (dict, list)) else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def calls(self, method: str, pattern: str) -> list[Recorded]:
        rx = re.compile(pattern)
        return [r for r in self.requests if r.method == method and rx.fullmatch(r.path)]
