// wave-mcp viewer shell.
// - hosts the Surfer WASM app in an iframe (?load_url + ?startup_commands)
// - long-polls /api/view-state and applies desired-state deltas
// - renders annotations into the log popup (collapsible capsule)
// - writes back "actual" state (applied revision, page readiness)
//
// License isolation boundary: this Apache-2.0 shell talks to the EUPL viewer app
// ONLY through page-load URL parameters and standard window.postMessage.
// It must never reach into the viewer's JS realm: no contentWindow.eval,
// no importing viewer modules, no holding viewer function handles.
//
// Update strategy (probed on the pinned Surfer build, see dev-docs):
//   * cursor / viewport / markers  -> flicker-free runtime InjectMessage
//     (BigInt fields serialize as [sign, [u32 digits]])
//   * signals / sources change     -> iframe reboot with new startup
//     commands (~1s; AddVariables has no working runtime encoding)
//   * annotations                  -> log popup only, no Surfer traffic

(function () {
  "use strict";

  var qs = new URLSearchParams(location.search);
  var token = qs.get("token") || "";
  var frame = document.getElementById("surfer");
  var frameB = document.getElementById("surfer-b");
  var paneB = document.getElementById("pane-b");
  var panel = document.getElementById("log-panel");
  var body = document.getElementById("log-body");
  var capsule = document.getElementById("log-capsule");

  var appliedRevision = 0;
  var renderedAnnotations = {};
  var compareMode = false;
  var sourcesInfo = [];       // [{id, path, label, end_time}]

  // boot health: visible progress + bounded self-recovery
  var bootEl = document.getElementById("wv-boot");
  var bootText = document.getElementById("wv-boot-text");
  var toastEl = document.getElementById("wv-toast");
  var bootFinished = false;
  var bootFailed = false;
  var healAttempted = false;
  var signalsHintShown = false;
  var shownWarnings = {};
  var latestDesired = null;
  var latestWarnings = [];
  var pollFailStreak = 0;     // consecutive /api/view-state failures

  // ---- BigInt encoding for Surfer Message fields ----------------------

  function bigIntParts(numStr) {
    // Surfer's num::BigInt serializes as (sign, [u32 little-endian digits])
    // A malformed time (never produced by the validated tool path, but the
    // shell is also reachable over plain HTTP) must not abort the whole
    // state update: return null and let call sites skip that injection.
    try {
      var n = BigInt(numStr);
      var sign = n < 0n ? -1 : 1;
      if (n < 0n) n = -n;
      var digits = [];
      while (n > 0n) {
        digits.push(Number(n & 0xFFFFFFFFn));
        n >>= 32n;
      }
      if (digits.length === 0) digits.push(0);
      return [sign, digits];
    } catch (e) {
      console.warn("wave-mcp viewer: bad time value", numStr, e);
      return null;
    }
  }

  function injectTo(fr, obj) {
    if (!fr || !fr.contentWindow) return;
    fr.contentWindow.postMessage(
      { command: "InjectMessage", message: JSON.stringify(obj) }, "*");
  }

  function inject(obj) {                 // both panes in compare mode
    injectTo(frame, obj);
    if (compareMode) injectTo(frameB, obj);
  }

  // ---- boot: point the Surfer iframe(s) at the surver via same origin --

  function frameUrl(startupCommands) {
    var loadUrl = location.origin + "/surver/" + token;
    var url = "/index.html?load_url=" + encodeURIComponent(loadUrl);
    if (startupCommands) {
      url += "&startup_commands=" + encodeURIComponent(startupCommands);
    }
    return url;
  }

  function bootSurfer(desired) {
    var cmds = desired.startup_commands_cache || qs.get("cmds") || "";
    var sources = (desired.waveform || {}).sources || [];
    sourcesInfo = sources;
    compareMode = sources.length > 1;

    if (!compareMode) {
      paneB.style.display = "none";
      document.getElementById("label-a").style.display = "none";
      frame.src = frameUrl(cmds);
      return;
    }
    // compare mode: pane A shows source[0], pane B shows source[1]; each
    // pane strips the leading surver_select_file and prepends its own.
    paneB.style.display = "";
    var la = document.getElementById("label-a");
    var lb = document.getElementById("label-b");
    la.style.display = "";
    la.textContent = sources[0].label || sources[0].path;
    lb.textContent = sources[1].label || sources[1].path;
    var parts = cmds.split(";").filter(function (c) {
      return c.indexOf("surver_select_file") !== 0;
    });
    var rest = parts.join(";");
    frame.src = frameUrl(
      "surver_select_file " + sources[0].path + (rest ? ";" + rest : ""));
    frameB.src = frameUrl(
      "surver_select_file " + sources[1].path + (rest ? ";" + rest : ""));
  }

  // ---- log popup ----

  function mdRender(md) {
    var esc = md.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    esc = esc.replace(/^### (.*)$/gm, "<h3>$1</h3>")
             .replace(/^## (.*)$/gm, "<h2>$1</h2>")
             .replace(/^# (.*)$/gm, "<h1>$1</h1>")
             .replace(/`([^`]+)`/g, "<code>$1</code>")
             .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
             .replace(/\n/g, "<br>");
    esc = esc.replace(/\[([^\]]+)\]\(#t=([0-9]+)([a-z]+)\)/g,
      '<a class="tlink" data-t="$2" data-u="$3">$1</a>');
    esc = esc.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
    return esc;
  }

  function addAnnotation(an) {
    if (renderedAnnotations[an.id]) return false;
    renderedAnnotations[an.id] = true;
    var div = document.createElement("div");
    div.className = "log-entry";
    var conf = an.confidence
      ? ' · <span class="conf-' + an.confidence + '">' +
        an.confidence + " confidence</span>"
      : "";
    var evidence = (an.evidence && an.evidence.length)
      ? '<div class="evidence">evidence:\n  ' +
        an.evidence.join("\n  ").replace(/</g, "&lt;") + "</div>"
      : "";
    div.innerHTML =
      '<div class="meta">' + (an.timestamp || "") + conf + "</div>" +
      mdRender(an.markdown) + evidence;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
    return true;
  }

  function showPanel() {
    panel.classList.remove("collapsed");
    capsule.classList.remove("visible", "unread");
  }
  function collapsePanel() {
    panel.classList.add("collapsed");
    capsule.classList.add("visible");
  }
  document.getElementById("log-collapse").onclick = collapsePanel;
  capsule.onclick = showPanel;

  body.addEventListener("click", function (e) {
    var t = e.target;
    if (t.classList && t.classList.contains("tlink")) {
      jumpCursor(t.dataset.t);
    }
  });

  // ---- flicker-free runtime navigation --------------------------------

  function jumpCursor(time) {
    var parts = bigIntParts(time);
    if (!parts) return;      // malformed value: skip, never abort the batch
    inject({ CursorSet: parts });
    inject({ GoToTime: [parts, 0] });
  }

  // ---- navigation diffing ----------------------------------------------
  // Re-sending the whole navigation set on every update used to move the
  // window: a viewport-only update replayed cursor + GoToTime as well, and
  // its scroll overrode the requested zoom whenever the cursor sat outside
  // the new window. Only changed fields are sent now.

  function navSnapshot(desired) {
    return {
      cursor: desired.cursor || null,
      viewport: desired.viewport || null,
      markers: desired.markers || []
    };
  }

  function navDiffers(desired) {
    var a = navSnapshot(desired);
    var b = lastAppliedNav || {};
    return JSON.stringify(a.cursor) !== JSON.stringify(b.cursor || null)
        || JSON.stringify(a.viewport) !== JSON.stringify(b.viewport || null)
        || JSON.stringify(a.markers) !== JSON.stringify(b.markers || []);
  }

  function applyNavigation(desired) {
    var prev = lastAppliedNav || {};
    // cursor
    var cur = desired.cursor || null;
    if (cur && cur.time
        && JSON.stringify(cur) !== JSON.stringify(prev.cursor || null)) {
      jumpCursor(cur.time);
    }
    // viewport
    var vp = desired.viewport || null;
    if (vp && vp.from !== undefined && vp.to !== undefined
        && JSON.stringify(vp) !== JSON.stringify(prev.viewport || null)) {
      var from = bigIntParts(vp.from);
      var to = bigIntParts(vp.to);
      if (from && to) {
        inject({ ZoomToRange: { start: from, end: to, viewport_idx: 0 } });
      }
    }
    // markers: SetMarker is idempotent per id, so the full list is safe
    var marks = desired.markers || [];
    if (JSON.stringify(marks) !== JSON.stringify(prev.markers || [])) {
      for (var i = 0; i < marks.length; i++) {
        var mt = bigIntParts(marks[i].time);
        if (!mt) continue;     // skip this marker, keep the rest
        inject({ SetMarker: { id: i + 1, time: mt } });
      }
    }
    lastAppliedNav = navSnapshot(desired);
  }

  // ---- view-state long-poll --------------------------------------------

  var booted = false;
  var logEverShown = false;
  var lastSignalsKey = null;
  // cursor/viewport/markers as the frame already carries them; updates
  // only re-send fields that changed (see applyNavigation)
  var lastAppliedNav = null;

  function signalsKey(desired) {
    var src = (desired.waveform || {}).sources || [];
    return JSON.stringify([desired.signals || [],
                           src.map(function (s) { return s.path; })]);
  }

  function showError(msg) {
    var el = document.getElementById("wv-error");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("visible");
  }

  function clearError() {
    var el = document.getElementById("wv-error");
    if (el) el.classList.remove("visible");
  }

  // ---- boot health: progress, readiness probe, bounded recovery --------
  //
  // The entry page bounds its worker wait, so this page may arrive with no
  // service worker in control. The worker restores the `Server: Surfer`
  // header that gateways rewrite, which Surfer needs to detect the backend.
  // The probe below doubles as the readiness signal: it succeeds only once
  // the backend is actually answerable from this page, so the overlay can
  // honestly say "connected" instead of leaving a blank screen in silence.

  function setBootText(t, cls) {
    if (!bootText) return;
    bootText.textContent = t;
    bootText.className = cls || "";
  }

  function showToast(msg, warn) {
    if (!toastEl || !msg) return;
    toastEl.textContent = msg;
    toastEl.classList.add("visible");
    toastEl.classList.toggle("warn", !!warn);
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      toastEl.classList.remove("visible");
    }, 14000);
  }

  // Storage can be denied entirely (sandboxed IDE webviews); the recovery
  // path must keep working there, so both accesses go through these guards.
  function ssGet(key) {
    try { return sessionStorage.getItem(key); } catch (e) { return null; }
  }
  function ssSet(key, value) {
    try { sessionStorage.setItem(key, value); } catch (e) { /* no storage */ }
  }

  function surverReachable() {
    if (!token) return Promise.resolve(false);
    return fetch("/surver/" + token + "/get_status", { cache: "no-store" })
      .then(function (r) {
        var srv = (r.headers.get("server") || "").toLowerCase();
        return !!(r.ok && srv.indexOf("surfer") !== -1);
      })
      .catch(function () { return false; });
  }

  function markBootDone() {
    if (bootEl) bootEl.classList.add("done");
    if (bootFinished && !bootFailed) return;      // already done
    bootFinished = true;
    bootFailed = false;
    console.log("[shell] waveform backend reachable; viewer ready");
    postActual({ page_ready: true, page_error: null });
    checkSignalsHint();
    showNewWarnings();
  }

  function markBootFailed(msg) {
    if (bootFinished && bootFailed) return;
    bootFinished = true;
    bootFailed = true;
    if (bootEl) {
      bootEl.classList.add("stuck");
      var sp = bootEl.querySelector(".boot-spinner");
      if (sp) sp.style.display = "none";
    }
    setBootText(msg, "error");
    console.warn("[shell] waveform backend unreachable: " + msg.split("\n")[0]);
    postActual({ page_ready: false, page_error: msg });
  }

  function healFailureMessage() {
    return "The waveform backend could not be reached from this page.\n" +
           "Possible causes: the port for this URL is not forwarded, the " +
           "server was closed, or this browser blocks background workers " +
           "while a gateway rewrites the backend headers.\n" +
           "Try: reload this page once; if it stays blank, ask the agent " +
           "to reopen the view (the port may have changed).";
  }

  function bootHealOrFail() {
    if (bootFailed) return;
    var hasSW = "serviceWorker" in navigator;
    var controlled = hasSW && !!navigator.serviceWorker.controller;
    if (!hasSW || controlled || healAttempted
        || ssGet("wv_healed")) {
      markBootFailed(healFailureMessage());
      return;
    }
    healAttempted = true;
    console.log("[shell] backend not reachable; retrying via worker");
    setBootText("Connecting to the waveform backend...", "muted");
    var done = false;
    var budget = setTimeout(function () {
      if (!done) { done = true; markBootFailed(healFailureMessage()); }
    }, 6000);
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" })
      .then(function () { return navigator.serviceWorker.ready; })
      .then(function () {
        if (done) return;
        done = true;
        clearTimeout(budget);
        // A fresh navigation from an active worker IS controlled, even if
        // the current page never got claimed; reload once to pick it up.
        ssSet("wv_healed", "1");
        setBootText("Reconnecting with the background worker...", "muted");
        location.reload();
      })
      .catch(function () {
        if (done) return;
        done = true;
        clearTimeout(budget);
        markBootFailed(healFailureMessage());
      });
  }

  function bootWatchdog() {
    var t0 = Date.now();
    function tick() {
      surverReachable().then(function (ok) {
        if (ok) { markBootDone(); return; }
        if (bootFailed) {
          // failed overlay stays, but keep watching: a slow backend or a
          // restored port forward should recover the page by itself
          setTimeout(tick, 5000);
          return;
        }
        var waited = Date.now() - t0;
        if (waited >= 6000) {
          bootHealOrFail();
          setTimeout(tick, 5000);
          return;
        }
        setTimeout(tick, waited < 2500 ? 700 : 1500);
      });
    }
    setTimeout(tick, 800);
  }

  function checkSignalsHint() {
    if (signalsHintShown || !latestDesired) return;
    var sigs = latestDesired.signals || [];
    if (sigs.length > 0) return;
    signalsHintShown = true;
    showToast("No signals selected yet. Ask the agent to add them " +
              "(update_wave_view), or pick them in the signal tree.");
  }

  function showNewWarnings() {
    var fresh = [];
    for (var i = 0; i < latestWarnings.length; i++) {
      var w = String(latestWarnings[i]);
      if (shownWarnings[w]) continue;
      shownWarnings[w] = true;
      fresh.push(w);
    }
    if (!fresh.length) return;
    if (fresh.length > 3) {
      var extra = fresh.length - 3;
      fresh = fresh.slice(0, 3).concat("... and " + extra +
                                       " more (see get_view_state)");
    }
    showToast(fresh.join("\n"), true);
  }

  // Last-resort visibility: an unexpected script error must not leave a
  // silently stale page.
  window.addEventListener("error", function (e) {
    showError("viewer script error: " + (e.message || "unknown"));
  });

  function applySnapshot(snap) {
    try {
      applySnapshotInner(snap);
    } catch (e) {
      // A throw mid-apply used to drop the whole update with no visible
      // sign (the poll loop just retried and threw again). Show it and
      // keep the loop alive.
      showError("state update failed: " + e.message);
      console.warn("wave-mcp viewer: applySnapshot failed", e);
    }
  }

  function applySnapshotInner(snap) {
    // Some embedded browsers (IDE preview panes) drop the query string on
    // navigation, which used to leave the shell with an empty token, a
    // /surver/ URL that 404s and a permanently blank viewer. The server
    // reports the token with the state, so recover it here.
    if (!token && snap.token) { token = snap.token; }
    var desired = snap.desired || {};
    latestDesired = desired;
    latestWarnings = snap.warnings || [];
    var sigKey = signalsKey(desired);

    if (!booted) {
      booted = true;
      lastSignalsKey = sigKey;
      bootSurfer(desired);
      lastAppliedNav = navSnapshot(desired);
    } else if (sigKey !== lastSignalsKey) {
      // signal list / sources changed: no runtime encoding available,
      // reboot the iframe(s) with the full command set (~1s).
      lastSignalsKey = sigKey;
      bootSurfer(desired);
      lastAppliedNav = navSnapshot(desired);
    } else if (navDiffers(desired)) {
      // navigation-only change: flicker-free runtime injection of the
      // fields that actually changed.
      applyNavigation(desired);
    }

    var anns = desired.annotations || [];
    var added = 0;
    for (var i = 0; i < anns.length; i++) {
      if (addAnnotation(anns[i])) added++;
    }
    if (added > 0) {
      if (!logEverShown) {
        logEverShown = true;
        showPanel();
      } else if (panel.classList.contains("collapsed")) {
        capsule.classList.add("visible", "unread");
      } else {
        showPanel();
      }
    }
    appliedRevision = snap.revision || 0;
    if (bootFinished && !bootFailed) {
      checkSignalsHint();
      showNewWarnings();
    }
    postActual({});
  }

  function poll() {
    fetch("/api/view-state?since=" + appliedRevision)
      .then(function (r) { return r.json(); })
      .then(function (snap) {
        if (pollFailStreak >= 4) {
          // The backend was gone long enough that the waveform stream died
          // with it (sleep, VPN drop, a rebuilt port forward). The API
          // answers again, so reconnect the stream too; otherwise the page
          // stays empty even though polling recovered.
          console.log("[shell] backend back after " + pollFailStreak +
                      " failed polls; reconnecting the waveform stream");
          if (latestDesired) {
            bootSurfer(latestDesired);
            lastAppliedNav = navSnapshot(latestDesired);
          }
        }
        pollFailStreak = 0;
        applySnapshot(snap);
        clearError();
        setTimeout(poll, 200);
      })
      .catch(function () {
        // Backend unreachable (server stopped, port forward dropped): say so
        // instead of polling a dead endpoint in silence forever.
        pollFailStreak++;
        showError("viewer backend unreachable; retrying...");
        setTimeout(poll, 2000);
      });
  }

  // ---- actual write-back (bidirectional awareness) ---------------------

  function postActual(extra) {
    var payload = {
      applied_revision: appliedRevision
    };
    for (var k in extra) payload[k] = extra[k];
    fetch("/api/view-state/actual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).catch(function () { /* best effort */ });
  }

  // License isolation: the shell no longer imports viewer modules or calls
  // viewer functions (the former get_state cursor polling did exactly
  // that via contentWindow eval). User cursor readback is disabled; the
  // "actual" state carries page readiness and the applied revision only.

  // periodic heartbeat so updated_at reflects liveness
  setInterval(function () { postActual({}); }, 10000);

  // probe the backend as soon as the shell is up: this turns the overlay
  // into either "connected" or an actionable error, instead of a blank page
  bootWatchdog();

  // ---- compare-mode sync -------------------------------------------------
  // Agent-driven navigation (cursor / viewport / markers) is injected into
  // BOTH panes by inject(), so compare views stay aligned for every
  // agent-set state. Following the user's manual pane-A zoom used to rely
  // on polling the viewer's get_state from inside its JS realm; that direct
  // call crossed the license isolation boundary and has been removed.

  // initial snapshot (no ?since -> immediate return)
  fetch("/api/view-state")
    .then(function (r) { return r.json(); })
    .then(function (snap) { applySnapshot(snap); poll(); })
    .catch(function () {
      showError("viewer backend not reachable; reloading...");
      setTimeout(function () { location.reload(); }, 3000);
    });
})();
