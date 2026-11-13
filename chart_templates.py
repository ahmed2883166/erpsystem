"""Activity-specific starter charts for NEXUS ERP.
These are practical starting templates, not a substitute for a country-specific statutory chart.
"""
CHARTS = {
    "commercial": [
        ("1000","الأصول","asset",None,1),("1100","الأصول المتداولة","asset","1000",1),
        ("1110","الصندوق","asset","1100",0),("1120","البنوك","asset","1100",0),
        ("1130","العملاء","asset","1100",0),("1140","أوراق القبض","asset","1100",0),
        ("1150","المخزون التجاري","asset","1100",0),("1160","دفعات مقدمة للموردين","asset","1100",0),
        ("1200","الأصول غير المتداولة","asset","1000",1),("1210","الأصول الثابتة","asset","1200",1),
        ("1211","الأثاث والتجهيزات","asset","1210",0),("1212","السيارات","asset","1210",0),("1219","مجمع الإهلاك","asset","1210",0),
        ("2000","الالتزامات","liability",None,1),("2100","الالتزامات المتداولة","liability","2000",1),
        ("2110","الموردون","liability","2100",0),("2120","مصروفات مستحقة","liability","2100",0),
        ("2130","ضريبة القيمة المضافة","liability","2100",0),("2140","دفعات مقدمة من العملاء","liability","2100",0),
        ("2200","التزامات طويلة الأجل","liability","2000",1),("2210","قروض طويلة الأجل","liability","2200",0),
        ("3000","حقوق الملكية","equity",None,1),("3100","رأس المال","equity","3000",0),
        ("3200","الأرباح المحتجزة","equity","3000",0),("3300","جاري الشركاء","equity","3000",0),
        ("4000","الإيرادات","revenue",None,1),("4100","إيرادات المبيعات","revenue","4000",0),("4200","مردودات ومسموحات المبيعات","revenue","4000",0),
        ("5000","تكلفة المبيعات","expense",None,1),("5100","تكلفة البضاعة المباعة","expense","5000",0),
        ("6000","المصروفات التشغيلية","expense",None,1),("6100","الرواتب والأجور","expense","6000",0),
        ("6200","الإيجارات","expense","6000",0),("6300","التسويق والبيع","expense","6000",0),("6400","مصروفات إدارية وعمومية","expense","6000",0),
        ("7000","إيرادات أخرى","revenue",None,1),("7100","إيرادات أخرى","revenue","7000",0),("7200","مصروفات تمويلية","expense",None,0),
    ],
    "service": [
        ("1000","الأصول","asset",None,1),("1100","الأصول المتداولة","asset","1000",1),("1110","الصندوق والبنوك","asset","1100",0),
        ("1120","العملاء","asset","1100",0),("1130","إيرادات مستحقة / أصول عقود","asset","1100",0),("1140","مصروفات مقدمة","asset","1100",0),
        ("1200","الأصول غير المتداولة","asset","1000",1),("1210","أجهزة ومعدات","asset","1200",0),("1220","مجمع الإهلاك","asset","1200",0),
        ("2000","الالتزامات","liability",None,1),("2100","الموردون","liability","2000",0),("2110","مصروفات مستحقة","liability","2000",0),("2120","دفعات مقدمة من العملاء / التزامات عقود","liability","2000",0),("2130","ضريبة القيمة المضافة - مخرجات","liability","2000",0),("2150","ضرائب أخرى مستحقة","liability","2000",0),
        ("3000","حقوق الملكية","equity",None,1),("3100","رأس المال","equity","3000",0),("3200","الأرباح المحتجزة","equity","3000",0),
        ("4000","إيرادات النشاط","revenue",None,1),("4100","إيرادات الخدمات","revenue","4000",0),("4200","إيرادات عقود ومشروعات","revenue","4000",0),("4300","خصومات ومردودات خدمات","revenue","4000",0),
        ("5000","تكاليف تقديم الخدمة","expense",None,1),("5100","أجور فنيين ومقدمي الخدمة","expense","5000",0),("5200","مواد ومستلزمات مستخدمة","expense","5000",0),("5300","مقاولو الباطن","expense","5000",0),
        ("6000","مصروفات تشغيلية وإدارية","expense",None,1),("6100","إيجارات","expense","6000",0),("6200","تسويق","expense","6000",0),("6300","إدارية وعمومية","expense","6000",0),("6400","إهلاك","expense","6000",0),
    ],
    "medical": [
        ("1000","الأصول","asset",None,1),("1100","الأصول المتداولة","asset","1000",1),("1110","الصندوق","asset","1100",0),("1120","البنوك","asset","1100",0),
        ("1130","المرضى والعملاء","asset","1100",0),("1140","شركات التأمين - مطالبات مستحقة","asset","1100",0),("1150","الأدوية والمستلزمات الطبية","asset","1100",0),("1160","مخزون مستلزمات المعامل","asset","1100",0),
        ("1200","الأصول غير المتداولة","asset","1000",1),("1210","أجهزة ومعدات طبية","asset","1200",0),("1220","أثاث وتجهيزات","asset","1200",0),("1230","مجمع الإهلاك","asset","1200",0),
        ("2000","الالتزامات","liability",None,1),("2100","الموردون","liability","2000",0),("2110","أتعاب أطباء مستحقة","liability","2000",0),("2120","ضرائب أخرى مستحقة","liability","2000",0),("2130","ضريبة القيمة المضافة - مخرجات","liability","2000",0),("2140","مقدمات مرضى","liability","2000",0),
        ("3000","حقوق الملكية","equity",None,1),("3100","رأس المال","equity","3000",0),("3200","الأرباح المحتجزة","equity","3000",0),
        ("4000","إيرادات النشاط الطبي","revenue",None,1),("4100","إيرادات كشف","revenue","4000",0),("4200","إيرادات عمليات وإجراءات","revenue","4000",0),("4300","إيرادات تحاليل وأشعة","revenue","4000",0),("4400","إيرادات خدمات التأمين","revenue","4000",0),("4500","خصومات ومردودات","revenue","4000",0),
        ("5000","تكلفة الخدمات الطبية","expense",None,1),("5100","أتعاب الأطباء","expense","5000",0),("5200","مستهلكات طبية","expense","5000",0),("5300","تكلفة الأدوية والمستلزمات","expense","5000",0),
        ("6000","مصروفات تشغيلية","expense",None,1),("6100","رواتب","expense","6000",0),("6200","إيجارات","expense","6000",0),("6300","صيانة ومعايرة أجهزة","expense","6000",0),("6400","إدارية وعمومية","expense","6000",0),
    ],
    "restaurant": [
        ("1000","الأصول","asset",None,1),("1100","الأصول المتداولة","asset","1000",1),("1110","الصندوق ونقاط البيع","asset","1100",0),("1120","البنوك","asset","1100",0),
        ("1130","تطبيقات التوصيل - مستحقات","asset","1100",0),("1140","المخزون الغذائي","asset","1100",0),("1150","مخزون المشروبات","asset","1100",0),("1160","مواد التعبئة والتغليف","asset","1100",0),
        ("1200","الأصول غير المتداولة","asset","1000",1),("1210","معدات المطعم والمطبخ","asset","1200",0),("1220","الأثاث والديكورات","asset","1200",0),("1230","مجمع الإهلاك","asset","1200",0),
        ("2000","الالتزامات","liability",None,1),("2100","الموردون","liability","2000",0),("2110","رواتب مستحقة","liability","2000",0),("2120","ضرائب أخرى مستحقة","liability","2000",0),("2130","ضريبة القيمة المضافة - مخرجات","liability","2000",0),("2140","إكراميات مستحقة للعاملين","liability","2000",0),
        ("3000","حقوق الملكية","equity",None,1),("3100","رأس المال","equity","3000",0),("3200","الأرباح المحتجزة","equity","3000",0),
        ("4000","إيرادات المطعم","revenue",None,1),("4100","مبيعات صالة","revenue","4000",0),("4200","مبيعات تيك أواي","revenue","4000",0),("4300","مبيعات توصيل مباشر","revenue","4000",0),("4400","مبيعات تطبيقات التوصيل","revenue","4000",0),("4500","خصومات ومردودات","revenue","4000",0),
        ("5000","تكلفة الطعام والمشروبات","expense",None,1),("5100","تكلفة المواد الغذائية","expense","5000",0),("5200","تكلفة المشروبات","expense","5000",0),("5300","هالك وتالف","expense","5000",0),("5400","مواد التعبئة","expense","5000",0),
        ("6000","مصروفات التشغيل","expense",None,1),("6100","أجور العاملين","expense","6000",0),("6200","إيجار","expense","6000",0),("6300","غاز وكهرباء ومياه","expense","6000",0),("6400","عمولات تطبيقات التوصيل","expense","6000",0),("6500","صيانة ونظافة","expense","6000",0),
    ],
    "industrial": [
        ("1000","الأصول","asset",None,1),("1100","الأصول المتداولة","asset","1000",1),("1110","الصندوق والبنوك","asset","1100",0),("1120","العملاء","asset","1100",0),
        ("1130","مخزون المواد الخام","asset","1100",0),("1140","مخزون إنتاج تحت التشغيل","asset","1100",0),("1150","مخزون الإنتاج التام","asset","1100",0),("1160","مخزون قطع الغيار","asset","1100",0),
        ("1200","الأصول غير المتداولة","asset","1000",1),("1210","الآلات وخطوط الإنتاج","asset","1200",0),("1220","المباني","asset","1200",0),("1230","مجمع الإهلاك","asset","1200",0),
        ("2000","الالتزامات","liability",None,1),("2100","الموردون","liability","2000",0),("2110","أجور مستحقة","liability","2000",0),("2120","ضرائب أخرى مستحقة","liability","2000",0),("2130","ضريبة القيمة المضافة - مخرجات","liability","2000",0),("2200","التزامات طويلة الأجل","liability","2000",1),("2210","قروض طويلة الأجل","liability","2200",0),
        ("3000","حقوق الملكية","equity",None,1),("3100","رأس المال","equity","3000",0),("3200","الأرباح المحتجزة","equity","3000",0),
        ("4000","الإيرادات","revenue",None,1),("4100","مبيعات الإنتاج","revenue","4000",0),("4200","مردودات ومسموحات","revenue","4000",0),
        ("5000","تكلفة الإنتاج المباشرة","expense",None,1),("5100","مواد خام مباشرة","expense","5000",0),("5200","أجور مباشرة","expense","5000",0),("5300","تكاليف صناعية غير مباشرة","expense","5000",0),("5400","تكلفة الإنتاج التام المباع","expense","5000",0),
        ("6000","مصروفات تشغيلية وإدارية","expense",None,1),("6100","رواتب إدارية","expense","6000",0),("6200","بيع وتوزيع","expense","6000",0),("6300","إيجارات","expense","6000",0),("6400","إهلاك","expense","6000",0),("6500","صيانة المصنع","expense","6000",0),
    ],
}
ACTIVITY_LABELS = {
    "commercial": "تجاري", "service": "خدمي", "medical": "طبي",
    "restaurant": "مطاعم", "industrial": "صناعي"
}

