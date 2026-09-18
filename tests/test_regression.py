import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db


class SmartParkRegressionTests(unittest.TestCase):
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

    def test_rate_export_requires_sign_in(self):
        client = self.app.test_client()
        response = client.get("/api/reports/export?format=xlsx")
        self.assertEqual(response.status_code, 401)

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

    def test_manager_login_and_profile(self):
        client = self.app.test_client()
        login = client.post("/api/auth/login", json={"username": "manager", "password": "manager123"})
        self.assertEqual(login.status_code, 200)
        profile = client.get("/api/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertTrue(profile.json["sessions"])

    def test_revoke_other_session_and_relogin(self):
        # Login creates session 1.
        client = self.app.test_client()
        login = client.post("/api/auth/login", json={"username": "manager", "password": "manager123"})
        self.assertEqual(login.status_code, 200)
        profile = client.get("/api/profile")
        sessions = profile.json["sessions"]
        self.assertTrue(sessions)
        # Exactly one session should be flagged as current, and it must not
        # be revocable.
        current = [s for s in sessions if s.get("current")]
        self.assertEqual(len(current), 1)
        deny = client.delete(f"/api/profile/sessions/{current[0]['id']}")
        self.assertEqual(deny.status_code, 400)
        self.assertFalse(deny.json["ok"])
        # A second login creates a revocable session; revoking it works.
        other = self.app.test_client()
        self.assertEqual(other.post("/api/auth/login", json={"username": "manager", "password": "manager123"}).status_code, 200)
        other_profile = other.get("/api/profile")
        other_current = [s for s in other_profile.json["sessions"] if s.get("current")]
        self.assertEqual(len(other_current), 1)
        ok = client.delete(f"/api/profile/sessions/{other_current[0]['id']}")
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json["ok"])
        # The revoked session can no longer authenticate.
        self.assertEqual(other.get("/api/profile").status_code, 401)

    def test_mfa_endpoints_removed(self):
        # MFA was removed: endpoints must answer 410 Gone, and login must not
        # require an authenticator code even for previously enrolled accounts.
        client = self.app.test_client()
        self.assertEqual(client.post("/api/auth/login", json={"username": "manager", "password": "manager123"}).status_code, 200)
        for path in ("/api/profile/mfa/setup", "/api/profile/mfa/enable", "/api/profile/mfa/disable"):
            response = client.post(path, json={})
            self.assertEqual(response.status_code, 410)


if __name__ == "__main__":
    unittest.main()
