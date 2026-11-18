# جرد مخطط قاعدة البيانات — المرحلة الأولى من نقل PostgreSQL

تاريخ الجرد: 17 نوفمبر 2026  
المستودع: `ahmed2883166/erpsystem`  
الفرع: `feature/company-setup-currency-tax-activity`  
نسخة المصدر: `530ffc50cb591ae8b8345af65e1a3a949fa5a4d7`

## 1. منهجية وحدود الجرد

تم فحص ملفات Python الخاصة بالتطبيق التي تحتوي تعريفات `CREATE TABLE` و`ALTER TABLE`، مع استبعاد ملفات الاختبارات من جرد مخطط التشغيل. الجرد ساكن؛ لذلك قد لا يلتقط أسماء الجداول التي تُبنى ديناميكيًا من متغيرات، وقد يلتقط تعبيرات غير مقصودة في نصوص SQL المعقدة. يجب اعتبار القائمة أساسًا للمطابقة مع مخطط SQLite الفعلي، وليست بديلًا عن استخراج `sqlite_master` من نسخة قاعدة فعلية.

**نتيجة مهمة:** إنشاء وتعديل المخطط موزع على ملفات التطبيق والوحدات، وليس محصورًا في ملف ترحيلات مركزي واضح. لذلك لا يصح بناء PostgreSQL schema من قائمة CREATE TABLE وحدها.

## 2. جداول التشغيل التي ظهرت في المصدر

### النواة والمستخدمون والإعدادات
- `roles`, `users`, `accounts`
- `journal_entries`, `journal_lines`
- `audit_log`, `company_settings`, `currencies`, `tax_rates`
- `login_attempts`, `accounting_periods`, `opening_balance_import_refs`
- `document_sequences`

### النشاط والفروع والأبعاد التحليلية
- `activity_profiles`
- `branches`, `cost_centers`, `projects`

### المخزون
- `stock_products`, `stock_categories`, `stock_warehouses`, `stock_locations`
- `stock_product_locations`, `stock_lots`, `stock_movements`
- `stock_valuation_layers`, `stock_warehouse_product_settings`
- `stocktakes`, `stocktake_lines`
- `stock_uom_categories`, `stock_uoms`, `stock_product_uoms`

### المشتريات والموردون
- `suppliers`, `purchase_requests`, `purchase_orders`, `purchase_order_lines`
- `goods_receipts`, `goods_receipt_lines`
- `supplier_bills`, `supplier_bill_receipts`, `supplier_bill_inventory_lines`
- `supplier_credit_notes`, `supplier_credit_note_lines`, `supplier_credit_note_layer_allocations`
- `supplier_payments`, `supplier_payment_allocations`, `supplier_bill_asset_lines`
- `purchase_integrity_issues`

### المبيعات والعملاء
- `salespersons`, `customers`, `sales_invoices`, `sales_invoice_lines`
- `sales_invoice_line_lots`, `customer_receipts`, `customer_receipt_batches`
- `customer_receipt_allocations`, `customer_advance_receipts`
- `customer_advance_allocations`, `customer_advance_refunds`, `customer_advance_reversals`
- `sales_discount_approval_requests`, `sales_invoice_workflows`
- `sales_credit_notes`, `sales_credit_note_lines`, `sales_credit_note_line_lots`
- `customer_refunds`

### عروض الأسعار والتسليم وأوامر البيع
- `customer_price_rules`, `quotations`, `quotation_lines`
- `sales_orders`, `sales_order_lines`
- `delivery_notes`, `delivery_note_lines`, `delivery_note_line_lots`

### المرتبات والموارد البشرية
- `employees`, `employee_pay_items`, `employee_pay_item_catalog`
- `salary_change_batches`, `employee_salary_history`, `workforce_migrations`
- `employee_employment_history`, `employee_end_of_service_settlements`
- `employee_final_payrolls`, `employee_final_payroll_payments`
- `payroll_runs`, `payroll_lines`, `payroll_line_items`
- `payroll_payments`, `payroll_reversals`
- `employee_loans`, `employee_loan_installments`, `payroll_loan_allocations`
- `payroll_incentive_rules`, `employee_custodies`, `custody_expenses`
- `attendance_records`, `friday_allowance_batches`, `friday_allowance_lines`
- `employee_grant_types`, `employee_grants`, `employee_grant_lines`, `employee_grant_payments`
- `leave_types`, `employee_leave_balances`, `employee_leave_requests`
- `employee_penalties`, `payroll_payment_allocations`

### الأصول والبنوك والنقدية
- `fixed_assets`, `asset_depreciations`, `asset_depreciation_revisions`
- `asset_disposals`, `fixed_asset_supplier_bill_links`, `fixed_asset_journal_lines`
- `bank_transactions`, `cash_accounts`, `bank_statement_lines`, `bank_statement_balances`

