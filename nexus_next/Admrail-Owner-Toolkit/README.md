# Admrail Owner Toolkit (vendor-only)

This utility is for the software owner/vendor. It is separate from the customer-facing ERP interface and is not a customer employee tool.

## Install
1. Copy the entire `Admrail-Owner-Toolkit` folder beside the ERP application folder, or inside any subfolder under the ERP application root.
2. Run `Start-Owner-Toolkit.cmd`.
3. On first run, create a private owner PIN of at least 12 characters.

The toolkit searches parent folders for an application root containing `backend\manage.py` and an embedded or virtual-environment Python runtime. It does not require `backend\.env` to create a diagnostics report.

## Functions
- Diagnostics: reports paths, runtime presence, PostgreSQL service/tool availability, and whether `.env` exists. It does not print secrets.
- Encrypted backup: delegates to the application's existing encrypted backup workflow.
- Restore: restores to a separately named NEW database. It does not overwrite the active database.
- Migrations: runs Django checks and displays the migration plan, requires explicit `APPLY`, and first invokes the backup workflow.
- Logs: lists toolkit and application log files.

## Safety boundaries
- Database creation, rename, and deletion are available only through the separate PostgreSQL manager in menu option 6. Rename/deletion require the application to be stopped; deletion requires a verified encrypted backup and exact-name confirmation. The toolkit does not provide a database reset or silently switch the active database.
- The PIN is a local deterrent and PBKDF2 hash, not a substitute for Windows ACLs, signed vendor releases, or administrator account security.
- Backup requires a configured backup destination and initialized offline backup-encryption key.
- Restore and migration functions are not considered production-validated until tested on the target Windows installation with a separate restore database.
- Never distribute private owner signing keys or recovery secrets with this toolkit.


## إدارة قواعد البيانات والمستخدمين
- من القائمة الرئيسية اختر **6) إدارة قواعد البيانات ومستخدمي PostgreSQL** بعد تسجيل دخول مالك البرنامج.
- تفتح أداة إدارة PostgreSQL المرفقة بالبرنامج، وتعرض أسماء قواعد البيانات الموجودة فعليًا على الخادم، مع تمييز قاعدة Admrail النشطة.
- تشمل الأداة إنشاء قاعدة جديدة، تغيير اسم القاعدة النشطة، حذف قاعدة غير نشطة بعد نسخة احتياطية مشفرة وتأكيد الاسم، وعرض قواعد البيانات.
- تتضمن إدارة أدوار PostgreSQL (المستخدمين): عرض الحسابات، إنشاء حساب دخول، تفعيل/إيقاف تسجيل الدخول، وتغيير كلمة المرور أو حذف حساب غير مستخدم بعد فحوصات الحماية.
- كلمة مرور مسؤول PostgreSQL تُطلب داخل الأداة عند فتحها ولا تُحفظ في ملفات البرنامج. لا تُدخلها إلا على جهاز الخادم وبيد مالك البرنامج.
- لا تعرض هذه الوظيفة داخل حسابات موظفي الشركات أو واجهة الويب.
