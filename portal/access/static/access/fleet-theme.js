/* Run in <head> before styles to avoid a light flash. Preferences are per origin. */
(function () {
  function read() { try { return localStorage.getItem('fleet.theme') === 'light' ? 'light' : 'dark'; } catch (_) { return 'dark'; } }
  function apply(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll('[data-fleet-theme-toggle]').forEach(function (button) {
      button.textContent = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    });
  }
  apply(read());
  document.addEventListener('DOMContentLoaded', function () { apply(read()); });
  document.addEventListener('click', function (event) {
    if (!(event.target instanceof Element) || !event.target.closest('[data-fleet-theme-toggle]')) return;
    var theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('fleet.theme', theme); } catch (_) {}
    apply(theme);
  });
  window.addEventListener('storage', function (event) { if (event.key === 'fleet.theme' || event.key === null) apply(read()); });
})();
