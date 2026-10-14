"""Refresh bounded snapshots for existing sessions, including empty ones."""
import concurrent.futures
import importlib.util
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from bridge_common import (CACHE, MAX_CACHE_BYTES, discover_sessions,
                           log, now_ms, read_cache, run_cli, single_instance, valid_snapshot)

INTERVAL = 15


def load_fit_reaper():
    # NOTE: the responder module is import-safe (tests import it too);
    # a hyphenated filename needs an explicit spec.
    try:
        spec = importlib.util.spec_from_file_location(
            "bridge_fit_reap", Path(__file__).with_name("stdio-responder.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def reap_stale_panels(reaper):
    # NOTE: never let housekeeping kill the poller; a killed phone's
    # panels wait for the next cycle instead.
    try:
        released, failed = reaper.reap_stale_fits(reaper.FIT_TTL_MS)
    except Exception:
        log("fit_reap_failed")
        return
    if released or failed:
        log("fit_reap released=%d failed=%d" % (len(released), len(failed)))


def fetch_snapshot(name):
    out = run_cli(["--session", name, "api", "snapshot"], timeout=20,
                  max_bytes=MAX_CACHE_BYTES)
    if out.returncode:
        raise RuntimeError("snapshot query failed")
    data = json.loads(out.stdout)
    result = data.get("result") if isinstance(data, dict) else None
    snap = result.get("snapshot") if isinstance(result, dict) else None
    if not valid_snapshot(snap):
        raise ValueError("invalid snapshot")
    return {"at": now_ms(), "snapshot": snap}


def build():
    sessions = discover_sessions()
    old = (read_cache() or {}).get("snapshots", {})
    if not isinstance(old, dict):
        old = {}
    entries = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {s["name"]: pool.submit(fetch_snapshot, s["name"]) for s in sessions}
        for name, future in futures.items():
            try:
                entries[name] = future.result()
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                log("snapshot_query_failed")
                if name in old:
                    entries[name] = old[name]  # Never refresh a failed query's timestamp.
    return {"at": now_ms(), "sessions": sessions, "snapshots": entries}


def refresh():
    raw = json.dumps(build(), separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_CACHE_BYTES:
        raise ValueError("snapshot cache too large")
    fd, temporary = tempfile.mkstemp(prefix="snapshot-", suffix=".tmp", dir=CACHE.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CACHE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    reaper = load_fit_reaper()
    while True:
        try:
            refresh()
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            log("cache_refresh_failed")
        if reaper is not None:
            reap_stale_panels(reaper)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        with single_instance():
            main()
    except (BlockingIOError, PermissionError):
        # A startup shortcut and a manual installation may overlap.
        # Lock ownership lasts for the process lifetime and survives PID reuse.
        pass
