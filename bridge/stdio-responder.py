"""Windows Herdr Mobile JSONL adapter. SSH is the authentication boundary."""
import argparse
import concurrent.futures
import json
import re
import sqlite3
import subprocess
import sys
import time

from bridge_common import (HOME, OutputLimitError, discover_sessions, log,
                           now_ms, run_cli, session_cache, valid_session)
from command_store import CommandStore

BRIDGE_VERSION = "0.3.2"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 128 * 1024
MAX_PANES = 64
MAX_WORKERS = 4
MAX_READ_LINES = 2000
MAX_TEXT_CHARS = 12000
SESSION = "default"
STORE = CommandStore()
seq = 0


def send(msg_type, payload, session_id=None, in_reply_to=None, top_seq=None):
    global seq
    seq += 1
    body = dict(payload)
    if in_reply_to:
        body["requestId"] = in_reply_to
    env = {"version": 1, "id": "f-%d-%d" % (now_ms(), seq),
           "type": msg_type, "sessionId": session_id or SESSION,
           "timestamp": now_ms(), "payload": body}
    if top_seq is not None:
        env["seq"] = top_seq
    sys.stdout.write(json.dumps(env, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def checked_string(value, name, maximum=256, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value):
        raise ValueError("invalid " + name)
    if any(ord(c) < 32 for c in value):
        raise ValueError("invalid " + name)
    return value


def target(value):
    value = checked_string(value, "target")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value):
        raise ValueError("invalid target")
    return value


def validate(env):
    if not isinstance(env, dict):
        raise ValueError("frame must be an object")
    if type(env.get("version", 1)) is not int or env.get("version", 1) != 1:
        raise ValueError("unsupported protocol version")
    kind = checked_string(env.get("type"), "type", 64)
    request_id = checked_string(env.get("id", ""), "id", required=False)
    session = env.get("sessionId", SESSION)
    if session != SESSION:
        raise ValueError("frame session does not match SSH session")
    payload = env.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if kind in ("raw.read", "pane.sendText", "pane.send_text", "pane.sendKeys", "pane.send_keys",
                  "pane.fit", "pane.unfit", "pane.fitTouch"):
        target(payload.get("paneId"))
    if kind == "pane.unfitAll" and "staleOlderThanMs" in payload:
        stale = payload.get("staleOlderThanMs")
        if type(stale) is not int or not 60_000 <= stale <= 86_400_000:
            raise ValueError("staleOlderThanMs must be 60000..86400000")
    if kind == "raw.read":
        lines = payload.get("lines", 1000)
        if type(lines) is not int or not 1 <= lines <= MAX_READ_LINES:
            raise ValueError("lines must be between 1 and 2000")
        if type(payload.get("ansi", False)) is not bool:
            raise ValueError("invalid ansi flag")
    if kind == "pane.fit":
        cols = payload.get("cols")
        if type(cols) is not int or not FIT_MIN_COLS <= cols <= FIT_MAX_COLS:
            raise ValueError("cols must be between 20 and 120")
    if kind == "timeline.subscribe":
        panes = payload.get("paneIds", [])
        if not isinstance(panes, list) or len(panes) > MAX_PANES:
            raise ValueError("too many panes")
        for pane in panes:
            target(pane)
        if len(set(panes)) != len(panes):
            raise ValueError("duplicate panes")
        if type(payload.get("active", False)) is not bool:
            raise ValueError("invalid active flag")
        if type(payload.get("ansi", False)) is not bool:
            raise ValueError("invalid ansi flag")
        if payload.get("activePaneId"):
            target(payload["activePaneId"])
    if kind in ("pane.sendText", "pane.send_text"):
        text = payload.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or "\0" in text:
            raise ValueError("invalid or excessive text")
        text.encode("utf-8")  # Reject unpaired surrogates before starting a mutation.
    if kind in ("pane.sendKeys", "pane.send_keys"):
        keys = payload.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
            raise ValueError("invalid keys")
        for key in keys:
            checked_string(key, "key", 64)
            if key.startswith("-"):
                raise ValueError("invalid key")
    if kind in ("space.close", "space_close"):
        target(payload.get("spaceId"))
    if kind in MUTATIONS or kind in FIT_OPS:
        checked_string(payload.get("idempotencyKey") or request_id, "idempotency key")
    return kind, request_id, payload


def current_snapshot():
    entry = session_cache(SESSION)
    if entry is None:
        raise ValueError("session snapshot unavailable or stale")
    return entry["snapshot"]


def require_pane(pane_id, must_exist=True):
    target(pane_id)
    if must_exist and not any(p.get("pane_id") == pane_id for p in current_snapshot()["panes"]):
        raise ValueError("pane does not belong to selected session")


def pane_text(pane_id, lines, ansi=False):
    require_pane(pane_id)
    try:
        out = run_cli(["--session", SESSION, "pane", "read", pane_id,
                       "--source", "recent-unwrapped", "--lines", str(lines),
                       "--format", "ansi" if ansi else "text"], timeout=12, max_bytes=512 * 1024)
        return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired, OutputLimitError):
        log("pane_read_failed")
        return ""


