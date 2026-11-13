"""Activity profile registry for NEXUS ERP.

Profiles describe operational configuration without owning or replacing the
company chart of accounts. Built-in profiles are immutable; custom profiles
inherit a supported base activity and can be selected independently.
"""
import json
import re
from chart_templates import (
    ACTIVITY_LABELS, ACTIVITY_ACCOUNT_ROLES, ACTIVITY_POSTING_ACCOUNTS,
    ACTIVITY_TAX_ACCOUNT_ROLES, CHARTS,
)

PROFILE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

SPECIALIZED_ACTIVITY_PRESETS = {
    "transport_logistics": {"name": "النقل والخدمات اللوجستية", "base_activity": "service",
        "description": "خدمات النقل والوساطة اللوجستية والشحن؛ يهيئ خرائط الخدمات والعملاء والموردين والضرائب، مع استكمال حسابات العهد والناقلين حسب دليل الشركة."},
    "construction_contracting": {"name": "المقاولات والإنشاءات", "base_activity": "service",
        "description": "مقاولات ومشروعات؛ يبدأ من النشاط الخدمي ويحتاج ربط حسابات تكاليف المشروعات والأعمال تحت التنفيذ والمستخلصات والاحتجازات."},
    "hotel_tourism": {"name": "الفنادق والسياحة", "base_activity": "service",
        "description": "الفنادق والخدمات السياحية؛ يحتاج تخصيص مراكز تكلفة الغرف والمطاعم والخدمات والإيرادات المقدمة."},
    "financial_leasing": {"name": "التأجير والتمويل التأجيري", "base_activity": "service",
        "description": "التأجير التشغيلي والتمويلي؛ يحتاج إعداد حسابات أصول حق الاستخدام والتزامات الإيجار والإيراد التمويلي وفق طبيعة العقد."},
    "wholesale_distribution": {"name": "تجارة الجملة والتوزيع", "base_activity": "commercial",
        "description": "تجارة الجملة والتوزيع؛ يرث حسابات المخزون والمبيعات والمشتريات مع إمكانية تخصيص الخصومات والعمولات ومراكز التوزيع."},
    "hospital_clinic": {"name": "المستشفيات والمراكز الطبية", "base_activity": "medical",
        "description": "الرعاية الصحية والمطالبات التأمينية والمخزون الطبي؛ يتطلب تخصيص حسابات شركات التأمين والمطالبات والمخزون الدوائي."},
}


