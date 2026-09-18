"""
db.py
=====
SmartPark KE — Database connection and schema.

Uses PostgreSQL when `DATABASE_URL` is configured for production concurrency,
and falls back to Python's built-in `sqlite3` module for local development and
tests.

DYNAMIC DATABASE DESIGN
------------------------
"Dynamic" is satisfied two ways here:
1. Operationally: rows are constantly inserted/updated as vehicles
   arrive and leave — this is a live transactional store, not a static
   lookup table.
2. Structurally: the schema is declared once in SCHEMA_SQL and can grow
   (new columns/tables — e.g. adding a `reservations` table later)
   without touching the rest of the application, because every module
   talks to the database only through the functions in this file and
   algorithms.py, never with inline SQL scattered around the codebase.

ENTITY-RELATIONSHIP SUMMARY
----------------------------
parking_slots (1) --- (0..1 active) parking_sessions (M) --- (1) vehicles
parking_sessions (1) --- (0..1) payments
"""
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # PostgreSQL is optional for local SQLite development.
    psycopg = None
    dict_row = None

DB_PATH = Path(os.environ.get("SQLITE_DB_PATH", Path(__file__).parent / "smartpark.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS parking_slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_number  INTEGER NOT NULL UNIQUE,
    zone         TEXT NOT NULL DEFAULT 'A',
    vehicle_type TEXT NOT NULL DEFAULT 'car',
    status       TEXT NOT NULL DEFAULT 'available'   -- available | occupied
);
CREATE INDEX IF NOT EXISTS idx_slots_status ON parking_slots(status);

CREATE TABLE IF NOT EXISTS vehicles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_number  TEXT NOT NULL UNIQUE,               -- HASH-INDEXED lookup key
    vehicle_type  TEXT NOT NULL DEFAULT 'car',
    owner_phone   TEXT,
    first_seen    TEXT NOT NULL,
    visit_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vehicles_plate ON vehicles(plate_number);

CREATE TABLE IF NOT EXISTS parking_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id       INTEGER NOT NULL REFERENCES vehicles(id),
    slot_id          INTEGER NOT NULL REFERENCES parking_slots(id),
    entry_time       TEXT NOT NULL,
    exit_time        TEXT,
    duration_minutes INTEGER,
    fee_charged      INTEGER,
    subtotal_amount  INTEGER,
    vat_rate         REAL,
    vat_amount       INTEGER,
    total_amount     INTEGER,
    status           TEXT NOT NULL DEFAULT 'active'   -- active | awaiting_payment | completed
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON parking_sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_vehicle ON parking_sessions(vehicle_id);

CREATE TABLE IF NOT EXISTS parking_rates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    max_minutes  INTEGER NOT NULL UNIQUE,
    fee_amount   INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tax_settings (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    vat_rate     REAL NOT NULL DEFAULT 16,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'attendant',
    created_at    TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    mfa_secret    TEXT,
    mfa_enabled   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS active_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    user_agent    TEXT,
    ip_address    TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_active_sessions_user ON active_sessions(user_id);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES parking_sessions(id),
    amount      INTEGER NOT NULL,
    method      TEXT NOT NULL DEFAULT 'mpesa',
    paid_at     TEXT NOT NULL,
    reference   TEXT,
    provider_reference TEXT,
    phone_number TEXT,
    status      TEXT NOT NULL DEFAULT 'success',
    initiated_at TEXT,
    updated_at TEXT,
    receipt_number TEXT UNIQUE
);
"""

POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS parking_slots (
    id SERIAL PRIMARY KEY, slot_number INTEGER NOT NULL UNIQUE,
    zone TEXT NOT NULL DEFAULT 'A', vehicle_type TEXT NOT NULL DEFAULT 'car',
    status TEXT NOT NULL DEFAULT 'available'
);
CREATE INDEX IF NOT EXISTS idx_slots_status ON parking_slots(status);
CREATE TABLE IF NOT EXISTS vehicles (
    id SERIAL PRIMARY KEY, plate_number TEXT NOT NULL UNIQUE,
    vehicle_type TEXT NOT NULL DEFAULT 'car', owner_phone TEXT,
    first_seen TEXT NOT NULL, visit_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vehicles_plate ON vehicles(plate_number);
CREATE TABLE IF NOT EXISTS parking_sessions (
    id SERIAL PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    slot_id INTEGER NOT NULL REFERENCES parking_slots(id), entry_time TEXT NOT NULL,
    exit_time TEXT, duration_minutes INTEGER, fee_charged INTEGER,
    subtotal_amount INTEGER, vat_rate REAL, vat_amount INTEGER, total_amount INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON parking_sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_vehicle ON parking_sessions(vehicle_id);
CREATE TABLE IF NOT EXISTS parking_rates (
    id SERIAL PRIMARY KEY, max_minutes INTEGER NOT NULL UNIQUE,
    fee_amount INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tax_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1), vat_rate REAL NOT NULL DEFAULT 16,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'attendant', created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    mfa_secret TEXT, mfa_enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS active_sessions (
    id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, last_seen TEXT NOT NULL,
    expires_at TEXT NOT NULL, user_agent TEXT, ip_address TEXT, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_active_sessions_user ON active_sessions(user_id);
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES parking_sessions(id),
    amount INTEGER NOT NULL, method TEXT NOT NULL DEFAULT 'mpesa', paid_at TEXT NOT NULL,
    reference TEXT, provider_reference TEXT, phone_number TEXT,
    status TEXT NOT NULL DEFAULT 'success', initiated_at TEXT, updated_at TEXT,
    receipt_number TEXT UNIQUE
);
"""


class PostgresConnection:
    """Small compatibility adapter for the existing qmark-style SQL calls."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _sql(statement):
        return statement.replace("?", "%s")

    def execute(self, statement, parameters=()):
        return self.connection.execute(self._sql(statement), parameters)

    def executemany(self, statement, parameters):
        return self.connection.executemany(self._sql(statement), parameters)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()

# Lot layout: how many bays of each type to seed on first run.
LOT_LAYOUT = {
    "car": 24,
    "motorcycle": 10,
    "van": 6,
}
ZONE_OF_TYPE = {"car": "A", "motorcycle": "B", "van": "C"}


def get_connection():
    """One connection per call; Flask wraps this per-request (see app.py)."""
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("PostgreSQL is configured but psycopg is not installed.")
        return PostgresConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they don't exist, and seed slots on first run."""
    conn = get_connection()
    if DATABASE_URL:
        conn.executescript(POSTGRES_SCHEMA_SQL)
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled INTEGER NOT NULL DEFAULT 0")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if conn.execute("SELECT COUNT(*) AS n FROM parking_rates").fetchone()["n"] == 0:
            conn.executemany(
                "INSERT INTO parking_rates (max_minutes, fee_amount, updated_at) VALUES (?, ?, ?)",
                [(30, 0, now), (120, 50, now), (240, 100, now), (360, 300, now), (2147483647, 500, now)],
            )
        if conn.execute("SELECT COUNT(*) AS n FROM tax_settings").fetchone()["n"] == 0:
            conn.execute("INSERT INTO tax_settings (id, vat_rate, updated_at) VALUES (1, 16, ?)", (now,))
        admin_username = os.environ.get("ADMIN_USERNAME")
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if admin_username and admin_password and not conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (admin_username,)
        ).fetchone():
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'manager', ?)",
                (admin_username, generate_password_hash(admin_password), now),
            )
        if conn.execute("SELECT COUNT(*) AS n FROM parking_slots").fetchone()["n"] == 0:
            slot_number = 1
            for vtype, count in LOT_LAYOUT.items():
                for _ in range(count):
                    conn.execute(
                        "INSERT INTO parking_slots (slot_number, zone, vehicle_type, status) VALUES (?, ?, ?, 'available')",
                        (slot_number, ZONE_OF_TYPE[vtype], vtype),
                    )
                    slot_number += 1
        conn.commit()
        conn.close()
        return
    conn.executescript(SCHEMA_SQL)
    rate_count = conn.execute("SELECT COUNT(*) AS n FROM parking_rates").fetchone()["n"]
    if rate_count == 0:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        conn.executemany(
            "INSERT INTO parking_rates (max_minutes, fee_amount, updated_at) VALUES (?, ?, ?)",
            [(30, 0, now), (120, 50, now), (240, 100, now), (360, 300, now), (2147483647, 500, now)],
        )
    tax_count = conn.execute("SELECT COUNT(*) AS n FROM tax_settings").fetchone()["n"]
    if tax_count == 0:
        conn.execute(
            "INSERT INTO tax_settings (id, vat_rate, updated_at) VALUES (1, 16, ?)",
            (datetime.now(timezone.utc).replace(microsecond=0).isoformat(),),
        )
    admin_username = os.environ.get("ADMIN_USERNAME")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if admin_username and admin_password and not conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (admin_username,)
    ).fetchone():
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'manager', ?)",
            (admin_username, generate_password_hash(admin_password),
             datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )
    payment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(payments)")}
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    user_migrations = {
        "mfa_secret": "ALTER TABLE users ADD COLUMN mfa_secret TEXT",
        "mfa_enabled": "ALTER TABLE users ADD COLUMN mfa_enabled INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in user_migrations.items():
        if column not in user_columns:
            conn.execute(statement)
    session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(parking_sessions)")}
    session_migrations = {
        "subtotal_amount": "ALTER TABLE parking_sessions ADD COLUMN subtotal_amount INTEGER",
        "vat_rate": "ALTER TABLE parking_sessions ADD COLUMN vat_rate REAL",
        "vat_amount": "ALTER TABLE parking_sessions ADD COLUMN vat_amount INTEGER",
        "total_amount": "ALTER TABLE parking_sessions ADD COLUMN total_amount INTEGER",
    }
    for column, statement in session_migrations.items():
        if column not in session_columns:
            conn.execute(statement)
    migrations = {
        "provider_reference": "ALTER TABLE payments ADD COLUMN provider_reference TEXT",
        "phone_number": "ALTER TABLE payments ADD COLUMN phone_number TEXT",
        "status": "ALTER TABLE payments ADD COLUMN status TEXT NOT NULL DEFAULT 'success'",
        "initiated_at": "ALTER TABLE payments ADD COLUMN initiated_at TEXT",
        "updated_at": "ALTER TABLE payments ADD COLUMN updated_at TEXT",
        "receipt_number": "ALTER TABLE payments ADD COLUMN receipt_number TEXT",
    }
    for column, statement in migrations.items():
        if column not in payment_columns:
            conn.execute(statement)
    conn.execute(
        "UPDATE payments SET receipt_number = 'SP-' || upper(hex(randomblob(8))) "
        "WHERE status = 'success' AND receipt_number IS NULL"
    )
    conn.commit()

    existing = conn.execute("SELECT COUNT(*) AS n FROM parking_slots").fetchone()["n"]
    if existing == 0:
        slot_number = 1
        for vtype, count in LOT_LAYOUT.items():
            zone = ZONE_OF_TYPE[vtype]
            for _ in range(count):
                conn.execute(
                    "INSERT INTO parking_slots (slot_number, zone, vehicle_type, status) "
                    "VALUES (?, ?, ?, 'available')",
                    (slot_number, zone, vtype),
                )
                slot_number += 1
        conn.commit()
    conn.close()
