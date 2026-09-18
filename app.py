"""
app.py
======
SmartPark KE — Flask application entry point.

Wires together the database (db.py) and the core algorithms
(algorithms.py) behind a small REST API, and serves the single-page
web UI (templates/index.html) that drivers and the attendant use.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000

Only Flask itself is required — the database layer uses Python's
built-in sqlite3 module, so there is nothing else to install.
"""
import os
import uuid
import hashlib
import hmac
import base64
import secrets
import logging
import time
from datetime import timedelta
from functools import wraps
from io import BytesIO
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import requests
import qrcode
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, render_template, send_file, url_for, session as user_session
from docx import Document
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash

from db import DATABASE_URL, get_connection, init_db
import algorithms

app = Flask(__name__)
load_dotenv()
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("smartpark")


@app.before_request
def start_request_timer():
    g.request_id = secrets.token_hex(8)
    g.request_started = time.perf_counter()


@app.after_request
def log_request(response):
    elapsed_ms = (time.perf_counter() - g.get("request_started", time.perf_counter())) * 1000
    response.headers["X-Request-ID"] = g.get("request_id", "unknown")
    logger.info("request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                g.get("request_id", "unknown"), request.method, request.path,
                response.status_code, elapsed_ms)
    return response


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def require_role(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_authenticated_user():
                return jsonify({"ok": False, "error": "Sign in is required for this operation."}), 401
            if roles and user_session.get("role") not in roles:
                return jsonify({"ok": False, "error": "You do not have permission for this operation."}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def session_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_authenticated_user():
    token = user_session.get("session_token")
    user_id = user_session.get("user_id")
    if not token or not user_id:
        return None
    conn = get_connection()
    row = conn.execute(
        "SELECT u.id, u.username, u.role, u.password_hash, u.mfa_secret, u.mfa_enabled "
        "FROM active_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.user_id = ? AND s.revoked = 0 AND s.expires_at > ? AND u.active = 1",
        (session_token_hash(token), user_id, utc_now()),
    ).fetchone()
    if row:
        conn.execute("UPDATE active_sessions SET last_seen = ? WHERE token_hash = ?", (utc_now(), session_token_hash(token)))
        conn.commit()
    conn.close()
    return row


def create_authenticated_session(user, request):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    conn = get_connection()
    conn.execute(
        "INSERT INTO active_sessions (user_id, token_hash, created_at, last_seen, expires_at, user_agent, ip_address) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], session_token_hash(token), now.isoformat(), now.isoformat(),
         (now + timedelta(hours=12)).isoformat(), request.headers.get("User-Agent"), request.remote_addr),
    )
    conn.commit()
    conn.close()
    user_session.clear()
    user_session.update({"user_id": user["id"], "username": user["username"], "role": user["role"], "session_token": token})


def payhero_headers():
    return {
        "Authorization": os.environ.get("PAYHERO_AUTH_TOKEN", ""),
        "Content-Type": "application/json",
    }


# Entry ticket: HMAC-signed code + QR that opens the vehicle-details page.
TICKET_PREFIX = "SP-TK"


def _ticket_secret():
    # Signing key: stable per deployment (FLASK_SECRET_KEY in production).
    return (os.environ.get("FLASK_SECRET_KEY") or "smartpark-local-fallback-key").encode()


def public_base_url():
    """Base URL as the driver's phone sees it, honouring the reverse proxy.

    Render terminates TLS in front of gunicorn, so ``request.url_root`` can
    still report ``http://``. The forwarded scheme is used so a scanned QR
    code opens the public https address instead of a broken one.
    """
    root = request.url_root.rstrip("/")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if forwarded_proto == "https" and root.startswith("http://"):
        root = "https://" + root[len("http://"):]
    return root


def receipt_link_token(session_id):
    """Short HMAC for a receipt link so receipt urls cannot be enumerated.

    The value only ever travels inside the QR image (never printed as text),
    and it grants read access to one receipt.
    """
    return hmac.new(_ticket_secret(), f"receipt.{session_id}".encode(), hashlib.sha256).hexdigest()[:32]


def make_ticket_code(session):
    payload = f"{session['id']}.{session['vehicle']}"
    raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_ticket_secret(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{TICKET_PREFIX}-{raw}-{sig}"


def resolve_ticket_code(code, require_active=True):
    """Verify a signed ticket code and return its session, or (None, error).

    ``require_active`` keeps the exit-panel contract (a ticket already used for
    a completed exit is refused). The public scan page passes False so the
    driver can still open the vehicle details after paying.
    """
    code = (code or "").strip()
    if not code.upper().startswith(f"{TICKET_PREFIX}-"):
        return None, "Not a valid SmartPark ticket code."
    try:
        head, sig = code.rsplit("-", 1)
        if not head.upper().startswith(f"{TICKET_PREFIX}-"):
            return None, "Not a valid SmartPark ticket code."
        encoded = head[len(TICKET_PREFIX) + 1:]
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        session_id, plate = payload.split(".", 1)
    except (ValueError, TypeError):
        return None, "Ticket code is malformed."
    expected = hmac.new(_ticket_secret(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    if not secrets.compare_digest(sig, expected):
        return None, "Ticket signature check failed."
    conn = get_connection()
    session = conn.execute(
        "SELECT ps.*, v.plate_number AS vehicle, v.vehicle_type AS vehicle_type, sl.slot_number "
        "FROM parking_sessions ps "
        "JOIN vehicles v ON v.id = ps.vehicle_id "
        "JOIN parking_slots sl ON sl.id = ps.slot_id WHERE ps.id = ?",
        (int(session_id),),
    ).fetchone()
    conn.close()
    if session is None or session["vehicle"] != plate:
        return None, "Ticket does not match any active vehicle."
    if require_active and session["status"] not in ("active", "awaiting_payment"):
        return None, "This ticket has already been used for a completed exit."
    return dict(session), None


def ticket_qr_data_uri(payload):
    image = qrcode.make(payload)
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")

def payhero_url(path):
    base = os.environ.get("PAYHERO_API_BASE_URL", "https://backend.payhero.co.ke/api/v2").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def payhero_configured():
    return bool(os.environ.get("PAYHERO_AUTH_TOKEN") and os.environ.get("PAYHERO_CHANNEL_ID"))


def initiate_payhero_stk(session, external_reference=None):
    external_reference = external_reference or f"SMARTPARK-{session['id']}-{uuid.uuid4().hex[:10].upper()}"
    payload = {
        "amount": session["fee_charged"],
        "phone_number": session["owner_phone"],
        "channel_id": int(os.environ["PAYHERO_CHANNEL_ID"]),
        "provider": "m-pesa",
        "external_reference": external_reference,
        "customer_name": session["vehicle"],
    }
    callback_url = os.environ.get("PAYHERO_CALLBACK_URL")
    if callback_url:
        payload["callback_url"] = callback_url
    response = requests.post(payhero_url("payments"), headers=payhero_headers(), json=payload, timeout=20)
    body = response.json()
    if response.status_code >= 400 or not body.get("success"):
        raise RuntimeError(body.get("message") or body.get("error") or "PayHero rejected the STK request.")
    return body, external_reference


def record_payment(session, status, reference=None, provider_reference=None, method="mpesa"):
    timestamp = utc_now()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO payments (session_id, amount, method, paid_at, reference, "
            "provider_reference, phone_number, status, initiated_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], session["fee_charged"], method, timestamp, reference,
             provider_reference, session["owner_phone"], status, timestamp, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def update_payment_status(session_id, status, reference=None, provider_reference=None):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE payments SET status = ?, reference = COALESCE(?, reference), "
            "provider_reference = COALESCE(?, provider_reference), updated_at = ? "
            "WHERE session_id = ? AND status IN ('pending', 'initiating')",
            (status, reference, provider_reference, utc_now(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_payment_record(session_id):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM payments WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()
    finally:
        conn.close()


def payhero_status(reference):
    response = requests.get(
        payhero_url("transaction-status"),
        headers=payhero_headers(),
        params={"reference": reference},
        timeout=20,
    )
    body = response.json()
    if response.status_code >= 400:
        raise RuntimeError(body.get("message") or body.get("error") or "Unable to fetch PayHero status.")
    return body


def paypal_base_url():
    return "https://api-m.paypal.com" if os.environ.get("PAYPAL_ENV", "sandbox").lower() == "live" else "https://api-m.sandbox.paypal.com"


def paypal_configured():
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    return bool(client_id and client_secret and not client_id.startswith("REPLACE_WITH"))


def paypal_access_token():
    response = requests.post(
        f"{paypal_base_url()}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    body = response.json()
    if response.status_code >= 400 or not body.get("access_token"):
        raise RuntimeError(body.get("error_description") or "PayPal authentication failed.")
    return body["access_token"]


def paypal_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def paypal_amount(kes_amount):
    rate = Decimal(os.environ.get("PAYPAL_KES_TO_CURRENCY_RATE", "0"))
    if rate <= 0:
        raise RuntimeError("PAYPAL_KES_TO_CURRENCY_RATE must be greater than zero.")
    amount = (Decimal(kes_amount) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise RuntimeError("The converted PayPal amount must be greater than zero.")
    return str(amount)


def create_paypal_order(session):
    token = paypal_access_token()
    currency = os.environ.get("PAYPAL_CURRENCY", "USD").upper()
    if currency == "KES":
        raise RuntimeError("PayPal Checkout requires a supported currency; configure PAYPAL_CURRENCY and its conversion rate.")
    response = requests.post(
        f"{paypal_base_url()}/v2/checkout/orders",
        headers={**paypal_headers(token), "PayPal-Request-Id": f"smartpark-{session['id']}-{uuid.uuid4().hex}"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": f"SMARTPARK-{session['id']}",
                "custom_id": str(session["id"]),
                "description": f"SmartPark parking fee - {session['vehicle']}",
                "amount": {"currency_code": currency, "value": paypal_amount(session["fee_charged"])},
            }],
            "application_context": {
                "brand_name": "SmartPark KE",
                "user_action": "PAY_NOW",
                "return_url": os.environ.get("PAYPAL_RETURN_URL", "http://localhost:5000/"),
                "cancel_url": os.environ.get("PAYPAL_CANCEL_URL", "http://localhost:5000/"),
            },
        },
        timeout=20,
    )
    body = response.json()
    if response.status_code >= 400 or body.get("status") not in ("CREATED", "APPROVED"):
        raise RuntimeError(body.get("message") or body.get("details", [{}])[0].get("description") or "PayPal order creation failed.")
    approval = next((link["href"] for link in body.get("links", []) if link.get("rel") == "approve"), None)
    if not approval:
        raise RuntimeError("PayPal did not return an approval link.")
    return body, approval


def capture_paypal_order(order_id):
    token = paypal_access_token()
    response = requests.post(
        f"{paypal_base_url()}/v2/checkout/orders/{order_id}/capture",
        headers=paypal_headers(token),
        timeout=20,
    )
    body = response.json()
    if response.status_code >= 400 or body.get("status") != "COMPLETED":
        raise RuntimeError(body.get("message") or body.get("details", [{}])[0].get("description") or "PayPal capture failed.")
    capture_id = body.get("purchase_units", [{}])[0].get("payments", {}).get("captures", [{}])[0].get("id")
    return body, capture_id


def paypal_approval_url(order_id):
    host = "www.paypal.com" if os.environ.get("PAYPAL_ENV", "sandbox").lower() == "live" else "www.sandbox.paypal.com"
    return f"https://{host}/checkoutnow?token={order_id}"


def mask_phone(phone):
    """Show only the last 4 digits of a phone number (data minimisation)."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) < 4:
        return "—"
    return f"***{digits[-4:]}"


def get_receipt(session_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT ps.id, ps.entry_time, ps.exit_time, ps.fee_charged, ps.subtotal_amount, "
            "ps.vat_rate, ps.vat_amount, ps.total_amount, "
            "v.plate_number, v.owner_phone, v.vehicle_type, p.id AS payment_id, "
            "p.amount, p.method, p.paid_at, p.reference, p.provider_reference, p.receipt_number "
            "FROM parking_sessions ps JOIN vehicles v ON v.id = ps.vehicle_id "
            "JOIN payments p ON p.session_id = ps.id "
            "WHERE ps.id = ? AND ps.status = 'completed' AND p.status = 'success' "
            "ORDER BY p.id DESC LIMIT 1", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    # No hash or signature is printed: verification is server-side state, and
    # the QR carries a signed link so receipt ids cannot be enumerated.
    verification_url = f"{public_base_url()}/receipt/{row['id']}?t={receipt_link_token(row['id'])}"
    qr_image = qrcode.make(verification_url)
    qr_output = BytesIO()
    qr_image.save(qr_output, format="PNG")
    qr_code = "data:image/png;base64," + base64.b64encode(qr_output.getvalue()).decode("ascii")
    return {
        "business_name": os.environ.get("BUSINESS_NAME", "SmartPark KE"),
        "business_address": os.environ.get("BUSINESS_ADDRESS", ""),
        "kra_pin": os.environ.get("KRA_PIN", ""),
        "receipt_number": row["receipt_number"],
        "registration_number": row["plate_number"],
        "phone_number": mask_phone(row["owner_phone"]),
        "vehicle_type": row["vehicle_type"].capitalize(),
        "checkin": row["entry_time"],
        "checkout": row["exit_time"],
        "date_time": row["paid_at"],
        "amount_paid": row["amount"],
        "subtotal_amount": row["subtotal_amount"] if row["subtotal_amount"] is not None else row["amount"],
        "vat_rate": row["vat_rate"] or 0,
        "vat_amount": row["vat_amount"] or 0,
        "total_amount": row["total_amount"] or row["amount"],
        "currency": "KES",
        "payment_method": row["method"].upper(),
        "reference": row["provider_reference"] or row["reference"],
        "verified": True,
        "verification_url": verification_url,
        "qr_code": qr_code,
    }


def report_rows():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT ps.id, v.plate_number, v.owner_phone, v.vehicle_type, sl.slot_number, "
            "ps.entry_time, ps.exit_time, p.paid_at, ps.fee_charged, ps.subtotal_amount, "
            "ps.vat_rate, ps.vat_amount, ps.total_amount, ps.status AS session_status, "
            "p.method, p.status AS payment_status, p.id AS payment_id "
            ", p.receipt_number "
            "FROM parking_sessions ps JOIN vehicles v ON v.id = ps.vehicle_id "
            "JOIN parking_slots sl ON sl.id = ps.slot_id "
            "LEFT JOIN payments p ON p.id = ("
            "SELECT p2.id FROM payments p2 WHERE p2.session_id = ps.id ORDER BY p2.id DESC LIMIT 1) "
            "ORDER BY ps.entry_time DESC"
        ).fetchall()
    finally:
        conn.close()
    report = []
    for row in rows:
        paid = row["session_status"] == "completed" and row["payment_status"] == "success"
        report.append({
            "Receipt No.": row["receipt_number"] if paid else "—",
            "Registration No.": row["plate_number"],
            "Phone Number": row["owner_phone"] or "—",
            "Vehicle Type": row["vehicle_type"].capitalize(),
            "Slot": row["slot_number"],
            "Check-in": row["entry_time"] or "—",
            "Checkout": row["exit_time"] or "—",
            "Payment Date/Time": row["paid_at"] if paid else "—",
            "Method of Payment": row["method"].upper() if paid else "—",
            "Amount Paid (KES)": row["fee_charged"] if paid else "—",
            "Subtotal (KES)": row["subtotal_amount"] if paid else "—",
            "VAT Rate": f"{row['vat_rate'] or 0:g}%" if paid else "—",
            "VAT (KES)": row["vat_amount"] if paid else "—",
            "Total (KES)": row["total_amount"] if paid else "—",
            "Status": row["session_status"],
        })
    return report

# Create/seed the schema on first run, then warm the slot heap from the DB.
init_db()
algorithms.load_heaps_from_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health/live")
def health_live():
    return jsonify({"status": "ok", "service": "smartpark", "time": utc_now()})


@app.route("/health/ready")
def health_ready():
    checks = {"database": "ok"}
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception:
        logger.exception("Readiness database check failed")
        checks["database"] = "failed"
    checks["database_backend"] = "postgresql" if DATABASE_URL else "sqlite"
    checks["payhero_configured"] = payhero_configured()
    checks["paypal_configured"] = paypal_configured()
    ready = checks["database"] == "ok"
    return jsonify({"status": "ready" if ready else "not_ready", "checks": checks}), 200 if ready else 503


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    conn = get_connection()
    user = conn.execute(
        "SELECT id, username, password_hash, role, mfa_secret, mfa_enabled FROM users WHERE username = ? AND active = 1",
        (username,),
    ).fetchone()
    conn.close()
    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "error": "Invalid sign-in details."}), 401
    # No MFA: username + password only (legacy mfa_* columns are never read).
    create_authenticated_session(user, request)
    return jsonify({"ok": True, "user": {"username": user["username"], "role": user["role"], "mfa_enabled": False}})


@app.route("/api/auth/me")
def auth_me():
    user = current_authenticated_user()
    return jsonify({"authenticated": bool(user),
                    "user": {"username": user["username"], "role": user["role"], "mfa_enabled": False}
                    if user else None})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = user_session.get("session_token")
    if token:
        conn = get_connection()
        conn.execute("UPDATE active_sessions SET revoked = 1 WHERE token_hash = ?", (session_token_hash(token),))
        conn.commit()
        conn.close()
    user_session.clear()
    return jsonify({"ok": True})


@app.route("/api/profile", methods=["GET"])
@require_role()
def profile():
    user = current_authenticated_user()
    conn = get_connection()
    sessions = conn.execute(
        "SELECT id, created_at, last_seen, expires_at, user_agent, ip_address, revoked, token_hash "
        "FROM active_sessions WHERE user_id = ? ORDER BY last_seen DESC", (user["id"],)
    ).fetchall()
    conn.close()
    current_hash = session_token_hash(user_session.get("session_token", ""))
    visible = []
    for row in sessions:
        item = dict(row)
        item["current"] = (item.pop("token_hash", None) == current_hash)
        visible.append(item)
    return jsonify({"user": {"username": user["username"], "role": user["role"], "mfa_enabled": False},
                    "sessions": visible})


@app.route("/api/profile", methods=["PUT"])
@require_role()
def update_profile():
    user = current_authenticated_user()
    data = request.get_json(force=True)
    if not check_password_hash(user["password_hash"], str(data.get("current_password") or "")):
        return jsonify({"ok": False, "error": "Current password is incorrect."}), 400
    new_username = str(data.get("username") or user["username"]).strip()
    new_password = str(data.get("new_password") or "")
    if len(new_username) < 3:
        return jsonify({"ok": False, "error": "Username must be at least 3 characters."}), 400
    conn = get_connection()
    try:
        if new_username != user["username"]:
            conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user["id"]))
        if new_password:
            if len(new_password) < 8:
                return jsonify({"ok": False, "error": "New password must be at least 8 characters."}), 400
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"ok": False, "error": "That username is already in use."}), 409
    finally:
        conn.close()
    user_session["username"] = new_username
    return jsonify({"ok": True, "username": new_username})


@app.route("/api/profile/mfa/setup", methods=["POST"])
@require_role()
def setup_mfa():
    # 410 Gone: removed feature, so old clients fail loudly instead of silently.
    return jsonify({"ok": False, "error": "Two-factor authentication has been removed. Sign in with username and password."}), 410


@app.route("/api/profile/mfa/enable", methods=["POST"])
@require_role()
def enable_mfa():
    return jsonify({"ok": False, "error": "Two-factor authentication has been removed. Sign in with username and password."}), 410


@app.route("/api/profile/mfa/disable", methods=["POST"])
@require_role()
def disable_mfa():
    return jsonify({"ok": False, "error": "Two-factor authentication has been removed. Sign in with username and password."}), 410


@app.route("/api/profile/sessions/<int:session_id>", methods=["DELETE"])
@require_role()
def revoke_session(session_id):
    user = current_authenticated_user()
    conn = get_connection()
    try:
        target = conn.execute(
            "SELECT id, token_hash FROM active_sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])
        ).fetchone()
        if target is None:
            return jsonify({"ok": False, "error": "Session not found."}), 404
        if target["token_hash"] == session_token_hash(user_session.get("session_token", "")):
            return jsonify({"ok": False, "error": "You cannot revoke the session you are currently using. Sign out instead."}), 400
        conn.execute("UPDATE active_sessions SET revoked = 1 WHERE id = ? AND user_id = ?", (session_id, user["id"]))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


# Display module: live slot availability (polled by the UI).
@app.route("/api/slots")
def api_slots():
    return jsonify({
        "slots": algorithms.list_slots(),
        "stats": algorithms.get_dashboard_stats(),
    })


@app.route("/api/rates", methods=["GET"])
def api_rates():
    return jsonify({"rates": algorithms.get_parking_rates(), "vat_rate": algorithms.get_vat_rate()})


@app.route("/api/rates", methods=["PUT"])
@require_role("manager")
def api_update_rates():
    data = request.get_json(force=True)
    submitted = data.get("rates")
    if not isinstance(submitted, list) or not submitted:
        return jsonify({"ok": False, "error": "At least one parking rate is required."}), 400
    try:
        rates = sorted(
            [{"max_minutes": int(item["max_minutes"]), "fee_amount": int(item["fee_amount"])} for item in submitted],
            key=lambda item: item["max_minutes"],
        )
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "Each rate needs a valid time limit and fee."}), 400
    if any(rate["max_minutes"] <= 0 or rate["fee_amount"] < 0 for rate in rates):
        return jsonify({"ok": False, "error": "Time limits must be positive and fees cannot be negative."}), 400
    if any(first["max_minutes"] == second["max_minutes"] for first, second in zip(rates, rates[1:])):
        return jsonify({"ok": False, "error": "Each rate must have a unique time limit."}), 400
    if rates[-1]["max_minutes"] < 2147483647:
        return jsonify({"ok": False, "error": "The final rate must cover stays longer than six hours."}), 400
    try:
        vat_rate = float(data.get("vat_rate", algorithms.get_vat_rate()))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "VAT rate must be a valid percentage."}), 400
    if vat_rate < 0 or vat_rate > 100:
        return jsonify({"ok": False, "error": "VAT rate must be between 0 and 100 percent."}), 400
    conn = get_connection()
    try:
        now = utc_now()
        conn.execute("DELETE FROM parking_rates")
        conn.executemany(
            "INSERT INTO parking_rates (max_minutes, fee_amount, updated_at) VALUES (?, ?, ?)",
            [(rate["max_minutes"], rate["fee_amount"], now) for rate in rates],
        )
        conn.execute(
            "INSERT INTO tax_settings (id, vat_rate, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET vat_rate = excluded.vat_rate, updated_at = excluded.updated_at",
            (vat_rate, now),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "rates": algorithms.get_parking_rates(), "vat_rate": vat_rate})


# Vehicle entry module.
@app.route("/api/entry", methods=["POST"])
def api_entry():
    data = request.get_json(force=True)
    plate = data.get("plate_number", "")
    vtype = data.get("vehicle_type", "car")
    phone = str(data.get("owner_phone") or "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Phone number is required for payment."}), 400

    session, error = algorithms.register_entry(plate, vtype, phone)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    ticket_code = make_ticket_code(session)
    # Encode the page url, not the bare code, so a phone camera opens details.
    ticket_url = f"{public_base_url()}{url_for('ticket_page', code=ticket_code)}"
    barrier = algorithms.barrier_queue.request(
        {"id": session["id"], "status": "completed", "slot_number": session["slot_number"]}
    )
    return jsonify({
        "ok": True,
        "message": f"Welcome {session['vehicle']}! Proceed to slot {session['slot_number']}.",
        "session": session,
        "ticket_code": ticket_code,
        "ticket_url": ticket_url,
        "ticket_qr": ticket_qr_data_uri(ticket_url),
        "barrier": {"opened": True, "slot": session["slot_number"]},
    })


@app.route("/api/ticket/<code>")
def api_ticket_resolve(code):
    """Scan/paste a signed entry ticket -> plate + slot for the exit panel."""
    session, error = resolve_ticket_code(code)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({
        "ok": True,
        "session_id": session["id"],
        "plate_number": session["vehicle"],
        "slot_number": session["slot_number"],
        "entry_time": session["entry_time"],
    })


# Public scan pages opened by the QR codes. No login: the ticket is HMAC-signed
# and the receipt link carries its own token, so neither url can be guessed.
def public_page_context(mode, session=None, receipt=None, error=None):
    business_name = (receipt or {}).get("business_name") or os.environ.get("BUSINESS_NAME", "SmartPark KE")
    return {
        "mode": mode,
        "session": session,
        "receipt": receipt,
        "error": error,
        "business_name": business_name,
    }


@app.template_filter("stamp")
def format_stamp(value):
    """Render an ISO timestamp for the public scan pages (scan-friendly text)."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.strftime("%d %b %Y, %H:%M UTC")


@app.route("/ticket/<code>")
def ticket_page(code):
    """Vehicle details page opened by scanning the entry ticket QR."""
    session, error = resolve_ticket_code(code, require_active=False)
    if error:
        return render_template("ticket.html", **public_page_context("ticket", error=error)), 404
    receipt = get_receipt(session["id"]) if session["status"] == "completed" else None
    return render_template("ticket.html", **public_page_context("ticket", session=session, receipt=receipt))


@app.route("/receipt/<int:session_id>")
def receipt_page(session_id):
    """Receipt page opened by scanning the receipt QR (signed link required)."""
    token = request.args.get("t", "")
    if not token or not secrets.compare_digest(token, receipt_link_token(session_id)):
        return render_template(
            "ticket.html",
            **public_page_context("receipt", error="This receipt link is invalid. Please ask the attendant for a printed receipt."),
        ), 403
    receipt = get_receipt(session_id)
    if receipt is None:
        return render_template(
            "ticket.html",
            **public_page_context("receipt", error="Receipt is not available until payment is successful."),
        ), 404
    return render_template("ticket.html", **public_page_context("receipt", receipt=receipt))


# Vehicle exit + billing module.
@app.route("/api/exit", methods=["POST"])
def api_exit():
    data = request.get_json(force=True)
    plate = data.get("plate_number", "")

    session, error = algorithms.process_exit(plate)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    if session["status"] == "completed":
        # Free tier (<=30 min): open the barrier now, through the FIFO queue.
        barrier = algorithms.barrier_queue.request(session)
        return jsonify({
            "ok": True, "payment_required": False,
            "session": session, "barrier": barrier,
        })

    return jsonify({
        "ok": True, "payment_required": True,
        "session": session,
    })


# Payment module.
@app.route("/api/pay", methods=["POST"])
def api_pay():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    method = data.get("method", "mpesa")
    reference = data.get("reference")

    if method == "mpesa":
        if not payhero_configured():
            return jsonify({"ok": False, "error": "PayHero is not configured. Add credentials to .env."}), 503
        conn = get_connection()
        session = conn.execute(
            "SELECT ps.*, v.plate_number AS vehicle, v.owner_phone "
            "FROM parking_sessions ps JOIN vehicles v ON v.id = ps.vehicle_id "
            "WHERE ps.id = ? AND ps.status = 'awaiting_payment'", (session_id,)
        ).fetchone()
        conn.close()
        if session is None:
            return jsonify({"ok": False, "error": "No pending payment for this session."}), 400
        session = dict(session)
        existing = get_payment_record(session_id)
        if existing and existing["method"] == "mpesa" and existing["status"] == "pending":
            return jsonify({
                "ok": True, "session": session,
                "payment": {"type": "stk_push", "status": "pending", "phone": session["owner_phone"],
                            "reference": existing["provider_reference"], "message": "A payment request is already pending."},
            })
        external_reference = f"SMARTPARK-{session_id}-{uuid.uuid4().hex[:10].upper()}"
        try:
            body, _ = initiate_payhero_stk(session, external_reference)
            record_payment(session, "pending", external_reference, body.get("reference"), "mpesa")
        except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
            record_payment(session, "failed", external_reference, method="mpesa")
            return jsonify({"ok": False, "error": f"STK push could not be started: {exc}"}), 502
        return jsonify({
            "ok": True,
            "session": session,
            "payment": {"type": "stk_push", "status": "pending", "phone": session["owner_phone"],
                        "reference": body.get("reference"), "message": "STK push sent. Waiting for payment."},
        })

    if method == "card":
        if not paypal_configured():
            app.logger.warning("Card payment service is unavailable because its provider configuration is incomplete.")
            return jsonify({
                "ok": False,
                "error": "Card payment is temporarily unavailable. Please choose another payment method or try again later.",
            }), 503
        conn = get_connection()
        session = conn.execute(
            "SELECT ps.*, v.plate_number AS vehicle, v.owner_phone "
            "FROM parking_sessions ps JOIN vehicles v ON v.id = ps.vehicle_id "
            "WHERE ps.id = ? AND ps.status = 'awaiting_payment'", (session_id,)
        ).fetchone()
        conn.close()
        if session is None:
            return jsonify({"ok": False, "error": "No pending payment for this session."}), 400
        session = dict(session)
        existing = get_payment_record(session_id)
        if existing and existing["method"] == "paypal" and existing["status"] == "pending":
            return jsonify({
                "ok": True, "session": session,
                "payment": {"type": "paypal", "status": "pending", "order_id": existing["provider_reference"],
                            "approval_url": paypal_approval_url(existing["provider_reference"]),
                            "currency": os.environ.get("PAYPAL_CURRENCY", "USD").upper()},
            })
        try:
            body, approval_url = create_paypal_order(session)
            record_payment(session, "pending", f"PAYPAL-{session_id}", body["id"], "paypal")
        except (requests.RequestException, ValueError, RuntimeError, KeyError, IndexError) as exc:
            record_payment(session, "failed", f"PAYPAL-{session_id}", method="paypal")
            return jsonify({"ok": False, "error": f"PayPal checkout could not be started: {exc}"}), 502
        return jsonify({
            "ok": True,
            "session": session,
            "payment": {"type": "paypal", "status": "pending", "order_id": body["id"],
                        "approval_url": approval_url, "currency": os.environ.get("PAYPAL_CURRENCY", "USD").upper(),
                        "message": "Approve the payment in PayPal to continue."},
        })

    session, error = algorithms.settle_payment(session_id, method, reference)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "session": session, "barrier": algorithms.barrier_queue.request(session),
                    "payment": {"status": "success"}, "receipt": get_receipt(session_id)})


@app.route("/api/paypal/capture/<int:session_id>", methods=["POST"])
def api_paypal_capture(session_id):
    payment = get_payment_record(session_id)
    if payment is None or payment["method"] != "paypal" or payment["status"] != "pending":
        return jsonify({"ok": False, "error": "No pending PayPal order for this session."}), 400
    try:
        body, capture_id = capture_paypal_order(payment["provider_reference"])
        session, error = algorithms.settle_payment(session_id, "paypal", capture_id, payment["provider_reference"])
    except (requests.RequestException, ValueError, RuntimeError, KeyError, IndexError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "status": "success", "session": session,
                    "barrier": algorithms.barrier_queue.request(session), "paypal": body,
                    "receipt": get_receipt(session_id)})


@app.route("/api/payment-status/<int:session_id>")
def api_payment_status(session_id):
    payment = get_payment_record(session_id)
    if payment is None:
        return jsonify({"ok": False, "error": "Payment transaction not found."}), 404
    if payment["status"] in ("success", "failed"):
        response = {"ok": True, "status": payment["status"], "reference": payment["provider_reference"]}
        if payment["status"] == "success":
            response["receipt"] = get_receipt(session_id)
        return jsonify(response)
    try:
        body = payhero_status(payment["provider_reference"] or payment["reference"])
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    status = str(body.get("status", "QUEUED")).lower()
    if status == "success":
        session, error = algorithms.settle_payment(session_id, "mpesa", body.get("provider_reference"), body.get("reference"))
        if error:
            return jsonify({"ok": False, "error": error}), 400
        return jsonify({"ok": True, "status": "success", "session": session,
                "barrier": algorithms.barrier_queue.request(session), "receipt": get_receipt(session_id)})
    if status in ("failed", "cancelled"):
        update_payment_status(session_id, "failed", body.get("reference"), body.get("provider_reference"))
        return jsonify({"ok": True, "status": "failed", "message": "Payment was not completed."})
    return jsonify({"ok": True, "status": "pending"})


@app.route("/api/payhero/callback", methods=["POST"])
def api_payhero_callback():
    data = request.get_json(silent=True) or {}
    callback = data.get("response", data)
    external_reference = callback.get("ExternalReference") or callback.get("external_reference")
    if not external_reference:
        return jsonify({"ok": False, "error": "Missing external reference."}), 400
    conn = get_connection()
    payment = conn.execute("SELECT * FROM payments WHERE reference = ?", (external_reference,)).fetchone()
    conn.close()
    if payment is None:
        return jsonify({"ok": False, "error": "Transaction not found."}), 404
    try:
        provider_status = payhero_status(external_reference)
        verified_success = str(provider_status.get("status", "")).lower() == "success"
    except (requests.RequestException, ValueError, RuntimeError):
        verified_success = False
    status = "success" if verified_success and (str(callback.get("Status", "")).lower() == "success" or callback.get("ResultCode") == 0) else "failed"
    if status == "success":
        algorithms.settle_payment(payment["session_id"], "mpesa", callback.get("MpesaReceiptNumber"), external_reference)
    else:
        update_payment_status(payment["session_id"], status, external_reference, callback.get("MpesaReceiptNumber"))
    return jsonify({"ok": True})


@app.route("/api/receipt/<int:session_id>")
def api_receipt(session_id):
    receipt = get_receipt(session_id)
    if receipt is None:
        return jsonify({"ok": False, "error": "Receipt is not available until payment is successful."}), 404
    return jsonify({"ok": True, "receipt": receipt})


@app.route("/api/reports/export")
@require_role("manager", "attendant")
def export_report():
    report_format = request.args.get("format", "xlsx").lower()
    rows = report_rows()
    headers = list(rows[0].keys()) if rows else [
        "Receipt No.", "Registration No.", "Phone Number", "Vehicle Type", "Slot",
        "Check-in", "Checkout", "Payment Date/Time", "Method of Payment", "Amount Paid (KES)", "Status",
    ]
    generated_at = utc_now()

    if report_format == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Recent Activity"
        sheet.append(["SmartPark KE Payment and Activity Report"])
        sheet.append([f"Generated: {generated_at}"])
        sheet.append([])
        sheet.append(headers)
        for row in rows:
            sheet.append([row[header] for header in headers])
        for cell in sheet[4]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="172231")
        sheet.freeze_panes = "A5"
        sheet.auto_filter.ref = f"A4:K{max(4, sheet.max_row)}"
        for column in sheet.columns:
            width = min(max(len(str(cell.value or "")) for cell in column) + 2, 30)
            sheet.column_dimensions[column[0].column_letter].width = width
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="smartpark-report.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if report_format == "pdf":
        output = BytesIO()
        document = SimpleDocTemplate(output, pagesize=landscape(letter), leftMargin=0.35 * inch,
                                     rightMargin=0.35 * inch, topMargin=0.35 * inch, bottomMargin=0.35 * inch)
        styles = getSampleStyleSheet()
        content = [Paragraph("SmartPark KE Payment and Activity Report", styles["Title"]),
                   Paragraph(f"Generated: {generated_at}", styles["Normal"]), Spacer(1, 0.18 * inch)]
        table_data = [headers] + [[str(row[header]) for header in headers] for row in rows]
        table = Table(table_data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#172231")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 6.5),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F5F9")]),
        ]))
        content.append(table)
        document.build(content)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="smartpark-report.pdf", mimetype="application/pdf")

    if report_format == "docx":
        document = Document()
        document.add_heading("SmartPark KE Payment and Activity Report", 0)
        document.add_paragraph(f"Generated: {generated_at}")
        table = document.add_table(rows=1, cols=len(headers))
        table.style = "Table Grid"
        for cell, header in zip(table.rows[0].cells, headers):
            cell.text = header
        for row in rows:
            cells = table.add_row().cells
            for cell, header in zip(cells, headers):
                cell.text = str(row[header])
        output = BytesIO()
        document.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="smartpark-report.docx",
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    return jsonify({"ok": False, "error": "Unsupported report format."}), 400


