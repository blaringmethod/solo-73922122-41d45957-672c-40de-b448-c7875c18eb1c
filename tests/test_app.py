"""Unit and end-to-end tests (stdlib unittest, no extra dependencies)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="pump-test-")
os.environ["PUMP_DB"] = os.path.join(_tmp_dir, "test.db")
os.environ["FLASK_SECRET_KEY"] = "test-secret"

from app import create_app, services  # noqa: E402
from app.csvio import parse_csv  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.lifetable import (PumpRecord, build_life_table, incidence_at)  # noqa: E402

SAMPLE_CSV = """泵号,入册累计运行小时,出册累计运行小时,结束方式
P001,0,1000,故障
P002,0,1000,预防更换
P003,0,2000,观察结束
P004,500,2000,故障
P005,1000,3000,观察结束
P006,0,3000,预防更换
"""


def sample_records():
    return parse_csv(SAMPLE_CSV)


class LifeTableMathTests(unittest.TestCase):
    def setUp(self):
        self.rows = build_life_table(sample_records())
        self.by_t = {r["t_hours"]: r for r in self.rows}

    def test_time_points_and_order(self):
        self.assertEqual([r["t_hours"] for r in self.rows],
                         [0.0, 500.0, 1000.0, 2000.0, 3000.0])

    def test_entry_before_risk_count(self):
        # t=0: four pumps enter before any rate is computed
        r0 = self.by_t[0.0]
        self.assertEqual(r0["entered"], 4)
        self.assertEqual(r0["at_risk"], 4)
        # t=500: P004 enters, no exits -> risk set 5
        self.assertEqual(self.by_t[500.0]["at_risk"], 5)
        # t=1000: P005 enters (6 at risk), then 1 failure + 1 preventive leave
        r1000 = self.by_t[1000.0]
        self.assertEqual(r1000["entered"], 1)
        self.assertEqual(r1000["at_risk"], 6)
        self.assertEqual(r1000["failures"], 1)
        self.assertEqual(r1000["preventive"], 1)

    def test_hazards_and_cif(self):
        r1000 = self.by_t[1000.0]
        self.assertAlmostEqual(r1000["hazard_failure"], 1 / 6)
        self.assertAlmostEqual(r1000["hazard_preventive"], 1 / 6)
        self.assertAlmostEqual(r1000["survival"], 2 / 3)
        self.assertAlmostEqual(r1000["cum_failure"], 1 / 6)
        self.assertAlmostEqual(r1000["cum_preventive"], 1 / 6)

        r2000 = self.by_t[2000.0]
        # at risk: 4 remaining (P003,P004,P006 + P005); 1 failure, 1 censored
        self.assertEqual(r2000["at_risk"], 4)
        self.assertEqual(r2000["failures"], 1)
        self.assertEqual(r2000["censored"], 1)
        self.assertAlmostEqual(r2000["hazard_failure"], 1 / 4)
        self.assertAlmostEqual(r2000["survival"], (2 / 3) * (3 / 4))
        # CIF weighted by survival just before t (2/3)
        self.assertAlmostEqual(r2000["cum_failure"],
                               1 / 6 + (2 / 3) * (1 / 4))
        self.assertAlmostEqual(r2000["cum_preventive"], 1 / 6)

    def test_censoring_only_leaves_risk_set(self):
        r3000 = self.by_t[3000.0]
        # one censored (P005) and one preventive (P006) leave; n=2
        self.assertEqual(r3000["at_risk"], 2)
        self.assertEqual(r3000["censored"], 1)
        self.assertEqual(r3000["failures"], 0)
        self.assertAlmostEqual(r3000["hazard_failure"], 0.0)
        self.assertAlmostEqual(r3000["cum_failure"],
                               self.by_t[2000.0]["cum_failure"])
        # after all exits the risk set is empty
        self.assertEqual(
            r3000["at_risk"] - r3000["failures"] - r3000["preventive"]
            - r3000["censored"], 0)

    def test_arbitrary_hour_step_function(self):
        before = incidence_at(self.rows, 999.0)
        self.assertAlmostEqual(before["cum_failure"], 0.0)
        at = incidence_at(self.rows, 1000.0)
        self.assertAlmostEqual(at["cum_failure"], 1 / 6)
        between = incidence_at(self.rows, 1500.0)
        self.assertAlmostEqual(between["cum_failure"], 1 / 6)
        self.assertEqual(between["basis_row"]["t_hours"], 1000.0)
        after = incidence_at(self.rows, 99999.0)
        self.assertAlmostEqual(after["cum_failure"],
                               self.by_t[3000.0]["cum_failure"])

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            build_life_table([PumpRecord("A", 10, 5, "failure")])
        with self.assertRaises(ValueError):
            build_life_table([PumpRecord("A", 0, 1, "unknown")])
        with self.assertRaises(ValueError):
            build_life_table([PumpRecord("A", 0, 1, "failure"),
                              PumpRecord("A", 0, 2, "failure")])

    def test_same_hour_entry_and_exit_no_zero_division(self):
        # pump enters and leaves at the same hour: entry is counted first,
        # so the risk set never reaches zero before hazard computation
        rows = build_life_table([
            PumpRecord("A", 100, 100, "failure"),
            PumpRecord("B", 100, 100, "censored"),
        ])
        row = rows[0]
        self.assertEqual(row["t_hours"], 100.0)
        self.assertEqual(row["entered"], 2)
        self.assertEqual(row["at_risk"], 2)
        self.assertAlmostEqual(row["hazard_failure"], 0.5)
        self.assertAlmostEqual(row["cum_failure"], 0.5)
        self.assertAlmostEqual(row["survival"], 0.5)

    def test_all_censored_means_zero_incidence(self):
        rows = build_life_table([
            PumpRecord("A", 0, 100, "censored"),
            PumpRecord("B", 0, 200, "censored"),
        ])
        for row in rows:
            self.assertEqual(row["failures"], 0)
            self.assertEqual(row["preventive"], 0)
            self.assertAlmostEqual(row["cum_failure"], 0.0)
            self.assertAlmostEqual(row["cum_preventive"], 0.0)
            self.assertAlmostEqual(row["survival"], 1.0)

    def test_empty_ledger_gives_empty_summary(self):
        self.assertEqual(build_life_table([]), [])
        self.assertEqual(incidence_at([], 500.0)["cum_failure"], 0.0)


class CsvParseTests(unittest.TestCase):
    def test_english_headers_and_aliases(self):
        text = ("id,entry,exit,event\n"
                "X1,0,100,fail\nX2,0,200,p\nX3,0,300,c\n")
        recs = parse_csv(text)
        self.assertEqual([r.event_type for r in recs],
                         ["failure", "preventive", "censored"])

    def test_bad_rows_collected(self):
        text = ("泵号,入册累计运行小时,出册累计运行小时,结束方式\n"
                "B1,0,100,爆炸\n"
                "B2,abc,100,故障\n"
                "B3,500,100,故障\n")
        with self.assertRaises(ValueError) as ctx:
            parse_csv(text)
        self.assertEqual(str(ctx.exception).count("\n"), 3)

    def test_missing_column(self):
        with self.assertRaises(ValueError):
            parse_csv("泵号,入册,出册\nA,1,2\n")


class ServiceTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(_tmp_dir, f"svc-{id(self)}.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_replace_and_query_summary(self):
        services.replace_ledger(self.conn, sample_records(), source="test")
        rows = services.get_life_table(self.conn)
        self.assertEqual(len(rows), 5)
        result = services.query_incidence(self.conn, 2000.0)
        self.assertAlmostEqual(result["cum_failure"],
                               1 / 6 + (2 / 3) * (1 / 4))

    def test_single_pump_correction_without_other_details(self):
        services.replace_ledger(self.conn, sample_records())
        before = services.query_incidence(self.conn, 2000.0)["cum_failure"]

        # caller only supplies the one pump being corrected
        services.upsert_pump(self.conn,
                             PumpRecord("P004", 500, 2000, "censored"))
        pumps = {r["pump_id"]: r for r in services.list_pumps(self.conn)}
        self.assertEqual(pumps["P004"]["event_type"], "censored")
        # other pumps untouched
        self.assertEqual(pumps["P001"]["event_type"], "failure")
        after = services.query_incidence(self.conn, 2000.0)["cum_failure"]
        self.assertNotAlmostEqual(before, after)

        # insert a brand new pump the same way
        res = services.upsert_pump(self.conn,
                                   PumpRecord("P999", 0, 4000, "failure"))
        self.assertEqual(res["action"], "inserted")
        self.assertEqual(len(services.list_pumps(self.conn)), 7)

    def test_reclassification_matches_full_rebuild(self):
        services.replace_ledger(self.conn, sample_records())
        changed = {r.pump_id: r for r in sample_records()}
        changed["P004"] = PumpRecord("P004", 500, 2000, "preventive")
        services.upsert_pump(self.conn, changed["P004"])
        self.assertEqual(services.get_meta(self.conn)["last_source"],
                         "single-pump-reclassified")
        incremental = {r["t_hours"]: r for r in services.get_life_table(self.conn)}
        full = {r["t_hours"]: r
                for r in build_life_table(list(changed.values()))}
        self.assertEqual(incremental.keys(), full.keys())
        for t in full:
            for col in ("cum_failure", "cum_preventive", "survival",
                        "failures", "preventive", "hazard_failure",
                        "hazard_preventive"):
                self.assertAlmostEqual(incremental[t][col], full[t][col],
                                       places=12, msg=f"{col}@{t}")
        # point queries agree as well
        for h in (999, 1000, 1500, 2000, 3000, 99999):
            self.assertAlmostEqual(
                services.query_incidence(self.conn, h)["cum_failure"],
                incidence_at(list(full.values()), h)["cum_failure"], places=12)

    def test_same_hour_multiple_and_revert(self):
        records = sample_records()
        services.replace_ledger(self.conn, records)
        # two pumps at the same exit hour flip in the same direction, then
        # one flips back: suffix corrections must accumulate exactly
        services.upsert_pump(self.conn, PumpRecord("P001", 0, 1000, "preventive"))
        services.upsert_pump(self.conn, PumpRecord("P002", 0, 1000, "failure"))
        records[0] = PumpRecord("P001", 0, 1000, "preventive")
        records[1] = PumpRecord("P002", 0, 1000, "failure")
        expected = {r["t_hours"]: r for r in build_life_table(records)}
        for h, row in expected.items():
            self.assertAlmostEqual(
                services.query_incidence(self.conn, h)["cum_failure"],
                row["cum_failure"], places=12, msg=f"t={h}")
            self.assertAlmostEqual(
                services.query_incidence(self.conn, h)["cum_preventive"],
                row["cum_preventive"], places=12, msg=f"t={h}")
        # revert both -> back to original numbers
        services.upsert_pump(self.conn, PumpRecord("P001", 0, 1000, "failure"))
        services.upsert_pump(self.conn, PumpRecord("P002", 0, 1000, "preventive"))
        original = {r["t_hours"]: r for r in build_life_table(sample_records())}
        for h, row in original.items():
            self.assertAlmostEqual(
                services.query_incidence(self.conn, h)["cum_failure"],
                row["cum_failure"], places=12, msg=f"t={h}")

    def test_full_replace_discards_prior_corrections(self):
        services.replace_ledger(self.conn, sample_records())
        services.upsert_pump(self.conn, PumpRecord("P004", 500, 2000, "preventive"))
        # re-upload the original ledger: the correction must not be layered
        # on top of the rebuilt base values
        services.replace_ledger(self.conn, sample_records())
        expected = {r["t_hours"]: r for r in build_life_table(sample_records())}
        for row in services.get_life_table(self.conn):
            self.assertAlmostEqual(row["cum_failure"],
                                   expected[row["t_hours"]]["cum_failure"],
                                   places=12)

    def test_reclassification_rollback_keeps_both_sides(self):
        services.replace_ledger(self.conn, sample_records())
        old_summary = [dict(r) for r in services.get_life_table(self.conn)]

        original = services._apply_reclassification

        def boom(*args, **kwargs):
            raise RuntimeError("simulated incremental failure")

        services._apply_reclassification = boom
        try:
            with self.assertRaises(RuntimeError):
                services.upsert_pump(
                    self.conn, PumpRecord("P001", 0, 1000, "preventive"))
        finally:
            services._apply_reclassification = original

        self.assertEqual(services.get_pump(self.conn, "P001")["event_type"],
                         "failure")
        self.assertEqual([dict(r) for r in services.get_life_table(self.conn)],
                         old_summary)

    def test_reclassification_reads_only_its_ledger_row(self):
        services.replace_ledger(self.conn, sample_records())
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            services.upsert_pump(
                self.conn, PumpRecord("P004", 500, 2000, "preventive"))
        finally:
            self.conn.set_trace_callback(None)
        ledger_reads = [s for s in statements
                        if " FROM ledger" in s and s.lstrip().upper().startswith("SELECT")]
        # exactly one ledger row lookup (this pump), no full-ledger scan
        self.assertEqual(len(ledger_reads), 1)
        self.assertIn("WHERE pump_id =", ledger_reads[0])
        # and no summary-wide delete/rebuild happened
        self.assertFalse(any("DELETE FROM life_table" in s for s in statements))

    def test_queries_use_one_snapshot(self):
        services.replace_ledger(self.conn, sample_records())
        services.upsert_pump(self.conn, PumpRecord("P004", 500, 2000, "preventive"))
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            result = services.query_incidence(self.conn, 2500.0)
        finally:
            self.conn.set_trace_callback(None)
        upper = [s.strip().upper() for s in statements]
        begin = upper.index("BEGIN")
        reads = [s for s in statements[begin:] if s.lstrip().upper().startswith("SELECT")]
        # the summary row, meta and auxiliary rows are all read after BEGIN,
        # before the read-only snapshot is released, without any COMMIT
        self.assertEqual(len(reads), 3)
        self.assertFalse(any(s.startswith("COMMIT") for s in upper[begin:]))
        self.assertEqual(result["meta"]["last_source"],
                         "single-pump-reclassified")

    def test_reclassification_and_query_cost_grow_logarithmically(self):
        """Row counts for one reclassification / one query are O(log M)."""
        import math

        def counts_for(m):
            db = os.path.join(_tmp_dir, f"scale-{m}-{id(self)}.db")
            conn = connect(db)
            init_db(conn)
            try:
                services.replace_ledger(conn, [
                    PumpRecord(f"Q{i:06d}", 0, float(i + 1),
                               "failure" if i % 2 else "preventive")
                    for i in range(m)
                ])
                target = PumpRecord("Q000000", 0, 1.0,
                                    "preventive" if m % 2 == 0 else "failure")
                # Q000000 exits at t=1; flip it relative to stored value
                stored = services.get_pump(conn, "Q000000")
                new_event = ("preventive" if stored["event_type"] == "failure"
                             else "failure")
                ops = {"summary_r": 0, "summary_w": 0, "aux_r": 0, "aux_w": 0}

                def trace(sql):
                    s = sql.strip().upper()
                    summary = "LIFE_TABLE" in s
                    aux = "CIF_TREE" in s or "CIF_DELTA" in s
                    if s.startswith("SELECT"):
                        if summary:
                            ops["summary_r"] += 1
                        if aux:
                            ops["aux_r"] += 1
                    elif s.startswith(("INSERT", "UPDATE", "DELETE")):
                        if summary:
                            ops["summary_w"] += 1
                        if aux:
                            ops["aux_w"] += 1

                conn.set_trace_callback(trace)
                services.upsert_pump(conn, PumpRecord(
                    "Q000000", 0, 1.0, new_event))
                conn.set_trace_callback(None)
                write_ops = dict(ops)

                ops.update(summary_r=0, summary_w=0, aux_r=0, aux_w=0)
                conn.set_trace_callback(trace)
                services.query_incidence(conn, float(m) + 1)
                conn.set_trace_callback(None)
                return write_ops, ops
            finally:
                conn.close()

        small = counts_for(64)
        large = counts_for(4096)
        for label, ops in (("reclass", small[0]), ("query", small[1]),
                           ("reclass-large", large[0]),
                           ("query-large", large[1])):
            self.assertLessEqual(ops["summary_r"], 2, label)
            self.assertLessEqual(ops["summary_w"], 1, label)
            self.assertLessEqual(ops["aux_r"] + ops["aux_w"],
                                 2 * math.ceil(math.log2(4096)) + 4, label)
        # growing M 64x does not multiply the work; it only adds a few levels
        self.assertLessEqual(large[0]["aux_w"] - small[0]["aux_w"], 8)
        self.assertLessEqual(large[1]["aux_r"] - small[1]["aux_r"], 8)

    def test_other_correction_rebuild_does_not_double_count(self):
        records = sample_records()
        services.replace_ledger(self.conn, records)
        # accumulate a reclassification first
        services.upsert_pump(self.conn, PumpRecord("P001", 0, 1000, "preventive"))
        records[0] = PumpRecord("P001", 0, 1000, "preventive")
        # a different kind of correction rebuilds from the full ledger
        services.upsert_pump(self.conn, PumpRecord("P004", 500, 2500, "failure"))
        records[3] = PumpRecord("P004", 500, 2500, "failure")
        expected = {r["t_hours"]: r for r in build_life_table(records)}
        for row in services.get_life_table(self.conn):
            self.assertAlmostEqual(row["cum_failure"],
                                   expected[row["t_hours"]]["cum_failure"],
                                   places=12)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cif_delta").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cif_tree").fetchone()[0], 0)

    def test_reclassify_service_errors_and_noop(self):
        services.replace_ledger(self.conn, sample_records())
        with self.assertRaises(ValueError):
            services.reclassify_pump(self.conn, "P001", "censored")
        with self.assertRaises(ValueError):
            services.reclassify_pump(self.conn, "P003", "failure")
        with self.assertRaises(ValueError):
            services.reclassify_pump(self.conn, "P001", "failure")
        with self.assertRaises(LookupError):
            services.reclassify_pump(self.conn, "missing", "failure")
        # nothing above wrote anything
        self.assertEqual(services.get_pump(self.conn, "P001")["event_type"],
                         "failure")
        self.assertEqual(services.get_pump(self.conn, "P003")["event_type"],
                         "censored")

    def test_query_before_first_time_point_and_empty(self):
        services.replace_ledger(self.conn, sample_records())
        at_zero = services.query_incidence(self.conn, 0)
        self.assertAlmostEqual(at_zero["cum_failure"], 0.0)
        self.assertEqual(at_zero["basis_row"]["t_hours"], 0.0)
        with self.assertRaises(ValueError):
            services.query_incidence(self.conn, -1)

        # a ledger whose first time point is after 0: an earlier query is zero
        # with no basis row, and an empty ledger behaves the same way
        other = os.path.join(_tmp_dir, f"later-{id(self)}.db")
        conn = connect(other)
        init_db(conn)
        try:
            services.replace_ledger(conn, [PumpRecord("L1", 100, 200, "failure")])
            before = services.query_incidence(conn, 50)
            self.assertIsNone(before["basis_row"])
            self.assertAlmostEqual(before["cum_failure"], 0.0)
            services.replace_ledger(conn, [])
            self.assertEqual(services.get_life_table(conn), [])
            self.assertAlmostEqual(
                services.query_incidence(conn, 500)["cum_failure"], 0.0)
        finally:
            conn.close()

    def test_dashboard_materialization_matches_lifetable(self):
        services.replace_ledger(self.conn, sample_records())
        services.reclassify_pump(self.conn, "P004", "preventive")
        meta, table = services.get_dashboard(self.conn)
        self.assertEqual(meta["last_source"], "single-pump-reclassified")
        self.assertEqual(table, services.get_life_table(self.conn))
        self.assertNotIn("seq", table[0])

    def test_old_database_upgrades_in_place(self):
        import sqlite3

        old_path = os.path.join(_tmp_dir, f"old-{id(self)}.db")
        raw = sqlite3.connect(old_path)
        raw.executescript("""
        CREATE TABLE ledger (
            pump_id TEXT PRIMARY KEY, entry_hours REAL NOT NULL,
            exit_hours REAL NOT NULL, event_type TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        CREATE TABLE life_table (
            t_hours REAL PRIMARY KEY, entered INTEGER NOT NULL,
            at_risk INTEGER NOT NULL, failures INTEGER NOT NULL,
            preventive INTEGER NOT NULL, censored INTEGER NOT NULL,
            hazard_failure REAL NOT NULL, hazard_preventive REAL NOT NULL,
            survival REAL NOT NULL, cum_failure REAL NOT NULL,
            cum_preventive REAL NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        base = build_life_table(sample_records())
        raw.executemany(
            "INSERT INTO ledger VALUES (?,?,?,?,?)",
            [(r.pump_id, r.entry_hours, r.exit_hours, r.event_type, "old")
             for r in sample_records()])
        raw.executemany(
            "INSERT INTO life_table VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [tuple(row[c] for c in (
                "t_hours", "entered", "at_risk", "failures", "preventive",
                "censored", "hazard_failure", "hazard_preventive", "survival",
                "cum_failure", "cum_preventive")) for row in base])
        raw.execute("INSERT INTO meta VALUES ('pump_count','6')")
        raw.commit()
        raw.close()

        conn = connect(old_path)
        try:
            # no CSV re-upload: queries and corrections work immediately
            self.assertAlmostEqual(
                services.query_incidence(conn, 2000.0)["cum_failure"],
                base[3]["cum_failure"], places=12)
            services.upsert_pump(conn, PumpRecord("P004", 500, 2000,
                                                  "preventive"))
            records = sample_records()
            records[3] = PumpRecord("P004", 500, 2000, "preventive")
            expected = {r["t_hours"]: r for r in build_life_table(records)}
            for h in (1000, 2000, 3000):
                self.assertAlmostEqual(
                    services.query_incidence(conn, h)["cum_failure"],
                    expected[h]["cum_failure"], places=12)
            self.assertEqual(services.get_meta(conn)["pump_count"], "6")
        finally:
            conn.close()

    def test_failure_rolls_back_keeps_old_values(self):
        services.replace_ledger(self.conn, sample_records())
        old_rows = [dict(r) for r in services.get_life_table(self.conn)]
        old_pumps = len(services.list_pumps(self.conn))

        def boom(_records):
            raise RuntimeError("simulated rebuild failure")

        original = services.build_life_table
        services.build_life_table = boom
        try:
            with self.assertRaises(RuntimeError):
                services.replace_ledger(
                    self.conn, [PumpRecord("Z", 0, 1, "failure")])
            with self.assertRaises(RuntimeError):
                services.upsert_pump(
                    self.conn, PumpRecord("P001", 0, 999, "censored"))
        finally:
            services.build_life_table = original

        self.assertEqual(len(services.list_pumps(self.conn)), old_pumps)
        new_rows = [dict(r) for r in services.get_life_table(self.conn)]
        self.assertEqual(new_rows, old_rows)
        p001 = services.get_pump(self.conn, "P001")
        self.assertEqual(p001["exit_hours"], 1000.0)
        self.assertEqual(p001["event_type"], "failure")


