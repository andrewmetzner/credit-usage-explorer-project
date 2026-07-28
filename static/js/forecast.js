/**
 * Forecast page logic. All server data arrives via the #forecast-data JSON
 * island; this file is plain cacheable JS with no template interpolation.
 *
 * Functions are intentionally declared at the top level (global) so the inline
 * onclick handlers in the markup resolve. Chart-building IIFEs guard on canvas
 * existence so they self-skip on the no-data page.
 */
'use strict';

const D = (function () {
  const el = document.getElementById('forecast-data');
  try { return el ? JSON.parse(el.textContent) : {}; } catch (_) { return {}; }
})();
D.urls = D.urls || {};

/* ===================================================================== *
 * Snapshot comparison + overlays
 * ===================================================================== */
const selectedSnaps  = new Map();
const seriesCache    = new Map();
const SNAP_DATA_MAP  = {};
(D.snapshots || []).forEach(h => { SNAP_DATA_MAP[snapKey(h)] = h; });
const SNAP_COLORS    = ['#20c997','#6f42c1','#d63384','#ffc107','#0dcaf0','#198754'];
const CURRENT_REMAINING = D.currentRemaining || 0;
let compareChart     = null;

// Cache which snapshots are enabled on the graph (per browser), so reopening
// the forecast page restores the same overlay instead of starting blank.
// Uses the 'fc-' prefix so "Reset all chart settings" sweeps it along with
// every other saved preference.
const SNAP_SELECTION_KEY = 'fc-selected-snapshots';
function persistSelectedSnaps() {
  // Snapshot mode is a transient URL-driven preset — never persist its
  // auto-selection, or it would leak into the next live-view session.
  if (D.snapshotTs) return;
  try { localStorage.setItem(SNAP_SELECTION_KEY, JSON.stringify([...selectedSnaps.keys()])); } catch (_) {}
}

function fmtN(v) { const n = parseFloat(v); return isNaN(n) ? '—' : Math.round(n).toLocaleString(); }
function fmtPct(v) { const n = parseFloat(v); return isNaN(n) ? '—' : Math.round(n * 100) + '%'; }
function truncLabel(s, max) { max = max || 12; return s && s.length > max ? s.slice(0, max) + '…' : (s || ''); }

// Strip the "Week of " prefix the weekly snapshots carry so legend / tooltip
// rows read as plain dates (e.g. "Week of 2026-05-03 · forecast" → "2026-05-03 · forecast").
function cleanLegendLabel(s) { return String(s == null ? '' : s).replace(/^week of:?\s*/i, ''); }

// A snapshot's display name: weekly snapshots collapse to just their date;
// user-named snapshots keep their custom label.
function snapDisplayName(h) {
  const l = (h && h.label) || '';
  return (!l || /^week of/i.test(l)) ? (h && h.snapshot_date) || '' : l;
}

/* The Show/hide dropdown menu is the single control surface for the burndown
 * chart (the built-in Chart.js legend is off unless the user turns on the
 * on-chart legend for PNG export). It renders rows for the projection models,
 * the actual/what-if lines, and one row per selected snapshot — each snapshot
 * row carrying its own MC and ML toggles. */
function _modelOn(id) {
  const v = localStorage.getItem('forecast-model-' + id);
  return id === 'deterministic' ? v !== '0' : v === '1';
}

function _menuHeader(text) {
  const h = document.createElement('div');
  h.className = 'fc-menu-header';
  h.textContent = text;
  return h;
}

// A toggle row: check box indicator + colour swatch + label, plus optional
// trailing controls (used for the per-snapshot MC / ML buttons).
function _menuRow(label, color, on, onClick, title, trailing) {
  const row = document.createElement('div');
  row.className = 'fc-menu-row';
  if (title) row.title = title;
  const left = document.createElement('span');
  left.className = 'fc-menu-left';
  left.style.cursor = 'pointer';
  left.innerHTML =
    `<span class="fc-menu-check">${on ? '&#10003;' : ''}</span>` +
    `<span class="fc-legend-swatch" style="color:${color};"></span>` +
    `<span class="fc-menu-label" style="${on ? '' : 'opacity:.55;'}">${label}</span>`;
  left.addEventListener('click', onClick);
  row.appendChild(left);
  if (trailing) row.appendChild(trailing);
  return row;
}

function _snapBandBtn(text, activeColor, active, onClick, title) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = text;
  b.title = title;
  b.className = 'fc-snap-band-btn' + (active ? ' is-on' : '');
  b.style.setProperty('--band-color', activeColor);
  b.addEventListener('click', e => { e.stopPropagation(); onClick(); });
  return b;
}

function refreshBurndownLegend() {
  const bc = window.burndownChart;
  const menu = document.getElementById('burndownSeriesMenu');
  if (!bc || !menu) return;
  const chart = bc.chart || bc;
  const datasets = chart.data.datasets || [];
  menu.innerHTML = '';

  // ── Forecast models (load / remove on click) ──
  // In snapshot mode the page's live models are hidden — the forecast comes
  // from the snapshot's own frozen Base/MC/ML in the Snapshots section below.
  // Monte Carlo / Linear Trend work in "view other contracts" mode too —
  // /forecast/model-data accepts a contract_id and runs the forecast engine
  // against that contract's own dates/ledger (see window._fcOtherContract).
  if (!D.snapshotTs) {
    menu.appendChild(_menuHeader('Forecasts'));
    [
      { id: 'deterministic',     name: 'Base',              color: getChartColor('proj') },
      { id: 'monte_carlo',       name: 'Monte Carlo',       color: '#fd7e14' },
      { id: 'linear_regression', name: 'Linear Trend (ML)', color: '#198754' },
    ].forEach(m => {
      const on = _modelOn(m.id);
      menu.appendChild(_menuRow(m.name, m.color, on,
        () => { window.toggleForecastModel(m.id, !on); refreshBurndownLegend(); },
        on ? 'Hide ' + m.name : 'Show ' + m.name));
    });
  }

  // ── Other displayed lines (visibility toggle): actual, what-if ──
  // Model main lines are covered above; their bands carry _noLegend. Snapshot
  // datasets are handled in their own section below.
  const skip = new Set(['Projected remaining', 'MC P50 (median)', 'Linear Trend (ML)']);
  const lineRows = [];
  datasets.forEach((d, i) => {
    if (d._noLegend || d._snapOverlay || skip.has(d.label)) return;
    const meta = chart.getDatasetMeta(i);
    const hidden = meta.hidden === true;
    const color = (typeof d.borderColor === 'string' && d.borderColor) ||
                  (typeof d.backgroundColor === 'string' && d.backgroundColor) || '#888';
    lineRows.push(_menuRow(cleanLegendLabel(d.label), color, !hidden, () => {
      const mm = chart.getDatasetMeta(i);
      const show = mm.hidden === true;
      // Actual remaining routes through its Advanced-settings toggle so the
      // checkbox there and the persisted preference stay in sync.
      if (d.label === 'Actual remaining' && typeof window.toggleActualLine === 'function') {
        window.toggleActualLine(show);
        return;
      }
      mm.hidden = !show;
      chart.update();
      refreshBurndownLegend();
    }, 'Show / hide'));
  });
  if (lineRows.length) {
    menu.appendChild(_menuHeader('Lines'));
    lineRows.forEach(r => menu.appendChild(r));
  }

  // ── Snapshots: one row each, with independent MC + ML toggles ──
  if (typeof selectedSnaps !== 'undefined' && selectedSnaps.size > 0) {
    menu.appendChild(_menuHeader('Snapshots'));
    let i = 0;
    selectedSnaps.forEach((h, key) => {
      const color = h.color || SNAP_COLORS[i % SNAP_COLORS.length];
      const name = truncLabel(snapDisplayName(h), 18);
      const trailing = document.createElement('span');
      trailing.className = 'fc-snap-bands';
      trailing.appendChild(_snapBandBtn('Base', color, !snapBaseOff.has(key),
        () => toggleSnapBaseFor(key), 'Base forecast line for this snapshot'));
      trailing.appendChild(_snapBandBtn('MC', '#fd7e14', snapMcKeys.has(key),
        () => toggleSnapMcFor(key), 'Monte Carlo bands for this snapshot'));
      trailing.appendChild(_snapBandBtn('ML', '#198754', snapMlKeys.has(key),
        () => toggleSnapMlFor(key), 'Linear-trend (ML) projection for this snapshot'));
      // "As of this week" view: hide the actual line FROM the day the
      // snapshot's forecast starts — that day belongs to the forecast, so
      // hovering the seam shows one value, not actual + forecast twins.
      const fcStart = String(h.latest_usage_date || h.snapshot_date || '').slice(0, 10);
      const cutDay = fcStart ? shiftDayStr(fcStart, -1) : '';
      const cutEl = document.getElementById('actual-cutoff-to');
      trailing.appendChild(_snapBandBtn('Cut', '#d63384', !!(cutEl && cutDay && cutEl.value === cutDay),
        () => toggleSnapCutFor(cutDay),
        "Hide the actual line after this snapshot's data (up to the day before its forecast starts) — view the chart as it looked back then"));
      if (h.snapshot_ts) {
        trailing.appendChild(_snapBandBtn('CSV', '#6c757d', false, () => {
          window.location.href = D.urls.snapshotExport + '?ts=' + encodeURIComponent(h.snapshot_ts);
        }, "Download this snapshot's full series as CSV — actual up to that week plus its forecast and bands"));
      }
      // Row label is informational; the Base / MC / ML buttons do the per-snapshot work.
      const row = _menuRow(name, color, true, e => e.preventDefault(),
        'Add / remove snapshots from the Snapshots dropdown', trailing);
      row.querySelector('.fc-menu-left').style.cursor = 'default';
      const chk = row.querySelector('.fc-menu-check');
      if (chk) chk.innerHTML = '';   // not a toggle — Base / MC / ML buttons carry the action
      menu.appendChild(row);
      i++;
    });
  }
}
window.refreshBurndownLegend = refreshBurndownLegend;

function shiftDayStr(dstr, n) {
  const d = new Date(dstr + 'T12:00:00');
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}

// Chart-mode values that mean "the burndown line/labels/markers are built
// from the active contract's own data" (current, chained, or overall) —
// as opposed to a specific OTHER contract's id ("view other contracts"),
// which is a fully independent reconstruction. Shared by both the main
// chart IIFE and the separate MC/ML overlay IIFE (via window._fcChartMode).
function isMainChartMode(mode) {
  return mode === 'current' || mode === 'chained' || mode === 'overall';
}

// "Cut" toggle on a snapshot row: set the actual line's hide-after cutoff to
// the day before that snapshot's forecast starts (click again to clear) — the
// chart then shows what was known as of that week, the forecast owning its
// start day outright.
function toggleSnapCutFor(cutDay) {
  const toEl = document.getElementById('actual-cutoff-to');
  if (!toEl || !cutDay) return;
  toEl.value = toEl.value === cutDay ? '' : cutDay;
  if (typeof window.applyActualCutoffs === 'function') window.applyActualCutoffs();
  refreshBurndownLegend();
}

/* ===================================================================== *
 * "View as of a date" — one control that ties together snapshot select,
 * Cut, and CSV export: pick a date, land on the nearest snapshot at or
 * before it, see the chart as it looked then, and grab its data sheet.
 * ===================================================================== */
let viewAsOfKey = null;

function _removeViewAsOfSnap() {
  if (!viewAsOfKey || !selectedSnaps.has(viewAsOfKey)) { viewAsOfKey = null; return; }
  const row = findRowByKey(viewAsOfKey);
  if (row) { toggleRow(row); } else { selectedSnaps.delete(viewAsOfKey); renderComparePanel(); }
  viewAsOfKey = null;
}

