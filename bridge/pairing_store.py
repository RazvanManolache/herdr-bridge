"""Local-owner approval of short-lived requests; network clients never approve devices."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager


def digest(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 128:
        raise ValueError('invalid device credential')
    return hashlib.sha256(value.encode('ascii')).hexdigest()


class PairingStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, until REAL NOT NULL);
              INSERT OR IGNORE INTO settings VALUES (1, 0);
              CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL,
                secret_hash TEXT NOT NULL, code_hash TEXT NOT NULL, expires REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending');
              CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, secret_hash TEXT UNIQUE NOT NULL,
                created REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def open_window(self, seconds=300):
        with self.db() as db:
            db.execute('UPDATE settings SET until=? WHERE id=1', (self.clock() + min(seconds, 300),))

    def close_window(self):
        with self.db() as db:
            db.execute('UPDATE settings SET until=0 WHERE id=1')
            db.execute("UPDATE requests SET status='denied' WHERE status='pending'")

    def start(self, name, address, secret):
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or not name.isprintable():
            raise ValueError('invalid device name')
        hashed = digest(secret)
        now = self.clock()
        request_id, code = secrets.token_hex(16), f'{secrets.randbelow(1000000):06d}'
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM requests WHERE expires < ?', (now - 300,))
            if db.execute('SELECT until FROM settings WHERE id=1').fetchone()[0] <= now:
                raise ValueError('Open Pair devices inside Herdr first')
            if db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] >= 32:
                raise ValueError('Too many pairing requests; try later')
            if db.execute('SELECT COUNT(*) FROM requests WHERE address=?', (address,)).fetchone()[0] >= 5:
                raise ValueError('Too many pairing requests; try later')
            if db.execute('SELECT COUNT(*) FROM devices WHERE revoked=0').fetchone()[0] >= 16:
                raise ValueError('Revoke an unused device first')
            if db.execute('SELECT 1 FROM devices WHERE secret_hash=?', (hashed,)).fetchone():
                raise ValueError('Device credential already used')
            db.execute('INSERT INTO requests(id,name,address,secret_hash,code_hash,expires) VALUES(?,?,?,?,?,?)',
                       (request_id, name, address, hashed, hashlib.sha256((request_id + code).encode()).hexdigest(), now + 180))
        return {'requestId': request_id, 'code': code, 'expiresIn': 180}

    def status(self, request_id, secret):
        if not isinstance(request_id, str) or len(request_id) != 32: raise ValueError('Pairing request unavailable')
        hashed = digest(secret)
        with self.db() as db:
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
        if row is None or not hmac.compare_digest(hashed, row['secret_hash']):
            raise ValueError('Pairing request unavailable')
        status = row['status']
        if row['expires'] <= self.clock() and status == 'pending':
            status = 'expired'
        if status == 'approved' and not self.authorized(request_id, secret): status = 'revoked'
        return {'status': status, 'deviceId': request_id if status == 'approved' else None}

    def pending(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,name,address,expires FROM requests WHERE status='pending' AND expires>? ORDER BY expires", (self.clock(),))]

    def approve(self, request_id, code):
        now = self.clock()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if db.execute('SELECT until FROM settings WHERE id=1').fetchone()[0] <= now:
                return False
            if row is None or row['status'] != 'pending' or row['expires'] <= now or row['attempts'] >= 5:
                return False
            good = isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit()
            good = good and hmac.compare_digest(hashlib.sha256((request_id + code).encode()).hexdigest(), row['code_hash'])
            if not good:
                db.execute('UPDATE requests SET attempts=attempts+1 WHERE id=?', (request_id,))
                return False
            if db.execute('SELECT COUNT(*) FROM devices WHERE revoked=0').fetchone()[0] >= 16:
                return False
            db.execute('INSERT INTO devices VALUES(?,?,?,?,0)', (request_id, row['name'], row['secret_hash'], now))
            db.execute("UPDATE requests SET status='approved' WHERE id=?", (request_id,))
        return True

    def authorized(self, device_id, secret):
        if not isinstance(device_id, str) or len(device_id) != 32: return False
        try:
            hashed = digest(secret)
        except (ValueError, UnicodeError):
            return False
        with self.db() as db:
            row = db.execute('SELECT secret_hash FROM devices WHERE id=? AND revoked=0', (device_id,)).fetchone()
        return row is not None and hmac.compare_digest(hashed, row[0])

    def devices(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT id,name,created FROM devices WHERE revoked=0 ORDER BY created')]

    def revoke(self, device_id):
        with self.db() as db:
            db.execute('UPDATE devices SET revoked=1 WHERE id=?', (device_id,))

    def deny(self, request_id):
        with self.db() as db:
            db.execute("UPDATE requests SET status='denied' WHERE id=? AND status='pending'", (request_id,))

    def cancel(self, request_id, secret):
        # A device may cancel only its own pending request or newly issued grant.
        status = self.status(request_id, secret)
        if status['status'] == 'approved': self.revoke(request_id)
        self.deny(request_id)
