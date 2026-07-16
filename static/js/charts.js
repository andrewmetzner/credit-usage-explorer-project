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

class BNLChart {
  /**
   * @param {string} canvasId
   * @param {object} config     Chart.js config
   * @param {object} [opts]     { exportName: string }
   */
  constructor(canvasId, config, opts = {}) {
    this.canvasId   = canvasId;
    this.exportName = opts.exportName || canvasId;
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
      const datasets = src.data.datasets.map(ds => ({
        ...ds,
        data: Array.isArray(ds.data) ? [...ds.data] : ds.data,
      }));
      const cfg = {
        type: src.config.type,
        data: { labels: [...(src.data.labels || [])], datasets },
        options: JSON.parse(JSON.stringify(src.options || {})),
      };
      cfg.options.maintainAspectRatio = false;
      cfg.options.animation = false;
      // Disable zoom plugin in modal (pan state won't carry over cleanly)
      if (cfg.options.plugins) delete cfg.options.plugins.zoom;

      if (fsChart) { fsChart.destroy(); }
      fsChart = new Chart(document.getElementById(`${MODAL_ID}-canvas`), cfg);

      document.getElementById(`${MODAL_ID}-export`).onclick = () => {
        const a = document.createElement('a');
        a.download = this.exportName.replace(/\s+/g, '_') + '_fs.png';
        a.href = fsChart.toBase64Image('image/png', 1);
        a.click();
      };
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