FIT_MIN_COLS = 20
FIT_MAX_COLS = 120
FIT_MIN_RATIO = 0.08
FIT_MAX_RATIO = 0.9
FIT_OPS = {"pane.fit": "fit_pane", "pane.unfit": "unfit_pane", "pane.unfitAll": "unfit_all"}
# NOTE: a killed phone stops its heartbeat; the poller reaps fits older
# than this even when no client ever attaches again.
FIT_TTL_MS = 5 * 60 * 1000
NOTICE_RE = re.compile(r"[A-Za-z0-9 .,:\-()]{1,120}")
NOTICE_MARKER = "PHONE VIEW - NARROWED TO"
NOTICE_LINE_RE = re.compile(r"PHONE VIEW - NARROWED TO \d+ COLS - RESTORES WHEN YOU LEAVE")


def fit_sides(db):
    db.execute("""CREATE TABLE IF NOT EXISTS fit_sides (
        session TEXT NOT NULL, tab_id TEXT NOT NULL, side_pane TEXT NOT NULL,
        at INTEGER NOT NULL, PRIMARY KEY(session, tab_id))""")


def tab_of_pane(pane_id):
    # NOTE: the poller cache lags the desktop by seconds; tab decisions
    # read the live layout first so a fast tab switch cannot mis-record
    # a split or phantom-release it.
    live = live_pane_map()
    if live is not None:
        tab = live.get(pane_id)
        if tab:
            return tab
    for pane in current_snapshot()["panes"]:
        if pane.get("pane_id") == pane_id:
            return pane.get("tab_id")
    return None


def tab_panes(tab_id):
    return [p for p in current_snapshot()["panes"] if p.get("tab_id") == tab_id]


def tab_area_width(tab_id):
    for layout in current_snapshot().get("layouts", []):
        if layout.get("tab_id") == tab_id:
            area = layout.get("area", {})
            if type(area.get("width")) in (int, float) and area["width"] > 0:
                return area["width"]
    return None


def read_width(pane_id):
    # NOTE: recent (not unwrapped) wraps at the live pty width, so the
    # longest visual line approximates the pane's column count.
    try:
        out = run_cli(["--session", SESSION, "pane", "read", pane_id,
                       "--source", "recent", "--lines", "20",
                       "--format", "text"], timeout=12, max_bytes=256 * 1024)
        if out.returncode != 0:
            return -1
        return max([len(line) for line in out.stdout.decode("utf-8", errors="replace").splitlines()] or [0])
    except (OSError, subprocess.TimeoutExpired, OutputLimitError):
        return -1


def notice_text(cols):
    text = "%s %d COLS - RESTORES WHEN YOU LEAVE" % (NOTICE_MARKER, cols)
    if not NOTICE_RE.fullmatch(text):
        raise ValueError("invalid notice text")
    return text


def pane_snippet(pane_id, lines=6):
    try:
        out = run_cli(["--session", SESSION, "pane", "read", pane_id,
                       "--source", "recent-unwrapped", "--lines", str(lines),
                       "--format", "text"], timeout=12, max_bytes=64 * 1024)
        return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired, OutputLimitError, ValueError):
        return ""


