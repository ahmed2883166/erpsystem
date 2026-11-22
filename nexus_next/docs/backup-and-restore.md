# PostgreSQL backup and restore on Windows

The NEXUS backup destination is stored in PostgreSQL and can be changed only by an authenticated Django superuser through the Backup workspace. Company administrators and ordinary users have read-only access. The selected folder must already exist on a drive other than C:.

## Configure destination and schedule

1. Sign in as the system superuser.
2. Open **النسخ الاحتياطي والتصفير**.
3. Select an existing folder such as `D:\NEXUS_Backups`, choose daily, weekly, or monthly scheduling, and save.
4. Run `scripts/install-backup-task.ps1` from PowerShell to register/update the Windows Task Scheduler job.

The scheduled task runs under the current Windows account. That account must have write access to the backup folder and permission to connect to the PostgreSQL service. The backup runner reads the destination from Django's saved settings; it does not accept a destination override.

## Run and verify a backup

From PowerShell:

```powershell
cd nexus_next
.\scripts\backup-postgres.ps1
```

The script connects to the native PostgreSQL Windows service using the connection settings in `backend/.env`, creates a PostgreSQL custom-format archive with `pg_dump`, validates it with `pg_restore --list`, writes daily archives to the configured folder, and creates a monthly snapshot on the first day of the month. Retention is read from the saved settings. PostgreSQL client tools (`pg_dump.exe` and `pg_restore.exe`) must be installed with PostgreSQL or available in PATH.

## Restore test without overwriting the active database

Restore into a separate database only:

```powershell
.\scripts\restore-postgres-to-new-db.ps1 -BackupFile D:\NEXUS_Backups\daily\nexus-YYYYMMDD_HHMMSS.dump
```

The restore script refuses to use the active database name and uses `createdb` without dropping any existing database. A failed restore may leave its newly created test database in place for inspection; the active NEXUS database is never overwritten. Validate the restored database with the application's checks before considering any controlled cutover.

## Operational safeguards

- Backup files contain sensitive accounting and company data. Restrict Windows folder permissions and use BitLocker or another approved at-rest encryption method.
- Keep another protected copy on a separate physical device.
- An archive listing is not proof of a successful restore. Run and document a restore test on the target Windows machine before production use.
- The scripts never reset, flush, or delete records from the active database.

## Controlled operational reset

The **النسخ الاحتياطي** workspace provides a reset preview and a separate operational-data reset action. The reset is restricted to a Django superuser and requires a successful backup recorded within the previous 24 hours, the superuser's current password, and the exact confirmation phrase `تصفير الحركات` plus a separate browser confirmation. This operation is destructive by design and is not part of normal backup or restore. Do not use it on a company database containing records that must be retained.