window.applyViewAsOf = function () {
  const dateEl = document.getElementById('view-as-of-date');
  const statusEl = document.getElementById('view-as-of-status');
  const date = dateEl && dateEl.value;
  if (!date) return;

  const candidates = (D.snapshots || []).filter(h => (h.snapshot_date || '') <= date);
  if (!candidates.length) {
    if (statusEl) statusEl.textContent = 'No snapshot on or before that date.';
    return;
  }
  candidates.sort((a, b) => (a.snapshot_date < b.snapshot_date ? 1 : -1));
  const h = candidates[0];
  const key = snapKey(h);

  if (viewAsOfKey && viewAsOfKey !== key) _removeViewAsOfSnap();
  viewAsOfKey = key;

  if (!selectedSnaps.has(key)) {
    const wasEmpty = selectedSnaps.size === 0;
    selectedSnaps.set(key, h);
    fetchSnapSeries(h);
    if (wasEmpty) {
      const target = document.getElementById('burndown-chart-section');
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
  renderComparePanel();

  // Cut the actual line right where this snapshot's forecast starts, same
  // as its row's Cut button — "view as of" means seeing only what was known
  // by then.
  const fcStart = String(h.latest_usage_date || h.snapshot_date || '').slice(0, 10);
  const cutDay = fcStart ? shiftDayStr(fcStart, -1) : '';
  const toEl = document.getElementById('actual-cutoff-to');
  if (toEl && cutDay && toEl.value !== cutDay) {
    toEl.value = cutDay;
    if (typeof window.applyActualCutoffs === 'function') window.applyActualCutoffs();
  }

  if (statusEl) {
    const label = snapDisplayName(h);
    const csvUrl = h.snapshot_ts ? (D.urls.snapshotExport + '?ts=' + encodeURIComponent(h.snapshot_ts)) : '';
    statusEl.innerHTML = 'Showing ' + label
      + (csvUrl ? ' &middot; <a href="' + csvUrl + '">Download data sheet</a>' : '');
  }
  refreshBurndownLegend();
};

window.clearViewAsOf = function () {
  const dateEl = document.getElementById('view-as-of-date');
  const statusEl = document.getElementById('view-as-of-status');
  if (dateEl) dateEl.value = '';
  if (statusEl) statusEl.textContent = '';
  // Remove the view-as-of snapshot from the comparison FIRST — clearing the
  // cutoff now reloads the page (see applyActualCutoffs), and it snapshots
  // the current selection into sessionStorage right before doing so, so
  // that has to reflect the removal, not the stale pre-removal selection.
  _removeViewAsOfSnap();
  if (typeof window.clearActualCutoffs === 'function') window.clearActualCutoffs();
};

// Download the selected snapshots' history rows as CSV (all when none picked).
window.exportSelectedSnapshots = function () {
  const params = new URLSearchParams();
  if (typeof selectedSnaps !== 'undefined') {
    selectedSnaps.forEach((h, key) => params.append('key', key));
  }
  const qs = params.toString();
  window.location.href = D.urls.historyExport + (qs ? '?' + qs : '');
};

function snapRemainingAt(h, dateStr) {
  const snap   = new Date(h.snapshot_date);
  const target = new Date(dateStr);
  const weeks  = (target - snap) / (7 * 24 * 3600 * 1000);
  if (weeks < 0) return null;
  return Math.max(parseFloat(h.credits_remaining || 0) - parseFloat(h.forecast_weekly_burn || 0) * weeks, 0);
}

async function fetchSnapSeries(snap) {
  const ts = snap.snapshot_ts;
  if (!ts || seriesCache.has(ts)) { updateBurndownOverlays(); return; }
  try {
    const resp = await fetch(D.urls.snapshotSeries + '?ts=' + encodeURIComponent(ts));
    if (!resp.ok) return;
    seriesCache.set(ts, await resp.json());
    updateBurndownOverlays();
    renderComparePanel();
  } catch (_) {}
}

// "Show negative credits" (Advanced settings toggle): let a forecast line
// keep declining past zero using its own last real slope, instead of
// stopping dead once the underlying (clamped/truncated) source data runs
// out. Purely a display extrapolation on the already-plotted arrays — never
// touches the model math, the KPI numbers, or stored snapshot data.
function showNegativeCredits() {
  return localStorage.getItem('fc-show-negative') === '1';
}
function extendBelowZero(data) {
  if (!showNegativeCredits() || !data || !data.length) return data;
  let lastPos = -1;
  for (let i = 0; i < data.length; i++) {
    if (data[i] != null && data[i] > 0) lastPos = i;
  }
  if (lastPos < 0 || lastPos >= data.length - 1) return data;
  let prevIdx = -1;
  for (let j = lastPos - 1; j >= 0; j--) {
    if (data[j] != null) { prevIdx = j; break; }
  }
  if (prevIdx < 0 || prevIdx === lastPos) return data;
  const slope = (data[lastPos] - data[prevIdx]) / (lastPos - prevIdx);
  if (!(slope < 0)) return data;
  const out = data.slice();
  for (let i = lastPos + 1; i < out.length; i++) {
    out[i] = data[lastPos] + slope * (i - lastPos);
  }
  return out;
}

function interpDateSeries(pts, allLabels, dateKey, valKey) {
  dateKey = dateKey || 'date';
  valKey = valKey || 'value';
  if (!pts || !pts.length) return allLabels.map(() => null);
  const start = pts[0][dateKey], end = pts[pts.length - 1][dateKey];
  return allLabels.map(l => {
    if (l < start || l > end) return null;
    const idx = pts.findIndex(p => p[dateKey] > l);
    if (idx === -1) return pts[pts.length - 1][valKey];
    if (idx === 0)  return pts[0][valKey];
    const a = pts[idx - 1], b = pts[idx];
    const t = (new Date(l) - new Date(a[dateKey])) / (new Date(b[dateKey]) - new Date(a[dateKey]));
    return a[valKey] + t * (b[valKey] - a[valKey]);
  });
}

function updateBurndownOverlays() {
  const bc = window.burndownChart;
  if (!bc) return;
  bc.data.datasets = bc.data.datasets.filter(d => !d._snapOverlay);
  const snaps = [...selectedSnaps.values()];
  let maxRemaining = window.burndownMaxY || 0;
  const allLabels  = window.burndownLabels || [];

  // Drop per-snapshot flags for snapshots that are no longer selected.
  const liveKeys = new Set(snaps.map(h => snapKey(h)));
  [...snapBaseOff].forEach(k => { if (!liveKeys.has(k)) snapBaseOff.delete(k); });
  [...snapMcKeys].forEach(k => { if (!liveKeys.has(k)) snapMcKeys.delete(k); });
  [...snapMlKeys].forEach(k => { if (!liveKeys.has(k)) snapMlKeys.delete(k); });

  // The blue actual line's value at each label — snapshot overlays snap their
  // first plotted point onto it so the dashed forecast/MC/ML rides seamlessly
  // out of the actual instead of starting at a slightly different frozen value
  // (weekly-vs-daily, or a resynced remaining). The forecast's first point is
  // redundant with the actual, so it's also dropped from the hover.
  const actualDs = bc.data.datasets.find(d => d.label === 'Actual remaining');
  const actualByLabel = {};
  if (actualDs && Array.isArray(actualDs.data)) {
    allLabels.forEach((l, j) => { if (actualDs.data[j] != null) actualByLabel[l] = actualDs.data[j]; });
  }
  // Shift a snapshot overlay group vertically so its first plotted point lands
  // on the blue actual line — connecting seamlessly WHILE preserving the
  // forecast shape (decline rate, band width). Moving only the first point
  // instead left the interpolated 2nd point high, drawing a spurious up-tick
  // before the decline (visible in daily view). Pass the primary line first
  // (its offset drives the whole group); extra arrays (MC/ML bands) shift by
  // the same delta so the band doesn't distort. Returns the anchor label for
  // tooltip suppression. No-op when the actual doesn't reach the anchor.
  function alignToActual(primary, ...others) {
    const idx = primary.findIndex(v => v !== null && v !== undefined);
    if (idx < 0) return null;
    const label = allLabels[idx];
    const target = actualByLabel[label];
    if (target == null || primary[idx] == null) return label;
    const delta = target - primary[idx];
    [primary, ...others].forEach(arr => {
      for (let j = 0; j < arr.length; j++) if (arr[j] != null) arr[j] = Math.max(arr[j] + delta, 0);
    });
    return label;
  }

  snaps.forEach((h, i) => {
    maxRemaining = Math.max(maxRemaining, parseFloat(h.credits_remaining || 0));
    const key       = snapKey(h);
    const color     = h.color || SNAP_COLORS[i % SNAP_COLORS.length];
    const snapLabel = truncLabel(snapDisplayName(h), 16);
    const series    = seriesCache.get(h.snapshot_ts);
    const fb        = series && series.forecast_burndown && series.forecast_burndown.length
      ? series.forecast_burndown : null;

    if (fb) {
      if (!snapBaseOff.has(key)) {
        let forecastData = interpDateSeries(fb, allLabels, 'date', 'remaining');
        const anchorLabel = alignToActual(forecastData);
        forecastData = extendBelowZero(forecastData);
        const startIdx = forecastData.findIndex(v => v !== null);
        // In snapshot mode the base forecast is RED like the live "Projected
        // remaining"; in comparison mode it keeps the snapshot's own color so
        // multiple snapshots stay distinguishable.
        const baseColor = D.snapshotTs ? (getChartColor('proj') || '#dc3545') : color;
        bc.data.datasets.push({
          label: snapLabel + ' · forecast',
          data: forecastData,
          borderColor: baseColor, borderDash: [5, 4], borderWidth: 2,
          backgroundColor: 'transparent', fill: false, tension: 0.05,
          pointRadius: forecastData.map((v, j) => j === startIdx ? 7 : 0),
          pointBackgroundColor: baseColor,
          spanGaps: false, _snapOverlay: true, _baseOverlay: true, _snapAnchorLabel: anchorLabel,
        });
      }

      const mc = series && series.mc;
      if (snapMcKeys.has(key) && mc && mc.p50 && mc.p50.length) {
        let p10d = interpDateSeries(mc.p10 || [], allLabels);
        let p50d = interpDateSeries(mc.p50,       allLabels);
        let p90d = interpDateSeries(mc.p90 || [], allLabels);
        // Connect the band to the actual at the anchor (P50 drives the shift;
        // P10/P90 move with it so the band width is preserved).
        const mcAnchor = alignToActual(p50d, p10d, p90d);
        p10d = extendBelowZero(p10d);
        p50d = extendBelowZero(p50d);
        p90d = extendBelowZero(p90d);
        // Same orange as the live Monte Carlo so snapshot MC reads the same.
        const MC = getChartColor('mc') || '#fd7e14';
        // P90/P10 stay out of the legend but DO show in the hover tooltip so a
        // snapshot's MC band reads as numbers, not just the P50 line.
        bc.data.datasets.push({
          label: snapLabel + ' · MC P90', data: p90d,
          borderColor: hexToRgba(MC, 0.55), borderWidth: 1, borderDash: [2, 3],
          backgroundColor: hexToRgba(MC, 0.14), fill: '+2', tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true, _mcOverlay: true, _noLegend: true, _snapAnchorLabel: mcAnchor,
        });
        bc.data.datasets.push({
          label: snapLabel + ' · MC P50', data: p50d,
          borderColor: MC, borderWidth: 2, borderDash: [4, 3],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true, _mcOverlay: true, _snapAnchorLabel: mcAnchor,
        });
        bc.data.datasets.push({
          label: snapLabel + ' · MC P10', data: p10d,
          borderColor: hexToRgba(MC, 0.55), borderWidth: 1, borderDash: [2, 3],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true, _mcOverlay: true, _noLegend: true, _snapAnchorLabel: mcAnchor,
        });
      }

      const ml = series && series.ml;
      if (snapMlKeys.has(key) && ml && ml.p50 && ml.p50.length) {
        let m10 = interpDateSeries(ml.p10 || [], allLabels);
        let m50 = interpDateSeries(ml.p50,       allLabels);
        let m90 = interpDateSeries(ml.p90 || [], allLabels);
        const mlAnchor = alignToActual(m50, m10, m90);
        m10 = extendBelowZero(m10);
        m50 = extendBelowZero(m50);
        m90 = extendBelowZero(m90);
        // Same green as the live Linear Trend (ML).
        const ML = getChartColor('ml') || '#198754';
        bc.data.datasets.push({
          label: snapLabel + ' · ML P90', data: m90,
          borderColor: hexToRgba(ML, 0.4), borderWidth: 1, borderDash: [1, 3],
          backgroundColor: hexToRgba(ML, 0.10), fill: '+2', tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true, _lrOverlay: true, _noTooltip: true, _noLegend: true, _snapAnchorLabel: mlAnchor,
        });
        bc.data.datasets.push({
          label: snapLabel + ' · ML trend', data: m50,
          borderColor: ML, borderWidth: 2, borderDash: [1, 2],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true, _lrOverlay: true, _snapAnchorLabel: mlAnchor,
        });
        bc.data.datasets.push({
          label: snapLabel + ' · ML P10', data: m10,
          borderColor: hexToRgba(ML, 0.4), borderWidth: 1, borderDash: [1, 3],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true, _lrOverlay: true, _noTooltip: true, _noLegend: true, _snapAnchorLabel: mlAnchor,
        });
      }
    } else if (!snapBaseOff.has(key)) {
      let data     = allLabels.map(l => snapRemainingAt(h, l));
      const anchorLabel = alignToActual(data);
      data = extendBelowZero(data);
      const firstIdx = data.findIndex(v => v !== null);
      bc.data.datasets.push({
        label: snapLabel + ' · forecast', data,
        borderColor: color, borderDash: [4, 3], borderWidth: 2,
        backgroundColor: 'transparent', fill: false, tension: 0.05,
        pointRadius:          data.map((v, j) => j === firstIdx ? 7 : 0),
        pointBackgroundColor: data.map((v, j) => j === firstIdx ? color : 'transparent'),
        spanGaps: false, _snapOverlay: true, _baseOverlay: true, _snapAnchorLabel: anchorLabel,
      });
    }
  });

  bc.update();
  refreshBurndownLegend();
}

function forecastAccuracyHtml(h) {
  const series = seriesCache.get(h.snapshot_ts);
  if (!series || !series.forecast_burndown || !series.forecast_burndown.length) return '';
  const todayStr = new Date().toISOString().slice(0, 10);
  if (h.snapshot_date >= todayStr) return '';

  const fb  = series.forecast_burndown;
  let predictedNow;
  const idx = fb.findIndex(p => p.date >= todayStr);
  if (idx === -1) {
    predictedNow = fb[fb.length - 1].remaining;
  } else if (idx === 0) {
    predictedNow = fb[0].remaining;
  } else {
    const a = fb[idx - 1], b = fb[idx];
    const t = (new Date(todayStr) - new Date(a.date)) / (new Date(b.date) - new Date(a.date));
    predictedNow = a.remaining + t * (b.remaining - a.remaining);
  }

  const error    = predictedNow - CURRENT_REMAINING;
  const errorPct = CURRENT_REMAINING > 0 ? error / CURRENT_REMAINING * 100 : 0;
  const sign     = error >= 0 ? '+' : '';
  const cls      = Math.abs(errorPct) < 5 ? 'text-success' : Math.abs(errorPct) < 15 ? 'text-warning' : 'text-danger';
  const note     = error > 0 ? 'forecast overestimated remaining' : 'forecast underestimated remaining';

  return `<div style="margin-top:.5rem;padding-top:.5rem;border-top:1px solid #eee;">
    <div style="font-size:.65rem;font-weight:700;color:#8a92a0;letter-spacing:.06em;margin-bottom:.25rem;">FORECAST ACCURACY TODAY</div>
    <div class="d-flex justify-content-between"><span class="text-muted">Predicted now</span><strong>${fmtN(predictedNow)}</strong></div>
    <div class="d-flex justify-content-between mt-1"><span class="text-muted">Actual now</span><strong>${fmtN(CURRENT_REMAINING)}</strong></div>
    <div class="d-flex justify-content-between mt-1"><span class="text-muted">Error</span><strong class="${cls}">${sign}${fmtN(error)} (${sign}${Math.round(errorPct)}%)</strong></div>
    <div style="font-size:.65rem;color:#8a92a0;margin-top:.2rem;">${note}</div>
  </div>`;
}

// Comparison bar chart is off by default (the cards already show the data);
// the "Chart" checkbox in the compare panel turns it on.
let showCompareChart = false;
function toggleCompareChart(on) {
  showCompareChart = !!on;
  updateCompareChart([...selectedSnaps.values()]);
}

function updateCompareChart(snaps) {
  const wrap = document.getElementById('compare-chart-wrap');
  if (!showCompareChart || snaps.length < 2) {
    if (wrap) wrap.style.display = 'none';
    if (compareChart) { compareChart.destroy(); compareChart = null; }
    return;
  }
  wrap.style.display = '';
  const labels   = snaps.map(h => h.label ? truncLabel(h.label) : h.snapshot_date);
  const burnData = snaps.map(h => parseFloat(h.forecast_weekly_burn || 0));
  const remData  = snaps.map(h => parseFloat(h.credits_remaining || 0));
  const balData  = snaps.map(h => parseFloat(h.forecast_contract_end_balance || 0));
  const datasets = [
    { label: 'Weekly Burn',       data: burnData, backgroundColor: 'rgba(13,110,253,0.7)' },
    { label: 'Credits Remaining', data: remData,  backgroundColor: 'rgba(108,117,125,0.5)' },
    { label: 'End Balance',       data: balData,  backgroundColor: balData.map(v => v < 0 ? 'rgba(220,53,69,0.7)' : 'rgba(25,135,84,0.7)') },
  ];
  if (compareChart) { compareChart.destroy(); compareChart = null; }
  compareChart = new Chart(document.getElementById('compare-chart'), {
    type: 'bar', data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: true, position: 'top', labels: { font: { size: 11 }, boxWidth: 12 } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: ${Math.round(ctx.raw ?? 0).toLocaleString()}` } },
      },
      scales: {
        y: { ticks: { callback: v => v.toLocaleString(), font: { size: 10 } }, grid: { color: 'rgba(0,0,0,.05)' } },
        x: { ticks: { font: { size: 10 } }, grid: { display: false } },
      },
    },
  });
}

function renderComparePanel() {
  persistSelectedSnaps();
  const panel = document.getElementById('compare-panel');
  const grid  = document.getElementById('compare-grid');
  const count = document.getElementById('compare-count');
  const snaps = [...selectedSnaps.values()];
  const n     = snaps.length;
  if (panel) panel.style.display = n >= 2 ? '' : 'none';
  updateBurndownOverlays();
  syncQuickSelect();
  if (n < 2) { updateCompareChart([]); return; }
  if (count) count.textContent = n;
  if (!grid) { updateCompareChart(snaps); return; }
  grid.innerHTML = snaps.map((h, i) => {
    const color = h.color || SNAP_COLORS[i % SNAP_COLORS.length];
    return `<div class="col-sm-6 col-lg-3">
      <div style="background:#fff;border:2px solid ${color};border-radius:8px;padding:.75rem .9rem;font-size:.78rem;">
        <div style="font-weight:700;color:#1a1d23;margin-bottom:.1rem;">${h.snapshot_date}</div>
        ${h.label ? `<div style="font-size:.72rem;color:#6c757d;margin-bottom:.35rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${h.label}">${h.label}</div>` : '<div style="margin-bottom:.35rem;"></div>'}
        <div class="d-flex justify-content-between"><span class="text-muted">Pacing</span><span class="pacing-pill ${h.pacing_status}" style="font-size:.68rem;padding:.1rem .4rem;">${(h.pacing_status||'').replace(/_/g,' ')}</span></div>
        <div class="d-flex justify-content-between mt-1"><span class="text-muted">Forecast</span><span class="pacing-pill ${h.forecast_status}" style="font-size:.68rem;padding:.1rem .4rem;">${(h.forecast_status||'').replace(/_/g,' ')}</span></div>
        <div class="d-flex justify-content-between mt-1"><span class="text-muted">Weekly burn</span><strong>${fmtN(h.forecast_weekly_burn)}</strong></div>
        <div class="d-flex justify-content-between mt-1"><span class="text-muted">Remaining</span><strong>${fmtN(h.credits_remaining)}</strong></div>
        <div class="d-flex justify-content-between mt-1"><span class="text-muted">End balance</span><strong class="${parseFloat(h.forecast_contract_end_balance||0)<0?'text-danger':'text-success'}">${fmtN(h.forecast_contract_end_balance)}</strong></div>
        <div class="d-flex justify-content-between mt-1"><span class="text-muted">Exhaustion</span><strong>${h.forecast_exhaustion_date||'—'}</strong></div>
        ${h.mc_exhaustion_prob ? `<div class="d-flex justify-content-between mt-1"><span class="text-muted">MC Risk</span><strong class="${parseFloat(h.mc_exhaustion_prob)>0.5?'text-danger':parseFloat(h.mc_exhaustion_prob)>0.1?'text-warning':'text-success'}">${fmtPct(h.mc_exhaustion_prob)}</strong></div>` : ''}
        ${forecastAccuracyHtml(h)}
      </div></div>`;
  }).join('');
  updateCompareChart(snaps);
}

function snapKey(snap) { return snap.snapshot_ts || (snap.snapshot_date + '|' + (snap.label || '')); }

function setSnapColor(input) {
  const color = input.value;
  const row = input.closest('tr');
  if (row) {
    try {
      const snap = JSON.parse(row.dataset.snap || '{}');
      snap.color = color;
      row.dataset.snap = JSON.stringify(snap);
      const key = snapKey(snap);
      if (selectedSnaps.has(key)) {
        selectedSnaps.set(key, snap);
        renderComparePanel();
      }
    } catch (_) {}
  }
  fetch(D.urls.color, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      snapshot_ts: input.dataset.ts,
      snapshot_date: input.dataset.date,
      label: input.dataset.label,
      color,
    }),
  });
}

function toggleRow(tr) {
  const snap = JSON.parse(tr.dataset.snap);
  const cb   = tr.querySelector('.history-cb');
  const key  = snapKey(snap);
  if (selectedSnaps.has(key)) {
    selectedSnaps.delete(key);
    tr.style.background = '';
    if (cb) cb.checked = false;
  } else {
    const wasEmpty = selectedSnaps.size === 0;
    selectedSnaps.set(key, snap);
    tr.style.background = SNAP_COLORS[(selectedSnaps.size - 1) % SNAP_COLORS.length] + '22';
    if (cb) cb.checked = true;
    fetchSnapSeries(snap);
    if (wasEmpty) {
      const target = document.getElementById('burndown-chart-section');
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
  renderComparePanel();
}

function clearComparison() {
  selectedSnaps.clear();
  document.querySelectorAll('.history-row').forEach(r => {
    r.style.background = '';
    const cb = r.querySelector('.history-cb');
    if (cb) cb.checked = false;
  });
  renderComparePanel();
}

function selectAllSnaps() {
  document.querySelectorAll('.history-row').forEach(tr => {
    const snap = (() => { try { return JSON.parse(tr.dataset.snap); } catch(_){return null;} })();
    if (!snap) return;
    const key = snapKey(snap);
    if (!selectedSnaps.has(key)) toggleRow(tr);
  });
}

// ---- Quick-select dropdown (below the chart) ----
function findRowByKey(key) {
  return [...document.querySelectorAll('.history-row')].find(tr => {
    try { return snapKey(JSON.parse(tr.dataset.snap)) === key; } catch (_) { return false; }
  });
}

function quickToggle(cb) {
  const key = cb.dataset.key;
  const row = findRowByKey(key);
  if (row) {
    toggleRow(row);
    return;
  }
  // No DOM row (table removed) — drive selection directly from data map
  const snap = SNAP_DATA_MAP[key];
  if (!snap) return;
  if (selectedSnaps.has(key)) {
    selectedSnaps.delete(key);
  } else {
    selectedSnaps.set(key, snap);
    fetchSnapSeries(snap);
  }
  renderComparePanel();
}

function quickSelectAll() {
  const rows = document.querySelectorAll('.history-row');
  if (rows.length > 0) {
    selectAllSnaps();
  } else {
    Object.keys(SNAP_DATA_MAP).forEach(key => {
      if (!selectedSnaps.has(key)) {
        selectedSnaps.set(key, SNAP_DATA_MAP[key]);
        fetchSnapSeries(SNAP_DATA_MAP[key]);
      }
    });
    renderComparePanel();
  }
}
function quickSelectNone() {
  selectedSnaps.clear();
  renderComparePanel();
}

// Per-snapshot overlays: each selected snapshot independently controls its base
// forecast line, its Monte Carlo bands, and its linear-trend (ML) projection.
// The base line is shown by default, so we track the snapshots whose base is OFF.
const snapBaseOff = new Set();
const snapMcKeys = new Set();
const snapMlKeys = new Set();

// Restore per-snapshot overlay state cached in this browser (which
// snapshots have Base off / MC on / ML on), paired with the snapshot
// selection restored below.
(function () {
  const load = key => { try { return JSON.parse(localStorage.getItem(key) || '[]') || []; } catch (_) { return []; } };
  load('fc-snap-base-off').forEach(k => snapBaseOff.add(k));
  load('fc-snap-mc').forEach(k => snapMcKeys.add(k));
  load('fc-snap-ml').forEach(k => snapMlKeys.add(k));
})();
function persistSnapshotOverlayState() {
  // Transient in snapshot mode — don't leak the preset's overlays into live.
  if (D.snapshotTs) return;
  try {
    localStorage.setItem('fc-snap-base-off', JSON.stringify([...snapBaseOff]));
    localStorage.setItem('fc-snap-mc', JSON.stringify([...snapMcKeys]));
    localStorage.setItem('fc-snap-ml', JSON.stringify([...snapMlKeys]));
  } catch (_) {}
}

function toggleSnapBaseFor(key) {
  if (snapBaseOff.has(key)) snapBaseOff.delete(key); else snapBaseOff.add(key);
  persistSnapshotOverlayState();
  updateBurndownOverlays();
  refreshBurndownLegend();
}

function toggleSnapMcFor(key) {
  if (snapMcKeys.has(key)) {
    snapMcKeys.delete(key);
  } else {
    snapMcKeys.add(key);
    const h = selectedSnaps.get(key);
    if (h) fetchSnapSeries(h);
  }
  persistSnapshotOverlayState();
  updateBurndownOverlays();
  refreshBurndownLegend();
}

function toggleSnapMlFor(key) {
  if (snapMlKeys.has(key)) {
    snapMlKeys.delete(key);
  } else {
    snapMlKeys.add(key);
    const h = selectedSnaps.get(key);
    if (h) fetchSnapSeries(h);
  }
  persistSnapshotOverlayState();
  updateBurndownOverlays();
  refreshBurndownLegend();
}

function syncQuickSelect() {
  document.querySelectorAll('.quick-snap-cb').forEach(cb => {
    cb.checked = selectedSnaps.has(cb.dataset.key);
  });
  const n = selectedSnaps.size;
  const lbl = document.getElementById('quick-select-label');
  if (lbl) lbl.textContent = n === 0 ? 'Select snapshots…' : (n + ' selected');
  const pills = document.getElementById('quick-select-pills');
  if (pills) {
    pills.innerHTML = [...selectedSnaps.values()].map((h, i) => {
      const color = h.color || SNAP_COLORS[i % SNAP_COLORS.length];
      const text = truncLabel(snapDisplayName(h), 18);
      return `<span class="badge d-inline-flex align-items-center" style="background:${color};font-size:.66rem;font-weight:600;gap:.25rem;">${text}`
        + `<span style="cursor:pointer;" title="Remove" onclick="removeQuickSnap('${h.snapshot_ts || (h.snapshot_date + '|' + (h.label||''))}')">&times;</span></span>`;
    }).join('');
  }
}

function removeQuickSnap(key) {
  const row = findRowByKey(key);
  if (row && selectedSnaps.has(key)) {
    toggleRow(row);
  } else if (selectedSnaps.has(key)) {
    selectedSnaps.delete(key);
    renderComparePanel();
  }
}

function viewSnapForecast(tr, e) {
  e.stopPropagation();
  try {
    const snap = JSON.parse(tr.dataset.snap || '{}');
    const p = new URLSearchParams(window.location.search);
    if (snap.contract_start_date) p.set('contract_start_date', String(snap.contract_start_date).slice(0, 10));
    if (snap.contract_end_date)   p.set('contract_end_date',   String(snap.contract_end_date).slice(0, 10));
    if (snap.purchased_credits)   p.set('purchased_credits',   snap.purchased_credits);
    window.location.href = D.urls.forecastPage + '?' + p.toString();
  } catch (_) {}
}

function deleteSnapshot(btn, e) {
  e.stopPropagation();
  if (!confirm('Delete this snapshot?')) return;
  fetch(D.urls.delete, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ snapshot_ts: btn.dataset.ts, snapshot_date: btn.dataset.date, label: btn.dataset.label }),
  }).then(r => {
    if (r.status === 204) {
      const row = btn.closest('tr');
      const key = btn.dataset.ts || (btn.dataset.date + '|' + btn.dataset.label);
      selectedSnaps.delete(key);
      seriesCache.delete(btn.dataset.ts);
      row.remove();
      renderComparePanel();
    } else { r.text().then(msg => alert('Delete failed: ' + (msg || r.status))); }
  }).catch(() => alert('Delete request failed.'));
}

// Capture snapshot selections active before a forecast page reload. The actual
// restore waits until the burndown chart has rebuilt its daily/weekly labels.
let pendingSnapRestoreKeys = [];
(function () {
  // Snapshot mode: the page IS a snapshot, so its own frozen forecast is the
  // one shown — auto-select it (its key is its timestamp) and turn on its
  // Base/MC/ML, ignoring any other cached selection.
  if (D.snapshotTs) {
    pendingSnapRestoreKeys = [D.snapshotTs];
    snapMcKeys.add(D.snapshotTs);
    snapMlKeys.add(D.snapshotTs);
    return;
  }
  const saved = sessionStorage.getItem('forecast-snap-keys');
  if (saved) {
    sessionStorage.removeItem('forecast-snap-keys');
    try {
      const keys = JSON.parse(saved);
      pendingSnapRestoreKeys = Array.isArray(keys) ? keys.filter(Boolean) : [];
    } catch (_) {}
    return;
  }
  // Plain page load (not a granularity-switch reload): restore whichever
  // snapshots were left enabled last time, cached per browser.
  try {
    const cached = JSON.parse(localStorage.getItem(SNAP_SELECTION_KEY) || '[]');
    pendingSnapRestoreKeys = Array.isArray(cached) ? cached.filter(Boolean) : [];
  } catch (_) {}
})();

function restorePendingSnapshotSelections() {
  const keys = pendingSnapRestoreKeys;
  pendingSnapRestoreKeys = [];
  if (!keys.length) {
    syncQuickSelect();
    return;
  }

  keys.forEach(key => {
    const row = findRowByKey(key);
    let snap = SNAP_DATA_MAP[key] || null;
    if (row) {
      try { snap = JSON.parse(row.dataset.snap || '{}'); } catch (_) {}
    }
    if (!snap) return;

    if (!selectedSnaps.has(key)) {
      selectedSnaps.set(key, snap);
    }
    if (row) {
      const idx = Math.max(0, [...selectedSnaps.keys()].indexOf(key));
      row.style.background = SNAP_COLORS[idx % SNAP_COLORS.length] + '22';
      const cb = row.querySelector('.history-cb');
      if (cb) cb.checked = true;
    }
    fetchSnapSeries(snap);
  });
  renderComparePanel();
}

/* ===================================================================== *
 * Chart color helpers
 * ===================================================================== */
const CHART_COLOR_DEFAULTS = { actual: '#0d6efd', proj: '#dc3545', weekly: '#0d6efd' };
// Nuke every saved chart preference — colors, toggles, axis windows, line
// cutoffs, granularity, model overlays — and reload the default view. All
// forecast-chart preferences live under the 'fc-' / 'forecast-' key prefixes.
window.resetAllChartSettings = function () {
  const doomed = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && (k.indexOf('fc-') === 0 || k.indexOf('forecast-') === 0)) doomed.push(k);
  }
  doomed.forEach(k => localStorage.removeItem(k));
  window.location.href = D.urls.forecastPage;
};

function getChartColor(key) {
  return localStorage.getItem('fc-color-' + key) || CHART_COLOR_DEFAULTS[key];
}
function setChartColor(key, val) { localStorage.setItem('fc-color-' + key, val); }
function resetChartColors() {
  Object.keys(CHART_COLOR_DEFAULTS).forEach(k => localStorage.removeItem('fc-color-' + k));
  Object.keys(CHART_COLOR_DEFAULTS).forEach(k => {
    const el = document.getElementById('color-' + k);
    if (el) el.value = CHART_COLOR_DEFAULTS[k];
  });
  applyAllChartColors();
}
function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${alpha})`;
}
function applyAllChartColors() {
  const bc = window.burndownChart;
  if (bc) {
    bc.data.datasets.forEach(ds => {
      if (!ds._mcOverlay && !ds._snapOverlay) {
        if (ds.label === 'Actual remaining') {
          ds.borderColor = getChartColor('actual');
          ds.backgroundColor = hexToRgba(getChartColor('actual'), 0.07);
        } else if (ds.label === 'Projected remaining') {
          ds.borderColor = getChartColor('proj');
        }
      }
    });
    bc.update('none');
  }
  const wc = window.weeklyChart;
  if (wc) {
    const wCol = getChartColor('weekly');
    wc.data.datasets[0].backgroundColor = wc.data.datasets[0].backgroundColor.map((c, i) =>
      window._weeklyInContract && window._weeklyInContract[i] ? hexToRgba(wCol, 0.72) : c
    );
    wc.data.datasets[0].borderColor = wc.data.datasets[0].borderColor.map((c, i) =>
      window._weeklyInContract && window._weeklyInContract[i] ? wCol : c
    );
    wc.update('none');
  }
}
window.setBurndownColor = function(key, val) {
  setChartColor(key, val);
  applyAllChartColors();
};
(function() {
  ['actual','proj','weekly'].forEach(k => {
    const el = document.getElementById('color-' + k);
    if (el) el.value = getChartColor(k);
  });
})();

/* ===================================================================== *
 * Burndown chart
 * ===================================================================== */

// Vertical markers where credit-ledger entries land (purchases / gifted
// credits), so a mid-contract grant is visible as the reason the remaining
// line steps up. Set chart.$creditEvents = [{effective_date, label}].
if (typeof Chart !== 'undefined') {
  Chart.register({
    id: 'bnl-credit-events',
    afterDraw(chart) {
      const events = chart.$creditEvents;
      if (!events || !events.length) return;
      const labels = chart.data.labels || [];
      const { ctx, chartArea: { top, bottom, right } } = chart;
      events.forEach((ev, i) => {
        let idx = labels.indexOf(ev.effective_date);
        if (idx < 0) idx = labels.findIndex(l => String(l) >= ev.effective_date);
        if (idx < 0) return;
        const x = chart.scales.x.getPixelForValue(idx);
        if (!Number.isFinite(x)) return;
        // Green for credit additions (default); a per-event color lets
        // expiration markers render red so "credits expired here" reads
        // distinctly from "credits added here".
        const color = ev.color || '#198754';
        const stroke = ev.strokeStyle || 'rgba(25,135,84,.85)';
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, bottom);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = stroke;
        ctx.setLineDash([2, 3]);
        ctx.stroke();
        ctx.setLineDash([]);
        // Shared pill label from charts.js — flips left of the line near the
        // chart's right edge; stacked one row per event.
        bnlDrawMarkerLabel(ctx, ev.label || 'credits added', x, top + 4 + i * 15, right, color);
        ctx.restore();
      });
    },
  });
}

(function () {
  if (!document.getElementById('burndownChart')) return;
  const rawData    = D.weeklyChartData || [];
  const dailyActualRaw = D.dailyActualData || [];
  const purchased  = D.purchased;
  const remaining  = D.remaining;
  const weeklyBurn = D.weeklyBurn;
  const weeksLeft  = D.weeksLeft;
  const latestDate = D.latestDate;
  const contractStartDate = D.contractStartDate;

  // Contract transitions chained beyond the active contract — empty when
  // there's nothing configured to chain into, in which case the chart
  // behaves exactly as it always has (single-contract "Projected remaining").
  // Each entry: {date, end, label, delta, carry}. The line is rebuilt
  // client-side (see buildProjPts) as one fresh weekly/daily-grid segment per
  // contract, anchored at its own start, so each segment forecasts exactly
  // like a normal single-contract projection instead of inheriting an
  // off-grid date from the previous contract's cadence.
  const contractBoundaries = (D.contractBoundaries || [])
    .filter(b => b && b.date)
    .slice()
    .sort((a, b) => (a.date < b.date ? -1 : 1));

  // Chart mode: 'current' (this contract only, same forecast as before
  // chaining existed — the default), 'chained' (steps into the next
  // configured contract), 'overall' (chained + every PAST contract's own
  // actual history stitched in too), or a specific contract id ("view
  // other contracts"). Persisted per browser like the other chart view prefs.
  let currentChartMode = localStorage.getItem('fc-chart-mode') || 'current';
  if (!isMainChartMode(currentChartMode) && !(D.allContracts || []).some(c => c.id === currentChartMode)) {
    currentChartMode = 'current'; // stale/deleted contract id -> fall back
  }
  // Exposed for the separate Monte Carlo/ML overlay IIFE below (its own
  // closure, no access to this one's locals) — those models are computed
  // server-side for the ACTIVE contract only and must not be offered while
  // viewing an arbitrary other contract (see refreshBurndownLegend/
  // toggleForecastModel's guards in that section).
  window._fcChartMode = currentChartMode;

  // Credits available as of a date = sum of ledger entries effective by then,
  // so a mid-contract grant steps the line up on its date instead of
  // inflating the whole history back to contract start.
  const creditEvents = (D.creditEvents || []).filter(e => e && e.effective_date && e.credits > 0);
  const addedBy = dateStr => creditEvents.length
    ? creditEvents.reduce((s, e) => s + (e.effective_date <= dateStr ? e.credits : 0), 0)
    : purchased;

  // Actual data can only exist THROUGH today. The server's latestDate
  // (=latest_usage_date) is an exclusive weekly-axis boundary set to
  // last_data_day + 1 (see service.py) — which in daily terms is TOMORROW,
  // a day that hasn't happened. Using it as the actual line's endpoint drew
  // an "actual remaining" point for a future date. actualBoundary caps the
  // actual line at today; anything past it belongs to the forecast line.
  // Now that usage syncs live from the API, a PAST day with no data genuinely
  // means zero usage that day (not "not uploaded yet"), so the actual line
  // flat-carries its last value up to today instead of stopping short — see
  // the known-facts bridge below.
  const todayStr = D.today || '';
  const actualBoundary = (latestDate && todayStr && latestDate > todayStr) ? todayStr : latestDate;

  const inContractRaw = rawData.filter(w => w.in_contract).sort((a,b) => a.week_start < b.week_start ? -1 : 1);
  let cumUsed = 0;
  const actualRawPts = inContractRaw.map(w => {
    cumUsed += w.total_credits_used;
    const d = new Date((w.week_end || w.week_start) + 'T12:00:00');
    d.setDate(d.getDate() + 1);
    let pointDate = d.toISOString().slice(0, 10);
    pointDate = pointDate > actualBoundary ? actualBoundary : pointDate;
    return [pointDate, Math.max(addedBy(pointDate) - cumUsed, 0)];
  });
  const actualPts = [];
  if (contractStartDate && (!actualBoundary || contractStartDate <= actualBoundary)) {
    actualPts.push([contractStartDate, addedBy(contractStartDate)]);
  }
  actualRawPts.forEach(pt => {
    if (actualPts.length && actualPts[actualPts.length - 1][0] === pt[0]) {
      actualPts[actualPts.length - 1] = pt;
    } else {
      actualPts.push(pt);
    }
  });
  if (actualBoundary && (!actualPts.length || actualPts[actualPts.length - 1][0] !== actualBoundary)) {
    // End the weekly actual line at today (actualBoundary), never at the
    // future latest_usage_date. Ledger-aware: only entries effective by
    // this day count; grants after it step up inside the bridge below.
    actualPts.push([actualBoundary, Math.max(addedBy(actualBoundary) - cumUsed, 0)]);
  }
  const dailyActualPts = dailyActualRaw
    .filter(d => d && d.date)
    .map(d => [d.date, Number(d.remaining)])
    .filter(p => Number.isFinite(p[1]));
  // The day-level reconstruction (D.dailyActualData) ends at the last day
  // with real data. Extension to today is handled by the known-facts bridge
  // below (flat-carry + grant steps), capped at today — so no future
  // ("hasn't happened yet") point is ever plotted as actual.

  // Known-facts bridge: extend each series past its LAST REAL data point up
  // to today. Now that usage syncs live from the API, a past day with no
  // data means zero usage that day (not "not uploaded yet, usage still
  // happening" as with the old lagging manual uploads), so remaining holds
  // CONSTANT across those days — a flat carry — stepping up only where a
  // credit grant lands. Capped at today, so a not-yet-happened day is never
  // drawn as actual; the forecast line owns everything past today.
  // (todayStr and actualBoundary are defined above.)
  const addDaysStr = (dstr, n) => {
    const d = new Date(dstr + 'T12:00:00');
    d.setDate(d.getDate() + n);
    return d.toISOString().slice(0, 10);
  };
  function buildBridgePts(lastPt, weekly) {
    if (!lastPt || !todayStr || todayStr <= lastPt[0]) return [];
    const gapEvents = creditEvents
      .filter(e => e.effective_date > lastPt[0] && e.effective_date <= todayStr)
      .sort((a, b) => (a.effective_date < b.effective_date ? -1 : 1));
    const levelBy = dstr =>
      lastPt[1] + gapEvents.reduce((s, e) => s + (e.effective_date <= dstr ? e.credits : 0), 0);
    if (weekly) {
      // Weekly view augments the completed-week rollup with the single
      // latest REAL per-day actual already uploaded past the last complete
      // week (the current, still-accumulating week) — a real fact from the
      // upload, not a flat guess. Only that one point is added (not every
      // day in between) so the weekly chart keeps its weekly hover cadence
      // instead of exposing single-day categories. A credit grant landing
      // after that point still steps the line from there.
      const dailyTail = dailyActualPts.filter(p => p[0] > lastPt[0]);
      const latestReal = dailyTail.length ? [dailyTail[dailyTail.length - 1]] : [];
      const baseline = latestReal.length ? latestReal[0] : lastPt;
      const extraGrants = gapEvents
        .filter(e => e.effective_date > baseline[0])
        .reduce((acc, e) => {
          const prevVal = acc.length ? acc[acc.length - 1][1] : baseline[1];
          acc.push([e.effective_date, prevVal + e.credits]);
          return acc;
        }, []);
      return latestReal.concat(extraGrants);
    }
    // Daily view: step up at each credit grant in the gap, then flat-carry
    // the last remaining value to today. With live sync, a past day missing
    // data genuinely had zero usage, so holding the line constant there is
    // the truth — and it self-corrects the instant a later sync fills that
    // day in (the page now auto-reloads on new data). today never runs past
    // the real current date, so a not-yet-happened day is left to the
    // forecast line, never drawn as actual.
    const pts = [];
    let prev = lastPt[0];
    gapEvents.forEach(e => {
      const dayBefore = addDaysStr(e.effective_date, -1);
      if (dayBefore > prev) pts.push([dayBefore, levelBy(dayBefore)]);
      pts.push([e.effective_date, levelBy(e.effective_date)]);
      prev = e.effective_date;
    });
    // Flat-carry to today: remaining = last real value + any grants since.
    if (todayStr > prev) pts.push([todayStr, levelBy(todayStr)]);
    return pts;
  }
  function extendWithBridge(pts, weekly) {
    if (!pts.length) return null;
    const lastReal = pts[pts.length - 1];
    buildBridgePts(lastReal, weekly).forEach(pt => {
      if (pt[0] > pts[pts.length - 1][0]) pts.push(pt);
    });
    return lastReal;
  }
  extendWithBridge(actualPts, true);
  extendWithBridge(dailyActualPts, false);
  const useDaily = (D.granularity || 'weekly') === 'daily' && dailyActualPts.length > 0;
  const activeSeries = useDaily ? dailyActualPts : actualPts;
  const activeLast = activeSeries.length ? activeSeries[activeSeries.length - 1] : [actualBoundary || latestDate, remaining];
  const projAnchorDate = activeLast[0];
  const projAnchorRemaining = activeLast[1];
  // Exposed so the MC/ML overlay IIFE (separate scope, loads its bands
  // later/async) can snap its series onto the exact point where the actual
  // line stops and the projection takes over — same anchor the red
  // "Projected remaining" line uses — instead of the server's raw "today"
  // anchor, which can sit many days past the last known-real data point and
  // otherwise leaves the band floating with a visible gap.
  window.burndownProjAnchor = { date: projAnchorDate, remaining: projAnchorRemaining };

  // Builds one contract's worth of projection: a plain weekly/daily grid
  // anchored at (anchorDate, anchorRemaining), declining at weeklyBurn for
  // `weeksHorizon` weeks, with the zero-crossing floored exactly like the
  // stated exhaustion date. `endSpliceStr`, when given, is inserted as an
  // exact off-grid label for DAILY granularity only (weekly stays on its own
  // Monday-from-anchor cadence — splicing a non-Monday date would put one
  // off-grid weekday column on an otherwise all-Monday axis).
  function buildSegmentPts(anchorDate, anchorRemaining, weeksHorizon, granularity, endSpliceStr) {
    const pts = [[anchorDate, anchorRemaining]];
    const base = new Date(anchorDate);
    const dateAfterDays = days => {
      const d = new Date(base);
      d.setDate(d.getDate() + days);
      return d.toISOString().slice(0, 10);
    };
    const dailyBurn = weeklyBurn / 7;
    const crossDateStr = (dailyBurn > 0 && anchorRemaining > 0)
      ? dateAfterDays(Math.floor(anchorRemaining / dailyBurn))
      : null;
    let crossInserted = false;
    const stepDays = granularity === 'daily' ? 1 : 7;
    const steps = granularity === 'daily'
      ? Math.min(Math.ceil(weeksHorizon * 7) + 1, 420)
      : Math.min(Math.ceil(weeksHorizon) + 1, 60);
    for (let i = 1; i <= steps; i++) {
      const days = i * stepDays;
      const dstr = dateAfterDays(days);
      if (crossDateStr && !crossInserted && dstr >= crossDateStr) {
        if (dstr > crossDateStr) pts.push([crossDateStr, 0]);
        crossInserted = true;
      }
      const rem = (crossDateStr && dstr >= crossDateStr)
        ? 0
        : Math.max(anchorRemaining - dailyBurn * days, 0);
      pts.push([dstr, rem]);
    }
    // Splice the EXACT value at endSpliceStr (contract end) into the grid
    // regardless of granularity. Weekly steps land on whatever weekday the
    // anchor falls on, so without this the nearest weekly grid point to
    // contract end can be several days off — its value then reads as a
    // different number than the precise date-based "End balance" KPI (and
    // than daily mode, which always had this splice) even though both are
    // meant to describe the same contract-end balance.
    if (endSpliceStr) {
      for (let j = 1; j < pts.length; j++) {
        if (pts[j - 1][0] < endSpliceStr && endSpliceStr < pts[j][0]) {
          const t = (new Date(endSpliceStr) - new Date(pts[j - 1][0]))
                  / (new Date(pts[j][0]) - new Date(pts[j - 1][0]));
          pts.splice(j, 0, [endSpliceStr, pts[j - 1][1] + t * (pts[j][1] - pts[j - 1][1])]);
          break;
        }
      }
    }
    return pts;
  }

  // Interpolate an exact point at `exactDateStr` into a segment's grid (the
  // same technique buildSegmentPts already uses for a daily-mode contract-
  // end splice).
  function spliceExactDate(segPts, exactDateStr) {
    if (!exactDateStr || segPts.some(p => p[0] === exactDateStr)) return segPts;
    for (let j = 1; j < segPts.length; j++) {
      if (segPts[j - 1][0] < exactDateStr && exactDateStr < segPts[j][0]) {
        const t = (new Date(exactDateStr) - new Date(segPts[j - 1][0]))
                / (new Date(segPts[j][0]) - new Date(segPts[j - 1][0]));
        const val = segPts[j - 1][1] + t * (segPts[j][1] - segPts[j - 1][1]);
        const out = segPts.slice();
        out.splice(j, 0, [exactDateStr, val]);
        return out;
      }
    }
    return segPts; // exactDateStr outside this segment's plotted range
  }

  // A plain declining segment that RIDES INTO `targetDate` — its last point
  // lands exactly there (spliced in, interpolated) instead of stopping at
  // its own natural horizon or an approximate grid point. Used so a
  // contract's line visibly reaches the boundary marker for whatever comes
  // next before it ends, rather than trailing off early.
  function buildSegmentRidingInto(anchorDate, anchorRemaining, targetDate, granularity) {
    if (!targetDate || targetDate <= anchorDate) return [[anchorDate, anchorRemaining]];
    const weeksHorizon = Math.max((new Date(targetDate) - new Date(anchorDate)) / (7 * 86400000), 0);
    const full = buildSegmentPts(anchorDate, anchorRemaining, weeksHorizon, granularity, targetDate);
    return spliceExactDate(full, targetDate).filter(p => p[0] <= targetDate);
  }

  // Advanced settings' X-axis date pickers can be set to any future date,
  // but a category-axis chart can only ever show labels that actually
  // exist as data points — so when the (Basic model's own) exhaustion week
  // falls AFTER contract end, extend the projection horizon out to cover
  // it, so there's real data for the picker to reach/snap to. When
  // exhaustion falls BEFORE contract end instead (credits run out early),
  // it's already within the normal contract-end-bounded horizon, so this
  // must NOT shrink anything — Math.max below handles that automatically.
  function extendedWeeksLeft() {
    if (!D.exhaustionWeek) return weeksLeft;
    const exhDate = new Date(D.exhaustionWeek + 'T12:00:00');
    if (Number.isNaN(exhDate.getTime())) return weeksLeft;
    const exhWeeks = (exhDate - new Date(projAnchorDate + 'T12:00:00')) / (7 * 86400000);
    return Math.max(weeksLeft, exhWeeks);
  }

  function buildProjPts(granularity) {
    // Segment 0: the active contract, anchored at the end of the known-facts
    // bridge (today, when data lags) so the grant steps stay on the actual
    // line and the decline starts from what is actually known now. Exactly
    // the same forecast as before chaining existed — including its normal
    // ~1-week overshoot past contract end (steps = ceil(weeksHorizon)+1) —
    // extended further when the exhaustion week runs past contract end (see
    // extendedWeeksLeft).
    const seg0Full = buildSegmentPts(projAnchorDate, projAnchorRemaining, extendedWeeksLeft(), granularity, D.contractEndDate);
    const chaining = currentChartMode === 'chained' || currentChartMode === 'overall';
    if (!chaining || !contractBoundaries.length) return seg0Full;
    // Chained (or overall, which chains too — it just ALSO shows past
    // history, see buildPastContractSegments): this dataset covers ONLY the
    // active contract, ridden into ITS OWN contract end date and ending
    // exactly there — NOT the next contract's start date
    // (contractBoundaries[0].date), which is usually a day or more later.
    // Riding all the way to the next contract's start used to draw the old
    // contract's decline through days that are outside its own window (only
    // ever a 1-day gap in Weekly, where it's invisible between 7-day-apart
    // labels — but a fully visible extra day in Daily). See
    // buildFutureContractSegments for what draws beyond contract end, as its
    // OWN separate line(s) starting fresh at the next contract's own date.
    return buildSegmentRidingInto(projAnchorDate, projAnchorRemaining, D.contractEndDate, granularity);
  }

  // One segment per configured future contract, each its own separate line:
  // starts at that contract's own start date/credits (added onto whatever
  // carried over, if its predecessor rolled over) and rides into the NEXT
  // boundary the same way the active contract rides into the first one —
  // ending exactly there so a 3+ contract chain reads as a clean relay, not
  // one line jumping between unrelated values. The last contract in the
  // chain just runs its own natural horizon to its own end. Kept as
  // SEPARATE Chart.js datasets (not merged into one array) specifically so
  // each can end AT the same label its successor starts from, at a
  // different height — a single dataset can only hold one value per label.
  function buildFutureContractSegments(granularity) {
    const chaining = currentChartMode === 'chained' || currentChartMode === 'overall';
    if (!chaining || !contractBoundaries.length) return [];
    const dailyBurn = weeklyBurn / 7;
    const segments = [];
    let curAnchorDate = projAnchorDate;
    let curAnchorRemaining = projAnchorRemaining;
    contractBoundaries.forEach((b, i) => {
      const days = Math.round((new Date(b.date) - new Date(curAnchorDate)) / 86400000);
      const declined = Math.max(curAnchorRemaining - dailyBurn * days, 0);
      const startValue = Math.max((Number(b.delta) || 0) + (b.carry ? declined : 0), 0);
      const nextBoundary = contractBoundaries[i + 1];
      let data;
      if (nextBoundary) {
        data = buildSegmentRidingInto(b.date, startValue, nextBoundary.date, granularity);
      } else {
        const weeksHorizon = b.end
          ? Math.max((new Date(b.end) - new Date(b.date)) / (7 * 86400000), 0)
          : 12;
        data = buildSegmentPts(b.date, startValue, weeksHorizon, granularity, b.end);
      }
      segments.push({ label: b.label || 'Next contract', data });
      curAnchorDate = b.date;
      curAnchorRemaining = startValue;
    });
    return segments;
  }

  // "Overall" mode only: every OTHER contract that's already started (not
  // the active one, which uses the real known-facts-bridge reconstruction
  // below instead) gets its own actual-history segment, in chronological
  // order — so the actual line reads continuously from the very first
  // configured contract through to where the active contract's own actual
  // data picks up, instead of starting cold at the active contract's start.
  function buildPastContractSegments(granularity) {
    if (currentChartMode !== 'overall') return [];
    const todayStr = D.today || '';
    return (D.allContracts || [])
      .filter(c => c.id !== D.activeContractId && c.start && (!todayStr || c.start <= todayStr))
      .sort((a, b) => (a.start < b.start ? -1 : 1))
      .map(c => ({ label: c.label || 'Contract', purchased: c.purchased || 0, contract: c,
                   data: buildOtherContractSeries(c, granularity).actual }))
      .filter(seg => seg.data.length);
  }

  let currentGranularity = D.granularity || 'weekly';
  let projPts = buildProjPts(currentGranularity);
  let futureSegments = buildFutureContractSegments(currentGranularity);
  let pastSegments = buildPastContractSegments(currentGranularity);
  // Exposed for the separate Monte Carlo/ML overlay IIFE below (its own
  // closure, no access to this one's locals) — see chainedFetchTargets there.
  window._fcContractBoundaries = contractBoundaries;
  window._fcFutureSegments = futureSegments;

  const lookup = (pts, lbl) => { const p = pts.find(x => x[0] === lbl); return p != null ? p[1] : null; };
  const visiblePointRadius = () => 0;
  const hoverPointRadius = () => currentGranularity === 'daily' ? 5 : 6;
  const pointHitRadius = () => currentGranularity === 'daily' ? 8 : 4;
  const activeActualPts = () => currentGranularity === 'daily' && dailyActualPts.length ? dailyActualPts : actualPts;
  const isProjectionDataset = ds => ds && (
    ds.label === 'Projected remaining' || ds._futureSegment || ds._mcOverlay || ds._lrOverlay
  );
  const isLatestActualProjectionHover = item =>
    projAnchorDate && item.label === projAnchorDate && isProjectionDataset(item.dataset);

  function buildAllLabels(ppts) {
    const labels = new Set([...activeActualPts(), ...ppts].map(p => p[0]));
    futureSegments.forEach(s => s.data.forEach(p => labels.add(p[0])));
    pastSegments.forEach(s => s.data.forEach(p => labels.add(p[0])));
    // Contract end is added as its own category only for daily granularity —
    // for weekly, every label here is already Monday-anchored (actual points
    // land on week_end+1 = Monday; projected points step +7 days from there),
    // and injecting the raw contract_end_date would put one off-grid weekday
    // column on the axis whenever the contract doesn't end on a Monday.
    if (D.contractEndDate && currentGranularity === 'daily') labels.add(D.contractEndDate);
    return [...labels].sort();
  }

  function formatBurndownTickLabel(value, index, ticks) {
    const label = this.getLabelForValue(value);
    if (!label) return '';
    // Every weekly label is already Monday-anchored at the data layer (see
    // buildAllLabels/buildProjPts), so this just formats whichever date it
    // actually is — no forced re-labeling, which would mask a real
    // inconsistency instead of surfacing it.
    const dt = new Date(label + 'T12:00:00');
    if (Number.isNaN(dt.getTime())) return label;
    return `${dt.getMonth() + 1}/${dt.getDate()}`;
  }

  function applyBurndownXAxisStyle(chart, granularity) {
    if (!chart || !chart.options || !chart.options.scales || !chart.options.scales.x) return;
    chart.options.scales.x.ticks.maxRotation = 45;
    chart.options.scales.x.ticks.minRotation = 0;
    chart.options.scales.x.ticks.maxTicksLimit = granularity === 'daily' ? 16 : 14;
    chart.options.scales.x.ticks.autoSkip = true;
  }

  function getNearestLabel(value, direction) {
    if (!value || !allLabels.length) return null;
    if (direction === 'start') return allLabels.find(l => l >= value) || allLabels[allLabels.length - 1];
    return [...allLabels].reverse().find(l => l <= value) || allLabels[0];
  }

  function labelIndex(label) {
    const idx = allLabels.indexOf(label);
    return idx >= 0 ? idx : null;
  }

  function applyAxisWindow(min, max) {
    const bc = window.burndownChart;
    if (!bc) return;
    const scale = bc.chart.options.scales.x;
    scale.min = min;
    scale.max = max;
    bc.chart.update('none');
  }

  function defaultViewRange() {
    // Weekly: a date-range view (viewFrom/viewTo) starts the x-axis at viewFrom
    // so the window's actual data leads the chart. Daily keeps its original
    // full-contract window (contract start -> end) regardless of any view.
    const startAnchor = (currentGranularity !== 'daily' && D.viewFrom)
      || D.contractStartDate || allLabels[0];
    // Overall mode: pull the window's left edge back to the earliest past
    // contract's own start (pastSegments is sorted chronologically) so all
    // of history is visible by default instead of starting at the active
    // contract like every other mode does.
    const overallStart = currentChartMode === 'overall' && pastSegments.length
      ? pastSegments[0].data[0][0]
      : null;
    const min = getNearestLabel(overallStart || startAnchor, 'start') || allLabels[0];
    const contractEnd = getNearestLabel(D.contractEndDate, 'end');

    // "Current contract" mode is a clean, bounded view of just this contract
    // — the x-axis stops exactly at its own end date regardless of whether
    // the projection crosses zero before or after it. (The projection data
    // itself may still carry a point or two past contract end — see
    // buildSegmentPts — this only clips the default VIEW; panning/zooming
    // out still reaches them.)
    if (currentChartMode === 'current') {
      return { min, max: contractEnd || allLabels[allLabels.length - 1] };
    }

    // Chained mode: the window's right edge used to stop dead at contract
    // end, cutting off the projection's own tail whenever it runs out AFTER
    // the contract does (a high-burn or underfunded contract). Extend to
    // whichever is later: contract end, or a few weeks past where the line
    // first reaches (or would go below) zero — MC/ML tails run further still
    // and stay reachable by panning, but the common "did we run out, and
    // when" question shouldn't require manual zooming to answer. The zero
    // crossing can now land in ANY segment of the chain (buildProjPts only
    // covers the active contract; futureSegments covers the rest).
    const allChainedPts = projPts.concat(futureSegments.flatMap(s => s.data));
    const zeroPt = allChainedPts.find(p => p[1] != null && p[1] <= 0);
    // 'start' direction rounds UP to the next available label — needed here
    // so the window actually reaches past the target date instead of
    // rounding back down to whatever label sits just before it.
    const pastZero = zeroPt ? getNearestLabel(shiftDayStr(zeroPt[0], 21), 'start') : null;
    // When it never crosses zero, fall back to the chain's own last point
    // (the last future segment's, if any) so the last contract is still
    // visible by default without manual panning.
    const lastSeg = futureSegments.length ? futureSegments[futureSegments.length - 1] : null;
    const chainedEnd = lastSeg && lastSeg.data.length
      ? getNearestLabel(lastSeg.data[lastSeg.data.length - 1][0], 'end')
      : null;
    const candidates = [contractEnd, pastZero, chainedEnd].filter(Boolean);
    const max = (candidates.length ? candidates.sort().pop() : null) || allLabels[allLabels.length - 1];
    return { min, max };
  }

  function setViewInputBounds() {
    const fromEl = document.getElementById('chart-view-from');
    const toEl = document.getElementById('chart-view-to');
    if (!fromEl || !toEl || !allLabels.length) return;
    [fromEl, toEl].forEach(el => {
      el.min = allLabels[0];
      el.max = allLabels[allLabels.length - 1];
    });
  }

  window.applyBurndownViewRange = function(persist = true) {
    const bc = window.burndownChart;
    const fromEl = document.getElementById('chart-view-from');
    const toEl = document.getElementById('chart-view-to');
    if (!bc || !fromEl || !toEl || !allLabels.length) return;
    const from = fromEl.value || '';
    const to = toEl.value || '';
    if (!from && !to) {
      const def = defaultViewRange();
      fromEl.value = def.min;
      toEl.value = def.max;
      applyAxisWindow(def.min, def.max);
      return;
    }
    const min = getNearestLabel(from, 'start') || allLabels[0];
    const max = getNearestLabel(to, 'end') || allLabels[allLabels.length - 1];
    if (min > max) {
      alert('Choose a view start date before the end date.');
      return;
    }
    applyAxisWindow(min, max);
  };

  window.shiftBurndownViewWindow = function(direction) {
    const bc = window.burndownChart;
    const fromEl = document.getElementById('chart-view-from');
    const toEl = document.getElementById('chart-view-to');
    if (!bc || !fromEl || !toEl || !allLabels.length) return;

    const scale = bc.chart.options.scales.x;
    const currentMin = typeof scale.min === 'string' ? scale.min : allLabels[0];
    const currentMax = typeof scale.max === 'string' ? scale.max : allLabels[allLabels.length - 1];
    const minIdx = labelIndex(getNearestLabel(fromEl.value || currentMin, 'start')) ?? 0;
    const maxIdx = labelIndex(getNearestLabel(toEl.value || currentMax, 'end')) ?? allLabels.length - 1;
    const width = Math.max(1, maxIdx - minIdx);
    const step = Math.max(1, Math.round(width * 0.8)) * (direction < 0 ? -1 : 1);
    let nextMinIdx = Math.max(0, Math.min(allLabels.length - 1 - width, minIdx + step));
    let nextMaxIdx = nextMinIdx + width;
    if (nextMaxIdx >= allLabels.length) {
      nextMaxIdx = allLabels.length - 1;
      nextMinIdx = Math.max(0, nextMaxIdx - width);
    }

    fromEl.value = allLabels[nextMinIdx];
    toEl.value = allLabels[nextMaxIdx];
    window.applyBurndownViewRange();
  };

  window.clearBurndownViewRange = function() {
    const bc = window.burndownChart;
    const fromEl = document.getElementById('chart-view-from');
    const toEl = document.getElementById('chart-view-to');
    if (!bc || !fromEl || !toEl || !allLabels.length) return;
    const def = defaultViewRange();
    fromEl.value = def.min;
    toEl.value = def.max;
    applyAxisWindow(def.min, def.max);
  };

  // Y-axis window ("calculator" style): fixed min/max in credits so exports
  // can be framed exactly; blank = auto. Persisted per browser and cloned
  // into the fullscreen chart (and therefore its PNG export) via options.
  window.applyBurndownYRange = function (persist = true) {
    const bc = window.burndownChart;
    if (!bc) return;
    const minEl = document.getElementById('chart-view-ymin');
    const maxEl = document.getElementById('chart-view-ymax');
    const yMin = minEl && minEl.value !== '' ? Number(minEl.value) : null;
    const yMax = maxEl && maxEl.value !== '' ? Number(maxEl.value) : null;
    if (yMin != null && yMax != null && yMin >= yMax) {
      alert('Y-axis min must be below max.');
      return;
    }
    const scale = bc.chart.options.scales.y;
    if (yMin != null) scale.min = yMin; else delete scale.min;
    if (yMax != null) { scale.max = yMax; delete scale.suggestedMax; }
    else { delete scale.max; scale.suggestedMax = window.burndownMaxY || purchased; }
    if (persist) {
      if (yMin != null) localStorage.setItem('fc-view-ymin', String(yMin));
      else localStorage.removeItem('fc-view-ymin');
      if (yMax != null) localStorage.setItem('fc-view-ymax', String(yMax));
      else localStorage.removeItem('fc-view-ymax');
    }
    bc.chart.update('none');
  };
  window.clearBurndownYRange = function () {
    ['chart-view-ymin', 'chart-view-ymax'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    window.applyBurndownYRange();
  };
  function initBurndownYRange() {
    const minEl = document.getElementById('chart-view-ymin');
    const maxEl = document.getElementById('chart-view-ymax');
    if (!minEl || !maxEl) return;
    minEl.value = localStorage.getItem('fc-view-ymin') || '';
    maxEl.value = localStorage.getItem('fc-view-ymax') || '';
    if (minEl.value !== '' || maxEl.value !== '') window.applyBurndownYRange(false);
  }

  window.resetBurndownView = function() {
    window.clearBurndownViewRange();
    window.clearBurndownYRange();
  };

  // Always resets to the sensible default view range — on every mode switch
  // and every page load, never restoring a previously remembered manual
  // pan/zoom (deliberate: X-axis view state does not persist across
  // reloads). defaultViewRange() itself still honors a URL-driven
  // view_from/view_to (the Advanced burn window), which is a separate,
  // explicit mechanism, not a remembered pan.
  function initBurndownViewRange() {
    const fromEl = document.getElementById('chart-view-from');
    const toEl = document.getElementById('chart-view-to');
    if (!fromEl || !toEl) return;
    setViewInputBounds();
    const def = defaultViewRange();
    fromEl.value = def.min;
    toEl.value = def.max;
    window.applyBurndownViewRange(false);
  }

  let allLabels = buildAllLabels(projPts);
  window.burndownLabels = allLabels;
  // Overall mode: a past contract can have purchased more credits than the
  // active one, so the default Y suggestion should cover the tallest of
  // them all, not just the active contract's own total.
  window.burndownMaxY = pastSegments.length
    ? Math.max(purchased, ...pastSegments.map(s => s.purchased || 0))
    : purchased;

  // One projection line for the active contract (built by buildProjPts) —
  // in "chained" mode it rides into the first future contract's own marker
  // and ends exactly there. Plus one MORE line per future contract in the
  // chain (futureSegments) — each its own dataset so it can start at that
  // same label, at its own (usually different) value, rather than one line
  // trying to hold two values at once. Colors cycle so a 3+ contract chain
  // stays visually distinguishable; each still uses the "proj" dash style.
  const FUTURE_SEGMENT_COLORS = ['#fd7e14', '#6f42c1', '#20c997', '#d63384'];
  const PAST_SEGMENT_COLORS = ['#6c757d', '#495057', '#adb5bd', '#343a40'];
  const burndownDatasets = [
    // Overall mode only: one solid "actual" line per prior contract, in
    // chronological order, so history reads continuously before the active
    // contract's own "Actual remaining" line picks up below.
    ...pastSegments.map((seg, i) => ({
      label: `Actual — ${seg.label}`,
      data: allLabels.map(l => lookup(seg.data, l)),
      borderColor: PAST_SEGMENT_COLORS[i % PAST_SEGMENT_COLORS.length],
      backgroundColor: 'transparent',
      fill: false, tension: 0.1, pointRadius: visiblePointRadius(), pointHoverRadius: hoverPointRadius(), pointHitRadius: pointHitRadius(), spanGaps: false,
      _pastSegment: true,
    })),
    {
      label: 'Actual remaining',
      data: allLabels.map(l => lookup(activeActualPts(), l)),
      borderColor: getChartColor('actual'), backgroundColor: hexToRgba(getChartColor('actual'), 0.07),
      fill: true, tension: 0.1, pointRadius: visiblePointRadius(), pointHoverRadius: hoverPointRadius(), pointHitRadius: pointHitRadius(), spanGaps: false,
    },
    {
      label: 'Projected remaining',
      data: extendBelowZero(allLabels.map(l => lookup(projPts, l))),
      borderColor: getChartColor('proj'), borderDash: [5, 4], backgroundColor: 'transparent',
      tension: 0.05, pointRadius: visiblePointRadius(), pointHoverRadius: hoverPointRadius(), pointHitRadius: pointHitRadius(), spanGaps: false,
      _baseOverlay: true,
    },
    ...futureSegments.map((seg, i) => ({
      label: `Projected — ${seg.label}`,
      data: extendBelowZero(allLabels.map(l => lookup(seg.data, l))),
      borderColor: FUTURE_SEGMENT_COLORS[i % FUTURE_SEGMENT_COLORS.length],
      borderDash: [5, 4], backgroundColor: 'transparent',
      tension: 0.05, pointRadius: visiblePointRadius(), pointHoverRadius: hoverPointRadius(), pointHitRadius: pointHitRadius(), spanGaps: false,
      _baseOverlay: true, _futureSegment: true,
    })),
  ];

  window.burndownChart = new BNLChart('burndownChart', {
    type: 'line',
    data: {
      labels: allLabels,
      datasets: burndownDatasets,
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        // Series toggling lives in the Show/hide dropdown. The built-in legend
        // is opt-in (the "Legend on chart" switch) so it can be baked into the
        // exported PNG. When shown it is display-only — clicking the dropdown,
        // not the legend, toggles series — and it hides the faint bands + any
        // currently-hidden series so it reads as "what's on the chart".
        legend: {
          display: localStorage.getItem('fc-legend-on') === '1',
          position: 'top',
          onClick: () => {},
          labels: {
            usePointStyle: true,
            boxWidth: 10,
            font: { size: 10 },
            generateLabels: (ch) => {
              const base = Chart.defaults.plugins.legend.labels.generateLabels(ch);
              return base
                .filter(it => {
                  const ds = ch.data.datasets[it.datasetIndex];
                  const meta = ch.getDatasetMeta(it.datasetIndex);
                  return ds && !ds._noLegend && meta.hidden !== true;
                })
                .map(it => { it.text = cleanLegendLabel(it.text); return it; });
            },
          },
        },
        tooltip: {
          // Keep the hover panel compact so it doesn't overflow / clip when
          // many overlays are active: drop empty points and the faint P10/P90
          // band lines (marked _noTooltip), then cap the visible rows.
          itemSort: (a, b) => (b.raw ?? -Infinity) - (a.raw ?? -Infinity),
          filter: (item) => item.raw != null && !item.dataset._noTooltip
            && !isLatestActualProjectionHover(item)
            // A snapshot overlay's first point sits on the actual line (they
            // share that value) — the actual already reports it, so don't
            // double it up in the hover.
            && item.dataset._snapAnchorLabel !== item.label,
          usePointStyle: true,
          boxWidth: 8,
          boxHeight: 8,
          bodyFont: { size: 11 },
          titleFont: { size: 11 },
          maxWidth: 320,
          callbacks: {
            title: items => {
              if (!items || !items.length) return '';
              const lbl = items[0].label || '';
              // Weekly labels are already Monday-anchored at the data layer —
              // show the hovered date as-is, no forced re-labeling.
              const dt = new Date(lbl + 'T12:00:00');
              if (Number.isNaN(dt.getTime())) return lbl;
              return `${dt.getMonth() + 1}/${dt.getDate()}`;
            },
            label: ctx => `  ${cleanLegendLabel(ctx.dataset.label)}: ${Math.round(ctx.raw ?? 0).toLocaleString()} credits`,
          },
        },
        zoom: {
          zoom: { wheel: { enabled: true, modifierKey: 'ctrl' }, pinch: { enabled: true }, mode: 'x' },
          pan:  { enabled: true, mode: 'x' },
          limits: { y: { min: 0 } },
        },
      },
      scales: {
        y: {
          beginAtZero: true, suggestedMax: window.burndownMaxY,
          ticks: { callback: v => v >= 1000000 ? (v/1000000).toFixed(1)+'M' : v >= 1000 ? (v/1000).toFixed(0)+'k' : v, font: { size: 10 } },
          grid: { color: 'rgba(0,0,0,.05)' },
        },
        x: {
          ticks: {
            maxRotation: 45,
            minRotation: 0,
            maxTicksLimit: currentGranularity === 'daily' ? 16 : 14,
            autoSkip: true,
            callback: formatBurndownTickLabel,
            font: { size: 10 },
          },
          grid: { display: false },
        },
      },
    },
  }, {
    exportName: 'Credit Burndown',
    // Bulk export: one clean PNG per model, each with the actual line for
    // context, so a poster can show Basic/MC/ML side by side without the
    // other models' bands cluttering each image.
    exportGroups: [
      { label: 'Basic', match: ds => ds.label === 'Actual remaining' || ds._baseOverlay },
      { label: 'MC', match: ds => ds.label === 'Actual remaining' || ds._mcOverlay },
      { label: 'ML', match: ds => ds.label === 'Actual remaining' || ds._lrOverlay },
    ],
  });

  // Show where each ledger entry (purchase / gifted grace credits) lands.
  // The initial purchase at contract start is implicit, so only mark
  // mid-contract additions. The "Credit markers" toggle below the chart
  // hides them (persisted per browser, default on).
  window._fcCreditEvents = creditEvents.filter(
    e => contractStartDate && e.effective_date > contractStartDate
  );
  // Contract-transition markers — green for the next contract's own credits
  // landing on its start date, red for credits that expired at a
  // non-rolling contract's end. Kept SEPARATE from the plain ledger markers
  // above and only shown in "chain into next contract" mode (see
  // activeMarkerEvents): "current contract" mode is a clean view of just
  // this contract and must not imply a next contract's credits at all.
  window._fcChainMarkers = [];
  (D.contractBoundaries || []).forEach(b => {
    if (b && b.date && b.delta > 0) {
      window._fcChainMarkers.push({
        effective_date: b.date,
        credits: b.delta,
        label: `+${Math.round(b.delta).toLocaleString()} ${b.label || 'next contract'}`,
      });
    }
  });
  (D.contractExpirations || []).forEach(e => {
    if (e && e.date && e.amount > 0) {
      window._fcChainMarkers.push({
        effective_date: e.date,
        credits: -e.amount,
        color: '#dc3545',
        strokeStyle: 'rgba(220,53,69,.85)',
        label: `−${Math.round(e.amount).toLocaleString()} expired (${e.label || 'contract end'})`,
      });
    }
  });
  // Overall mode: each past contract's own mid-contract ledger additions
  // (same "skip the implicit start-of-contract entry" filter as the active
  // contract's window._fcCreditEvents above), so history shows the same
  // marker detail the active contract gets.
  const pastCreditEvents = pastSegments.flatMap(seg =>
    (seg.contract.creditEvents || []).filter(e => e && e.effective_date && e.credits > 0 && e.effective_date > seg.contract.start)
      .map(e => ({ effective_date: e.effective_date, credits: e.credits, label: e.label }))
  );
  function activeMarkerEvents() {
    const base = window._fcCreditEvents || [];
    const chaining = currentChartMode === 'chained' || currentChartMode === 'overall';
    const withChain = chaining ? base.concat(window._fcChainMarkers || []) : base;
    return currentChartMode === 'overall' ? withChain.concat(pastCreditEvents) : withChain;
  }
  const creditMarkersOn = localStorage.getItem('fc-credit-markers') !== '0';
  window.burndownChart.chart.$creditEvents = creditMarkersOn ? activeMarkerEvents() : [];
  window.toggleCreditMarkers = function (on) {
    localStorage.setItem('fc-credit-markers', on ? '1' : '0');
    const bc = window.burndownChart;
    if (!bc) return;
    // "View other contracts" mode has its own markers (see
    // applyOtherContractView) — activeMarkerEvents() is always the ACTIVE
    // contract's own, so using it here regardless of mode showed the wrong
    // contract's markers as soon as the toggle was unchecked then re-checked.
    const markers = isMainChartMode(currentChartMode)
      ? activeMarkerEvents()
      : (window._fcOtherContractMarkers || []);
    bc.chart.$creditEvents = on ? markers : [];
    bc.chart.update('none');
  };
  (function () {
    const cb = document.getElementById('credit-markers-on');
    if (cb) cb.checked = creditMarkersOn;
  })();

  /* ── Chart mode: current contract / chained / view another contract ──
   * The "view other contracts" mode is a fully independent reconstruction
   * (its own actual+projected line, own ledger, own Y-scale/window) — it
   * does NOT touch the active-contract bridge/MC/ML machinery above, which
   * only makes sense for whichever contract is actually live right now. */

  function buildOtherContractSeries(contract, granularity) {
    const events = (contract.creditEvents || []).filter(e => e && e.effective_date && e.credits > 0);
    const addedByOwn = dateStr => events.length
      ? events.reduce((s, e) => s + (e.effective_date <= dateStr ? e.credits : 0), 0)
      : contract.purchased;
    const weeks = rawData
      .filter(w => w.week_start >= contract.start && (!contract.end || w.week_start <= contract.end))
      .sort((a, b) => (a.week_start < b.week_start ? -1 : 1));
    let cum = 0;
    const weeklyActual = [];
    if (contract.start) weeklyActual.push([contract.start, addedByOwn(contract.start)]);
    weeks.forEach(w => {
      cum += w.total_credits_used;
      const d = new Date((w.week_end || w.week_start) + 'T12:00:00');
      d.setDate(d.getDate() + 1);
      const pointDate = d.toISOString().slice(0, 10);
      const pt = [pointDate, Math.max(addedByOwn(pointDate) - cum, 0)];
      if (weeklyActual.length && weeklyActual[weeklyActual.length - 1][0] === pt[0]) weeklyActual[weeklyActual.length - 1] = pt;
      else weeklyActual.push(pt);
    });
    // Daily granularity uses this contract's OWN day-level reconstruction
    // (D.allContracts[i].dailyActual, server-built the exact same way
    // D.dailyActualData is for the active contract — see
    // _daily_actual_for_contract) instead of the weekly rollup above, so
    // "view other contracts" Daily matches "current contract" Daily
    // formatting point-for-point rather than looking like a coarser series.
    const dailyActual = (contract.dailyActual || [])
      .filter(d => d && d.date)
      .map(d => [d.date, Number(d.remaining)])
      .filter(p => Number.isFinite(p[1]));
    const actual = (granularity === 'daily' && dailyActual.length) ? dailyActual : weeklyActual;
    // Anchor the projection at the last real point in THIS contract's own
    // range, or its own start (full credits) when it has none yet (a future
    // contract that hasn't started, or one with no usage recorded).
    const anchor = actual.length ? actual[actual.length - 1] : [contract.start, contract.purchased];
    const weeksHorizon = contract.end
      ? Math.max((new Date(contract.end) - new Date(anchor[0])) / (7 * 86400000), 0)
      : 12;
    const proj = buildSegmentPts(anchor[0], anchor[1], weeksHorizon, granularity, contract.end);
    // Only markers that actually fall within THIS contract's own window —
    // a ledger entry dated after the contract's own end (bad data entry, or
    // a stray future-dated grant) would otherwise still render as a marker
    // even though it's outside the visible/relevant range for this contract.
    const markers = events
      .filter(e => e.effective_date > contract.start && (!contract.end || e.effective_date <= contract.end))
      .map(e => ({ effective_date: e.effective_date, credits: e.credits, label: e.label }));
    return { actual, proj, markers, anchor };
  }

  function setOtherContractNote(text) {
    const note = document.getElementById('fc-other-contract-note');
    if (!note) return;
    note.textContent = text || '';
    note.style.display = text ? '' : 'none';
  }

  // Like getNearestLabel, but against an arbitrary label array instead of
  // the active-contract's allLabels closure — needed here since "view other
  // contracts" mode plots a different contract's own label set.
  function nearestLabelIn(labelsArr, value, direction) {
    if (!value || !labelsArr.length) return null;
    if (direction === 'start') return labelsArr.find(l => l >= value) || labelsArr[labelsArr.length - 1];
    return [...labelsArr].reverse().find(l => l <= value) || labelsArr[0];
  }

  function applyOtherContractView(contractId) {
    const bc = window.burndownChart;
    const contract = (D.allContracts || []).find(c => c.id === contractId);
    if (!bc || !contract) return;
    const { actual, proj, markers, anchor } = buildOtherContractSeries(contract, currentGranularity);
    const labels = [...new Set([...actual, ...proj].map(p => p[0]))].sort();
    bc.chart.data.labels = labels;
    bc.chart.data.datasets[0].data = labels.map(l => lookup(actual, l));
    bc.chart.data.datasets[1].data = extendBelowZero(labels.map(l => lookup(proj, l)));
    bc.chart.$creditEvents = creditMarkersOn ? markers : [];
    bc.chart.options.scales.y.suggestedMax = contract.purchased;
    window.burndownLabels = labels;
    window.burndownMaxY = contract.purchased;
    // Exposed for the MC/ML overlay IIFE (separate closure): the anchor its
    // bands should snap onto, and the contract id + own [start, end] window
    // to fetch and burn-scope server-side stats for THIS contract instead of
    // the active one (see /forecast/model-data's contract_id param).
    window._fcOtherContractAnchor = { date: anchor[0], remaining: anchor[1] };
    window._fcOtherContract = { id: contract.id, start: contract.start, end: contract.end };
    // Exposed so toggleCreditMarkers (unchecking then re-checking "Credit
    // markers") can re-apply THIS contract's own markers instead of falling
    // back to the active contract's (activeMarkerEvents()) — it has no other
    // way to know which contract is currently being viewed.
    window._fcOtherContractMarkers = markers;
    if (labels.length) {
      // Stops exactly at this contract's own end date — same bounded-view
      // principle as "current contract" mode — rather than the tail end of
      // its projection data, which can overshoot by a step (buildSegmentPts).
      bc.chart.options.scales.x.min = labels[0];
      bc.chart.options.scales.x.max = nearestLabelIn(labels, contract.end, 'end') || labels[labels.length - 1];
    }
    setOtherContractNote(`Viewing ${contract.label} on its own — starts ${contract.start || '—'}.`);
    bc.chart.update();
  }

  // Switching mode reloads the page rather than patching the chart in place
  // — same reasoning (and same pattern) as setBurndownGranularity/
  // applyActualCutoffs below: an in-place update left stale state behind
  // (old Monte Carlo/ML overlay datasets sized for the previous labels, zoom/
  // pan, credit markers) whenever the shape of the data actually changed.
  // Reloading with the new mode already resolved (see currentChartMode at
  // the top of this file) guarantees every plugin/overlay rebuilds clean —
  // "current"/"chained" are correct from the very first chart construction
  // above; only "view other contracts" needs the one-time patch below,
  // applied once after the page's own default view-range init has run.
  window.setBurndownChartMode = function (mode) {
    if (mode === currentChartMode) return;
    localStorage.setItem('fc-chart-mode', mode);
    sessionStorage.setItem('forecast-scroll', window.scrollY);
    if (typeof selectedSnaps !== 'undefined' && selectedSnaps.size > 0) {
      sessionStorage.setItem('forecast-snap-keys', JSON.stringify([...selectedSnaps.keys()]));
    }
    window.location.reload();
  };

  // Populate "View other contracts" + restore the persisted selection value.
  (function populateChartModeSelect() {
    const sel = document.getElementById('chart-mode-select');
    const group = document.getElementById('chart-mode-other-group');
    if (!sel || !group) return;
    (D.allContracts || []).forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.label;
      group.appendChild(opt);
    });
    sel.value = currentChartMode;
  })();

  // Whether Basic/MC/ML keep declining past zero (extendBelowZero) instead
  // of stopping dead — reload so every series (live-fetched and snapshot,
  // built across several separate code paths) rebuilds consistently.
  window.toggleNegativeCredits = function (on) {
    localStorage.setItem('fc-show-negative', on ? '1' : '0');
    window.location.reload();
  };
  (function () {
    const cb = document.getElementById('show-negative-credits');
    if (cb) cb.checked = showNegativeCredits();
  })();

  // ── Actual-line display options (Advanced settings) ──
  // Both are display-only and persisted per browser; the burn-rate math and
  // every forecast model keep using the full history regardless.
  const findActualDs = () => window.burndownChart.data.datasets.findIndex(
    d => d.label === 'Actual remaining'
  );

  // Trim: don't draw actual points before / after the chosen dates. Reads
  // both inputs so either bound (or both) can be active.
  window.applyActualCutoffs = function (persist = true) {
    const bc = window.burndownChart;
    if (!bc) return;
    const idx = findActualDs();
    if (idx < 0) return;
    const fromEl = document.getElementById('actual-cutoff');
    const toEl = document.getElementById('actual-cutoff-to');
    const before = ((fromEl && fromEl.value) || '').trim();
    const after = ((toEl && toEl.value) || '').trim();
    if (persist) {
      if (before) localStorage.setItem('fc-actual-cutoff', before);
      else localStorage.removeItem('fc-actual-cutoff');
      if (after) localStorage.setItem('fc-actual-cutoff-to', after);
      else localStorage.removeItem('fc-actual-cutoff-to');
      // A real (persist=true) cutoff change reloads the page instead of
      // patching the chart in place — no-animation in-place updates still
      // left the line's shape reading "off" until a fresh render, so this
      // just re-initializes the chart directly from the (now-persisted)
      // cutoff state, same as how a granularity switch already reloads
      // rather than redraws live. Scroll position and the current snapshot
      // selection are preserved the same way setBurndownGranularity does;
      // the on-load restore IIFE below picks the cutoff itself back up
      // from localStorage (persist=false, so it won't loop).
      sessionStorage.setItem('forecast-scroll', window.scrollY);
      if (typeof selectedSnaps !== 'undefined' && selectedSnaps.size > 0) {
        sessionStorage.setItem('forecast-snap-keys', JSON.stringify([...selectedSnaps.keys()]));
      }
      window.location.reload();
      return;
    }
    const labels = bc.data.labels;
    const values = labels.map(
      l => ((before && l < before) || (after && l > after))
        ? null : lookup(activeActualPts(), l)
    );
    // A "hide after" cutoff almost always lands BETWEEN two plotted labels
    // (e.g. weekly points, 7 days apart) rather than exactly on one, so the
    // line's last visible point sits one whole label short of whatever comes
    // next — a snapshot's forecast dot included. Carry the line one label
    // further so it visibly reaches that next point instead of stopping
    // short. Use the REAL value there (daily-resolution data when available,
    // so a weekly gap reflects the actual trend across those days rather
    // than a flat plateau); only fall back to repeating the last visible
    // value if no real data exists that far out at all. Using the real
    // value here (not a fabricated flat/interpolated one) is also what
    // makes this connect to the dot exactly: alignToActual looks up the
    // actual line's value at that same label to anchor the snapshot
    // overlay, so whatever real number lands here is exactly what the dot
    // snaps to — never a mismatch.
    if (after) {
      let lastVisible = -1;
      for (let i = 0; i < values.length; i++) if (values[i] != null) lastVisible = i;
      if (lastVisible >= 0 && lastVisible + 1 < values.length && values[lastVisible + 1] == null) {
        const nextLabel = labels[lastVisible + 1];
        const real = dailyActualPts.length ? lookup(dailyActualPts, nextLabel) : null;
        values[lastVisible + 1] = real != null ? real : values[lastVisible];
      }
    }
    bc.data.datasets[idx].data = values;
    // No animation: Chart.js's default eased transition interpolates the
    // actual line frame-by-frame between its old (uncut) and new (cut)
    // shape, and the removed points make that in-between animation dip
    // below the real trend for a moment — reading as a "sag" even though
    // the settled end state (and any other dataset using the same values,
    // e.g. a snapshot's aligned dot) was correct the whole time. Same
    // no-animation convention already used for other instant chart-state
    // updates in this file (view range, colors, legend toggles).
    bc.chart.update('none');
  };
  window.clearActualCutoffs = function () {
    ['actual-cutoff', 'actual-cutoff-to'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    window.applyActualCutoffs();
  };

  // Hide the whole line. Sets dataset.hidden too so the fullscreen copy
  // (which clones data, not runtime meta) matches what's on screen.
  window.toggleActualLine = function (show, persist = true) {
    const bc = window.burndownChart;
    if (!bc) return;
    const idx = findActualDs();
    if (idx < 0) return;
    bc.data.datasets[idx].hidden = !show;
    bc.chart.getDatasetMeta(idx).hidden = !show;
    if (persist) localStorage.setItem('fc-actual-hidden', show ? '0' : '1');
    const cb = document.getElementById('actual-show');
    if (cb) cb.checked = !!show;
    bc.chart.update();
    refreshBurndownLegend();
  };

  (function () {
    const before = localStorage.getItem('fc-actual-cutoff') || '';
    const after = localStorage.getItem('fc-actual-cutoff-to') || '';
    const fromEl = document.getElementById('actual-cutoff');
    const toEl = document.getElementById('actual-cutoff-to');
    if (fromEl) fromEl.value = before;
    if (toEl) toEl.value = after;
    if (before || after) window.applyActualCutoffs(false);
    if (localStorage.getItem('fc-actual-hidden') === '1') {
      window.toggleActualLine(false, false);
    }
  })();

  // On-chart legend (baked into the PNG export). Default off for a clean plot.
  window.toggleChartLegend = function (on) {
    localStorage.setItem('fc-legend-on', on ? '1' : '0');
    const bc = window.burndownChart;
    if (!bc) return;
    bc.chart.options.plugins.legend.display = !!on;
    bc.chart.update();
  };
  (function () {
    const on = localStorage.getItem('fc-legend-on') === '1';
    const cb = document.getElementById('legend-on-chart');
    if (cb) cb.checked = on;
  })();

  refreshBurndownLegend();

  window.setBurndownGranularity = function(gran) {
    if (gran === currentGranularity) return;
    localStorage.setItem('fc-gran', gran);
    sessionStorage.setItem('forecast-scroll', window.scrollY);
    if (typeof selectedSnaps !== 'undefined' && selectedSnaps.size > 0) {
      sessionStorage.setItem('forecast-snap-keys', JSON.stringify([...selectedSnaps.keys()]));
    }
    const url = new URL(window.location.href);
    url.searchParams.set('granularity', gran);
    url.searchParams.delete('exclude_partial');
    window.location.href = url.toString();
  };

  if (currentGranularity === 'daily') {
    document.getElementById('gran-weekly').classList.remove('active');
    document.getElementById('gran-daily').classList.add('active');
  }
  applyBurndownXAxisStyle(window.burndownChart, currentGranularity);
  initBurndownViewRange();
  initBurndownYRange();
  // "current"/"chained"/"overall" are already correctly built from scratch
  // (see buildProjPts, defaultViewRange, activeMarkerEvents — all resolve
  // currentChartMode before this point). Only "view other contracts" needs a
  // one-time patch, applied now that the default view-range init above has
  // already run (it would otherwise overwrite this).
  if (!isMainChartMode(currentChartMode)) {
    applyOtherContractView(currentChartMode);
  }
  restorePendingSnapshotSelections();

  // Confirmed bug: after some container resizes, Chart.js recomputes the x
  // scale (so ticks and the credit-marker plugin, which reads
  // scales.x.getPixelForValue() fresh every draw, land correctly) but leaves
  // the dataset's cached point positions (meta.data[i].x) stale — the
  // actual/projected LINE then renders at an old x that no longer matches
  // the axis, until something forces a full update. A one-time fix at
  // window 'load' only catches a resize that happens to land before that
  // event; any later resize (sidebar content shifting, font swap, etc.)
  // reintroduces the same desync. Watch the container directly and force a
  // full update (not just resize()) on every observed size change so
  // dataset positions are always recomputed to match the current scale.
  if (typeof ResizeObserver !== 'undefined') {
    const container = window.burndownChart.chart.canvas.parentElement;
    if (container) {
      let raf = null;
      const ro = new ResizeObserver(() => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = null;
          const bc = window.burndownChart;
          if (bc && bc.chart) bc.chart.update();
        });
      });
      ro.observe(container);
    }
  }
})();

