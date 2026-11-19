"""Explicit runtime database selection for NEXUS ERP.

SQLite remains the default for existing installations. PostgreSQL is an
explicitly selected runtime backend and never falls back to the SQLite file.
Production cutover still requires the separate owner acceptance gates.
"""
import os


SUPPORTED_RUNTIME_BACKENDS = frozenset({"sqlite", "postgresql"})


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
    """Resolve the explicitly selected backend; never silently fall back."""
    backend = configured_database_backend(environ)
    if backend not in SUPPORTED_RUNTIME_BACKENDS:
        raise RuntimeError(
            f"Unsupported NEXUS runtime backend: {backend!r}. "
            "Choose 'sqlite' or 'postgresql'."
        )
    return backend
