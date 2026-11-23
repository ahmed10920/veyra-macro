"""One local SQLite database per Windows user.

Created empty on first run under %LOCALAPPDATA%\\Veyra\\veyra.db, so a fresh
GitHub download starts with no settings, no clicks, no history and no merchant
data. Old config.json / history.jsonl files are imported once when found.
"""
import json
import os
import shutil
import sqlite3
import sys
import threading

DB_FILE = os.path.join(os.getenv('LOCALAPPDATA') or os.path.expanduser('~'), 'Veyra', 'veyra.db')
# An empty template ships in the repo / exe, so first run always starts clean.
BUNDLED_DB = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'veyra.db')
_lock = threading.RLock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        if not os.path.exists(DB_FILE) and os.path.isfile(BUNDLED_DB):
            try:
                shutil.copy2(BUNDLED_DB, DB_FILE)
            except OSError:
                pass
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _conn.execute('PRAGMA journal_mode=WAL')
        _conn.executescript(
            'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);'
            'CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'ts REAL, time TEXT, biome TEXT, day TEXT);'
            'CREATE TABLE IF NOT EXISTS merchant_events (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'ts REAL, name TEXT, how TEXT, server TEXT);'
        )
        _conn.commit()
    return _conn


def get_settings():
    """All stored settings as a plain dict."""
    with _lock:
        rows = _db().execute('SELECT key, value FROM settings').fetchall()
    out = {}
    for key, value in rows:
        try:
            out[key] = json.loads(value)
        except (TypeError, ValueError):
            pass
    return out


def save_settings(values):
    """Write settings into the DB (merge, not replace)."""
    with _lock:
        c = _db()
        for key, value in (values or {}).items():
            c.execute('INSERT INTO settings(key, value) VALUES(?, ?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (str(key), json.dumps(value)))
        c.commit()


def add_history(ts, stamp, biome, day=''):
    with _lock:
        c = _db()
        c.execute('INSERT INTO history(ts, time, biome, day) VALUES(?, ?, ?, ?)',
                  (float(ts), str(stamp), str(biome), str(day)))
        c.commit()


def history(limit=100):
    with _lock:
        rows = _db().execute(
            'SELECT ts, time, biome, day FROM history ORDER BY id DESC LIMIT ?', (int(limit),)
        ).fetchall()
    return [{'ts': ts, 'time': t, 'biome': b, 'day': d} for ts, t, b, d in reversed(rows)]


def history_counts():
    """Every saved visit, not just the latest page."""
    with _lock:
        rows = _db().execute('SELECT biome, COUNT(*) FROM history GROUP BY biome').fetchall()
    return {b: int(n) for b, n in rows}


def latest_history():
    """(timestamp, biome) of the newest saved visit, or (0, None)."""
    with _lock:
        row = _db().execute('SELECT ts, biome FROM history ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        return 0.0, None
    try:
        return float(row[0] or 0), row[1]
    except (TypeError, ValueError):
        return 0.0, row[1]


def history_times():
    """Every saved (timestamp, biome), used to avoid importing the same visit twice."""
    with _lock:
        return _db().execute('SELECT ts, biome FROM history').fetchall()


def add_merchant(name, how='', server='', ts=None):
    with _lock:
        c = _db()
        c.execute('INSERT INTO merchant_events(ts, name, how, server) VALUES(?, ?, ?, ?)',
                  (float(ts or 0), str(name), str(how), str(server)))
        c.commit()


def merchant_events(limit=50):
    with _lock:
        rows = _db().execute(
            'SELECT ts, name, how, server FROM merchant_events ORDER BY id DESC LIMIT ?', (int(limit),)
        ).fetchall()
    return [{'ts': ts, 'name': n, 'how': h, 'server': s} for ts, n, h, s in rows]


def reset():
    """Everything empty again (used before publishing / for a clean slate)."""
    with _lock:
        c = _db()
        for table in ('settings', 'history', 'merchant_events'):
            c.execute('DELETE FROM ' + table)
        c.commit()


def stats():
    """Counts for diagnostics."""
    with _lock:
        c = _db()
        return {
            'settings': c.execute('SELECT COUNT(*) FROM settings').fetchone()[0],
            'history': c.execute('SELECT COUNT(*) FROM history').fetchone()[0],
            'merchant_events': c.execute('SELECT COUNT(*) FROM merchant_events').fetchone()[0],
            'file': DB_FILE,
        }
