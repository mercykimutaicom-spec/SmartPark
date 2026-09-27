import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import db
import algorithms


class ParkFlowFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Isolate from the developer's real SQLite database: point DB_PATH at a
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

    # Billing boundaries
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

    # Nearest-slot allocation (Dijkstra-ordered heap)
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

    # FIFO barrier queue
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

    # Trie plate prefix search
    def test_plate_trie_prefix_search(self):
        trie = self.algorithms.PlateTrie()
        for plate in ("KDA123B", "KDB456C", "KDA999Z", "XYZ1"):
            trie.insert(plate)
        self.assertEqual(trie.starts_with("KDA"), ["KDA123B", "KDA999Z"])
        self.assertEqual(trie.starts_with("K"), ["KDA123B", "KDA999Z", "KDB456C"])
        self.assertEqual(trie.starts_with("X"), ["XYZ1"])
        self.assertEqual(trie.starts_with("ZZZ"), [])
        self.assertEqual(trie.starts_with("KDA1"), ["KDA123B"])

    def test_plate_search_is_open(self):
        client = self.app.test_client()  # never signed in: there is no sign-in
        # Own vehicle: never depend on another test's leftover data.
        session, error = self.algorithms.register_entry("PLTSEARCH1", "car", "0700000061")
        self.assertIsNone(error)
        response = client.get("/api/plates?prefix=PLTSEAR")
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

    # Entry ticket QR
    def test_entry_ticket_roundtrip(self):
        client = self.app.test_client()
        entry = client.post("/api/entry", json={
            "plate_number": "TICKETQR1", "vehicle_type": "car", "owner_phone": "0700000011",
        })
        self.assertEqual(entry.status_code, 200)
        code = entry.json["ticket_code"]
        self.assertTrue(code.startswith("PF-TK-"))
        self.assertTrue(entry.json["ticket_qr"].startswith("data:image/png;base64,"))
        resolved = client.get(f"/api/ticket/{code}")
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json["plate_number"], "TICKETQR1")
        tampered = client.get("/api/ticket/" + code[:-2] + "zz")
        self.assertEqual(tampered.status_code, 400)
        self._cleanup_plates("TICKETQR1")

    # Receipt / ticket scan pages
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
        # No signature or hash is ever returned.
        self.assertNotIn("electronic_signature", receipt)
        self.assertIs(receipt["verified"], True)
        digest = re.compile(r"\b[0-9a-fA-F]{64}\b")
        for key, value in receipt.items():
            if isinstance(value, str) and key != "qr_code":
                self.assertIsNone(digest.search(value), f"{key} leaked a hash value")
        # The printed phone number is masked.
        self.assertTrue(receipt["phone_number"].startswith("***"))
        # The QR points at the receipt page with a signed token.
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
        # A tampered ticket never shows vehicle details.
        tampered = client.get("/ticket/" + entry.json["ticket_code"][:-2] + "zz")
        self.assertEqual(tampered.status_code, 404)
        self.assertNotIn("TICKETPG1", tampered.data.decode("utf-8"))
        # The public page carries the brand mark + favicon like the main app.
        self.assertIn("/static/img/mark.svg", body)
        self.assertIn("/static/img/favicon.svg", body)
        self._cleanup_plates("TICKETPG1")

    # Brand assets (SVG)
    def test_brand_svg_assets_are_served_and_well_formed(self):
        client = self.app.test_client()
        for name in ("mark.svg", "favicon.svg"):
            response = client.get(f"/static/img/{name}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/svg+xml")
            root = ElementTree.fromstring(response.data)  # raises if malformed
            self.assertEqual(root.get("viewBox"), "0 0 64 64")

    def test_index_links_favicon_and_brand_mark(self):
        html = self.app.test_client().get("/").data.decode("utf-8")
        self.assertIn("/static/img/favicon.svg", html)
        self.assertIn("/static/img/mark.svg", html)

    def test_the_one_page_has_no_duplicate_element_ids(self):
        # Six panels now share a single document. A duplicate id is invalid HTML
        # and silently breaks byId(): it returns the first match, so the second
        # element is never wired up. That is how the exit ticket input and the
        # ticket modal's <code> both ended up called #ticket-code, and the code
        # was then written as textContent to an <input> — never shown.
        html = self.app.test_client().get("/").data.decode("utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dupes, [], f"duplicate element ids on the one page: {dupes}")
        # The two ticket-code elements must stay distinct and both stay present.
        self.assertEqual(ids.count("ticket-code"), 1)      # the modal's <code>
        self.assertEqual(ids.count("exit-ticket-code"), 1)  # the exit-form input

    def test_every_panel_lives_on_the_one_overview_page(self):
        # The kiosk is a single page: the top bar scrolls to a section instead
        # of loading another url, so a driver never loses the live slot map or
        # the entry form by clicking around.
        html = self.app.test_client().get("/").data.decode("utf-8")
        sections = [
            ("overview", "stat-available", "Overview"),
            ("operations", "entry-form", "Entry / Exit"),
            ("slots", "slot-grid", "Slots"),
            ("rates", "rates-list", "Rates"),
            ("activity", "activity-list", "Activity"),
            ("tools", "plate-search", "Tools"),
        ]
        for anchor, marker, label in sections:
            self.assertIn(f'id="{anchor}"', html, f"missing the #{anchor} section")
            self.assertIn(f'id="{marker}"', html, f"missing the {marker} panel")
            self.assertIn(f">{label}</a>", html, f"missing the {label} nav link")
        for chrome in ('id="toast-stack"', 'id="ticket-modal"'):
            self.assertIn(chrome, html)

    def test_top_bar_links_are_anchors_to_real_sections(self):
        html = self.app.test_client().get("/").data.decode("utf-8")
        links = re.findall(r'<a class="topnav__link[^"]*" href="#([^"]+)"', html)
        self.assertEqual(
            links, ["overview", "operations", "slots", "rates", "activity", "tools"],
            "the top bar must scroll to sections on the one page",
        )
        for anchor in links:
            self.assertIn(f'id="{anchor}"', html, f"#{anchor} has no target on the page")
        # No nav link may point at another url any more.
        for stale in ('href="/entry-exit"', 'href="/rates"', 'href="/slots"',
                      'href="/activity"', 'href="/tools"'):
            self.assertNotIn(stale, html, f"{stale} would reload the page instead of scrolling")

    def test_legacy_subpage_urls_redirect_to_their_section(self):
        # The old per-panel urls are still in bookmarks and kiosk shortcuts.
        client = self.app.test_client()
        for path, anchor in (
            ("/entry-exit", "#operations"),
            ("/slots", "#slots"),
            ("/rates", "#rates"),
            ("/activity", "#activity"),
            ("/tools", "#tools"),
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 302, f"{path} must redirect, not 404")
            self.assertTrue(
                response.headers["Location"].endswith(anchor),
                f"{path} must land on {anchor}, got {response.headers['Location']}",
            )

    # Full lot: the entry door is shut and the driver is told why.
    def _fill_the_lot(self):
        """Occupy every bay, then resync the in-memory allocation cache."""
        conn = db.get_connection()
        try:
            conn.execute("UPDATE parking_slots SET status = 'occupied'")
            conn.commit()
        finally:
            conn.close()
        self.algorithms.load_heaps_from_db()
        self.assertEqual(self.algorithms.count_available_slots(), 0)

    def _release_every_bay(self):
        conn = db.get_connection()
        try:
            conn.execute("UPDATE parking_slots SET status = 'available'")
            conn.commit()
        finally:
            conn.close()
        self.algorithms.load_heaps_from_db()

    def test_entry_is_refused_while_no_bay_is_free(self):
        self._fill_the_lot()
        try:
            session, error = self.algorithms.register_entry("FULL001", "car", "0700000021")
            self.assertIsNone(session)
            self.assertEqual(error, self.algorithms.PARKING_FULL_MESSAGE)
            # The refusal must not leave a half-written vehicle behind.
            conn = db.get_connection()
            try:
                vehicle = conn.execute(
                    "SELECT id FROM vehicles WHERE plate_number = 'FULL001'"
                ).fetchone()
                sessions = conn.execute(
                    "SELECT COUNT(*) AS n FROM parking_sessions ps "
                    "JOIN vehicles v ON v.id = ps.vehicle_id WHERE v.plate_number = 'FULL001'"
                ).fetchone()["n"]
            finally:
                conn.close()
            self.assertIsNone(vehicle, "a refused entry must not create a vehicle record")
            self.assertEqual(sessions, 0)
        finally:
            self._release_every_bay()

    def test_entry_refusal_survives_a_stale_slot_cache(self):
        # The heap says "space available" (nothing was pushed back on release)
        # while the database says the lot is full: the database must win.
        self._fill_the_lot()
        self.algorithms._free_slot_heaps.setdefault("car", []).append((0, 1))
        try:
            session, error = self.algorithms.register_entry("FULL002", "car", "0700000022")
            self.assertIsNone(session)
            self.assertEqual(error, self.algorithms.PARKING_FULL_MESSAGE)
            conn = db.get_connection()
            try:
                active = conn.execute(
                    "SELECT COUNT(*) AS n FROM parking_sessions WHERE slot_id = 1 AND status = 'active'"
                ).fetchone()["n"]
            finally:
                conn.close()
            self.assertEqual(active, 0, "a bay already taken must never be handed out twice")
        finally:
            self._release_every_bay()

    def test_entry_endpoint_reports_a_full_lot(self):
        self._fill_the_lot()
        try:
            client = self.app.test_client()
            response = client.post("/api/entry", json={
                "plate_number": "FULL003", "vehicle_type": "car", "owner_phone": "0700000023",
            })
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json["full"])
            self.assertEqual(response.json["error"], self.algorithms.PARKING_FULL_MESSAGE)
            self.assertEqual(response.headers.get("Retry-After"), "60")
            # The live counter the UI locks itself on agrees.
            self.assertEqual(client.get("/api/slots").json["available_total"], 0)
        finally:
            self._release_every_bay()

    def test_entry_reopens_once_a_bay_is_released(self):
        self._fill_the_lot()
        conn = db.get_connection()
        try:
            conn.execute("UPDATE parking_slots SET status = 'available' WHERE slot_number = 1")
            conn.commit()
        finally:
            conn.close()
        self.algorithms.load_heaps_from_db()
        try:
            session, error = self.algorithms.register_entry("FULL004", "car", "0700000024")
            self.assertIsNone(error)
            self.assertEqual(session["slot_number"], 1)
            self._cleanup_plates("FULL004")
        finally:
            self._release_every_bay()

    # Overstay alerts
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

    # Removed features: analytics and attendant overrides
    def test_analytics_and_override_endpoints_are_gone(self):
        client = self.app.test_client()
        for path in ("/analytics", "/api/analytics"):
            self.assertEqual(client.get(path).status_code, 404, path)
        self.assertEqual(
            client.post("/api/barrier/override", json={"session_id": 1, "reason": "x"}).status_code, 404)
        self.assertEqual(
            client.post("/api/slots/1/maintenance", json={"out_of_service": True, "reason": "x"}).status_code, 404)
        for name in ("analytics_summary", "set_slot_maintenance", "record_barrier_override", "record_override_event"):
            self.assertFalse(hasattr(self.algorithms, name), f"{name} should be gone")

    def test_authentication_endpoints_are_gone(self):
        client = self.app.test_client()
        for method, path in (
            ("post", "/api/auth/login"), ("post", "/api/auth/logout"), ("get", "/api/auth/me"),
            ("get", "/api/profile"), ("put", "/api/profile"),
            ("post", "/api/profile/mfa/setup"), ("post", "/api/profile/mfa/enable"),
            ("post", "/api/profile/mfa/disable"), ("delete", "/api/profile/sessions/1"),
        ):
            response = getattr(client, method)(path, json={})
            self.assertEqual(response.status_code, 404, f"{method.upper()} {path} should not exist")

    def test_management_endpoints_need_no_sign_in(self):
        client = self.app.test_client()  # a fresh client, never signed in
        self.assertEqual(client.get("/api/rates").status_code, 200)
        self.assertEqual(client.get("/api/plates?prefix=ZZZZ").status_code, 200)
        self.assertEqual(client.get("/api/activity").status_code, 200)
        export = client.get("/api/reports/export?format=xlsx")
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.data[:2] == b"PK", "an unauthenticated export must still be a real workbook")

    def test_no_auth_chrome_left_in_the_ui(self):
        html = self.app.test_client().get("/").data.decode("utf-8")
        for marker in ("login-modal", "login-form", "profile-modal", "auth-btn", "profile-btn"):
            self.assertNotIn(marker, html, f"{marker} still on the page")
        self.assertNotIn("Sign in", html, "the page still asks for a sign-in")

    def test_removed_features_leave_no_trace_in_the_ui(self):
        home = self.app.test_client().get("/").data.decode("utf-8")
        self.assertNotIn("Analytics", home)
        self.assertNotIn('href="/analytics"', home)
        for marker in ("override-open", "maintenance-slot", "maintenance-off", "override-session"):
            self.assertNotIn(marker, home, f"{marker} still exposed on the page")

    def test_override_audit_history_is_preserved(self):
        # The overrides feature is gone, but its audit trail is financial-grade
        # history: the table and every row recorded against it must survive.
        conn = db.get_connection()
        try:
            conn.execute(
                "INSERT INTO override_events (created_at, username, action, target, reason) "
                "VALUES (?, 'attendant', 'slot_maintenance', 'slot:1', 'historic entry kept for audit')",
                (datetime.now(timezone.utc).replace(microsecond=0).isoformat(),),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT action, target FROM override_events WHERE target = 'slot:1'"
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(any(row["action"] == "slot_maintenance" for row in rows))

    def test_bays_left_out_of_service_return_on_boot(self):
        # Without the removed maintenance control a stranded bay would stay
        # unusable forever, so start-up returns it to service and audits it.
        conn = db.get_connection()
        try:
            conn.execute("UPDATE parking_slots SET status = 'maintenance' WHERE slot_number = 2")
            conn.commit()
        finally:
            conn.close()
        try:
            db.init_db()
            conn = db.get_connection()
            try:
                status = conn.execute(
                    "SELECT status FROM parking_slots WHERE slot_number = 2"
                ).fetchone()["status"]
                audited = conn.execute(
                    "SELECT COUNT(*) AS n FROM override_events WHERE action = 'maintenance_reactivated'"
                ).fetchone()["n"]
            finally:
                conn.close()
            self.assertEqual(status, "available")
            self.assertGreaterEqual(audited, 1)
        finally:
            self._release_every_bay()

    # ParkFlow brand
    def test_ui_is_branded_parkflow(self):
        client = self.app.test_client()
        html = client.get("/").data.decode("utf-8")
        self.assertIn("ParkFlow", html)
        self.assertNotIn("SmartPark", html, "the page still shows the old brand")
        entry = client.post("/api/entry", json={
            "plate_number": "BRAND001", "vehicle_type": "car", "owner_phone": "0700000081",
        })
        self.assertEqual(entry.status_code, 200)
        self.assertTrue(entry.json["ticket_code"].startswith("PF-TK-"))
        self._cleanup_plates("BRAND001")

    def test_legacy_ticket_prefix_still_resolves(self):
        # Paper tickets printed before the rebrand must keep working.
        client = self.app.test_client()
        entry = client.post("/api/entry", json={
            "plate_number": "LEGACYTK1", "vehicle_type": "car", "owner_phone": "0700000082",
        })
        self.assertEqual(entry.status_code, 200)
        legacy_code = "SP-TK" + entry.json["ticket_code"][len("PF-TK"):]
        resolved = client.get(f"/api/ticket/{legacy_code}")
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json["plate_number"], "LEGACYTK1")
        self._cleanup_plates("LEGACYTK1")

    def test_entry_section_shows_the_full_lot_notice(self):
        html = self.app.test_client().get("/").data.decode("utf-8")
        self.assertIn('id="lot-full"', html)
        self.assertIn("Parking is currently full, please try again later.", html)
        self.assertIn('id="entry-submit"', html)

    def test_full_lot_notice_is_hidden_while_bays_are_free(self):
        # Regression: the notice is toggled with el.hidden, but
        # `.lot-full{display:grid}` outranks the user-agent [hidden] rule, so
        # the banner stayed on screen and told drivers the lot was full while
        # the Slots page showed free bays. A global [hidden] reset is what
        # keeps the attribute authoritative for every component.
        css_path = Path(__file__).resolve().parent.parent / "static" / "css" / "style.css"
        css = css_path.read_text(encoding="utf-8")
        # Anchor to a *global* rule only: `.fee-box[hidden]` is class-prefixed
        # and would otherwise satisfy this match on its own.
        rule = re.search(r"(?m)^[^{}\n]*\[hidden\]\s*\{([^}]*)\}", css)
        self.assertIsNotNone(
            rule, "stylesheet must force [hidden] to beat component display rules"
        )
        self.assertIn("display", rule.group(1))
        self.assertIn("none", rule.group(1))
        self.assertIn(
            "!important", rule.group(1),
            "[hidden] needs !important to outrank .lot-full{display:grid}",
        )

        # And the notice must ship hidden, so a lot with free bays is silent.
        html = self.app.test_client().get("/").data.decode("utf-8")
        banner = re.search(r'<p[^>]*id="lot-full"[^>]*>', html)
        self.assertIsNotNone(banner, "the entry section must keep the full-lot notice")
        self.assertIn(
            "hidden", banner.group(0),
            "the full-lot notice must ship hidden while bays are free",
        )


if __name__ == "__main__":
    unittest.main()