def sweep_notice_orphans():
    # NOTE: records can be lost across a crash or reinstall, leaving a
    # notice panel no record points at. The notice line itself identifies
    # our panels. A tab always keeps its last pane, so a lone pane is
    # never closed even if it echoes the marker.
    live = live_pane_map()
    if not live:
        return [], []
    recorded = {side for _, side in all_fit_sides()}
    by_tab = {}
    for pane, tab in live.items():
        by_tab.setdefault(tab, []).append(pane)
    released, failed = [], []
    for panes in by_tab.values():
        remaining = list(panes)
        for pane in panes:
            if len(remaining) < 2:
                break
            if pane in recorded:
                continue
            if not NOTICE_LINE_RE.search(pane_snippet(pane)):
                continue
            if close_side(pane, attempts=2):
                remaining.remove(pane)
                released.append(pane)
            else:
                failed.append(pane)
    return released, failed


def touch_fit(pane_id):
    # NOTE: heartbeats never fail the client; a missed beat just lets
    # the record age toward the reap below.
    tab = tab_of_pane(pane_id)
    if not tab:
        return False
    with STORE.connect() as db:
        fit_sides(db)
        row = db.execute("SELECT side_pane FROM fit_sides WHERE session=? AND tab_id=?",
                         (SESSION, tab)).fetchone()
        if not row:
            return False
        db.execute("UPDATE fit_sides SET at=? WHERE session=? AND tab_id=?",
                   (now_ms(), SESSION, tab))
    return True


def reap_stale_fits(ttl_ms):
    # NOTE: a killed phone never sends unfit, so the poller reaps fits
    # whose heartbeat stopped over ttl_ms ago. Only recorded sides close
    # here, so a user's own splits are never at risk.
    try:
        ttl = int(ttl_ms)
    except (TypeError, ValueError):
        return [], []
    cutoff = now_ms() - max(ttl, 0)
    with STORE.connect() as db:
        fit_sides(db)
        rows = db.execute("SELECT session, tab_id, side_pane FROM fit_sides WHERE at < ?",
                          (cutoff,)).fetchall()
    released, failed = [], []
    for session, tab, side in rows:
        live = live_pane_map(session=session)
        if live is not None and side not in live:
            forget_fit_side(tab_id=tab, session=session)
            released.append(side)
        elif close_side(side, attempts=2, session=session):
            forget_fit_side(tab_id=tab, session=session)
            released.append(side)
        else:
            failed.append(side)
    return released, failed


def close_side(side_pane, attempts=1, session=None):
    for _ in range(max(attempts, 1)):
        try:
            if not cli_error(run_cli(["--session", session or SESSION, "pane", "close", side_pane], timeout=15)):
                return True
        except (OSError, subprocess.TimeoutExpired, OutputLimitError):
            pass
        time.sleep(2)
    return False


def all_fit_sides():
    with STORE.connect() as db:
        fit_sides(db)
        return db.execute("SELECT tab_id, side_pane FROM fit_sides WHERE session=?",
                          (SESSION,)).fetchall()


def forget_fit_side(tab_id=None, side_pane=None, session=None):
    session = session or SESSION
    with STORE.connect() as db:
        fit_sides(db)
        if tab_id is not None:
            db.execute("DELETE FROM fit_sides WHERE session=? AND tab_id=?", (session, tab_id))
        else:
            db.execute("DELETE FROM fit_sides WHERE session=? AND side_pane=?", (session, side_pane))


NOTICE_COMMAND = ["powershell", "-NoProfile", "-Command"]


def live_pane_map(session=None):
    # NOTE: the poller cache lags the desktop by seconds, so split/close
    # decisions read the live layout; pane list is cheap.
    try:
        out = run_cli(["--session", session or SESSION, "pane", "list"], timeout=15)
        if cli_error(out):
            return None
        panes = json.loads(out.stdout).get("result", {}).get("panes", [])
        live = {p.get("pane_id"): p.get("tab_id") for p in panes
                if p.get("pane_id") and p.get("tab_id")}
        return live or None
    except (OSError, subprocess.TimeoutExpired, OutputLimitError, ValueError):
        return None


def tab_panes_now(tab_id):
    live = live_pane_map()
    if live is not None:
        return [p for p, t in live.items() if t == tab_id]
    return tab_panes(tab_id)