# Recent activity feed (attendant view).
@app.route("/api/activity")
def api_activity():
    sessions = algorithms.list_recent_activity(limit=1000)
    return jsonify({"sessions": sessions, "overstays": sum(1 for s in sessions if s.get("overstay"))})


# Attendant tools: trie plate search, overrides, analytics.
@app.route("/api/plates")
@require_role()
def api_plate_search():
    prefix = request.args.get("prefix", "")
    if len(prefix) < 1:
        return jsonify({"ok": False, "error": "Provide a plate prefix."}), 400
    matches = algorithms.search_plates(prefix, limit=10)
    conn = get_connection()
    live = {}
    if matches:
        placeholders = ",".join("?" for _ in matches)
        rows = conn.execute(
            "SELECT v.plate_number, sl.slot_number, ps.status FROM vehicles v "
            "LEFT JOIN parking_sessions ps ON ps.vehicle_id = v.id AND ps.status = 'active' "
            "LEFT JOIN parking_slots sl ON sl.id = ps.slot_id "
            f"WHERE v.plate_number IN ({placeholders})",
            matches,
        ).fetchall()
        for row in rows:
            live[row["plate_number"]] = {"slot_number": row["slot_number"], "status": row["status"]}
    conn.close()
    return jsonify({"ok": True, "matches": [
        {"plate_number": plate, **(live.get(plate) or {"slot_number": None, "status": None})}
        for plate in matches
    ]})


