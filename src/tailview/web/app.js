/* tailview — dashboard behaviour.
 *
 * One state object arrives over server-sent events every poll. History is
 * seeded once per window change and appended to in place, so a live update
 * never re-fetches the whole window and never blanks the page while it waits. */

import { fmt, renderStream, renderBars, sparkline, resolve } from './charts.js';

/* Path identity is fixed: a path keeps its colour whatever else is on screen,
 * so "blue means direct over IPv4" stays true when a filter changes. The
 * order is also the stack order, and was validated for colour-vision
 * separation in that order — reordering it needs re-validation. */
const PATHS = [
  { key: 'direct_ipv4',     label: 'direct IPv4',     short: 'direct v4', color: '--series-direct4', direct: true },
  { key: 'direct_ipv6',     label: 'direct IPv6',     short: 'direct v6', color: '--series-direct6', direct: true },
  { key: 'derp',            label: 'DERP relay',      short: 'DERP',      color: '--series-derp',    direct: false },
  { key: 'peer_relay_ipv4', label: 'peer relay IPv4', short: 'relay v4',  color: '--series-relay4',  direct: false },
  { key: 'peer_relay_ipv6', label: 'peer relay IPv6', short: 'relay v6',  color: '--series-relay6',  direct: false },
];

const DROP_HELP = {
  acl: 'Blocked by a tailnet access rule.',
  multicast: 'Multicast is not carried across the tailnet.',
  link_local_unicast: 'Link-local address, not routable here.',
  too_short: 'Packet was shorter than its header claimed.',
  fragment: 'IP fragment; Tailscale does not reassemble.',
  unknown_protocol: 'Protocol Tailscale does not carry.',
  error: 'The daemon could not process the packet.',
};

const ICONS = {
  warning: '<svg class="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4"/><path d="M12 17.5v.01"/></svg>',
  good: '<svg class="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m4 12.5 5 5L20 6.5"/></svg>',
  critical: '<svg class="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5"/><path d="M12 16.2v.01"/></svg>',
  off: '<svg class="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8.5 12h7"/></svg>',
};

const $ = (id) => document.getElementById(id);

const view = {
  window: 900,
  activePaths: new Set(PATHS.map((p) => p.key)),
  dropDirection: 'inbound',
  peerFilter: 'all',
  peerSearch: '',
  peerSort: { column: 'traffic', descending: true },
  metricSearch: '',
  showRaw: false,
  showStreamTable: false,
  openPeer: null,
};

let state = null;
let history = { t: [], values: {} };
let seeding = false;

/* True while the pointer is over the stream chart or something inside it has
 * focus. A live chart that rebuilds itself every couple of seconds would
 * otherwise throw away the crosshair the moment someone tried to read a
 * value, so the redraw waits until they are done. */
let streamHeld = false;
let streamStale = false;

/* -- helpers ----------------------------------------------------------- */

const escapeHtml = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const byteKey = (direction, path) =>
  `tailscaled_${direction}_bytes_total{path="${path}"}`;

const packetKey = (direction, path) =>
  `tailscaled_${direction}_packets_total{path="${path}"}`;

const dropKey = (direction, reason) =>
  `tailscaled_${direction}_dropped_packets_total{reason="${reason}"}`;

/** Per-second rates for one series across the current history. */
function ratesFor(key) {
  const column = history.values[key];
  if (!column) return [];
  const out = new Array(column.length).fill(0);
  let previous = null;
  for (let i = 0; i < column.length; i += 1) {
    const current = column[i];
    if (current === null || current === undefined) continue;
    if (previous !== null) {
      const dt = history.t[i] - history.t[previous];
      const delta = current - column[previous];
      // A counter that went backwards means the daemon restarted. Report no
      // flow for that step rather than an invented spike.
      out[i] = dt > 0 && delta >= 0 ? delta / dt : 0;
    }
    previous = i;
  }
  return out;
}

/** Total increase of a counter across the window. */
function windowDelta(key) {
  const column = history.values[key];
  if (!column) return 0;
  let total = 0;
  let previous = null;
  for (let i = 0; i < column.length; i += 1) {
    const current = column[i];
    if (current === null || current === undefined) continue;
    if (previous !== null) {
      const delta = current - column[previous];
      if (delta > 0) total += delta;
    }
    previous = i;
  }
  return total;
}

function windowSeconds() {
  if (history.t.length < 2) return 0;
  return history.t[history.t.length - 1] - history.t[0];
}

/* -- data ------------------------------------------------------------- */

async function seedHistory() {
  seeding = true;
  try {
    const query = view.window === 'all' ? 'all' : String(view.window);
    const response = await fetch(`/api/series?window=${query}`);
    const data = await response.json();
    history = { t: data.t || [], values: data.values || {} };
  } catch (error) {
    history = { t: [], values: {} };
  } finally {
    seeding = false;
  }
}

