// The Constellation Forecast view: one renderer for every mission control system's widget (the
// Yamcs page, the Open MCT view). It draws what a Constellation bridge wrote for each workflow:
// the run's results and the blocks the workflow chose, grouped by what the results are (the
// bridge's `kind`, named as the template that makes them; items do not name their template).
//
// Items are Constellation's item shape: entity, target_at, until, value, explain (each column the
// row carries: {value, unit, source}), and the optional series, severity and action.
//
//   mountForecast(element, { load }) -> { destroy }
//     load(name) resolves a JSON object the bridge wrote ("index.json", "workflow-<slug>.json")
//     through the host's own session, so the view holds no Constellation key.
//
// Framework-free, inline SVG, themed from the host's CSS variables. Every chart has a hover
// tooltip and a table (the list block) beside it.

const REFRESH_MS = 15000;
const ROWS = 100;

const CATEGORIES = [
  { key: 'passes', label: 'Passes', kinds: ['pass-yield', 'modcod-plan'] },
  { key: 'capacity', label: 'Capacity', kinds: ['antenna-capacity'] },
  { key: 'risk', label: 'Risk', kinds: ['rain-fade-swap', 'rfi-watch'] },
  { key: 'audit', label: 'Audit', kinds: ['audit-ledger'] },
];
export const TITLES = {
  'pass-yield': 'Pass yield',
  'modcod-plan': 'MODCOD plan',
  'antenna-capacity': 'Antenna capacity',
  'rain-fade-swap': 'Rain fade swap',
  'audit-ledger': 'Audit ledger',
  'rfi-watch': 'RFI watch',
};

// --- formatting ----------------------------------------------------------------------------------

const esc = (v) =>
  String(v ?? '—').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const num = (v, d = 1) => (typeof v === 'number' && isFinite(v) ? v.toFixed(d) : '—');
const big = (v) => (typeof v !== 'number' ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v >= 100 ? v.toFixed(0) : v.toFixed(1));
// Times come from the forecast doc as strings: escaped here, since every caller writes them into HTML.
const hhmm = (iso) => (iso ? esc(iso.slice(11, 16)) : '—');
const minute = (iso) => (iso ? `${esc(iso.slice(0, 16).replace('T', ' '))}Z` : '—');
const ago = (iso) => {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
};
const until = (iso) => {
  const m = Math.round((Date.parse(iso) - Date.now()) / 60000);
  return m <= 0 ? 'passed' : m < 120 ? `in ${m} min` : `in ${Math.round(m / 60)} h`;
};
// Expected GB at p10/p50/p90 (and, for an antenna-day, its passes and sellable minutes); null for
// a pass pricing could not price.
const gbOf = (item) => {
  // The ledger node first (it carries an antenna-day's passes and minutes), else the item's value,
  // the output column, as a copied workflow may rename the node.
  const ledger = item.explain?.expected_gb?.value;
  const g = typeof ledger?.p50 === 'number' ? ledger : item.value;
  return g && typeof g.p50 === 'number' ? g : null;
};
const ledgerOf = (item) => item.explain?.ledger?.value || {};
// A pass's max elevation: the ledger entry the orbit wrote, under whichever node wrote it.
const elevationOf = (item) =>
  Object.values(item.explain || {}).find((e) => e?.source === 'orbit' && e?.unit === 'deg')?.value;
// A column's value to show: a number as it is, a quantile column by its p50.
const shown = (v) => (typeof v === 'number' ? v : typeof v?.p50 === 'number' ? v.p50 : null);
// "climatology:unavailable:<the model's whole refusal>" reads as the fallback it is; the reason is
// on hover.
const sourceLabel = (source) =>
  !source ? '—' : source.startsWith('climatology:') ? `climatology${source.includes(':unavailable') ? ' (model unavailable)' : ''}` : source;
const shortAntenna = (id) => (id && id.includes('__') ? id.split('__').pop() : id);
// "SITE-000000000001__ant0" as "000001 ant0": enough of the station to tell rows apart.
const stationTag = (id) => (id && id.includes('__') ? `${id.split('__')[0].slice(-6)} ${shortAntenna(id)}` : id);
const stationOf = (item) => item.entity.ground_station_id;
const linkLabel = (item) =>
  item.entity.type === 'antenna'
    ? `${item.entity.id}`
    : `${item.entity.satellite_id} @ ${item.entity.antenna_id}`;

// --- SVG charts ----------------------------------------------------------------------------------

const W = 640;
const tip = (text) => `data-tip="${esc(text)}"`;

function axisTicks(max, n = 4) {
  if (!(max > 0)) return [0];
  const raw = max / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = 0; v <= max + step * 0.01; v += step) out.push(v);
  return out;
}

/** p10 to p90 band with a p50 line over time buckets. */
function bandChart(buckets, { height = 180, unit = 'GB' } = {}) {
  const pad = { l: 44, r: 12, t: 12, b: 26 };
  const h = height;
  const max = Math.max(...buckets.map((b) => b.p90), 0.001);
  const ticks = axisTicks(max);
  const top = ticks[ticks.length - 1];
  const x = (i) => pad.l + (i / Math.max(buckets.length - 1, 1)) * (W - pad.l - pad.r);
  const y = (v) => h - pad.b - (v / top) * (h - pad.t - pad.b);
  const band = buckets.map((b, i) => `${x(i)},${y(b.p90)}`).join(' ') + ' ' + [...buckets].reverse().map((b, i) => `${x(buckets.length - 1 - i)},${y(b.p10)}`).join(' ');
  const line = buckets.map((b, i) => `${i ? 'L' : 'M'}${x(i)},${y(b.p50)}`).join(' ');
  const grid = ticks
    .map((t) => `<line class="cf-grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y(t)}" y2="${y(t)}"/><text class="cf-axis" x="${pad.l - 6}" y="${y(t) + 4}" text-anchor="end">${big(t)}</text>`)
    .join('');
  const labels = buckets
    .map((b, i) => (i % Math.ceil(buckets.length / 8) === 0 ? `<text class="cf-axis" x="${x(i)}" y="${h - 8}" text-anchor="middle">${esc(b.label)}</text>` : ''))
    .join('');
  const hits = buckets
    .map(
      (b, i) =>
        `<rect class="cf-hit" x="${x(i) - (W - pad.l - pad.r) / buckets.length / 2}" y="${pad.t}" width="${(W - pad.l - pad.r) / buckets.length}" height="${h - pad.t - pad.b}" ${tip(`${b.label} · ${num(b.p50)} ${unit} expected (p10 ${num(b.p10)}, p90 ${num(b.p90)}) · ${b.count} passes`)}/>`,
    )
    .join('');
  return `<svg class="cf-chart" viewBox="0 0 ${W} ${h}" role="img" aria-label="Expected ${unit} over time, p10 to p90 band with p50 line">${grid}<polygon class="cf-band" points="${band}"/><path class="cf-line" d="${line}"/>${labels}${hits}</svg>`;
}

