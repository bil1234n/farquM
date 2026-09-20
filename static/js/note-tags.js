/*
 * Coloured note marks - the picker.
 *
 * WHAT IT IS FOR
 * --------------
 * A note is easier to act on when you know what kind of note it is before
 * you read it: red "Bad" on a customer who never pays, green "Good" on one
 * who always does. So every note in the system can carry a MARK - a colour
 * with a meaning - and the list of marks belongs to the people using it:
 * they add their own ("Urgent", orange), change what a colour means, or
 * change the colour itself.
 *
 * HOW
 * ---
 * The server renders an ordinary <select data-note-tag> (see
 * core.forms.NoteTagSelect), each option carrying data-color. It works with
 * no JavaScript at all. This script hides it - it still posts with the form -
 * and draws a row of coloured chips in its place, plus an "Edit marks" panel
 * that talks to /api/options/ (the same endpoint the phone uses, so a mark
 * added in the browser is on the phone a moment later, and the other way
 * round).
 *
 * The text box the mark describes (data-note-field) is tinted in the chosen
 * colour, so the colour is visible while the note is being written too.
 */
(function () {
  "use strict";

  var GROUP = "NOTE_TAG";
  var FALLBACK = "#64748B";
  var HEX = /^#[0-9A-Fa-f]{6}$/;
  // One tap for the usual meanings: red, orange, amber, green, teal, blue,
  // violet, pink, slate.
  var PRESETS = [
    "#DC2626", "#EA580C", "#D97706", "#16A34A", "#0D9488",
    "#2563EB", "#7C3AED", "#DB2777", "#64748B"
  ];
  var pickers = [];

  function csrf() {
    var match = document.cookie.match(/(^|;)\s*csrftoken=([^;]+)/);
    if (match) { return decodeURIComponent(match[2]); }
    var field = document.querySelector("input[name=csrfmiddlewaretoken]");
    return field ? field.value : "";
  }

  /* The first readable sentence in a DRF error body. */
  function explain(body) {
    if (!body) { return "That could not be saved."; }
    if (body.detail) { return body.detail; }
    for (var key in body) {
      if (!Object.prototype.hasOwnProperty.call(body, key)) { continue; }
      var value = body[key];
      if (Array.isArray(value) && value.length) { return String(value[0]); }
      if (typeof value === "string") { return value; }
    }
    return "That could not be saved.";
  }

  function api(url, options) {
    options = options || {};
    options.headers = {"Content-Type": "application/json", "X-CSRFToken": csrf()};
    options.credentials = "same-origin";
    return fetch(url, options).then(function (response) {
      if (response.status === 204) { return null; }
      return response.json().then(function (body) {
        if (!response.ok) { throw new Error(explain(body)); }
        return body;
      }, function () {
        if (!response.ok) { throw new Error("That could not be saved."); }
        return null;
      });
    });
  }

  function listMarks() {
    return api("/api/options/?group=" + GROUP);
  }

  function colour(value) {
    return HEX.test(value || "") ? String(value).toUpperCase() : FALLBACK;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function button(className, text, title) {
    var node = el("button", className, text);
    node.type = "button";
    if (title) { node.title = title; }
    return node;
  }

  function optionFor(select, value) {
    for (var i = 0; i < select.options.length; i += 1) {
      if (select.options[i].value === value) { return select.options[i]; }
    }
    return null;
  }

  /* Rebuild a select from the list the server returned, keeping whatever it
     had chosen - even a mark that has since been switched off, which stays
     at the end with its old wording, because that is what the record holds. */
  function refill(select, rows) {
    var keep = select.value;
    var kept = keep ? optionFor(select, keep) : null;
    var keptLabel = kept ? kept.textContent : "";
    var keptColor = kept ? kept.getAttribute("data-color") : "";
    var blank = optionFor(select, "");
    var blankText = blank ? blank.textContent : "No mark";

    select.innerHTML = "";
    var none = el("option", null, blankText);
    none.value = "";
    select.appendChild(none);

    var found = !keep;
    rows.forEach(function (row) {
      var option = el("option", null, row.label);
      option.value = String(row.id);
      option.setAttribute("data-color", colour(row.color));
      select.appendChild(option);
      if (option.value === keep) { found = true; }
    });
    if (!found) {
      var orphan = el("option", null, keptLabel);
      orphan.value = keep;
      orphan.setAttribute("data-color", colour(keptColor));
      select.appendChild(orphan);
    }
    select.value = keep;
  }

  /* After any change to the list: every picker on the page redraws, so a
     mark added in one form is in the others without a reload. */
  function refreshAll() {
    return listMarks().then(function (rows) {
      pickers.forEach(function (picker) { picker.update(rows); });
      return rows;
    });
  }

  function enhance(select) {
    if (select.dataset.ntReady === "1") { return; }
    select.dataset.ntReady = "1";

    var host = el("div", "note-picker-host");
    select.parentNode.insertBefore(host, select.nextSibling);
    select.hidden = true;   // hidden, not disabled: it still posts

    var chips = el("div", "note-picker");
    chips.setAttribute("role", "group");
    chips.setAttribute("aria-label", "Mark");
    host.appendChild(chips);

    var manager = el("div", "note-manager");
    manager.hidden = true;
    host.appendChild(manager);

    var noteName = select.getAttribute("data-note-field");
    var form = select.form;
    var textBox = form && noteName
      ? form.querySelector('[name="' + noteName + '"]')
      : null;

    function tint() {
      if (!textBox) { return; }
      var chosen = select.value ? optionFor(select, select.value) : null;
      if (chosen) {
        textBox.classList.add("note-tinted");
        textBox.style.setProperty("--mark", colour(chosen.getAttribute("data-color")));
      } else {
        textBox.classList.remove("note-tinted");
        textBox.style.removeProperty("--mark");
      }
    }

    function choose(value) {
      select.value = value;
      // The change listener below redraws the chips and the tint.
      select.dispatchEvent(new Event("change", {bubbles: true}));
    }

    function drawChips() {
      chips.innerHTML = "";
      Array.prototype.forEach.call(select.options, function (option) {
        var chip = button("note-chip", null, option.value ? "" : "No mark");
        if (option.value) {
          chip.style.setProperty("--mark", colour(option.getAttribute("data-color")));
        } else {
          chip.classList.add("is-none");
        }
        chip.setAttribute("aria-pressed", option.value === select.value ? "true" : "false");
        chip.appendChild(el("span", "note-dot"));
        chip.appendChild(document.createTextNode(option.textContent));
        chip.addEventListener("click", function () { choose(option.value); });
        chips.appendChild(chip);
      });

      var tool = button(
        "note-chip note-chip-tool", null,
        "Add a mark, or change what a colour means"
      );
      tool.setAttribute("aria-expanded", manager.hidden ? "false" : "true");
      tool.appendChild(el("i", "bi bi-sliders"));
      tool.appendChild(document.createTextNode(manager.hidden ? " Edit marks" : " Close"));
      tool.addEventListener("click", function () {
        if (manager.hidden) { openManager(); } else { closeManager(); }
      });
      chips.appendChild(tool);
      tint();
    }

    var errorBox = null;
    function fail(error) {
      if (!errorBox) { return; }
      errorBox.textContent = error ? (error.message || String(error)) : "";
      errorBox.hidden = !error;
    }

    function closeManager() {
      manager.hidden = true;
      manager.innerHTML = "";
      errorBox = null;
      drawChips();
    }

    function openManager() {
      manager.hidden = false;
      manager.innerHTML = "";
      manager.appendChild(el("div", "nm-title", "Marks - a colour and what it means"));
      manager.appendChild(el("div", "text-muted-sm", "Loading…"));
      drawChips();
      listMarks().then(drawManager).catch(function (error) {
        manager.innerHTML = "";
        manager.appendChild(el("div", "small text-danger",
          "The marks could not be loaded: " + error.message));
      });
    }

    function editRow(row) {
      var line = el("div", "nm-row");
      var paint = el("input");
      paint.type = "color";
      paint.value = colour(row.color).toLowerCase();
      paint.title = "Colour";
      var label = el("input", "form-control form-control-sm");
      label.type = "text";
      label.maxLength = 120;
      label.value = row.label;
      label.setAttribute("aria-label", "What this colour means");
      var save = button("btn btn-sm btn-primary", "Save");
      var remove = button("btn btn-sm btn-outline-danger", null, "Remove this mark");
      remove.appendChild(el("i", "bi bi-trash"));

      if (!row.can_rename) {
        paint.disabled = true;
        label.disabled = true;
        save.hidden = true;
        line.title = "Only somebody who manages the shared lists can change this mark.";
      }
      remove.hidden = !row.can_remove;

      function commit() {
        var text = label.value.trim();
        if (!text) { fail(new Error("Give the mark a name.")); return; }
        fail(null);
        api("/api/options/" + row.id + "/", {
          method: "PATCH",
          body: JSON.stringify({label: text, color: paint.value.toUpperCase()})
        }).then(refreshAll).catch(fail);
      }

      save.addEventListener("click", commit);
      label.addEventListener("keydown", function (event) {
        // Enter here means "save this mark", not "submit the whole form".
        if (event.key === "Enter") { event.preventDefault(); commit(); }
      });
      remove.addEventListener("click", function () {
        if (!window.confirm("Remove the mark “" + row.label + "”?\n\n" +
                            "Notes that already carry it keep their colour.")) {
          return;
        }
        fail(null);
        api("/api/options/" + row.id + "/", {method: "DELETE"})
          .then(refreshAll).catch(fail);
      });

      line.appendChild(paint);
      line.appendChild(label);
      line.appendChild(save);
      line.appendChild(remove);
      return line;
    }

    function newRow() {
      var box = el("div", "mt-2 pt-2 border-top");
      box.appendChild(el("div", "nm-title", "Add a mark"));

      var paint = el("input");
      paint.type = "color";
      paint.value = PRESETS[5].toLowerCase();
      paint.title = "Colour";

      var swatches = el("div", "note-swatches");
      PRESETS.forEach(function (hex) {
        var swatch = button("note-swatch", null, hex);
        swatch.style.setProperty("--mark", hex);
        swatch.setAttribute("aria-label", "Use " + hex);
        swatch.addEventListener("click", function () { paint.value = hex.toLowerCase(); });
        swatches.appendChild(swatch);
      });
      box.appendChild(swatches);

      var line = el("div", "nm-row");
      var label = el("input", "form-control form-control-sm");
      label.type = "text";
      label.maxLength = 120;
      label.placeholder = "What it means - e.g. Urgent, Promised, Check again";
      var add = button("btn btn-sm btn-success text-nowrap", null);
      add.appendChild(el("i", "bi bi-plus-lg me-1"));
      add.appendChild(document.createTextNode("Add"));

      function create() {
        var text = label.value.trim();
        if (!text) { fail(new Error("Type what the new mark means.")); label.focus(); return; }
        fail(null);
        api("/api/options/", {
          method: "POST",
          body: JSON.stringify({group: GROUP, label: text, color: paint.value.toUpperCase()})
        }).then(function (saved) {
          return refreshAll().then(function () {
            // Chosen straight away: whoever typed it wanted to use it.
            if (saved && saved.id) { choose(String(saved.id)); }
          });
        }).catch(fail);
      }

      add.addEventListener("click", create);
      label.addEventListener("keydown", function (event) {
        if (event.key === "Enter") { event.preventDefault(); create(); }
      });
      line.appendChild(paint);
      line.appendChild(label);
      line.appendChild(add);
      box.appendChild(line);
      return box;
    }

    function drawManager(rows) {
      manager.innerHTML = "";
      manager.appendChild(el("div", "nm-title", "Marks - a colour and what it means"));
      rows.forEach(function (row) { manager.appendChild(editRow(row)); });
      manager.appendChild(newRow());

      errorBox = el("div", "small text-danger mt-1");
      errorBox.hidden = true;
      manager.appendChild(errorBox);

      manager.appendChild(el("div", "form-text",
        "Changing a mark changes it on every note that carries it. A removed " +
        "mark leaves this list, but notes that have it keep their colour."));

      var done = button("btn btn-sm btn-outline-secondary mt-2", "Done");
      done.addEventListener("click", closeManager);
      manager.appendChild(done);
    }

    pickers.push({
      update: function (rows) {
        refill(select, rows);
        drawChips();
        if (!manager.hidden) { drawManager(rows); }
      }
    });

    select.addEventListener("change", function () { drawChips(); });
    drawChips();
  }

  function scan(root) {
    (root || document).querySelectorAll("select[data-note-tag]").forEach(enhance);
  }

  document.addEventListener("DOMContentLoaded", function () { scan(document); });
  window.NoteTags = {scan: scan, refresh: refreshAll};
})();