def sweep_gone_sides():
    # NOTE: only tabs visibly gone lose their records. A side pane created
    # seconds ago is missing from the lagging cache, but its tab is old;
    # dropping by side would orphan live panels.
    with STORE.connect() as db:
        fit_sides(db)
        try:
            tabs = sorted({t.get("tab_id") for t in current_snapshot().get("tabs", []) if t.get("tab_id")})
        except ValueError:
            return
        if tabs:
            db.execute("DELETE FROM fit_sides WHERE session=? AND tab_id NOT IN (%s)" % ",".join("?" * len(tabs)),
                       [SESSION] + tabs)


def fit_pane(request_id, payload, kind):
    canonical = FIT_OPS[kind]
    key = payload.get("idempotencyKey") or request_id
    identity = {"paneId": payload.get("paneId"), "operation": canonical}
    if canonical == "fit_pane":
        identity["cols"] = payload.get("cols")
    side_created = None
    try:
        claimed, previous = STORE.claim(SESSION, key, identity)
        if not claimed:
            send("ack", {"duplicate": True}, in_reply_to=request_id)
            send("command.result", previous, in_reply_to=request_id)
            return
        sweep_gone_sides()
        if canonical == "unfit_all" and payload.get("staleOlderThanMs") is not None:
            # NOTE: the poller's periodic reap; read-free, records only.
            released, failed = reap_stale_fits(payload["staleOlderThanMs"])
            result = {"ok": not failed, "released": released, "failed": failed}
            if failed:
                result["error"] = "a notice panel resisted closing; not repeated"
            STORE.finish(SESSION, key, result)
            send("ack", {"duplicate": False}, in_reply_to=request_id)
            send("command.result", result, in_reply_to=request_id)
            return
        if canonical == "unfit_all":
            released, failed = [], []
            live = live_pane_map()
            for _, side in all_fit_sides():
                if live is not None and side not in live:
                    forget_fit_side(side_pane=side)
                    released.append(side)
                elif close_side(side, attempts=2):
                    forget_fit_side(side_pane=side)
                    released.append(side)
                else:
                    failed.append(side)
            orphans, orphan_failed = sweep_notice_orphans()
            released.extend(orphans)
            failed.extend(orphan_failed)
            result = {"ok": not failed, "released": released, "failed": failed}
            if failed:
                result["error"] = "a notice panel resisted closing; not repeated"
            STORE.finish(SESSION, key, result)
            send("ack", {"duplicate": False}, in_reply_to=request_id)
            send("command.result", result, in_reply_to=request_id)
            return
        pane = payload["paneId"]
        cols = payload["cols"] if canonical == "fit_pane" else None
        require_pane(pane, must_exist=(canonical == "fit_pane"))
        tab = tab_of_pane(pane)
        if not tab:
            if canonical == "unfit_pane":
                # NOTE: the pane (and probably its tab) is already gone;
                # there is nothing left to release.
                result = {"ok": True, "released": False}
                STORE.finish(SESSION, key, result)
                send("ack", {"duplicate": False}, in_reply_to=request_id)
                send("command.result", result, in_reply_to=request_id)
                return
            raise ValueError("pane has no tab in the selected session")
        stale_released = False
        with STORE.connect() as db:
            fit_sides(db)
            stale = db.execute("SELECT side_pane FROM fit_sides WHERE session=? AND tab_id=?",
                               (SESSION, tab)).fetchone()
            if stale:
                # NOTE: a side already gone needs no close; a stuck close
                # keeps its record so the next unfit retries it.
                live = live_pane_map()
                if live is not None and stale[0] not in live:
                    db.execute("DELETE FROM fit_sides WHERE session=? AND tab_id=?", (SESSION, tab))
                    stale_released = True
                elif close_side(stale[0], attempts=3):
                    db.execute("DELETE FROM fit_sides WHERE session=? AND tab_id=?", (SESSION, tab))
                    stale_released = True
                elif canonical == "fit_pane":
                    raise ValueError("previous notice panel is stuck; not repeated")
        if canonical == "unfit_pane":
            if stale and not stale_released:
                result = {"ok": False, "released": False,
                          "error": "notice panel resisted closing; not repeated"}
            else:
                result = {"ok": True, "released": stale_released}
            STORE.finish(SESSION, key, result)
            send("ack", {"duplicate": False}, in_reply_to=request_id)
            send("command.result", result, in_reply_to=request_id)
            return
        if len(tab_panes_now(tab)) != 1:
            raise ValueError("tab already has splits; close them before fitting")
        area = tab_area_width(tab)
        if area:
            ratio = (cols + 3) / area
        else:
            measured = read_width(pane)
            ratio = (cols + 3) / (max(measured, FIT_MIN_COLS) + 3)
        ratio = min(FIT_MAX_RATIO, max(FIT_MIN_RATIO, ratio))
        out = run_cli(["--session", SESSION, "pane", "split", pane,
                       "--direction", "right", "--ratio", str(round(ratio, 3))], timeout=15)
        side = None
        if not cli_error(out):
            try:
                side = json.loads(out.stdout).get("result", {}).get("pane", {}).get("pane_id")
            except ValueError:
                side = None
            if not side:
                # NOTE: split succeeded but its reply is unreadable; adopt
                # the tab's only other pane instead of orphaning a split.
                others = [p.get("pane_id") for p in tab_panes_now(tab) if p.get("pane_id") != pane]
                side = others[0] if len(others) == 1 else None
        if not side:
            raise ValueError("Herdr rejected the split; not repeated")
        side_created = side
        with STORE.connect() as db:
            fit_sides(db)
            db.execute("INSERT OR REPLACE INTO fit_sides VALUES(?,?,?,?)", (SESSION, tab, side, now_ms()))
        notice_cmd = (["--session", SESSION, "pane", "run", side] + NOTICE_COMMAND +
                      ["Write-Host '%s'; while($true){Start-Sleep 86400}" % notice_text(cols)])
        try:
            # NOTE: a print-then-sleep loop, never a shell: keystrokes
            # typed into the panel are ignored instead of executed.
            ran_ok = not cli_error(run_cli(notice_cmd, timeout=15))
            if not ran_ok:
                time.sleep(2)
                ran_ok = not cli_error(run_cli(notice_cmd, timeout=15))
        except subprocess.TimeoutExpired:
            ran_ok = True  # The notice spawn outlived the wait; the split itself is verifiable.
        if not ran_ok:
            raise ValueError("Herdr rejected the notice panel; not repeated")
        result = {"ok": True, "sidePaneId": side, "ratio": round(ratio, 3), "width": read_width(pane)}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "uncertain": True, "error": "command outcome unknown; not repeated"}
    except (OSError, ValueError) as e:
        if side_created:
            # NOTE: only forget a panel Herdr confirms closed; a stuck
            # close keeps its record so the next unfit retries it.
            if close_side(side_created, attempts=3):
                try:
                    forget_fit_side(side_pane=side_created)
                except (OSError, sqlite3.Error):
                    pass
        result = {"ok": False, "error": "command unavailable or target invalid; not repeated",
                  "reason": "%s: %s" % (type(e).__name__, str(e)[:160])}
    except sqlite3.Error:
        if side_created:
            close_side(side_created)
        result = {"ok": False, "uncertain": True, "error": "command store unavailable; command not repeated"}
    try:
        STORE.finish(SESSION, key, result)
    except (OSError, sqlite3.Error):
        result = {"ok": False, "uncertain": True, "error": "command store unavailable; command not repeated"}
    send("ack", {"duplicate": False}, in_reply_to=request_id)
    send("command.result", result, in_reply_to=request_id)


