import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import db
import algorithms


class SmartParkFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Isolate from the developer's real smartpark.db: point DB_PATH at a
        # temp file BEFORE importing app (app.py runs init_db() at import).
        cls.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.temp_dir.name) / "features.db"
        db.init_db()
        import app
        cls.app = app.app
        cls.algorithms = algorithms
        cls.algorithms.load_heaps_from_db()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def login_manager(self):
        client = self.app.test_client()
        response = client.post("/api/auth/login", json={"username": "manager", "password": "manager123"})
        self.assertEqual(response.status_code, 200)
        return client

    # ---- Billing boundaries -------------------------------------------------
    def test_calculate_fee_tier_boundaries(self):
        entry = datetime.now(timezone.utc).replace(microsecond=0)
        cases = [
            (1, 0), (30, 0), (31, 50), (120, 50), (121, 100),
            (240, 100), (241, 300), (360, 300), (361, 500), (1500, 500),
        ]
        for minutes, expected in cases:
            duration, subtotal = self.algorithms.calculate_fee(
                entry, entry + timedelta(minutes=minutes)
            )
            self.assertEqual(duration, minutes)
            self.assertEqual(subtotal, expected, f"{minutes} min should map to {expected}")

    def test_calculate_totals_vat(self):
        subtotal, vat_rate, vat_amount, total = self.algorithms.calculate_totals(50)
        self.assertEqual((subtotal, vat_rate, vat_amount, total), (50, 16.0, 8, 58))

    # ---- Nearest-slot allocation (Dijkstra-ordered heap) ---------------------
    def test_slot_distance_grid(self):
        self.assertEqual(self.algorithms.slot_distance(1), 0)     # entrance bay
        self.assertEqual(self.algorithms.slot_distance(2), 1)     # adjacent
        self.assertEqual(self.algorithms.slot_distance(3), 2)
        self.assertEqual(self.algorithms.slot_distance(40), 11)   # far corner (4,7)

    def test_allocation_prefers_nearest_slot(self):
        session, error = self.algorithms.register_entry("NEAREST1", "car", "0700000001")
        self.assertIsNone(error)
        self.assertEqual(session["slot_number"], 1)  # distance-0 bay taken first
        session2, error2 = self.algorithms.register_entry("NEAREST2", "car", "0700000002")
        self.assertIsNone(error2)
        self.assertIn(session2["slot_number"], (2, 9))  # distance-1 bays
        self._cleanup_plates("NEAREST1", "NEAREST2")

    # ---- FIFO barrier queue ---------------------------------------------------
    def test_barrier_queue_serves_fifo(self):
        queue = self.algorithms.BarrierQueue()
        sessions = [{"id": i, "status": "completed", "slot_number": i} for i in range(1, 6)]
        results = [queue.request(s) for s in sessions]
        self.assertTrue(all(r["opened"] for r in results))
        order = [r["session_id"] for r in queue.service_order()[-5:]]
        self.assertEqual(order, [1, 2, 3, 4, 5])  # strict FIFO

    def test_barrier_queue_handles_unpaid(self):
        queue = self.algorithms.BarrierQueue()
        result = queue.request({"id": 999999, "status": "awaiting_payment", "slot_number": 3})
        self.assertFalse(result["opened"])
        self.assertIn("Payment", result["reason"])

    # ---- Trie plate prefix search ---------------------------------------------
    def test_plate_trie_prefix_search(self):
        trie = self.algorithms.PlateTrie()
        for plate in ("KDA123B", "KDB456C", "KDA999Z", "XYZ1"):
            trie.insert(plate)
        self.assertEqual(trie.starts_with("KDA"), ["KDA123B", "KDA999Z"])
        self.assertEqual(trie.starts_with("K"), ["KDA123B", "KDA999Z", "KDB456C"])
        self.assertEqual(trie.starts_with("X"), ["XYZ1"])
        self.assertEqual(trie.starts_with("ZZZ"), [])
        self.assertEqual(trie.starts_with("KDA1"), ["KDA123B"])

    def test_plate_search_endpoint_requires_manager(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/api/plates?prefix=K").status_code, 401)
        # Create our own vehicle so this test never depends on another module.
        session, error = self.algorithms.register_entry("PLTSEARCH1", "car", "0700000061")
        self.assertIsNone(error)
        manager = self.login_manager()
        response = manager.get("/api/plates?prefix=PLTSEAR")
        self.assertEqual(response.status_code, 200)
        matches = [m["plate_number"] for m in response.json["matches"]]
        self.assertIn("PLTSEARCH1", matches)
        parked = next(m for m in response.json["matches"] if m["plate_number"] == "PLTSEARCH1")
        self.assertEqual(parked["slot_number"], session["slot_number"])
        self._cleanup_plates("PLTSEARCH1")

    def _cleanup_plates(self, *plates):
        """Remove test vehicles and everything that references them.

        payments -> parking_sessions -> vehicles must be deleted in that order
        (foreign keys), and the bays those sessions held are released again.
        The connection is always closed so a failure can never leave a write
        lock behind for the next test.
        """
        conn = db.get_connection()
        try:
            marks = tuple(plates)
            placeholders = ",".join("?" * len(marks))
            sessions = conn.execute(
                "SELECT id, slot_id FROM parking_sessions WHERE vehicle_id IN "
                f"(SELECT id FROM vehicles WHERE plate_number IN ({placeholders}))",
                marks,
            ).fetchall()
            if sessions:
                session_ids = tuple(row["id"] for row in sessions)
                session_marks = ",".join("?" * len(session_ids))
                conn.execute(f"DELETE FROM payments WHERE session_id IN ({session_marks})", session_ids)
                conn.execute(f"DELETE FROM parking_sessions WHERE id IN ({session_marks})", session_ids)
                for row in sessions:
                    conn.execute(
                        "UPDATE parking_slots SET status = 'available' "
                        "WHERE id = ? AND status = 'occupied'",
                        (row["slot_id"],),
                    )
            conn.execute(f"DELETE FROM vehicles WHERE plate_number IN ({placeholders})", marks)
            conn.commit()
        finally:
            conn.close()
        self.algorithms.load_heaps_from_db()

    # ---- Entry ticket QR -------------------------------------------------------
    def test_entry_ticket_roundtrip(self):
        client = self.app.test_client()
        entry = client.post("/api/entry", json={
            "plate_number": "TICKETQR1", "vehicle_type": "car", "owner_phone": "0700000011",
        })
        self.assertEqual(entry.status_code, 200)
        code = entry.json["ticket_code"]
        self.assertTrue(code.startswith("SP-TK-"))
        self.assertTrue(entry.json["ticket_qr"].startswith("data:image/png;base64,"))
        resolved = client.get(f"/api/ticket/{code}")
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json["plate_number"], "TICKETQR1")
        tampered = client.get("/api/ticket/" + code[:-2] + "zz")
        self.assertEqual(tampered.status_code, 400)
        self._cleanup_plates("TICKETQR1")

    # ---- Receipt / ticket scan pages -------------------------------------------
    def _park_and_pay(self, plate, phone):
        session, error = self.algorithms.register_entry(plate, "car", phone)
        self.assertIsNone(error)
        conn = db.get_connection()
        conn.execute(
            "UPDATE parking_sessions SET entry_time = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat(), session["id"]),
        )
        conn.commit()
        conn.close()
        exited, err = self.algorithms.process_exit(plate)
        self.assertIsNone(err)
        settled, err2 = self.algorithms.settle_payment(exited["id"], "cash", f"CASH-{plate}")
        self.assertIsNone(err2)
        return settled

    def test_receipt_hides_signature_hash_and_needs_signed_link(self):
        client = self.app.test_client()
        settled = self._park_and_pay("RCPTHASH1", "0700000061")
        response = client.get(f"/api/receipt/{settled['id']}")
        self.assertEqual(response.status_code, 200)
        receipt = response.json["receipt"]
        # No signature/hash is ever returned...
        self.assertNotIn("electronic_signature", receipt)
        self.assertIs(receipt["verified"], True)
        digest = re.compile(r"\b[0-9a-fA-F]{64}\b")
        for key, value in receipt.items():
            if isinstance(value, str) and key != "qr_code":
                self.assertIsNone(digest.search(value), f"{key} leaked a hash value")
        # ...the printed phone number is minimised...
        self.assertTrue(receipt["phone_number"].startswith("***"))
        # ...and the QR points at the public receipt page with a signed token.
        link = urlsplit(receipt["verification_url"])
        self.assertEqual(link.path, f"/receipt/{settled['id']}")
        self.assertTrue(link.query.startswith("t="))
        page = client.get(f"{link.path}?{link.query}")
        self.assertEqual(page.status_code, 200)
        body = page.data.decode("utf-8")
        self.assertIn("RCPTHASH1", body)
        self.assertIsNone(digest.search(body), "receipt page leaked a hash value")
        # Receipt ids cannot be enumerated without the token.
        self.assertEqual(client.get(f"/receipt/{settled['id']}").status_code, 403)
        self.assertEqual(client.get(f"/receipt/{settled['id']}?t=not-a-real-token").status_code, 403)
        self.assertEqual(client.get("/receipt/999999?t=" + "0" * 32).status_code, 403)
        self._cleanup_plates("RCPTHASH1")

    def test_ticket_qr_opens_vehicle_details_page(self):
        client = self.app.test_client()
        entry = client.post("/api/entry", json={
            "plate_number": "TICKETPG1", "vehicle_type": "car", "owner_phone": "0700000071",
        })
        self.assertEqual(entry.status_code, 200)
        ticket_url = entry.json["ticket_url"]
        self.assertIn("/ticket/", ticket_url)
        self.assertIn("/ticket/", urlsplit(ticket_url).path)
        page = client.get(urlsplit(ticket_url).path)
        self.assertEqual(page.status_code, 200)
        body = page.data.decode("utf-8")
        self.assertIn("TICKETPG1", body)                                   # plate
        self.assertIn("Vehicle pass", body)                                # vehicle details page
        self.assertIn(str(entry.json["session"]["slot_number"]), body)     # allocated slot
        self.assertIn("Check-in", body)                                    # entry time
        # A tampered ticket shows the "not available" page, never vehicle details.
        tampered = client.get("/ticket/" + entry.json["ticket_code"][:-2] + "zz")
        self.assertEqual(tampered.status_code, 404)
        self.assertNotIn("TICKETPG1", tampered.data.decode("utf-8"))
        self._cleanup_plates("TICKETPG1")

    # ---- Attendant overrides ----------------------------------------------------
    def test_slot_maintenance_toggle_and_allocation_exclusion(self):
        client = self.login_manager()
        # Pick a bay that is genuinely free right now (other tests park cars).
        slots = client.get("/api/slots").json["slots"]
        free = next(s for s in slots if s["status"] == "available" and s["vehicle_type"] == "car")
        target = free["slot_number"]
        out = client.post(f"/api/slots/{target}/maintenance", json={"out_of_service": True, "reason": "test jam"})
        self.assertEqual(out.status_code, 200)
        session, error = self.algorithms.register_entry("MAINTX1", "car", "0700000021")
        self.assertIsNone(error)
        self.assertNotEqual(session["slot_number"], target)
        self._cleanup_plates("MAINTX1")
        back = client.post(f"/api/slots/{target}/maintenance", json={"out_of_service": False, "reason": "test done"})
        self.assertEqual(back.status_code, 200)
        conn = db.get_connection()
        status = conn.execute("SELECT status FROM parking_slots WHERE slot_number = ?", (target,)).fetchone()["status"]
        events = conn.execute("SELECT COUNT(*) AS n FROM override_events WHERE action = 'slot_maintenance'").fetchone()["n"]
        conn.close()
        self.assertEqual(status, "available")
        self.assertGreaterEqual(events, 2)

    def test_barrier_override_rejects_unpaid_and_audits_paid(self):
        client = self.login_manager()
        session, _ = self.algorithms.register_entry("OVRTEST1", "car", "0700000031")
        refused = client.post("/api/barrier/override", json={"session_id": session["id"], "reason": "vip"})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("completed", refused.json["error"])
        conn = db.get_connection()
        conn.execute(
            "UPDATE parking_sessions SET entry_time = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(), session["id"]),
        )
        conn.commit()
        conn.close()
        exited, err = self.algorithms.process_exit("OVRTEST1")
        self.assertIsNone(err)
        settled, err2 = self.algorithms.settle_payment(exited["id"], "cash", "CASH-OVR")
        self.assertIsNone(err2)
        override = client.post("/api/barrier/override", json={"session_id": settled["id"], "reason": "sensor missed car"})
        self.assertEqual(override.status_code, 200)
        self.assertTrue(override.json["barrier"]["opened"])
        conn = db.get_connection()
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM override_events WHERE action = 'barrier_override' AND target = ?",
            (f"session:{settled['id']}",),
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(events, 1)

    # ---- Overstay alerts ---------------------------------------------------------
    def test_overstay_flag_and_count(self):
        session, _ = self.algorithms.register_entry("OVERSTAY1", "car", "0700000041")
        conn = db.get_connection()
        conn.execute(
            "UPDATE parking_sessions SET entry_time = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(), session["id"]),
        )
        conn.commit()
        conn.close()
        activity = self.algorithms.list_recent_activity(limit=50)
        flagged = [a for a in activity if a["vehicle"] == "OVERSTAY1"]
        self.assertTrue(flagged and flagged[0]["overstay"])
        self.assertGreaterEqual(self.algorithms.count_overstays(), 1)
        self._cleanup_plates("OVERSTAY1")

    # ---- Analytics ----------------------------------------------------------------
    def test_analytics_summary_shape_and_revenue(self):
        session, _ = self.algorithms.register_entry("ANALYTIC1", "car", "0700000051")
        conn = db.get_connection()
        conn.execute(
            "UPDATE parking_sessions SET entry_time = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(), session["id"]),
        )
        conn.commit()
        conn.close()
        exited, err = self.algorithms.process_exit("ANALYTIC1")
        self.assertIsNone(err)
        settled, err2 = self.algorithms.settle_payment(exited["id"], "cash", "CASH-AN1")
        self.assertIsNone(err2)

        summary = self.algorithms.analytics_summary(days=14)
        self.assertGreaterEqual(summary["total_revenue"], settled["fee_charged"])
        self.assertTrue(summary["revenue_by_day"])
        self.assertIn("busiest_zone", summary)
        zones = {z["zone"] for z in summary["occupancy_by_zone"]}
        self.assertEqual(zones, {"A", "B", "C"})
        self.assertTrue(any(m["method"] == "cash" for m in summary["revenue_by_method"]))

        client = self.login_manager()
        response = client.get("/api/analytics")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])


if __name__ == "__main__":
    unittest.main()
