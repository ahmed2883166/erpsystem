import csv
import hashlib
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
from accounting_core import reconciliation_snapshot

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "nexus.db"
BACKUP_DIR = DATA_DIR / "backups"

app = Flask(__name__)
app.register_blueprint(stock_bp)
app.register_blueprint(purchase_bp)
app.config["SECRET_KEY"] = os.environ.get("NEXUS_SECRET_KEY", "change-this-secret-before-production")

PERMISSIONS = {
    "admin": {"accounts.manage", "journal.manage", "reports.view", "users.manage", "backup.manage", "import_export.manage", "stock.manage", "stock.view", "purchases.manage", "purchases.view"},
    "accountant": {"accounts.manage", "journal.manage", "reports.view", "import_export.manage", "stock.manage", "stock.view", "purchases.manage", "purchases.view"},
    "viewer": {"reports.view", "stock.view", "purchases.view"},
}


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def password_hash(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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
        connection.close()
        if user and user["password_hash"] == password_hash(request.form["password"]):
            session.clear()
            session.update(user_id=user["id"], username=user["username"], full_name=user["full_name"], role=user["role"])
            log("login", "تسجيل دخول")
            return redirect(url_for("dashboard"))
        flash("بيانات الدخول غير صحيحة.", "error")
    return render_template("login.html")


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
        tax_regime = request.form.get("tax_regime", "CUSTOM")
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
            "default_vat_rate": request.form.get("default_vat_rate", "0").strip()
        }
        try:
            vat_rate = float(settings_values["default_vat_rate"])
            if not 0 <= vat_rate <= 100 or not settings_values["company_name"]:
                raise ValueError()
        except ValueError:
            flash("أدخل اسم شركة ومعدل ضريبة صحيحًا من 0 إلى 100.", "error")
            connection.close()
            return redirect(url_for("company_setup"))
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
        if request.form.get("replace_chart") == "yes":
            has_entries = connection.execute("SELECT 1 FROM journal_entries LIMIT 1").fetchone()
            if has_entries:
                flash("لا يمكن استبدال دليل الحسابات بعد وجود قيود يومية.", "error")
                connection.rollback()
                connection.close()
                return redirect(url_for("company_setup"))
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
            connection.execute("INSERT INTO accounts(code,name_ar,account_type,parent_id) VALUES(?,?,?,?)",
                               (request.form["code"].strip(), request.form["name_ar"].strip(), request.form["account_type"], request.form.get("parent_id") or None))
            connection.commit()
            log("account_created", request.form["code"])
            flash("تمت إضافة الحساب.", "success")
        except sqlite3.IntegrityError:
            flash("رمز الحساب مستخدم بالفعل.", "error")
    rows = connection.execute("SELECT * FROM accounts ORDER BY code").fetchall()
    connection.close()
    return render_template("accounts.html", accounts=rows)


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
            cursor = connection.execute("INSERT INTO journal_entries(entry_date,reference,description,created_by,created_at) VALUES(?,?,?,?,?)",
                                        (request.form["entry_date"], request.form.get("reference", ""), request.form["description"].strip(), session["user_id"], now()))
            connection.executemany("INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?)",
                                   [(cursor.lastrowid, *line) for line in lines])
            connection.commit()
            log("journal_posted", f"قيد #{cursor.lastrowid}")
            flash("تم ترحيل القيد بنجاح.", "success")
            return redirect(url_for("journal"))
    entries = connection.execute("SELECT * FROM journal_entries ORDER BY entry_date DESC, id DESC LIMIT 20").fetchall()
    connection.close()
    return render_template("journal.html", accounts=accounts_list, entries=entries, today=datetime.now().date().isoformat())


@app.route("/reports/trial-balance")
@requires("reports.view")
def trial_balance():
    connection = db()
    rows = connection.execute("""SELECT a.code, a.name_ar, a.account_type,
        COALESCE(SUM(jl.debit),0) debit, COALESCE(SUM(jl.credit),0) credit
        FROM accounts a LEFT JOIN journal_lines jl ON jl.account_id=a.id
        GROUP BY a.id ORDER BY a.code""").fetchall()
    connection.close()
    return render_template("trial_balance.html", rows=rows)


@app.route("/reports/ledger")
@requires("reports.view")
def ledger():
    account_id = request.args.get("account_id", type=int)
    connection = db()
    accounts_list = connection.execute("SELECT * FROM accounts ORDER BY code").fetchall()
    rows, selected = [], None
    if account_id:
        selected = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        rows = connection.execute("""SELECT je.entry_date, je.reference, je.description, jl.debit, jl.credit, jl.memo
            FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id WHERE jl.account_id=? ORDER BY je.entry_date, jl.id""", (account_id,)).fetchall()
    connection.close()
    return render_template("ledger.html", accounts=accounts_list, selected=selected, rows=rows)


@app.route("/reports/reconciliation")
@requires("reports.view")
def reconciliation():
    connection=db()
    snapshot=reconciliation_snapshot(connection)
    snapshot["grni"]=connection.execute("SELECT COALESCE(SUM(CAST(r.total_value AS REAL)),0) total FROM goods_receipts r WHERE r.status='posted' AND NOT EXISTS(SELECT 1 FROM supplier_bill_receipts x WHERE x.receipt_id=r.id)").fetchone()["total"]
    snapshot["open_ap"]=connection.execute("SELECT COALESCE(SUM(CAST(total_amount AS REAL)-CAST(paid_amount AS REAL)),0) total FROM supplier_bills WHERE status='posted'").fetchone()["total"]
    settings={r["setting_key"]:r["setting_value"] for r in connection.execute("SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('company_name','base_currency')")}
    connection.close()
    return render_template("reconciliation.html",snapshot=snapshot,settings=settings)

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