/* ===================================================================== *
 * Prediction model overlays (Monte Carlo + Linear Regression)
 * ===================================================================== */
(function () {
  if (!document.getElementById('burndownChart')) return;
  // The server's MC/ML series anchor exactly at "today" with today's precise
  // value, while the actual line (weekly view especially) may end earlier, at
  // the last uploaded week (or a known credit-grant date in between) rather
  // than today itself. A same-index value-only shift left the band floating
  // with a visible x/y gap whenever the actual line had no plotted value at
  // MC/ML's own "today" label (routine once data lags by more than a few
  // days). Instead, date-shift + value-shift the RAW series so its very
  // first point lands exactly on the chart's own anchor — the same one the
  // "Projected remaining" line starts from — before it's interpolated onto
  // the chart's labels, guaranteeing the band starts exactly where the
  // actual line stops. In "view other contracts" mode that anchor is a
  // DIFFERENT contract's own (see applyOtherContractView), not the active
  // contract's window.burndownProjAnchor. `anchorOverride`, when given,
  // wins over both — used to snap a CHAINED mode's next-contract band onto
  // that contract's own start instead of the active contract's anchor.
  function anchorShiftFor(series, dateKey, valKey, anchorOverride) {
    const anchor = anchorOverride || (window._fcOtherContract ? window._fcOtherContractAnchor : window.burndownProjAnchor);
    if (!anchor || !series || !series.length) return null;
    const first = series[0];
    const deltaDays = Math.round(
      (new Date(anchor.date + 'T12:00:00') - new Date(first[dateKey] + 'T12:00:00')) / 86400000
    );
    const deltaVal = anchor.remaining - first[valKey];
    return (deltaDays || deltaVal) ? { deltaDays, deltaVal } : null;
  }
  function applyShift(series, shift, dateKey, valKey) {
    if (!shift || !series || !series.length) return series;
    return series.map(p => {
      const d = new Date(p[dateKey] + 'T12:00:00');
      d.setDate(d.getDate() + shift.deltaDays);
      return { ...p, [dateKey]: d.toISOString().slice(0, 10), [valKey]: Math.max(p[valKey] + shift.deltaVal, 0) };
    });
  }
  // Null out any values on/after cutoffDate — applied to the ACTIVE
  // contract's own MC/ML band in "chained" mode so it stops exactly at the
  // boundary into the next contract, the same way the deterministic "Base"
  // line rides into that marker and ends there (see buildProjPts) instead
  // of running its normal fuller horizon (toward exhaustion) through where
  // a different contract's own band has already taken over.
  function trimAtCutoff(values, labels, cutoffDate) {
    if (!cutoffDate || !values) return values;
    return values.map((v, i) => (labels[i] > cutoffDate ? null : v));
  }
  // One fetch target per segment of the burndown line: the active contract
  // (default resolution, no contract_id), plus — in "chained" mode — one per
  // future contract in the chain, each asking the server for THAT
  // contract's own Monte Carlo / ML stats (via contract_id + its own
  // [start, end] burn window) and carrying the anchor its band should snap
  // onto (its carry-aware starting balance — the same value the
  // deterministic "Projected — <contract>" line starts from, computed once
  // in buildFutureContractSegments so both stay consistent).
  function chainedFetchTargets() {
    // The default (idx-0) target's anchor must be the OTHER contract's own
    // (window._fcOtherContractAnchor) when in "view other contracts" mode —
    // it used to always fall back to window.burndownProjAnchor (the ACTIVE
    // contract's), which shifted the fetched MC/ML series onto the active
    // contract's dates. Interpolated onto the other contract's own (totally
    // different) chart labels, every point then fell outside the label
    // range and nothing rendered — MC/ML looked like it "didn't work" for
    // any non-current contract.
    const defaultAnchor = window._fcOtherContract ? window._fcOtherContractAnchor : window.burndownProjAnchor;
    const targets = [{ extra: {}, anchor: defaultAnchor, label: '' }];
    if (window._fcChartMode === 'chained' || window._fcChartMode === 'overall') {
      const boundaries = window._fcContractBoundaries || [];
      const segs = window._fcFutureSegments || [];
      boundaries.forEach((b, i) => {
        if (!b.id) return; // no contract id on this boundary -> can't ask the server for it
        const seg = segs[i];
        const startValue = seg && seg.data.length ? seg.data[0][1] : (Number(b.delta) || 0);
        targets.push({
          extra: {
            contract_id: b.id,
            data_from: b.date,
            data_to: b.end || '',
            credits_override: String(startValue),
          },
          anchor: { date: b.date, remaining: startValue },
          label: b.label || '',
        });
      });
    }
    return targets;
  }

  const MC_RUNS = D.mcRuns;
  let mcCache  = null;
  let mcLoading = null;
  let detDataset = null;
  let lrCache = null, lrLoading = null;

  window.toggleForecastModel = function (modelId, enabled, persist = true) {
    if (persist) localStorage.setItem('forecast-model-' + modelId, enabled ? '1' : '0');
    if (modelId === 'deterministic') {
      const bc = window.burndownChart;
      if (!bc) return;
      if (enabled) {
        if (detDataset) { bc.data.datasets.splice(1, 0, detDataset); detDataset = null; }
      } else {
        detDataset = bc.data.datasets.splice(1, 1)[0] || null;
      }
      bc.update();
      refreshBurndownLegend();
    } else if (modelId === 'monte_carlo') {
      if (enabled) loadMcOverlay(); else removeMcOverlay();
    } else if (modelId === 'linear_regression') {
      if (enabled) loadLrOverlay(); else removeLrOverlay();
    }
  };

  // Shared by getLrData/getMcData: current-URL params plus the model-specific
  // ones, fetched from the /forecast/model-data endpoint they both hit.
  // In "view other contracts" mode, target THAT contract instead of the
  // active one: contract_id tells the server which contract's own
  // dates/ledger to run the forecast engine against, and data_from/data_to
  // (the existing "Advanced burn window" params) scope the burn-rate
  // statistics to that contract's own operational weeks — otherwise the
  // server would blend in whatever other contracts' weeks are also in the
  // pipeline's operational data.
  function fetchModelData(extraParams) {
    const params = new URLSearchParams(window.location.search);
    Object.entries(extraParams).forEach(([k, v]) => params.set(k, v));
    const other = window._fcOtherContract;
    if (other && other.id) {
      params.set('contract_id', other.id);
      if (other.start) params.set('data_from', other.start);
      if (other.end) params.set('data_to', other.end);
    }
    return fetch('/forecast/model-data?' + params.toString())
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
  }

  // ── Linear Trend (ML) overlay ──
  // lrCache is an ARRAY, one entry per chainedFetchTargets() target: [0] is
  // always the active contract; [1+] (chained mode only) are the future
  // contracts in the chain, each fetched with its own contract_id/anchor so
  // its band picks up right where the previous segment's line does.
  function getLrData() {
    if (lrCache) return Promise.resolve(lrCache);
    if (!lrLoading) {
      const targets = chainedFetchTargets();
      lrLoading = Promise.allSettled(
        targets.map(t => fetchModelData({ model: 'linear_regression', ...t.extra })
          .then(data => ({ data, anchor: t.anchor, label: t.label })))
      ).then(results => {
        const ok = results.filter(r => r.status === 'fulfilled').map(r => r.value);
        lrCache = ok;
        lrLoading = null;
        if (!ok.length) throw new Error('No segments loaded');
        return ok;
      }).catch(e => { lrLoading = null; throw e; });
    }
    return lrLoading;
  }

  function applyLrToChart(entries) {
    const bc = window.burndownChart;
    if (!bc) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._lrOverlay);
    const allLabels = bc.data.labels;
    const C = '#198754';
    // Active contract's band (entry 0) stops at the first boundary in
    // chained mode — see trimAtCutoff.
    // Cutoff is the active contract's OWN end date, not the next contract's
    // start (window._fcContractBoundaries[0].date) — see buildProjPts for
    // why: riding through the gap between them drew an extra, out-of-window
    // day (invisible in Weekly, visible in Daily).
    const chainCutoff = (window._fcChartMode === 'chained' || window._fcChartMode === 'overall') && window._fcContractBoundaries
      && window._fcContractBoundaries[0] ? D.contractEndDate : null;
    entries.forEach(({ data, anchor, label }, idx) => {
      if (!data || data.error) return;
      const suffix = label ? ` — ${label}` : '';
      const cutoff = idx === 0 ? chainCutoff : null;
      const shift = anchorShiftFor(data.burndown, 'date', 'value', anchor);
      let p50 = interpDateSeries(applyShift(data.burndown, shift, 'date', 'value'), allLabels);
      const hasBand = data.p10 && data.p90 && data.p10.length && data.p90.length;
      let p90 = hasBand ? interpDateSeries(applyShift(data.p90, shift, 'date', 'value'), allLabels) : null;
      let p10 = hasBand ? interpDateSeries(applyShift(data.p10, shift, 'date', 'value'), allLabels) : null;
      p50 = trimAtCutoff(extendBelowZero(p50), allLabels, cutoff);
      if (hasBand) {
        p90 = trimAtCutoff(extendBelowZero(p90), allLabels, cutoff);
        p10 = trimAtCutoff(extendBelowZero(p10), allLabels, cutoff);
      }
      if (hasBand) {
        bc.data.datasets.push({
          label: `LR P90${suffix}`, data: p90,
          borderColor: hexToRgba(C, 0.4), borderWidth: 1, borderDash: [2, 3],
          backgroundColor: hexToRgba(C, 0.10), fill: '+1', tension: 0.1, pointRadius: 0,
          spanGaps: false, _lrOverlay: true, _noTooltip: true, _noLegend: true,
        });
        bc.data.datasets.push({
          label: `LR P10${suffix}`, data: p10,
          borderColor: hexToRgba(C, 0.4), borderWidth: 1, borderDash: [2, 3],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0,
          spanGaps: false, _lrOverlay: true, _noTooltip: true, _noLegend: true,
        });
      }
      bc.data.datasets.push({
        label: `Linear Trend (ML)${suffix}`, data: p50,
        borderColor: C, borderWidth: 2, borderDash: [6, 3],
        backgroundColor: 'transparent', fill: false, tension: 0.1,
        pointRadius: 0, pointHoverRadius: 5, pointHitRadius: D.granularity === 'daily' ? 8 : 4, spanGaps: false,
        _lrOverlay: true, _noLegend: !!suffix,
      });
    });
    bc.update();
    refreshBurndownLegend();
  }

  async function loadLrOverlay() {
    const status = document.getElementById('lr-status');
    if (status && !lrCache) status.textContent = 'loading…';
    try {
      const entries = await getLrData();
      applyLrToChart(entries);
      if (status) {
        const m = (entries[0] && entries[0].data && entries[0].data.metadata) || {};
        status.textContent = (m.slope_credits_per_week != null)
          ? `slope ${Math.round(m.slope_credits_per_week).toLocaleString()}/wk · R² ${m.r_squared}`
          : '';
      }
    } catch (e) {
      if (status) status.textContent = 'load failed';
      const cb = document.getElementById('model-lr');
      if (cb) cb.checked = false;
    }
  }

  function removeLrOverlay() {
    const bc = window.burndownChart;
    if (!bc) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._lrOverlay);
    bc.update();
    refreshBurndownLegend();
    const status = document.getElementById('lr-status');
    if (status) status.textContent = '';
  }
  window.refreshLrOverlay = function () { if (lrCache) applyLrToChart(lrCache); };

  // mcCache is an ARRAY, same convention as lrCache above: [0] the active
  // contract, [1+] (chained mode only) one per future contract in the chain.
  function getMcData() {
    if (mcCache) return Promise.resolve(mcCache);
    if (!mcLoading) {
      const targets = chainedFetchTargets();
      mcLoading = Promise.allSettled(
        targets.map(t => fetchModelData({ model: 'monte_carlo', runs: MC_RUNS, ...t.extra })
          .then(data => ({ data, anchor: t.anchor, label: t.label })))
      ).then(results => {
        const ok = results.filter(r => r.status === 'fulfilled').map(r => r.value);
        mcCache = ok;
        mcLoading = null;
        if (!ok.length) throw new Error('No segments loaded');
        return ok;
      }).catch(e => { mcLoading = null; throw e; });
    }
    return mcLoading;
  }

  async function loadMcOverlay() {
    const bc = window.burndownChart;
    if (!bc) return;
    const status = document.getElementById('mc-status');
    if (status && !mcCache) { status.textContent = 'loading…'; status.style.display = ''; }
    try {
      const entries = await getMcData();
      applyMcToChart(entries);
      const ctrl = document.getElementById('mc-band-controls');
      if (ctrl) ctrl.style.display = '';
      if (status) {
        const md = (entries[0] && entries[0].data && entries[0].data.metadata) || {};
        const ep = md.exhaustion_probability != null
          ? Math.round(md.exhaustion_probability * 100) + '% exhaustion risk  ·  '
            + (md.runs || 0).toLocaleString() + ' runs'
          : '';
        status.textContent = ep;
        status.style.display = ep ? '' : 'none';
      }
    } catch (e) {
      console.error('MC fetch failed:', e);
      if (status) { status.textContent = 'load failed'; status.style.display = ''; }
      const cb = document.getElementById('model-mc');
      if (cb) cb.checked = false;
    }
  }

  window.toggleMcBand = function (band, visible) {
    localStorage.setItem('forecast-mc-' + band, visible ? '1' : '0');
    applyMcBands(visible ? 'show' : 'default');
  };

  function updateMcStats(data) {
    const md     = data.metadata || {};
    const ep     = md.exhaustion_probability != null ? md.exhaustion_probability : null;
    const runs   = md.runs || 0;
    // Contract-end balances come from metadata now that the plotted series
    // can extend past contract end (to exhaustion); fall back to series end
    // for older snapshot payloads.
    const p10End = md.p10_end_balance != null ? md.p10_end_balance
      : (data.p10 && data.p10.length ? data.p10[data.p10.length - 1].value : null);
    const p50End = md.p50_end_balance != null ? md.p50_end_balance
      : (data.burndown && data.burndown.length ? data.burndown[data.burndown.length - 1].value : null);
    const p90End = md.p90_end_balance != null ? md.p90_end_balance
      : (data.p90 && data.p90.length ? data.p90[data.p90.length - 1].value : null);
    const p10ExhDate = md.p10_exhaustion_date || '—';
    const p50ExhDate = md.p50_exhaustion_date || '—';
    const p90ExhDate = md.p90_exhaustion_date || '—';
    const p10Ceb = md.p10_contract_end_balance != null ? md.p10_contract_end_balance : null;
    const p50Ceb = md.p50_contract_end_balance != null ? md.p50_contract_end_balance : null;
    const p90Ceb = md.p90_contract_end_balance != null ? md.p90_contract_end_balance : null;

    const riskCls = ep === null ? '' : ep > 0.5 ? 'text-danger' : ep > 0.1 ? 'text-warning' : 'text-success';
    const balCls  = v => v !== null && v < 0 ? 'text-danger' : v !== null ? 'text-success' : '';
    const fmtBal  = v => v !== null ? Math.round(v).toLocaleString() : '—';

    const probEl = document.getElementById('mc-kpi-prob');
    const subEl  = document.getElementById('mc-kpi-sub');
    if (probEl) {
      if (ep !== null) {
        probEl.innerHTML = `<span class="${riskCls}">${Math.round(ep * 100)}%</span>`;
        if (subEl) subEl.textContent = `P50 end: ${p50End !== null ? Math.round(p50End).toLocaleString() : '—'}`;
      } else {
        probEl.textContent = '—';
      }
    }

    const badge = document.getElementById('mc-acc-badge');
    if (badge && ep !== null) {
      badge.textContent = Math.round(ep * 100) + '% risk';
      badge.className   = `ms-2 badge bg-${ep > 0.5 ? 'danger' : ep > 0.1 ? 'warning' : 'success'}`;
      badge.style.display = '';
    }

    const body = document.getElementById('mc-acc-body');
    if (body) {
      const interp = ep === null ? '' : ep > 0.5
        ? 'High risk — credits likely exhausted before contract end at current burn rate.'
        : ep > 0.1
        ? 'Moderate risk — some probability of exhaustion; monitor burn rate closely.'
        : 'Low risk — credits expected to last through contract end in most scenarios.';
      body.innerHTML = `
        <div class="row g-3">
          <div class="col-md-5">
            <p class="text-muted small mb-1 fw-semibold">Simulation Summary</p>
            <table class="table table-sm mb-2">
              <tr><td>Exhaustion probability</td><td class="text-end fw-semibold ${riskCls}">${ep !== null ? Math.round(ep * 100) + '%' : '—'}</td></tr>
              <tr><td>Simulation runs</td><td class="text-end">${runs.toLocaleString()}</td></tr>
            </table>
            ${interp ? `<p class="mb-0 small ${riskCls}">${interp}</p>` : ''}
          </div>
          <div class="col-md-7">
            <p class="text-muted small mb-1 fw-semibold">End Balance Distribution</p>
            <table class="table table-sm mb-0">
              <thead><tr><th>Percentile</th><th class="text-end">End Balance</th><th class="text-end">Contract End Balance</th><th class="text-end">Exhaustion Date</th><th class="text-muted text-end" style="font-size:.7rem;">Interpretation</th></tr></thead>
              <tbody>
                <tr><td>P10 <span class="text-muted small">(pessimistic)</span></td><td class="text-end ${balCls(p10End)}">${fmtBal(p10End)}</td><td class="text-end ${balCls(p10Ceb)}">${fmtBal(p10Ceb)}</td><td class="text-end">${p10ExhDate}</td><td class="text-muted text-end small">90% of runs end higher</td></tr>
                <tr><td>P50 <span class="text-muted small">(median)</span></td><td class="text-end ${balCls(p50End)}">${fmtBal(p50End)}</td><td class="text-end ${balCls(p50Ceb)}">${fmtBal(p50Ceb)}</td><td class="text-end">${p50ExhDate}</td><td class="text-muted text-end small">most likely outcome</td></tr>
                <tr><td>P90 <span class="text-muted small">(optimistic)</span></td><td class="text-end ${balCls(p90End)}">${fmtBal(p90End)}</td><td class="text-end ${balCls(p90Ceb)}">${fmtBal(p90Ceb)}</td><td class="text-end">${p90ExhDate}</td><td class="text-muted text-end small">10% of runs end higher</td></tr>
              </tbody>
            </table>
          </div>
        </div>`;
    }
  }

  function applyMcToChart(_entries) {
    applyMcBands('show');
  }

  function applyMcBands(mode) {
    const bc = window.burndownChart;
    if (!bc || !mcCache || !mcCache.length) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._mcOverlay);

    const allLabels = bc.data.labels;
    const showP90 = localStorage.getItem('forecast-mc-p90') !== '0';
    const showP10 = localStorage.getItem('forecast-mc-p10') !== '0';
    const showP50 = localStorage.getItem('forecast-mc-p50') !== '0';
    const p90Fill = !showP10 ? false : showP50 ? '+2' : '+1';
    // Active contract's band (entry 0) stops at the first boundary in
    // chained mode — same "ride into the marker, end there" cutoff the
    // deterministic Base line uses (see buildProjPts).
    // Cutoff is the active contract's OWN end date, not the next contract's
    // start (window._fcContractBoundaries[0].date) — see buildProjPts for
    // why: riding through the gap between them drew an extra, out-of-window
    // day (invisible in Weekly, visible in Daily).
    const chainCutoff = (window._fcChartMode === 'chained' || window._fcChartMode === 'overall') && window._fcContractBoundaries
      && window._fcContractBoundaries[0] ? D.contractEndDate : null;

    mcCache.forEach(({ data, anchor, label }, idx) => {
      if (!data || data.error) return;
      const suffix = label ? ` — ${label}` : '';
      const cutoff = idx === 0 ? chainCutoff : null;
      const shift = anchorShiftFor(data.burndown, 'date', 'value', anchor);
      let p90data = interpDateSeries(applyShift(data.p90, shift, 'date', 'value'), allLabels);
      let p50data = interpDateSeries(applyShift(data.burndown, shift, 'date', 'value'), allLabels);
      let p10data = interpDateSeries(applyShift(data.p10, shift, 'date', 'value'), allLabels);
      p90data = trimAtCutoff(extendBelowZero(p90data), allLabels, cutoff);
      p50data = trimAtCutoff(extendBelowZero(p50data), allLabels, cutoff);
      p10data = trimAtCutoff(extendBelowZero(p10data), allLabels, cutoff);

      if (showP90) {
        bc.data.datasets.push({
          label: `MC P90 (optimistic)${suffix}`,
          data: p90data,
          borderColor: 'rgba(253,126,20,0.55)', borderWidth: 1.5, borderDash: [3, 3],
          backgroundColor: showP10 ? 'rgba(253,126,20,0.18)' : 'transparent',
          fill: p90Fill,
          pointRadius: 0, tension: 0.1, spanGaps: false,
          _mcOverlay: true, _mcBand: 'p90', _noLegend: true,
        });
      }
      if (showP50) {
        bc.data.datasets.push({
          label: `MC P50 (median)${suffix}`,
          data: p50data,
          borderColor: '#fd7e14', borderWidth: 2.5, borderDash: [6, 3],
          backgroundColor: 'transparent', fill: false,
          pointRadius: 0, pointHoverRadius: 5, pointHitRadius: D.granularity === 'daily' ? 8 : 4, tension: 0.1, spanGaps: false,
          _mcOverlay: true, _mcBand: 'p50', _noLegend: !!suffix,
        });
      }
      if (showP10) {
        bc.data.datasets.push({
          label: `MC P10 (pessimistic)${suffix}`,
          data: p10data,
          borderColor: 'rgba(253,126,20,0.55)', borderWidth: 1.5, borderDash: [3, 3],
          backgroundColor: 'transparent', fill: false,
          pointRadius: 0, tension: 0.1, spanGaps: false,
          _mcOverlay: true, _mcBand: 'p10', _noLegend: true,
        });
      }
    });

    ['p90', 'p50', 'p10'].forEach(band => {
      if (localStorage.getItem('forecast-mc-' + band) === '0') {
        const cb = document.getElementById('mc-show-' + band);
        if (cb) cb.checked = false;
      }
    });

    bc.chart.update(mode || 'default');
    refreshBurndownLegend();
  }

  window.refreshMcBands = function() { if (mcCache) applyMcBands('none'); };

  function removeMcOverlay() {
    const bc = window.burndownChart;
    if (!bc) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._mcOverlay);
    bc.update();
    refreshBurndownLegend();
    const ctrl = document.getElementById('mc-band-controls');
    if (ctrl) ctrl.style.display = 'none';
    mcCache = null;
  }

  // Snapshot mode is a transient preset on the live page: hide the live
  // forecasts (base + MC/ML) and cut the actual at the snapshot's date, so the
  // selected snapshot's own overlay is what shows. Everything is applied
  // WITHOUT persisting, so returning to the live view is perfectly clean.
  if (D.snapshotTs) {
    window.toggleForecastModel('deterministic', false, false);  // hide base, don't persist
    // Cut the actual line where this snapshot's forecast starts (the day
    // before its data anchor), same as the per-snapshot Cut button.
    const snapObj = (D.snapshots || []).find(h => h.snapshot_ts === D.snapshotTs);
    const fcStart = snapObj ? String(snapObj.latest_usage_date || snapObj.snapshot_date || '').slice(0, 10) : '';
    const cutDay = fcStart ? shiftDayStr(fcStart, -1) : '';
    const toEl = document.getElementById('actual-cutoff-to');
    if (toEl && cutDay && typeof window.applyActualCutoffs === 'function') {
      toEl.value = cutDay;
      window.applyActualCutoffs(false);  // transient
    }
  } else {
    // Restore model overlays from the last session (pills read the same state).
    if (localStorage.getItem('forecast-model-deterministic') === '0') {
      window.toggleForecastModel('deterministic', false);
    }
    if (localStorage.getItem('forecast-model-monte_carlo') === '1') {
      window.toggleForecastModel('monte_carlo', true);
    }
    if (localStorage.getItem('forecast-model-linear_regression') === '1') {
      window.toggleForecastModel('linear_regression', true);
    }
  }
  refreshBurndownLegend();

  // ── ML (linear trend) statistics — KPI card + accordion ──
  function updateMlStats(data) {
    const md = (data && data.metadata) || {};
    const slope   = md.slope_credits_per_week;
    const r2      = md.r_squared;
    const quality = md.model_quality;
    const dir     = md.trend_direction;
    const fmt = v => (v == null ? '—' : Math.round(v).toLocaleString());

    const slopeEl = document.getElementById('ml-kpi-slope');
    const subEl   = document.getElementById('ml-kpi-sub');
    if (slopeEl) {
      if (slope != null) {
        slopeEl.textContent = (slope > 0 ? '+' : '') + fmt(slope) + '/wk';
        slopeEl.style.color = slope > 0 ? '#dc3545' : (slope < 0 ? '#198754' : '');
      } else {
        slopeEl.textContent = '—';
      }
    }
    if (subEl) {
      subEl.textContent = (r2 != null) ? ('R² ' + r2)
                        : (quality ? quality.replace(/_/g, ' ') : '—');
    }

    const badge = document.getElementById('ml-acc-badge');
    if (badge && quality) {
      const cls = quality === 'strong_fit' ? 'success'
                : quality === 'moderate_fit' ? 'warning'
                : quality === 'weak_fit' ? 'secondary' : 'info';
      badge.textContent = quality.replace(/_/g, ' ');
      badge.className = 'ms-2 badge bg-' + cls;
      badge.style.display = '';
    }

    const body = document.getElementById('ml-acc-body');
    if (!body) return;
    if (md.insufficient_data) {
      body.innerHTML = '<div class="text-muted small">Not enough weekly history to fit a reliable trend yet.</div>';
      return;
    }
    const dirCls = dir === 'increasing' ? 'text-danger'
                 : dir === 'decreasing' ? 'text-success' : 'text-muted';
    const p10 = data.p10 && data.p10.length ? data.p10[data.p10.length - 1].value : md.p10_end_balance;
    const p50 = data.burndown && data.burndown.length ? data.burndown[data.burndown.length - 1].value : md.p50_end_balance;
    const p90 = data.p90 && data.p90.length ? data.p90[data.p90.length - 1].value : md.p90_end_balance;
    const p10ExhDate = md.p10_exhaustion_date || '—';
    const p50ExhDate = md.p50_exhaustion_date || '—';
    const p90ExhDate = md.p90_exhaustion_date || '—';
    const p10Ceb = md.p10_contract_end_balance != null ? md.p10_contract_end_balance : null;
    const p50Ceb = md.p50_contract_end_balance != null ? md.p50_contract_end_balance : null;
    const p90Ceb = md.p90_contract_end_balance != null ? md.p90_contract_end_balance : null;
    const balCls = v => v != null && v < 0 ? 'text-danger' : v != null ? 'text-success' : '';
    body.innerHTML = `
      <div class="row g-3">
        <div class="col-md-6">
          <p class="text-muted small mb-1 fw-semibold">Trend Fit</p>
          <table class="table table-sm mb-0">
            <tr><td>Direction</td><td class="text-end fw-semibold ${dirCls}">${dir ? dir.replace(/_/g,' ') : '—'}</td></tr>
            <tr><td>Slope</td><td class="text-end">${slope != null ? (slope > 0 ? '+' : '') + fmt(slope) + ' / wk' : '—'}</td></tr>
            <tr><td>R² (fit quality)</td><td class="text-end">${r2 != null ? r2 : '—'}</td></tr>
            <tr><td>RMSE</td><td class="text-end">${fmt(md.rmse)}</td></tr>
            <tr><td>Weeks of history</td><td class="text-end">${md.observations_used != null ? md.observations_used : '—'}</td></tr>
            <tr><td>Engine</td><td class="text-end text-muted small">${md.model_engine || 'linear regression'}</td></tr>
          </table>
        </div>
        <div class="col-md-6">
          <p class="text-muted small mb-1 fw-semibold">Projected End Balance</p>
          <table class="table table-sm mb-2">
            <thead><tr><th></th><th class="text-end">End Balance</th><th class="text-end">Contract End Balance</th><th class="text-end">Exhaustion Date</th></tr></thead>
            <tr><td>P10 <span class="text-muted small">(pessimistic)</span></td><td class="text-end ${balCls(p10)}">${fmt(p10)}</td><td class="text-end ${balCls(p10Ceb)}">${fmt(p10Ceb)}</td><td class="text-end">${p10ExhDate}</td></tr>
            <tr><td>P50 <span class="text-muted small">(expected)</span></td><td class="text-end ${balCls(p50)}">${fmt(p50)}</td><td class="text-end ${balCls(p50Ceb)}">${fmt(p50Ceb)}</td><td class="text-end">${p50ExhDate}</td></tr>
            <tr><td>P90 <span class="text-muted small">(optimistic)</span></td><td class="text-end ${balCls(p90)}">${fmt(p90)}</td><td class="text-end ${balCls(p90Ceb)}">${fmt(p90Ceb)}</td><td class="text-end">${p90ExhDate}</td></tr>
          </table>
          ${md.projected_exhaustion_date
            ? `<p class="mb-0 small text-danger">Trend projects exhaustion around ${md.projected_exhaustion_date}.</p>`
            : `<p class="mb-0 small text-success">Trend does not project exhaustion before contract end.</p>`}
        </div>
      </div>`;
  }

  // Auto-load MC/ML stats on page load (also serves chart overlay if it was
  // restored) — LIVE data, so it must not run in snapshot mode: the server
  // already rendered the Risk+ML KPI card from the snapshot's own frozen
  // stats, and this fetch would silently clobber it with today's live
  // numbers (same regardless of which snapshot was picked — the actual bug
  // behind "doesn't update to the snapshot selected").
  if (!D.snapshotTs) {
    // The KPI/accordion stats always reflect segment 0 — the active
    // contract's own numbers — even in chained mode where getMcData()/
    // getLrData() resolve an array with one entry per chain segment.
    getMcData().then(entries => updateMcStats(entries[0].data)).catch(() => {
      const probEl = document.getElementById('mc-kpi-prob');
      if (probEl) probEl.textContent = '—';
      const body = document.getElementById('mc-acc-body');
      if (body) body.innerHTML = '<div class="text-muted small">Simulation data unavailable.</div>';
    });

    getLrData().then(entries => updateMlStats(entries[0].data)).catch(() => {
      const slopeEl = document.getElementById('ml-kpi-slope');
      if (slopeEl) slopeEl.textContent = '—';
      const subEl = document.getElementById('ml-kpi-sub');
      if (subEl) subEl.textContent = 'unavailable';
      const body = document.getElementById('ml-acc-body');
      if (body) body.innerHTML = '<div class="text-muted small">ML model data unavailable.</div>';
    });
  }
})();

