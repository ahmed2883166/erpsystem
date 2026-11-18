"""Company-level activity pack assignments and explicit posting context.

This module is an additive foundation: it stores enabled packs and per-pack
posting overrides without rewriting the legacy company activity setting or
the existing chart of accounts. Callers must pass a document's activity code
explicitly when posting; legacy documents retain the old active-profile path.
"""
import json

from db_compat import execute_script, table_exists
from db_sql import execute_compat
from chart_templates import CHARTS
from activity_profiles import (
    initialize_activity_profiles_schema,
    get_active_activity_profile,
)

POSTING_ACCOUNT_TYPES = {
    "ar": "asset", "revenue": "revenue", "vat": "liability",
    "inventory": "asset", "cogs": "expense",
    "purchase_inventory": "asset", "supplier_ap": "liability",
    "input_vat": "asset", "grni": "liability",
    "stocktake_shortage": "expense", "stocktake_surplus": "expense",
    "interbranch_transfer": "asset", "stock_scrap_expense": "expense",
    "internal_stock_use": "expense",
}
ACCOUNT_ROLE_TYPES = {
    "cash": "asset", "receivables": "asset", "contract_assets": "asset",
    "payables": "liability", "grni": "liability", "customer_advances": "liability",
    "inventory": "asset", "direct_cost": "expense", "retained_earnings": "equity",
    "interbranch_transfers": "asset",
}
TAX_ROLE_TYPES = {"output_vat": "liability", "input_vat": "asset"}
ACTIVITY_ROLE_TYPES = {**ACCOUNT_ROLE_TYPES, **POSTING_ACCOUNT_TYPES, **TAX_ROLE_TYPES}


def initialize_company_activity_schema(conn):
    """Create additive company activity tables; never alter legacy ledgers."""
    initialize_activity_profiles_schema(conn)
    execute_script(conn, """
    CREATE TABLE IF NOT EXISTS company_activity_packs (
      activity_code TEXT PRIMARY KEY,
      profile_code TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
      is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uq_company_activity_primary
      ON company_activity_packs(is_primary) WHERE is_primary=1;
    CREATE TABLE IF NOT EXISTS company_activity_account_map (
      activity_code TEXT NOT NULL,
      role TEXT NOT NULL,
      account_code TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(activity_code, role),
      FOREIGN KEY(activity_code) REFERENCES company_activity_packs(activity_code)
    );
    """)


def prepare_company_activity(conn, activity_code, profile_code=None):
    """Create a disabled activity pack so its roles can be mapped before activation."""
    activity_code = str(activity_code or "").strip().lower()
    if activity_code not in CHARTS:
        raise ValueError("النشاط غير مدعوم.")
    initialize_company_activity_schema(conn)
    profile_code = str(profile_code or activity_code).strip().lower()
    profile = execute_compat(
        conn, "SELECT code,base_activity,active FROM activity_profiles WHERE code=?",
        (profile_code,),
    ).fetchone()
    if not profile or not profile["active"] or profile["base_activity"] != activity_code:
        raise ValueError("ملف النشاط غير موجود أو غير نشط أو لا يطابق النشاط.")
    execute_compat(conn, """
      INSERT INTO company_activity_packs(activity_code,profile_code,enabled,is_primary)
      VALUES(?,?,0,0)
      ON CONFLICT(activity_code) DO UPDATE SET
        profile_code=excluded.profile_code,updated_at=CURRENT_TIMESTAMP
    """, (activity_code, profile_code))
    return activity_code


