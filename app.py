import csv
import hashlib
import hmac
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from flask import Flask, Response, flash, redirect, render_template, request, send_file, session, url_for, has_request_context
from chart_templates import CHARTS, ACTIVITY_LABELS, seed_activity_chart, ensure_activity_chart_accounts, migrate_vat_accounts
from stock import stock_bp, initialize_stock_schema
from purchases import purchase_bp, init_purchase_schema
from sales import sales_bp, init_sales_schema
from accounting_core import post_journal, reconciliation_snapshot
from banking import bank_bp, init_bank_schema
from fixed_assets import assets_bp, initialize_assets_schema
from receivables import aging_bp, initialize_aging_schema
from quotations import quotes_bp, initialize_quotes_schema
from tax_center import tax_bp, initialize_tax_schema
from workforce import workforce_bp, initialize_workforce_schema
from payroll_compliance import payroll_compliance_bp, initialize_payroll_compliance_schema
from currency_center import currency_bp, initialize_currency_schema
from party_reports import party_bp
from management_dashboard import cached_management_snapshot, initialize_management_dashboard_schema
from analytic_dimensions import initialize_analytic_dimensions_schema, active_dimension_options, validate_journal_dimensions
from financial_planning import planning_bp, initialize_planning_schema
from activity_profiles import (initialize_activity_profiles_schema, list_activity_profiles, create_activity_profile, set_activity_profile_active, validate_activity_profile, update_custom_activity_profile)
from report_exports import register_report_exports
from licensing import get_license_status, activate_license
from database import connect_database, enable_wal, fetch_page
from app_paths import DATA_DIR, DB_PATH, BACKUP_DIR
from backup_scheduler import create_backup_archive, next_backup_datetime, start_backup_scheduler
from backup_restore import extract_validated_backup, restore_database_with_rollback
from income_statement import (build_income_statement, normalize_income_statement_period,
                               comparison_period, add_income_statement_comparison,
                               closing_entries_exclusion_sql)

# Development/demo builds run locally without a licensing server. Set NEXUS_DEMO_MODE=0
# explicitly in a commercial deployment to enforce the signed remote licensing flow.
NEXUS_DEMO_MODE = os.environ.get("NEXUS_DEMO_MODE", "1").strip().lower() not in {"0", "false", "no", "off"}

def current_license_status():
    if NEXUS_DEMO_MODE:
        return {"status": "demo", "license_type": "demo", "message": "نسخة تجريبية محلية — لا تحتاج إلى خادم تراخيص."}
    return get_license_status()
from security_controls import login_attempts_exceeded, linked_operational_source


app = Flask(__name__)

@app.template_filter("money")
def format_money(value):
    try:
        amount = Decimal(str(value or "0"))
        return f"{amount:,.2f}"
    except (InvalidOperation, ValueError, TypeError):
        return "0.00"

def _load_secret_key():
    configured = os.environ.get("NEXUS_SECRET_KEY", "").strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError("NEXUS_SECRET_KEY must contain at least 32 characters.")
        return configured
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / ".nexus_secret_key"
    try:
        with key_file.open("x", encoding="ascii") as handle:
            import secrets
            handle.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key_file.read_text(encoding="ascii").strip()

app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("NEXUS_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=1800,
)
from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect(app)

app.register_blueprint(stock_bp)
app.register_blueprint(purchase_bp)
app.register_blueprint(sales_bp)
app.register_blueprint(bank_bp)
app.register_blueprint(assets_bp)
app.register_blueprint(aging_bp)
app.register_blueprint(quotes_bp)
app.register_blueprint(tax_bp)
app.register_blueprint(workforce_bp)
app.register_blueprint(payroll_compliance_bp)
app.register_blueprint(currency_bp)
app.register_blueprint(party_bp)
app.register_blueprint(planning_bp)
# SECRET_KEY and session/CSRF settings are initialized above.

PERMISSIONS = {
    "admin": {"accounts.view", "accounts.manage", "journal.manage", "reports.view", "users.manage", "backup.manage", "import_export.manage", "stock.manage", "stock.view", "purchases.manage", "purchases.view", "sales.manage", "sales.view", "assets.manage", "assets.view", "quotes.manage", "quotes.view", "bank.manage", "bank.view", "tax.manage", "tax.view", "workforce.manage", "workforce.view", "currency.view", "currency.manage", "party.view", "budget.view", "budget.manage", "cashflow.view", "audit.view"},
    "accountant": {"accounts.view", "accounts.manage", "journal.manage", "reports.view", "import_export.manage", "stock.manage", "stock.view", "purchases.manage", "purchases.view", "sales.manage", "sales.view", "assets.manage", "assets.view", "quotes.manage", "quotes.view", "bank.manage", "bank.view", "tax.manage", "tax.view", "workforce.manage", "workforce.view", "currency.view", "currency.manage", "party.view", "budget.view", "budget.manage", "cashflow.view"},
    "viewer": {"accounts.view", "currency.view", "reports.view", "stock.view", "purchases.view", "sales.view", "assets.view", "quotes.view", "bank.view", "tax.view", "workforce.view", "party.view", "budget.view", "cashflow.view"},
}
PERMISSION_LABELS = {
    "accounts.view": "عرض شجرة الحسابات",
    "accounts.manage": "إضافة وتعديل الحسابات",
    "journal.manage": "إنشاء وترحيل القيود اليومية",
    "reports.view": "عرض التقارير ولوحة الإدارة",
    "users.manage": "إدارة المستخدمين والصلاحيات",
    "backup.manage": "النسخ الاحتياطي والاستعادة",
    "import_export.manage": "استيراد وتصدير البيانات",
    "stock.view": "عرض المخزون والتقارير",
    "stock.manage": "إضافة وتعديل حركات المخزون",
    "purchases.view": "عرض المشتريات والموردين",
    "purchases.manage": "إدارة المشتريات وإشعارات المورد",
    "sales.view": "عرض المبيعات والعملاء",
    "sales.manage": "إدارة المبيعات",
    "assets.view": "عرض الأصول الثابتة",
    "assets.manage": "إدارة الأصول الثابتة",
    "quotes.view": "عرض عروض الأسعار",
    "quotes.manage": "إدارة عروض الأسعار",
    "bank.view": "عرض البنوك والمطابقات",
    "bank.manage": "إدارة البنوك والمطابقات",
    "tax.view": "عرض مركز الضرائب",
    "tax.manage": "إدارة مركز الضرائب",
    "workforce.view": "عرض الموظفين والرواتب",
    "workforce.manage": "إدارة الموظفين والرواتب",
    "currency.view": "عرض أسعار العملات وفروق الصرف",
    "currency.manage": "إدارة العملات وفروق الصرف",
    "budget.view": "عرض الموازنات ومقارنة الفعلي",
    "budget.manage": "إنشاء وتعديل الموازنات",
    "cashflow.view": "عرض قائمة التدفقات النقدية",
    "audit.view": "عرض سجل المراجعة والتدقيق",
    "party.view": "عرض كشوف العملاء والموردين",
}
ALL_PERMISSIONS = tuple(PERMISSION_LABELS)
_PERMISSION_IMPLICATIONS = {
    "accounts.manage": "accounts.view", "stock.manage": "stock.view",
    "purchases.manage": "purchases.view", "sales.manage": "sales.view",
    "assets.manage": "assets.view", "quotes.manage": "quotes.view",
    "bank.manage": "bank.view", "tax.manage": "tax.view",
    "workforce.manage": "workforce.view", "currency.manage": "currency.view", "budget.manage": "budget.view",
}

def normalize_user_permissions(values):
    selected = set(values or ()) & set(ALL_PERMISSIONS)
    for manage, view in _PERMISSION_IMPLICATIONS.items():
        if manage in selected:
            selected.add(view)
    return sorted(selected)

def stored_user_permissions(raw, role):
    try:
        values = json.loads(raw) if raw else PERMISSIONS.get(role, set())
    except (TypeError, ValueError, json.JSONDecodeError):
        values = PERMISSIONS.get(role, set())
    if not isinstance(values, (list, tuple, set)):
        values = PERMISSIONS.get(role, set())
    return normalize_user_permissions(values)

register_report_exports(app, DB_PATH, PERMISSIONS)


def db():
    return connect_database(DB_PATH, isolation_level="")


def password_hash(password, salt=None, iterations=310000):
    """Create a salted PBKDF2-SHA256 password hash for new and changed passwords."""
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$" + str(iterations) + "$" + salt.hex() + "$" + digest.hex()


def verify_password(password, stored_hash):
    """Verify current PBKDF2 hashes and legacy SHA-256 hashes during migration."""
    if not stored_hash:
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            algorithm, rounds, salt_hex, expected_hex = stored_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            rounds = int(rounds)
            if rounds < 100000 or rounds > 2000000:
                return False
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(expected_hex)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False
    # Compatibility only: successful legacy logins are upgraded immediately.
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored_hash)


def password_hash_needs_upgrade(stored_hash):
    return not (stored_hash or "").startswith("pbkdf2_sha256$")


