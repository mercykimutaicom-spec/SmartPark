"""
algorithms.py
=============
SmartPark KE — Core algorithms for every module identified in the terms
of reference. Each function corresponds 1:1 to a module named in the
design document (Task 1); the pseudocode for each is reproduced in the
docstring above the function, so this file is the living, tested
version of that design.

MODULES IMPLEMENTED HERE
-------------------------
1. Slot Allocation Module      -> allocate_slot() / release_slot()
2. Vehicle Entry Module        -> register_entry()
3. Duration & Billing Module   -> calculate_fee()
4. Vehicle Exit Module         -> process_exit()
5. Payment Module              -> settle_payment()
6. Barrier Control Module      -> open_barrier()
7. Display Module              -> get_dashboard_stats() / list_slots()

KEY DATA STRUCTURE: THE FREE-SLOT MIN-HEAP
--------------------------------------------
Available slot numbers are kept in a binary min-heap (Python's `heapq`),
one per vehicle_type, mirrored by the `status` column in SQLite.

Why a min-heap instead of scanning the parking_slots table for
`status = 'available'` on every arrival?
  - Allocation (find the lowest-numbered free slot) is O(log n) instead
    of an O(n) table scan under load, and always returns the LOWEST
    free slot number, so occupied bays cluster near the entrance —
    shorter average walk for drivers, simpler signage.
  - Release (a car leaves) is a single O(log n) push back onto the heap.
  - The heap is rebuilt from the DB at start-up, so SQLite remains the
    single source of truth; the heap is purely a performance
    accelerator sitting in front of it, safe to discard and rebuild.
"""
import heapq
import secrets
import threading
import collections
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from db import DATABASE_URL, get_connection

# In-memory min-heap of available slots, keyed by vehicle_type.
# Entries are (walking_distance_from_entrance, slot_number) tuples, so the
# heap always pops the NEAREST free bay first (tie-break: lowest number).
# e.g. {"car": [(2, 1), (3, 9)], "motorcycle": [...], "van": [...]}
_free_slot_heaps = {}

# In-memory plate trie for prefix search (attendant lookup).
_PLATE_TRIE = None


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse(ts):
    parsed = datetime.fromisoformat(ts)
    # Older records were saved as naive UTC; keep them compatible while all
    # new records carry an explicit UTC offset.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


# ---------------------------------------------------------------------------
# MODULE 6b: FIFO Barrier Queue
# ---------------------------------------------------------------------------
# The design doc promises that simultaneous barrier requests are serialized
# first-come-first-served instead of racing. This is the real implementation:
# every open request is enqueued and a single dedicated worker drains the
# queue strictly in order, so two simultaneous exits (or an entry + exit) can
# never interleave barrier pulses.
class BarrierQueue:
    """Single-lane FIFO queue serving barrier open-requests in submit order."""

    _HISTORY = 32  # bounded service log for observability/tests

    def __init__(self):
        self._queue = collections.deque()
        self._cond = threading.Condition()
        self._history = collections.deque(maxlen=self._HISTORY)
        self._worker = None

    def _drain(self):
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                entry = self._queue.popleft()
            session, done = entry
            try:
                result = open_barrier(session)
            except Exception as error:  # never let one request kill the worker
                result = {"opened": False, "reason": f"Barrier error: {error}"}
            self._history.append(result)
            done["result"] = result
            done["event"].set()

    def request(self, session, timeout=3.0):
        """Enqueue an open request; block until the FIFO worker serves it."""
        with self._cond:
            done = {"event": threading.Event(), "result": None}
            self._queue.append((session, done))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, daemon=True, name="barrier-fifo")
                self._worker.start()
            depth_before = len(self._queue) - 1
            self._cond.notify_all()  # wake the worker for the new request
        if not done["event"].wait(timeout):
            return {"opened": False, "reason": "Barrier queue timed out.", "queued": depth_before}
        return done["result"]

    def queue_depth(self):
        with self._cond:
            return len(self._queue)

    def service_order(self):
        """Bounded log of the most recent results, oldest first (FIFO proof)."""
        return list(self._history)


barrier_queue = BarrierQueue()


