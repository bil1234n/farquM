/*
 * Managed pick-lists for the web forms.
 *
 * WHAT THIS REPLACES
 * ------------------
 * A text box. Typed free-hand, the same bank arrives as "Dashen", "dashen
 * bank", "Dashn" and "DB" - four rows that are one bank, and a report that can
 * never add them up. A fixed <select> fixes that and creates a worse problem:
 * the one answer somebody needs is missing, so they pick the nearest wrong one
 * or write it in the notes.
 *
 * So: a select that can grow. Choosing "+ Add..." opens an input in place,
 * saves the new entry to the shared list, and selects it - without leaving the
 * form. The "x" beside it takes back a mistake. Both go through
 * /api/options/, which is the same endpoint the phone uses, so a bank added at
 * the counter is in the browser's dropdown a moment later.
 *
 * USAGE
 *   <div class="option-select"
 *        data-group="BANK"
 *        data-id-field="payment_channel"      <- hidden input carrying the id
 *        data-name-field="payment_channel_name" <- and the label
 *        data-placeholder="Select a bank"></div>
 *
 * Field names ending in [] are handled too, for the repeating rows on the
 * production form.
 *
 * AN EXISTING <select> CAN BE ENHANCED INSTEAD
 *   <select name="unit" data-option-group="PRODUCT_UNIT" data-option-value="code">
 *
 * The server renders the list and the chosen value as an ordinary select, so
 * the form works with no JavaScript at all; this then adds "+ Add a new one",
 * the pencil and the bin to it in place. `data-option-value="code"` is for the
 * lists stored BY CODE - a product holds PIECE, not "Piece" - which is what
 * makes re-wording an entry re-label every product that uses it instead of
 * orphaning them.
 */
