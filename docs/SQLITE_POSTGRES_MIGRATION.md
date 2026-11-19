# SQLite to PostgreSQL migration runbook

## Current status

The Flask application still uses SQLite at runtime. The PostgreSQL migration
runner and the copy utility are separate tools; neither is called during Flask
startup. This runbook describes a controlled rehearsal, not a production cutover.

## Safety requirements

- Development and rehearsal changes for this phase are restricted to branch `work/phase7-bootstrap-ux`; do not use `main` or merge without explicit approval.
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

5. Run the independent, read-only post-copy verification against the same
   source backup and isolated target:

   ```bash
   python scripts/verify_sqlite_postgres_copy.py \
     --sqlite /path/to/company-backup.sqlite \
     --postgres-dsn "$NEXUS_POSTGRES_DSN" \
     --schema nexus_rehearsal
   ```

6. Retain both JSON reports. The verifier checks table/column coverage and row
   counts, exact-decimal journal debit/credit totals, customer/supplier debit
   and credit totals by party ID when those columns exist, and inventory
   valuation-layer quantity/value totals overall and by product/warehouse.
   Controls whose required table or columns are unavailable are listed
   separately as skipped. A skipped financial/inventory control now makes
   the verifier return `passed: false` and `requires_review: true`; resolve
   every skip and rerun verification before approval. Any mismatch also blocks
   approval.

## Important limitations

The verifier is a reconciliation gate, not a migration or cutover tool. It does
not yet prove every operational workflow, tax report, opening/closing balance,
or bank statement reconciliation. It does not switch Flask to PostgreSQL.
Do not treat matching row counts and control totals as approval to cut over.

## Production cutover is not yet authorized

The rehearsal test suite creates representative data in temporary databases. It
does not replace a rehearsal against a current, customer-approved backup, and it
does not authorize changing the application's runtime backend.

Before a real cutover, all of the following are mandatory:

- Close the latest Linux and Windows CI runs on the exact branch HEAD; retain
  their run URLs and job conclusions.
- Complete PostgreSQL workflow acceptance against the full application schema
  for payroll posting, bank statement reconciliation, Egypt/Saudi tax
  preparation, inventory movements, and all critical accounting reports.
  Helper-level tests alone are not sufficient.
- Run a read-only preflight against a customer-created backup, then copy only
  into a separately provisioned rehearsal database. Preserve the source file.
- Require a clean post-copy verifier report: zero missing/extra tables or
  columns, zero row-count mismatches, zero skipped required financial or
  inventory controls, zero unbalanced journal groups, and exact matching
  monetary and stock valuation controls.
- Compare trial balance, general ledger, balance sheet, income statement,
  customer/supplier balances, bank balances, VAT summaries, and inventory
  valuation before and after migration. Investigate every difference.
- Complete a real PostgreSQL backup/restore round-trip and verify the restored
  database independently.
- Obtain explicit owner approval for a maintenance window and a rollback
  plan. Keep the original SQLite database untouched and recoverable.
- Only after approval, implement and test a controlled runtime switch. Keep
  SQLite as the rollback source until post-cutover acceptance is signed off.

Never run the rehearsal commands against the live company database or point
them at the production PostgreSQL schema. No customer data is required for
ordinary CI tests.
