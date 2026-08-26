/*
 * JSON-driven translator for the Faruq Management System.
 * -------------------------------------------------------
 * SOURCE_LANG is the language the templates are authored in (English).
 * DEFAULT_LANG is what a first-time visitor sees (Amharic).
 *
 * FOUR passes run on every switch:
 *
 *   1. Explicit - data-i18n / data-i18n-placeholder / data-i18n-title.
 *   2. Exact    - reverse map (English string -> key), whole-string matches.
 *   3. Pattern  - regexes from en.json["_patterns"] handle strings holding
 *                 live numbers, e.g. "3 sales recorded". The target file
 *                 supplies a template with {0}, {1} placeholders.
 *   4. Dates    - Gregorian dates rendered by Django are converted to the
 *                 Ethiopian calendar.
 *
 * SAFETY: passes 2-4 walk the document with a TreeWalker but SKIP any node
 * inside .mono, .money, .num, [data-i18n-skip], <script>, <style>, <code>
 * and form inputs. Those hold user data - names, SKUs, references, amounts.
 * Matching is whole-string against a curated dictionary, so a product called
 * "Sofa" matches nothing and is left alone.
 */
(function (window, document) {
  "use strict";

  var LANGUAGES = ["en", "am"];
  var SOURCE_LANG = "en";    // language the HTML is written in
  var DEFAULT_LANG = "am";   // what a new user gets
  var STORAGE_KEY = "faruq.lang";

  /* Only genuinely untranslatable content is skipped. .mono holds SKUs and
     document references. Note that .num and .money are NOT skipped: numeric
     TABLE HEADERS carry class="num" too, and skipping them left "Revenue",
     "Profit", "Total", "Paid" and "Balance" stranded in English. Exact
     whole-string matching means the values inside those cells match nothing
     in the dictionary and are left alone regardless. */
  var SKIP_SELECTOR = "script,style,code,pre,textarea,.mono,[data-i18n-skip]";

  var dictionaries = {};
  var reverseEn = {};
  var patterns = [];
  var current = DEFAULT_LANG;

  function normalise(t) { return (t || "").replace(/\s+/g, " ").trim(); }

  function load(code) {
    if (dictionaries[code]) return Promise.resolve(dictionaries[code]);
    return fetch(window.I18N_BASE_URL + code + ".json", { cache: "no-cache" })
      .then(function (r) {
        if (!r.ok) throw new Error("Could not load " + code + ".json");
        return r.json();
      })
      .then(function (d) { dictionaries[code] = d; return d; });
  }

  function buildMaps(en) {
    reverseEn = {};
    Object.keys(en).forEach(function (k) {
      if (k.charAt(0) === "_") return;
      var v = normalise(en[k]);
      if (v) reverseEn[v.toLowerCase()] = k;
    });

    /* Count templates are written as readable English in en.json, e.g.
         "{0} sale(s) recorded"
       and the matching regex is derived from them. Writing regexes by hand
       was error-prone - a single wrong character silently disabled a rule
       with no way to notice. Markers:
         {0}  -> a number, possibly with , or . separators
         {$0} -> any run of text (money, dates, phone numbers)
         (s)  -> optional plural */
    patterns = [];
    var counts = en._counts || {};
    Object.keys(counts).forEach(function (id) {
      var tpl = counts[id];
      var order = [];
      var re = tpl
        .replace(/[.*+?^${}()|[\]\\]/g, "\\$&")   // escape everything first
        .replace(/\\\{\\\$(\d+)\\\}/g, function (m, i) {
          order.push(parseInt(i, 10)); return "(.+?)";
        })
        .replace(/\\\{(\d+)\\\}/g, function (m, i) {
          order.push(parseInt(i, 10)); return "([\\d][\\d.,]*)";
        })
        .replace(/\\\(s\\\)/g, "s?");
      try {
        patterns.push({ id: id, re: new RegExp(re, "i"), order: order });
      } catch (e) {
        console.warn("[i18n] bad count template", id, e);
      }
    });
  }

  function fill(tpl, groups) {
    return tpl.replace(/\{(\d+)\}/g, function (m, i) {
      var g = groups[parseInt(i, 10)];
      return g !== undefined ? g : m;
    });
  }

  // ---- Date conversion --------------------------------------------------
  var MONTHS_EN = {
    jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3, apr: 4, april: 4,
    may: 5, jun: 6, june: 6, jul: 7, july: 7, aug: 8, august: 8,
    sep: 9, sept: 9, september: 9, oct: 10, october: 10,
    nov: 11, november: 11, dec: 12, december: 12
  };

  var DATE_RE = new RegExp(
    "(?:(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday),\\s*)?" +
    "\\b(\\d{1,2})\\s+" +
    "(January|February|March|April|May|June|July|August|September|October|" +
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)" +
    "(?:\\s+(\\d{4}))?" +
    "(,\\s*\\d{1,2}:\\d{2})?",
    "gi"
  );

  function convertDates(text) {
    if (!window.EthiopianDate) return text;
    return text.replace(DATE_RE, function (match, weekday, day, mon, year, time) {
      var m = MONTHS_EN[mon.toLowerCase()];
      if (!m) return match;
      // A year-less date appears only in chart labels, which always sit in a
      // recent window, so the current year is a safe assumption.
      var y = year ? parseInt(year, 10) : new Date().getFullYear();
      var style = weekday ? "full" : (year ? "long" : "day");
      return window.EthiopianDate.format(y, m, parseInt(day, 10), style) + (time || "");
    });
  }

  function translateString(text, dict, wantDates) {
    var key = reverseEn[text.toLowerCase()];
    if (key && dict[key]) return dict[key];

    var counts = dict._counts || {};
    for (var i = 0; i < patterns.length; i++) {
      var p = patterns[i];
      if (!counts[p.id]) continue;
      var m = text.match(p.re);
      if (!m) continue;
      // Map captured groups back to their {n} indices, then substitute.
      var groups = [];
      p.order.forEach(function (slot, pos) { groups[slot] = m[pos + 1]; });
      var out = counts[p.id].replace(/\{\$?(\d+)\}/g, function (mm, idx) {
        var g = groups[parseInt(idx, 10)];
        return g !== undefined ? g : mm;
      });
      // A count template may only cover part of the node, so splice it back in.
      var result = text.replace(p.re, out);
      // Captured groups are inserted verbatim, so any date inside one is
      // still Gregorian at this point and needs a second pass.
      return wantDates ? convertDates(result) : result;
    }

    if (wantDates) {
      var converted = convertDates(text);
      if (converted !== text) return converted;
    }
    return null;
  }

  // ---- DOM walking ------------------------------------------------------
  function eachTextNode(fn) {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!normalise(node.nodeValue)) return NodeFilter.FILTER_REJECT;
        var p = node.parentElement;
        if (!p || p.closest(SKIP_SELECTOR)) return NodeFilter.FILTER_REJECT;
        if (p.hasAttribute("data-i18n")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n;
    while ((n = walker.nextNode())) fn(n);
  }

  function applyExplicit(dict) {
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      var key = el.getAttribute("data-i18n");
      if (!el.hasAttribute("data-i18n-original")) {
        el.setAttribute("data-i18n-original", el.textContent);
      }
      el.textContent = dict[key] || el.getAttribute("data-i18n-original");
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      var k = el.getAttribute("data-i18n-placeholder");
      if (dict[k]) el.setAttribute("placeholder", dict[k]);
    });
    document.querySelectorAll("[data-i18n-title]").forEach(function (el) {
      var k = el.getAttribute("data-i18n-title");
      if (dict[k]) el.setAttribute("title", dict[k]);
    });
  }

  function applySweep(dict) {
    eachTextNode(function (node) {
      if (node._i18nOriginal === undefined) node._i18nOriginal = node.nodeValue;
      var original = node._i18nOriginal;
      var source = normalise(original);
      var out = translateString(source, dict, true);
      if (!out) return;

      /* Rebuild the node from the normalised source rather than doing
         original.replace(source, out).

         Django templates wrap long sentences across lines, so a text node
         often reads "...system\n  settings..." while the normalised source
         reads "...system settings...". That string does not literally occur
         in the node, so replace() matched nothing and silently did nothing -
         which is why every multi-line paragraph stayed in English while
         short single-line labels translated fine.

         Leading and trailing whitespace is preserved so words either side of
         an inline <strong> or <a> do not run together. */
      var lead = original.match(/^\s*/)[0];
      var trail = original.match(/\s*$/)[0];
      node.nodeValue = lead + out + trail;
    });

    document.querySelectorAll("[placeholder]").forEach(function (el) {
      if (el.hasAttribute("data-i18n-placeholder")) return;
      if (el._i18nPh === undefined) el._i18nPh = el.getAttribute("placeholder");
      var out = translateString(normalise(el._i18nPh), dict, false);
      if (out) el.setAttribute("placeholder", out);
    });

    document.querySelectorAll("[title]").forEach(function (el) {
      if (el.hasAttribute("data-i18n-title")) return;
      if (el._i18nTitle === undefined) el._i18nTitle = el.getAttribute("title");
      var out = translateString(normalise(el._i18nTitle), dict, false);
      if (out) el.setAttribute("title", out);
    });

    // <option> text lives in a text node, but selects with a chosen value
    // also need the closed display refreshed in some browsers.
    document.querySelectorAll("select").forEach(function (sel) {
      var v = sel.value; sel.value = v;
    });
  }

  function restoreOriginals() {
    eachTextNode(function (node) {
      if (node._i18nOriginal !== undefined) node.nodeValue = node._i18nOriginal;
    });
    document.querySelectorAll("[placeholder]").forEach(function (el) {
      if (el._i18nPh !== undefined) el.setAttribute("placeholder", el._i18nPh);
    });
    document.querySelectorAll("[title]").forEach(function (el) {
      if (el._i18nTitle !== undefined) el.setAttribute("title", el._i18nTitle);
    });
  }

  function setLanguage(code) {
    if (LANGUAGES.indexOf(code) === -1) code = DEFAULT_LANG;

    var work = code === SOURCE_LANG
      ? load(SOURCE_LANG).then(function (en) {
          buildMaps(en); restoreOriginals(); applyExplicit(en); return en;
        })
      : Promise.all([load(SOURCE_LANG), load(code)]).then(function (b) {
          buildMaps(b[0]); restoreOriginals();
          applyExplicit(b[1]); applySweep(b[1]);
          return b[1];
        });

    return work.then(function (dict) {
      current = code;
      try { localStorage.setItem(STORAGE_KEY, code); } catch (e) {}

      document.documentElement.setAttribute("lang", code);
      var meta = dict._meta || {};
      document.documentElement.setAttribute("dir", meta.dir || "ltr");

      var label = document.getElementById("langLabel");
      if (label) label.textContent = meta.nativeName || code;
      document.querySelectorAll("[data-lang-option]").forEach(function (el) {
        el.classList.toggle("active", el.getAttribute("data-lang-option") === code);
      });
      document.dispatchEvent(new CustomEvent("i18n:changed", { detail: { lang: code } }));
    }).catch(function (err) { console.error("[i18n]", err); });
  }

  function stored() {
    try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }

  window.I18N = {
    setLanguage: setLanguage,
    get current() { return current; },
    languages: LANGUAGES,
    t: function (k, f) { return (dictionaries[current] || {})[k] || f || k; },
    refresh: function () {
      if (current === SOURCE_LANG) return;
      var d = dictionaries[current];
      if (d) { applyExplicit(d); applySweep(d); }
    }
  };

  document.addEventListener("DOMContentLoaded", function () {
    setLanguage(stored() || DEFAULT_LANG);

    document.querySelectorAll("[data-lang-option]").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.preventDefault();
        setLanguage(el.getAttribute("data-lang-option"));
      });
    });

    // The POS cart, customer dropdown and receipt previews are built by JS
    // after the first pass, so watch for injected DOM and re-translate.
    var timer = null;
    new MutationObserver(function () {
      clearTimeout(timer);
      timer = setTimeout(function () { window.I18N.refresh(); }, 60);
    }).observe(document.body, { childList: true, subtree: true });
  });
})(window, document);