/** Horizontal bars with p10 to p90 whiskers, one row per key. */
function rangeBars(rows, { unit = 'GB' } = {}) {
  const rowH = 20;
  const pad = { l: 128, r: 56, t: 4 };
  const h = pad.t + rows.length * rowH;
  const max = Math.max(...rows.map((r) => r.p90 ?? r.value), 0.001);
  const x = (v) => pad.l + (v / max) * (W - pad.l - pad.r);
  const body = rows
    .map((r, i) => {
      const y = pad.t + i * rowH;
      const value = r.p50 ?? r.value;
      const whisker =
        r.p10 !== undefined
          ? `<line class="cf-whisker" x1="${x(r.p10)}" x2="${x(r.p90)}" y1="${y + rowH / 2}" y2="${y + rowH / 2}"/>`
          : '';
      return `<g ${tip(r.tip ?? `${r.label}: ${num(value)} ${unit}`)}><text class="cf-label" x="${pad.l - 8}" y="${y + rowH / 2 + 4}" text-anchor="end">${esc(r.label)}</text><rect class="cf-bar${r.warn ? ' cf-bar-warn' : ''}" x="${pad.l}" y="${y + 6}" width="${Math.max(x(value) - pad.l, 2)}" height="${rowH - 12}" rx="2"/>${whisker}<text class="cf-value" x="${x(r.p90 ?? value) + 6}" y="${y + rowH / 2 + 4}">${big(value)}</text></g>`;
    })
    .join('');
  return `<svg class="cf-chart" viewBox="0 0 ${W} ${h}" role="img" aria-label="Values per row">${body}</svg>`;
}

/** A grid of cells shaded by value (one hue, light to dark). */
function heatGrid(rows, cols, cell, { unit = 'GB' } = {}) {
  const pad = { l: 128, t: 22 };
  const cw = Math.min(120, (W - pad.l) / Math.max(cols.length, 1));
  const ch = 22;
  const h = pad.t + rows.length * ch + 4;
  const max = Math.max(...rows.flatMap((r) => cols.map((c) => cell(r, c)?.value ?? 0)), 0.001);
  const head = cols.map((c, j) => `<text class="cf-axis" x="${pad.l + j * cw + cw / 2}" y="14" text-anchor="middle">${esc(c.slice(5))}</text>`).join('');
  const body = rows
    .map((r, i) => {
      const cells = cols
        .map((c, j) => {
          const v = cell(r, c);
          const share = v ? v.value / max : 0;
          const label = v && cw > 48 ? `<text class="cf-heat-label${share > 0.55 ? ' cf-on-dark' : ''}" x="${pad.l + j * cw + cw / 2}" y="${pad.t + i * ch + 15}" text-anchor="middle">${big(v.value)}</text>` : '';
          return `<g ${tip(v ? `${r} · ${c}: ${num(v.value)} ${unit}, ${v.detail}` : `${r} · ${c}: no passes`)}><rect class="cf-heat" x="${pad.l + j * cw + 1}" y="${pad.t + i * ch + 1}" width="${cw - 2}" height="${ch - 2}" rx="3" style="--share:${share.toFixed(3)}"/>${label}</g>`;
        })
        .join('');
      return `<text class="cf-label" x="${pad.l - 8}" y="${pad.t + i * ch + 15}" text-anchor="end">${esc(r)}</text>${cells}`;
    })
    .join('');
  return `<svg class="cf-chart" viewBox="0 0 ${W} ${h}" role="img" aria-label="Grid of values per row and day">${head}${body}</svg>`;
}

/** Two points per row joined by a line: what a pass keeps at p10 against its p50. */
function dumbbell(rows, { unit = 'GB' } = {}) {
  const rowH = 22;
  const pad = { l: 200, r: 24, t: 4 };
  const h = pad.t + rows.length * rowH;
  const max = Math.max(...rows.map((r) => r.b), 0.001);
  const x = (v) => pad.l + (v / max) * (W - pad.l - pad.r);
  return `<svg class="cf-chart" viewBox="0 0 ${W} ${h}" role="img" aria-label="p10 against p50 per pass">${rows
    .map((r, i) => {
      const y = pad.t + i * rowH + rowH / 2;
      return `<g ${tip(r.tip)}><text class="cf-label" x="${pad.l - 8}" y="${y + 4}" text-anchor="end">${esc(r.label)}</text><line class="cf-whisker" x1="${x(r.a)}" x2="${x(r.b)}" y1="${y}" y2="${y}"/><circle class="cf-dot-warn" cx="${x(r.a)}" cy="${y}" r="5"/><circle class="cf-dot" cx="${x(r.b)}" cy="${y}" r="5"/></g>`;
    })
    .join('')}</svg>`;
}