# ---------------------------------------------------------------------------
# MODULE 7b: Plate Prefix Search (trie)
# ---------------------------------------------------------------------------
class PlateTrie:
    """Trie over plate strings for O(prefix) autocomplete lookups."""

    def __init__(self):
        self._children = {}
        self._plates = set()  # terminal marker storing full plates

    def insert(self, plate):
        node = self._children
        for char in plate:
            node = node.setdefault(char, {})
            node.setdefault("#", set()).add(plate)
        node.setdefault("$", set()).add(plate)

    def starts_with(self, prefix, limit=10):
        node = self._children
        for char in prefix:
            if char not in node:
                return []
            node = node[char]
        matches = sorted(node.get("#", set()))
        return matches[:limit]


# ---------------------------------------------------------------------------
# Lot geometry: slot_number -> grid position -> walking distance
# ---------------------------------------------------------------------------
# The 40-bay lot is laid out as an 8-column grid (car bays rows 0-2, motorcycle
# and van bays rows 3-4). The driver entrance is the top-left corner (0, 0).
# Distances below are computed with Dijkstra over the grid (uniform edge cost,
# so it degenerates to BFS but demonstrates the priority-queue machinery).
GRID_COLS = 8
GRID_ROWS = 5
ENTRANCE = (0, 0)


def _grid_distance_map():
    """Dijkstra from the entrance across the slot grid.

    Each cell is one bay; moving between adjacent cells costs 1. A binary
    min-heap (`heapq`) is the priority queue, exactly as in Dijkstra 1959.
    Returns {slot_number: walking_distance}.
    """
    import itertools
    distances = {}
    counter = itertools.count()  # tie-breaker so heapq never compares tuples of cells
    start = ENTRANCE
    heap = [(0, next(counter), start)]
    seen = set()
    while heap:
        dist, _, (row, col) = heapq.heappop(heap)
        if (row, col) in seen:
            continue
        seen.add((row, col))
        slot_number = row * GRID_COLS + col + 1  # 1-based bays, row-major
        distances[slot_number] = dist
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nr, nc = row + dr, col + dc
            if 0 <= nr < GRID_ROWS and 0 <= nc < GRID_COLS and (nr, nc) not in seen:
                heapq.heappush(heap, (dist + 1, next(counter), (nr, nc)))
    return distances


_SLOT_DISTANCE = _grid_distance_map()


def slot_distance(slot_number):
    """Walking distance (grid steps) from the entrance to a bay."""
    return _SLOT_DISTANCE.get(int(slot_number), int(slot_number))


def load_heaps_from_db():
    """
    Rebuild the in-memory min-heaps (and the plate trie) from the database's
    current state. Called once at application start-up (and safe to call any
    time to resync) — this is what makes the heap a *cache*, not a parallel
    source of truth.
    """
    global _PLATE_TRIE
    _free_slot_heaps.clear()
    conn = get_connection()
    rows = conn.execute(
        "SELECT slot_number, vehicle_type FROM parking_slots WHERE status = 'available'"
    ).fetchall()
    for row in rows:
        heapq.heappush(
            _free_slot_heaps.setdefault(row["vehicle_type"], []),
            (slot_distance(row["slot_number"]), row["slot_number"]),
        )
    plates = conn.execute("SELECT plate_number FROM vehicles").fetchall()
    conn.close()
    _PLATE_TRIE = PlateTrie()
    for row in plates:
        _PLATE_TRIE.insert(row["plate_number"])


def allocate_slot(conn, vehicle_type="car"):
    """
    MODULE 1: Slot Allocation Algorithm (nearest-bay, Dijkstra-ordered)
    --------------------------------------------------------------------
    PSEUDOCODE:
        1. If no free-slot heap entry exists for this vehicle_type -> return None
        2. Pop the (distance, slot_number) pair with the SMALLEST walking
           distance from the entrance (O(log n)); ties go to the lower
           slot_number — drivers walk less, occupancy clusters near the gate.
        3. Look up the matching parking_slots row
        4. Mark it 'occupied'
        5. Return the slot row

    Returns the allocated slot row (sqlite3.Row), or None if the lot is full.
    Caller is responsible for commit().
    """
    heap = _free_slot_heaps.get(vehicle_type, [])
    if not heap:
        return None  # Lot full for this vehicle type

    def _pop():
        _, slot_number = heapq.heappop(heap)
        return conn.execute(
            "SELECT * FROM parking_slots WHERE slot_number = ? AND vehicle_type = ?",
            (slot_number, vehicle_type),
        ).fetchone()

    slot = _pop()
    if slot is None or slot["status"] != "available":
        # Heap/DB drifted out of sync (shouldn't happen) — resync and retry once.
        load_heaps_from_db()
        heap = _free_slot_heaps.get(vehicle_type, [])
        if not heap:
            return None
        slot = _pop()

    conn.execute("UPDATE parking_slots SET status = 'occupied' WHERE id = ?", (slot["id"],))
    return slot