def mutation_commands(kind, payload):
    if kind == "send_text":
        text = payload["text"]
        body = text[:-1] if text.endswith("\n") else text
        commands = [["pane", "send-text", payload["paneId"], body]] if body else []
        if text.endswith("\n"):
            commands.append(["pane", "send-keys", payload["paneId"], "enter"])
        return commands
    if kind == "send_keys":
        return [["pane", "send-keys", payload["paneId"], *payload["keys"]]]
    return [["workspace", "close", payload["spaceId"]]]


def cli_error(out):
    if out.returncode:
        return True
    for stream in (out.stdout, out.stderr):
        try:
            data = json.loads(stream)
            if isinstance(data, dict) and "error" in data:
                return True
        except ValueError:
            pass
    return False


def mutate(kind, request_id, payload):
    canonical = MUTATIONS[kind]
    key = payload.get("idempotencyKey") or request_id
    identity = {k: payload[k] for k in
                (("paneId", "text") if canonical == "send_text" else
                 ("paneId", "keys") if canonical == "send_keys" else ("spaceId",))}
    identity["operation"] = canonical
    try:
        claimed, previous = STORE.claim(SESSION, key, identity)
        if not claimed:
            send("ack", {"duplicate": True}, in_reply_to=request_id)
            send("command.result", previous, in_reply_to=request_id)
            return
        # Reserve before checking targets: completed close commands must still
        # return their recorded result after the workspace disappears.
        completed = 0
        result = {"ok": True}
        try:
            if canonical == "close_space":
                if not any(w.get("workspace_id", w.get("id")) == payload["spaceId"]
                           for w in current_snapshot()["workspaces"]):
                    raise ValueError("workspace does not belong to selected session")
            else:
                require_pane(payload["paneId"])
            for args in mutation_commands(canonical, payload):
                out = run_cli(["--session", SESSION, *args], timeout=15)
                if cli_error(out):
                    result = {"ok": False, "partial": completed > 0,
                              "error": "Herdr rejected the command; not repeated"}
                    break
                completed += 1
        except (subprocess.TimeoutExpired, OutputLimitError):
            result = {"ok": False, "uncertain": True, "partial": completed > 0,
                      "error": "command outcome unknown; not repeated"}
        except (OSError, ValueError):
            result = {"ok": False, "partial": completed > 0,
                      "error": "command unavailable or target invalid; not repeated"}
        STORE.finish(SESSION, key, result)
        send("ack", {"duplicate": False}, in_reply_to=request_id)
        send("command.result", result, in_reply_to=request_id)
    except (OSError, ValueError, sqlite3.Error):
        # Storage failure must never fall through to execution.
        send("command.result", {"ok": False, "uncertain": True,
             "error": "command store unavailable; command not repeated"}, in_reply_to=request_id)


