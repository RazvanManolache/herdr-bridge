"""Offline regression tests: no live pane writes or SSH configuration changes."""
import concurrent.futures
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BRIDGE = Path(__file__).resolve().parents[1] / "bridge"
sys.path.insert(0, str(BRIDGE))
import bridge_common as common
from command_store import CommandStore


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BRIDGE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(panes=("w1:p1", "w1:p2", "w1:p3"), agents=None):
    return {"version": "0.9.3", "protocol": 22, "workspaces": [{"workspace_id": "w1"}],
            "tabs": [], "layouts": [], "agents": agents or [],
            "panes": [{"pane_id": p, "revision": 1} for p in panes]}


def process_claim(path, gate, queue):
    gate.wait(10)
    store = CommandStore(path, Path(path).with_suffix(".legacy"))
    queue.put(store.claim("default", "concurrent", {"operation": "close"})[0])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.m = load("responder", "stdio-responder.py")
        self.p = load("poller", "snapshot-poller.py")
        self.cache = self.home / "cache.json"
        self.cache_patch = patch.object(common, "CACHE", self.cache)
        self.cache_patch.start()
        self.m.STORE = CommandStore(self.home / "commands.db", self.home / "legacy.json")
        self.frames = []
        self.m.send = lambda kind, payload, *a, **kw: self.frames.append((kind, payload, kw))
        self.calls = []
        self.m.run_cli = self.success
        self.save_cache()

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    def success(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    def save_cache(self, name="default", snap=None, at=None):
        self.cache.write_text(json.dumps({"snapshots": {name: {
            "at": common.now_ms() if at is None else at,
            "snapshot": snapshot() if snap is None else snap}}}), encoding="utf-8")

    def request(self, kind="pane.sendText", key="key", **payload):
        body = {"paneId": "w1:p1", "text": "hello"}
        body.update(payload)
        body["idempotencyKey"] = key
        return {"type": kind, "id": "request", "sessionId": self.m.SESSION, "payload": body}

    def results(self):
        return [p for kind, p, _ in self.frames if kind == "command.result"]

    def test_successful_retry_does_not_send_twice(self):
        self.m.handle(self.request(text="hello\n"))
        self.m.handle(self.request(text="hello\n"))
        self.assertEqual(len(self.calls), 2)  # Text + Enter, one time.
        self.assertEqual(self.results(), [{"ok": True}, {"ok": True}])

    def test_failed_retry_preserves_failure(self):
        def fail(args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1, b"", b"failed")
        self.m.run_cli = fail
        self.m.handle(self.request())
        self.m.handle(self.request())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.results()[0], self.results()[1])
        self.assertFalse(self.results()[1]["ok"])

    def test_partial_send_does_not_replay_body(self):
        def partial(args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, int("send-keys" in args), b"", b"")
        self.m.run_cli = partial
        self.m.handle(self.request(text="hello\n"))
        self.m.handle(self.request(text="hello\n"))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.results()[-1]["partial"])
        self.assertFalse(self.results()[-1]["ok"])

    def test_timeout_is_uncertain_and_not_repeated(self):
        self.m.run_cli = lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("fake", 1))
        self.m.handle(self.request())
        self.m.run_cli = self.success
        self.m.handle(self.request())
        self.assertTrue(self.results()[-1]["uncertain"])
        self.assertEqual(self.calls, [])

    def test_reused_key_with_changed_payload_is_rejected(self):
        self.m.handle(self.request())
        self.m.handle(self.request(text="different"))
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.results()[-1]["ok"])

    def test_pending_crash_reservation_is_not_replayed(self):
        identity = {"operation": "send_text", "paneId": "w1:p1", "text": "hello"}
        self.m.STORE.claim("default", "key", identity)
        self.m.handle(self.request())
        self.assertEqual(self.calls, [])
        self.assertTrue(self.results()[-1]["uncertain"])

    def test_cross_process_claim_has_exactly_one_winner(self):
        context = multiprocessing.get_context("spawn")
        gate, queue = context.Event(), context.Queue()
        workers = [context.Process(target=process_claim, args=(str(self.home / "race.db"), gate, queue))
                   for _ in range(4)]
        try:
            for worker in workers:
                worker.start()
            gate.set()
            results = [queue.get(timeout=15) for _ in workers]
            self.assertEqual(sum(results), 1)
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
            queue.close()

    def test_legacy_keys_are_unknown_not_success(self):
        self.m.STORE.legacy.write_text(json.dumps({"key": common.now_ms()}))
        self.m.handle(self.request())
        self.assertEqual(self.calls, [])
        self.assertTrue(self.results()[-1]["uncertain"])

    def test_storage_failure_prevents_command(self):
        self.m.STORE.path = self.home / "missing" / "file.db"
        self.m.handle(self.request())
        self.assertEqual(self.calls, [])
        self.assertFalse(self.results()[-1]["ok"])

    def test_installed_close_and_key_features_preserved(self):
        self.m.handle(self.request("pane.sendKeys", keys=["ctrl+c"]))
        self.m.handle(self.request("space.close", key="close", spaceId="w1"))
        self.assertEqual(self.calls[0], ["--session", "default", "pane", "send-keys", "w1:p1", "ctrl+c"])
        self.assertEqual(self.calls[1], ["--session", "default", "workspace", "close", "w1"])

    def test_named_session_routes_all_cli_calls(self):
        self.m.SESSION = "work"
        self.save_cache("work")
        self.m.handle(self.request())
        self.m.pane_text("w1:p1", 100)
        self.assertTrue(all(args[:2] == ["--session", "work"] for args in self.calls))

    def test_session_mismatch_and_foreign_pane_are_rejected(self):
        request = self.request()
        request["sessionId"] = "other"
        with self.assertRaises(ValueError):
            self.m.handle(request)
        self.m.handle(self.request(paneId="w9:p99"))
        self.assertFalse(self.results()[-1]["ok"])
        self.assertEqual(self.calls, [])

    def test_same_key_in_separate_sessions_is_independent(self):
        self.m.handle(self.request())
        self.m.SESSION = "work"
        self.save_cache("work")
        self.m.handle(self.request())
        self.assertEqual(len(self.calls), 2)

    def test_bad_frames_do_not_crash_connection(self):
        bad = [[], None, {"type": "raw.read", "payload": "bad"},
               {"type": "raw.read", "payload": {"paneId": "w1:p1", "lines": "10"}},
               self.request(text="\ud800")]
        data = b"".join(json.dumps(frame).encode() + b"\n" for frame in bad)
        data += b'{"type":"snapshot.get"}\n'
        self.assertEqual(self.m.main([], io.BytesIO(data)), 0)
        self.assertEqual(len(self.results()), len(bad))
        self.assertEqual(self.frames[-1][0], "snapshot")

    def test_oversize_frame_closes_connection_without_drain(self):
        data = io.BytesIO(b"x" * (self.m.MAX_FRAME_BYTES + 100))
        self.assertEqual(self.m.main([], data), 2)
        self.assertEqual(data.tell(), self.m.MAX_FRAME_BYTES + 1)

    def test_resource_limits_reject_before_cli(self):
        bad = [self.request(text="x" * (self.m.MAX_TEXT_CHARS + 1)),
               {"type": "raw.read", "payload": {"paneId": "w1:p1", "lines": 999999}},
               {"type": "timeline.subscribe", "payload": {"active": True, "paneIds": ["p%d" % i for i in range(1000)]}}]
        for request in bad:
            with self.assertRaises(ValueError):
                self.m.handle(request)
        self.assertEqual(self.calls, [])

    def test_timeline_wire_order_and_fixed_worker_count(self):
        self.m.pane_text = lambda *args: "terminal"
        real_pool = concurrent.futures.ThreadPoolExecutor
        for active, expected in [("w1:p1", ["timeline.delta", "timeline.batch", "timeline.batch", "timeline.batch"]),
                                 ("w1:p2", ["timeline.batch", "timeline.delta", "timeline.batch", "timeline.batch"])]:
            self.frames.clear()
            with patch.object(self.m.concurrent.futures, "ThreadPoolExecutor", wraps=real_pool) as pool:
                self.m.handle({"type": "timeline.subscribe", "payload": {
                    "active": True, "activePaneId": active, "paneIds": ["w1:p1", "w1:p2", "w1:p3"]}})
                pool.assert_called_once_with(max_workers=4)
            self.assertEqual([k for k, _, _ in self.frames][2:], expected)
            batches = [p for k, p, _ in self.frames if k == "timeline.batch"]
            self.assertTrue(all(p["paneId"] == p["events"][0]["paneId"] for p in batches))

    def test_valid_empty_snapshot_replaces_old_cache(self):
        self.p.CACHE = self.cache
        self.p.discover_sessions = lambda: [{"name": "default", "running": True}]
        self.p.fetch_snapshot = lambda name: {"at": common.now_ms(), "snapshot": snapshot(panes=())}
        self.p.refresh()
        self.assertEqual(common.session_cache("default")["snapshot"]["panes"], [])

    def test_failed_snapshot_keeps_original_timestamp(self):
        old_at = common.now_ms() - 50_000
        self.save_cache(at=old_at)
        self.p.discover_sessions = lambda: [{"name": "default", "running": True}]
        self.p.fetch_snapshot = lambda name: (_ for _ in ()).throw(ValueError())
        self.assertEqual(self.p.build()["snapshots"]["default"]["at"], old_at)

    def test_old_or_wrong_session_cache_is_not_used(self):
        self.save_cache(at=common.now_ms() - 61_000)
        self.assertIsNone(common.session_cache("default"))
        self.save_cache()
        self.assertIsNone(common.session_cache("another"))

    def test_unknown_session_does_not_fall_back_or_spawn(self):
        with patch.object(self.m.sys, "stderr", io.StringIO()):
            self.assertEqual(self.m.main(["stdio", "--session", "absent"], io.BytesIO()), 1)
        self.assertEqual(self.calls, [])

    def test_command_text_is_not_persisted(self):
        secret = "PRIVATE_REVIEW_MARKER_34952"
        self.m.handle(self.request(text=secret))
        for path in self.home.iterdir():
            self.assertNotIn(secret.encode(), path.read_bytes())

    def test_redacted_diagnostics_and_rotation(self):
        with patch.object(common, "HOME", self.home), patch.dict(os.environ, {"HERDR_BRIDGE_DIAGNOSTICS": "1"}):
            common.log("private secret content")
            self.assertEqual(list(self.home.glob("diagnostic-*")), [])
            common.log("invalid_frame")
            log = next(self.home.glob("diagnostic-*.log"))
            log.write_bytes(b"x" * (256 * 1024))
            common.log("invalid_frame")
            self.assertTrue(log.with_suffix(".log.1").exists())
            self.assertLess(log.stat().st_size, 100)

    def test_failed_session_discovery_keeps_cache_unchanged(self):
        self.p.CACHE = self.cache
        before = self.cache.read_bytes()
        self.p.discover_sessions = lambda: (_ for _ in ()).throw(RuntimeError())
        with self.assertRaises(RuntimeError):
            self.p.refresh()
        self.assertEqual(self.cache.read_bytes(), before)

    def test_polling_routes_named_session_and_accepts_zero_agents(self):
        calls = []
        def cli(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": {"snapshot": snapshot(agents=[])}}).encode(), b"")
        self.p.run_cli = cli
        self.assertEqual(self.p.fetch_snapshot("work")["snapshot"]["agents"], [])
        self.assertEqual(calls, [["--session", "work", "api", "snapshot"]])

    def test_cli_output_is_bounded(self):
        with patch.object(common, "HERDR", sys.executable):
            with self.assertRaises(common.OutputLimitError):
                common.run_cli(["-c", "import sys; sys.stdout.write('x'*1000000)"], max_bytes=4096)

    def test_poller_lock_excludes_a_second_process(self):
        code = """import sys; sys.path.insert(0,sys.argv[1]); import bridge_common as c
try:
    with c.single_instance(): print('unexpected lock acquisition')
except (BlockingIOError, PermissionError):
    print('locked')
"""
        with patch.object(common, "HOME", self.home):
            with common.single_instance():
                env = dict(os.environ, HERDR_BRIDGE_HOME=str(self.home))
                result = subprocess.run([sys.executable, "-B", "-c", code, str(BRIDGE)],
                                        env=env, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), b"locked")


if __name__ == "__main__":
    unittest.main()