/* ===================================================================== *
 * Weekly / Active Users / Usage Type / Cumulative charts
 * ===================================================================== */
(function () {
  if (!document.getElementById('weeklyChart')) return;
  const rawData    = D.weeklyChartData || [];
  const labels     = rawData.map(d => d.week_start);
  const credits    = rawData.map(d => d.total_credits_used);
  const inContract = rawData.map(d => d.in_contract);
  window._weeklyInContract = inContract;
  const wCol = getChartColor('weekly');
  const bgColors   = inContract.map(ic => ic ? hexToRgba(wCol, 0.72) : 'rgba(108,117,125,0.35)');
  const bdColors   = inContract.map(ic => ic ? wCol                   : 'rgba(108,117,125,0.7)');

  window.weeklyChart = new BNLChart('weeklyChart', {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Credits used', data: credits, backgroundColor: bgColors, borderColor: bdColors, borderWidth: 1, borderRadius: 4 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => 'Week of ' + items[0].label,
            label: ctx => `  ${ctx.parsed.y.toLocaleString()} credits`,
            footer: items => inContract[items[0].dataIndex] ? 'In contract period' : 'Pre-contract',
          },
          footerColor: ctx => inContract[ctx[0]?.dataIndex] ? '#6ea8fe' : '#adb5bd',
        },
        zoom: {
          zoom: { wheel: { enabled: true, modifierKey: 'ctrl' }, pinch: { enabled: true }, mode: 'x' },
          pan:  { enabled: true, mode: 'x' },
        },
      },
      scales: {
        y: { beginAtZero: true, ticks: { callback: v => v >= 1000 ? (v/1000).toFixed(0)+'k' : v, font: { size: 10 } }, grid: { color: 'rgba(0,0,0,.05)' } },
        x: { ticks: { maxRotation: 40, font: { size: 10 }, maxTicksLimit: 20 }, grid: { display: false } },
      },
    },
  }, { exportName: 'Weekly Credit Burn' });

  const firstContractIdx = inContract.indexOf(true);
  if (firstContractIdx > 0 && labels.length > firstContractIdx + 4) {
    try { window.weeklyChart.chart.zoomScale('x', { min: labels[Math.max(0, firstContractIdx - 1)], max: labels[labels.length - 1] }, 'none'); } catch (_) {}
  }
})();

