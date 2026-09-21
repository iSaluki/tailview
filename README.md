# tailview

A local dashboard for your own Tailscale node.

It reads `tailscale metrics print` and the rest of the local CLI, keeps a rolling
window in memory, and renders it as something you can actually explore: how much
of your traffic went peer-to-peer versus through a relay, which peers are
connected directly, how far away your DERP regions are, and what every raw
counter is doing.

Nothing is installed and nothing leaves the machine. The whole tool is the
Python standard library plus one page of hand-written HTML, CSS and JavaScript.

<img width="1325" height="523" alt="image" src="https://github.com/user-attachments/assets/fff1bf7f-52bf-49ea-96ee-79e5a583e4a2" />
<img width="1325" height="523" alt="image" src="https://github.com/user-attachments/assets/08cbcc6e-ba08-46c3-b32c-4f9e20901a5d" />
<img width="1619" height="989" alt="image" src="https://github.com/user-attachments/assets/3837c050-fe45-47d3-a5b0-01e5227b4c36" />
<img width="1619" height="748" alt="image" src="https://github.com/user-attachments/assets/e5f17c04-0136-4ea3-aa94-343ec584f901" />


## Run it

```sh
git clone https://github.com/iSaluki/tailview.git
cd tailview
./tailview
```

That prints a URL and opens it:

```
  tailview is reading your node's metrics
  http://localhost:8829/
  Press Ctrl-C to stop.
```

Requirements: **Python 3.9+** and the **`tailscale` CLI** on `PATH`. Fedora,
Ubuntu, Arch and macOS all ship a suitable Python already.

Want to see it before pointing it at your tailnet?

```sh
./tailview --demo
```

That runs on generated sample data and contacts no daemon.

## What it shows

| Panel | Reads |
| --- | --- |
| **Carried directly** — the headline share of bytes that went peer-to-peer | `tailscale metrics print` |
| **Flow by path** — live mirrored stream, inbound above the rule, outbound below, stacked by path | `tailscale metrics print` |
| **Relay round trips** — latency to each DERP region, your home relay highlighted | `tailscale netcheck --format=json`, `tailscale debug derp-map` |
| **Dropped packets** — by reason, inbound and outbound | `tailscale metrics print` |
| **Peers** — sortable, searchable; direct or relayed, throughput, handshake age; click one for detail | `tailscale status --json` |
| **Health** — daemon warnings, key expiry, available updates, sources that failed | `tailscale status --json` |
| **This node** — addresses, routes, exit node, DNS, SSH, serve targets, tailnet lock | `tailscale debug prefs`, `serve status`, `dns status`, `lock status` |
| **Metrics explorer** — every series with its value, change over the window, rate and trend, plus the raw exposition text | `tailscale metrics print` |

Everything is scoped by the one window control at the top: 5 minutes, 15
minutes, 1 hour, or everything collected since start. Each chart has a table
view next to it, so no value is reachable only by hovering.

## Options

```
./tailview --help
```

| Flag | Default | Does |
| --- | --- | --- |
| `-p`, `--port` | `8829` | Port to listen on. If it is busy, the next free port is used. |
| `--host` | `127.0.0.1` | Address to bind. Non-loopback needs `--allow-remote`. |
| `-i`, `--interval` | `3` | Seconds between metric polls. |
| `--history` | `60` | Minutes of history to keep in memory. |
| `--netcheck-interval` | `300` | Seconds between DERP netchecks. |
| `--no-netcheck` | off | Never run netcheck. The relay latency panel stays empty. |
| `--tailscale` | found on `PATH` | Path to the `tailscale` binary. |
| `--demo` | off | Generated sample data; no daemon is contacted. |
| `--no-browser` | off | Do not open a browser on start. |
| `--allow-remote` | off | Permit binding to a non-loopback address. |
| `--allowed-host` | — | Extra `Host` header to accept. Repeatable. |

## If a panel says a command did not answer

On Linux the daemon only talks to root and to the configured operator. If the
Health panel reports that `tailscale metrics print` or `tailscale debug prefs`
was refused, make yourself the operator once:

```sh
sudo tailscale set --operator=$USER
```

Then reload the page. Panels whose source your `tailscale` version does not
offer stay empty and say so; the rest keep working.

Netcheck sends probes to DERP regions, so it runs every five minutes rather
than every poll. The **re-run netcheck** button forces one.

## Notes on safety

- Binds to loopback only unless you pass `--allow-remote`. There is no
  authentication, so anyone who can reach the port can read your node's metrics.
- Every request's `Host` header is checked against localhost. That is what stops
  a page in another browser tab from reaching this server by rebinding a DNS
  name to `127.0.0.1`.
- The page loads no third-party code. Its content security policy allows scripts
  from this server only, which is also why it works with no internet connection.
- Read-only: every `tailscale` subcommand it runs reports state. None change it.

## Layout

```
tailview              the launch script
src/tailview/
  cli.py              argument parsing and startup
  collector.py        the poll loop, the rolling window, rate derivation
  promparse.py        Prometheus text exposition parser
  server.py           JSON API, server-sent events, static files
  tsclient.py         runs the tailscale CLI; never raises on failure
  demo.py             sample data source for --demo
  web/                the dashboard: index.html, style.css, app.js, charts.js
tests/                unit tests
```

## Tests

```sh
python3 -m unittest discover -s tests
```

## Design notes

Monospace carries the labels, axes and columns because that is the typeface the
underlying command speaks in; the system sans is reserved for magnitudes, so a
number reads as a quantity rather than as more terminal output. Section headings
use the `# family_name` form from the exposition format itself.

The series colours are not chosen by eye. The five path colours were checked in
both light and dark mode for colour-vision separation, lightness band, chroma
floor and contrast against the surface they are drawn on. Two light-mode slots
fall below 3:1 against the surface, which is why every chart ships a legend and
a table view rather than relying on colour alone. Reordering the paths changes
which pairs sit next to each other in a stack, so it would need re-checking.

## Licence

MIT. See [LICENSE](LICENSE).
