/*
 * Light / dark theme switch.
 * --------------------------
 * Uses Bootstrap 5.3's native data-bs-theme attribute, so every Bootstrap
 * component adapts automatically; app.css supplies the custom variables.
 *
 * Order of preference:
 *   1. an explicit choice saved in localStorage
 *   2. the operating system setting (prefers-color-scheme)
 *   3. light
 *
 * The applied theme is written to <html> in a blocking inline script in
 * base.html BEFORE the stylesheet loads, which avoids the white flash you
 * would otherwise get on every page load in dark mode.
 */
(function (window, document) {
  "use strict";

  var STORAGE_KEY = "faruq.theme";

  function systemPref() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark" : "light";
  }

  function stored() {
    try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }

  function resolve() {
    return stored() || systemPref();
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-bs-theme", theme);

    var icon = document.getElementById("themeIcon");
    if (icon) icon.className = theme === "dark" ? "bi bi-sun" : "bi bi-moon-stars";

    var btn = document.getElementById("themeToggle");
    if (btn) {
      btn.setAttribute("title", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
      btn.setAttribute("data-i18n-title", theme === "dark" ? "common.light_mode" : "common.dark_mode");
    }

    document.dispatchEvent(new CustomEvent("theme:changed", { detail: { theme: theme } }));
  }

  function toggle() {
    var next = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
    try { localStorage.setItem(STORAGE_KEY, next); } catch (e) { /* private mode */ }
    apply(next);
  }

  window.Theme = { apply: apply, toggle: toggle, resolve: resolve };

  document.addEventListener("DOMContentLoaded", function () {
    apply(resolve());

    var btn = document.getElementById("themeToggle");
    if (btn) btn.addEventListener("click", toggle);

    // Follow the OS only while the user has not made an explicit choice.
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (e) {
        if (!stored()) apply(e.matches ? "dark" : "light");
      });
    }
  });
})(window, document);