# Canonical account-role and posting mappings, kept beside each activity chart.
# These codes are validated against the actual CHARTS definitions in tests.
ACTIVITY_ACCOUNT_ROLES = {
    "commercial": {
        "cash": ("1110", "1120"), "receivables": ("1130",),
        "payables": ("2110",), "customer_advances": ("2160",), "inventory": ("1150",),
        "quick_assets": ("1110", "1120", "1130", "1140"),
        "current_assets": ("1110", "1120", "1130", "1140", "1150", "1160", "1165", "1191", "1192", "1193"),
        "current_liabilities": ("2110", "2120", "2130", "2140", "2160", "2191", "2192"),
        "direct_cost": ("5100", "5610", "5620", "5630", "5640", "5650"),
    },
    "service": {
        "cash": ("1110",), "receivables": ("1120", "1130"),
        "payables": ("2100",), "customer_advances": ("2160",), "inventory": (),
        "quick_assets": ("1110", "1120", "1130"),
        "current_assets": ("1110", "1120", "1130", "1140", "1165", "1191", "1192", "1193"),
        "current_liabilities": ("2100", "2110", "2120", "2130", "2150", "2160", "2191", "2192"),
        "direct_cost": ("5100", "5200", "5300", "5610", "5620", "5630", "5640", "5650"),
    },
    "medical": {
        "cash": ("1110", "1120"), "receivables": ("1130", "1140"),
        "payables": ("2100",), "customer_advances": ("2160",), "inventory": ("1150", "1160"),
        "quick_assets": ("1110", "1120", "1130", "1140"),
        "current_assets": ("1110", "1120", "1130", "1140", "1150", "1160", "1165", "1191", "1192", "1193"),
        "current_liabilities": ("2100", "2110", "2120", "2130", "2140", "2160", "2191", "2192"),
        "direct_cost": ("5100", "5200", "5300", "5610", "5620", "5630", "5640", "5650"),
    },
    "restaurant": {
        "cash": ("1110", "1120"), "receivables": ("1130",),
        "payables": ("2100",), "customer_advances": ("2160",), "inventory": ("1140", "1150", "1160"),
        "quick_assets": ("1110", "1120", "1130"),
        "current_assets": ("1110", "1120", "1130", "1140", "1150", "1160", "1165", "1191", "1192", "1193"),
        "current_liabilities": ("2100", "2110", "2120", "2130", "2140", "2160", "2191", "2192"),
        "direct_cost": ("5100", "5200", "5300", "5400", "5610", "5620", "5630", "5640", "5650"),
    },
    "industrial": {
        "cash": ("1110",), "receivables": ("1120",),
        "payables": ("2100",), "customer_advances": ("2160",), "inventory": ("1130", "1140", "1150", "1160"),
        "quick_assets": ("1110", "1120"),
        "current_assets": ("1110", "1120", "1130", "1140", "1150", "1160", "1165", "1191", "1192", "1193"),
        "current_liabilities": ("2100", "2110", "2120", "2130", "2160", "2191", "2192"),
        "direct_cost": ("5400", "5610", "5620", "5630", "5640", "5650"),
    },
}

