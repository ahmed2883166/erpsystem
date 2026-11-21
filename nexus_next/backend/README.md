# NEXUS backend

Python 3.12+ and Django 5.2 LTS. PostgreSQL is the only supported application database.

## Local setup (PowerShell)
From the `nexus_next` directory, run:
```powershell
.\start-local.ps1
```
The helper starts the PostgreSQL 16 container bound to `127.0.0.1`, installs backend/frontend dependencies, applies the committed migrations, seeds reference currencies, and starts both development servers. It never connects to the legacy SQLite database.

On first use, create the administrator in the Backend PowerShell window:
```powershell
python manage.py createsuperuser
```

To stop the database container:
```powershell
docker compose -f compose.yaml down
```
To keep local database contents, do not pass `-v`. The named volume is dedicated to NEXUS Next.

Run backend checks from this directory:
```powershell
.\.venv\Scripts\Activate.ps1
python manage.py check
python manage.py makemigrations --check --dry-run accounts companies accounting
python manage.py migrate
python manage.py test
```

The accounting API is under `/api/accounting/`:
- `accounts/` — chart of accounts
- `account-roles/` — semantic account mappings
- `cost-centers/` — cost centers
- `journal-entries/` — journal drafts, posting, and totals

The setup API is under `/api/setup/`:
- `currencies/`
- `companies/`
- `branches/`
- `fiscal-years/`

Authenticated users can read setup records. Only staff users can create, edit, or delete setup records. Accounting write actions use Django model permissions; assign permissions through groups rather than granting staff access to ordinary business users.

## First test company
1. Open `http://127.0.0.1:5173/admin/` and sign in with the superuser created above.
2. In Currencies, confirm EGP or SAR exists (the startup helper seeds EGP, SAR, USD, and EUR).
3. Create a Company and select its base currency, country (Egypt or Saudi Arabia), and activity.
4. Create its head-office Branch and an open FiscalYear with valid start/end dates.
5. Create at least two active, detailed, postable ChartOfAccount records for the company (for example, Cash and Owner's Equity). For group accounts, set `is_group=True` and `allow_posting=False`.
6. Open `http://127.0.0.1:5173/` in the same browser profile. The app uses the Django session established by the admin login.
7. Create a balanced journal draft, review it, save it, open Review from the journal list, and post it. Then open the trial balance.

Use a superuser only for the first local acceptance test. Before shared LAN use, create least-privilege groups and users, and complete the LAN security review.
Keep the server bound to localhost until LAN security acceptance is complete. Never connect this project to the legacy database during development.
