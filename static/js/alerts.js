/**
 * Notification read/unread state for the navbar alerts bell.
 *
 * Read state is persisted SERVER-SIDE (config/alert_read_state.json) so it
 * survives across browsers and machines. The server renders the correct unread
 * count, badge color, and read/hidden items on every page load; this script
 * only handles the "mark read" actions and repaints the bell in place.
 *
 * The badge shows the UNREAD count; read items are hidden from the bell and
 * dimmed on the Notifications page. Resolved conditions are pruned server-side,
 * so a condition that recurs later re-notifies.
 */
'use strict';

(function () {
  const ENDPOINT = '/alerts/read';

  // Repaint the badge from a server response ({unread_count, sev}).
  function paintBadge(res) {
    const badge = document.getElementById('alert-badge');
    if (!badge || !res) return;
    badge.textContent = res.unread_count;
    badge.hidden = res.unread_count <= 0;
    if (res.sev) {
      badge.classList.remove('bg-danger', 'bg-warning', 'bg-info');
      badge.classList.add('bg-' + res.sev);
    }
  }

  // Reflect the empty-state row once the dropdown has no visible alerts left.
  function refreshEmptyState() {
    const menu = document.getElementById('nav-alert-menu');
    if (!menu) return;
    const anyUnread = [...menu.querySelectorAll('.nav-alert-li')].some(li => !li.hidden);
    const empty = document.getElementById('nav-alert-empty');
    if (empty) empty.hidden = anyUnread;
  }

  // Dim + hide every rendering of a given alert id (bell + Notifications page).
  function applyReadUI(id) {
    document.querySelectorAll('.alert-item[data-alert-id]').forEach(el => {
      if (el.dataset.alertId !== id) return;
      el.classList.add('read');
      const li = el.closest('.nav-alert-li');
      if (li) li.hidden = true;
    });
    refreshEmptyState();
  }

  // Fire a beacon so the write survives a click that also navigates away.
  function persist(body) {
    const data = JSON.stringify(body);
    if (navigator.sendBeacon) {
      navigator.sendBeacon(ENDPOINT, new Blob([data], { type: 'application/json' }));
      return Promise.resolve(null);
    }
    return fetch(ENDPOINT, {
      method: 'POST', keepalive: true,
      headers: { 'Content-Type': 'application/json' }, body: data,
    }).then(r => (r.ok ? r.json() : null)).catch(() => null);
  }

  window.markAlertRead = function (id) {
    // Optimistic UI; the link itself usually navigates, and the next page render
    // reflects the persisted read-state. The beacon makes the write reliable.
    applyReadUI(id);
    persist({ ids: [id] });
  };

  window.markAllAlertsRead = function (ev) {
    if (ev) ev.preventDefault();
    document.querySelectorAll('.alert-item[data-alert-id]').forEach(el => el.classList.add('read'));
    document.querySelectorAll('#nav-alert-menu .nav-alert-li').forEach(li => { li.hidden = true; });
    refreshEmptyState();
    // No navigation here, so use fetch and repaint the badge from the response.
    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ all: true }),
    }).then(r => (r.ok ? r.json() : null)).then(paintBadge).catch(() => {});
  };
})();
