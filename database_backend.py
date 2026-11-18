"""Explicit runtime database selection for the staged PostgreSQL migration.

The application is still SQLite-only: PostgreSQL support in postgres_database.py
is a tested connection prototype, not yet a drop-in runtime backend. Refuse an
explicit PostgreSQL runtime request rather than silently opening nexus.db.
"""
import os


SUPPORTED_RUNTIME_BACKENDS = frozenset({"sqlite"})


def configured_database_backend(environ=None):
    """Return the requested backend, defaulting to the existing SQLite runtime."""
    source = os.environ if environ is None else environ
    value = str(source.get("NEXUS_DB_BACKEND", "sqlite") or "sqlite").strip().lower()
    aliases = {"postgres": "postgresql", "postgresql": "postgresql", "sqlite": "sqlite"}
    backend = aliases.get(value)
    if backend is None:
        raise ValueError("NEXUS_DB_BACKEND must be either 'sqlite' or 'postgresql'.")
    return backend


def require_supported_runtime_backend(environ=None):
    """Fail closed until all application modules support the selected backend."""
    backend = configured_database_backend(environ)
    if backend not in SUPPORTED_RUNTIME_BACKENDS:
        raise RuntimeError(
            "PostgreSQL runtime is not enabled yet: application schema initialization "
            "and remaining SQLite-specific SQL must be migrated and verified first. "
            "PostgreSQL backup/restore support is available but does not enable runtime by itself. "
            "SQLite remains the active supported runtime."
        )
    return backend