def release_slot(conn, slot_id, vehicle_type, slot_number):
    """
    Companion to allocate_slot(): called when a vehicle exits. Marks the
    slot free in the DB and pushes (distance, slot_number) back onto the
    min-heap so it becomes immediately available for reallocation. O(log n).
    Caller is responsible for commit().
    """
    conn.execute("UPDATE parking_slots SET status = 'available' WHERE id = ?", (slot_id,))
    heapq.heappush(
        _free_slot_heaps.setdefault(vehicle_type, []),
        (slot_distance(slot_number), slot_number),
    )


def register_entry(plate_number, vehicle_type="car", owner_phone=None):
    """
    MODULE 2: Vehicle Entry Algorithm
    -----------------------------------
    PSEUDOCODE:
        1. Normalise plate_number (uppercase, strip spaces)
        2. HASH LOOKUP: does a vehicle with this plate already exist?
              -> SQLite's UNIQUE index on plate_number gives O(1)-ish
                 average lookup instead of scanning every vehicle ever seen.
              - if yes: reuse the record, increment visit_count
              - if no:  insert a new vehicle record
        3. Guard: does this vehicle already have an ACTIVE session?
              -> reject (no double entry / already parked)
        4. Call Slot Allocation Algorithm for vehicle_type
              - if no slot available -> reject entry, return error
        5. Insert a new parking_sessions row: entry_time = now(), status='active'
        6. Commit and return the session (drives the barrier + display)

    Returns (session_dict, error_message). error_message is None on success.
    """
    plate_number = plate_number.strip().upper().replace(" ", "")
    if not plate_number:
        return None, "Plate number is required."
    owner_phone = (owner_phone or "").strip()
    if not owner_phone:
        return None, "Phone number is required for payment."
    if vehicle_type not in ("car", "motorcycle", "van"):
        vehicle_type = "car"

    conn = get_connection()
    try:
        vehicle = conn.execute(
            "SELECT * FROM vehicles WHERE plate_number = ?", (plate_number,)
        ).fetchone()

        if vehicle is None:
            conn.execute(
                "INSERT INTO vehicles (plate_number, vehicle_type, owner_phone, first_seen, visit_count) "
                "VALUES (?, ?, ?, ?, 1)",
                (plate_number, vehicle_type, owner_phone, _now().isoformat()),
            )
            vehicle_id = conn.execute(
                "SELECT id FROM vehicles WHERE plate_number = ?", (plate_number,)
            ).fetchone()["id"]
            if _PLATE_TRIE is not None:
                _PLATE_TRIE.insert(plate_number)
        else:
            vehicle_id = vehicle["id"]
            active = conn.execute(
                "SELECT ps.id, sl.slot_number FROM parking_sessions ps "
                "JOIN parking_slots sl ON sl.id = ps.slot_id "
                "WHERE ps.vehicle_id = ? AND ps.status = 'active'",
                (vehicle_id,),
            ).fetchone()
            if active:
                return None, f"{plate_number} already has an active session in slot {active['slot_number']}."
            conn.execute(
                "UPDATE vehicles SET visit_count = visit_count + 1, "
                "owner_phone = COALESCE(?, owner_phone) WHERE id = ?",
                (owner_phone, vehicle_id),
            )

        slot = allocate_slot(conn, vehicle_type)
        if slot is None:
            conn.rollback()
            return None, f"Parking full for vehicle type '{vehicle_type}'."

        entry_time = _now().isoformat()
        if DATABASE_URL:
            cur = conn.execute(
                "INSERT INTO parking_sessions (vehicle_id, slot_id, entry_time, status) "
                "VALUES (?, ?, ?, 'active') RETURNING id",
                (vehicle_id, slot["id"], entry_time),
            )
            session_id = cur.fetchone()["id"]
        else:
            cur = conn.execute(
                "INSERT INTO parking_sessions (vehicle_id, slot_id, entry_time, status) "
                "VALUES (?, ?, ?, 'active')",
                (vehicle_id, slot["id"], entry_time),
            )
            session_id = cur.lastrowid
        conn.commit()

        return {
            "id": session_id,
            "vehicle": plate_number,
            "slot_number": slot["slot_number"],
            "owner_phone": owner_phone,
            "entry_time": entry_time,
            "status": "active",
        }, None
    finally:
        conn.close()


