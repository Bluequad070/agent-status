/* Agent Status check logic (spec v0.1, section c). No framework.
 * The same rules live in scripts/check_action.py for the GitHub Action.
 * Keep the two in step when a rule changes. */
(function (root) {
  "use strict";

  var AGENTS = [
    { id: "claude-chat", name: "Claude chat", surface: "chat" },
    { id: "claude-code", name: "Claude Code", surface: "code" },
    { id: "codex", name: "Codex", surface: "code" },
    { id: "chatgpt", name: "ChatGPT", surface: "chat" },
    { id: "gemini", name: "Gemini", surface: "chat" },
    { id: "grok", name: "Grok", surface: "chat" }
  ];

  var RULES = {
    freshHours: 24,      // last_seen older than this fails
    staleHours: 72,      // older than this shows as stale X
    logDays: 7,          // last_session_log_date must be within this
    pauseSeconds: 45,    // gap between check one and check two (30 to 60)
    flapWindowMinutes: 60
  };

  var HOUR = 3600 * 1000;
  var DAY = 24 * HOUR;

  // One check of one status object. Returns { ok, stale, reasons[], ageMs }.
  function evaluate(s, now) {
    now = now || Date.now();
    var reasons = [];
    var stale = false;
    var ageMs = null;
    if (!s || typeof s !== "object") {
      return { ok: false, stale: false, reasons: ["not a JSON object"], ageMs: null };
    }
    var seen = Date.parse(s.last_seen);
    if (!s.last_seen || isNaN(seen)) {
      reasons.push("last_seen missing");
    } else {
      ageMs = now - seen;
      if (ageMs > RULES.staleHours * HOUR) {
        stale = true;
        reasons.push("last_seen older than " + RULES.staleHours + "h (stale)");
      } else if (ageMs > RULES.freshHours * HOUR) {
        reasons.push("last_seen older than " + RULES.freshHours + "h");
      }
    }
    var log = Date.parse((s.last_session_log_date || "") + "T00:00:00Z");
    if (!s.last_session_log_date || isNaN(log)) {
      reasons.push("no session log date");
    } else if (now - log > (RULES.logDays + 1) * DAY) {
      // +1 day: a date means the whole day, so 7 days back still counts.
      reasons.push("session log older than " + RULES.logDays + " days");
    }
    // An agent with no current project has no handoff to be canonical about.
    var noProject = String(s.current_project || "none").trim().toLowerCase() === "none";
    if (!noProject && s.handoff_authority !== "CANONICAL") {
      reasons.push("handoff_authority is " + (s.handoff_authority || "missing") + ", not CANONICAL");
    }
    return { ok: reasons.length === 0, stale: stale, reasons: reasons, ageMs: ageMs };
  }

  // Fetch and evaluate one agent once. Never throws.
  function checkOnce(id, base) {
    var url = (base || "") + "status/" + id + ".json?t=" + Date.now();
    return fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json().catch(function () { throw new Error("JSON did not parse"); });
      })
      .then(function (s) {
        var v = evaluate(s);
        v.status = s;
        v.at = new Date().toISOString();
        return v;
      })
      .catch(function (e) {
        return { ok: false, stale: false, reasons: ["unreachable: " + (e.message || "fetch failed")],
                 ageMs: null, status: null, at: new Date().toISOString() };
      });
  }

  // State from the two runs. first/second are evaluate() results or null.
  function combine(first, second) {
    if (!first) return "pending";
    if (!second) return first.ok ? "green" : "red";
    if (first.ok && second.ok) return "double-green";
    if (!first.ok && second.ok) return "flapping";
    return "x";
  }

  // history: [{state, at}] oldest first. Double-green, X, double-green within the
  // window marks the agent flapping (treated as X for the big light).
  function isFlapping(history, now) {
    now = now || Date.now();
    var recent = (history || []).filter(function (h) {
      return now - Date.parse(h.at) <= RULES.flapWindowMinutes * 60 * 1000;
    }).map(function (h) { return h.state; });
    for (var i = 0; i + 2 < recent.length; i++) {
      if (recent[i] === "double-green" && recent[i + 1] === "x" && recent[i + 2] === "double-green") return true;
    }
    return false;
  }

  // Worst first. Flapping counts as X for the big light.
  var RANK = { "x": 0, "flapping": 0, "red": 1, "pending": 2, "green": 3, "double-green": 4 };

  function overall(states) {
    if (!states.length) return "pending";
    var worst = states[0];
    states.forEach(function (s) { if (RANK[s] < RANK[worst]) worst = s; });
    if (worst === "flapping") return "x";
    return worst;
  }

  var LOOK = {
    "pending": { icon: "⚪", label: "Pending" },
    "green": { icon: "🟢", label: "Green, recheck coming" },
    "red": { icon: "🔴", label: "Red, recheck coming" },
    "double-green": { icon: "🟢🟢", label: "Confirmed" },
    "x": { icon: "❌", label: "Failed recheck" },
    "flapping": { icon: "🟡", label: "Flapping" }
  };

  var api = { AGENTS: AGENTS, RULES: RULES, evaluate: evaluate, checkOnce: checkOnce,
              combine: combine, isFlapping: isFlapping, overall: overall, LOOK: LOOK };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AgentCheck = api;
})(this);
