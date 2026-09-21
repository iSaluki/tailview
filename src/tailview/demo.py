"""A stand-in for the local daemon, for trying the dashboard without a tailnet.

Enabled with `--demo`. It answers the same calls as `TailscaleClient` with
synthetic but plausible output, so every panel has something to draw. Nothing
here touches a real daemon, and the interface labels the session as sample data
so a demo screenshot is never mistaken for a real node.
"""

from __future__ import annotations

import json
import math
import random
import time
from typing import Any, Dict, List

from .tsclient import Result

_REGIONS = [
    (1, "nyc", "New York City", "US"),
    (2, "sfo", "San Francisco", "US"),
    (4, "fra", "Frankfurt", "DE"),
    (5, "sin", "Singapore", "SG"),
    (6, "syd", "Sydney", "AU"),
    (7, "tok", "Tokyo", "JP"),
    (8, "blr", "Bangalore", "IN"),
    (9, "dfw", "Dallas", "US"),
    (10, "sea", "Seattle", "US"),
    (11, "sao", "São Paulo", "BR"),
    (18, "lhr", "London", "GB"),
    (22, "waw", "Warsaw", "PL"),
]

_PEERS = [
    # hostname, os, online, connection, relay, base traffic weight
    ("fileserver", "linux", True, "direct", "", 9.0),
    ("workshop-pi", "linux", True, "direct", "", 2.5),
    ("thinkpad-x1", "linux", True, "relay", "fra", 1.4),
    ("pixel-9", "android", True, "relay", "lhr", 0.6),
    ("macbook-air", "macOS", True, "direct", "", 3.1),
    ("ipad", "iOS", False, "idle", "", 0.0),
    ("vps-hetzner", "linux", True, "direct", "", 5.5),
    ("shed-camera", "linux", True, "relay", "fra", 0.9),
    ("nas", "linux", True, "direct", "", 7.2),
    ("winbox", "windows", False, "idle", "", 0.0),
    ("ci-runner", "linux", True, "direct", "", 1.1),
    ("phone-backup", "android", False, "idle", "", 0.0),
]


def _ok(command: List[str], stdout: str = "", data: Any = None) -> Result:
    return Result(command=command, ok=True, stdout=stdout, data=data, duration_ms=random.randint(4, 30))


