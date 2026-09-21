"""Polls the local daemon and keeps a rolling window of what it said.

The collector runs one background thread. Fast sources (client metrics and
node status) are read every poll interval; slow, more expensive sources (a DERP
netcheck, the preference dump, the DERP map) are read on their own longer
cadences so that watching the dashboard never becomes the reason your node is
busy.

Counter samples are stored raw. Rates are derived on the way out, which keeps
the stored window honest and lets a counter reset (a daemon restart) be
detected rather than rendered as an enormous spike.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from . import promparse
from .tsclient import Result, TailscaleClient

PATH_ORDER = ["direct_ipv4", "direct_ipv6", "derp", "peer_relay_ipv4", "peer_relay_ipv6"]
DIRECT_PATHS = {"direct_ipv4", "direct_ipv6"}

DROP_REASONS = [
    "acl",
    "multicast",
    "link_local_unicast",
    "too_short",
    "fragment",
    "unknown_protocol",
    "error",
]


def _peer_key(peer_id: str, field: str) -> str:
    return f"peer:{peer_id}:{field}"


class Collector:
    def __init__(
        self,
        client: TailscaleClient,
        interval: float = 3.0,
        history_seconds: float = 3600.0,
        netcheck_interval: float = 300.0,
        slow_interval: float = 900.0,
        run_netcheck: bool = True,
    ):
        self.client = client
        self.interval = max(1.0, interval)
        self.history_seconds = max(60.0, history_seconds)
        self.netcheck_interval = netcheck_interval
        self.slow_interval = slow_interval
        self.run_netcheck = run_netcheck

        capacity = int(self.history_seconds / self.interval) + 8
        self._times: deque[float] = deque(maxlen=capacity)
        self._samples: deque[Dict[str, float]] = deque(maxlen=capacity)

        self._meta: Dict[str, Dict[str, str]] = {}  # series key -> {name,type,help,labels}
        self._latest_raw = ""
        self._status: Optional[Dict[str, Any]] = None
        self._netcheck: Optional[Dict[str, Any]] = None
        self._netcheck_at: float = 0.0
        self._derp_regions: Dict[str, Dict[str, Any]] = {}
        self._version: Optional[Dict[str, Any]] = None
        self._prefs: Optional[Dict[str, Any]] = None
        self._serve: Optional[Dict[str, Any]] = None
        self._dns: Optional[Dict[str, Any]] = None
        self._lock_status: Optional[Dict[str, Any]] = None
        self._sources: Dict[str, Dict[str, Any]] = {}
        self._started_at = time.time()

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._tick = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._netcheck_request = threading.Event()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="tailview-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._netcheck_request.set()
        with self._cond:
            self._cond.notify_all()

    def request_netcheck(self) -> None:
        self._netcheck_request.set()

    def wait_for_tick(self, last_seen: int, timeout: float = 30.0) -> int:
        with self._cond:
            if self._tick != last_seen:
                return self._tick
            self._cond.wait(timeout)
            return self._tick

    @property
    def tick(self) -> int:
        with self._lock:
            return self._tick

    # -- collection ------------------------------------------------------

    def _record_source(self, name: str, result: Result) -> None:
        self._sources[name] = result.summary()

    def _loop(self) -> None:
        last_slow = 0.0
        last_netcheck = 0.0
        while not self._stop.is_set():
            cycle_started = time.monotonic()
            now = time.time()

            try:
                self._collect_fast()
            except Exception as exc:  # a poll must never kill the thread
                with self._lock:
                    self._sources["collector"] = {
                        "command": "collector",
                        "ok": False,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "kind": "error",
                        "durationMs": 0,
                        "at": now,
                    }

            if cycle_started - last_slow >= self.slow_interval or last_slow == 0.0:
                last_slow = cycle_started
                try:
                    self._collect_slow()
                except Exception:
                    pass

            wants_netcheck = self._netcheck_request.is_set()
            due = cycle_started - last_netcheck >= self.netcheck_interval or last_netcheck == 0.0
            if self.run_netcheck and (wants_netcheck or due):
                self._netcheck_request.clear()
                last_netcheck = cycle_started
                try:
                    self._collect_netcheck()
                except Exception:
                    pass

            with self._cond:
                self._tick += 1
                self._cond.notify_all()

            elapsed = time.monotonic() - cycle_started
            self._stop.wait(max(0.2, self.interval - elapsed))

    def _collect_fast(self) -> None:
        now = time.time()
        sample: Dict[str, float] = {}

        metrics = self.client.metrics()
        meta_updates: Dict[str, Dict[str, str]] = {}
        if metrics.ok:
            for parsed in promparse.parse(metrics.stdout):
                sample[parsed.key] = parsed.value
                meta_updates[parsed.key] = {
                    "name": parsed.name,
                    "type": parsed.type,
                    "help": parsed.help,
                    "labels": parsed.labels,
                }

        status = self.client.status()
        status_data = status.data if status.ok and isinstance(status.data, dict) else None
        if status_data:
            for peer_id, peer in (status_data.get("Peer") or {}).items():
                if not isinstance(peer, dict):
                    continue
                sample[_peer_key(peer_id, "rx")] = float(peer.get("RxBytes") or 0)
                sample[_peer_key(peer_id, "tx")] = float(peer.get("TxBytes") or 0)

        with self._lock:
            self._latest_raw = metrics.stdout if metrics.ok else self._latest_raw
            self._meta.update(meta_updates)
            self._record_source("metrics", metrics)
            self._record_source("status", status)
            if status_data:
                self._status = status_data
            if sample:
                self._times.append(now)
                self._samples.append(sample)

    def _collect_slow(self) -> None:
        version = self.client.version()
        prefs = self.client.prefs()
        derp = self.client.derp_map()
        serve = self.client.serve_status()
        dns = self.client.dns_status()
        lock_status = self.client.lock_status()

        regions: Dict[str, Dict[str, Any]] = {}
        if derp.ok and isinstance(derp.data, dict):
            for region_id, region in (derp.data.get("Regions") or {}).items():
                if not isinstance(region, dict):
                    continue
                nodes = region.get("Nodes") or []
                regions[str(region_id)] = {
                    "id": int(region_id),
                    "code": region.get("RegionCode") or f"r{region_id}",
                    "name": region.get("RegionName") or f"Region {region_id}",
                    "country": (nodes[0].get("CountryCode") if nodes else None),
                    "nodes": len(nodes),
                    "avoid": bool(region.get("Avoid")),
                }

        with self._lock:
            self._record_source("version", version)
            self._record_source("prefs", prefs)
            self._record_source("derp-map", derp)
            self._record_source("serve", serve)
            self._record_source("dns", dns)
            self._record_source("lock", lock_status)
            if version.ok and isinstance(version.data, dict):
                self._version = version.data
            if prefs.ok and isinstance(prefs.data, dict):
                self._prefs = prefs.data
            if regions:
                self._derp_regions = regions
            if serve.ok and isinstance(serve.data, dict):
                self._serve = serve.data
            if dns.ok and isinstance(dns.data, dict):
                self._dns = dns.data
            if lock_status.ok and isinstance(lock_status.data, dict):
                self._lock_status = lock_status.data

    def _collect_netcheck(self) -> None:
        result = self.client.netcheck()
        with self._lock:
            self._record_source("netcheck", result)
            if result.ok and isinstance(result.data, dict):
                self._netcheck = result.data
                self._netcheck_at = time.time()

    # -- readers ---------------------------------------------------------

    def raw_metrics(self) -> str:
        with self._lock:
            return self._latest_raw

    def series(self, window_seconds: Optional[float] = None) -> Dict[str, Any]:
        """History for every series, as raw values plus derived per-second rates."""
        with self._lock:
            times = list(self._times)
            samples = list(self._samples)
            meta = dict(self._meta)

        if window_seconds:
            cutoff = time.time() - window_seconds
            keep = [i for i, t in enumerate(times) if t >= cutoff]
            # Keep one extra sample before the cutoff so the first rate is real.
            if keep and keep[0] > 0:
                keep.insert(0, keep[0] - 1)
            times = [times[i] for i in keep]
            samples = [samples[i] for i in keep]

        keys: List[str] = []
        seen = set()
        for sample in samples:
            for key in sample:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)

        values: Dict[str, List[Optional[float]]] = {}
        rates: Dict[str, List[Optional[float]]] = {}
        for key in keys:
            column: List[Optional[float]] = [s.get(key) for s in samples]
            values[key] = column
            rate_column: List[Optional[float]] = [None] * len(column)
            previous_index = None
            for i, current in enumerate(column):
                if current is None:
                    continue
                if previous_index is not None:
                    previous = column[previous_index]
                    dt = times[i] - times[previous_index]
                    if previous is not None and dt > 0:
                        delta = current - previous
                        # A negative delta means the counter restarted with the
                        # daemon; report no flow rather than a phantom spike.
                        rate_column[i] = max(0.0, delta) / dt if delta >= 0 else 0.0
                previous_index = i
            rates[key] = rate_column

        return {
            "t": times,
            "values": values,
            "rates": rates,
            "meta": meta,
            "interval": self.interval,
        }

    def _latest(self) -> Tuple[float, Dict[str, float]]:
        if not self._samples:
            return 0.0, {}
        return self._times[-1], self._samples[-1]

    def _rate_now(self, key: str) -> Optional[float]:
        if len(self._samples) < 2:
            return None
        current = self._samples[-1].get(key)
        if current is None:
            return None
        for i in range(len(self._samples) - 2, -1, -1):
            previous = self._samples[i].get(key)
            if previous is None:
                continue
            dt = self._times[-1] - self._times[i]
            if dt <= 0:
                return None
            delta = current - previous
            return max(0.0, delta) / dt if delta >= 0 else 0.0
        return None

    def state(self) -> Dict[str, Any]:
        """The full current picture, shaped for the dashboard."""
        with self._lock:
            latest_at, latest = self._latest()
            status = self._status or {}
            meta = dict(self._meta)
            derp_regions = dict(self._derp_regions)
            netcheck = self._netcheck
            netcheck_at = self._netcheck_at
            version = self._version or {}
            prefs = self._prefs or {}
            serve = self._serve
            dns = self._dns
            lock_status = self._lock_status
            sources = dict(self._sources)
            rates = {key: self._rate_now(key) for key in latest}
            sample_count = len(self._samples)
            first_at = self._times[0] if self._times else 0.0
            tick = self._tick

        return {
            "tick": tick,
            "generatedAt": time.time(),
            "sampledAt": latest_at,
            "node": _shape_node(status, version, prefs, derp_regions),
            "health": _shape_health(status, latest),
            "peers": _shape_peers(status, derp_regions, rates),
            "metrics": _shape_metrics(latest, rates, meta),
            "netcheck": _shape_netcheck(netcheck, derp_regions, netcheck_at),
            "serve": serve,
            "dns": dns,
            "tailnetLock": lock_status,
            "sources": sources,
            "meta": {
                "interval": self.interval,
                "historySeconds": self.history_seconds,
                "samples": sample_count,
                "firstSampleAt": first_at,
                "startedAt": self._started_at,
                "tailscaleBinary": self.client.path,
                "netcheckEnabled": self.run_netcheck,
            },
        }


# -- shaping helpers -----------------------------------------------------


def _shape_node(
    status: Dict[str, Any],
    version: Dict[str, Any],
    prefs: Dict[str, Any],
    derp_regions: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    self_peer = status.get("Self") or {}
    tailnet = status.get("CurrentTailnet") or {}
    client_version = status.get("ClientVersion") or {}
    relay_code = self_peer.get("Relay") or ""
    home_region = None
    for region in derp_regions.values():
        if region["code"] == relay_code:
            home_region = region
            break
    return {
        "hostName": self_peer.get("HostName"),
        "dnsName": (self_peer.get("DNSName") or "").rstrip("."),
        "os": self_peer.get("OS"),
        "id": self_peer.get("ID"),
        "addresses": status.get("TailscaleIPs") or self_peer.get("TailscaleIPs") or [],
        "backendState": status.get("BackendState"),
        "authURL": status.get("AuthURL"),
        "tun": status.get("TUN"),
        "tailnetName": tailnet.get("Name"),
        "magicDNSSuffix": (tailnet.get("MagicDNSSuffix") or status.get("MagicDNSSuffix") or "").rstrip("."),
        "magicDNSEnabled": tailnet.get("MagicDNSEnabled"),
        "version": status.get("Version") or version.get("long") or version.get("short"),
        "versionShort": version.get("short"),
        "runningLatest": client_version.get("RunningLatest"),
        "latestVersion": client_version.get("LatestVersion"),
        "urgentSecurityUpdate": client_version.get("UrgentSecurityUpdate"),
        "keyExpiry": self_peer.get("KeyExpiry"),
        "expired": self_peer.get("Expired"),
        "created": self_peer.get("Created"),
        "online": self_peer.get("Online"),
        "relay": relay_code,
        "homeRegion": home_region,
        "exitNodeActive": bool(self_peer.get("ExitNode")),
        "tags": self_peer.get("Tags") or [],
        "primaryRoutes": self_peer.get("PrimaryRoutes") or [],
        "prefs": _shape_prefs(prefs),
    }


def _shape_prefs(prefs: Dict[str, Any]) -> Dict[str, Any]:
    if not prefs:
        return {}
    config = prefs.get("Config") or {}
    return {
        "exitNodeID": prefs.get("ExitNodeID") or None,
        "exitNodeIP": prefs.get("ExitNodeIP") or None,
        "exitNodeAllowLANAccess": prefs.get("ExitNodeAllowLANAccess"),
        "routeAll": prefs.get("RouteAll"),
        "corpDNS": prefs.get("CorpDNS"),
        "runSSH": prefs.get("RunSSH"),
        "runWebClient": prefs.get("RunWebClient"),
        "shieldsUp": prefs.get("ShieldsUp"),
        "wantRunning": prefs.get("WantRunning"),
        "loggedOut": prefs.get("LoggedOut"),
        "advertiseRoutes": prefs.get("AdvertiseRoutes") or [],
        "advertiseTags": prefs.get("AdvertiseTags") or [],
        "hostname": prefs.get("Hostname") or config.get("Hostname"),
        "netfilterMode": prefs.get("NetfilterMode"),
        "noSNAT": prefs.get("NoSNAT"),
        "noStatefulFiltering": prefs.get("NoStatefulFiltering"),
        "postureChecking": prefs.get("PostureChecking"),
        "autoUpdate": prefs.get("AutoUpdate"),
    }


def _shape_health(status: Dict[str, Any], latest: Dict[str, float]) -> Dict[str, Any]:
    messages = [m for m in (status.get("Health") or []) if isinstance(m, str)]
    counts: Dict[str, float] = {}
    for key, value in latest.items():
        if key.startswith("tailscaled_health_messages"):
            labels = promparse.parse_labels(key.partition("{")[2].rstrip("}"))
            counts[labels.get("type", "unknown")] = value
    return {"messages": messages, "counts": counts}


def _shape_peers(
    status: Dict[str, Any],
    derp_regions: Dict[str, Dict[str, Any]],
    rates: Dict[str, Optional[float]],
) -> List[Dict[str, Any]]:
    users = status.get("User") or {}
    peers: List[Dict[str, Any]] = []
    for peer_id, peer in (status.get("Peer") or {}).items():
        if not isinstance(peer, dict):
            continue
        cur_addr = peer.get("CurAddr") or ""
        relay_code = peer.get("Relay") or ""
        region = next((r for r in derp_regions.values() if r["code"] == relay_code), None)
        if cur_addr:
            connection, via = "direct", cur_addr
        elif relay_code:
            connection, via = "relay", (region["name"] if region else relay_code)
        else:
            connection, via = "idle", ""
        user = users.get(str(peer.get("UserID"))) or {}
        peers.append(
            {
                "id": peer_id,
                "nodeId": peer.get("ID"),
                "hostName": peer.get("HostName"),
                "dnsName": (peer.get("DNSName") or "").rstrip("."),
                "os": peer.get("OS") or "",
                "addresses": peer.get("TailscaleIPs") or [],
                "online": bool(peer.get("Online")),
                "active": bool(peer.get("Active")),
                "connection": connection,
                "via": via,
                "relay": relay_code,
                "relayRegion": region,
                "rxBytes": peer.get("RxBytes") or 0,
                "txBytes": peer.get("TxBytes") or 0,
                "rxRate": rates.get(_peer_key(peer_id, "rx")),
                "txRate": rates.get(_peer_key(peer_id, "tx")),
                "lastSeen": peer.get("LastSeen"),
                "lastHandshake": peer.get("LastHandshake"),
                "lastWrite": peer.get("LastWrite"),
                "created": peer.get("Created"),
                "exitNode": bool(peer.get("ExitNode")),
                "exitNodeOption": bool(peer.get("ExitNodeOption")),
                "tags": peer.get("Tags") or [],
                "primaryRoutes": peer.get("PrimaryRoutes") or [],
                "expired": bool(peer.get("Expired")),
                "owner": user.get("LoginName") or user.get("DisplayName") or "",
                "sshHostKeys": bool(peer.get("SSH_HostKeys")),
            }
        )
    peers.sort(key=lambda p: (not p["online"], (p["hostName"] or "").lower()))
    return peers


def _shape_metrics(
    latest: Dict[str, float],
    rates: Dict[str, Optional[float]],
    meta: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    series = []
    for key, value in latest.items():
        if key.startswith("peer:"):
            continue
        info = meta.get(key, {})
        series.append(
            {
                "key": key,
                "name": info.get("name", key),
                "labels": info.get("labels", {}),
                "type": info.get("type", "untyped"),
                "help": info.get("help", ""),
                "value": value,
                "rate": rates.get(key),
            }
        )
    series.sort(key=lambda s: s["key"])

    def path_totals(prefix: str) -> Dict[str, float]:
        out = {}
        for path in PATH_ORDER:
            out[path] = latest.get(f'{prefix}{{path="{path}"}}', 0.0)
        return out

    def drop_totals(prefix: str) -> Dict[str, float]:
        out = {}
        for key, value in latest.items():
            if key.startswith(prefix + "{"):
                labels = promparse.parse_labels(key.partition("{")[2].rstrip("}"))
                reason = labels.get("reason")
                if reason:
                    out[reason] = value
        return out

    return {
        "series": series,
        "bytes": {
            "inbound": path_totals("tailscaled_inbound_bytes_total"),
            "outbound": path_totals("tailscaled_outbound_bytes_total"),
        },
        "packets": {
            "inbound": path_totals("tailscaled_inbound_packets_total"),
            "outbound": path_totals("tailscaled_outbound_packets_total"),
        },
        "drops": {
            "inbound": drop_totals("tailscaled_inbound_dropped_packets_total"),
            "outbound": drop_totals("tailscaled_outbound_dropped_packets_total"),
        },
        "routes": {
            "advertised": latest.get("tailscaled_advertised_routes"),
            "approved": latest.get("tailscaled_approved_routes"),
        },
        "peerRelay": {
            "endpoints": {
                state: latest.get(f'tailscaled_peer_relay_endpoints{{state="{state}"}}')
                for state in ("connecting", "open")
            },
            "forwardedBytes": sum(
                value
                for key, value in latest.items()
                if key.startswith("tailscaled_peer_relay_forwarded_bytes_total")
            ),
            "forwardedPackets": sum(
                value
                for key, value in latest.items()
                if key.startswith("tailscaled_peer_relay_forwarded_packets_total")
            ),
        },
        "serveBytes": {
            "inbound": sum(
                value
                for key, value in latest.items()
                if key.startswith("tailscaled_serve_inbound_bytes_total")
            ),
            "outbound": sum(
                value
                for key, value in latest.items()
                if key.startswith("tailscaled_serve_outbound_bytes_total")
            ),
        },
        "homeDerpRegionId": latest.get("tailscaled_home_derp_region_id"),
    }


def _shape_netcheck(
    netcheck: Optional[Dict[str, Any]],
    derp_regions: Dict[str, Dict[str, Any]],
    ran_at: float,
) -> Optional[Dict[str, Any]]:
    if not netcheck:
        return None
    latencies = []
    for region_id, seconds in (netcheck.get("RegionLatency") or {}).items():
        region = derp_regions.get(str(region_id))
        # netcheck reports Go durations in nanoseconds when decoded from JSON.
        ms = float(seconds) / 1e6 if seconds else None
        latencies.append(
            {
                "regionId": int(region_id),
                "code": region["code"] if region else f"r{region_id}",
                "name": region["name"] if region else f"Region {region_id}",
                "country": region.get("country") if region else None,
                "latencyMs": ms,
                "v4": bool((netcheck.get("RegionV4Latency") or {}).get(str(region_id))),
                "v6": bool((netcheck.get("RegionV6Latency") or {}).get(str(region_id))),
            }
        )
    latencies.sort(key=lambda r: (r["latencyMs"] is None, r["latencyMs"] or 0))
    return {
        "ranAt": ran_at,
        "preferredDERP": netcheck.get("PreferredDERP"),
        "regions": latencies,
        "capabilities": {
            "udp": netcheck.get("UDP"),
            "ipv4": netcheck.get("IPv4"),
            "ipv6": netcheck.get("IPv6"),
            "upnp": netcheck.get("UPnP"),
            "pmp": netcheck.get("PMP"),
            "pcp": netcheck.get("PCP"),
            "mappingVariesByDestIP": netcheck.get("MappingVariesByDestIP"),
            "hairPinning": netcheck.get("HairPinning"),
            "captivePortal": netcheck.get("CaptivePortal"),
        },
        "globalV4": netcheck.get("GlobalV4"),
        "globalV6": netcheck.get("GlobalV6"),
        "osHasIPv6": netcheck.get("OSHasIPv6"),
    }
