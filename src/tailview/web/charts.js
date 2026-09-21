/* tailview — SVG chart primitives.
 *
 * Hand-rolled rather than pulled from a CDN: the dashboard has to render on a
 * laptop with no internet, and its content security policy allows scripts from
 * this server only.
 *
 * Shared rules, applied here once so every chart obeys them: hairline solid
 * gridlines, 2px surface gaps instead of borders between touching marks, 4px
 * rounded data-ends on bars, selective direct labels (never one per point),
 * and a hover layer on everything that plots. */

const SVG_NS = 'http://www.w3.org/2000/svg';

export const fmt = {
  bytes(value, digits) {
    if (value === null || value === undefined || Number.isNaN(value)) return { n: '—', u: '' };
    const units = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];
    let n = Math.abs(value);
    let i = 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
    const places = digits !== undefined ? digits : (n < 10 && i > 0 ? 1 : 0);
    return { n: n.toFixed(places), u: units[i] };
  },
  bytesText(value, digits) {
    const { n, u } = fmt.bytes(value, digits);
    return u ? `${n} ${u}` : n;
  },
  rate(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    const { n, u } = fmt.bytes(value);
    return `${n} ${u}/s`;
  },
  count(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
    return String(Math.round(value * 100) / 100);
  },
  percent(value, digits = 1) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    return `${value.toFixed(digits)}%`;
  },
  ms(value) {
    if (value === null || value === undefined) return '—';
    return value >= 100 ? `${Math.round(value)} ms` : `${value.toFixed(1)} ms`;
  },
  clock(epochSeconds) {
    const d = new Date(epochSeconds * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  },
  shortClock(epochSeconds) {
    const d = new Date(epochSeconds * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  },
  since(iso) {
    if (!iso || iso.startsWith('0001')) return 'never';
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return '—';
    const seconds = Math.max(0, (Date.now() - then) / 1000);
    if (seconds < 45) return 'just now';
    if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
    return `${Math.round(seconds / 86400)} d ago`;
  },
  until(iso) {
    if (!iso || iso.startsWith('0001')) return null;
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return null;
    const days = (then - Date.now()) / 86400000;
    if (days < 0) return 'expired';
    if (days < 1) return 'under a day';
    return `${Math.round(days)} days`;
  },
};

function el(name, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of children) node.appendChild(child);
  return node;
}

const SCALE_STEPS = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

