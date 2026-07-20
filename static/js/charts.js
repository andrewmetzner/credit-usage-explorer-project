/**
 * BNLChart — Chart.js wrapper with Export PNG and Fullscreen modal.
 * Requires: Chart.js 4.x, Bootstrap 5, chartjs-plugin-zoom (for zoom/pan charts).
 */
'use strict';

// Crosshair Plugin - registered globally so all charts benefit
if (typeof Chart !== 'undefined') {
  Chart.register({
    id: 'bnl-crosshair',
    afterDraw(chart) {
      if (chart._crosshairX == null) return;
      const { ctx, chartArea: { top, bottom } } = chart;
      const dark = document.documentElement.getAttribute('data-theme') === 'dark';
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(chart._crosshairX, top);
      ctx.lineTo(chart._crosshairX, bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = dark ? 'rgba(255,255,255,.28)' : 'rgba(0,0,0,.18)';
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.restore();
    },
    afterEvent(chart, args) {
      const t = args.event.type;
      if (t === 'mousemove') { chart._crosshairX = args.event.x; args.changed = true; }
      else if (t === 'mouseleave' || t === 'mouseout') { chart._crosshairX = null; args.changed = true; }
    },
  });
}

// Theme-aware chart colors: charts read the app's CSS variables so text/grid
// match the active light/dark theme — applied when charts finish building
// (window load) and again whenever the user toggles the theme.
function bnlChartThemeColors() {
  const css = getComputedStyle(document.documentElement);
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  return {
    text: css.getPropertyValue('--text').trim() || '#2c3140',
    muted: css.getPropertyValue('--muted').trim() || '#8a92a0',
    grid: dark ? 'rgba(255,255,255,.07)' : 'rgba(0,0,0,.05)',
    surface: css.getPropertyValue('--surface').trim() || '#fff',
  };
}

function bnlApplyChartTheme() {
  if (typeof Chart === 'undefined') return;
  const t = bnlChartThemeColors();
  Chart.defaults.color = t.muted;
  Chart.defaults.borderColor = t.grid;
  document.querySelectorAll('canvas').forEach(cv => {
    const ch = Chart.getChart(cv);
    if (!ch) return;
    Object.values(ch.options.scales || {}).forEach(s => {
      if (s.ticks) s.ticks.color = t.muted;
      if (s.grid) s.grid.color = t.grid;
    });
    const legend = ch.options.plugins && ch.options.plugins.legend;
    if (legend && legend.labels) legend.labels.color = t.text;
    // Datasets whose borders should match the card background (e.g. donut
    // segment separators) — flagged with _surfaceBorder at creation.
    (ch.data.datasets || []).forEach(ds => {
      if (ds._surfaceBorder) ds.borderColor = t.surface;
    });
    ch.update('none');
  });
}

if (typeof Chart !== 'undefined') {
  const t0 = bnlChartThemeColors();
  Chart.defaults.color = t0.muted;
  Chart.defaults.borderColor = t0.grid;
  window.addEventListener('load', bnlApplyChartTheme);
  window.addEventListener('bnl-theme-change', bnlApplyChartTheme);
}

// Label pill for vertical marker lines (e.g. the forecast chart's
// credit-event markers), drawn with its top edge at `y`.
// Flips to the left side of the line when the text would clip past the
// chart's right edge, and sits on a theme-matched backing so it stays
// readable over bars/lines.
function bnlDrawMarkerLabel(ctx, text, x, y, right, color) {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  ctx.font = '10px sans-serif';
  const w = ctx.measureText(text).width;
  const pad = 3;
  const gap = 5;
  const onRight = x + gap + w + pad * 2 <= right;
  const tx = onRight ? x + gap : x - gap - w - pad * 2;
  const ty = y;
  ctx.fillStyle = dark ? 'rgba(30,33,42,.82)' : 'rgba(255,255,255,.82)';
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(tx, ty, w + pad * 2, 13, 3);
  else ctx.rect(tx, ty, w + pad * 2, 13);
  ctx.fill();
  ctx.fillStyle = color;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, tx + pad, ty + 7);
}

// Shared categorical palette for multi-series charts.
const BNL_PALETTE = [
  '#0d6efd', '#20c997', '#fd7e14', '#6f42c1', '#d63384',
  '#0dcaf0', '#198754', '#ffc107', '#dc3545', '#6c757d',
];

/**
 * Render the "credits by usage type per week" stacked bar chart.
 * @param {string} canvasId
 * @param {{weeks:string[], series:{name:string,data:number[]}[]}} data
 * @param {object} [opts]  { exportName, stacked }
 * @returns {BNLChart|null}
 */