def _unresolved_role_conflicts(conn, report):
    """Return missing/overlapping role mappings for every conflicting account code."""
    unresolved = []
    for conflict in report["role_conflicts"]:
        family_codes = {}
        missing = []
        for assignment in conflict["assignments"]:
            activity = assignment["activity"]
            role = assignment["role"]
            family = assignment["family"]
            expected = ACTIVITY_ROLE_TYPES.get(role)
            mapping = execute_compat(
                conn,
                "SELECT account_code FROM company_activity_account_map WHERE activity_code=? AND role=?",
                (activity, role),
            ).fetchone()
            if not expected or not mapping:
                missing.append(f"{activity}.{role}")
                continue
            code = mapping["account_code"]
            account = execute_compat(
                conn, "SELECT account_type,is_group,active FROM accounts WHERE code=?", (code,)
            ).fetchone()
            if not account or account["account_type"] != expected or account["is_group"] or not account["active"]:
                missing.append(f"{activity}.{role}({code})")
                continue
            family_codes.setdefault(family, set()).add(str(code))
        if missing:
            unresolved.extend(missing)
            continue
        families = sorted(family_codes)
        for index, left in enumerate(families):
            for right in families[index + 1:]:
                overlap = family_codes[left] & family_codes[right]
                if overlap:
                    unresolved.append(
                        f"{conflict['account_code']}:{left}/{right}→{','.join(sorted(overlap))}"
                    )
    return sorted(set(unresolved))


def enable_company_activity(conn, activity_code, profile_code=None, primary=False):
    """Enable one supported pack; primary changes are explicit and reversible."""
    activity_code = str(activity_code or "").strip().lower()
    if activity_code not in CHARTS:
        raise ValueError("النشاط غير مدعوم.")
    initialize_company_activity_schema(conn)
    profile_code = str(profile_code or activity_code).strip().lower()
    profile = execute_compat(
        conn, "SELECT code,base_activity,active FROM activity_profiles WHERE code=?",
        (profile_code,),
    ).fetchone()
    if not profile or not profile["active"]:
        raise ValueError("ملف النشاط غير موجود أو غير نشط.")
    if profile["base_activity"] != activity_code:
        raise ValueError("ملف النشاط لا يطابق النشاط المطلوب.")
    # A conflicting secondary pack must be prepared and mapped explicitly
    # before activation. The existing company chart is never reseeded.
    existing = [
        row["activity_code"] for row in execute_compat(
            conn,
            "SELECT activity_code FROM company_activity_packs WHERE enabled=1 AND activity_code<>?",
            (activity_code,),
        ).fetchall()
    ]
    if existing:
        from industry_packs import analyze_activity_pack_compatibility

        report = analyze_activity_pack_compatibility(existing + [activity_code])
        unresolved = _unresolved_role_conflicts(conn, report)
        if unresolved:
            preview = "، ".join(unresolved[:8])
            raise ValueError(
                "لا يمكن تفعيل النشاط قبل ربط الأدوار المتعارضة بحسابات تفصيلية مختلفة. "
                f"الأدوار غير المحسومة: {preview}"
            )
    if primary:
        execute_compat(conn, "UPDATE company_activity_packs SET is_primary=0 WHERE is_primary=1")
    execute_compat(conn, """
      INSERT INTO company_activity_packs(activity_code,profile_code,enabled,is_primary)
      VALUES(?,?,1,?)
      ON CONFLICT(activity_code) DO UPDATE SET
        profile_code=excluded.profile_code, enabled=1,
        is_primary=excluded.is_primary, updated_at=CURRENT_TIMESTAMP
    """, (activity_code, profile_code, 1 if primary else 0))
    return activity_code


def disable_company_activity(conn, activity_code):
    """Disable a pack without deleting its mapping history."""
    row = execute_compat(
        conn, "SELECT is_primary FROM company_activity_packs WHERE activity_code=?",
        (activity_code,),
    ).fetchone()
    if not row:
        raise ValueError("النشاط غير مفعّل للشركة.")
    if row["is_primary"]:
        raise ValueError("لا يمكن تعطيل النشاط الأساسي قبل تعيين نشاط أساسي بديل.")
    execute_compat(
        conn, "UPDATE company_activity_packs SET enabled=0,updated_at=CURRENT_TIMESTAMP WHERE activity_code=?",
        (activity_code,),
    )


def list_company_activities(conn):
    initialize_company_activity_schema(conn)
    return [dict(row) for row in execute_compat(
        conn, """SELECT activity_code,profile_code,enabled,is_primary
                 FROM company_activity_packs ORDER BY is_primary DESC,activity_code"""
    ).fetchall()]


