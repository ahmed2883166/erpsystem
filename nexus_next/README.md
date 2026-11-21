# NEXUS ERP — Next Generation

This directory contains the new, independent NEXUS ERP implementation.

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
.\start-local.ps1
```
If no administrator exists, the startup helper opens a separate PowerShell window to create one. The database port is bound to localhost only.

See [docs/architecture.md](docs/architecture.md) and [backend/README.md](backend/README.md) for details.

## Database protection

See [Windows PostgreSQL backup and restore](docs/backup-and-restore.md) for daily/monthly backups and a non-destructive restore test.
