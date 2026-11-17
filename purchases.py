from app_paths import DB_PATH
from database import connect_database, fetch_page
"""NEXUS ERP purchasing v1: suppliers, purchase orders, GRNI receipts, bills and payments.
All posting is transactional and idempotent. Tax is posted only when an explicit input-tax account exists.
"""
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from functools import wraps
from datetime import date, datetime, timedelta
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal, money, safe_error_message, decimal_sum_expression
from db_compat import table_columns, table_exists, table_indexes, begin_write_transaction, insert_and_get_id
from stock import consume_layers, q4, make_lot, product_uom_factor, product_uom_options, product_uom_storage
from analytic_dimensions import initialize_analytic_dimensions_schema, validate_journal_dimensions, active_dimension_options
from code_sequences import next_code

purchase_bp = Blueprint("purchase", __name__, url_prefix="/purchases")

def db():
    return connect_database(DB_PATH, isolation_level=None)


def _table_exists(conn, name):
    """Database-neutral table existence check; SQL execution remains opt-in."""
    return table_exists(conn, name)


def _columns(conn, name):
    """Database-neutral column metadata for a known table."""
    return table_columns(conn, name)

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
    CREATE TABLE IF NOT EXISTS purchase_requests(
      id INTEGER PRIMARY KEY, request_no TEXT NOT NULL UNIQUE,
      request_date TEXT NOT NULL, required_date TEXT,
      department TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL,
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      quantity TEXT NOT NULL, estimated_unit_price TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'draft',
      created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      submitted_by INTEGER, submitted_at TEXT,
      approved_by INTEGER, approved_at TEXT, approval_reason TEXT,
      rejected_by INTEGER, rejected_at TEXT, rejection_reason TEXT,
      converted_to_po_id INTEGER REFERENCES purchase_orders(id), converted_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_purchase_requests_status_date
      ON purchase_requests(status, request_date, id);
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
    CREATE TABLE IF NOT EXISTS supplier_bill_inventory_lines(
      id INTEGER PRIMARY KEY,
      bill_id INTEGER NOT NULL REFERENCES supplier_bills(id) ON DELETE CASCADE,
      line_no INTEGER NOT NULL,
      receipt_line_id INTEGER NOT NULL REFERENCES goods_receipt_lines(id),
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      billed_quantity TEXT NOT NULL,
      invoice_unit_price TEXT NOT NULL,
      line_total TEXT NOT NULL,
      UNIQUE(bill_id,line_no)
    );
    CREATE INDEX IF NOT EXISTS idx_supplier_bill_inventory_receipt_line
      ON supplier_bill_inventory_lines(receipt_line_id);

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
      quantity TEXT NOT NULL, unit_cost TEXT NOT NULL, total_cost TEXT NOT NULL,
      entered_quantity TEXT, uom_id INTEGER REFERENCES stock_uoms(id), uom_key TEXT,
      entered_uom_name TEXT, conversion_factor TEXT NOT NULL DEFAULT '1'
    );
    CREATE TABLE IF NOT EXISTS supplier_credit_note_layer_allocations(
      id INTEGER PRIMARY KEY,
      credit_note_line_id INTEGER NOT NULL REFERENCES supplier_credit_note_lines(id) ON DELETE CASCADE,
      stock_movement_id INTEGER NOT NULL REFERENCES stock_movements(id),
      source_layer_id INTEGER NOT NULL REFERENCES stock_valuation_layers(id),
      lot_id INTEGER REFERENCES stock_lots(id),
      location_id INTEGER REFERENCES stock_locations(id),
      quantity TEXT NOT NULL, unit_cost TEXT NOT NULL, total_cost TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_supplier_credit_layer_line
      ON supplier_credit_note_layer_allocations(credit_note_line_id);
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
    credit_note_columns=_columns(c, "supplier_credit_notes")
    for name,declaration in (
        ("cancel_journal_entry_id","INTEGER"),
        ("cancelled_by","INTEGER"),
        ("cancelled_at","TEXT"),
        ("cancellation_reason","TEXT"),
    ):
        if name not in credit_note_columns:
            c.execute("ALTER TABLE supplier_credit_notes ADD COLUMN "+name+" "+declaration)
    credit_line_columns=_columns(c, "supplier_credit_note_lines")
    for name,declaration in (("entered_quantity","TEXT"),("uom_id","INTEGER REFERENCES stock_uoms(id)"),
        ("uom_key","TEXT"),("entered_uom_name","TEXT"),("conversion_factor","TEXT NOT NULL DEFAULT '1'")):
        if name not in credit_line_columns:
            c.execute("ALTER TABLE supplier_credit_note_lines ADD COLUMN "+name+" "+declaration)
    # suppliers now exists: install the supplier_id column on journal lines
    # through the same additive migration used by the other analytical dimensions.
    initialize_analytic_dimensions_schema(c)
    for table, fields in (
        ("purchase_order_lines",(("entered_quantity","TEXT"),("uom_id","INTEGER REFERENCES stock_uoms(id)"),("uom_key","TEXT"),("entered_uom_name","TEXT"),("conversion_factor","TEXT NOT NULL DEFAULT '1'"))),
        ("goods_receipt_lines",(("entered_quantity","TEXT"),("uom_id","INTEGER REFERENCES stock_uoms(id)"),("uom_key","TEXT"),("entered_uom_name","TEXT"),("conversion_factor","TEXT NOT NULL DEFAULT '1'"))),
    ):
        existing=_columns(c, table)
        for name,declaration in fields:
            if name not in existing: c.execute("ALTER TABLE "+table+" ADD COLUMN "+name+" "+declaration)
    order_columns = _columns(c, "purchase_orders")
    for name, declaration in (
        ("submitted_by", "INTEGER"), ("submitted_at", "TEXT"),
        ("approved_by", "INTEGER"), ("approved_at", "TEXT"),
        ("rejected_by", "INTEGER"), ("rejected_at", "TEXT"),
        ("rejection_reason", "TEXT"), ("approval_reason", "TEXT"),
    ):
        if name not in order_columns:
            c.execute("ALTER TABLE purchase_orders ADD COLUMN " + name + " " + declaration)
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_approval ON purchase_orders(status, created_at, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchase_requests_creator ON purchase_requests(created_by, status, id)")
    bill_columns = _columns(c, "supplier_bills")
    if "credited_amount" not in bill_columns:
        c.execute("ALTER TABLE supplier_bills ADD COLUMN credited_amount TEXT NOT NULL DEFAULT '0'")
    if "input_vat_deductible_percent" not in bill_columns:
        c.execute("ALTER TABLE supplier_bills ADD COLUMN input_vat_deductible_percent TEXT NOT NULL DEFAULT '100'")
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
    receipt_line_columns = _columns(c, "goods_receipt_lines")
    if "location_id" not in receipt_line_columns:
        c.execute("ALTER TABLE goods_receipt_lines ADD COLUMN location_id INTEGER REFERENCES stock_locations(id)")
    receipt_columns = _columns(c, "goods_receipts")
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
    integrity_columns = _columns(c, "purchase_integrity_issues")
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
    begin_write_transaction(c)
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
    begin_write_transaction(c)
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
    begin_write_transaction(c)
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
    if _table_exists(c, "fixed_assets"):
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

def _supplier_price_insights(conn, lookback_days=365):
    """Compare posted historical supplier prices by product using quantity-weighted averages."""
    required=("supplier_bill_inventory_lines","supplier_bills","suppliers","stock_products")
    present={name for name in required if _table_exists(conn, name)}
    if not set(required).issubset(present):
        return [], False
    start=(date.today()-timedelta(days=lookback_days)).isoformat()
    rows=conn.execute("""SELECT bl.product_id,bl.billed_quantity,bl.invoice_unit_price,
            b.supplier_id,b.bill_date,b.id bill_id,s.name supplier_name,
            p.sku,p.name_ar
        FROM supplier_bill_inventory_lines bl
        JOIN supplier_bills b ON b.id=bl.bill_id
        JOIN suppliers s ON s.id=b.supplier_id
        LEFT JOIN stock_products p ON p.id=bl.product_id
        WHERE b.status='posted' AND b.bill_date>=? AND b.bill_date<=?
        ORDER BY b.bill_date DESC,b.id DESC,bl.id DESC""",
        (start,date.today().isoformat())).fetchall()
    grouped={}
    for row in rows:
        key=(row["product_id"],row["supplier_id"])
        item=grouped.setdefault(key,{
            "product_id":row["product_id"],"sku":row["sku"] or "",
            "product_name":row["name_ar"] or "صنف غير معروف",
            "supplier_id":row["supplier_id"],"supplier_name":row["supplier_name"],
            "quantity":Decimal("0"),"weighted_value":Decimal("0"),
            "latest_price":None,"latest_date":None,
        })
        qty=Decimal(str(row["billed_quantity"] or "0"))
        price=Decimal(str(row["invoice_unit_price"] or "0"))
        if qty>0 and price>=0:
            item["quantity"]+=qty
            item["weighted_value"]+=qty*price
        if item["latest_price"] is None:
            item["latest_price"]=price
            item["latest_date"]=row["bill_date"]
    by_product={}
    for item in grouped.values():
        item["weighted_average"]=(item["weighted_value"]/item["quantity"]) if item["quantity"] else Decimal("0")
        item["change_percent"]=(
            (item["latest_price"]-item["weighted_average"])*Decimal("100")/item["weighted_average"]
            if item["weighted_average"] else None
        )
        by_product.setdefault(item["product_id"],[]).append(item)
    result=[]
    for suppliers_for_product in by_product.values():
        suppliers_for_product.sort(key=lambda item:(item["latest_price"],item["supplier_name"]))
        best_price=suppliers_for_product[0]["latest_price"]
        for item in suppliers_for_product:
            item["best_latest_price"]=item["latest_price"]==best_price
            result.append(item)
    result.sort(key=lambda item:(item["sku"],item["latest_price"],item["supplier_name"]))
    return result, True


@purchase_bp.route("/")
@require("purchases.view")
def index():
    c=db()
    suppliers=c.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    price_insights, price_insights_available = _supplier_price_insights(c)
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
        price_insights=price_insights,price_insights_available=price_insights_available,
        today=date.today().isoformat())


def build_grni_reconciliation(c, month):
    """Reconcile posted GRNI receipt/bill subledger movements to GL account 2190."""
    try:
        period_start = date.fromisoformat(str(month) + "-01")
    except (TypeError, ValueError):
        raise ValueError("حدد شهرًا صحيحًا بصيغة سنة-شهر.")
    if period_start.strftime("%Y-%m") != str(month):
        raise ValueError("حدد شهرًا صحيحًا بصيغة سنة-شهر.")
    next_month = date(period_start.year + (period_start.month == 12),
                      1 if period_start.month == 12 else period_start.month + 1, 1)
    period_end = next_month - timedelta(days=1)
    start_text, end_text = period_start.isoformat(), period_end.isoformat()
    account_row = c.execute(
        """SELECT id,code,name_ar FROM accounts WHERE code='2190' AND active=1 AND is_group=0 LIMIT 1"""
    ).fetchone()
    account_available = account_row is not None
    receipts = c.execute(
        """SELECT r.id,r.receipt_no,r.receipt_date,r.total_value,s.name AS supplier_name,
                  w.code AS warehouse_code,w.name_ar AS warehouse_name
           FROM goods_receipts r JOIN suppliers s ON s.id=r.supplier_id
           JOIN stock_warehouses w ON w.id=r.warehouse_id
           WHERE r.status='posted' AND r.receipt_date<=? ORDER BY r.receipt_date,r.id""",
        (end_text,),
    ).fetchall()
    receipt_ids = [row["id"] for row in receipts]
    allocations, bill_period_total, bill_opening_total = {}, money("0"), money("0")
    for offset in range(0, len(receipt_ids), 500):
        batch = receipt_ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = c.execute(
            f"""SELECT x.receipt_id,x.matched_amount,b.bill_no,b.bill_date
                FROM supplier_bill_receipts x JOIN supplier_bills b ON b.id=x.bill_id
                WHERE b.status='posted' AND b.bill_kind='inventory'
                  AND b.bill_date<=? AND x.receipt_id IN ({placeholders})
                ORDER BY b.bill_date,b.id""", [end_text, *batch]
        ).fetchall()
        for item in rows:
            amount = money(item["matched_amount"] or "0")
            bucket = allocations.setdefault(item["receipt_id"], {
                "total": money("0"), "opening": money("0"), "period": money("0"), "bills": []
            })
            bucket["total"] = money(bucket["total"] + amount)
            if item["bill_date"] < start_text:
                bucket["opening"] = money(bucket["opening"] + amount)
                bill_opening_total = money(bill_opening_total + amount)
            else:
                bucket["period"] = money(bucket["period"] + amount)
                bill_period_total = money(bill_period_total + amount)
            bucket["bills"].append({"bill_no": item["bill_no"], "bill_date": item["bill_date"], "amount": amount})
    opening_receipts = period_receipts = closing_receipts = money("0")
    detail_rows = []
    for receipt in receipts:
        received = money(receipt["total_value"] or "0")
        bucket = allocations.get(receipt["id"], {})
        billed = bucket.get("total", money("0"))
        billed_period = bucket.get("period", money("0"))
        if receipt["receipt_date"] < start_text:
            opening_receipts = money(opening_receipts + received)
        else:
            period_receipts = money(period_receipts + received)
        closing_receipts = money(closing_receipts + received)
        outstanding = money(received - billed)
        if receipt["receipt_date"] < start_text and outstanding == 0 and billed_period == 0:
            continue
        detail_rows.append({
            "receipt_no": receipt["receipt_no"], "receipt_date": receipt["receipt_date"],
            "supplier_name": receipt["supplier_name"],
            "warehouse": " — ".join(x for x in (receipt["warehouse_code"], receipt["warehouse_name"]) if x),
            "received": received, "billed": billed,
            "billed_opening": bucket.get("opening", money("0")), "billed_period": billed_period,
            "outstanding": outstanding,
            "age_days": max(0, (period_end - date.fromisoformat(receipt["receipt_date"])).days),
            "status": ("مسوّى بالكامل" if outstanding == 0 else
                       "غير مفوتر" if billed == 0 else
                       "تجاوز تخصيص الفواتير قيمة الاستلام" if outstanding < 0 else "مسوّى جزئيًا"),
            "bills": bucket.get("bills", []),
        })
    subledger_opening = money(opening_receipts - bill_opening_total)
    subledger_closing = money(closing_receipts - bill_opening_total - bill_period_total)
    subledger_period_net = money(period_receipts - bill_period_total)
    gl_opening_debit = gl_opening_credit = gl_period_debit = gl_period_credit = money("0")
    if account_available:
        gl_rows = c.execute(
            """SELECT je.entry_date,jl.debit,jl.credit FROM journal_lines jl
               JOIN journal_entries je ON je.id=jl.entry_id
               WHERE jl.account_id=? AND je.status='posted' AND je.entry_date<=?
               ORDER BY je.entry_date,je.id,jl.id""", (account_row["id"], end_text)
        ).fetchall()
        for line in gl_rows:
            debit, credit = money(line["debit"] or "0"), money(line["credit"] or "0")
            if line["entry_date"] < start_text:
                gl_opening_debit = money(gl_opening_debit + debit)
                gl_opening_credit = money(gl_opening_credit + credit)
            else:
                gl_period_debit = money(gl_period_debit + debit)
                gl_period_credit = money(gl_period_credit + credit)
    gl_opening = money(gl_opening_credit - gl_opening_debit)
    gl_period_net = money(gl_period_credit - gl_period_debit)
    gl_closing = money(gl_opening + gl_period_net)
    return {
        "month": str(month), "period_start": start_text, "period_end": end_text,
        "account_available": account_available,
        "account_code": account_row["code"] if account_available else "2190",
        "account_name": account_row["name_ar"] if account_available else "بضاعة مستلمة غير مفوترة GRNI",
        "opening_receipts": opening_receipts, "opening_bill_settlements": bill_opening_total,
        "subledger_opening": subledger_opening, "period_receipts": period_receipts,
        "period_bill_settlements": bill_period_total, "subledger_period_net": subledger_period_net,
        "subledger_closing": subledger_closing, "gl_opening_debit": gl_opening_debit,
        "gl_opening_credit": gl_opening_credit, "gl_opening": gl_opening,
        "gl_period_debit": gl_period_debit, "gl_period_credit": gl_period_credit,
        "gl_period_net": gl_period_net, "gl_closing": gl_closing,
        "opening_difference": money(gl_opening - subledger_opening) if account_available else None,
        "movement_difference": money(gl_period_net - subledger_period_net) if account_available else None,
        "closing_difference": money(gl_closing - subledger_closing) if account_available else None,
        "rows": detail_rows,
    }


@purchase_bp.route("/grni-reconciliation")
@require("purchases.view")
def grni_reconciliation():
    selected_month = (request.args.get("month") or date.today().strftime("%Y-%m")).strip()
    c = db()
    try:
        report = build_grni_reconciliation(c, selected_month)
        return render_template("purchase_grni_reconciliation.html", report=report, selected_month=selected_month)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("purchase.grni_reconciliation", month=date.today().strftime("%Y-%m")))
    finally:
        c.close()


