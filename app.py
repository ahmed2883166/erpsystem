import csv
import hashlib
import hmac
import io
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, flash, redirect, render_template, request, send_file, session, url_for
from chart_templates import CHARTS, ACTIVITY_LABELS
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
from currency_center import currency_bp, initialize_currency_schema
from party_reports import party_bp
from report_exports import register_report_exports

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "nexus.db"
BACKUP_DIR = DATA_DIR / "backups"

app = Flask(__name__)
app.register_blueprint(stock_bp)
app.register_blueprint(purchase_bp)
app.register_blueprint(sales_bp)
app.register_blueprint(bank_bp)
app.register_blueprint(assets_bp)
app.register_blueprint(aging_bp)
app.register_blueprint(quotes_bp)
app.register_blueprint(tax_bp)
app.register_blueprint(workforce_bp)
app.register_blueprint(currency_bp)
app.register_blueprint(party_bp)
app.config["SECRET_KEY"] = os.environ.get("NEXUS_SECRET_KEY", "change-this-secret-before-production")

PERMISSIONS = {
    "admin": {"accounts.manage", "journal.manage", "reports.view", "users.manage", "backup.manage", "import_export.manage", "stock.manage", "stock.view", "purchases.manage", "purchases.view", "sales.manage", "sales.view", "assets.manage", "assets.view", "quotes.manage", "quotes.view"},
    "accountant": {"accounts.manage", "journal.manage", "reports.view", "import_export.manage", "stock.manage", "stock.view", "purchases.manage", "purchases.view", "sales.manage", "sales.view", "assets.manage", "assets.view", "quotes.manage", "quotes.view"},
    "viewer": {"reports.view", "stock.view", "purchases.view", "sales.view"},
}

