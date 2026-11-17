# شريحة توافق PostgreSQL 8 — بيانات البنوك الوصفية

## النطاق
- أضيفت واجهتا `_table_exists` و`_columns` في `banking.py` باستخدام `db_compat`.
- استُبدل فحص أعمدة `bank_statement_lines` و`bank_statement_balances` بقراءة metadata محايدة بين SQLite وPostgreSQL.
- لا تغيير في مطابقة كشف البنك أو أرصدة الافتتاح والإقفال أو القيود أو صلاحيات المستخدمين.

## الحدود
- `db()` ما زالت تتصل بـSQLite عبر `connect_database(DB_PATH)`.
- تهيئة الجداول والفهارس ما زالت تستخدم DDL خاصًا بـSQLite و`executescript`؛ لم يتم تحويلها.
- أوامر SQL التشغيلية ومعاملات التسوية لم تُحوّل، ولا تعني هذه الشريحة أن وحدة البنوك جاهزة للتشغيل على PostgreSQL.
- لا يوجد تحويل لقاعدة بيانات الشركة ولا تغيير لبياناتها.

## الاختبارات
- اختبار SQLite بقاعدة `:memory:`.
- اختبار PostgreSQL اختياري عبر `NEXUS_TEST_POSTGRES_DSN`، داخل schema عشوائية مؤقتة ثم rollback.
