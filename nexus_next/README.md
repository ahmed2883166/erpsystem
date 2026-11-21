# Admrail ERP v2 ERP — Next Generation

This directory contains the new, independent Admrail ERP v2 ERP implementation.

## Technology
- Backend: Python 3.12+, Django 5.2 LTS, Django REST Framework
- Database: PostgreSQL 16+
- Frontend: React + TypeScript + Vite
- Architecture: modular monolith; API-first; Arabic RTL from the start

## Scope guardrails
- This is a rebuild, not a Flask migration. Existing application code is reference material only.
- No manufacturing execution, production orders, BOMs, or raw-to-finished conversion.
- Tax configuration is limited to Egypt and Saudi Arabia. No direct ETA/ZATCA integration.
- Do not connect this project to legacy databases or copy production data during development.
- Keep LAN deployment private; do not expose the service publicly by default.

## Start locally on Windows
Install Docker Desktop, Python 3.12, and Node.js 22, then run this from the `nexus_next` directory:
```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start-local.ps1
```
If no administrator exists, the startup helper opens a separate PowerShell window to create one. The database port and both application servers are bound to localhost only.

For the Windows trial ZIP, prerequisites, first company setup, and data-safety instructions, see [TRIAL-QUICKSTART.md](TRIAL-QUICKSTART.md). To build a clean ZIP locally, run `.scripts\package-trial.ps1`.

See [docs/architecture.md](docs/architecture.md) and [backend/README.md](backend/README.md) for details.

## Database protection

See [Windows PostgreSQL backup and restore](docs/backup-and-restore.md) for daily/monthly backups and a non-destructive restore test.
