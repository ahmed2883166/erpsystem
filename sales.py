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
    CREATE TABLE IF NOT EXISTS sales_invoice_line_lots(
      id INTEGER PRIMARY KEY, invoice_line_id INTEGER NOT NULL REFERENCES sales_invoice_lines(id),
      lot_id INTEGER REFERENCES stock_lots(id), quantity TEXT NOT NULL, cost_total TEXT NOT NULL
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
    line_columns = {row["name"] for row in c.execute("PRAGMA table_info(sales_invoice_lines)")}
    for name, definition in {
        "tax_rate": "TEXT NOT NULL DEFAULT '0'",
        "tax_treatment": "TEXT NOT NULL DEFAULT 'standard'",
        "tax_amount": "TEXT NOT NULL DEFAULT '0'",
    }.items():
        if name not in line_columns:
            c.execute(f"ALTER TABLE sales_invoice_lines ADD COLUMN {name} {definition}")
    c.execute("""CREATE TABLE IF NOT EXISTS sales_credit_notes(
      id INTEGER PRIMARY KEY, credit_note_no TEXT NOT NULL UNIQUE,
      invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id),
      credit_date TEXT NOT NULL, reason TEXT NOT NULL,
      subtotal TEXT NOT NULL, tax_amount TEXT NOT NULL, total_amount TEXT NOT NULL,
      journal_entry_id INTEGER UNIQUE, cogs_journal_entry_id INTEGER UNIQUE,
      status TEXT NOT NULL DEFAULT 'posted', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    credit_note_columns = {row["name"] for row in c.execute("PRAGMA table_info(sales_credit_notes)")}
    for name, definition in {
        "refundable_amount": "TEXT NOT NULL DEFAULT '0'",
        "cancel_journal_entry_id": "INTEGER",
        "cancel_cogs_journal_entry_id": "INTEGER",
        "cancelled_by": "INTEGER",
        "cancelled_at": "TEXT",
        "cancellation_reason": "TEXT",
    }.items():
        if name not in credit_note_columns:
            c.execute(f"ALTER TABLE sales_credit_notes ADD COLUMN {name} {definition}")
    c.execute("""CREATE TABLE IF NOT EXISTS customer_refunds(
      id INTEGER PRIMARY KEY, refund_no TEXT NOT NULL UNIQUE,
      credit_note_id INTEGER NOT NULL REFERENCES sales_credit_notes(id),
      refund_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE, reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sales_credit_note_lines(
      id INTEGER PRIMARY KEY, credit_note_id INTEGER NOT NULL REFERENCES sales_credit_notes(id),
      invoice_line_id INTEGER NOT NULL REFERENCES sales_invoice_lines(id),
      product_id INTEGER REFERENCES stock_products(id), description TEXT NOT NULL,
      quantity TEXT NOT NULL, unit_price TEXT NOT NULL, line_total TEXT NOT NULL,
      tax_rate TEXT NOT NULL, tax_treatment TEXT NOT NULL, tax_amount TEXT NOT NULL,
      cost_total TEXT NOT NULL DEFAULT '0', stock_movement_id INTEGER REFERENCES stock_movements(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sales_credit_note_line_lots(
      id INTEGER PRIMARY KEY, credit_note_line_id INTEGER NOT NULL REFERENCES sales_credit_note_lines(id),
      lot_id INTEGER REFERENCES stock_lots(id), quantity TEXT NOT NULL,
      stock_movement_id INTEGER REFERENCES stock_movements(id)
    )""")
    invoice_columns = {row["name"] for row in c.execute("PRAGMA table_info(sales_invoices)")}
    if "credited_amount" not in invoice_columns:
        c.execute("ALTER TABLE sales_invoices ADD COLUMN credited_amount TEXT NOT NULL DEFAULT '0'")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sales_credit_invoice ON sales_credit_notes(invoice_id,credit_date,id)")
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


def _allowed_vat_rates(conn):
    # The company tax setup controls the standard VAT rate. Zero is always
    # accepted for zero-rated/exempt/out-of-scope treatments.
    values = ["0"]
    try:
        configured = conn.execute("SELECT rate FROM tax_rates WHERE active=1 ORDER BY id").fetchall()
        values.extend(str(row["rate"]) for row in configured)
    except sqlite3.Error:
        pass
    if len(values) == 1:
        settings = {r["setting_key"]: r["setting_value"] for r in conn.execute(
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('tax_country','default_vat_rate')")}
        if settings.get("tax_country") == "EG":
            values.append("14")
        elif settings.get("tax_country") == "SA":
            values.append("15")
        else:
            values.append(settings.get("default_vat_rate", "0"))
    try:
        return {Decimal(str(value).strip()).quantize(CENT, rounding=ROUND_HALF_UP) for value in values}
    except (InvalidOperation, ValueError):
        raise ValueError("قائمة نسب الضريبة المعتمدة في إعدادات الشركة غير صحيحة.")


def post_invoice(conn, customer_id, invoice_date, warehouse_id, lines, tax_rate, created_by=None, allow_credit_override=False):
    """Create invoice, stock issues and balanced GL entries inside caller transaction."""
    rate = Decimal(str(tax_rate)).quantize(CENT, rounding=ROUND_HALF_UP)
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError("نسبة الضريبة الافتراضية يجب أن تكون بين صفر و100.")
    # Header rate is only a default for standard-tax lines; each line is validated
    # independently. Exempt/zero-rated invoices must not fail on an unused header rate.
    allowed_rates = _allowed_vat_rates(conn)

    customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
    if not customer:
        raise ValueError("العميل غير موجود أو غير نشط.")
    if not lines:
        raise ValueError("أضف صنفًا أو خدمة واحدة على الأقل.")

    activity = _activity(conn)
    codes = _sales_account_codes(activity)
    ar = _account(conn, codes["ar"], "asset")
    revenue = _account(conn, codes["revenue"], "revenue")
    vat = None
    inventory = _account(conn, codes["inventory"], "asset") if codes["inventory"] else None
    cogs = _account(conn, codes["cogs"], "expense") if codes["cogs"] else None
    if activity != "service" and not warehouse_id:
        raise ValueError("حدد مخزن الصرف قبل ترحيل فاتورة البيع.")

    normalized = []
    subtotal = Decimal("0.00")
    total_cost = Decimal("0.00")
    tax_amount = Decimal("0.00")
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
        treatment = str(item.get("tax_treatment") or "standard").strip().lower()
        if treatment not in {"standard", "zero_rated", "exempt", "out_of_scope"}:
            raise ValueError("المعاملة الضريبية للبند غير صحيحة.")
        line_rate = Decimal(str(item.get("tax_rate", rate if treatment == "standard" else "0"))).quantize(CENT, rounding=ROUND_HALF_UP)
        if not line_rate.is_finite() or line_rate < 0 or line_rate > 100:
            raise ValueError("نسبة ضريبة البند يجب أن تكون بين صفر و100.")
        if treatment != "standard":
            line_rate = Decimal("0.00")
        elif line_rate not in allowed_rates:
            raise ValueError("نسبة ضريبة البند غير معتمدة للدولة المحددة. راجع قائمة نسب الضريبة في إعدادات الشركة.")
        line_tax = (line_total * line_rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        subtotal += line_total
        tax_amount += line_tax
        total_cost += cost
        normalized.append({
            "product_id": int(product_id) if product_id else None,
            "description": description, "quantity": qty, "unit_price": unit_price,
            "line_total": line_total, "cost_total": cost,
            "tax_rate": line_rate, "tax_treatment": treatment, "tax_amount": line_tax,
            "stock_allocations": _allocations if product_id else [],
        })

    subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
    tax_amount = tax_amount.quantize(CENT, rounding=ROUND_HALF_UP)
    total_amount = subtotal + tax_amount
    if subtotal <= 0:
        raise ValueError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")
    if tax_amount > 0 and vat is None:
        vat = _account(conn, codes["vat"], "liability")
    credit_limit = Decimal(customer["credit_limit"] or "0")
    outstanding_rows = conn.execute(
        "SELECT total_amount,paid_amount,COALESCE(credited_amount,'0') credited_amount "
        "FROM sales_invoices WHERE customer_id=? AND status='posted'", (customer_id,)
    ).fetchall()
    current_exposure = sum((
        Decimal(str(row["total_amount"] or "0")) -
        Decimal(str(row["paid_amount"] or "0")) -
        Decimal(str(row["credited_amount"] or "0"))
        for row in outstanding_rows
    ), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP)
    credit_warning = bool(credit_limit > 0 and current_exposure + total_amount > credit_limit)
    if credit_warning and not allow_credit_override:
        raise ValueError(
            f"تم إيقاف الفاتورة لتجاوز الحد الائتماني. الحد: {credit_limit}، "
            f"المديونية الحالية: {current_exposure}، وبعد الفاتورة: {current_exposure + total_amount}. "
            "يلزم اعتماد مدير النظام لإتمامها."
        )

    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    invoice_no = "SINV-" + stamp
    effective_rate = (tax_amount * Decimal("100") / subtotal).quantize(CENT, rounding=ROUND_HALF_UP)
    cur = conn.execute(
        """INSERT INTO sales_invoices(invoice_no,customer_id,invoice_date,warehouse_id,subtotal,
           tax_rate,tax_amount,total_amount,created_by) VALUES(?,?,?,?,?,?,?,?,?)""",
        (invoice_no, customer_id, invoice_date, warehouse_id, str(subtotal), str(effective_rate),
         str(tax_amount), str(total_amount), created_by)
    )
    invoice_id = cur.lastrowid
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
        line_cursor = conn.execute(
            """INSERT INTO sales_invoice_lines(invoice_id,product_id,description,quantity,unit_price,
               line_total,cost_total,stock_movement_id,tax_rate,tax_treatment,tax_amount)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (invoice_id, item["product_id"], item["description"], str(item["quantity"]),
             str(item["unit_price"]), str(item["line_total"]), str(item["cost_total"]), movement_id,
             str(item["tax_rate"]), item["tax_treatment"], str(item["tax_amount"]))
        )
        invoice_line_id = line_cursor.lastrowid
        for lot_id, allocated_qty, allocated_unit_cost, allocated_cost in item.get("stock_allocations", []):
            conn.execute(
                "INSERT INTO sales_invoice_line_lots(invoice_line_id,lot_id,quantity,cost_total) VALUES(?,?,?,?)",
                (invoice_line_id, lot_id, str(allocated_qty), str(allocated_cost))
            )

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


def post_credit_note(conn, invoice_id, credit_date, reason, lines, created_by=None):
    """Post a sales credit note against an unpaid invoice and restore returned stock."""
    invoice = conn.execute(
        "SELECT * FROM sales_invoices WHERE id=? AND status='posted'", (invoice_id,)
    ).fetchone()
    if not invoice:
        raise ValueError("فاتورة البيع غير موجودة أو غير مرحلة.")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("سبب الإشعار الدائن مطلوب وبحد أقصى 500 حرف.")
    try:
        date.fromisoformat(credit_date)
    except (TypeError, ValueError):
        raise ValueError("تاريخ الإشعار الدائن غير صحيح.")
    if not lines:
        raise ValueError("حدد بندًا واحدًا على الأقل للإشعار الدائن.")

    credited = Decimal(invoice["credited_amount"] or "0")
    paid = Decimal(invoice["paid_amount"] or "0")
    invoice_total = Decimal(invoice["total_amount"])
    remaining_invoice = invoice_total - paid - credited
    maximum_credit = invoice_total - credited
    if maximum_credit <= 0:
        raise ValueError("تم إصدار إشعارات دائنة بكامل قيمة الفاتورة بالفعل.")

    activity = _activity(conn)
    codes = _sales_account_codes(activity)
    ar = _account(conn, codes["ar"], "asset")
    revenue = _account(conn, codes["revenue"], "revenue")
    vat_account = None
    inventory = _account(conn, codes["inventory"], "asset") if codes["inventory"] else None
    cogs = _account(conn, codes["cogs"], "expense") if codes["cogs"] else None

    normalized = []
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")
    cost_total = Decimal("0.00")
    for item in lines:
        line_id = int(item["invoice_line_id"])
        original = conn.execute(
            "SELECT * FROM sales_invoice_lines WHERE id=? AND invoice_id=?",
            (line_id, invoice_id),
        ).fetchone()
        if not original:
            raise ValueError("أحد بنود الإشعار لا ينتمي إلى فاتورة البيع المحددة.")
        qty = Decimal(str(item["quantity"]))
        sold_qty = Decimal(original["quantity"])
        if not qty.is_finite() or qty <= 0:
            raise ValueError("كمية الإشعار يجب أن تكون أكبر من صفر.")
        prior_credit_rows = conn.execute(
            """SELECT cl.quantity FROM sales_credit_note_lines cl
               JOIN sales_credit_notes cn ON cn.id=cl.credit_note_id
               WHERE cl.invoice_line_id=? AND cn.status='posted'""",
            (line_id,),
        ).fetchall()
        already_credited = sum((Decimal(row["quantity"]) for row in prior_credit_rows), Decimal("0"))
        available_qty = sold_qty - already_credited
        if qty > available_qty:
            raise ValueError(
                f"الكمية المطلوبة للإشعار أكبر من الكمية المتبقية من البند. المتاح {available_qty}."
            )
        unit_price = Decimal(original["unit_price"])
        line_value = (qty * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        tax_rate = Decimal(original["tax_rate"] or "0")
        treatment = original["tax_treatment"] or "standard"
        line_tax = (line_value * tax_rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        original_qty = sold_qty
        line_cost = (Decimal(original["cost_total"] or "0") * qty / original_qty).quantize(CENT, rounding=ROUND_HALF_UP)
        subtotal += line_value
        tax_total += line_tax
        cost_total += line_cost
        original_allocations = conn.execute(
            "SELECT lot_id,quantity,cost_total FROM sales_invoice_line_lots WHERE invoice_line_id=? ORDER BY id",
            (line_id,),
        ).fetchall()
        if not original_allocations:
            original_allocations = [{"lot_id": None, "quantity": original["quantity"],
                                     "cost_total": original["cost_total"] or "0"}]
        returned_rows = conn.execute(
            """SELECT rl.lot_id,rl.quantity FROM sales_credit_note_line_lots rl
               JOIN sales_credit_note_lines cl ON cl.id=rl.credit_note_line_id
               JOIN sales_credit_notes cn ON cn.id=cl.credit_note_id
               WHERE cl.invoice_line_id=? AND cn.status='posted'""", (line_id,)
        ).fetchall()
        returned_by_lot = {}
        for returned_row in returned_rows:
            key = returned_row["lot_id"]
            returned_by_lot[key] = returned_by_lot.get(key, Decimal("0")) + Decimal(returned_row["quantity"])
        lot_remaining = []
        for allocation in original_allocations:
            allocated_qty = Decimal(allocation["quantity"])
            available_lot_qty = allocated_qty - returned_by_lot.get(allocation["lot_id"], Decimal("0"))
            if available_lot_qty > 0:
                lot_remaining.append({
                    "lot_id": allocation["lot_id"], "available": available_lot_qty,
                    "original_qty": allocated_qty, "original_cost": Decimal(allocation["cost_total"]),
                })
        to_allocate = qty
        lot_allocations = []
        for lot in lot_remaining:
            if to_allocate <= 0:
                break
            take = min(to_allocate, lot["available"])
            lot_cost = (lot["original_cost"] * take / lot["original_qty"]).quantize(CENT, rounding=ROUND_HALF_UP)
            lot_allocations.append({"lot_id": lot["lot_id"], "quantity": take, "cost_total": lot_cost})
            to_allocate -= take
        if to_allocate > 0:
            raise ValueError("تعذر تحديد التشغيلة الأصلية للكمية المرتجعة. راجع تتبع مخزون الفاتورة.")
        if lot_allocations:
            lot_allocations[-1]["cost_total"] += line_cost - sum(
                (part["cost_total"] for part in lot_allocations), Decimal("0")
            )
        normalized.append({
            "original": original, "quantity": qty, "unit_price": unit_price,
            "line_total": line_value, "tax_rate": tax_rate,
            "tax_treatment": treatment, "tax_amount": line_tax,
            "cost_total": line_cost, "lot_allocations": lot_allocations,
        })

    subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
    tax_total = tax_total.quantize(CENT, rounding=ROUND_HALF_UP)
    total = subtotal + tax_total
    if total <= 0:
        raise ValueError("قيمة الإشعار الدائن يجب أن تكون أكبر من صفر.")
    if total > maximum_credit:
        raise ValueError(
            f"قيمة الإشعار ({total}) تتجاوز القيمة المتبقية من الفاتورة بعد الإشعارات السابقة ({maximum_credit})."
        )
    refundable_amount = max(Decimal("0"), total - max(Decimal("0"), remaining_invoice))
    if tax_total > 0:
        vat_account = _account(conn, codes["vat"], "liability")

    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    credit_no = "SCN-" + stamp
    cur = conn.execute(
        """INSERT INTO sales_credit_notes(credit_note_no,invoice_id,credit_date,reason,
           subtotal,tax_amount,total_amount,refundable_amount,created_by)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (credit_no, invoice_id, credit_date, reason, str(subtotal), str(tax_total), str(total),
         str(refundable_amount), created_by),
    )
    credit_id = cur.lastrowid
    journal_lines = [(revenue, subtotal, 0, "عكس إيراد بموجب إشعار دائن " + credit_no)]
    if tax_total:
        journal_lines.append((vat_account, tax_total, 0, "عكس ضريبة مخرجات بموجب " + credit_no))
    journal_lines.append((ar, 0, total, "تخفيض ذمم العميل بموجب " + credit_no))
    journal_id = post_journal(
        conn, credit_date, credit_no, "إشعار دائن لفاتورة " + invoice["invoice_no"],
        journal_lines, created_by,
    )
    conn.execute("UPDATE sales_credit_notes SET journal_entry_id=? WHERE id=?", (journal_id, credit_id))

    cogs_journal_id = None
    stock_index = 0
    for item in normalized:
        original = item["original"]
        movement_id = None
        product_id = original["product_id"]
        credit_line_cursor = conn.execute(
            """INSERT INTO sales_credit_note_lines(credit_note_id,invoice_line_id,product_id,
               description,quantity,unit_price,line_total,tax_rate,tax_treatment,tax_amount,
               cost_total,stock_movement_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (credit_id, original["id"], product_id, original["description"], str(item["quantity"]),
             str(item["unit_price"]), str(item["line_total"]), str(item["tax_rate"]),
             item["tax_treatment"], str(item["tax_amount"]), str(item["cost_total"])),
        )
        credit_line_id = credit_line_cursor.lastrowid
        if product_id:
            warehouse_id = invoice["warehouse_id"]
            if not warehouse_id:
                raise ValueError("فاتورة الصنف المخزني لا تحتوي على مخزن صرف صالح.")
            for lot_part in item["lot_allocations"]:
                stock_index += 1
                movement_no = credit_no + "-" + str(stock_index)
                part_qty, part_cost = lot_part["quantity"], lot_part["cost_total"]
                unit_cost = part_cost / part_qty
                movement = conn.execute(
                    """INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,
                       lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status,issue_purpose)
                       VALUES(?,'receipt',?,?,?,?,?,?,?,?,?, 'pending','sales_return')""",
                    (movement_no, product_id, warehouse_id, lot_part["lot_id"], str(part_qty), str(unit_cost),
                     str(part_cost), credit_date, "مرتجع مبيعات بموجب " + credit_no, created_by),
                )
                movement_id = movement.lastrowid
                conn.execute(
                    """INSERT INTO stock_valuation_layers(product_id,warehouse_id,lot_id,source_movement_id,
                       qty_in,qty_remaining,unit_cost,value_remaining) VALUES(?,?,?,?,?,?,?,?)""",
                    (product_id, warehouse_id, lot_part["lot_id"], movement_id, str(part_qty),
                     str(part_qty), str(unit_cost), str(part_cost)),
                )
                conn.execute(
                    """INSERT INTO sales_credit_note_line_lots(credit_note_line_id,lot_id,quantity,stock_movement_id)
                       VALUES(?,?,?,?)""",
                    (credit_line_id, lot_part["lot_id"], str(part_qty), movement_id),
                )
                if not conn.execute("SELECT stock_movement_id FROM sales_credit_note_lines WHERE id=?",
                                    (credit_line_id,)).fetchone()["stock_movement_id"]:
                    conn.execute("UPDATE sales_credit_note_lines SET stock_movement_id=? WHERE id=?",
                                 (movement_id, credit_line_id))

    if cost_total > 0:
        cogs_journal_id = post_journal(
            conn, credit_date, credit_no + "-COGS", "عكس تكلفة المبيعات " + credit_no,
            [(inventory, cost_total, 0, "إعادة تكلفة البضاعة المرتجعة للمخزون"),
             (cogs, 0, cost_total, "عكس تكلفة البضاعة المباعة")], created_by,
        )
        conn.execute(
            "UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE movement_no LIKE ?",
            (cogs_journal_id, credit_no + "-%"),
        )
    else:
        conn.execute(
            "UPDATE stock_movements SET accounting_status='not_required' WHERE movement_no LIKE ?",
            (credit_no + "-%",),
        )

    conn.execute(
        "UPDATE sales_credit_notes SET cogs_journal_entry_id=? WHERE id=?",
        (cogs_journal_id, credit_id),
    )
    conn.execute(
        "UPDATE sales_invoices SET credited_amount=? WHERE id=?",
        (str(credited + total), invoice_id),
    )
    return {
        "credit_note_id": credit_id, "credit_note_no": credit_no,
        "journal_entry_id": journal_id, "cogs_journal_entry_id": cogs_journal_id,
        "subtotal": subtotal, "tax_amount": tax_total, "total_amount": total,
        "total_cost": cost_total,
    }


def _reverse_posted_journal(conn, entry_id, reversal_date, reference, description, created_by):
    rows = conn.execute(
        "SELECT account_id,debit,credit,memo FROM journal_lines WHERE entry_id=? ORDER BY id",
        (entry_id,),
    ).fetchall()
    if not rows:
        raise ValueError("لا يمكن عكس قيد غير موجود أو بلا تفاصيل.")
    reversed_lines = [
        (row["account_id"], Decimal(str(row["credit"] or "0")),
         Decimal(str(row["debit"] or "0")), "عكس: " + str(row["memo"] or ""))
        for row in rows
    ]
    return post_journal(conn, reversal_date, reference, description, reversed_lines, created_by)


def post_customer_refund(conn, credit_note_id, refund_date, amount, cash_account_id,
                         created_by=None, reference=""):
    try:
        date.fromisoformat(refund_date)
    except (TypeError, ValueError):
        raise ValueError("تاريخ رد المبلغ غير صحيح.")
    amount = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("قيمة رد المبلغ يجب أن تكون أكبر من صفر.")
    note = conn.execute(
        "SELECT * FROM sales_credit_notes WHERE id=? AND status='posted'", (credit_note_id,)
    ).fetchone()
    if not note:
        raise ValueError("الإشعار الدائن غير موجود أو غير مرحل.")
    refunded_rows = conn.execute(
        "SELECT amount FROM customer_refunds WHERE credit_note_id=?", (credit_note_id,)
    ).fetchall()
    already_refunded = sum((Decimal(row["amount"]) for row in refunded_rows), Decimal("0"))
    refundable = Decimal(note["refundable_amount"] or "0") - already_refunded
    if amount > refundable:
        raise ValueError(f"قيمة الرد تتجاوز المبلغ القابل للاسترداد. المتاح {max(refundable, Decimal('0'))}.")
    cash = conn.execute(
        "SELECT id,is_group,active,account_type FROM accounts WHERE id=?", (cash_account_id,)
    ).fetchone()
    if not cash or cash["is_group"] or not cash["active"] or cash["account_type"] != "asset":
        raise ValueError("اختر حساب صندوق أو بنك تفصيليًا ونشطًا.")
    invoice = conn.execute("SELECT * FROM sales_invoices WHERE id=?", (note["invoice_id"],)).fetchone()
    ar = _account(conn, _sales_account_codes(_activity(conn))["ar"], "asset")
    refund_no = "SREF-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    journal_id = post_journal(
        conn, refund_date, refund_no, "رد مبلغ للعميل عن إشعار " + note["credit_note_no"],
        [(ar, amount, 0, "تسوية الرصيد الدائن للعميل"),
         (cash_account_id, 0, amount, "رد نقدي/بنكي للعميل")], created_by,
    )
    conn.execute(
        """INSERT INTO customer_refunds(refund_no,credit_note_id,refund_date,amount,cash_account_id,
           journal_entry_id,reference,created_by) VALUES(?,?,?,?,?,?,?,?)""",
        (refund_no, credit_note_id, refund_date, str(amount), cash_account_id, journal_id,
         reference, created_by),
    )
    return {"refund_no": refund_no, "journal_entry_id": journal_id, "amount": amount,
            "invoice_id": invoice["id"]}


def cancel_credit_note(conn, credit_note_id, cancellation_date, reason, created_by=None):
    try:
        date.fromisoformat(cancellation_date)
    except (TypeError, ValueError):
        raise ValueError("تاريخ الإلغاء غير صحيح.")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("سبب إلغاء الإشعار مطلوب وبحد أقصى 500 حرف.")
    note = conn.execute(
        "SELECT * FROM sales_credit_notes WHERE id=? AND status='posted'", (credit_note_id,)
    ).fetchone()
    if not note:
        raise ValueError("الإشعار الدائن غير موجود أو تم إلغاؤه بالفعل.")
    if conn.execute("SELECT 1 FROM customer_refunds WHERE credit_note_id=? LIMIT 1",
                    (credit_note_id,)).fetchone():
        raise ValueError("لا يمكن إلغاء الإشعار بعد رد مبالغ منه. اعكس عملية الرد أولًا.")
    movements = conn.execute(
        """SELECT DISTINCT m.id,m.movement_no,l.id layer_id,l.qty_in,l.qty_remaining,
                  l.value_remaining
           FROM sales_credit_note_line_lots x
           JOIN stock_movements m ON m.id=x.stock_movement_id
           JOIN stock_valuation_layers l ON l.source_movement_id=m.id
           WHERE x.credit_note_line_id IN
             (SELECT id FROM sales_credit_note_lines WHERE credit_note_id=?)""",
        (credit_note_id,),
    ).fetchall()
    for movement in movements:
        if Decimal(movement["qty_remaining"]) != Decimal(movement["qty_in"]):
            raise ValueError(
                "لا يمكن إلغاء الإشعار لأن كمية المرتجع من التشغيلة استُخدمت أو نُقلت بعد إعادتها للمخزون."
            )
    reversal_id = _reverse_posted_journal(
        conn, note["journal_entry_id"], cancellation_date,
        "CANCEL-" + note["credit_note_no"], "إلغاء إشعار دائن " + note["credit_note_no"], created_by,
    )
    cogs_reversal_id = None
    if note["cogs_journal_entry_id"]:
        cogs_reversal_id = _reverse_posted_journal(
            conn, note["cogs_journal_entry_id"], cancellation_date,
            "CANCEL-" + note["credit_note_no"] + "-COGS",
            "إلغاء عكس تكلفة المبيعات " + note["credit_note_no"], created_by,
        )
    for movement in movements:
        conn.execute("UPDATE stock_valuation_layers SET qty_remaining='0',value_remaining='0' WHERE id=?",
                     (movement["layer_id"],))
        conn.execute("UPDATE stock_movements SET movement_type='cancelled_receipt',accounting_status='reversed' WHERE id=?",
                     (movement["id"],))
    # Backward compatibility for older credit notes that did not store lot allocation rows.
    legacy = conn.execute(
        """SELECT stock_movement_id FROM sales_credit_note_lines
           WHERE credit_note_id=? AND stock_movement_id IS NOT NULL""", (credit_note_id,)
    ).fetchall()
    for row in legacy:
        if not conn.execute("SELECT 1 FROM sales_credit_note_line_lots WHERE stock_movement_id=?",
                            (row["stock_movement_id"],)).fetchone():
            layer = conn.execute(
                "SELECT id,qty_in,qty_remaining FROM stock_valuation_layers WHERE source_movement_id=?",
                (row["stock_movement_id"],),
            ).fetchone()
            if layer:
                if Decimal(layer["qty_remaining"]) != Decimal(layer["qty_in"]):
                    raise ValueError("لا يمكن إلغاء الإشعار لأن مخزون المرتجع القديم تم استخدامه.")
                conn.execute("UPDATE stock_valuation_layers SET qty_remaining='0',value_remaining='0' WHERE id=?",
                             (layer["id"],))
            conn.execute("UPDATE stock_movements SET movement_type='cancelled_receipt',accounting_status='reversed' WHERE id=?",
                         (row["stock_movement_id"],))
    conn.execute(
        """UPDATE sales_credit_notes SET status='cancelled',cancel_journal_entry_id=?,
           cancel_cogs_journal_entry_id=?,cancelled_by=?,cancelled_at=CURRENT_TIMESTAMP,
           cancellation_reason=? WHERE id=?""",
        (reversal_id, cogs_reversal_id, created_by, reason, credit_note_id),
    )
    invoice = conn.execute("SELECT credited_amount FROM sales_invoices WHERE id=?",
                           (note["invoice_id"],)).fetchone()
    new_credited = Decimal(invoice["credited_amount"] or "0") - Decimal(note["total_amount"])
    if new_credited < 0:
        raise ValueError("رصيد الإشعارات الدائنة على الفاتورة غير متسق؛ راجع سلامة البيانات.")
    conn.execute("UPDATE sales_invoices SET credited_amount=? WHERE id=?",
                 (str(new_credited), note["invoice_id"]))
    return {"reversal_journal_id": reversal_id, "cogs_reversal_journal_id": cogs_reversal_id}


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
    outstanding = Decimal(invoice["total_amount"]) - Decimal(invoice["paid_amount"]) - Decimal(invoice["credited_amount"] or "0")
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
    enriched_invoices = []
    for invoice in invoices:
        item = dict(invoice)
        item["remaining_amount"] = Decimal(invoice["total_amount"]) - Decimal(invoice["paid_amount"] or "0") - Decimal(invoice["credited_amount"] or "0")
        item["closed_by_credit"] = item["remaining_amount"] <= 0 and Decimal(invoice["credited_amount"] or "0") > 0 and Decimal(invoice["paid_amount"] or "0") < Decimal(invoice["total_amount"])
        enriched_invoices.append(item)
    receipts = c.execute("""SELECT r.*,i.invoice_no,c.name customer_name FROM customer_receipts r
                            JOIN sales_invoices i ON i.id=r.invoice_id JOIN customers c ON c.id=i.customer_id
                            ORDER BY r.id DESC LIMIT 20""").fetchall()
    credit_notes = c.execute("""SELECT n.*,i.invoice_no,c.name customer_name
                                FROM sales_credit_notes n JOIN sales_invoices i ON i.id=n.invoice_id
                                JOIN customers c ON c.id=i.customer_id
                                ORDER BY n.id DESC LIMIT 20""").fetchall()
    enriched_notes = []
    for note in credit_notes:
        refunds = c.execute("SELECT amount FROM customer_refunds WHERE credit_note_id=?", (note["id"],)).fetchall()
        refunded = sum((Decimal(row["amount"]) for row in refunds), Decimal("0"))
        item = dict(note)
        item["refunded_amount"] = refunded
        item["refundable_remaining"] = max(Decimal("0"), Decimal(note["refundable_amount"] or "0") - refunded)
        enriched_notes.append(item)
    c.close()
    return render_template("sales.html", customers=customers, invoices=enriched_invoices,
                           receipts=receipts, credit_notes=enriched_notes, today=date.today().isoformat())


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
    rows = c.execute("SELECT * FROM customers ORDER BY name").fetchall()
    invoices_by_customer = {}
    for invoice_row in c.execute(
        "SELECT customer_id,total_amount,paid_amount,COALESCE(credited_amount,'0') credited_amount "
        "FROM sales_invoices WHERE status='posted'"
    ).fetchall():
        balance = Decimal(invoice_row["total_amount"]) - Decimal(invoice_row["paid_amount"] or "0") - Decimal(invoice_row["credited_amount"] or "0")
        invoices_by_customer[invoice_row["customer_id"]] = invoices_by_customer.get(invoice_row["customer_id"], Decimal("0")) + balance
    rows = [dict(row, outstanding=invoices_by_customer.get(row["id"], Decimal("0"))) for row in rows]
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
            for pid, qty, price, desc, line_rate, treatment in zip(request.form.getlist("product_id"),
                    request.form.getlist("quantity"), request.form.getlist("unit_price"),
                    request.form.getlist("description"), request.form.getlist("line_tax_rate"),
                    request.form.getlist("line_tax_treatment")):
                if pid or desc.strip():
                    selected_price = price
                    if pid:
                        rule = c.execute("""SELECT unit_price FROM customer_price_rules
                          WHERE customer_id=? AND product_id=? AND active=1 AND valid_from<=?
                            AND (valid_to IS NULL OR valid_to>=?) AND CAST(min_qty AS REAL)<=CAST(? AS REAL)
                          ORDER BY valid_from DESC,id DESC LIMIT 1""",
                          (int(request.form["customer_id"]), int(pid), request.form["invoice_date"],
                           request.form["invoice_date"], qty)).fetchone()
                        if rule:
                            selected_price = rule["unit_price"]
                    items.append({"product_id": int(pid) if pid else None, "quantity": qty,
                                  "unit_price": selected_price, "description": desc, "tax_rate": line_rate, "tax_treatment": treatment})
            c.execute("BEGIN IMMEDIATE")
            result = post_invoice(c, int(request.form["customer_id"]), request.form["invoice_date"],
                                  request.form.get("warehouse_id", type=int), items,
                                  request.form.get("tax_rate", "0"), session["user_id"],
                                  allow_credit_override=(session.get("role") == "admin" and request.form.get("credit_override") == "1"))
            due_date = request.form.get("due_date") or request.form["invoice_date"]
            if due_date < request.form["invoice_date"]:
                raise ValueError("تاريخ الاستحقاق لا يمكن أن يسبق تاريخ الفاتورة.")
            c.execute("UPDATE sales_invoices SET due_date=? WHERE id=?", (due_date, result["invoice_id"]))
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
    customers = c.execute("SELECT * FROM customers WHERE active=1 ORDER BY name").fetchall()
    exposure_by_customer = {}
    for invoice_row in c.execute(
        "SELECT customer_id,total_amount,paid_amount,COALESCE(credited_amount,'0') credited_amount "
        "FROM sales_invoices WHERE status='posted'"
    ).fetchall():
        balance = Decimal(invoice_row["total_amount"]) - Decimal(invoice_row["paid_amount"] or "0") - Decimal(invoice_row["credited_amount"] or "0")
        exposure_by_customer[invoice_row["customer_id"]] = exposure_by_customer.get(invoice_row["customer_id"], Decimal("0")) + balance
    customers = [dict(row, outstanding=exposure_by_customer.get(row["id"], Decimal("0"))) for row in customers]
    products = c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    warehouses = c.execute("SELECT * FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity','default_vat_rate')")}
    c.close()
    return render_template("sales_invoice_new.html", customers=customers, products=products,
                           warehouses=warehouses, settings=settings, today=date.today().isoformat())


@sales_bp.route("/credit-notes/new", methods=["GET", "POST"])
@require("sales.manage")
def new_credit_note():
    c = db()
    invoice_id = request.values.get("invoice_id", type=int)
    invoice = None
    lines = []
    if invoice_id:
        invoice = c.execute(
            """SELECT i.*,cu.name customer_name FROM sales_invoices i
               JOIN customers cu ON cu.id=i.customer_id WHERE i.id=? AND i.status='posted'""",
            (invoice_id,),
        ).fetchone()
        if invoice:
            lines = c.execute(
                "SELECT * FROM sales_invoice_lines WHERE invoice_id=? ORDER BY id",
                (invoice_id,),
            ).fetchall()
            prior_by_line = {}
            for prior in c.execute(
                """SELECT cl.invoice_line_id,cl.quantity FROM sales_credit_note_lines cl
                   JOIN sales_credit_notes cn ON cn.id=cl.credit_note_id
                   JOIN sales_invoice_lines il ON il.id=cl.invoice_line_id
                   WHERE il.invoice_id=? AND cn.status='posted'""",
                (invoice_id,),
            ).fetchall():
                prior_by_line[prior["invoice_line_id"]] = (
                    prior_by_line.get(prior["invoice_line_id"], Decimal("0"))
                    + Decimal(prior["quantity"])
                )
            enriched_lines = []
            for line in lines:
                item = dict(line)
                item["credited_qty"] = str(prior_by_line.get(line["id"], Decimal("0")))
                enriched_lines.append(item)
            lines = enriched_lines
    if request.method == "POST":
        try:
            if not invoice:
                raise ValueError("فاتورة البيع غير موجودة.")
            selected = []
            for line_id, qty in zip(request.form.getlist("invoice_line_id"), request.form.getlist("return_quantity")):
                if qty and Decimal(qty) > 0:
                    selected.append({"invoice_line_id": int(line_id), "quantity": qty})
            c.execute("BEGIN IMMEDIATE")
            result = post_credit_note(
                c, invoice_id, request.form["credit_date"], request.form.get("reason", ""),
                selected, session["user_id"],
            )
            c.commit()
            flash("تم ترحيل الإشعار الدائن " + result["credit_note_no"], "success")
            return redirect(url_for("sales.index"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
    invoices = c.execute(
        """SELECT i.id,i.invoice_no,i.invoice_date,i.total_amount,i.paid_amount,
                  COALESCE(i.credited_amount,'0') credited_amount,cu.name customer_name
           FROM sales_invoices i JOIN customers cu ON cu.id=i.customer_id
           WHERE i.status='posted' ORDER BY i.id DESC"""
    ).fetchall()
    invoices = [row for row in invoices
                if Decimal(row["total_amount"]) > Decimal(row["credited_amount"] or "0")]
    c.close()
    return render_template(
        "sales_credit_note_new.html", invoices=invoices, invoice=invoice, lines=lines,
        today=date.today().isoformat(),
    )


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
    invoices = c.execute("""SELECT i.id,i.invoice_no,i.total_amount,i.paid_amount,
        COALESCE(i.credited_amount,'0') credited_amount,c.name customer_name
        FROM sales_invoices i JOIN customers c ON c.id=i.customer_id
        WHERE i.status='posted' ORDER BY i.invoice_date,i.id""").fetchall()
    invoices = [row for row in invoices if
                Decimal(row["total_amount"]) - Decimal(row["paid_amount"] or "0") -
                Decimal(row["credited_amount"] or "0") > 0]
    cash_accounts = c.execute("""SELECT id,code,name_ar FROM accounts
        WHERE account_type='asset' AND is_group=0 AND active=1 ORDER BY code""").fetchall()
    c.close()
    return render_template("sales_receipt_new.html", invoices=invoices, cash_accounts=cash_accounts,
                           today=date.today().isoformat())


if __name__ == "__main__":
    init_sales_schema()



@sales_bp.route("/credit-notes/<int:credit_note_id>/refund", methods=["GET", "POST"])
@require("sales.manage")
def refund_credit_note(credit_note_id):
    c = db()
    note = c.execute(
        """SELECT n.*,i.invoice_no,cu.name customer_name FROM sales_credit_notes n
           JOIN sales_invoices i ON i.id=n.invoice_id JOIN customers cu ON cu.id=i.customer_id
           WHERE n.id=? AND n.status='posted'""", (credit_note_id,)
    ).fetchone()
    if not note:
        c.close()
        flash("الإشعار الدائن غير موجود أو غير مرحل.", "error")
        return redirect(url_for("sales.index"))
    refunds = c.execute("SELECT amount FROM customer_refunds WHERE credit_note_id=?", (credit_note_id,)).fetchall()
    already_refunded = sum((Decimal(row["amount"]) for row in refunds), Decimal("0"))
    remaining_refundable = max(Decimal("0"), Decimal(note["refundable_amount"] or "0") - already_refunded)
    if request.method == "POST":
        try:
            c.execute("BEGIN IMMEDIATE")
            result = post_customer_refund(
                c, credit_note_id, request.form["refund_date"], request.form["amount"],
                int(request.form["cash_account_id"]), session["user_id"], request.form.get("reference", "")
            )
            c.commit()
            flash("تم تسجيل رد المبلغ " + result["refund_no"], "success")
            return redirect(url_for("sales.index"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
    cash_accounts = c.execute(
        "SELECT id,code,name_ar FROM accounts WHERE account_type='asset' AND is_group=0 AND active=1 ORDER BY code"
    ).fetchall()
    c.close()
    return render_template("sales_refund_new.html", note=note, remaining_refundable=remaining_refundable,
                           cash_accounts=cash_accounts, today=date.today().isoformat())


@sales_bp.route("/credit-notes/<int:credit_note_id>/cancel", methods=["POST"])
@require("sales.manage")
def cancel_credit_note_route(credit_note_id):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        result = cancel_credit_note(
            c, credit_note_id, request.form.get("cancellation_date") or date.today().isoformat(),
            request.form.get("reason", ""), session["user_id"]
        )
        c.commit()
        flash("تم إلغاء الإشعار الدائن بقيد عكسي رقم " + str(result["reversal_journal_id"]), "success")
    except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("sales.index"))
