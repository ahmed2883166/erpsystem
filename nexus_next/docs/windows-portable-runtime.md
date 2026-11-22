# Windows portable runtime packaging

## Scope

The portable package is an intermediate build artifact for the future guided Windows installer. It bundles the official 64-bit Python 3.12 embeddable runtime, backend dependencies, Django source and migrations, and the prebuilt React frontend. The target computer does not need Python, Node.js, npm, Docker, or WSL.

## External prerequisite

PostgreSQL 16 for Windows remains an external prerequisite in this milestone. The package deliberately does not silently install a database server, invent database credentials, or alter PostgreSQL service security. The final guided installer must validate the PostgreSQL service and collect/validate database connection settings without asking the customer to edit .env manually.

## Build and verification

Build the React frontend, then run scripts/package-portable-windows.ps1 from a Windows build machine with Python 3.12 and network access. The script downloads the matching official embeddable runtime, installs the project requirements into its Lib/site-packages folder, and verifies Django, psycopg, Waitress imports and manage.py check inside the embedded runtime.

The Windows CI job builds and uploads this artifact with a SHA-256 checksum. The package launcher binds to 127.0.0.1:8000 only.

## Not yet the final installer

This ZIP is not an end-user setup wizard. It still requires a controlled first-run database configuration and does not register a Windows service or perform a guided, backup-protected upgrade. Those remain explicit release gates. Never point it at a production database until the guided setup, backup/restore rehearsal, upgrade rehearsal, and LAN security checks have passed.
