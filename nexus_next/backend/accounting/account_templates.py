"""Versioned, activity-aware starter chart templates.

Account codes are unique within a company. The ranges below deliberately keep
VAT, social-insurance, revenue, direct-cost and operating-expense accounts in
separate sections. Posting code must use AccountRole, never these code strings.
"""

from .models import AccountRole, ChartOfAccount


ACTIVITY_LABELS = {
    "commercial": "تجاري",
    "service": "خدمي",
    "medical": "طبي",
    "restaurants": "مطاعم",
    "industrial": "صناعي",
}

ACTIVITY_ACCOUNT_NAMES = {
    "commercial": ("إيرادات مبيعات البضائع", "تكلفة البضاعة المباعة"),
    "service": ("إيرادات تقديم الخدمات", "تكلفة تقديم الخدمات"),
    "medical": ("إيرادات الخدمات الطبية", "تكلفة الخدمات والمستلزمات الطبية"),
    "restaurants": ("إيرادات المطاعم والمشروبات", "تكلفة الأغذية والمشروبات المباعة"),
    "industrial": ("إيرادات بيع المنتجات", "تكلفة المنتجات المباعة"),
}

# code, name, nature, parent_code, is_group, role
COMMON_ACCOUNTS = [
    ("1000", "الأصول", "asset", None, True, None),
    ("1100", "الأصول المتداولة", "asset", "1000", True, None),
    ("1110", "الصندوق والنقدية", "asset", "1100", False, AccountRole.Role.CASH),
    ("1120", "الحسابات البنكية", "asset", "1100", False, AccountRole.Role.BANK),
    ("1130", "العملاء وأوراق القبض", "asset", "1100", False, AccountRole.Role.ACCOUNTS_RECEIVABLE),
    ("1140", "المخزون", "asset", "1100", True, None),
    ("1141", "مخزون البضائع والمواد", "asset", "1140", False, AccountRole.Role.INVENTORY),
    ("1150", "ضريبة القيمة المضافة - مدخلات", "asset", "1100", False, AccountRole.Role.VAT_INPUT),
    ("1200", "الأصول غير المتداولة", "asset", "1000", True, None),
    ("1210", "الأصول الثابتة بالتكلفة", "asset", "1200", False, None),
    ("1220", "مجمع إهلاك الأصول الثابتة", "asset", "1200", False, None),
    ("2000", "الالتزامات", "liability", None, True, None),
    ("2100", "الالتزامات المتداولة", "liability", "2000", True, None),
    ("2110", "الموردون وأوراق الدفع", "liability", "2100", False, AccountRole.Role.ACCOUNTS_PAYABLE),
    ("2120", "ضريبة القيمة المضافة - مخرجات", "liability", "2100", False, AccountRole.Role.VAT_OUTPUT),
    ("2130", "ضريبة القيمة المضافة المستحقة للسداد", "liability", "2100", False, AccountRole.Role.VAT_PAYABLE),
    ("2140", "بضاعة مستلمة غير مفوترة", "liability", "2100", False, AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED),
    ("2150", "المرتبات المستحقة", "liability", "2100", False, AccountRole.Role.PAYROLL_PAYABLE),
    ("2160", "حصة الموظفين من التأمينات المستحقة", "liability", "2100", False, AccountRole.Role.EMPLOYEE_INSURANCE_PAYABLE),
    ("2170", "حصة الشركة من التأمينات المستحقة", "liability", "2100", False, AccountRole.Role.EMPLOYER_INSURANCE_PAYABLE),
    ("2180", "تسويات وحسابات بين الفروع", "liability", "2100", False, AccountRole.Role.INTER_BRANCH_CLEARING),
    ("3000", "حقوق الملكية", "equity", None, True, None),
    ("3100", "رأس المال", "equity", "3000", False, None),
    ("3200", "الأرباح والخسائر المرحلة", "equity", "3000", False, AccountRole.Role.RETAINED_EARNINGS),
    ("4000", "الإيرادات", "revenue", None, True, None),
    ("4100", "إيرادات النشاط الرئيسي", "revenue", "4000", True, None),
    ("4110", "__ACTIVITY_REVENUE__", "revenue", "4100", False, AccountRole.Role.SALES),
    ("4120", "مردودات وخصومات المبيعات", "revenue", "4100", False, AccountRole.Role.SALES_RETURNS),
    ("5000", "التكاليف المباشرة", "expense", None, True, None),
    ("5100", "تكاليف النشاط المباشرة", "expense", "5000", True, None),
    ("5110", "__ACTIVITY_COST__", "expense", "5100", False, AccountRole.Role.COGS),
    ("6000", "المصروفات", "expense", None, True, None),
    ("6100", "المصروفات التشغيلية والإدارية", "expense", "6000", True, None),
    ("6110", "مصروفات تشغيلية عامة", "expense", "6100", False, AccountRole.Role.OPERATING_EXPENSE),
    ("6120", "مصروف المرتبات والأجور", "expense", "6100", False, AccountRole.Role.PAYROLL_EXPENSE),
    ("6130", "مصروف حصة الشركة في التأمينات", "expense", "6100", False, AccountRole.Role.EMPLOYER_INSURANCE_EXPENSE),
    ("6140", "خسائر تسوية المخزون", "expense", "6100", False, AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS),
    ("6150", "خسائر فروق عملة محققة", "expense", "6100", False, AccountRole.Role.REALIZED_FX_LOSS),
    ("6160", "خسائر فروق عملة غير محققة", "expense", "6100", False, AccountRole.Role.UNREALIZED_FX_LOSS),
    ("7000", "إيرادات ومكاسب أخرى", "revenue", None, True, None),
    ("7100", "أرباح تسوية المخزون", "revenue", "7000", False, AccountRole.Role.INVENTORY_ADJUSTMENT_GAIN),
    ("7200", "أرباح فروق عملة محققة", "revenue", "7000", False, AccountRole.Role.REALIZED_FX_GAIN),
    ("7210", "أرباح فروق عملة غير محققة", "revenue", "7000", False, AccountRole.Role.UNREALIZED_FX_GAIN),
]


