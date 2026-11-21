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
python manage.py makemigrations accounts companies
python manage.py migrate
python manage.py seed_reference_data
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

The setup API is under `/api/setup/`:
- `currencies/`
- `companies/`
- `branches/`
- `fiscal-years/`

Authenticated users can read setup records. Only staff users can create, edit, or delete them. Use Django groups and permissions for role design; do not grant staff access as a substitute for ordinary business roles.

Keep the server bound to localhost until LAN security acceptance is complete. Never connect this project to the legacy database during development.