# Posting accounts follow each chart. Industrial purchases and sales both use
# 1130 in v1 because manufacturing/finished-goods conversion is not implemented.
ACTIVITY_POSTING_ACCOUNTS = {
    "commercial": {"ar": "1130", "revenue": "4100", "vat": "2130", "inventory": "1150", "cogs": "5100", "purchase_inventory": "1150", "supplier_ap": "2110", "input_vat": "1165"},
    "service": {"ar": "1120", "revenue": "4100", "vat": "2130", "inventory": None, "cogs": None, "purchase_inventory": None, "supplier_ap": "2100", "input_vat": "1165"},
    "medical": {"ar": "1130", "revenue": "4100", "vat": "2130", "inventory": "1150", "cogs": "5300", "purchase_inventory": "1150", "supplier_ap": "2100", "input_vat": "1165"},
    "restaurant": {"ar": "1130", "revenue": "4100", "vat": "2130", "inventory": "1140", "cogs": "5100", "purchase_inventory": "1140", "supplier_ap": "2100", "input_vat": "1165"},
    "industrial": {"ar": "1120", "revenue": "4100", "vat": "2130", "inventory": "1130", "cogs": "5400", "purchase_inventory": "1130", "supplier_ap": "2100", "input_vat": "1165"},
}

# Retained earnings is resolved through the same activity account-role mapping.
for _activity_roles in ACTIVITY_ACCOUNT_ROLES.values():
    _activity_roles["retained_earnings"] = ("3200",)

