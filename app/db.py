"""SQLite connection management and schema for the pump reliability app.

Schema notes
------------
``life_table`` keeps one row per distinct hour with the point-in-time counts
and the *base* cumulative incidence.  Single-pump reclassifications between
``failure`` and ``preventive`` do not rewrite the whole suffix; instead the
constant cumulative-incidence correction is stored in three small auxiliary
tables:

``cif_base``
    One row per touched time point holding the *base* hazards and the S(t-)
    from the last full rebuild.  Reclassifications compute their suffix
    correction against this base, so flipping a pump back restores the
    original numbers exactly instead of accumulating rounding drift.

``cif_delta``
    One logical suffix delta per adjusted time point (used when the full
    table is materialised for the dashboard / full export).
``cif_tree``
    A Fenwick (binary indexed) tree over the same deltas.  A suffix update
    and a point query each touch only O(log M) rows, where M is the number
    of summary time points.

All auxiliary tables are always written in the same transaction as the
ledger and the summary, and are cleared whenever the summary is rebuilt from
scratch, so a reclassification is never applied twice.
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.environ.get(
    "PUMP_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "instance", "pumps.db"),
)

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    pump_id     TEXT PRIMARY KEY,
    entry_hours REAL NOT NULL CHECK (entry_hours >= 0),
    exit_hours  REAL NOT NULL CHECK (exit_hours >= entry_hours),
    event_type  TEXT NOT NULL CHECK (event_type IN ('failure', 'preventive', 'censored')),
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS life_table (
    t_hours          REAL PRIMARY KEY,
    seq              INTEGER,
    entered          INTEGER NOT NULL,
    at_risk          INTEGER NOT NULL,
    failures         INTEGER NOT NULL,
    preventive       INTEGER NOT NULL,
    censored         INTEGER NOT NULL,
    hazard_failure   REAL NOT NULL,
    hazard_preventive REAL NOT NULL,
    survival         REAL NOT NULL,
    cum_failure      REAL NOT NULL,
    cum_preventive   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cif_base (
    seq              INTEGER PRIMARY KEY,
    base_failures    INTEGER NOT NULL,
    base_preventive  INTEGER NOT NULL,
    base_hazard_failure   REAL NOT NULL,
    base_hazard_preventive REAL NOT NULL,
    survival_before  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cif_delta (
    seq             INTEGER PRIMARY KEY,
    delta_failure   REAL NOT NULL DEFAULT 0,
    delta_preventive REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cif_tree (
    seq             INTEGER PRIMARY KEY,
    delta_failure   REAL NOT NULL DEFAULT 0,
    delta_preventive REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_db_path() -> str:
    return DEFAULT_DB_PATH


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection with WAL and foreign keys enabled."""
    path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_life_table_seq(conn: sqlite3.Connection) -> None:
    """Add/populate ``life_table.seq`` and its unique index (old databases)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(life_table)")}
    if "seq" not in columns:
        conn.execute("ALTER TABLE life_table ADD COLUMN seq INTEGER")

    missing = conn.execute(
        "SELECT COUNT(*) AS c FROM life_table WHERE seq IS NULL"
    ).fetchone()["c"]
    if missing:
        # One-time upgrade cost: number the existing rows in hour order.  The
        # unique index has not been created yet on a fresh upgrade; if it does
        # exist, NULLing first keeps the per-row updates collision free.
        conn.execute("UPDATE life_table SET seq = NULL")
        rows = conn.execute(
            "SELECT t_hours FROM life_table ORDER BY t_hours"
        ).fetchall()
        conn.executemany(
            "UPDATE life_table SET seq = ? WHERE t_hours = ?",
            [(idx, row["t_hours"]) for idx, row in enumerate(rows, start=1)],
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_life_table_seq ON life_table(seq)"
    )


def _ensure_aux_tables(conn: sqlite3.Connection) -> None:
    """Create the correction auxiliary tables on a pre-existing database."""
    for statement in (
        "CREATE TABLE IF NOT EXISTS cif_base ("
        " seq INTEGER PRIMARY KEY, base_failures INTEGER NOT NULL,"
        " base_preventive INTEGER NOT NULL,"
        " base_hazard_failure REAL NOT NULL,"
        " base_hazard_preventive REAL NOT NULL,"
        " survival_before REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS cif_delta ("
        " seq INTEGER PRIMARY KEY, delta_failure REAL NOT NULL DEFAULT 0,"
        " delta_preventive REAL NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS cif_tree ("
        " seq INTEGER PRIMARY KEY, delta_failure REAL NOT NULL DEFAULT 0,"
        " delta_preventive REAL NOT NULL DEFAULT 0)",
    ):
        conn.execute(statement)


def _resolve_pending_deltas(conn: sqlite3.Connection) -> None:
    """Reconcile a database that carries un-baked suffix corrections.

    Versions that stored corrections only in ``cif_delta``/``cif_tree``
    cannot be safely rebased by folding numbers into cumulative values (the
    per-time-point hazards were adjusted too).  Rebuilding from the ledger
    once is exact, keeps the visible results, and needs no CSV re-upload.
    Baseline databases have no deltas and skip this entirely.
    """
    if not conn.execute("SELECT COUNT(*) AS c FROM cif_delta").fetchone()["c"]:
        return
    from .lifetable import PumpRecord, build_life_table  # local: no cycle

    records = [
        PumpRecord(r["pump_id"], r["entry_hours"], r["exit_hours"],
                   r["event_type"])
        for r in conn.execute(
            "SELECT pump_id, entry_hours, exit_hours, event_type FROM ledger"
        )
    ]
    rows = build_life_table(records)
    columns = ("t_hours", "entered", "at_risk", "failures", "preventive",
               "censored", "hazard_failure", "hazard_preventive", "survival",
               "cum_failure", "cum_preventive")
    conn.execute("DELETE FROM life_table")
    conn.executemany(
        "INSERT INTO life_table (seq, " + ", ".join(columns) + ") VALUES ("
        + ", ".join("?" for _ in range(len(columns) + 1)) + ")",
        [tuple([seq] + [row[col] for col in columns])
         for seq, row in enumerate(rows, start=1)],
    )
    conn.execute("DELETE FROM cif_delta")
    conn.execute("DELETE FROM cif_tree")
    conn.execute("DELETE FROM cif_base")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('time_point_count', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(len(rows)),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('pump_count', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(len(records)),),
    )


def _ensure_meta(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'time_point_count'"
    ).fetchone()
    if row is None:
        count = conn.execute("SELECT COUNT(*) AS c FROM life_table").fetchone()["c"]
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('time_point_count', ?)",
            (str(count),),
        )


def init_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema.  Cheap and safe to call repeatedly.

    If the connection already has an open transaction we leave it alone:
    callers begin their transactions after initialising the schema, and a
    query's read snapshot must not be committed by a nested DDL call.  Once
    the schema is current this only reads ``PRAGMA user_version``, so hot
    read paths never issue DDL or take a write lock.

    The upgrade itself is serialised with ``BEGIN IMMEDIATE`` and re-checks
    the version inside the transaction, so two workers starting at the same
    moment on an old database cannot both run ``ALTER TABLE``.
    """
    if conn.in_transaction:
        return
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA)  # idempotent; adds the auxiliary tables
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            _ensure_life_table_seq(conn)
            _ensure_aux_tables(conn)
            _resolve_pending_deltas(conn)
            _ensure_meta(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
