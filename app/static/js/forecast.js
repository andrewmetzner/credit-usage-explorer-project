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
window.forecastDensity = localStorage.getItem('forecast-density') || 'clean';
if (localStorage.getItem('forecast-ui-version') !== 'simple-v6') {
  localStorage.setItem('forecast-ui-version', 'simple-v6');
  localStorage.setItem('forecast-density', 'clean');
  localStorage.removeItem('forecast-legend-open');
  localStorage.setItem('forecast-mc-p90', '0');
  localStorage.setItem('forecast-mc-p50', '1');
  localStorage.setItem('forecast-mc-p10', '0');
  window.forecastDensity = 'clean';
}

function fmtN(v) { const n = parseFloat(v); return isNaN(n) ? '—' : Math.round(n).toLocaleString(); }
function fmtPct(v) { const n = parseFloat(v); return isNaN(n) ? '—' : Math.round(n * 100) + '%'; }
function fmtR2(v) { const n = parseFloat(v); return isNaN(n) ? '—' : n.toFixed(2); }
function fmtSignedN(v) { const n = parseFloat(v); return isNaN(n) ? '—' : (n >= 0 ? '+' : '') + Math.round(n).toLocaleString(); }
function fmtSignedPerWeek(v) { const s = fmtSignedN(v); return s === '—' ? s : s + '/wk'; }
function truncLabel(s, max) { max = max || 12; return s && s.length > max ? s.slice(0, max) + '…' : (s || ''); }

function snapshotWeekDate(h) {
  const candidates = [h && h.label, h && h.snapshot_date, h && h.snapshot_ts].filter(Boolean).map(String);
  for (const c of candidates) {
    const m = c.match(/(20\d{2}-\d{2}-\d{2})/);
    if (m) return m[1];
  }
  return (h && h.snapshot_date) ? String(h.snapshot_date).slice(0, 10) : 'Snapshot';
}
function snapshotDisplayName(h, prefix) {
  return snapshotWeekDate(h);
}
function snapshotMenuName(h) {
  return 'Week of: ' + snapshotWeekDate(h);
}
function trendArrow(direction) { return direction === 'increasing' ? '↑' : direction === 'decreasing' ? '↓' : '→'; }
function trendClass(direction) { return direction === 'increasing' ? 'text-danger' : direction === 'decreasing' ? 'text-success' : 'text-muted'; }
function endBalanceClass(v) { const n = parseFloat(v); return isNaN(n) ? '' : n <= 0 ? 'text-danger' : n <= 50000 ? 'text-warning' : 'text-success'; }
function modelDataUrl(params) { return (D.urls.modelData || '/forecast/model-data') + '?' + params.toString(); }
function hasStat(v) { return v !== undefined && v !== null && String(v).trim() !== '' && !['nan','none','null'].includes(String(v).trim().toLowerCase()); }
function hasSnapshotMl(h) { return hasStat(h.ml_slope_per_week) || hasStat(h.ml_r_squared) || hasStat(h.ml_p50_end_balance); }

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