/** A step line over a pass: MODCOD changes as spectral efficiency over time. */
function stepChart(series) {
  const pts = series.map((s) => ({ t: Date.parse(s.t), v: s.value.sinr_db, label: s.value.modcod }));
  if (pts.length < 1) return '';
  // Drawn for the ledger column, so its own width keeps text readable.
  const W = 320;
  const h = 170;
  const pad = { l: 36, r: 8, t: 14, b: 22 };
  const t0 = pts[0].t;
  const t1 = Math.max(pts[pts.length - 1].t, t0 + 60000);
  const lo = Math.min(...pts.map((p) => p.v)) - 1;
  const hi = Math.max(...pts.map((p) => p.v)) + 1;
  const x = (t) => pad.l + ((t - t0) / (t1 - t0)) * (W - pad.l - pad.r);
  const y = (v) => h - pad.b - ((v - lo) / (hi - lo)) * (h - pad.t - pad.b);
  let d = '';
  pts.forEach((p, i) => {
    d += i ? `H${x(p.t)}V${y(p.v)}` : `M${x(p.t)},${y(p.v)}`;
  });
  d += `H${W - pad.r}`;
  const peak = pts.reduce((a, p) => (p.v > a.v ? p : a), pts[0]);
  const marks = pts
    .map((p) => `<circle class="cf-dot" cx="${x(p.t)}" cy="${y(p.v)}" r="4" ${tip(`${new Date(p.t).toISOString().slice(11, 16)}Z · ${p.label} · SINR ${num(p.v)} dB`)}/>`)
    .join('');
  const names = [pts[0], peak]
    .map((p, k) => `<text class="cf-step-label" x="${Math.min(x(p.t) + 4, W - 70)}" y="${y(p.v) - 7}">${k ? 'peak ' : 'AOS '}${esc(p.label)}</text>`)
    .join('');
  return `<svg class="cf-chart" viewBox="0 0 ${W} ${h}" role="img" aria-label="MODCOD over the pass"><text class="cf-axis" x="${pad.l - 6}" y="${y(hi - 1) + 4}" text-anchor="end">${num(hi - 1, 0)} dB</text><text class="cf-axis" x="${pad.l - 6}" y="${y(lo + 1) + 4}" text-anchor="end">${num(lo + 1, 0)} dB</text><path class="cf-line" d="${d}"/>${marks}${names}<text class="cf-axis" x="${pad.l}" y="${h - 6}">${new Date(t0).toISOString().slice(11, 16)}Z</text><text class="cf-axis" x="${W - pad.r}" y="${h - 6}" text-anchor="end">${new Date(t1).toISOString().slice(11, 16)}Z</text></svg>`;
}

// --- per-kind views ------------------------------------------------------------------------------

function upcoming(run) {
  const now = Date.now();
  if (run.kind === 'audit-ledger') return [...run.items].sort((a, b) => a.target_at.localeCompare(b.target_at));
  return run.items.filter((i) => !i.until || Date.parse(i.until) > now).sort((a, b) => a.target_at.localeCompare(b.target_at));
}

function hourly(items) {
  const now = Date.now();
  const start = Math.floor(now / 3600e3) * 3600e3;
  const buckets = Array.from({ length: 24 }, (_, k) => ({ label: new Date(start + k * 3600e3).toISOString().slice(11, 16), p10: 0, p50: 0, p90: 0, count: 0 }));
  for (const item of items) {
    const g = gbOf(item);
    const k = Math.floor((Date.parse(item.target_at) - start) / 3600e3);
    if (!g || !(k >= 0 && k < 24)) continue;
    buckets[k].p10 += g.p10;
    buckets[k].p50 += g.p50;
    buckets[k].p90 += g.p90;
    buckets[k].count += 1;
  }
  return buckets;
}

function perKey(items, key, value) {
  const out = new Map();
  for (const item of items) {
    const k = key(item);
    const g = value(item);
    if (!k || !g) continue;
    const row = out.get(k) || { label: k, p10: 0, p50: 0, p90: 0, count: 0 };
    row.p10 += g.p10;
    row.p50 += g.p50;
    row.p90 += g.p90;
    row.count += 1;
    out.set(k, row);
  }
  return [...out.values()].sort((a, b) => b.p50 - a.p50);
}

const tile = (label, value, hint) => `<div class="cf-tile"><div class="cf-tile-value">${esc(value)}</div><div class="cf-tile-label">${esc(label)}</div>${hint ? `<div class="cf-tile-hint">${esc(hint)}</div>` : ''}</div>`;
const section = (title, body, note) => `<section class="cf-section"><h3>${esc(title)}</h3>${note ? `<p class="cf-note">${esc(note)}</p>` : ''}${body}</section>`;

/** What an item's action is, as the bridges read it (constellation_space.results.action_state):
 * 'advice' (suggested), 'approved' (approved in Constellation: an instruction), 'pending' (held
 * for approval; its action is not sent), 'rejected', or null with no action. A status the view
 * does not know is held, never a plain forecast. */
export function actionState(item) {
  const status = item.action_status;
  if (status != null && status !== 'approved') return status === 'rejected' ? 'rejected' : 'pending';
  if (!item.action) return status == null ? null : 'pending';
  return status === 'approved' ? 'approved' : 'advice';
}

/** A swap to show: advice, or approved in Constellation; never one held or rejected. */
const actionable = (i) => actionState(i) === 'advice' || actionState(i) === 'approved';

