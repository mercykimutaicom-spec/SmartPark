# ParkFlow Deployment Checklist

## Open-access deployment (read this first)

ParkFlow has **no sign-in**. Sign-in, profile viewing and sign-out were
removed, so every screen and endpoint — including rate editing and the audit
report export, which contains customer names and phone numbers — is served
to anyone who can reach the app. The kiosk is designed to be walked up to
and used.

That makes the network the only perimeter:

- Bind and expose it on a **trusted LAN or VPN only**. Never publish it to
  the public internet, and do not put it behind a public reverse proxy.
- The development server binds `0.0.0.0`, so it answers on every interface.
  Use the network/firewall as the control, not the bind address alone.
- The `users` and `active_sessions` tables are kept for schema stability and
  historical rows, but nothing reads or writes them; no credential is
  created at start-up any more.

If a lock is ever needed again, it has to be re-implemented in front of the
app (network ACL, VPN, or an authenticating proxy) rather than assumed.

## Required environment variables

Copy `.env.example` to `.env` and set real values. Never commit `.env`.

- `FLASK_SECRET_KEY`: long random value used to sign ticket and receipt links.
- `SQLITE_DB_PATH`: optional local SQLite file (defaults to `parkflow.db`
    next to the application). If only a pre-rebrand `smartpark.db` exists it
    keeps being used and a warning is logged — rename it to finish the
    rebrand, never delete it.
- `PAYHERO_AUTH_TOKEN`, `PAYHERO_CHANNEL_ID`: PayHero credentials.
- `PAYHERO_CALLBACK_URL`: public HTTPS URL ending in `/api/payhero/callback`.
- `PAYPAL_ENV`: `sandbox` for testing or `live` for production.
- `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`: server-side PayPal credentials.
- `PAYPAL_CURRENCY` and `PAYPAL_KES_TO_CURRENCY_RATE`: explicit currency conversion for PayPal, since parking fees are stored in KES.
- `BUSINESS_NAME`, `BUSINESS_ADDRESS`, `KRA_PIN`: receipt and VAT invoice identity.
- `BACKUP_RETENTION_COUNT`: number of most recent backups to retain.
- `OVERSTAY_HOURS`: active sessions longer than this raise an attendant
    overstay alert (default `8`).
- `LOG_LEVEL`: application log level, normally `INFO` in production.

## ParkFlow rebrand checklist

The application, UI, assets, documentation, CI/CD, backups and exported
reports now all carry the ParkFlow name. The following steps are **manual on
purpose** because they touch live infrastructure:

1. **Renamed in code already** — brand strings, `BUSINESS_NAME` default
   (`ParkFlow`), PayPal `brand_name`, receipt/report text, ticket prefix
   (`PF-TK-`, with `SP-TK-` still accepted), receipt prefix (`PF-`),
   payment external references (`PARKFLOW-…`), the `parkflow` logger/service
   name, the SQLite file name and the `parkflow-<timestamp>.backup` names.
2. **Render resources — NOT renamed by this change.** `render.yaml` still
   declares `smartpark-web`, `smartpark-data` and `smartpark-db` on purpose:
   Render treats those as resource identities, so editing them in the
   Blueprint would create a brand-new empty service and database and cut the
   live lot off from its data. To finish the rename safely:
   1. Rename the service, disk and database in the **Render dashboard**
      (`smartpark-web` -> `parkflow-web`, etc.).
   2. Confirm the service still has its `DATABASE_URL`, `FLASK_SECRET_KEY`,
      disk mount and every `sync: false` variable.
   3. Update this repository's `render.yaml` to the new names.
   4. Re-copy the **Deploy Hook URL** and update the GitHub secrets
      `RENDER_DEPLOY_HOOK_URL` and `RENDER_APP_URL` to the new hostname.
3. **GitHub repository** — rename `mercykimutaicom-spec/SmartPark` to
   `ParkFlow`; the GHCR image name follows the repository automatically.
4. **PayHero / PayPal dashboards** — provider-side account names and the
   webhook description still show the old brand until an operator updates
   them. No code change is involved.
5. **Local SQLite** — rename `smartpark.db` to `parkflow.db` (the app keeps
   using the old file, with a warning, until you do). Never delete it.
6. **Verify** — `GET /health/live` returns `"service": "parkflow"`, the UI
   header reads ParkFlow, and `POST /api/entry` returns a `PF-TK-` ticket.

Data already issued is never rewritten: existing receipts keep their
original numbers, existing payments keep their original provider references,
and the `override_events` audit table is preserved in full.

## Render: run by itself (web + Postgres + disk + gated auto-deploy)