# Codes referenced by posting, tax, and subledger role mappings must remain stable.
# 2190 is also the GRNI control account used by the purchasing receipt/bill flow.
OPERATIONAL_ACCOUNT_CODES = frozenset(
    {code for mapping in ACTIVITY_ACCOUNT_ROLES.values() for values in mapping.values()
     for code in (values if isinstance(values, (tuple, list, set)) else (values,))}
    | {code for mapping in ACTIVITY_POSTING_ACCOUNTS.values() for code in mapping.values() if code}
    | {"2190"}
)

# Tax control accounts are resolved by accounting role, not by a sales-module code.
ACTIVITY_TAX_ACCOUNT_ROLES = {
    activity: {"output_vat": "2130", "input_vat": "1165"}
    for activity in ACTIVITY_POSTING_ACCOUNTS
}

_CONTROL_ROLE_NAMES = {"cash", "receivables", "payables", "inventory", "customer_advances"}
CONTROL_ACCOUNT_CODES = frozenset(
    {code for mapping in ACTIVITY_ACCOUNT_ROLES.values()
     for role, values in mapping.items() if role in _CONTROL_ROLE_NAMES
     for code in (values if isinstance(values, (tuple, list, set)) else (values,))}
    | {mapping[key] for mapping in ACTIVITY_POSTING_ACCOUNTS.values()
       for key in ("ar", "supplier_ap", "vat", "input_vat", "inventory", "purchase_inventory")
       if mapping.get(key)}
    | {"2190"}
)


