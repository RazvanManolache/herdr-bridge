"""Durable cross-process reservations; unknown outcomes are never replayed."""
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from bridge_common import HOME, now_ms


class CommandStore:
    def __init__(self, path=None, legacy=None):
        self.path = Path(path) if path else HOME / "commands.sqlite3"
        self.legacy = Path(legacy) if legacy else HOME / "idempotency.json"

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS commands (
            session TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT,
            state TEXT NOT NULL, result TEXT, at INTEGER NOT NULL,
            PRIMARY KEY(session, key))""")
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY)")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(self, session, key, request):
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                if self.legacy.exists():
                    # Old keys were saved before execution: importing them as
                    # unknown prevents replay of commands during upgrade.
                    if self.legacy.stat().st_size > 4 * 1024 * 1024:
                        raise ValueError("legacy store too large")
                    old = json.loads(self.legacy.read_text(encoding="utf-8"))
                    if not isinstance(old, dict):
                        raise ValueError("invalid legacy store")
                    for old_key, at in old.items():
                        if isinstance(at, (int, float)) and at > now_ms() - 86_400_000:
                            db.execute("INSERT OR IGNORE INTO commands VALUES(?,?,NULL,'unknown',NULL,?)",
                                       ("default", old_key, int(at)))
                db.execute("INSERT INTO metadata VALUES('legacy_imported')")
            db.execute("DELETE FROM commands WHERE state='done' AND at < ?",
                       (now_ms() - 86_400_000,))
            row = db.execute("SELECT fingerprint,state,result FROM commands WHERE session=? AND key=?",
                             (session, key)).fetchone()
            if row:
                old_fingerprint, state, result = row
                if old_fingerprint is not None and fingerprint != old_fingerprint:
                    return False, {"ok": False, "error": "idempotency key reused for a different command"}
                if state == "done":
                    return False, json.loads(result)
                return False, {"ok": False, "uncertain": True,
                               "error": "command in progress or outcome unknown; not repeated"}
            if db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] >= 100_000:
                raise ValueError("command store full")
            db.execute("INSERT INTO commands VALUES(?,?,?,'pending',NULL,?)",
                       (session, key, fingerprint, now_ms()))
        return True, None

    def finish(self, session, key, result):
        with self.connect() as db:
            db.execute("UPDATE commands SET state=?,result=?,at=? WHERE session=? AND key=?",
                       ("unknown" if result.get("uncertain") else "done",
                        json.dumps(result), now_ms(), session, key))
