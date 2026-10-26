/* Counter billing helpers (no library, works offline). Money is handled in whole paise to avoid rounding drift.
   The server recalculates everything: this only previews the total and the change to give back. */
(function () {
  "use strict";

  // Same rule as the server: each line's discount = round-half-up(line x percent), and the bill discount is their sum.
  function discountedTotal(linePaise, pctHundredths) {
    var gross = 0, discount = 0;
    linePaise.forEach(function (g) {
      gross += g;
      if (pctHundredths > 0) { discount += Math.floor((g * pctHundredths + 5000) / 10000); }
    });
    return { gross: gross, discount: discount, total: gross - discount };
  }
  function fmt(paise) { var sign = paise < 0 ? "-" : ""; paise = Math.abs(paise); return sign + Math.floor(paise / 100) + "." + ("0" + (paise % 100)).slice(-2); }
  function toPaise(text) { var v = parseFloat(text); return isNaN(v) ? 0 : Math.round(v * 100); }

  if (typeof module !== "undefined" && module.exports) { module.exports = { discountedTotal: discountedTotal, fmt: fmt, toPaise: toPaise }; }
  if (typeof document === "undefined") { return; }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("checkout");
    if (!form) { return; }
    var lines = (form.getAttribute("data-lines") || "").split(",").filter(Boolean).map(function (x) { return parseInt(x, 10); });
    var pct = document.getElementById("id_discount_percent");
    var pay1 = document.getElementById("id_pay1_amount");
    var pay2 = document.getElementById("id_pay2_amount");
    var method1 = document.getElementById("id_pay1_method");
    var given = document.getElementById("cash-given");
    var edited = !!(pay1 && pay1.dataset.edited);
    var current = { total: 0 };

    function refresh() {
      var h = pct && pct.value ? Math.round(parseFloat(pct.value) * 100) : 0;
      if (isNaN(h)) { h = 0; }
      current = discountedTotal(lines, h);
      document.getElementById("discount-amt").textContent = fmt(current.discount);
      document.getElementById("total-amt").textContent = fmt(current.total);
      if (pay1 && !edited && !(pay2 && pay2.value)) { pay1.value = fmt(current.total); }
      var cash = method1 && method1.value === "CASH" ? toPaise(pay1 && pay1.value) : 0;
      var change = given && given.value ? toPaise(given.value) - cash : 0;
      document.getElementById("change-amt").textContent = fmt(change > 0 ? change : 0);
    }
    [pct, pay2, given, method1].forEach(function (el) { if (el) { el.addEventListener("input", refresh); el.addEventListener("change", refresh); } });
    if (pay1) { pay1.addEventListener("input", function () { edited = true; pay1.dataset.edited = "1"; refresh(); }); }
    refresh();
  });
})();