MUTATIONS = {"pane.sendText": "send_text", "pane.send_text": "send_text",
             "pane.sendKeys": "send_keys", "pane.send_keys": "send_keys",
             "space.close": "close_space", "space_close": "close_space"}


def terminal_event(pane_id, text, session_id):
    # NOTE: byte-cloned timeline.batch event (real 0.3.1, observed live):
    # terminal_fallback with one terminal block. id suffix is the sha256
    # of the block text (empty text -> e3b0c44...). spaceId/tabId/serverId
    # are empty strings even in the real bridge — do NOT enrich.
    import hashlib
    ts = now_ms()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {
        "adapter": "unknown",
        "blocks": [{"text": text, "truncated": False, "type": "terminal"}],
        "confidence": "heuristic",
        "id": "terminal-%s-%s" % (pane_id, digest),
        "kind": "terminal",
        "paneId": pane_id,
        "seq": ts,
        "serverId": "",
        "sessionId": session_id,
        "source": "terminal_fallback",
        "spaceId": "",
        "tabId": "",
        "timestamp": ts,
    }


def cached_snapshot():
    inner = current_snapshot()
    out = dict(inner)
    panes = [dict(p) for p in inner["panes"]]
    for pane in panes:
        for key in ("agent_session", "display_agent", "foreground_cwd", "state_labels", "title"):
            pane.setdefault(key, None)
    out["panes"] = panes
    return out


