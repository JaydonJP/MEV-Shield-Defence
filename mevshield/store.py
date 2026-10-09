"""SQLite storage shared by the CLI, Guardian, bots, benchmark and dashboard."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tx_hash TEXT, sender TEXT, label TEXT, severity TEXT,
    score REAL, features TEXT, action TEXT, latency_ms REAL, block INTEGER
);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, amount REAL, token_in TEXT, slippage REAL,
    harm_public REAL, attacker_profit REAL, p_attack REAL, harm_hint REAL, harm_private REAL,
    closed_form_harm REAL, recommendation TEXT, costs TEXT, sim_ms REAL
);
CREATE TABLE IF NOT EXISTS blocks (
    number INTEGER PRIMARY KEY, ts REAL, txs TEXT
);
CREATE TABLE IF NOT EXISTS swaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tx_hash TEXT, user TEXT, amount REAL, defense TEXT,
    expected_out REAL, actual_out REAL, harm_usd REAL, sandwiched INTEGER, block INTEGER
);
CREATE TABLE IF NOT EXISTS relay_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, raw TEXT, tx_hash TEXT, sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS guardian_seen (
    tx_hash TEXT PRIMARY KEY, ts REAL
);
CREATE TABLE IF NOT EXISTS heartbeats (
    name TEXT PRIMARY KEY, ts REAL, info TEXT
);
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, data TEXT
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def clear_runtime() -> None:
    """Forget per-session state (used by `up` / `reset`). Experiments are kept."""
    with connect() as db:
        for table in ("alerts", "predictions", "blocks", "swaps", "relay_queue", "guardian_seen"):
            db.execute(f"DELETE FROM {table}")


def add_alert(tx_hash: str, sender: str, label: str, severity: str, score: float, features: dict,
              action: str = "", latency_ms: float = 0.0, block: int | None = None) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO alerts (ts, tx_hash, sender, label, severity, score, features, action, latency_ms, block)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), tx_hash, sender, label, severity, score, json.dumps(features, default=str), action,
             latency_ms, block),
        )


def add_prediction(row: dict) -> None:
    cols = ["amount", "token_in", "slippage", "harm_public", "attacker_profit", "p_attack", "harm_hint",
            "harm_private", "closed_form_harm", "recommendation", "costs", "sim_ms"]
    with connect() as db:
        db.execute(
            f"INSERT INTO predictions (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            (time.time(), *[row.get(c) if c != "costs" else json.dumps(row.get(c)) for c in cols]),
        )


def add_block(number: int, txs: list[dict]) -> None:
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO blocks (number, ts, txs) VALUES (?,?,?)",
                   (number, time.time(), json.dumps(txs, default=str)))


def add_swap(row: dict) -> None:
    cols = ["tx_hash", "user", "amount", "defense", "expected_out", "actual_out", "harm_usd", "sandwiched", "block"]
    with connect() as db:
        db.execute(f"INSERT INTO swaps (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                   (time.time(), *[row.get(c) for c in cols]))


def heartbeat(name: str, info: dict | None = None) -> None:
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO heartbeats (name, ts, info) VALUES (?,?,?)",
                   (name, time.time(), json.dumps(info or {})))


def is_alive(name: str, max_age: float = 3.0) -> bool:
    with connect() as db:
        row = db.execute("SELECT ts FROM heartbeats WHERE name=?", (name,)).fetchone()
    return bool(row) and time.time() - row["ts"] < max_age


def mark_seen(tx_hashes: list[str]) -> None:
    with connect() as db:
        db.executemany("INSERT OR IGNORE INTO guardian_seen (tx_hash, ts) VALUES (?,?)",
                       [(h.lower(), time.time()) for h in tx_hashes])


def seen(tx_hashes: list[str]) -> set[str]:
    if not tx_hashes:
        return set()
    with connect() as db:
        q = ",".join("?" * len(tx_hashes))
        rows = db.execute(f"SELECT tx_hash FROM guardian_seen WHERE tx_hash IN ({q})",
                          [h.lower() for h in tx_hashes]).fetchall()
    return {r["tx_hash"] for r in rows}


def relay_push(raw: str, tx_hash: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO relay_queue (ts, raw, tx_hash) VALUES (?,?,?)", (time.time(), raw, tx_hash))


def relay_pop_all() -> list[sqlite3.Row]:
    with connect() as db:
        rows = db.execute("SELECT * FROM relay_queue WHERE sent=0 ORDER BY id").fetchall()
        db.execute("UPDATE relay_queue SET sent=1 WHERE sent=0")
    return rows


def save_experiment(name: str, data) -> None:
    with connect() as db:
        db.execute("INSERT INTO experiments (ts, name, data) VALUES (?,?,?)",
                   (time.time(), name, json.dumps(data, default=str)))


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
