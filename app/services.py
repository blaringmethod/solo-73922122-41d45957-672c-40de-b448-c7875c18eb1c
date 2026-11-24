"""Transaction-bound services: ledger writes and summary rebuilds.

Every write path updates the ledger, the summary and the auxiliary
correction structures in a single SQLite transaction.  If any step fails the
transaction is rolled back and the previous ledger and summary stay intact.

Two write paths exist:

``replace_ledger``
    CSV upload: the ledger is replaced wholesale and the summary is rebuilt
    from that ledger.  Any earlier accumulated correction deltas are
    discarded first, so previously applied reclassifications cannot be
    counted twice.

``upsert_pump``
    Single-pump correction.  When the only change is the exit reason between
    ``failure`` and ``preventive`` while entry/exit hours stay identical, the
    update is incremental: only this pump's ledger row and O(1) summary rows
    are read, the affected summary row is adjusted in place, and the constant
    suffix correction is recorded in a Fenwick tree (O(log M) rows written,
    M = number of summary time points).  Every other correction falls back to
    a full rebuild.

Query helpers read the summary base rows plus the logarithmic suffix
correction inside one read transaction, so a query never mixes a summary row
from one committed state with auxiliary rows from another.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .db import init_db
from .lifetable import PumpRecord, build_life_table

LIFE_TABLE_COLUMNS = (
    "seq", "t_hours", "entered", "at_risk", "failures", "preventive",
    "censored", "hazard_failure", "hazard_preventive", "survival",
    "cum_failure", "cum_preventive",
)

# Only these two exit reasons are interchangeable without touching entry/exit
# hours or the risk set.
RECLASSIFIABLE = frozenset(("failure", "preventive"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def _read_tx(conn: sqlite3.Connection):
    """One committed snapshot for all reads inside the block (WAL readers)."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if own:
            conn.rollback()  # read-only: release the snapshot without writing


