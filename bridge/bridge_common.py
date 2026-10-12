"""Shared paths, redacted diagnostics and session validation."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import threading
import time

HOME = Path(os.environ.get("HERDR_BRIDGE_HOME", Path.home() / ".herdr-bridge-emu"))
HERDR = os.environ.get("HERDR_BRIDGE_EXE", str(
    Path.home() / ".herdr/packages/standalone/releases/0.9.3-x86_64-pc-windows-msvc/herdr.exe"))
CACHE = HOME / "snapshot-cache.json"
MAX_CACHE_BYTES = 16 * 1024 * 1024
CACHE_MAX_AGE_MS = 60_000
MAX_SESSIONS = 32


@contextmanager
def single_instance():
    with (HOME / "poller.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class OutputLimitError(RuntimeError):
    pass


def run_cli(args, timeout=15, max_bytes=2 * 1024 * 1024):
    """Drain both pipes concurrently without allowing unbounded captured output."""
    proc = subprocess.Popen([HERDR, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    streams = [bytearray(), bytearray()]
    exceeded = threading.Event()

    def drain(pipe, target):
        try:
            while True:
                block = pipe.read(8192)
                if not block:
                    return
                if len(target) + len(block) > max_bytes:
                    exceeded.set()
                    proc.kill()
                    return
                target.extend(block)
        finally:
            pipe.close()

    threads = [threading.Thread(target=drain, args=(pipe, target), daemon=True)
               for pipe, target in zip((proc.stdout, proc.stderr), streams)]
    for thread in threads:
        thread.start()
    try:
        proc.wait(timeout=timeout)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=2)
    if exceeded.is_set() or any(t.is_alive() for t in threads):
        raise OutputLimitError("CLI output exceeded limit or pipes did not close")
    return subprocess.CompletedProcess(proc.args, proc.returncode, bytes(streams[0]), bytes(streams[1]))


def now_ms():
    return int(time.time() * 1000)


def valid_session(name):
    return isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name))


def valid_snapshot(snap):
    return (isinstance(snap, dict) and isinstance(snap.get("version"), str)
            and isinstance(snap.get("protocol"), int)
            and all(isinstance(snap.get(k), list) for k in
                    ("workspaces", "tabs", "panes", "layouts", "agents"))
            and all(isinstance(p, dict) for p in snap["panes"]))


def read_cache():
    try:
        with CACHE.open("rb") as f:
            raw = f.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return None
        cache = json.loads(raw)
        return cache if isinstance(cache, dict) else None
    except (OSError, ValueError):
        return None


def session_cache(name):
    cache = read_cache() or {}
    entries = cache.get("snapshots", {})
    entry = entries.get(name) if isinstance(entries, dict) else None
    if entry is None and name == "default" and "snapshot" in cache:
        entry = cache
    if not isinstance(entry, dict):
        return None
    at = entry.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return None
    if not 0 <= now_ms() - at <= CACHE_MAX_AGE_MS:
        return None
    return entry if valid_snapshot(entry.get("snapshot")) else None


def discover_sessions():
    result = run_cli(["session", "list", "--json"], timeout=15)
    if result.returncode:
        raise RuntimeError("session discovery failed")
    data = json.loads(result.stdout)
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, list) or len(sessions) > MAX_SESSIONS:
        raise ValueError("invalid session discovery")
    return [s for s in sessions if isinstance(s, dict)
            and valid_session(s.get("name")) and s.get("running") is True]


def log(event):
    """Opt-in fixed-label metadata only; no content logging switch."""
    if os.environ.get("HERDR_BRIDGE_DIAGNOSTICS") != "1":
        return
    if not re.fullmatch(r"[a-z_.]{1,48}", event):
        return
    try:
        # Per-process rotation avoids multiple responders renaming one log.
        path = HOME / ("diagnostic-%d.log" % os.getpid())
        if path.exists() and path.stat().st_size >= 256 * 1024:
            os.replace(path, path.with_suffix(".log.1"))
        files = sorted(HOME.glob("diagnostic-*.log*"), key=lambda p: p.stat().st_mtime)
        for old in files[:-32]:
            old.unlink(missing_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("%d %s\n" % (now_ms(), event))
    except OSError:
        pass
