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

## Development
See [docs/architecture.md](docs/architecture.md) and [backend/README.md](backend/README.md).
