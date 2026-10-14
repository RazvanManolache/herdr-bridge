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

    def test_reaper_module_loads_responder(self):
        reaper = self.p.load_fit_reaper()
        self.assertEqual(reaper.FIT_TTL_MS, 5 * 60 * 1000)
        self.assertTrue(callable(reaper.reap_stale_fits))

    def test_reaper_logs_releases_without_killing_poller(self):
        seen = []
        class Reaper:
            FIT_TTL_MS = 60000
            def reap_stale_fits(self, ttl):
                seen.append(ttl)
                return (["w1:p9"], [])
        with patch.object(self.p, "log") as logged:
            self.p.reap_stale_panels(Reaper())
        self.assertEqual(seen, [60000])
        logged.assert_called_once()

    def test_reaper_failure_is_contained(self):
        class BadReaper:
            FIT_TTL_MS = 60000
            def reap_stale_fits(self, ttl):
                raise RuntimeError("boom")
        with patch.object(self.p, "log") as logged:
            self.p.reap_stale_panels(BadReaper())
        logged.assert_called_once_with("fit_reap_failed")


def fit_snapshot(panes=("w1:p1",), tab="w1:t1", area=254):
    snap = snapshot(panes)
    snap["panes"] = [{"pane_id": p, "revision": 1, "tab_id": tab} for p in panes]
    snap["tabs"] = [{"tab_id": tab, "pane_count": len(panes)}]
    snap["layouts"] = [{"tab_id": tab, "area": {"width": area, "height": 50}}]
    return snap


def split_reply(side="w1:p9"):
    return json.dumps({"id": "cli:pane:split",
                       "result": {"pane": {"pane_id": side, "tab_id": "w1:t1"},
                                  "type": "pane_info"}}).encode()


class FitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.m = load("responder_fit", "stdio-responder.py")
        self.cache = self.home / "cache.json"
        self.cache_patch = patch.object(common, "CACHE", self.cache)
        self.cache_patch.start()
        self.m.STORE = CommandStore(self.home / "commands.db", self.home / "legacy.json")
        self.frames = []
        self.m.send = lambda kind, payload, *a, **kw: self.frames.append((kind, payload, kw))
        self.calls = []
        self.reads = []
        self.behavior = {}
        self.m.run_cli = self.route
        self.live = None
        self.save(fit_snapshot())

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    def save(self, snap):
        self.cache.write_text(json.dumps({"snapshots": {"default": {
            "at": common.now_ms(), "snapshot": snap}}}), encoding="utf-8")

    def route(self, args, **kwargs):
        self.calls.append(args)
        if args[2:4] == ["pane", "list"]:
            if self.live is None:
                return subprocess.CompletedProcess(args, 1, b"", b"failed")
            panes = [{"pane_id": p, "tab_id": t} for p, t in self.live]
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"result": {"panes": panes}}).encode(), b"")
        mode = self.behavior.get(args[3] if len(args) > 3 else "", "ok")
        if callable(mode):
            return mode(args, kwargs)
        if mode != "ok":
            return subprocess.CompletedProcess(args, 1, b"", b"failed")
        if args[2:4] == ["pane", "split"]:
            return subprocess.CompletedProcess(args, 0, split_reply(), b"")
        if args[2:4] == ["pane", "read"]:
            body = "x" * 200 if "--source" in args and args[args.index("--source") + 1] == "recent" else "short"
            self.reads.append(args)
            return subprocess.CompletedProcess(args, 0, body.encode(), b"")
        return subprocess.CompletedProcess(args, 0, b'{"id":"x","result":{"type":"ok"}}', b"")

    def fit(self, pane="w1:p1", key="fit-key", **payload):
        body = {"paneId": pane, "cols": 48, "idempotencyKey": key}
        body.update(payload)
        self.m.handle({"type": "pane.fit", "id": "r1", "sessionId": "default", "payload": body})

    def unfit(self, pane="w1:p1", key="unfit-key", **payload):
        body = {"paneId": pane, "idempotencyKey": key}
        body.update(payload)
        self.m.handle({"type": "pane.unfit", "id": "r2", "sessionId": "default", "payload": body})

    def unfit_all(self, key="unfit-all-key"):
        self.m.handle({"type": "pane.unfitAll", "id": "r3", "sessionId": "default",
                       "payload": {"idempotencyKey": key}})

    def results(self):
        return [p for kind, p, _ in self.frames if kind == "command.result"]

    def test_fit_splits_runs_notice_and_reports_width(self):
        self.fit()
        kinds = [c[3] for c in self.calls]
        self.assertIn("split", kinds)
        self.assertIn("run", kinds)
        split = next(c for c in self.calls if c[3] == "split")
        self.assertEqual(split[4:8], ["w1:p1", "--direction", "right", "--ratio"])
        self.assertAlmostEqual(float(split[8]), (48 + 3) / 254, places=2)
        run = next(c for c in self.calls if c[3] == "run")
        # NOTE: the notice panel is never a shell: print-then-sleep only.
        self.assertEqual(run[4:7], ["w1:p9", "powershell", "-NoProfile"])
        self.assertEqual(run[7], "-Command")
        self.assertIn("Write-Host", run[-1])
        self.assertIn("Start-Sleep", run[-1])
        self.assertIn("48", run[-1])
        result = self.results()[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["sidePaneId"], "w1:p9")
        self.assertEqual(result["width"], 200)

    def test_fit_refuses_tab_with_splits(self):
        self.save(fit_snapshot(panes=("w1:p1", "w1:p2")))
        self.fit()
        self.assertFalse(any(c[3] == "split" for c in self.calls))
        self.assertFalse(self.results()[-1]["ok"])

    def test_fit_rejects_bad_cols(self):
        with self.assertRaises(ValueError):
            self.m.validate({"type": "pane.fit", "id": "r",
                             "payload": {"paneId": "w1:p1", "cols": 10, "idempotencyKey": "k"}})

    def test_fit_split_failure_leaves_no_split(self):
        self.behavior["split"] = "fail"
        self.fit()
        self.assertFalse(self.results()[-1]["ok"])
        self.assertFalse(any(c[3] == "close" for c in self.calls))

    def test_fit_run_failure_closes_split(self):
        self.behavior["run"] = "fail"
        self.fit()
        closes = [c for c in self.calls if c[3] == "close"]
        self.assertEqual([c[4] for c in closes], ["w1:p9"])
        self.assertFalse(self.results()[-1]["ok"])

    def test_unfit_closes_only_recorded_side(self):
        self.fit()
        self.calls.clear()
        self.save(fit_snapshot(panes=("w1:p1", "w1:p9", "w1:p2"), tab="w1:t1"))
        self.unfit()
        closes = [c for c in self.calls if c[3] == "close"]
        self.assertEqual([c[4] for c in closes], ["w1:p9"])
        self.assertEqual(self.results()[-1], {"ok": True, "released": True})

    def test_unfit_without_record_releases_nothing(self):
        self.unfit()
        self.assertFalse(any(c[3] == "close" for c in self.calls))
        self.assertEqual(self.results()[-1], {"ok": True, "released": False})

    def test_unfit_closed_tab_releases_nothing(self):
        self.fit()
        self.save({"version": "9.9", "protocol": 1,
                   "workspaces": [{"workspace_id": "w1"}],
                   "tabs": [], "panes": [], "layouts": [], "agents": []})
        self.unfit()
        # The pane (and its tab) is gone: nothing to close.
        self.assertFalse(any(c[3] == "close" for c in self.calls))
        self.assertEqual(self.results()[-1], {"ok": True, "released": False})

    def test_unfit_forgets_absent_side_without_closing(self):
        self.fit()
        self.save(fit_snapshot(panes=("w1:p1",), tab="w1:t1"))
        # NOTE: the record is fresh but the desktop already lost the side;
        # the old code closed nothing yet still called close.
        self.live = [("w1:p1", "w1:t1")]
        self.unfit()
        self.assertFalse(any(c[3] == "close" for c in self.calls))
        self.assertEqual(self.results()[-1], {"ok": True, "released": True})
        self.unfit(key="u-again")
        self.assertEqual(self.results()[-1], {"ok": True, "released": False})

    def test_count_check_reads_live_layout(self):
        self.save(fit_snapshot(panes=("w1:p1",), tab="w1:t1"))
        # NOTE: the cache still shows one pane, but the desktop split.
        self.live = [("w1:p1", "w1:t1"), ("w1:pX", "w1:t1")]
        self.fit(key="k2")
        self.assertFalse(any(c[3] == "split" for c in self.calls))
        self.assertFalse(self.results()[-1]["ok"])

    def two_tab_snapshot(self):
        snap = fit_snapshot(panes=("w1:p1",), tab="w1:t1")
        snap["panes"].append({"pane_id": "w1:p2", "revision": 1, "tab_id": "w1:t2"})
        snap["tabs"].append({"tab_id": "w1:t2", "pane_count": 1})
        snap["layouts"].append({"tab_id": "w1:t2", "area": {"width": 254, "height": 50}})
        return snap

    def test_unfit_stuck_close_keeps_record_and_retries(self):
        self.fit()
        # NOTE: production snapshots contain the live side pane, so the
        # sweep keeps the record and the stuck close is really retried.
        self.save(fit_snapshot(panes=("w1:p1", "w1:p9"), tab="w1:t1"))
        self.behavior["close"] = "fail"
        self.unfit(key="u1")
        first = self.results()[-1]
        self.assertFalse(first["ok"])
        self.assertFalse(first["released"])
        del self.behavior["close"]
        self.unfit(key="u2")
        closes = [c for c in self.calls if c[3] == "close"]
        # NOTE: three failed attempts, then the retry that releases it.
        self.assertEqual([c[4] for c in closes], ["w1:p9"] * 4)
        self.assertEqual(self.results()[-1], {"ok": True, "released": True})

    def test_unfit_all_releases_every_tab(self):
        self.save(self.two_tab_snapshot())
        self.fit(key="fit-key-1")
        # NOTE: the first fit really split tab one, so the side pane is
        # live when the second tab is fitted.
        snap = self.two_tab_snapshot()
        snap["panes"].append({"pane_id": "w1:p9", "revision": 1, "tab_id": "w1:t1"})
        snap["tabs"][0]["pane_count"] = 2
        self.save(snap)
        self.fit(pane="w1:p2", key="fit-key-2")
        self.unfit_all()
        result = self.results()[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["released"]), 2)
        self.assertEqual(result["failed"], [])
        self.unfit_all(key="unfit-all-2")
        self.assertEqual(self.results()[-1], {"ok": True, "released": [], "failed": []})

    def test_unfit_all_reports_stuck_panels(self):
        self.fit()
        self.save(fit_snapshot(panes=("w1:p1", "w1:p9"), tab="w1:t1"))
        self.behavior["close"] = "fail"
        self.unfit_all()
        result = self.results()[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["released"], [])

    def orphan_read(self, args, kwargs):
        if args[4] == "w1:pX":
            body = ("PS> powershell -NoProfile -Command Write-Host 'PHONE VIEW - "
                    "NARROWED TO 26 COLS - RESTORES WHEN YOU LEAVE'\n"
                    "PHONE VIEW - NARROWED TO 26 COLS - RESTORES WHEN YOU LEAVE\n")
            return subprocess.CompletedProcess(args, 0, body.encode(), b"")
        return subprocess.CompletedProcess(args, 0, b"shell> prompt\n", b"")

    def test_unfit_all_heals_unrecorded_notice_panel(self):
        self.save(fit_snapshot(panes=("w1:p1", "w1:pX"), tab="w1:t1"))
        self.live = [("w1:p1", "w1:t1"), ("w1:pX", "w1:t1")]
        self.behavior["read"] = self.orphan_read
        self.unfit_all()
        result = self.results()[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["released"], ["w1:pX"])
        self.assertEqual(result["failed"], [])
        closes = [c for c in self.calls if c[2:4] == ["pane", "close"]]
        self.assertEqual([c[4] for c in closes], ["w1:pX"])

    def test_unfit_all_ignores_panes_without_marker(self):
        self.save(fit_snapshot(panes=("w1:p1", "w1:pX"), tab="w1:t1"))
        self.live = [("w1:p1", "w1:t1"), ("w1:pX", "w1:t1")]
        self.behavior["read"] = lambda args, kwargs: subprocess.CompletedProcess(
            args, 0, b"shell> prompt\nuser work here\n", b"")
        self.unfit_all()
        self.assertEqual(self.results()[-1], {"ok": True, "released": [], "failed": []})
        closes = [c for c in self.calls if c[2:4] == ["pane", "close"]]
        self.assertEqual(closes, [])

    def test_unfit_all_keeps_lone_pane_with_marker(self):
        self.save(fit_snapshot(panes=("w1:p1",), tab="w1:t1"))
        self.live = [("w1:p1", "w1:t1")]
        self.behavior["read"] = lambda args, kwargs: subprocess.CompletedProcess(
            args, 0, b"PHONE VIEW - NARROWED TO 26 COLS - RESTORES WHEN YOU LEAVE\n", b"")
        self.unfit_all()
        self.assertEqual(self.results()[-1], {"ok": True, "released": [], "failed": []})
        closes = [c for c in self.calls if c[2:4] == ["pane", "close"]]
        self.assertEqual(closes, [])

    def test_unfit_all_heals_orphan_beside_recorded_side(self):
        self.fit()
        self.save(fit_snapshot(panes=("w1:p1", "w1:p9", "w1:pX"), tab="w1:t1"))
        self.live = [("w1:p1", "w1:t1"), ("w1:p9", "w1:t1"), ("w1:pX", "w1:t1")]
        self.behavior["read"] = self.orphan_read
        self.unfit_all()
        result = self.results()[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(result["released"]), ["w1:p9", "w1:pX"])
        self.assertEqual(result["failed"], [])

    def touch(self, pane="w1:p1", key="touch-key"):
        self.m.handle({"type": "pane.fitTouch", "id": "r4", "sessionId": "default",
                       "payload": {"paneId": pane, "idempotencyKey": key}})

    def test_fit_touch_refreshes_record(self):
        self.fit()
        with self.m.STORE.connect() as db:
            db.execute("UPDATE fit_sides SET at=? WHERE session='default'", (1,))
        self.touch()
        result = self.results()[-1]
        self.assertEqual(result, {"ok": True, "touched": True})
        with self.m.STORE.connect() as db:
            at = db.execute("SELECT at FROM fit_sides WHERE session='default'").fetchone()[0]
        self.assertGreater(at, 1)

    def test_fit_touch_unknown_pane_touches_nothing(self):
        self.touch(pane="w1:p9")
        self.assertEqual(self.results()[-1], {"ok": True, "touched": False})

    def test_fit_touch_rejects_bad_pane(self):
        with self.assertRaises(ValueError):
            self.m.handle({"type": "pane.fitTouch", "id": "r5", "sessionId": "default",
                           "payload": {"paneId": "../evil", "idempotencyKey": "k"}})

    def test_unfit_all_stale_mode_reaps_only_old(self):
        self.fit()
        self.save(self.two_tab_snapshot())
        self.live = [("w1:p1", "w1:t1"), ("w1:p9", "w1:t1"),
                     ("w1:p2", "w1:t2"), ("w1:pX", "w1:t2")]
        with self.m.STORE.connect() as db:
            self.m.fit_sides(db)
            db.execute("INSERT INTO fit_sides VALUES('default','w1:t2','w1:pX',1)")
        self.m.handle({"type": "pane.unfitAll", "id": "r6", "sessionId": "default",
                       "payload": {"idempotencyKey": "stale-1", "staleOlderThanMs": 60000}})
        result = self.results()[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["released"], ["w1:pX"])
        self.assertEqual(result["failed"], [])
        rows = self.m.all_fit_sides()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "w1:t1")

    def test_unfit_all_stale_mode_rejects_bad_threshold(self):
        with self.assertRaises(ValueError):
            self.m.handle({"type": "pane.unfitAll", "id": "r7", "sessionId": "default",
                           "payload": {"idempotencyKey": "stale-2", "staleOlderThanMs": 1000}})

    def test_tab_resolution_prefers_live_layout(self):
        # NOTE: the cache still maps the pane to its old tab, but the
        # desktop already moved it; the record must follow reality.
        self.save(fit_snapshot(panes=("w1:p1",), tab="w1:t1"))
        self.live = [("w1:p1", "w1:t9")]
        self.fit()
        self.assertTrue(self.results()[-1]["ok"])
        rows = self.m.all_fit_sides()
        self.assertEqual(rows, [("w1:t9", "w1:p9")])

    def test_flaky_run_recovers_on_retry(self):
        seen = []
        base = self.route

        def flaky(args, **kwargs):
            if args[3] == "run" and not seen:
                seen.append(1)
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 1, b"", b"failed")
            return base(args, **kwargs)

        self.m.run_cli = flaky
        self.fit()
        self.assertTrue(self.results()[-1]["ok"])
        runs = [c for c in self.calls if c[3] == "run"]
        self.assertEqual(len(runs), 2)

    def test_failed_cleanup_keeps_record_and_names_reason(self):
        self.behavior["run"] = "fail"
        self.behavior["close"] = "fail"
        self.fit()
        result = self.results()[-1]
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)
        # The stuck panel keeps its record: unfit retries it later.
        del self.behavior["run"]
        del self.behavior["close"]
        self.save(fit_snapshot(panes=("w1:p1", "w1:p9"), tab="w1:t1"))
        self.unfit()
        self.assertEqual(self.results()[-1], {"ok": True, "released": True})

    def test_fit_is_idempotent(self):
        self.fit()
        calls = len(self.calls)
        self.fit()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.results()[-1], self.results()[-2])

    def test_subscribe_ansi_requests_ansi_format(self):
        self.m.handle({"type": "timeline.subscribe", "id": "s1", "sessionId": "default",
                       "payload": {"paneIds": ["w1:p1"], "active": True, "activePaneId": "w1:p1", "ansi": True}})
        formats = [self.reads[i][self.reads[i].index("--format") + 1] for i in range(len(self.reads))]
        self.assertTrue(formats)
        self.assertTrue(all(f == "ansi" for f in formats))

    def test_subscribe_defaults_to_plain_text(self):
        self.m.handle({"type": "timeline.subscribe", "id": "s2", "sessionId": "default",
                       "payload": {"paneIds": ["w1:p1"], "active": True, "activePaneId": "w1:p1"}})
        formats = [self.reads[i][self.reads[i].index("--format") + 1] for i in range(len(self.reads))]
        self.assertTrue(formats)
        self.assertTrue(all(f == "text" for f in formats))


if __name__ == "__main__":
    unittest.main()