// rfi-watch items are links whose value is RFI in dB (p10/p50/p90) with a lock-loss risk (a
// share), and whose action is a reroute with no deadline: never GB, never a swap.
const pct = (v) => (typeof v === 'number' ? `${Math.round(v * 100)}%` : num(v));
const RFI = {
  tiles(run, items) {
    const worst = Math.max(0, ...items.map((i) => i.value?.p90 ?? 0));
    const critical = items.filter((i) => i.severity === 'critical').length;
    const approved = items.filter((i) => actionState(i) === 'approved').length;
    const held = items.filter((i) => actionState(i) === 'pending').length;
    return `<div class="cf-tiles">${tile('links over the RFI threshold', items.length)}${tile('worst RFI', `${num(worst)} dB`, 'p90, any link')}${tile('links losing lock to RFI', critical)}${tile('reroutes to decide', items.filter(actionable).length - approved, `${approved} approved · ${held} awaiting approval`)}${tile('workflow version', `v${run.workflow_version}`)}</div>`;
  },
  chart(run, items) {
    const rows = [...items].sort((a, b) => (b.value?.p90 ?? 0) - (a.value?.p90 ?? 0)).slice(0, 14);
    return section(
      'RFI per link',
      rangeBars(
        rows.map((i) => ({
          label: linkLabel(i),
          value: i.value?.p50 ?? 0,
          p10: i.value?.p10 ?? 0,
          p90: i.value?.p90 ?? 0,
          warn: i.severity === 'critical',
          tip: `${linkLabel(i)}: ${num(i.value?.p50)} dB (p10 ${num(i.value?.p10)}, p90 ${num(i.value?.p90)}), lock-loss risk ${pct(i.value?.lock_loss_risk)}`,
        })),
        { unit: 'dB' },
      ),
      'Bar is p50 RFI in dB; the line runs p10 to p90. Amber: RFI alone loses lock.',
    );
  },
  warnings(run, items) {
    const approvedFirst = (a, b) => (actionState(b) === 'approved') - (actionState(a) === 'approved');
    const risk = (i) => i.value?.lock_loss_risk ?? 0;
    const all = items.filter(actionable).sort((a, b) => approvedFirst(a, b) || risk(b) - risk(a));
    const held = items.filter((i) => actionState(i) === 'pending').length;
    const cards = all.slice(0, 6).map((i) => {
      const approved = actionState(i) === 'approved';
      return `<div class="cf-card"><div class="cf-card-head"><span class="cf-status-warn" aria-hidden="true">▲</span><b>${approved ? 'Approved: carry out' : 'Reroute advised'}</b></div><div>${esc(linkLabel(i))} from ${minute(i.target_at)}: up to ${num(i.value?.p90)} dB of RFI at p90, lock-loss risk ${pct(risk(i))}.</div><div class="cf-card-action">Reroute off ${esc(shortAntenna(i.entity.antenna_id))}</div>${approved ? '<div class="cf-note">Approved in Constellation.</div>' : ''}</div>`;
    });
    const waiting = held ? ` ${held} awaiting approval in Constellation.` : '';
    const body = cards.length ? `<div class="cf-cards">${cards.join('')}</div>` : `<p class="cf-empty">No reroutes advised.${waiting}</p>`;
    return section('Reroutes', body, `Approved first, then the highest lock-loss risk.${waiting}`);
  },
  list(run, items, selected) {
    const rows = items
      .slice(0, ROWS)
      .map(
        (i) =>
          `<tr data-id="${esc(i.entity.id)}" class="${i.entity.id === selected ? 'cf-sel' : ''}"><td>${minute(i.target_at)}</td><td>${esc(linkLabel(i))}</td><td class="cf-num">${num(i.value?.p10)} / <b>${num(i.value?.p50)}</b> / ${num(i.value?.p90)}</td><td class="cf-num">${pct(i.value?.lock_loss_risk)}</td><td>${i.action ? '<span class="cf-status-warn" title="reroute">▲</span>' : ''}</td></tr>`,
      )
      .join('');
    const head = '<th>From (UTC)</th><th>Link</th><th class="cf-num">RFI dB p10 / p50 / p90</th><th class="cf-num">Lock-loss risk</th><th></th>';
    const note = items.length > ROWS ? `The first ${ROWS} of ${items.length}.` : `${items.length} results.`;
    return section('Results', `<div class="cf-scroll"><table class="cf-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`, `${note} Select a row for its ledger.`);
  },
};