function appendSample(next) {
  const at = next.sampledAt;
  if (!at) return;
  if (history.t.length && at <= history.t[history.t.length - 1]) return;

  const flat = {};
  for (const series of next.metrics.series) flat[series.key] = series.value;
  for (const peer of next.peers) {
    flat[`peer:${peer.id}:rx`] = peer.rxBytes;
    flat[`peer:${peer.id}:tx`] = peer.txBytes;
  }

  const index = history.t.length;
  history.t.push(at);
  for (const key of Object.keys(flat)) {
    if (!history.values[key]) history.values[key] = new Array(index).fill(null);
    history.values[key].push(flat[key]);
  }
  for (const [key, column] of Object.entries(history.values)) {
    while (column.length <= index) column.push(null);
  }

  if (view.window !== 'all') {
    const cutoff = at - view.window;
    let drop = 0;
    while (drop < history.t.length - 2 && history.t[drop] < cutoff) drop += 1;
    if (drop > 0) {
      history.t = history.t.slice(drop);
      for (const key of Object.keys(history.values)) {
        history.values[key] = history.values[key].slice(drop);
      }
    }
  }
}

/* -- rendering: masthead and chrome ----------------------------------- */

function renderMasthead() {
  const node = state.node;
  $('node-name').textContent = node.dnsName || node.hostName || 'unknown node';
  const bits = [];
  if (node.addresses?.length) bits.push(node.addresses[0]);
  if (node.tailnetName) bits.push(node.tailnetName);
  if (node.version) bits.push(`v${String(node.version).split('-')[0]}`);
  $('node-sub').textContent = bits.join('  ·  ');

  const pulse = $('pulse');
  const text = $('pulse-text');
  const metricsOk = state.sources.metrics?.ok;
  const age = state.sampledAt ? (Date.now() / 1000) - state.sampledAt : Infinity;

  if (!metricsOk) {
    pulse.dataset.state = 'down';
    text.textContent = 'no metrics';
  } else if (age > state.meta.interval * 4) {
    pulse.dataset.state = 'stale';
    text.textContent = 'stale';
  } else {
    pulse.dataset.state = 'live';
    text.textContent = `live · ${state.meta.interval}s`;
  }

  $('demo-note').hidden = !String(state.meta.tailscaleBinary || '').includes('demo');

  const span = windowSeconds();
  $('window-note').textContent = history.t.length < 2
    ? 'collecting…'
    : `${history.t.length} samples over ${span < 90 ? `${Math.round(span)}s` : `${Math.round(span / 60)}m`}`;
}

/* -- rendering: hero --------------------------------------------------- */

function pathTotals() {
  return PATHS.map((path) => {
    const inbound = windowDelta(byteKey('inbound', path.key));
    const outbound = windowDelta(byteKey('outbound', path.key));
    return { ...path, inbound, outbound, total: inbound + outbound };
  });
}

function renderHero() {
  const totals = pathTotals();
  const grand = totals.reduce((sum, p) => sum + p.total, 0);
  const direct = totals.filter((p) => p.direct).reduce((sum, p) => sum + p.total, 0);

  const heroValue = $('hero-value');
  const caption = $('hero-caption');

  if (grand <= 0) {
    heroValue.innerHTML = '—';
    caption.textContent = 'No traffic has crossed the tailnet in this window. The figure appears once bytes move.';
  } else {
    const share = (direct / grand) * 100;
    heroValue.innerHTML = `${share.toFixed(share >= 99.95 ? 0 : 1)}<span class="hero-unit">%</span>`;
    const relayed = grand - direct;
    caption.textContent = relayed > 0
      ? `${fmt.bytesText(direct)} went peer-to-peer. ${fmt.bytesText(relayed)} took a relay.`
      : `All ${fmt.bytesText(grand)} went peer-to-peer. Nothing was relayed.`;
  }

  const span = windowSeconds();
  $('hero-window').textContent = span < 60
    ? 'over the last minute'
    : `over the last ${Math.round(span / 60)} min`;

  const meter = $('path-meter');
  const key = $('path-key');
  meter.textContent = '';
  key.textContent = '';

  const present = totals.filter((p) => p.total > 0);
  if (!present.length) {
    key.innerHTML = '<div class="meter-key-row"><span></span><span>waiting for bytes</span><span></span></div>';
    meter.setAttribute('aria-label', 'No traffic yet');
    return;
  }

  meter.setAttribute('aria-label',
    `Share of traffic by path: ${present.map((p) => `${p.label} ${((p.total / grand) * 100).toFixed(1)}%`).join(', ')}`);

  for (const path of present) {
    const segment = document.createElement('span');
    segment.style.background = `var(${path.color})`;
    segment.style.flex = `${path.total} 1 0`;
    meter.appendChild(segment);
  }

  key.innerHTML = present.map((path) => `
    <div class="meter-key-row">
      <span class="swatch" style="background:var(${path.color})"></span>
      <span>${escapeHtml(path.label)}</span>
      <span class="share">${((path.total / grand) * 100).toFixed(1)}%</span>
    </div>`).join('');
}

function streamSeries() {
  return PATHS.map((path) => ({
    key: path.key,
    label: path.short,
    color: resolve(`var(${path.color})`),
    inbound: ratesFor(byteKey('inbound', path.key)),
    outbound: ratesFor(byteKey('outbound', path.key)),
  }));
}

