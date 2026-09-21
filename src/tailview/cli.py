"""Command line entry point: start collecting, start serving, print the URL."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import webbrowser

from .collector import Collector
from .server import find_free_port, serve
from .tsclient import TailscaleClient

DEFAULT_PORT = 8829

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
YELLOW = "\033[33m"
RED = "\033[31m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _style(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _supports_color() else text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tailview",
        description="A local dashboard for your Tailscale node's own metrics.",
    )
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default {DEFAULT_PORT}; the next free port is used if taken)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind (default 127.0.0.1, loopback only)")
    parser.add_argument("-i", "--interval", type=float, default=3.0,
                        help="seconds between metric polls (default 3)")
    parser.add_argument("--history", type=float, default=60.0,
                        help="minutes of history to keep in memory (default 60)")
    parser.add_argument("--netcheck-interval", type=float, default=300.0,
                        help="seconds between DERP netchecks (default 300)")
    parser.add_argument("--no-netcheck", action="store_true",
                        help="never run netcheck; the DERP latency panel stays empty")
    parser.add_argument("--tailscale", default=None,
                        help="path to the tailscale binary (default: found on PATH)")
    parser.add_argument("--demo", action="store_true",
                        help="run on generated sample data, without contacting a daemon")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window on start")
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit binding to a non-loopback address")
    parser.add_argument("--allowed-host", action="append", default=[],
                        help="extra Host header value to accept (repeatable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not args.allow_remote:
        print(
            _style("tailview binds to loopback only unless you ask otherwise.", RED),
            file=sys.stderr,
        )
        print(
            f"  The dashboard has no authentication, so anyone who can reach {args.host} "
            f"could read your node's metrics.\n"
            f"  Re-run with --allow-remote if that is what you want.",
            file=sys.stderr,
        )
        return 2

    if args.demo:
        from .demo import DemoClient

        client = DemoClient()
    else:
        client = TailscaleClient(args.tailscale)
        if not client.available:
            binary = args.tailscale or os.environ.get("TAILSCALE_BIN") or "tailscale"
            print(_style(f"Could not find `{binary}` on PATH.", RED), file=sys.stderr)
            print(
                "  Install Tailscale, pass --tailscale /path/to/tailscale, "
                "or try the dashboard on sample data with --demo.",
                file=sys.stderr,
            )
            return 2

    collector = Collector(
        client,
        interval=args.interval,
        history_seconds=args.history * 60.0,
        netcheck_interval=args.netcheck_interval,
        run_netcheck=not args.no_netcheck,
    )
    collector.start()

    try:
        port = find_free_port(args.host, args.port)
    except OSError as exc:
        print(_style(str(exc), RED), file=sys.stderr)
        return 1

    server, _thread = serve(
        collector,
        host=args.host,
        port=port,
        verbose=args.verbose,
        allowed_hosts=args.allowed_host,
    )

    display_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{display_host}:{port}/"

    print()
    print(f"  {_style('tailview', BOLD)} is reading your node's metrics")
    print(f"  {_style(url, BOLD)}")
    if port != args.port:
        print(_style(f"  (port {args.port} was busy)", DIM))
    if args.demo:
        print(_style("  Sample data — no daemon was contacted.", YELLOW))
    print(_style("  Press Ctrl-C to stop.", DIM))
    print()

    # Give the first poll a moment to land so the page opens with data on it.
    if not args.no_browser:
        def open_later() -> None:
            time.sleep(0.8)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=open_later, daemon=True).start()

    stopping = threading.Event()

    def handle_signal(_signum, _frame) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stopping.is_set():
            stopping.wait(0.5)
    finally:
        print(_style("\n  Stopping.", DIM))
        collector.stop()
        server.shutdown()
        server.server_close()
    return 0