export const VIEWS = {
  tiles(run, items) {
    if (run.kind === 'rfi-watch') return RFI.tiles(run, items);
    if (run.kind === 'audit-ledger') {
      const possible = items.reduce((s, i) => s + (ledgerOf(i).possible_gb ?? 0), 0);
      const delivered = items.reduce((s, i) => s + (ledgerOf(i).delivered_gb ?? 0), 0);
      const dead = items.filter((i) => ledgerOf(i).dead).length;
      return `<div class="cf-tiles">${tile('passes audited', items.length)}${tile('GB delivered', big(delivered), `of ${big(possible)} possible`)}${tile('delivered', possible ? `${Math.round((delivered / possible) * 100)}%` : '—')}${tile('dead passes', dead, 'failed, missed or unlogged')}</div>`;
    }
    if (run.kind === 'antenna-capacity') {
      const gb = items.reduce((s, i) => s + (gbOf(i)?.p50 ?? 0), 0);
      const minutes = items.reduce((s, i) => s + (gbOf(i)?.sellable_min ?? 0), 0);
      return `<div class="cf-tiles">${tile('antenna-days', items.length)}${tile('sellable minutes', big(minutes))}${tile('GB expected', big(gb), 'p50, summed')}${tile('antennas', new Set(items.map((i) => i.entity.id)).size)}</div>`;
    }
    if (run.kind === 'rain-fade-swap') {
      const swaps = items.filter(actionable).sort((a, b) => a.action.deadline.localeCompare(b.action.deadline));
      const approved = swaps.filter((i) => actionState(i) === 'approved').length;
      const held = items.filter((i) => actionState(i) === 'pending').length;
      const atRisk = swaps.reduce((s, i) => s + (gbOf(i).p50 - gbOf(i).p10), 0);
      const soonest = swaps.find((i) => Date.parse(i.action.deadline) > Date.now());
      const booked = swaps.filter((i) => i.action.to).length;
      const hint = approved || held ? `${approved} approved · ${held} awaiting approval` : `${booked} with a better backup`;
      return `<div class="cf-tiles">${tile('swaps to decide', swaps.length - approved, hint)}${tile('GB at risk if it rains', big(atRisk), 'p50 minus p10, summed')}${tile('soonest decision', soonest ? until(soonest.action.deadline) : '—', soonest ? `cancel by ${minute(soonest.action.deadline)}` : '')}${tile('workflow version', `v${run.workflow_version}`)}</div>`;
    }
    const day = items.filter((i) => Date.parse(i.target_at) < Date.now() + 86400e3);
    const p50 = day.reduce((s, i) => s + (gbOf(i)?.p50 ?? 0), 0);
    const p10 = day.reduce((s, i) => s + (gbOf(i)?.p10 ?? 0), 0);
    const swaps = items.filter(actionable).length;
    const next = items.find((i) => Date.parse(i.target_at) > Date.now());
    return `<div class="cf-tiles">${tile('passes, next 24 h', day.length)}${tile('GB expected', big(p50), `${big(p10)} at worst (p10)`)}${swaps ? tile('swaps advised', swaps, 'see Risk below') : tile('next pass', next ? `${hhmm(next.target_at)}Z` : '—', next ? `${next.entity.satellite_id} @ ${shortAntenna(next.entity.antenna_id)}` : '')}${tile('workflow version', `v${run.workflow_version}`)}</div>`;
  },

  chart(run, items) {
    if (run.kind === 'rfi-watch') return RFI.chart(run, items);
    if (run.kind === 'antenna-capacity') {
      const days = [...new Set(items.map((i) => i.target_at.slice(0, 10)))].sort();
      const antennas = [...new Set(items.map((i) => i.entity.id))].sort();
      const at = new Map(items.map((i) => [`${i.entity.id}|${i.target_at.slice(0, 10)}`, i]));
      return section(
        'Expected GB per antenna per day',
        heatGrid(antennas, days, (a, d) => {
          const i = at.get(`${a}|${d}`);
          return i ? { value: gbOf(i)?.p50 ?? 0, detail: `${gbOf(i)?.passes} passes, ${num(gbOf(i)?.sellable_min, 0)} sellable min` } : null;
        }),
        'Darker is more. Hover a cell for its passes and minutes.',
      );
    }
    if (run.kind === 'rain-fade-swap') {
      const risky = items.filter(actionable).slice(0, 20);
      if (!risky.length) return section('Passes at weather risk', '<p class="cf-empty">No pass is at risk in this run.</p>');
      return section(
        'What each at-risk pass keeps if it rains',
        dumbbell(
          risky.map((i) => ({
            label: `${i.entity.satellite_id} @ ${stationTag(i.entity.antenna_id)}`,
            a: gbOf(i).p10,
            b: gbOf(i).p50,
            tip: `${linkLabel(i)} at ${minute(i.target_at)}: ${num(gbOf(i).p10)} GB at p10 against ${num(gbOf(i).p50)} GB at p50`,
          })),
        ),
        'Amber is the wet tail (p10); dark is the median (p50). The wider the gap, the more rain costs.',
      );
    }
    if (run.kind === 'audit-ledger') {
      const rows = perKey(items, stationOf, (i) => ({ p10: ledgerOf(i).delivered_gb ?? 0, p50: ledgerOf(i).delivered_gb ?? 0, p90: ledgerOf(i).possible_gb ?? 0 }));
      return section(
        'Delivered against possible, per station',
        rangeBars(rows.map((r) => ({ label: r.label, value: r.p50, p10: r.p50, p90: r.p90, tip: `${r.label}: ${num(r.p50)} GB delivered of ${num(r.p90)} possible, ${r.count} passes` }))),
        'The bar is what was delivered; the line runs to what was possible.',
      );
    }
    if (run.kind === 'modcod-plan') {
      const mix = new Map();
      for (const item of items) for (const s of item.series || []) mix.set(s.value.modcod, (mix.get(s.value.modcod) || 0) + 1);
      const rows = [...mix.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([label, value]) => ({ label, value, tip: `${label}: chosen ${value} times across the plan` }));
      return section('MODCOD mix across the plan', rangeBars(rows, { unit: 'steps' }), 'How often each MODCOD is planned. Select a pass below to see its own plan.');
    }
    return section('Expected GB over the next 24 hours', bandChart(hourly(items)), 'The line is the median (p50); the band runs from the wet tail (p10) to the best case (p90). Hover an hour for its passes.');
  },

  breakdown(run, items) {
    if (['audit-ledger', 'modcod-plan', 'rfi-watch'].includes(run.kind)) return '';
    if (run.kind === 'antenna-capacity') {
      const rows = perKey(items, (i) => i.entity.id, gbOf).slice(0, 14);
      return section('Antennas ranked by expected GB', rangeBars(rows.map((r) => ({ ...r, label: shortAntenna(r.label) === r.label ? r.label : `${r.label.split('__')[0].slice(-6)} ${shortAntenna(r.label)}`, tip: `${r.label}: ${num(r.p50)} GB (p10 ${num(r.p10)}, p90 ${num(r.p90)}) over ${r.count} days` }))));
    }
    const day = items.filter((i) => Date.parse(i.target_at) < Date.now() + 86400e3);
    const rows = perKey(day, (i) => i.entity.antenna_id, gbOf).slice(0, 14);
    return section(
      'Where the GB lands: antennas, next 24 h',
      rangeBars(rows.map((r) => ({ ...r, label: stationTag(r.label), tip: `${r.label}: ${num(r.p50)} GB expected over ${r.count} passes (p10 ${num(r.p10)}, p90 ${num(r.p90)})` }))),
      'Bar is p50; the line runs p10 to p90.',
    );
  },

  warnings(run, items) {
    if (run.kind === 'rfi-watch') return RFI.warnings(run, items);
    const approvedFirst = (a, b) => (actionState(b) === 'approved') - (actionState(a) === 'approved');
    const all = items
      .filter((i) => actionable(i) && Date.parse(i.action.deadline) > Date.now())
      .sort((a, b) => approvedFirst(a, b) || a.action.deadline.localeCompare(b.action.deadline));
    const held = items.filter((i) => actionState(i) === 'pending').length;
    const cards = all
      .slice(0, 6)
      .map((i) => {
        const a = i.action;
        const g = gbOf(i);
        const approved = actionState(i) === 'approved';
        const backup = a.to ? `Book ${esc(i.entity.satellite_id)} on ${esc(shortAntenna(a.to_antenna_id))} at ${minute(a.to_at)}, +${num(a.gain_gb_p10)} GB at p10` : 'No better pass within 6 h';
        const head = approved ? 'Approved: carry out' : 'Rain-fade risk';
        const note = approved ? `Approved in Constellation. Cancel the original by ${minute(a.deadline)}` : `Cancel by ${minute(a.deadline)}`;
        return `<div class="cf-card" ${tip(`Decide by ${minute(a.deadline)}`)}><div class="cf-card-head"><span class="cf-status-warn" aria-hidden="true">▲</span><b>${head}</b><span class="cf-chip">${until(a.deadline)}</span></div><div>${esc(linkLabel(i))} at ${minute(i.target_at)} keeps ${num(g.p10)} of ${num(g.p50)} GB if it rains.</div><div class="cf-card-action">${backup}</div><div class="cf-note">${note}</div></div>`;
      });
    // A pass at risk with no backup comes as advice alone (a warning and its note): nothing to decide.
    const advisories = items
      .filter((i) => actionState(i) === null && i.severity)
      .slice(0, Math.max(0, 6 - cards.length))
      .map((i) => `<div class="cf-card"><div class="cf-card-head"><span class="cf-status-warn" aria-hidden="true">▲</span><b>Rain-fade risk</b></div><div>${esc(linkLabel(i))} at ${minute(i.target_at)} keeps ${num(gbOf(i).p10)} of ${num(gbOf(i).p50)} GB if it rains.</div><div class="cf-card-action">${esc(i.note || 'No backup pass in the window')}</div></div>`);
    cards.push(...advisories);
    const waiting = held ? ` ${held} awaiting approval in Constellation.` : '';
    const more = (all.length > 6 ? `The ${6} soonest of ${all.length}. Every swap is in Results below.` : 'Approved first, then the soonest decision.') + waiting;
    const title = all.some((i) => actionState(i) === 'approved') ? 'Swaps to carry out and to decide' : 'Swaps to decide';
    return section(title, cards.length ? `<div class="cf-cards">${cards.join('')}</div>` : `<p class="cf-empty">No swaps to decide.${waiting}</p>`, more);
  },

  list(run, items, selected) {
    if (run.kind === 'rfi-watch') return RFI.list(run, items, selected);
    const shown = items.slice(0, ROWS);
    const audit = run.kind === 'audit-ledger';
    const head = audit
      ? '<th>Pass</th><th>Result</th><th class="cf-num">GB delivered / possible</th><th>Status</th>'
      : run.kind === 'antenna-capacity'
        ? '<th>Day</th><th>Antenna</th><th class="cf-num">GB p10 / p50 / p90</th><th>Passes · minutes</th>'
        : '<th>Time (UTC)</th><th>Pass</th><th class="cf-num">GB p10 / p50 / p90</th><th></th>';
    const rows = shown
      .map((i) => {
        const g = gbOf(i);
        const cells = audit
          ? `<td>${minute(i.target_at)}</td><td>${esc(linkLabel(i))}</td><td class="cf-num">${num(ledgerOf(i).delivered_gb)} / ${num(ledgerOf(i).possible_gb)}</td><td>${ledgerOf(i).dead ? `<span class="cf-status-warn">▲</span> ${esc(ledgerOf(i).status)}` : esc(ledgerOf(i).status)}</td>`
          : run.kind === 'antenna-capacity'
            ? `<td>${esc(i.target_at.slice(0, 10))}</td><td>${esc(i.entity.id)}</td><td class="cf-num">${num(g?.p10)} / <b>${num(g?.p50)}</b> / ${num(g?.p90)}</td><td>${esc(gbOf(i)?.passes)} · ${num(gbOf(i)?.sellable_min, 0)} min</td>`
            : `<td>${minute(i.target_at)}</td><td>${esc(linkLabel(i))}</td><td class="cf-num">${num(g?.p10)} / <b>${num(g?.p50)}</b> / ${num(g?.p90)}</td><td>${i.action || i.severity ? '<span class="cf-status-warn" title="rain-fade risk">▲</span>' : ''}${i.series ? `${esc(i.series.length)} MODCODs` : elevationOf(i) != null ? `${Math.round(elevationOf(i))}°` : ''}</td>`;
        return `<tr data-id="${esc(i.entity.id)}" class="${i.entity.id === selected ? 'cf-sel' : ''}">${cells}</tr>`;
      })
      .join('');
    const note = items.length > ROWS ? `The first ${ROWS} of ${items.length}.` : `${items.length} results.`;
    return section('Results', `<div class="cf-scroll"><table class="cf-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`, `${note} Select a row for its ledger.`);
  },

  ledger(run, items, selected) {
    const item = items.find((i) => i.entity.id === selected) || items[0];
    if (!item) return '';
    const x = item.explain || {};
    const laneRows = Object.entries(x)
      .filter(([, l]) => shown(l?.value) != null)
      .map(([column, l]) => `<tr><td>${esc(column)}</td><td class="cf-num">${num(shown(l.value))} ${esc(l.unit ?? '')}</td><td class="cf-src" ${tip(l.source ?? '')}>${esc(sourceLabel(l.source))}</td></tr>`)
      .join('');
    const g = run.kind === 'rfi-watch' ? null : gbOf(item);
    const plan = item.series?.length ? `<h4>Plan over the pass</h4>${stepChart(item.series)}` : '';
    const range = g
      ? `<div class="cf-range" ${tip(`p10 ${num(g.p10)} · p50 ${num(g.p50)} · p90 ${num(g.p90)} GB`)}><span style="left:${(g.p10 / g.p90) * 100}%;right:0"></span><i style="left:${(g.p50 / g.p90) * 100}%"></i></div><div class="cf-range-labels"><span>p10 ${num(g.p10)}</span><b>p50 ${num(g.p50)} GB</b><span>p90 ${num(g.p90)}</span></div>`
      : '';
    return `<aside class="cf-ledger"><h3>${run.kind === 'rfi-watch' ? 'Why this link is flagged' : 'Why this pass is worth what it is'}</h3><div class="cf-ledger-title">${esc(linkLabel(item))}<span>${minute(item.target_at)}${item.until ? ` – ${hhmm(item.until)}Z` : ''}</span></div>${range}<table class="cf-table cf-compact">${laneRows}</table>${plan}<p class="cf-note">Workflow ${esc(run.workflow)} v${esc(item.workflow_version ?? run.workflow_version)} · run ${esc(run.run_id)}</p></aside>`;
  },
};

