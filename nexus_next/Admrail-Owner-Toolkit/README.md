# Admrail Owner Toolkit (owner-only)

Admrail Owner Toolkit is a **standalone vendor/owner utility**. It is not embedded in the customer-facing ERP, is not exposed to company employees, and is not required for normal daily ERP use. The owner carries it on a removable drive, performs server/database setup or maintenance, then removes the toolkit.

## Agreed responsibility split

### Owner Toolkit — server and PostgreSQL administration
The owner installs PostgreSQL manually and enters the PostgreSQL administrator password when prompted. The toolkit is responsible for the remaining technical database work:

- Detect PostgreSQL services and trusted command-line tools, then test the supplied administrator credentials.
- Create and administer the Admrail database and a separate least-privilege application login.
- Configure the ERP database connection without overwriting unrelated application settings or printing credentials.
- Run database migrations and post-setup checks only after explicit confirmation and appropriate backup safeguards.
- Diagnose service, connection, storage, backup, and migration status.
- Perform owner-level encrypted backup verification and restore to a separate database for maintenance/recovery.
- Keep an auditable local operation log without recording passwords or secret values.

The toolkit does **not** download or install PostgreSQL. It must not silently overwrite or delete an existing database.

### Admrail ERP — company onboarding and daily operations
The ERP remains responsible for customer-facing business setup and normal operations:

- First-time company profile and company settings.
- Creation of the first/main ERP administrator account.
- Branches and other company master data.
- Routine in-app backup and restore actions exposed to authorized ERP administrators.
- Showing the last backup result and relevant status to authorized users.

Company profile creation and ERP administrator creation must not be duplicated in the owner toolkit. The toolkit prepares the server/database; the ERP onboarding flow creates the company and its first administrator.

## Portable use
1. Install PostgreSQL on the customer server yourself.
2. Copy the toolkit to your removable drive and run `Start-Owner-Toolkit.cmd` as an authorized Windows administrator.
3. Select the customer Admrail application folder when prompted.
4. Create/use the private owner PIN, then provide the PostgreSQL administrator password only when a database operation requires it.
5. Complete the database connection/setup checks and review the final report.
6. Remove the toolkit drive after maintenance. Do not leave owner-only credentials or recovery material on the customer server.

## Current implemented functions
- Owner PIN using PBKDF2 and a local failed-attempt lockout.
- Diagnostics without printing secrets.
- PostgreSQL service/tool discovery.
- Encrypted backup delegated to the ERP's existing backup workflow.
- Restore to a separately named NEW database; the active database is not overwritten.
- Django checks and migration plan, with explicit `APPLY` confirmation and backup-first behavior.
- PostgreSQL database and login-role maintenance through the separate database manager.

## Safety boundaries and remaining validation
- Database rename/deletion requires the application to be stopped; deletion requires a verified encrypted backup and exact-name confirmation.
- Restore is isolated to a new database and verifies public tables and Django migration history. Application login, reports, and accounting balances still require a separate acceptance test before any controlled switch.
- The PIN is a local deterrent, not a substitute for Windows account security, ACLs, signed releases, or private distribution.
- Backup requires a configured destination and initialized offline backup-encryption key.
- Windows static checks are not a substitute for runtime testing on a clean Windows server with a real PostgreSQL installation.
- Do not store customer credentials, database passwords, recovery keys, or private signing keys in source control or in the portable toolkit package.
- The source currently resides in the public ERP repository branch until the owner moves it to a private repository. Do not put secrets or customer data on that branch.
