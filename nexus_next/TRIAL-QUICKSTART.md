# Admrail ERP v2 ERP — Windows Trial

This is a local development/trial package, not a production installer. It starts the new Django + React application with a dedicated PostgreSQL 16 database. It does not read, import, or modify the legacy Flask/SQLite application or its data.

## Requirements

- Windows 10/11 64-bit
- Docker Desktop (running, Linux containers)
- Python 3.12 with the Windows Python Launcher (`py`)
- Node.js 22 or newer (includes npm)
- Internet access for the first dependency installation

## Start

1. Extract the ZIP to a normal writable folder, for example `D:\Admrail ERP v2-Trial`.
2. Open PowerShell in the extracted `nexus_next` folder.
3. Run:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\start-local.ps1
   ```

4. If the system has no administrator user yet, a separate window asks you to create one.
5. Open `http://127.0.0.1:5173`. The Django admin is at `http://127.0.0.1:8000/admin/`.

The first start downloads PostgreSQL and installs Python/Node dependencies, so it may take several minutes. Keep Docker Desktop running while using the application.

## First company setup

1. Sign in to Django admin with the superuser you created.
2. Create a company and choose Egypt or Saudi Arabia, the base currency, and the activity.
3. Create an active head-office branch and an open fiscal year.
4. Create detailed, active, postable accounts and assign the required account roles.
5. Sign in to the main application using the same browser session.

The trial starts with reference currencies only. It does not load fake accounting transactions or a sample company.

## Database and safety

- PostgreSQL is dedicated to this new application and uses a named Docker volume.
- The database port and both web servers bind to `127.0.0.1` only; the app is not exposed to the LAN or internet.
- The first run creates local-only secrets in ignored `.env` files.
- Do not delete the Docker volume if you want to preserve trial data.
- Do not connect this trial to any legacy SQLite database.
- Do not use trial data as production data. Complete security, backup/restore, and acceptance checks before any shared or production deployment.

## Stop

Close the two application server windows with Ctrl+C. To stop PostgreSQL while keeping trial data, run:

```powershell
docker compose -f compose.yaml stop db
```

To start the database again, rerun `.\start-local.ps1`.