def account_metadata_for(account_type, code, name_ar):
    """Return non-posting chart metadata without changing ledger identity or balances."""
    code = str(code or "").strip()
    name = str(name_ar or "").strip()

    normal_balance = {
        "asset": "debit", "expense": "debit",
        "liability": "credit", "equity": "credit", "revenue": "credit",
    }.get(account_type, "debit")
    if account_type == "asset" and any(term in name for term in (
        "مجمع الإهلاك", "مجمع إطفاء", "مخصص خسائر ائتمانية",
    )):
        normal_balance = "credit"
    elif account_type == "revenue" and any(term in name for term in (
        "مردودات", "خصومات", "مسموحات",
    )):
        normal_balance = "debit"

    if account_type == "asset":
        statement_section = "current_assets" if code.startswith("11") else (
            "non_current_assets" if code.startswith("12") else "assets"
        )
    elif account_type == "liability":
        statement_section = "current_liabilities" if code.startswith("21") else (
            "non_current_liabilities" if code.startswith("22") else "liabilities"
        )
    elif account_type == "equity":
        statement_section = "equity"
    elif account_type == "revenue":
        statement_section = "revenue"
    elif account_type == "expense":
        statement_section = "direct_costs" if code.startswith(("5", "56")) else (
            "finance_costs" if code.startswith("72") else "operating_expenses"
        )
    else:
        statement_section = "unclassified"

    if any(term in name for term in ("صندوق", "بنك", "نقدية", "نقاط البيع")):
        account_subtype = "cash"
    elif code in {"1191", "1192"}:
        account_subtype = "contract_asset_or_retention"
    elif code == "1193":
        account_subtype = "supplier_or_carrier_advance"
    elif code == "2160" or ("دفعات مقدمة" in name and "عملاء" in name):
        account_subtype = "customer_advance"
    elif any(term in name for term in ("مجمع الإهلاك", "مجمع إطفاء")):
        account_subtype = "accumulated_depreciation_or_amortization"
    elif "مخزون" in name:
        account_subtype = "inventory"
    elif any(term in name for term in ("عميل", "عملاء", "مرضى", "ذمم مدينة")):
        account_subtype = "receivable"
    elif any(term in name for term in ("مورد", "ناقل", "دائن")):
        account_subtype = "payable"
    elif "ضريبة" in name or "زكاة" in name:
        account_subtype = "tax_control"
    elif "حق الاستخدام" in name:
        account_subtype = "right_of_use_asset"
    elif "إيجار" in name and account_type == "liability":
        account_subtype = "lease_liability"
    elif "قروض" in name or "تسهيلات" in name:
        account_subtype = "loan"
    elif "مكافأة نهاية الخدمة" in name or "منافع العاملين" in name:
        account_subtype = "employee_benefit"
    elif code == "2232":
        account_subtype = "warranty_provision"
    elif "مخصص" in name:
        account_subtype = "provision_or_allowance"
    elif "مردودات" in name or "خصومات" in name or "مسموحات" in name:
        account_subtype = "contra_revenue"
    elif code == "2190":
        account_subtype = "goods_received_not_invoiced"
    elif code == "2191":
        account_subtype = "retention_payable"
    elif code == "2192":
        account_subtype = "carrier_payable"
    elif account_type == "expense" and statement_section == "direct_costs":
        account_subtype = "direct_cost"
    elif account_type == "expense" and statement_section == "finance_costs":
        account_subtype = "finance_cost"
    else:
        account_subtype = account_type or "unclassified"

    cash_flow_category = "cash_equivalents" if account_subtype == "cash" else None
    return {
        "normal_balance": normal_balance,
        "statement_section": statement_section,
        "cash_flow_category": cash_flow_category,
        "account_subtype": account_subtype,
        "is_control_account": int(code in CONTROL_ACCOUNT_CODES),
    }


