# PostgreSQL backup and restore on Windows

These scripts operate only on the PostgreSQL container defined by `nexus_next/compose.yaml`. They do not read, change, or import the legacy SQLite application or its data.

## Daily backup

From PowerShell, run:

```powershell
cd nexus_next
.\scripts\backup-postgres.ps1
```

The script:
- creates a PostgreSQL custom-format archive using `pg_dump`;
- checks that PostgreSQL can read the archive with `pg_restore --list`;
- copies it to `nexus_next/backups/daily`;
- makes an additional monthly snapshot on the first day of each month;
- retains daily archives for 30 days and monthly archives for 12 months.

## Schedule daily backups

Run once from PowerShell:

```powershell
.\scripts\install-backup-task.ps1
```

Windows Task Scheduler runs the backup at 10:00 PM for the current Windows user. The user must be signed in, and Docker Desktop and the NEXUS PostgreSQL container must be running. If the machine is asleep, the task is configured to start when available.

## Restore test without overwriting the active database

First restore into a separate database:

```powershell
.\scripts\restore-postgres-to-new-db.ps1 -BackupFile .\backups\daily\nexus-YYYYMMDD_HHMMSS.dump
```

The default target name is `nexus_restore_YYYYMMDD_HHMMSS`. You may supply another new database name with `-TargetDatabase`. The script refuses invalid names and fails if the target database already exists. It never drops or overwrites the active NEXUS database.

After restore, validate the separate database before planning any controlled switch. Do not point the live application at the restored database until its schema, company records, balances, and critical reports have been checked.

## Operational safeguards

- Backups contain sensitive accounting and company information. Restrict Windows folder permissions and use BitLocker or another approved at-rest encryption method for the backup drive.
- Keep at least one copy on a separate protected physical device; a backup on the same disk is not disaster recovery.
- This automation is not considered proven until a restore test has been run successfully on the target Windows machine.
- The current scripts do not send backup files to cloud storage or expose PostgreSQL to the public internet.
