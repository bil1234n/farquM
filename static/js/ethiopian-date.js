/*
 * Ethiopian (Ge'ez) calendar conversion.
 * --------------------------------------
 * The Ethiopian year has 13 months: twelve of 30 days plus Pagume, which
 * runs 5 days (6 in a leap year). New Year falls on 11 September Gregorian
 * (12 September in the year before an Ethiopian leap year), so the Ethiopian
 * year runs roughly 7-8 years behind the Gregorian one.
 *
 * Conversion goes through the Julian Day Number, which is calendar-agnostic
 * and avoids all the month-length special cases.
 */
(function (window) {
  "use strict";

  var MONTHS = [
    "መስከረም", "ጥቅምት", "ኅዳር", "ታኅሣሥ", "ጥር", "የካቲት", "መጋቢት",
    "ሚያዝያ", "ግንቦት", "ሰኔ", "ሐምሌ", "ነሐሴ", "ጳጉሜን"
  ];

  var WEEKDAYS = ["እሑድ", "ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ"];

  // Amete Mihret epoch offset.
  var JD_EPOCH_OFFSET = 1723856;

  function gregorianToJDN(y, m, d) {
    var a = Math.floor((14 - m) / 12);
    var yy = y + 4800 - a;
    var mm = m + 12 * a - 3;
    return d + Math.floor((153 * mm + 2) / 5) + 365 * yy
      + Math.floor(yy / 4) - Math.floor(yy / 100) + Math.floor(yy / 400) - 32045;
  }

  function toEthiopian(y, m, d) {
    var jdn = gregorianToJDN(y, m, d);
    var offset = jdn - JD_EPOCH_OFFSET;
    var r = offset % 1461;
    var n = (r % 365) + 365 * Math.floor(r / 1460);

    return {
      year: 4 * Math.floor(offset / 1461) + Math.floor(r / 365) - Math.floor(r / 1460),
      month: Math.floor(n / 30) + 1,
      day: (n % 30) + 1,
      weekday: new Date(Date.UTC(y, m - 1, d)).getUTCDay()
    };
  }

  /* style: "full"  -> ቅዳሜ፣ ነሐሴ 16 ቀን 2018 ዓ.ም.
     style: "long"  -> ነሐሴ 16 ቀን 2018 ዓ.ም.
     style: "short" -> ነሐሴ 16, 2018
     style: "day"   -> ነሐሴ 16                      */
  function format(y, m, d, style) {
    var e = toEthiopian(y, m, d);
    var month = MONTHS[e.month - 1];
    if (style === "full") {
      return WEEKDAYS[e.weekday] + "፣ " + month + " " + e.day + " ቀን " + e.year + " ዓ.ም.";
    }
    if (style === "long") {
      return month + " " + e.day + " ቀን " + e.year + " ዓ.ም.";
    }
    if (style === "day") {
      // Day first: Ge'ez month names are long, and chart labels are narrow.
      // If the label truncates, the day number is what must survive.
      return e.day + " " + month;
    }
    return month + " " + e.day + ", " + e.year;
  }

  window.EthiopianDate = {
    toEthiopian: toEthiopian,
    format: format,
    MONTHS: MONTHS,
    WEEKDAYS: WEEKDAYS
  };
})(window);
