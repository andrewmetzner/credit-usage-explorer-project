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
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(chart._crosshairX, top);
      ctx.lineTo(chart._crosshairX, bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = 'rgba(0,0,0,.18)';
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