(function () {
  if (!document.getElementById('activeUsersChart')) return;
  const auData = D.activeUsers || [];
  if (!auData.length) return;
  const labels   = auData.map(d => d.week_start);
  const values   = auData.map(d => d.active_users);
  const ic       = auData.map(d => d.in_contract);
  window.activeUsersChart = new BNLChart('activeUsersChart', {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Active users', data: values, backgroundColor: ic.map(v => v ? 'rgba(111,66,193,0.7)' : 'rgba(108,117,125,0.35)'), borderRadius: 4 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: items => 'Week of ' + items[0].label,
          label: ctx => `  Active users: ${ctx.parsed.y}`,
          footer: items => ic[items[0].dataIndex] ? 'In contract period' : 'Pre-contract',
        }},
      },
      scales: {
        y: { beginAtZero: true, ticks: { stepSize: 1, font: { size: 10 } }, grid: { color: 'rgba(0,0,0,.05)' } },
        x: { ticks: { maxRotation: 40, maxTicksLimit: 16, font: { size: 10 } }, grid: { display: false } },
      },
    },
  }, { exportName: 'Active Users per Week' });
})();

// Credits by Usage Type per Week (stacked bar)
window.forecastUsageTypeChart = renderUsageTypeChart('forecastUsageTypeChart', D.usageType);