function renderStreamPanel({ force = false } = {}) {
  const root = $('stream');

  if (streamHeld && !force) {
    streamStale = true;
  } else {
    streamStale = false;
    const series = streamSeries();
    const height = Math.max(240, Math.min(340, root.clientWidth * 0.42));
    renderStream(root, {
      times: history.t,
      series,
      height,
      activeKeys: view.activePaths,
    });

    if (!history.t.length) {
      root.innerHTML = '<div class="empty"><strong>No samples yet.</strong>' +
        '<span>The first reading lands within a poll interval.</span></div>';
    }
  }

  const totals = pathTotals();
  const legend = $('stream-legend');
  legend.innerHTML = PATHS.map((path) => {
    const on = view.activePaths.has(path.key);
    const rates = ratesFor(byteKey('inbound', path.key));
    const current = rates.length ? rates[rates.length - 1] : 0;
    const total = totals.find((t) => t.key === path.key)?.total || 0;
    const label = total > 0 ? fmt.rate(current) : 'idle';
    return `<button type="button" data-path="${path.key}" aria-pressed="${on}">
      <span class="swatch" style="background:var(${path.color})"></span>
      <span>${escapeHtml(path.label)}</span>
      <span class="value">${label}</span>
    </button>`;
  }).join('');

  renderStreamTable();
}

