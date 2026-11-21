# NEXUS backend

Python 3.12+ and Django 5.2 LTS. PostgreSQL is the only supported application database.

## Local setup (PowerShell)
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py check
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Create the PostgreSQL database and user separately before migrating. Keep the server bound to localhost until LAN security acceptance is complete.