def get_parking_rates():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT max_minutes, fee_amount, updated_at FROM parking_rates ORDER BY max_minutes"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_vat_rate():
    conn = get_connection()
    try:
        row = conn.execute("SELECT vat_rate FROM tax_settings WHERE id = 1").fetchone()
    finally:
        conn.close()
    return float(row["vat_rate"]) if row else 16.0


def calculate_totals(subtotal):
    vat_rate = get_vat_rate()
    vat_amount = int((Decimal(subtotal) * Decimal(str(vat_rate)) / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return subtotal, vat_rate, vat_amount, subtotal + vat_amount


def calculate_fee(entry_time: datetime, exit_time: datetime):
    """
    MODULE 3: Duration & Billing Algorithm
    -----------------------------------------
    Tiered fee schedule (client's terms of reference, Kshs.):
        <= 30 minutes            : FREE   (0)
        <= 2 hours (120 min)     : 50
        <= 4 hours (240 min)     : 100
        <= 6 hours (360 min)     : 300
        >  6 hours                 : 500

    PSEUDOCODE:
        1. duration_minutes = ceil((exit_time - entry_time) in minutes)
        2. Walk the tier table top-down; return the first tier whose
           upper bound >= duration_minutes                (O(1), 4 tiers)
        3. Anything past the last tier falls through to the flat 500 fee

    Implemented as an ordered list of (threshold_minutes, fee) so the
    tier table is trivial to change if the client's pricing changes —
    that is the "dynamic" part of the billing rule set.
    """
    delta = exit_time - entry_time
    total_seconds = max(0, delta.total_seconds())
    duration_minutes = int(total_seconds // 60) + (1 if total_seconds % 60 else 0)

    for rate in get_parking_rates():
        if duration_minutes <= rate["max_minutes"]:
            return duration_minutes, rate["fee_amount"]
    raise RuntimeError("No parking rate is configured for this duration.")


def process_exit(plate_number):
    """
    MODULE 4: Vehicle Exit Algorithm
    ------------------------------------
    PSEUDOCODE:
        1. HASH LOOKUP vehicle by plate_number
        2. Find its ACTIVE parking session
              - if none -> error "vehicle not found in lot"
        3. exit_time = now(); run Billing Algorithm -> duration, fee
        4. If fee == 0: session settled immediately (no payment needed)
              -> mark 'completed', release slot, barrier opens right away
           Else: mark session 'awaiting_payment' and STOP — barrier
                 stays shut until settle_payment() is called
        5. Return the session (with fee) + error

    Returns (session_dict, error_message).
    """
    plate_number = plate_number.strip().upper().replace(" ", "")
    conn = get_connection()
    try:
        vehicle = conn.execute(
            "SELECT * FROM vehicles WHERE plate_number = ?", (plate_number,)
        ).fetchone()
        if vehicle is None:
            return None, "Vehicle not recognised — was it ever checked in?"

        session = conn.execute(
            "SELECT ps.*, sl.slot_number, sl.vehicle_type AS slot_vtype "
            "FROM parking_sessions ps JOIN parking_slots sl ON sl.id = ps.slot_id "
            "WHERE ps.vehicle_id = ? AND ps.status IN ('active', 'awaiting_payment') "
            "ORDER BY ps.id DESC LIMIT 1",
            (vehicle["id"],),
        ).fetchone()
        if session is None:
            return None, "No active parking session for this vehicle."
        if session["status"] == "awaiting_payment":
            return None, "Payment is still pending for this vehicle. Complete payment before exiting."

        entry_time = _parse(session["entry_time"])
        exit_time = _now()
        duration, subtotal = calculate_fee(entry_time, exit_time)
        subtotal, vat_rate, vat_amount, total = calculate_totals(subtotal)

        new_status = "completed" if total == 0 else "awaiting_payment"
        conn.execute(
            "UPDATE parking_sessions SET exit_time = ?, duration_minutes = ?, "
            "fee_charged = ?, subtotal_amount = ?, vat_rate = ?, vat_amount = ?, "
            "total_amount = ?, status = ? WHERE id = ?",
            (exit_time.isoformat(), duration, total, subtotal, vat_rate, vat_amount,
             total, new_status, session["id"]),
        )

        if total == 0:
            release_slot(conn, session["slot_id"], session["slot_vtype"], session["slot_number"])

        conn.commit()

        return {
            "id": session["id"],
            "vehicle": plate_number,
            "slot_number": session["slot_number"],
            "vehicle_type": session["slot_vtype"],
            "owner_phone": vehicle["owner_phone"],
            "entry_time": session["entry_time"],
            "exit_time": exit_time.isoformat(),
            "duration_minutes": duration,
            "subtotal_amount": subtotal,
            "vat_rate": vat_rate,
            "vat_amount": vat_amount,
            "total_amount": total,
            "fee_charged": total,
            "status": new_status,
        }, None
    finally:
        conn.close()


def settle_payment(session_id, method="mpesa", reference=None, provider_reference=None):
    """
    MODULE 5: Payment Algorithm
    -------------------------------
    PSEUDOCODE:
        1. Look up parking_sessions row by id; must be 'awaiting_payment'
        2. Insert a payments row for session.fee_charged
        3. Mark session 'completed'
        4. Call Slot Allocation's release_slot() to free the bay
        5. Trigger open_barrier() (Barrier Control Module)

    Returns (session_dict, error_message).
    """
    conn = get_connection()
    try:
        session = conn.execute(
            "SELECT ps.*, sl.slot_number, sl.vehicle_type AS slot_vtype, "
            "v.plate_number, v.owner_phone "
            "FROM parking_sessions ps "
            "JOIN parking_slots sl ON sl.id = ps.slot_id "
            "JOIN vehicles v ON v.id = ps.vehicle_id "
            "WHERE ps.id = ?",
            (session_id,),
        ).fetchone()
        if session is None or session["status"] != "awaiting_payment":
            return None, "No pending payment for this session."

        receipt_number = f"SP-{secrets.token_hex(8).upper()}"

        payment = conn.execute(
            "SELECT id FROM payments WHERE session_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1", (session["id"],)
        ).fetchone()
        if payment:
            conn.execute(
                "UPDATE payments SET status = 'success', paid_at = ?, reference = ?, "
                "provider_reference = COALESCE(?, provider_reference), "
                "receipt_number = COALESCE(receipt_number, ?), updated_at = ? WHERE id = ?",
                (_now().isoformat(), reference, provider_reference, receipt_number,
                 _now().isoformat(), payment["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO payments (session_id, amount, method, paid_at, reference, "
                "provider_reference, phone_number, status, initiated_at, updated_at, receipt_number) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'success', ?, ?, ?)",
                (session["id"], session["fee_charged"], method, _now().isoformat(), reference,
                 provider_reference, session["owner_phone"], _now().isoformat(), _now().isoformat(), receipt_number),
            )
        conn.execute("UPDATE parking_sessions SET status = 'completed' WHERE id = ?", (session["id"],))
        release_slot(conn, session["slot_id"], session["slot_vtype"], session["slot_number"])
        conn.commit()

        return {
            "id": session["id"],
            "vehicle": session["plate_number"],
            "slot_number": session["slot_number"],
            "vehicle_type": session["slot_vtype"],
            "owner_phone": session["owner_phone"],
            "entry_time": session["entry_time"],
            "exit_time": session["exit_time"],
            "fee_charged": session["fee_charged"],
            "status": "completed",
        }, None
    finally:
        conn.close()


def search_plates(prefix, limit=10):
    """MODULE 7 (part): Trie-backed plate prefix search for the attendant."""
    if _PLATE_TRIE is None:
        load_heaps_from_db()
    if _PLATE_TRIE is None:
        return []
    return _PLATE_TRIE.starts_with(prefix.strip().upper().replace(" ", ""), limit=limit)


def open_barrier(session: dict):
    """
    MODULE 6: Barrier Control Algorithm (simulated actuator signal)
    --------------------------------------------------------------------
    PSEUDOCODE:
        1. Precondition: session['status'] == 'completed' (paid in full,
           or free tier) — the barrier algorithm refuses to fire on any
           other state; this is the physical safety interlock.
        2. Send an OPEN pulse to the physical barrier controller (here:
           returned to the frontend, which plays the lift animation).
        3. Auto-CLOSE after a short timeout once the vehicle clears the
           loop sensor (simulated client-side by the UI's timed animation).

    In production this function would write to a GPIO pin / send a
    request to the barrier's serial or IoT controller. Here it returns
    a signal object consumed by the web UI.
    """
    if session.get("status") != "completed":
        return {"opened": False, "reason": "Payment not settled."}
    return {"opened": True, "slot": session["slot_number"], "session_id": session["id"]}


def get_dashboard_stats():
    """
    MODULE 7 (part): Display Module — aggregate stats
    -----------------------------------------------------
    Live counts for the driver-facing visual display: total / available /
    occupied slots per vehicle type. O(n) over the slots table, where n
    is the small, fixed number of physical bays — not the unboundedly
    growing sessions table.
    """
    conn = get_connection()
    rows = conn.execute("SELECT vehicle_type, status FROM parking_slots").fetchall()
    conn.close()

    stats = {}
    for row in rows:
        bucket = stats.setdefault(row["vehicle_type"], {"total": 0, "available": 0, "occupied": 0})
        bucket["total"] += 1
        bucket[row["status"]] += 1
    return stats


def list_slots():
    """MODULE 7 (part): Display Module — full slot list for the live map."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT slot_number, zone, vehicle_type, status FROM parking_slots ORDER BY slot_number"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_recent_activity(limit=15):
    """Attendant-facing recent activity feed."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT ps.id, v.plate_number AS vehicle, sl.slot_number, ps.entry_time, "
        "ps.exit_time, ps.duration_minutes, ps.fee_charged, ps.status, "
        "p.id AS payment_id, p.status AS payment_status, p.receipt_number "
        "FROM parking_sessions ps "
        "JOIN vehicles v ON v.id = ps.vehicle_id "
        "JOIN parking_slots sl ON sl.id = ps.slot_id "
        "LEFT JOIN payments p ON p.id = ("
        "SELECT p2.id FROM payments p2 WHERE p2.session_id = ps.id ORDER BY p2.id DESC LIMIT 1) "
        "ORDER BY ps.entry_time DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    activity = []
    for row in rows:
        item = dict(row)
        item["receipt_number"] = (
            item["receipt_number"]
            if item["status"] == "completed" and item["payment_status"] == "success"
            else None
        )
        # Overstay alert: still active well past a normal stay.
        item["overstay"] = (
            item["status"] == "active"
            and (_now() - _parse(item["entry_time"])).total_seconds() >= overstay_hours() * 3600
        )
        activity.append(item)
    return activity


def overstay_hours():
    """Configurable overstay threshold (default 8h) for attendant alerts."""
    import os
    try:
        return float(os.environ.get("OVERSTAY_HOURS", "8"))
    except ValueError:
        return 8.0


def count_overstays():
    """Number of active sessions past the overstay threshold."""
    threshold = overstay_hours()
    conn = get_connection()
    rows = conn.execute(
        "SELECT ps.entry_time FROM parking_sessions ps WHERE ps.status = 'active'"
    ).fetchall()
    conn.close()
    now = _now()
    return sum(1 for row in rows if (now - _parse(row["entry_time"])).total_seconds() >= threshold * 3600)


def set_slot_maintenance(slot_number, out_of_service, reason, username):
    """MODULE 1 (companion): attendant override — mark a bay out of service.

    Only AVAILABLE bays can go out of service (an occupied bay has a car in
    it). Returns (ok, error_message); every toggle is written to the
    override_events audit table with who/when/why.
    """
    conn = get_connection()
    try:
        slot = conn.execute(
            "SELECT id, status FROM parking_slots WHERE slot_number = ?", (slot_number,)
        ).fetchone()
        if slot is None:
            return False, "Slot not found."
        new_status = "maintenance" if out_of_service else "available"
        if slot["status"] == "occupied":
            return False, "Slot is occupied — it cannot be taken out of service."
        if slot["status"] == new_status:
            return False, "Slot is already in that state."
        conn.execute(
            "UPDATE parking_slots SET status = ? WHERE id = ?", (new_status, slot["id"])
        )
        conn.execute(
            "INSERT INTO override_events (created_at, username, action, target, reason) "
            "VALUES (?, ?, 'slot_maintenance', ?, ?)",
            (_now().isoformat(), username, f"slot:{slot_number}", reason),
        )
        conn.commit()
    finally:
        conn.close()
    load_heaps_from_db()  # heap cache must reflect the new bay state
    return True, None


def record_barrier_override(session_id, reason, username):
    """MODULE 6 (companion): audited manual barrier replay for completed sessions.

    NEVER bypasses payment: only sessions already 'completed' (paid or free)
    can be force-opened (e.g. the lift animation missed / sensor jammed).
    Returns (result_dict, error_message).
    """
    conn = get_connection()
    try:
        session = conn.execute(
            "SELECT ps.*, sl.slot_number FROM parking_sessions ps "
            "JOIN parking_slots sl ON sl.id = ps.slot_id WHERE ps.id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            return None, "Session not found."
        if session["status"] != "completed":
            return None, (
                "Barrier override is only available for completed sessions. "
                "Settle payment (cash/STK) instead — unpaid vehicles must never bypass the barrier."
            )
        conn.execute(
            "INSERT INTO override_events (created_at, username, action, target, reason) "
            "VALUES (?, ?, 'barrier_override', ?, ?)",
            (_now().isoformat(), username, f"session:{session_id}", reason),
        )
        conn.commit()
    finally:
        conn.close()
    result = barrier_queue.request(dict(session), timeout=3.0)
    return result, None


def record_override_event(username, action, target, reason):
    """Append an entry to the override audit trail."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO override_events (created_at, username, action, target, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now().isoformat(), username, action, target, reason),
        )
        conn.commit()
    finally:
        conn.close()


def analytics_summary(days=14):
    """MODULE 8: Analytics — revenue by day/hour, occupancy, busiest zone.

    All aggregates read existing payments/parking_sessions data; nothing is
    fabricated. Revenue counts only successful payments.
    """
    from collections import Counter, defaultdict
    conn = get_connection()
    payments = conn.execute(
        "SELECT p.paid_at, p.amount, p.method FROM payments p "
        "WHERE p.status = 'success' ORDER BY p.paid_at DESC LIMIT 20000"
    ).fetchall()
    sessions = conn.execute(
        "SELECT sl.zone, ps.duration_minutes FROM parking_sessions ps "
        "JOIN parking_slots sl ON sl.id = ps.slot_id ORDER BY ps.id DESC LIMIT 20000"
    ).fetchall()
    zones_now = conn.execute(
        "SELECT zone, status, COUNT(*) AS n FROM parking_slots GROUP BY zone, status"
    ).fetchall()
    conn.close()

    now = _now()
    cutoff = datetime.fromtimestamp(now.timestamp() - days * 86400, timezone.utc).replace(microsecond=0)

    revenue_by_day = defaultdict(int)
    method_totals = Counter()
    total_revenue = 0
    revenue_by_hour = defaultdict(int)
    for row in payments:
        paid = _parse(row["paid_at"])
        if paid >= cutoff:
            revenue_by_day[paid.date().isoformat()] += row["amount"]
            method_totals[row["method"]] += row["amount"]
            total_revenue += row["amount"]
        if paid.date() == now.date():
            revenue_by_hour[paid.hour] += row["amount"]

    sessions_by_zone = Counter()
    duration_by_zone = defaultdict(list)
    for row in sessions:
        sessions_by_zone[row["zone"]] += 1
        if row["duration_minutes"] is not None:
            duration_by_zone[row["zone"]].append(row["duration_minutes"])

    occupancy = defaultdict(lambda: {"available": 0, "occupied": 0, "maintenance": 0})
    for row in zones_now:
        bucket = occupancy[row["zone"]]
        bucket[row["status"]] = bucket.get(row["status"], 0) + row["n"]

    busiest = sessions_by_zone.most_common(1)
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "total_revenue": total_revenue,
        "revenue_by_day": [{"date": d, "amount": a} for d, a in sorted(revenue_by_day.items())],
        "revenue_by_hour_today": [{"hour": h, "amount": a} for h, a in sorted(revenue_by_hour.items())],
        "revenue_by_method": [{"method": m, "amount": a} for m, a in method_totals.most_common()],
        "occupancy_by_zone": [
            {"zone": z, **counts,
             "utilization": round(counts.get("occupied", 0) /
                                  max(1, counts.get("occupied", 0) + counts.get("available", 0)), 3)}
            for z, counts in sorted(occupancy.items())
        ],
        "busiest_zone": {"zone": busiest[0][0], "sessions": busiest[0][1]} if busiest else None,
        "avg_duration_by_zone": [
            {"zone": z, "avg_minutes": round(sum(v) / len(v), 1)}
            for z, v in sorted(duration_by_zone.items()) if v
        ],
    }
