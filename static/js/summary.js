/**
 * Summary page charts. All server data arrives via the #summary-data JSON
 * island, so this file is plain cacheable JS with zero template interpolation.
 *
 * Each chart has its own client-side dropdown filters (time range, and for the
 * usage-type chart a type focus / for active users a contract scope). Filtering
 * mutates the existing chart in place — no page reload — so the controls feel
 * instant and stay independent per chart.
 */
'use strict';

(function () {
  const el = document.getElementById('summary-data');
  if (!el) return;
  let D;
  try { D = JSON.parse(el.textContent); } catch (_) { return; }

  // Keep the last `n` weeks of an array (n <= 0 means "all").
  const lastN = (arr, n) => (n > 0 ? arr.slice(-n) : arr);
  const weeksOf = sel => (sel ? parseInt(sel.value, 10) || 0 : 0);
  const emptyMsg = (canvasId, msg) => {
    const c = document.getElementById(canvasId);
    if (c) { const host = c.closest('.dash-section-body') || c.parentElement;
             if (host) host.innerHTML = `<p class="text-muted small mb-0 p-3">${msg}</p>`; }
  };

  // Remember each chart control's last value per browser, so leaving the page
  // (e.g. to change a setting) and coming back doesn't silently reset every
  // chart to its defaults (weekly view, all weeks, negatives shown, ...).
  const PREF_PREFIX = 'summaryChartPref.';
  const getPref = key => { try { return localStorage.getItem(PREF_PREFIX + key); } catch (_) { return null; } };
  const setPref = (key, val) => { try { localStorage.setItem(PREF_PREFIX + key, val); } catch (_) { /* noop */ } };
  // Restores a select/checkbox from its saved value (if any options already
  // match — callers that populate <option>s dynamically must do so first)
  // and keeps future changes saved. Returns the element for convenience.
  function persistControl(id) {
    const el = document.getElementById(id);
    if (!el) return el;
    const saved = getPref(id);
    if (saved !== null) {
      if (el.type === 'checkbox') el.checked = saved === '1';
      else el.value = saved;
    }
    el.addEventListener('change', () => {
      setPref(id, el.type === 'checkbox' ? (el.checked ? '1' : '0') : el.value);
    });
    return el;
  }

  // ===== Credits by usage type per week (stacked bar) =====
  (function initUsageType() {
    const data = D.usageType;
    const chart = renderUsageTypeChart('summaryUsageTypeChart', data);
    window.summaryUsageTypeChart = chart;
    if (!chart) return;  // renderUsageTypeChart already showed an empty state

    // Preserve each type's original palette color when filtering.
    const colorByName = {};
    data.series.forEach((s, i) => { colorByName[s.name] = BNL_PALETTE[i % BNL_PALETTE.length]; });

    const typeSel = document.getElementById('ut-type-filter');
    const weeksSel = document.getElementById('ut-weeks-filter');
    const scopeSel = document.getElementById('ut-scope-filter');
    const inC = data.in_contract || data.weeks.map(() => true);
    if (typeSel) {
      typeSel.insertAdjacentHTML('beforeend',
        data.series.map(s => `<option value="${s.name}">${s.name}</option>`).join(''));
    }
    // Restore saved filter choices now that typeSel's per-type options exist.
    [typeSel, weeksSel, scopeSel].forEach(s => s && persistControl(s.id));

    function apply() {
      const type = typeSel ? typeSel.value : '__all__';
      // Visible week indices: drop pre-contract weeks (if scoped), then last-N.
      let idx = data.weeks.map((_, i) => i);
      if (scopeSel && scopeSel.value === 'contract') idx = idx.filter(i => inC[i]);
      const n = weeksOf(weeksSel);
      if (n > 0) idx = idx.slice(-n);
      const series = (type === '__all__') ? data.series : data.series.filter(s => s.name === type);
      const c = chart.chart;
      c.data.labels = idx.map(i => data.weeks[i]);
      c.data.datasets = series.map(s => ({
        label: s.name,
        data: idx.map(i => s.data[i]),
        backgroundColor: colorByName[s.name],
        borderWidth: 0,
        borderRadius: 2,
      }));
      c.update();
    }
    [typeSel, weeksSel, scopeSel].forEach(s => s && s.addEventListener('change', apply));
    apply();  // reflect any restored filter choice immediately
  })();

  // ===== Weekly credit burn =====
  (function initWeekly() {
    const series = {
      weekly: D.weeklyTrend || [],
      daily: D.dailyTrend || [],
    };
    if (!series.weekly.length && !series.daily.length) {
      emptyMsg('weeklyChart', 'No date data available to plot.');
      return;
    }

    // Pre-contract weeks render gray (matching the Active Users chart).
    const IN_BG = 'rgba(13,110,253,0.65)', IN_BD = 'rgba(13,110,253,1)';
    const PRE_BG = 'rgba(108,117,125,0.45)', PRE_BD = 'rgba(108,117,125,0.85)';
    const bgFor = rows => rows.map(d => (d.in_contract ? IN_BG : PRE_BG));
    const bdFor = rows => rows.map(d => (d.in_contract ? IN_BD : PRE_BD));
    const titleEl = document.getElementById('burn-chart-title');
    const weeklyBtn = document.getElementById('wb-gran-weekly');
    const dailyBtn = document.getElementById('wb-gran-daily');
    const weeksSel = document.getElementById('wb-weeks-filter');
    const scopeSel = document.getElementById('wb-scope-filter');
    const hideNegSel = document.getElementById('wb-hide-negatives');
    const rangeOptions = Array.from(weeksSel ? weeksSel.options : []);
    [weeksSel, scopeSel, hideNegSel].forEach(s => s && persistControl(s.id));

    let currentMode = series.weekly.length ? 'weekly' : 'daily';
    const savedMode = getPref('wb-granularity');
    if (savedMode && series[savedMode] && series[savedMode].length) currentMode = savedMode;
    let currentRows = series[currentMode];
    let curIc = currentRows.map(d => d.in_contract);

    // "Hide negatives": a big same-day refund/correction can drag the net
    // total below zero and bury real usage under it. When on, the bar shows
    // only the positive (consumed) portion; the tooltip footer notes what
    // was refunded so the dip isn't just silently dropped.
    const hidingNegatives = () => !!(hideNegSel && hideNegSel.checked);
    const valueFor = d => (hidingNegatives() ? d.positive_credits : d.total_credits);

    function modeConfig(mode) {
      return mode === 'daily'
        ? {
            rows: series.daily,
            labelKey: 'day',
            title: 'Daily Credit Burn',
            exportName: 'Daily Credit Burn',
            unit: 'days',
            labelPrefix: 'Day of ',
          }
        : {
            rows: series.weekly,
            labelKey: 'week',
            title: 'Weekly Credit Burn',
            exportName: 'Weekly Credit Burn',
            unit: 'wks',
            labelPrefix: 'Week of ',
          };
    }

    function refreshRangeLabels() {
      if (!weeksSel || rangeOptions.length === 0) return;
      const cfg = modeConfig(currentMode);
      rangeOptions[0].text = `All ${currentMode === 'daily' ? 'days' : 'weeks'}`;
      [4, 8, 12, 26].forEach((n, idx) => {
        if (rangeOptions[idx + 1]) rangeOptions[idx + 1].text = `Last ${n} ${cfg.unit}`;
      });
    }

    function refreshGranularityUI() {
      const cfg = modeConfig(currentMode);
      if (titleEl) titleEl.textContent = cfg.title;
      if (weeklyBtn) weeklyBtn.classList.toggle('active', currentMode === 'weekly');
      if (dailyBtn) dailyBtn.classList.toggle('active', currentMode === 'daily');
      if (window.summaryWeeklyChart) window.summaryWeeklyChart.exportName = cfg.exportName;
      refreshRangeLabels();
    }

    window.summaryWeeklyChart = new BNLChart('weeklyChart', {
      type: 'bar',
      data: {
        labels: currentRows.map(d => d[modeConfig(currentMode).labelKey]),
        datasets: [{
          label: 'Credits used',
          data: currentRows.map(valueFor),
          backgroundColor: bgFor(currentRows),
          borderColor: bdFor(currentRows),
          borderWidth: 1,
          borderRadius: 3,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            title: items => (modeConfig(currentMode).labelPrefix + items[0].label),
            footer: items => {
              const i = items[0].dataIndex;
              const lines = [];
              if (!curIc[i]) lines.push('Pre-contract');
              const refunded = currentRows[i] && currentRows[i].refunded_credits;
              if (hidingNegatives() && refunded > 0) {
                lines.push(`Refunded ${refunded.toLocaleString()} credits that ${currentMode === 'daily' ? 'day' : 'week'}`);
              }
              return lines;
            },
          } },
        },
        scales: {
          y: { beginAtZero: true, ticks: { callback: v => v.toLocaleString() } },
          x: { ticks: { maxRotation: 45, font: { size: 10 }, maxTicksLimit: 16 } },
        },
      },
    }, { exportName: 'Weekly Credit Burn' });

    function apply() {
      const cfg = modeConfig(currentMode);
      let rows = cfg.rows;
      if (scopeSel && scopeSel.value === 'contract') rows = rows.filter(d => d.in_contract);
      rows = lastN(rows, weeksOf(weeksSel));
      currentRows = rows;
      curIc = rows.map(d => d.in_contract);
      const c = window.summaryWeeklyChart.chart;
      c.data.labels = rows.map(d => d[cfg.labelKey]);
      c.data.datasets[0].data = rows.map(valueFor);
      c.data.datasets[0].backgroundColor = bgFor(rows);
      c.data.datasets[0].borderColor = bdFor(rows);
      c.update();
    }

    window.setSummaryBurnGranularity = function (mode, btn) {
      if (!series[mode] || mode === currentMode) return;
      currentMode = mode;
      setPref('wb-granularity', mode);
      refreshGranularityUI();
      apply();
      if (btn && btn.parentElement) {
        Array.from(btn.parentElement.querySelectorAll('button')).forEach(b => {
          b.classList.toggle('active', b === btn);
        });
      }
    };

    [weeksSel, scopeSel, hideNegSel].forEach(s => s && s.addEventListener('change', apply));
    refreshGranularityUI();
    apply();
    if (!series.daily.length && dailyBtn) dailyBtn.disabled = true;
    if (!series.weekly.length && weeklyBtn) weeklyBtn.disabled = true;
  })();

  // ===== Active users per week =====
  (function initActiveUsers() {
    const auData = D.activeUsers || [];
    if (!auData.length) { emptyMsg('activeUsersChart', 'No active user data available.'); return; }

    // `curIc` tracks the in-contract flags for the rows CURRENTLY shown, so the
    // tooltip footer stays correct after filtering.
    let curIc = auData.map(d => d.in_contract);
    const bgFor = ic => ic.map(v => (v ? 'rgba(111,66,193,0.7)' : 'rgba(108,117,125,0.35)'));

    window.summaryActiveUsersChart = new BNLChart('activeUsersChart', {
      type: 'bar',
      data: {
        labels: auData.map(d => d.week_start),
        datasets: [{ label: 'Active users', data: auData.map(d => d.active_users),
                     backgroundColor: bgFor(curIc), borderRadius: 3 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            title: items => 'Week of ' + items[0].label,
            label: ctx => `  Active users: ${ctx.parsed.y}`,
            footer: items => curIc[items[0].dataIndex] ? 'In contract period' : 'Pre-contract',
          } },
        },
        scales: {
          y: { beginAtZero: true, ticks: { stepSize: 1, font: { size: 10 } }, grid: { color: 'rgba(0,0,0,.05)' } },
          x: { ticks: { maxRotation: 45, maxTicksLimit: 16, font: { size: 10 } }, grid: { display: false } },
        },
      },
    }, { exportName: 'Active Users per Week' });

    const weeksSel = document.getElementById('au-weeks-filter');
    const scopeSel = document.getElementById('au-scope-filter');
    [weeksSel, scopeSel].forEach(s => s && persistControl(s.id));
    function apply() {
      let rows = auData;
      if (scopeSel && scopeSel.value === 'contract') rows = rows.filter(d => d.in_contract);
      rows = lastN(rows, weeksOf(weeksSel));
      curIc = rows.map(d => d.in_contract);
      const c = window.summaryActiveUsersChart.chart;
      c.data.labels = rows.map(d => d.week_start);
      c.data.datasets[0].data = rows.map(d => d.active_users);
      c.data.datasets[0].backgroundColor = bgFor(curIc);
      c.update();
    }
    [weeksSel, scopeSel].forEach(s => s && s.addEventListener('change', apply));
    apply();  // reflect any restored filter choice immediately
  })();
})();