function renderUsageTypeChart(canvasId, data, opts = {}) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  if (!data || !data.weeks || !data.weeks.length || !data.series.length) {
    const host = canvas.parentElement;
    if (host) host.innerHTML = '<div class="text-muted small p-3 text-center">No usage-type data available.</div>';
    return null;
  }
  const stacked = opts.stacked !== false;
  const datasets = data.series.map((s, i) => ({
    label: s.name,
    data: s.data,
    backgroundColor: BNL_PALETTE[i % BNL_PALETTE.length],
    borderWidth: 0,
    borderRadius: 2,
  }));
  // Optional cap lines: opts.capSeries = [{week, cap, monthly?}]. Stepped
  // dashed lines — the weekly pace on every week, plus the month's total
  // allowance on weeks that carry a `monthly` value (the monthly-cap era).
  // Own stack groups keep them out of the bar stack; _capLine keeps them out
  // of the tooltip's Total footer. pointStyle 'line' = dashed legend swatch.
  if (opts.capSeries && opts.capSeries.length) {
    const capByWeek = {};
    opts.capSeries.forEach(c => { capByWeek[c.week] = c; });
    const capLine = (label, valueOf, color) => {
      const vals = data.weeks.map(w => {
        const c = capByWeek[w];
        const v = c ? valueOf(c) : null;
        return v != null ? v : null;
      });
      return {
        type: 'line',
        label,
        data: vals,
        borderColor: color, borderWidth: 1.5, borderDash: [6, 4],
        backgroundColor: 'transparent', fill: false,
        // A cap that applies to only ONE plotted week (e.g. the monthly cap
        // right after the weekly->monthly switch) has no neighbour to draw a
        // segment to, so it would render as nothing. Give isolated points a
        // dot; runs of points draw as a line and stay dot-free.
        pointRadius: vals.map((v, i) =>
          v != null && vals[i - 1] == null && vals[i + 1] == null ? 3 : 0),
        pointBackgroundColor: color,
        pointHoverRadius: 0, stepped: 'middle',
        pointStyle: 'line',
        spanGaps: false, stack: 'bnl-cap-' + label, order: -1, _capLine: true,
      };
    };
    datasets.push(capLine(opts.capLabel || 'Weekly cap', c => c.cap, '#d63384'));
    if (opts.capSeries.some(c => c.monthly != null)) {
      const monthly = capLine(opts.monthlyCapLabel || 'Monthly cap', c => c.monthly, '#6f42c1');
      // Callers can start the monthly line hidden (e.g. only auto-show it
      // when a month actually went over its cap).
      monthly.hidden = !!opts.monthlyCapHidden;
      datasets.push(monthly);
    }
  }
  const chart = new BNLChart(canvasId, {
    type: 'bar',
    data: { labels: data.weeks, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, position: 'bottom', labels: { font: { size: 10 }, boxWidth: 12, padding: 8, usePointStyle: true } },
        tooltip: {
          callbacks: {
            title: items => 'Week of ' + items[0].label,
            label: ctx => `  ${ctx.dataset.label}: ${Math.round(ctx.raw || 0).toLocaleString()} credits`,
            footer: items => 'Total: ' + Math.round(
              items.filter(i => !i.dataset._capLine).reduce((s, i) => s + (i.raw || 0), 0)
            ).toLocaleString(),
          },
        },
      },
      scales: {
        x: { stacked, grid: { display: false }, ticks: { font: { size: 9 }, maxRotation: 45, maxTicksLimit: 24 } },
        y: {
          stacked, beginAtZero: true, grid: { color: 'rgba(0,0,0,.05)' },
          ticks: { font: { size: 10 }, callback: v => v >= 1000 ? (v / 1000).toFixed(0) + 'k' : v },
        },
      },
    },
  }, { exportName: opts.exportName || 'Credits by Usage Type per Week' });
  chart._stacked = stacked;
  return chart;
}

/** Toggle a usage-type chart between stacked and grouped bars. */
function setUsageTypeStacked(chart, stacked) {
  if (!chart || !chart.chart) return;
  chart._stacked = stacked;
  chart.chart.options.scales.x.stacked = stacked;
  chart.chart.options.scales.y.stacked = stacked;
  chart.chart.update();
}

/** Toolbar handler: switch a named window chart's bar mode and toggle button state. */
function setUsageTypeMode(varName, stacked, btn) {
  setUsageTypeStacked(window[varName], stacked);
  if (btn && btn.parentElement) {
    Array.from(btn.parentElement.querySelectorAll('button'))
      .forEach(b => b.classList.toggle('active', b === btn));
  }
}

