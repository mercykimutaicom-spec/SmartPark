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
- **Recent activity feed** for the attendant.
- **Management rate editor** — authorized staff can update time limits and
  fees from the dashboard; changes apply to the next checkout.
- **Audit and reporting** — every paid session has a receipt number, payment
  method, electronic signature, QR verification, and Excel/PDF/Word exports.

## Tech stack

- **Backend:** Python 3 + Flask
- **Database:** SQLite (via the standard-library `sqlite3` module — no
  extra DB dependency to install)
- **Frontend:** hand-written HTML/CSS/JS (no build step), polling a small
  JSON REST API

## Project structure

```
smartpark/
├── app.py              # Flask routes / REST API
├── algorithms.py        # All core algorithms (see design doc), heavily commented
├── db.py                 # SQLite schema, connection, seeding
├── requirements.txt
├── templates/
│   └── index.html        # Single-page UI
├── static/
│   ├── css/style.css
│   └── js/app.js
└── smartpark.db          # created automatically on first run (git-ignored)
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