def ensure_account_metadata(conn):
    """Add and refresh derived chart metadata; never touches journal lines or balances."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    declarations = {
        "normal_balance": "TEXT NOT NULL DEFAULT 'debit'",
        "statement_section": "TEXT",
        "cash_flow_category": "TEXT",
        "account_subtype": "TEXT",
        "is_control_account": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, declaration in declarations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {declaration}")
    for row in conn.execute("SELECT id,code,name_ar,account_type FROM accounts").fetchall():
        metadata = account_metadata_for(row["account_type"], row["code"], row["name_ar"])
        conn.execute(
            """UPDATE accounts SET normal_balance=?,statement_section=?,
               cash_flow_category=?,account_subtype=?,is_control_account=? WHERE id=?""",
            (metadata["normal_balance"], metadata["statement_section"],
             metadata["cash_flow_category"], metadata["account_subtype"],
             metadata["is_control_account"], row["id"]),
        )



# Common support branches available in every activity. Existing account IDs and
# posted journal links are preserved by the explicit, idempotent VAT migration below.
_COMMON_ACTIVITY_ACCOUNTS = [
    ("1165", "ضريبة قيمة مضافة مدخلات قابلة للخصم", "asset", "1100", 0),
    ("2160", "دفعات مقدمة من العملاء", "liability", "2000", 0),
    ("1240", "أصول ثابتة إضافية", "asset", "1200", 1),
    ("1241", "مباني وتحسينات", "asset", "1240", 0),
    ("1242", "أثاث وتجهيزات", "asset", "1240", 0),
    ("1243", "سيارات ووسائل نقل", "asset", "1240", 0),
    ("1244", "آلات ومعدات", "asset", "1240", 0),
    ("1245", "أجهزة حاسب وشبكات", "asset", "1240", 0),
    ("1246", "أدوات ومعدات صغيرة", "asset", "1240", 0),
    ("1249", "مجمع إهلاك الأصول الثابتة الإضافية", "asset", "1240", 0),
    ("6600", "الأجور والمرتبات ومزايا العاملين", "expense", "6000", 1),
    ("6610", "الأجور والمرتبات الأساسية", "expense", "6600", 0),
    ("6620", "حصة الشركة في التأمينات الاجتماعية", "expense", "6600", 0),
    ("6630", "بدلات ومزايا العاملين", "expense", "6600", 0),
    ("6640", "مصروف مخصص مكافأة نهاية الخدمة", "expense", "6600", 0),
    ("6700", "التسويق والمبيعات", "expense", "6000", 1),
    ("6710", "إعلانات وحملات تسويقية", "expense", "6700", 0),
    ("6720", "عمولات البيع والتسويق", "expense", "6700", 0),
    ("6730", "ضيافة وعروض ترويجية", "expense", "6700", 0),
    # Additional current assets and receivables.
    ("1170", "سلف وعهد وأرصدة مدينة أخرى", "asset", "1100", 1),
    ("1171", "سلف العاملين", "asset", "1170", 0),
    ("1172", "عهد العاملين", "asset", "1170", 0),
    ("1173", "مصروفات مدفوعة مقدمًا", "asset", "1170", 0),
    ("1174", "إيرادات مستحقة وأرصدة مدينة أخرى", "asset", "1170", 0),
    ("1180", "مخصص خسائر ائتمانية متوقعة - العملاء", "asset", "1100", 0),
    # Long-term operating assets.
    ("1250", "أصول حق الاستخدام", "asset", "1200", 0),
    ("1260", "الأصول غير الملموسة", "asset", "1200", 1),
    ("1261", "برامج وتراخيص وأنظمة", "asset", "1260", 0),
    ("1269", "مجمع إطفاء الأصول غير الملموسة", "asset", "1260", 0),
    ("1270", "استثمارات طويلة الأجل", "asset", "1200", 0),
    # Current liabilities, accruals and GRNI.
    ("2170", "التزامات تشغيلية مستحقة", "liability", "2000", 1),
    ("2171", "مصروفات مستحقة", "liability", "2170", 0),
    ("2172", "رواتب وأجور مستحقة", "liability", "2170", 0),
    ("2173", "تأمينات اجتماعية مستحقة", "liability", "2170", 0),
    ("2180", "الجزء المتداول من القروض", "liability", "2000", 0),
    ("2190", "بضاعة مستلمة ولم ترد فاتورتها - GRNI", "liability", "2000", 0),
    # Long-term liabilities and employee benefits.
    ("2200", "الالتزامات طويلة الأجل", "liability", "2000", 1),
    ("2210", "قروض وتسهيلات طويلة الأجل", "liability", "2200", 0),
    ("2220", "التزامات عقود الإيجار", "liability", "2200", 0),
    ("2230", "التزامات منافع العاملين", "liability", "2200", 1),
    ("2231", "مخصص مكافأة نهاية الخدمة", "liability", "2230", 0),
    ("2240", "التزامات ضريبة مؤجلة", "liability", "2200", 0),
    # Additional income, inventory losses and operating expenses.
    ("4700", "إيرادات ومكاسب أخرى", "revenue", "4000", 1),
    ("4710", "أرباح بيع أصول", "revenue", "4700", 0),
    ("4720", "أرباح فروق عملة", "revenue", "4700", 0),
    ("5500", "فروق وهالك وتسويات المخزون", "expense", "5000", 1),
    ("5510", "عجز وهالك مخزون", "expense", "5500", 0),
    ("6800", "مصروفات تشغيلية أخرى", "expense", "6000", 1),
    ("6810", "مصروف الإهلاك", "expense", "6800", 0),
    ("6820", "مصروف الخسائر الائتمانية", "expense", "6800", 0),
    ("6830", "خسائر انخفاض قيمة الأصول", "expense", "6800", 0),
    ("7240", "مصروفات تمويلية", "expense", None, 1),
    ("7241", "مصروف فوائد وتمويل", "expense", "7240", 0),
    ("7242", "عمولات ومصاريف بنكية", "expense", "7240", 0),
    ("7243", "خسائر فروق عملة", "expense", "7240", 0),
    # Contracting, project and logistics support accounts.
    ("1190", "أصول عقود واحتجازات لدى العملاء", "asset", "1100", 1),
    ("1191", "مستخلصات واحتجازات لدى العملاء", "asset", "1190", 0),
    ("1192", "أصول عقود وإيرادات غير مفوترة", "asset", "1190", 0),
    ("1193", "دفعات مقدمة لمقاولي الباطن والناقلين", "asset", "1190", 0),
    ("2191", "احتجازات مستحقة لمقاولي الباطن", "liability", "2000", 0),
    ("2192", "مستحقات الناقلين ومقدمي الخدمات", "liability", "2000", 0),
    ("2232", "مخصص ضمان وصيانة تعاقدية", "liability", "2230", 0),
    ("5600", "تكاليف المشروعات والنقل المباشرة", "expense", "5000", 1),
    ("5610", "تكاليف مقاولي الباطن والناقلين", "expense", "5600", 0),
    ("5620", "مواد ومستلزمات المشروعات", "expense", "5600", 0),
    ("5630", "أجور مباشرة للمشروعات", "expense", "5600", 0),
    ("5640", "معدات ونقل ومناولة للمشروعات", "expense", "5600", 0),
    ("5650", "مصروفات مواقع ومشروعات", "expense", "5600", 0),
]

for _activity, _chart in CHARTS.items():
    _existing = {row[0] for row in _chart}
    _available = set(_existing)
    for _row in _COMMON_ACTIVITY_ACCOUNTS:
        _code, _name, _type, _parent, _is_group = _row
        if _code in _existing or (_parent is not None and _parent not in _available):
            continue
        _chart.append(_row)
        _available.add(_code)


def seed_activity_chart(conn, activity):
    """Create the selected hierarchical starter chart only for an empty ledger."""
    chart = CHARTS.get(activity, CHARTS["commercial"])
    ids = {}
    for code, name, account_type, parent_code, is_group in chart:
        parent_id = ids.get(parent_code) if parent_code else None
        cur = conn.execute(
            "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,?)",
            (code, name, account_type, parent_id, int(is_group)),
        )
        ids[code] = cur.lastrowid
    return ids


def ensure_common_activity_accounts(conn):
    """Add missing standard branches without changing existing account IDs or balances."""
    existing = {row["code"]: row for row in conn.execute(
        "SELECT id,code,account_type,is_group FROM accounts"
    )}
    for code, name, account_type, parent_code, is_group in _COMMON_ACTIVITY_ACCOUNTS:
        if code in existing:
            continue
        parent = existing.get(parent_code)
        parent_id = parent["id"] if parent and parent["is_group"] and parent["account_type"] == account_type else None
        cur = conn.execute(
            "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,?)",
            (code, name, account_type, parent_id, int(is_group)),
        )
        existing[code] = {"id": cur.lastrowid, "code": code,
                          "account_type": account_type, "is_group": int(is_group)}


def ensure_activity_chart_accounts(conn, activity):
    """Append missing accounts from the selected chart without altering legacy accounts."""
    chart = CHARTS.get(activity, CHARTS["commercial"])
    existing = {row["code"]: row for row in conn.execute(
        "SELECT id,code,account_type,is_group FROM accounts"
    )}
    for code, name, account_type, parent_code, is_group in chart:
        if code in existing:
            continue
        parent = existing.get(parent_code) if parent_code else None
        parent_id = parent["id"] if parent and parent["is_group"] and parent["account_type"] == account_type else None
        cur = conn.execute(
            "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,?)",
            (code, name, account_type, parent_id, int(is_group)),
        )
        existing[code] = {"id": cur.lastrowid, "code": code,
                          "account_type": account_type, "is_group": int(is_group)}


def migrate_vat_accounts(conn, activity):
    """Standardize output VAT at 2130 and deductible input VAT at 1165.

    When an older activity used 2120 for output VAT, that same account ID is
    moved to 2130. Legacy non-VAT uses of 2130 are moved to a dedicated code.
    Journal lines remain attached to their original account IDs.
    """
    legacy_destinations = {
        "medical": ("2140", "مقدمات مرضى"),
        "restaurant": ("2140", "إكراميات مستحقة للعاملين"),
        "industrial": ("2210", "قروض طويلة الأجل"),
    }
    old_2130 = conn.execute(
        "SELECT id, name_ar, account_type, is_group, parent_id FROM accounts WHERE code='2130'"
    ).fetchone()
    moved_legacy_2130 = False
    if old_2130 and "ضريبة القيمة المضافة" not in str(old_2130["name_ar"] or ""):
        if old_2130["account_type"] != "liability" or old_2130["is_group"]:
            raise ValueError("الحساب 2130 القديم ليس حساب التزام تفصيليًا؛ أوقف الترحيل لمراجعته يدويًا.")
        destination = legacy_destinations.get(activity)
        if destination:
            new_code, new_name = destination
            collision = conn.execute("SELECT id FROM accounts WHERE code=?", (new_code,)).fetchone()
            if collision:
                raise ValueError(
                    f"تعذر توحيد الضريبة: الحساب البديل {new_code} موجود بالفعل. "
                    "لم يتم تغيير أي حساب حتى لا تختلط الأرصدة."
                )
            parent_id = old_2130["parent_id"]
            if activity == "industrial":
                parent = conn.execute("SELECT id,account_type,is_group FROM accounts WHERE code='2200'").fetchone()
                if parent and (parent["account_type"] != "liability" or not parent["is_group"]):
                    raise ValueError("الحساب 2200 موجود لكنه ليس مجموعة التزامات صالحة.")
                if not parent:
                    root = conn.execute("SELECT id FROM accounts WHERE code='2000'").fetchone()
                    cur = conn.execute(
                        "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,1)",
                        ("2200", "التزامات طويلة الأجل", "liability", root["id"] if root else None),
                    )
                    parent_id = cur.lastrowid
                else:
                    parent_id = parent["id"]
            conn.execute(
                "UPDATE accounts SET code=?, name_ar=?, parent_id=? WHERE id=?",
                (new_code, new_name, parent_id, old_2130["id"]),
            )
            moved_legacy_2130 = True
        elif activity not in {"commercial", "service"}:
            raise ValueError(
                "الحساب 2130 مستخدم لغرض غير معروف؛ أوقف الترحيل لمراجعة دليل الحسابات يدويًا."
            )

    # In the legacy medical, restaurant and industrial charts, 2120 was the
    # account actually used by sales postings for output VAT. Recode that same
    # account ID only when 2130 was a non-VAT account and has just been freed.
    if activity in {"medical", "restaurant", "industrial"} and moved_legacy_2130:
        old_vat = conn.execute(
            "SELECT id,account_type,is_group FROM accounts WHERE code='2120'"
        ).fetchone()
        if old_vat:
            if old_vat["account_type"] != "liability" or old_vat["is_group"]:
                raise ValueError("الحساب 2120 القديم لا يصلح لنقل ضريبة المخرجات إلى 2130.")
            conn.execute(
                "UPDATE accounts SET code='2130', name_ar='ضريبة القيمة المضافة - مخرجات' WHERE id=?",
                (old_vat["id"],),
            )

    vat = conn.execute(
        "SELECT id,account_type,is_group FROM accounts WHERE code='2130'"
    ).fetchone()
    if vat:
        if vat["account_type"] != "liability" or vat["is_group"]:
            raise ValueError("الحساب 2130 يجب أن يكون حسابًا تفصيليًا من نوع الالتزامات.")
        conn.execute(
            "UPDATE accounts SET name_ar='ضريبة القيمة المضافة - مخرجات' WHERE id=?",
            (vat["id"],),
        )
    else:
        parent_code = "2100" if activity == "commercial" else "2000"
        parent = conn.execute("SELECT id,account_type,is_group FROM accounts WHERE code=?", (parent_code,)).fetchone()
        if parent and (parent["account_type"] != "liability" or not parent["is_group"]):
            parent = conn.execute("SELECT id FROM accounts WHERE code='2000'").fetchone()
        conn.execute(
            "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,0)",
            ("2130", "ضريبة القيمة المضافة - مخرجات", "liability", parent["id"] if parent else None),
        )

    input_vat = conn.execute("SELECT id,account_type,is_group FROM accounts WHERE code='1165'").fetchone()
    if input_vat and (input_vat["account_type"] != "asset" or input_vat["is_group"]):
        raise ValueError("الحساب 1165 محجوز لضريبة القيمة المضافة - مدخلات قابلة للخصم ويجب أن يكون أصلًا تفصيليًا.")
    if not input_vat:
        parent = conn.execute("SELECT id FROM accounts WHERE code='1100' AND is_group=1 AND account_type='asset'").fetchone()
        conn.execute(
            "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,0)",
            ("1165", "ضريبة قيمة مضافة مدخلات قابلة للخصم", "asset", parent["id"] if parent else None),
        )