def set_activity_posting_account(conn, activity_code, role, account_code):
    """Set a validated posting account override for one enabled activity."""
    if role not in ACTIVITY_ROLE_TYPES:
        raise ValueError("دور حساب النشاط غير مدعوم.")
    pack = execute_compat(
        conn, "SELECT enabled FROM company_activity_packs WHERE activity_code=?",
        (activity_code,),
    ).fetchone()
    if not pack:
        raise ValueError("جهّز حزمة النشاط أولًا قبل ربط حساباتها.")
    account = execute_compat(
        conn, "SELECT account_type,is_group,active FROM accounts WHERE code=?",
        (str(account_code),),
    ).fetchone()
    if not account:
        raise ValueError(f"الحساب {account_code} غير موجود.")
    if account["is_group"] or not account["active"]:
        raise ValueError(f"الحساب {account_code} تجميعي أو غير نشط.")
    expected = ACTIVITY_ROLE_TYPES[role]
    if account["account_type"] != expected:
        raise ValueError(f"نوع الحساب {account_code} لا يتوافق مع الدور {role}; المتوقع {expected}.")
    execute_compat(conn, """
      INSERT INTO company_activity_account_map(activity_code,role,account_code)
      VALUES(?,?,?)
      ON CONFLICT(activity_code,role) DO UPDATE SET
        account_code=excluded.account_code,updated_at=CURRENT_TIMESTAMP
    """, (activity_code, role, str(account_code)))


def resolve_activity_profile(conn, activity_code=None, *, allow_disabled=False):
    """Resolve an activity profile; disabled packs are opt-in for historical reversals only."""
    if not activity_code:
        return get_active_activity_profile(conn)
    enabled_clause = "" if allow_disabled else "AND cap.enabled=1"
    row = execute_compat(conn, f"""
      SELECT p.* FROM company_activity_packs cap
      JOIN activity_profiles p ON p.code=cap.profile_code
      WHERE cap.activity_code=? {enabled_clause} AND p.active=1
    """, (str(activity_code).strip().lower(),)).fetchone()
    if not row:
        raise ValueError("نشاط المستند غير مفعّل للشركة أو ملفه غير نشط.")
    profile = dict(row)
    for source, target in (
        ("account_roles_json", "account_roles"),
        ("posting_accounts_json", "posting_accounts"),
        ("tax_roles_json", "tax_roles"),
    ):
        try:
            profile[target] = json.loads(profile.pop(source))
        except (TypeError, ValueError):
            raise ValueError("إعدادات ملف النشاط تالفة؛ أوقف الترحيل وراجع الملف.")
    overrides = execute_compat(
        conn, "SELECT role,account_code FROM company_activity_account_map WHERE activity_code=?",
        (str(activity_code).strip().lower(),),
    ).fetchall()
    for override in overrides:
        role, code = override["role"], override["account_code"]
        expected = ACTIVITY_ROLE_TYPES.get(role)
        account = execute_compat(
            conn, "SELECT account_type,is_group,active FROM accounts WHERE code=?", (code,)
        ).fetchone()
        if not expected or not account or account["account_type"] != expected or account["is_group"] or not account["active"]:
            raise ValueError(f"ربط حساب النشاط للدور {role} غير صالح؛ أوقف الترحيل.")
        if role in POSTING_ACCOUNT_TYPES:
            profile["posting_accounts"][role] = code
        if role in ACCOUNT_ROLE_TYPES:
            profile["account_roles"][role] = [code]
        if role in TAX_ROLE_TYPES:
            profile["tax_roles"][role] = code
    profile["company_activity_code"] = str(activity_code).strip().lower()
    return profile


def resolve_activity_posting_account(conn, role, activity_code=None):
    profile = resolve_activity_profile(conn, activity_code)
    code = profile.get("posting_accounts", {}).get(role) or profile.get("tax_roles", {}).get(role)
    if not code:
        raise ValueError(f"لا يوجد حساب ترحيل مضبوط للدور {role}.")
    return code