register_report_exports(app, DB_PATH, PERMISSIONS)


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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
    BACKUP_DIR.mkdir(exist_ok=True)
    connection = db()
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
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
            account_id INTEGER NOT NULL REFERENCES accounts(id), debit REAL NOT NULL DEFAULT 0,
            credit REAL NOT NULL DEFAULT 0, memo TEXT
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
    """)
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
        connection.execute("INSERT INTO users(username, full_name, password_hash, role, created_at) VALUES(?,?,?,?,?)",
                           ("admin", "مدير النظام", password_hash("admin123"), "admin", now()))
    if not connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        connection.executemany("INSERT INTO accounts(code, name_ar, account_type) VALUES(?,?,?)", [
            ("1000", "الصندوق والبنوك", "asset"), ("1100", "العملاء", "asset"),
            ("1200", "المخزون", "asset"), ("2000", "الموردون", "liability"),
            ("3000", "رأس المال", "equity"), ("4000", "إيرادات المبيعات والخدمات", "revenue"),
            ("5000", "تكلفة المبيعات", "expense"), ("6000", "المصروفات التشغيلية", "expense"),
        ])
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
    initialize_currency_schema()


def now():
    return datetime.now().isoformat(timespec="seconds")


def log(action, details=""):
    connection = db()
    connection.execute("INSERT INTO audit_log(user_id, action, details, created_at) VALUES(?,?,?,?)",
                       (session.get("user_id"), action, details, now()))
    connection.commit()
    connection.close()


def requires(permission):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if permission not in PERMISSIONS.get(session.get("role"), set()):
                flash("لا تملك الصلاحية المطلوبة.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def shared_context():
    return {"permissions": PERMISSIONS.get(session.get("role"), set()), "year": datetime.now().year}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        connection = db()
        user = connection.execute("SELECT * FROM users WHERE username=? AND active=1", (request.form["username"],)).fetchone()
        valid = bool(user and verify_password(request.form["password"], user["password_hash"]))
        if valid and password_hash_needs_upgrade(user["password_hash"]):
            connection.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (password_hash(request.form["password"]), user["id"]))
            connection.commit()
        connection.close()
        if valid:
            session.clear()
            session.update(user_id=user["id"], username=user["username"], full_name=user["full_name"], role=user["role"])
            log("login", "تسجيل دخول")
            return redirect(url_for("dashboard"))
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
                connection.execute("UPDATE users SET password_hash=? WHERE id=?",
                                   (password_hash(new), session["user_id"]))
                connection.commit()
                connection.close()
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
            connection.execute("DELETE FROM accounts")
            inserted = {}
            for code, name, account_type, parent_code, is_group in CHARTS[activity]:
                parent_id = inserted.get(parent_code)
                cur = connection.execute(
                    "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group) VALUES(?,?,?,?,?)",
                    (code, name, account_type, parent_id, is_group)
                )
                inserted[code] = cur.lastrowid
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


@app.route("/")
@requires("reports.view")
def dashboard():
    connection = db()
    totals = connection.execute("SELECT COUNT(*) accounts, (SELECT COUNT(*) FROM journal_entries) entries, (SELECT COUNT(*) FROM users WHERE active=1) users FROM accounts").fetchone()
    balance = connection.execute("SELECT COALESCE(SUM(debit),0) debit, COALESCE(SUM(credit),0) credit FROM journal_lines").fetchone()
    revenue = connection.execute("SELECT COALESCE(SUM(jl.credit-jl.debit),0) total FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id WHERE a.account_type='revenue'").fetchone()["total"]
    expenses = connection.execute("SELECT COALESCE(SUM(jl.debit-jl.credit),0) total FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id WHERE a.account_type='expense'").fetchone()["total"]
    recent = connection.execute("SELECT * FROM journal_entries ORDER BY id DESC LIMIT 5").fetchall()
    currency_row=connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='base_currency'").fetchone()
    base_currency=currency_row["setting_value"] if currency_row else "EGP"
    company_row=connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='company_name'").fetchone()
    company_name=company_row["setting_value"] if company_row else "NEXUS ERP"
    connection.close()
    return render_template("dashboard.html", totals=totals, balance=balance, revenue=revenue, expenses=expenses, recent=recent, base_currency=base_currency, company_name=company_name)


@app.route("/accounts", methods=["GET", "POST"])
@requires("accounts.manage")
def accounts():
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


@app.route("/journal", methods=["GET", "POST"])
@requires("journal.manage")
def journal():
    connection = db()
    accounts_list = connection.execute("SELECT * FROM accounts WHERE active=1 AND is_group=0 ORDER BY code").fetchall()
    if request.method == "POST":
        account_ids = request.form.getlist("account_id")
        debits = request.form.getlist("debit")
        credits = request.form.getlist("credit")
        memos = request.form.getlist("memo")
        lines = []
        for account_id, debit, credit, memo in zip(account_ids, debits, credits, memos):
            debit, credit = float(debit or 0), float(credit or 0)
            if debit or credit:
                lines.append((int(account_id), debit, credit, memo.strip()))
        total_debit, total_credit = sum(x[1] for x in lines), sum(x[2] for x in lines)
        if len(lines) < 2 or abs(total_debit - total_credit) > 0.005 or total_debit <= 0:
            flash("يجب إدخال سطرين على الأقل وأن يتساوى إجمالي المدين والدائن.", "error")
        else:
            try:
                reference=request.form.get("reference","").strip() or "MAN-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
                entry_id=post_journal(connection,request.form["entry_date"],reference,
                    request.form["description"].strip(),lines,session["user_id"])
                connection.commit()
                log("journal_posted", f"قيد #{entry_id}")
                flash("تم ترحيل القيد بنجاح.", "success")
                return redirect(url_for("journal"))
            except ValueError as exc:
                connection.rollback()
                flash(str(exc),"error")
    entries = connection.execute("SELECT * FROM journal_entries ORDER BY entry_date DESC, id DESC LIMIT 20").fetchall()
    connection.close()
    return render_template("journal.html", accounts=accounts_list, entries=entries, today=datetime.now().date().isoformat())


@app.route("/reports/trial-balance")
@requires("reports.view")
def trial_balance():
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    account_type = request.args.get("account_type", "").strip()
    q = request.args.get("q", "").strip()
    allowed_types = {"asset", "liability", "equity", "revenue", "expense"}
    if account_type not in allowed_types:
        account_type = ""
    conditions, params = ["1=1"], []
    if date_from:
        conditions.append("je.entry_date >= ?"); params.append(date_from)
    if date_to:
        conditions.append("je.entry_date <= ?"); params.append(date_to)
    if account_type:
        conditions.append("a.account_type = ?"); params.append(account_type)
    if q:
        conditions.append("(a.code LIKE ? OR a.name_ar LIKE ?)"); params.extend([f"%{q}%", f"%{q}%"])
    connection = db()
    rows = connection.execute(f"""SELECT a.id, a.code, a.name_ar, a.account_type,
        COALESCE(SUM(jl.debit),0) debit, COALESCE(SUM(jl.credit),0) credit
        FROM accounts a LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON je.id=jl.entry_id AND je.status='posted'
        WHERE {' AND '.join(conditions)}
        GROUP BY a.id ORDER BY a.code""", params).fetchall()
    settings = {r["setting_key"]: r["setting_value"] for r in connection.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('base_currency')")}
    connection.close()
    types = {"asset":"أصول","liability":"التزامات","equity":"حقوق ملكية","revenue":"إيرادات","expense":"مصروفات"}
    return render_template("trial_balance.html", rows=rows, date_from=date_from, date_to=date_to,
        account_type=account_type, q=q, types=types, base_currency=settings.get("base_currency",""))


@app.route("/reports/ledger")
@requires("reports.view")
def ledger():
    account_id = request.args.get("account_id", type=int)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    connection = db()
    accounts_list = connection.execute("SELECT * FROM accounts WHERE active=1 ORDER BY code").fetchall()
    rows, selected = [], None
    if account_id:
        selected = connection.execute("SELECT * FROM accounts WHERE id=? AND is_group=0", (account_id,)).fetchone()
        if selected:
            conditions = ["jl.account_id=?", "je.status='posted'"]
            params = [account_id]
            if date_from:
                conditions.append("je.entry_date >= ?"); params.append(date_from)
            if date_to:
                conditions.append("je.entry_date <= ?"); params.append(date_to)
            if date_from:
                opening_row = connection.execute("""SELECT COALESCE(SUM(jl.debit-jl.credit),0) balance
                    FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE jl.account_id=? AND je.status='posted' AND je.entry_date<?""",
                    (account_id, date_from)).fetchone()
                opening_balance = float(opening_row["balance"] or 0)
            else:
                opening_balance = 0.0
            raw_rows = connection.execute(f"""SELECT je.id entry_id, je.entry_date, je.reference, je.description,
                jl.debit, jl.credit, jl.memo,
                (SELECT GROUP_CONCAT(a2.code || ' - ' || a2.name_ar, '، ')
                   FROM journal_lines jl2 JOIN accounts a2 ON a2.id=jl2.account_id
                  WHERE jl2.entry_id=je.id AND jl2.account_id<>jl.account_id) counterpart_accounts
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE {' AND '.join(conditions)} ORDER BY je.entry_date, jl.id""", params).fetchall()
            running_balance = opening_balance
            rows = []
            for row in raw_rows:
                item = dict(row)
                running_balance += float(item["debit"] or 0) - float(item["credit"] or 0)
                item["running_balance"] = round(running_balance, 2)
                rows.append(item)
            closing_balance = round(running_balance, 2)
        else:
            opening_balance = 0.0
            closing_balance = 0.0
    else:
        opening_balance = 0.0
        closing_balance = 0.0
    connection.close()
    return render_template("ledger.html", accounts=accounts_list, selected=selected, rows=rows,
        date_from=date_from, date_to=date_to, opening_balance=opening_balance, closing_balance=closing_balance)


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
            connection.execute("INSERT INTO users(username,full_name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                               (request.form["username"].strip(), request.form["full_name"].strip(), password_hash(request.form["password"]), request.form["role"], now()))
            connection.commit()
            log("user_created", request.form["username"])
            flash("تم إنشاء المستخدم.", "success")
        except sqlite3.IntegrityError:
            flash("اسم المستخدم مستخدم بالفعل.", "error")
    rows = connection.execute("SELECT id,username,full_name,role,active,created_at FROM users ORDER BY id").fetchall()
    connection.close()
    return render_template("users.html", users=rows, role_permissions=PERMISSIONS)


@app.route("/users/<int:user_id>/edit",methods=["GET","POST"])
@requires("users.manage")
def edit_user(user_id):
    connection=db(); user=connection.execute("SELECT id,username,full_name,role,active FROM users WHERE id=?",(user_id,)).fetchone()
    if not user:
        connection.close(); flash("المستخدم غير موجود.","error"); return redirect(url_for("users"))
    if request.method=="POST":
        role=request.form.get("role")
        if role not in PERMISSIONS:
            flash("الدور المحدد غير صحيح.","error")
        else:
            try:
                connection.execute("UPDATE users SET username=?,full_name=?,role=?,active=? WHERE id=?",
                  (request.form["username"].strip(),request.form["full_name"].strip(),role,1 if request.form.get("active")=="yes" else 0,user_id))
                connection.commit(); connection.close(); log("user_updated",request.form["username"].strip())
                flash("تم تعديل المستخدم.","success"); return redirect(url_for("users"))
            except sqlite3.IntegrityError:
                connection.rollback(); flash("اسم المستخدم مستخدم بالفعل.","error")
    connection.close(); return render_template("user_edit.html",user=user,roles=PERMISSIONS)


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
          ("SELECT 1 FROM bank_transactions WHERE created_by=? LIMIT 1","الحركات البنكية")]
        used=next((label for q,label in checks if connection.execute(q,(user_id,)).fetchone()),None)
        if used: flash("لا يمكن حذف المستخدم لوجود نشاط مسجل في "+used+". يمكنك إيقافه بدل الحذف.","error")
        else:
            try: connection.execute("DELETE FROM users WHERE id=?",(user_id,)); connection.commit(); flash("تم حذف المستخدم.","success")
            except sqlite3.IntegrityError: connection.rollback(); flash("لا يمكن حذف المستخدم لوجود سجلات مرتبطة به.","error")
    connection.close(); return redirect(url_for("users"))


@app.route("/data/export/<format>")
@requires("import_export.manage")
def export_data(format):
    connection = db()
    accounts_data = [dict(row) for row in connection.execute("SELECT code,name_ar,account_type,active FROM accounts ORDER BY code")]
    entries_data = [dict(row) for row in connection.execute("SELECT * FROM journal_entries ORDER BY id")]
    connection.close()
    if format == "json":
        return Response(json.dumps({"accounts": accounts_data, "journal_entries": entries_data}, ensure_ascii=False, indent=2), mimetype="application/json", headers={"Content-Disposition": "attachment; filename=nexus-export.json"})
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["code", "name_ar", "account_type", "active"])
        writer.writeheader(); writer.writerows(accounts_data)
        return Response(output.getvalue().encode("utf-8-sig"), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=chart-of-accounts.csv"})
    return "Unsupported format", 400


@app.route("/data/import/accounts", methods=["POST"])
@requires("import_export.manage")
def import_accounts():
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".csv"):
        flash("اختر ملف CSV للحسابات.", "error"); return redirect(url_for("accounts"))
    reader = csv.DictReader(io.StringIO(upload.stream.read().decode("utf-8-sig")))
    connection, count = db(), 0
    for row in reader:
        if row.get("code") and row.get("name_ar") and row.get("account_type"):
            connection.execute("INSERT OR IGNORE INTO accounts(code,name_ar,account_type) VALUES(?,?,?)", (row["code"].strip(), row["name_ar"].strip(), row["account_type"].strip()))
            count += 1
    connection.commit(); connection.close()
    log("accounts_imported", str(count)); flash(f"تمت معالجة {count} صفًا.", "success")
    return redirect(url_for("accounts"))


@app.route("/backup/create")
@requires("backup.manage")
def create_backup():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = BACKUP_DIR / f"nexus-backup-{stamp}.zip"
    with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(DB_PATH, "nexus.db")
        archive.writestr("manifest.json", json.dumps({"application": "NEXUS ERP", "created_at": now()}, ensure_ascii=False))
    log("backup_created", filename.name)
    return send_file(filename, as_attachment=True, download_name=filename.name)


@app.route("/backup/restore", methods=["POST"])
@requires("backup.manage")
def restore_backup():
    upload = request.files.get("backup")
    if not upload or not upload.filename.endswith(".zip"):
        flash("اختر ملف نسخة احتياطية بصيغة ZIP.", "error"); return redirect(url_for("dashboard"))
    temp = BACKUP_DIR / "restore-temp.zip"; upload.save(temp)
    try:
        with zipfile.ZipFile(temp) as archive:
            if "nexus.db" not in archive.namelist(): raise ValueError("ملف غير صالح")
            safety_copy = BACKUP_DIR / f"before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
            shutil.copy2(DB_PATH, safety_copy)
            with archive.open("nexus.db") as source, open(DB_PATH, "wb") as target: shutil.copyfileobj(source, target)
        log("backup_restored", upload.filename); flash("تمت الاستعادة مع حفظ نسخة أمان من قاعدة البيانات السابقة.", "success")
    except (zipfile.BadZipFile, ValueError):
        flash("ملف النسخة الاحتياطية غير صالح.", "error")
    finally:
        temp.unlink(missing_ok=True)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    initialize_database()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