function renderStreamTable() {
  const host = $('stream-table');
  host.hidden = !view.showStreamTable;
  if (!view.showStreamTable) return;

  const totals = pathTotals();
  const grand = totals.reduce((sum, p) => sum + p.total, 0) || 1;
  host.innerHTML = `
    <div class="table-scroll" style="margin-top:14px">
      <table class="data">
        <caption class="visually-hidden"></caption>
        <thead><tr>
          <th scope="col">path</th>
          <th scope="col" class="num">received</th>
          <th scope="col" class="num">sent</th>
          <th scope="col" class="num">in now</th>
          <th scope="col" class="num">out now</th>
          <th scope="col" class="num">share</th>
        </tr></thead>
        <tbody>${totals.map((path) => {
          const inRates = ratesFor(byteKey('inbound', path.key));
          const outRates = ratesFor(byteKey('outbound', path.key));
          const inNow = inRates.length ? inRates[inRates.length - 1] : 0;
          const outNow = outRates.length ? outRates[outRates.length - 1] : 0;
          return `<tr>
            <td class="primary"><span class="swatch-cell">
              <span class="swatch" style="background:var(${path.color})"></span>${escapeHtml(path.label)}</span></td>
            <td class="num">${fmt.bytesText(path.inbound)}</td>
            <td class="num">${fmt.bytesText(path.outbound)}</td>
            <td class="num">${fmt.rate(inNow)}</td>
            <td class="num">${fmt.rate(outNow)}</td>
            <td class="num">${((path.total / grand) * 100).toFixed(1)}%</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>`;
}

function renderTiles() {
  const totals = pathTotals();
  const received = totals.reduce((sum, p) => sum + p.inbound, 0);
  const sent = totals.reduce((sum, p) => sum + p.outbound, 0);

  let peak = 0;
  const combined = [];
  for (let i = 0; i < history.t.length; i += 1) {
    let value = 0;
    for (const path of PATHS) {
      value += ratesFor(byteKey('inbound', path.key))[i] || 0;
      value += ratesFor(byteKey('outbound', path.key))[i] || 0;
    }
    combined.push(value);
    if (value > peak) peak = value;
  }

  const online = state.peers.filter((p) => p.online).length;
  const relayed = state.peers.filter((p) => p.online && p.connection === 'relay').length;
  const packets = PATHS.reduce((sum, path) =>
    sum + windowDelta(packetKey('inbound', path.key)) + windowDelta(packetKey('outbound', path.key)), 0);

  const tiles = [
    {
      label: 'Received', value: fmt.bytes(received), sub: 'in this window',
      spark: sparkline(PATHS.reduce((acc, path) => {
        const rates = ratesFor(byteKey('inbound', path.key));
        return rates.map((v, i) => (acc[i] || 0) + v);
      }, []), resolve('var(--series-direct4)')),
    },
    {
      label: 'Sent', value: fmt.bytes(sent), sub: 'in this window',
      spark: sparkline(PATHS.reduce((acc, path) => {
        const rates = ratesFor(byteKey('outbound', path.key));
        return rates.map((v, i) => (acc[i] || 0) + v);
      }, []), resolve('var(--series-direct6)')),
    },
    {
      label: 'Peak rate', value: { n: fmt.bytes(peak).n, u: `${fmt.bytes(peak).u}/s` },
      sub: 'in and out combined', spark: sparkline(combined, resolve('var(--series-derp)')),
    },
    {
      label: 'Peers online', value: { n: String(online), u: `of ${state.peers.length}` },
      sub: relayed ? `${relayed} on a relay` : 'all connected directly', spark: '',
    },
    {
      label: 'Packets', value: fmt.bytes(packets, 1), sub: 'carried in this window', spark: '',
    },
  ];

  $('tiles').innerHTML = tiles.map((tile) => `
    <div class="tile">
      <div class="label">${escapeHtml(tile.label)}</div>
      <div class="value">${escapeHtml(tile.value.n)}<span class="unit">${escapeHtml(tile.value.u)}</span></div>
      <div class="sub">${escapeHtml(tile.sub)}</div>
      ${tile.spark}
    </div>`).join('');
}

/* -- rendering: DERP --------------------------------------------------- */

function renderDerp() {
  const root = $('derp-chart');
  const chips = $('netcheck-chips');
  const netcheck = state.netcheck;

  if (!netcheck || !netcheck.regions.length) {
    const source = state.sources.netcheck;
    const enabled = state.meta.netcheckEnabled;
    root.innerHTML = `<div class="empty">
      <strong>${enabled ? 'No netcheck result yet.' : 'Netcheck is switched off.'}</strong>
      <span>${enabled
        ? escapeHtml(source && !source.ok ? source.reason : 'The first netcheck runs shortly after start.')
        : 'Restart without --no-netcheck to measure relay latency.'}</span>
    </div>`;
    chips.innerHTML = '';
    $('derp-note').textContent = '';
    return;
  }

  const home = netcheck.preferredDERP;
  const shown = netcheck.regions.filter((r) => r.latencyMs !== null).slice(0, 10);

  renderBars(root, {
    rows: shown.map((region) => ({
      id: region.regionId,
      label: `${region.code}  ${region.name}`,
      value: region.latencyMs,
      note: region.regionId === home ? 'your home relay' : undefined,
    })),
    format: (v) => fmt.ms(v),
    color: resolve('var(--series-direct4)'),
    emphasis: home,
    emphasisColor: resolve('var(--series-derp)'),
    labelWidth: 132,
    description: `Round trip time to the nearest ${shown.length} DERP relay regions. ` +
      `Your home relay is region ${home}.`,
  });

  const homeRegion = netcheck.regions.find((r) => r.regionId === home);
  $('derp-note').textContent = homeRegion
    ? `home: ${homeRegion.name} · ${fmt.ms(homeRegion.latencyMs)}`
    : `home region ${home}`;

  const caps = netcheck.capabilities;

  /* Only the conditions that actually stop direct connections are warnings.
   * A port-mapping protocol being absent is the normal case on most home
   * routers and says nothing on its own, so it is reported, not flagged. */
  const verdict = (label, value, { goodWhen = true, neutral = false } = {}) => {
    if (value === null || value === undefined) {
      return { status: 'off', text: `${label} unknown` };
    }
    const word = value ? 'yes' : 'no';
    if (neutral) return { status: value ? 'good' : 'off', text: `${label} ${word}` };
    return { status: value === goodWhen ? 'good' : 'warning', text: `${label} ${word}` };
  };

  const items = [
    verdict('UDP', caps.udp),
    verdict('IPv4', caps.ipv4),
    verdict('IPv6', caps.ipv6, { neutral: true }),
    verdict('UPnP', caps.upnp, { neutral: true }),
    verdict('NAT-PMP', caps.pmp, { neutral: true }),
    verdict('PCP', caps.pcp, { neutral: true }),
    verdict('Stable NAT mapping', caps.mappingVariesByDestIP === null ? null : !caps.mappingVariesByDestIP),
    verdict('Captive portal', caps.captivePortal, { goodWhen: false }),
  ];

  chips.innerHTML = items.map((item) => `
    <span class="chip" data-status="${item.status}">${ICONS[item.status] || ICONS.off}${escapeHtml(item.text)}</span>`).join('');
}

/* -- rendering: drops -------------------------------------------------- */

function renderDrops() {
  const root = $('drops-chart');
  const direction = view.dropDirection;
  const reasons = Object.keys(DROP_HELP).map((reason) => ({
    id: reason,
    label: reason,
    value: windowDelta(dropKey(direction, reason)),
    note: DROP_HELP[reason],
  })).filter((row) => row.value > 0).sort((a, b) => b.value - a.value);

  if (!reasons.length) {
    root.innerHTML = `<div class="empty">
      <strong>Nothing dropped ${direction === 'inbound' ? 'on the way in' : 'on the way out'}.</strong>
      <span>Every packet in this window was carried.</span></div>`;
    return;
  }

  renderBars(root, {
    rows: reasons,
    format: (v) => `${fmt.count(v)} pkt`,
    color: resolve('var(--series-direct4)'),
    labelWidth: 140,
    description: `${direction} dropped packets by reason over the selected window.`,
  });
}

/* -- rendering: peers -------------------------------------------------- */

function peerRows() {
  const term = view.peerSearch.trim().toLowerCase();
  let rows = state.peers.slice();

  if (view.peerFilter === 'online') rows = rows.filter((p) => p.online);
  if (view.peerFilter === 'direct') rows = rows.filter((p) => p.connection === 'direct');
  if (view.peerFilter === 'relay') rows = rows.filter((p) => p.connection === 'relay');

  if (term) {
    rows = rows.filter((peer) => [
      peer.hostName, peer.dnsName, peer.os, peer.via, peer.owner,
      ...(peer.addresses || []), ...(peer.tags || []),
    ].join(' ').toLowerCase().includes(term));
  }

  const { column, descending } = view.peerSort;
  const value = (peer) => {
    switch (column) {
      case 'name': return (peer.hostName || '').toLowerCase();
      case 'os': return (peer.os || '').toLowerCase();
      case 'link': return peer.connection;
      case 'rx': return peer.rxBytes;
      case 'tx': return peer.txBytes;
      case 'rate': return (peer.rxRate || 0) + (peer.txRate || 0);
      case 'seen': return Date.parse(peer.lastHandshake || 0) || 0;
      default: return peer.rxBytes + peer.txBytes;
    }
  };
  rows.sort((a, b) => {
    const left = value(a);
    const right = value(b);
    if (left === right) return (a.hostName || '').localeCompare(b.hostName || '');
    if (typeof left === 'string') return descending ? right.localeCompare(left) : left.localeCompare(right);
    return descending ? right - left : left - right;
  });
  return rows;
}

function connectionChip(peer) {
  if (peer.connection === 'direct') {
    return `<span class="chip" data-status="good">${ICONS.good}direct</span>`;
  }
  if (peer.connection === 'relay') {
    return `<span class="chip" data-status="warning">${ICONS.warning}via ${escapeHtml(peer.via)}</span>`;
  }
  return `<span class="chip" data-status="off">${ICONS.off}idle</span>`;
}

function renderPeers() {
  const rows = peerRows();
  const online = state.peers.filter((p) => p.online).length;
  $('peers-note').textContent = `${rows.length} shown · ${online} online`;

  if (!rows.length) {
    $('peers-table').innerHTML = `<div class="empty">
      <strong>No peers match.</strong><span>Clear the search or pick a different filter.</span></div>`;
    return;
  }

  const header = (id, label, numeric = false) => {
    const active = view.peerSort.column === id;
    const sort = active ? (view.peerSort.descending ? 'descending' : 'ascending') : null;
    return `<th scope="col" class="sortable${numeric ? ' num' : ''}" data-sort="${id}"
      ${sort ? `aria-sort="${sort}"` : ''} tabindex="0" role="columnheader">
      ${label}<span class="arrow">${active && !view.peerSort.descending ? '↑' : '↓'}</span></th>`;
  };

  $('peers-table').innerHTML = `
    <table class="data">
      <thead><tr>
        ${header('name', 'peer')}
        ${header('os', 'os')}
        <th scope="col">address</th>
        ${header('link', 'link')}
        ${header('rx', 'received', true)}
        ${header('tx', 'sent', true)}
        ${header('rate', 'now', true)}
        <th scope="col">trend</th>
        ${header('seen', 'handshake', true)}
      </tr></thead>
      <tbody>${rows.map((peer) => {
        const rates = ratesFor(`peer:${peer.id}:rx`);
        const spark = sparkline(rates, resolve(peer.connection === 'relay'
          ? 'var(--series-derp)' : 'var(--series-direct4)'));
        const now = (peer.rxRate || 0) + (peer.txRate || 0);
        return `<tr class="clickable" tabindex="0" data-peer="${escapeHtml(peer.id)}">
          <td class="primary"><span class="swatch-cell">
            <span class="status-dot" data-state="${peer.online ? 'online' : 'offline'}"></span>
            ${escapeHtml(peer.hostName || peer.dnsName)}
            ${peer.exitNode ? '<span class="chip" data-status="good">exit node</span>' : ''}
          </span></td>
          <td>${escapeHtml(peer.os || '—')}</td>
          <td>${escapeHtml(peer.addresses?.[0] || '—')}</td>
          <td>${connectionChip(peer)}</td>
          <td class="num">${fmt.bytesText(peer.rxBytes)}</td>
          <td class="num">${fmt.bytesText(peer.txBytes)}</td>
          <td class="num">${now > 0 ? fmt.rate(now) : '—'}</td>
          <td>${spark}</td>
          <td class="num">${escapeHtml(fmt.since(peer.lastHandshake))}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

/* -- rendering: health and configuration ------------------------------- */

function renderHealth() {
  const messages = state.health.messages || [];
  const failing = Object.entries(state.sources)
    .filter(([, source]) => !source.ok)
    .map(([name, source]) => ({ name, ...source }));

  const node = state.node;
  const items = [];

  if (node.backendState && node.backendState !== 'Running') {
    items.push({
      status: 'critical',
      title: `Backend state: ${node.backendState}`,
      body: node.authURL
        ? 'This node needs to be authenticated before it will carry traffic.'
        : 'The daemon is not in the Running state, so traffic is not flowing.',
    });
  }

  for (const message of messages) {
    items.push({ status: 'warning', title: 'Daemon warning', body: message });
  }

  const expiry = fmt.until(node.keyExpiry);
  if (expiry === 'expired') {
    items.push({ status: 'critical', title: 'Node key expired', body: 'Re-authenticate this node to bring it back onto the tailnet.' });
  } else if (expiry && Number.parseInt(expiry, 10) <= 14) {
    items.push({ status: 'warning', title: `Node key expires in ${expiry}`, body: 'Re-authenticate, or disable key expiry for this node in the admin console.' });
  }

  if (node.runningLatest === false && node.latestVersion) {
    items.push({
      status: node.urgentSecurityUpdate ? 'critical' : 'warning',
      title: `Update available: ${node.latestVersion}`,
      body: node.urgentSecurityUpdate
        ? 'This release carries an urgent security fix.'
        : `This node is on ${String(node.version || '').split('-')[0]}.`,
    });
  }

  for (const source of failing) {
    const remedy = source.kind === 'permission'
      ? 'Grant your user access with <code>sudo tailscale set --operator=$USER</code>, then reload.'
      : source.kind === 'unsupported'
        ? 'Your tailscale version does not offer this command; the panel it feeds stays empty.'
        : escapeHtml(source.reason);
    items.push({
      status: source.kind === 'permission' ? 'warning' : 'warning',
      title: `<code>${escapeHtml(source.command)}</code> did not answer`,
      body: remedy,
      raw: true,
    });
  }

  $('health-note').textContent = items.length ? `${items.length} to look at` : 'nothing to report';

  if (!items.length) {
    $('health-body').innerHTML = `<div class="empty">
      <strong>The daemon reports no problems.</strong>
      <span>Every source answered and no warnings are set.</span></div>`;
    return;
  }

  $('health-body').innerHTML = `<ul class="notice-list">${items.map((item) => `
    <li class="notice" data-status="${item.status}">
      ${ICONS[item.status]}
      <span><strong>${item.raw ? item.title : escapeHtml(item.title)}</strong><br>${item.raw ? item.body : escapeHtml(item.body)}</span>
    </li>`).join('')}</ul>`;
}

function renderFacts() {
  const node = state.node;
  const prefs = node.prefs || {};
  const metrics = state.metrics;

  const serveTargets = [];
  if (state.serve?.Web) {
    for (const [host, config] of Object.entries(state.serve.Web)) {
      for (const [path, handler] of Object.entries(config.Handlers || {})) {
        serveTargets.push(`${host}${path} → ${handler.Proxy || handler.Path || 'static'}`);
      }
    }
  }

  const facts = [
    ['addresses', (node.addresses || []).join('  ')],
    ['tailnet', node.tailnetName || '—'],
    ['MagicDNS', node.magicDNSEnabled ? `on · ${node.magicDNSSuffix}` : 'off'],
    ['version', node.version || '—'],
    ['key expires', fmt.until(node.keyExpiry) || 'never'],
    ['home relay', node.homeRegion ? `${node.homeRegion.name} (${node.homeRegion.code})` : node.relay || '—'],
    ['routes', metrics.routes.advertised === null || metrics.routes.advertised === undefined
      ? '—'
      : `${fmt.count(metrics.routes.approved)} approved of ${fmt.count(metrics.routes.advertised)} advertised`],
    ['advertising', (prefs.advertiseRoutes || []).join('  ') || 'no routes'],
    ['exit node', node.exitNodeActive ? 'using an exit node' : (prefs.exitNodeID ? 'configured' : 'not in use')],
    ['accept routes', prefs.routeAll ? 'yes' : 'no'],
    ['accept DNS', prefs.corpDNS ? 'yes' : 'no'],
    ['Tailscale SSH', prefs.runSSH ? 'enabled' : 'off'],
    ['shields up', prefs.shieldsUp ? 'yes — incoming blocked' : 'no'],
    ['serving', serveTargets.join('  ') || 'nothing served'],
    ['peer relay', `${fmt.count(metrics.peerRelay.endpoints.open)} open · ${fmt.bytesText(metrics.peerRelay.forwardedBytes)} forwarded`],
    ['tailnet lock', state.tailnetLock?.text?.split('\n')[0] || '—'],
  ];

  $('node-facts').innerHTML = `<dl class="facts">${facts.map(([term, value]) => `
    <div class="fact"><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')}</dl>`;
}

/* -- rendering: metrics explorer --------------------------------------- */

function renderExplorer() {
  const term = view.metricSearch.trim().toLowerCase();
  const rows = state.metrics.series.filter((series) =>
    !term || series.key.toLowerCase().includes(term) || (series.help || '').toLowerCase().includes(term));

  $('explorer-note').textContent =
    `${rows.length} of ${state.metrics.series.length} series`;

  const raw = $('raw-metrics');
  raw.hidden = !view.showRaw;
  $('metrics-table').hidden = view.showRaw;

  if (view.showRaw) {
    fetch('/api/raw').then((r) => r.text()).then((text) => {
      raw.innerHTML = escapeHtml(text)
        .replace(/^(#.*)$/gm, '<span class="comment">$1</span>')
        .replace(/^([a-z_]+(?:\{[^}]*\})?)(\s+)(-?[\d.e+]+)$/gim,
          '<span class="metric">$1</span>$2<span class="number">$3</span>');
    });
    return;
  }

  if (!rows.length) {
    $('metrics-table').innerHTML = `<div class="empty">
      <strong>No series matches “${escapeHtml(view.metricSearch)}”.</strong>
      <span>Try a metric family name such as <code>inbound</code> or <code>derp</code>.</span></div>`;
    return;
  }

  $('metrics-table').innerHTML = `
    <table class="data">
      <thead><tr>
        <th scope="col">series</th>
        <th scope="col">type</th>
        <th scope="col" class="num">value</th>
        <th scope="col" class="num">change in window</th>
        <th scope="col" class="num">per second</th>
        <th scope="col">trend</th>
      </tr></thead>
      <tbody>${rows.map((series) => {
        const counter = series.type === 'counter';
        const trend = counter ? ratesFor(series.key) : (history.values[series.key] || []);
        const delta = counter ? windowDelta(series.key) : null;
        const bytes = series.key.includes('_bytes');
        const showValue = bytes ? fmt.bytesText(series.value) : fmt.count(series.value);
        const showDelta = delta === null ? '—' : (bytes ? fmt.bytesText(delta) : fmt.count(delta));
        const showRate = !counter ? '—' : (bytes ? fmt.rate(series.rate) : `${fmt.count(series.rate)}/s`);
        return `<tr>
          <td class="primary" title="${escapeHtml(series.help)}">${escapeHtml(series.key)}</td>
          <td>${escapeHtml(series.type)}</td>
          <td class="num">${escapeHtml(showValue)}</td>
          <td class="num">${escapeHtml(showDelta)}</td>
          <td class="num">${escapeHtml(showRate)}</td>
          <td>${sparkline(trend, resolve('var(--series-direct4)'))}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

function renderSources() {
  $('sources').innerHTML = Object.entries(state.sources).map(([name, source]) => `
    <span class="source" data-ok="${source.ok}" title="${escapeHtml(source.command)}${source.ok ? '' : ` — ${escapeHtml(source.reason)}`}">
      <span class="status-dot"></span>${escapeHtml(name)}</span>`).join(' ');
}

/* -- peer drawer -------------------------------------------------------- */

function renderDrawer() {
  const root = $('drawer-root');
  if (!view.openPeer) { root.textContent = ''; return; }

  const peer = state.peers.find((p) => p.id === view.openPeer);
  if (!peer) { view.openPeer = null; root.textContent = ''; return; }

  const rxRates = ratesFor(`peer:${peer.id}:rx`);
  const txRates = ratesFor(`peer:${peer.id}:tx`);

  const facts = [
    ['status', peer.online ? 'online' : 'offline'],
    ['link', peer.connection === 'direct' ? `direct to ${peer.via}`
      : peer.connection === 'relay' ? `relayed via ${peer.via}` : 'idle'],
    ['addresses', (peer.addresses || []).join('  ')],
    ['dns name', peer.dnsName || '—'],
    ['os', peer.os || '—'],
    ['owner', peer.owner || '—'],
    ['received', fmt.bytesText(peer.rxBytes)],
    ['sent', fmt.bytesText(peer.txBytes)],
    ['last handshake', fmt.since(peer.lastHandshake)],
    ['last seen', fmt.since(peer.lastSeen)],
    ['tags', (peer.tags || []).join('  ') || 'none'],
    ['routes', (peer.primaryRoutes || []).join('  ') || 'none'],
    ['exit node', peer.exitNode ? 'in use by this node' : peer.exitNodeOption ? 'offered' : 'no'],
    ['Tailscale SSH', peer.sshHostKeys ? 'available' : 'no'],
  ];

  root.innerHTML = `
    <div class="drawer-backdrop" data-close="true"></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="Peer ${escapeHtml(peer.hostName)}">
      <div class="drawer-head">
        <h3>${escapeHtml(peer.hostName || peer.dnsName)}</h3>
        <span class="spacer"></span>
        <button class="icon-button" type="button" data-close="true" aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="m6 6 12 12M18 6 6 18"/></svg>
        </button>
      </div>
      <div class="chips" style="margin-bottom:16px">
        ${connectionChip(peer)}
        ${peer.tags?.map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join('') || ''}
      </div>
      <div class="legend" style="margin:0 0 6px">
        <span style="display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11.5px;color:var(--ink-secondary)">
          <span class="swatch" style="background:var(--series-direct4)"></span>received
          <span class="value">${fmt.rate(peer.rxRate)}</span></span>
        <span style="display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11.5px;color:var(--ink-secondary)">
          <span class="swatch" style="background:var(--series-direct6)"></span>sent
          <span class="value">${fmt.rate(peer.txRate)}</span></span>
      </div>
      <div style="display:flex;gap:10px;align-items:center;margin-bottom:18px">
        ${sparkline(rxRates, resolve('var(--series-direct4)')) || '<span class="note">no history yet</span>'}
        ${sparkline(txRates, resolve('var(--series-direct6)'))}
      </div>
      <dl class="facts">${facts.map(([term, value]) => `
        <div class="fact"><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')}</dl>
    </aside>`;

  root.querySelector('.drawer')?.focus?.();
}

/* -- orchestration ----------------------------------------------------- */

let renderScheduled = false;

function render() {
  if (!state) return;
  renderMasthead();
  renderHero();
  renderStreamPanel();
  renderTiles();
  renderDerp();
  renderDrops();
  renderPeers();
  renderHealth();
  renderFacts();
  renderExplorer();
  renderSources();
  renderDrawer();
}

function scheduleRender() {
  if (renderScheduled) return;
  renderScheduled = true;
  requestAnimationFrame(() => {
    renderScheduled = false;
    render();
  });
}

/* -- events ------------------------------------------------------------ */

function connect() {
  const source = new EventSource('/api/events');

  source.addEventListener('state', (event) => {
    const next = JSON.parse(event.data);
    if (!seeding) appendSample(next);
    state = next;
    scheduleRender();
  });

  source.addEventListener('error', () => {
    const pulse = $('pulse');
    pulse.dataset.state = 'down';
    $('pulse-text').textContent = 'reconnecting';
  });
}

function bind() {
  $('window-picker').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-window]');
    if (!button) return;
    const value = button.dataset.window;
    view.window = value === 'all' ? 'all' : Number(value);
    for (const other of $('window-picker').querySelectorAll('button')) {
      other.setAttribute('aria-pressed', String(other === button));
    }
    document.body.dataset.refreshing = 'true';
    await seedHistory();
    document.body.dataset.refreshing = 'false';
    scheduleRender();
  });

  $('stream-legend').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-path]');
    if (!button) return;
    const key = button.dataset.path;
    if (view.activePaths.has(key)) {
      if (view.activePaths.size === 1) return; // never leave the plot empty
      view.activePaths.delete(key);
    } else {
      view.activePaths.add(key);
    }
    renderStreamPanel({ force: true });
  });

  const stream = $('stream');
  const hold = () => { streamHeld = true; };
  const release = () => {
    streamHeld = false;
    if (streamStale) renderStreamPanel({ force: true });
  };
  stream.addEventListener('pointerenter', hold);
  stream.addEventListener('pointerleave', release);
  stream.addEventListener('focusin', hold);
  stream.addEventListener('focusout', release);

  $('stream-table-toggle').addEventListener('click', (event) => {
    view.showStreamTable = !view.showStreamTable;
    event.currentTarget.setAttribute('aria-pressed', String(view.showStreamTable));
    renderStreamTable();
  });

  $('drop-direction').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-direction]');
    if (!button) return;
    view.dropDirection = button.dataset.direction;
    for (const other of $('drop-direction').querySelectorAll('button')) {
      other.setAttribute('aria-pressed', String(other === button));
    }
    renderDrops();
  });

  $('peer-filter').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-filter]');
    if (!button) return;
    view.peerFilter = button.dataset.filter;
    for (const other of $('peer-filter').querySelectorAll('button')) {
      other.setAttribute('aria-pressed', String(other === button));
    }
    renderPeers();
  });

  $('peer-search').addEventListener('input', (event) => {
    view.peerSearch = event.target.value;
    renderPeers();
  });

  $('metric-search').addEventListener('input', (event) => {
    view.metricSearch = event.target.value;
    renderExplorer();
  });

  $('raw-toggle').addEventListener('click', (event) => {
    view.showRaw = !view.showRaw;
    event.currentTarget.setAttribute('aria-pressed', String(view.showRaw));
    renderExplorer();
  });

  $('netcheck-button').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = 'running…';
    await fetch('/api/netcheck', { method: 'POST' }).catch(() => {});
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = 're-run netcheck';
    }, 6000);
  });

  $('peers-table').addEventListener('click', (event) => {
    const head = event.target.closest('th[data-sort]');
    if (head) {
      const column = head.dataset.sort;
      if (view.peerSort.column === column) view.peerSort.descending = !view.peerSort.descending;
      else view.peerSort = { column, descending: true };
      renderPeers();
      return;
    }
    const row = event.target.closest('tr[data-peer]');
    if (row) { view.openPeer = row.dataset.peer; renderDrawer(); }
  });

  $('peers-table').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const head = event.target.closest('th[data-sort]');
    if (head) { event.preventDefault(); head.click(); return; }
    const row = event.target.closest('tr[data-peer]');
    if (row) { event.preventDefault(); view.openPeer = row.dataset.peer; renderDrawer(); }
  });

  $('drawer-root').addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) { view.openPeer = null; renderDrawer(); }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && view.openPeer) { view.openPeer = null; renderDrawer(); }
  });

  $('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme
      || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('tailview-theme', next); } catch (error) { /* private mode */ }
    render();
  });

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => { if (state) render(); }, 140);
  });
}

function restoreTheme() {
  try {
    const saved = localStorage.getItem('tailview-theme');
    if (saved === 'dark' || saved === 'light') document.documentElement.dataset.theme = saved;
  } catch (error) { /* private mode: fall back to the OS setting */ }
}

async function boot() {
  restoreTheme();
  bind();
  await seedHistory();
  try {
    const response = await fetch('/api/state');
    state = await response.json();
    render();
    const stream = $('stream').querySelector('svg');
    if (stream && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      stream.classList.add('wipe');
    }
  } catch (error) {
    $('pulse-text').textContent = 'no server';
  }
  connect();
}

boot();