`render.yaml` is the Blueprint. `autoDeploy: false` is deliberate: GitHub
pushes do NOT go live until CI + GHCR smoke-test pass and the
`Render Deploy` workflow fires the Deploy Hook.

Setup (once):

1. Push to GitHub `main` including `render.yaml`, `Dockerfile`,
   `.github/workflows/ci.yml`, `docker.yml`, `render-deploy.yml`.
2. Render Dashboard > New > Blueprint > select the repo. Creates
   `smartpark-web` (Docker, `plan: starter` for disk support) and
   `smartpark-db` (Postgres). Note: free Postgres expires after 30 days;
   switch to `basic_256mb` for a permanently running lot.
3. Render > `smartpark-web` > Environment: fill every `sync: false` key
   (`PAYHERO_*`, `PAYPAL_*`, `BUSINESS_ADDRESS`, `KRA_PIN`,
   callback/return URLs). `DATABASE_URL` and `FLASK_SECRET_KEY` are
   wired/generated by the Blueprint.
4. Render > `smartpark-web` > Settings: copy the Deploy Hook URL.
5. GitHub repo > Settings > Secrets and variables > Actions, add:
   - `RENDER_DEPLOY_HOOK_URL`: the hook URL from step 4.
   - `RENDER_APP_URL`: `https://<your-service>.onrender.com` (no slash).
6. After deploy, set provider URLs to the live domain:
   `PAYHERO_CALLBACK_URL=https://<your-service>.onrender.com/api/payhero/callback`,
   `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL` to the same origin.

Live chain: `git push main` -> CI -> Docker/GHCR + smoke-test ->
`Render Deploy` (workflow_run, main + success only) fires hook ->
Render restarts gunicorn -> workflow probes `/health/live` then
`/health/ready` and fails loudly if not ready.

Verify: `curl https://<your-service>.onrender.com/health/ready`.
Free web instances sleep when idle; `plan: starter` (or higher) stays
running continuously.

## Production requirements

1. Render terminates TLS/HTTPS; callbacks must use the public
   `https://<your-service>.onrender.com` origin (see Render section above).
2. `DATABASE_URL` is injected from the Render Postgres instance (currently
   named `smartpark-db`); local dev falls back to SQLite.
3. Back up the database and test restoration before accepting live payments.
4. There is no sign-in: restrict access with the network policy (private
   network / VPN / firewall) — the dashboard has no credential of its own.
5. Rotate provider credentials and `FLASK_SECRET_KEY` through the Render
   dashboard (Environment tab), never source code.
6. Monitor payment callbacks, failed captures, duplicate requests, slot/session errors, and export failures.
7. Confirm the KRA VAT registration details and invoice requirements with the business tax adviser before issuing live tax invoices.

## PostgreSQL example

```env
DATABASE_URL=postgresql://parkflow:strong-password@db.example.com:5432/parkflow?sslmode=require
```

Create the database and role before starting the application. The application
creates its tables and default parking rates on first startup. Existing SQLite
data is not copied automatically; export and validate it before migration.

## Local verification

```powershell
c:/Projects/smartpark/.venv/Scripts/python.exe -m unittest discover -s tests -v
c:/Projects/smartpark/.venv/Scripts/python.exe app.py
```

The local callback URL works for polling-based tests, but external provider callbacks require a public HTTPS address.

## Backups

Run the backup utility from the project directory. It selects PostgreSQL when
`DATABASE_URL` is set and otherwise creates a consistent SQLite backup:

```powershell
c:/Projects/smartpark/.venv/Scripts/python.exe scripts/backup_database.py --keep 14
```

Schedule that command with Windows Task Scheduler or your production job
runner. Copy `backups/` to separate durable storage and periodically restore a
backup into a temporary database. PostgreSQL backups require `pg_dump` to be
installed and available on `PATH`.

## Monitoring

- `GET /health/live` confirms the process is responding.
- `GET /health/ready` checks database connectivity and reports backend/provider
    configuration state without exposing credentials.
- Every response includes `X-Request-ID`; application logs include request ID,
    route, status, and duration for correlation.

Configure the reverse proxy/load balancer to poll `/health/live` for liveness
and `/health/ready` before routing traffic.

## Credential rotation

1. Create replacement PayHero and PayPal credentials in their provider consoles.
2. Update the secret values in the deployment secret store, never source code.
3. Restart or roll the application instances so they load the new environment.
4. Verify `/health/ready`, then run a sandbox/small controlled payment test.
5. Revoke the old provider credentials after the new ones are verified.
6. Rotate `FLASK_SECRET_KEY` only during a planned session invalidation window,
     because existing signed sessions will become invalid.
