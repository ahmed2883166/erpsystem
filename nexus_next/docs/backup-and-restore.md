# PostgreSQL backup and restore on Windows

The NEXUS backup destination is stored in PostgreSQL and can be changed only by an authenticated Django superuser through the Backup workspace. Company administrators and ordinary users have read-only access. The selected folder must already exist on a drive other than C:.

## Configure destination and schedule

1. Sign in as the system superuser.
2. Open **النسخ الاحتياطي والتصفير**.
3. Select an existing folder such as `D:\NEXUS_Backups`, choose daily, weekly, or monthly scheduling, and save.
4. Run `scripts/install-backup-task.ps1` from PowerShell to register/update the Windows Task Scheduler job. The task reads the saved settings; it does not accept a user-supplied destination.

The Windows account running the scheduled task must have write access to the selected folder. The PostgreSQL container and Docker Desktop must be running. The backup runner reads the destination from the Django database and refuses C: or unavailable destinations.

## Run and verify a backup

From PowerShell:

```powershell
cd nexus_next
.\scripts\backup-postgres.ps1
```

The script creates a PostgreSQL custom-format archive, validates it with `pg_restore --list`, writes daily archives to the configured folder, and creates a monthly snapshot on the first day of the month. Retention is read from the saved settings.

## Restore test without overwriting the active database

Restore into a separate database only:

```powershell
.\scripts\restore-postgres-to-new-db.ps1 -BackupFile D:\NEXUS_Backups\daily\nexus-YYYYMMDD_HHMMSS.dump
```

The restore script refuses an existing target database. It never drops or overwrites the active NEXUS database.

## Operational safeguards

- Backup files contain sensitive accounting and company data. Restrict Windows folder permissions and use BitLocker or another approved at-rest encryption method.
- Keep another protected copy on a separate physical device.
- A successful archive listing is not proof of a successful restore. Run and document a restore test on the target Windows machine before production use.


## Controlled operational reset

The **النسخ الاحتياطي** workspace provides a reset preview and an operational-data reset action. The reset is restricted to a Django superuser and requires all of the following:

- A successful backup recorded within the previous 24 hours, with its archive still present and at least 1 KiB in size.
- The superuser's current password.
- The exact confirmation phrase `تصفير الحركات` plus a separate browser confirmation.

The reset removes journal entries and lines, sales and purchase transactions, stock movements and balances, treasury transactions and reconciliations, fixed-asset register/depreciation records, attendance, leave, employee loans, payroll cycles/results, and transaction approval requests.

It deliberately preserves company and branch setup, users and permissions, chart of accounts, cost centers, tax codes, customers, suppliers, product catalog, warehouses and locations, employee master records and HR configuration, system settings, backup history, and immutable audit history. Fixed-asset records are cleared because their acquisition and depreciation journals are cleared too.

All deletions run in one database transaction. If a protected relationship prevents deletion, the transaction is rolled back. The reset is never allowed to run without a recent verified backup.
