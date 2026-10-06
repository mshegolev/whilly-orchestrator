/* Run synchronously in <head>: choose colors before the first content paint. */
(function () {
  'use strict';
  const key = 'whilly-theme';
  const valid = value => ['light', 'dark', 'system'].includes(value) ? value : 'system';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try { preference = valid(window.localStorage.getItem(key)); } catch (_) { /* Storage may be disabled. */ }
  function apply() {
    document.documentElement.dataset.theme = preference === 'system' ? (media.matches ? 'dark' : 'light') : preference;
    document.querySelectorAll('[data-theme-select]').forEach(control => { control.value = preference; });
  }
  apply();
  media.addEventListener('change', () => { if (preference === 'system') apply(); });
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    preference = valid(event.newValue);
    apply();
  });
  // The dashboard replaces <body> during refresh; listeners must outlive it.
  document.addEventListener('change', event => {
    if (!event.target?.matches?.('[data-theme-select]')) return;
    preference = valid(event.target.value);
    try { window.localStorage.setItem(key, preference); } catch (_) { /* Keep the page usable. */ }
    apply();
  });
  document.addEventListener('htmx:afterSwap', apply);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply);
  else apply();
})();
