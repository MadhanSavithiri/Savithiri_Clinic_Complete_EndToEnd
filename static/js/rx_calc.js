/* Works out "Total quantity to supply" from Dose x Frequency x Duration. Same rules as apps/clinical/rxcalc.py.
   The doctor can always type their own number; once they do, this stops overwriting it. */
(function () {
  "use strict";
  var SCALE = 1000;
  var FRACTION = { "\u00bd": 500, "\u00bc": 250, "\u00be": 750 };
  var NUM = "[0-9\u00bd\u00bc\u00be][0-9\u00bd\u00bc\u00be./]*";
  var PATTERN = new RegExp("^\\s*(" + NUM + "(?:\\s*[-\u2013]\\s*" + NUM + "){1,3})\\s*$");
  var WORDS = [
    [/^(once( a| per)? day|once daily|daily|od|qd)$/, 1000],
    [/^(twice( a| per)? day|twice daily|bd|bid)$/, 2000],
    [/^(three times( a| per)? day|three times daily|thrice( daily)?|tds|tid)$/, 3000],
    [/^(four times( a| per)? day|four times daily|qid|qds)$/, 4000],
    [/^(at bedtime|bedtime|hs|at night|night|nocte)$/, 1000]
  ];
  var EVERY = /^every\s+(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)$/;
  var DOSE = new RegExp("^\\s*(?:(\\d+)\\s*([\u00bd\u00bc\u00be])|(\\d+(?:\\.\\d+)?(?:/\\d+)?|[\u00bd\u00bc\u00be]))");
  var DURATION = /^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$/;
  var UNIT_DAYS = { "": 1, d: 1, day: 1, days: 1, w: 7, wk: 7, wks: 7, week: 7, weeks: 7, mo: 30, mos: 30, month: 30, months: 30 };

  function scaled(token) {
    var t = token.trim();
    if (FRACTION[t] !== undefined) { return FRACTION[t]; }
    if (t.indexOf("/") >= 0) {
      var p = t.split("/"), top = parseFloat(p[0]), bottom = parseFloat(p[1]);
      if (isNaN(top) || isNaN(bottom) || bottom === 0) { return null; }
      return Math.floor((top * SCALE) / bottom + 0.5);
    }
    var v = parseFloat(t);
    return isNaN(v) ? null : Math.floor(v * SCALE + 0.5);
  }
  function dosesPerDay(frequency) {
    var text = (frequency || "").toLowerCase().replace(/\./g, "").trim().split(/\s+/).join(" ");
    if (!text) { return null; }
    var m = PATTERN.exec(text);
    if (m) {
      var parts = m[1].split(/[-\u2013]/).map(scaled), sum = 0;
      for (var i = 0; i < parts.length; i++) { if (parts[i] === null) { return null; } sum += parts[i]; }
      return sum;
    }
    for (var k = 0; k < WORDS.length; k++) { if (WORDS[k][0].test(text)) { return WORDS[k][1]; } }
    var e = EVERY.exec(text);
    if (e && parseFloat(e[1]) > 0) { return Math.floor(24000 / parseFloat(e[1]) + 0.5); }
    return null;
  }
  function doseAmount(dose) {
    var m = DOSE.exec(dose || "");
    if (!m) { return SCALE; }
    if (m[1]) { return parseInt(m[1], 10) * SCALE + FRACTION[m[2]]; }
    return scaled(m[3]) || SCALE;
  }
  function durationDays(duration) {
    var m = DURATION.exec((duration || "").toLowerCase());
    if (!m || UNIT_DAYS[m[2]] === undefined) { return null; }
    var days = Math.floor(parseFloat(m[1]) * UNIT_DAYS[m[2]] + 0.5);
    return days || null;
  }
  function g(n) { return String(parseFloat((n / SCALE).toFixed(3))); }
  function calcQuantity(dose, frequency, duration) {
    var perDay = dosesPerDay(frequency), days = durationDays(duration);
    if (perDay === null) { return { quantity: null, explanation: "Enter the quantity yourself: this frequency cannot be counted automatically." }; }
    if (days === null) { return { quantity: null, explanation: "Enter the duration (for example 5 days) to calculate the quantity." }; }
    var amount = doseAmount(dose);
    var total = Math.ceil((perDay * amount * days) / (SCALE * SCALE));
    return { quantity: total, explanation: g(perDay) + " dose(s) a day \u00d7 " + g(amount) + " per dose \u00d7 " + days + " day(s) = " + total };
  }

  if (typeof module !== "undefined" && module.exports) { module.exports = { calcQuantity: calcQuantity }; }
  if (typeof document === "undefined") { return; }

  document.addEventListener("DOMContentLoaded", function () {
    var dose = document.getElementById("id_item_dose"), freq = document.getElementById("id_item_frequency");
    var dur = document.getElementById("id_item_duration"), qty = document.getElementById("id_item_quantity");
    var note = document.getElementById("qty-explain");
    if (!dose || !freq || !dur || !qty) { return; }
    var manual = qty.value !== "", lastAuto = null;

    function update() {
      if (manual) { return; }
      var r = calcQuantity(dose.value, freq.value, dur.value);
      if (r.quantity !== null) { qty.value = r.quantity; lastAuto = String(r.quantity); } else if (lastAuto !== null && qty.value === lastAuto) { qty.value = ""; lastAuto = null; }
      if (note) { note.textContent = (freq.value || dur.value) ? r.explanation : "The quantity is worked out from the frequency and duration. You can change it."; }
    }
    [dose, freq, dur].forEach(function (el) { el.addEventListener("input", update); el.addEventListener("change", update); });
    qty.addEventListener("input", function () {
      if (qty.value === "") { manual = false; update(); return; }
      if (qty.value !== lastAuto) { manual = true; if (note) { note.textContent = "You entered the quantity yourself."; } }
    });
    if (note) { note.textContent = manual ? "Quantity is set. Change the frequency or duration and clear this box to recalculate." : "The quantity is worked out from the frequency and duration. You can change it."; }
    update();
  });
})();