def build_activity_template(activity):
    if activity not in ACTIVITY_LABELS:
        raise ValueError("النشاط المحدد غير مدعوم.")
    revenue_name, cost_name = ACTIVITY_ACCOUNT_NAMES[activity]
    rows = []
    for code, name, nature, parent_code, is_group, role in COMMON_ACCOUNTS:
        if name == "__ACTIVITY_REVENUE__":
            name = revenue_name
        elif name == "__ACTIVITY_COST__":
            name = cost_name
        rows.append({
            "code": code,
            "name_ar": name,
            "nature": nature,
            "parent_code": parent_code,
            "is_group": is_group,
            "allow_posting": not is_group,
            "role": role,
        })
    return rows


def preview_activity_template(company, activity):
    rows = build_activity_template(activity)
    existing = {
        account.code: account
        for account in ChartOfAccount.objects.filter(company=company)
    }
    conflicts = []
    reusable = []
    for row in rows:
        current = existing.get(row["code"])
        if not current:
            continue
        actual_parent_code = current.parent.code if current.parent_id else None
        matches = (
            current.name_ar == row["name_ar"]
            and current.nature == row["nature"]
            and current.is_group == row["is_group"]
            and current.allow_posting == row["allow_posting"]
            and actual_parent_code == row["parent_code"]
        )
        if matches:
            reusable.append(row["code"])
        else:
            conflicts.append({
                "code": row["code"],
                "existing_name": current.name_ar,
                "template_name": row["name_ar"],
            })
    return rows, reusable, conflicts


def apply_activity_template(company, activity):
    rows, reusable, conflicts = preview_activity_template(company, activity)
    if conflicts:
        return {
            "created_accounts": [],
            "reused_accounts": reusable,
            "role_assignments_created": [],
            "role_conflicts": [],
            "code_conflicts": conflicts,
        }

    accounts_by_code = {
        account.code: account
        for account in ChartOfAccount.objects.filter(company=company)
    }
    created = []
    for row in rows:
        account = accounts_by_code.get(row["code"])
        if account is None:
            parent = accounts_by_code.get(row["parent_code"]) if row["parent_code"] else None
            account = ChartOfAccount.objects.create(
                company=company,
                code=row["code"],
                name_ar=row["name_ar"],
                parent=parent,
                nature=row["nature"],
                is_group=row["is_group"],
                allow_posting=row["allow_posting"],
                is_active=True,
            )
            accounts_by_code[account.code] = account
            created.append(account.code)

    role_created = []
    role_conflicts = []
    for row in rows:
        role = row["role"]
        if not role:
            continue
        account = accounts_by_code[row["code"]]
        current = AccountRole.objects.filter(company=company, role=role).first()
        if current:
            if current.account_id != account.pk:
                role_conflicts.append({
                    "role": role,
                    "existing_account_code": current.account.code,
                    "template_account_code": account.code,
                })
            continue
        AccountRole.objects.create(company=company, role=role, account=account)
        role_created.append(role)

    return {
        "created_accounts": created,
        "reused_accounts": reusable,
        "role_assignments_created": role_created,
        "role_conflicts": role_conflicts,
        "code_conflicts": [],
    }