function interpSeriesData(pts, allLabels, dateKey, valKey) {
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
  const detailedSnapshots = window.forecastDensity === 'full';

  snaps.forEach((h, i) => {
    maxRemaining = Math.max(maxRemaining, parseFloat(h.credits_remaining || 0));
    const color     = h.color || SNAP_COLORS[i % SNAP_COLORS.length];
    const snapLabel = snapshotDisplayName(h, false);
    const series    = seriesCache.get(h.snapshot_ts);
    const fb        = series && series.forecast_burndown && series.forecast_burndown.length
      ? series.forecast_burndown : null;

    if (fb) {
      const forecastData = interpSeriesData(fb, allLabels, 'date', 'remaining');
      const startIdx = forecastData.findIndex(v => v !== null);
      bc.data.datasets.push({
        label: snapLabel,
        data: forecastData,
        borderColor: color, borderDash: [5, 4], borderWidth: 2,
        backgroundColor: 'transparent', fill: false, tension: 0.05,
        pointRadius: forecastData.map((v, j) => j === startIdx ? 7 : 0),
        pointBackgroundColor: color,
        spanGaps: false, _snapOverlay: true,
      });

      const mc = series && series.mc;
      if (detailedSnapshots && window.showSnapMc && mc && mc.p50 && mc.p50.length) {
        const p10d = interpSeriesData(mc.p10 || [], allLabels, 'date', 'value');
        const p50d = interpSeriesData(mc.p50,       allLabels, 'date', 'value');
        const p90d = interpSeriesData(mc.p90 || [], allLabels, 'date', 'value');
        const alpha = hexToRgba(color, 0.12);
        bc.data.datasets.push({
          label: snapLabel + ' MC P90', data: p90d,
          borderColor: hexToRgba(color, 0.45), borderWidth: 1, borderDash: [2, 3],
          backgroundColor: alpha, fill: '+2', tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true,
        });
        bc.data.datasets.push({
          label: snapLabel + ' MC P50', data: p50d,
          borderColor: hexToRgba(color, 0.7), borderWidth: 1.5, borderDash: [4, 3],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true,
        });
        bc.data.datasets.push({
          label: snapLabel + ' MC P10', data: p10d,
          borderColor: hexToRgba(color, 0.45), borderWidth: 1, borderDash: [2, 3],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true,
        });
      }

      const ml = series && series.ml;
      if (detailedSnapshots && window.showSnapMl && ml && ml.p50 && ml.p50.length) {
        const m10 = interpSeriesData(ml.p10 || [], allLabels, 'date', 'value');
        const m50 = interpSeriesData(ml.p50,       allLabels, 'date', 'value');
        const m90 = interpSeriesData(ml.p90 || [], allLabels, 'date', 'value');
        bc.data.datasets.push({
          label: snapLabel + ' ML P90', data: m90,
          borderColor: hexToRgba(color, 0.4), borderWidth: 1, borderDash: [1, 3],
          backgroundColor: hexToRgba(color, 0.08), fill: '+2', tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true,
        });
        bc.data.datasets.push({
          label: snapLabel + ' ML trend', data: m50,
          borderColor: hexToRgba(color, 0.75), borderWidth: 1.5, borderDash: [1, 2],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true,
        });
        bc.data.datasets.push({
          label: snapLabel + ' ML P10', data: m10,
          borderColor: hexToRgba(color, 0.4), borderWidth: 1, borderDash: [1, 3],
          backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0, spanGaps: false, _snapOverlay: true,
        });
      }
    } else {
      const data     = allLabels.map(l => snapRemainingAt(h, l));
      const firstIdx = data.findIndex(v => v !== null);
      bc.data.datasets.push({
        label: snapLabel, data,
        borderColor: color, borderDash: [4, 3], borderWidth: 2,
        backgroundColor: 'transparent', fill: false, tension: 0.05,
        pointRadius:          data.map((v, j) => j === firstIdx ? 7 : 0),
        pointBackgroundColor: data.map((v, j) => j === firstIdx ? color : 'transparent'),
        spanGaps: false, _snapOverlay: true,
      });
    }
  });

  updateForecastChart(bc, 'none');
  if (typeof syncForecastDensityButtons === 'function') syncForecastDensityButtons();
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
  const labels   = snaps.map(h => snapshotDisplayName(h, false));
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
        ${hasSnapshotMl(h) ? `<div style="margin-top:.45rem;padding-top:.45rem;border-top:1px solid #eee;">
          <div style="font-size:.65rem;font-weight:700;color:#8a92a0;letter-spacing:.06em;margin-bottom:.25rem;">ML TREND</div>
          <div class="d-flex justify-content-between"><span class="text-muted">Slope</span><strong class="${trendClass(h.ml_trend_direction)}">${trendArrow(h.ml_trend_direction)} ${fmtSignedPerWeek(h.ml_slope_per_week)}</strong></div>
          <div class="d-flex justify-content-between mt-1"><span class="text-muted">R²</span><strong>${fmtR2(h.ml_r_squared)}</strong></div>
          <div class="d-flex justify-content-between mt-1"><span class="text-muted">ML P50 end</span><strong class="${endBalanceClass(h.ml_p50_end_balance)}">${fmtN(h.ml_p50_end_balance)}</strong></div>
        </div>` : ''}
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

window.showSnapMc = false;
function toggleSnapMc(on) {
  window.showSnapMc = !!on;
  if (window.showSnapMc && window.forecastDensity !== 'full') {
    window.forecastDensity = 'full';
    localStorage.setItem('forecast-density', 'full');
    syncForecastDensityButtons();
  }
  [...selectedSnaps.values()].forEach(h => fetchSnapSeries(h));
  updateBurndownOverlays();
  if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
}

window.showSnapMl = false;
function toggleSnapMl(on) {
  window.showSnapMl = !!on;
  if (window.showSnapMl && window.forecastDensity !== 'full') {
    window.forecastDensity = 'full';
    localStorage.setItem('forecast-density', 'full');
    syncForecastDensityButtons();
  }
  [...selectedSnaps.values()].forEach(h => fetchSnapSeries(h));
  updateBurndownOverlays();
  if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
}

function syncForecastDensityButtons() {
  const clean = document.getElementById('density-clean');
  const full = document.getElementById('density-full');
  if (clean) {
    clean.classList.toggle('active', window.forecastDensity !== 'full');
    clean.setAttribute('aria-pressed', window.forecastDensity !== 'full' ? 'true' : 'false');
  }
  if (full) {
    full.classList.toggle('active', window.forecastDensity === 'full');
    full.setAttribute('aria-pressed', window.forecastDensity === 'full' ? 'true' : 'false');
  }
}

window.setForecastDensity = function(mode) {
  window.forecastDensity = mode === 'full' ? 'full' : 'clean';
  localStorage.setItem('forecast-density', window.forecastDensity);
  if (window.forecastDensity !== 'full') {
    window.showSnapMc = false;
    window.showSnapMl = false;
    const smc = document.getElementById('snap-mc-toggle');
    const sml = document.getElementById('snap-ml-toggle');
    if (smc) smc.checked = false;
    if (sml) sml.checked = false;
  }
  syncForecastDensityButtons();
  updateBurndownOverlays();
  if (typeof window.refreshLrOverlay === 'function') window.refreshLrOverlay();
  if (typeof window.refreshMcBands === 'function') window.refreshMcBands();
  if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
};


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
      const text = snapshotDisplayName(h, false);
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

// Restore snapshot selections active before an exclude-partial page reload
(function () {
  const saved = sessionStorage.getItem('forecast-snap-keys');
  if (!saved) return;
  sessionStorage.removeItem('forecast-snap-keys');
  let keys;
  try { keys = JSON.parse(saved); } catch (_) { return; }
  if (!keys.length) return;
  document.querySelectorAll('.history-row').forEach(tr => {
    try {
      const snap = JSON.parse(tr.dataset.snap);
      if (keys.includes(snapKey(snap))) toggleRow(tr);
    } catch (_) {}
  });
  // Also restore from data map when no DOM rows
  keys.forEach(key => {
    if (!selectedSnaps.has(key) && SNAP_DATA_MAP[key]) {
      selectedSnaps.set(key, SNAP_DATA_MAP[key]);
      fetchSnapSeries(SNAP_DATA_MAP[key]);
    }
  });
  if ([...selectedSnaps.keys()].some(k => keys.includes(k))) renderComparePanel();
})();

/* ===================================================================== *
 * Chart color helpers
 * ===================================================================== */
const CHART_COLOR_DEFAULTS = { actual: '#0d6efd', proj: '#dc3545', weekly: '#0d6efd', mc: '#fd7e14', ml: '#198754' };
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
    const actualCol = getChartColor('actual');
    const projCol = getChartColor('proj');
    const mcCol = getChartColor('mc');
    const mlCol = getChartColor('ml');
    bc.data.datasets.forEach(ds => {
      if (!ds._snapOverlay) {
        if (ds.label === 'Actual remaining') {
          ds.borderColor = actualCol;
          ds.backgroundColor = hexToRgba(actualCol, 0.07);
        } else if (ds.label === 'Projected remaining') {
          ds.borderColor = projCol;
        } else if (ds._mcOverlay) {
          const isMid = String(ds.label || '').toLowerCase().includes('p50');
          ds.borderColor = isMid ? mcCol : hexToRgba(mcCol, 0.55);
          if (ds.fill) ds.backgroundColor = hexToRgba(mcCol, 0.16);
        } else if (ds._lrOverlay) {
          const isTrend = String(ds.label || '').toLowerCase().includes('trend') || String(ds.label || '').toLowerCase().includes('linear');
          ds.borderColor = isTrend ? mlCol : hexToRgba(mlCol, 0.42);
          if (ds.fill) ds.backgroundColor = hexToRgba(mlCol, 0.10);
        }
      }
    });
    updateForecastChart(bc, 'none');
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
  if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
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


function unwrapChart(c) {
  return c && c.chart ? c.chart : c;
}
function forceHideNativeLegend(c) {
  const targets = [];
  if (c) targets.push(c);
  if (c && c.chart && c.chart !== c) targets.push(c.chart);
  targets.forEach(ch => {
    if (!ch || !ch.options) return;
    ch.options.plugins = ch.options.plugins || {};
    ch.options.plugins.legend = ch.options.plugins.legend || {};
    ch.options.plugins.legend.display = false;
    ch.options.plugins.legend.labels = ch.options.plugins.legend.labels || {};
    ch.options.plugins.legend.labels.generateLabels = function () { return []; };
    if (ch.legend && ch.legend.options) ch.legend.options.display = false;
  });
}
function updateForecastChart(c, mode) {
  if (!c) return;
  forceHideNativeLegend(c);
  const ch = unwrapChart(c);
  if (ch && ch.options) {
    ch.options.plugins = ch.options.plugins || {};
    ch.options.plugins.legend = ch.options.plugins.legend || {};
    ch.options.plugins.legend.display = false;
    ch.options.plugins.legend.labels = ch.options.plugins.legend.labels || {};
    ch.options.plugins.legend.labels.generateLabels = function () { return []; };
  }
  if (ch && typeof ch.update === 'function') ch.update(mode || 'default');
  else if (typeof c.update === 'function') c.update(mode || 'default');
  forceHideNativeLegend(c);
  if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
}

/* ===================================================================== *
 * Compact burndown legend and chart-density helpers
 * ===================================================================== */
function firstColor(value, fallback) {
  if (Array.isArray(value)) return firstColor(value.find(Boolean), fallback);
  return value || fallback || '#64748b';
}
function safeSwatchColor(ds) {
  return firstColor(ds.borderColor, firstColor(ds.backgroundColor, '#64748b'));
}
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function compactDatasetLabel(label) {
  const txt = String(label || 'Series');
  return txt.length > 34 ? txt.slice(0, 31) + '...' : txt;
}

function conciseBurndownLabel(label) {
  const txt = String(label || 'Series');
  if (txt === 'Actual remaining') return 'Actual';
  if (txt === 'Projected remaining') return 'Base forecast';
  if (txt === 'Linear Trend (ML)') return 'ML trend';
  if (txt.startsWith('MC P50')) return 'MC median';
  if (txt.startsWith('MC P90')) return 'MC optimistic';
  if (txt.startsWith('MC P10')) return 'MC pessimistic';
  const m = txt.match(/(20\d{2}-\d{2}-\d{2})/);
  if (m && /forecast/i.test(txt)) return m[1] + ' snapshot';
  if (m && /MC P50/i.test(txt)) return m[1] + ' MC median';
  if (m && /ML trend/i.test(txt)) return m[1] + ' ML';
  return compactDatasetLabel(txt);
}
function isKeyBurndownTooltipItem(item) {
  const ds = item && item.dataset ? item.dataset : {};
  const label = String(ds.label || '');
  const raw = item.raw;
  if (raw === null || raw === undefined || Number.isNaN(Number(raw))) return false;
  // Keep hover callouts readable: show key lines only. MC/ML bands still draw on the chart.
  if (/P90|P10/i.test(label)) return false;
  if (ds._snapOverlay && !/forecast/i.test(label) && !/20\d{2}-\d{2}-\d{2}$/.test(label)) return false;
  if (ds._snapOverlay && (/MC|ML/i.test(label))) return false;
  return true;
}
function datasetLegendGroup(ds) {
  if (ds._whatif) return 'Scenario';
  if (ds._snapOverlay) return 'Snapshot overlays';
  if (ds._mcOverlay) return 'Monte Carlo';
  if (ds._lrOverlay) return 'ML trend';
  return 'Core';
}
function makeSummaryChip(text, detail) {
  return `<span class="fc-summary-chip">${escapeHtml(text)}${detail ? ` <strong>${escapeHtml(detail)}</strong>` : ''}</span>`;
}

window.toggleChartLegend = function() {
  const panel = document.getElementById('burndownLegendPanel');
  if (!panel) return;
  panel.classList.toggle('is-open');
  localStorage.setItem('forecast-legend-open', panel.classList.contains('is-open') ? '1' : '0');
  refreshBurndownLegend();
};

window.toggleChartSettings = function() {
  const panel = document.getElementById('chart-settings-panel');
  if (!panel) return;
  panel.classList.toggle('is-open');
  localStorage.setItem('forecast-style-open', panel.classList.contains('is-open') ? '1' : '0');
  if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
};

window.toggleLegendDataset = function(idx) {
  const bc = window.burndownChart;
  if (!bc || !bc.data || !bc.data.datasets[idx]) return;
  const ds = bc.data.datasets[idx];
  ds.hidden = !ds.hidden;
  updateForecastChart(bc, 'none');
};

function refreshBurndownLegend() {
  const bc = window.burndownChart;
  const panel = document.getElementById('burndownLegendPanel');
  const summary = document.getElementById('burndownOverlaySummary');
  if (bc) forceHideNativeLegend(bc);

  const selected = typeof selectedSnaps !== 'undefined' ? [...selectedSnaps.values()] : [];
  const selectedNames = selected.map(h => snapshotDisplayName(h, false));
  if (summary) summary.innerHTML = '';
  if (!panel) return;

  const detOn = document.getElementById('model-det')?.checked;
  const mcOn = document.getElementById('model-mc')?.checked;
  const lrOn = document.getElementById('model-lr')?.checked;
  const chipClass = on => 'fc-summary-chip' + (on ? '' : ' is-muted');
  const mcText = mcOn ? 'MC risk' : 'MC risk off';
  const lrText = lrOn ? 'ML trend' : 'ML trend off';
  const snapChips = selectedNames.length
    ? selected.map((h, i) => {
        const color = h.color || SNAP_COLORS[i % SNAP_COLORS.length];
        const text = snapshotDisplayName(h, false);
        return `<span class="fc-summary-chip"><span class="fc-legend-swatch" style="color:${escapeHtml(color)}"></span>${escapeHtml(text)}</span>`;
      }).join('')
    : '<span class="fc-legend-note">No snapshots selected.</span>';

  panel.innerHTML = `
    <span class="fc-control-label">Legend</span>
    <span class="fc-summary-chip"><span class="fc-legend-swatch" style="color:${escapeHtml(getChartColor('actual'))}"></span>Actual</span>
    <button type="button" class="${chipClass(detOn)}" onclick="document.getElementById('model-det').click()"><span class="fc-legend-swatch" style="color:${escapeHtml(getChartColor('proj'))}"></span>Base</button>
    <button type="button" class="${chipClass(mcOn)}" onclick="document.getElementById('model-mc').click()"><span class="fc-legend-swatch" style="color:${escapeHtml(getChartColor('mc'))}"></span>${mcText}</button>
    <button type="button" class="${chipClass(lrOn)}" onclick="document.getElementById('model-lr').click()"><span class="fc-legend-swatch" style="color:${escapeHtml(getChartColor('ml'))}"></span>${lrText}</button>
    <span class="fc-separator"></span>
    <span class="fc-legend-note">Snapshot weeks:</span>
    ${snapChips}
  `;
}
(function restoreForecastPanels() {
  const stylePanel = document.getElementById('chart-settings-panel');
  if (stylePanel && localStorage.getItem('forecast-style-open') === '1') stylePanel.classList.add('is-open');
  syncForecastDensityButtons();
})();

/* ===================================================================== *
 * Burndown chart
 * ===================================================================== */
(function () {
  if (!document.getElementById('burndownChart')) return;
  const rawData    = D.weeklyChartData || [];
  const purchased  = D.purchased;
  const remaining  = D.remaining;
  const weeklyBurn = D.weeklyBurn;
  const weeksLeft  = D.weeksLeft;
  const latestDate = D.latestDate;

  const inContractRaw = rawData.filter(w => w.in_contract).sort((a,b) => a.week_start < b.week_start ? -1 : 1);
  let r = purchased;
  const actualPts = inContractRaw.map(w => {
    r = Math.max(r - w.total_credits_used, 0);
    const d = new Date((w.week_end || w.week_start) + 'T12:00:00');
    d.setDate(d.getDate() + 1);
    return [d.toISOString().slice(0, 10), r];
  });
  actualPts.push([latestDate, remaining]);

  function buildProjPts(granularity) {
    const pts = [[latestDate, remaining]];
    const base = new Date(latestDate);
    if (granularity === 'daily') {
      const totalDays = Math.min(Math.ceil(weeksLeft * 7) + 1, 420);
      const dailyBurn = weeklyBurn / 7;
      for (let i = 1; i <= totalDays; i++) {
        const d = new Date(base);
        d.setDate(d.getDate() + i);
        const rem = Math.max(remaining - dailyBurn * i, 0);
        pts.push([d.toISOString().slice(0, 10), rem]);
      }
    } else {
      for (let i = 1; i <= Math.min(Math.ceil(weeksLeft) + 1, 60); i++) {
        const d = new Date(base);
        d.setDate(d.getDate() + i * 7);
        const rem = Math.max(remaining - weeklyBurn * i, 0);
        pts.push([d.toISOString().slice(0, 10), rem]);
      }
    }
    return pts;
  }

  let currentGranularity = localStorage.getItem('fc-gran') || 'weekly';
  let projPts = buildProjPts(currentGranularity);

  const lookup = (pts, lbl) => { const p = pts.find(x => x[0] === lbl); return p != null ? p[1] : null; };

  function buildAllLabels(ppts) {
    return [...new Set([...actualPts, ...ppts].map(p => p[0]))].sort();
  }

  let allLabels = buildAllLabels(projPts);
  window.burndownLabels = allLabels;
  window.burndownMaxY   = purchased;

  const _prevLegendDisplay = window.Chart && Chart.defaults && Chart.defaults.plugins && Chart.defaults.plugins.legend ? Chart.defaults.plugins.legend.display : undefined;
  const _prevGenerateLabels = window.Chart && Chart.defaults && Chart.defaults.plugins && Chart.defaults.plugins.legend && Chart.defaults.plugins.legend.labels ? Chart.defaults.plugins.legend.labels.generateLabels : undefined;
  if (window.Chart && Chart.defaults && Chart.defaults.plugins && Chart.defaults.plugins.legend) {
    Chart.defaults.plugins.legend.display = false;
    Chart.defaults.plugins.legend.labels = Chart.defaults.plugins.legend.labels || {};
    Chart.defaults.plugins.legend.labels.generateLabels = function () { return []; };
  }

  window.burndownChart = new BNLChart('burndownChart', {
    type: 'line',
    plugins: [{ id: 'forceNoLegend', beforeInit: chart => { chart.options.plugins.legend.display = false; }, beforeUpdate: chart => { chart.options.plugins.legend.display = false; } }],
    data: {
      labels: allLabels,
      datasets: [
        {
          label: 'Actual remaining',
          data: allLabels.map(l => lookup(actualPts, l)),
          borderColor: getChartColor('actual'), backgroundColor: hexToRgba(getChartColor('actual'), 0.07),
          fill: true, tension: 0.1, pointRadius: 3, pointHoverRadius: 6, spanGaps: false,
        },
        {
          label: 'Projected remaining',
          data: allLabels.map(l => lookup(projPts, l)),
          borderColor: getChartColor('proj'), borderDash: [5, 4], backgroundColor: 'transparent',
          tension: 0.05, pointRadius: 2, pointHoverRadius: 5, spanGaps: false,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      // Keep hover readable even with many overlays: nearest line only, plus filtered key items.
      interaction: { mode: 'nearest', intersect: false, axis: 'xy' },
      plugins: {
        legend: { display: false, labels: { generateLabels: function () { return []; } } },
        tooltip: {
          mode: 'nearest', intersect: false, position: 'nearest',
          filter: isKeyBurndownTooltipItem,
          itemSort: (a, b) => (b.raw ?? -Infinity) - (a.raw ?? -Infinity),
          callbacks: {
            label: ctx => `  ${conciseBurndownLabel(ctx.dataset.label)}: ${Math.round(ctx.raw ?? 0).toLocaleString()} credits`,
            footer: items => {
              const selected = (typeof selectedSnaps !== 'undefined' && selectedSnaps.size) ? selectedSnaps.size : 0;
              return selected > 1 ? 'Bands are drawn but hidden from hover to keep this readable.' : '';
            },
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
          beginAtZero: true, suggestedMax: purchased,
          ticks: { callback: v => v >= 1000000 ? (v/1000000).toFixed(1)+'M' : v >= 1000 ? (v/1000).toFixed(0)+'k' : v, font: { size: 10 } },
          grid: { color: 'rgba(0,0,0,.05)' },
        },
        x: { ticks: { maxRotation: 40, maxTicksLimit: 14, font: { size: 10 } }, grid: { display: false } },
      },
    },
  }, { exportName: 'Credit Burndown' });
  updateForecastChart(window.burndownChart, 'none');
  if (window.Chart && Chart.defaults && Chart.defaults.plugins && Chart.defaults.plugins.legend) {
    Chart.defaults.plugins.legend.display = _prevLegendDisplay;
    if (_prevGenerateLabels) Chart.defaults.plugins.legend.labels.generateLabels = _prevGenerateLabels;
  }

  window.setBurndownGranularity = function(gran) {
    if (gran === currentGranularity) return;
    currentGranularity = gran;
    localStorage.setItem('fc-gran', gran);
    projPts = buildProjPts(gran);
    allLabels = buildAllLabels(projPts);
    window.burndownLabels = allLabels;
    const bc = window.burndownChart;
    if (!bc) return;
    bc.data.labels = allLabels;
    bc.data.datasets.forEach(ds => {
      if (ds._mcOverlay || ds._snapOverlay) return;
      if (ds.label === 'Actual remaining')    ds.data = allLabels.map(l => lookup(actualPts, l));
      if (ds.label === 'Projected remaining') ds.data = allLabels.map(l => lookup(projPts, l));
    });
    updateForecastChart(bc, 'none');
    if (typeof updateBurndownOverlays === 'function') updateBurndownOverlays();
    if (typeof window.refreshMcBands    === 'function') window.refreshMcBands();
    if (typeof window.refreshLrOverlay  === 'function') window.refreshLrOverlay();
    if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
    document.getElementById('gran-weekly').classList.toggle('active', gran === 'weekly');
    document.getElementById('gran-daily').classList.toggle('active', gran === 'daily');
  };

  if (currentGranularity === 'daily') {
    document.getElementById('gran-weekly').classList.remove('active');
    document.getElementById('gran-daily').classList.add('active');
  }
  if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
})();

/* ===================================================================== *
 * Prediction model overlays (Monte Carlo + Linear Regression)
 * ===================================================================== */
(function () {
  if (!document.getElementById('burndownChart')) return;
  const MC_RUNS = D.mcRuns || 10000;
  let mcCache  = null;
  let mcLoading = null;
  let detDataset = null;
  let lrCache = null;
  let lrLoading = null;

  function isChecked(id) {
    const el = document.getElementById(id);
    return !!(el && el.checked);
  }

  function updateChart(bc, mode) {
    updateForecastChart(bc, mode || 'default');
  }

  function fetchModelData(params) {
    return fetch(modelDataUrl(params)).then(async r => {
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      if (!r.ok || (data && data.error)) {
        throw new Error((data && data.error) || ('HTTP ' + r.status));
      }
      return data || {};
    });
  }

  window.toggleForecastModel = function (modelId, enabled) {
    localStorage.setItem('forecast-model-' + modelId, enabled ? '1' : '0');
    if (modelId === 'deterministic') {
      const bc = window.burndownChart;
      if (!bc) return;
      const idx = bc.data.datasets.findIndex(d => d.label === 'Projected remaining' && !d._mcOverlay && !d._lrOverlay && !d._snapOverlay);
      if (enabled) {
        if (detDataset && idx < 0) {
          const actualIdx = bc.data.datasets.findIndex(d => d.label === 'Actual remaining');
          bc.data.datasets.splice(Math.max(actualIdx + 1, 1), 0, detDataset);
          detDataset = null;
        }
      } else if (idx >= 0) {
        detDataset = bc.data.datasets.splice(idx, 1)[0] || null;
      }
      updateChart(bc);
    } else if (modelId === 'monte_carlo') {
      if (enabled) loadMcOverlay(); else removeMcOverlay();
    } else if (modelId === 'linear_regression') {
      if (enabled) loadLrOverlay(); else removeLrOverlay();
    }
  };

  // ---- Linear Trend (ML) overlay + statistics ----
  function getLrData() {
    if (lrCache) return Promise.resolve(lrCache);
    if (!lrLoading) {
      const params = new URLSearchParams(window.location.search);
      params.set('model', 'linear_regression');
      lrLoading = fetchModelData(params)
        .then(data => { lrCache = data; lrLoading = null; return data; })
        .catch(e => { lrLoading = null; throw e; });
    }
    return lrLoading;
  }

  function interpPts(pts, allLabels) {
    if (!pts || !pts.length) return allLabels.map(() => null);
    const start = pts[0].date, end = pts[pts.length - 1].date;
    return allLabels.map(l => {
      if (l < start || l > end) return null;
      const idx = pts.findIndex(p => p.date > l);
      if (idx === -1) return pts[pts.length - 1].value;
      if (idx === 0)  return pts[0].value;
      const a = pts[idx - 1], b = pts[idx];
      const denom = new Date(b.date) - new Date(a.date);
      const t = denom ? (new Date(l) - new Date(a.date)) / denom : 0;
      return a.value + t * (b.value - a.value);
    });
  }

  function endVal(arr) {
    return arr && arr.length ? arr[arr.length - 1].value : null;
  }

  function applyLrToChart(data) {
    const bc = window.burndownChart;
    if (!bc) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._lrOverlay);
    const allLabels = bc.data.labels;
    const C = getChartColor('ml');
    const p50 = interpPts(data.burndown, allLabels);
    const hasBand = data.p10 && data.p90 && data.p10.length && data.p90.length;
    const showBands = hasBand && window.forecastDensity === 'full';
    if (showBands) {
      bc.data.datasets.push({
        label: 'ML P90 (optimistic)', data: interpPts(data.p90, allLabels),
        borderColor: hexToRgba(C, 0.4), borderWidth: 1, borderDash: [2, 3],
        backgroundColor: hexToRgba(C, 0.10), fill: '+1', tension: 0.1, pointRadius: 0,
        spanGaps: false, _lrOverlay: true,
      });
      bc.data.datasets.push({
        label: 'ML P10 (pessimistic)', data: interpPts(data.p10, allLabels),
        borderColor: hexToRgba(C, 0.4), borderWidth: 1, borderDash: [2, 3],
        backgroundColor: 'transparent', fill: false, tension: 0.1, pointRadius: 0,
        spanGaps: false, _lrOverlay: true,
      });
    }
    bc.data.datasets.push({
      label: 'Linear Trend (ML)', data: p50,
      borderColor: C, borderWidth: 2.5, borderDash: [6, 3],
      backgroundColor: 'transparent', fill: false, tension: 0.1,
      pointRadius: 0, pointHoverRadius: 4, spanGaps: false, _lrOverlay: true,
    });
    updateChart(bc);
  }

  function updateLrStats(data) {
    const m = data.metadata || {};
    const slope = m.slope_credits_per_week;
    const direction = m.trend_direction || (parseFloat(slope) > 0 ? 'increasing' : parseFloat(slope) < 0 ? 'decreasing' : 'flat');
    const obs = m.observations_used || 0;
    const p10End = hasStat(m.p10_end_balance) ? m.p10_end_balance : endVal(data.p10);
    const p50End = hasStat(m.p50_end_balance) ? m.p50_end_balance : endVal(data.burndown);
    const p90End = hasStat(m.p90_end_balance) ? m.p90_end_balance : endVal(data.p90);
    const quality = (m.model_quality || 'unknown').replace(/_/g, ' ');
    const engine = (m.model_engine || 'unknown').replace(/_/g, ' ');
    const statusText = m.insufficient_data
      ? `${obs} obs · flat fallback`
      : `${trendArrow(direction)} ${fmtSignedPerWeek(slope)} · R² ${fmtR2(m.r_squared)}`;

    const status = document.getElementById('lr-status');
    if (status) status.textContent = statusText;

    const slopeEl = document.getElementById('ml-kpi-slope');
    const subEl = document.getElementById('ml-kpi-sub');
    if (slopeEl) {
      slopeEl.innerHTML = `<span class="${trendClass(direction)}">${trendArrow(direction)} ${fmtSignedPerWeek(slope)}</span>`;
    }
    if (subEl) subEl.textContent = `R² ${fmtR2(m.r_squared)} · ${obs} weeks`;

    const badge = document.getElementById('ml-acc-badge');
    if (badge) {
      const bg = direction === 'increasing' ? 'danger' : direction === 'decreasing' ? 'success' : 'secondary';
      badge.textContent = `${direction} · R² ${fmtR2(m.r_squared)}`;
      badge.className = `ms-2 badge bg-${bg}`;
      badge.style.display = '';
    }

    const body = document.getElementById('ml-acc-body');
    if (body) {
      const exhausted = m.projected_exhaustion ? 'Yes' : 'No';
      const exhaustionDate = m.projected_exhaustion_date || '—';
      const fallbackNote = m.insufficient_data
        ? '<p class="mb-0 small text-warning">Not enough weekly observations for a fitted trend, so the ML model is using a flat fallback projection.</p>'
        : '';
      body.innerHTML = `
        <div class="row g-3">
          <div class="col-md-5">
            <p class="text-muted small mb-1 fw-semibold">Trend Fit</p>
            <table class="table table-sm mb-2">
              <tr><td>Trend slope</td><td class="text-end fw-semibold ${trendClass(direction)}">${trendArrow(direction)} ${fmtSignedPerWeek(slope)}</td></tr>
              <tr><td>R²</td><td class="text-end">${fmtR2(m.r_squared)}</td></tr>
              <tr><td>Observations</td><td class="text-end">${obs}</td></tr>
              <tr><td>Model quality</td><td class="text-end text-capitalize">${quality}</td></tr>
              <tr><td>Engine</td><td class="text-end text-capitalize">${engine}</td></tr>
            </table>
            ${fallbackNote}
          </div>
          <div class="col-md-7">
            <p class="text-muted small mb-1 fw-semibold">ML Projection</p>
            <table class="table table-sm mb-0">
              <thead><tr><th>Metric</th><th class="text-end">Value</th><th class="text-muted text-end" style="font-size:.7rem;">Notes</th></tr></thead>
              <tbody>
                <tr><td>Next-week burn</td><td class="text-end">${fmtN(m.next_week_predicted_burn)}</td><td class="text-muted text-end small">trend estimate</td></tr>
                <tr><td>P10 end balance</td><td class="text-end ${endBalanceClass(p10End)}">${fmtN(p10End)}</td><td class="text-muted text-end small">pessimistic</td></tr>
                <tr><td>P50 end balance</td><td class="text-end ${endBalanceClass(p50End)}">${fmtN(p50End)}</td><td class="text-muted text-end small">median</td></tr>
                <tr><td>P90 end balance</td><td class="text-end ${endBalanceClass(p90End)}">${fmtN(p90End)}</td><td class="text-muted text-end small">optimistic</td></tr>
                <tr><td>Projected exhaustion</td><td class="text-end ${m.projected_exhaustion ? 'text-danger' : 'text-success'}">${exhausted}</td><td class="text-muted text-end small">${exhaustionDate}</td></tr>
                <tr><td>RMSE</td><td class="text-end">${fmtN(m.rmse)}</td><td class="text-muted text-end small">fit error</td></tr>
                <tr><td>MAE</td><td class="text-end">${fmtN(m.mae)}</td><td class="text-muted text-end small">fit error</td></tr>
              </tbody>
            </table>
          </div>
        </div>`;
    }
  }

  function markLrUnavailable(msg) {
    const slopeEl = document.getElementById('ml-kpi-slope');
    const body = document.getElementById('ml-acc-body');
    const status = document.getElementById('lr-status');
    if (slopeEl) slopeEl.textContent = '—';
    if (status) status.textContent = msg || 'load failed';
    if (body) body.innerHTML = `<div class="text-muted small">ML model statistics unavailable${msg ? ': ' + msg : ''}.</div>`;
  }

  async function loadLrOverlay() {
    const status = document.getElementById('lr-status');
    if (status && !lrCache) status.textContent = 'loading…';
    try {
      const data = await getLrData();
      updateLrStats(data);
      applyLrToChart(data);
    } catch (e) {
      markLrUnavailable(e.message || 'load failed');
      const cb = document.getElementById('model-lr');
      if (cb) cb.checked = false;
    }
  }

  function removeLrOverlay() {
    const bc = window.burndownChart;
    if (!bc) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._lrOverlay);
    updateChart(bc);
    const status = document.getElementById('lr-status');
    if (status && !isChecked('model-lr')) status.textContent = lrCache ? 'stats loaded' : '';
  }
  window.refreshLrOverlay = function () {
    if (lrCache && isChecked('model-lr')) applyLrToChart(lrCache);
    else {
      const bc = window.burndownChart;
      if (bc) {
        bc.data.datasets = bc.data.datasets.filter(d => !d._lrOverlay);
        updateChart(bc, 'none');
      }
    }
  };

  // ---- Monte Carlo overlay + statistics ----
  function getMcData() {
    if (mcCache) return Promise.resolve(mcCache);
    if (!mcLoading) {
      const params = new URLSearchParams(window.location.search);
      params.set('model', 'monte_carlo');
      params.set('runs', MC_RUNS);
      mcLoading = fetchModelData(params)
        .then(data => { mcCache = data; mcLoading = null; return data; })
        .catch(e => { mcLoading = null; throw e; });
    }
    return mcLoading;
  }

  async function loadMcOverlay() {
    const bc = window.burndownChart;
    if (!bc) return;
    const status = document.getElementById('mc-status');
    if (status && !mcCache) { status.textContent = 'loading…'; status.style.display = ''; }
    try {
      const data = await getMcData();
      updateMcStats(data);
      applyMcToChart(data);
      const ctrl = document.getElementById('mc-band-controls');
      if (ctrl) ctrl.style.display = 'inline-flex';
      if (status) {
        const ep = data.metadata && data.metadata.exhaustion_probability != null
          ? Math.round(data.metadata.exhaustion_probability * 100) + '% exhaustion risk  ·  '
            + (data.metadata.runs || 0).toLocaleString() + ' runs'
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
    if (isChecked('model-mc')) applyMcBands(visible ? 'show' : 'default');
    if (typeof refreshBurndownLegend === 'function') refreshBurndownLegend();
  };

  function updateMcStats(data) {
    const ep     = data.metadata && data.metadata.exhaustion_probability != null ? data.metadata.exhaustion_probability : null;
    const runs   = (data.metadata && data.metadata.runs) || 0;
    const p10End = data.p10     && data.p10.length     ? data.p10[data.p10.length - 1].value         : null;
    const p50End = data.burndown && data.burndown.length ? data.burndown[data.burndown.length - 1].value : null;
    const p90End = data.p90     && data.p90.length     ? data.p90[data.p90.length - 1].value         : null;

    const riskCls = ep === null ? '' : ep > 0.5 ? 'text-danger' : ep > 0.1 ? 'text-warning' : 'text-success';
    const balCls  = v => v !== null && v <= 0 ? 'text-danger' : v !== null && v <= 50000 ? 'text-warning' : v !== null ? 'text-success' : '';
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
              <thead><tr><th>Percentile</th><th class="text-end">End Balance</th><th class="text-muted text-end" style="font-size:.7rem;">Interpretation</th></tr></thead>
              <tbody>
                <tr><td>P10 <span class="text-muted small">(pessimistic)</span></td><td class="text-end ${balCls(p10End)}">${fmtBal(p10End)}</td><td class="text-muted text-end small">90% of runs end higher</td></tr>
                <tr><td>P50 <span class="text-muted small">(median)</span></td><td class="text-end ${balCls(p50End)}">${fmtBal(p50End)}</td><td class="text-muted text-end small">most likely outcome</td></tr>
                <tr><td>P90 <span class="text-muted small">(optimistic)</span></td><td class="text-end ${balCls(p90End)}">${fmtBal(p90End)}</td><td class="text-muted text-end small">10% of runs end higher</td></tr>
              </tbody>
            </table>
          </div>
        </div>`;
    }
  }

  function applyMcToChart(_data) {
    applyMcBands('show');
  }

  function applyMcBands(mode) {
    const bc = window.burndownChart;
    if (!bc || !mcCache || !isChecked('model-mc')) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._mcOverlay);

    const data      = mcCache;
    const allLabels = bc.data.labels;

    const p90data = interpPts(data.p90, allLabels);
    const p50data = interpPts(data.burndown, allLabels);
    const p10data = interpPts(data.p10, allLabels);

    const showP90 = localStorage.getItem('forecast-mc-p90') !== '0';
    const showP10 = localStorage.getItem('forecast-mc-p10') !== '0';
    const showP50 = localStorage.getItem('forecast-mc-p50') !== '0';

    const p90Fill = !showP10 ? false : showP50 ? '+2' : '+1';

    if (showP90) {
      bc.data.datasets.push({
        label: 'MC P90 (optimistic)',
        data: p90data,
        borderColor: hexToRgba(getChartColor('mc'), 0.55), borderWidth: 1.5, borderDash: [3, 3],
        backgroundColor: showP10 ? hexToRgba(getChartColor('mc'), 0.18) : 'transparent',
        fill: p90Fill,
        pointRadius: 0, tension: 0.1, spanGaps: false,
        _mcOverlay: true, _mcBand: 'p90',
      });
    }
    if (showP50) {
      bc.data.datasets.push({
        label: 'MC P50 (median)',
        data: p50data,
        borderColor: getChartColor('mc'), borderWidth: 2.5, borderDash: [6, 3],
        backgroundColor: 'transparent', fill: false,
        pointRadius: 0, pointHoverRadius: 4, tension: 0.1, spanGaps: false,
        _mcOverlay: true, _mcBand: 'p50',
      });
    }
    if (showP10) {
      bc.data.datasets.push({
        label: 'MC P10 (pessimistic)',
        data: p10data,
        borderColor: hexToRgba(getChartColor('mc'), 0.55), borderWidth: 1.5, borderDash: [3, 3],
        backgroundColor: 'transparent', fill: false,
        pointRadius: 0, tension: 0.1, spanGaps: false,
        _mcOverlay: true, _mcBand: 'p10',
      });
    }

    ['p90', 'p50', 'p10'].forEach(band => {
      if (localStorage.getItem('forecast-mc-' + band) === '0') {
        const cb = document.getElementById('mc-show-' + band);
        if (cb) cb.checked = false;
      }
    });

    updateChart(bc, mode || 'default');
  }

  window.refreshMcBands = function() {
    if (mcCache && isChecked('model-mc')) applyMcBands('none');
    else {
      const bc = window.burndownChart;
      if (bc) {
        bc.data.datasets = bc.data.datasets.filter(d => !d._mcOverlay);
        updateChart(bc, 'none');
      }
    }
  };

  function removeMcOverlay() {
    const bc = window.burndownChart;
    if (!bc) return;
    bc.data.datasets = bc.data.datasets.filter(d => !d._mcOverlay);
    updateChart(bc);
    const ctrl = document.getElementById('mc-band-controls');
    if (ctrl) ctrl.style.display = 'none';
    const status = document.getElementById('mc-status');
    if (status) status.style.display = 'none';
  }

  // Restore checkbox states after page reload
  const savedDet = localStorage.getItem('forecast-model-deterministic');
  const savedMc  = localStorage.getItem('forecast-model-monte_carlo');
  if (savedDet === '0') {
    const cb = document.getElementById('model-det');
    if (cb) { cb.checked = false; window.toggleForecastModel('deterministic', false); }
  }
  if (savedMc === '1') {
    const cb = document.getElementById('model-mc');
    if (cb) { cb.checked = true; window.toggleForecastModel('monte_carlo', true); }
  }
  if (localStorage.getItem('forecast-model-linear_regression') === '1') {
    const cb = document.getElementById('model-lr');
    if (cb) { cb.checked = true; window.toggleForecastModel('linear_regression', true); }
  }

  // Auto-load model statistics on page load.  These calls populate KPI and
  // accordion stats without drawing overlays unless the checkbox is enabled.
  getMcData().then(updateMcStats).catch(() => {
    const probEl = document.getElementById('mc-kpi-prob');
    if (probEl) probEl.textContent = '—';
    const body = document.getElementById('mc-acc-body');
    if (body) body.innerHTML = '<div class="text-muted small">Simulation data unavailable.</div>';
  });
  getLrData().then(updateLrStats).catch(e => markLrUnavailable(e.message || 'load failed'));
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
 * Page-level handlers (exclude-partial, data window, inline rename)
 * ===================================================================== */
(function () {
  const y = sessionStorage.getItem('forecast-scroll');
  if (y !== null) { sessionStorage.removeItem('forecast-scroll'); window.scrollTo(0, parseInt(y, 10)); }
})();

function applyExcludePartial(checked) {
  sessionStorage.setItem('forecast-scroll', window.scrollY);
  if (typeof selectedSnaps !== 'undefined' && selectedSnaps.size > 0) {
    sessionStorage.setItem('forecast-snap-keys', JSON.stringify([...selectedSnaps.keys()]));
  } else {
    sessionStorage.removeItem('forecast-snap-keys');
  }
  document.cookie = 'forecast_excl_partial=' + (checked ? '1' : '0') + '; path=/; max-age=31536000; SameSite=Lax';
  const url = new URL(window.location.href);
  url.searchParams.set('exclude_partial', checked ? '1' : '0');
  window.location.href = url.toString();
}

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
