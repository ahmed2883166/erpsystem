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
    "input_vat": "asset", "stocktake_shortage": "expense",
    "stocktake_surplus": "expense",
}


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
    if role not in POSTING_ACCOUNT_TYPES:
        raise ValueError("دور حساب الترحيل غير مدعوم.")
    pack = execute_compat(
        conn, "SELECT enabled FROM company_activity_packs WHERE activity_code=?",
        (activity_code,),
    ).fetchone()
    if not pack or not pack["enabled"]:
        raise ValueError("يجب تفعيل النشاط قبل ربط حساباته.")
    account = execute_compat(
        conn, "SELECT account_type,is_group,active FROM accounts WHERE code=?",
        (str(account_code),),
    ).fetchone()
    if not account:
        raise ValueError(f"الحساب {account_code} غير موجود.")
    if account["is_group"] or not account["active"]:
        raise ValueError(f"الحساب {account_code} تجميعي أو غير نشط.")
    expected = POSTING_ACCOUNT_TYPES[role]
    if account["account_type"] != expected:
        raise ValueError(f"نوع الحساب {account_code} لا يتوافق مع الدور {role}; المتوقع {expected}.")
    execute_compat(conn, """
      INSERT INTO company_activity_account_map(activity_code,role,account_code)
      VALUES(?,?,?)
      ON CONFLICT(activity_code,role) DO UPDATE SET
        account_code=excluded.account_code,updated_at=CURRENT_TIMESTAMP
    """, (activity_code, role, str(account_code)))


def resolve_activity_profile(conn, activity_code=None):
    """Resolve explicit document context, falling back only for legacy documents."""
    if not activity_code:
        return get_active_activity_profile(conn)
    row = execute_compat(conn, """
      SELECT p.* FROM company_activity_packs cap
      JOIN activity_profiles p ON p.code=cap.profile_code
      WHERE cap.activity_code=? AND cap.enabled=1 AND p.active=1
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
        expected = POSTING_ACCOUNT_TYPES.get(role)
        account = execute_compat(
            conn, "SELECT account_type,is_group,active FROM accounts WHERE code=?", (code,)
        ).fetchone()
        if not expected or not account or account["account_type"] != expected or account["is_group"] or not account["active"]:
            raise ValueError(f"ربط حساب النشاط للدور {role} غير صالح؛ أوقف الترحيل.")
        profile["posting_accounts"][role] = code
    profile["company_activity_code"] = str(activity_code).strip().lower()
    return profile


def resolve_activity_posting_account(conn, role, activity_code=None):
    profile = resolve_activity_profile(conn, activity_code)
    code = profile.get("posting_accounts", {}).get(role)
    if not code:
        raise ValueError(f"لا يوجد حساب ترحيل مضبوط للدور {role}.")
    return code