// --- the view ------------------------------------------------------------------------------------

const STYLE = `
.cf { --cf-ink: var(--y-foreground-color, #0b0b0b); --cf-muted: var(--y-hint-foreground, #6b6b66); --cf-line: var(--y-border-color, rgba(0,0,0,.1));
  --cf-surface: var(--y-background-color, #fff); --cf-raise: var(--y-detail-pane-background-color, #fafaf9); --cf-series: #2a78d6; --cf-band: rgba(42,120,214,.18);
  --cf-warn: #fab219; --cf-warn-ink: #8a5a00;
  font: 13px/1.45 Roboto, "Helvetica Neue", system-ui, sans-serif; color: var(--cf-ink); background: var(--cf-surface); padding: 20px 24px 48px; position: relative; }
/* Dark hosts (Open MCT's Espresso, Yamcs's dark theme): the same roles stepped for a dark surface. */
.cf[data-theme="dark"] { --cf-ink: #e8e8e6; --cf-muted: #a3a29c; --cf-line: rgba(255,255,255,.12); --cf-surface: #1a1a19;
  --cf-raise: #242423; --cf-series: #3987e5; --cf-band: rgba(57,135,229,.28); --cf-warn: #fab219; --cf-warn-ink: #fab219; }
.cf * { box-sizing: border-box; }
.cf-head { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:4px; }
.cf-head h2 { font-size:20px; font-weight:500; margin:0; }
.cf-forecast { font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--cf-warn-ink); border:1px solid var(--cf-warn); border-radius:999px; padding:1px 8px; }
.cf-meta { color:var(--cf-muted); font-size:12px; }
.cf-cats { display:flex; gap:2px; border-bottom:1px solid var(--cf-line); margin:16px 0 0; }
.cf-cats button { background:none; border:none; border-bottom:2px solid transparent; padding:8px 14px; color:var(--cf-muted); cursor:pointer; font:inherit; }
.cf-cats button[aria-selected="true"] { color:var(--cf-ink); border-bottom-color:var(--cf-ink); font-weight:500; }
.cf-cats button:disabled { opacity:.4; cursor:default; }
.cf-count { font-size:11px; margin-left:6px; color:var(--cf-muted); }
.cf-flows { display:flex; gap:6px; margin:12px 0 4px; flex-wrap:wrap; }
.cf-flows button { border:1px solid var(--cf-line); background:var(--cf-surface); border-radius:6px; padding:4px 10px; cursor:pointer; font:inherit; color:inherit; }
.cf-flows button[aria-pressed="true"] { border-color:var(--cf-ink); }
.cf-grid2 { display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:20px; align-items:start; margin-top:12px; }
@media (max-width: 1100px) { .cf-grid2 { grid-template-columns:1fr; } }
.cf-tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:10px; margin:12px 0 4px; }
.cf-tile { border:1px solid var(--cf-line); border-radius:8px; padding:10px 12px; background:var(--cf-surface); }
.cf-tile-value { font-size:24px; font-weight:500; font-variant-numeric:tabular-nums; }
.cf-tile-label { color:var(--cf-muted); }
.cf-tile-hint { font-size:11px; color:var(--cf-muted); margin-top:2px; }
.cf-section { margin:18px 0 0; }
.cf-section h3, .cf-ledger h3 { font-size:13px; font-weight:500; margin:0 0 6px; }
.cf-ledger h4 { font-size:12px; font-weight:500; margin:12px 0 4px; }
.cf-note { color:var(--cf-muted); font-size:12px; margin:0 0 6px; }
.cf-empty { color:var(--cf-muted); }
.cf-chart { width:100%; height:auto; display:block; overflow:visible; }
.cf-grid { stroke:var(--cf-line); stroke-width:1; }
.cf-axis, .cf-label, .cf-value { font-size:9.5px; fill:var(--cf-muted); }
.cf-chart { max-width:820px; }
.cf-label { fill:var(--cf-ink); }
.cf-band { fill:var(--cf-band); }
.cf-line { fill:none; stroke:var(--cf-series); stroke-width:2; stroke-linejoin:round; }
.cf-bar { fill:var(--cf-series); }
.cf-bar-warn { fill:var(--cf-warn); }
.cf-whisker { stroke:var(--cf-ink); stroke-width:1.5; opacity:.55; }
.cf-dot { fill:var(--cf-series); stroke:var(--cf-surface); stroke-width:2; }
.cf-dot-warn { fill:var(--cf-warn); stroke:var(--cf-surface); stroke-width:2; }
.cf-heat { fill:var(--cf-series); fill-opacity:calc(.08 + var(--share) * .92); }
.cf-hit { fill:transparent; }
.cf-heat-label { font-size:9.5px; fill:var(--cf-ink); }
.cf-on-dark { fill:#fff; }
.cf-step-label { font-size:10px; fill:var(--cf-ink); }
.cf-hit:hover { fill:var(--cf-line); }
g[data-tip]:hover .cf-bar, g[data-tip]:hover .cf-dot { filter:brightness(1.15); }
.cf-cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(260px,1fr)); gap:10px; }
.cf-card { border:1px solid var(--cf-line); border-left:3px solid var(--cf-warn); border-radius:8px; padding:10px 12px; background:var(--cf-surface); }
.cf-card-head { display:flex; gap:6px; align-items:center; margin-bottom:4px; }
.cf-card-head .cf-chip { margin-left:auto; }
.cf-card-action { font-weight:500; margin-top:4px; }
.cf-chip { font-size:11px; border:1px solid var(--cf-line); border-radius:999px; padding:0 8px; color:var(--cf-muted); }
.cf-status-warn { color:var(--cf-warn); }
.cf-scroll { max-height:420px; overflow:auto; border:1px solid var(--cf-line); border-radius:8px; }
.cf-table { border-collapse:collapse; width:100%; }
.cf-table th { position:sticky; top:0; background:var(--cf-raise); font-weight:500; text-align:left; font-size:12px; }
.cf-table th, .cf-table td { padding:6px 10px; border-bottom:1px solid var(--cf-line); }
.cf-table tbody tr { cursor:pointer; }
.cf-table tbody tr:hover { background:var(--cf-raise); }
.cf-table tr.cf-sel { background:var(--cf-band); }
.cf-num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.cf-compact td { padding:5px 6px; font-size:12px; cursor:default; }
.cf-src { color:var(--cf-muted); font-size:11px; overflow-wrap:anywhere; }
.cf-ledger { border:1px solid var(--cf-line); border-radius:8px; padding:12px 14px; background:var(--cf-surface); position:sticky; top:12px; }
.cf-ledger-title { font-weight:500; display:flex; flex-direction:column; margin-bottom:10px; }
.cf-ledger-title span { color:var(--cf-muted); font-weight:400; font-size:12px; }
.cf-range { position:relative; height:10px; background:var(--cf-raise); border-radius:5px; margin:6px 0 2px; }
.cf-range span { position:absolute; top:0; bottom:0; background:var(--cf-band); border-radius:5px; }
.cf-range i { position:absolute; top:-3px; width:3px; height:16px; background:var(--cf-series); border-radius:2px; transform:translateX(-50%); }
.cf-range-labels { display:flex; justify-content:space-between; font-size:11px; color:var(--cf-muted); margin-bottom:8px; }
.cf-range-labels b { color:var(--cf-ink); font-weight:500; }
.cf-tip { position:fixed; pointer-events:none; background:var(--cf-ink); color:var(--cf-surface); font-size:12px; padding:5px 8px; border-radius:6px; max-width:320px; z-index:1000; opacity:0; transition:opacity .08s; }
.cf-error { border:1px solid var(--cf-line); border-left:3px solid #d03b3b; border-radius:8px; padding:10px 12px; margin-top:12px; }
.cf-blank { color:var(--cf-muted); margin-top:24px; }
`;

