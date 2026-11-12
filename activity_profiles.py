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
            """INSERT OR IGNORE INTO activity_profiles
               (code,name,base_activity,description,account_roles_json,
                posting_accounts_json,tax_roles_json,is_builtin,active)
               VALUES(?,?,?,?,?,?,?,1,1)""",
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
    return errors
