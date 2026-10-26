/* Live refresh without any library (works offline).
   <div data-live-url="/queue/partial/" data-live-interval="4000"> ... </div>
   <span data-live-status></span>  (optional indicator)
   Search box: <input data-live-search="/patients/search/" data-live-target="#results">  */
(function () {
  "use strict";
  var HEAD = { "X-Live-Poll": "1", "X-Requested-With": "fetch" };

  function fmtTime() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }

  function setStatus(text, bad) {
    document.querySelectorAll("[data-live-status]").forEach(function (el) {
      el.textContent = text;
      el.classList.toggle("bad", !!bad);
    });
  }

  function handle(response) {
    if (response.redirected || response.status === 401) { window.location.reload(); return null; }  // session ended
    if (response.status === 403) { setStatus("No longer allowed to view this", true); return null; }
    if (!response.ok) { throw new Error("HTTP " + response.status); }
    return response.text();
  }

  function startPolling(el) {
    var url = el.getAttribute("data-live-url");
    var interval = parseInt(el.getAttribute("data-live-interval") || "5000", 10);
    var last = el.innerHTML;
    var stopped = false;

    function tick() {
      if (stopped) { return; }
      if (document.hidden) { return schedule(); }
      fetch(url, { headers: HEAD, credentials: "same-origin", cache: "no-store" })
        .then(handle)
        .then(function (html) {
          if (html === null) { stopped = true; return; }
          if (html !== last) { el.innerHTML = html; last = html; }
          setStatus("Live. Updated " + fmtTime(), false);
        })
        .catch(function () { setStatus("Connection lost. Retrying...", true); })
        .then(function () { if (!stopped) { schedule(); } });
    }
    function schedule() { window.setTimeout(tick, interval); }
    document.addEventListener("visibilitychange", function () { if (!document.hidden) { tick(); } });
    schedule();
  }

  function startSearch(input) {
    var url = input.getAttribute("data-live-search");
    var target = document.querySelector(input.getAttribute("data-live-target"));
    var timer = null, controller = null;
    input.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        if (controller) { controller.abort(); }
        controller = new AbortController();
        fetch(url + "?q=" + encodeURIComponent(input.value), { headers: HEAD, credentials: "same-origin", signal: controller.signal })
          .then(handle)
          .then(function (html) { if (html !== null) { target.innerHTML = html; } })
          .catch(function () { /* keep the old results; the normal Search button still works */ });
      }, 250);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-live-url]").forEach(startPolling);
    document.querySelectorAll("[data-live-search]").forEach(startSearch);
  });
})();
