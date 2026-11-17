# PostgreSQL reporting and backup phase

This phase adds a PostgreSQL-native logical backup path without changing the
existing SQLite runtime or SQLite backup scheduler.

## PostgreSQL backups

- `postgres_backup.py` creates PostgreSQL custom-format dumps using
  `pg_dump`, packages the dump with a NEXUS manifest and SHA-256 checksum,
  and validates the archive table of contents with `pg_restore --list`.
- Credentials are passed to libpq through environment variables, not command
  arguments.
- Restore creates a **new database only**. Existing database names are rejected;
  a failed restore drops only the newly-created target database.
- The module is not wired into Flask startup or the SQLite scheduler. It requires
  PostgreSQL client tools and an explicit PostgreSQL DSN.

## Reporting compatibility

Inventory ageing now uses dialect-specific date-prefix expressions:
SQLite keeps `substr`; PostgreSQL uses `LEFT(CAST(... AS TEXT), 10)`.
A PostgreSQL 16 integration test exercises valuation layers, expiry, and latest
movement dates in an isolated schema.

## Remaining boundary

This does not switch NEXUS runtime to PostgreSQL, schedule PostgreSQL backups,
or prove every report/workflow is PostgreSQL-compatible. No production database
is copied, restored, or modified by CI. SQLite remains the supported runtime.
