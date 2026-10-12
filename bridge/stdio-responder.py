"""Windows Herdr Mobile JSONL adapter. SSH is the authentication boundary."""
import argparse
import concurrent.futures
import json
import re
import sqlite3
import subprocess
import sys

from bridge_common import (HOME, OutputLimitError, discover_sessions, log,
                           now_ms, run_cli, session_cache, valid_session)
from command_store import CommandStore

BRIDGE_VERSION = "0.3.1"
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
    if kind in ("raw.read", "pane.sendText", "pane.send_text", "pane.sendKeys", "pane.send_keys"):
        target(payload.get("paneId"))
    if kind == "raw.read":
        lines = payload.get("lines", 1000)
        if type(lines) is not int or not 1 <= lines <= MAX_READ_LINES:
            raise ValueError("lines must be between 1 and 2000")
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
    if kind in MUTATIONS:
        checked_string(payload.get("idempotencyKey") or request_id, "idempotency key")
    return kind, request_id, payload


def current_snapshot():
    entry = session_cache(SESSION)
    if entry is None:
        raise ValueError("session snapshot unavailable or stale")
    return entry["snapshot"]


def require_pane(pane_id):
    if not any(p.get("pane_id") == pane_id for p in current_snapshot()["panes"]):
        raise ValueError("pane does not belong to selected session")


def pane_text(pane_id, lines):
    require_pane(pane_id)
    try:
        out = run_cli(["--session", SESSION, "pane", "read", pane_id,
                       "--source", "recent-unwrapped", "--lines", str(lines),
                       "--format", "text"], timeout=12, max_bytes=512 * 1024)
        return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired, OutputLimitError):
        log("pane_read_failed")
        return ""


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
                                        "pane.sendKeys", "close"],
                       "herdrProtocol": snap["protocol"], "herdrVersion": snap["version"],
                       "hostname": "localhost", "serverId": "localhost:" + SESSION}})
        send("connection.state", {"phase": "stale"}, top_seq=1)
    elif kind in ("snapshot.get", "snapshot.request"):
        send("snapshot", {"snapshot": cached_snapshot()}, top_seq=2)
    elif kind == "timeline.subscribe":
        panes = payload.get("paneIds", [])
        active = payload.get("activePaneId") or (panes[0] if panes else None)
        if payload.get("active") and panes:
            for pane in set(panes + [active]):
                require_pane(pane)
        send("ack", {"duplicate": False}, in_reply_to=request_id)
        if payload.get("active") and panes:
            send("connection.state", {"phase": "stale"}, top_seq=1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                texts = {pane: pool.submit(pane_text, pane, 400) for pane in dict.fromkeys(panes + [active])}
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
        text = pane_text(pane, lines)
        revision = next((p.get("revision", 0) for p in current_snapshot()["panes"]
                         if p.get("pane_id") == pane), 0)
        send("raw.read.result", {"paneId": pane, "revision": revision,
             "text": text, "truncated": len(text.splitlines()) >= lines}, in_reply_to=request_id)
    elif kind in MUTATIONS:
        mutate(kind, request_id, payload)
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
