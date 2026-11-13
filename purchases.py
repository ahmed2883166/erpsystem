from app_paths import DB_PATH
from database import connect_database, fetch_page
"""NEXUS ERP purchasing v1: suppliers, purchase orders, GRNI receipts, bills and payments.
All posting is transactional and idempotent. Tax is posted only when an explicit input-tax account exists.
"""
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from functools import wraps
from datetime import date, datetime
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal, money
from stock import consume_layers, q4, make_lot
from analytic_dimensions import initialize_analytic_dimensions_schema, validate_journal_dimensions, active_dimension_options

purchase_bp = Blueprint("purchase", __name__, url_prefix="/purchases")

def db():
    return connect_database(DB_PATH, isolation_level=None)

def init_purchase_schema():
    c=db()
    initialize_analytic_dimensions_schema(c)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS suppliers(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      tax_number TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
      email TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS purchase_orders(
      id INTEGER PRIMARY KEY, po_no TEXT NOT NULL UNIQUE, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      order_date TEXT NOT NULL, expected_date TEXT, status TEXT NOT NULL DEFAULT 'draft',
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      submitted_by INTEGER, submitted_at TEXT, approved_by INTEGER, approved_at TEXT,
      rejected_by INTEGER, rejected_at TEXT, rejection_reason TEXT, approval_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS purchase_order_lines(
      id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES stock_products(id), quantity TEXT NOT NULL,
      unit_price TEXT NOT NULL, received_qty TEXT NOT NULL DEFAULT '0'
    );
    CREATE TABLE IF NOT EXISTS goods_receipts(
      id INTEGER PRIMARY KEY, receipt_no TEXT NOT NULL UNIQUE, po_id INTEGER REFERENCES purchase_orders(id),
      supplier_id INTEGER NOT NULL REFERENCES suppliers(id), warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      receipt_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'posted',
      journal_entry_id INTEGER UNIQUE, total_value TEXT NOT NULL DEFAULT '0',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS goods_receipt_lines(
      id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
      po_line_id INTEGER REFERENCES purchase_order_lines(id), product_id INTEGER NOT NULL REFERENCES stock_products(id),
      quantity TEXT NOT NULL, unit_cost TEXT NOT NULL, total_cost TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS supplier_bills(
      id INTEGER PRIMARY KEY, bill_no TEXT NOT NULL UNIQUE, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      bill_date TEXT NOT NULL, due_date TEXT, supplier_invoice_no TEXT NOT NULL, subtotal TEXT NOT NULL,
      tax_rate TEXT NOT NULL DEFAULT '0', tax_amount TEXT NOT NULL DEFAULT '0',
      total_amount TEXT NOT NULL, paid_amount TEXT NOT NULL DEFAULT '0',
      credited_amount TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'posted', journal_entry_id INTEGER UNIQUE,
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS supplier_bill_receipts(
      bill_id INTEGER NOT NULL REFERENCES supplier_bills(id), receipt_id INTEGER NOT NULL REFERENCES goods_receipts(id),
      matched_amount TEXT NOT NULL, PRIMARY KEY(bill_id,receipt_id)
    );
    CREATE TABLE IF NOT EXISTS supplier_credit_notes(
      id INTEGER PRIMARY KEY, credit_no TEXT NOT NULL UNIQUE,
      supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      bill_id INTEGER NOT NULL REFERENCES supplier_bills(id),
      credit_date TEXT NOT NULL, supplier_credit_no TEXT NOT NULL,
      reason TEXT NOT NULL, subtotal TEXT NOT NULL, tax_rate TEXT NOT NULL,
      tax_amount TEXT NOT NULL, total_amount TEXT NOT NULL,
      journal_entry_id INTEGER UNIQUE, status TEXT NOT NULL DEFAULT 'posted',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(supplier_id,supplier_credit_no)
    );
    CREATE TABLE IF NOT EXISTS supplier_credit_note_lines(
      id INTEGER PRIMARY KEY,
      credit_note_id INTEGER NOT NULL REFERENCES supplier_credit_notes(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      quantity TEXT NOT NULL, unit_cost TEXT NOT NULL, total_cost TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS supplier_payments(
      id INTEGER PRIMARY KEY, payment_no TEXT NOT NULL UNIQUE, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      payment_date TEXT NOT NULL, amount TEXT NOT NULL, cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER UNIQUE, reference TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS supplier_payment_allocations(
      payment_id INTEGER NOT NULL REFERENCES supplier_payments(id), bill_id INTEGER NOT NULL REFERENCES supplier_bills(id),
      amount TEXT NOT NULL, PRIMARY KEY(payment_id,bill_id)
    );
    CREATE INDEX IF NOT EXISTS idx_purchase_bills_supplier ON supplier_bills(supplier_id,bill_date);
    CREATE INDEX IF NOT EXISTS idx_supplier_bill_invoice_lookup
      ON supplier_bills(supplier_id, supplier_invoice_no);
    CREATE INDEX IF NOT EXISTS idx_supplier_bill_receipt_lookup
      ON supplier_bill_receipts(receipt_id);
    CREATE INDEX IF NOT EXISTS idx_purchase_receipts_supplier ON goods_receipts(supplier_id,receipt_date);
    """)
    # suppliers now exists: install the supplier_id column on journal lines
    # through the same additive migration used by the other analytical dimensions.
    initialize_analytic_dimensions_schema(c)
    order_columns = {row["name"] for row in c.execute("PRAGMA table_info(purchase_orders)")}
    for name, declaration in (
        ("submitted_by", "INTEGER"), ("submitted_at", "TEXT"),
        ("approved_by", "INTEGER"), ("approved_at", "TEXT"),
        ("rejected_by", "INTEGER"), ("rejected_at", "TEXT"),
        ("rejection_reason", "TEXT"), ("approval_reason", "TEXT"),
    ):
        if name not in order_columns:
            c.execute("ALTER TABLE purchase_orders ADD COLUMN " + name + " " + declaration)
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_approval ON purchase_orders(status, created_at, id)")
    bill_columns = {row["name"] for row in c.execute("PRAGMA table_info(supplier_bills)")}
    if "credited_amount" not in bill_columns:
        c.execute("ALTER TABLE supplier_bills ADD COLUMN credited_amount TEXT NOT NULL DEFAULT '0'")
    if "due_date" not in bill_columns:
        c.execute("ALTER TABLE supplier_bills ADD COLUMN due_date TEXT")
        c.execute("UPDATE supplier_bills SET due_date=bill_date WHERE due_date IS NULL")
    for name, declaration in (
        ("asset_code", "TEXT"), ("asset_name", "TEXT"), ("asset_category", "TEXT"),
        ("asset_ready_date", "TEXT"), ("asset_residual_value", "TEXT NOT NULL DEFAULT '0'"),
        ("asset_useful_life_months", "INTEGER"),
        ("asset_depreciation_method", "TEXT NOT NULL DEFAULT 'straight_line'"),
        ("asset_annual_rate", "TEXT NOT NULL DEFAULT '0'"),
        ("asset_account_id", "INTEGER REFERENCES accounts(id)"),
        ("asset_accum_account_id", "INTEGER REFERENCES accounts(id)"),
        ("asset_expense_account_id", "INTEGER REFERENCES accounts(id)"),
        ("bill_kind", "TEXT NOT NULL DEFAULT 'inventory'"),
        ("expense_account_id", "INTEGER"),
        ("submitted_by", "INTEGER"), ("submitted_at", "TEXT"),
        ("approved_by", "INTEGER"), ("approved_at", "TEXT"),
        ("rejected_by", "INTEGER"), ("rejected_at", "TEXT"),
        ("rejection_reason", "TEXT"), ("approval_reason", "TEXT"),
    ):
        if name not in bill_columns:
            c.execute("ALTER TABLE supplier_bills ADD COLUMN " + name + " " + declaration)
    c.execute("""CREATE TABLE IF NOT EXISTS supplier_bill_asset_lines(
      id INTEGER PRIMARY KEY,
      bill_id INTEGER NOT NULL REFERENCES supplier_bills(id) ON DELETE CASCADE,
      line_no INTEGER NOT NULL,
      asset_code TEXT NOT NULL,
      asset_name TEXT NOT NULL,
      asset_category TEXT NOT NULL DEFAULT '',
      cost TEXT NOT NULL,
      ready_date TEXT NOT NULL,
      residual_value TEXT NOT NULL DEFAULT '0',
      useful_life_months INTEGER NOT NULL,
      depreciation_method TEXT NOT NULL DEFAULT 'straight_line',
      annual_rate TEXT NOT NULL DEFAULT '0',
      asset_account_id INTEGER NOT NULL REFERENCES accounts(id),
      accum_account_id INTEGER NOT NULL REFERENCES accounts(id),
      expense_account_id INTEGER NOT NULL REFERENCES accounts(id),
      UNIQUE(bill_id,line_no), UNIQUE(bill_id,asset_code)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_supplier_bill_asset_lines ON supplier_bill_asset_lines(bill_id,line_no)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_supplier_bills_approval ON supplier_bills(status, bill_date, id)")
    for name, table in (("branch_id", "branches"), ("cost_center_id", "cost_centers"), ("project_id", "projects")):
        if name not in bill_columns:
            c.execute("ALTER TABLE supplier_bills ADD COLUMN " + name + " INTEGER REFERENCES " + table + "(id)")
    receipt_line_columns = {row["name"] for row in c.execute("PRAGMA table_info(goods_receipt_lines)")}
    if "location_id" not in receipt_line_columns:
        c.execute("ALTER TABLE goods_receipt_lines ADD COLUMN location_id INTEGER REFERENCES stock_locations(id)")
    receipt_columns = {row["name"] for row in c.execute("PRAGMA table_info(goods_receipts)")}
    for name, table in (("branch_id", "branches"), ("cost_center_id", "cost_centers"), ("project_id", "projects")):
        if name not in receipt_columns:
            c.execute("ALTER TABLE goods_receipts ADD COLUMN " + name + " INTEGER REFERENCES " + table + "(id)")

    # Legacy-safe integrity migration: never fail application startup because
    # old posted documents contain duplicates. Keep every financial document,
    # report the duplicates, and rely on BEGIN IMMEDIATE + application guards
    # until an accountant resolves the legacy records and UNIQUE indexes can be added.
    c.execute("""CREATE TABLE IF NOT EXISTS purchase_integrity_issues(
      id INTEGER PRIMARY KEY, issue_type TEXT NOT NULL, issue_key TEXT NOT NULL,
      detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved INTEGER NOT NULL DEFAULT 0,
      resolved_by INTEGER, resolved_at TEXT,
      UNIQUE(issue_type,issue_key)
    )""")
    integrity_columns = {row["name"] for row in c.execute("PRAGMA table_info(purchase_integrity_issues)")}
    if "resolved_by" not in integrity_columns:
        c.execute("ALTER TABLE purchase_integrity_issues ADD COLUMN resolved_by INTEGER")
    if "resolved_at" not in integrity_columns:
        c.execute("ALTER TABLE purchase_integrity_issues ADD COLUMN resolved_at TEXT")
    duplicate_bills=c.execute("""SELECT supplier_id,lower(trim(supplier_invoice_no)) k,COUNT(*) n
      FROM supplier_bills GROUP BY supplier_id,k HAVING COUNT(*)>1""").fetchall()
    for row in duplicate_bills:
        c.execute("""INSERT INTO purchase_integrity_issues(issue_type,issue_key)
                     VALUES('duplicate_supplier_invoice',?)
                     ON CONFLICT(issue_type,issue_key) DO UPDATE SET
                       resolved=0,resolved_by=NULL,resolved_at=NULL""",
                  (str(row["supplier_id"])+":"+row["k"],))
    # One GRNI receipt may be invoiced in several partial supplier bills.
    # Drop the legacy one-receipt/one-bill index additively; existing allocation
    # rows and journal links remain untouched.
    c.execute("DROP INDEX IF EXISTS uq_supplier_bill_receipt_once")
    if not duplicate_bills:
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_bill_invoice_no
          ON supplier_bills(supplier_id, lower(trim(supplier_invoice_no)))""")
    c.commit()
    c.close()

def require(permission):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a,**kw):
            if "user_id" not in session: return redirect(url_for("login"))
            allowed={"purchases.manage":{"admin","accountant"},"purchases.view":{"admin","accountant","viewer"},"purchases.approve":{"admin"}}
            if (permission not in set(session["permissions"])) if "permissions" in session else (session.get("role") not in allowed.get(permission,set())):
                flash("لا تملك الصلاحية المطلوبة.","error"); return redirect(url_for("dashboard"))
            return fn(*a,**kw)
        return wrapped
    return deco

def submit_purchase_order(c, order_id, actor_id):
    c.execute("BEGIN IMMEDIATE")
    order = c.execute("SELECT * FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ValueError("أمر الشراء غير موجود.")
    if order["status"] not in {"draft", "rejected"}:
        raise ValueError("لا يمكن إرسال أمر الشراء للمراجعة من حالته الحالية.")
    if not c.execute("SELECT 1 FROM purchase_order_lines WHERE po_id=? LIMIT 1", (order_id,)).fetchone():
        raise ValueError("لا يمكن إرسال أمر شراء بدون بنود.")
    c.execute("""UPDATE purchase_orders SET status='pending_approval',
        submitted_by=?, submitted_at=CURRENT_TIMESTAMP, approved_by=NULL, approved_at=NULL,
        rejected_by=NULL, rejected_at=NULL, rejection_reason=NULL, approval_reason=NULL
        WHERE id=?""", (actor_id, order_id))


def approve_purchase_order(c, order_id, actor_id, reason=""):
    c.execute("BEGIN IMMEDIATE")
    order = c.execute("SELECT * FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ValueError("أمر الشراء غير موجود.")
    if order["status"] != "pending_approval":
        raise ValueError("أمر الشراء ليس بانتظار الاعتماد.")
    if order["created_by"] == actor_id or order["submitted_by"] == actor_id:
        raise ValueError("لا يجوز اعتماد أمر شراء أنشأته أو أرسلته للمراجعة بنفسك.")
    if order["created_by"] is None or order["submitted_by"] is None:
        raise ValueError("بيانات منشئ أو مرسل أمر الشراء غير مكتملة؛ راجع المستند.")
    c.execute("""UPDATE purchase_orders SET status='approved', approved_by=?,
        approved_at=CURRENT_TIMESTAMP, approval_reason=? WHERE id=? AND status='pending_approval'""",
        (actor_id, reason.strip(), order_id))
    if c.execute("SELECT changes()").fetchone()[0] != 1:
        raise ValueError("تغيرت حالة أمر الشراء؛ أعد تحميل الصفحة.")


def reject_purchase_order(c, order_id, actor_id, reason):
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("اكتب سبب رفض واضحًا (5 أحرف على الأقل).")
    c.execute("BEGIN IMMEDIATE")
    order = c.execute("SELECT * FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ValueError("أمر الشراء غير موجود.")
    if order["status"] != "pending_approval":
        raise ValueError("أمر الشراء ليس بانتظار الاعتماد.")
    if order["created_by"] == actor_id or order["submitted_by"] == actor_id:
        raise ValueError("لا يجوز رفض مستند أنشأته أو أرسلته للمراجعة بنفسك.")
    c.execute("""UPDATE purchase_orders SET status='rejected', rejected_by=?,
        rejected_at=CURRENT_TIMESTAMP, rejection_reason=? WHERE id=? AND status='pending_approval'""",
        (actor_id, reason, order_id))
    if c.execute("SELECT changes()").fetchone()[0] != 1:
        raise ValueError("تغيرت حالة أمر الشراء؛ أعد تحميل الصفحة.")

def account(c, code, typ=None):
    row=c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code=?",(code,)).fetchone()
    if not row or row["is_group"] or not row["active"] or (typ and row["account_type"]!=typ):
        raise ValueError(f"حساب الربط {code} غير موجود أو غير نشط/تفصيلي. راجع دليل الحسابات.")
    return row["id"]

def create_journal(c, d, ref, desc, lines, user, dimensions=None):
    return post_journal(c,d,ref,desc,lines,user,dimensions=dimensions)

def ensure_detail_account(c, code, name, typ, preferred_parent):
    # Account mappings must be configured in the chart of accounts; posting never creates GL accounts.
    found=c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code=?",(code,)).fetchone()
    if not found or found["is_group"] or not found["active"] or found["account_type"]!=typ:
        raise ValueError("حساب الربط المطلوب "+code+" ("+name+") غير مضبوط. أضفه كحساب تفصيلي نشط بالنوع "+typ+" قبل الترحيل.")
    return found["id"]

def _activity_posting_accounts(c):
    from activity_profiles import active_posting_accounts
    mapping = active_posting_accounts(c)
    if not mapping or not mapping.get("supplier_ap"):
        raise ValueError("ملف النشاط الحالي لا يحتوي حساب الموردين المطلوب للمشتريات.")
    return mapping

def _activity_tax_roles(c):
    from activity_profiles import active_tax_roles
    return active_tax_roles(c)


def supplier_ap_account(c):
    mapping=_activity_posting_accounts(c)
    return account(c,mapping["supplier_ap"],"liability")

def activity_inventory_code(c):
    return _activity_posting_accounts(c)["purchase_inventory"]


def validate_asset_bill_lines(c, form, bill_date, subtotal):
    """Validate repeated asset rows; retain backward compatibility with old forms."""
    fields = ("asset_code","asset_name","asset_category","asset_ready_date","asset_residual_value",
              "asset_useful_life_months","asset_depreciation_method","asset_annual_rate",
              "asset_account_id","asset_accum_account_id","asset_expense_account_id","asset_cost")
    rows = {name: form.getlist(name + "[]") for name in fields}
    count = max((len(values) for values in rows.values()), default=0)
    if count == 0:
        legacy = dict(form)
        return [dict(validate_asset_bill_data(c, legacy, bill_date, subtotal), cost=str(subtotal))]
    lines, seen, total = [], set(), money("0")
    for index in range(count):
        def val(name, default=""):
            values = rows[name]
            return values[index].strip() if index < len(values) and values[index] is not None else default
        cost = money(val("asset_cost", "0") or "0")
        line = {
            "asset_code": val("asset_code"), "asset_name": val("asset_name"),
            "asset_category": val("asset_category"), "asset_ready_date": val("asset_ready_date", bill_date) or bill_date,
            "asset_residual_value": val("asset_residual_value", "0") or "0",
            "asset_useful_life_months": val("asset_useful_life_months", "0") or "0",
            "asset_depreciation_method": val("asset_depreciation_method", "straight_line") or "straight_line",
            "asset_annual_rate": val("asset_annual_rate", "0") or "0",
            "asset_account_id": val("asset_account_id"),
            "asset_accum_account_id": val("asset_accum_account_id"),
            "asset_expense_account_id": val("asset_expense_account_id"),
        }
        if not line["asset_code"] and not line["asset_name"] and cost == 0:
            continue
        if line["asset_code"] in seen:
            raise ValueError("كود الأصل مكرر داخل الفاتورة: " + line["asset_code"])
        seen.add(line["asset_code"])
        validated = validate_asset_bill_data(c, line, bill_date, cost)
        validated["cost"] = str(cost)
        lines.append(validated)
        total = money(total + cost)
    if not lines:
        raise ValueError("أضف أصلًا واحدًا على الأقل إلى فاتورة الأصل.")
    if total != money(subtotal):
        raise ValueError("مجموع تكاليف الأصول يجب أن يساوي صافي فاتورة المورد قبل الضريبة.")
    return lines


def validate_asset_bill_data(c, form, bill_date, subtotal):
    """Validate asset capitalization details stored on a supplier bill."""
    asset_code = str(form.get("asset_code", "") or "").strip()
    asset_name = str(form.get("asset_name", "") or "").strip()
    if not asset_code or not asset_name:
        raise ValueError("كود الأصل واسم الأصل مطلوبان لفاتورة الأصل الثابت.")
    ready_date = date.fromisoformat(str(form.get("asset_ready_date", "") or bill_date)).isoformat()
    bill_date = date.fromisoformat(str(bill_date)).isoformat()
    if ready_date < bill_date:
        raise ValueError("تاريخ جاهزية الأصل لا يمكن أن يسبق تاريخ فاتورة الشراء.")
    residual = money(form.get("asset_residual_value", "0") or "0")
    life = int(form.get("asset_useful_life_months", 0) or 0)
    method = str(form.get("asset_depreciation_method", "straight_line") or "straight_line")
    annual_rate = Decimal(str(form.get("asset_annual_rate", "0") or "0"))
    if subtotal <= 0 or residual < 0 or residual >= subtotal or life <= 0:
        raise ValueError("تحقق من تكلفة الأصل والقيمة التخريدية والعمر الإنتاجي.")
    if method not in {"straight_line", "declining_balance"}:
        raise ValueError("طريقة إهلاك الأصل غير صحيحة.")
    if method == "declining_balance" and not (Decimal("0") < annual_rate <= Decimal("100")):
        raise ValueError("معدل الإهلاك السنوي يجب أن يكون أكبر من صفر ولا يتجاوز 100%.")
    ids = {}
    for field, expected, label in (
        ("asset_account_id", "asset", "حساب تكلفة الأصل"),
        ("asset_accum_account_id", "asset", "حساب مجمع الإهلاك"),
        ("asset_expense_account_id", "expense", "حساب مصروف الإهلاك"),
    ):
        try:
            account_id = int(form.get(field, 0))
        except (TypeError, ValueError):
            account_id = 0
        row = c.execute(
            "SELECT account_type,is_group,active FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row or row["account_type"] != expected or row["is_group"] or not row["active"]:
            raise ValueError("اختر " + label + " تفصيليًا ونشطًا ومن النوع الصحيح.")
        ids[field] = account_id
    if ids["asset_account_id"] == ids["asset_accum_account_id"]:
        raise ValueError("حساب تكلفة الأصل يجب أن يختلف عن حساب مجمع الإهلاك.")
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fixed_assets'"
    ).fetchone():
        existing = c.execute(
            "SELECT 1 FROM fixed_assets WHERE asset_code=? LIMIT 1", (asset_code,)
        ).fetchone()
        if existing:
            raise ValueError("كود الأصل مستخدم بالفعل في سجل الأصول.")
    return {
        "asset_code": asset_code, "asset_name": asset_name,
        "asset_category": str(form.get("asset_category", "") or "").strip(),
        "asset_ready_date": ready_date, "asset_residual_value": str(residual),
        "asset_useful_life_months": life, "asset_depreciation_method": method,
        "asset_annual_rate": str(annual_rate),
        "asset_account_id": ids["asset_account_id"],
        "asset_accum_account_id": ids["asset_accum_account_id"],
        "asset_expense_account_id": ids["asset_expense_account_id"],
    }

@purchase_bp.route("/")
@require("purchases.view")
def index():
    c=db()
    suppliers=c.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    orders_page = fetch_page(c,
        """SELECT p.*,s.name supplier_name FROM purchase_orders p
           JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC""",
        "SELECT COUNT(*) FROM purchase_orders",
        page=request.args.get("orders_page",1,type=int),
        per_page=request.args.get("orders_per_page",50,type=int))
    receipts_page = fetch_page(c,
        """SELECT r.*,s.name supplier_name FROM goods_receipts r
           JOIN suppliers s ON s.id=r.supplier_id ORDER BY r.id DESC""",
        "SELECT COUNT(*) FROM goods_receipts",
        page=request.args.get("receipts_page",1,type=int),
        per_page=request.args.get("receipts_per_page",50,type=int))
    bill_page = fetch_page(c,
        """SELECT b.*,s.name supplier_name FROM supplier_bills b
           JOIN suppliers s ON s.id=b.supplier_id ORDER BY b.id DESC""",
        "SELECT COUNT(*) FROM supplier_bills",
        page=request.args.get("bill_page",1,type=int),
        per_page=request.args.get("bill_per_page",50,type=int))
    payments_page = fetch_page(c,
        """SELECT p.*,s.name supplier_name FROM supplier_payments p
           JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC""",
        "SELECT COUNT(*) FROM supplier_payments",
        page=request.args.get("payments_page",1,type=int),
        per_page=request.args.get("payments_per_page",50,type=int))
    credit_notes_page = fetch_page(c,
        """SELECT n.*,s.name supplier_name,b.bill_no FROM supplier_credit_notes n
           JOIN suppliers s ON s.id=n.supplier_id JOIN supplier_bills b ON b.id=n.bill_id
           ORDER BY n.id DESC""",
        "SELECT COUNT(*) FROM supplier_credit_notes",
        page=request.args.get("credit_notes_page",1,type=int),
        per_page=request.args.get("credit_notes_per_page",50,type=int))
    orders, receipts, bills = orders_page["rows"], receipts_page["rows"], bill_page["rows"]
    payments, credit_notes = payments_page["rows"], credit_notes_page["rows"]
    c.close()
    return render_template("purchases.html",suppliers=suppliers,orders=orders,receipts=receipts,bills=bills,
        payments=payments,credit_notes=credit_notes,orders_page=orders_page,receipts_page=receipts_page,
        bill_page=bill_page,payments_page=payments_page,credit_notes_page=credit_notes_page,
        today=date.today().isoformat())

@purchase_bp.route("/integrity")
@require("purchases.view")
def integrity_issues():
    c=db()
    issues=c.execute("""SELECT * FROM purchase_integrity_issues
        WHERE resolved=0 ORDER BY detected_at,id""").fetchall()
    details=[]
    for issue in issues:
        key=issue["issue_key"]
        if issue["issue_type"]=="duplicate_supplier_invoice":
            supplier_text, invoice_key = key.split(":",1)
            rows=c.execute("""SELECT b.id,b.bill_no,b.supplier_invoice_no,b.bill_date,b.total_amount,
                s.name supplier_name
                FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
                WHERE b.supplier_id=? AND lower(trim(b.supplier_invoice_no))=?
                ORDER BY b.id""",(int(supplier_text),invoice_key)).fetchall()
            if len(rows)>1:
                details.append({"issue":issue,"kind":"duplicate_supplier_invoice","rows":rows,
                    "summary":"رقم فاتورة مورد مكرر لدى نفس المورد"})
        elif issue["issue_type"]=="duplicate_receipt_link":
            receipt_id=int(key)
            rows=c.execute("""SELECT r.id receipt_id,r.receipt_no,b.id bill_id,b.bill_no,
                b.supplier_invoice_no,s.name supplier_name,x.matched_amount
                FROM supplier_bill_receipts x
                JOIN goods_receipts r ON r.id=x.receipt_id
                JOIN supplier_bills b ON b.id=x.bill_id
                JOIN suppliers s ON s.id=b.supplier_id
                WHERE x.receipt_id=? ORDER BY b.id""",(receipt_id,)).fetchall()
            if len(rows)>1:
                details.append({"issue":issue,"kind":"duplicate_receipt_link","rows":rows,
                    "summary":"إيصال استلام مربوط بأكثر من فاتورة"})
    indexes = {
        row["name"] for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    index_status = {
        "supplier_invoice": "uq_supplier_bill_invoice_no" in indexes,
        "receipt_partial_allocation": "uq_supplier_bill_receipt_once" not in indexes,
    }
    c.close()
    return render_template(
        "purchase_integrity.html", issues=details, index_status=index_status
    )


@purchase_bp.route("/integrity/<int:issue_id>/resolve", methods=["POST"])
@require("purchases.manage")
def resolve_integrity_issue(issue_id):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        issue = c.execute(
            "SELECT * FROM purchase_integrity_issues WHERE id=? AND resolved=0",
            (issue_id,),
        ).fetchone()
        if not issue:
            raise ValueError("حالة المراجعة غير موجودة أو تم إغلاقها بالفعل.")

        key = issue["issue_key"]
        if issue["issue_type"] == "duplicate_supplier_invoice":
            supplier_text, invoice_key = key.split(":", 1)
            remaining = c.execute(
                """SELECT COUNT(*) FROM supplier_bills
                   WHERE supplier_id=? AND lower(trim(supplier_invoice_no))=?""",
                (int(supplier_text), invoice_key),
            ).fetchone()[0]
        elif issue["issue_type"] == "duplicate_receipt_link":
            remaining = c.execute(
                "SELECT COUNT(*) FROM supplier_bill_receipts WHERE receipt_id=?",
                (int(key),),
            ).fetchone()[0]
        else:
            raise ValueError("نوع حالة المراجعة غير معروف.")
        if remaining > 1:
            raise ValueError(
                "لا يمكن إغلاق الحالة: التكرار ما زال موجودًا. صحح المستندات أولًا ثم أعد المحاولة."
            )

        c.execute(
            """UPDATE purchase_integrity_issues
               SET resolved=1,resolved_by=?,resolved_at=CURRENT_TIMESTAMP
               WHERE id=? AND resolved=0""",
            (session["user_id"], issue_id),
        )
        c.commit()
        flash("تم إغلاق حالة المراجعة وتسجيل المستخدم وتاريخ الحل.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.integrity_issues"))


@purchase_bp.route("/suppliers",methods=["GET","POST"])
@require("purchases.manage")
def suppliers():
    c=db()
    if request.method=="POST":
        try:
            c.execute("INSERT INTO suppliers(code,name,tax_number,phone,email,address) VALUES(?,?,?,?,?,?)",
              (request.form["code"].strip(),request.form["name"].strip(),request.form.get("tax_number","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("address","").strip()))
            c.commit(); flash("تم حفظ المورد.","success")
        except sqlite3.IntegrityError: c.rollback(); flash("كود المورد مستخدم بالفعل.","error")
    suppliers_page = fetch_page(c,"SELECT * FROM suppliers ORDER BY name",
        "SELECT COUNT(*) FROM suppliers",
        page=request.args.get("suppliers_page",1,type=int),
        per_page=request.args.get("suppliers_per_page",50,type=int))
    suppliers_rows=suppliers_page["rows"]
    outstanding_by_supplier={}
    for bill in c.execute("""SELECT supplier_id,
        COALESCE(DECIMAL_SUM(total_amount),'0') total_amount,
        COALESCE(DECIMAL_SUM(paid_amount),'0') paid_amount,
        COALESCE(DECIMAL_SUM(credited_amount),'0') credited_amount
        FROM supplier_bills WHERE status='posted' GROUP BY supplier_id"""):
        outstanding_by_supplier[bill["supplier_id"]] = (
            Decimal(bill["total_amount"]) - Decimal(bill["paid_amount"]) - Decimal(bill["credited_amount"])
        )
    rows=[dict(row,outstanding=outstanding_by_supplier.get(row["id"],Decimal("0"))) for row in suppliers_rows]
    c.close()
    return render_template("purchase_suppliers.html",rows=rows,suppliers_page=suppliers_page)


@purchase_bp.route("/suppliers/<int:supplier_id>/edit",methods=["GET","POST"])
@require("purchases.manage")
def edit_supplier(supplier_id):
    c=db(); supplier=c.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
    if not supplier:
        c.close(); flash("المورد غير موجود.","error"); return redirect(url_for("purchase.suppliers"))
    used=any(c.execute(q,(supplier_id,)).fetchone() for q in [
      "SELECT 1 FROM purchase_orders WHERE supplier_id=? LIMIT 1",
      "SELECT 1 FROM goods_receipts WHERE supplier_id=? LIMIT 1",
      "SELECT 1 FROM supplier_bills WHERE supplier_id=? LIMIT 1",
      "SELECT 1 FROM supplier_payments WHERE supplier_id=? LIMIT 1"])
    if request.method=="POST":
        try:
            c.execute("UPDATE suppliers SET code=?,name=?,tax_number=?,phone=?,email=?,address=? WHERE id=?",
              (request.form["code"].strip(),request.form["name"].strip(),request.form.get("tax_number","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("address","").strip(),supplier_id))
            c.commit(); c.close(); flash("تم تعديل بيانات المورد.","success"); return redirect(url_for("purchase.suppliers"))
        except sqlite3.IntegrityError: c.rollback(); flash("كود المورد مستخدم بالفعل.","error")
    c.close(); return render_template("purchase_supplier_edit.html",supplier=supplier,used=used)


@purchase_bp.route("/suppliers/<int:supplier_id>/delete",methods=["POST"])
@require("purchases.manage")
def delete_supplier(supplier_id):
    c=db(); supplier=c.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
    if not supplier: flash("المورد غير موجود.","error")
    else:
        checks=[
          ("SELECT 1 FROM purchase_orders WHERE supplier_id=? LIMIT 1","أوامر الشراء"),
          ("SELECT 1 FROM goods_receipts WHERE supplier_id=? LIMIT 1","استلامات المشتريات"),
          ("SELECT 1 FROM supplier_bills WHERE supplier_id=? LIMIT 1","فواتير الموردين"),
          ("SELECT 1 FROM supplier_payments WHERE supplier_id=? LIMIT 1","مدفوعات الموردين")]
        used=next((label for q,label in checks if c.execute(q,(supplier_id,)).fetchone()),None)
        if used: flash("لا يمكن حذف المورد لوجود "+used+" مرتبطة به.","error")
        else:
            try: c.execute("DELETE FROM suppliers WHERE id=?",(supplier_id,)); c.commit(); flash("تم حذف المورد.","success")
            except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف المورد لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("purchase.suppliers"))

@purchase_bp.route("/orders/new",methods=["GET","POST"])
@require("purchases.manage")
def new_order():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"])
            qty=Decimal(request.form["quantity"]); price=money(request.form["unit_price"])
            if qty<=0 or price<0: raise ValueError("الكمية والسعر غير صحيحين.")
            c.execute("BEGIN IMMEDIATE")
            no="PO-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            cur=c.execute("INSERT INTO purchase_orders(po_no,supplier_id,order_date,expected_date,notes,created_by) VALUES(?,?,?,?,?,?)",
              (no,supplier,request.form["order_date"],request.form.get("expected_date") or None,request.form.get("notes",""),session["user_id"]))
            c.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",(cur.lastrowid,product,str(qty),str(price)))
            c.commit(); flash(f"تم إنشاء أمر الشراء {no}.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    products=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    c.close(); return render_template("purchase_order_new.html",suppliers=su,products=products,today=date.today().isoformat())


@purchase_bp.route("/orders/<int:order_id>/submit", methods=["POST"])
@require("purchases.manage")
def submit_order(order_id):
    c = db()
    try:
        submit_purchase_order(c, order_id, session["user_id"])
        c.commit()
        flash("تم إرسال أمر الشراء للمراجعة والاعتماد.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.index"))


@purchase_bp.route("/orders/<int:order_id>/approve", methods=["POST"])
@require("purchases.approve")
def approve_order(order_id):
    c = db()
    try:
        approve_purchase_order(c, order_id, session["user_id"], request.form.get("reason", ""))
        c.commit()
        flash("تم اعتماد أمر الشراء. أصبح الاستلام مسموحًا.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.index"))


@purchase_bp.route("/orders/<int:order_id>/reject", methods=["POST"])
@require("purchases.approve")
def reject_order(order_id):
    c = db()
    try:
        reject_purchase_order(c, order_id, session["user_id"], request.form.get("reason", ""))
        c.commit()
        flash("تم رفض أمر الشراء وإعادته للتعديل مع حفظ سبب الرفض.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.index"))

@purchase_bp.route("/orders/<int:order_id>/edit",methods=["GET","POST"])
@require("purchases.manage")
def edit_order(order_id):
    c=db(); order=c.execute("""SELECT p.*,l.id line_id,l.product_id,l.quantity,l.unit_price
      FROM purchase_orders p LEFT JOIN purchase_order_lines l ON l.po_id=p.id WHERE p.id=?""",(order_id,)).fetchone()
    if not order:
        c.close(); flash("أمر الشراء غير موجود.","error"); return redirect(url_for("purchase.index"))
    received=c.execute("SELECT 1 FROM goods_receipts WHERE po_id=? LIMIT 1",(order_id,)).fetchone() is not None
    if received or order["status"] not in {"draft", "rejected"}:
        c.close(); flash("لا يمكن تعديل أمر شراء تم استلامه أو اعتماده.","error"); return redirect(url_for("purchase.index"))
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"])
            qty=Decimal(request.form["quantity"]); price=money(request.form["unit_price"])
            if qty<=0 or price<0: raise ValueError("الكمية والسعر غير صحيحين.")
            c.execute("""UPDATE purchase_orders SET supplier_id=?,order_date=?,expected_date=?,notes=?,status='draft',
              rejected_by=NULL,rejected_at=NULL,rejection_reason=NULL WHERE id=?""",
              (supplier,request.form["order_date"],request.form.get("expected_date") or None,request.form.get("notes",""),order_id))
            c.execute("UPDATE purchase_order_lines SET product_id=?,quantity=?,unit_price=? WHERE id=?",
              (product,str(qty),str(price),order["line_id"]))
            c.commit(); c.close(); flash("تم تعديل أمر الشراء.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    suppliers=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    products=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    c.close(); return render_template("purchase_order_edit.html",order=order,suppliers=suppliers,products=products)


@purchase_bp.route("/orders/<int:order_id>/delete",methods=["POST"])
@require("purchases.manage")
def delete_order(order_id):
    c=db(); order=c.execute("SELECT * FROM purchase_orders WHERE id=?",(order_id,)).fetchone()
    if not order: flash("أمر الشراء غير موجود.","error")
    elif order["status"] not in {"draft", "rejected"}: flash("لا يمكن حذف أمر شراء تم إرساله للمراجعة أو اعتماده.","error")
    elif c.execute("SELECT 1 FROM goods_receipts WHERE po_id=? LIMIT 1",(order_id,)).fetchone():
        flash("لا يمكن حذف أمر الشراء لوجود استلام مرتبط به.","error")
    else:
        try: c.execute("DELETE FROM purchase_orders WHERE id=?",(order_id,)); c.commit(); flash("تم حذف أمر الشراء غير المستلم.","success")
        except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف أمر الشراء لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("purchase.index"))

@purchase_bp.route("/receipts/new",methods=["GET","POST"])
@require("purchases.manage")
def new_receipt():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"]); wh=int(request.form["warehouse_id"])
            po_id=int(request.form["po_id"]) if request.form.get("po_id") else None
            qty=Decimal(request.form["quantity"]); unit=money(request.form["unit_cost"]); total=money(qty*unit)
            if qty<=0 or unit<0: raise ValueError("الكمية أو التكلفة غير صحيحة.")
            c.execute("BEGIN IMMEDIATE")
            dimensions = validate_journal_dimensions(c, {**request.form, "supplier_id": supplier})
            product_row=c.execute("SELECT * FROM stock_products WHERE id=? AND active=1",(product,)).fetchone()
            if not product_row or product_row["product_type"]=="service": raise ValueError("اختر صنفًا مخزنيًا نشطًا.")
            location_id=request.form.get("location_id",type=int)
            if not location_id:
                default_location=c.execute("SELECT location_id FROM stock_product_locations WHERE product_id=? AND warehouse_id=?",(product,wh)).fetchone()
                location_id=default_location["location_id"] if default_location else None
            if location_id and not c.execute("SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(location_id,wh)).fetchone():
                raise ValueError("موقع التخزين لا يتبع المخزن المحدد.")
            if c.execute("SELECT 1 FROM stock_locations WHERE warehouse_id=? AND active=1 LIMIT 1",(wh,)).fetchone() and not location_id:
                raise ValueError("حدد الدور/الموقع الذي ستُخزن فيه الكمية.")
            lot_id=make_lot(c,product_row,request.form.get("lot_number","").strip(),
                request.form.get("production_date",""),request.form.get("expiry_date",""),request.form["receipt_date"])
            po_line_id=None
            if po_id:
                po=c.execute("SELECT * FROM purchase_orders WHERE id=? AND supplier_id=? AND status IN ('approved','partially_received')",
                             (po_id,supplier)).fetchone()
                if not po: raise ValueError("أمر الشراء غير موجود أو لا يتبع المورد أو مغلق.")
                po_lines=c.execute("SELECT * FROM purchase_order_lines WHERE po_id=? AND product_id=? ORDER BY id",
                                   (po_id,product)).fetchall()
                if len(po_lines)!=1: raise ValueError("الصنف يجب أن يقابل سطرًا واحدًا فقط في أمر الشراء.")
                po_line=po_lines[0]
                outstanding=Decimal(po_line["quantity"])-Decimal(po_line["received_qty"])
                if qty>outstanding: raise ValueError("كمية الاستلام أكبر من الكمية المتبقية في أمر الشراء.")
                po_line_id=po_line["id"]
            invcode=activity_inventory_code(c)
            if not invcode: raise ValueError("النشاط الحالي لا يملك حساب مخزون تلقائيًا؛ اضبط الربط أولًا.")
            inv=account(c,invcode,"asset")
            # GRNI must be explicitly configured as a detailed liability account.
            grni=ensure_detail_account(c,"2190","بضاعة مستلمة غير مفوترة GRNI","liability","2000")
            no="GRN-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            cur=c.execute("""INSERT INTO goods_receipts(receipt_no,po_id,supplier_id,warehouse_id,receipt_date,total_value,created_by,
              branch_id,cost_center_id,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (no,po_id,supplier,wh,request.form["receipt_date"],str(total),session["user_id"],
               dimensions["branch_id"],dimensions["cost_center_id"],dimensions["project_id"]))
            rid=cur.lastrowid
            c.execute("INSERT INTO goods_receipt_lines(receipt_id,po_line_id,product_id,quantity,unit_cost,total_cost,location_id) VALUES(?,?,?,?,?,?,?)",
                      (rid,po_line_id,product,str(qty),str(unit),str(total),location_id))
            if po_id:
                received_total=Decimal(po_line["received_qty"])+qty
                new_status="received" if received_total==Decimal(po_line["quantity"]) else "partially_received"
                c.execute("UPDATE purchase_order_lines SET received_qty=? WHERE id=?",(str(received_total),po_line_id))
                c.execute("UPDATE purchase_orders SET status=? WHERE id=?",(new_status,po_id))
            # Stock ledger + valuation layer are updated in the same DB transaction.
            move_no="PUR-"+no
            movement=c.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,target_location_id,lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status)
              VALUES(?,'receipt',?,?,?,?,?,?,?,?,?,?, 'pending_document')""",
              (move_no,product,wh,location_id,lot_id,str(qty),str(unit),str(total),request.form["receipt_date"],"استلام مشتريات "+no,session["user_id"]))
            mid=movement.lastrowid
            c.execute("INSERT INTO stock_valuation_layers(product_id,warehouse_id,location_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost) VALUES(?,?,?,?,?,?,?,?)",
              (product,wh,location_id,lot_id,mid,str(qty),str(qty),str(unit)))
            eid=create_journal(c,request.form["receipt_date"],no,"استلام مشتريات غير مفوتر "+no,[(inv,total,0,"إثبات مخزون مستلم"),(grni,0,total,"بضاعة مستلمة غير مفوترة")],session["user_id"],dimensions=dimensions)
            c.execute("UPDATE goods_receipts SET journal_entry_id=? WHERE id=?",(eid,rid))
            c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?",(eid,mid))
            c.commit(); g.audit_created_record = ("goods_receipts", rid, "goods_receipt_lines", "receipt_id"); flash(f"تم الاستلام وترحيل قيد GRNI رقم {no}.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    wh=c.execute("SELECT * FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    pr=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    orders=c.execute("SELECT id,po_no,supplier_id,status FROM purchase_orders WHERE status IN ('approved','partially_received') ORDER BY id DESC").fetchall()
    locations=c.execute("SELECT id,warehouse_id,code,name_ar,location_type FROM stock_locations WHERE active=1 ORDER BY warehouse_id,code").fetchall()
    dimension_options=active_dimension_options(c)
    c.close(); return render_template("purchase_receipt_new.html",suppliers=su,warehouses=wh,products=pr,orders=orders,locations=locations,dimension_options=dimension_options,today=date.today().isoformat())

def _supplier_receipt_available_amount(c, receipt_id, exclude_bill_id=None):
    params = [receipt_id]
    sql = """SELECT x.matched_amount FROM supplier_bill_receipts x
             WHERE x.receipt_id=?"""
    if exclude_bill_id is not None:
        sql += " AND x.bill_id<>?"
        params.append(exclude_bill_id)
    allocated = sum((money(row["matched_amount"]) for row in c.execute(sql, params)), money("0"))
    receipt = c.execute("SELECT total_value FROM goods_receipts WHERE id=?", (receipt_id,)).fetchone()
    if not receipt:
        raise ValueError("إيصال الاستلام غير موجود.")
    return money(money(receipt["total_value"]) - allocated)


def _validate_supplier_receipt_allocations(c, supplier_id, receipt_ids, subtotal, form,
                                           exclude_bill_id=None):
    allocations = []
    total_allocated = money("0")
    for receipt_id in dict.fromkeys(receipt_ids):
        receipt = c.execute("""SELECT id,total_value FROM goods_receipts
            WHERE id=? AND supplier_id=? AND status='posted'""",
            (receipt_id, supplier_id)).fetchone()
        if not receipt:
            raise ValueError("إيصال الاستلام غير تابع لهذا المورد أو غير مرحّل.")
        available = _supplier_receipt_available_amount(c, receipt_id, exclude_bill_id)
        raw_amount = form.get("receipt_amount_" + str(receipt_id))
        amount = money(raw_amount) if raw_amount not in (None, "") else available
        if amount <= 0:
            raise ValueError("قيمة تخصيص إيصال الاستلام يجب أن تكون أكبر من صفر.")
        if amount > available:
            raise ValueError("قيمة التخصيص تتجاوز المتبقي غير المفوتر من إيصال الاستلام.")
        allocations.append((receipt_id, amount))
        total_allocated = money(total_allocated + amount)
    if total_allocated != money(subtotal):
        raise ValueError("مجموع تخصيصات إيصالات GRNI يجب أن يساوي صافي فاتورة المورد.")
    return allocations


def _available_supplier_receipts(c, supplier_id, exclude_bill_id=None):
    if supplier_id is None:
        rows = c.execute("""SELECT r.*,s.name supplier_name FROM goods_receipts r
            JOIN suppliers s ON s.id=r.supplier_id
            WHERE r.status='posted' ORDER BY r.id""").fetchall()
    else:
        rows = c.execute("""SELECT r.*,s.name supplier_name FROM goods_receipts r
            JOIN suppliers s ON s.id=r.supplier_id
            WHERE r.supplier_id=? AND r.status='posted' ORDER BY r.id""",
            (supplier_id,)).fetchall()
    available = []
    for row in rows:
        amount = _supplier_receipt_available_amount(c, row["id"], exclude_bill_id)
        if amount > 0:
            item = dict(row)
            item["available_amount"] = amount
            available.append(item)
    return available


def submit_supplier_bill(c, bill_id, actor_id):
    c.execute("BEGIN IMMEDIATE")
    bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        raise ValueError("فاتورة المورد غير موجودة.")
    if bill["status"] not in {"draft", "rejected"}:
        raise ValueError("لا يمكن إرسال فاتورة المورد من حالتها الحالية.")
    if bill["journal_entry_id"] is not None:
        raise ValueError("الفاتورة مرتبطة بقيد مرحل ولا يمكن إعادة إرسالها.")
    if not bill["supplier_invoice_no"].strip():
        raise ValueError("رقم فاتورة المورد مطلوب.")
    bill_receipts = c.execute("""SELECT receipt_id,matched_amount FROM supplier_bill_receipts
        WHERE bill_id=? ORDER BY receipt_id""", (bill_id,)).fetchall()
    if bill["bill_kind"] == "inventory":
        if not bill_receipts:
            raise ValueError("فاتورة الأصناف المخزنية تتطلب إيصال استلام.")
        receipt_ids = [row["receipt_id"] for row in bill_receipts]
        allocation_form = {
            "receipt_amount_" + str(row["receipt_id"]): row["matched_amount"]
            for row in bill_receipts
        }
        _validate_supplier_receipt_allocations(
            c, bill["supplier_id"], receipt_ids, money(bill["subtotal"]),
            allocation_form, exclude_bill_id=bill_id
        )
    elif bill["bill_kind"] == "expense":
        if bill_receipts:
            raise ValueError("فاتورة المصروف لا يجوز ربطها بإيصالات استلام مخزني.")
    elif bill["bill_kind"] == "asset":
        if bill_receipts:
            raise ValueError("فاتورة الأصل الثابت لا يجوز ربطها بإيصالات استلام مخزني.")
        validate_asset_bill_data(c, dict(bill), bill["bill_date"], money(bill["subtotal"]))
    else:
        raise ValueError("نوع فاتورة المورد المخزن غير معروف.")
    c.execute("""UPDATE supplier_bills SET status='pending_approval',
        submitted_by=?,submitted_at=CURRENT_TIMESTAMP,approved_by=NULL,approved_at=NULL,
        rejected_by=NULL,rejected_at=NULL,rejection_reason=NULL,approval_reason=NULL
        WHERE id=?""", (actor_id, bill_id))


def approve_supplier_bill(c, bill_id, actor_id, reason=""):
    c.execute("BEGIN IMMEDIATE")
    bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        raise ValueError("فاتورة المورد غير موجودة.")
    if bill["status"] != "pending_approval":
        raise ValueError("فاتورة المورد ليست بانتظار الاعتماد.")
    if bill["created_by"] == actor_id or bill["submitted_by"] == actor_id:
        raise ValueError("لا يجوز اعتماد فاتورة أنشأتها أو أرسلتها للمراجعة بنفسك.")
    if bill["created_by"] is None or bill["submitted_by"] is None:
        raise ValueError("بيانات منشئ أو مرسل فاتورة المورد غير مكتملة؛ راجع المستند.")
    c.execute("""UPDATE supplier_bills SET status='approved',approved_by=?,
        approved_at=CURRENT_TIMESTAMP,approval_reason=? WHERE id=? AND status='pending_approval'""",
        (actor_id, (reason or "").strip(), bill_id))
    if c.execute("SELECT changes()").fetchone()[0] != 1:
        raise ValueError("تغيرت حالة فاتورة المورد؛ أعد تحميل الصفحة.")


def reject_supplier_bill(c, bill_id, actor_id, reason):
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("اكتب سبب رفض واضحًا (5 أحرف على الأقل).")
    c.execute("BEGIN IMMEDIATE")
    bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        raise ValueError("فاتورة المورد غير موجودة.")
    if bill["status"] != "pending_approval":
        raise ValueError("فاتورة المورد ليست بانتظار الاعتماد.")
    if bill["created_by"] == actor_id or bill["submitted_by"] == actor_id:
        raise ValueError("لا يجوز رفض فاتورة أنشأتها أو أرسلتها للمراجعة بنفسك.")
    c.execute("""UPDATE supplier_bills SET status='rejected',rejected_by=?,
        rejected_at=CURRENT_TIMESTAMP,rejection_reason=? WHERE id=? AND status='pending_approval'""",
        (actor_id, reason, bill_id))
    if c.execute("SELECT changes()").fetchone()[0] != 1:
        raise ValueError("تغيرت حالة فاتورة المورد؛ أعد تحميل الصفحة.")


def post_supplier_bill(c, bill_id, actor_id):
    c.execute("BEGIN IMMEDIATE")
    bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        raise ValueError("فاتورة المورد غير موجودة.")
    if bill["status"] != "approved":
        raise ValueError("لا يمكن ترحيل فاتورة المورد قبل اعتمادها.")
    if bill["journal_entry_id"] is not None:
        raise ValueError("فاتورة المورد مرحّلة بالفعل.")
    dimensions = validate_journal_dimensions(c, {
        "branch_id": bill["branch_id"], "cost_center_id": bill["cost_center_id"],
        "project_id": bill["project_id"], "supplier_id": bill["supplier_id"],
    })
    ap = supplier_ap_account(c)
    subtotal = money(bill["subtotal"])
    tax = money(bill["tax_amount"])
    journal_lines = []
    if bill["bill_kind"] == "inventory":
        receipt_rows = c.execute("""SELECT x.receipt_id,x.matched_amount,r.supplier_id,r.status
            FROM supplier_bill_receipts x JOIN goods_receipts r ON r.id=x.receipt_id
            WHERE x.bill_id=? ORDER BY x.receipt_id""", (bill_id,)).fetchall()
        if not receipt_rows:
            raise ValueError("لا توجد استلامات مرتبطة بفاتورة الأصناف.")
        matched_total = money("0")
        for receipt in receipt_rows:
            if receipt["supplier_id"] != bill["supplier_id"] or receipt["status"] != "posted":
                raise ValueError("يوجد إيصال استلام غير صالح أو لا يتبع المورد.")
            amount = money(receipt["matched_amount"])
            if amount <= 0:
                raise ValueError("قيمة تسوية إيصال GRNI يجب أن تكون أكبر من صفر.")
            available_before_this_bill = _supplier_receipt_available_amount(
                c, receipt["receipt_id"], exclude_bill_id=bill_id
            )
            if amount > available_before_this_bill:
                raise ValueError("تجاوزت تخصيصات فواتير المورد قيمة إيصال GRNI المتاحة.")
            matched_total = money(matched_total + amount)
            grni = account(c, "2190", "liability")
            journal_lines.extend([
                (grni, amount, 0, "تسوية GRNI"),
                (ap, 0, amount, "إثبات المورد"),
            ])
        if matched_total != subtotal:
            raise ValueError("مجموع تسويات إيصالات GRNI لا يطابق صافي فاتورة المورد.")
    elif bill["bill_kind"] == "expense":
        expense_id = bill["expense_account_id"]
        expense = c.execute("SELECT account_type,is_group,active FROM accounts WHERE id=?", (expense_id,)).fetchone()
        if not expense or expense["account_type"] != "expense" or expense["is_group"] or not expense["active"]:
            raise ValueError("حساب مصروف الفاتورة غير صالح أو غير نشط.")
        journal_lines.extend([
            (expense_id, subtotal, 0, "مصروف فاتورة المورد"),
            (ap, 0, subtotal, "إثبات المورد"),
        ])
    elif bill["bill_kind"] == "asset":
        if c.execute("SELECT 1 FROM supplier_bill_receipts WHERE bill_id=? LIMIT 1", (bill_id,)).fetchone():
            raise ValueError("فاتورة الأصل الثابت لا يجوز ربطها بإيصالات استلام مخزني.")
        asset_rows = c.execute("SELECT * FROM supplier_bill_asset_lines WHERE bill_id=? ORDER BY line_no",(bill_id,)).fetchall()
        if asset_rows:
            line_total = money("0")
            for row in asset_rows:
                cost = money(row["cost"])
                line_total = money(line_total + cost)
                journal_lines.append((row["asset_account_id"],cost,0,
                    "رسملة الأصل " + row["asset_code"] + " - " + row["asset_name"]))
            if line_total != subtotal:
                raise ValueError("مجموع تكاليف الأصول لا يساوي صافي فاتورة المورد.")
        else:
            asset_data = validate_asset_bill_data(c, dict(bill), bill["bill_date"], subtotal)
            journal_lines.append((asset_data["asset_account_id"],subtotal,0,
                "رسملة تكلفة الأصل " + asset_data["asset_name"]))
        journal_lines.append((ap,0,subtotal,"إثبات المورد مقابل شراء أصل ثابت"))
    else:
        raise ValueError("نوع فاتورة المورد المخزن غير معروف.")
    if tax:
        vat_code = _activity_tax_roles(c)["input_vat"]
        vat = ensure_detail_account(c, vat_code, "ضريبة مدخلات قابلة للخصم", "asset", "1100")
        journal_lines.extend([
            (vat, tax, 0, "ضريبة مدخلات حسب الفاتورة"),
            (ap, 0, tax, "ضريبة ضمن إجمالي المورد"),
        ])
    total = money(subtotal + tax)
    if total != money(bill["total_amount"]):
        raise ValueError("إجمالي الفاتورة لا يطابق الصافي والضريبة؛ راجع المستند.")
    entry_no = bill["bill_no"]
    entry_id = create_journal(c, bill["bill_date"], entry_no,
        "فاتورة مورد " + bill["supplier_invoice_no"], journal_lines, actor_id, dimensions=dimensions)
    c.execute("""UPDATE supplier_bills SET journal_entry_id=?,status='posted'
        WHERE id=? AND status='approved' AND journal_entry_id IS NULL""", (entry_id, bill_id))
    if c.execute("SELECT changes()").fetchone()[0] != 1:
        raise ValueError("تغيرت حالة فاتورة المورد أثناء الترحيل.")
    if bill["bill_kind"] == "asset":
        from fixed_assets import create_asset_from_supplier_bill
        posted_bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        create_asset_from_supplier_bill(c, posted_bill, actor_id)
    return entry_id


@purchase_bp.route("/bills/new",methods=["GET","POST"])
@require("purchases.manage")
def new_bill():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); subtotal=money(request.form["subtotal"]); rate=Decimal(request.form.get("tax_rate","0"))
            if subtotal<=0 or rate<0 or rate>100: raise ValueError("قيمة الفاتورة أو الضريبة غير صحيحة.")
            tax=money(subtotal*rate/Decimal("100")); total=subtotal+tax
            receipts=[int(x) for x in request.form.getlist("receipt_id")]
            supplier_invoice_no=request.form["supplier_invoice_no"].strip()
            if not supplier_invoice_no:
                raise ValueError("رقم فاتورة المورد مطلوب.")
            c.execute("BEGIN IMMEDIATE")
            dimensions = validate_journal_dimensions(c, request.form)
            if c.execute("SELECT 1 FROM supplier_bills WHERE supplier_id=? AND lower(trim(supplier_invoice_no))=lower(trim(?)) LIMIT 1",
                         (supplier,supplier_invoice_no)).fetchone():
                raise ValueError("رقم فاتورة المورد مسجل بالفعل لهذا المورد.")
            bill_kind=request.form.get("bill_kind","inventory").strip()
            if bill_kind not in {"inventory","expense","asset"}:
                raise ValueError("نوع فاتورة المورد غير صحيح.")
            expense_account_id=None
            asset_data={}
            if bill_kind=="asset":
                if receipts:
                    raise ValueError("فاتورة الأصل الثابت لا تُربط بإيصالات استلام مخزني.")
                asset_lines=validate_asset_bill_lines(c,request.form,request.form["bill_date"],subtotal)
                supplier_ap_account(c)
            if bill_kind=="expense":
                expense_account_id=int(request.form.get("expense_account_id","0"))
                expense_row=c.execute("SELECT account_type,is_group,active FROM accounts WHERE id=?",(expense_account_id,)).fetchone()
                if not expense_row or expense_row["account_type"]!="expense" or expense_row["is_group"] or not expense_row["active"]:
                    raise ValueError("اختر حساب مصروف تفصيليًا ونشطًا.")
                if receipts:
                    raise ValueError("فاتورة المصروف لا تُربط بإيصالات استلام مخزني.")
            elif bill_kind=="inventory":
                if not receipts:
                    raise ValueError("فاتورة الأصناف المخزنية تتطلب إيصال استلام.")
                supplier_ap_account(c)
            receipt_allocations=[]
            if bill_kind=="inventory":
                receipt_allocations = _validate_supplier_receipt_allocations(
                    c, supplier, receipts, subtotal, request.form
                )
            if tax:
                ensure_detail_account(c,_activity_tax_roles(c)["input_vat"],"ضريبة مدخلات قابلة للخصم","asset","1100")
            no="BILL-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            due_date=request.form.get("due_date") or request.form["bill_date"]
            if due_date < request.form["bill_date"]: raise ValueError("تاريخ استحقاق المورد لا يمكن أن يسبق تاريخ الفاتورة.")
            cur=c.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
                subtotal,tax_rate,tax_amount,total_amount,created_by,branch_id,cost_center_id,project_id,
                status,bill_kind,expense_account_id,asset_code,asset_name,asset_category,asset_ready_date,
                asset_residual_value,asset_useful_life_months,asset_depreciation_method,asset_annual_rate,
                asset_account_id,asset_accum_account_id,asset_expense_account_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (no,supplier,request.form["bill_date"],due_date,supplier_invoice_no,str(subtotal),str(rate),
                 str(tax),str(total),session["user_id"],dimensions["branch_id"],dimensions["cost_center_id"],
                 dimensions["project_id"],bill_kind,expense_account_id,
                 asset_data.get("asset_code"),asset_data.get("asset_name"),asset_data.get("asset_category"),
                 asset_data.get("asset_ready_date"),asset_data.get("asset_residual_value","0"),
                 asset_data.get("asset_useful_life_months"),asset_data.get("asset_depreciation_method","straight_line"),
                 asset_data.get("asset_annual_rate","0"),asset_data.get("asset_account_id"),
                 asset_data.get("asset_accum_account_id"),asset_data.get("asset_expense_account_id")))
            bid=cur.lastrowid
            if bill_kind == "asset":
                for line_no, line in enumerate(asset_lines, 1):
                    c.execute("""INSERT INTO supplier_bill_asset_lines(
                        bill_id,line_no,asset_code,asset_name,asset_category,cost,ready_date,
                        residual_value,useful_life_months,depreciation_method,annual_rate,
                        asset_account_id,accum_account_id,expense_account_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid,line_no,line["asset_code"],line["asset_name"],line["asset_category"],line["cost"],
                     line["asset_ready_date"],line["asset_residual_value"],line["asset_useful_life_months"],
                     line["asset_depreciation_method"],line["asset_annual_rate"],line["asset_account_id"],
                     line["asset_accum_account_id"],line["asset_expense_account_id"]))
            for rid,amt in receipt_allocations:
                c.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                          (bid,rid,str(amt)))
            c.commit()
            g.audit_created_record = ("supplier_bills", bid, None, None)
            c.close()
            flash(f"تم إنشاء مسودة فاتورة المورد {no}. أرسلها للمراجعة قبل الترحيل.","success")
            return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e:
            c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    expense_accounts=c.execute("SELECT id,code,name_ar FROM accounts WHERE account_type='expense' AND is_group=0 AND active=1 ORDER BY code").fetchall()
    asset_accounts=c.execute("SELECT id,code,name_ar FROM accounts WHERE account_type='asset' AND is_group=0 AND active=1 ORDER BY code").fetchall()
    receipts = _available_supplier_receipts(c)
    dimension_options=active_dimension_options(c)
    c.close(); return render_template("purchase_bill_new.html",suppliers=su,receipts=receipts,
        expense_accounts=expense_accounts,asset_accounts=asset_accounts,
        dimension_options=dimension_options,today=date.today().isoformat())


@purchase_bp.route("/bills/<int:bill_id>/edit", methods=["GET", "POST"])
@require("purchases.manage")
def edit_bill(bill_id):
    c = db()
    bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        c.close()
        from flask import abort
        abort(404)
    if bill["status"] not in {"draft", "rejected"} or bill["journal_entry_id"] is not None:
        c.close()
        flash("لا يمكن تعديل فاتورة إلا إذا كانت مسودة أو مرفوضة وغير مرتبطة بقيد مرحّل.", "error")
        return redirect(url_for("purchase.index"))

    if request.method == "POST":
        try:
            supplier = int(request.form["supplier_id"])
            subtotal = money(request.form["subtotal"])
            rate = Decimal(request.form.get("tax_rate", "0"))
            bill_date = request.form["bill_date"]
            due_date = request.form.get("due_date") or bill_date
            supplier_invoice_no = request.form["supplier_invoice_no"].strip()
            if subtotal <= 0 or rate < 0 or rate > 100:
                raise ValueError("قيمة الفاتورة أو الضريبة غير صحيحة.")
            if not supplier_invoice_no or not bill_date:
                raise ValueError("رقم فاتورة المورد وتاريخها مطلوبان.")
            if due_date < bill_date:
                raise ValueError("تاريخ استحقاق المورد لا يمكن أن يسبق تاريخ الفاتورة.")
            tax = money(subtotal * rate / Decimal("100"))
            total = money(subtotal + tax)
            receipts = list(dict.fromkeys(int(x) for x in request.form.getlist("receipt_id")))
            bill_kind = request.form.get("bill_kind", "inventory").strip()
            if bill_kind not in {"inventory", "expense", "asset"}:
                raise ValueError("نوع فاتورة المورد غير صحيح.")

            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
            if not current or current["status"] not in {"draft", "rejected"} or current["journal_entry_id"] is not None:
                raise ValueError("تغيرت حالة الفاتورة؛ لا يمكن تعديلها الآن.")
            dimensions = validate_journal_dimensions(c, request.form)
            duplicate = c.execute("""SELECT 1 FROM supplier_bills
                WHERE supplier_id=? AND lower(trim(supplier_invoice_no))=lower(trim(?)) AND id<>? LIMIT 1""",
                (supplier, supplier_invoice_no, bill_id)).fetchone()
            if duplicate:
                raise ValueError("رقم فاتورة المورد مسجل بالفعل لهذا المورد.")

            expense_account_id = None
            asset_data = {}
            if bill_kind == "expense":
                expense_account_id = int(request.form.get("expense_account_id", "0"))
                expense_row = c.execute("""SELECT account_type,is_group,active FROM accounts WHERE id=?""",
                                        (expense_account_id,)).fetchone()
                if not expense_row or expense_row["account_type"] != "expense" or expense_row["is_group"] or not expense_row["active"]:
                    raise ValueError("اختر حساب مصروف تفصيليًا ونشطًا.")
                if receipts:
                    raise ValueError("فاتورة المصروف لا تُربط بإيصالات استلام مخزني.")
            elif bill_kind == "asset":
                if receipts:
                    raise ValueError("فاتورة الأصل الثابت لا تُربط بإيصالات استلام مخزني.")
                asset_lines = validate_asset_bill_lines(c, request.form, bill_date, subtotal)
                supplier_ap_account(c)
            else:
                if not receipts:
                    raise ValueError("فاتورة الأصناف المخزنية تتطلب إيصال استلام.")
                supplier_ap_account(c)

            allocations = []
            if bill_kind == "inventory":
                allocations = _validate_supplier_receipt_allocations(
                    c, supplier, receipts, subtotal, request.form, exclude_bill_id=bill_id
                )
            if tax:
                ensure_detail_account(c, _activity_tax_roles(c)["input_vat"],
                                      "ضريبة مدخلات قابلة للخصم", "asset", "1100")

            c.execute("""UPDATE supplier_bills SET supplier_id=?,bill_date=?,due_date=?,
                supplier_invoice_no=?,subtotal=?,tax_rate=?,tax_amount=?,total_amount=?,
                branch_id=?,cost_center_id=?,project_id=?,bill_kind=?,expense_account_id=?,
                asset_code=?,asset_name=?,asset_category=?,asset_ready_date=?,asset_residual_value=?,
                asset_useful_life_months=?,asset_depreciation_method=?,asset_annual_rate=?,
                asset_account_id=?,asset_accum_account_id=?,asset_expense_account_id=?,
                status='draft',submitted_by=NULL,submitted_at=NULL,approved_by=NULL,approved_at=NULL,
                rejected_by=NULL,rejected_at=NULL,rejection_reason=NULL,approval_reason=NULL
                WHERE id=? AND status IN ('draft','rejected') AND journal_entry_id IS NULL""",
                (supplier, bill_date, due_date, supplier_invoice_no, str(subtotal), str(rate),
                 str(tax), str(total), dimensions["branch_id"], dimensions["cost_center_id"],
                 dimensions["project_id"], bill_kind, expense_account_id,
                 asset_data.get("asset_code"),asset_data.get("asset_name"),asset_data.get("asset_category"),
                 asset_data.get("asset_ready_date"),asset_data.get("asset_residual_value","0"),
                 asset_data.get("asset_useful_life_months"),asset_data.get("asset_depreciation_method","straight_line"),
                 asset_data.get("asset_annual_rate","0"),asset_data.get("asset_account_id"),
                 asset_data.get("asset_accum_account_id"),asset_data.get("asset_expense_account_id"),bill_id))
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("لم يتم تحديث الفاتورة؛ أعد تحميل الصفحة.")
            c.execute("DELETE FROM supplier_bill_asset_lines WHERE bill_id=?", (bill_id,))
            if bill_kind == "asset":
                for line_no, line in enumerate(asset_lines, 1):
                    c.execute("""INSERT INTO supplier_bill_asset_lines(
                        bill_id,line_no,asset_code,asset_name,asset_category,cost,ready_date,
                        residual_value,useful_life_months,depreciation_method,annual_rate,
                        asset_account_id,accum_account_id,expense_account_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bill_id,line_no,line["asset_code"],line["asset_name"],line["asset_category"],line["cost"],
                     line["asset_ready_date"],line["asset_residual_value"],line["asset_useful_life_months"],
                     line["asset_depreciation_method"],line["asset_annual_rate"],line["asset_account_id"],
                     line["asset_accum_account_id"],line["asset_expense_account_id"]))
            c.execute("DELETE FROM supplier_bill_receipts WHERE bill_id=?", (bill_id,))
            for receipt_id, amount in allocations:
                c.execute("""INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount)
                    VALUES(?,?,?)""", (bill_id, receipt_id, str(amount)))
            c.commit()
            flash("تم حفظ التعديلات كمسودة. يجب إرسال الفاتورة للمراجعة من جديد.", "success")
            return redirect(url_for("purchase.index"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
            bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()

    suppliers = c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    expense_accounts = c.execute("""SELECT id,code,name_ar FROM accounts
        WHERE account_type='expense' AND is_group=0 AND active=1 ORDER BY code""").fetchall()
    asset_accounts = c.execute("""SELECT id,code,name_ar FROM accounts
        WHERE account_type='asset' AND is_group=0 AND active=1 ORDER BY code""").fetchall()
    receipts = _available_supplier_receipts(c, bill["supplier_id"], exclude_bill_id=bill_id)
    existing_allocations = {row["receipt_id"]: money(row["matched_amount"]) for row in c.execute(
        "SELECT receipt_id,matched_amount FROM supplier_bill_receipts WHERE bill_id=?", (bill_id,)).fetchall()}
    selected_receipt_ids = set(existing_allocations)
    existing_asset_lines = c.execute("SELECT * FROM supplier_bill_asset_lines WHERE bill_id=? ORDER BY line_no",(bill_id,)).fetchall()
    dimension_options = active_dimension_options(c)
    c.close()
    return render_template("purchase_bill_new.html", suppliers=suppliers, receipts=receipts,
        expense_accounts=expense_accounts, asset_accounts=asset_accounts,
        dimension_options=dimension_options, today=date.today().isoformat(), bill=bill, selected_receipt_ids=selected_receipt_ids,
        receipt_allocations=existing_allocations, existing_asset_lines=existing_asset_lines, edit_mode=True)


@purchase_bp.route("/bills/<int:bill_id>/submit", methods=["POST"])
@require("purchases.manage")
def submit_bill(bill_id):
    c=db()
    try:
        submit_supplier_bill(c,bill_id,session["user_id"]); c.commit()
        flash("تم إرسال فاتورة المورد للمراجعة والاعتماد.","success")
    except (ValueError,sqlite3.Error) as exc:
        c.rollback(); flash(str(exc),"error")
    finally: c.close()
    return redirect(url_for("purchase.index"))


@purchase_bp.route("/bills/<int:bill_id>/approve", methods=["POST"])
@require("purchases.approve")
def approve_bill(bill_id):
    c=db()
    try:
        approve_supplier_bill(c,bill_id,session["user_id"],request.form.get("reason","")); c.commit()
        flash("تم اعتماد فاتورة المورد. أصبحت جاهزة للترحيل بواسطة المحاسب.","success")
    except (ValueError,sqlite3.Error) as exc:
        c.rollback(); flash(str(exc),"error")
    finally: c.close()
    return redirect(url_for("purchase.index"))


@purchase_bp.route("/bills/<int:bill_id>/reject", methods=["POST"])
@require("purchases.approve")
def reject_bill(bill_id):
    c=db()
    try:
        reject_supplier_bill(c,bill_id,session["user_id"],request.form.get("reason","")); c.commit()
        flash("تم رفض فاتورة المورد مع حفظ سبب الرفض.","success")
    except (ValueError,sqlite3.Error) as exc:
        c.rollback(); flash(str(exc),"error")
    finally: c.close()
    return redirect(url_for("purchase.index"))


@purchase_bp.route("/bills/<int:bill_id>/post", methods=["POST"])
@require("purchases.manage")
def post_bill(bill_id):
    c=db()
    try:
        entry_id=post_supplier_bill(c,bill_id,session["user_id"]); c.commit()
        g.audit_created_record = ("supplier_bills", bill_id, None, None)
        flash(f"تم ترحيل فاتورة المورد وربطها بالقيد رقم {entry_id}.","success")
        return redirect(url_for("purchase.bill_print",bill_id=bill_id))
    except (ValueError,sqlite3.Error) as exc:
        c.rollback(); flash(str(exc),"error")
    finally: c.close()
    return redirect(url_for("purchase.index"))


@purchase_bp.route("/bills/<int:bill_id>/print")
@require("purchases.view")
def bill_print(bill_id):
    c=db()
    bill=c.execute("""SELECT b.*,s.code supplier_code,s.name supplier_name,s.tax_number supplier_tax_number
      FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id WHERE b.id=?""",(bill_id,)).fetchone()
    if not bill:
        c.close(); from flask import abort; abort(404)
    journal_lines=c.execute("""SELECT a.code,a.name_ar,jl.debit,jl.credit,jl.memo
      FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
      WHERE jl.entry_id=? ORDER BY jl.id""",(bill["journal_entry_id"],)).fetchall()
    receipts=c.execute("""SELECT r.receipt_no,r.receipt_date,r.total_value
      FROM supplier_bill_receipts x JOIN goods_receipts r ON r.id=x.receipt_id
      WHERE x.bill_id=? ORDER BY r.id""",(bill_id,)).fetchall()
    settings={r["setting_key"]:r["setting_value"] for r in c.execute(
      "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('company_name','base_currency','tax_number')")}
    c.close()
    return render_template("purchase_bill_print.html",bill=bill,journal_lines=journal_lines,receipts=receipts,settings=settings)


@purchase_bp.route("/credit-notes/new", methods=["GET","POST"])
@require("purchases.manage")
def new_credit_note():
    if "permissions" in session and "stock.manage" not in set(session["permissions"]):
        flash("يلزم امتلاك صلاحية إدارة المشتريات وإدارة المخزون معًا لترحيل مرتجع المورد.","error")
        return redirect(url_for("purchase.index"))
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"])
            bill_id=int(request.form["bill_id"])
            supplier_credit_no=request.form.get("supplier_credit_no","").strip()
            reason=request.form.get("reason","").strip()
            if not supplier_credit_no or not reason:
                raise ValueError("رقم إشعار المورد وسبب المرتجع مطلوبان.")
            product_ids=request.form.getlist("product_id")
            warehouse_ids=request.form.getlist("warehouse_id")
            quantities=request.form.getlist("quantity")
            if not product_ids or not (len(product_ids)==len(warehouse_ids)==len(quantities)):
                raise ValueError("أضف صنفًا واحدًا على الأقل وحدد المخزن والكمية لكل صنف.")
            c.execute("BEGIN IMMEDIATE")
            bill=c.execute("SELECT * FROM supplier_bills WHERE id=? AND supplier_id=? AND status='posted'",(bill_id,supplier)).fetchone()
            if not bill:
                raise ValueError("فاتورة المورد غير موجودة أو لا تتبع المورد المحدد.")
            dimensions = validate_journal_dimensions(c, {
                "branch_id": bill["branch_id"], "cost_center_id": bill["cost_center_id"],
                "project_id": bill["project_id"], "supplier_id": supplier,
            })
            if request.form["credit_date"] < bill["bill_date"]:
                raise ValueError("تاريخ إشعار الدائن لا يمكن أن يسبق تاريخ فاتورة المورد.")
            if not c.execute("SELECT 1 FROM supplier_bill_receipts WHERE bill_id=? LIMIT 1",(bill_id,)).fetchone():
                raise ValueError("إشعار الدائن المخزني متاح فقط لفواتير الأصناف المرتبطة باستلام مشتريات، وليس فواتير المصروفات والخدمات.")
            if c.execute("SELECT 1 FROM supplier_credit_notes WHERE supplier_id=? AND lower(trim(supplier_credit_no))=lower(trim(?))",(supplier,supplier_credit_no)).fetchone():
                raise ValueError("رقم إشعار الدائن مسجل بالفعل لهذا المورد.")
            inventory_code=activity_inventory_code(c)
            if not inventory_code:
                raise ValueError("النشاط الحالي لا يملك حساب مخزون معرفًا.")
            inventory_id=account(c,inventory_code,"asset")
            ap_id=supplier_ap_account(c)
            tax_rate=Decimal(bill["tax_rate"] or "0")
            method_row=c.execute("SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
            method=method_row["setting_value"] if method_row else "average"
            line_data=[]; movement_ids=[]; subtotal=Decimal("0")
            for raw_pid,raw_wh,raw_qty in zip(product_ids,warehouse_ids,quantities):
                product_id=int(raw_pid); warehouse_id=int(raw_wh)
                qty=q4(Decimal(raw_qty))
                if qty<=0: raise ValueError("كمية المرتجع يجب أن تكون أكبر من صفر.")
                product=c.execute("SELECT * FROM stock_products WHERE id=? AND active=1",(product_id,)).fetchone()
                warehouse=c.execute("SELECT id FROM stock_warehouses WHERE id=? AND active=1",(warehouse_id,)).fetchone()
                if not product or product["product_type"]=="service" or not warehouse:
                    raise ValueError("اختر صنفًا مخزنيًا ومخزنًا نشطًا.")
                belongs_to_bill=c.execute("""SELECT 1 FROM supplier_bill_receipts sb
                  JOIN goods_receipts gr ON gr.id=sb.receipt_id
                  JOIN goods_receipt_lines gl ON gl.receipt_id=gr.id
                  WHERE sb.bill_id=? AND gl.product_id=? AND gr.warehouse_id=? LIMIT 1""",
                  (bill_id,product_id,warehouse_id)).fetchone()
                if not belongs_to_bill:
                    raise ValueError("الصنف والمخزن المحددان غير موجودين ضمن استلامات فاتورة المورد الأصلية.")
                cost,allocations=consume_layers(c,product_id,warehouse_id,qty,method)
                subtotal+=cost
                line_data.append((product_id,warehouse_id,qty,cost))
                for lot_id,allocated_qty,unit_cost,allocated_total in allocations:
                    movement_no="SUPRET-"+datetime.now().strftime("%Y%m%d%H%M%S%f")+"-"+uuid.uuid4().hex[:5].upper()
                    cur=c.execute("""INSERT INTO stock_movements(
                      movement_no,movement_type,product_id,source_warehouse_id,lot_id,quantity,unit_cost,total_cost,
                      movement_date,notes,created_by,issue_purpose,accounting_status)
                      VALUES(?,'issue',?,?,?,?,?,?,?,?,?,'supplier_return','pending_document')""",
                      (movement_no,product_id,warehouse_id,lot_id,str(allocated_qty),str(unit_cost),str(allocated_total),
                       request.form["credit_date"],"مرتجع مورد — "+supplier_credit_no,session["user_id"]))
                    movement_ids.append(cur.lastrowid)
            subtotal=money(subtotal)
            if subtotal<=0: raise ValueError("قيمة المرتجع يجب أن تكون أكبر من صفر.")
            tax=money(subtotal*tax_rate/Decimal("100"))
            total=subtotal+tax
            already_credited=Decimal(bill["credited_amount"] or "0")
            bill_remaining=Decimal(bill["total_amount"])-already_credited
            if total>bill_remaining:
                raise ValueError("قيمة إشعار الدائن تتجاوز المتبقي من قيمة فاتورة المورد بعد الإشعارات السابقة.")
            credit_no="SCN-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            cur=c.execute("""INSERT INTO supplier_credit_notes(
              credit_no,supplier_id,bill_id,credit_date,supplier_credit_no,reason,subtotal,tax_rate,tax_amount,total_amount,created_by)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (credit_no,supplier,bill_id,request.form["credit_date"],supplier_credit_no,reason,
               str(subtotal),str(tax_rate),str(tax),str(total),session["user_id"]))
            note_id=cur.lastrowid
            for product_id,warehouse_id,qty,cost in line_data:
                c.execute("""INSERT INTO supplier_credit_note_lines(
                  credit_note_id,product_id,warehouse_id,quantity,unit_cost,total_cost) VALUES(?,?,?,?,?,?)""",
                  (note_id,product_id,warehouse_id,str(qty),str(money(cost/qty)),str(cost)))
            journal_lines=[(ap_id,total,0,"تخفيض مديونية المورد بإشعار دائن"),
                           (inventory_id,0,subtotal,"إخراج مخزون مرتجع للمورد")]
            if tax:
                vat_id=account(c,_activity_tax_roles(c)["input_vat"],"asset")
                journal_lines.append((vat_id,0,tax,"عكس ضريبة المدخلات حسب إشعار المورد"))
            entry_id=create_journal(c,request.form["credit_date"],credit_no,
                "إشعار دائن مشتريات — "+supplier_credit_no,journal_lines,session["user_id"],dimensions=dimensions)
            c.execute("UPDATE supplier_credit_notes SET journal_entry_id=? WHERE id=?",(entry_id,note_id))
            c.execute("UPDATE supplier_bills SET credited_amount=? WHERE id=?",
                      (str(money(already_credited+total)),bill_id))
            for movement_id in movement_ids:
                c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?",(entry_id,movement_id))
            c.commit()
            c.close()
            flash(f"تم ترحيل إشعار الدائن {credit_no} وربطه بالمخزون وفاتورة المورد.","success")
            return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e:
            c.rollback()
            flash(str(e),"error")
    suppliers=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    bills=c.execute("""SELECT b.id,b.bill_no,b.supplier_id,b.bill_date,b.total_amount,b.credited_amount,s.name supplier_name
      FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
      WHERE b.status='posted' AND EXISTS(SELECT 1 FROM supplier_bill_receipts sb WHERE sb.bill_id=b.id)
      ORDER BY b.bill_date DESC,b.id DESC""").fetchall()
    bills=[b for b in bills if Decimal(b["total_amount"])>Decimal(b["credited_amount"] or "0")]
    products=c.execute("SELECT id,sku,name_ar,uom FROM stock_products WHERE active=1 AND product_type!='service' ORDER BY name_ar").fetchall()
    warehouses=c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    c.close()
    return render_template("purchase_credit_note_new.html",suppliers=suppliers,bills=bills,products=products,warehouses=warehouses,today=date.today().isoformat())

@purchase_bp.route("/payments/new",methods=["GET","POST"])
@require("purchases.manage")
def new_payment():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); amount=money(request.form["amount"]); cash=int(request.form["cash_account_id"])
            if amount<=0: raise ValueError("قيمة السداد يجب أن تكون أكبر من صفر.")
            cash_row=c.execute("SELECT code,name_ar,account_type,is_group,active FROM accounts WHERE id=?",(cash,)).fetchone()
            cash_name=(cash_row["name_ar"] or "").lower() if cash_row else ""
            cash_codes={"1000","1110","1120","1121","1122"}
            cash_words=("صندوق","بنك","بنوك","مصرف","نقد","cash","bank")
            if (not cash_row or cash_row["account_type"]!="asset" or cash_row["is_group"] or not cash_row["active"]
                or (cash_row["code"] not in cash_codes and not any(word in cash_name for word in cash_words))):
                raise ValueError("حساب السداد يجب أن يكون حساب صندوق أو بنك تفصيليًا ونشطًا، وليس حساب إيراد أو مصروف أو أصل آخر.")
            ap=supplier_ap_account(c); c.execute("BEGIN IMMEDIATE")
            dimensions = validate_journal_dimensions(c, {"supplier_id": supplier})
            no="PAY-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            eid=create_journal(c,request.form["payment_date"],no,"سداد مورد",[(ap,amount,0,"تخفيض مديونية المورد"),(cash,0,amount,"سداد نقدي/بنكي")],session["user_id"],dimensions=dimensions)
            cur=c.execute("INSERT INTO supplier_payments(payment_no,supplier_id,payment_date,amount,cash_account_id,journal_entry_id,reference,created_by) VALUES(?,?,?,?,?,?,?,?)",
              (no,supplier,request.form["payment_date"],str(amount),cash,eid,request.form.get("reference",""),session["user_id"]))
            payment_id=cur.lastrowid; remaining=amount
            bills=c.execute("""SELECT * FROM supplier_bills WHERE supplier_id=? AND status='posted' ORDER BY bill_date,id""",(supplier,)).fetchall()
            bills=[b for b in bills if Decimal(b["total_amount"])>Decimal(b["paid_amount"] or "0")+Decimal(b["credited_amount"] or "0")]
            for b in bills:
                due=money(Decimal(b["total_amount"])-Decimal(b["paid_amount"] or "0")-Decimal(b["credited_amount"] or "0")); alloc=min(due,remaining)
                if alloc>0:
                    c.execute("INSERT INTO supplier_payment_allocations(payment_id,bill_id,amount) VALUES(?,?,?)",(payment_id,b["id"],str(alloc)))
                    c.execute("UPDATE supplier_bills SET paid_amount=? WHERE id=?",(str(money(Decimal(b["paid_amount"])+alloc)),b["id"]))
                    remaining-=alloc
                if remaining<=0: break
            if remaining>0: raise ValueError("المبلغ أكبر من مديونية الفواتير المفتوحة؛ لا يتم تسجيل دفعة مقدمة تلقائيًا.")
            c.commit(); g.audit_created_record = ("supplier_payments", payment_id, "supplier_payment_allocations", "payment_id"); flash(f"تم تسجيل السداد {no} وتوزيعه على أقدم الفواتير.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    cash=c.execute("""SELECT * FROM accounts WHERE active=1 AND is_group=0 AND account_type='asset'
      AND (lower(name_ar) LIKE '%صندوق%' OR lower(name_ar) LIKE '%بنك%' OR lower(name_ar) LIKE '%نقد%' OR lower(name_ar) LIKE '%نقاط البيع%'
           OR code IN ('1000','1110','1120')) ORDER BY code""").fetchall()
    c.close(); return render_template("purchase_payment_new.html",suppliers=su,cash_accounts=cash,today=date.today().isoformat())