def handle(env):
    kind, request_id, payload = validate(env)
    if kind == "hello":
        snap = current_snapshot()
        send("hello.result", {
            "authRequired": False,
            "protocol": {"degraded": ["approval.respond"], "role": "admin",
                         "selected": 1, "supported": [1]},
            "server": {"bridgeVersion": BRIDGE_VERSION,
                       "capabilities": ["snapshot", "timeline", "pane.read", "pane.sendText",
                                        "pane.sendKeys", "pane.fit", "pane.unfitAll", "pane.fitTouch", "pane.ansi", "close"],
                       "herdrProtocol": snap["protocol"], "herdrVersion": snap["version"],
                       "hostname": "localhost", "serverId": "localhost:" + SESSION}})
        send("connection.state", {"phase": "stale"}, top_seq=1)
    elif kind in ("snapshot.get", "snapshot.request"):
        send("snapshot", {"snapshot": cached_snapshot()}, top_seq=2)
    elif kind == "timeline.subscribe":
        panes = payload.get("paneIds", [])
        active = payload.get("activePaneId") or (panes[0] if panes else None)
        ansi = payload.get("ansi", False)
        if payload.get("active") and panes:
            for pane in set(panes + [active]):
                require_pane(pane)
        send("ack", {"duplicate": False}, in_reply_to=request_id)
        if payload.get("active") and panes:
            send("connection.state", {"phase": "stale"}, top_seq=1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                texts = {pane: pool.submit(pane_text, pane, 400, ansi) for pane in dict.fromkeys(panes + [active])}
            number = 2
            def emit(pane, delta=False):
                nonlocal number
                event = terminal_event(pane, texts[pane].result(), SESSION)
                body = {"event": event} if delta else {"events": [event], "paneId": pane}
                send("timeline.delta" if delta else "timeline.batch", body, top_seq=number)
                number += 1
            if active == panes[0]:
                emit(active, True)
                rest = panes
            else:
                emit(panes[0])
                emit(active, True)
                rest = panes[1:]
            for pane in rest:
                emit(pane)
    elif kind == "raw.read":
        pane = payload["paneId"]
        lines = payload.get("lines", 1000)
        text = pane_text(pane, lines, payload.get("ansi", False))
        revision = next((p.get("revision", 0) for p in current_snapshot()["panes"]
                         if p.get("pane_id") == pane), 0)
        send("raw.read.result", {"paneId": pane, "revision": revision,
             "text": text, "truncated": len(text.splitlines()) >= lines}, in_reply_to=request_id)
    elif kind in MUTATIONS:
        mutate(kind, request_id, payload)
    elif kind == "pane.fitTouch":
        touched = touch_fit(payload["paneId"])
        send("ack", {"duplicate": False}, in_reply_to=request_id)
        send("command.result", {"ok": True, "touched": touched}, in_reply_to=request_id)
    elif kind in FIT_OPS:
        fit_pane(request_id, payload, kind)
    else:
        send("command.result", {"ok": False, "error": "unsupported request"}, in_reply_to=request_id)


def main(argv=None, input_stream=None):
    global SESSION
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="stdio", choices=("stdio", "sessions"))
    parser.add_argument("--version", action="version", version="herdr-mobile-bridge " + BRIDGE_VERSION)
    parser.add_argument("--session", default="default")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "sessions":
            home = HOME.parent.as_posix()
            if re.match(r"^[A-Za-z]:/", home):
                home = "/" + home[0].lower() + home[2:]
            socket = home + "/.config/herdr/herdr.sock"
            print(json.dumps([{"name": s["name"], "reachable": True,
                               "socket_path": socket, "session_dir": home + "/.config/herdr"}
                              for s in discover_sessions()]))
            return 0
        if not valid_session(args.session):
            raise ValueError("invalid session")
        SESSION = args.session
        current_snapshot()  # Fail closed; never silently select default or create a session.
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        sys.stderr.write("Herdr bridge: selected session unavailable\n")
        return 1
    try:
        sys.stdout.reconfigure(newline="", encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass
    stream = input_stream if input_stream is not None else sys.stdin.buffer
    while True:
        line = stream.readline(MAX_FRAME_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_FRAME_BYTES:
            send("command.result", {"ok": False, "error": "frame too large; connection closed"})
            return 2  # Do not drain an unlimited hostile frame.
        if not line.strip():
            continue
        env = None
        try:
            env = json.loads(line)
            handle(env)
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
            log("invalid_frame")
            request_id = env.get("id") if isinstance(env, dict) else None
            if not isinstance(request_id, str) or len(request_id) > 256:
                request_id = None
            send("command.result", {"ok": False, "error": "invalid request or stale session"},
                 in_reply_to=request_id)
        except (OSError, sqlite3.Error):
            log("request_failed")
            send("command.result", {"ok": False, "error": "request unavailable"})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
