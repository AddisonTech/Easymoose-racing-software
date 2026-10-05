/* Timing tent screen. Read only: there is nothing on this page that can
   change the race. Polls the board endpoint once a second, the same way the
   console does, and keeps showing the last good picture if a poll fails. */

(function () {
  "use strict";

  var slug = document.body.dataset.slug;
  var POLL_MS = 1000;
  var LABELS = { adult_male: "M", adult_female: "F", junior: "J" };

  var gunUtc = null;
  var stoppedUtc = null;
  var clockOffsetMs = 0; // server clock minus browser clock
  var shownCount = -1;

  function el(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function two(n) { return n < 10 ? "0" + n : String(n); }

  function tickClock() {
    var clock = el("clock");
    var label = el("clock-label");
    if (gunUtc === null) {
      clock.textContent = "--:--:--";
      label.textContent = "waiting for the start";
      return;
    }
    // Stopped races show the time the reader was stopped, not a clock that
    // keeps running after everyone has gone home.
    var endMs = stoppedUtc !== null ? stoppedUtc / 1000 : Date.now() + clockOffsetMs;
    var whole = Math.max(0, Math.floor((endMs - gunUtc / 1000) / 1000));
    clock.textContent = Math.floor(whole / 3600) + ":" + two(Math.floor(whole / 60) % 60) +
      ":" + two(whole % 60);
    label.textContent = stoppedUtc !== null ? "race clock stopped" : "race clock";
  }

  var finisherRows = [];
  // Bibs already on screen. A new finisher can land anywhere in place order,
  // so the highlight goes by bib, not by position. Nothing is highlighted on
  // the first draw, or every row would flash when the screen is opened.
  var seenBibs = null;
  var arrivedAt = {};  // bib -> when it first appeared, for the highlight
  var FRESH_MS = 6000;  // matches the arrive animation in board.css
  var rowsKey = null;

  function renderFinishers(rows) {
    el("count").textContent = rows.length ? "(" + rows.length + ")" : "";
    // Redraw only when something changed. Redrawing every poll would restart,
    // or cut short, the highlight on the newest finisher.
    var key = JSON.stringify(rows);
    if (key === rowsKey) { return; }
    rowsKey = key;
    var changed = rows.length !== shownCount;
    var now = Date.now();
    var current = {};
    for (var i = 0; i < rows.length; i++) {
      current[rows[i].bib] = true;
      if (seenBibs !== null && !seenBibs[rows[i].bib]) { arrivedAt[rows[i].bib] = now; }
    }
    for (var bib in arrivedAt) {
      if (now - arrivedAt[bib] > FRESH_MS) { delete arrivedAt[bib]; }
    }
    seenBibs = current;
    finisherRows = rows;
    shownCount = rows.length;
    if (!rows.length) {
      el("finishers").innerHTML = '<p class="empty">No finishers yet.</p>';
      return;
    }
    if (changed) {
      fitFinishers();
    } else {
      layout(currentColumns);  // a correction can change a time in place
    }
  }

  /* Crossing order, which is gun time order: the overall place, the gun
     time, and the chip time smaller beside it. Laid out as the console's
     results table, one table per column. */
  var currentColumns = 1;

  function layout(columns) {
    var rows = finisherRows;
    var perColumn = Math.ceil(rows.length / columns);
    var html = "";
    for (var c = 0; c < columns; c++) {
      html += '<table class="results"><thead><tr><th class="num">Place</th>' +
        '<th class="num">Bib</th><th>Name</th><th></th><th class="num">Time</th>' +
        '<th class="num chip">Chip</th></tr></thead><tbody>';
      for (var i = c * perColumn; i < Math.min(rows.length, (c + 1) * perColumn); i++) {
        var r = rows[i];
        // A row redrawn mid highlight picks the animation up where it was.
        var age = arrivedAt[r.bib] !== undefined ? Date.now() - arrivedAt[r.bib] : null;
        html += '<tr' + (age !== null && age < FRESH_MS
          ? ' class="fresh" style="--fresh-delay:-' + age + 'ms"' : "") + '>' +
          '<td class="num place">' + escapeHtml(r.place) + '</td>' +
          '<td class="num">' + escapeHtml(r.bib) + '</td>' +
          '<td class="name">' + escapeHtml(r.name || "Bib " + r.bib) + '</td>' +
          '<td class="cat ' + escapeHtml(r.category || "") + '">' +
          escapeHtml(LABELS[r.category] || "") + '</td>' +
          '<td class="num time">' + escapeHtml(r.time) + '</td>' +
          '<td class="num chip">' + escapeHtml(r.chip) + '</td></tr>';
      }
      html += "</tbody></table>";
    }
    el("finishers").innerHTML = html;
    currentColumns = columns;
  }

  /* The whole field has to be on screen: the largest text, and the fewest
     columns at that size, that holds every finisher. A column stays wide
     enough for a full name. */
  var MAX_FONT = 22, MIN_FONT = 10, MAX_COLUMNS = 5, COLUMN_EMS = 28;

  function fitFinishers() {
    var box = el("finishers");
    if (!finisherRows.length) { return; }
    var width = box.clientWidth;
    for (var size = MAX_FONT; size >= MIN_FONT; size--) {
      box.style.fontSize = size + "px";
      for (var columns = 1; columns <= MAX_COLUMNS; columns++) {
        if (columns > 1 && width / columns < COLUMN_EMS * size) { break; }
        layout(columns);
        if (box.scrollHeight <= box.clientHeight + 1) { return; }
      }
    }
  }

  window.addEventListener("resize", fitFinishers);

  function renderLeaders(leaders) {
    var boxes = document.querySelectorAll(".box");
    for (var i = 0; i < boxes.length; i++) {
      var rows = leaders[boxes[i].dataset.category] || [];
      var html = "";
      for (var j = 0; j < 3; j++) {
        var r = rows[j];
        html += r
          ? '<li><span class="rank">' + (j + 1) + '</span>' +
            '<span class="name">' + escapeHtml(r.name || "Bib " + r.bib) +
            ' <small>#' + escapeHtml(r.bib) + '</small></span>' +
            '<span class="time">' + escapeHtml(r.time) + '</span></li>'
          : '<li class="open"><span class="rank">' + (j + 1) + '</span><span class="name">&mdash;</span></li>';
      }
      boxes[i].querySelector(".podium").innerHTML = html;
    }
  }

  /* Sponsor logos behind the header. The list is drawn twice and slid left
     by half its width, so the loop never shows a seam. Only rebuilt when the
     sponsors change, so the scroll does not restart every poll. */
  var sponsorKey = null;
  var PIXELS_PER_SECOND = 60;

  function renderSponsors(sponsors) {
    var key = JSON.stringify(sponsors.map(function (s) { return s.url; }));
    if (key === sponsorKey) { return; }
    sponsorKey = key;
    var track = document.querySelector(".reel-track");
    track.classList.remove("rolling");
    if (!sponsors.length) {
      track.innerHTML = "";
      return;
    }
    var once = "";
    for (var i = 0; i < sponsors.length; i++) {
      once += '<img src="' + escapeHtml(sponsors[i].url) + '" alt="">';
    }
    // Enough copies to cover the header even with one small logo, then the
    // whole run twice for the seamless loop.
    var copies = Math.max(1, Math.ceil(8 / sponsors.length));
    var run = new Array(copies + 1).join(once);
    track.innerHTML = run + run;
    var images = track.querySelectorAll("img");
    var pending = images.length;
    function start() {
      var seconds = Math.max(20, track.scrollWidth / 2 / PIXELS_PER_SECOND);
      track.style.setProperty("--reel-seconds", seconds + "s");
      track.classList.add("rolling");
    }
    for (var j = 0; j < images.length; j++) {
      if (images[j].complete) { pending--; continue; }
      images[j].addEventListener("load", function () { if (--pending === 0) { start(); } });
      images[j].addEventListener("error", function () { if (--pending === 0) { start(); } });
    }
    if (pending === 0) { start(); }
  }

  function poll() {
    fetch("/api/races/" + encodeURIComponent(slug) + "/board", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("board " + response.status); }
        return response.json();
      })
      .then(function (state) {
        clockOffsetMs = state.server_now_utc / 1000 - Date.now();
        gunUtc = state.race.gun_time_utc;
        stoppedUtc = state.race.stopped_utc;
        renderFinishers(state.finishers);
        renderLeaders(state.leaders);
        renderSponsors(state.sponsors || []);
        el("offline").hidden = true;
        tickClock();
      })
      .catch(function () { el("offline").hidden = false; });
  }

  poll();
  setInterval(poll, POLL_MS);
  setInterval(tickClock, 200);
})();
