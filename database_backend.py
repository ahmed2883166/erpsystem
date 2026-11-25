"""Explicit runtime database selection for NEXUS ERP.

Legacy installations keep SQLite as their default. The PostgreSQL-native
deployment profile selects PostgreSQL by default and fails closed when its DSN
or explicit runtime approval is missing.
"""
import os


SUPPORTED_RUNTIME_BACKENDS = frozenset({"sqlite"})


def configured_database_backend(environ=None):
    """Resolve the backend; PostgreSQL-native profile never defaults to SQLite."""
    source = os.environ if environ is None else environ
    profile = str(source.get("NEXUS_DEPLOYMENT_PROFILE", "") or "").strip().lower()
    configured = source.get("NEXUS_DB_BACKEND")
    if profile == "postgresql_native":
        if configured is not None and str(configured).strip().lower() not in {
            "postgres", "postgresql"
        }:
            raise ValueError(
                "NEXUS_DEPLOYMENT_PROFILE=postgresql_native requires "
                "NEXUS_DB_BACKEND=postgresql."
            )
        value = configured or "postgresql"
    else:
        value = configured or "sqlite"
    value = str(value or "").strip().lower()
    aliases = {"postgres": "postgresql", "postgresql": "postgresql", "sqlite": "sqlite"}
    backend = aliases.get(value)
    if backend is None:
        raise ValueError("NEXUS_DB_BACKEND must be either 'sqlite' or 'postgresql'.")
    return backend


def require_supported_runtime_backend(environ=None):
    """Resolve the selected backend; never silently fall back to SQLite."""
    source = os.environ if environ is None else environ
    backend = configured_database_backend(source)
    profile = str(source.get("NEXUS_DEPLOYMENT_PROFILE", "") or "").strip().lower()
    if profile == "postgresql_native" and backend != "postgresql":
        raise RuntimeError("The PostgreSQL-native deployment profile requires PostgreSQL.")
    if backend == "postgresql":
        dsn = str(source.get("NEXUS_POSTGRES_DSN", "") or "").strip()
        if not dsn:
            raise RuntimeError("NEXUS_POSTGRES_DSN is required when PostgreSQL is selected.")
        runtime_enabled = str(
            source.get("NEXUS_POSTGRES_RUNTIME_ENABLE", "") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not runtime_enabled:
            raise RuntimeError(
                "PostgreSQL runtime is not enabled. Set "
                "NEXUS_POSTGRES_RUNTIME_ENABLE=1 only for an explicitly approved "
                "PostgreSQL deployment. No automatic SQLite fallback is performed."
            )
        return backend
    if backend not in SUPPORTED_RUNTIME_BACKENDS:
        raise RuntimeError(
            f"Unsupported NEXUS runtime backend: {backend!r}. Choose 'sqlite'."
        )
    return backend