def _clear_correction_deltas(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM cif_base")
    conn.execute("DELETE FROM cif_delta")
    conn.execute("DELETE FROM cif_tree")


def _rebuild_summary(conn: sqlite3.Connection, records: list[PumpRecord]) -> None:
    """Full recompute from the ledger that is to be stored.

    The correction tables are cleared *before* the new base summary is
    written: the ledger already contains every correction, so no previously
    applied delta may be carried over on top of the rebuilt base values.
    """
    rows = build_life_table(records)
    conn.execute("DELETE FROM life_table")
    _clear_correction_deltas(conn)
    placeholders = ",".join("?" for _ in LIFE_TABLE_COLUMNS)
    conn.executemany(
        f"INSERT INTO life_table ({','.join(LIFE_TABLE_COLUMNS)}) "
        f"VALUES ({placeholders})",
        [
            tuple([seq] + [row[col] for col in LIFE_TABLE_COLUMNS[1:]])
            for seq, row in enumerate(rows, start=1)
        ],
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('pump_count', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(len(records)),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('time_point_count', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(len(rows)),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('summary_updated_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_now(),),
    )


def _load_all_records(conn: sqlite3.Connection) -> list[PumpRecord]:
    return [
        PumpRecord(r["pump_id"], r["entry_hours"], r["exit_hours"], r["event_type"])
        for r in conn.execute(
            "SELECT pump_id, entry_hours, exit_hours, event_type FROM ledger"
        )
    ]


def _set_source(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_source', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (source,),
    )


# -------------------------------------------------- incremental corrections --

def _tree_add(conn: sqlite3.Connection, index: int, size: int,
              change_f: float, change_p: float) -> None:
    """Add a change to one time point in the Fenwick tree (O(log M) rows)."""
    if change_f == 0.0 and change_p == 0.0:
        return
    i = index
    while i <= size:
        conn.execute(
            "INSERT INTO cif_tree(seq, delta_failure, delta_preventive)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(seq) DO UPDATE SET"
            " delta_failure = delta_failure + excluded.delta_failure,"
            " delta_preventive = delta_preventive + excluded.delta_preventive",
            (i, change_f, change_p),
        )
        i += i & -i


def _delta_at(conn: sqlite3.Connection, index: int) -> tuple[float, float]:
    row = conn.execute(
        "SELECT delta_failure, delta_preventive FROM cif_delta WHERE seq = ?",
        (index,),
    ).fetchone()
    if row is None:
        return 0.0, 0.0
    return row["delta_failure"], row["delta_preventive"]


def _tree_prefix(conn: sqlite3.Connection, index: int) -> tuple[float, float]:
    """Prefix sum over deltas at time points 1..index (O(log M) rows read)."""
    total_f = 0.0
    total_p = 0.0
    i = index
    while i > 0:
        row = conn.execute(
            "SELECT delta_failure, delta_preventive FROM cif_tree WHERE seq = ?",
            (i,),
        ).fetchone()
        if row is not None:
            total_f += row["delta_failure"]
            total_p += row["delta_preventive"]
        i -= i & -i
    return total_f, total_p


def _time_point_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'time_point_count'"
    ).fetchone()
    if row is not None:
        return int(row["value"])
    # Fallback for a partially upgraded database.
    row = conn.execute("SELECT MAX(seq) AS m FROM life_table").fetchone()
    return int(row["m"] or 0)


def _apply_reclassification(conn: sqlite3.Connection,
                            old_event: str, new_event: str,
                            exit_hours: float) -> None:
    """Incrementally reclassify one pump at its (unchanged) exit hour.

    Only this exit hour's summary row (plus the previous row for S(t-)) is
    read.  The total hazard at that hour is unchanged, so S(t) and the risk
    set stay valid; the cause-specific cumulative incidences receive a
    constant suffix correction S(t-) * (current hazard - base hazard).

    The base hazards are captured the first time a time point is touched
    after a full rebuild.  Because the correction is always recomputed from
    the current counts versus that base, flipping a pump back yields an
    exactly zero delta instead of a rounding residue.
    """
    row = conn.execute(
        "SELECT seq, at_risk, failures, preventive, hazard_failure,"
        " hazard_preventive FROM life_table WHERE t_hours = ?",
        (exit_hours,),
    ).fetchone()
    if row is None:
        raise ValueError(f"汇总中缺少出册时点 {exit_hours}，无法增量改判")

    n = row["at_risk"]
    if n <= 0:
        raise ValueError("出册时点的在册数为 0，无法增量改判")

    base = conn.execute(
        "SELECT base_failures, base_preventive, base_hazard_failure,"
        " base_hazard_preventive, survival_before FROM cif_base WHERE seq = ?",
        (row["seq"],),
    ).fetchone()
    if base is None:
        prev = conn.execute(
            "SELECT survival FROM life_table WHERE t_hours < ?"
            " ORDER BY t_hours DESC LIMIT 1",
            (exit_hours,),
        ).fetchone()
        survival_before = prev["survival"] if prev is not None else 1.0
        conn.execute(
            "INSERT INTO cif_base(seq, base_failures, base_preventive,"
            " base_hazard_failure, base_hazard_preventive, survival_before)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (row["seq"], row["failures"], row["preventive"],
             row["hazard_failure"], row["hazard_preventive"], survival_before),
        )
        base_f = row["failures"]
        base_p = row["preventive"]
        base_hazard_f = row["hazard_failure"]
        base_hazard_p = row["hazard_preventive"]
    else:
        survival_before = base["survival_before"]
        base_f = base["base_failures"]
        base_p = base["base_preventive"]
        base_hazard_f = base["base_hazard_failure"]
        base_hazard_p = base["base_hazard_preventive"]

    old_f = 1 if old_event == "failure" else 0
    old_p = 1 if old_event == "preventive" else 0
    new_f = 1 if new_event == "failure" else 0
    new_p = 1 if new_event == "preventive" else 0

    failures = row["failures"] - old_f + new_f
    preventive = row["preventive"] - old_p + new_p
    if failures < 0 or preventive < 0:
        raise ValueError("汇总计数与台账不一致，请重新上传台账")

    hazard_f = failures / n
    hazard_p = preventive / n
    # Absolute correction relative to the post-rebuild base: identical
    # arithmetic when the pump returns to its base reason -> exactly zero.
    delta_f = survival_before * (hazard_f - base_hazard_f)
    delta_p = survival_before * (hazard_p - base_hazard_p)
    if failures == base_f:
        delta_f = 0.0
    if preventive == base_p:
        delta_p = 0.0

    # Changing the logical delta means the Fenwick tree must move by the
    # difference between the new and the stored delta; read the stored value
    # before overwriting it.  This also makes flipping a pump back restore
    # the base values exactly (delta becomes zero).
    current_f, current_p = _delta_at(conn, row["seq"])

    conn.execute(
        "UPDATE life_table SET failures = ?, preventive = ?,"
        " hazard_failure = ?, hazard_preventive = ? WHERE seq = ?",
        (failures, preventive, hazard_f, hazard_p, row["seq"]),
    )
    # Logical delta (used when materialising the whole table) ...
    conn.execute(
        "INSERT INTO cif_delta(seq, delta_failure, delta_preventive)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(seq) DO UPDATE SET"
        " delta_failure = excluded.delta_failure,"
        " delta_preventive = excluded.delta_preventive",
        (row["seq"], delta_f, delta_p),
    )
    # ... and the Fenwick tree for O(log M) suffix queries.
    _tree_add(conn, row["seq"], _time_point_count(conn),
              delta_f - current_f, delta_p - current_p)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('summary_updated_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_now(),),
    )


def _is_reclassification_only(existing: sqlite3.Row,
                               record: PumpRecord) -> bool:
    return (
        existing["entry_hours"] == record.entry_hours
        and existing["exit_hours"] == record.exit_hours
        and existing["event_type"] != record.event_type
        and existing["event_type"] in RECLASSIFIABLE
        and record.event_type in RECLASSIFIABLE
    )


# --------------------------------------------------------------- write paths --

def replace_ledger(conn: sqlite3.Connection, records: list[PumpRecord],
                   source: str = "csv-upload") -> dict:
    """Replace the whole ledger and rebuild the summary atomically."""
    init_db(conn)
    with _write_tx(conn):
        conn.execute("DELETE FROM ledger")
        conn.executemany(
            "INSERT INTO ledger (pump_id, entry_hours, exit_hours, event_type, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(r.pump_id, r.entry_hours, r.exit_hours, r.event_type, _now())
             for r in records],
        )
        _rebuild_summary(conn, records)
        _set_source(conn, source)
    return {"pump_count": len(records)}


def upsert_pump(conn: sqlite3.Connection, record: PumpRecord) -> dict:
    """Correct a single pump without the caller supplying other pumps.

    Callers never need to read or resubmit the other pumps' details.  A pure
    failure/preventive reclassification is applied in place in O(log M);
    anything else rebuilds the summary from the ledger inside the same
    transaction.
    """
    init_db(conn)
    with _write_tx(conn):
        existing = conn.execute(
            "SELECT pump_id, entry_hours, exit_hours, event_type FROM ledger"
            " WHERE pump_id = ?",
            (record.pump_id,),
        ).fetchone()
        action = "updated" if existing else "inserted"

        unchanged = (
            existing is not None
            and existing["entry_hours"] == record.entry_hours
            and existing["exit_hours"] == record.exit_hours
            and existing["event_type"] == record.event_type
        )
        incremental = existing is not None and _is_reclassification_only(
            existing, record)

        conn.execute(
            "INSERT INTO ledger (pump_id, entry_hours, exit_hours, event_type, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(pump_id) DO UPDATE SET"
            " entry_hours=excluded.entry_hours,"
            " exit_hours=excluded.exit_hours,"
            " event_type=excluded.event_type,"
            " updated_at=excluded.updated_at",
            (record.pump_id, record.entry_hours, record.exit_hours,
             record.event_type, _now()),
        )

        if incremental:
            _apply_reclassification(
                conn, existing["event_type"], record.event_type,
                record.exit_hours,
            )
            _set_source(conn, "single-pump-reclassified")
        elif unchanged:
            # Nothing structural changed; no summary work at all.
            _set_source(conn, "single-pump-touch")
        else:
            _rebuild_summary(conn, _load_all_records(conn))
            _set_source(conn, f"single-pump-{action}")
    return {"pump_id": record.pump_id, "action": action}


def reclassify_pump(conn: sqlite3.Connection, pump_id: str,
                    new_event: str) -> dict:
    """Flip one pump's exit reason between failure and preventive only.

    The stored entry/exit hours are reused verbatim; the request only names
    the pump and the target reason.  Everything (lookup, validation, ledger
    update, summary correction) happens inside one write transaction, so a
    concurrent edit cannot slip between the check and the update.
    """
    if new_event not in RECLASSIFIABLE:
        raise ValueError("只能在本泵的 故障 / 预防更换 之间改判出册原因")
    init_db(conn)
    with _write_tx(conn):
        existing = conn.execute(
            "SELECT pump_id, entry_hours, exit_hours, event_type FROM ledger"
            " WHERE pump_id = ?",
            (pump_id,),
        ).fetchone()
        if existing is None:
            raise LookupError(f"泵 {pump_id} 不在台账中")
        if existing["event_type"] not in RECLASSIFIABLE:
            raise ValueError("只能在本泵的 故障 / 预防更换 之间改判出册原因")
        if existing["event_type"] == new_event:
            raise ValueError("只能在本泵的 故障 / 预防更换 之间改判出册原因")

        conn.execute(
            "UPDATE ledger SET event_type = ?, updated_at = ?"
            " WHERE pump_id = ?",
            (new_event, _now(), pump_id),
        )
        _apply_reclassification(
            conn, existing["event_type"], new_event, existing["exit_hours"])
        _set_source(conn, "single-pump-reclassified")
    return {"pump_id": pump_id, "action": "updated"}


# ------------------------------------------------------------------ ledger --

def get_pump(conn: sqlite3.Connection, pump_id: str) -> sqlite3.Row | None:
    init_db(conn)
    return conn.execute(
        "SELECT pump_id, entry_hours, exit_hours, event_type, updated_at"
        " FROM ledger WHERE pump_id = ?",
        (pump_id,),
    ).fetchone()


def list_pumps(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    init_db(conn)
    return list(conn.execute(
        "SELECT pump_id, entry_hours, exit_hours, event_type, updated_at"
        " FROM ledger ORDER BY pump_id"
    ))


# ---------------------------------------------------------------- queries --
# Everything below is read-only.  Each public helper reads the summary base
# and its auxiliary correction rows inside one committed snapshot.

def _materialize(conn: sqlite3.Connection,
                 rows: list[sqlite3.Row]) -> list[dict]:
    """Apply the stored suffix corrections to base cumulative values."""
    deltas = [
        (r["seq"], r["delta_failure"], r["delta_preventive"])
        for r in conn.execute(
            "SELECT seq, delta_failure, delta_preventive FROM cif_delta"
            " ORDER BY seq"
        )
    ]
    delta_index = 0
    run_f = 0.0
    run_p = 0.0
    result: list[dict] = []
    for row in rows:
        while delta_index < len(deltas) and deltas[delta_index][0] <= row["seq"]:
            run_f += deltas[delta_index][1]
            run_p += deltas[delta_index][2]
            delta_index += 1
        item = dict(row)
        item.pop("seq", None)  # internal ordinal, keep the public row shape
        item["cum_failure"] = row["cum_failure"] + run_f
        item["cum_preventive"] = row["cum_preventive"] + run_p
        result.append(item)
    return result


def get_life_table(conn: sqlite3.Connection) -> list[dict]:
    init_db(conn)
    with _read_tx(conn):
        rows = list(conn.execute(
            "SELECT * FROM life_table ORDER BY t_hours"
        ))
        return _materialize(conn, rows)


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    init_db(conn)
    with _read_tx(conn):
        return {r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM meta")}


def get_dashboard(conn: sqlite3.Connection) -> tuple[dict[str, str], list[dict]]:
    """Meta + materialised summary from one committed snapshot."""
    init_db(conn)
    with _read_tx(conn):
        meta = {r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM meta")}
        rows = list(conn.execute(
            "SELECT * FROM life_table ORDER BY t_hours"
        ))
        return meta, _materialize(conn, rows)


def query_incidence(conn: sqlite3.Connection, hours: float) -> dict:
    """Read-only cumulative incidence query backed by the summary table.

    Reads exactly one summary row (the last time point <= hours) and O(log M)
    auxiliary Fenwick rows, all from the same committed snapshot.
    """
    if hours < 0:
        raise ValueError("查询小时数不能为负")
    init_db(conn)
    with _read_tx(conn):
        chosen = conn.execute(
            "SELECT * FROM life_table WHERE t_hours <= ?"
            " ORDER BY t_hours DESC LIMIT 1",
            (hours + 1e-9,),
        ).fetchone()
        meta = {r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM meta")}
        if chosen is None:
            return {
                "hours": float(hours),
                "cum_failure": 0.0,
                "cum_preventive": 0.0,
                "survival": 1.0,
                "basis_row": None,
                "meta": meta,
            }
        delta_f, delta_p = _tree_prefix(conn, chosen["seq"])
        basis = dict(chosen)
        basis.pop("seq", None)  # internal ordinal, not part of the public row
        basis["cum_failure"] = chosen["cum_failure"] + delta_f
        basis["cum_preventive"] = chosen["cum_preventive"] + delta_p
        return {
            "hours": float(hours),
            "cum_failure": basis["cum_failure"],
            "cum_preventive": basis["cum_preventive"],
            "survival": chosen["survival"],
            "basis_row": basis,
            "meta": meta,
        }
