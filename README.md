# SmartPark KE

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
  a QR image. At the exit panel, paste or scan the code to auto-fill the plate.
- **Attendant overrides** — managers can take a bay out of service (e.g. jammed
  lock) or replay the barrier open signal for an already-paid session. Every
  override is written to the `override_events` audit trail with who/when/why.
  Unpaid vehicles can never bypass the barrier.
- **Overstay alerts** — active sessions past `OVERSTAY_HOURS` (default 8) are
  flagged in the activity feed.
- **Analytics** — revenue by day and by hour, revenue per payment method,
  occupancy/utilization per zone, busiest zone, average stay per zone.
- **Sound cues** — soft chimes on successful entry/exit, distinct tone on errors.
- **Recent activity feed** for the attendant.
- **Management rate editor** — authorized staff can update time limits and
  fees from the dashboard; changes apply to the next checkout.
- **Audit and reporting** — every paid session has a receipt number, payment
  method, electronic signature, QR verification, and Excel/PDF/Word exports.

## Algorithms (DSA design → code)

| Module | Data structure / algorithm | Where |
|--------|---------------------------|-------|
| Slot allocation | Binary min-heap keyed by walking distance (Dijkstra over the bay grid), tie-break lowest bay | `algorithms.allocate_slot`, `_grid_distance_map` |
| Slot release | Heap push back, O(log n) | `algorithms.release_slot` |
| Vehicle entry | Hash-indexed plate lookup + visit counter | `algorithms.register_entry` |
| Duration & billing | Tiered rate table with VAT rounding | `algorithms.calculate_fee`, `calculate_totals` |
| Barrier control | **Single-lane FIFO queue** with one worker, serialized open pulses | `algorithms.BarrierQueue` |
| Plate lookup | **Trie** prefix search | `algorithms.PlateTrie`, `search_plates` |
| Overstay detection | Time-window scan over active sessions | `algorithms.count_overstays` |
| Analytics | Hash-map aggregation over payments/sessions | `algorithms.analytics_summary` |

`algorithms.py` keeps the pseudocode for each module in its docstrings; the
test suite in `tests/test_features.py` asserts the tier boundaries, FIFO
service order, nearest-bay ordering, trie results, ticket signing, override
rules, overstay flags, and analytics output.

## Tech stack

- **Backend:** Python 3 + Flask (gunicorn in production)
- **Database:** PostgreSQL in production (`DATABASE_URL`), SQLite for local
  development and tests
- **Frontend:** hand-written HTML/CSS/JS (no build step), polling a small
  JSON REST API
- **Tests:** `unittest` suites in `tests/`, run on every push by GitHub Actions

## Project structure

```
smartpark/
├── app.py               # Flask routes / REST API
├── algorithms.py         # Core algorithms + data structures, heavily commented
├── db.py                  # Schema (SQLite + PostgreSQL), connection, seeding
├── wsgi.py                # Production entrypoint (gunicorn wsgi:app)
├── Dockerfile             # Container image (gunicorn on $PORT)
├── render.yaml            # Render Blueprint: web + Postgres + disk
├── requirements.txt
├── .github/workflows/     # ci.yml, docker.yml, render-deploy.yml
├── templates/index.html   # Single-page UI
├── static/
│   ├── css/style.css
│   └── js/app.js
├── scripts/backup_database.py
├── tests/                 # test_regression.py, test_features.py
└── smartpark.db           # local SQLite fallback (git-ignored)
```

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:5000**.

The database and a 40-bay lot (24 car / 10 motorcycle / 6 van) are
created automatically on first run.

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

See `SmartPark_KE_Design_Task1.docx` for the module breakdown, algorithm
pseudocode, data-structure justification, and the dynamic database
(ER) design that this build implements.
