"""Parser for the Prometheus text exposition format.

`tailscale metrics print` speaks this format, so this module is the only thing
standing between the daemon's output and the dashboard. It is deliberately
tolerant: a line it cannot make sense of is skipped rather than raised, because
a single malformed sample should never blank the whole dashboard.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, NamedTuple, Optional

_SAMPLE_RE = re.compile(
    r"""^
    (?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)      # metric name
    (?:\{(?P<labels>.*)\})?                  # optional label set
    \s+
    (?P<value>[^\s]+)                        # value
    (?:\s+(?P<timestamp>-?\d+))?             # optional timestamp (ms)
    \s*$""",
    re.VERBOSE,
)

_LABEL_RE = re.compile(
    r"""([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*,?\s*"""
)

_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}


class Sample(NamedTuple):
    """One parsed line of exposition format."""

    name: str
    labels: Dict[str, str]
    value: float
    key: str
    help: str
    type: str


def _unescape(raw: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_labels(raw: Optional[str]) -> Dict[str, str]:
    if not raw or not raw.strip():
        return {}
    labels: Dict[str, str] = {}
    for name, value in _LABEL_RE.findall(raw):
        labels[name] = _unescape(value)
    return labels


def parse_value(raw: str) -> Optional[float]:
    lowered = raw.lower()
    if lowered == "nan":
        return None
    if lowered in ("+inf", "inf"):
        return math.inf
    if lowered == "-inf":
        return -math.inf
    try:
        return float(raw)
    except ValueError:
        return None


def series_key(name: str, labels: Dict[str, str]) -> str:
    """Canonical, stable identifier for one series.

    Labels are sorted so the same series always produces the same key, which is
    what lets the history buffer line samples up across polls.
    """
    if not labels:
        return name
    inner = ",".join(f'{k}="{labels[k]}"' for k in sorted(labels))
    return f"{name}{{{inner}}}"


def parse(text: str) -> List[Sample]:
    """Parse an exposition document into samples, preserving HELP and TYPE."""
    helps: Dict[str, str] = {}
    types: Dict[str, str] = {}
    samples: List[Sample] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line[1:].strip().split(None, 2)
            if len(parts) >= 3 and parts[0] == "HELP":
                helps[parts[1]] = parts[2]
            elif len(parts) >= 3 and parts[0] == "TYPE":
                types[parts[1]] = parts[2]
            continue

        match = _SAMPLE_RE.match(line)
        if not match:
            continue
        value = parse_value(match.group("value"))
        if value is None or math.isinf(value):
            continue
        name = match.group("name")
        labels = parse_labels(match.group("labels"))
        base = name
        for suffix in ("_bucket", "_sum", "_count"):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                break
        samples.append(
            Sample(
                name=name,
                labels=labels,
                value=value,
                key=series_key(name, labels),
                help=helps.get(name) or helps.get(base, ""),
                type=types.get(name) or types.get(base, "untyped"),
            )
        )
    return samples


def to_dict(samples: Iterable[Sample]) -> Dict[str, float]:
    return {s.key: s.value for s in samples}
