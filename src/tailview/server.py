"""The local HTTP server: a small JSON API plus the dashboard's static files.

The server binds to the loopback interface by default and checks the Host
header on every request. That check is what stops a page you happen to have
open in another tab from reaching this server through a rebound DNS name — the
browser will happily connect to 127.0.0.1 on behalf of any site that asks.
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .collector import Collector

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


class TailviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, collector: Collector, options: Dict[str, Any]):
        self.collector = collector
        self.options = options
        self.allowed_hosts = set(options.get("allowed_hosts") or [])
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "tailview"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        if self.server.options.get("verbose"):
            super().log_message(fmt, *args)

    @property
    def collector(self) -> Collector:
        return self.server.collector

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if name in _LOOPBACK_HOSTS or name in self.server.allowed_hosts:
            return True
        return name.endswith(".localhost")

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=_jsonable).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _query(self) -> Tuple[str, Dict[str, list]]:
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    # -- routing ---------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._send(HTTPStatus.FORBIDDEN, b"Host not allowed\n")
            return
        path, _ = self._query()
        if path == "/api/netcheck":
            self.collector.request_netcheck()
            self._send_json({"queued": True})
            return
        self._send(HTTPStatus.NOT_FOUND, b"Not found\n")

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._send(
                HTTPStatus.FORBIDDEN,
                b"tailview only answers requests addressed to localhost.\n",
            )
            return

        path, query = self._query()

        if path == "/api/state":
            self._send_json(self.collector.state())
            return

        if path == "/api/series":
            window = query.get("window", [None])[0]
            seconds = None
            if window and window != "all":
                try:
                    seconds = float(window)
                except ValueError:
                    seconds = None
            self._send_json(self.collector.series(seconds))
            return

        if path == "/api/raw":
            self._send(HTTPStatus.OK, self.collector.raw_metrics().encode("utf-8"))
            return

        if path == "/api/events":
            self._stream_events()
            return

        self._serve_static(path)

    # -- server-sent events ---------------------------------------------

    def _stream_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        last_tick = -1
        try:
            while True:
                state = self.collector.state()
                if state["tick"] != last_tick:
                    last_tick = state["tick"]
                    payload = json.dumps(state, default=_jsonable)
                    self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                self.collector.wait_for_tick(last_tick, timeout=20.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # -- static files ----------------------------------------------------

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        relative = os.path.normpath(path.lstrip("/"))
        if relative.startswith("..") or os.path.isabs(relative):
            self._send(HTTPStatus.FORBIDDEN, b"Forbidden\n")
            return
        full = os.path.join(WEB_ROOT, relative)
        if not os.path.isfile(full):
            self._send(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        content_type, _ = mimetypes.guess_type(full)
        with open(full, "rb") as handle:
            body = handle.read()
        self._send(
            HTTPStatus.OK,
            body,
            content_type or "application/octet-stream",
            extra={
                # Scripts stay locked to this server. Inline styles are
                # allowed because series colours are set per element from the
                # data; everything rendered into the page is escaped first.
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                    "form-action 'none'; frame-ancestors 'none'"
                )
            },
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def find_free_port(host: str, preferred: int, attempts: int = 40) -> int:
    """Return `preferred` if it is free, else the next port that is."""
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise OSError(f"No free port found between {preferred} and {preferred + attempts - 1}")


def serve(
    collector: Collector,
    host: str = "127.0.0.1",
    port: int = 8829,
    verbose: bool = False,
    allowed_hosts: Optional[list] = None,
) -> Tuple[TailviewServer, threading.Thread]:
    options = {"verbose": verbose, "allowed_hosts": allowed_hosts or []}
    server = TailviewServer((host, port), Handler, collector, options)
    thread = threading.Thread(target=server.serve_forever, name="tailview-http", daemon=True)
    thread.start()
    return server, thread


__all__ = ["serve", "find_free_port", "TailviewServer", "Handler", "WEB_ROOT"]