(function () {
  "use strict";

  var ADD = "__add__";
  var cache = {};       // group -> promise of rows
  var pending = {};     // group -> subscribers waiting for a refresh

  function csrf() {
    var match = document.cookie.match(/(^|;)\s*csrftoken=([^;]+)/);
    if (match) { return decodeURIComponent(match[2]); }
    var field = document.querySelector("input[name=csrfmiddlewaretoken]");
    return field ? field.value : "";
  }

  function api(url, options) {
    options = options || {};
    options.headers = Object.assign(
      {"Content-Type": "application/json", "X-CSRFToken": csrf()},
      options.headers || {}
    );
    options.credentials = "same-origin";
    return fetch(url, options).then(function (response) {
      if (response.status === 204) { return null; }
      return response.json().then(function (body) {
        if (!response.ok) {
          throw new Error((body && body.detail) || "That could not be saved.");
        }
        return body;
      });
    });
  }

  function rename(row, label) {
    return api("/api/options/" + row.id + "/", {
      method: "PATCH",
      body: JSON.stringify({label: label})
    });
  }

  function load(group, force) {
    if (!cache[group] || force) {
      cache[group] = api("/api/options/?group=" + encodeURIComponent(group));
    }
    return cache[group];
  }

  /* Everything showing this group redraws together, so an entry added in one
     row of a repeating form appears in the others without a page reload. */
  function broadcast(group) {
    load(group, true).then(function (rows) {
      (pending[group] || []).forEach(function (redraw) { redraw(rows); });
    });
  }

  function subscribe(group, redraw) {
    (pending[group] = pending[group] || []).push(redraw);
  }

  function build(host) {
    var group = host.dataset.group;
    if (!group || host.dataset.ready === "1") { return; }
    host.dataset.ready = "1";

    var idName = host.dataset.idField || "";
    var nameName = host.dataset.nameField || "";
    var placeholder = host.dataset.placeholder || "Select one";
    var initialId = host.dataset.value || "";
    var initialName = host.dataset.valueName || "";

    host.classList.add("option-select-ready");
    host.innerHTML =
      '<div class="input-group input-group-sm">' +
        '<select class="form-select form-select-sm os-select"></select>' +
        '<button type="button" class="btn btn-outline-secondary os-rename" ' +
                'title="Rename this entry" hidden>' +
          '<i class="bi bi-pencil"></i></button>' +
        '<button type="button" class="btn btn-outline-secondary os-remove" ' +
                'title="Remove this entry from the list" hidden>' +
          '<i class="bi bi-trash"></i></button>' +
      '</div>' +
      '<div class="input-group input-group-sm mt-1 os-adder" hidden>' +
        '<input type="text" class="form-control form-control-sm os-new" ' +
               'maxlength="120" placeholder="Type the new name">' +
        '<button type="button" class="btn btn-primary os-save">Save</button>' +
        '<button type="button" class="btn btn-outline-secondary os-cancel">Cancel</button>' +
      '</div>' +
      '<div class="small text-danger mt-1 os-error" hidden></div>' +
      (idName ? '<input type="hidden" class="os-id" name="' + idName + '">' : "") +
      (nameName ? '<input type="hidden" class="os-name" name="' + nameName + '">' : "");

    var select = host.querySelector(".os-select");
    var renameBtn = host.querySelector(".os-rename");
    var removeBtn = host.querySelector(".os-remove");
    var adder = host.querySelector(".os-adder");
    var newInput = host.querySelector(".os-new");
    var errorBox = host.querySelector(".os-error");
    var idField = host.querySelector(".os-id");
    var nameField = host.querySelector(".os-name");
    var rows = [];

    function fail(message) {
      errorBox.textContent = message;
      errorBox.hidden = !message;
    }

    /* What was chosen before "+ Add" was picked, so a cancelled add puts the
       select back on it instead of emptying a field somebody had filled. */
    var lastValue = "";

    function sync() {
      var value = select.value;
      if (value !== ADD) { lastValue = value; }
      var row = rows.filter(function (r) { return String(r.id) === value; })[0];
      if (idField) { idField.value = row ? row.id : ""; }
      if (nameField) { nameField.value = row ? row.label : ""; }
      // Renaming is offered more widely than removal: an entry that shipped
      // with the system can be re-worded, but taking it out is a shared-list
      // decision. The server says which - see Option.may_be_renamed_by.
      renameBtn.hidden = !(row && row.can_rename);
      removeBtn.hidden = !(row && row.can_remove);
      host.dispatchEvent(new CustomEvent("option:change", {
        bubbles: true,
        detail: {group: group, id: row ? row.id : null, label: row ? row.label : ""}
      }));
    }

    function draw(list) {
      rows = list || [];
      var keep = select.value;
      select.innerHTML = "";

      var blank = document.createElement("option");
      blank.value = "";
      blank.textContent = placeholder;
      select.appendChild(blank);

      rows.forEach(function (row) {
        var option = document.createElement("option");
        option.value = String(row.id);
        option.textContent = row.label;
        select.appendChild(option);
      });

      var add = document.createElement("option");
      add.value = ADD;
      add.textContent = "+ Add a new one…";
      select.appendChild(add);

      /* Keep what was chosen. On first draw, honour a value the server
         rendered, or match on the stored name - which is how an old record
         whose option id has since gone still shows the right label. */
      if (keep && keep !== ADD) {
        select.value = keep;
      } else if (initialId) {
        select.value = String(initialId);
        initialId = "";
      } else if (initialName) {
        var match = rows.filter(function (r) {
          return r.label.toLowerCase() === initialName.toLowerCase();
        })[0];
        if (match) { select.value = String(match.id); }
        initialName = "";
      }
      if (select.value === ADD) { select.value = ""; }
      sync();
    }

    function closeAdder() {
      adder.hidden = true;
      editing = null;
      fail("");
      if (select.value === ADD) { select.value = lastValue; sync(); }
    }

    /* The entry being re-worded, or null when the box is for a new one. */
    var editing = null;

    function openAdder(row) {
      editing = row || null;
      adder.hidden = false;
      newInput.value = row ? row.label : "";
      newInput.placeholder = row ? "The new wording" : "Type the new name";
      newInput.focus();
      newInput.select();
    }

    function save() {
      var label = newInput.value.trim();
      if (!label) { fail("Type the name you want to add."); return; }
      fail("");
      var request = editing
        ? rename(editing, label)
        : api("/api/options/", {
            method: "POST",
            body: JSON.stringify({group: group, label: label})
          });
      var wasEditing = editing;
      request.then(function (saved) {
        adder.hidden = true;
        editing = null;
        broadcast(group);
        /* Select it the moment the list comes back, so the person who typed
           it does not have to find it again. A rename keeps the same row, so
           this simply holds the selection still while the wording changes. */
        load(group, false).then(function () {
          window.setTimeout(function () {
            select.value = String((saved && saved.id) ||
                                  (wasEditing && wasEditing.id) || "");
            sync();
          }, 0);
        });
      }).catch(function (error) { fail(error.message); });
    }

    function removeCurrent() {
      var value = select.value;
      if (!value || value === ADD) { return; }
      var row = rows.filter(function (r) { return String(r.id) === value; })[0];
      if (!row) { return; }
      if (!window.confirm("Remove “" + row.label + "” from this list?\n\n" +
                          "Records that already use it keep their wording.")) {
        return;
      }
      api("/api/options/" + row.id + "/", {method: "DELETE"})
        .then(function () { select.value = ""; broadcast(group); })
        .catch(function (error) { fail(error.message); });
    }

    select.addEventListener("change", function () {
      if (select.value === ADD) {
        openAdder(null);
      } else {
        adder.hidden = true;
        editing = null;
        sync();
      }
    });
    renameBtn.addEventListener("click", function () {
      var row = rows.filter(function (r) {
        return String(r.id) === select.value;
      })[0];
      if (row) { openAdder(row); }
    });
    host.querySelector(".os-save").addEventListener("click", save);
    host.querySelector(".os-cancel").addEventListener("click", closeAdder);
    newInput.addEventListener("keydown", function (event) {
      // Enter inside a form would submit the whole thing; here it means
      // "save this entry", which is what somebody mid-typing expects.
      if (event.key === "Enter") { event.preventDefault(); save(); }
    });
    removeBtn.addEventListener("click", removeCurrent);

    subscribe(group, draw);
    load(group, false).then(draw).catch(function () {
      fail("This list could not be loaded. Reload the page to try again.");
    });
  }

  /*
   * The second shape: a real <select> the server already rendered.
   *
   * WHY NOT REPLACE IT WITH THE WIDGET ABOVE
   * ----------------------------------------
   * Because "Sold by" is part of a Django ModelForm. The server renders the
   * list and the chosen value, validates what comes back, and re-renders the
   * form with the value still selected when something else on it is wrong -
   * and all of that has to keep working with JavaScript switched off. So the
   * select stays exactly where it is and this adds the three things it cannot
   * do on its own: add, rename, remove.
   *
   * With data-option-value="code" the posted value is the CODE (PIECE), which
   * is what the product row stores - so re-wording the entry to "Each"
   * re-labels every product at once and detaches none of them.
   */
  function enhance(select) {
    var group = select.dataset.optionGroup;
    if (!group || select.dataset.osReady === "1") { return; }
    select.dataset.osReady = "1";
    var coded = select.dataset.optionValue === "code";

    var host = document.createElement("div");
    host.className = "option-select-inline";
    select.parentNode.insertBefore(host, select);

    var group_ = document.createElement("div");
    group_.className = "input-group input-group-sm";
    host.appendChild(group_);
    group_.appendChild(select);
    group_.insertAdjacentHTML("beforeend",
      '<button type="button" class="btn btn-outline-secondary os-rename" ' +
              'title="Rename this entry" hidden>' +
        '<i class="bi bi-pencil"></i></button>' +
      '<button type="button" class="btn btn-outline-secondary os-remove" ' +
              'title="Remove this entry from the list" hidden>' +
        '<i class="bi bi-trash"></i></button>');
    host.insertAdjacentHTML("beforeend",
      '<div class="input-group input-group-sm mt-1 os-adder" hidden>' +
        '<input type="text" class="form-control form-control-sm os-new" ' +
               'maxlength="120" placeholder="Type the new name">' +
        '<button type="button" class="btn btn-primary os-save">Save</button>' +
        '<button type="button" class="btn btn-outline-secondary os-cancel">Cancel</button>' +
      '</div>' +
      '<div class="small text-danger mt-1 os-error" hidden></div>');

    var renameBtn = host.querySelector(".os-rename");
    var removeBtn = host.querySelector(".os-remove");
    var adder = host.querySelector(".os-adder");
    var newInput = host.querySelector(".os-new");
    var errorBox = host.querySelector(".os-error");
    var rows = [];
    var editing = null;

    /* What the page was rendered with, kept so a value the list no longer
       offers - a unit deactivated after this product was saved - stays
       selected instead of the form quietly moving the product onto another
       unit. */
    var initial = select.value;
    var initialLabel = select.selectedOptions.length
      ? select.selectedOptions[0].textContent
      : "";

    function fail(message) {
      errorBox.textContent = message || "";
      errorBox.hidden = !message;
    }

    function current() {
      var value = select.value;
      return rows.filter(function (row) {
        return String(coded ? row.value : row.id) === value;
      })[0];
    }

    function sync() {
      var row = current();
      renameBtn.hidden = !(row && row.can_rename);
      removeBtn.hidden = !(row && row.can_remove);
    }

    function draw(list) {
      rows = list || [];
      var keep = select.value && select.value !== ADD ? select.value : initial;
      select.innerHTML = "";

      var orphan = keep;
      rows.forEach(function (row) {
        var option = document.createElement("option");
        option.value = String(coded ? row.value : row.id);
        option.textContent = row.label;
        select.appendChild(option);
        if (option.value === keep) { orphan = ""; }
      });

      if (orphan) {
        /* Kept at the end and selected: the honest answer when the entry has
           gone is to show what the record actually holds. */
        var kept = document.createElement("option");
        kept.value = orphan;
        kept.textContent = initialLabel || orphan;
        select.appendChild(kept);
      }

      var add = document.createElement("option");
      add.value = ADD;
      add.textContent = "+ Add a new one…";
      select.appendChild(add);

      select.value = keep;
      if (!select.value) { select.selectedIndex = 0; }
      sync();
    }

    function openAdder(row) {
      editing = row || null;
      adder.hidden = false;
      newInput.value = row ? row.label : "";
      newInput.placeholder = row ? "The new wording" : "Type the new name";
      newInput.focus();
      newInput.select();
    }

    function closeAdder() {
      adder.hidden = true;
      editing = null;
      fail("");
      if (select.value === ADD) { select.value = initial; sync(); }
    }

    function save() {
      var label = newInput.value.trim();
      if (!label) { fail("Type the name you want to add."); return; }
      fail("");
      var wasEditing = editing;
      var request = wasEditing
        ? rename(wasEditing, label)
        : api("/api/options/", {
            method: "POST",
            body: JSON.stringify({group: group, label: label})
          });
      request.then(function (saved) {
        adder.hidden = true;
        editing = null;
        /* A rename does not move the value - that is the whole point of a
           coded list - so only an addition changes what is selected. */
        initial = wasEditing
          ? initial
          : String(coded ? saved.value : saved.id);
        select.value = initial;
        broadcast(group);
      }).catch(function (error) { fail(error.message); });
    }

    function removeCurrent() {
      var row = current();
      if (!row) { return; }
      if (!window.confirm("Remove “" + row.label + "” from this list?\n\n" +
                          "Records that already use it keep their wording.")) {
        return;
      }
      api("/api/options/" + row.id + "/", {method: "DELETE"})
        .then(function () { initial = ""; broadcast(group); })
        .catch(function (error) { fail(error.message); });
    }

    select.addEventListener("change", function () {
      if (select.value === ADD) {
        openAdder(null);
      } else {
        adder.hidden = true;
        editing = null;
        initial = select.value;
        sync();
      }
    });
    renameBtn.addEventListener("click", function () {
      var row = current();
      if (row) { openAdder(row); }
    });
    removeBtn.addEventListener("click", removeCurrent);
    host.querySelector(".os-save").addEventListener("click", save);
    host.querySelector(".os-cancel").addEventListener("click", closeAdder);
    newInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); save(); }
    });

    subscribe(group, draw);
    load(group, false).then(draw).catch(function () {
      /* The list could not be loaded - leave the server-rendered select
         exactly as it is. A form that still saves beats a clever one that
         cannot. */
      fail("This list could not be refreshed. The choices above still work.");
    });
  }

  function scan(root) {
    root = root || document;
    root.querySelectorAll(".option-select").forEach(build);
    root.querySelectorAll("select[data-option-group]").forEach(enhance);
  }

  document.addEventListener("DOMContentLoaded", function () { scan(document); });
  // Rows added after load (the "add more damage" button) enhance themselves.
  window.OptionSelect = {scan: scan, refresh: broadcast};
})();
