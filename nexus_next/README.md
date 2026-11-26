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
Install PostgreSQL 16 for Windows, Python 3.12, and Node.js 22. Start the PostgreSQL Windows service, configure `backend/.env`, then run this from the `nexus_next` directory:
```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start-local.ps1
```
If no administrator exists, the startup helper opens a separate PowerShell window to create one. The application connects to the existing PostgreSQL Windows service; it does not install Docker, create or reset a database, or open LAN access. Both application servers are bound to localhost only.

For the Windows trial ZIP, prerequisites, first company setup, and data-safety instructions, see [TRIAL-QUICKSTART.md](TRIAL-QUICKSTART.md). To build a clean ZIP locally, run `.scripts\package-trial.ps1`.

See [docs/architecture.md](docs/architecture.md) and [backend/README.md](backend/README.md) for details.

## Database protection

See [Windows PostgreSQL backup and restore](docs/backup-and-restore.md) for daily/monthly backups and a non-destructive restore test.

## Windows Control Center and owner tools

The portable Windows package includes `Admrail-Control-Center.cmd`. It provides a desktop control panel for starting/stopping the secured HTTPS LAN runner, checking PostgreSQL and service ports, opening the LAN URL, and opening the PIN-protected Owner Toolkit. Database creation, rename, deletion, backup, restore, and PostgreSQL user management remain inside the Owner Toolkit/database manager; the Control Center does not expose destructive database actions directly.

The Control Center only stops the LAN process tree that it started and recorded. It will refuse to start when ports 8000 or 443 are already occupied, and it does not terminate unrelated services. The source-only development workflow remains `start-local.ps1` and is localhost-only.
