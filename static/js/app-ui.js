/*
 * Small behaviours every page shares, most of them about working well on a
 * phone:
 *
 *   1. Password eye   - every password box gets an eye at its end: tap to see
 *                       what you typed, tap again to hide it.
 *   2. Side menu      - on a phone the menu slides in over a dimmed page and
 *                       closes on a tap outside it, on Escape, or on the
 *                       close button.
 *   3. Stacked tables - a list table is drawn as one card per row on a narrow
 *                       screen, each value under its column's name, instead of
 *                       a grid you have to scroll sideways to read.
 *   4. Filter panel   - a page's filters fold away behind one "Filters" button
 *                       on a phone, with the number of filters in use on it.
 *
 * Everything here is an enhancement: with the script missing the page still
 * works, just less comfortably on a small screen.
 */
(function (window, document) {
  "use strict";

  function t(key, fallback) {
    return window.I18N && window.I18N.t ? window.I18N.t(key, fallback) : fallback;
  }

  /* ---------------------------------------------------------------------
     1. Password eye
     --------------------------------------------------------------------- */
  function setEye(btn, shown) {
    var key = shown ? "r4.hide_password" : "r4.show_password";
    var text = shown ? "Hide password" : "Show password";
    btn.innerHTML = '<i class="bi ' + (shown ? "bi-eye-slash" : "bi-eye") + '"></i>';
    btn.setAttribute("aria-pressed", shown ? "true" : "false");
    btn.setAttribute("data-i18n-title", key);
    btn.setAttribute("title", t(key, text));
    btn.setAttribute("aria-label", t(key, text));
  }

  function addEye(input) {
    // data-eye="own": the page draws its own toggle (the sign-in form).
    if (input.dataset.eye) return;
    input.dataset.eye = "1";

    var btn = document.createElement("button");
    btn.type = "button";
    btn.tabIndex = -1;
    setEye(btn, false);
    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      setEye(btn, show);
      input.focus();
    });

    var group = input.closest(".input-group");
    if (group) {
      // Inside an input group the eye is one more segment of the group.
      btn.className = "btn btn-outline-secondary pw-eye-btn";
      input.insertAdjacentElement("afterend", btn);
      return;
    }
    // Moving a box takes the cursor out of it; put it back if it was there.
    var focused = document.activeElement === input;
    var wrap = document.createElement("div");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    btn.className = "pw-eye";
    wrap.appendChild(btn);
    if (focused) input.focus();
  }

  function enhancePasswords(root) {
    (root || document).querySelectorAll('input[type="password"]').forEach(addEye);
  }

  /* ---------------------------------------------------------------------
     2. Side menu
     --------------------------------------------------------------------- */
  function setMenu(open) {
    document.body.classList.toggle("sidebar-open", open);
    var toggle = document.querySelector("[data-sidebar-toggle]");
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function wireMenu() {
    document.querySelectorAll("[data-sidebar-toggle]").forEach(function (el) {
      el.addEventListener("click", function () {
        setMenu(!document.body.classList.contains("sidebar-open"));
      });
    });
    document.querySelectorAll("[data-sidebar-close]").forEach(function (el) {
      el.addEventListener("click", function () { setMenu(false); });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setMenu(false);
    });
    // Following a link closes the menu, so going back does not land on a
    // page still covered by it.
    document.querySelectorAll(".sidebar a.nav-link").forEach(function (a) {
      a.addEventListener("click", function () { setMenu(false); });
    });
  }

  /* ---------------------------------------------------------------------
     3. Stacked tables
     Each cell carries its column's name in data-label; the stylesheet shows
     it on a narrow screen. The names are read from the header AFTER it has
     been translated, and read again whenever the language changes.
     --------------------------------------------------------------------- */
  function headerNames(table) {
    var row = table.tHead && table.tHead.rows[table.tHead.rows.length - 1];
    if (!row) return null;
    var names = [];
    Array.prototype.forEach.call(row.cells, function (th) {
      var span = th.colSpan || 1;
      var text = (th.textContent || "").replace(/\s+/g, " ").trim();
      for (var i = 0; i < span; i++) names.push(text);
    });
    return names;
  }

  function labelRows(table) {
    var names = headerNames(table);
    if (!names) return;
    Array.prototype.forEach.call(table.tBodies, function (body) {
      Array.prototype.forEach.call(body.rows, function (tr) {
        var col = 0;
        Array.prototype.forEach.call(tr.cells, function (td) {
          if (td.colSpan > 1 && td.colSpan >= names.length - 1) {
            td.classList.add("cell-wide");
          } else {
            td.setAttribute("data-label", names[col] || "");
          }
          col += td.colSpan || 1;
        });
      });
    });
  }

  function stackTables() {
    document.querySelectorAll(".table-responsive > table.table, table.table-stackable")
      .forEach(function (table) {
        if (table.classList.contains("no-stack") || table.closest(".no-stack")) return;
        var names = headerNames(table);
        // Two columns read fine side by side on any phone.
        if (!names || names.length < 3) return;
        table.classList.add("table-stack");
        labelRows(table);
        if (!table._stackWatch) {
          // Rows added later - the till's cart - get their labels too.
          table._stackWatch = new MutationObserver(function () { labelRows(table); });
          Array.prototype.forEach.call(table.tBodies, function (body) {
            table._stackWatch.observe(body, { childList: true });
          });
        }
      });
  }

  /* ---------------------------------------------------------------------
     4. Filter panel
     --------------------------------------------------------------------- */
  function activeFilters(form) {
    var n = 0;
    form.querySelectorAll("input, select").forEach(function (el) {
      if (el.type === "hidden" || el.type === "submit" || el.type === "button") return;
      if (el.name === "tab" || el.name === "page" || el.name === "month") return;
      if (el.type === "checkbox" || el.type === "radio") { if (el.checked) n++; return; }
      if ((el.value || "").trim() !== "") n++;
    });
    return n;
  }

  function foldFilters() {
    document.querySelectorAll("form.filter-bar").forEach(function (form) {
      if (form.dataset.folded) return;
      form.dataset.folded = "1";
      var count = activeFilters(form);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "filter-toggle";
      btn.setAttribute("aria-expanded", "false");
      btn.innerHTML =
        '<span><i class="bi bi-funnel me-2"></i><span>Filters</span></span>' +
        (count ? '<span class="filter-count">' + count + "</span>" : "") +
        '<i class="bi bi-chevron-down filter-caret"></i>';
      btn.addEventListener("click", function () {
        var open = !form.classList.contains("is-open");
        form.classList.toggle("is-open", open);
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      form.insertBefore(btn, form.firstChild);
      form.classList.add("is-foldable");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    enhancePasswords();
    wireMenu();
    stackTables();
    foldFilters();
  });
  // Column names come back translated after a language switch.
  document.addEventListener("i18n:changed", function () {
    document.querySelectorAll("table.table-stack").forEach(labelRows);
    document.querySelectorAll(".pw-eye, .pw-eye-btn").forEach(function (btn) {
      setEye(btn, btn.getAttribute("aria-pressed") === "true");
    });
  });

  window.AppUI = { enhancePasswords: enhancePasswords, stackTables: stackTables };
})(window, document);
