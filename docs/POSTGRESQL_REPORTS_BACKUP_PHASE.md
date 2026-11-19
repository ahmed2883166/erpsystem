# PostgreSQL runtime, reporting, and backup acceptance

## Runtime selection

- SQLite remains the default for existing installations.
- PostgreSQL can be selected explicitly with `NEXUS_DB_BACKEND=postgresql`
  and `NEXUS_POSTGRES_DSN`.
- The PostgreSQL connection factory is exercised through the normal
  `require_supported_runtime_backend()` gate; CI must not monkeypatch that
  gate to simulate runtime support.
- A PostgreSQL schema is isolated through the DSN search path during fresh
  installation. The application must never silently fall back to SQLite.
- Enabling the backend selector is not production cutover approval.

## PostgreSQL reporting compatibility

- Monthly grouping casts text dates to DATE before applying `TO_CHAR`.
- Expense analysis compares `is_group` and `active` using integer-compatible
  predicates matching the generated PostgreSQL baseline.
- The full PostgreSQL smoke test executes the expense analysis and monthly
  budget comparison against posted purchase activity.
- This is targeted regression coverage, not yet a parity certificate for every
  financial, tax, payroll, inventory, ageing, and management report. Full report
  parity still requires a fixed representative dataset and automated
  before/after comparison.

## Concurrency controls

- Customer advance allocation locks the receipt row and all affected invoice
  rows in a deterministic order on PostgreSQL.
- Cash commitment payment and reversal lock the commitment/payment rows.
- Recurring schedule generation locks the schedule row; uniqueness constraints
  remain the final duplicate guard.
- These controls require PostgreSQL integration/concurrency tests before
  production approval.

## PostgreSQL backup and restore

- `postgres_backup.py` creates PostgreSQL custom-format dumps using
  `pg_dump`, packages the dump with a NEXUS manifest and SHA-256 checksum,
  and validates the archive table of contents with `pg_restore --list`.
- Credentials are passed to libpq through environment variables, not command
  arguments.
- Restore creates a **new database only**. Existing database names are rejected;
  a failed restore drops only the newly-created target database.
- Unit/integration tests cover archive creation and restore safety. Production
  acceptance still requires a rehearsal using a real backup of a representative
  company database, full restore into a separate target, and reconciliation
  checks after restore.

## Mandatory production acceptance gates

Do not migrate a live company or declare PostgreSQL production-ready until all
items below are recorded as PASS:

1. Full application workflow acceptance on PostgreSQL, including sales,
   purchasing, receipts/payments, journals, inventory, fixed assets, payroll,
   tax workpapers, and approvals.
2. Real backup rehearsal and full restore into a separate database.
3. Before/after comparison of all required reports on the same fixed dataset,
   including trial balance, general ledger, balance sheet, income statement,
   cash flow, customer/supplier statements, bank reconciliation, tax, payroll,
   inventory valuation/ageing, budgets, and management analytics.
4. Concurrency tests for customer advance allocation and cash commitments.
5. Owner approval of the cutover window and a documented rollback plan.
6. A verified backup immediately before cutover. Keep the original SQLite
   database untouched and available for rollback; do not auto-copy or delete it.

CI uses disposable PostgreSQL schemas/databases only. It does not read, modify,
or restore any real company database.
