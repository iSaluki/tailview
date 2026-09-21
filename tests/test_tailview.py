"""Tests for the parts that would quietly produce wrong numbers.

Run with: python3 -m unittest discover -s tests
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tailview import promparse  # noqa: E402
from tailview.collector import Collector  # noqa: E402
from tailview.demo import DemoClient  # noqa: E402
from tailview.tsclient import TailscaleClient  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_parses_labels_value_help_and_type(self):
        text = (
            "# HELP tailscaled_inbound_bytes_total Bytes received\n"
            "# TYPE tailscaled_inbound_bytes_total counter\n"
            'tailscaled_inbound_bytes_total{path="direct_ipv4"} 4096\n'
        )
        [sample] = promparse.parse(text)
        self.assertEqual(sample.name, "tailscaled_inbound_bytes_total")
        self.assertEqual(sample.labels, {"path": "direct_ipv4"})
        self.assertEqual(sample.value, 4096.0)
        self.assertEqual(sample.type, "counter")
        self.assertEqual(sample.help, "Bytes received")

    def test_label_order_does_not_change_the_key(self):
        a = promparse.series_key("m", {"b": "2", "a": "1"})
        b = promparse.series_key("m", {"a": "1", "b": "2"})
        self.assertEqual(a, b)
        self.assertEqual(a, 'm{a="1",b="2"}')

    def test_skips_malformed_lines_without_raising(self):
        samples = promparse.parse("this is not a sample\nvalid_metric 1\n")
        self.assertEqual([s.key for s in samples], ["valid_metric"])

    def test_handles_escaped_label_values(self):
        [sample] = promparse.parse('m{note="a \\"quoted\\" word"} 1')
        self.assertEqual(sample.labels["note"], 'a "quoted" word')

    def test_drops_nan_and_infinity(self):
        self.assertEqual(promparse.parse("a NaN\nb +Inf\nc 3"), promparse.parse("c 3"))

    def test_ignores_trailing_timestamp(self):
        [sample] = promparse.parse("m 7 1700000000000")
        self.assertEqual(sample.value, 7.0)


class RateTests(unittest.TestCase):
    """Rates are derived from stored counters, so resets must not spike."""

    def _collector(self):
        collector = Collector(DemoClient(), interval=1.0, history_seconds=600.0)
        return collector

    def test_rate_is_delta_over_elapsed_time(self):
        collector = self._collector()
        now = time.time()
        collector._times.extend([now - 10, now])
        collector._samples.extend([{"m": 100.0}, {"m": 300.0}])
        series = collector.series()
        self.assertAlmostEqual(series["rates"]["m"][1], 20.0, places=6)

    def test_counter_reset_reports_no_flow_not_a_spike(self):
        collector = self._collector()
        now = time.time()
        collector._times.extend([now - 10, now])
        collector._samples.extend([{"m": 5000.0}, {"m": 12.0}])
        series = collector.series()
        self.assertEqual(series["rates"]["m"][1], 0.0)

    def test_first_sample_has_no_rate(self):
        collector = self._collector()
        collector._times.append(time.time())
        collector._samples.append({"m": 1.0})
        self.assertIsNone(collector.series()["rates"]["m"][0])

    def test_window_keeps_one_sample_before_the_cutoff(self):
        collector = self._collector()
        now = time.time()
        for offset in (600, 300, 60, 30, 0):
            collector._times.append(now - offset)
            collector._samples.append({"m": float(offset)})
        series = collector.series(window_seconds=90)
        # 60, 30 and 0 are inside the window; 300 is kept so the first rate
        # in the window is a real measurement rather than a gap.
        self.assertEqual(len(series["t"]), 4)


class ShapingTests(unittest.TestCase):
    def test_state_is_fully_shaped_from_demo_sources(self):
        collector = Collector(DemoClient(), interval=1.0)
        collector._collect_slow()
        collector._collect_fast()
        time.sleep(0.15)
        collector._collect_fast()
        collector._collect_netcheck()
        state = collector.state()

        self.assertEqual(state["node"]["hostName"], "fedora-workstation")
        self.assertEqual(state["node"]["homeRegion"]["code"], "lhr")
        self.assertEqual(len(state["peers"]), 12)
        self.assertTrue(all(s["ok"] for s in state["sources"].values()))

        paths = state["metrics"]["bytes"]["inbound"]
        self.assertGreater(paths["direct_ipv4"], 0)
        self.assertIn("peer_relay_ipv6", paths)

        self.assertEqual(state["netcheck"]["preferredDERP"], 18)
        self.assertEqual(state["netcheck"]["regions"][0]["code"], "lhr")
        # Latency arrives as nanoseconds and is reported in milliseconds.
        self.assertLess(state["netcheck"]["regions"][0]["latencyMs"], 100)

    def test_peer_connection_is_classified(self):
        collector = Collector(DemoClient(), interval=1.0)
        collector._collect_slow()
        collector._collect_fast()
        peers = {p["hostName"]: p for p in collector.state()["peers"]}
        self.assertEqual(peers["fileserver"]["connection"], "direct")
        self.assertEqual(peers["thinkpad-x1"]["connection"], "relay")
        self.assertEqual(peers["thinkpad-x1"]["via"], "Frankfurt")
        self.assertEqual(peers["ipad"]["connection"], "idle")


class ClientTests(unittest.TestCase):
    def test_missing_binary_degrades_instead_of_raising(self):
        client = TailscaleClient("definitely-not-a-real-binary")
        result = client.metrics()
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, "missing")
        self.assertIn("not found", result.reason)

    def test_permission_errors_are_classified(self):
        from tailview.tsclient import _classify

        kind, _ = _classify("access denied; use sudo tailscale ...", 1)
        self.assertEqual(kind, "permission")

    def test_unsupported_subcommands_are_classified(self):
        from tailview.tsclient import _classify

        kind, _ = _classify("flag provided but not defined: -json", 1)
        self.assertEqual(kind, "unsupported")


if __name__ == "__main__":
    unittest.main()