class DemoClient:
    """Duck-typed replacement for TailscaleClient."""

    available = True
    path = "(demo mode — no daemon contacted)"
    binary = "tailscale"

    def __init__(self) -> None:
        self.started = time.time()
        self._counters: Dict[str, float] = {}
        self._peer_counters: Dict[str, Dict[str, float]] = {
            name: {"rx": random.uniform(2e8, 4e9), "tx": random.uniform(1e8, 2e9)}
            for name, *_ in _PEERS
        }
        self._last = time.time()

    # -- synthesis -------------------------------------------------------

    def _advance(self) -> float:
        now = time.time()
        dt = max(0.001, now - self._last)
        self._last = now
        return dt

    def _path_rates(self, now: float) -> Dict[str, Dict[str, float]]:
        """Bytes per second per path, with a slow swell plus bursts."""
        swell = 0.45 + 0.35 * math.sin(now / 47.0)
        burst = 1.0 + 3.0 * max(0.0, math.sin(now / 11.0) ** 8)
        jitter = lambda: random.uniform(0.75, 1.3)
        return {
            "direct_ipv4": {
                "in": 1_650_000 * swell * burst * jitter(),
                "out": 420_000 * swell * burst * jitter(),
            },
            "direct_ipv6": {
                "in": 260_000 * swell * jitter(),
                "out": 95_000 * swell * jitter(),
            },
            "derp": {
                "in": 34_000 * (1.2 - swell) * jitter(),
                "out": 21_000 * (1.2 - swell) * jitter(),
            },
            "peer_relay_ipv4": {"in": 4_200 * jitter(), "out": 2_900 * jitter()},
            "peer_relay_ipv6": {"in": 0.0, "out": 0.0},
        }

    def _bump(self, key: str, amount: float) -> float:
        self._counters[key] = self._counters.get(key, 0.0) + max(0.0, amount)
        return self._counters[key]

    def metrics(self) -> Result:
        now = time.time()
        dt = self._advance()
        rates = self._path_rates(now)

        lines = [
            "# HELP tailscaled_advertised_routes Number of subnet routes advertised by this node",
            "# TYPE tailscaled_advertised_routes gauge",
            "tailscaled_advertised_routes 2",
            "# HELP tailscaled_approved_routes Number of approved subnet routes on this node",
            "# TYPE tailscaled_approved_routes gauge",
            "tailscaled_approved_routes 2",
            "# HELP tailscaled_health_messages Number of health messages broken down by type",
            "# TYPE tailscaled_health_messages gauge",
            'tailscaled_health_messages{type="warning"} 1',
            "# HELP tailscaled_home_derp_region_id The current home DERP region ID",
            "# TYPE tailscaled_home_derp_region_id gauge",
            "tailscaled_home_derp_region_id 18",
        ]

        for direction, help_text in (
            ("inbound", "Counts the number of bytes received from other peers"),
            ("outbound", "Counts the number of bytes sent to other peers"),
        ):
            lines.append(f"# HELP tailscaled_{direction}_bytes_total {help_text}")
            lines.append(f"# TYPE tailscaled_{direction}_bytes_total counter")
            short = "in" if direction == "inbound" else "out"
            for path, values in rates.items():
                total = self._bump(f"{direction}_bytes_{path}", values[short] * dt)
                lines.append(f'tailscaled_{direction}_bytes_total{{path="{path}"}} {total:.0f}')

        for direction in ("inbound", "outbound"):
            lines.append(
                f"# HELP tailscaled_{direction}_packets_total "
                f"Counts the number of packets {'received from' if direction == 'inbound' else 'sent to'} other peers"
            )
            lines.append(f"# TYPE tailscaled_{direction}_packets_total counter")
            short = "in" if direction == "inbound" else "out"
            for path, values in rates.items():
                total = self._bump(f"{direction}_packets_{path}", values[short] * dt / 1180.0)
                lines.append(f'tailscaled_{direction}_packets_total{{path="{path}"}} {total:.0f}')

        drop_rates = {
            "acl": 0.9,
            "multicast": 2.6,
            "link_local_unicast": 1.4,
            "too_short": 0.02,
            "fragment": 0.05,
            "unknown_protocol": 0.11,
            "error": 0.0,
        }
        for direction in ("inbound", "outbound"):
            lines.append(
                f"# HELP tailscaled_{direction}_dropped_packets_total "
                f"Counts the number of {direction} dropped packets, by reason"
            )
            lines.append(f"# TYPE tailscaled_{direction}_dropped_packets_total counter")
            scale = 1.0 if direction == "inbound" else 0.35
            for reason, rate in drop_rates.items():
                total = self._bump(
                    f"{direction}_drop_{reason}",
                    rate * scale * dt * random.uniform(0.0, 2.0),
                )
                lines.append(
                    f'tailscaled_{direction}_dropped_packets_total{{reason="{reason}"}} {total:.0f}'
                )

        lines += [
            "# HELP tailscaled_peer_relay_endpoints Number of peer relay endpoints, by state",
            "# TYPE tailscaled_peer_relay_endpoints gauge",
            'tailscaled_peer_relay_endpoints{state="connecting"} 0',
            'tailscaled_peer_relay_endpoints{state="open"} 1',
            "# HELP tailscaled_peer_relay_forwarded_bytes_total Bytes forwarded as a peer relay",
            "# TYPE tailscaled_peer_relay_forwarded_bytes_total counter",
            'tailscaled_peer_relay_forwarded_bytes_total{transport_in="udp4",transport_out="udp4"} '
            f"{self._bump('relay_fwd_bytes', 3100 * dt):.0f}",
            "# HELP tailscaled_peer_relay_forwarded_packets_total Packets forwarded as a peer relay",
            "# TYPE tailscaled_peer_relay_forwarded_packets_total counter",
            'tailscaled_peer_relay_forwarded_packets_total{transport_in="udp4",transport_out="udp4"} '
            f"{self._bump('relay_fwd_packets', 2.7 * dt):.0f}",
            "# HELP tailscaled_serve_inbound_bytes_total Bytes received for a served service",
            "# TYPE tailscaled_serve_inbound_bytes_total counter",
            'tailscaled_serve_inbound_bytes_total{service="svc:grafana"} '
            f"{self._bump('serve_in', 8400 * dt):.0f}",
            "# HELP tailscaled_serve_outbound_bytes_total Bytes sent for a served service",
            "# TYPE tailscaled_serve_outbound_bytes_total counter",
            'tailscaled_serve_outbound_bytes_total{service="svc:grafana"} '
            f"{self._bump('serve_out', 61000 * dt):.0f}",
        ]
        return _ok(["tailscale", "metrics", "print"], stdout="\n".join(lines) + "\n")

    def status(self) -> Result:
        now = time.time()
        dt = max(0.001, now - self.started)
        peers: Dict[str, Any] = {}
        for index, (name, os_name, online, connection, relay, weight) in enumerate(_PEERS):
            counters = self._peer_counters[name]
            if online and weight:
                step = random.uniform(0.6, 1.5) * weight
                counters["rx"] += step * 42_000 * (time.time() % 1 + 0.4)
                counters["tx"] += step * 12_000 * (time.time() % 1 + 0.4)
            key = f"nodekey:demo{index:02d}" + "0" * 50
            peers[key] = {
                "ID": f"n{index+100}CNTRL",
                "PublicKey": key,
                "HostName": name,
                "DNSName": f"{name}.tail9e4f.ts.net.",
                "OS": os_name,
                "UserID": 1001 if index % 3 else 1002,
                "TailscaleIPs": [f"100.64.{index}.{index+7}", f"fd7a:115c:a1e0::{index+7}"],
                "CurAddr": f"192.168.1.{40+index}:41641" if connection == "direct" else "",
                "Relay": relay,
                "RxBytes": int(counters["rx"]),
                "TxBytes": int(counters["tx"]),
                "Created": _iso(now - 86400 * (30 + index * 9)),
                "LastSeen": _iso(now - (4 if online else 7200 + index * 400)),
                "LastHandshake": _iso(now - random.uniform(5, 110)) if online else _iso(0),
                "LastWrite": _iso(now - random.uniform(0, 30)) if online else _iso(0),
                "Online": online,
                "Active": online and weight > 1.0,
                "ExitNode": name == "vps-hetzner",
                "ExitNodeOption": name in ("vps-hetzner", "fileserver"),
                "Expired": False,
                "Tags": ["tag:server"] if name in ("fileserver", "nas", "ci-runner", "vps-hetzner") else [],
                "PrimaryRoutes": ["192.168.7.0/24"] if name == "fileserver" else [],
                "SSH_HostKeys": ["ssh-ed25519 AAAA..."] if os_name == "linux" and online else None,
            }

        data = {
            "Version": "1.90.8-t9a2b1c3d",
            "TUN": True,
            "BackendState": "Running",
            "TailscaleIPs": ["100.64.0.3", "fd7a:115c:a1e0::3"],
            "Self": {
                "ID": "nSELFCNTRL",
                "HostName": "fedora-workstation",
                "DNSName": "fedora-workstation.tail9e4f.ts.net.",
                "OS": "linux",
                "UserID": 1001,
                "TailscaleIPs": ["100.64.0.3", "fd7a:115c:a1e0::3"],
                "Relay": "lhr",
                "Online": True,
                "ExitNode": False,
                "Created": _iso(self.started - 86400 * 412),
                "KeyExpiry": _iso(now + 86400 * 63 + 3600 * 5),
                "Expired": False,
                "Tags": [],
                "PrimaryRoutes": ["192.168.7.0/24", "10.20.0.0/16"],
                "RxBytes": 0,
                "TxBytes": 0,
            },
            "Health": [
                "Some peers are advertising routes but are not enabled as subnet routers",
            ],
            "MagicDNSSuffix": "tail9e4f.ts.net",
            "CurrentTailnet": {
                "Name": "example.github",
                "MagicDNSSuffix": "tail9e4f.ts.net",
                "MagicDNSEnabled": True,
            },
            "ClientVersion": {"RunningLatest": False, "LatestVersion": "1.92.0"},
            "Peer": peers,
            "User": {
                "1001": {"LoginName": "you@example.com", "DisplayName": "You"},
                "1002": {"LoginName": "housemate@example.com", "DisplayName": "Housemate"},
            },
        }
        _ = dt
        return _ok(["tailscale", "status", "--json"], data=data)

    def netcheck(self) -> Result:
        base = {1: 74, 2: 141, 4: 18, 5: 182, 6: 271, 7: 219, 8: 154, 9: 112, 10: 158, 11: 197, 18: 11, 22: 39}
        latency = {
            str(region): int((base[region] + random.uniform(-3, 5)) * 1e6) for region in base
        }
        data = {
            "UDP": True,
            "IPv4": True,
            "IPv6": True,
            "IPv4CanSend": True,
            "IPv6CanSend": True,
            "OSHasIPv6": True,
            "UPnP": False,
            "PMP": False,
            "PCP": False,
            "HairPinning": None,
            "MappingVariesByDestIP": False,
            "CaptivePortal": False,
            "PreferredDERP": 18,
            "GlobalV4": "203.0.113.42:41641",
            "GlobalV6": "[2001:db8:1234::1]:41641",
            "RegionLatency": latency,
            "RegionV4Latency": latency,
            "RegionV6Latency": {k: v for k, v in latency.items() if int(k) in (1, 4, 18, 22)},
        }
        return _ok(["tailscale", "netcheck", "--format=json"], data=data)

    def version(self) -> Result:
        return _ok(
            ["tailscale", "version", "--json"],
            data={
                "majorMinorPatch": "1.90.8",
                "short": "1.90.8",
                "long": "1.90.8-t9a2b1c3d",
                "gitCommit": "9a2b1c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b",
                "cap": 122,
            },
        )

    def prefs(self) -> Result:
        return _ok(
            ["tailscale", "debug", "prefs"],
            data={
                "ControlURL": "https://controlplane.tailscale.com",
                "RouteAll": False,
                "CorpDNS": True,
                "RunSSH": True,
                "RunWebClient": False,
                "WantRunning": True,
                "LoggedOut": False,
                "ShieldsUp": False,
                "AdvertiseRoutes": ["192.168.7.0/24", "10.20.0.0/16"],
                "AdvertiseTags": None,
                "Hostname": "fedora-workstation",
                "NetfilterMode": 2,
                "NoSNAT": False,
                "NoStatefulFiltering": True,
                "PostureChecking": False,
                "ExitNodeID": "",
                "ExitNodeIP": "",
                "ExitNodeAllowLANAccess": False,
                "AutoUpdate": {"Check": True, "Apply": False},
            },
        )

    def derp_map(self) -> Result:
        regions = {
            str(rid): {
                "RegionID": rid,
                "RegionCode": code,
                "RegionName": name,
                "Nodes": [
                    {"Name": f"{rid}a", "RegionID": rid, "HostName": f"derp{rid}a.tailscale.com", "CountryCode": cc},
                    {"Name": f"{rid}b", "RegionID": rid, "HostName": f"derp{rid}b.tailscale.com", "CountryCode": cc},
                ],
            }
            for rid, code, name, cc in _REGIONS
        }
        return _ok(["tailscale", "debug", "derp-map"], data={"Regions": regions})

    def serve_status(self) -> Result:
        return _ok(
            ["tailscale", "serve", "status", "--json"],
            data={
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "fedora-workstation.tail9e4f.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}
                    }
                },
                "AllowFunnel": None,
            },
        )

    def dns_status(self) -> Result:
        return _ok(
            ["tailscale", "dns", "status", "--json"],
            data={
                "MagicDNSEnabled": True,
                "MagicDNSSuffix": "tail9e4f.ts.net",
                "Nameservers": ["100.100.100.100"],
                "SearchDomains": ["tail9e4f.ts.net"],
            },
        )

    def lock_status(self) -> Result:
        return _ok(
            ["tailscale", "lock", "status"],
            stdout="Tailnet lock is NOT enabled.\n",
            data={"text": "Tailnet lock is NOT enabled."},
        )


def _iso(epoch: float) -> str:
    if epoch <= 0:
        return "0001-01-01T00:00:00Z"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2)
