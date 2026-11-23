#!/usr/bin/env python3
"""Twice-check every agent endpoint and write results.json (spec v0.1, section c).

Runs in the GitHub Action every 30 minutes. Same rules as check.js; keep them in step.

Env:
  STATUS_BASE     site to check (default https://bluequad070.github.io/agent-status/)
  PAUSE_SECONDS   gap between check one and check two (30 to 60, default 45)
  GOOGLE_SA_KEY   optional. A Google service account's JSON key. With INDEX_DOC_ID,
  INDEX_DOC_ID    the Action reads the private Project Index (shared view-only with
                  that service account) and gives chat agents' status files the
                  date of their newest dated log line. Only dates are written out;
                  Index text is never printed or saved.
  INDEX_TXT_URL   optional fallback: a plain-text export URL of the Index.
  Neither set: the Index step is skipped.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

AGENTS = ["claude-chat", "claude-code", "codex", "chatgpt", "gemini", "grok"]
CHAT_AGENTS = ["claude-chat", "chatgpt", "gemini", "grok"]
FRESH_HOURS, STALE_HOURS, LOG_DAYS, FLAP_WINDOW_MIN = 24, 72, 7, 60
HISTORY_KEEP_HOURS = 3
RANK = {"x": 0, "flapping": 0, "red": 1, "pending": 2, "green": 3, "double-green": 4}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def evaluate(s, now):
    """One check of one status object -> {ok, stale, reasons}."""
    if not isinstance(s, dict):
        return {"ok": False, "stale": False, "reasons": ["not a JSON object"]}
    reasons, stale = [], False
    seen = parse_iso(s.get("last_seen"))
    if seen is None:
        reasons.append("last_seen missing")
    else:
        age = now - seen
        if age > timedelta(hours=STALE_HOURS):
            stale = True
            reasons.append("last_seen older than %dh (stale)" % STALE_HOURS)
        elif age > timedelta(hours=FRESH_HOURS):
            reasons.append("last_seen older than %dh" % FRESH_HOURS)
    try:
        log = datetime.strptime(s.get("last_session_log_date") or "", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # +1 day: a date means the whole day, so 7 days back still counts.
        if now - log > timedelta(days=LOG_DAYS + 1):
            reasons.append("session log older than %d days" % LOG_DAYS)
    except ValueError:
        reasons.append("no session log date")
    # An agent with no current project has no handoff to be canonical about.
    no_project = (s.get("current_project") or "none").strip().lower() == "none"
    if not no_project and s.get("handoff_authority") != "CANONICAL":
        reasons.append("handoff_authority is %s, not CANONICAL" % (s.get("handoff_authority") or "missing"))
    return {"ok": not reasons, "stale": stale, "reasons": reasons}


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "agent-status-check", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def check_once(base, agent):
    url = "%sstatus/%s.json?t=%d" % (base, agent, int(time.time()))
    try:
        code, body = fetch(url)
        if code != 200:
            raise ValueError("HTTP %d" % code)
        try:
            s = json.loads(body)
        except ValueError:
            raise ValueError("JSON did not parse")
        v = evaluate(s, now_utc())
        v["status"] = s
    except Exception as e:  # noqa: BLE001 - any failure is a failed check
        msg = getattr(e, "code", None)
        v = {"ok": False, "stale": False, "status": None,
             "reasons": ["unreachable: %s" % ("HTTP %d" % msg if msg else e)]}
    v["at"] = iso(now_utc())
    return v


def combine(first, second):
    if not first:
        return "pending"
    if not second:
        return "green" if first["ok"] else "red"
    if first["ok"] and second["ok"]:
        return "double-green"
    if not first["ok"] and second["ok"]:
        return "flapping"
    return "x"


def is_flapping(history, now):
    recent = [h["state"] for h in history
              if now - parse_iso(h["at"]) <= timedelta(minutes=FLAP_WINDOW_MIN)]
    return any(recent[i:i + 3] == ["double-green", "x", "double-green"] for i in range(len(recent) - 2))


def overall(states):
    if not states:
        return "pending"
    worst = min(states, key=lambda s: RANK[s])
    return "x" if worst == "flapping" else worst


# --- Index log parsing (chat agents) -------------------------------------------

LOG_LINE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:—|–|-|:)\s*([^:\n]{1,60}?):")


def agent_for_name(name):
    n = name.lower()
    if "code" in n or "codex" in n:
        return None  # code agents write their own files
    if "chatgpt" in n:
        return "chatgpt"
    if "gemini" in n:
        return "gemini"
    if "grok" in n:
        return "grok"
    if "claude" in n:
        return "claude-chat"
    return None


def newest_log_lines(text):
    """agent -> (date, sha1 of its newest dated log line). The line text itself is never kept."""
    newest = {}
    for line in text.splitlines():
        m = LOG_LINE.search(line)
        if not m:
            continue
        date, name = m.group(1), m.group(2)
        agent = agent_for_name(name)
        if agent and date >= newest.get(agent, ("", ""))[0]:
            newest[agent] = (date, hashlib.sha1(line.strip().encode()).hexdigest()[:12])
    return newest


READY_MINUTES = 30  # spec v0.2: a chat agent shows ready for 30 minutes after a new Index line


def update_chat_agents(text, now):
    """New Index line -> that chat agent is ready for 30 minutes. Returns changed ids."""
    changed = []
    newest = newest_log_lines(text)
    for agent in CHAT_AGENTS:
        path = os.path.join(ROOT, "status", agent + ".json")
        try:
            with open(path) as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        before = json.dumps(s, sort_keys=True)
        if agent in newest:
            date, digest = newest[agent]
            if digest != s.get("last_log_hash"):
                s["last_log_hash"] = digest
                s["last_session_log_date"] = max(date, s.get("last_session_log_date") or "")
                s["last_seen"] = iso(now)  # when the Action first saw the new log line
                s["writer"] = "action"
                s["activity"] = "ready"
                s["ready_note"] = "New note in the Project Index"
                s["updated_at"] = iso(now)
                s["checks"] = [c for c in s.get("checks", []) if c.get("id") != "wrote_session_log"]
                s["checks"].append({"id": "wrote_session_log", "ok": True, "at": iso(now)})
                s["note"] = "Action saw a new Index log line dated %s." % date
        if s.get("activity") == "ready":
            since = parse_iso(s.get("updated_at"))
            if since is None or now - since > timedelta(minutes=READY_MINUTES):
                s["activity"] = "idle"
                s["ready_note"] = ""
                s["updated_at"] = iso(now)
        s.setdefault("activity", "idle")
        s.setdefault("speaking", False)
        if json.dumps(s, sort_keys=True) != before:
            with open(path, "w") as f:
                json.dump(s, f, indent=2)
                f.write("\n")
            changed.append(agent)
    return changed


def read_index_text():
    """Index as plain text, or None when no access is configured."""
    key = os.environ.get("GOOGLE_SA_KEY", "").strip()
    doc_id = os.environ.get("INDEX_DOC_ID", "").strip()
    if key and doc_id:
        from google.oauth2 import service_account  # installed by the workflow
        from google.auth.transport.requests import AuthorizedSession
        creds = service_account.Credentials.from_service_account_info(
            json.loads(key), scopes=["https://www.googleapis.com/auth/drive.readonly"])
        r = AuthorizedSession(creds).get(
            "https://www.googleapis.com/drive/v3/files/%s/export" % doc_id,
            params={"mimeType": "text/plain"}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError("Drive export HTTP %d" % r.status_code)
        return r.text
    url = os.environ.get("INDEX_TXT_URL", "").strip()
    if url:
        return fetch(url, timeout=30)[1].decode("utf-8", "replace")
    return None


# --- main ----------------------------------------------------------------------

def main():
    base = os.environ.get("STATUS_BASE", "https://bluequad070.github.io/agent-status/")
    if not base.endswith("/"):
        base += "/"
    pause = min(60, max(30, int(os.environ.get("PAUSE_SECONDS", "45"))))
    results_path = os.path.join(ROOT, "results.json")
    try:
        with open(results_path) as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = {}

    index_note = "skipped: no Index access configured"
    try:
        text = read_index_text()
        if text is not None:
            changed = update_chat_agents(text, now_utc())
            index_note = "ok: updated %s" % (", ".join(changed) if changed else "nothing")
    except Exception as e:  # noqa: BLE001 - never print Index content, only the error type
        index_note = "failed: %s" % (getattr(e, "code", None) or type(e).__name__)

    first = {a: check_once(base, a) for a in AGENTS}
    time.sleep(pause)
    second = {a: check_once(base, a) for a in AGENTS}
    now = now_utc()

    history = previous.get("history", {})
    agents_out = {}
    for a in AGENTS:
        state = combine(first[a], second[a])
        h = [x for x in history.get(a, []) if now - parse_iso(x["at"]) <= timedelta(hours=HISTORY_KEEP_HOURS)]
        h.append({"state": state, "at": iso(now)})
        history[a] = h
        if is_flapping(h, now):
            state = "flapping"
        s = second[a].get("status") or first[a].get("status") or {}
        agents_out[a] = {
            "state": state,
            "first": {k: first[a][k] for k in ("ok", "stale", "reasons", "at")},
            "second": {k: second[a][k] for k in ("ok", "stale", "reasons", "at")},
            "last_seen": s.get("last_seen", ""),
            "current_project": s.get("current_project", ""),
        }

    out = {
        "checked_at": iso(now),
        "base": base,
        "pause_seconds": pause,
        "overall": overall([v["state"] for v in agents_out.values()]),
        "index_parse": index_note,
        "agents": agents_out,
        "history": history,
        "version": "0.1",
    }
    with open(results_path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print("overall:", out["overall"], "|", index_note)
    for a in AGENTS:
        print(" ", a, agents_out[a]["state"], "; ".join(agents_out[a]["second"]["reasons"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
