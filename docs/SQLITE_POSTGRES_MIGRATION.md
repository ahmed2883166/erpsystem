# SQLite to PostgreSQL migration runbook

## Current status

The Flask application still uses SQLite at runtime. The PostgreSQL migration
runner and the copy utility are separate tools; neither is called during Flask
startup. This runbook describes a controlled rehearsal, not a production cutover.

## Safety requirements

- Work only on branch `feature/company-setup-currency-tax-activity`.
- Keep an untouched backup of the SQLite database before any rehearsal.
- Use a dedicated, disposable PostgreSQL schema. The copy utility rejects
  `public`, missing/extra tables, missing/extra columns, and non-empty target
  tables.
- Never point the copy utility at a production schema or a company database.
- The SQLite source is opened read-only. The PostgreSQL write is one transaction;
  any error rolls the transaction back.
- The copy utility requires the explicit `--apply` flag. Without it, it refuses
  to connect and copy.
- Foreign-key cycles are rejected. Self-referencing rows are parent-first
  ordered; cycles and oversized self-referencing tables fail closed.

## Rehearsal sequence

1. Create a separate PostgreSQL database/schema from a reviewed, complete
   PostgreSQL schema baseline. The generated baseline is conservative and its
   review report must be examined before use; do not assume it is production
   ready merely because PostgreSQL accepts the DDL.
2. Run the read-only audit:

   ```bash
   python scripts/audit_sqlite_postgres_migration.py \
     --sqlite /path/to/company-backup.sqlite \
     --postgres-dsn "$NEXUS_POSTGRES_DSN" \
     --schema nexus_rehearsal
   ```

3. Continue only if the report says `preflight_passed: true`. Resolve every
   missing/extra table, column mismatch, source integrity error, foreign-key
   violation, or non-empty target before proceeding.
4. Run the explicit copy against that same disposable schema:

   ```bash
   python scripts/copy_sqlite_to_postgres.py \
     --sqlite /path/to/company-backup.sqlite \
     --postgres-dsn "$NEXUS_POSTGRES_DSN" \
     --schema nexus_rehearsal \
     --batch-size 500 \
     --apply
   ```

5. Retain the JSON report and independently reconcile row counts, journal debit
   and credit totals, opening balances, inventory quantities/values, and
   customer/supplier balances against the source before considering a cutover.

## Important limitations

The copy utility currently verifies SQLite integrity and foreign keys, exact
table/column coverage, empty target tables, per-table copied row counts, and
identity sequence positions. It does not yet perform NEXUS-specific accounting
or inventory balance reconciliation, and it does not switch Flask to
PostgreSQL. Do not treat a successful row copy as approval to cut over.
