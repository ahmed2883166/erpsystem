# PostgreSQL backup and restore on Windows

The NEXUS backup destination is stored in PostgreSQL and can be changed only by an authenticated Django superuser through the Backup workspace. Company administrators and ordinary users have read-only access. The selected folder must already exist on a drive other than C:.

## Initialize unattended encryption (required)

Before enabling scheduled backups, initialize the machine-bound encryption key once from an elevated PowerShell window:

```powershell
cd nexus_next
.\\scripts\\initialize-backup-encryption.ps1 -RecoveryFile E:\\NEXUS_Backup_Recovery.nexuskey
```

The command prompts twice for a recovery passphrase; it is never supplied on a command line or stored in `.env`. Windows DPAPI protects a random 256-bit key for this machine. A separate recovery file is encrypted with the passphrase and should be stored offline on USB; keep the passphrase separately. The local DPAPI file is restricted to the current account and SYSTEM. Without the recovery file and passphrase, a lost local key cannot be reconstructed.

To recover onto a replacement Windows installation, copy the recovery file from USB and run:

```powershell
cd nexus_next
.\\backend\\.venv\\Scripts\\python.exe .\\backend\\manage.py restore_backup_encryption_key --recovery-file E:\\NEXUS_Backup_Recovery.nexuskey
```

The recovery command prompts for the passphrase and binds the recovered key to the replacement machine's DPAPI. Do not delete or replace the original USB recovery file.

## Configure destination and schedule

1. Sign in as the system superuser.
2. Open **النسخ الاحتياطي والتصفير**.
3. Select an existing local folder such as `D:\\NEXUS_Backups`, choose daily, weekly, or monthly scheduling, and save.
4. Run `scripts/install-backup-task.ps1` from an elevated PowerShell window to register/update the Windows Task Scheduler job. It verifies the local DPAPI key, grants SYSTEM modify access to the chosen backup folder, and runs the task as SYSTEM so it can run while no user is logged in. Use a locally attached drive; mapped network drives are not available to SYSTEM.

The scheduled task reads the destination from Django's saved settings and the PostgreSQL connection settings from `backend/.env`; it does not accept a destination override. The task requires the Windows PostgreSQL service and its local client tools to remain available.

## Run and verify a backup

From PowerShell:

```powershell
cd nexus_next
.\\scripts\\backup-postgres.ps1
```

The script creates a PostgreSQL custom-format archive, validates it with `pg_restore --list`, encrypts it with AES-256-GCM using the DPAPI-protected machine key, decrypts it to a temporary location to authenticate and compare its SHA-256 digest, then deletes the plaintext source. Daily and monthly archives use the `.dump.nexusenc` extension. Retention runs only on encrypted archives. Existing legacy plaintext `.dump` archives in the daily/monthly folders are validated, encrypted, and their history references updated before a new scheduled backup begins. The original plaintext archive is removed only after authenticated encryption succeeds. If encryption fails, the original is retained and the job reports failure.

## Restore test without overwriting the active database

Restore into a separate database only:

```powershell
.\\scripts\\restore-postgres-to-new-db.ps1 -BackupFile D:\\NEXUS_Backups\\daily\\nexus-YYYYMMDD_HHMMSS.dump.nexusenc
```

The restore script authenticates and decrypts the archive into a temporary file, validates it with `pg_restore --list`, creates a new database name, restores into that separate database, and removes the temporary plaintext file. It refuses to use the active database name and never drops or overwrites an existing database. Validate the restored database with the application's checks before considering any controlled cutover.

## Operational safeguards

- Encrypted backup files contain sensitive accounting and company data. Restrict access to the destination folder and keep another encrypted copy on a separate physical device.
- Keep the USB recovery file offline and the recovery passphrase separately; test recovery periodically on a separate Windows machine.
- An archive listing is not proof of a successful restore. Run and document a restore test on a separate database before production use.
- The scripts never reset, flush, or delete records from the active database.

## Controlled operational reset

The **النسخ الاحتياطي** workspace provides a reset preview and a separate operational-data reset action. The reset is restricted to a Django superuser and requires a successful backup recorded within the previous 24 hours, the superuser's current password, and the exact confirmation phrase `تصفير الحركات` plus a separate browser confirmation. This operation is destructive by design and is not part of normal backup or restore. Do not use it on a company database containing records that must be retained.