// Export each of `groups` as its own PNG by toggling dataset visibility
// (label/flag matched per group). Downloads are staggered slightly since
// firing several `a.click()` calls in the same tick can trip a browser's
// multi-download prompt/blocker. `onDone` (if given) runs after the last
// group instead of restoring visibility — used when `chart` is a disposable
// clone rather than the chart actually on screen.
function bnlBulkExportPNG(chart, exportName, groups, onDone) {
  if (!groups || !groups.length) { if (onDone) onDone(); return; }
  const originalHidden = chart.data.datasets.map(ds => !!ds.hidden);
  // setDatasetVisibility (not just `dataset.hidden`) also updates the
  // per-dataset meta the legend reads — mutating `.hidden` alone left the
  // on-chart legend showing every series from the live/selected view instead
  // of just the one model that's actually drawn in this particular PNG.
  const finish = () => {
    if (onDone) { onDone(); return; }
    chart.data.datasets.forEach((ds, i) => {
      ds.hidden = originalHidden[i];
      chart.setDatasetVisibility(i, !originalHidden[i]);
    });
    chart.update();
  };
  const runGroup = i => {
    if (i >= groups.length) { finish(); return; }
    const group = groups[i];
    chart.data.datasets.forEach((ds, di) => {
      const visible = group.match(ds);
      ds.hidden = !visible;
      chart.setDatasetVisibility(di, visible);
    });
    chart.update();
    const a = document.createElement('a');
    a.download = (exportName + '_' + group.label).replace(/\s+/g, '_') + '.png';
    a.href = chart.toBase64Image('image/png', 1);
    a.click();
    setTimeout(() => runGroup(i + 1), 250);
  };
  runGroup(0);
}

// JSON.stringify silently drops function values, so cloning `chart.options`
// through it (the only practical way most code reaches for deep-copying a
// Chart.js options object) strips every callback: tooltip formatters, tick
// formatters, and the legend's generateLabels — which is what actually
// filters the on-chart legend down to visible/relevant series. Without it, a
// cloned chart falls back to Chart.js's default legend generator and lists
// every dataset again (crossed out, not removed). Deep-clones plain
// objects/arrays but keeps function values by reference instead of
// round-tripping through JSON, so no callback is ever lost.
function bnlDeepCloneKeepFns(v) {
  if (Array.isArray(v)) return v.map(bnlDeepCloneKeepFns);
  if (v && typeof v === 'object') {
    const out = {};
    Object.keys(v).forEach(k => { out[k] = bnlDeepCloneKeepFns(v[k]); });
    return out;
  }
  return v; // primitives and functions both pass through as-is
}

// Build a Chart.js config cloning a live chart's datasets/labels/options —
// deep-cloning objects/arrays but keeping function values by reference (see
// bnlDeepCloneKeepFns), so no callback is ever lost — plus the tweaks every
// disposable copy needs regardless of where it renders: no animation (it's
// built and read immediately) and no zoom plugin (a copy's pan/zoom state
// can't carry over cleanly). Shared by the off-screen export clone and the
// fullscreen modal clone; callers layer their own canvas/sizing on top.
function bnlCloneChartConfig(src) {
  const datasets = src.data.datasets.map(ds => ({
    ...ds,
    data: Array.isArray(ds.data) ? [...ds.data] : ds.data,
  }));
  const cfg = {
    type: src.config.type,
    data: { labels: [...(src.data.labels || [])], datasets },
    options: bnlDeepCloneKeepFns(src.options || {}),
  };
  cfg.options.animation = false;
  cfg.options.maintainAspectRatio = false;
  if (cfg.options.plugins) delete cfg.options.plugins.zoom;
  return cfg;
}

// Build a disposable, off-screen clone of a live Chart.js instance (same
// technique as the fullscreen modal) — so exports that need to mutate
// visibility/fonts never touch what's actually rendered on the page.
function bnlCloneChartOffscreen(src) {
  const cfg = bnlCloneChartConfig(src);
  // Fixed size, not responsive: a detached canvas has no sized parent for
  // Chart.js's ResizeObserver to measure, so with responsive left on it was
  // sizing (and cropping the aspect ratio) as if it were the fullscreen
  // modal instead of matching the on-page chart it's a copy of.
  cfg.options.responsive = false;
  const canvas = document.createElement('canvas');
  const width = src.canvas.width || src.width || 1200;
  const height = src.canvas.height || src.height || 600;
  canvas.style.position = 'fixed';
  canvas.style.left = '-99999px';
  canvas.style.top = '0';
  canvas.style.width = width + 'px';
  canvas.style.height = height + 'px';
  document.body.appendChild(canvas);
  const clone = new Chart(canvas, cfg);
  clone.resize(width, height);
  Object.keys(src).filter(k => k.startsWith('$')).forEach(k => { clone[k] = src[k]; });
  clone.update();
  return { clone, canvas };
}

class BNLChart {
  /**
   * @param {string} canvasId
   * @param {object} config     Chart.js config
   * @param {object} [opts]     { exportName: string, exportGroups: [{label, match(dataset)}] }
   */
  constructor(canvasId, config, opts = {}) {
    this.canvasId   = canvasId;
    this.exportName = opts.exportName || canvasId;
    this.exportGroups = opts.exportGroups || null;
    const canvas    = document.getElementById(canvasId);
    if (!canvas) throw new Error(`BNLChart: no canvas #${canvasId}`);
    this.chart = new Chart(canvas, config);
  }

