"""Database connection + query helpers."""
import sqlite3

DB_FILE = "business_vault.db"


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def q(sql, params=(), fetch=False):
    conn = db()
    cur  = conn.cursor()
    cur.execute(sql, params)
    result = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result
