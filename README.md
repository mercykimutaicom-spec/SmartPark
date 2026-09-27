# ParkFlow

A modern, web-based automated parking system built for a Kenyan client's
terms of reference: drivers see live slot availability before entry, the
system records vehicles on arrival, calculates the fee automatically on
exit, and the barrier opens once payment clears.

Built for **Data Structures & Algorithms — Task One / Two**, Multimedia
University of Kenya.

## Features

- **Live slot display** — a driver-facing dashboard showing free/occupied
  bays per vehicle type (car, motorcycle, van), refreshing automatically.
- **Vehicle entry** — plate number is recorded, a slot is allocated
  automatically, and the barrier animation opens.
- **Full-lot lockout** — while not a single bay is free, no vehicle can be
  checked in. The entry panel shows *"Parking is currently full, please try
  again later."*, the check-in button is disabled, and `POST /api/entry`
  answers `503` with `{"full": true, "error": "Parking is currently full,
  please try again later."}` and a `Retry-After` header. The refusal happens
  before any write, so a full lot never leaves half-written records, and it
  is decided from the database rather than the in-memory slot cache.
- **Automatic billing on exit** — duration is computed from the recorded
  entry time and mapped to the client's tiered fee schedule:

  | Duration          | Fee (KES) |
  |--------------------|-----------|
  | Up to 30 minutes   | Free      |
  | Up to 2 hours      | 50        |
  | Up to 4 hours      | 100       |
  | Up to 6 hours      | 300       |
  | Over 6 hours       | 500       |

  Kenya's standard VAT rate of 16% is applied to the parking subtotal. The
  checkout screen, payment amount, receipt, and audit exports show the
  subtotal, VAT, and VAT-inclusive total. The VAT rate is editable from the
  management panel.

- **Payment & barrier control** — the barrier only opens once payment is
  confirmed (or immediately, for the free tier).
- **Entry ticket QR** — every check-in issues an HMAC-signed ticket code plus
  a QR image. Scanning it with any phone camera opens a public vehicle-details
  page (plate, allocated slot, check-in time, live status); at the exit panel,
  paste or scan the code to auto-fill the plate.
- **Overstay alerts** — active sessions past `OVERSTAY_HOURS` (default 8) are
  flagged in the activity feed.
- **Sound cues** — soft chimes on successful entry/exit, distinct tone on errors.
- **Recent activity feed** for the attendant.
- **Management rate editor** — the rate schedule and VAT rate are edited
  straight from the dashboard; changes apply to the next checkout.
- **Audit and reporting** — every paid session has a receipt number, payment
  method, QR verification, and Excel/PDF/Word exports. Receipts print the
  transaction detail only: no hash or signature value is ever shown, and the
  receipt QR carries a signed link so receipt urls cannot be enumerated.
- **No sign-in** — the kiosk is open: walk up to it and use it. Sign-in,
  profile viewing and sign-out were removed, so **anyone who can reach the
  app can check vehicles in and out, edit rates and download the audit
  export** (which includes names and phone numbers). Run it on a trusted
  LAN/VPN only — see [DEPLOYMENT.md](DEPLOYMENT.md) →
  "Open-access deployment".

### Removed features

The analytics dashboard and the attendant override controls were removed on
purpose. The `override_events` table and every row it holds are **kept** —
it is audit history, not a feature — and any bay still flagged `maintenance`
by the old override control is returned to service (and audited) on start-up,
because nothing could restore it any more. The `users` and `active_sessions`
tables are likewise kept but are no longer read or written.

## Algorithms (DSA design → code)

| Module | Data structure / algorithm | Where |
|--------|---------------------------|-------|
| Slot allocation | Binary min-heap keyed by walking distance (Dijkstra over the bay grid), tie-break lowest bay | `algorithms.allocate_slot`, `_grid_distance_map` |
| Slot release | Heap push back, O(log n) | `algorithms.release_slot` |
| Capacity guard | `COUNT(*)` of available bays (database is the source of truth, the heap is a cache) | `algorithms.count_available_slots`, `register_entry` |
| Vehicle entry | Hash-indexed plate lookup + visit counter | `algorithms.register_entry` |
| Duration & billing | Tiered rate table with VAT rounding | `algorithms.calculate_fee`, `calculate_totals` |
| Barrier control | **Single-lane FIFO queue** with one worker, serialized open pulses | `algorithms.BarrierQueue` |
| Plate lookup | **Trie** prefix search | `algorithms.PlateTrie`, `search_plates` |
| Overstay detection | Time-window scan over active sessions | `algorithms.count_overstays` |

`algorithms.py` keeps the pseudocode for each module in its docstrings; the
test suite in `tests/test_features.py` asserts the tier boundaries, FIFO
service order, nearest-bay ordering, the full-lot lockout (including a stale
cache), trie results, ticket signing, the removal of the analytics/override
endpoints, and the ParkFlow branding.

## Tech stack

- **Backend:** Python 3 + Flask (gunicorn in production)
- **Database:** PostgreSQL in production (`DATABASE_URL`), SQLite for local
  development and tests
- **Frontend:** hand-written HTML/CSS/JS (no build step), polling a small
  JSON REST API
- **Tests:** `unittest` suites in `tests/`, run on every push by GitHub Actions

## Project structure

```
parkflow/
├── app.py               # Flask routes / REST API
├── algorithms.py         # Core algorithms + data structures, heavily commented
├── db.py                  # Schema (SQLite + PostgreSQL), connection, seeding
├── wsgi.py                # Production entrypoint (gunicorn wsgi:app)
├── Dockerfile             # Container image (gunicorn on $PORT)
├── render.yaml            # Render Blueprint: web + Postgres + disk
├── requirements.txt
├── .github/workflows/     # ci.yml, docker.yml, render-deploy.yml
├── templates/             # Jinja UI (base shell, single-page index, ticket)
├── static/
│   ├── css/style.css
│   ├── js/app.js
│   └── img/               # mark.svg, favicon.svg
├── scripts/backup_database.py
├── tests/                 # test_regression.py, test_features.py
└── parkflow.db            # local SQLite fallback (git-ignored)
```

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:5000**. There is no sign-in — the dashboard is
usable straight away.

The database and a 40-bay lot (24 car / 10 motorcycle / 6 van) are
created automatically on first run.

Fill the lot to see the lockout: occupy every bay and the entry panel
switches to *"Parking is currently full, please try again later."* and
refuses new check-ins until one is released.

## Try it

1. **Entry:** type a plate number (e.g. `KDA 123B`), pick a vehicle type,
   submit — a slot is assigned and the barrier lifts.
2. **Exit:** type the same plate number in the Exit panel. If the stay is
   under 30 minutes the barrier opens immediately (free). Otherwise a fee
   is shown — pick a payment method and confirm to settle it and lift the
   barrier.
3. Watch the **live slot map** update, and the **recent activity** feed
   fill in.
4. Use **Parking rates** to update the billing tiers. Save the schedule before
  processing the next checkout.
5. Use the Recent Activity export actions to download an Excel, PDF, or Word
  audit report.

See [DEPLOYMENT.md](DEPLOYMENT.md) for production credentials, HTTPS callbacks,
database, security, backup, and VAT compliance requirements.

## Design documentation

See the design document (`ParkFlow_Design_Task1.docx`, previously shipped as
`SmartPark_KE_Design_Task1.docx`) for the module breakdown, algorithm
pseudocode, data-structure justification, and the dynamic database (ER)
design that this build implements.