(function () {
  if (!document.getElementById('cumulativeChart')) return;
  const cumData = D.cumulative || [];
  if (!cumData.length) return;
  const labels  = cumData.map(d => d.week_start);
  const values  = cumData.map(d => d.cumulative);
  const ic      = cumData.map(d => d.in_contract);
  window.cumulativeChart = new BNLChart('cumulativeChart', {
    type: 'line',
    data: { labels, datasets: [{
      label: 'Cumulative credits used', data: values,
      borderColor: '#198754', backgroundColor: 'rgba(25,135,84,0.08)',
      fill: true, tension: 0.1, pointRadius: 2, pointHoverRadius: 5,
      pointBackgroundColor: ic.map(v => v ? '#198754' : '#6c757d'),
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `  Cumulative: ${Math.round(ctx.raw).toLocaleString()} credits` } },
      },
      scales: {
        y: { beginAtZero: true, ticks: { callback: v => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1000 ? (v/1000).toFixed(0)+'k' : v, font: { size: 10 } }, grid: { color: 'rgba(0,0,0,.05)' } },
        x: { ticks: { maxRotation: 40, maxTicksLimit: 16, font: { size: 10 } }, grid: { display: false } },
      },
    },
  }, { exportName: 'Cumulative Credit Burn' });
})();

