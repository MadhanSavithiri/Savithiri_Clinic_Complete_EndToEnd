/* Live BMI while the doctor types height and weight. All maths is in whole numbers (tenths) so it matches the server. */
(function () {
  "use strict";
  function tenths(text) { var v = parseFloat(text); return isNaN(v) ? null : Math.floor(v * 10 + 0.5); }

  // height in cm, weight in kg -> BMI in tenths, rounded half-up. W and H are tenths of kg and tenths of cm.
  function bmiTenths(heightCm, weightKg) {
    var H = tenths(heightCm), W = tenths(weightKg);
    if (H === null || W === null || H <= 0) { return null; }
    var num = W * 1000000, den = H * H;
    return Math.floor((2 * num + den) / (2 * den));
  }
  function category(bmiT, under, over, obese) {
    if (bmiT < under) { return { key: "under", label: "Underweight" }; }
    if (bmiT < over) { return { key: "normal", label: "Normal" }; }
    if (bmiT < obese) { return { key: "over", label: "Overweight" }; }
    return { key: "obese", label: "Obese" };
  }
  function describe(heightText, weightText, adult, cut) {
    var h = tenths(heightText), w = tenths(weightText);
    var hasH = (heightText || "").trim() !== "", hasW = (weightText || "").trim() !== "";
    if (!hasH && !hasW) { return { text: "Enter height (cm) and weight (kg) to see the BMI.", key: "" }; }
    if (hasH && !hasW) { return { text: "Enter the weight (kg) too, to see the BMI.", key: "" }; }
    if (!hasH && hasW) { return { text: "Enter the height (cm) too, to see the BMI.", key: "" }; }
    if (h === null || h < 200 || h > 2500) { return { text: "Check the height: it should be in centimetres, for example 165.", key: "" }; }
    if (w === null || w < 3 || w > 4000) { return { text: "Check the weight: it should be in kilograms, for example 62.", key: "" }; }
    var b = bmiTenths(heightText, weightText);
    var value = (b / 10).toFixed(1);
    var warn = (b < 100 || b > 600) ? " This looks unusual: please check the height and weight." : "";
    if (!adult) {
      return { text: "BMI " + value + " kg/m\u00b2. Adult categories do not apply to children and adolescents: use age- and sex-specific growth charts." + warn, key: "" };
    }
    var c = category(b, cut.under, cut.over, cut.obese);
    return { text: "BMI " + value + " kg/m\u00b2: " + c.label + " (" + cut.label + ")." + warn, key: c.key };
  }

  if (typeof module !== "undefined" && module.exports) { module.exports = { bmiTenths: bmiTenths, category: category, describe: describe }; }
  if (typeof document === "undefined") { return; }

  document.addEventListener("DOMContentLoaded", function () {
    var box = document.getElementById("bmi-live");
    var height = document.getElementById("id_vitals_height_cm"), weight = document.getElementById("id_vitals_weight_kg");
    if (!box || !height || !weight) { return; }
    var cut = {
      under: Math.round(parseFloat(box.getAttribute("data-under")) * 10), over: Math.round(parseFloat(box.getAttribute("data-over")) * 10),
      obese: Math.round(parseFloat(box.getAttribute("data-obese")) * 10), label: box.getAttribute("data-label") || "adult cut-offs"
    };
    var adult = box.getAttribute("data-adult") === "1";
    function refresh() {
      var r = describe(height.value, weight.value, adult, cut);
      box.textContent = r.text;
      box.className = "bmi-live" + (r.key ? " " + r.key : "");
    }
    height.addEventListener("input", refresh);
    weight.addEventListener("input", refresh);
    refresh();
  });
})();
