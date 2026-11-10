"""NEXUS ERP sales v1: customer invoices, stock issue, GL posting and receipts.

Sales invoices are posted atomically with stock valuation and the shared journal engine.
Quotations, separate delivery orders, returns and credit notes remain future work.
"""
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal
from stock import consume_layers, D, money

sales_bp = Blueprint("sales", __name__, url_prefix="/sales")
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
CENT = Decimal("0.01")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def init_sales_schema():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS customers(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      tax_number TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
      email TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
      credit_limit TEXT NOT NULL DEFAULT '0',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS sales_invoices(
      id INTEGER PRIMARY KEY, invoice_no TEXT NOT NULL UNIQUE,
      customer_id INTEGER NOT NULL REFERENCES customers(id),
      invoice_date TEXT NOT NULL, warehouse_id INTEGER REFERENCES stock_warehouses(id),
      subtotal TEXT NOT NULL, tax_rate TEXT NOT NULL, tax_amount TEXT NOT NULL,
      total_amount TEXT NOT NULL, paid_amount TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'posted', journal_entry_id INTEGER UNIQUE,
      cogs_journal_entry_id INTEGER UNIQUE, created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS sales_invoice_lines(
      id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id) ON DELETE CASCADE,
      product_id INTEGER REFERENCES stock_products(id), description TEXT NOT NULL,
      quantity TEXT NOT NULL, unit_price TEXT NOT NULL, line_total TEXT NOT NULL,
      cost_total TEXT NOT NULL DEFAULT '0', stock_movement_id INTEGER REFERENCES stock_movements(id)
    );
    CREATE TABLE IF NOT EXISTS customer_receipts(
      id INTEGER PRIMARY KEY, receipt_no TEXT NOT NULL UNIQUE,
      invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id),
      receipt_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER UNIQUE, reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_sales_invoice_customer ON sales_invoices(customer_id,invoice_date);
    CREATE INDEX IF NOT EXISTS idx_sales_receipts_invoice ON customer_receipts(invoice_id,receipt_date);
    """)
    customer_columns = {row["name"] for row in c.execute("PRAGMA table_info(customers)")}
    if "credit_limit" not in customer_columns:
        c.execute("ALTER TABLE customers ADD COLUMN credit_limit TEXT NOT NULL DEFAULT '0'")
    c.close()


def require(permission):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            allowed = {
                "sales.manage": {"admin", "accountant"},
                "sales.view": {"admin", "accountant", "viewer"},
            }
            if session.get("role") not in allowed.get(permission, set()):
                flash("لا تملك الصلاحية المطلوبة.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco


def _account(conn, code, expected_type):
    row = conn.execute(
        "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (code,)
    ).fetchone()
    if not row or row["is_group"] or not row["active"] or row["account_type"] != expected_type:
        raise ValueError("حساب الربط " + code + " غير موجود أو غير نشط أو غير تفصيلي.")
    return row["id"]


def _activity(conn):
    row = conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
    return row["setting_value"] if row else "commercial"


def _sales_account_codes(activity):
    mapping = {
        "commercial": {"ar": "1130", "revenue": "4100", "vat": "2130", "inventory": "1150", "cogs": "5100"},
        "service": {"ar": "1120", "revenue": "4100", "vat": "2130", "inventory": None, "cogs": None},
        "medical": {"ar": "1130", "revenue": "4100", "vat": "2120", "inventory": "1150", "cogs": "5300"},
        "restaurant": {"ar": "1130", "revenue": "4100", "vat": "2120", "inventory": "1140", "cogs": "5100"},
        "industrial": {"ar": "1120", "revenue": "4100", "vat": "2120", "inventory": "1150", "cogs": "5400"},
    }
    if activity not in mapping:
        raise ValueError("نشاط الشركة غير مدعوم في دورة المبيعات.")
    return mapping[activity]


def post_invoice(conn, customer_id, invoice_date, warehouse_id, lines, tax_rate, created_by=None):
    """Create invoice, stock issues and balanced GL entries inside caller transaction."""
    rate = Decimal(str(tax_rate)).quantize(CENT, rounding=ROUND_HALF_UP)
    if rate < 0 or rate > 100:
        raise ValueError("نسبة الضريبة يجب أن تكون بين صفر و100.")
    customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
    if not customer:
        raise ValueError("العميل غير موجود أو غير نشط.")
    if not lines:
        raise ValueError("أضف صنفًا أو خدمة واحدة على الأقل.")

    activity = _activity(conn)
    codes = _sales_account_codes(activity)
    ar = _account(conn, codes["ar"], "asset")
    revenue = _account(conn, codes["revenue"], "revenue")
    vat = _account(conn, codes["vat"], "liability") if rate else None
    inventory = _account(conn, codes["inventory"], "asset") if codes["inventory"] else None
    cogs = _account(conn, codes["cogs"], "expense") if codes["cogs"] else None
    if rate and not vat:
        raise ValueError("يجب ضبط حساب ضريبة المخرجات أولًا.")
    if activity != "service" and not warehouse_id:
        raise ValueError("حدد مخزن الصرف قبل ترحيل فاتورة البيع.")

    normalized = []
    subtotal = Decimal("0.00")
    total_cost = Decimal("0.00")
    for item in lines:
        qty = Decimal(str(item.get("quantity", "1")))
        unit_price = Decimal(str(item.get("unit_price", "0"))).quantize(CENT, rounding=ROUND_HALF_UP)
        if not qty.is_finite() or qty <= 0 or not unit_price.is_finite() or unit_price < 0:
            raise ValueError("الكمية أو سعر البيع غير صحيح.")
        product_id = item.get("product_id")
        description = str(item.get("description") or "").strip()
        if product_id:
            product = conn.execute("SELECT * FROM stock_products WHERE id=? AND active=1", (int(product_id),)).fetchone()
            if not product:
                raise ValueError("الصنف غير موجود أو غير نشط.")
            if activity == "service":
                raise ValueError("نشاط الخدمات لا يستخدم صرف المخزون من شاشة المبيعات.")
            cost, _allocations = consume_layers(
                conn, product["id"], int(warehouse_id), qty,
                "average" if activity == "service" else _inventory_method(conn)
            )
            description = description or product["name_ar"]
        else:
            if activity != "service":
                raise ValueError("اختر صنف مخزني لكل بند في فاتورة هذا النشاط.")
            cost = Decimal("0.00")
            if not description:
                raise ValueError("اكتب وصف الخدمة.")
        line_total = (qty * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        subtotal += line_total
        total_cost += cost
        normalized.append({
            "product_id": int(product_id) if product_id else None,
            "description": description, "quantity": qty, "unit_price": unit_price,
            "line_total": line_total, "cost_total": cost,
        })

    subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
    tax_amount = (subtotal * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    total_amount = subtotal + tax_amount
    if subtotal <= 0:
        raise ValueError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")
    credit_limit = Decimal(customer["credit_limit"] or "0")
    outstanding_row = conn.execute("""SELECT COALESCE(SUM(CAST(total_amount AS REAL)-CAST(paid_amount AS REAL)),0) balance
        FROM sales_invoices WHERE customer_id=? AND status='posted'""", (customer_id,)).fetchone()
    current_exposure = Decimal(str(outstanding_row["balance"] or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    credit_warning = bool(credit_limit > 0 and current_exposure + total_amount > credit_limit)

    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    invoice_no = "SINV-" + stamp
    cur = conn.execute(
        """INSERT INTO sales_invoices(invoice_no,customer_id,invoice_date,warehouse_id,subtotal,
           tax_rate,tax_amount,total_amount,created_by) VALUES(?,?,?,?,?,?,?,?,?)""",
        (invoice_no, customer_id, invoice_date, warehouse_id, str(subtotal), str(rate),
         str(tax_amount), str(total_amount), created_by)
    )
    invoice_id = cur.lastrowid
    cogs_lines = []
    for index, item in enumerate(normalized, 1):
        movement_id = None
        if item["product_id"]:
            movement_no = invoice_no + "-" + str(index)
            move = conn.execute(
                """INSERT INTO stock_movements(movement_no,movement_type,product_id,source_warehouse_id,
                   quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status,issue_purpose)
                   VALUES(?,'issue',?,?,?,?,?,?,?,?, 'pending', 'sale')""",
                (movement_no, item["product_id"], warehouse_id, str(item["quantity"]),
                 str((item["cost_total"] / item["quantity"]).quantize(CENT)), str(item["cost_total"]),
                 invoice_date, "صرف مبيعات " + invoice_no, created_by)
            )
            movement_id = move.lastrowid
        conn.execute(
            """INSERT INTO sales_invoice_lines(invoice_id,product_id,description,quantity,unit_price,
               line_total,cost_total,stock_movement_id) VALUES(?,?,?,?,?,?,?,?)""",
            (invoice_id, item["product_id"], item["description"], str(item["quantity"]),
             str(item["unit_price"]), str(item["line_total"]), str(item["cost_total"]), movement_id)
        )
        if item["product_id"] and item["cost_total"] > 0:
            cogs_lines.append(item["cost_total"])

    invoice_lines = [(ar, total_amount, 0, "ذمم العميل - " + customer["name"]),
                     (revenue, 0, subtotal, "إيراد فاتورة " + invoice_no)]
    if tax_amount:
        invoice_lines.append((vat, 0, tax_amount, "ضريبة مخرجات"))
    journal_id = post_journal(conn, invoice_date, invoice_no, "فاتورة مبيعات " + invoice_no,
                              invoice_lines, created_by)
    conn.execute("UPDATE sales_invoices SET journal_entry_id=? WHERE id=?", (journal_id, invoice_id))

    cogs_journal_id = None
    if total_cost > 0:
        cogs_journal_id = post_journal(
            conn, invoice_date, invoice_no + "-COGS", "تكلفة مبيعات " + invoice_no,
            [(cogs, total_cost, 0, "تكلفة البضاعة المباعة"),
             (inventory, 0, total_cost, "تخفيض المخزون")], created_by
        )
    for row in conn.execute("SELECT id FROM stock_movements WHERE movement_no LIKE ?", (invoice_no + "-%",)).fetchall():
        conn.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status=? WHERE id=?",
                     (cogs_journal_id, "posted" if cogs_journal_id else "not_required", row["id"]))
    return {"invoice_id": invoice_id, "invoice_no": invoice_no, "journal_entry_id": journal_id,
            "cogs_journal_entry_id": cogs_journal_id, "subtotal": subtotal,
            "tax_amount": tax_amount, "total_amount": total_amount, "total_cost": total_cost,
            "credit_warning": credit_warning, "credit_limit": credit_limit,
            "current_exposure": current_exposure, "new_exposure": current_exposure + total_amount}


def _inventory_method(conn):
    row = conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
    return row["setting_value"] if row else "average"


def post_customer_receipt(conn, invoice_id, receipt_date, amount, cash_account_id, created_by=None, reference=""):
    amount = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise ValueError("قيمة التحصيل يجب أن تكون أكبر من صفر.")
    invoice = conn.execute("SELECT * FROM sales_invoices WHERE id=? AND status='posted'", (invoice_id,)).fetchone()
    if not invoice:
        raise ValueError("فاتورة العميل غير موجودة أو غير مرحلة.")
    outstanding = Decimal(invoice["total_amount"]) - Decimal(invoice["paid_amount"])
    if amount > outstanding:
        raise ValueError("قيمة التحصيل أكبر من الرصيد المستحق على الفاتورة.")
    cash = conn.execute("SELECT id,is_group,active,account_type FROM accounts WHERE id=?", (cash_account_id,)).fetchone()
    if not cash or cash["is_group"] or not cash["active"] or cash["account_type"] != "asset":
        raise ValueError("اختر حساب صندوق أو بنك تفصيليًا ونشطًا.")
    activity = _activity(conn)
    ar = _account(conn, _sales_account_codes(activity)["ar"], "asset")
    receipt_no = "RCPT-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    journal_id = post_journal(
        conn, receipt_date, receipt_no, "تحصيل من عميل " + invoice["invoice_no"],
        [(cash_account_id, amount, 0, "تحصيل نقدي/بنكي"),
         (ar, 0, amount, "تخفيض ذمم العميل")], created_by
    )
    conn.execute(
        """INSERT INTO customer_receipts(receipt_no,invoice_id,receipt_date,amount,cash_account_id,
           journal_entry_id,reference,created_by) VALUES(?,?,?,?,?,?,?,?)""",
        (receipt_no, invoice_id, receipt_date, str(amount), cash_account_id, journal_id, reference, created_by)
    )
    conn.execute("UPDATE sales_invoices SET paid_amount=? WHERE id=?",
                 (str(Decimal(invoice["paid_amount"]) + amount), invoice_id))
    return {"receipt_no": receipt_no, "journal_entry_id": journal_id}


@sales_bp.route("/")
@require("sales.view")
def index():
    c = db()
    customers = c.execute("SELECT * FROM customers ORDER BY name").fetchall()
    invoices = c.execute("""SELECT i.*,c.name customer_name FROM sales_invoices i
                            JOIN customers c ON c.id=i.customer_id ORDER BY i.id DESC LIMIT 30""").fetchall()
    receipts = c.execute("""SELECT r.*,i.invoice_no,c.name customer_name FROM customer_receipts r
                            JOIN sales_invoices i ON i.id=r.invoice_id JOIN customers c ON c.id=i.customer_id
                            ORDER BY r.id DESC LIMIT 20""").fetchall()
    c.close()
    return render_template("sales.html", customers=customers, invoices=invoices, receipts=receipts)


@sales_bp.route("/customers", methods=["GET", "POST"])
@require("sales.manage")
def customer_list():
    c = db()
    if request.method == "POST":
        try:
            credit_limit = Decimal(request.form.get("credit_limit", "0") or "0")
            if not credit_limit.is_finite() or credit_limit < 0: raise ValueError("الحد الائتماني يجب أن يكون صفرًا أو أكبر.")
            c.execute("INSERT INTO customers(code,name,tax_number,phone,email,address,credit_limit) VALUES(?,?,?,?,?,?,?)",
                      (request.form["code"].strip(), request.form["name"].strip(),
                       request.form.get("tax_number", "").strip(), request.form.get("phone", "").strip(),
                       request.form.get("email", "").strip(), request.form.get("address", "").strip(), str(credit_limit)))
            c.commit()
            flash("تم حفظ العميل.", "success")
        except sqlite3.IntegrityError:
            c.rollback()
            flash("كود العميل مستخدم بالفعل.", "error")
        except (ValueError, InvalidOperation) as exc:
            c.rollback()
            flash(str(exc), "error")
    rows = c.execute("""SELECT cu.*,
        COALESCE((SELECT SUM(CAST(i.total_amount AS REAL)-CAST(i.paid_amount AS REAL))
                  FROM sales_invoices i WHERE i.customer_id=cu.id AND i.status='posted'),0) outstanding
        FROM customers cu ORDER BY cu.name""").fetchall()
    c.close()
    return render_template("sales_customers.html", rows=rows)


@sales_bp.route("/customers/<int:customer_id>/edit",methods=["GET","POST"])
@require("sales.manage")
def edit_customer(customer_id):
    c=db(); customer=c.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()
    if not customer:
        c.close(); flash("العميل غير موجود.","error"); return redirect(url_for("sales.customer_list"))
    used=c.execute("SELECT 1 FROM sales_invoices WHERE customer_id=? LIMIT 1",(customer_id,)).fetchone() is not None
    if request.method=="POST":
        try:
            limit=Decimal(request.form.get("credit_limit","0") or "0")
            if not limit.is_finite() or limit<0: raise ValueError("الحد الائتماني يجب أن يكون صفرًا أو أكبر.")
            c.execute("""UPDATE customers SET code=?,name=?,tax_number=?,phone=?,email=?,address=?,credit_limit=? WHERE id=?""",
              (request.form["code"].strip(),request.form["name"].strip(),request.form.get("tax_number","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("address","").strip(),str(limit),customer_id))
            c.commit(); c.close(); flash("تم تعديل بيانات العميل.","success"); return redirect(url_for("sales.customer_list"))
        except (ValueError,InvalidOperation,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,(ValueError,InvalidOperation)) else "كود العميل مستخدم بالفعل.","error")
    c.close(); return render_template("sales_customer_edit.html",customer=customer,used=used)


@sales_bp.route("/customers/<int:customer_id>/delete",methods=["POST"])
@require("sales.manage")
def delete_customer(customer_id):
    c=db(); customer=c.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()
    if not customer: flash("العميل غير موجود.","error")
    elif c.execute("SELECT 1 FROM sales_invoices WHERE customer_id=? LIMIT 1",(customer_id,)).fetchone():
        flash("لا يمكن حذف العميل لوجود فواتير أو عمليات بيع مرتبطة به. يمكنك تعديل بياناته بدل الحذف.","error")
    else:
        try: c.execute("DELETE FROM customers WHERE id=?",(customer_id,)); c.commit(); flash("تم حذف العميل.","success")
        except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف العميل لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("sales.customer_list"))


@sales_bp.route("/invoices/new", methods=["GET", "POST"])
@require("sales.manage")
def new_invoice():
    c = db()
    if request.method == "POST":
        try:
            items = []
            for pid, qty, price, desc in zip(request.form.getlist("product_id"),
                    request.form.getlist("quantity"), request.form.getlist("unit_price"),
                    request.form.getlist("description")):
                if pid or desc.strip():
                    items.append({"product_id": int(pid) if pid else None, "quantity": qty,
                                  "unit_price": price, "description": desc})
            c.execute("BEGIN IMMEDIATE")
            result = post_invoice(c, int(request.form["customer_id"]), request.form["invoice_date"],
                                  request.form.get("warehouse_id", type=int), items,
                                  request.form.get("tax_rate", "0"), session["user_id"])
            c.commit()
            flash("تم ترحيل فاتورة البيع " + result["invoice_no"], "success")
            if result.get("credit_warning"):
                flash("تنبيه ائتماني: تجاوز العميل الحد المسموح. الحد: " + str(result["credit_limit"]) +
                      " | المديونية السابقة: " + str(result["current_exposure"]) +
                      " | المديونية بعد الفاتورة: " + str(result["new_exposure"]), "warning")
            return redirect(url_for("sales.index"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
    customers = c.execute("""SELECT cu.*,
        COALESCE((SELECT SUM(CAST(i.total_amount AS REAL)-CAST(i.paid_amount AS REAL))
                  FROM sales_invoices i WHERE i.customer_id=cu.id AND i.status='posted'),0) outstanding
        FROM customers cu WHERE cu.active=1 ORDER BY cu.name""").fetchall()
    products = c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    warehouses = c.execute("SELECT * FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity','default_vat_rate')")}
    c.close()
    return render_template("sales_invoice_new.html", customers=customers, products=products,
                           warehouses=warehouses, settings=settings, today=date.today().isoformat())


@sales_bp.route("/receipts/new", methods=["GET", "POST"])
@require("sales.manage")
def new_receipt():
    c = db()
    if request.method == "POST":
        try:
            c.execute("BEGIN IMMEDIATE")
            result = post_customer_receipt(c, int(request.form["invoice_id"]), request.form["receipt_date"],
                request.form["amount"], int(request.form["cash_account_id"]), session["user_id"],
                request.form.get("reference", "").strip())
            c.commit()
            flash("تم تسجيل التحصيل " + result["receipt_no"], "success")
            return redirect(url_for("sales.index"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
    invoices = c.execute("""SELECT i.id,i.invoice_no,i.total_amount,i.paid_amount,c.name customer_name
        FROM sales_invoices i JOIN customers c ON c.id=i.customer_id
        WHERE CAST(i.total_amount AS REAL)>CAST(i.paid_amount AS REAL) ORDER BY i.invoice_date,i.id""").fetchall()
    cash_accounts = c.execute("""SELECT id,code,name_ar FROM accounts
        WHERE account_type='asset' AND is_group=0 AND active=1 ORDER BY code""").fetchall()
    c.close()
    return render_template("sales_receipt_new.html", invoices=invoices, cash_accounts=cash_accounts,
                           today=date.today().isoformat())


if __name__ == "__main__":
    init_sales_schema()
