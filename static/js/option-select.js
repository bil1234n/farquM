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

    function sync() {
      var value = select.value;
      var row = rows.filter(function (r) { return String(r.id) === value; })[0];
      if (idField) { idField.value = row ? row.id : ""; }
      if (nameField) { nameField.value = row ? row.label : ""; }
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

    function openAdder() {
      adder.hidden = false;
      newInput.value = "";
      newInput.focus();
    }

    function closeAdder() {
      adder.hidden = true;
      fail("");
      if (select.value === ADD) { select.value = ""; sync(); }
    }

    function save() {
      var label = newInput.value.trim();
      if (!label) { fail("Type the name you want to add."); return; }
      fail("");
      api("/api/options/", {
        method: "POST",
        body: JSON.stringify({group: group, label: label})
      }).then(function (created) {
        adder.hidden = true;
        broadcast(group);
        /* Select it the moment the list comes back, so the person who typed
           it does not have to find it again. */
        load(group, false).then(function () {
          window.setTimeout(function () {
            select.value = String(created.id);
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
      if (select.value === ADD) { openAdder(); } else { adder.hidden = true; sync(); }
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

  function scan(root) {
    (root || document).querySelectorAll(".option-select").forEach(build);
  }

  document.addEventListener("DOMContentLoaded", function () { scan(document); });
  // Rows added after load (the "add more damage" button) enhance themselves.
  window.OptionSelect = {scan: scan, refresh: broadcast};
})();
