import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db


class ParkFlowRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.temp_dir.name) / "test.db"
        db.init_db()

        import app
        cls.app = app.app
        cls.algorithms = app.algorithms
        # Rebuild the slot heap/trie cache against THIS temp database so
        # tests never inherit allocation state from another test module.
        cls.algorithms.load_heaps_from_db()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_kenya_vat_default(self):
        self.assertEqual(self.algorithms.get_vat_rate(), 16.0)
        self.assertEqual(self.algorithms.calculate_totals(50), (50, 16.0, 8, 58))

    def test_paid_exit_stores_tax_breakdown(self):
        session, error = self.algorithms.register_entry("VATTEST", "car", "0700000000")
        self.assertIsNone(error)
        conn = db.get_connection()
        conn.execute(
            "UPDATE parking_sessions SET entry_time = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(), session["id"]),
        )
        conn.commit()
        conn.close()

        exited, error = self.algorithms.process_exit("VATTEST")
        self.assertIsNone(error)
        self.assertEqual(exited["subtotal_amount"], 50)
        self.assertEqual(exited["vat_amount"], 8)
        self.assertEqual(exited["total_amount"], 58)
        self.assertEqual(exited["status"], "awaiting_payment")

    def test_report_export_is_open(self):
        # No sign-in any more: the attendant kiosk exports straight away.
        response = self.app.test_client().get("/api/reports/export?format=xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data[:2] == b"PK")

    def test_rates_are_editable_without_sign_in(self):
        client = self.app.test_client()
        response = client.put("/api/rates", json={"rates": [
            {"max_minutes": 30, "fee_amount": 0},
            {"max_minutes": 120, "fee_amount": 55},
            {"max_minutes": 2147483647, "fee_amount": 500},
        ], "vat_rate": 16})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(next(r["fee_amount"] for r in response.json["rates"] if r["max_minutes"] == 120), 55)
        # Put the schedule back so later billing tests see the real tiers.
        client.put("/api/rates", json={"rates": [
            {"max_minutes": 30, "fee_amount": 0},
            {"max_minutes": 120, "fee_amount": 50},
            {"max_minutes": 240, "fee_amount": 100},
            {"max_minutes": 360, "fee_amount": 300},
            {"max_minutes": 2147483647, "fee_amount": 500},
        ], "vat_rate": 16})

    def test_rate_schedule_is_persisted(self):
        conn = db.get_connection()
        conn.execute("UPDATE parking_rates SET fee_amount = 60 WHERE max_minutes = 120")
        conn.commit()
        conn.close()
        rates = self.algorithms.get_parking_rates()
        self.assertEqual(next(rate["fee_amount"] for rate in rates if rate["max_minutes"] == 120), 60)

    def test_health_endpoints(self):
        client = self.app.test_client()
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json["status"], "ok")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json["checks"]["database"], "ok")

    def test_authentication_is_fully_removed(self):
        # Sign-in, profile viewing and sign-out are gone: every endpoint 404s
        # and the app serves everything to an anonymous client.
        client = self.app.test_client()
        for method, path in (
            ("post", "/api/auth/login"), ("post", "/api/auth/logout"), ("get", "/api/auth/me"),
            ("get", "/api/profile"), ("put", "/api/profile"),
            ("delete", "/api/profile/sessions/1"),
        ):
            self.assertEqual(getattr(client, method)(path, json={}).status_code, 404, path)
        self.assertEqual(client.get("/").status_code, 200)
        # The legacy user tables are retained (not dropped) for audit history.
        conn = db.get_connection()
        try:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        finally:
            conn.close()
        self.assertTrue({"users", "active_sessions"} <= tables, "auth tables must not be dropped")


if __name__ == "__main__":
    unittest.main()