@app.route("/api/slots/<int:slot_number>/maintenance", methods=["POST"])
@require_role("manager")
def api_slot_maintenance(slot_number):
    data = request.get_json(force=True) or {}
    out_of_service = bool(data.get("out_of_service"))
    reason = str(data.get("reason") or "").strip()
    if not reason:
        return jsonify({"ok": False, "error": "A reason is required for the audit trail."}), 400
    ok, error = algorithms.set_slot_maintenance(
        slot_number, out_of_service, reason, user_session.get("username") or "unknown"
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "slot_number": slot_number,
                    "out_of_service": out_of_service, "status": "maintenance" if out_of_service else "available"})


@app.route("/api/barrier/override", methods=["POST"])
@require_role("manager")
def api_barrier_override():
    data = request.get_json(force=True) or {}
    try:
        session_id = int(data.get("session_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "session_id is required."}), 400
    reason = str(data.get("reason") or "").strip()
    if not reason:
        return jsonify({"ok": False, "error": "A reason is required for the audit trail."}), 400
    result, error = algorithms.record_barrier_override(
        session_id, reason, user_session.get("username") or "unknown"
    )
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "barrier": result})


@app.route("/api/analytics")
@require_role("manager")
def api_analytics():
    try:
        days = max(1, min(60, int(request.args.get("days", 14))))
    except ValueError:
        days = 14
    return jsonify({"ok": True, "analytics": algorithms.analytics_summary(days=days),
                    "overstays": algorithms.count_overstays()})


if __name__ == "__main__":
    import os
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
