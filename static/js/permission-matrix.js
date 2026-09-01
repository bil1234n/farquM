/**
 * Permission grid behaviour.
 *
 * Everything here is convenience. The server recomputes the whole set from the
 * submitted checkboxes and re-derives which are grants and which are denials,
 * so nothing this file does can widen someone's access - the worst a broken
 * script can do is make the page less pleasant to use.
 *
 * Four jobs:
 *   1. Keep the "n of m" counters honest as boxes are ticked.
 *   2. Relabel a row live - "From role" becomes "Removed" the moment you
 *      untick it - so the consequence of a click is visible before saving.
 *   3. Group-level All / None buttons.
 *   4. A filter box, because 42 permissions is more than anyone scans.
 */
(function () {
  "use strict";

  var form = document.getElementById("accessForm");
  if (!form) return;

  var boxes = Array.prototype.slice.call(
    form.querySelectorAll('.perm-matrix input[type="checkbox"][name="perm"]')
  );
  if (!boxes.length) return;

  var tally = document.getElementById("permTally");
  var tallyBar = document.getElementById("permTallyBar");
  var search = document.getElementById("permSearch");
  var matchRole = document.getElementById("permResetToRole");

  function stateFor(box) {
    var inRole = box.dataset.inRole === "1";
    if (box.checked) return inRole ? "inherited" : "granted";
    return inRole ? "denied" : "absent";
  }

  var STATE_TEXT = {
    inherited: "From role",
    granted: "Added",
    denied: "Removed",
    absent: "—"
  };

  function paint(box) {
    var row = box.closest(".perm-row");
    if (!row) return;
    var state = stateFor(box);
    row.className = row.className.replace(/state-\w+/, "state-" + state);
    var pill = row.querySelector("[data-state-for]");
    if (pill) {
      pill.className = "pill pill-" + state;
      pill.textContent = STATE_TEXT[state];
    }
  }

  function refreshCounts() {
    var total = 0;
    var perGroup = {};

    boxes.forEach(function (box) {
      var key = box.dataset.group;
      if (!perGroup[key]) perGroup[key] = { on: 0, all: 0 };
      perGroup[key].all += 1;
      if (box.checked) {
        perGroup[key].on += 1;
        total += 1;
      }
    });

    Object.keys(perGroup).forEach(function (key) {
      var el = document.querySelector('[data-group-count="' + key + '"]');
      if (el) el.textContent = perGroup[key].on + "/" + perGroup[key].all;
    });

    if (tally) tally.textContent = total;
    if (tallyBar) {
      tallyBar.style.width = Math.round((total / boxes.length) * 100) + "%";
    }
  }

  boxes.forEach(function (box) {
    box.addEventListener("change", function () {
      paint(box);
      refreshCounts();
    });
  });

  // -- Group All / None ------------------------------------------------------
  form.querySelectorAll("[data-perm-bulk]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var on = btn.dataset.permBulk === "on";
      var key = btn.dataset.target;
      var targets = boxes.filter(function (b) {
        return b.dataset.group === key && !b.disabled && isVisible(b);
      });

      // Granting a whole group at once is the one bulk action that can hand
      // over a void or a write-off without the person noticing what they
      // clicked, so name the risky ones before doing it.
      if (on) {
        var risky = targets.filter(function (b) {
          var row = b.closest(".perm-row");
          return row && row.classList.contains("is-sensitive") && !b.checked;
        });
        if (risky.length) {
          var names = risky
            .map(function (b) {
              var label = b.closest(".perm-row").querySelector(".perm-label");
              return "• " + (label ? label.textContent.trim() : b.value);
            })
            .join("\n");
          if (
            !window.confirm(
              "This also grants " +
                risky.length +
                " sensitive permission" +
                (risky.length === 1 ? "" : "s") +
                ":\n\n" +
                names +
                "\n\nContinue?"
            )
          ) {
            return;
          }
        }
      }

      targets.forEach(function (b) {
        b.checked = on;
        paint(b);
      });
      refreshCounts();
    });
  });

  // -- Match the role exactly -----------------------------------------------
  if (matchRole) {
    matchRole.addEventListener("click", function () {
      boxes.forEach(function (b) {
        if (b.disabled) return;
        b.checked = b.dataset.inRole === "1";
        paint(b);
      });
      refreshCounts();
    });
  }

  // -- Filter ----------------------------------------------------------------
  function isVisible(box) {
    var row = box.closest(".perm-row");
    return row && row.style.display !== "none";
  }

  if (search) {
    search.addEventListener("input", function () {
      var term = search.value.trim().toLowerCase();
      form.querySelectorAll(".perm-group").forEach(function (group) {
        var anyShown = false;
        group.querySelectorAll(".perm-row").forEach(function (row) {
          var hit = !term || row.textContent.toLowerCase().indexOf(term) !== -1;
          row.style.display = hit ? "" : "none";
          if (hit) anyShown = true;
        });
        group.style.display = anyShown ? "" : "none";
      });
    });
  }

  refreshCounts();
})();
