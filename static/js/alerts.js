/**
 * Notification read/unread state for the navbar alerts bell.
 * Read state is kept client-side (localStorage) keyed by each alert's stable id.
 * The badge shows the UNREAD count; read items are dimmed. Resolved conditions
 * are pruned, so a condition that recurs later re-notifies.
 */
'use strict';

(function () {
  const KEY = 'bnl-read-alerts';

  function getRead() {
    try { return new Set(JSON.parse(localStorage.getItem(KEY) || '[]')); }
    catch (_) { return new Set(); }
  }
  function setRead(s) { localStorage.setItem(KEY, JSON.stringify([...s])); }

  function currentIds() {
    // Prefer the complete list (the dropdown only shows the first few).
    const holder = document.getElementById('all-alert-ids');
    if (holder && holder.dataset.ids) return holder.dataset.ids.split(',').filter(Boolean);
    return [...document.querySelectorAll('.alert-item[data-alert-id]')].map(e => e.dataset.alertId);
  }

  function paint() {
    const read = getRead();
    document.querySelectorAll('.alert-item[data-alert-id]').forEach(el => {
      el.classList.toggle('read', read.has(el.dataset.alertId));
    });

    // The navbar bell is an "inbox": surface only UNREAD alerts there, so items
    // the user has already read don't linger. (The Notifications page still
    // lists everything as a history.)
    const menu = document.getElementById('nav-alert-menu');
    if (menu) {
      let anyUnread = false;
      menu.querySelectorAll('.nav-alert-li').forEach(li => {
        const item = li.querySelector('.alert-item[data-alert-id]');
        const isRead = item && read.has(item.dataset.alertId);
        li.hidden = !!isRead;
        if (!isRead) anyUnread = true;
      });
      const empty = document.getElementById('nav-alert-empty');
      if (empty) empty.hidden = anyUnread;
    }

    const unread = currentIds().filter(id => !read.has(id)).length;
    const badge = document.getElementById('alert-badge');
    if (badge) {
      badge.textContent = unread;
      badge.style.display = unread > 0 ? '' : 'none';
    }
  }

  // Prune read ids that no longer correspond to an active alert, so a resolved
  // condition that recurs later re-notifies.
  //
  // Guard: only prune when this page actually rendered a non-empty alert list.
  // The bell is fed by a global context processor, but if it ever yields an
  // empty list on a given page (e.g. a transient failure building the forecast
  // service on the Records page), pruning against an empty set would wipe ALL
  // read-state — making already-read alerts pop back as unread elsewhere.
  (function prune() {
    const ids = new Set(currentIds());
    if (ids.size === 0) return;            // nothing to prune against — leave read-state intact
    const read = new Set([...getRead()].filter(id => ids.has(id)));
    setRead(read);
  })();

  window.markAlertRead = function (id) {
    const s = getRead(); s.add(id); setRead(s); paint();
  };
  window.markAllAlertsRead = function (ev) {
    if (ev) ev.preventDefault();
    const s = getRead(); currentIds().forEach(id => s.add(id)); setRead(s); paint();
  };

  paint();
})();
