# الشريحة 18 — توحيد فحص metadata وبداية المعاملات

## النطاق
- إضافة `transaction_is_active` و`begin_write_transaction` إلى `db_compat.py`.
- SQLite يحتفظ بسلوك `BEGIN IMMEDIATE` عند عدم وجود معاملة نشطة.
- psycopg يبدأ `BEGIN` فقط عندما تكون حالة الاتصال IDLE؛ إذا كانت هناك معاملة بدأت بالفعل باستعلام سابق، لا يحاول فتح معاملة متداخلة.
- استبدال قراءات `PRAGMA table_info` في `cash_commitments.py` باستدعاءات `table_columns` التي تدعم SQLite وPostgreSQL.
- استبدال بدايات المعاملات الصريحة في `banking.py` و`cash_commitments.py` بالمساعد المشترك.
- إضافة اختبار SQLite يثبت عدم محاولة فتح معاملة متداخلة وأن المعاملة القائمة تظل قابلة للإكمال والـcommit.

## حدود مهمة قبل اعتماد PostgreSQL runtime
- هذه الشريحة لا تحول DDL الخاص بتهيئة cash commitments؛ `executescript` وتعريفات SQLite مثل `INTEGER PRIMARY KEY` ما زالت تحتاج شريحة مستقلة.
- `BEGIN IMMEDIATE` في SQLite يحجز حق الكتابة على مستوى قاعدة البيانات. PostgreSQL لا يملك سلوكًا مطابقًا؛ المساعد لا يدعي توفير القفل نفسه. يجب استكمال مراجعة نقاط التزامن بقفل الصفوف المناسبة وقيود قاعدة البيانات واختبارات التنافس قبل اعتماد هذه المسارات على PostgreSQL.
- لم يتم تغيير مصنع اتصال التطبيق أو قاعدة التشغيل، ولم يتم ترحيل أي بيانات.
- يجب تشغيل اختبارات PostgreSQL الفعلية في CI مع `NEXUS_TEST_POSTGRES_DSN`؛ نجاح اختبارات SQLite وحده لا يثبت توافق PostgreSQL.

## الملفات
- `db_compat.py`
- `banking.py`
- `cash_commitments.py`
- `tests/test_db_compat.py`

## حالة التحقق
اختبارات GitHub Actions للشريحة الحالية قيد التنفيذ عند إنشاء هذا التوثيق؛ لا تعتبر الشريحة مكتملة أو جاهزة للتشغيل قبل ظهور conclusion=success على HEAD النهائي.