/* ===================================================================== *
 * Page-level handlers (data window, inline rename)
 * ===================================================================== */
(function () {
  const y = sessionStorage.getItem('forecast-scroll');
  if (y !== null) { sessionStorage.removeItem('forecast-scroll'); window.scrollTo(0, parseInt(y, 10)); }
})();

function applyDataWindow() {
  const from = document.getElementById('data-from').value;
  const to   = document.getElementById('data-to').value;
  const url  = new URL(window.location.href);
  if (from) url.searchParams.set('data_from', from);
  else      url.searchParams.delete('data_from');
  if (to)   url.searchParams.set('data_to', to);
  else      url.searchParams.delete('data_to');
  window.location.href = url.toString();
}

// Inline Snapshot Label Rename
function startRename(cell) {
  const display = cell.querySelector('.label-display');
  const input   = cell.querySelector('.label-edit');
  if (!display || !input) return;
  display.closest('.d-flex').style.display = 'none';
  input.style.display = '';
  input.focus(); input.select();
}

document.querySelectorAll('.snapshot-label-cell').forEach(cell => {
  const display = cell.querySelector('.label-display');
  const input   = cell.querySelector('.label-edit');
  if (!display || !input) return;

  function commitRename() {
    const newLabel = input.value.trim();
    const oldLabel = cell.dataset.oldLabel;
    const snapTs   = cell.dataset.ts;
    const snapDate = cell.dataset.date;
    if (newLabel === oldLabel) { cancelRename(); return; }
    fetch(D.urls.rename, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ snapshot_ts: snapTs, snapshot_date: snapDate, old_label: oldLabel, new_label: newLabel }),
    }).then(r => {
      if (r.status === 204) {
        display.textContent = newLabel || 'No label';
        display.title = newLabel;
        if (newLabel) display.classList.remove('label-empty'); else display.classList.add('label-empty');
        cell.dataset.oldLabel = newLabel;
        input.value = newLabel;
        const row = cell.closest('tr');
        if (row) { try { const snap = JSON.parse(row.dataset.snap || '{}'); snap.label = newLabel; row.dataset.snap = JSON.stringify(snap); } catch (_) {} }
      } else { r.text().then(msg => alert('Rename failed: ' + (msg || r.status))); }
      cancelRename();
    }).catch(() => { alert('Rename request failed.'); cancelRename(); });
  }

  function cancelRename() {
    const displayRow = display.closest('.d-flex');
    input.style.display = 'none';
    if (displayRow) displayRow.style.display = '';
    input.value = cell.dataset.oldLabel;
    display.textContent = cell.dataset.oldLabel || 'No label';
    if (cell.dataset.oldLabel) display.classList.remove('label-empty'); else display.classList.add('label-empty');
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); commitRename(); }
    if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
  });
  input.addEventListener('blur', commitRename);
});