### الموازنات والتدفقات والالتزامات
- `budget_plans`, `budget_lines`, `budget_monthly_lines`, `budget_audit_log`
- `cash_flow_account_mappings`, `financial_years`, `financial_year_journal_history`
- `cash_commitments`, `cash_commitment_payments`
- `cash_commitment_schedules`, `cash_commitment_schedule_occurrences`

### العملات والضرائب والتسويات
- `fx_exchange_rates`, `fx_transactions`, `fx_settlements`, `fx_revaluations`
- `period_adjustments`, `period_adjustment_posts`
- `payroll_rule_sets`, `payroll_tax_brackets`, `payroll_tax_tier_brackets`
- `payroll_statutory_payments`, `tax_document_reference_audit`

### لوحة الإدارة والتراخيص
- `management_dashboard_data_version`, `management_account_roles`, `management_dashboard_meta`
- `licenses`, `trial_devices`

## 3. جداول/أسماء مرتبطة بإعادة بناء أو ترحيل داخلي

ظهرت أسماء وسيطة أو نسخ إعادة بناء داخل منطق التهيئة، ومنها:
- `journal_lines_decimal_new`
- `attendance_records_rebuilt`
- `payroll_runs_rebuilt`

كما ظهر أكثر من تعريف/مسار تهيئة لـ `stocktake_lines`. يجب فحص ترتيب التنفيذ وشروط النقل لهذه الجداول؛ لا تُنشأ كجداول تشغيل دائمة في PostgreSQL قبل تحديد هل هي جداول مرحلية مؤقتة أم جزء من آلية ترحيل قديمة.

## 4. ملفات تنشئ أو تعدل المخطط

| الملف | ملاحظات |
|---|---|
| `app.py` | الجداول الأساسية، ترحيل مبالغ القيود، المستخدمون، الحسابات، الإعدادات |
| `activity_profiles.py` | ملف تعريف النشاط |
| `analytic_dimensions.py` | الفروع ومراكز التكلفة والمشروعات، وتعديلات على الفروع والقيود |
| `stock.py` | المخزون والمواقع والتشغيلات والتقييم والجرد، مع عدة عمليات ALTER وإعادة بناء |
| `purchases.py` | الموردون ودورة الشراء والاستلام والفواتير والدفعات |
| `sales.py` | العملاء والفواتير والتحصيلات والدفعات المقدمة والمرتجعات |
| `quotations.py` | عروض الأسعار وأوامر البيع والتسليم |
| `workforce.py` | الموظفون والمرتبات والحضور والإجازات والسلف والحوافز |
| `workforce_extras.py` | الجزاءات وتفاصيل بنود المرتبات وتوزيع الدفعات |
| `fixed_assets.py` | الأصول والإهلاك والاستبعاد |
| `banking.py` | الحسابات النقدية وكشوف البنوك والمطابقة |
| `financial_planning.py` | الموازنات والسنوات المالية وربط التدفقات |
| `cash_commitments.py` | الالتزامات وجدولتها ومدفوعاتها |
| `currency_center.py` | أسعار الصرف ومعاملات وتسويات وإعادة تقييم العملات |
| `period_adjustments.py` | التسويات الدورية وترحيلاتها |
| `payroll_compliance.py` | قواعد المرتبات والضرائب والاستقطاعات النظامية |
| `tax_center.py` | سجل مراجع المستندات الضريبية |
| `management_dashboard.py` | بيانات وتهيئة أدوار لوحة الإدارة |
| `code_sequences.py` | تسلسل أرقام المستندات |
| `license_server.py` | جداول التراخيص والأجهزة التجريبية |
| `chart_templates.py` | تعديلات على شجرة الحسابات |

## 5. متطلبات استخراج المخطط الفعلي قبل كتابة PostgreSQL migrations

يجب على أداة الجرد التالية أن تعمل على قاعدة SQLite اختبارية مؤقتة تم إنشاؤها من نسخة المصدر، وأن تستخرج:

1. أسماء الجداول والـviews والفهارس من `sqlite_master`.
2. الأعمدة وأنواعها وخصائص `NOT NULL` و`DEFAULT` و`PRIMARY KEY` من `PRAGMA table_info`.
3. المفاتيح الخارجية من `PRAGMA foreign_key_list`.
4. الفهارس الفريدة وغير الفريدة من `PRAGMA index_list` و`PRAGMA index_info`.
5. تعريف SQL الأصلي لكل جدول وفهرس.
6. أعداد السجلات في قاعدة اختبار مرجعية فقط، مع عدم فتح أو لمس `data/` الحقيقية.
7. أنواع أعمدة المبالغ والتواريخ والمعرفات، وتصنيف كل جدول حسب الوحدة والملكية.

بعد الاستخراج، تُراجع العلاقات الدائرية، المفاتيح التي لا تحمل FK صريحًا، الحقول النصية المستخدمة كمبالغ، والجداول التي لا تحتوي مفتاحًا أساسيًا واضحًا.

## 6. قرارات فنية قبل كتابة أول migration