  zoom(f)   { this.chart.zoom(f); }
  pan(d)    { this.chart.pan(d); }
  resetZoom() { this.chart.resetZoom(); }
  destroy() { this.chart.destroy(); }
  update()  { this.chart.update(); }
  get data()    { return this.chart.data; }
  get options() { return this.chart.options; }

  /** Download chart as PNG. */
  exportPNG() {
    const a = document.createElement('a');
    a.download = this.exportName.replace(/\s+/g, '_') + '.png';
    a.href = this.chart.toBase64Image('image/png', 1);
    a.click();
  }

  /** Export each configured exportGroups entry as its own PNG. Runs against a
   * disposable off-screen clone, not the chart actually on screen, so the
   * inline chart never flashes hidden series / boosted fonts while exporting. */
  bulkExportPNG() {
    if (!this.exportGroups || !this.exportGroups.length) return;
    const { clone, canvas } = bnlCloneChartOffscreen(this.chart);
    bnlBulkExportPNG(clone, this.exportName, this.exportGroups, () => {
      clone.destroy();
      canvas.remove();
    });
  }

  /** Open a fullscreen copy in a Bootstrap modal. */
  openFullscreen() {
    const MODAL_ID = 'bnl-fs-modal';
    let modal = document.getElementById(MODAL_ID);
    if (!modal) {
      modal = document.createElement('div');
      modal.id = MODAL_ID;
      modal.className = 'modal fade';
      modal.tabIndex  = -1;
      modal.innerHTML = `
        <div class="modal-dialog modal-fullscreen">
          <div class="modal-content">
            <div class="modal-header py-2 px-3">
              <h6 class="modal-title mb-0 fw-semibold" id="${MODAL_ID}-title"></h6>
              <div class="ms-auto d-flex gap-2 me-2">
                <button class="btn btn-sm btn-outline-secondary d-none" id="${MODAL_ID}-bulk-export">&#8681; Bulk export</button>
                <button class="btn btn-sm btn-outline-secondary" id="${MODAL_ID}-export">&#8681; Export PNG</button>
              </div>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body" style="padding:1.25rem; display:flex; flex-direction:column;">
              <div style="position:relative; flex:1; min-height:0;">
                <canvas id="${MODAL_ID}-canvas"></canvas>
              </div>
            </div>
          </div>
        </div>`;
      document.body.appendChild(modal);
    }

    document.getElementById(`${MODAL_ID}-title`).textContent = this.exportName;

    const bsModal = bootstrap.Modal.getOrCreateInstance(modal);
    let   fsChart = null;

    const onShown = () => {
      const src = this.chart;
      const cfg = bnlCloneChartConfig(src);

      if (fsChart) { fsChart.destroy(); }
      fsChart = new Chart(document.getElementById(`${MODAL_ID}-canvas`), cfg);
      // Chart-instance properties set directly (not part of config), e.g. the
      // forecast page's credit-event markers ($creditEvents read by the
      // bnl-credit-events plugin's afterDraw) — cloning `options`/`data` above
      // doesn't carry these, so the fullscreen copy drew no markers. Copy any
      // `$`-prefixed instance property over and redraw.
      Object.keys(src).filter(k => k.startsWith('$')).forEach(k => { fsChart[k] = src[k]; });
      fsChart.update();

      document.getElementById(`${MODAL_ID}-export`).onclick = () => {
        const a = document.createElement('a');
        a.download = this.exportName.replace(/\s+/g, '_') + '_fs.png';
        a.href = fsChart.toBase64Image('image/png', 1);
        a.click();
      };

      const bulkBtn = document.getElementById(`${MODAL_ID}-bulk-export`);
      if (this.exportGroups && this.exportGroups.length) {
        bulkBtn.classList.remove('d-none');
        bulkBtn.onclick = () => bnlBulkExportPNG(fsChart, this.exportName, this.exportGroups);
      } else {
        bulkBtn.classList.add('d-none');
        bulkBtn.onclick = null;
      }
    };

    const onHidden = () => {
      if (fsChart) { fsChart.destroy(); fsChart = null; }
    };

    // Remove old listeners then add fresh ones
    modal.removeEventListener('shown.bs.modal',  modal._bnlShown);
    modal.removeEventListener('hidden.bs.modal', modal._bnlHidden);
    modal._bnlShown  = onShown;
    modal._bnlHidden = onHidden;
    modal.addEventListener('shown.bs.modal',  onShown);
    modal.addEventListener('hidden.bs.modal', onHidden);

    bsModal.show();
  }
}
