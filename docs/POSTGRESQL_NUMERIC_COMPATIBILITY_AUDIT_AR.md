# مراجعة التوافق الحسابي لأنواع PostgreSQL

## النتيجة الحاسمة

مخطط PostgreSQL الحالي هو **خط أساس بنيوي فقط** وليس مخطط تشغيل متوافقًا مع التطبيق بعد. في لقطة المخطط المؤقتة:

- 920 عمودًا معلنًا في SQLite على أنه TEXT.
- 628 عمودًا INTEGER.
- عمودان REAL.
- مولّد PostgreSQL يحافظ على TEXT كـTEXT عمدًا.

هذا يحافظ على النصوص والقيم العشرية المخزنة كسلاسل، لكنه لا يكفي لتشغيل استعلامات الحسابات على PostgreSQL. فـPostgreSQL لا يجمع TEXT باستخدام SUM ولا ينفذ عمليات حسابية بين أعمدة TEXT كما تفعل SQLite بتحويلاتها الضمنية.

## دليل من كود التطبيق

- `accounting_core.register_decimal_aggregates` يسجل `DECIMAL_SUM` و`DECIMAL_NET` و`DECIMAL_LAYER_VALUE_SUM` و`DECIMAL_RUNNING_NET` لقاعدة SQLite فقط؛ وعلى PostgreSQL يعتمد على الأنواع الرقمية الأصلية.
- `tax_center._tax_ledger_reconciliation` يستخدم `DECIMAL_SUM(jl.debit)` و`DECIMAL_SUM(jl.credit)`، لذا يجب أن يكون نوع حقلي دفتر اليومية رقميًا في PostgreSQL أو أن تتم إعادة كتابة الاستعلام/طبقة التوافق.
- حقلا `journal_lines.debit` و`journal_lines.credit` نصيان في SQLite. تركهما TEXT في PostgreSQL مع تعطيل التجميع المخصص سيجعل المطابقة الحسابية غير قابلة للتنفيذ.

هذا يثبت أن الاحتفاظ بكل الحقول المالية TEXT ليس حلًا نهائيًا للتوافق، رغم أنه مناسب لخط أساس لا يُستخدم لتشغيل التطبيق.

## الحقلان REAL في المخطط

| الجدول | الحقل | النوع الحالي | النوع الناتج |
|---|---|---|---|
| currencies | rate_to_base | REAL | DOUBLE PRECISION |
| tax_rates | rate | REAL | DOUBLE PRECISION |

تمت إضافة تحويل صريح لهذين الحقلين إلى PostgreSQL NUMERIC غير المقيد (دون تحديد precision/scale) في مولّد المخطط. هذا يمنع سلوك DOUBLE PRECISION التقريبي، ولا يفرض تقريبًا أو اقتطاعًا أثناء إنشاء المخطط. لكنه ليس اعتمادًا نهائيًا لسياسة الدقة؛ يجب تحديد المنازل العشرية وقواعد التقريب لاحقًا بعد اختبار مسارات الإدخال والحساب والترحيل.

## التحويلات الصريحة المنفذة في مولّد المخطط

الخريطة الحالية محدودة بأربعة حقول ثبتت حاجتها للحساب العددي:

| الجدول | الحقل | نوع PostgreSQL |
|---|---|---|
| journal_lines | debit | NUMERIC |
| journal_lines | credit | NUMERIC |
| currencies | rate_to_base | NUMERIC |
| tax_rates | rate | NUMERIC |

استخدام NUMERIC دون precision/scale مقصود كمرحلة انتقالية: يحافظ على القيمة العشرية دون اختيار مقياس قد يقرّب بيانات قديمة. لا يعني ذلك أن كل الحقول المالية أصبحت جاهزة؛ بقية الحقول تظل على التحويل المحافظ حتى مراجعتها. كما تُترك tax_number وtax_roles_json وrate_date ونحوها نصوصًا، ولا تُحوّل بناءً على اسم العمود وحده.

## مجموعات التحويل التي تحتاج خريطة صريحة

1. **دفتر الأستاذ:** debit, credit وأي أرصدة مدينة/دائنة أو opening/closing balances.
2. **المبيعات والمشتريات:** quantity, unit_price, line_total, cost_total, tax_amount, discount_value.
3. **المخزون والتكلفة:** qty_in, qty_remaining, unit_cost, value_remaining, movement quantities/costs.
4. **الرواتب والتأمينات:** basic_salary, allowances, gross_pay, net_pay, deductions, insured_wage, contribution rates.
5. **العملات:** rate_to_base, original_rate, base_amount, balance_base, settlement/revaluation differences.
6. **الأصول:** cost, depreciation, accumulated depreciation, net book value, proceeds, gain/loss.
7. **الضرائب:** rate, input/output VAT, deductible percentage, taxable amounts.

هذه أسماء مجموعات مراجعة وليست موافقة على دقة واحدة. يلزم اختيار `NUMERIC(p,s)` لكل حقل بحسب أقصى قيمة وعدد المنازل المطلوبة وقواعد العمل الفعلية.

## استثناءات يجب أن تبقى نصوصًا

لا تحوّل الحقول التالية إلى NUMERIC لمجرد احتوائها على كلمات مثل tax أو rate أو value:
- أرقام التسجيل الضريبي والمعرفات والباركود والأكواد.
- التاريخ والوقت وحقول `*_at`.
- JSON وسجلات التدقيق والوصف والملاحظات.
- الحقول التصنيفية مثل `tax_treatment`, `discount_type`, `depreciation_method`, `normal_balance`.
- المراجع التي قد تكون نصوصًا أو أكوادًا.

قائمة المراجعة المستخرجة من artifact النسخة `605c9d3` تحتوي على 227 حقلًا من نوع TEXT في 90 جدولًا؛ وهي قائمة استكشافية وليست خريطة تحويل. تتضمن نتائج موجبة كاذبة مثل `branches.tax_number`, `activity_profiles.tax_roles_json`, `fx_exchange_rates.rate_date`, و`sales_invoice_lines.tax_treatment`.

## بوابات القبول قبل ربط التطبيق بـPostgreSQL

1. اعتماد قاموس أنواع لكل عمود مرشح مع `NUMERIC(p,s)` أو إبقائه TEXT لسبب موثق.
2. فحص كل عمليات SQL التي تعتمد على الجمع والطرح والمقارنة والترتيب والضرب والقسمة.
3. اختبار تحويل بيانات تجريبية من SQLite إلى PostgreSQL مع مقارنة الأرصدة وميزان المراجعة وتفاصيل الأستاذ والمخزون والرواتب والضرائب.
4. اختبار round-trip للأعداد الكبيرة والكسور والأرقام السالبة والصفر والقيم القديمة غير القياسية.
5. اختبار الترحيل العكسي/الاستعادة من نسخة احتياطية قبل أي قرار إنتاجي.
6. تشغيل مجموعة اختبارات التطبيق على PostgreSQL فعليًا، لا الاكتفاء بإنشاء DDL.

## الحالة

لم تتغير أنواع الأعمدة في قاعدة SQLite، ولم يتم تشغيل ترحيل بيانات، ولم يتم توصيل PostgreSQL بتطبيق Flask. هذا التقرير يحدد مانعًا تقنيًا يجب إغلاقه قبل اعتبار مرحلة التوافق جاهزة.
