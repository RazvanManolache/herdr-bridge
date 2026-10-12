"""Exercise only a disposable headless session; never touch existing panes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
sys.path.insert(0, str(BRIDGE))
from bridge_common import HERDR, HOME, run_cli


def main():
    name = "bridge-check-" + uuid.uuid4().hex[:12]
    marker = "BRIDGE_OK_" + uuid.uuid4().hex[:8]
    wrapper = HOME / "sh-log.exe"
    spec = importlib.util.spec_from_file_location("poller", BRIDGE / "snapshot-poller.py")
    poller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(poller)
    report = {"session": name, "checks": []}
    proc = None
    with tempfile.TemporaryDirectory(prefix="herdr-bridge-validation-") as cwd:
        try:
            with open(os.devnull, "wb") as sink:
                proc = subprocess.Popen([HERDR, "--session", name, "server"],
                    stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
                    cwd=cwd, creationflags=subprocess.CREATE_NO_WINDOW)
            for _ in range(40):
                try:
                    out = run_cli(["--session", name, "api", "snapshot"], timeout=2)
                    if out.returncode == 0:
                        break
                except (OSError, subprocess.TimeoutExpired):
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("disposable server did not start")
            out = run_cli(["--session", name, "workspace", "create", "--cwd", cwd,
                           "--label", "bridge verification", "--no-focus"])
            if out.returncode:
                raise RuntimeError("test workspace creation failed")
            snap = json.loads(run_cli(["--session", name, "api", "snapshot"]).stdout)["result"]["snapshot"]
            pane = snap["panes"][0]["pane_id"]
            workspace = snap["workspaces"][0]["workspace_id"]
            poller.refresh()

            def frame(kind, payload, key=None):
                if key:
                    payload = dict(payload, idempotencyKey=key)
                return {"version": 1, "id": uuid.uuid4().hex, "type": kind,
                        "sessionId": name, "payload": payload}

            def exchange(frames):
                args = [str(wrapper), "-c", "~/.local/bin/herdr-mobile-bridge stdio --session " + name]
                out = subprocess.run(args, input=b"".join(json.dumps(f).encode() + b"\n" for f in frames),
                                     capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                if out.returncode or b"\r\n" in out.stdout:
                    raise RuntimeError("installed wrapper failed or emitted CRLF JSONL")
                return [json.loads(line) for line in out.stdout.splitlines()]

            events = exchange([frame("hello", {}), frame("snapshot.get", {})])
            assert [e["type"] for e in events] == ["hello.result", "connection.state", "snapshot"]
            assert events[-1]["payload"]["snapshot"]["panes"][0]["pane_id"] == pane
            assert events[-1]["sessionId"] == name
            report["checks"].append("named-session handshake and isolated snapshot")

            request = frame("pane.sendText", {"paneId": pane, "text": "echo " + marker + "\n"}, uuid.uuid4().hex)
            first = exchange([request])
            duplicate = exchange([request])  # A new responder, as with an SSH reconnect.
            assert first[-1]["payload"]["ok"] is True
            assert duplicate[0]["payload"]["duplicate"] is True
            assert duplicate[-1]["payload"]["ok"] is True
            time.sleep(0.5)
            read = exchange([frame("raw.read", {"paneId": pane, "lines": 100})])
            assert read[-1]["payload"]["text"].count(marker) == 2
            report["checks"].append("real CLI text plus Enter, readback, and reconnect deduplication")

            wrong = frame("pane.sendText", {"paneId": pane, "text": "MUST_NOT_SEND"}, uuid.uuid4().hex)
            wrong["sessionId"] = "default"
            assert exchange([wrong])[-1]["payload"]["ok"] is False
            report["checks"].append("cross-session frame rejected")

            timeline = exchange([frame("timeline.subscribe", {"paneIds": [pane], "active": True,
                                                                 "activePaneId": pane})])
            assert [e["type"] for e in timeline] == ["ack", "connection.state", "timeline.delta", "timeline.batch"]
            assert marker in timeline[-1]["payload"]["events"][0]["blocks"][0]["text"]
            report["checks"].append("per-pane timeline protocol and terminal content")

            keys = exchange([frame("pane.sendKeys", {"paneId": pane, "keys": ["enter"]}, uuid.uuid4().hex)])
            assert keys[-1]["payload"]["ok"] is True
            close = frame("space.close", {"spaceId": workspace}, uuid.uuid4().hex)
            assert exchange([close])[-1]["payload"]["ok"] is True
            assert exchange([close])[-1]["payload"]["ok"] is True
            poller.refresh()
            empty = exchange([frame("snapshot.get", {})])[-1]["payload"]["snapshot"]
            assert not empty["panes"] and not empty["agents"]
            report["checks"].append("send keys, workspace close, close retry, and fresh empty snapshot")
            report["passed"] = True
        finally:
            if proc is not None:
                run_cli(["--session", name, "server", "stop"], timeout=10)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                poller.refresh()
                report["server_stopped"] = proc.poll() is not None
            (ROOT / "build" / "live-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
