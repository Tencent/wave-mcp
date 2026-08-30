// wave-mcp viewer shell.
// - hosts the Surfer WASM app in an iframe (?load_url + ?startup_commands)
// - long-polls /api/view-state and applies desired-state deltas
// - renders annotations into the log popup (collapsible capsule)
// - writes back "actual" state (cursor from get_state polling) so agents
//   can perceive the user via get_view_state (bidirectional awareness)
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
  var userDirty = false;
  var lastUserCursor = null;
  var compareMode = false;
  var sourcesInfo = [];       // [{id, path, label, end_time}]

  // ---- BigInt encoding for Surfer Message fields ----------------------

  function bigIntParts(numStr) {
    // Surfer's num::BigInt serializes as (sign, [u32 little-endian digits])
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
    inject({ CursorSet: bigIntParts(time) });
    inject({ GoToTime: [bigIntParts(time), 0] });
  }

  function applyNavigation(desired) {
    // cursor
    var cur = desired.cursor;
    if (cur && cur.time) {
      jumpCursor(cur.time);
    }
    // viewport
    var vp = desired.viewport;
    if (vp && vp.from !== undefined && vp.to !== undefined) {
      inject({ ZoomToRange: { start: bigIntParts(vp.from),
                              end: bigIntParts(vp.to),
                              viewport_idx: 0 } });
    }
    // markers: SetMarker is idempotent per id
    var marks = desired.markers || [];
    for (var i = 0; i < marks.length; i++) {
      inject({ SetMarker: { id: i + 1,
                            time: bigIntParts(marks[i].time) } });
    }
  }

  // ---- view-state long-poll --------------------------------------------

  var booted = false;
  var logEverShown = false;
  var lastSignalsKey = null;
  var lastNavKey = null;

  function signalsKey(desired) {
    var src = (desired.waveform || {}).sources || [];
    return JSON.stringify([desired.signals || [],
                           src.map(function (s) { return s.path; })]);
  }

  function navKey(desired) {
    return JSON.stringify([desired.cursor, desired.viewport,
                           desired.markers]);
  }

  function applySnapshot(snap) {
    var desired = snap.desired || {};
    var sigKey = signalsKey(desired);
    var nKey = navKey(desired);

    if (!booted) {
      booted = true;
      lastSignalsKey = sigKey;
      lastNavKey = nKey;
      bootSurfer(desired);
    } else if (sigKey !== lastSignalsKey) {
      // signal list / sources changed: no runtime encoding available,
      // reboot the iframe(s) with the full command set (~1s).
      lastSignalsKey = sigKey;
      lastNavKey = nKey;
      bootSurfer(desired);
    } else if (nKey !== lastNavKey) {
      // navigation-only change: flicker-free runtime injection.
      lastNavKey = nKey;
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
    postActual({});
  }

  function poll() {
    fetch("/api/view-state?since=" + appliedRevision)
      .then(function (r) { return r.json(); })
      .then(function (snap) {
        applySnapshot(snap);
        setTimeout(poll, 200);
      })
      .catch(function () { setTimeout(poll, 2000); });
  }

  // ---- actual write-back (bidirectional awareness) ---------------------

  function postActual(extra) {
    var payload = {
      applied_revision: appliedRevision,
      user_dirty: userDirty
    };
    for (var k in extra) payload[k] = extra[k];
    fetch("/api/view-state/actual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).catch(function () { /* best effort */ });
  }

  // Poll Surfer's full app state for the user's cursor position. The
  // pinned build has no push callback for user interactions, but
  // get_state() reflects cursor moves from user clicks; we sample at 1 Hz.
  // get_state is a module export, not on window: import surfer.js inside
  // the iframe's realm (the ES module cache returns the same instance the
  // app booted with) and cache the handle on the iframe window.
  function pollSurferCursor() {
    try {
      var w = frame.contentWindow;
      if (!w) { return schedule(); }
      if (!w.__wv_get_state) {
        if (!w.__wv_importing && w.eval) {
          w.__wv_importing = true;
          w.eval("import('./surfer.js').then(function(m){" +
                 "window.__wv_get_state = m.get_state;})" +
                 ".catch(function(){})");
        }
        return schedule();
      }
      Promise.resolve(w.__wv_get_state()).then(function (st) {
        var s = String(st);
        var m = s.match(/cursor: Some\(\((-?1), \[\s*([0-9,\s]*?)\s*\]/);
        if (m) {
          // digits are u32 little-endian chunks
          var digits = m[2].split(",").map(function (x) {
            return x.trim();
          }).filter(Boolean);
          var val = 0n;
          for (var i = digits.length - 1; i >= 0; i--) {
            val = (val << 32n) + BigInt(digits[i]);
          }
          var cur = val.toString();
          if (cur !== lastUserCursor) {
            var isFirst = lastUserCursor === null;
            lastUserCursor = cur;
            if (!isFirst) userDirty = true;
            postActual({ cursor: { time: cur, unit: "ps" } });
          }
        }
        schedule();
      }).catch(schedule);
    } catch (e) { schedule(); }
    function schedule() { setTimeout(pollSurferCursor, 1000); }
  }

  // periodic heartbeat so updated_at reflects liveness
  setInterval(function () { postActual({}); }, 10000);
  setTimeout(pollSurferCursor, 4000);

  // ---- compare-mode lockstep sync ---------------------------------------
  // Pane A is the master. Poll its viewport (relative 0..1 fractions from
  // get_state), convert to absolute time via the source's end_time, and
  // inject ZoomToRange + CursorSet into pane B when they drift. 4 Hz gives
  // smooth-enough tracking without saturating the WASM.

  function ensureGetState(w) {
    if (!w) return null;
    if (!w.__wv_get_state && !w.__wv_importing && w.eval) {
      w.__wv_importing = true;
      w.eval("import('./surfer.js').then(function(m){" +
             "window.__wv_get_state = m.get_state;})" +
             ".catch(function(){})");
    }
    return w.__wv_get_state || null;
  }

  var lastSync = null;

  function lockstepSync() {
    if (!compareMode || sourcesInfo.length < 2
        || sourcesInfo[0].end_time == null) {
      return setTimeout(lockstepSync, 1500);
    }
    var getA = ensureGetState(frame.contentWindow);
    if (!getA) return setTimeout(lockstepSync, 1000);
    Promise.resolve(getA()).then(function (st) {
      var s = String(st);
      var lm = s.match(/curr_left: \(([-0-9.e]+)\)/);
      var rm = s.match(/curr_right: \(([-0-9.e]+)\)/);
      var cm = s.match(/cursor: Some\(\((-?1), \[\s*([0-9,\s]*?)\s*\]/);
      if (lm && rm) {
        var end = sourcesInfo[0].end_time;
        var from = Math.round(parseFloat(lm[1]) * end);
        var to = Math.round(parseFloat(rm[1]) * end);
        var key = from + ":" + to + ":" + (cm ? cm[2] : "");
        if (key !== lastSync && to > from) {
          lastSync = key;
          injectTo(frameB, { ZoomToRange: {
            start: bigIntParts(String(Math.max(0, from))),
            end: bigIntParts(String(to)), viewport_idx: 0 } });
          if (cm) {
            var digits = cm[2].split(",").map(function (x) {
              return x.trim();
            }).filter(Boolean);
            var val = 0n;
            for (var i = digits.length - 1; i >= 0; i--) {
              val = (val << 32n) + BigInt(digits[i]);
            }
            injectTo(frameB, { CursorSet: bigIntParts(val.toString()) });
          }
        }
      }
      setTimeout(lockstepSync, 250);
    }).catch(function () { setTimeout(lockstepSync, 1000); });
  }
  setTimeout(lockstepSync, 5000);

  // initial snapshot (no ?since -> immediate return)
  fetch("/api/view-state")
    .then(function (r) { return r.json(); })
    .then(function (snap) { applySnapshot(snap); poll(); })
    .catch(function () { setTimeout(function () { location.reload(); }, 3000); });
})();