- لا نضيف `company_id` أو `branch_id` جماعيًا ضمن نقل المحرك؛ تعدد الشركات/الفروع مشروع منفصل يحتاج تصميم عزل بيانات واختبارات صلاحيات.
- لا نحول `TEXT` إلى `NUMERIC` قبل تقرير الدقة المطلوبة وفحص القيم الفعلية والتقارير التي تعتمد على دوال Decimal.
- لا نستبدل كل `INSERT OR IGNORE` آليًا؛ يجب تحديد مفتاح التكرار لكل عملية.
- لا نترجم `BEGIN IMMEDIATE` إلى قفل عام؛ نحدد الصفوف/الموارد التي تحتاج `SELECT ... FOR UPDATE` أو قيد uniqueness أو معاملة عادية.
- لا ننقل نسخ SQLite الاحتياطية إلى PostgreSQL؛ نحتاج مسار نسخ واستعادة مستقلًا.

## 7. الحالة

- اكتمل الجرد الساكن الأولي لأسماء الجداول ومواقع CREATE/ALTER في ملفات التطبيق.
- لم يكتمل بعد استخراج مخطط فعلي موحد من قاعدة اختبار مهيأة بالكامل.
- لم تُكتب migrations PostgreSQL، ولم يتغير كود التشغيل.
- الخطوة التالية: بناء أداة schema introspection للاختبار المؤقت، ثم حفظ تقرير JSON/Markdown قابل للمقارنة، وبعدها تصميم ترحيلات PostgreSQL المرقمة.

## 8. تحديث التنفيذ — 18 نوفمبر 2026

تمت إضافة مسار آلي لاستخراج ومراجعة المخطط الفعلي، بدل الاعتماد على الجرد الساكن وحده:

1. `scripts/export_initialized_sqlite_schema.py` ينشئ قاعدة SQLite جديدة داخل مجلد النظام المؤقت، ويستدعي تهيئة التطبيق الفعلية، ثم يصدّر تعريفات الجداول والأعمدة والمفاتيح الخارجية والفهارس والمشغلات. لا يفتح قاعدة الشركة الموجودة.
2. `scripts/inspect_sqlite_schema.py` يقرأ قاعدة الاختبار بوضع read-only، ويستخرج تعريفات `sqlite_master` وخصائص الأعمدة والمفاتيح والفهارس، مع فحوص سلامة SQLite.
3. `scripts/generate_postgres_schema.py` يولّد خط أساس PostgreSQL محافظًا. الحقول النصية لا تتحول تلقائيًا إلى أرقام؛ التحويلات الرقمية محصورة في قائمة مراجعة صريحة. كما يحافظ على المفاتيح الخارجية المركبة ويحل مراجع SQLite التي لا تسمي أعمدة الهدف إلى المفتاح الأساسي المرتب للجدول الأب.
4. ملف المراجعة الناتج يتضمن بيانًا تفصيليًا لكل علاقة FK: الجدول والأعمدة المصدرية والجدول والأعمدة الهدف وإجراءات التحديث والحذف.
5. `scripts/validate_postgres_schema.py` ينشئ schema عشوائيًا مؤقتًا في PostgreSQL، ينفذ DDL، ثم يقارن أعداد الجداول والأعمدة والعلاقات والفهارس والمشغلات، ويتحقق من تطابق بيان المفاتيح الخارجية نفسه، ثم يحذف schema الاختبار فقط.
6. GitHub Actions يشغّل هذه الخطوات بعد الاختبارات على PostgreSQL 16، ويحفظ ملفات المخطط والتقرير كـ artifact مؤقت.

### حدود ما تم إنجازه

- هذا المسار يثبت أن خط أساس المخطط المولّد قابل للتنفيذ على PostgreSQL 16 وأن العلاقات التي أعلنها المولّد تطابق العلاقات المنشأة.
- لا يعني ذلك أن كل جداول وعمليات التطبيق أصبحت تعمل على PostgreSQL؛ التطبيق ما زال SQLite runtime.
- لا يعني ذلك أن ترحيل بيانات الشركات قد تم؛ لا توجد أي بيانات شركة في مخطط الاختبار.
- وجود views أو تعبيرات/خصائص SQLite غير مدعومة يوقف التوليد بدل إسقاطها بصمت.
- لا يُسمح بتبديل backend الافتراضي أو إزالة حاجز منع PostgreSQL runtime قبل اكتمال adapter التطبيق، وترحيلات البيانات، والنسخ والاستعادة، ومراجعة SQL واختبارات العمليات المحاسبية والمخزنية والرواتب.

**الحالة الحالية:** استخراج المخطط الفعلي وتوليد DDL والتحقق من بنية العلاقات أصبح مسار CI آليًا. الخطوة التالية هي إكمال تغطية الترحيلات التشغيلية ومقارنة كل جدول وعمود وفهرس وقيد مع التطبيق، ثم اختبار نسخ بيانات اصطناعية متوازنة ومطابقتها قبل أي ربط للتطبيق بمحرك PostgreSQL.