function categoryOf(kind) {
  return CATEGORIES.find((c) => c.kinds.includes(kind))?.key ?? 'passes';
}

export function mountForecast(element, { load, refreshMs = REFRESH_MS, theme = 'light' }) {
  const state = { workflows: [], category: null, workflow: null, run: null, selected: null, error: null, loaded: false };
  element.innerHTML = '';
  const root = document.createElement('div');
  root.className = 'cf';
  root.dataset.theme = theme;
  const style = document.createElement('style');
  style.textContent = STYLE;
  const tipEl = document.createElement('div');
  tipEl.className = 'cf-tip';
  element.append(style, root, tipEl);

  async function refresh() {
    try {
      const index = await load('index.json');
      state.workflows = index.workflows || [];
      if (!state.workflow || !state.workflows.some((w) => w.workflow === state.workflow)) {
        const first = CATEGORIES.map((c) => state.workflows.find((w) => c.kinds.includes(w.kind))).find(Boolean) || state.workflows[0];
        state.workflow = first?.workflow ?? null;
        state.category = first ? categoryOf(first.kind) : null;
      }
      if (state.workflow) state.run = await load(`workflow-${state.workflow}.json`);
      state.error = null;
    } catch (err) {
      const text = err?.message ?? String(err);
      // A 502, 503 or 504 is the host restarting or its proxy starting up: say so, keep what is
      // shown, and the next refresh clears it.
      state.error = /404|none yet|not found/i.test(text) ? null : /\b50[234]\b/.test(text) ? 'restarting' : text;
    }
    state.loaded = true;
    draw();
  }

  function draw() {
    const { workflows, run, selected, error, category } = state;
    const counts = Object.fromEntries(CATEGORIES.map((c) => [c.key, workflows.filter((w) => c.kinds.includes(w.kind)).length]));
    const cats = CATEGORIES.map(
      (c) => `<button role="tab" data-category="${c.key}" aria-selected="${c.key === category}" ${counts[c.key] ? '' : 'disabled'}>${c.label}<span class="cf-count">${counts[c.key]}</span></button>`,
    ).join('');
    const inCategory = workflows.filter((w) => categoryOf(w.kind) === category);
    const flows =
      inCategory.length > 1
        ? `<div class="cf-flows">${inCategory.map((w) => `<button data-workflow="${esc(w.workflow)}" aria-pressed="${w.workflow === state.workflow}">${esc(TITLES[w.kind] ?? w.kind)} · ${esc(w.workflow)}</button>`).join('')}</div>`
        : '';
    let body = '';
    if (!state.loaded) body = '<p class="cf-blank">Loading forecasts…</p>';
    else if (!workflows.length) body = '<p class="cf-blank">No forecasts yet. They appear here after the next workflow run reaches this system.</p>';
    else if (run) {
      const items = upcoming(run);
      const blocks = run.blocks || ['tiles', 'list', 'ledger'];
      const main = blocks
        .filter((b) => b !== 'ledger')
        .map((b) => (VIEWS[b] ? VIEWS[b](run, items, selected) : `<p class="cf-note">This section needs a newer Constellation Forecast view (${esc(b)}).</p>`))
        .join('');
      const side = blocks.includes('ledger') ? VIEWS.ledger(run, items, selected) : '';
      body = `<p class="cf-meta">${esc(TITLES[run.kind] ?? run.kind)} · workflow ${esc(run.workflow)} v${esc(run.workflow_version)} · run ${esc(run.run_id)} · delivered ${ago(run.delivered_at)}</p>${flows}<div class="cf-grid2"><div>${main}</div><div>${side}</div></div>`;
    }
    root.innerHTML = `<div class="cf-head"><h2>Constellation Forecast</h2><span class="cf-forecast">Forecast · not scheduled contacts</span></div>
      <p class="cf-meta">Predictions from Constellation workflows. Measured telemetry lives in its usual place.</p>
      ${error === 'restarting' ? '<p class="cf-note">Waiting for the host to come back; this refreshes on its own.</p>' : error ? `<div class="cf-error">Could not read the forecasts: ${esc(error)}</div>` : ''}
      ${workflows.length ? `<div class="cf-cats" role="tablist">${cats}</div>` : ''}${body}`;
  }

  root.addEventListener('click', (e) => {
    const cat = e.target.closest('[data-category]');
    if (cat) {
      state.category = cat.dataset.category;
      const first = state.workflows.find((w) => categoryOf(w.kind) === state.category);
      state.workflow = first?.workflow ?? null;
      state.selected = null;
      return void refresh();
    }
    const flow = e.target.closest('[data-workflow]');
    if (flow) {
      state.workflow = flow.dataset.workflow;
      state.selected = null;
      return void refresh();
    }
    const row = e.target.closest('tr[data-id]');
    if (row) {
      state.selected = row.dataset.id;
      draw();
    }
  });
  root.addEventListener('mousemove', (e) => {
    const target = e.target.closest('[data-tip]');
    if (!target) return void (tipEl.style.opacity = '0');
    tipEl.textContent = target.getAttribute('data-tip');
    tipEl.style.left = `${Math.min(e.clientX + 12, window.innerWidth - 330)}px`;
    tipEl.style.top = `${e.clientY + 14}px`;
    tipEl.style.opacity = '1';
  });
  root.addEventListener('mouseleave', () => (tipEl.style.opacity = '0'));

  refresh();
  const timer = setInterval(refresh, refreshMs);
  return { destroy: () => clearInterval(timer), refresh };
}