def initialize_database():
    DATA_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    enable_wal(DB_PATH)
    connection = db()
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            must_change_password INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
            account_type TEXT NOT NULL, parent_id INTEGER REFERENCES accounts(id),
            is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, reference TEXT, description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'posted', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS journal_lines (
            id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id), debit TEXT NOT NULL DEFAULT '0',
            credit TEXT NOT NULL DEFAULT '0', memo TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
            details TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_settings (
            setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS currencies (
            code TEXT PRIMARY KEY, name_ar TEXT NOT NULL, symbol TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0, is_base INTEGER NOT NULL DEFAULT 0,
            rate_to_base REAL NOT NULL DEFAULT 0, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tax_rates (
            id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
            rate REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL, remote_addr TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounting_periods (
            id INTEGER PRIMARY KEY, period_name TEXT NOT NULL, start_date TEXT NOT NULL,
            end_date TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
    """)
    audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_log)")}
    for column, declaration in (
        ("entity_type", "TEXT"),
        ("entity_id", "TEXT"),
        ("remote_addr", "TEXT"),
        ("outcome", "TEXT NOT NULL DEFAULT 'success'"),
    ):
        if column not in audit_columns:
            connection.execute(f"ALTER TABLE audit_log ADD COLUMN {column} {declaration}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at,id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_user_action ON audit_log(user_id,action,created_at)")
    connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS audit_log_block_update
        BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_log_block_delete
        BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    """)

    user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    if "must_change_password" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    if "permissions_json" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN permissions_json TEXT")
    account_columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "is_group" not in account_columns:
        connection.execute("ALTER TABLE accounts ADD COLUMN is_group INTEGER NOT NULL DEFAULT 0")
    currency_seed = [
        ("SAR","ريال سعودي","ر.س"),("EGP","جنيه مصري","ج.م"),("USD","دولار أمريكي","$"),
        ("EUR","يورو","€"),("AED","درهم إماراتي","د.إ"),("GBP","جنيه إسترليني","£"),
        ("KWD","دينار كويتي","د.ك"),("QAR","ريال قطري","ر.ق")
    ]
    connection.executemany("INSERT OR IGNORE INTO currencies(code,name_ar,symbol,active,is_base,rate_to_base) VALUES(?,?,?,0,0,0)", currency_seed)
    defaults = {"company_name":"NEXUS ERP Demo", "tax_country":"EG", "tax_regime":"EG_VAT",
                "tax_number":"", "activity":"commercial", "base_currency":"EGP", "default_vat_rate":"14", "inventory_cost_method":"average"}
    for key, value in defaults.items():
        connection.execute("INSERT OR IGNORE INTO company_settings(setting_key,setting_value) VALUES(?,?)", (key,value))
    connection.execute("INSERT OR IGNORE INTO currencies(code,name_ar,symbol,active,is_base,rate_to_base) VALUES('EGP','جنيه مصري','ج.م',1,1,1)")
    if not connection.execute("SELECT 1 FROM users WHERE username = 'admin'").fetchone():
        connection.execute("INSERT INTO users(username, full_name, password_hash, role, created_at, must_change_password) VALUES(?,?,?,?,?,1)",
                           ("admin", "مدير النظام", password_hash("admin123"), "admin", now()))
    else:
        admin = connection.execute("SELECT id,password_hash FROM users WHERE username='admin'").fetchone()
        if admin and verify_password("admin123", admin["password_hash"]):
            connection.execute("UPDATE users SET must_change_password=1 WHERE id=?", (admin["id"],))
    if not connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        activity_row = connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
        seed_activity_chart(connection, activity_row["setting_value"] if activity_row else "commercial")
    activity_row = connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
    selected_activity = activity_row["setting_value"] if activity_row else "commercial"
    # Move legacy non-VAT uses of 2130 without changing account IDs or posted journal links.
    migrate_vat_accounts(connection, selected_activity)
    ensure_activity_chart_accounts(connection, selected_activity)
    # SQLite CREATE TABLE IF NOT EXISTS does not change a legacy REAL
    # declaration. Rebuild journal_lines once with TEXT amount columns so
    # existing installations use the same decimal-safe storage as new ones.
    journal_columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(journal_lines)")}
    debit_type = str(journal_columns.get("debit")["type"]).upper() if "debit" in journal_columns else ""
    credit_type = str(journal_columns.get("credit")["type"]).upper() if "credit" in journal_columns else ""
    if debit_type != "TEXT" or credit_type != "TEXT":
        connection.execute("""CREATE TABLE journal_lines_decimal_new(
            id INTEGER PRIMARY KEY,
            entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            debit TEXT NOT NULL DEFAULT '0',
            credit TEXT NOT NULL DEFAULT '0',
            memo TEXT
        )""")
        connection.execute("""INSERT INTO journal_lines_decimal_new(id,entry_id,account_id,debit,credit,memo)
            SELECT id,entry_id,account_id,CAST(debit AS TEXT),CAST(credit AS TEXT),memo
            FROM journal_lines ORDER BY id""")
        connection.execute("DROP TABLE journal_lines")
        connection.execute("ALTER TABLE journal_lines_decimal_new RENAME TO journal_lines")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_entry ON journal_lines(entry_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_account_entry ON journal_lines(account_id,entry_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_entry_account ON journal_lines(entry_id,account_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_journal_entries_entry_date ON journal_entries(entry_date,id)")
    connection.commit()
    connection.close()
    initialize_stock_schema()
    init_purchase_schema()
    init_sales_schema()
    init_bank_schema()
    initialize_assets_schema()
    initialize_aging_schema()
    initialize_quotes_schema()
    initialize_tax_schema()
    initialize_workforce_schema()
    initialize_payroll_compliance_schema()
    initialize_currency_schema()
    dashboard_connection = db()
    activity_row = dashboard_connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
    initialize_management_dashboard_schema(dashboard_connection, activity_row["setting_value"] if activity_row else "commercial")
    initialize_analytic_dimensions_schema(dashboard_connection)
    initialize_planning_schema(dashboard_connection)
    initialize_activity_profiles_schema(dashboard_connection)
    dashboard_connection.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


def log(action, details="", entity_type=None, entity_id=None, outcome="success"):
    connection = db()
    user_id = session.get("user_id") if has_request_context() else None
    remote_addr = request.remote_addr if has_request_context() else None
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        if user_id is not None:
            try:
                actor_exists = connection.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
            except sqlite3.Error:
                actor_exists = None
            if not actor_exists:
                user_id = None
        connection.execute(
            """INSERT INTO audit_log
               (user_id, action, details, created_at, entity_type, entity_id, remote_addr, outcome)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user_id, action, str(details or "")[:1000], timestamp,
             str(entity_type)[:100] if entity_type else None,
             str(entity_id)[:100] if entity_id is not None else None,
             remote_addr, outcome if outcome in {"success", "failure"} else "success"),
        )
        connection.commit()
    finally:
        connection.close()



@app.after_request
def audit_operational_mutations(response):
    endpoint = request.endpoint or ""
    audited_prefixes = (
        "stock.", "purchases.", "sales.", "bank.", "assets.", "aging.",
        "quotes.", "tax.", "workforce.", "payroll_compliance.",
        "currency_center.", "party_reports.", "planning.",
    )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and session.get("user_id") and endpoint.startswith(audited_prefixes):
        try:
            module = endpoint.split(".", 1)[0]
            view_args = request.view_args or {}
            record_id = next(
                (view_args[key] for key in ("id", "entry_id", "invoice_id", "bill_id", "document_id",
                                             "year_id", "run_id", "product_id", "warehouse_id")
                 if view_args.get(key) is not None),
                None,
            )
            log(
                "module_operation",
                f"{request.method} {endpoint} ({response.status_code})",
                entity_type=module,
                entity_id=record_id,
                outcome="success" if response.status_code < 400 else "failure",
            )
        except Exception:
            app.logger.exception("Could not write operational audit event.")
    return response


def requires(permission):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            effective = set(session.get("permissions", PERMISSIONS.get(session.get("role"), set())))
            if permission not in effective:
                flash("لا تملك الصلاحية المطلوبة.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.before_request
def enforce_forced_password_change():
    if session.get("must_change_password"):
        allowed_endpoints = {"change_password", "logout", "static"}
        if (request.endpoint or "") not in allowed_endpoints:
            return redirect(url_for("change_password"))


@app.before_request
def enforce_license():
    if NEXUS_DEMO_MODE:
        return None
    endpoint = request.endpoint or ""
    allowed = {"static", "login", "logout", "license_page", "about", "create_backup", "export_data", "change_password"}
    if endpoint in allowed:
        return None
    status = current_license_status()
    if status["status"] not in {"trial", "licensed"}:
        if request.path.startswith("/api/") or request.is_json:
            return {"error":"license_required","message":status.get("message","انتهت صلاحية الترخيص.")}, 402
        return redirect(url_for("license_page"))
    return None


@app.context_processor
def shared_context():
    return {"permissions":set(session.get("permissions", PERMISSIONS.get(session.get("role"),set()))),"year":datetime.now().year,"license_info":current_license_status()}


@app.route("/license", methods=["GET", "POST"])
def license_page():
    if request.method == "POST":
        try:
            activate_license(request.form.get("activation_code",""))
            flash("تم تفعيل NEXUS ERP بنجاح.","success")
            return redirect(url_for("about"))
        except RuntimeError as exc:
            flash(str(exc),"error")
    return render_template("license.html",license_info=current_license_status())


@app.route("/about")
def about():
    return render_template("about.html",license_info=current_license_status())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:150]
        remote_addr = request.remote_addr or "unknown"
        connection = db()
        cutoff = datetime.now().timestamp() - 900
        cutoff_text = datetime.fromtimestamp(cutoff).isoformat(timespec="seconds")
        connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff_text,))
        if login_attempts_exceeded(connection, username, remote_addr, cutoff_text):
            connection.close()
            log("login_blocked", f"username={username}", entity_type="auth", outcome="failure")
            flash("تم إيقاف محاولات الدخول مؤقتًا بسبب تكرار المحاولات الفاشلة. حاول بعد 15 دقيقة.", "error")
            return render_template("login.html"), 429
        user = connection.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        valid = bool(user and verify_password(request.form.get("password", ""), user["password_hash"]))
        if valid and password_hash_needs_upgrade(user["password_hash"]):
            connection.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (password_hash(request.form["password"]), user["id"]))
            connection.commit()
        if valid:
            connection.execute("DELETE FROM login_attempts WHERE username=? AND remote_addr=?", (username, remote_addr))
        else:
            connection.execute("INSERT INTO login_attempts(username,remote_addr,attempted_at) VALUES(?,?,?)",
                               (username, remote_addr, now()))
        connection.commit()
        connection.close()
        if valid:
            session.clear()
            session.permanent = True
            session.update(user_id=user["id"], username=user["username"], full_name=user["full_name"],
                           role=user["role"], must_change_password=bool(user["must_change_password"]),
                           permissions=stored_user_permissions(user["permissions_json"], user["role"]))
            log("login", "تسجيل دخول")
            if user["must_change_password"]:
                flash("يجب تغيير كلمة مرور المدير الافتراضية قبل استخدام النظام.", "warning")
                return redirect(url_for("change_password"))
            return redirect(url_for("dashboard"))
        log("login_failed", f"username={username}", entity_type="auth", outcome="failure")
        flash("بيانات الدخول غير صحيحة.", "error")
    return render_template("login.html")


@app.route("/profile/password", methods=["GET", "POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new) < 12:
            flash("كلمة المرور الجديدة يجب ألا تقل عن 12 حرفًا.", "error")
        elif new != confirm:
            flash("تأكيد كلمة المرور غير مطابق.", "error")
        else:
            connection = db()
            user = connection.execute("SELECT password_hash FROM users WHERE id=? AND active=1",
                                      (session["user_id"],)).fetchone()
            if not user or not verify_password(current, user["password_hash"]):
                flash("كلمة المرور الحالية غير صحيحة.", "error")
            else:
                connection.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                                   (password_hash(new), session["user_id"]))
                connection.commit()
                connection.close()
                session["must_change_password"] = False
                log("password_changed", "تغيير كلمة مرور المستخدم الحالي")
                flash("تم تغيير كلمة المرور بنجاح.", "success")
                return redirect(url_for("dashboard"))
            connection.close()
    return render_template("change_password.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/company/setup", methods=["GET", "POST"])
@requires("users.manage")
def company_setup():
    connection = db()
    if request.method == "POST":
        activity = request.form.get("activity", "commercial")
        tax_country = request.form.get("tax_country", "OTHER")
        # Country localization is deterministic: Egypt and Saudi Arabia get their standard VAT defaults.
        if tax_country == "EG":
            tax_regime, base_currency, automatic_vat = "EG_VAT", "EGP", "14"
        elif tax_country == "SA":
            tax_regime, base_currency, automatic_vat = "SA_VAT_ZAKAT", "SAR", "15"
        else:
            tax_regime, automatic_vat = "CUSTOM", request.form.get("default_vat_rate", "0").strip()
            base_currency = request.form.get("base_currency", "").upper()
        if activity not in CHARTS or tax_country not in {"EG", "SA", "OTHER"} or tax_regime not in {"EG_VAT", "SA_VAT_ZAKAT", "CUSTOM"}:
            flash("اختيارات الإعدادات غير صحيحة.", "error")
            connection.close()
            return redirect(url_for("company_setup"))
        if not connection.execute("SELECT 1 FROM currencies WHERE code=?", (base_currency,)).fetchone():
            flash("العملة الأساسية غير مدعومة في قائمة العملات الحالية.", "error")
            connection.close()
            return redirect(url_for("company_setup"))
        inventory_cost_method = request.form.get("inventory_cost_method", "average")
        if inventory_cost_method not in {"average", "fifo"}:
            flash("طريقة تقييم المخزون غير صحيحة.", "error")
            connection.close()
            return redirect(url_for("company_setup"))
        settings_values = {
            "inventory_cost_method": inventory_cost_method,
            "company_name": request.form.get("company_name", "").strip(),
            "tax_country": tax_country, "tax_regime": tax_regime,
            "tax_number": request.form.get("tax_number", "").strip(),
            "activity": activity, "base_currency": base_currency,
            "default_vat_rate": automatic_vat
        }
        try:
            vat_rate = float(settings_values["default_vat_rate"])
            if not 0 <= vat_rate <= 100 or not settings_values["company_name"]:
                raise ValueError()
        except ValueError:
            flash("أدخل اسم شركة ومعدل ضريبة صحيحًا من 0 إلى 100.", "error")
            connection.close()
            return redirect(url_for("company_setup"))
        previous_activity_row = connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
        previous_activity = previous_activity_row["setting_value"] if previous_activity_row else None
        has_entries = connection.execute("SELECT 1 FROM journal_entries LIMIT 1").fetchone()
        for key, value in settings_values.items():
            connection.execute("INSERT INTO company_settings(setting_key,setting_value) VALUES(?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value", (key, value))
        selected = set(request.form.getlist("active_currencies"))
        selected.add(base_currency)
        for row in connection.execute("SELECT code FROM currencies").fetchall():
            code = row["code"]
            if code not in selected:
                connection.execute("UPDATE currencies SET active=0,is_base=0 WHERE code=?", (code,))
                continue
            try:
                rate = 1.0 if code == base_currency else float(request.form.get("rate_" + code, "0") or 0)
            except ValueError:
                rate = 0
            if rate <= 0:
                flash(f"أدخل سعر صرف صحيح للعملة {code}. لم يتم حفظ إعدادات العملات.", "error")
                connection.rollback()
                connection.close()
                return redirect(url_for("company_setup"))
            connection.execute("UPDATE currencies SET active=1,is_base=?,rate_to_base=?,updated_at=? WHERE code=?", (1 if code == base_currency else 0, rate, now(), code))
        if has_entries and previous_activity and previous_activity != activity:
            flash("لا يمكن تغيير النشاط بعد ترحيل قيود؛ لأن ذلك قد يغيّر دلالات وربط الحسابات. أنشئ شركة جديدة أو نفّذ تحويلًا محاسبيًا معتمدًا.", "error")
            connection.rollback()
            connection.close()
            return redirect(url_for("company_setup"))
        should_load_chart = not has_entries and (request.form.get("replace_chart") == "yes" or previous_activity != activity)
        if should_load_chart:
            # Activity changes are append-only: never delete or rewrite existing accounts.
            ensure_activity_chart_accounts(connection, activity)
        connection.execute("INSERT INTO tax_rates(code,name_ar,rate,active) VALUES('VAT',?,?,1) ON CONFLICT(code) DO UPDATE SET name_ar=excluded.name_ar,rate=excluded.rate,active=1",
                           ("ضريبة القيمة المضافة", vat_rate))
        connection.commit()
        connection.close()
        log("company_setup_updated", f"{activity}/{tax_country}/{base_currency}")
        flash("تم حفظ إعدادات الشركة والعملات والنظام الضريبي.", "success")
        return redirect(url_for("company_setup"))
    settings = {row["setting_key"]: row["setting_value"] for row in connection.execute("SELECT * FROM company_settings")}
    currencies = connection.execute("SELECT * FROM currencies ORDER BY code").fetchall()
    has_entries = connection.execute("SELECT 1 FROM journal_entries LIMIT 1").fetchone() is not None
    connection.close()
    return render_template("company_setup.html", settings=settings, currencies=currencies, activities=ACTIVITY_LABELS, has_entries=has_entries)


@app.route("/settings/activity-profiles", methods=["GET", "POST"])
@requires("users.manage")
def activity_profiles():
    connection = db()
    initialize_activity_profiles_schema(connection)
    if request.method == "POST":
        action = request.form.get("action", "create")
        try:
            if action == "create":
                profile_id = create_activity_profile(
                    connection, request.form.get("code"), request.form.get("name"),
                    request.form.get("base_activity"), request.form.get("description", ""),
                )
                connection.commit()
                log("activity_profile_created", request.form.get("code", ""), "activity_profile", profile_id)
                flash("تم إنشاء ملف النشاط. راجع توافق الحسابات قبل استخدامه.", "success")
            elif action == "update_mappings":
                profile_id = int(request.form.get("profile_id", "0"))
                update_custom_activity_profile(
                    connection, profile_id,
                    request.form.get("account_roles_json", ""),
                    request.form.get("posting_accounts_json", ""),
                    request.form.get("tax_roles_json", ""),
                )
                connection.commit()
                log("activity_profile_mappings_updated", str(profile_id), "activity_profile", profile_id)
                flash("تم حفظ خرائط الحسابات والضرائب بعد التحقق من نوع كل حساب.", "success")
            elif action in {"activate", "deactivate"}:
                profile_id = int(request.form.get("profile_id", "0"))
                profile = next((p for p in list_activity_profiles(connection, True) if p["id"] == profile_id), None)
                if not profile:
                    raise ValueError("ملف النشاط غير موجود.")
                if action == "activate":
                    current_activity = connection.execute(
                        "SELECT setting_value FROM company_settings WHERE setting_key='activity'"
                    ).fetchone()
                    has_posted_entries = connection.execute(
                        "SELECT 1 FROM journal_entries WHERE status='posted' LIMIT 1"
                    ).fetchone()
                    if has_posted_entries and current_activity and current_activity["setting_value"] != profile["base_activity"]:
                        raise ValueError("لا يمكن تغيير النشاط الأساسي بعد ترحيل قيود؛ يلزم إجراء تحويل محاسبي معتمد.")
                    errors = validate_activity_profile(connection, profile)
                    if errors:
                        raise ValueError("لا يمكن تفعيل الملف قبل معالجة الحسابات: " + "؛ ".join(errors[:5]))
                    connection.execute(
                        "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code',?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                        (profile["code"],),
                    )
                    connection.execute(
                        "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity',?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                        (profile["base_activity"],),
                    )
                    connection.commit()
                    log("activity_profile_activated", profile["code"], "activity_profile", profile_id)
                    flash("تم تفعيل ملف النشاط. لم يتم حذف أو تعديل أي حساب قائم.", "success")
                else:
                    active_code = connection.execute(
                        "SELECT setting_value FROM company_settings WHERE setting_key='activity_profile_code'"
                    ).fetchone()
                    if active_code and active_code["setting_value"] == profile["code"]:
                        raise ValueError("لا يمكن تعطيل ملف النشاط المستخدم حاليًا.")
                    set_activity_profile_active(connection, profile_id, False)
                    connection.commit()
                    log("activity_profile_deactivated", profile["code"], "activity_profile", profile_id)
                    flash("تم تعطيل ملف النشاط.", "success")
            else:
                raise ValueError("العملية المطلوبة غير معروفة.")
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            connection.rollback()
            flash(str(exc), "error")
        connection.close()
        return redirect(url_for("activity_profiles"))
    profiles = list_activity_profiles(connection, True)
    active_row = connection.execute(
        "SELECT setting_value FROM company_settings WHERE setting_key='activity_profile_code'"
    ).fetchone()
    active_code = active_row["setting_value"] if active_row else "commercial"
    for profile in profiles:
        profile["validation_errors"] = validate_activity_profile(connection, profile)
        profile["selected"] = profile["code"] == active_code
    connection.close()
    return render_template("activity_profiles.html", profiles=profiles, activities=ACTIVITY_LABELS)


@app.route("/")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if "reports.view" not in set(session.get("permissions", PERMISSIONS.get(session.get("role"), set()))):
        return render_template("limited_home.html")
    return redirect(url_for("management_dashboard"))


@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    if "user_id" not in session:
        return redirect(url_for("login"))
    effective_permissions = set(session.get("permissions", PERMISSIONS.get(session.get("role"), set())))
    required_permission = "accounts.manage" if request.method == "POST" else "accounts.view"
    if required_permission not in effective_permissions and not (request.method == "GET" and "accounts.manage" in effective_permissions):
        flash("لا تملك صلاحية عرض أو إدارة شجرة الحسابات.","error")
        return redirect(url_for("dashboard"))
    connection = db()
    if request.method == "POST":
        try:
            code = request.form["code"].strip()
            name = request.form["name_ar"].strip()
            account_type = request.form["account_type"]
            parent_id = request.form.get("parent_id") or None
            is_group = 1 if request.form.get("is_group") == "yes" else 0
            if account_type not in {"asset","liability","equity","revenue","expense"} or not code or not name:
                raise ValueError("بيانات الحساب غير صحيحة.")
            if parent_id:
                parent = connection.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?", (parent_id,)).fetchone()
                if not parent or not parent["active"] or not parent["is_group"] or parent["account_type"] != account_type:
                    raise ValueError("الحساب الأب يجب أن يكون مجموعة نشطة ومن نفس نوع الحساب.")
            elif not is_group:
                raise ValueError("الحساب الفرعي يجب أن يرتبط بحساب رئيسي. لإنشاء مستوى أول اختر حسابًا رئيسيًا تجميعيًا.")
            connection.execute("INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,?)",
                               (code, name, account_type, parent_id, is_group))
            connection.commit()
            log("account_created", code)
            flash("تمت إضافة الحساب.", "success")
        except sqlite3.IntegrityError:
            connection.rollback()
            flash("رمز الحساب مستخدم بالفعل.", "error")
        except ValueError as exc:
            connection.rollback()
            flash(str(exc), "error")
    raw_rows = connection.execute("SELECT * FROM accounts ORDER BY code").fetchall()
    by_id = {row["id"]: dict(row) for row in raw_rows}
    def account_depth(row, seen=None):
        seen = seen or set()
        parent_id = row.get("parent_id")
        if not parent_id or parent_id not in by_id or parent_id in seen:
            return 0
        return 1 + account_depth(by_id[parent_id], seen | {parent_id})
    children_by_parent = {}
    for row in raw_rows:
        children_by_parent.setdefault(row["parent_id"], []).append(dict(row))
    rows = []
    def append_branch(parent_id, depth=0, ancestors=None):
        ancestors = ancestors or set()
        for item in sorted(children_by_parent.get(parent_id, []), key=lambda x: x["code"]):
            if item["id"] in ancestors:
                continue
            item["depth"] = depth
            item["has_children"] = bool(children_by_parent.get(item["id"]))
            item["parent_code"] = by_id.get(item["parent_id"], {}).get("code", "")
            rows.append(item)
            append_branch(item["id"], depth + 1, ancestors | {item["id"]})
    append_branch(None)
    # Keep any orphaned/cyclic records visible rather than silently hiding them.
    visible_ids = {item["id"] for item in rows}
    for raw in raw_rows:
        if raw["id"] not in visible_ids:
            item = dict(raw)
            item["depth"] = account_depth(item, {item["id"]})
            item["has_children"] = bool(children_by_parent.get(item["id"]))
            item["parent_code"] = by_id.get(item["parent_id"], {}).get("code", "")
            rows.append(item)
    groups = [row for row in raw_rows if row["is_group"] and row["active"]]
    connection.close()
    return render_template("accounts.html", accounts=rows, groups=groups)


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
@requires("accounts.manage")
def delete_account(account_id):
    connection = db()
    account = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not account:
        flash("الحساب غير موجود.", "error")
    else:
        child = connection.execute("SELECT 1 FROM accounts WHERE parent_id=? LIMIT 1", (account_id,)).fetchone()
        used = connection.execute("SELECT 1 FROM journal_lines WHERE account_id=? LIMIT 1", (account_id,)).fetchone()
        if child:
            flash("لا يمكن حذف الحساب: يوجد حساب فرعي مرتبط به. احذف/انقل الحسابات الفرعية أولًا.", "error")
        elif used:
            flash("لا يمكن حذف الحساب: سبق استخدامه في قيد يومية. احتفظ به أو عطّله بدل الحذف.", "error")
        else:
            try:
                connection.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                connection.commit()
                log("account_deleted", account["code"])
                flash("تم حذف الحساب لعدم وجود حسابات فرعية أو حركات مرتبطة.", "success")
            except sqlite3.IntegrityError:
                connection.rollback()
                flash("لا يمكن حذف الحساب لأنه مرتبط بمستند أو سجل آخر.", "error")
    connection.close()
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
@requires("accounts.manage")
def edit_account(account_id):
    connection = db()
    account = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not account:
        connection.close()
        flash("الحساب غير موجود.", "error")
        return redirect(url_for("accounts"))
    used = connection.execute("SELECT 1 FROM journal_lines WHERE account_id=? LIMIT 1", (account_id,)).fetchone() is not None
    if request.method == "POST":
        code, name = request.form.get("code", "").strip(), request.form.get("name_ar", "").strip()
        if not code or not name:
            flash("كود واسم الحساب مطلوبان.", "error")
        elif used and (request.form.get("account_type") != account["account_type"] or (int(request.form["parent_id"]) if request.form.get("parent_id") else None) != account["parent_id"] or (1 if request.form.get("is_group") == "yes" else 0) != account["is_group"]):
            flash("الحساب مستخدم في قيود سابقة؛ يسمح بتعديل الكود والاسم فقط حفاظًا على سلامة التقارير.", "error")
        else:
            parent_id = request.form.get("parent_id") or None
            new_type = request.form.get("account_type")
            new_group = 1 if request.form.get("is_group")=="yes" else 0
            parent = connection.execute("SELECT * FROM accounts WHERE id=?", (parent_id,)).fetchone() if parent_id else None
            descendants = connection.execute("""WITH RECURSIVE tree(id) AS (
                SELECT id FROM accounts WHERE parent_id=?
                UNION ALL SELECT a.id FROM accounts a JOIN tree t ON a.parent_id=t.id
            ) SELECT id FROM tree""", (account_id,)).fetchall()
            descendant_ids = {row["id"] for row in descendants}
            if new_type not in {"asset","liability","equity","revenue","expense"}:
                flash("نوع الحساب غير صحيح.", "error")
            elif new_group == 0 and connection.execute("SELECT 1 FROM accounts WHERE parent_id=? LIMIT 1",(account_id,)).fetchone():
                flash("لا يمكن تحويل حساب أب له حسابات فرعية إلى حساب تفصيلي.", "error")
            elif parent_id and (not parent or not parent["is_group"] or not parent["active"] or parent["account_type"] != new_type or int(parent_id)==account_id or int(parent_id) in descendant_ids):
                flash("الحساب الأب غير صالح أو يؤدي إلى تكوين دورة في الشجرة.", "error")
            else:
                try:
                    connection.execute("UPDATE accounts SET code=?,name_ar=?,account_type=?,parent_id=?,is_group=? WHERE id=?",
                        (code,name,new_type,parent_id,new_group,account_id))
                    connection.commit()
                    log("account_updated", code)
                    connection.close()
                    flash("تم تعديل الحساب.", "success")
                    return redirect(url_for("accounts"))
                except sqlite3.IntegrityError:
                    connection.rollback()
                    flash("كود الحساب مستخدم بالفعل.", "error")
    groups = connection.execute("SELECT * FROM accounts WHERE is_group=1 AND active=1 AND id<>? ORDER BY code", (account_id,)).fetchall()
    connection.close()
    return render_template("account_edit.html", account=account, groups=groups, used=used)



@app.route("/search")
def global_search():
    if "user_id" not in session:
        return redirect(url_for("login"))
    q = request.args.get("q", "").strip()[:120]
    results = []
    if q:
        conn = db()

        def exists(table):
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None

        def columns(table):
            if not exists(table):
                return set()
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

        like = f"%{q}%"

        # Search journal entries, including their linked sales/purchase documents.
        if exists("journal_entries"):
            journal_columns = columns("journal_entries")
            predicates = []
            args = []
            for column in ("reference", "description", "entry_date"):
                if column in journal_columns:
                    predicates.append(f"CAST(j.{column} AS TEXT) LIKE ?")
                    args.append(like)

            if exists("journal_lines"):
                predicates.append(
                    "EXISTS(SELECT 1 FROM journal_lines jl "
                    "WHERE jl.entry_id=j.id AND jl.memo LIKE ?)"
                )
                args.append(like)

            if exists("sales_invoices") and exists("customers"):
                sales_invoice_columns = columns("sales_invoices")
                if "journal_entry_id" in sales_invoice_columns:
                    predicates.append(
                        "EXISTS(SELECT 1 FROM sales_invoices si "
                        "JOIN customers sc ON sc.id=si.customer_id "
                        "WHERE si.journal_entry_id=j.id "
                        "AND (si.invoice_no LIKE ? OR sc.name LIKE ? OR sc.code LIKE ?))"
                    )
                    args.extend([like, like, like])

            if exists("supplier_bills") and exists("suppliers"):
                supplier_bill_columns = columns("supplier_bills")
                if "journal_entry_id" in supplier_bill_columns:
                    predicates.append(
                        "EXISTS(SELECT 1 FROM supplier_bills sb "
                        "JOIN suppliers ss ON ss.id=sb.supplier_id "
                        "WHERE sb.journal_entry_id=j.id "
                        "AND (sb.bill_no LIKE ? OR sb.supplier_invoice_no LIKE ? "
                        "OR ss.name LIKE ? OR ss.code LIKE ?))"
                    )
                    args.extend([like, like, like, like])

            if q.isdigit():
                predicates.append("j.id=?")
                args.append(int(q))

            if predicates:
                for row in conn.execute(
                    "SELECT j.id,j.entry_date,j.reference,j.description "
                    "FROM journal_entries j WHERE "
                    + " OR ".join(predicates)
                    + " ORDER BY j.id DESC LIMIT 30",
                    args,
                ):
                    results.append({
                        "kind": "قيد يومية",
                        "title": f"قيد رقم {row['id']} — {row['reference'] or ''}",
                        "detail": f"{row['entry_date']} | {row['description']}",
                        "url": url_for("journal_detail", entry_id=row["id"]),
                    })

        # Search customers and suppliers independently from journal search.
        if exists("customers"):
            customer_columns = columns("customers")
            searchable = [column for column in ("name", "code", "phone") if column in customer_columns]
            if searchable:
                where = " OR ".join(f"{column} LIKE ?" for column in searchable)
                for row in conn.execute(
                    f"SELECT * FROM customers WHERE {where} ORDER BY name LIMIT 20",
                    [like] * len(searchable),
                ):
                    results.append({
                        "kind": "عميل",
                        "title": f"{row['code']} — {row['name']}",
                        "detail": f"هاتف: {row['phone'] or '—'}",
                        "url": url_for("sales.edit_customer", customer_id=row["id"]),
                    })

        if exists("suppliers"):
            supplier_columns = columns("suppliers")
            searchable = [column for column in ("name", "code", "phone") if column in supplier_columns]
            if searchable:
                where = " OR ".join(f"{column} LIKE ?" for column in searchable)
                for row in conn.execute(
                    f"SELECT * FROM suppliers WHERE {where} ORDER BY name LIMIT 20",
                    [like] * len(searchable),
                ):
                    results.append({
                        "kind": "مورد",
                        "title": f"{row['code']} — {row['name']}",
                        "detail": f"هاتف: {row['phone'] or '—'}",
                        "url": url_for("purchase.edit_supplier", supplier_id=row["id"]),
                    })

        if exists("sales_invoices") and exists("customers"):
            for row in conn.execute(
                """SELECT i.id,i.invoice_no,i.invoice_date,i.total_amount,c.name party
                   FROM sales_invoices i JOIN customers c ON c.id=i.customer_id
                   WHERE i.invoice_no LIKE ? OR c.name LIKE ? OR c.code LIKE ?
                   ORDER BY i.id DESC LIMIT 30""",
                (like, like, like),
            ):
                results.append({
                    "kind": "فاتورة بيع",
                    "title": row["invoice_no"],
                    "detail": f"{row['party']} | {row['invoice_date']} | {row['total_amount']}",
                    "url": url_for("sales.invoice_print", invoice_id=row["id"]),
                })

        if exists("supplier_bills") and exists("suppliers"):
            for row in conn.execute(
                """SELECT b.id,b.bill_no,b.supplier_invoice_no,b.bill_date,b.total_amount,s.name party
                   FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
                   WHERE b.bill_no LIKE ? OR b.supplier_invoice_no LIKE ?
                      OR s.name LIKE ? OR s.code LIKE ?
                   ORDER BY b.id DESC LIMIT 30""",
                (like, like, like, like),
            ):
                results.append({
                    "kind": "فاتورة مورد",
                    "title": row["bill_no"],
                    "detail": (
                        f"{row['party']} | فاتورة المورد {row['supplier_invoice_no']} "
                        f"| {row['bill_date']} | {row['total_amount']}"
                    ),
                    "url": url_for("purchase.bill_print", bill_id=row["id"]),
                })

        if exists("stock_products"):
            for row in conn.execute(
                """SELECT id,sku,name_ar,barcode,purchase_price,sale_price
                   FROM stock_products
                   WHERE sku LIKE ? OR name_ar LIKE ? OR barcode LIKE ?
                   ORDER BY name_ar LIMIT 30""",
                (like, like, like),
            ):
                results.append({
                    "kind": "منتج / مخزون",
                    "title": f"{row['sku']} — {row['name_ar']}",
                    "detail": (
                        f"شراء: {row['purchase_price']} | بيع: {row['sale_price']} "
                        f"| باركود: {row['barcode'] or '—'}"
                    ),
                    "url": url_for("stock.edit_product", product_id=row["id"]),
                })

        if exists("accounts"):
            for row in conn.execute(
                "SELECT id,code,name_ar FROM accounts "
                "WHERE code LIKE ? OR name_ar LIKE ? ORDER BY code LIMIT 20",
                (like, like),
            ):
                results.append({
                    "kind": "حساب",
                    "title": f"{row['code']} — {row['name_ar']}",
                    "detail": "دليل الحسابات",
                    "url": url_for("accounts"),
                })

        if exists("company_settings"):
            for row in conn.execute(
                "SELECT setting_key,setting_value FROM company_settings "
                "WHERE setting_value LIKE ? LIMIT 20",
                (like,),
            ):
                if "company" in row["setting_key"].lower() or "name" in row["setting_key"].lower():
                    results.append({
                        "kind": "إعداد شركة",
                        "title": row["setting_key"],
                        "detail": row["setting_value"],
                        "url": url_for("company_setup"),
                    })

        conn.close()

    return render_template("global_search.html", q=q, results=results)

@app.route("/journal", methods=["GET", "POST"])
@requires("journal.manage")
def journal():
    connection = db()
    accounts_list = connection.execute("SELECT * FROM accounts WHERE active=1 AND is_group=0 ORDER BY code").fetchall()
    dimension_options = active_dimension_options(connection)
    if request.method == "POST":
        account_ids = request.form.getlist("account_id")
        debits = request.form.getlist("debit")
        credits = request.form.getlist("credit")
        memos = request.form.getlist("memo")
        try:
            entry_date = datetime.fromisoformat(request.form.get("entry_date", "").strip()).date().isoformat()
            description = request.form.get("description", "").strip()
            if not description or len(description) > 500:
                raise ValueError("أدخل بيانًا صحيحًا للقيد (حتى 500 حرف).")
            if not (len(account_ids) == len(debits) == len(credits) == len(memos)):
                raise ValueError("بيانات سطور القيد غير مكتملة.")
            lines = []
            for account_id, debit_text, credit_text, memo in zip(account_ids, debits, credits, memos):
                if not account_id:
                    raise ValueError("اختر حسابًا لكل سطر.")
                try:
                    aid = int(account_id)
                    debit = Decimal((debit_text or "0").strip())
                    credit = Decimal((credit_text or "0").strip())
                except (ValueError, InvalidOperation):
                    raise ValueError("أدخل مبالغ رقمية صحيحة واختر حسابًا صالحًا.")
                if not debit.is_finite() or not credit.is_finite() or debit < 0 or credit < 0:
                    raise ValueError("المبالغ يجب أن تكون أرقامًا موجبة أو صفرًا.")
                if debit > 0 and credit > 0:
                    raise ValueError("لا يجوز أن يجمع السطر الواحد بين المدين والدائن.")
                if debit or credit:
                    lines.append((aid, debit, credit, (memo or "").strip()))
            total_debit = sum((x[1] for x in lines), Decimal("0"))
            total_credit = sum((x[2] for x in lines), Decimal("0"))
            if len(lines) < 2 or total_debit <= 0 or total_debit != total_credit:
                raise ValueError("يجب إدخال سطرين على الأقل وأن يتساوى إجمالي المدين والدائن.")
            reference = request.form.get("reference", "").strip() or "MAN-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
            if len(reference) > 100:
                raise ValueError("المرجع لا يجب أن يتجاوز 100 حرف.")
            dimensions = validate_journal_dimensions(connection, {key: request.form.get(key) for key in ("branch_id", "cost_center_id", "project_id")})
            entry_id = post_journal(connection, entry_date, reference, description, lines, session["user_id"], dimensions)
            connection.commit()
            log("journal_posted", f"قيد #{entry_id}")
            flash("تم ترحيل القيد بنجاح.", "success")
            return redirect(url_for("journal"))
        except (ValueError, TypeError, OverflowError) as exc:
            connection.rollback()
            flash(str(exc) or "بيانات القيد غير صحيحة.", "error")
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    status_filter = request.args.get("status", "").strip()
    entry_type_filter = request.args.get("entry_type", "").strip()
    account_filter = request.args.get("account_id", type=int)
    search_query = request.args.get("q", "").strip()[:100]
    for value in (date_from, date_to):
        if value:
            try:
                if datetime.fromisoformat(value).date().isoformat() != value:
                    raise ValueError()
            except ValueError:
                flash("تاريخ البحث غير صحيح.", "error")
                date_from = date_to = ""
                break
    if date_from and date_to and date_from > date_to:
        flash("تاريخ البداية يجب ألا يسبق تاريخ النهاية.", "error")
        date_from = date_to = ""
    if status_filter not in ("draft", "posted"):
        status_filter = ""
    if entry_type_filter not in ("general", "year_end_closing", "year_end_closing_reversal"):
        entry_type_filter = ""
    filters, filter_params = [], []
    if date_from:
        filters.append("entry_date>=?"); filter_params.append(date_from)
    if date_to:
        filters.append("entry_date<=?"); filter_params.append(date_to)
    if status_filter:
        filters.append("status=?"); filter_params.append(status_filter)
    entry_columns = {row["name"] for row in connection.execute("PRAGMA table_info(journal_entries)")}
    if entry_type_filter and "entry_type" in entry_columns:
        filters.append("entry_type=?"); filter_params.append(entry_type_filter)
    if account_filter:
        valid_account = connection.execute(
            "SELECT 1 FROM accounts WHERE id=? AND is_group=0 AND active=1", (account_filter,)
        ).fetchone()
        if valid_account:
            filters.append("""EXISTS(SELECT 1 FROM journal_lines account_lines
                WHERE account_lines.entry_id=journal_entries.id AND account_lines.account_id=?)""")
            filter_params.append(account_filter)
        else:
            account_filter = None
    if search_query:
        search_like = "%" + search_query + "%"
        search_parts = [
            "reference LIKE ?", "description LIKE ?",
            """EXISTS(SELECT 1 FROM journal_lines search_lines
                JOIN accounts search_accounts ON search_accounts.id=search_lines.account_id
                WHERE search_lines.entry_id=journal_entries.id
                  AND (search_lines.memo LIKE ? OR search_accounts.code LIKE ? OR search_accounts.name_ar LIKE ?))"""
        ]
        filter_params.extend([search_like, search_like, search_like, search_like, search_like])
        if search_query.isdigit():
            search_parts.append("CAST(id AS TEXT)=?")
            filter_params.append(search_query)
        filters.append("(" + " OR ".join(search_parts) + ")")
    where_sql = " AND ".join(filters) if filters else "1=1"
    journal_page = max(1, request.args.get("journal_page", 1, type=int) or 1)
    journal_per_page = request.args.get("journal_per_page", 50, type=int) or 50
    if journal_per_page not in (50, 100):
        journal_per_page = 50
    journal_total = connection.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE " + where_sql, filter_params
    ).fetchone()[0]
    journal_pages = max(1, (journal_total + journal_per_page - 1) // journal_per_page)
    journal_page = min(journal_page, journal_pages)
    entries = connection.execute(
        "SELECT * FROM journal_entries WHERE " + where_sql +
        " ORDER BY entry_date DESC,id DESC LIMIT ? OFFSET ?",
        (*filter_params, journal_per_page, (journal_page - 1) * journal_per_page),
    ).fetchall()
    connection.close()
    return render_template("journal.html", accounts=accounts_list, entries=entries,
        today=datetime.now().date().isoformat(), dimension_options=dimension_options,
        journal_page=journal_page, journal_pages=journal_pages,
        journal_per_page=journal_per_page, journal_total=journal_total,
        date_from=date_from, date_to=date_to, status_filter=status_filter,
        entry_type_filter=entry_type_filter, account_filter=account_filter, search_query=search_query)



@app.route("/dashboard/management")
@requires("reports.view")
def management_dashboard():
    today = date.today()
    default_start, default_end = today.replace(day=1), today
    raw_from = request.args.get("date_from", "").strip()
    raw_to = request.args.get("date_to", "").strip()
    try:
        start = date.fromisoformat(raw_from) if raw_from else default_start
        end = date.fromisoformat(raw_to) if raw_to else default_end
        if end < start:
            raise ValueError("تاريخ النهاية يجب ألا يسبق تاريخ البداية.")
    except ValueError:
        flash("الفترة غير صحيحة، تم عرض الشهر الحالي.", "error")
        start, end = default_start, default_end

    dimensions = {}
    for key in ("branch_id", "cost_center_id", "project_id", "customer_id", "salesperson_id"):
        raw = request.args.get(key, "").strip()
        if raw:
            try:
                dimensions[key] = int(raw)
            except ValueError:
                flash("أحد فلاتر الأبعاد التحليلية غير صحيح.", "error")
                dimensions = {}
                break
    connection = db()
    try:
        snapshot = cached_management_snapshot(connection, start, end, dimensions)
        dimension_options = active_dimension_options(connection)
    finally:
        connection.close()
    return render_template(
        "management_dashboard.html", s=snapshot,
        date_from=start.isoformat(), date_to=end.isoformat(),
        dimension_options=dimension_options, selected_dimensions=dimensions,
    )



@app.route("/dashboard/management/minimum-cash", methods=["POST"])
@requires("accounts.manage")
def management_minimum_cash():
    raw = request.form.get("minimum_cash_balance", "").strip()
    try:
        value = Decimal(raw or "0")
        if not value.is_finite() or value < 0:
            raise ValueError()
        connection = db()
        connection.execute("""INSERT INTO company_settings(setting_key,setting_value)
            VALUES('minimum_cash_balance',?) ON CONFLICT(setting_key)
            DO UPDATE SET setting_value=excluded.setting_value""",
            (str(value.quantize(Decimal("0.01"))),))
        connection.commit()
        connection.close()
        log("minimum_cash_balance_updated", str(value))
        flash("تم حفظ الحد الأدنى للنقدية.", "success")
    except (InvalidOperation, ValueError, sqlite3.Error):
        flash("أدخل حدًا أدنى صحيحًا لا يقل عن صفر.", "error")
    return redirect(url_for("management_dashboard"))


@app.route("/settings/analytic-dimensions", methods=["GET", "POST"])
@requires("accounts.manage")
def analytic_dimensions_settings():
    connection = db()
    if request.method == "POST":
        try:
            kind = request.form.get("dimension_type", "").strip()
            config = {
                "branch": ("branches", "الفرع"),
                "cost_center": ("cost_centers", "مركز التكلفة"),
                "project": ("projects", "المشروع"),
            }
            if kind not in config:
                raise ValueError("نوع البُعد التحليلي غير صحيح.")
            table, label = config[kind]
            code = request.form.get("code", "").strip().upper()
            name = request.form.get("name_ar", "").strip()
            if not code or len(code) > 30 or not name or len(name) > 120:
                raise ValueError("أدخل كودًا واسمًا صحيحين للبُعد.")
            connection.execute(f"INSERT INTO {table}(code,name_ar) VALUES(?,?)", (code, name))
            connection.commit()
            flash("تمت إضافة " + label + " بنجاح.", "success")
            return redirect(url_for("analytic_dimensions_settings"))
        except (ValueError, sqlite3.IntegrityError) as exc:
            connection.rollback()
            flash(str(exc) or "تعذر إضافة البُعد. تأكد من عدم تكرار الكود.", "error")
    options = active_dimension_options(connection)
    connection.close()
    return render_template("analytic_dimensions.html", options=options)


@app.route("/management-dashboard/account-mapping", methods=["GET", "POST"])
@requires("accounts.manage")
def management_account_mapping():
    connection = db()
    from management_dashboard import MANAGEMENT_ROLES, load_management_account_roles
    load_management_account_roles(connection)
    if request.method == "POST":
        try:
            selections = {}
            for role, (_, allowed_types) in MANAGEMENT_ROLES.items():
                ids = []
                for raw_id in request.form.getlist("role_" + role):
                    account_id = int(raw_id)
                    account = connection.execute(
                        "SELECT id,account_type,is_group,active FROM accounts WHERE id=?", (account_id,)
                    ).fetchone()
                    if not account or account["is_group"] or not account["active"] or account["account_type"] not in allowed_types:
                        raise ValueError("يوجد حساب غير تفصيلي أو غير نشط أو من نوع غير مناسب في الربط.")
                    ids.append(account_id)
                selections[role] = set(ids)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM management_account_roles")
            connection.execute("""INSERT INTO management_dashboard_meta(meta_key,meta_value) VALUES('roles_customized','1') ON CONFLICT(meta_key) DO UPDATE SET meta_value='1'""")
            for role, ids in selections.items():
                connection.executemany(
                    "INSERT INTO management_account_roles(account_id,role) VALUES(?,?)",
                    [(account_id, role) for account_id in sorted(ids)]
                )
            connection.commit()
            flash("تم حفظ ربط حسابات لوحة الإدارة بشجرة الحسابات الحالية.", "success")
            return redirect(url_for("management_account_mapping"))
        except (ValueError, sqlite3.Error) as exc:
            connection.rollback()
            flash(str(exc) or "تعذر حفظ ربط الحسابات.", "error")
    roles = load_management_account_roles(connection)
    accounts = connection.execute(
        "SELECT id,code,name_ar,account_type FROM accounts WHERE is_group=0 AND active=1 ORDER BY code"
    ).fetchall()
    connection.close()
    return render_template("management_account_mapping.html", roles=MANAGEMENT_ROLES,
                           selected=roles, accounts=accounts)


@app.route("/security/audit-log")
@requires("audit.view")
def audit_log():
    action = request.args.get("action", "").strip()[:100]
    user_query = request.args.get("user", "").strip()[:100]
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    conditions, params = ["1=1"], []
    if action:
        conditions.append("al.action LIKE ?"); params.append(f"%{action}%")
    if user_query:
        conditions.append("(u.username LIKE ? OR u.full_name LIKE ?)"); params.extend([f"%{user_query}%", f"%{user_query}%"])
    if date_from:
        conditions.append("substr(al.created_at,1,10) >= ?"); params.append(date_from)
    if date_to:
        conditions.append("substr(al.created_at,1,10) <= ?"); params.append(date_to)
    where = " AND ".join(conditions)
    connection = db()
    page = fetch_page(
        connection,
        f"""SELECT al.id,al.action,al.details,al.created_at,al.entity_type,al.entity_id,
                   al.remote_addr,al.outcome,u.username,u.full_name
            FROM audit_log al LEFT JOIN users u ON u.id=al.user_id
            WHERE {where} ORDER BY al.created_at DESC,al.id DESC""",
        f"""SELECT COUNT(*) FROM audit_log al LEFT JOIN users u ON u.id=al.user_id
            WHERE {where}""",
        params, page=request.args.get("page", 1, type=int), per_page=50,
    )
    connection.close()
    return render_template(
        "audit_log.html", audit_page=page, action=action, user_query=user_query,
        date_from=date_from, date_to=date_to,
    )


@app.route("/reports/trial-balance")
@requires("reports.view")
def trial_balance():
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    account_type = request.args.get("account_type", "").strip()
    q = request.args.get("q", "").strip()
    include_closing_values = request.args.getlist("include_closing")
    include_closing = not include_closing_values or include_closing_values[-1].strip() != "0"
    allowed_types = {"asset", "liability", "equity", "revenue", "expense"}
    if account_type not in allowed_types:
        account_type = ""
    conditions, params = ["1=1"], []
    if account_type:
        conditions.append("a.account_type = ?"); params.append(account_type)
    if q:
        conditions.append("(a.code LIKE ? OR a.name_ar LIKE ?)"); params.extend([f"%{q}%", f"%{q}%"])
    connection = db()
    years = connection.execute(
        "SELECT id,year_name,start_date,end_date,status FROM financial_years ORDER BY start_date DESC,id DESC"
    ).fetchall()
    period_ranges = []
    today = date.today().isoformat()
    for year in years:
        period_from = max(year["start_date"], date_from) if date_from else year["start_date"]
        natural_end = min(year["end_date"], today) if year["status"] == "open" else year["end_date"]
        period_to = min(natural_end, date_to) if date_to else natural_end
        if period_from <= period_to:
            period_ranges.append({
                "id": year["id"], "name": year["year_name"], "status": year["status"],
                "date_from": period_from, "date_to": period_to,
                "label": "الفترة الحالية / مفتوحة" if year["status"] == "open" else "فترة مالية مقفولة",
            })
    # Legacy installations without financial-year records retain the original
    # date-filter behavior rather than hiding their historical trial balance.
    if not years:
        period_ranges.append({
            "id": None, "name": "الفترة المحددة", "status": "open",
            "date_from": date_from, "date_to": date_to,
            "label": "الفترة الحالية",
        })
    sections = []
    for period in period_ranges:
        join_conditions = ["je.id=jl.entry_id", "je.status='posted'"]
        if not include_closing:
            join_conditions.append(closing_entries_exclusion_sql(connection))
        join_params = []
        if period["date_from"]:
            join_conditions.append("je.entry_date >= ?"); join_params.append(period["date_from"])
        if period["date_to"]:
            join_conditions.append("je.entry_date <= ?"); join_params.append(period["date_to"])
        rows = connection.execute(f"""SELECT a.id, a.code, a.name_ar, a.account_type,
            COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),0) debit,
            COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),0) credit
            FROM accounts a LEFT JOIN journal_lines jl ON jl.account_id=a.id
            LEFT JOIN journal_entries je ON {' AND '.join(join_conditions)}
            WHERE {' AND '.join(conditions)}
            GROUP BY a.id ORDER BY a.code""", [*join_params, *params]).fetchall()
        period_rows = [dict(row, debit=Decimal(str(row["debit"] or "0")),
                            credit=Decimal(str(row["credit"] or "0"))) for row in rows]
        sections.append({**period, "rows": period_rows})
    settings = {r["setting_key"]: r["setting_value"] for r in connection.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('base_currency')")}
    connection.close()
    types = {"asset":"أصول","liability":"التزامات","equity":"حقوق ملكية","revenue":"إيرادات","expense":"مصروفات"}
    return render_template("trial_balance.html", sections=sections, date_from=date_from, date_to=date_to,
        account_type=account_type, q=q, include_closing=include_closing,
        types=types, base_currency=settings.get("base_currency",""))


@app.route("/reports/income-statement")
@requires("reports.view")
def income_statement():
    date_from, date_to = normalize_income_statement_period(
        request.args.get("date_from"), request.args.get("date_to")
    )
    compare_mode = request.args.get("compare", "previous_year").strip()
    if compare_mode not in {"none", "previous_period", "previous_year"}:
        compare_mode = "previous_year"
    compare_dates = comparison_period(date_from, date_to, compare_mode) if compare_mode != "none" else None
    connection = db()
    try:
        statement = build_income_statement(connection, date_from, date_to)
        if compare_dates:
            previous = build_income_statement(connection, *compare_dates)
            statement = add_income_statement_comparison(statement, previous)
        settings = {r["setting_key"]: r["setting_value"] for r in connection.execute(
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('base_currency','company_name')"
        )}
    finally:
        connection.close()
    return render_template(
        "income_statement.html", **statement,
        date_from=date_from, date_to=date_to, compare_mode=compare_mode,
        compare_date_from=compare_dates[0] if compare_dates else "",
        compare_date_to=compare_dates[1] if compare_dates else "",
        base_currency=settings.get("base_currency", ""),
        company_name=settings.get("company_name", "NEXUS ERP"),
    )


@app.route("/reports/ledger")
@requires("reports.view")
def ledger():
    account_id = request.args.get("account_id", type=int)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    search_query = request.args.get("q", "").strip()[:100]
    for value in (date_from, date_to):
        if value:
            try:
                if datetime.fromisoformat(value).date().isoformat() != value:
                    raise ValueError()
            except ValueError:
                flash("تاريخ البحث غير صحيح.", "error")
                date_from = date_to = ""
                break
    if date_from and date_to and date_from > date_to:
        flash("تاريخ البداية يجب ألا يسبق تاريخ النهاية.", "error")
        date_from = date_to = ""

    connection = db()
    accounts_list = connection.execute("SELECT * FROM accounts WHERE active=1 ORDER BY code").fetchall()
    rows, selected = [], None
    opening_balance = Decimal("0")
    closing_balance = Decimal("0")
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    ledger_page = {"rows": [], "total": 0, "page": 1, "pages": 1, "per_page": 50}
    if account_id:
        selected = connection.execute(
            "SELECT * FROM accounts WHERE id=? AND is_group=0", (account_id,)
        ).fetchone()
        if selected:
            date_conditions = ["jl.account_id=?", "je.status='posted'"]
            date_params = [account_id]
            if date_from:
                date_conditions.append("je.entry_date>=?"); date_params.append(date_from)
            if date_to:
                date_conditions.append("je.entry_date<=?"); date_params.append(date_to)

            display_conditions = list(date_conditions)
            display_params = list(date_params)
            search_conditions = []
            search_params = []
            if search_query:
                like = "%" + search_query + "%"
                search_conditions = [
                    "ledger_rows.reference LIKE ?",
                    "ledger_rows.description LIKE ?",
                    "ledger_rows.memo LIKE ?",
                ]
                search_params.extend([like, like, like])
                display_conditions.append("(je.reference LIKE ? OR je.description LIKE ? OR jl.memo LIKE ?)")
                display_params.extend([like, like, like])
                if search_query.isdigit():
                    search_conditions.append("ledger_rows.entry_id=?")
                    display_conditions.append("CAST(je.id AS TEXT)=?")
                    search_params.append(int(search_query))
                    display_params.append(search_query)

            base_opening = Decimal("0")
            if date_from:
                opening_row = connection.execute("""SELECT COALESCE(DECIMAL_NET(jl.debit,jl.credit),'0') balance
                    FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE jl.account_id=? AND je.status='posted' AND je.entry_date<?""",
                    (account_id, date_from)).fetchone()
                base_opening = Decimal(str(opening_row["balance"] or "0"))

            if hasattr(connection, "create_window_function"):
                # Compute chronological balances once using exact Decimal math,
                # then filter the rendered rows outside the CTE.
                outer_where = " AND ".join(search_conditions) if search_conditions else "1=1"
                query = f"""WITH ledger_rows AS (
                    SELECT je.id entry_id,je.entry_date,je.reference,je.description,
                        jl.id line_id,jl.account_id,jl.debit,jl.credit,jl.memo,
                        (? + DECIMAL_RUNNING_NET(jl.debit,jl.credit) OVER (
                            ORDER BY je.entry_date,jl.id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        )) running_balance
                    FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE {' AND '.join(date_conditions)}
                )
                SELECT ledger_rows.*,
                    (SELECT GROUP_CONCAT(a2.code || ' - ' || a2.name_ar, '، ')
                     FROM journal_lines jl2 JOIN accounts a2 ON a2.id=jl2.account_id
                     WHERE jl2.entry_id=ledger_rows.entry_id
                       AND jl2.account_id<>ledger_rows.account_id) counterpart_accounts
                FROM ledger_rows WHERE {outer_where}
                ORDER BY entry_date,line_id"""
                query_params = [str(base_opening), *date_params, *search_params]
            else:
                history_conditions = ["hist.account_id=jl.account_id", "he.status='posted'",
                    "(he.entry_date<je.entry_date OR (he.entry_date=je.entry_date AND hist.id<=jl.id))"]
                history_params = []
                if date_from:
                    history_conditions.append("he.entry_date>=?"); history_params.append(date_from)
                query = f"""SELECT je.id entry_id,je.entry_date,je.reference,je.description,
                    jl.id line_id,jl.account_id,jl.debit,jl.credit,jl.memo,
                    (SELECT GROUP_CONCAT(a2.code || ' - ' || a2.name_ar, '، ')
                       FROM journal_lines jl2 JOIN accounts a2 ON a2.id=jl2.account_id
                      WHERE jl2.entry_id=je.id AND jl2.account_id<>jl.account_id) counterpart_accounts,
                    (? + COALESCE((SELECT DECIMAL_NET(hist.debit,hist.credit)
                        FROM journal_lines hist JOIN journal_entries he ON he.id=hist.entry_id
                        WHERE {' AND '.join(history_conditions)}),'0')) running_balance
                    FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE {' AND '.join(display_conditions)}
                    ORDER BY je.entry_date,jl.id"""
                query_params = [str(base_opening), *history_params, *display_params]
            ledger_page = fetch_page(
                connection, query,
                f"""SELECT COUNT(*) FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE {' AND '.join(display_conditions)}""",
                query_params,
                count_params=display_params,
                page=request.args.get("ledger_page",1,type=int),
                per_page=request.args.get("ledger_per_page",50,type=int),
            )
            raw_rows = ledger_page["rows"]
            if raw_rows:
                first = raw_rows[0]
                first_running = Decimal(str(first["running_balance"] or "0"))
                opening_balance = first_running - Decimal(str(first["debit"] or "0")) + Decimal(str(first["credit"] or "0"))
            else:
                opening_balance = base_opening

            filtered_totals = connection.execute(f"""SELECT
                COALESCE(DECIMAL_SUM(jl.debit),'0') debit,
                COALESCE(DECIMAL_SUM(jl.credit),'0') credit
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE {' AND '.join(display_conditions)}""", display_params).fetchone()
            total_debit = Decimal(str(filtered_totals["debit"] or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total_credit = Decimal(str(filtered_totals["credit"] or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            period_net = connection.execute(f"""SELECT COALESCE(DECIMAL_NET(jl.debit,jl.credit),'0') net
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE {' AND '.join(date_conditions)}""", date_params).fetchone()
            closing_balance = (base_opening + Decimal(str(period_net["net"] or "0"))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            for row in raw_rows:
                item = dict(row)
                item["debit"] = Decimal(str(item["debit"] or "0"))
                item["credit"] = Decimal(str(item["credit"] or "0"))
                item["running_balance"] = Decimal(str(item["running_balance"] or "0")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                rows.append(item)
    connection.close()
    return render_template("ledger.html", accounts=accounts_list, selected=selected, rows=rows,
        date_from=date_from, date_to=date_to, search_query=search_query,
        opening_balance=opening_balance, closing_balance=closing_balance,
        total_debit=total_debit, total_credit=total_credit, ledger_page=ledger_page)


@app.route("/journal/<int:entry_id>")
@requires("reports.view")
def journal_detail(entry_id):
    connection = db()
    entry = connection.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        connection.close()
        flash("القيد المحاسبي غير موجود.", "error")
        return redirect(url_for("trial_balance"))
    lines = connection.execute("""SELECT jl.*,a.code account_code,a.name_ar account_name
        FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
        WHERE jl.entry_id=? ORDER BY jl.id""", (entry_id,)).fetchall()
    lines = [dict(row, debit=Decimal(str(row["debit"] or "0")),
                  credit=Decimal(str(row["credit"] or "0"))) for row in lines]
    totals_row = connection.execute("""SELECT COALESCE(DECIMAL_SUM(debit),0) debit,COALESCE(DECIMAL_SUM(credit),0) credit
        FROM journal_lines WHERE entry_id=?""", (entry_id,)).fetchone()
    totals = {"debit":Decimal(str(totals_row["debit"] or "0")),
              "credit":Decimal(str(totals_row["credit"] or "0"))}
    connection.close()
    return render_template("journal_detail.html", entry=entry, lines=lines, totals=totals, today=datetime.now().date().isoformat())


@app.route("/accounting/periods", methods=["GET", "POST"])
@requires("users.manage")
def accounting_periods():
    connection = db()
    if request.method == "POST":
        name = request.form.get("period_name", "").strip()
        start_text = request.form.get("start_date", "").strip()
        end_text = request.form.get("end_date", "").strip()
        try:
            start_date = date.fromisoformat(start_text).isoformat()
            end_date = date.fromisoformat(end_text).isoformat()
            if start_date != start_text or end_date != end_text:
                raise ValueError()
            if not name or len(name) > 100 or start_date > end_date:
                raise ValueError()
            overlap = connection.execute("""SELECT 1 FROM accounting_periods
                WHERE NOT(end_date < ? OR start_date > ?) LIMIT 1""",
                (start_date, end_date)).fetchone()
            if overlap:
                raise ValueError("توجد فترة محاسبية أخرى تتداخل مع هذا النطاق.")
            has_years = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_years'"
            ).fetchone()
            if has_years and connection.execute("SELECT 1 FROM financial_years LIMIT 1").fetchone():
                containing_year = connection.execute("""SELECT 1 FROM financial_years
                    WHERE start_date<=? AND end_date>=? LIMIT 1""",
                    (start_date, end_date)).fetchone()
                if not containing_year:
                    raise ValueError("يجب أن تقع الفترة المحاسبية بالكامل داخل سنة مالية مسجلة.")
            connection.execute("""INSERT INTO accounting_periods(period_name,start_date,end_date,locked,created_at)
                VALUES(?,?,?,0,?)""", (name, start_date, end_date, now()))
            connection.commit()
            log("accounting_period_created", f"{name}: {start_date} - {end_date}")
            flash("تم إنشاء الفترة المحاسبية بحالة مفتوحة.", "success")
        except (ValueError, TypeError):
            connection.rollback()
            flash("أدخل اسمًا وتاريخي بداية ونهاية صحيحين، وتأكد من عدم تداخل الفترات.", "error")
        finally:
            connection.close()
        return redirect(url_for("accounting_periods"))
    rows = connection.execute("SELECT * FROM accounting_periods ORDER BY start_date DESC").fetchall()
    connection.close()
    return render_template("periods.html", periods=rows)


@app.route("/accounting/periods/<int:period_id>/state", methods=["POST"])
@requires("users.manage")
def accounting_period_state(period_id):
    state = request.form.get("locked", "")
    if state not in {"0", "1"}:
        flash("حالة الفترة غير صحيحة.", "error")
        return redirect(url_for("accounting_periods"))
    connection = db()
    row = connection.execute("SELECT period_name,start_date,end_date FROM accounting_periods WHERE id=?", (period_id,)).fetchone()
    if not row:
        connection.close()
        flash("الفترة المحاسبية غير موجودة.", "error")
        return redirect(url_for("accounting_periods"))
    closed_year_overlap = False
    has_financial_years = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_years'"
    ).fetchone()
    if state == "0" and has_financial_years:
        closed_year_overlap = connection.execute("""SELECT 1 FROM financial_years
            WHERE status='closed' AND NOT(end_date<? OR start_date>?) LIMIT 1""",
            (row["start_date"], row["end_date"])).fetchone()
    if closed_year_overlap:
        connection.close()
        flash("لا يمكن فتح فترة تتقاطع مع سنة مالية مقفولة. استخدم إجراء عكس الإقفال المعتمد أولًا.", "error")
        return redirect(url_for("accounting_periods"))
    connection.execute("UPDATE accounting_periods SET locked=? WHERE id=?", (int(state), period_id))
    connection.commit()
    connection.close()
    log("accounting_period_locked" if state == "1" else "accounting_period_unlocked", row["period_name"])
    flash("تم إقفال الفترة؛ لن تقبل قيودًا جديدة." if state == "1" else "تم فتح الفترة المحاسبية.", "success")
    return redirect(url_for("accounting_periods"))


@app.route("/journal/<int:entry_id>/reverse", methods=["POST"])
@requires("journal.manage")
def reverse_journal(entry_id):
    connection = db()
    original = connection.execute("SELECT * FROM journal_entries WHERE id=? AND status='posted'", (entry_id,)).fetchone()
    if not original:
        connection.close()
        flash("لا يمكن عكس قيد غير موجود أو غير مرحّل.", "error")
        return redirect(url_for("trial_balance"))
    # Financial-year opening/closing journals must use their dedicated
    # reversal workflow so the year pointers and status remain consistent.
    has_financial_year_schema = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_years'").fetchone()
    financial_year_link = None
    if has_financial_year_schema:
        financial_year_link = connection.execute("""SELECT id,year_name FROM financial_years
            WHERE opening_entry_id=? OR closing_entry_id=? LIMIT 1""",
            (entry_id, entry_id)).fetchone()
        if not financial_year_link and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_year_journal_history'").fetchone():
            financial_year_link = connection.execute("""SELECT financial_year_id AS id
                FROM financial_year_journal_history WHERE entry_id=? LIMIT 1""",
                (entry_id,)).fetchone()
    if financial_year_link:
        connection.close()
        flash("هذا قيد افتتاح أو إقفال مرتبط بسنة مالية؛ استخدم إجراء العكس من شاشة إدارة السنوات المالية حتى تُحدّث حالة السنة والارتباطات بأمان.", "error")
        return redirect(url_for("planning.financial_years"))
    # A journal linked from an operational document must be reversed through
    # that document's own workflow (credit note, stock return, payment reversal, etc.).
    linked_source = linked_operational_source(connection, entry_id)
    if linked_source:
        connection.close()
        flash(
            f"تم منع عكس القيد المرتبط بمستند في {linked_source} لحماية ترابط الحسابات والمخزون. "
            "تنبيه: إجراء الإلغاء/الإشعار غير متاح حاليًا لهذا النوع داخل NEXUS. "
            "لا تعكس القيد يدويًا ولا تحذف المستند. صحح المستند بمستند تسوية معتمد خارج النظام "
            "وسجل أثره في NEXUS وفق صلاحيات وإجراءات الشركة، أو اطلب من مسؤول النظام تنفيذ التصحيح "
            "بعد توفير مسار تصحيح خاص بالموديول.",
            "error",
        )
        return redirect(url_for("journal_detail", entry_id=entry_id))
    reference = f"REV-{entry_id}"
    existing = connection.execute("SELECT id FROM journal_entries WHERE reference=? AND status='posted'", (reference,)).fetchone()
    if existing:
        connection.close()
        flash(f"تم إنشاء القيد العكسي مسبقًا: رقم {existing['id']}.", "warning")
        return redirect(url_for("journal_detail", entry_id=entry_id))
    original_lines = connection.execute("SELECT account_id,debit,credit,memo FROM journal_lines WHERE entry_id=? ORDER BY id",
                                        (entry_id,)).fetchall()
    if len(original_lines) < 2:
        connection.close()
        flash("القيد الأصلي لا يحتوي على سطور كافية للعكس.", "error")
        return redirect(url_for("journal_detail", entry_id=entry_id))
    reverse_lines = [(r["account_id"], r["credit"], r["debit"], f"عكس: {r['memo'] or ''}") for r in original_lines]
    reversal_date_text = request.form.get("reversal_date", "").strip()
    reversal_reason = request.form.get("reversal_reason", "").strip()
    try:
        reversal_date = datetime.strptime(reversal_date_text, "%Y-%m-%d").date().isoformat()
        if reversal_date != reversal_date_text:
            raise ValueError("تاريخ القيد العكسي غير صحيح.")
        if not reversal_reason or len(reversal_reason) > 500:
            raise ValueError("اكتب سببًا للقيد العكسي (بحد أقصى 500 حرف).")
        reversal_id = post_journal(connection, reversal_date, reference,
            f"قيد عكسي للقيد رقم {entry_id}: {original['description']} — السبب: {reversal_reason}",
            reverse_lines, session["user_id"])
        connection.commit()
        log("journal_reversed", f"الأصلي #{entry_id}؛ العكسي #{reversal_id}؛ التاريخ {reversal_date}؛ السبب: {reversal_reason}")
        flash(f"تم إنشاء القيد العكسي رقم {reversal_id}. القيد الأصلي محفوظ دون تعديل.", "success")
        return redirect(url_for("journal_detail", entry_id=reversal_id))
    except (ValueError, TypeError) as exc:
        connection.rollback()
        flash(f"تعذر عكس القيد: {exc}", "error")
        return redirect(url_for("journal_detail", entry_id=entry_id))
    finally:
        connection.close()


@app.route("/reports/reconciliation")
@requires("reports.view")
def reconciliation():
    connection = db()
    try:
        snapshot = reconciliation_snapshot(connection)
    finally:
        connection.close()
    settings = snapshot.get("settings", {})
    return render_template("reconciliation.html", snapshot=snapshot, settings=settings)

@app.route("/users", methods=["GET", "POST"])
@requires("users.manage")
def users():
    connection = db()
    if request.method == "POST":
        try:
            role = request.form["role"]
            if role not in PERMISSIONS:
                raise ValueError("الدور المحدد غير صحيح.")
            selected = normalize_user_permissions(request.form.getlist("permissions"))
            connection.execute("INSERT INTO users(username,full_name,password_hash,role,created_at,permissions_json) VALUES(?,?,?,?,?,?)",
                               (request.form["username"].strip(), request.form["full_name"].strip(), password_hash(request.form["password"]), role, now(), json.dumps(selected, ensure_ascii=False)))
            connection.commit()
            log("user_created", request.form["username"])
            flash("تم إنشاء المستخدم.", "success")
        except sqlite3.IntegrityError:
            flash("اسم المستخدم مستخدم بالفعل.", "error")
        except ValueError as exc:
            flash(str(exc), "error")
    rows = connection.execute("SELECT id,username,full_name,role,active,created_at,permissions_json FROM users ORDER BY id").fetchall()
    connection.close()
    return render_template("users.html", users=rows, role_permissions={role: sorted(values) for role, values in PERMISSIONS.items()}, default_permissions=sorted(PERMISSIONS.get("admin", set())), all_permissions=ALL_PERMISSIONS, permission_labels=PERMISSION_LABELS)


@app.route("/users/<int:user_id>/edit",methods=["GET","POST"])
@requires("users.manage")
def edit_user(user_id):
    connection=db(); user=connection.execute("SELECT id,username,full_name,role,active,permissions_json FROM users WHERE id=?",(user_id,)).fetchone()
    if not user:
        connection.close(); flash("المستخدم غير موجود.","error"); return redirect(url_for("users"))
    if request.method=="POST":
        role=request.form.get("role")
        if role not in PERMISSIONS:
            flash("الدور المحدد غير صحيح.","error")
        else:
            try:
                selected = normalize_user_permissions(request.form.getlist("permissions"))
                if user_id == session.get("user_id"):
                    current_permissions = stored_user_permissions(user["permissions_json"], user["role"])
                    requested_active = 1 if request.form.get("active") == "yes" else 0
                    if role != user["role"] or selected != current_permissions or requested_active != user["active"]:
                        raise ValueError("لا يمكنك تغيير دورك أو صلاحياتك أو حالة حسابك من الجلسة الحالية.")
                connection.execute("UPDATE users SET username=?,full_name=?,role=?,active=?,permissions_json=? WHERE id=?",
                  (request.form["username"].strip(),request.form["full_name"].strip(),role,1 if request.form.get("active")=="yes" else 0,json.dumps(selected, ensure_ascii=False),user_id))
                connection.commit(); connection.close(); log("user_updated",request.form["username"].strip())
                flash("تم تعديل المستخدم.","success"); return redirect(url_for("users"))
            except sqlite3.IntegrityError:
                connection.rollback(); flash("اسم المستخدم مستخدم بالفعل.","error")
            except ValueError as exc:
                connection.rollback(); flash(str(exc),"error")
    selected_permissions = set(stored_user_permissions(user["permissions_json"] if "permissions_json" in user.keys() else None, user["role"]))
    connection.close(); return render_template("user_edit.html",user=user,roles=PERMISSIONS,all_permissions=ALL_PERMISSIONS,permission_labels=PERMISSION_LABELS,selected_permissions=selected_permissions)


@app.route("/users/<int:user_id>/delete",methods=["POST"])
@requires("users.manage")
def delete_user(user_id):
    connection=db(); user=connection.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
    if not user:
        flash("المستخدم غير موجود.","error")
    elif user_id==session.get("user_id"):
        flash("لا يمكنك حذف المستخدم الذي سجلت الدخول به.","error")
    else:
        checks=[
          ("SELECT 1 FROM audit_log WHERE user_id=? LIMIT 1","سجل المراجعة والتدقيق"),
          ("SELECT 1 FROM journal_entries WHERE created_by=? LIMIT 1","قيود اليومية"),
          ("SELECT 1 FROM stock_movements WHERE created_by=? LIMIT 1","حركات المخزون"),
          ("SELECT 1 FROM sales_invoices WHERE created_by=? LIMIT 1","فواتير البيع"),
          ("SELECT 1 FROM purchase_orders WHERE created_by=? LIMIT 1","أوامر الشراء"),
          ("SELECT 1 FROM goods_receipts WHERE created_by=? LIMIT 1","استلامات المشتريات"),
          ("SELECT 1 FROM supplier_payments WHERE created_by=? LIMIT 1","مدفوعات الموردين"),
          ("SELECT 1 FROM supplier_credit_notes WHERE created_by=? LIMIT 1","إشعارات دائنة المشتريات"),
          ("SELECT 1 FROM bank_transactions WHERE created_by=? LIMIT 1","الحركات البنكية")]
        used=next((label for q,label in checks if connection.execute(q,(user_id,)).fetchone()),None)
        if used: flash("لا يمكن حذف المستخدم لوجود نشاط مسجل في "+used+". يمكنك إيقافه بدل الحذف.","error")
        else:
            try:
                connection.execute("DELETE FROM users WHERE id=?",(user_id,))
                connection.commit()
                log("user_deleted", user["username"], entity_type="user", entity_id=user_id)
                flash("تم حذف المستخدم.","success")
            except sqlite3.IntegrityError: connection.rollback(); flash("لا يمكن حذف المستخدم لوجود سجلات مرتبطة به.","error")
    connection.close(); return redirect(url_for("users"))


@app.route("/data/export/<format>")
@requires("import_export.manage")
def export_data(format):
    connection = db()
    accounts_data = [dict(row) for row in connection.execute("SELECT code,name_ar,account_type,active FROM accounts ORDER BY code")]
    entries_data = [dict(row) for row in connection.execute("SELECT * FROM journal_entries ORDER BY id")]
    lines_data = [dict(row) for row in connection.execute("""
        SELECT jl.id,jl.entry_id,a.code account_code,jl.debit,jl.credit,jl.memo
        FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id ORDER BY jl.entry_id,jl.id
    """)]
    connection.close()
    if format == "json":
        return Response(json.dumps({"accounts": accounts_data, "journal_entries": entries_data,
            "journal_lines": lines_data}, ensure_ascii=False, indent=2), mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=nexus-export.json"})
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["code", "name_ar", "account_type", "active"])
        writer.writeheader()
        # Prevent spreadsheet formula execution when a text cell starts with
        # a formula marker (including markers hidden behind whitespace/control chars).
        def safe_csv_cell(value):
            text = "" if value is None else str(value)
            probe = text.lstrip(" \t\r\n")
            if probe.startswith(("=", "+", "-", "@")):
                return "'" + text
            return text
        for account_row in accounts_data:
            writer.writerow({key: safe_csv_cell(value) if key in {"code", "name_ar"} else value
                             for key, value in account_row.items()})
        return Response(output.getvalue().encode("utf-8-sig"), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=chart-of-accounts.csv"})
    return "Unsupported format", 400


@app.route("/data/import/accounts", methods=["POST"])
@requires("import_export.manage")
def import_accounts():
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".csv"):
        flash("اختر ملف CSV للحسابات.", "error"); return redirect(url_for("accounts"))
    raw = upload.stream.read()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = raw.decode("cp1256")
        except UnicodeDecodeError:
            flash("ترميز ملف CSV غير مدعوم. احفظه بصيغة UTF-8 أو Windows-1256.", "error")
            return redirect(url_for("accounts"))
    reader = csv.DictReader(io.StringIO(decoded))
    allowed_types = {"asset", "liability", "equity", "revenue", "expense"}
    connection, inserted, skipped = db(), 0, 0
    try:
        for row in reader:
            code = (row.get("code") or "").strip()
            name = (row.get("name_ar") or "").strip()
            account_type = (row.get("account_type") or "").strip().lower()
            if not code or not name or account_type not in allowed_types:
                skipped += 1
                continue
            try:
                connection.execute("INSERT INTO accounts(code,name_ar,account_type) VALUES(?,?,?)",
                                   (code, name, account_type))
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    log("accounts_imported", f"inserted={inserted}; skipped={skipped}")
    flash(f"تم استيراد {inserted} حسابًا، وتخطي {skipped} صفًا غير صالح أو مكرر.", "success" if skipped == 0 else "warning")
    return redirect(url_for("accounts"))


@app.route("/backup/create")
@requires("backup.manage")
def create_backup():
    connection = db()
    keep_row = connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='backup_keep_count'").fetchone()
    connection.close()
    filename = create_backup_archive(DB_PATH, BACKUP_DIR, int(keep_row["setting_value"]) if keep_row else 30)
    log("backup_created", filename.name)
    return send_file(filename, as_attachment=True, download_name=filename.name)


@app.route("/backup/download/<filename>")
@requires("backup.manage")
def backup_download(filename):
    if (Path(filename).name != filename or not filename.startswith("nexus-backup-")
            or not filename.endswith(".zip")):
        return "Not found", 404
    archive_path = BACKUP_DIR / filename
    if not archive_path.is_file():
        return "Not found", 404
    return send_file(archive_path, as_attachment=True, download_name=filename)


@app.route("/backup/settings", methods=["GET", "POST"])
@requires("backup.manage")
def backup_settings():
    connection = db()
    if request.method == "POST":
        try:
            enabled = request.form.get("enabled") == "1"
            frequency = request.form.get("frequency", "daily").strip()
            backup_time = request.form.get("backup_time", "").strip()
            weekday = int(request.form.get("weekday", "0"))
            month_day = int(request.form.get("month_day", "1"))
            interval_days = int(request.form.get("interval_days", "1"))
            keep_count = int(request.form.get("keep_count", "30"))
            if frequency not in {"daily", "weekly", "monthly", "every_n_days"}:
                raise ValueError("اختر تكرارًا صحيحًا للنسخ الاحتياطي.")
            if keep_count < 1 or keep_count > 365:
                raise ValueError("عدد النسخ المحتفظ بها يجب أن يكون من 1 إلى 365.")
            next_run = next_backup_datetime(frequency, backup_time, weekday=weekday,
                month_day=month_day, interval_days=interval_days)
            values = {
                "backup_enabled": "1" if enabled else "0",
                "backup_frequency": frequency, "backup_time": backup_time,
                "backup_weekday": str(weekday), "backup_month_day": str(month_day),
                "backup_interval_days": str(interval_days), "backup_keep_count": str(keep_count),
                "backup_next_run": next_run.isoformat(timespec="seconds"),
                "backup_last_status": "scheduled" if enabled else "disabled",
                "backup_last_error": "",
            }
            connection.executemany("""INSERT INTO company_settings(setting_key,setting_value)
                VALUES(?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
                list(values.items()))
            connection.commit()
            connection.close()
            log("backup_schedule_updated", f"{frequency}; next={next_run.isoformat(timespec='seconds')}")
            flash("تم حفظ إعدادات النسخ الاحتياطي التلقائي.", "success")
            return redirect(url_for("backup_settings"))
        except (ValueError, TypeError) as exc:
            connection.rollback()
            flash(str(exc) or "راجع إعدادات النسخ الاحتياطي.", "error")
    settings = {row["setting_key"]: row["setting_value"] for row in connection.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key LIKE 'backup_%'")}
    connection.close()
    files = sorted(BACKUP_DIR.glob("nexus-backup-*.zip"),
                   key=lambda item: item.stat().st_mtime, reverse=True)
    backup_files = [{"name": item.name, "size_mb": round(item.stat().st_size / 1024 / 1024, 2),
                     "modified": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
                    for item in files[:30]]
    return render_template("backup_settings.html", settings=settings, files=backup_files)


@app.route("/backup/run-now", methods=["POST"])
@requires("backup.manage")
def backup_run_now():
    connection = db()
    keep_row = connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='backup_keep_count'").fetchone()
    keep_count = int(keep_row["setting_value"]) if keep_row else 30
    connection.close()
    filename = create_backup_archive(DB_PATH, BACKUP_DIR, keep_count)
    connection = db()
    values = {"backup_last_run": now(), "backup_last_status": "success",
              "backup_last_error": "", "backup_last_file": filename.name}
    connection.executemany("""INSERT INTO company_settings(setting_key,setting_value)
        VALUES(?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
        list(values.items()))
    connection.commit()
    connection.close()
    log("backup_created", filename.name)
    flash("تم إنشاء النسخة الاحتياطية الآن بنجاح.", "success")
    return redirect(url_for("backup_settings"))


@app.route("/backup/restore", methods=["POST"])
@requires("backup.manage")
def restore_backup():
    upload = request.files.get("backup")
    if not upload or not upload.filename or not upload.filename.lower().endswith(".zip"):
        flash("اختر ملف نسخة احتياطية بصيغة ZIP.", "error")
        return redirect(url_for("backup_settings"))

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    temp_paths = []
    safety_path = None
    try:
        archive_fd, archive_name = tempfile.mkstemp(prefix="restore-upload-", suffix=".zip", dir=BACKUP_DIR)
        os.close(archive_fd)
        archive_path = Path(archive_name)
        temp_paths.append(archive_path)
        max_archive_bytes = 2 * 1024 * 1024 * 1024
        written = 0
        with archive_path.open("wb") as target:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_archive_bytes:
                    raise ValueError("حجم ملف النسخة الاحتياطية أكبر من الحد المسموح.")
                target.write(chunk)

        candidate_fd, candidate_name = tempfile.mkstemp(prefix="restore-candidate-", suffix=".db", dir=BACKUP_DIR)
        os.close(candidate_fd)
        candidate_path = Path(candidate_name)
        temp_paths.append(candidate_path)
        extract_validated_backup(archive_path, candidate_path)

        settings_connection = db()
        keep_row = settings_connection.execute(
            "SELECT setting_value FROM company_settings WHERE setting_key='backup_keep_count'"
        ).fetchone()
        keep_count = int(keep_row["setting_value"]) if keep_row else 30
        settings_connection.close()
        safety_archive = create_backup_archive(DB_PATH, BACKUP_DIR, keep_count)
        safety_fd, safety_name = tempfile.mkstemp(prefix="before-restore-", suffix=".db", dir=BACKUP_DIR)
        os.close(safety_fd)
        safety_path = Path(safety_name)
        temp_paths.append(safety_path)
        extract_validated_backup(safety_archive, safety_path)
        restore_database_with_rollback(candidate_path, DB_PATH, safety_path, initialize_database)

        log("backup_restored", upload.filename)
        flash("تم استرجاع النسخة بنجاح بعد فحصها، مع الاحتفاظ بنسخة أمان من البيانات السابقة.", "success")
    except (zipfile.BadZipFile, ValueError, sqlite3.DatabaseError, OSError, RuntimeError) as exc:
        flash(f"تعذرت الاستعادة: {exc}", "error")
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)
    return redirect(url_for("backup_settings"))


# Initialize schema for every WSGI entry point. The explicit skip switch is only
# intended for isolated route tests that provide their own temporary database.
if os.environ.get("NEXUS_SKIP_DB_INIT", "0").strip() != "1":
    initialize_database()
    start_backup_scheduler(DB_PATH, BACKUP_DIR)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
          threads=max(2, int(os.environ.get("NEXUS_THREADS", "8"))))
