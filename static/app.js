/* Race console. Plain browser JavaScript, no framework, no network beyond
   this Pi. Short polling rather than server-sent events: one fetch a second
   is nothing, and a poll that fails simply succeeds again a second later
   instead of leaving a dead stream open on a flaky wifi bridge. */

(function () {
  "use strict";

  var slug = document.body.dataset.slug;
  var isLive = document.body.dataset.live === "yes";
  var POLL_MS = 1000;

  var gunUtc = null;
  var clockOffsetMs = 0; // server clock minus browser clock
  var lastFinisherCount = 0;

  // ----------------------------------------------------------------
  // helpers
  // ----------------------------------------------------------------

  function el(id) { return document.getElementById(id); }

  function text(node, value) {
    if (node && node.textContent !== String(value)) { node.textContent = value; }
  }

  function flash(message, kind) {
    var box = el("flash");
    box.textContent = message;
    box.className = "notice " + (kind || "");
    box.hidden = false;
  }

  function formatElapsed(seconds) {
    if (seconds === null || seconds === undefined) { return "--:--"; }
    var hundredths = Math.floor(seconds * 100);
    var whole = Math.floor(hundredths / 100);
    hundredths = hundredths % 100;
    var s = whole % 60;
    var m = Math.floor(whole / 60) % 60;
    var h = Math.floor(whole / 3600);
    var two = function (n) { return n < 10 ? "0" + n : String(n); };
    if (h > 0) { return h + ":" + two(m) + ":" + two(s); }
    return m + ":" + two(s) + "." + two(hundredths);
  }

  // ----------------------------------------------------------------
  // the clock, ticked locally between polls
  // ----------------------------------------------------------------

  function tickClock() {
    if (gunUtc === null) {
      text(el("clock"), "--:--:--");
      return;
    }
    var nowServerMs = Date.now() + clockOffsetMs;
    var elapsed = (nowServerMs - gunUtc / 1000) / 1000;
    if (elapsed < 0) { elapsed = 0; }
    var whole = Math.floor(elapsed);
    var two = function (n) { return n < 10 ? "0" + n : String(n); };
    text(el("clock"),
      Math.floor(whole / 3600) + ":" + two(Math.floor(whole / 60) % 60) + ":" + two(whole % 60));
  }

  // ----------------------------------------------------------------
  // rendering
  // ----------------------------------------------------------------

  function renderFinishers(rows) {
    var body = el("finishers");
    if (!rows.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="4">No finishers yet.</td></tr>';
      return;
    }
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var fresh = i === 0 && rows.length > lastFinisherCount ? " fresh" : "";
      html += '<tr class="' + fresh + '"><td class="num place">' + (r.place || "") +
        '</td><td class="num">' + escapeHtml(r.bib) +
        '</td><td>' + escapeHtml(r.name) +
        '</td><td class="num time">' + escapeHtml(r.elapsed) + '</td></tr>';
    }
    body.innerHTML = html;
    lastFinisherCount = rows.length;
  }

  function renderParticipants(rows) {
    var body = el("participants");
    var onlyUnread = el("only-unread").checked;
    var shown = onlyUnread ? rows.filter(function (r) { return r.status === "not_started"; }) : rows;
    if (!shown.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">' +
        (onlyUnread ? "Everyone has been read at the start." : "No participants imported.") +
        "</td></tr>";
      return;
    }
    var html = "";
    for (var i = 0; i < shown.length; i++) {
      var r = shown[i];
      html += '<tr><td class="num">' + escapeHtml(r.bib) +
        '</td><td>' + escapeHtml(r.name) +
        '</td><td class="num">' + (r.age === null || r.age === undefined ? "" : r.age) +
        '</td><td>' + escapeHtml(r.gender || "") +
        '</td><td><span class="pill ' + r.status + '">' + r.status.replace("_", " ") +
        '</span></td><td class="num time">' + escapeHtml(r.elapsed || "") + '</td></tr>';
    }
    body.innerHTML = html;
  }

  function renderExports(names) {
    var list = el("exports");
    if (!names.length) {
      list.innerHTML = '<li class="label">none yet</li>';
      return;
    }
    var html = "";
    for (var i = 0; i < names.length; i++) {
      html += '<li><a href="/races/' + encodeURIComponent(slug) + '/exports/' +
        encodeURIComponent(names[i]) + '">' + escapeHtml(names[i]) + "</a></li>";
    }
    list.innerHTML = html;
  }

  function renderClockWarning(state, offset) {
    /* The software corrects for the offset whatever its size, so this is not
       an error. It is a sign the setup is wrong, and the operator wants to
       know that now rather than from the results afterwards. */
    var box = el("clock-warning");
    if (!state.live || !state.reader.clock_warning || offset === null) {
      box.hidden = true;
      return;
    }
    box.textContent =
      "Reader clock is " + Math.abs(offset).toFixed(1) + " s " +
      (offset > 0 ? "ahead of" : "behind") + " the Pi. Times are corrected for this, " +
      "but point the reader at the Pi's NTP server before the race: see Reader " +
      "configuration in the README.";
    box.hidden = false;
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function apply(state) {
    clockOffsetMs = state.server_now_utc / 1000 - Date.now();
    gunUtc = state.race.gun_time_utc;

    var s = state.summary;
    text(el("c-registered"), s.registered);
    text(el("c-started"), s.started);
    text(el("c-finished"), s.finished);
    text(el("c-course"), s.on_course);
    text(el("c-review"), s.review);

    var offset = state.reader.clock_offset_seconds;
    var line;
    if (!state.live) {
      line = "reader not attached, showing stored results";
    } else if (state.reader.error) {
      line = "reader error: " + state.reader.error;
    } else {
      line = state.reader.mode + ", " + state.reader.read_count + " reads logged, " +
        (offset === null || offset === undefined
          ? "clock offset not measured yet"
          : "clock offset " + (offset >= 0 ? "+" : "") + offset.toFixed(1) + " s");
    }
    text(el("reader-line"), line);
    renderClockWarning(state, offset);

    renderFinishers(state.finishers);
    renderParticipants(state.participants);
    renderExports(state.exports);

    var startButton = el("start");
    if (startButton && gunUtc !== null) {
      startButton.disabled = true;
      startButton.textContent = "RACE STARTED";
      startButton.classList.remove("go", "confirm");
    }
    tickClock();
  }

  function poll() {
    fetch("/api/races/" + encodeURIComponent(slug) + "/state", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("state " + response.status); }
        return response.json();
      })
      .then(apply)
      .catch(function (error) { flash("lost contact with the server: " + error.message, "bad"); });
  }

  // ----------------------------------------------------------------
  // controls
  // ----------------------------------------------------------------

  var startButton = el("start");
  if (startButton) {
    var armed = false;
    var armTimer = null;
    startButton.addEventListener("click", function () {
      if (!armed) {
        // Two deliberate presses. This button is the one thing on the page
        // that cannot be undone.
        armed = true;
        startButton.textContent = "CONFIRM START";
        startButton.classList.add("confirm");
        armTimer = setTimeout(function () {
          armed = false;
          startButton.textContent = "START RACE";
          startButton.classList.remove("confirm");
        }, 5000);
        return;
      }
      clearTimeout(armTimer);
      armed = false;
      startButton.disabled = true;
      fetch("/races/" + encodeURIComponent(slug) + "/start", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            flash("Gun time recorded.", "good");
          } else {
            flash(data.error, "bad");
            startButton.disabled = false;
            startButton.textContent = "START RACE";
            startButton.classList.remove("confirm");
          }
          poll();
        });
    });
  }

  el("export").addEventListener("click", function () {
    var button = el("export");
    button.disabled = true;
    fetch("/races/" + encodeURIComponent(slug) + "/export", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        button.disabled = false;
        if (data.ok) {
          flash("Exported " + data.file, "good");
          poll();
        } else {
          flash(data.error || "export failed", "bad");
        }
      })
      .catch(function (error) {
        button.disabled = false;
        flash("export failed: " + error.message, "bad");
      });
  });

  var tabs = document.querySelectorAll(".tab");
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].addEventListener("click", function (event) {
      var name = event.target.dataset.tab;
      for (var j = 0; j < tabs.length; j++) {
        tabs[j].classList.toggle("active", tabs[j] === event.target);
      }
      var panels = document.querySelectorAll(".tab-panel");
      for (var k = 0; k < panels.length; k++) {
        panels[k].hidden = panels[k].id !== "tab-" + name;
      }
    });
  }

  el("only-unread").addEventListener("change", poll);

  // Messages handed back by the form posts.
  var params = new URLSearchParams(window.location.search);
  if (params.get("error")) { flash(params.get("error"), "bad"); }
  if (params.get("imported")) { flash("Imported " + params.get("imported") + " participants.", "good"); }

  poll();
  setInterval(poll, POLL_MS);
  setInterval(tickClock, 200);
})();