def initialize_activity_profiles_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS activity_profiles (
      id INTEGER PRIMARY KEY,
      code TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      base_activity TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      account_roles_json TEXT NOT NULL,
      posting_accounts_json TEXT NOT NULL,
      tax_roles_json TEXT NOT NULL,
      is_builtin INTEGER NOT NULL DEFAULT 0 CHECK(is_builtin IN (0,1)),
      active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_activity_profiles_active
      ON activity_profiles(active,name);
    """)
    for activity, label in ACTIVITY_LABELS.items():
        roles = ACTIVITY_ACCOUNT_ROLES.get(activity, {})
        posting = ACTIVITY_POSTING_ACCOUNTS.get(activity, {})
        tax_roles = ACTIVITY_TAX_ACCOUNT_ROLES.get(activity, {})
        conn.execute(
            """INSERT INTO activity_profiles
               (code,name,base_activity,description,account_roles_json,
                posting_accounts_json,tax_roles_json,is_builtin,active)
               VALUES(?,?,?,?,?,?,?,1,1)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name,
                 base_activity=excluded.base_activity,
                 description=excluded.description,
                 account_roles_json=excluded.account_roles_json,
                 posting_accounts_json=excluded.posting_accounts_json,
                 tax_roles_json=excluded.tax_roles_json,
                 updated_at=CURRENT_TIMESTAMP
               WHERE activity_profiles.is_builtin=1""",
            (activity, label, activity, "ملف نشاط أساسي مدمج مع النظام.",
             json.dumps(roles, ensure_ascii=False, sort_keys=True),
             json.dumps(posting, ensure_ascii=False, sort_keys=True),
             json.dumps(tax_roles, ensure_ascii=False, sort_keys=True)),
        )


def list_activity_profiles(conn, include_inactive=False):
    where = "" if include_inactive else "WHERE active=1"
    rows = conn.execute(
        f"SELECT * FROM activity_profiles {where} ORDER BY is_builtin DESC,name,code"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for field, target in (
            ("account_roles_json", "account_roles"),
            ("posting_accounts_json", "posting_accounts"),
            ("tax_roles_json", "tax_roles"),
        ):
            try:
                item[target] = json.loads(item.pop(field))
            except (TypeError, ValueError):
                raise ValueError(f"ملف النشاط {item.get('code')} يحتوي إعدادات غير صالحة.")
        if "customer_advances" not in item["account_roles"]:
            item["account_roles"]["customer_advances"] = list(
                ACTIVITY_ACCOUNT_ROLES.get(item["base_activity"], {}).get("customer_advances", ("2160",))
            )
        result.append(item)
    return result


def create_activity_profile(conn, code, name, base_activity, description=""):
    code = str(code or "").strip().lower()
    name = str(name or "").strip()
    description = str(description or "").strip()
    if not PROFILE_CODE.fullmatch(code):
        raise ValueError("رمز الملف يجب أن يبدأ بحرف إنجليزي ويحتوي حروفًا صغيرة أو أرقامًا أو _ فقط.")
    if not name or len(name) > 100 or len(description) > 500:
        raise ValueError("اسم الملف مطلوب (حتى 100 حرف)، والوصف لا يتجاوز 500 حرف.")
    if base_activity not in CHARTS:
        raise ValueError("النشاط الأساسي غير مدعوم.")
    roles = ACTIVITY_ACCOUNT_ROLES[base_activity]
    posting = ACTIVITY_POSTING_ACCOUNTS[base_activity]
    tax_roles = ACTIVITY_TAX_ACCOUNT_ROLES[base_activity]
    cur = conn.execute(
        """INSERT INTO activity_profiles
           (code,name,base_activity,description,account_roles_json,
            posting_accounts_json,tax_roles_json,is_builtin,active)
           VALUES(?,?,?,?,?,?,?,0,1)""",
        (code,name,base_activity,description,
         json.dumps(roles,ensure_ascii=False,sort_keys=True),
         json.dumps(posting,ensure_ascii=False,sort_keys=True),
         json.dumps(tax_roles,ensure_ascii=False,sort_keys=True)),
    )
    return cur.lastrowid



def create_specialized_activity_profile(conn, preset_code):
    """Create a curated specialized profile once, inheriting a safe base mapping."""
    preset = SPECIALIZED_ACTIVITY_PRESETS.get(str(preset_code or "").strip())
    if not preset:
        raise ValueError("النشاط المتخصص المطلوب غير معروف.")
    existing = conn.execute(
        "SELECT id FROM activity_profiles WHERE code=?", (preset_code,)
    ).fetchone()
    if existing:
        return existing["id"]
    return create_activity_profile(
        conn, preset_code, preset["name"], preset["base_activity"], preset["description"]
    )


def set_activity_profile_active(conn, profile_id, active):
    row = conn.execute(
        "SELECT code,is_builtin FROM activity_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if not row:
        raise ValueError("ملف النشاط غير موجود.")
    if row["is_builtin"]:
        raise ValueError("لا يمكن تعطيل ملفات الأنشطة الأساسية المدمجة.")
    conn.execute(
        "UPDATE activity_profiles SET active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (1 if active else 0, profile_id),
    )


def validate_activity_profile(conn, profile):
    """Check every configured account code against the current company ledger."""
    if profile["base_activity"] not in CHARTS:
        return ["النشاط الأساسي غير معروف."]
    required = set()
    for mapping_name in ("account_roles", "posting_accounts", "tax_roles"):
        mapping = profile.get(mapping_name) or {}
        if not isinstance(mapping, dict):
            return [f"إعداد {mapping_name} ليس قاموسًا صالحًا."]
        for value in mapping.values():
            values = value if isinstance(value, (list, tuple)) else (value,)
            required.update(str(code) for code in values if code not in (None, ""))
    existing = {row["code"]: row for row in conn.execute(
        "SELECT code,account_type,is_group,active FROM accounts"
    )}
    errors = []
    for code in sorted(required):
        row = existing.get(code)
        if not row:
            errors.append(f"الحساب {code} غير موجود في دليل حسابات الشركة.")
        elif row["is_group"] or not row["active"]:
            errors.append(f"الحساب {code} تجميعي أو غير نشط ولا يصلح للترحيل.")
    expected_types = {
        "posting_accounts": {
            "ar": "asset", "revenue": "revenue", "vat": "liability",
            "inventory": "asset", "cogs": "expense",
            "purchase_inventory": "asset", "supplier_ap": "liability",
            "input_vat": "asset",
        },
        "tax_roles": {"output_vat": "liability", "input_vat": "asset"},
        "account_roles": {
            "cash": "asset", "receivables": "asset", "payables": "liability",
            "inventory": "asset", "quick_assets": "asset", "current_assets": "asset",
            "current_liabilities": "liability", "customer_advances": "liability", "direct_cost": "expense",
            "retained_earnings": "equity",
        },
    }
    for mapping_name, types in expected_types.items():
        for role, expected_type in types.items():
            mapping = profile.get(mapping_name) or {}
            value = mapping.get(role)
            values = value if isinstance(value, (list, tuple)) else (value,)
            for code in values:
                if code in (None, ""):
                    continue
                row = existing.get(str(code))
                if row and row["account_type"] != expected_type:
                    errors.append(
                        f"الحساب {code} نوعه {row['account_type']} ولا يتوافق مع الدور {role} ({expected_type})."
                    )
    return errors



def get_active_activity_profile(conn):
    """Resolve the selected profile; legacy databases safely fall back to built-ins."""
    settings_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_settings'"
    ).fetchone()
    settings = {}
    if settings_table:
        settings = {row["setting_key"]: row["setting_value"] for row in conn.execute(
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity_profile_code','activity')"
        )}
    code = settings.get("activity_profile_code") or settings.get("activity") or "commercial"
    profile_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_profiles'"
    ).fetchone()
    row = conn.execute(
        "SELECT * FROM activity_profiles WHERE code=? AND active=1", (code,)
    ).fetchone() if profile_table else None
    if not row:
        activity = settings.get("activity") or code
        from chart_templates import ACTIVITY_POSTING_ACCOUNTS, ACTIVITY_TAX_ACCOUNT_ROLES
        if activity not in ACTIVITY_POSTING_ACCOUNTS:
            raise ValueError("ملف النشاط الحالي غير معروف أو غير نشط.")
        return {
            "code": activity, "base_activity": activity,
            "account_roles": {k: list(v) for k,v in ACTIVITY_ACCOUNT_ROLES[activity].items()},
            "posting_accounts": dict(ACTIVITY_POSTING_ACCOUNTS[activity]),
            "tax_roles": dict(ACTIVITY_TAX_ACCOUNT_ROLES[activity]),
        }
    profile = dict(row)
    for source, target in (
        ("account_roles_json", "account_roles"),
        ("posting_accounts_json", "posting_accounts"),
        ("tax_roles_json", "tax_roles"),
    ):
        try:
            profile[target] = json.loads(profile.pop(source))
        except (TypeError, ValueError):
            raise ValueError("إعدادات ملف النشاط الحالي تالفة؛ أوقف الترحيل وراجع الملف.")
    if "customer_advances" not in profile["account_roles"]:
        profile["account_roles"]["customer_advances"] = list(
            ACTIVITY_ACCOUNT_ROLES.get(profile["base_activity"], {}).get("customer_advances", ("2160",))
        )
    return profile


def active_posting_accounts(conn):
    return get_active_activity_profile(conn)["posting_accounts"]


def active_tax_roles(conn):
    return get_active_activity_profile(conn)["tax_roles"]


def active_account_role_ids(conn):
    profile = get_active_activity_profile(conn)
    codes = profile["account_roles"]
    result = {}
    account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    group_expr = "COALESCE(is_group,0)" if "is_group" in account_columns else "0"
    active_expr = "COALESCE(active,1)" if "active" in account_columns else "1"
    for role, values in codes.items():
        ids = set()
        for code in values if isinstance(values, list) else [values]:
            row = conn.execute(
                f"SELECT id,{group_expr} is_group,{active_expr} active FROM accounts WHERE code=?",
                (str(code),)
            ).fetchone()
            if row and not row["is_group"] and row["active"]:
                ids.add(row["id"])
        result[role] = ids
    return result



def update_custom_activity_profile(conn, profile_id, account_roles_json,
                                   posting_accounts_json, tax_roles_json):
    """Update mappings for a custom profile after strict structure/type validation."""
    row = conn.execute(
        "SELECT * FROM activity_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if not row:
        raise ValueError("ملف النشاط غير موجود.")
    if row["is_builtin"]:
        raise ValueError("لا يمكن تعديل ملفات الأنشطة الأساسية المدمجة.")
    try:
        account_roles = json.loads(account_roles_json)
        posting_accounts = json.loads(posting_accounts_json)
        tax_roles = json.loads(tax_roles_json)
    except (TypeError, ValueError):
        raise ValueError("صيغة JSON غير صحيحة في أحد إعدادات الحسابات.")
    from chart_templates import ACTIVITY_ACCOUNT_ROLES, ACTIVITY_POSTING_ACCOUNTS, ACTIVITY_TAX_ACCOUNT_ROLES
    base = row["base_activity"]
    if base not in ACTIVITY_POSTING_ACCOUNTS:
        raise ValueError("النشاط الأساسي غير مدعوم.")
    expected = (
        (account_roles, ACTIVITY_ACCOUNT_ROLES[base], "أدوار الحسابات"),
        (posting_accounts, ACTIVITY_POSTING_ACCOUNTS[base], "حسابات الترحيل"),
        (tax_roles, ACTIVITY_TAX_ACCOUNT_ROLES[base], "حسابات الضرائب"),
    )
    for mapping, template, label in expected:
        if not isinstance(mapping, dict) or set(mapping) != set(template):
            raise ValueError(f"مفاتيح {label} يجب أن تطابق مفاتيح النشاط الأساسي دون زيادة أو نقص.")
    for role, values in account_roles.items():
        if not isinstance(values, list):
            raise ValueError(f"الدور {role} يجب أن يكون قائمة أكواد حسابات.")
        if not values and ACTIVITY_ACCOUNT_ROLES[base].get(role):
            raise ValueError(f"الدور {role} لا يجوز أن يكون فارغًا لهذا النشاط.")
    posting_types = {
        "ar": "asset", "revenue": "revenue", "vat": "liability",
        "inventory": "asset", "cogs": "expense", "purchase_inventory": "asset",
        "supplier_ap": "liability", "input_vat": "asset",
    }
    tax_types = {"output_vat": "liability", "input_vat": "asset"}
    role_types = {
        "cash": "asset", "receivables": "asset", "payables": "liability",
        "inventory": "asset", "quick_assets": "asset", "current_assets": "asset",
        "current_liabilities": "liability", "customer_advances": "liability", "direct_cost": "expense",
        "retained_earnings": "equity",
    }
    accounts = {r["code"]: r for r in conn.execute(
        "SELECT code,account_type,is_group,active FROM accounts"
    )}
    def check_code(code, expected_type, label):
        if code in (None, ""):
            return
        account = accounts.get(str(code))
        if not account:
            raise ValueError(f"الحساب {code} في {label} غير موجود بدليل الحسابات.")
        if account["is_group"] or not account["active"]:
            raise ValueError(f"الحساب {code} في {label} تجميعي أو غير نشط.")
        if expected_type and account["account_type"] != expected_type:
            raise ValueError(f"نوع الحساب {code} لا يتوافق مع {label}; المتوقع {expected_type}.")
    for role, values in account_roles.items():
        if not isinstance(values, list):
            raise ValueError(f"الدور {role} يجب أن يكون قائمة أكواد حسابات.")
        for code in values:
            check_code(code, role_types.get(role), role)
    for key, code in posting_accounts.items():
        if code is None and key in {"inventory", "cogs", "purchase_inventory"}:
            continue
        if code in (None, ""):
            raise ValueError(f"حساب الترحيل {key} مطلوب.")
        check_code(code, posting_types.get(key), key)
    for key, code in tax_roles.items():
        check_code(code, tax_types.get(key), key)
    conn.execute(
        """UPDATE activity_profiles
           SET account_roles_json=?,posting_accounts_json=?,tax_roles_json=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (json.dumps(account_roles,ensure_ascii=False,sort_keys=True),
         json.dumps(posting_accounts,ensure_ascii=False,sort_keys=True),
         json.dumps(tax_roles,ensure_ascii=False,sort_keys=True), profile_id),
    )