@purchase_bp.route("/three-way-match")
@require("purchases.view")
def three_way_match():
    c = db()
    query = """
        SELECT po.po_no,po.order_date,po.status AS order_status,
               s.name AS supplier_name,p.sku,p.name_ar AS product_name,p.uom,
               pol.id AS po_line_id,pol.quantity AS ordered_quantity,
               pol.unit_price AS ordered_unit_price
        FROM purchase_order_lines pol
        JOIN purchase_orders po ON po.id=pol.po_id
        JOIN suppliers s ON s.id=po.supplier_id
        JOIN stock_products p ON p.id=pol.product_id
        WHERE po.status<>'cancelled'
        ORDER BY po.order_date DESC,po.id DESC,pol.id
    """
    page = fetch_page(c, query, "SELECT COUNT(*) FROM purchase_order_lines pol JOIN purchase_orders po ON po.id=pol.po_id WHERE po.status<>'cancelled'",
        page=request.args.get("page",1,type=int),
        per_page=request.args.get("per_page",50,type=int))
    rows = [dict(row) for row in page["rows"]]
    line_ids = [row["po_line_id"] for row in rows]
    received_totals = {}
    billed_totals = {}
    legacy_bill_counts = {}
    if line_ids:
        placeholders = ",".join("?" for _ in line_ids)
        for item in c.execute(f"""
            SELECT gl.po_line_id,gl.quantity,gl.total_cost
            FROM goods_receipt_lines gl
            JOIN goods_receipts gr ON gr.id=gl.receipt_id
            WHERE gr.status='posted' AND gl.po_line_id IN ({placeholders})
        """, line_ids).fetchall():
            key = item["po_line_id"]
            total = received_totals.setdefault(key, {"quantity": Decimal("0"), "value": Decimal("0")})
            total["quantity"] += Decimal(str(item["quantity"] or "0"))
            total["value"] += money(item["total_cost"] or "0")
        for item in c.execute(f"""
            SELECT gl.po_line_id,bl.billed_quantity,bl.line_total
            FROM supplier_bill_inventory_lines bl
            JOIN supplier_bills sb ON sb.id=bl.bill_id
            JOIN goods_receipt_lines gl ON gl.id=bl.receipt_line_id
            WHERE sb.status IN ('draft','pending_approval','approved','posted')
              AND gl.po_line_id IN ({placeholders})
        """, line_ids).fetchall():
            key = item["po_line_id"]
            total = billed_totals.setdefault(key, {"quantity": Decimal("0"), "value": Decimal("0")})
            total["quantity"] += Decimal(str(item["billed_quantity"] or "0"))
            total["value"] += money(item["line_total"] or "0")
        for item in c.execute(f"""
            SELECT gl.po_line_id,COUNT(DISTINCT sb.id) AS legacy_count
            FROM supplier_bill_receipts alloc
            JOIN supplier_bills sb ON sb.id=alloc.bill_id
            JOIN goods_receipt_lines gl ON gl.receipt_id=alloc.receipt_id
            WHERE sb.status IN ('draft','pending_approval','approved','posted')
              AND gl.po_line_id IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM supplier_bill_inventory_lines detail
                  WHERE detail.bill_id=sb.id
              )
            GROUP BY gl.po_line_id
        """, line_ids).fetchall():
            legacy_bill_counts[item["po_line_id"]] = item["legacy_count"]
    for row in rows:
        ordered = Decimal(str(row["ordered_quantity"] or "0"))
        ordered_price = Decimal(str(row["ordered_unit_price"] or "0"))
        received = received_totals.get(row["po_line_id"], {"quantity": Decimal("0"), "value": Decimal("0")})
        billed = billed_totals.get(row["po_line_id"], {"quantity": Decimal("0"), "value": Decimal("0")})
        received_qty, received_value = received["quantity"], received["value"]
        billed_qty, billed_value = billed["quantity"], billed["value"]
        row["received_quantity"] = received_qty
        row["billed_quantity"] = billed_qty
        row["unbilled_quantity"] = received_qty - billed_qty
        row["received_unit_price"] = (received_value / received_qty) if received_qty else None
        row["invoice_unit_price"] = (billed_value / billed_qty) if billed_qty else None
        row["price_gap"] = (row["invoice_unit_price"] - ordered_price) if billed_qty else None
        row["legacy_bill_count"] = legacy_bill_counts.get(row["po_line_id"], 0)
        if received_qty == 0:
            row["match_status"] = "لم يتم الاستلام"
        elif row["legacy_bill_count"] and billed_qty == 0:
            row["match_status"] = "فاتورة قديمة بلا تفاصيل صنف"
        elif row["legacy_bill_count"]:
            row["match_status"] = "مطابقة غير مكتملة - فواتير قديمة"
        elif billed_qty == 0:
            row["match_status"] = "مستلم ولم يفوتر"
        elif billed_qty > received_qty:
            row["match_status"] = "كمية مفوترة أكبر من المستلم"
        elif billed_qty < received_qty:
            row["match_status"] = "فوترة جزئية"
        elif row["price_gap"] is not None and abs(row["price_gap"]) >= Decimal("0.01"):
            row["match_status"] = "فرق سعر"
        elif received_qty < ordered:
            row["match_status"] = "استلام جزئي"
        else:
            row["match_status"] = "مطابق"
    c.close()
    return render_template("purchase_three_way_match.html", rows=rows, page=page)


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
    indexes = table_indexes(c)
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
        begin_write_transaction(c)
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
        flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.integrity_issues"))


