# Windows portable runtime packaging

## Scope
The portable package is a Windows deployment bundle for Admrail ERP v2. It bundles the official 64-bit Python 3.12 embeddable runtime, backend dependencies, Django source and migrations, and the prebuilt React frontend. The target computer does not need Python, Node.js, npm, Docker, or WSL.

The ZIP includes a guided first-run setup wizard (Setup-Admrail.cmd / setup-admrail.ps1). The wizard creates a dedicated application login and a new empty PostgreSQL database, writes the private backend configuration, applies migrations, prepares reference data, and creates the first administrator.

## External prerequisite
PostgreSQL 16 for Windows remains an external prerequisite. The package does not download or install PostgreSQL, create or change its Windows service, or silently reuse an existing admrail_db database or admrail_app login. Install PostgreSQL 16 with Command Line Tools and start its Windows service before running the setup wizard. The wizard refuses to overwrite an existing backend/.env or delete an existing database.

## Build and verification
Build the React frontend, then run scripts/package-portable-windows.ps1 from a Windows build machine with Python 3.12 and network access. The script downloads the matching official embeddable runtime, installs the project requirements into its Lib/site-packages folder, and verifies Django, psycopg, Waitress imports and manage.py check inside the embedded runtime.

The Windows CI job builds and uploads this artifact with a SHA-256 checksum. The default package launcher binds to 127.0.0.1:8000. Optional private-LAN mode uses Caddy for HTTPS on TCP 443 while keeping Waitress bound to localhost.

## Not yet a complete hands-off Windows installer
This ZIP is a portable runtime bundle with a guided first-run database/application setup, not a full Windows installer. It does not install PostgreSQL, register Admrail/Waitress as a Windows service, configure company DNS, distribute the internal TLS root certificate to employee PCs, or perform a guided backup-protected application/database upgrade. Those remain release and deployment tasks.

Before production use, rehearse first-time setup on a clean Windows machine, verify backup and restore, rehearse upgrades on a disposable database copy, and complete LAN security and multi-user performance checks. Never point an unverified package at a production database.