function niceCeil(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const base = 10 ** Math.floor(Math.log10(value));
  const scaled = value / base;
  const step = SCALE_STEPS.find((candidate) => scaled <= candidate + 1e-9) ?? 10;
  return step * base;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Resolve `var(--token)` to a concrete colour.
 *
 * SVG presentation attributes do not reliably accept `var()`, and the theme
 * toggle swaps token values at runtime, so colours are resolved at draw time
 * and the charts are redrawn when the theme changes. */
export function resolve(color) {
  if (typeof color !== 'string') return color;
  const match = color.match(/^var\((--[\w-]+)\)$/);
  return match ? cssVar(match[1]) || color : color;
}

/* -- mirrored stacked stream ------------------------------------------
 *
 * Inbound stacks upward from a centre rule, outbound downward, both on the
 * same scale. This is the one shape that answers the question the tool is
 * about: how much of the flow is direct and how much is being relayed. */

export function renderStream(root, options) {
  const { times, series, height = 300, activeKeys } = options;
  const width = root.clientWidth || 720;
  root.textContent = '';
  if (!times.length) return null;

  const padding = { top: 18, right: 14, bottom: 26, left: 74 };
  const plotWidth = Math.max(40, width - padding.left - padding.right);
  const plotHeight = Math.max(80, height - padding.top - padding.bottom);
  const centerY = padding.top + plotHeight / 2;
  const armHeight = plotHeight / 2 - 10;

  const shown = series.filter((s) => activeKeys.has(s.key));

  const stackTotals = (direction) => times.map((_, i) =>
    shown.reduce((sum, s) => sum + (s[direction][i] || 0), 0));
  const inTotals = stackTotals('inbound');
  const outTotals = stackTotals('outbound');
  const peak = Math.max(1, ...inTotals, ...outTotals);
  const scaleMax = niceCeil(peak);

  const x = (i) => padding.left + (times.length === 1 ? plotWidth / 2 : (i / (times.length - 1)) * plotWidth);
  const armY = (value, up) => centerY + (up ? -1 : 1) * (value / scaleMax) * armHeight;

  const svg = el('svg', {
    class: 'chart',
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    tabindex: '0',
    'aria-label':
      `Throughput by network path over time. Inbound above the centre line, outbound below. ` +
      `Peak ${fmt.rate(peak)}. Use the table view for exact values.`,
  });

  const surface = cssVar('--surface');

  // Gridlines: solid hairlines at a quarter and the full scale on each arm.
  const gridGroup = el('g');
  for (const fraction of [0.5, 1]) {
    for (const up of [true, false]) {
      const y = armY(scaleMax * fraction, up);
      gridGroup.appendChild(el('line', {
        class: 'grid-line', x1: padding.left, x2: padding.left + plotWidth, y1: y, y2: y,
      }));
    }
  }
  svg.appendChild(gridGroup);

  // Y ticks: one rate scale, mirrored. Zero sits on the centre rule.
  const tickGroup = el('g');
  for (const up of [true, false]) {
    for (const fraction of [0.5, 1]) {
      const y = armY(scaleMax * fraction, up);
      const tick = el('text', { x: padding.left - 8, y: y + 3.5, 'text-anchor': 'end' });
      tick.textContent = fmt.rate(scaleMax * fraction);
      tickGroup.appendChild(tick);
    }
  }
  const zero = el('text', { x: padding.left - 8, y: centerY + 3.5, 'text-anchor': 'end' });
  zero.textContent = '0';
  tickGroup.appendChild(zero);
  svg.appendChild(tickGroup);

  // Stacked bands, drawn from the centre outwards so the first path sits on
  // the rule. A 2px stroke in the surface color separates touching bands.
  const bands = el('g', { class: 'bands' });
  for (const up of [true, false]) {
    const direction = up ? 'inbound' : 'outbound';
    const baseline = new Array(times.length).fill(0);
    for (const s of shown) {
      const carries = times.some((_, i) => (s[direction][i] || 0) > 0);
      if (!carries) continue;
      const upper = times.map((_, i) => baseline[i] + (s[direction][i] || 0));
      const forward = times.map((_, i) => `${x(i)},${armY(upper[i], up)}`);

      // How thick this band actually gets on screen decides how it is drawn.
      // A 2px surface separator would swallow a hairline band whole, leaving
      // only its coloured edge — which then reads as an outline around the
      // whole stack rather than as a sliver of its own.
      const thickest = Math.max(
        ...times.map((_, i) => ((s[direction][i] || 0) / scaleMax) * armHeight)
      );

      if (thickest >= 3) {
        const backward = [];
        for (let i = times.length - 1; i >= 0; i -= 1) backward.push(`${x(i)},${armY(baseline[i], up)}`);
        bands.appendChild(el('polygon', {
          points: [...forward, ...backward].join(' '),
          fill: s.color,
          'fill-opacity': 0.7,
          stroke: surface,
          'stroke-width': 2,
          'stroke-linejoin': 'round',
        }));
      }
      bands.appendChild(el('polyline', {
        points: forward.join(' '),
        fill: 'none',
        stroke: s.color,
        'stroke-width': thickest >= 3 ? 1.5 : 1.25,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
      }));
      for (let i = 0; i < times.length; i += 1) baseline[i] = upper[i];
    }
  }
  svg.appendChild(bands);

  // The centre rule is the axis: above it is arriving, below it is leaving.
  svg.appendChild(el('line', {
    class: 'axis-line', x1: padding.left, x2: padding.left + plotWidth, y1: centerY, y2: centerY,
  }));

  for (const [label, up] of [['inbound', true], ['outbound', false]]) {
    const y = centerY + (up ? -1 : 1) * (armHeight / 2);
    const node = el('text', {
      class: 'band-label',
      x: 12,
      y,
      'text-anchor': 'middle',
      transform: `rotate(-90 12 ${y})`,
    });
    node.textContent = label;
    svg.appendChild(node);
  }

  // X ticks
  const tickCount = Math.max(2, Math.min(5, Math.floor(plotWidth / 110)));
  const xTicks = el('g');
  for (let t = 0; t < tickCount; t += 1) {
    const i = Math.round((t / (tickCount - 1)) * (times.length - 1));
    const anchor = t === 0 ? 'start' : t === tickCount - 1 ? 'end' : 'middle';
    const node = el('text', { x: x(i), y: height - 8, 'text-anchor': anchor });
    const span = times[times.length - 1] - times[0];
    node.textContent = span < 900 ? fmt.clock(times[i]) : fmt.shortClock(times[i]);
    xTicks.appendChild(node);
  }
  svg.appendChild(xTicks);

  const crosshair = el('line', { class: 'crosshair', y1: padding.top, y2: padding.top + plotHeight, opacity: 0 });
  svg.appendChild(crosshair);

  const hit = el('rect', {
    class: 'hit', x: padding.left, y: padding.top, width: plotWidth, height: plotHeight,
  });
  svg.appendChild(hit);
  root.appendChild(svg);

  const tooltip = document.createElement('div');
  tooltip.className = 'tooltip';
  tooltip.setAttribute('role', 'status');
  root.appendChild(tooltip);

  let focusIndex = times.length - 1;

  const indexFromX = (clientX) => {
    const box = svg.getBoundingClientRect();
    const ratio = (clientX - box.left - padding.left) / plotWidth;
    return Math.max(0, Math.min(times.length - 1, Math.round(ratio * (times.length - 1))));
  };

  const show = (i) => {
    focusIndex = i;
    crosshair.setAttribute('x1', x(i));
    crosshair.setAttribute('x2', x(i));
    crosshair.setAttribute('opacity', '1');

    const rows = [];
    for (const s of shown) {
      const inbound = s.inbound[i] || 0;
      const outbound = s.outbound[i] || 0;
      if (inbound === 0 && outbound === 0) continue;
      rows.push(
        `<div class="row"><span class="swatch" style="background:${s.color}"></span>` +
        `<span class="name">${s.label}</span>` +
        `<span class="value">${fmt.rate(inbound)} / ${fmt.rate(outbound)}</span></div>`
      );
    }
    tooltip.innerHTML =
      `<div class="when">${fmt.clock(times[i])} · in / out</div>` +
      (rows.join('') || '<div class="row"><span></span><span class="name">no traffic</span><span></span></div>') +
      `<div class="row total"><span></span><span class="name">total</span>` +
      `<span class="value">${fmt.rate(inTotals[i])} / ${fmt.rate(outTotals[i])}</span></div>`;
    tooltip.dataset.open = 'true';

    const rootBox = root.getBoundingClientRect();
    const tipWidth = tooltip.offsetWidth || 200;
    let left = x(i) + 14;
    if (left + tipWidth > rootBox.width) left = x(i) - tipWidth - 14;
    tooltip.style.left = `${Math.max(0, left)}px`;
    tooltip.style.top = `${Math.max(0, padding.top + 4)}px`;
  };

  const hide = () => {
    crosshair.setAttribute('opacity', '0');
    tooltip.dataset.open = 'false';
  };

  hit.addEventListener('pointermove', (event) => show(indexFromX(event.clientX)));
  hit.addEventListener('pointerleave', hide);
  svg.addEventListener('blur', hide);
  svg.addEventListener('focus', () => show(focusIndex));
  svg.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowLeft') { show(Math.max(0, focusIndex - 1)); event.preventDefault(); }
    if (event.key === 'ArrowRight') { show(Math.min(times.length - 1, focusIndex + 1)); event.preventDefault(); }
    if (event.key === 'Home') { show(0); event.preventDefault(); }
    if (event.key === 'End') { show(times.length - 1); event.preventDefault(); }
    if (event.key === 'Escape') hide();
  });

  return { peak, scaleMax, inTotals, outTotals };
}