@purchase_bp.route("/suppliers",methods=["GET","POST"])
@require("purchases.manage")
def suppliers():
    c=db()
    if request.method=="POST":
        try:
            supplier_code = next_code(c, "supplier", "SUP", "suppliers", "code")
            c.execute("INSERT INTO suppliers(code,name,tax_number,phone,email,address) VALUES(?,?,?,?,?,?)",
              (supplier_code,request.form["name"].strip(),request.form.get("tax_number","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("address","").strip()))
            c.commit(); flash("تم حفظ المورد.","success")
        except sqlite3.IntegrityError: c.rollback(); flash("كود المورد مستخدم بالفعل.","error")
    suppliers_page = fetch_page(c,"SELECT * FROM suppliers ORDER BY name",
        "SELECT COUNT(*) FROM suppliers",
        page=request.args.get("suppliers_page",1,type=int),
        per_page=request.args.get("suppliers_per_page",50,type=int))
    suppliers_rows=suppliers_page["rows"]
    outstanding_by_supplier={}
    bill_total_sum_sql = decimal_sum_expression(c, "total_amount")
    bill_paid_sum_sql = decimal_sum_expression(c, "paid_amount")
    bill_credited_sum_sql = decimal_sum_expression(c, "credited_amount")
    for bill in c.execute(f"""SELECT supplier_id,
        {bill_total_sum_sql} total_amount,
        {bill_paid_sum_sql} paid_amount,
        {bill_credited_sum_sql} credited_amount
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


@purchase_bp.route("/requests")
@require("purchases.view")
def purchase_requests():
    c = db()
    rows = c.execute("""
        SELECT r.*,p.sku,p.name_ar AS product_name,
               po.po_no AS converted_po_no
        FROM purchase_requests r
        JOIN stock_products p ON p.id=r.product_id
        LEFT JOIN purchase_orders po ON po.id=r.converted_to_po_id
        ORDER BY r.id DESC
    """).fetchall()
    suppliers = c.execute("SELECT id,name FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    c.close()
    return render_template("purchase_requests.html", requests=rows, suppliers=suppliers)


@purchase_bp.route("/requests/new", methods=["GET", "POST"])
@require("purchases.manage")
def new_purchase_request():
    c = db()
    if request.method == "POST":
        try:
            product_id = int(request.form["product_id"])
            quantity = Decimal(request.form["quantity"])
            estimated_price = money(request.form.get("estimated_unit_price", "0"))
            purpose = (request.form.get("purpose") or "").strip()
            department = (request.form.get("department") or "").strip()
            request_date = (request.form.get("request_date") or "").strip()
            required_date = (request.form.get("required_date") or "").strip() or None
            product = c.execute("SELECT id FROM stock_products WHERE id=? AND active=1", (product_id,)).fetchone()
            if not product:
                raise ValueError("الصنف غير موجود أو غير نشط.")
            if not quantity.is_finite() or not estimated_price.is_finite() or quantity <= 0 or estimated_price < 0:
                raise ValueError("الكمية يجب أن تكون رقمًا موجبًا صالحًا والسعر التقديري لا يقل عن صفر.")
            if not purpose or len(purpose) < 5:
                raise ValueError("اكتب الغرض من الطلب بوضوح (5 أحرف على الأقل).")
            if not request_date:
                raise ValueError("تاريخ الطلب مطلوب.")
            try:
                date.fromisoformat(request_date)
                if required_date:
                    date.fromisoformat(required_date)
            except ValueError:
                raise ValueError("صيغة تاريخ الطلب أو الاحتياج غير صحيحة.")
            if required_date and required_date < request_date:
                raise ValueError("تاريخ الاحتياج لا يمكن أن يسبق تاريخ الطلب.")
            begin_write_transaction(c)
            request_no = "PR-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
            c.execute("""INSERT INTO purchase_requests(
                request_no,request_date,required_date,department,purpose,product_id,
                quantity,estimated_unit_price,status,created_by
            ) VALUES(?,?,?,?,?,?,?,?,'draft',?)""",
                (request_no,request_date,required_date,department,purpose,product_id,
                 str(quantity),str(estimated_price),session["user_id"]))
            c.commit()
            flash(f"تم إنشاء طلب الشراء الداخلي {request_no}.", "success")
            return redirect(url_for("purchase.purchase_requests"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(safe_error_message(exc, "تعذر حفظ طلب الشراء. لم يتم حفظ أي تغييرات."), "error")
    products = c.execute("SELECT id,sku,name_ar,uom FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    c.close()
    return render_template("purchase_request_new.html", products=products, today=date.today().isoformat())


@purchase_bp.route("/requests/<int:request_id>/submit", methods=["POST"])
@require("purchases.manage")
def submit_purchase_request(request_id):
    c = db()
    try:
        begin_write_transaction(c)
        row = c.execute("SELECT * FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise ValueError("طلب الشراء غير موجود.")
        if row["status"] not in {"draft", "rejected"}:
            raise ValueError("لا يمكن إرسال الطلب من حالته الحالية.")
        c.execute("""UPDATE purchase_requests SET status='pending_approval',
            submitted_by=?,submitted_at=CURRENT_TIMESTAMP,approved_by=NULL,approved_at=NULL,
            approval_reason=NULL,rejected_by=NULL,rejected_at=NULL,rejection_reason=NULL
            WHERE id=?""", (session["user_id"], request_id))
        c.commit()
        flash("تم إرسال طلب الشراء للاعتماد.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(safe_error_message(exc, "تعذر إرسال الطلب."), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.purchase_requests"))


@purchase_bp.route("/requests/<int:request_id>/approve", methods=["POST"])
@require("purchases.approve")
def approve_purchase_request(request_id):
    c = db()
    try:
        begin_write_transaction(c)
        row = c.execute("SELECT * FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "pending_approval":
            raise ValueError("طلب الشراء غير موجود أو ليس بانتظار الاعتماد.")
        if row["created_by"] == session["user_id"] or row["submitted_by"] == session["user_id"]:
            raise ValueError("لا يجوز اعتماد طلب أنشأته أو أرسلته للمراجعة بنفسك.")
        if row["created_by"] is None or row["submitted_by"] is None:
            raise ValueError("بيانات منشئ أو مرسل الطلب غير مكتملة.")
        c.execute("""UPDATE purchase_requests SET status='approved',approved_by=?,
            approved_at=CURRENT_TIMESTAMP,approval_reason=?
            WHERE id=? AND status='pending_approval'""",
            (session["user_id"], (request.form.get("reason") or "").strip(), request_id))
        if c.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("تغيرت حالة الطلب؛ أعد تحميل الصفحة.")
        c.commit()
        flash("تم اعتماد طلب الشراء الداخلي.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(safe_error_message(exc, "تعذر اعتماد الطلب."), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.purchase_requests"))


@purchase_bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@require("purchases.approve")
def reject_purchase_request(request_id):
    c = db()
    try:
        reason = (request.form.get("reason") or "").strip()
        if len(reason) < 5:
            raise ValueError("سبب الرفض مطلوب (5 أحرف على الأقل).")
        begin_write_transaction(c)
        row = c.execute("SELECT * FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "pending_approval":
            raise ValueError("طلب الشراء غير موجود أو ليس بانتظار الاعتماد.")
        if row["created_by"] == session["user_id"] or row["submitted_by"] == session["user_id"]:
            raise ValueError("لا يجوز رفض طلب أنشأته أو أرسلته للمراجعة بنفسك.")
        c.execute("""UPDATE purchase_requests SET status='rejected',rejected_by=?,
            rejected_at=CURRENT_TIMESTAMP,rejection_reason=?
            WHERE id=? AND status='pending_approval'""", (session["user_id"],reason,request_id))
        if c.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("تغيرت حالة الطلب؛ أعد تحميل الصفحة.")
        c.commit()
        flash("تم رفض الطلب مع حفظ السبب.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(safe_error_message(exc, "تعذر رفض الطلب."), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.purchase_requests"))


@purchase_bp.route("/requests/<int:request_id>/convert", methods=["POST"])
@require("purchases.manage")
def convert_purchase_request(request_id):
    c = db()
    try:
        supplier_id = int(request.form["supplier_id"])
        unit_price = money(request.form["unit_price"])
        begin_write_transaction(c)
        row = c.execute("""SELECT r.*,p.active AS product_active
            FROM purchase_requests r JOIN stock_products p ON p.id=r.product_id
            WHERE r.id=?""", (request_id,)).fetchone()
        supplier = c.execute("SELECT id FROM suppliers WHERE id=? AND active=1", (supplier_id,)).fetchone()
        if not row or row["status"] != "approved" or row["converted_to_po_id"]:
            raise ValueError("لا يمكن تحويل الطلب إلا إذا كان معتمدًا وغير محول سابقًا.")
        if not supplier or not row["product_active"]:
            raise ValueError("المورد أو الصنف غير نشط أو غير موجود.")
        if not unit_price.is_finite() or unit_price < 0:
            raise ValueError("سعر الوحدة يجب أن يكون رقمًا صالحًا ولا يمكن أن يكون سالبًا.")
        po_no = "PO-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        po_id = insert_and_get_id(c, """INSERT INTO purchase_orders(
            po_no,supplier_id,order_date,expected_date,notes,created_by
        ) VALUES(?,?,?,?,?,?)""",
            (po_no,supplier_id,date.today().isoformat(),row["required_date"],
             "من طلب الشراء الداخلي " + row["request_no"] + " — " + row["purpose"],
             session["user_id"]))
        c.execute("""INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price)
            VALUES(?,?,?,?)""", (po_id,row["product_id"],row["quantity"],str(unit_price)))
        c.execute("""UPDATE purchase_requests SET status='converted',
            converted_to_po_id=?,converted_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='approved' AND converted_to_po_id IS NULL""", (po_id,request_id))
        if c.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("تم تحويل الطلب بواسطة مستخدم آخر؛ لم يتم إنشاء أمر إضافي.")
        c.commit()
        flash(f"تم تحويل الطلب إلى أمر الشراء {po_no}. يجب إرسال أمر الشراء للمراجعة كالمعتاد.", "success")
    except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
        c.rollback()
        flash(safe_error_message(exc, "تعذر تحويل الطلب. لم يتم إنشاء أمر شراء."), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.purchase_requests"))


@purchase_bp.route("/orders/new",methods=["GET","POST"])
@require("purchases.manage")
def new_order():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"])
            entered_qty=Decimal(request.form["quantity"]); entered_price=money(request.form["unit_price"])
            if entered_qty<=0 or entered_price<0: raise ValueError("الكمية والسعر غير صحيحين.")
            factor,uom_name=product_uom_factor(c,product,request.form.get("uom_id"),"purchase")
            qty=q4(entered_qty*factor); price=entered_price/factor
            stored_uom_id,entered_uom_name=product_uom_storage(c,product,request.form.get("uom_id"))
            begin_write_transaction(c)
            no="PO-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            order_id=insert_and_get_id(c,"INSERT INTO purchase_orders(po_no,supplier_id,order_date,expected_date,notes,created_by) VALUES(?,?,?,?,?,?)",
              (no,supplier,request.form["order_date"],request.form.get("expected_date") or None,request.form.get("notes",""),session["user_id"]))
            c.execute("""INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price,entered_quantity,uom_id,uom_key,entered_uom_name,conversion_factor)
              VALUES(?,?,?,?,?,?,?,?,?)""",(order_id,product,str(qty),str(price),str(entered_qty),stored_uom_id,request.form.get("uom_id"),entered_uom_name,str(factor)))
            c.commit(); flash(f"تم إنشاء أمر الشراء {no}.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    products=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    product_uoms={p["id"]:product_uom_options(c,p["id"]) for p in products}
    c.close(); return render_template("purchase_order_new.html",suppliers=su,products=products,product_uoms=product_uoms,today=date.today().isoformat())


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
        flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
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
        flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
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
        flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
    finally:
        c.close()
    return redirect(url_for("purchase.index"))

@purchase_bp.route("/orders/<int:order_id>/edit",methods=["GET","POST"])
@require("purchases.manage")
def edit_order(order_id):
    c=db(); order=c.execute("""SELECT p.*,l.id line_id,l.product_id,l.quantity,l.unit_price,l.entered_quantity,l.uom_id,l.uom_key,l.entered_uom_name,l.conversion_factor
      FROM purchase_orders p LEFT JOIN purchase_order_lines l ON l.po_id=p.id WHERE p.id=?""",(order_id,)).fetchone()
    if not order:
        c.close(); flash("أمر الشراء غير موجود.","error"); return redirect(url_for("purchase.index"))
    order=dict(order)
    order["entered_unit_price"]=str(Decimal(str(order["unit_price"] or "0"))*Decimal(str(order.get("conversion_factor") or "1")))
    received=c.execute("SELECT 1 FROM goods_receipts WHERE po_id=? LIMIT 1",(order_id,)).fetchone() is not None
    if received or order["status"] not in {"draft", "rejected"}:
        c.close(); flash("لا يمكن تعديل أمر شراء تم استلامه أو اعتماده.","error"); return redirect(url_for("purchase.index"))
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"])
            entered_qty=Decimal(request.form["quantity"]); entered_price=money(request.form["unit_price"])
            if entered_qty<=0 or entered_price<0: raise ValueError("الكمية والسعر غير صحيحين.")
            factor,uom_name=product_uom_factor(c,product,request.form.get("uom_id"),"purchase")
            qty=q4(entered_qty*factor); price=entered_price/factor
            stored_uom_id,entered_uom_name=product_uom_storage(c,product,request.form.get("uom_id"))
            c.execute("""UPDATE purchase_orders SET supplier_id=?,order_date=?,expected_date=?,notes=?,status='draft',
              rejected_by=NULL,rejected_at=NULL,rejection_reason=NULL WHERE id=?""",
              (supplier,request.form["order_date"],request.form.get("expected_date") or None,request.form.get("notes",""),order_id))
            c.execute("""UPDATE purchase_order_lines SET product_id=?,quantity=?,unit_price=?,entered_quantity=?,uom_id=?,uom_key=?,entered_uom_name=?,conversion_factor=? WHERE id=?""",
              (product,str(qty),str(price),str(entered_qty),stored_uom_id,request.form.get("uom_id"),entered_uom_name,str(factor),order["line_id"]))
            c.commit(); c.close(); flash("تم تعديل أمر الشراء.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,sqlite3.Error) as e: c.rollback(); flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    suppliers=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    products=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    product_uoms={p["id"]:product_uom_options(c,p["id"]) for p in products}
    c.close(); return render_template("purchase_order_edit.html",order=order,suppliers=suppliers,products=products,product_uoms=product_uoms)



@purchase_bp.route("/orders/<int:order_id>/print")
@require("purchases.view")
def order_print(order_id):
    c = db()
    order = c.execute("""SELECT p.*,s.code supplier_code,s.name supplier_name,
        s.tax_number supplier_tax_number FROM purchase_orders p
        JOIN suppliers s ON s.id=p.supplier_id WHERE p.id=?""", (order_id,)).fetchone()
    if not order:
        c.close()
        from flask import abort
        abort(404)
    raw_lines = c.execute("""SELECT l.*,p.sku,p.name_ar product_name,p.uom
        FROM purchase_order_lines l JOIN stock_products p ON p.id=l.product_id
        WHERE l.po_id=? ORDER BY l.id""", (order_id,)).fetchall()
    lines = []
    order_total = Decimal("0")
    for row in raw_lines:
        item = dict(row)
        item["line_total"] = Decimal(str(row["quantity"])) * Decimal(str(row["unit_price"]))
        item["display_quantity"] = row["entered_quantity"] or row["quantity"]
        item["display_uom"] = row["entered_uom_name"] or row["uom"]
        try:
            factor, _ = product_uom_factor(c,row["product_id"],row["uom_key"])
            item["display_unit_price"] = Decimal(str(row["unit_price"]))*factor
        except (ValueError,TypeError):
            item["display_unit_price"] = Decimal(str(row["unit_price"]))
        order_total += item["line_total"]
        lines.append(item)
    settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','base_currency','tax_number','company_address','company_phone','company_email','company_website','company_logo_data')")}
    c.close()
    return render_template("purchase_order_print.html", order=order, lines=lines,
                           settings=settings, branch=None, order_total=order_total)


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

def refresh_purchase_order_receipt_status(c, po_id):
    """Derive the PO header status from every line after a goods receipt."""
    rows = c.execute(
        "SELECT quantity,received_qty FROM purchase_order_lines WHERE po_id=? ORDER BY id",
        (po_id,),
    ).fetchall()
    if not rows:
        raise ValueError("أمر الشراء لا يحتوي على أصناف قابلة للاستلام.")
    quantities = [(Decimal(str(row["quantity"])), Decimal(str(row["received_qty"] or "0")))
                  for row in rows]
    if all(received >= ordered for ordered, received in quantities):
        status = "received"
    elif any(received > 0 for _, received in quantities):
        status = "partially_received"
    else:
        status = "approved"
    c.execute("UPDATE purchase_orders SET status=? WHERE id=?", (status, po_id))
    return status


@purchase_bp.route("/receipts/new",methods=["GET","POST"])
@require("purchases.manage")
def new_receipt():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"]); wh=int(request.form["warehouse_id"])
            po_id=int(request.form["po_id"]) if request.form.get("po_id") else None
            entered_qty=Decimal(request.form["quantity"]); entered_unit=money(request.form["unit_cost"])
            if entered_qty<=0 or entered_unit<0: raise ValueError("الكمية أو التكلفة غير صحيحة.")
            factor,uom_name=product_uom_factor(c,product,request.form.get("uom_id"),"purchase")
            qty=q4(entered_qty*factor); unit=entered_unit/factor; total=money(entered_qty*entered_unit)
            stored_uom_id,entered_uom_name=product_uom_storage(c,product,request.form.get("uom_id"))
            begin_write_transaction(c)
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
            from activity_profiles import resolve_product_account_code
            invcode=resolve_product_account_code(
                c, product_row["id"], "inventory", activity_inventory_code(c)
            )
            if not invcode: raise ValueError("النشاط الحالي لا يملك حساب مخزون تلقائيًا؛ اضبط الربط أولًا.")
            inv=account(c,invcode,"asset")
            # GRNI must be explicitly configured as a detailed liability account.
            grni=ensure_detail_account(c,"2190","بضاعة مستلمة غير مفوترة GRNI","liability","2000")
            no="GRN-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            rid=insert_and_get_id(c, """INSERT INTO goods_receipts(receipt_no,po_id,supplier_id,warehouse_id,receipt_date,total_value,created_by,
              branch_id,cost_center_id,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (no,po_id,supplier,wh,request.form["receipt_date"],str(total),session["user_id"],
               dimensions["branch_id"],dimensions["cost_center_id"],dimensions["project_id"]))
            c.execute("""INSERT INTO goods_receipt_lines(receipt_id,po_line_id,product_id,quantity,unit_cost,total_cost,location_id,entered_quantity,uom_id,uom_key,entered_uom_name,conversion_factor)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (rid,po_line_id,product,str(qty),str(unit),str(total),location_id,str(entered_qty),stored_uom_id,request.form.get("uom_id"),entered_uom_name,str(factor)))
            if po_id:
                received_total=Decimal(po_line["received_qty"])+qty
                c.execute("UPDATE purchase_order_lines SET received_qty=? WHERE id=?",(str(received_total),po_line_id))
                refresh_purchase_order_receipt_status(c, po_id)
            # Stock ledger + valuation layer are updated in the same DB transaction.
            move_no="PUR-"+no
            mid=insert_and_get_id(c, """INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,target_location_id,lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status,entered_quantity,entered_uom_id,entered_uom_key,entered_uom_name)
              VALUES(?,'receipt',?,?,?,?,?,?,?,?,?,?, 'pending_document',?,?,?,?)""",
              (move_no,product,wh,location_id,lot_id,str(qty),str(unit),str(total),request.form["receipt_date"],"استلام مشتريات "+no,session["user_id"],str(entered_qty),stored_uom_id,request.form.get("uom_id"),entered_uom_name))
            c.execute("INSERT INTO stock_valuation_layers(product_id,warehouse_id,location_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost) VALUES(?,?,?,?,?,?,?,?)",
              (product,wh,location_id,lot_id,mid,str(qty),str(qty),str(unit)))
            stock_line_dimensions = {
                "stock_product_id": int(product),
                "stock_warehouse_id": int(wh),
            }
            eid=create_journal(c,request.form["receipt_date"],no,"استلام مشتريات غير مفوتر "+no,
                [(inv,total,0,"إثبات مخزون مستلم",stock_line_dimensions),
                 (grni,0,total,"بضاعة مستلمة غير مفوترة")],session["user_id"],dimensions=dimensions)
            c.execute("UPDATE goods_receipts SET journal_entry_id=? WHERE id=?",(eid,rid))
            c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?",(eid,mid))
            c.commit(); g.audit_created_record = ("goods_receipts", rid, "goods_receipt_lines", "receipt_id"); flash(f"تم الاستلام وترحيل قيد GRNI رقم {no}.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    wh=c.execute("SELECT * FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    pr=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    product_uoms={p["id"]:product_uom_options(c,p["id"]) for p in pr}
    orders=c.execute("SELECT id,po_no,supplier_id,status FROM purchase_orders WHERE status IN ('approved','partially_received') ORDER BY id DESC").fetchall()
    locations=c.execute("SELECT id,warehouse_id,code,name_ar,location_type FROM stock_locations WHERE active=1 ORDER BY warehouse_id,code").fetchall()
    dimension_options=active_dimension_options(c)
    c.close(); return render_template("purchase_receipt_new.html",suppliers=su,warehouses=wh,products=pr,product_uoms=product_uoms,orders=orders,locations=locations,dimension_options=dimension_options,today=date.today().isoformat())

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
    if total_allocated < money(subtotal):
        raise ValueError("مجموع تخصيصات إيصالات GRNI أقل من صافي فاتورة المورد.")
    if total_allocated > money(subtotal):
        # A lower supplier invoice is treated as a purchase-price discount only
        # when the selected receipts are fully settled. A partial allocation
        # remains an unbilled GRNI balance and must not be called a discount.
        for receipt_id, amount in allocations:
            available = _supplier_receipt_available_amount(c, receipt_id, exclude_bill_id)
            if amount != available:
                raise ValueError(
                    "فرق سعر المورد لا يُعتمد كخصم إلا عند تسوية كامل قيمة إيصالات GRNI المحددة."
                )
    return allocations


def _supplier_receipt_line_rows(c, receipt_ids):
    receipt_ids = list(dict.fromkeys(int(value) for value in receipt_ids))
    result = []
    # Keep each IN clause comfortably below SQLite's host-parameter limit.
    for offset in range(0, len(receipt_ids), 500):
        batch = receipt_ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        result.extend(c.execute(f"""
            SELECT gl.id AS receipt_line_id,gl.receipt_id,gl.po_line_id,gl.product_id,
                   gl.quantity AS received_quantity,gl.unit_cost AS received_unit_cost,
                   gl.total_cost AS received_total_cost,r.supplier_id,r.status AS receipt_status,
                   r.receipt_no,s.name AS supplier_name,p.sku,p.name_ar AS product_name,p.uom,
                   po.po_no,pol.unit_price AS ordered_unit_price
            FROM goods_receipt_lines gl
            JOIN goods_receipts r ON r.id=gl.receipt_id
            JOIN suppliers s ON s.id=r.supplier_id
            JOIN stock_products p ON p.id=gl.product_id
            LEFT JOIN purchase_order_lines pol ON pol.id=gl.po_line_id
            LEFT JOIN purchase_orders po ON po.id=pol.po_id
            WHERE gl.receipt_id IN ({placeholders})
            ORDER BY r.receipt_date,r.id,gl.id
        """, batch).fetchall())
    return result


def _validate_supplier_bill_inventory_lines(c, supplier_id, receipt_ids, subtotal, form,
                                            exclude_bill_id=None):
    """Validate invoice quantities/prices against selected posted receipt lines."""
    rows = _supplier_receipt_line_rows(c, receipt_ids)
    allowed = {}
    for row in rows:
        if row["supplier_id"] != supplier_id or row["receipt_status"] != "posted":
            raise ValueError("يوجد بند استلام لا يتبع المورد أو لم يتم ترحيله.")
        allowed[row["receipt_line_id"]] = row
    lines = []
    total = money("0")
    for receipt_line_id, row in allowed.items():
        raw_quantity = (form.get("invoice_quantity_" + str(receipt_line_id)) or "").strip()
        if not raw_quantity:
            continue
        quantity = Decimal(raw_quantity)
        unit_price = money(form.get("invoice_unit_price_" + str(receipt_line_id), ""))
        if not quantity.is_finite() or quantity <= 0 or not unit_price.is_finite() or unit_price < 0:
            raise ValueError("كمية وسعر بند فاتورة المورد غير صالحين.")
        already_billed_sql = """
            SELECT COALESCE(SUM(x.billed_quantity), '0')
            FROM supplier_bill_inventory_lines x
            JOIN supplier_bills b ON b.id=x.bill_id
            WHERE x.receipt_line_id=? AND b.status IN ('draft','pending_approval','approved','posted')
        """
        params = [receipt_line_id]
        if exclude_bill_id is not None:
            already_billed_sql += " AND x.bill_id<>?"
            params.append(exclude_bill_id)
        already_billed = Decimal(str(c.execute(already_billed_sql, params).fetchone()[0] or "0"))
        received = Decimal(str(row["received_quantity"]))
        if quantity > received - already_billed:
            raise ValueError(
                "كمية الفاتورة للصنف " + row["product_name"] +
                " تتجاوز الكمية المستلمة غير المفوترة في الإيصال " + row["receipt_no"] + "."
            )
        line_total = money(quantity * unit_price)
        total = money(total + line_total)
        lines.append({
            "receipt_line_id": receipt_line_id, "product_id": row["product_id"],
            "quantity": quantity, "unit_price": unit_price, "line_total": line_total,
        })
    if not lines:
        raise ValueError("أدخل كمية وسعرًا لفاتورة المورد في بند استلام واحد على الأقل.")
    if total != money(subtotal):
        raise ValueError("مجموع بنود فاتورة المورد لا يساوي صافي الفاتورة قبل الضريبة.")
    return lines


def _validate_saved_supplier_bill_inventory_lines(c, bill):
    rows = c.execute("""
        SELECT x.*,gl.quantity AS received_quantity,r.supplier_id,r.status AS receipt_status,
               b.supplier_id AS bill_supplier_id
        FROM supplier_bill_inventory_lines x
        JOIN goods_receipt_lines gl ON gl.id=x.receipt_line_id
        JOIN goods_receipts r ON r.id=gl.receipt_id
        JOIN supplier_bills b ON b.id=x.bill_id
        WHERE x.bill_id=? ORDER BY x.line_no
    """, (bill["id"],)).fetchall()
    if not rows:
        raise ValueError("فاتورة الأصناف تحتاج تفاصيل كمية وسعر لكل بند استلام قبل إرسالها للاعتماد.")
    total = money("0")
    for row in rows:
        if row["supplier_id"] != bill["supplier_id"] or row["bill_supplier_id"] != bill["supplier_id"] or row["receipt_status"] != "posted":
            raise ValueError("تفاصيل فاتورة المورد مرتبطة بإيصال غير صالح.")
        qty = Decimal(str(row["billed_quantity"]))
        total = money(total + money(row["line_total"]))
        already = Decimal(str(c.execute("""
            SELECT COALESCE(SUM(x.billed_quantity),'0')
            FROM supplier_bill_inventory_lines x JOIN supplier_bills b ON b.id=x.bill_id
            WHERE x.receipt_line_id=? AND b.status<>'cancelled' AND x.bill_id<>?
        """, (row["receipt_line_id"], bill["id"])).fetchone()[0] or "0"))
        if qty <= 0 or qty > Decimal(str(row["received_quantity"])) - already:
            raise ValueError("كمية الفاتورة تجاوزت الكمية المستلمة غير المفوترة.")
    if total != money(bill["subtotal"]):
        raise ValueError("مجموع بنود فاتورة المورد لا يساوي صافي الفاتورة.")
    return rows


def _supplier_grni_price_variance_lines(c, receipt_rows, discount_amount, matched_total):
    """Reduce inventory/COGS for a supplier discount against fully settled GRNI."""
    if discount_amount <= 0:
        return []
    from activity_profiles import resolve_product_account_code

    posting = _activity_posting_accounts(c)
    default_inventory = posting.get("purchase_inventory")
    default_cogs = posting.get("cogs")
    source_lines = []
    for receipt in receipt_rows:
        header = c.execute(
            "SELECT receipt_no,warehouse_id FROM goods_receipts WHERE id=?",
            (receipt["receipt_id"],),
        ).fetchone()
        if not header:
            raise ValueError("إيصال GRNI غير موجود أثناء تسوية فرق السعر.")
        receipt_lines = c.execute(
            "SELECT * FROM goods_receipt_lines WHERE receipt_id=? ORDER BY id",
            (receipt["receipt_id"],),
        ).fetchall()
        if not receipt_lines:
            raise ValueError("إيصال GRNI لا يحتوي على تفاصيل أصناف يمكن تسوية تكلفتها.")
        for line in receipt_lines:
            movement_rows = c.execute(
                """SELECT * FROM stock_movements
                   WHERE movement_type='receipt' AND product_id=?
                     AND target_warehouse_id=? AND notes=?
                   ORDER BY id""",
                (line["product_id"], header["warehouse_id"],
                 "استلام مشتريات " + header["receipt_no"]),
            ).fetchall()
            if len(movement_rows) != 1:
                raise ValueError(
                    "تعذر تحديد حركة استلام واحدة مؤكدة للصنف داخل إيصال GRNI؛ "
                    "لم يتم ترحيل خصم المورد حتى لا تختلف تكلفة المخزون عن الدفاتر."
                )
            movement = movement_rows[0]
            layers = c.execute(
                """SELECT id,qty_in,qty_remaining,unit_cost,value_remaining
                   FROM stock_valuation_layers WHERE source_movement_id=? ORDER BY id""",
                (movement["id"],),
            ).fetchall()
            if not layers:
                raise ValueError("لا توجد طبقة تقييم مرتبطة بإيصال GRNI؛ راجع مطابقة المخزون قبل ترحيل الخصم.")
            qty_in = sum((Decimal(str(row["qty_in"])) for row in layers), Decimal("0"))
            qty_remaining = sum((Decimal(str(row["qty_remaining"])) for row in layers), Decimal("0"))
            line_qty = Decimal(str(line["quantity"]))
            if qty_in != line_qty or qty_remaining < 0 or qty_remaining > qty_in:
                raise ValueError("كمية طبقات التقييم لا تطابق كمية إيصال GRNI؛ لم يتم ترحيل فرق السعر.")
            gross = money(line["total_cost"])
            if gross <= 0:
                raise ValueError("تكلفة سطر إيصال GRNI غير صالحة لتسوية الخصم.")
            source_lines.append({
                "line": line, "movement": movement, "layers": layers,
                "qty_in": qty_in, "qty_remaining": qty_remaining,
                "gross": gross, "warehouse_id": header["warehouse_id"],
            })

    source_total = money(sum((item["gross"] for item in source_lines), Decimal("0")))
    if source_total != money(matched_total):
        raise ValueError(
            "إجمالي تفاصيل إيصالات GRNI لا يطابق القيمة المسواة؛ راجع تفاصيل الاستلام قبل ترحيل الخصم."
        )

    remaining_discount = money(discount_amount)
    journal_lines = []
    for index, item in enumerate(source_lines):
        if index == len(source_lines) - 1:
            line_discount = remaining_discount
            remaining_discount = money("0")
        else:
            line_discount = money(discount_amount * item["gross"] / source_total)
            remaining_discount = money(remaining_discount - line_discount)
        if line_discount <= 0:
            continue
        net_line_cost = money(item["gross"] - line_discount)
        new_unit_cost = q4(net_line_cost / item["qty_in"])
        consumed_qty = item["qty_in"] - item["qty_remaining"]
        consumed_discount = money(line_discount * consumed_qty / item["qty_in"])
        remaining_value_discount = money(line_discount - consumed_discount)
        product_id = int(item["line"]["product_id"])
        warehouse_id = int(item["warehouse_id"])
        inventory_code = resolve_product_account_code(
            c, product_id, "inventory", default_inventory
        )
        cogs_code = resolve_product_account_code(
            c, product_id, "cogs", default_cogs
        )
        inventory_id = account(c, inventory_code, "asset")
        cogs_id = account(c, cogs_code, "expense")
        if remaining_value_discount > 0:
            journal_lines.append((
                inventory_id, 0, remaining_value_discount,
                "خصم سعر شراء يخفض قيمة المخزون",
                {"stock_product_id": product_id, "stock_warehouse_id": warehouse_id},
            ))
        if consumed_discount > 0:
            journal_lines.append((
                cogs_id, 0, consumed_discount,
                "خصم سعر شراء يخفض تكلفة الأصناف المصروفة",
            ))
        # Keep the authoritative remaining layer value in sync with the
        # IAS 2 discount allocation.  Distribute the remaining-stock portion
        # by remaining quantity and put any cent-rounding residual on the last
        # valuation layer so the layers reconcile exactly to the journal.
        layer_rows = item["layers"]
        remaining_layers = [
            layer for layer in layer_rows
            if Decimal(str(layer["qty_remaining"])) > 0
        ]
        layer_discount_left = remaining_value_discount
        remaining_layer_qty = sum(
            (Decimal(str(layer["qty_remaining"])) for layer in remaining_layers),
            Decimal("0"),
        )
        remaining_layer_ids = {int(layer["id"]) for layer in remaining_layers}
        for layer in layer_rows:
            layer_qty = Decimal(str(layer["qty_remaining"]))
            old_value = (
                Decimal(str(layer["value_remaining"]))
                if layer["value_remaining"] not in (None, "")
                else layer_qty * Decimal(str(layer["unit_cost"]))
            )
            if int(layer["id"]) not in remaining_layer_ids:
                layer_discount = money("0")
            elif layer["id"] == remaining_layers[-1]["id"]:
                layer_discount = layer_discount_left
                layer_discount_left = money("0")
            else:
                layer_discount = money(remaining_value_discount * layer_qty / remaining_layer_qty)
                layer_discount_left = money(layer_discount_left - layer_discount)
            new_layer_value = money(old_value - layer_discount)
            if new_layer_value < 0:
                raise ValueError("توزيع خصم المورد تجاوز قيمة إحدى طبقات المخزون.")
            c.execute(
                """UPDATE stock_valuation_layers
                   SET unit_cost=?,value_remaining=? WHERE id=?""",
                (str(new_unit_cost), str(new_layer_value), layer["id"]),
            )
        if layer_discount_left != 0:
            raise ValueError("تعذر توزيع خصم المورد بالكامل على طبقات المخزون المتبقية.")
        c.execute(
            "UPDATE stock_movements SET unit_cost=?,total_cost=? WHERE id=?",
            (str(new_unit_cost), str(net_line_cost), item["movement"]["id"]),
        )
        c.execute(
            "UPDATE goods_receipt_lines SET unit_cost=?,total_cost=? WHERE id=?",
            (str(new_unit_cost), str(net_line_cost), item["line"]["id"]),
        )
    if remaining_discount != 0:
        raise ValueError("تعذر توزيع خصم المورد بالكامل على تفاصيل إيصالات GRNI.")
    return journal_lines


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
    begin_write_transaction(c)
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
        _validate_saved_supplier_bill_inventory_lines(c, bill)
    elif bill["bill_kind"] == "expense":
        if bill_receipts:
            raise ValueError("فاتورة المصروف لا يجوز ربطها بإيصالات استلام مخزني.")
    elif bill["bill_kind"] == "asset":
        if bill_receipts:
            raise ValueError("فاتورة الأصل الثابت لا يجوز ربطها بإيصالات استلام مخزني.")
        asset_line_exists = _table_exists(c, "supplier_bill_asset_lines")
        asset_lines = c.execute(
            "SELECT * FROM supplier_bill_asset_lines WHERE bill_id=? ORDER BY line_no",
            (bill_id,),
        ).fetchall() if asset_line_exists else []
        if asset_lines:
            validated_total = money("0")
            for asset_line in asset_lines:
                validated = {
                    "asset_code": asset_line["asset_code"],
                    "asset_name": asset_line["asset_name"],
                    "asset_category": asset_line["asset_category"],
                    "asset_ready_date": asset_line["ready_date"],
                    "asset_residual_value": asset_line["residual_value"],
                    "asset_useful_life_months": asset_line["useful_life_months"],
                    "asset_depreciation_method": asset_line["depreciation_method"],
                    "asset_annual_rate": asset_line["annual_rate"],
                    "asset_account_id": asset_line["asset_account_id"],
                    "asset_accum_account_id": asset_line["accum_account_id"],
                    "asset_expense_account_id": asset_line["expense_account_id"],
                }
                validate_asset_bill_data(c, validated, bill["bill_date"], money(asset_line["cost"]))
                validated_total = money(validated_total + money(asset_line["cost"]))
            if validated_total != money(bill["subtotal"]):
                raise ValueError("مجموع تكاليف الأصول لا يساوي صافي فاتورة المورد.")
        else:
            # Backward compatibility for older single-asset bills.
            validate_asset_bill_data(c, dict(bill), bill["bill_date"], money(bill["subtotal"]))
    else:
        raise ValueError("نوع فاتورة المورد المخزن غير معروف.")
    c.execute("""UPDATE supplier_bills SET status='pending_approval',
        submitted_by=?,submitted_at=CURRENT_TIMESTAMP,approved_by=NULL,approved_at=NULL,
        rejected_by=NULL,rejected_at=NULL,rejection_reason=NULL,approval_reason=NULL
        WHERE id=?""", (actor_id, bill_id))


def approve_supplier_bill(c, bill_id, actor_id, reason=""):
    begin_write_transaction(c)
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
    begin_write_transaction(c)
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
    begin_write_transaction(c)
    bill = c.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        raise ValueError("فاتورة المورد غير موجودة.")
    if bill["journal_entry_id"] is not None or bill["status"] == "posted":
        raise ValueError("فاتورة المورد مرحّلة بالفعل؛ لا تعِد ترحيلها.")
    if bill["status"] != "approved":
        raise ValueError("لا يمكن ترحيل فاتورة المورد قبل اعتمادها.")
    dimensions = validate_journal_dimensions(c, {
        "branch_id": bill["branch_id"], "cost_center_id": bill["cost_center_id"],
        "project_id": bill["project_id"], "supplier_id": bill["supplier_id"],
    })
    ap = supplier_ap_account(c)
    subtotal = money(bill["subtotal"])
    tax = money(bill["tax_amount"])
    journal_lines = []
    deductible_percent = Decimal(str(bill["input_vat_deductible_percent"] or "100"))
    if not deductible_percent.is_finite() or not Decimal("0") <= deductible_percent <= Decimal("100"):
        raise ValueError("نسبة خصم ضريبة المدخلات يجب أن تكون من 0 إلى 100.")
    recoverable_tax = money(tax * deductible_percent / Decimal("100"))
    non_deductible_tax = money(tax - recoverable_tax)
    if bill["bill_kind"] == "inventory" and non_deductible_tax:
        raise ValueError(
            "لا يمكن ترحيل ضريبة مخزون غير قابلة للخصم حاليًا؛ يلزم أولًا توزيعها على طبقات المخزون "
            "والأصناف المصروفة حتى لا تختلف تكلفة المخزون عن الأستاذ. استخدم 100% أو أجّل الترحيل."
        )
    if bill["bill_kind"] == "inventory":
        receipt_rows = c.execute("""SELECT x.receipt_id,x.matched_amount,r.supplier_id,r.status
            FROM supplier_bill_receipts x JOIN goods_receipts r ON r.id=x.receipt_id
            WHERE x.bill_id=? ORDER BY x.receipt_id""", (bill_id,)).fetchall()
        if not receipt_rows:
            raise ValueError("لا توجد استلامات مرتبطة بفاتورة الأصناف.")
        matched_total = money("0")
        grni = account(c, "2190", "liability")
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
        if matched_total < subtotal:
            raise ValueError("مجموع تسويات إيصالات GRNI لا يطابق صافي فاتورة المورد؛ القيمة أقل من المطلوب.")
        price_discount = money(matched_total - subtotal)
        if price_discount:
            for receipt in receipt_rows:
                allocated = money(receipt["matched_amount"])
                available_before_this_bill = _supplier_receipt_available_amount(
                    c, receipt["receipt_id"], exclude_bill_id=bill_id
                )
                if allocated != available_before_this_bill:
                    raise ValueError(
                        "لا يمكن اعتبار الفرق خصم مورد إلا بعد تسوية كامل قيمة إيصالات GRNI المحددة."
                    )
        remaining_ap = subtotal
        for index, receipt in enumerate(receipt_rows):
            amount = money(receipt["matched_amount"])
            if index == len(receipt_rows) - 1:
                ap_share = remaining_ap
            else:
                ap_share = money(subtotal * amount / matched_total)
                remaining_ap = money(remaining_ap - ap_share)
            journal_lines.extend([
                (grni, amount, 0, "تسوية GRNI"),
                (ap, 0, ap_share, "إثبات المورد"),
            ])
        if price_discount:
            journal_lines.extend(
                _supplier_grni_price_variance_lines(
                    c, receipt_rows, price_discount, matched_total
                )
            )
    elif bill["bill_kind"] == "expense":
        expense_id = bill["expense_account_id"]
        expense = c.execute("SELECT account_type,is_group,active FROM accounts WHERE id=?", (expense_id,)).fetchone()
        if not expense or expense["account_type"] != "expense" or expense["is_group"] or not expense["active"]:
            raise ValueError("حساب مصروف الفاتورة غير صالح أو غير نشط.")
        journal_lines.extend([
            (expense_id, subtotal + non_deductible_tax, 0, "مصروف فاتورة المورد شامل ضريبة المدخلات غير القابلة للخصم"),
            (ap, 0, subtotal, "إثبات المورد قبل الضريبة"),
        ])
    elif bill["bill_kind"] == "asset":
        if c.execute("SELECT 1 FROM supplier_bill_receipts WHERE bill_id=? LIMIT 1", (bill_id,)).fetchone():
            raise ValueError("فاتورة الأصل الثابت لا يجوز ربطها بإيصالات استلام مخزني.")
        asset_rows = c.execute("SELECT * FROM supplier_bill_asset_lines WHERE bill_id=? ORDER BY line_no",(bill_id,)).fetchall()
        if asset_rows:
            line_total = money("0")
            remaining_non_deductible = non_deductible_tax
            for index, row in enumerate(asset_rows):
                base_cost = money(row["cost"])
                if index == len(asset_rows) - 1:
                    allocated_non_deductible = remaining_non_deductible
                else:
                    allocated_non_deductible = money(non_deductible_tax * base_cost / subtotal)
                    remaining_non_deductible = money(remaining_non_deductible - allocated_non_deductible)
                cost = money(base_cost + allocated_non_deductible)
                stored_cost = str(row["cost"]) if allocated_non_deductible == 0 else str(cost)
                line_total = money(line_total + cost)
                c.execute("UPDATE supplier_bill_asset_lines SET cost=? WHERE id=?", (stored_cost, row["id"]))
                journal_lines.append((row["asset_account_id"],cost,0,
                    "رسملة الأصل " + row["asset_code"] + " - " + row["asset_name"] +
                    (" شامل الضريبة غير القابلة للخصم" if allocated_non_deductible else "")))
            if line_total != money(subtotal + non_deductible_tax):
                raise ValueError("مجموع تكاليف الأصول بعد إضافة الضريبة غير القابلة للخصم لا يطابق صافي الفاتورة والضريبة المرسملة.")
        else:
            asset_data = validate_asset_bill_data(c, dict(bill), bill["bill_date"], subtotal)
            asset_cost = money(subtotal + non_deductible_tax)
            journal_lines.append((asset_data["asset_account_id"],asset_cost,0,
                "رسملة تكلفة الأصل " + asset_data["asset_name"] +
                (" شامل الضريبة غير القابلة للخصم" if non_deductible_tax else "")))
        journal_lines.append((ap,0,subtotal,"إثبات المورد مقابل شراء أصل ثابت"))
    else:
        raise ValueError("نوع فاتورة المورد المخزن غير معروف.")
    if recoverable_tax:
        vat_code = _activity_tax_roles(c)["input_vat"]
        vat = ensure_detail_account(c, vat_code, "ضريبة مدخلات قابلة للخصم", "asset", "1100")
        journal_lines.append((vat, recoverable_tax, 0, "ضريبة مدخلات قابلة للخصم حسب الفاتورة"))
    if tax:
        journal_lines.append((ap, 0, tax, "ضريبة ضمن إجمالي المورد"))
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
            deductible_percent = Decimal(request.form.get("input_vat_deductible_percent", "100"))
            if not deductible_percent.is_finite() or not Decimal("0") <= deductible_percent <= Decimal("100"):
                raise ValueError("نسبة خصم ضريبة المدخلات يجب أن تكون من 0 إلى 100.")
            tax=money(subtotal*rate/Decimal("100")); total=subtotal+tax
            receipts=[int(x) for x in request.form.getlist("receipt_id")]
            supplier_invoice_no=request.form["supplier_invoice_no"].strip()
            if not supplier_invoice_no:
                raise ValueError("رقم فاتورة المورد مطلوب.")
            begin_write_transaction(c)
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
                # Keep the first asset in legacy header fields for older screens,
                # while the complete asset list remains in supplier_bill_asset_lines.
                asset_data=dict(asset_lines[0])
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
                inventory_lines = _validate_supplier_bill_inventory_lines(
                    c, supplier, receipts, subtotal, request.form
                )
            else:
                inventory_lines = []
            if tax:
                ensure_detail_account(c,_activity_tax_roles(c)["input_vat"],"ضريبة مدخلات قابلة للخصم","asset","1100")
            no="BILL-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            due_date=request.form.get("due_date") or request.form["bill_date"]
            if due_date < request.form["bill_date"]: raise ValueError("تاريخ استحقاق المورد لا يمكن أن يسبق تاريخ الفاتورة.")
            bid=insert_and_get_id(c,"""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
                subtotal,tax_rate,tax_amount,total_amount,input_vat_deductible_percent,created_by,branch_id,cost_center_id,project_id,
                status,bill_kind,expense_account_id,asset_code,asset_name,asset_category,asset_ready_date,
                asset_residual_value,asset_useful_life_months,asset_depreciation_method,asset_annual_rate,
                asset_account_id,asset_accum_account_id,asset_expense_account_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (no,supplier,request.form["bill_date"],due_date,supplier_invoice_no,str(subtotal),str(rate),
                 str(tax),str(total),str(deductible_percent),session["user_id"],dimensions["branch_id"],dimensions["cost_center_id"],
                 dimensions["project_id"],bill_kind,expense_account_id,
                 asset_data.get("asset_code"),asset_data.get("asset_name"),asset_data.get("asset_category"),
                 asset_data.get("asset_ready_date"),asset_data.get("asset_residual_value","0"),
                 asset_data.get("asset_useful_life_months"),asset_data.get("asset_depreciation_method","straight_line"),
                 asset_data.get("asset_annual_rate","0"),asset_data.get("asset_account_id"),
                 asset_data.get("asset_accum_account_id"),asset_data.get("asset_expense_account_id")))
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
            for line_no, line in enumerate(inventory_lines, 1):
                c.execute("""INSERT INTO supplier_bill_inventory_lines(
                    bill_id,line_no,receipt_line_id,product_id,billed_quantity,invoice_unit_price,line_total
                ) VALUES(?,?,?,?,?,?,?)""",
                (bid,line_no,line["receipt_line_id"],line["product_id"],str(line["quantity"]),
                 str(line["unit_price"]),str(line["line_total"])))
            c.commit()
            g.audit_created_record = ("supplier_bills", bid, None, None)
            c.close()
            flash(f"تم إنشاء مسودة فاتورة المورد {no}. أرسلها للمراجعة قبل الترحيل.","success")
            return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e:
            c.rollback(); flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    expense_accounts=c.execute("SELECT id,code,name_ar FROM accounts WHERE account_type='expense' AND is_group=0 AND active=1 ORDER BY code").fetchall()
    asset_accounts=c.execute("SELECT id,code,name_ar FROM accounts WHERE account_type='asset' AND is_group=0 AND active=1 ORDER BY code").fetchall()
    receipts = _available_supplier_receipts(c, None)
    receipt_lines = _supplier_receipt_line_rows(c, [row["id"] for row in receipts])
    dimension_options=active_dimension_options(c)
    c.close(); return render_template("purchase_bill_new.html",suppliers=su,receipts=receipts,
        receipt_lines=receipt_lines, expense_accounts=expense_accounts,asset_accounts=asset_accounts,
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
            deductible_percent = Decimal(request.form.get("input_vat_deductible_percent", "100"))
            if not deductible_percent.is_finite() or not Decimal("0") <= deductible_percent <= Decimal("100"):
                raise ValueError("نسبة خصم ضريبة المدخلات يجب أن تكون من 0 إلى 100.")
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

            begin_write_transaction(c)
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
                # Preserve the first asset's details in legacy header columns.
                asset_data = dict(asset_lines[0])
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
                inventory_lines = _validate_supplier_bill_inventory_lines(
                    c, supplier, receipts, subtotal, request.form, exclude_bill_id=bill_id
                )
            else:
                inventory_lines = []
            if tax:
                ensure_detail_account(c, _activity_tax_roles(c)["input_vat"],
                                      "ضريبة مدخلات قابلة للخصم", "asset", "1100")

            c.execute("""UPDATE supplier_bills SET supplier_id=?,bill_date=?,due_date=?,
                supplier_invoice_no=?,subtotal=?,tax_rate=?,tax_amount=?,total_amount=?,input_vat_deductible_percent=?,
                branch_id=?,cost_center_id=?,project_id=?,bill_kind=?,expense_account_id=?,
                asset_code=?,asset_name=?,asset_category=?,asset_ready_date=?,asset_residual_value=?,
                asset_useful_life_months=?,asset_depreciation_method=?,asset_annual_rate=?,
                asset_account_id=?,asset_accum_account_id=?,asset_expense_account_id=?,
                status='draft',submitted_by=NULL,submitted_at=NULL,approved_by=NULL,approved_at=NULL,
                rejected_by=NULL,rejected_at=NULL,rejection_reason=NULL,approval_reason=NULL
                WHERE id=? AND status IN ('draft','rejected') AND journal_entry_id IS NULL""",
                (supplier, bill_date, due_date, supplier_invoice_no, str(subtotal), str(rate),
                 str(tax), str(total), str(deductible_percent), dimensions["branch_id"], dimensions["cost_center_id"],
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
            c.execute("DELETE FROM supplier_bill_inventory_lines WHERE bill_id=?", (bill_id,))
            c.execute("DELETE FROM supplier_bill_receipts WHERE bill_id=?", (bill_id,))
            for receipt_id, amount in allocations:
                c.execute("""INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount)
                    VALUES(?,?,?)""", (bill_id, receipt_id, str(amount)))
            for line_no, line in enumerate(inventory_lines, 1):
                c.execute("""INSERT INTO supplier_bill_inventory_lines(
                    bill_id,line_no,receipt_line_id,product_id,billed_quantity,invoice_unit_price,line_total
                ) VALUES(?,?,?,?,?,?,?)""",
                (bill_id,line_no,line["receipt_line_id"],line["product_id"],str(line["quantity"]),
                 str(line["unit_price"]),str(line["line_total"])))
            c.commit()
            flash("تم حفظ التعديلات كمسودة. يجب إرسال الفاتورة للمراجعة من جديد.", "success")
            return redirect(url_for("purchase.index"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
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
    receipt_lines = _supplier_receipt_line_rows(c, [row["id"] for row in receipts])
    existing_invoice_lines = {
        row["receipt_line_id"]: row for row in c.execute(
            "SELECT * FROM supplier_bill_inventory_lines WHERE bill_id=?", (bill_id,)
        ).fetchall()
    }
    existing_asset_lines = c.execute("SELECT * FROM supplier_bill_asset_lines WHERE bill_id=? ORDER BY line_no",(bill_id,)).fetchall()
    dimension_options = active_dimension_options(c)
    c.close()
    return render_template("purchase_bill_new.html", suppliers=suppliers, receipts=receipts,
        receipt_lines=receipt_lines, existing_invoice_lines=existing_invoice_lines,
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
        c.rollback(); flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
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
        c.rollback(); flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
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
        c.rollback(); flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
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
        c.rollback(); flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
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
      "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
      "('company_name','base_currency','tax_number','company_address','company_phone','company_email','company_website','company_logo_data')")}
    branch = None
    bill_columns = _columns(c, "supplier_bills")
    if "branch_id" in bill_columns and _table_exists(c, "branches") and bill["branch_id"]:
        branch_columns = _columns(c, "branches")
        branch_fields = [
            column if column in branch_columns else "'' AS " + column
            for column in ("address", "phone", "email", "tax_number")
        ]
        branch = c.execute(
            "SELECT code,name_ar," + ",".join(branch_fields) + " FROM branches WHERE id=?",
            (bill["branch_id"],),
        ).fetchone()
    c.close()
    return render_template("purchase_bill_print.html",bill=bill,journal_lines=journal_lines,
                           receipts=receipts,settings=settings,branch=branch)


def _supplier_bill_returnable_quantity(c, bill_id, product_id, warehouse_id):
    """Quantity invoiced on this bill for a product/warehouse, less posted prior returns."""
    billed = c.execute("""
        SELECT COALESCE(SUM(x.billed_quantity),'0') quantity
        FROM supplier_bill_inventory_lines x
        JOIN goods_receipt_lines gl ON gl.id=x.receipt_line_id
        JOIN goods_receipts gr ON gr.id=gl.receipt_id
        WHERE x.bill_id=? AND x.product_id=? AND gr.warehouse_id=?
    """, (bill_id, product_id, warehouse_id)).fetchone()
    returned = c.execute("""
        SELECT COALESCE(SUM(l.quantity),'0') quantity
        FROM supplier_credit_note_lines l
        JOIN supplier_credit_notes n ON n.id=l.credit_note_id
        WHERE n.bill_id=? AND n.status='posted'
          AND l.product_id=? AND l.warehouse_id=?
    """, (bill_id, product_id, warehouse_id)).fetchone()
    return max(
        Decimal("0"),
        Decimal(str(billed["quantity"] or "0")) -
        Decimal(str(returned["quantity"] or "0")),
    )


def _reverse_supplier_credit_journal(conn, entry_id, reversal_date, reference, description, created_by):
    original=conn.execute("SELECT status,entry_date FROM journal_entries WHERE id=?",(entry_id,)).fetchone()
    if not original or original["status"]!="posted":
        raise ValueError("لا يمكن عكس قيد إشعار مورد غير موجود أو غير مرحل.")
    try:
        reversal_day=date.fromisoformat(str(reversal_date))
        original_day=date.fromisoformat(str(original["entry_date"]))
    except (TypeError,ValueError):
        raise ValueError("تاريخ الإلغاء أو تاريخ القيد الأصلي غير صحيح.")
    if reversal_day<original_day:
        raise ValueError("تاريخ إلغاء إشعار المورد لا يمكن أن يسبق تاريخ الإشعار الأصلي.")
    dimension_keys=("branch_id","cost_center_id","project_id","customer_id","salesperson_id","supplier_id","stock_product_id","stock_warehouse_id")
    available=_columns(conn, "journal_lines")
    dimensions=[key for key in dimension_keys if key in available]
    columns=["account_id","debit","credit","memo",*dimensions]
    rows=conn.execute("SELECT "+",".join(columns)+" FROM journal_lines WHERE entry_id=? ORDER BY id",(entry_id,)).fetchall()
    if not rows:
        raise ValueError("قيد إشعار المورد لا يحتوي على تفاصيل قابلة للعكس.")
    lines=[(row["account_id"],Decimal(str(row["credit"] or "0")),
            Decimal(str(row["debit"] or "0")),"عكس: "+str(row["memo"] or ""),
            {key:row[key] for key in dimensions}) for row in rows]
    return post_journal(conn,reversal_date,reference,description,lines,created_by)


def cancel_supplier_credit_note(conn, credit_note_id, cancellation_date, reason, created_by=None):
    try:
        date.fromisoformat(str(cancellation_date))
    except (TypeError,ValueError):
        raise ValueError("تاريخ الإلغاء غير صحيح.")
    reason=str(reason or "").strip()
    if not reason or len(reason)>500:
        raise ValueError("سبب إلغاء إشعار المورد مطلوب وبحد أقصى 500 حرف.")
    note=conn.execute(
        "SELECT * FROM supplier_credit_notes WHERE id=? AND status='posted'",(credit_note_id,)
    ).fetchone()
    if not note:
        raise ValueError("إشعار المورد غير موجود أو تم إلغاؤه بالفعل.")
    if str(cancellation_date)<str(note["credit_date"]):
        raise ValueError("تاريخ إلغاء إشعار المورد لا يمكن أن يسبق تاريخ الإشعار.")
    allocations=conn.execute(
        """SELECT a.*,m.movement_no,m.movement_type,m.accounting_status,
                  m.product_id movement_product_id,m.source_warehouse_id movement_warehouse_id,
                  m.quantity movement_quantity,m.total_cost movement_total_cost,
                  v.product_id source_product_id,v.warehouse_id source_warehouse_id,
                  v.lot_id source_lot_id,v.location_id source_location_id
           FROM supplier_credit_note_layer_allocations a
           JOIN stock_movements m ON m.id=a.stock_movement_id
           JOIN stock_valuation_layers v ON v.id=a.source_layer_id
           JOIN supplier_credit_note_lines l ON l.id=a.credit_note_line_id
           WHERE l.credit_note_id=? ORDER BY a.id""",(credit_note_id,)
    ).fetchall()
    line_count=conn.execute(
        "SELECT COUNT(*) FROM supplier_credit_note_lines WHERE credit_note_id=?",(credit_note_id,)
    ).fetchone()[0]
    missing_allocations=conn.execute(
        """SELECT 1 FROM supplier_credit_note_lines l
           WHERE l.credit_note_id=? AND NOT EXISTS(
             SELECT 1 FROM supplier_credit_note_layer_allocations a WHERE a.credit_note_line_id=l.id
           ) LIMIT 1""",(credit_note_id,)
    ).fetchone()
    if not allocations or not line_count or missing_allocations:
        raise ValueError("لا يمكن إلغاء إشعار قديم لا يحتوي على روابط كاملة لطبقات المخزون الأصلية؛ يلزم فحصه يدويًا.")
    if any(row["movement_type"]!="issue" or row["accounting_status"]!="posted" for row in allocations):
        raise ValueError("حركات مخزون إشعار المورد تغيرت بالفعل؛ لا يمكن تنفيذ الإلغاء مرة أخرى.")
    for line in conn.execute(
        "SELECT * FROM supplier_credit_note_lines WHERE credit_note_id=? ORDER BY id",
        (credit_note_id,)
    ).fetchall():
        parts=[row for row in allocations if row["credit_note_line_id"]==line["id"]]
        qty_sum=sum((Decimal(str(row["quantity"])) for row in parts),Decimal("0"))
        cost_sum=sum((Decimal(str(row["total_cost"])) for row in parts),Decimal("0"))
        if qty_sum!=Decimal(str(line["quantity"])) or money(cost_sum)!=money(line["total_cost"]):
            raise ValueError("تفاصيل طبقات التكلفة لا تطابق كمية أو قيمة سطر المرتجع؛ تم إيقاف الإلغاء للحماية.")
        for part in parts:
            if (part["movement_product_id"]!=line["product_id"]
                or part["movement_warehouse_id"]!=line["warehouse_id"]
                or part["source_product_id"]!=line["product_id"]
                or part["source_warehouse_id"]!=line["warehouse_id"]
                or part["source_lot_id"]!=part["lot_id"]
                or part["source_location_id"]!=part["location_id"]
                or Decimal(str(part["movement_quantity"]))!=Decimal(str(part["quantity"]))
                or money(part["movement_total_cost"])!=money(part["total_cost"])):
                raise ValueError("مرجع حركة أو طبقة مخزون لا يطابق سطر المرتجع؛ تم إيقاف الإلغاء للحماية.")
    reversal_id=_reverse_supplier_credit_journal(
        conn,note["journal_entry_id"],cancellation_date,
        "CANCEL-"+note["credit_no"],"إلغاء إشعار دائن مشتريات "+note["supplier_credit_no"],created_by
    )
    for allocation in allocations:
        movement_no="SUPCANCEL-"+datetime.now().strftime("%Y%m%d%H%M%S%f")+"-"+uuid.uuid4().hex[:5].upper()
        movement_id=insert_and_get_id(conn,
            """INSERT INTO stock_movements(
              movement_no,movement_type,product_id,target_warehouse_id,target_location_id,lot_id,
              quantity,unit_cost,total_cost,movement_date,notes,created_by,journal_entry_id,accounting_status)
              SELECT ?, 'receipt',cnl.product_id,cnl.warehouse_id,?, ?,?,?,?,?,?,?,?,'posted'
              FROM supplier_credit_note_lines cnl
              JOIN supplier_credit_notes n ON n.id=cnl.credit_note_id
              JOIN stock_products l ON l.id=cnl.product_id
              WHERE cnl.id=?""",
            (movement_no,allocation["location_id"],allocation["lot_id"],allocation["quantity"],
             allocation["unit_cost"],allocation["total_cost"],cancellation_date,
             "إعادة مخزون عند إلغاء مرتجع المورد "+note["credit_no"],created_by,reversal_id,
             allocation["credit_note_line_id"]))
        conn.execute(
            """INSERT INTO stock_valuation_layers(
              product_id,warehouse_id,location_id,lot_id,source_movement_id,
              qty_in,qty_remaining,unit_cost,value_remaining)
              SELECT cnl.product_id,cnl.warehouse_id,?,?,?, ?,?,?,?
              FROM supplier_credit_note_lines cnl
              JOIN stock_products l ON l.id=cnl.product_id
              WHERE cnl.id=?""",
            (allocation["location_id"],allocation["lot_id"],movement_id,
             allocation["quantity"],allocation["quantity"],allocation["unit_cost"],
             allocation["total_cost"],allocation["credit_note_line_id"])
        )
        # Keep the original issue immutable: the new receipt is its stock reversal.
    credited=Decimal(str(note["total_amount"] or "0"))
    bill=conn.execute("SELECT credited_amount FROM supplier_bills WHERE id=?",(note["bill_id"],)).fetchone()
    new_credited=Decimal(str(bill["credited_amount"] or "0"))-credited
    if new_credited<0:
        raise ValueError("رصيد الإشعارات الدائنة على فاتورة المورد غير متسق؛ راجع البيانات قبل الإلغاء.")
    conn.execute("UPDATE supplier_bills SET credited_amount=? WHERE id=?",(str(money(new_credited)),note["bill_id"]))
    conn.execute(
        """UPDATE supplier_credit_notes SET status='cancelled',cancel_journal_entry_id=?,
           cancelled_by=?,cancelled_at=CURRENT_TIMESTAMP,cancellation_reason=? WHERE id=?""",
        (reversal_id,created_by,reason,credit_note_id)
    )
    return {"reversal_journal_id":reversal_id}


@purchase_bp.route("/credit-notes/<int:credit_note_id>/cancel",methods=["POST"])
@require("purchases.manage")
def cancel_credit_note(credit_note_id):
    if "permissions" in session and "stock.manage" not in set(session["permissions"]):
        flash("يلزم امتلاك صلاحية إدارة المشتريات وإدارة المخزون لإلغاء مرتجع المورد.","error")
        return redirect(url_for("purchase.index"))
    c=db()
    try:
        begin_write_transaction(c)
        result=cancel_supplier_credit_note(
            c,credit_note_id,request.form.get("cancellation_date") or date.today().isoformat(),
            request.form.get("reason",""),session["user_id"]
        )
        c.commit()
        flash("تم إلغاء إشعار المورد وإعادة المخزون بالقيد العكسي رقم "+str(result["reversal_journal_id"]),"success")
    except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as exc:
        c.rollback()
        flash(safe_error_message(exc,"تعذر إلغاء إشعار المورد. لم يتم حفظ أي تغييرات."),"error")
    finally:
        c.close()
    return redirect(url_for("purchase.index"))


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
            uom_keys=request.form.getlist("uom_key")
            if not product_ids or not (len(product_ids)==len(warehouse_ids)==len(quantities)==len(uom_keys)):
                raise ValueError("أضف صنفًا واحدًا على الأقل وحدد المخزن والكمية لكل صنف.")
            begin_write_transaction(c)
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
            ap_id=supplier_ap_account(c)
            tax_rate=Decimal(bill["tax_rate"] or "0")
            method_row=c.execute("SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
            method=method_row["setting_value"] if method_row else "average"
            line_data=[]; movement_ids=[]; subtotal=Decimal("0")
            remaining_returnable={}
            for raw_pid,raw_wh,raw_qty,raw_uom_key in zip(product_ids,warehouse_ids,quantities,uom_keys):
                product_id=int(raw_pid); warehouse_id=int(raw_wh)
                entered_qty=Decimal(raw_qty)
                conversion_factor,entered_uom_name=product_uom_factor(c,product_id,raw_uom_key or None,"purchase")
                uom_id,stored_uom_name=product_uom_storage(c,product_id,raw_uom_key or None)
                qty=q4(entered_qty*conversion_factor)
                if qty<=0: raise ValueError("كمية المرتجع يجب أن تكون أكبر من صفر.")
                return_key=(product_id,warehouse_id)
                if return_key not in remaining_returnable:
                    remaining_returnable[return_key]=_supplier_bill_returnable_quantity(
                        c,bill_id,product_id,warehouse_id
                    )
                if qty>remaining_returnable[return_key]:
                    raise ValueError(
                        "كمية مرتجع الصنف تتجاوز الكمية المفوترة المتبقية من الفاتورة الأصلية. "
                        f"المتاح للمرتجع: {remaining_returnable[return_key]}."
                    )
                remaining_returnable[return_key]-=qty
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
                cost,allocations,layer_details=consume_layers(
                c,product_id,warehouse_id,qty,method,include_layer_details=True
            )
                subtotal+=cost
                line_item={"product_id":product_id,"warehouse_id":warehouse_id,"quantity":qty,
                    "cost":cost,"entered_quantity":entered_qty,"uom_id":uom_id,"uom_key":raw_uom_key,
                    "entered_uom_name":entered_uom_name or stored_uom_name,
                    "conversion_factor":conversion_factor,"layer_details":layer_details,
                    "layer_allocations":[]}
                line_data.append(line_item)
                for allocation_index,(lot_id,allocated_qty,unit_cost,allocated_total) in enumerate(allocations):
                    layer_detail=layer_details[allocation_index]
                    movement_no="SUPRET-"+datetime.now().strftime("%Y%m%d%H%M%S%f")+"-"+uuid.uuid4().hex[:5].upper()
                    movement_id=insert_and_get_id(c,"""INSERT INTO stock_movements(
                      movement_no,movement_type,product_id,source_warehouse_id,lot_id,quantity,unit_cost,total_cost,
                      movement_date,notes,created_by,issue_purpose,accounting_status,
                      entered_quantity,entered_uom_id,entered_uom_key,entered_uom_name)
                      VALUES(?,'issue',?,?,?,?,?,?,?,?,?,'supplier_return','pending_document',?,?,?,?)""",
                      (movement_no,product_id,warehouse_id,lot_id,str(allocated_qty),str(unit_cost),str(allocated_total),
                       request.form["credit_date"],"مرتجع مورد — "+supplier_credit_no,session["user_id"],
                       str(allocated_qty/conversion_factor),uom_id,raw_uom_key,entered_uom_name or stored_uom_name))
                    movement_ids.append(movement_id)
                    line_item["layer_allocations"].append({
                        "movement_id":movement_id,"source_layer_id":layer_detail["layer_id"],
                        "lot_id":layer_detail["lot_id"],"location_id":layer_detail["location_id"],
                        "quantity":layer_detail["quantity"],"unit_cost":layer_detail["unit_cost"],
                        "total_cost":layer_detail["total_cost"],
                    })
            subtotal=money(subtotal)
            if subtotal<=0: raise ValueError("قيمة المرتجع يجب أن تكون أكبر من صفر.")
            tax=money(subtotal*tax_rate/Decimal("100"))
            total=subtotal+tax
            already_credited=Decimal(bill["credited_amount"] or "0")
            bill_remaining=Decimal(bill["total_amount"])-already_credited
            if total>bill_remaining:
                raise ValueError("قيمة إشعار الدائن تتجاوز المتبقي من قيمة فاتورة المورد بعد الإشعارات السابقة.")
            credit_no="SCN-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            note_id=insert_and_get_id(c,"""INSERT INTO supplier_credit_notes(
              credit_no,supplier_id,bill_id,credit_date,supplier_credit_no,reason,subtotal,tax_rate,tax_amount,total_amount,created_by)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (credit_no,supplier,bill_id,request.form["credit_date"],supplier_credit_no,reason,
               str(subtotal),str(tax_rate),str(tax),str(total),session["user_id"]))
            for item in line_data:
                line_cur=c.execute("""INSERT INTO supplier_credit_note_lines(
                  credit_note_id,product_id,warehouse_id,quantity,unit_cost,total_cost,
                  entered_quantity,uom_id,uom_key,entered_uom_name,conversion_factor)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (note_id,item["product_id"],item["warehouse_id"],str(item["quantity"]),
                   str(money(item["cost"]/item["quantity"])),str(item["cost"]),
                   str(item["entered_quantity"]),item["uom_id"],item["uom_key"],
                   item["entered_uom_name"],str(item["conversion_factor"])))
                credit_line_id=line_cur.lastrowid
                for allocation in item["layer_allocations"]:
                    c.execute("""INSERT INTO supplier_credit_note_layer_allocations(
                      credit_note_line_id,stock_movement_id,source_layer_id,lot_id,location_id,
                      quantity,unit_cost,total_cost) VALUES(?,?,?,?,?,?,?,?)""",
                      (credit_line_id,allocation["movement_id"],allocation["source_layer_id"],
                       allocation["lot_id"],allocation["location_id"],str(allocation["quantity"]),
                       str(allocation["unit_cost"]),str(allocation["total_cost"])))
            journal_lines=[(ap_id,total,0,"تخفيض مديونية المورد بإشعار دائن")]
            from activity_profiles import resolve_product_account_code
            return_inventory_groups={}
            for item in line_data:
                item_inventory_code=resolve_product_account_code(
                    c,item["product_id"],"inventory",inventory_code
                )
                item_key=(item_inventory_code,int(item["product_id"]),int(item["warehouse_id"]))
                return_inventory_groups[item_key]=return_inventory_groups.get(item_key,Decimal("0"))+item["cost"]
            for (item_inventory_code,product_id,warehouse_id),item_cost in sorted(return_inventory_groups.items()):
                item_inventory_id=account(c,item_inventory_code,"asset")
                journal_lines.append((
                    item_inventory_id,0,item_cost,"إخراج مخزون مرتجع للمورد",
                    {"stock_product_id":product_id,"stock_warehouse_id":warehouse_id}
                ))
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
            flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    suppliers=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    bills=c.execute("""SELECT b.id,b.bill_no,b.supplier_id,b.bill_date,b.total_amount,b.credited_amount,s.name supplier_name
      FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
      WHERE b.status='posted' AND EXISTS(SELECT 1 FROM supplier_bill_receipts sb WHERE sb.bill_id=b.id)
      ORDER BY b.bill_date DESC,b.id DESC""").fetchall()
    bills=[b for b in bills if Decimal(b["total_amount"])>Decimal(b["credited_amount"] or "0")]
    products=c.execute("SELECT id,sku,name_ar,uom FROM stock_products WHERE active=1 AND product_type!='service' ORDER BY name_ar").fetchall()
    products=[dict(p,uom_options=([u for u in product_uom_options(c,p["id"]) if u.get("purchase_enabled",1)] or [{"id":"","name_ar":p["uom"],"is_base":1}])) for p in products]
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
            ap=supplier_ap_account(c); begin_write_transaction(c)
            dimensions = validate_journal_dimensions(c, {"supplier_id": supplier})
            no="PAY-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            eid=create_journal(c,request.form["payment_date"],no,"سداد مورد",[(ap,amount,0,"تخفيض مديونية المورد"),(cash,0,amount,"سداد نقدي/بنكي")],session["user_id"],dimensions=dimensions)
            payment_id=insert_and_get_id(c,"INSERT INTO supplier_payments(payment_no,supplier_id,payment_date,amount,cash_account_id,journal_entry_id,reference,created_by) VALUES(?,?,?,?,?,?,?,?)",
              (no,supplier,request.form["payment_date"],str(amount),cash,eid,request.form.get("reference",""),session["user_id"]))
            remaining=amount
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
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    cash=c.execute("""SELECT * FROM accounts WHERE active=1 AND is_group=0 AND account_type='asset'
      AND (lower(name_ar) LIKE '%صندوق%' OR lower(name_ar) LIKE '%بنك%' OR lower(name_ar) LIKE '%نقد%' OR lower(name_ar) LIKE '%نقاط البيع%'
           OR code IN ('1000','1110','1120')) ORDER BY code""").fetchall()
    c.close(); return render_template("purchase_payment_new.html",suppliers=su,cash_accounts=cash,today=date.today().isoformat())


@purchase_bp.route("/payments/<int:payment_id>/print")
@require("purchases.view")
def payment_print(payment_id):
    c = db()
    payment = c.execute("""SELECT p.*,s.code supplier_code,s.name supplier_name,
        a.code cash_code,a.name_ar cash_name FROM supplier_payments p
        JOIN suppliers s ON s.id=p.supplier_id
        JOIN accounts a ON a.id=p.cash_account_id WHERE p.id=?""", (payment_id,)).fetchone()
    if not payment:
        c.close()
        from flask import abort
        abort(404)
    allocations = c.execute("""SELECT b.bill_no,b.bill_date,a.amount
        FROM supplier_payment_allocations a JOIN supplier_bills b ON b.id=a.bill_id
        WHERE a.payment_id=? ORDER BY b.bill_date,b.id""", (payment_id,)).fetchall()
    settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','base_currency','tax_number','company_address','company_phone','company_email','company_website','company_logo_data')")}
    c.close()
    return render_template("supplier_payment_print.html", payment=payment, allocations=allocations,
                           settings=settings, branch=None)


