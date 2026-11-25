# فحص القيم الرقمية قبل نقل PostgreSQL

## الهدف

يحتوي خط أساس NEXUS الحالي على 227 عمودًا من نوع SQLite TEXT مرشحًا لتمثيل مبالغ أو كميات أو أسعار أو معدلات رقمية. لا يجوز تحويلها إلى PostgreSQL NUMERIC اعتمادًا على اسم العمود فقط؛ يجب فحص بيانات المصدر أولًا.

## الأمان

- الأداة `scripts/audit_postgres_numeric_data.py` لا تنفذ أي INSERT أو UPDATE أو DELETE أو DDL.
- تفتح قاعدة SQLite باستخدام `mode=ro` وتفعّل `PRAGMA query_only = ON`.
- لا تستخدم `float` ولا تعيد تنسيق القيم المالية؛ التحليل يتم باستخدام `Decimal`.
- افتراضيًا تقبل قاعدة موجودة داخل مجلد النظام المؤقت فقط. لفحص مصدر آخر يجب تمرير `--allow-non-temp-read-only` صراحةً؛ يظل الاتصال للقراءة فقط.
- يفضّل فحص نسخة احتياطية متسقة من قاعدة الشركة، وعدم نسخ ملف SQLite يدويًا أثناء وجود كتابة نشطة. لا تشغّل أي تجربة نقل على ملف الشركة الأصلي.

## تجهيز مخطط مرجعي

أنشئ لقطة مخطط جديدة من تهيئة نظيفة ومعزولة:

```bash
python scripts/export_initialized_sqlite_schema.py --output /tmp/nexus-initialized-schema.json
```

هذه اللقطة لا تحتوي بيانات الشركة. هي قائمة مرجعية لأسماء الجداول والأعمدة وأنواعها.

## تشغيل الفحص على قاعدة المصدر

على نسخة SQLite متسقة موجودة في مجلد مؤقت:

```bash
python scripts/audit_postgres_numeric_data.py \
  --database /tmp/nexus-company-copy.sqlite \
  --schema /tmp/nexus-initialized-schema.json \
  --output /tmp/nexus-numeric-audit.json
```

إذا كان ملف المصدر في مسار خارج مجلد النظام المؤقت، لا تستخدم الاستثناء إلا بعد التأكد من أنك تقصد فحص هذا الملف:

```bash
python scripts/audit_postgres_numeric_data.py \
  --database /path/to/company.sqlite \
  --schema /tmp/nexus-initialized-schema.json \
  --output /tmp/nexus-numeric-audit.json \
  --allow-non-temp-read-only
```

## تفسير النتيجة

| الحالة | معناها | الإجراء |
|---|---|---|
| `convertible_exact_decimal` | كل القيم غير الفارغة أرقام عشرية صالحة | مرشح لمراجعة اعتماد NUMERIC |
| `invalid_values` | توجد قيم غير رقمية أو NaN/Infinity أو صيغة غير مقبولة | مراجعة السجلات المصدرية؛ لا تحويل تلقائي |
| `empty_values_require_policy` | توجد سلاسل فارغة أو مسافات | تحديد هل تمثل NULL أم صفرًا أم قيمة ناقصة؛ لا تخمين |
| `no_source_data` | لا توجد قيم غير NULL لفحصها | لا يمكن اعتماد النوع من هذا الجدول وحده |
| `schema_mismatch` | العمود مفقود أو نوعه الفعلي مختلف عن لقطة المخطط | مراجعة اختلافات الترحيل والمخطط |
| `postgres_numeric_limit_exceeded` | القيمة تتجاوز حدود PostgreSQL NUMERIC (131,072 خانة صحيحة أو 16,383 خانة عشرية) | مراجعة القيمة وتصميم تمثيل مناسب قبل النقل |

الحقل `safe_for_numeric_migration` لا يصبح `true` إلا إذا اجتازت جميع الحقول المرشحة الفحص، وكانت سلامة SQLite سليمة ولا توجد مخالفات مفاتيح خارجية، ولم تتجاوز القيم حدود PostgreSQL NUMERIC. عينات القيم غير الرقمية لا تُنسخ إلى التقرير؛ يُذكر موضع العينة فقط. هذه النتيجة بوابة فحص بيانات فقط؛ لا تنفذ تحويلًا ولا تعني اعتماد تشغيل PostgreSQL. رمز الخروج `0` يعني اجتياز الفحص، و`2` يعني وجود بند يحتاج مراجعة.

## الاختبارات

تغطي `tests/test_postgres_numeric_data_audit.py` القيم العشرية عالية الدقة، القيم السالبة، الأصفار العشرية، النصوص غير الرقمية، السلاسل الفارغة، الحقول بلا بيانات، اختلاف المخطط، مخالفات المفاتيح الخارجية، وإثبات أن اتصال المصدر لا يسمح بالكتابة.