class FlaskWebTests(unittest.TestCase):
    def setUp(self):
        self.web_db = os.path.join(_tmp_dir, f"web-{id(self)}-{os.getpid()}.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.web_db + suffix):
                os.remove(self.web_db + suffix)
        self.app = create_app(self.web_db)
        self.client = self.app.test_client()

    def test_health_and_empty_pages(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("暂无数据", page)
        self.assertEqual(self.client.get("/query").status_code, 200)

    def test_upload_lifecycle_and_query_api(self):
        import io
        resp = self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode("utf-8")),
                               "pumps.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("上传成功", resp.get_data(as_text=True))

        api = self.client.get("/api/query?hours=2000").get_json()
        self.assertAlmostEqual(api["cum_failure"],
                               1 / 6 + (2 / 3) * (1 / 4), places=12)
        self.assertEqual(api["basis_row"]["at_risk"], 4)

        page = self.client.get("/query?hours=1500").get_data(as_text=True)
        self.assertIn("逐时点计数", page)

        bad = self.client.get("/api/query?hours=abc")
        self.assertEqual(bad.status_code, 400)

    def test_bad_upload_keeps_previous_data(self):
        import io
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")
        good = self.client.get("/api/lifetable").get_json()
        self.assertEqual(len(good), 5)

        bad_csv = "泵号,入册累计运行小时,出册累计运行小时,结束方式\nX,0,1,爆炸\n"
        resp = self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(bad_csv.encode()), "bad.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("CSV 解析失败", resp.get_data(as_text=True))
        self.assertEqual(len(self.client.get("/api/lifetable").get_json()), 5)

    def test_query_does_not_modify_summary(self):
        import io
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")
        before = self.client.get("/api/lifetable").get_json()
        for h in (0, 999, 1000, 2500, 100000):
            self.client.get(f"/query?hours={h}")
            self.client.get(f"/api/query?hours={h}")
        after = self.client.get("/api/lifetable").get_json()
        self.assertEqual(before, after)

    def test_single_pump_edit_via_web(self):
        import io
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")
        resp = self.client.post("/pumps/P002/edit", data={
            "pump_id": "P002",
            "entry_hours": "0",
            "exit_hours": "1234",
            "event_type": "failure",
        }, follow_redirects=True)
        self.assertIn("更正成功", resp.get_data(as_text=True))
        with self.app.app_context():
            row = services.get_pump(connect(self.web_db), "P002")
        self.assertEqual(row["exit_hours"], 1234.0)
        self.assertEqual(row["event_type"], "failure")

        # invalid form is rejected and old value preserved
        resp = self.client.post("/pumps/P002/edit", data={
            "pump_id": "P002", "entry_hours": "9000",
            "exit_hours": "10", "event_type": "failure",
        })
        self.assertEqual(resp.status_code, 400)
        row = services.get_pump(connect(self.web_db), "P002")
        self.assertEqual(row["exit_hours"], 1234.0)

    def test_reclassify_button_updates_all_views(self):
        import io

        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")

        before = self.client.get("/api/query?hours=2000").get_json()["cum_failure"]
        resp = self.client.post("/pumps/P004/reclassify",
                                data={"event_type": "preventive"},
                                follow_redirects=True)
        self.assertIn("已改判为「预防更换」", resp.get_data(as_text=True))

        records = sample_records()
        records[3] = PumpRecord("P004", 500, 2000, "preventive")
        expected = {r["t_hours"]: r for r in build_life_table(records)}

        # dashboard table reflects the correction
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("%.4f" % expected[2000.0]["cum_failure"], page)
        # query page and API agree with the corrected full recomputation
        api = self.client.get("/api/query?hours=2500").get_json()
        self.assertAlmostEqual(api["cum_failure"],
                               expected[2000.0]["cum_failure"], places=12)
        self.assertNotAlmostEqual(api["cum_failure"], before)
        qpage = self.client.get("/query?hours=2500").get_data(as_text=True)
        self.assertIn("%.4f" % expected[2000.0]["cum_failure"], qpage)
        # lifetable API had the correction applied too
        lt = {r["t_hours"]: r for r in self.client.get("/api/lifetable").get_json()}
        self.assertAlmostEqual(lt[2000.0]["cum_failure"],
                               expected[2000.0]["cum_failure"], places=12)
        self.assertEqual(lt[2000.0]["failures"], 0)
        self.assertEqual(lt[2000.0]["preventive"], 1)

    def test_reclassify_rejects_censored_and_unknown(self):
        import io

        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")
        for pump, target in (("P003", "failure"), ("P001", "censored"),
                             ("P001", "failure")):
            resp = self.client.post(f"/pumps/{pump}/reclassify",
                                    data={"event_type": target},
                                    follow_redirects=True)
            self.assertIn("只能在本泵的 故障 / 预防更换 之间改判",
                          resp.get_data(as_text=True))
        self.assertEqual(
            self.client.post("/pumps/NOPE/reclassify",
                             data={"event_type": "failure"}).status_code, 404)
        pump = services.get_pump(connect(self.web_db), "P001")
        self.assertEqual(pump["event_type"], "failure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
