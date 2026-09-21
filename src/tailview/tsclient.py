"""Thin wrapper around the local `tailscale` CLI.

Everything the dashboard shows comes from commands run against the daemon on
this machine. Nothing here talks to the network or to the Tailscale API, so the
tool works on an air-gapped laptop exactly as it does on a connected one.

Every call is best-effort. A command that is missing, unsupported by the
installed version, or refused for lack of permission returns a `Result` with
`ok=False` and a reason the interface can show the person, rather than raising.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_TIMEOUT = 15.0
NETCHECK_TIMEOUT = 40.0

# Phrases the daemon uses when the calling user is not allowed to talk to it.
_PERMISSION_HINTS = (
    "permission denied",
    "access denied",
    "operation not permitted",
    "connect: permission denied",
    "you must be root",
    "is not the operator",
    "--operator",
)


@dataclass
class Result:
    """Outcome of one CLI invocation."""

    command: List[str]
    ok: bool
    stdout: str = ""
    stderr: str = ""
    data: Any = None
    reason: str = ""
    kind: str = ""  # "", "missing", "permission", "unsupported", "timeout", "error"
    duration_ms: int = 0
    at: float = field(default_factory=time.time)

    def summary(self) -> Dict[str, Any]:
        return {
            "command": " ".join(self.command),
            "ok": self.ok,
            "reason": self.reason,
            "kind": self.kind,
            "durationMs": self.duration_ms,
            "at": self.at,
        }


def _classify(stderr: str, returncode: int) -> tuple[str, str]:
    low = stderr.lower()
    if any(hint in low for hint in _PERMISSION_HINTS):
        return "permission", stderr.strip().splitlines()[0] if stderr.strip() else "Permission denied"
    if "unknown subcommand" in low or "unknown command" in low or "flag provided but not defined" in low:
        return "unsupported", "This tailscale version does not support the command"
    first = stderr.strip().splitlines()[0] if stderr.strip() else f"exited with status {returncode}"
    return "error", first


class TailscaleClient:
    """Runs `tailscale` subcommands and decodes their output."""

    def __init__(self, binary: Optional[str] = None):
        self.binary = binary or os.environ.get("TAILSCALE_BIN") or "tailscale"
        self._resolved: Optional[str] = shutil.which(self.binary)

    @property
    def available(self) -> bool:
        return self._resolved is not None

    @property
    def path(self) -> Optional[str]:
        return self._resolved

    def run(self, args: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> Result:
        command = [self.binary, *args]
        if self._resolved is None:
            # Re-check: tailscale may have been installed since startup.
            self._resolved = shutil.which(self.binary)
        if self._resolved is None:
            return Result(
                command=command,
                ok=False,
                kind="missing",
                reason=f"{self.binary} was not found on PATH",
            )

        started = time.monotonic()
        try:
            proc = subprocess.run(
                [self._resolved, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Result(
                command=command,
                ok=False,
                kind="timeout",
                reason=f"No response within {timeout:.0f}s",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except OSError as exc:
            return Result(command=command, ok=False, kind="error", reason=str(exc))

        duration_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0:
            kind, reason = _classify(proc.stderr, proc.returncode)
            return Result(
                command=command,
                ok=False,
                stdout=proc.stdout,
                stderr=proc.stderr,
                kind=kind,
                reason=reason,
                duration_ms=duration_ms,
            )
        return Result(
            command=command,
            ok=True,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=duration_ms,
        )

    def run_json(self, args: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> Result:
        result = self.run(args, timeout=timeout)
        if not result.ok:
            return result
        text = result.stdout.strip()
        if not text:
            result.data = None
            return result
        try:
            result.data = json.loads(text)
        except json.JSONDecodeError as exc:
            result.ok = False
            result.kind = "error"
            result.reason = f"Could not read JSON output: {exc}"
        return result

    # -- the individual data sources -------------------------------------

    def metrics(self) -> Result:
        return self.run(["metrics", "print"])

    def status(self) -> Result:
        return self.run_json(["status", "--json"])

    def netcheck(self) -> Result:
        return self.run_json(["netcheck", "--format=json"], timeout=NETCHECK_TIMEOUT)

    def version(self) -> Result:
        return self.run_json(["version", "--json"])

    def prefs(self) -> Result:
        return self.run_json(["debug", "prefs"])

    def derp_map(self) -> Result:
        return self.run_json(["debug", "derp-map"], timeout=25.0)

    def dns_status(self) -> Result:
        result = self.run_json(["dns", "status", "--json"])
        if not result.ok and result.kind in ("unsupported", "error"):
            plain = self.run(["dns", "status"])
            if plain.ok:
                plain.data = {"text": plain.stdout}
                return plain
        return result

    def serve_status(self) -> Result:
        result = self.run_json(["serve", "status", "--json"])
        if not result.ok and result.kind in ("unsupported", "error"):
            plain = self.run(["serve", "status"])
            if plain.ok:
                plain.data = {"text": plain.stdout}
                return plain
        return result

    def lock_status(self) -> Result:
        result = self.run(["lock", "status"])
        if result.ok:
            result.data = {"text": result.stdout.strip()}
        return result
