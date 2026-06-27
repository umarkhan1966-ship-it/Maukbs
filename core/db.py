"""Database connection + query helpers."""
import sqlite3

DB_FILE = "business_vault.db"


def db():
    # timeout: wait up to 30s for a transient lock (e.g. OneDrive syncing the
    # file, or another request mid-write) instead of failing instantly with
    # "database is locked".
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def q(sql, params=(), fetch=False):
    conn = db()
    cur  = conn.cursor()
    cur.execute(sql, params)
    result = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result
