# NEXUS backend

Python 3.12+ and Django 5.2 LTS. PostgreSQL is the only supported application database.

## Local setup (PowerShell)
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Create the PostgreSQL database and user separately.
python manage.py check
python manage.py makemigrations accounts companies accounting
python manage.py migrate
python manage.py seed_reference_data
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
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

Keep the server bound to localhost until LAN security acceptance is complete. Never connect this project to the legacy database during development.