/* -- horizontal bars ---------------------------------------------------
 *
 * One series, one color, by the categorical rule: bar length already encodes
 * magnitude, so hue is free to carry emphasis instead. */

export function renderBars(root, options) {
  const {
    rows, format = (v) => fmt.count(v), color = 'var(--series-direct4)',
    emphasis = null, emphasisColor = 'var(--series-derp)', labelWidth = 92, max = null,
  } = options;

  root.textContent = '';
  if (!rows.length) return;

  const barColor = resolve(color);
  const highlightColor = resolve(emphasisColor);
  const inkColor = resolve('var(--ink)');
  const inkSecondary = resolve('var(--ink-secondary)');

  const width = root.clientWidth || 480;
  const band = 26;
  const barThickness = 14;
  const height = rows.length * band + 6;
  const plotLeft = labelWidth;
  const valueGutter = 76;
  const plotWidth = Math.max(30, width - plotLeft - valueGutter);
  const scaleMax = max !== null ? max : Math.max(1, ...rows.map((r) => r.value || 0));

  const svg = el('svg', {
    class: 'chart', width, height, viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': options.description || 'Bar chart. Exact values are in the table view.',
  });

  rows.forEach((row, index) => {
    const y = index * band + 4;
    const barY = y + (band - barThickness) / 2 - 2;
    const length = Math.max(row.value > 0 ? 3 : 0, ((row.value || 0) / scaleMax) * plotWidth);
    const isEmphasis = emphasis !== null && row.id === emphasis;

    const group = el('g');
    group.appendChild(el('rect', {
      class: 'hit', x: 0, y, width, height: band, rx: 4,
    }));

    const label = el('text', {
      x: plotLeft - 10, y: barY + barThickness / 2 + 3.5, 'text-anchor': 'end',
      fill: isEmphasis ? inkColor : inkSecondary,
    });
    label.textContent = row.label;
    group.appendChild(label);

    if (length > 0) {
      // 4px rounded data-end, square at the baseline.
      const r = Math.min(4, length);
      const path =
        `M ${plotLeft} ${barY} H ${plotLeft + length - r} ` +
        `A ${r} ${r} 0 0 1 ${plotLeft + length} ${barY + r} ` +
        `V ${barY + barThickness - r} ` +
        `A ${r} ${r} 0 0 1 ${plotLeft + length - r} ${barY + barThickness} ` +
        `H ${plotLeft} Z`;
      group.appendChild(el('path', {
        d: path,
        fill: isEmphasis ? highlightColor : barColor,
        'fill-opacity': isEmphasis ? 1 : 0.82,
      }));
    }

    const value = el('text', {
      x: plotLeft + length + 8, y: barY + barThickness / 2 + 3.5,
      fill: isEmphasis ? inkColor : inkSecondary,
    });
    value.textContent = format(row.value);
    group.appendChild(value);

    if (row.note) {
      const note = el('title');
      note.textContent = `${row.label}: ${format(row.value)} — ${row.note}`;
      group.appendChild(note);
    } else {
      const note = el('title');
      note.textContent = `${row.label}: ${format(row.value)}`;
      group.appendChild(note);
    }

    svg.appendChild(group);
  });

  svg.appendChild(el('line', {
    class: 'axis-line', x1: plotLeft, x2: plotLeft, y1: 2, y2: height - 4,
  }));

  root.appendChild(svg);
}

/* -- sparkline ---------------------------------------------------------- */

export function sparkline(values, color) {
  const clean = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (clean.length < 2) return '';
  // A flat series has no trend worth drawing; an empty cell says more than a
  // straight line that looks like a rule.
  if (Math.max(...clean) === Math.min(...clean)) return '';
  const width = 72;
  const height = 20;
  const max = Math.max(1, ...clean);
  const step = width / (values.length - 1);
  const points = [];
  values.forEach((value, index) => {
    if (value === null || value === undefined || Number.isNaN(value)) return;
    const x = index * step;
    const y = height - 1 - (value / max) * (height - 3);
    points.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  });
  if (points.length < 2) return '';
  const lastPoint = points[points.length - 1].split(',');
  return (
    `<svg class="sparkline" viewBox="0 0 ${width} ${height}" aria-hidden="true">` +
    `<polyline points="${points.join(' ')}" fill="none" stroke="${color}" stroke-width="2" ` +
    `stroke-linejoin="round" stroke-linecap="round"/>` +
    `<circle cx="${lastPoint[0]}" cy="${lastPoint[1]}" r="3" fill="${color}" ` +
    `stroke="var(--surface)" stroke-width="2"/></svg>`
  );
}
