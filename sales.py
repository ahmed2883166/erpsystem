"""NEXUS ERP sales: quotations, approved sales orders, delivery notes, invoices and receipts.

Direct invoices and delivered-order invoices share the same accounting engine. Goods
issued through delivery notes are costed and posted once; their later invoices only
recognize receivables, revenue and VAT.
"""
from app_paths import DB_PATH
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal
from database import connect_database, fetch_page
from stock import consume_layers
from analytic_dimensions import initialize_analytic_dimensions_schema, validate_journal_dimensions, active_dimension_options

sales_bp = Blueprint("sales", __name__, url_prefix="/sales")

CENT = Decimal("0.01")


def db():
    return connect_database(DB_PATH, isolation_level=None)


def init_sales_schema():
    c = db()
    initialize_analytic_dimensions_schema(c)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS salespersons(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS customers(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      tax_number TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
      email TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
      credit_limit TEXT NOT NULL DEFAULT '0',
      discount_type TEXT NOT NULL DEFAULT 'none', discount_value TEXT NOT NULL DEFAULT '0',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS sales_invoices(
      id INTEGER PRIMARY KEY, invoice_no TEXT NOT NULL UNIQUE,
      customer_id INTEGER NOT NULL REFERENCES customers(id),
      invoice_date TEXT NOT NULL, due_date TEXT, warehouse_id INTEGER REFERENCES stock_warehouses(id),
      subtotal TEXT NOT NULL, tax_rate TEXT NOT NULL, tax_amount TEXT NOT NULL,
      discount_type TEXT NOT NULL DEFAULT 'none', discount_value TEXT NOT NULL DEFAULT '0',
      discount_amount TEXT NOT NULL DEFAULT '0',
      total_amount TEXT NOT NULL, paid_amount TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'posted', journal_entry_id INTEGER UNIQUE,
      cogs_journal_entry_id INTEGER UNIQUE, created_by INTEGER,
      sales_channel TEXT NOT NULL DEFAULT 'default',
      revenue_account_code TEXT NOT NULL DEFAULT '',
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
    CREATE TABLE IF NOT EXISTS customer_receipt_batches(
      id INTEGER PRIMARY KEY, receipt_no TEXT NOT NULL UNIQUE,
      customer_id INTEGER NOT NULL REFERENCES customers(id), receipt_date TEXT NOT NULL,
      total_amount TEXT NOT NULL, cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER UNIQUE, reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS customer_receipt_allocations(
      id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES customer_receipt_batches(id) ON DELETE CASCADE,
      invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id), amount TEXT NOT NULL,
      UNIQUE(batch_id,invoice_id)
    );
    CREATE INDEX IF NOT EXISTS idx_customer_receipt_alloc_invoice ON customer_receipt_allocations(invoice_id);
    CREATE TABLE IF NOT EXISTS customer_advance_receipts(
      id INTEGER PRIMARY KEY, receipt_no TEXT NOT NULL UNIQUE,
      customer_id INTEGER NOT NULL REFERENCES customers(id),
      receipt_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE, reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS customer_advance_allocations(
      id INTEGER PRIMARY KEY,
      advance_receipt_id INTEGER NOT NULL REFERENCES customer_advance_receipts(id),
      invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id),
      allocation_date TEXT NOT NULL, amount TEXT NOT NULL,
      journal_entry_id INTEGER NOT NULL,
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_customer_advance_alloc_receipt
      ON customer_advance_allocations(advance_receipt_id,allocation_date,id);
    CREATE INDEX IF NOT EXISTS idx_customer_advance_alloc_invoice
      ON customer_advance_allocations(invoice_id);
    CREATE TABLE IF NOT EXISTS customer_advance_refunds(
      id INTEGER PRIMARY KEY,
      refund_no TEXT NOT NULL UNIQUE,
      advance_receipt_id INTEGER NOT NULL REFERENCES customer_advance_receipts(id),
      refund_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE,
      reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS customer_advance_reversals(
      id INTEGER PRIMARY KEY,
      source_type TEXT NOT NULL CHECK(source_type IN ('receipt','allocation','refund')),
      source_id INTEGER NOT NULL,
      original_journal_entry_id INTEGER NOT NULL UNIQUE,
      reversal_journal_entry_id INTEGER NOT NULL UNIQUE,
      reversal_date TEXT NOT NULL,
      reason TEXT NOT NULL,
      created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(source_type,source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_customer_advance_reversals_date
      ON customer_advance_reversals(reversal_date,source_type);
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
    invoice_columns = {row["name"] for row in c.execute("PRAGMA table_info(sales_invoices)")}
    if "sales_order_id" not in invoice_columns:
        c.execute("ALTER TABLE sales_invoices ADD COLUMN sales_order_id INTEGER")
    if "sales_channel" not in invoice_columns:
        c.execute("ALTER TABLE sales_invoices ADD COLUMN sales_channel TEXT NOT NULL DEFAULT 'default'")
    if "revenue_account_code" not in invoice_columns:
        c.execute("ALTER TABLE sales_invoices ADD COLUMN revenue_account_code TEXT NOT NULL DEFAULT ''")
    if "sales_order_line_id" not in line_columns:
        c.execute("ALTER TABLE sales_invoice_lines ADD COLUMN sales_order_line_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sales_invoices_order ON sales_invoices(sales_order_id,id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sales_invoice_lines_order_line ON sales_invoice_lines(sales_order_line_id,invoice_id)")
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
    if "due_date" not in invoice_columns:
        c.execute("ALTER TABLE sales_invoices ADD COLUMN due_date TEXT")
        c.execute("UPDATE sales_invoices SET due_date=invoice_date WHERE due_date IS NULL")
    if "credited_amount" not in invoice_columns:
        c.execute("ALTER TABLE sales_invoices ADD COLUMN credited_amount TEXT NOT NULL DEFAULT '0'")
    for name, table in (("branch_id", "branches"), ("cost_center_id", "cost_centers"), ("project_id", "projects"), ("salesperson_id", "salespersons")):
        if name not in invoice_columns:
            c.execute("ALTER TABLE sales_invoices ADD COLUMN " + name + " INTEGER REFERENCES " + table + "(id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sales_credit_invoice ON sales_credit_notes(invoice_id,credit_date,id)")
    customer_columns = {row["name"] for row in c.execute("PRAGMA table_info(customers)")}
    if "credit_limit" not in customer_columns:
        c.execute("ALTER TABLE customers ADD COLUMN credit_limit TEXT NOT NULL DEFAULT '0'")
    for name, definition in {
        "discount_type": "TEXT NOT NULL DEFAULT 'none'",
        "discount_value": "TEXT NOT NULL DEFAULT '0'",
    }.items():
        if name not in customer_columns:
            c.execute(f"ALTER TABLE customers ADD COLUMN {name} {definition}")
    for name, definition in {
        "discount_type": "TEXT NOT NULL DEFAULT 'none'",
        "discount_value": "TEXT NOT NULL DEFAULT '0'",
        "discount_amount": "TEXT NOT NULL DEFAULT '0'",
    }.items():
        if name not in invoice_columns:
            c.execute(f"ALTER TABLE sales_invoices ADD COLUMN {name} {definition}")
    # Re-run after creating customers and salespersons so their journal dimensions exist.
    initialize_analytic_dimensions_schema(c)
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
            if (permission not in set(session["permissions"])) if "permissions" in session else (session.get("role") not in allowed.get(permission, set())):
                flash("لا تملك الصلاحية المطلوبة.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco


def _select_customer_price_rule(price_rules, quantity):
    """Choose the highest applicable quantity tier; newest effective rule breaks ties."""
    try:
        quantity = Decimal(str(quantity).strip())
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError
        eligible = [
            rule for rule in price_rules
            if quantity >= Decimal(str(rule["min_qty"] or "0"))
        ]
        if not eligible:
            return None
        eligible.sort(
            key=lambda rule: (
                Decimal(str(rule["min_qty"] or "0")),
                str(rule["valid_from"] or ""),
                int(rule["id"]),
            ),
            reverse=True,
        )
    except (InvalidOperation, TypeError, ValueError, KeyError) as exc:
        raise ValueError("كمية الصنف غير صحيحة. أدخل كمية رقمية أكبر من صفر.") from exc
    return eligible[0]["unit_price"]


def _account(conn, code, expected_type):
    row = conn.execute(
        "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (code,)
    ).fetchone()
    if not row or row["is_group"] or not row["active"] or row["account_type"] != expected_type:
        raise ValueError("حساب الربط " + code + " غير موجود أو غير نشط أو غير تفصيلي.")
    return row["id"]


def _refund_total(conn, invoice_id):
    rows = conn.execute(
        """SELECT rf.amount FROM customer_refunds rf
           JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id WHERE cn.invoice_id=?""",
        (invoice_id,),
    ).fetchall()
    return sum((Decimal(str(row["amount"] or "0")) for row in rows), Decimal("0"))


def _activity(conn):
    row = conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
    return row["setting_value"] if row else "commercial"


def _sales_account_codes(conn, activity):
    from activity_profiles import active_posting_accounts
    codes = active_posting_accounts(conn)
    if not codes or not codes.get("ar") or not codes.get("revenue"):
        raise ValueError("ملف النشاط لا يحتوي حسابات العملاء والإيرادات المطلوبة للمبيعات.")
    return codes


def _sales_output_vat_account(conn, activity):
    from activity_profiles import active_tax_roles
    role_map = active_tax_roles(conn)
    if not role_map or not role_map.get("output_vat"):
        raise ValueError("لم يتم تعريف دور حساب ضريبة المخرجات لهذا النشاط.")
    return _account(conn, role_map["output_vat"], "liability")


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


def post_invoice(conn, customer_id, invoice_date, warehouse_id, lines, tax_rate, created_by=None, allow_credit_override=False, dimensions=None, preissued_costs=None, sales_order_id=None, discount_override=None, sales_channel="default"):
    """Create invoice and balanced GL; optionally consume stock already issued by delivery notes."""
    rate = Decimal(str(tax_rate)).quantize(CENT, rounding=ROUND_HALF_UP)
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError("نسبة الضريبة الافتراضية يجب أن تكون بين صفر و100.")
    # Header rate is only a default for standard-tax lines; each line is validated
    # independently. Exempt/zero-rated invoices must not fail on an unused header rate.
    allowed_rates = _allowed_vat_rates(conn)

    customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
    if not customer:
        raise ValueError("العميل غير موجود أو غير نشط.")
    dimensions = validate_journal_dimensions(conn, dimensions or {})
    dimensions["customer_id"] = int(customer_id)
    if dimensions.get("salesperson_id") not in (None, ""):
        dimensions["salesperson_id"] = int(dimensions["salesperson_id"])
    if not lines:
        raise ValueError("أضف صنفًا أو خدمة واحدة على الأقل.")

    activity = _activity(conn)
    codes = _sales_account_codes(conn, activity)
    ar = _account(conn, codes["ar"], "asset")
    sales_channel = str(sales_channel or "default").strip().lower()
    restaurant_channels = {
        "dine_in": "4100", "takeaway": "4200",
        "direct_delivery": "4300", "delivery_apps": "4400",
    }
    if activity == "restaurant":
        if sales_channel not in restaurant_channels:
            raise ValueError("حدد قناة بيع صحيحة: صالة أو تيك أواي أو توصيل مباشر أو تطبيقات.")
        revenue_code = restaurant_channels[sales_channel]
    else:
        sales_channel = "default"
        revenue_code = codes["revenue"]
    revenue = _account(conn, revenue_code, "revenue")
    vat = None
    if activity != "service" and not warehouse_id:
        raise ValueError("حدد مخزن الصرف قبل ترحيل فاتورة البيع.")

    normalized = []
    subtotal = Decimal("0.00")
    total_cost = Decimal("0.00")
    tax_amount = Decimal("0.00")
    if preissued_costs is not None and len(preissued_costs) != len(lines):
        raise ValueError("بيانات تكلفة إذن التسليم لا تطابق بنود الفاتورة.")
    for line_index, item in enumerate(lines):
        qty = Decimal(str(item.get("quantity", "1")))
        unit_price = Decimal(str(item.get("unit_price", "0"))).quantize(CENT, rounding=ROUND_HALF_UP)
        if not qty.is_finite() or qty <= 0 or not unit_price.is_finite() or unit_price < 0:
            raise ValueError("الكمية أو سعر البيع غير صحيح.")
        product_id = item.get("product_id")
        description = str(item.get("description") or "").strip()
        product_inventory_code = codes.get("inventory")
        product_cogs_code = codes.get("cogs")
        if product_id:
            product = conn.execute("SELECT * FROM stock_products WHERE id=? AND active=1", (int(product_id),)).fetchone()
            if not product:
                raise ValueError("الصنف غير موجود أو غير نشط.")
            from activity_profiles import resolve_product_account_code
            product_inventory_code = resolve_product_account_code(
                conn, product["id"], "inventory", codes.get("inventory")
            )
            product_cogs_code = resolve_product_account_code(
                conn, product["id"], "cogs", codes.get("cogs")
            )
            if not product_inventory_code or not product_cogs_code:
                raise ValueError("لم يتم تحديد حساب مخزون وتكلفة لهذا الصنف ضمن ملف النشاط.")
            _account(conn, product_inventory_code, "asset")
            _account(conn, product_cogs_code, "expense")
            if activity == "service":
                raise ValueError("نشاط الخدمات لا يستخدم صرف المخزون من شاشة المبيعات.")
            if preissued_costs is not None:
                fulfillment = preissued_costs[line_index]
                cost = Decimal(str(fulfillment.get("cost_total", "0"))).quantize(CENT, rounding=ROUND_HALF_UP)
                _allocations = fulfillment.get("allocations", [])
                if cost < 0:
                    raise ValueError("تكلفة إذن التسليم غير صحيحة.")
            else:
                cost, _allocations = consume_layers(
                    conn, product["id"], int(warehouse_id), qty,
                    "average" if activity == "service" else _inventory_method(conn)
                )
            description = description or product["name_ar"]
        else:
            if activity != "service" and preissued_costs is None:
                raise ValueError("اختر صنف مخزني لكل بند في فاتورة هذا النشاط.")
            cost = Decimal("0.00")
            if not description:
                raise ValueError("اكتب وصف الخدمة أو البند غير المخزني.")
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
        subtotal += line_total
        total_cost += cost
        normalized.append({
            "product_id": int(product_id) if product_id else None,
            "description": description, "quantity": qty, "unit_price": unit_price,
            "gross_line_total": line_total, "line_total": line_total, "cost_total": cost,
            "tax_rate": line_rate, "tax_treatment": treatment, "tax_amount": Decimal("0.00"),
            "stock_allocations": _allocations if product_id else [],
            "inventory_account_code": product_inventory_code if product_id else None,
            "cogs_account_code": product_cogs_code if product_id else None,
            "sales_order_line_id": item.get("sales_order_line_id"),
        })

    gross_subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
    if discount_override is not None:
        discount_type = str(discount_override.get("discount_type", "none")).strip().lower()
        discount_value = Decimal(str(discount_override.get("discount_value", "0"))).quantize(CENT, rounding=ROUND_HALF_UP)
        discount_amount = Decimal(str(discount_override.get("discount_amount", "0"))).quantize(CENT, rounding=ROUND_HALF_UP)
        if discount_type not in {"none", "percent", "fixed"}:
            raise ValueError("نوع خصم أمر البيع غير صحيح.")
        if not discount_value.is_finite() or discount_value < 0 or not discount_amount.is_finite() or discount_amount < 0:
            raise ValueError("قيمة خصم أمر البيع غير صحيحة.")
        if discount_type == "percent" and discount_value > 100:
            raise ValueError("نسبة خصم أمر البيع يجب ألا تتجاوز 100%.")
        if discount_amount >= gross_subtotal and discount_amount > 0:
            raise ValueError("خصم أمر البيع لا يمكن أن يساوي أو يتجاوز إجمالي الفاتورة.")
    else:
        discount_type = str(customer["discount_type"] or "none").strip().lower()
        discount_value = Decimal(str(customer["discount_value"] or "0")).quantize(CENT, rounding=ROUND_HALF_UP)
        if discount_type not in {"none", "percent", "fixed"}:
            raise ValueError("نوع خصم العميل غير صحيح.")
        if not discount_value.is_finite() or discount_value < 0:
            raise ValueError("قيمة خصم العميل يجب أن تكون صفرًا أو أكبر.")
        if discount_type == "percent":
            if discount_value > 100:
                raise ValueError("نسبة خصم العميل يجب ألا تتجاوز 100%.")
            discount_amount = (gross_subtotal * discount_value / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        elif discount_type == "fixed":
            if discount_value >= gross_subtotal and discount_value > 0:
                raise ValueError("قيمة الخصم الثابت يجب أن تكون أقل من إجمالي البنود.")
            discount_amount = discount_value
        else:
            discount_value = Decimal("0.00")
            discount_amount = Decimal("0.00")
    if discount_amount >= gross_subtotal and discount_amount > 0:
        raise ValueError("الخصم لا يمكن أن يساوي أو يتجاوز إجمالي الفاتورة.")
    remaining_discount = discount_amount
    for index, item in enumerate(normalized):
        if index == len(normalized) - 1:
            line_discount = min(max(remaining_discount, Decimal("0.00")), item["gross_line_total"])
        elif gross_subtotal:
            line_discount = (discount_amount * item["gross_line_total"] / gross_subtotal).quantize(CENT, rounding=ROUND_HALF_UP)
            line_discount = min(max(line_discount, Decimal("0.00")), remaining_discount, item["gross_line_total"])
            remaining_discount -= line_discount
        else:
            line_discount = Decimal("0.00")
        item["discount_amount"] = line_discount
        item["line_total"] = (item["gross_line_total"] - line_discount).quantize(CENT, rounding=ROUND_HALF_UP)
        item["tax_amount"] = (item["line_total"] * item["tax_rate"] / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        tax_amount += item["tax_amount"]

    subtotal = (gross_subtotal - discount_amount).quantize(CENT, rounding=ROUND_HALF_UP)
    tax_amount = tax_amount.quantize(CENT, rounding=ROUND_HALF_UP)
    total_amount = subtotal + tax_amount
    if subtotal <= 0:
        raise ValueError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")
    if tax_amount > 0 and vat is None:
        vat = _sales_output_vat_account(conn, activity)
    credit_limit = Decimal(customer["credit_limit"] or "0")
    outstanding_rows = conn.execute(
        """SELECT i.id,i.total_amount,i.paid_amount,COALESCE(i.credited_amount,'0') credited_amount
           FROM sales_invoices i WHERE i.customer_id=? AND i.status='posted'""", (customer_id,)
    ).fetchall()
    current_exposure = sum((
        Decimal(str(row["total_amount"] or "0")) -
        Decimal(str(row["paid_amount"] or "0")) -
        Decimal(str(row["credited_amount"] or "0")) +
        _refund_total(conn, row["id"])
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
           tax_rate,tax_amount,discount_type,discount_value,discount_amount,total_amount,
           created_by,branch_id,cost_center_id,project_id,salesperson_id,sales_order_id,
           sales_channel,revenue_account_code)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (invoice_no, customer_id, invoice_date, warehouse_id, str(subtotal), str(effective_rate),
         str(tax_amount), discount_type, str(discount_value), str(discount_amount), str(total_amount),
         created_by, dimensions["branch_id"], dimensions["cost_center_id"],
         dimensions["project_id"], dimensions["salesperson_id"], sales_order_id,
         sales_channel, revenue_code)
    )
    invoice_id = cur.lastrowid
    for index, item in enumerate(normalized, 1):
        movement_id = None
        if item["product_id"] and preissued_costs is None:
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
        order_line_id = item.get("sales_order_line_id")
        line_cursor = conn.execute(
            """INSERT INTO sales_invoice_lines(invoice_id,product_id,description,quantity,unit_price,
               line_total,cost_total,stock_movement_id,tax_rate,tax_treatment,tax_amount,sales_order_line_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (invoice_id, item["product_id"], item["description"], str(item["quantity"]),
             str(item["unit_price"]), str(item["line_total"]), str(item["cost_total"]), movement_id,
             str(item["tax_rate"]), item["tax_treatment"], str(item["tax_amount"]), order_line_id)
        )
        invoice_line_id = line_cursor.lastrowid
        for allocation in item.get("stock_allocations", []):
            if len(allocation) == 4:
                lot_id, allocated_qty, allocated_unit_cost, allocated_cost = allocation
            else:
                lot_id, allocated_qty, allocated_cost = allocation
            conn.execute(
                "INSERT INTO sales_invoice_line_lots(invoice_line_id,lot_id,quantity,cost_total) VALUES(?,?,?,?)",
                (invoice_line_id, lot_id, str(allocated_qty), str(allocated_cost))
            )

    invoice_lines = [(ar, total_amount, 0, "ذمم العميل - " + customer["name"]),
                     (revenue, 0, subtotal, "إيراد فاتورة " + invoice_no)]
    if tax_amount:
        invoice_lines.append((vat, 0, tax_amount, "ضريبة مخرجات"))
    journal_id = post_journal(conn, invoice_date, invoice_no, "فاتورة مبيعات " + invoice_no,
                              invoice_lines, created_by, dimensions=dimensions)
    conn.execute("UPDATE sales_invoices SET journal_entry_id=? WHERE id=?", (journal_id, invoice_id))

    cogs_journal_id = None
    if total_cost > 0 and preissued_costs is None:
        cost_groups = {}
        for item in normalized:
            if not item["product_id"] or item["cost_total"] <= 0:
                continue
            key = (item["cogs_account_code"], item["inventory_account_code"])
            cost_groups[key] = cost_groups.get(key, Decimal("0.00")) + item["cost_total"]
        cogs_lines = []
        for (cogs_code, inventory_code), group_cost in sorted(cost_groups.items()):
            cogs_id = _account(conn, cogs_code, "expense")
            inventory_id = _account(conn, inventory_code, "asset")
            cogs_lines.extend([
                (cogs_id, group_cost, 0, "تكلفة المبيعات - " + cogs_code),
                (inventory_id, 0, group_cost, "تخفيض المخزون - " + inventory_code),
            ])
        cogs_journal_id = post_journal(
            conn, invoice_date, invoice_no + "-COGS", "تكلفة مبيعات " + invoice_no,
            cogs_lines, created_by, dimensions=dimensions
        )
    if preissued_costs is None:
        for row in conn.execute("SELECT id FROM stock_movements WHERE movement_no LIKE ?", (invoice_no + "-%",)).fetchall():
            conn.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status=? WHERE id=?",
                         (cogs_journal_id, "posted" if cogs_journal_id else "not_required", row["id"]))
    return {"invoice_id": invoice_id, "invoice_no": invoice_no, "journal_entry_id": journal_id,
            "cogs_journal_entry_id": cogs_journal_id, "subtotal": subtotal,
            "gross_subtotal": gross_subtotal, "discount_type": discount_type,
            "discount_value": discount_value, "discount_amount": discount_amount,
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
    if credit_date < invoice["invoice_date"]:
        raise ValueError("لا يمكن إصدار إشعار دائن بتاريخ يسبق تاريخ فاتورة البيع.")
    dimensions = validate_journal_dimensions(conn, {
        "branch_id": invoice["branch_id"], "cost_center_id": invoice["cost_center_id"],
        "project_id": invoice["project_id"], "customer_id": invoice["customer_id"],
        "salesperson_id": invoice["salesperson_id"],
    })
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
    codes = _sales_account_codes(conn, activity)
    ar = _account(conn, codes["ar"], "asset")
    credit_revenue_code = invoice["revenue_account_code"] or codes["revenue"]
    revenue = _account(conn, credit_revenue_code, "revenue")
    vat_account = None

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
            """SELECT cl.quantity,cl.line_total,cl.tax_amount,cl.cost_total
               FROM sales_credit_note_lines cl
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

        # Credit the actual net invoiced value after line/customer discounts, not
        # the original gross unit price. Allocate rounded cents proportionally and
        # give the final returned quantity the exact remaining net/tax/cost balance.
        remaining_net = Decimal(original["line_total"]) - sum(
            (Decimal(row["line_total"] or "0") for row in prior_credit_rows), Decimal("0")
        )
        remaining_tax = Decimal(original["tax_amount"] or "0") - sum(
            (Decimal(row["tax_amount"] or "0") for row in prior_credit_rows), Decimal("0")
        )
        remaining_cost = Decimal(original["cost_total"] or "0") - sum(
            (Decimal(row["cost_total"] or "0") for row in prior_credit_rows), Decimal("0")
        )
        if qty == available_qty:
            line_value = remaining_net.quantize(CENT, rounding=ROUND_HALF_UP)
            line_tax = remaining_tax.quantize(CENT, rounding=ROUND_HALF_UP)
            line_cost = remaining_cost.quantize(CENT, rounding=ROUND_HALF_UP)
        else:
            line_value = (remaining_net * qty / available_qty).quantize(CENT, rounding=ROUND_HALF_UP)
            line_tax = (remaining_tax * qty / available_qty).quantize(CENT, rounding=ROUND_HALF_UP)
            line_cost = (remaining_cost * qty / available_qty).quantize(CENT, rounding=ROUND_HALF_UP)
        unit_price = Decimal(original["unit_price"])
        tax_rate = Decimal(original["tax_rate"] or "0")
        treatment = original["tax_treatment"] or "standard"
        subtotal += line_value
        tax_total += line_tax
        cost_total += line_cost
        original_allocations = conn.execute(
            "SELECT lot_id,quantity,cost_total FROM sales_invoice_line_lots WHERE invoice_line_id=? ORDER BY id",
            (line_id,),
        ).fetchall()
        if not original_allocations:
            product_tracking = conn.execute(
                "SELECT track_lot,track_expiry FROM stock_products WHERE id=?", (original["product_id"],)
            ).fetchone() if original["product_id"] else None
            if product_tracking and (product_tracking["track_lot"] or product_tracking["track_expiry"]):
                raise ValueError(
                    "الفاتورة قديمة ولا تحتوي على سجل التشغيلة الأصلية. لا يمكن إرجاعها إلى مخزون عام؛ "
                    "راجع حركة الصرف وحدد التشغيلة قبل إصدار المرتجع."
                )
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
        from activity_profiles import resolve_product_account_code
        product_id = original["product_id"]
        inventory_code = resolve_product_account_code(
            conn, product_id, "inventory", codes.get("inventory")
        ) if product_id else None
        cogs_code = resolve_product_account_code(
            conn, product_id, "cogs", codes.get("cogs")
        ) if product_id else None
        normalized.append({
            "original": original, "quantity": qty, "unit_price": unit_price,
            "line_total": line_value, "tax_rate": tax_rate,
            "tax_treatment": treatment, "tax_amount": line_tax,
            "cost_total": line_cost, "lot_allocations": lot_allocations,
            "inventory_account_code": inventory_code, "cogs_account_code": cogs_code,
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
        vat_account = _sales_output_vat_account(conn, activity)

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
        journal_lines, created_by, dimensions=dimensions,
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
            default_location = conn.execute("SELECT location_id FROM stock_product_locations WHERE product_id=? AND warehouse_id=?",
                                            (product_id, warehouse_id)).fetchone()
            return_location_id = default_location["location_id"] if default_location else None
            for lot_part in item["lot_allocations"]:
                stock_index += 1
                movement_no = credit_no + "-" + str(stock_index)
                part_qty, part_cost = lot_part["quantity"], lot_part["cost_total"]
                unit_cost = part_cost / part_qty
                movement = conn.execute(
                    """INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,target_location_id,
                       lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status,issue_purpose)
                       VALUES(?,'receipt',?,?,?,?,?,?,?,?,?,?, 'pending','sales_return')""",
                    (movement_no, product_id, warehouse_id, return_location_id, lot_part["lot_id"], str(part_qty), str(unit_cost),
                     str(part_cost), credit_date, "مرتجع مبيعات بموجب " + credit_no, created_by),
                )
                movement_id = movement.lastrowid
                conn.execute(
                    """INSERT INTO stock_valuation_layers(product_id,warehouse_id,location_id,lot_id,source_movement_id,
                       qty_in,qty_remaining,unit_cost,value_remaining) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (product_id, warehouse_id, return_location_id, lot_part["lot_id"], movement_id, str(part_qty),
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
        return_cost_groups = {}
        for item in normalized:
            if not item["original"]["product_id"] or item["cost_total"] <= 0:
                continue
            key = (item["inventory_account_code"], item["cogs_account_code"])
            return_cost_groups[key] = return_cost_groups.get(key, Decimal("0.00")) + item["cost_total"]
        return_cost_lines = []
        for (inventory_code, cogs_code), group_cost in sorted(return_cost_groups.items()):
            inventory_id = _account(conn, inventory_code, "asset")
            cogs_id = _account(conn, cogs_code, "expense")
            return_cost_lines.extend([
                (inventory_id, group_cost, 0, "إعادة تكلفة المرتجع للمخزون - " + inventory_code),
                (cogs_id, 0, group_cost, "عكس تكلفة المبيعات - " + cogs_code),
            ])
        cogs_journal_id = post_journal(
            conn, credit_date, credit_no + "-COGS", "عكس تكلفة المبيعات " + credit_no,
            return_cost_lines, created_by, dimensions=dimensions,
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
    original = conn.execute(
        "SELECT status FROM journal_entries WHERE id=?", (entry_id,)
    ).fetchone()
    if not original or original["status"] != "posted":
        raise ValueError("لا يمكن عكس قيد غير موجود أو غير مرحل.")
    dimension_keys = (
        "branch_id", "cost_center_id", "project_id", "customer_id",
        "salesperson_id", "supplier_id",
    )
    available_dimensions = {
        row["name"] for row in conn.execute("PRAGMA table_info(journal_lines)")
    }
    selected_dimensions = [key for key in dimension_keys if key in available_dimensions]
    selected_columns = ["account_id", "debit", "credit", "memo", *selected_dimensions]
    rows = conn.execute(
        "SELECT " + ",".join(selected_columns) +
        " FROM journal_lines WHERE entry_id=? ORDER BY id",
        (entry_id,),
    ).fetchall()
    if not rows:
        raise ValueError("لا يمكن عكس قيد غير موجود أو بلا تفاصيل.")
    reversed_lines = [
        (
            row["account_id"], Decimal(str(row["credit"] or "0")),
            Decimal(str(row["debit"] or "0")), "عكس: " + str(row["memo"] or ""),
            {key: row[key] for key in selected_dimensions},
        )
        for row in rows
    ]
    return post_journal(
        conn, reversal_date, reference, description, reversed_lines, created_by,
    )


def post_customer_refund(conn, credit_note_id, refund_date, amount, cash_account_id,
                         created_by=None, reference=""):
    refund_date = _validate_customer_advance_date(refund_date)
    amount = _validated_customer_advance_amount(amount, "قيمة رد المبلغ")
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
    if refund_date < note["credit_date"]:
        raise ValueError("لا يمكن رد مبلغ بتاريخ يسبق تاريخ الإشعار الدائن.")
    _ensure_unique_customer_cash_reference(conn, reference)
    _customer_cash_account(conn, cash_account_id)
    invoice = conn.execute("SELECT * FROM sales_invoices WHERE id=?", (note["invoice_id"],)).fetchone()
    ar = _account(conn, _sales_account_codes(conn, _activity(conn))["ar"], "asset")
    refund_no = "SREF-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    dimensions = validate_journal_dimensions(conn, _customer_invoice_dimensions(invoice))
    journal_id = post_journal(
        conn, refund_date, refund_no, "رد مبلغ للعميل عن إشعار " + note["credit_note_no"],
        [(ar, amount, 0, "تسوية الرصيد الدائن للعميل"),
         (cash_account_id, 0, amount, "رد نقدي/بنكي للعميل")], created_by, dimensions=dimensions,
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
    receipt_date = _validate_customer_advance_date(receipt_date)
    amount = _validated_customer_advance_amount(amount, "قيمة التحصيل")
    invoice = conn.execute("SELECT * FROM sales_invoices WHERE id=? AND status='posted'", (invoice_id,)).fetchone()
    if not invoice:
        raise ValueError("فاتورة العميل غير موجودة أو غير مرحلة.")
    if "invoice_date" in invoice.keys() and receipt_date < invoice["invoice_date"]:
        raise ValueError("لا يمكن تسجيل تحصيل بتاريخ يسبق تاريخ فاتورة البيع.")
    _ensure_unique_customer_cash_reference(conn, reference)
    outstanding = (Decimal(invoice["total_amount"]) - Decimal(invoice["paid_amount"]) -
                   Decimal(invoice["credited_amount"] or "0") + _refund_total(conn, invoice_id))
    if amount > outstanding:
        raise ValueError("قيمة التحصيل أكبر من الرصيد المستحق على الفاتورة.")
    _customer_cash_account(conn, cash_account_id)
    activity = _activity(conn)
    ar = _account(conn, _sales_account_codes(conn, activity)["ar"], "asset")
    receipt_no = "RCPT-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    dimensions = validate_journal_dimensions(conn, {
        "branch_id": invoice["branch_id"], "cost_center_id": invoice["cost_center_id"],
        "project_id": invoice["project_id"], "customer_id": invoice["customer_id"],
        "salesperson_id": invoice["salesperson_id"] if "salesperson_id" in invoice.keys() else None,
    })
    journal_id = post_journal(
        conn, receipt_date, receipt_no, "تحصيل من عميل " + invoice["invoice_no"],
        [(cash_account_id, amount, 0, "تحصيل نقدي/بنكي"),
         (ar, 0, amount, "تخفيض ذمم العميل")], created_by, dimensions=dimensions
    )
    conn.execute(
        """INSERT INTO customer_receipts(receipt_no,invoice_id,receipt_date,amount,cash_account_id,
           journal_entry_id,reference,created_by) VALUES(?,?,?,?,?,?,?,?)""",
        (receipt_no, invoice_id, receipt_date, str(amount), cash_account_id, journal_id, reference, created_by)
    )
    conn.execute("UPDATE sales_invoices SET paid_amount=? WHERE id=?",
                 (str(Decimal(invoice["paid_amount"]) + amount), invoice_id))
    return {"receipt_no": receipt_no, "journal_entry_id": journal_id}


def post_customer_receipt_batch(conn, customer_id, receipt_date, allocations, cash_account_id,
                                created_by=None, reference=""):
    """Post one cash receipt allocated across multiple open invoices of one customer."""
    receipt_date = _validate_customer_advance_date(receipt_date)
    customer = conn.execute("SELECT id,name FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
    if not customer:
        raise ValueError("العميل غير موجود أو غير نشط.")
    normalized = []
    seen = set()
    total = Decimal("0")
    for invoice_id, raw_amount in allocations:
        if invoice_id in seen:
            raise ValueError("لا يمكن اختيار الفاتورة أكثر من مرة في نفس الإيصال.")
        seen.add(invoice_id)
        amount = _validated_customer_advance_amount(raw_amount, "قيمة التوزيع على كل فاتورة")
        invoice = conn.execute("SELECT * FROM sales_invoices WHERE id=? AND customer_id=? AND status='posted'",
                               (invoice_id, customer_id)).fetchone()
        if not invoice:
            raise ValueError("إحدى الفواتير المحددة لا تتبع العميل أو غير مرحلة.")
        if "invoice_date" in invoice.keys() and receipt_date < invoice["invoice_date"]:
            raise ValueError("لا يمكن تسجيل تحصيل بتاريخ يسبق تاريخ إحدى فواتير البيع.")
        outstanding = (Decimal(invoice["total_amount"]) - Decimal(invoice["paid_amount"] or "0")
                       - Decimal(invoice["credited_amount"] or "0") + _refund_total(conn, invoice_id))
        if outstanding <= 0:
            raise ValueError("إحدى الفواتير المحددة لا يوجد عليها رصيد مستحق.")
        if amount > outstanding:
            raise ValueError(f"مبلغ التحصيل على الفاتورة {invoice['invoice_no']} أكبر من المتبقي {outstanding:.2f}.")
        normalized.append((invoice, amount))
        total += amount
    if not normalized or total <= 0:
        raise ValueError("اختر فاتورة واحدة على الأقل وأدخل مبلغ التحصيل.")
    _ensure_unique_customer_cash_reference(conn, reference)
    _customer_cash_account(conn, cash_account_id)
    ar = _account(conn, _sales_account_codes(conn, _activity(conn))["ar"], "asset")
    receipt_no = "RCPTB-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    journal_lines = [
        (cash_account_id, total, 0, "إجمالي التحصيل المجمع", {"customer_id": customer_id})
    ]
    for invoice, amount in normalized:
        invoice_dimensions = validate_journal_dimensions(
            conn, _customer_invoice_dimensions(invoice)
        )
        journal_lines.append((
            ar, 0, amount, "تخفيض ذمم الفاتورة " + invoice["invoice_no"],
            invoice_dimensions,
        ))
    journal_id = post_journal(
        conn, receipt_date, receipt_no, "تحصيل مجمع من العميل " + customer["name"],
        journal_lines, created_by,
    )
    cursor = conn.execute("""INSERT INTO customer_receipt_batches
        (receipt_no,customer_id,receipt_date,total_amount,cash_account_id,journal_entry_id,reference,created_by)
        VALUES(?,?,?,?,?,?,?,?)""",
        (receipt_no, customer_id, receipt_date, str(total), cash_account_id, journal_id, reference, created_by))
    batch_id = cursor.lastrowid
    for invoice, amount in normalized:
        conn.execute("INSERT INTO customer_receipt_allocations(batch_id,invoice_id,amount) VALUES(?,?,?)",
                     (batch_id, invoice["id"], str(amount)))
        conn.execute("UPDATE sales_invoices SET paid_amount=? WHERE id=?",
                     (str(Decimal(invoice["paid_amount"] or "0") + amount), invoice["id"]))
    return {"receipt_no": receipt_no, "journal_entry_id": journal_id, "total_amount": total,
            "invoice_count": len(normalized)}




def _customer_invoice_dimensions(invoice):
    """Return the analytical dimensions carried by a customer invoice."""
    keys = ("branch_id", "cost_center_id", "project_id", "customer_id", "salesperson_id")
    available = set(invoice.keys())
    return {key: invoice[key] for key in keys if key in available and invoice[key] not in (None, "")}


def _common_customer_invoice_dimensions(conn, invoices, customer_id):
    """Keep invoice dimensions when a grouped settlement shares one dimension set."""
    candidates = [_customer_invoice_dimensions(invoice) for invoice in invoices]
    if candidates and all(item == candidates[0] for item in candidates[1:]):
        return validate_journal_dimensions(conn, candidates[0])
    # One journal line cannot represent multiple branch/project assignments.
    # Preserve the customer dimension rather than falsely assigning one invoice's dimensions.
    return validate_journal_dimensions(conn, {"customer_id": customer_id})


def _ensure_unique_customer_cash_reference(conn, reference):
    """Reject a reused external cash/bank reference across customer cash documents."""
    reference = str(reference or "").strip()
    if not reference:
        return
    sources = (
        ("customer_receipts", "reference"),
        ("customer_receipt_batches", "reference"),
        ("customer_advance_receipts", "reference"),
        ("customer_advance_refunds", "reference"),
        ("customer_refunds", "reference"),
    )
    for table, column in sources:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            columns = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if column in columns and conn.execute(
                f'SELECT 1 FROM "{table}" WHERE TRIM(COALESCE("{column}",\'\'))=? LIMIT 1',
                (reference,),
            ).fetchone():
                raise ValueError("رقم مرجع البنك/الإيصال مستخدم بالفعل في عملية تحصيل أو رد سابقة.")


def _customer_cash_account(conn, cash_account_id):
    """Accept only active detailed cash/bank accounts mapped for this activity."""
    from activity_profiles import active_account_role_ids
    allowed_ids = active_account_role_ids(conn).get("cash", set())
    row = conn.execute(
        "SELECT id,is_group,active,account_type FROM accounts WHERE id=?",
        (cash_account_id,),
    ).fetchone()
    if (not row or row["id"] not in allowed_ids or row["is_group"] or
            not row["active"] or row["account_type"] != "asset"):
        raise ValueError("اختر حساب صندوق أو بنك تفصيليًا ونشطًا ومربوطًا بملف النشاط.")
    return row


def _customer_advances_account(conn):
    """Resolve the customer-advances liability through the selected activity profile."""
    from activity_profiles import active_account_role_ids
    account_ids = active_account_role_ids(conn).get("customer_advances", set())
    if len(account_ids) != 1:
        raise ValueError("يجب ربط دور دفعات العملاء المقدمة بحساب التزام تفصيلي واحد في ملف النشاط.")
    row = conn.execute(
        "SELECT id,is_group,active,account_type FROM accounts WHERE id=?",
        (next(iter(account_ids)),),
    ).fetchone()
    if not row or row["is_group"] or not row["active"] or row["account_type"] != "liability":
        raise ValueError("حساب دفعات العملاء المقدمة غير موجود أو غير نشط أو ليس التزامًا تفصيليًا.")
    return row["id"]


def _validated_customer_advance_amount(raw_amount, label):
    try:
        amount = Decimal(str(raw_amount)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} يجب أن تكون قيمة مالية صحيحة.")
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{label} يجب أن تكون أكبر من صفر.")
    return amount


def _validate_customer_advance_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("تاريخ العملية غير صحيح.")


def _customer_advance_available(conn, advance_receipt_id):
    receipt = conn.execute(
        """SELECT r.amount FROM customer_advance_receipts r
           WHERE r.id=? AND NOT EXISTS (
             SELECT 1 FROM customer_advance_reversals x
             WHERE x.source_type='receipt' AND x.source_id=r.id
           )""",
        (advance_receipt_id,),
    ).fetchone()
    if not receipt:
        raise ValueError("إيصال مقدم العميل غير موجود أو تم عكسه.")
    allocated = conn.execute(
        """SELECT a.amount FROM customer_advance_allocations a
           WHERE a.advance_receipt_id=? AND NOT EXISTS (
             SELECT 1 FROM customer_advance_reversals x
             WHERE x.source_type='allocation' AND x.source_id=a.journal_entry_id
           )""",
        (advance_receipt_id,),
    ).fetchall()
    refunded = conn.execute(
        """SELECT r.amount FROM customer_advance_refunds r
           WHERE r.advance_receipt_id=? AND NOT EXISTS (
             SELECT 1 FROM customer_advance_reversals x
             WHERE x.source_type='refund' AND x.source_id=r.id
           )""",
        (advance_receipt_id,),
    ).fetchall()
    used = sum((Decimal(str(row["amount"] or "0")) for row in allocated + refunded), Decimal("0"))
    return Decimal(str(receipt["amount"])) - used


def post_customer_advance_receipt(conn, customer_id, receipt_date, amount,
                                  cash_account_id, created_by=None, reference=""):
    """Receive an unallocated customer advance; it is a liability, not revenue or AR collection."""
    receipt_date = _validate_customer_advance_date(receipt_date)
    amount = _validated_customer_advance_amount(amount, "قيمة مقدم العميل")
    customer = conn.execute(
        "SELECT id,name FROM customers WHERE id=? AND active=1", (customer_id,)
    ).fetchone()
    if not customer:
        raise ValueError("العميل غير موجود أو غير نشط.")
    _ensure_unique_customer_cash_reference(conn, reference)
    _customer_cash_account(conn, cash_account_id)
    advance_account = _customer_advances_account(conn)
    receipt_no = "CADV-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    dimensions = validate_journal_dimensions(conn, {"customer_id": customer_id})
    journal_id = post_journal(
        conn, receipt_date, receipt_no, "استلام مقدم من العميل " + customer["name"],
        [(cash_account_id, amount, 0, "استلام مقدم عميل"),
         (advance_account, 0, amount, "التزام دفعات مقدمة من العميل")],
        created_by, dimensions=dimensions,
    )
    cur = conn.execute(
        """INSERT INTO customer_advance_receipts
           (receipt_no,customer_id,receipt_date,amount,cash_account_id,journal_entry_id,reference,created_by)
           VALUES(?,?,?,?,?,?,?,?)""",
        (receipt_no, customer_id, receipt_date, str(amount), cash_account_id,
         journal_id, reference, created_by),
    )
    return {"advance_receipt_id": cur.lastrowid, "receipt_no": receipt_no,
            "journal_entry_id": journal_id, "amount": amount}


def allocate_customer_advance(conn, advance_receipt_id, allocations, allocation_date,
                               created_by=None):
    """Apply one advance to one or more posted invoices belonging to the same customer."""
    allocation_date = _validate_customer_advance_date(allocation_date)
    receipt = conn.execute(
        """SELECT r.*,c.name customer_name FROM customer_advance_receipts r
           JOIN customers c ON c.id=r.customer_id WHERE r.id=? AND c.active=1""",
        (advance_receipt_id,),
    ).fetchone()
    if not receipt:
        raise ValueError("إيصال مقدم العميل غير موجود أو العميل غير نشط.")
    if allocation_date < receipt["receipt_date"]:
        raise ValueError("لا يمكن تخصيص مقدم العميل بتاريخ يسبق تاريخ استلامه.")
    normalized, seen = [], set()
    total = Decimal("0")
    for invoice_id, raw_amount in allocations:
        if invoice_id in seen:
            raise ValueError("لا يمكن اختيار الفاتورة أكثر من مرة في نفس عملية التخصيص.")
        seen.add(invoice_id)
        amount = _validated_customer_advance_amount(raw_amount, "قيمة التخصيص")
        invoice = conn.execute(
            "SELECT * FROM sales_invoices WHERE id=? AND customer_id=? AND status='posted'",
            (invoice_id, receipt["customer_id"]),
        ).fetchone()
        if not invoice:
            raise ValueError("إحدى الفواتير لا تتبع نفس العميل أو غير مرحلة.")
        if allocation_date < date.fromisoformat(invoice["invoice_date"]).isoformat():
            raise ValueError(
                f"لا يمكن تخصيص المقدم على الفاتورة {invoice['invoice_no']} قبل تاريخ إصدارها."
            )
        outstanding = (Decimal(str(invoice["total_amount"])) -
                       Decimal(str(invoice["paid_amount"] or "0")) -
                       Decimal(str(invoice["credited_amount"] or "0")) +
                       _refund_total(conn, invoice_id))
        if outstanding <= 0:
            raise ValueError("إحدى الفواتير لا يوجد عليها رصيد مستحق.")
        if amount > outstanding:
            raise ValueError(
                f"قيمة التخصيص أكبر من المتبقي على الفاتورة {invoice['invoice_no']} ({outstanding:.2f})."
            )
        normalized.append((invoice, amount))
        total += amount
    if not normalized:
        raise ValueError("اختر فاتورة واحدة على الأقل وأدخل قيمة التخصيص.")
    available = _customer_advance_available(conn, advance_receipt_id)
    if total > available:
        raise ValueError(f"قيمة التخصيص تتجاوز المتبقي من مقدم العميل ({available:.2f}).")
    advance_account = _customer_advances_account(conn)
    ar = _account(conn, _sales_account_codes(conn, _activity(conn))["ar"], "asset")
    allocation_no = "CADVA-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    journal_lines = [
        (advance_account, total, 0, "تخفيض التزام مقدم العميل",
         validate_journal_dimensions(conn, {"customer_id": receipt["customer_id"]}))
    ]
    for invoice, amount in normalized:
        invoice_dimensions = validate_journal_dimensions(
            conn, _customer_invoice_dimensions(invoice)
        )
        journal_lines.append((
            ar, 0, amount, "تسوية ذمم الفاتورة " + invoice["invoice_no"],
            invoice_dimensions,
        ))
    journal_id = post_journal(
        conn, allocation_date, allocation_no,
        "تخصيص مقدم عميل على فواتير " + receipt["customer_name"],
        journal_lines, created_by,
    )
    for invoice, amount in normalized:
        conn.execute(
            """INSERT INTO customer_advance_allocations
               (advance_receipt_id,invoice_id,allocation_date,amount,journal_entry_id,created_by)
               VALUES(?,?,?,?,?,?)""",
            (advance_receipt_id, invoice["id"], allocation_date, str(amount), journal_id, created_by),
        )
        conn.execute(
            "UPDATE sales_invoices SET paid_amount=? WHERE id=?",
            (str(Decimal(str(invoice["paid_amount"] or "0")) + amount), invoice["id"]),
        )
    return {"journal_entry_id": journal_id, "total_amount": total,
            "invoice_count": len(normalized), "remaining_advance": available - total}


def reverse_customer_advance_transaction(
    conn, source_type, source_id, reversal_date, reason, created_by=None
):
    """Reverse an advance receipt, allocation batch, or refund with a linked journal."""
    if source_type not in {"receipt", "allocation", "refund"}:
        raise ValueError("نوع عملية المقدم المراد عكسها غير صحيح.")
    reversal_date = _validate_customer_advance_date(reversal_date)
    reason = str(reason or "").strip()
    if len(reason) < 5 or len(reason) > 500:
        raise ValueError("سبب العكس مطلوب من 5 إلى 500 حرف.")
    if conn.execute(
        """SELECT 1 FROM customer_advance_reversals
           WHERE source_type=? AND source_id=?""",
        (source_type, source_id),
    ).fetchone():
        raise ValueError("تم عكس هذه العملية من قبل.")
    if source_type == "receipt":
        source = conn.execute(
            "SELECT * FROM customer_advance_receipts WHERE id=?", (source_id,)
        ).fetchone()
        if not source:
            raise ValueError("إيصال المقدم غير موجود.")
        original_journal_id = source["journal_entry_id"]
        original_date = source["receipt_date"]
        available = _customer_advance_available(conn, source_id)
        if available != Decimal(str(source["amount"])):
            raise ValueError("لا يمكن عكس إيصال المقدم قبل عكس كل التخصيصات والردود المرتبطة به.")
    elif source_type == "allocation":
        original_journal_id = int(source_id)
        allocations = conn.execute(
            """SELECT a.*,i.invoice_no,i.paid_amount,i.customer_id
               FROM customer_advance_allocations a
               JOIN sales_invoices i ON i.id=a.invoice_id
               WHERE a.journal_entry_id=? ORDER BY a.id""",
            (original_journal_id,),
        ).fetchall()
        if not allocations:
            raise ValueError("عملية تخصيص المقدم غير موجودة.")
        original_date = max(row["allocation_date"] for row in allocations)
        for row in allocations:
            if Decimal(str(row["paid_amount"] or "0")) < Decimal(str(row["amount"])):
                raise ValueError("رصيد الفاتورة لا يسمح بعكس قيمة التخصيص بأمان.")
    else:
        source = conn.execute(
            "SELECT * FROM customer_advance_refunds WHERE id=?", (source_id,)
        ).fetchone()
        if not source:
            raise ValueError("عملية رد المقدم غير موجودة.")
        original_journal_id = source["journal_entry_id"]
        original_date = source["refund_date"]
    if reversal_date < original_date:
        raise ValueError("تاريخ العكس لا يجوز أن يسبق تاريخ العملية الأصلية.")
    reversal_no = "CADV-REV-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    reversal_journal_id = _reverse_posted_journal(
        conn, original_journal_id, reversal_date, reversal_no,
        "عكس عملية مقدم عميل: " + reason, created_by,
    )
    if source_type == "allocation":
        for row in allocations:
            conn.execute(
                "UPDATE sales_invoices SET paid_amount=? WHERE id=?",
                (str(Decimal(str(row["paid_amount"] or "0")) - Decimal(str(row["amount"]))),
                 row["invoice_id"]),
            )
    reversal_record = conn.execute(
        """INSERT INTO customer_advance_reversals
           (source_type,source_id,original_journal_entry_id,reversal_journal_entry_id,
            reversal_date,reason,created_by)
           VALUES(?,?,?,?,?,?,?)""",
        (source_type, source_id, original_journal_id, reversal_journal_id,
         reversal_date, reason, created_by),
    )
    return {
        "reversal_id": reversal_record.lastrowid,
        "source_type": source_type, "source_id": source_id,
        "original_journal_entry_id": original_journal_id,
        "reversal_journal_entry_id": reversal_journal_id,
        "reversal_no": reversal_no,
    }


def post_customer_advance_refund(conn, advance_receipt_id, refund_date, amount,
                                 cash_account_id, created_by=None, reference=""):
    """Refund only the unallocated portion of a customer advance."""
    refund_date = _validate_customer_advance_date(refund_date)
    amount = _validated_customer_advance_amount(amount, "قيمة رد مقدم العميل")
    receipt = conn.execute(
        """SELECT r.*,c.name customer_name FROM customer_advance_receipts r
           JOIN customers c ON c.id=r.customer_id WHERE r.id=? AND c.active=1""",
        (advance_receipt_id,),
    ).fetchone()
    if not receipt:
        raise ValueError("إيصال مقدم العميل غير موجود أو العميل غير نشط.")
    if refund_date < receipt["receipt_date"]:
        raise ValueError("لا يمكن رد مقدم العميل بتاريخ يسبق تاريخ استلامه.")
    available = _customer_advance_available(conn, advance_receipt_id)
    if amount > available:
        raise ValueError(f"قيمة الرد تتجاوز المتبقي من مقدم العميل ({available:.2f}).")
    _ensure_unique_customer_cash_reference(conn, reference)
    _customer_cash_account(conn, cash_account_id)
    advance_account = _customer_advances_account(conn)
    refund_no = "CADVR-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    dimensions = validate_journal_dimensions(conn, {"customer_id": receipt["customer_id"]})
    journal_id = post_journal(
        conn, refund_date, refund_no,
        "رد جزء من مقدم العميل " + receipt["customer_name"],
        [(advance_account, amount, 0, "تخفيض التزام مقدم العميل"),
         (cash_account_id, 0, amount, "رد نقدي/بنكي للعميل")],
        created_by, dimensions=dimensions,
    )
    conn.execute(
        """INSERT INTO customer_advance_refunds
           (refund_no,advance_receipt_id,refund_date,amount,cash_account_id,journal_entry_id,reference,created_by)
           VALUES(?,?,?,?,?,?,?,?)""",
        (refund_no, advance_receipt_id, refund_date, str(amount), cash_account_id,
         journal_id, reference, created_by),
    )
    return {"refund_no": refund_no, "journal_entry_id": journal_id,
            "amount": amount, "remaining_advance": available - amount}




@sales_bp.route("/advances")
@require("sales.view")
def customer_advances():
    c = db()
    customers = c.execute(
        "SELECT id,code,name FROM customers WHERE active=1 ORDER BY name"
    ).fetchall()
    from activity_profiles import active_account_role_ids
    cash_account_ids = sorted(active_account_role_ids(c).get("cash", set()))
    cash_accounts = []
    if cash_account_ids:
        placeholders = ",".join("?" for _ in cash_account_ids)
        cash_accounts = c.execute(
            f"""SELECT id,code,name_ar FROM accounts
                WHERE id IN ({placeholders}) AND active=1 AND is_group=0
                  AND account_type='asset' ORDER BY code""",
            tuple(cash_account_ids),
        ).fetchall()

    allocated_by_receipt = {}
    for row in c.execute(
        """SELECT a.advance_receipt_id,a.amount FROM customer_advance_allocations a
           WHERE NOT EXISTS (SELECT 1 FROM customer_advance_reversals x
             WHERE x.source_type='allocation' AND x.source_id=a.journal_entry_id)"""
    ).fetchall():
        allocated_by_receipt.setdefault(row["advance_receipt_id"], Decimal("0"))
        allocated_by_receipt[row["advance_receipt_id"]] += Decimal(str(row["amount"] or "0"))
    refunded_by_receipt = {}
    for row in c.execute(
        """SELECT r.advance_receipt_id,r.amount FROM customer_advance_refunds r
           WHERE NOT EXISTS (SELECT 1 FROM customer_advance_reversals x
             WHERE x.source_type='refund' AND x.source_id=r.id)"""
    ).fetchall():
        refunded_by_receipt.setdefault(row["advance_receipt_id"], Decimal("0"))
        refunded_by_receipt[row["advance_receipt_id"]] += Decimal(str(row["amount"] or "0"))

    refunds_by_invoice = {}
    for row in c.execute(
        """SELECT cn.invoice_id,rf.amount FROM customer_refunds rf
           JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id
           WHERE cn.status='posted'"""
    ).fetchall():
        refunds_by_invoice.setdefault(row["invoice_id"], Decimal("0"))
        refunds_by_invoice[row["invoice_id"]] += Decimal(str(row["amount"] or "0"))

    open_invoices_by_customer = {}
    for invoice in c.execute(
        "SELECT * FROM sales_invoices WHERE status='posted' ORDER BY invoice_date,id"
    ).fetchall():
        outstanding = (Decimal(str(invoice["total_amount"])) -
                       Decimal(str(invoice["paid_amount"] or "0")) -
                       Decimal(str(invoice["credited_amount"] or "0")) +
                       refunds_by_invoice.get(invoice["id"], Decimal("0")))
        if outstanding > 0:
            open_invoices_by_customer.setdefault(invoice["customer_id"], []).append({
                "id": invoice["id"], "invoice_no": invoice["invoice_no"],
                "invoice_date": invoice["invoice_date"], "outstanding": outstanding,
            })

    advances = []
    for row in c.execute(
        """SELECT r.*,cu.name customer_name,cu.code customer_code
           FROM customer_advance_receipts r
           JOIN customers cu ON cu.id=r.customer_id
           ORDER BY r.id DESC"""
    ).fetchall():
        item = dict(row)
        item["remaining_amount"] = (
            Decimal("0") if c.execute(
                "SELECT 1 FROM customer_advance_reversals WHERE source_type='receipt' AND source_id=?",
                (row["id"],),
            ).fetchone() else
            Decimal(str(row["amount"])) -
            allocated_by_receipt.get(row["id"], Decimal("0")) -
            refunded_by_receipt.get(row["id"], Decimal("0"))
        )
        item["receipt_reversal"] = c.execute(
            "SELECT reversal_date,reason,reversal_journal_entry_id FROM customer_advance_reversals WHERE source_type='receipt' AND source_id=?",
            (row["id"],),
        ).fetchone()
        item["reversed"] = item["receipt_reversal"] is not None
        item["allocation_batches"] = c.execute(
            """SELECT a.journal_entry_id,MIN(a.allocation_date) allocation_date,
                      SUM(CAST(a.amount AS NUMERIC)) amount,COUNT(*) invoice_count,
                      GROUP_CONCAT(i.invoice_no, ', ') invoice_numbers,
                      x.reversal_journal_entry_id,x.reversal_date,x.reason
               FROM customer_advance_allocations a
               JOIN sales_invoices i ON i.id=a.invoice_id
               LEFT JOIN customer_advance_reversals x
                 ON x.source_type='allocation' AND x.source_id=a.journal_entry_id
               WHERE a.advance_receipt_id=?
               GROUP BY a.journal_entry_id,x.reversal_journal_entry_id
               ORDER BY a.journal_entry_id DESC""",
            (row["id"],),
        ).fetchall()
        item["refunds"] = c.execute(
            """SELECT r.*,x.reversal_journal_entry_id,x.reversal_date,x.reason FROM customer_advance_refunds r
               LEFT JOIN customer_advance_reversals x
                 ON x.source_type='refund' AND x.source_id=r.id
               WHERE r.advance_receipt_id=? ORDER BY r.id DESC""",
            (row["id"],),
        ).fetchall()
        item["open_invoices"] = open_invoices_by_customer.get(row["customer_id"], [])
        advances.append(item)
    c.close()
    return render_template(
        "customer_advances.html", customers=customers, cash_accounts=cash_accounts,
        advances=advances, today=date.today().isoformat(),
    )

@sales_bp.route("/advances/receipt", methods=["POST"])
@require("sales.manage")
def create_customer_advance_receipt():
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        result = post_customer_advance_receipt(
            c, request.form.get("customer_id", type=int),
            request.form.get("receipt_date", ""), request.form.get("amount", ""),
            request.form.get("cash_account_id", type=int), session.get("user_id"),
            request.form.get("reference", ""),
        )
        c.commit()
        flash("تم تسجيل مقدم العميل وترحيل قيده بنجاح: " + result["receipt_no"], "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("sales.customer_advances"))


@sales_bp.route("/advances/<int:advance_id>/allocate", methods=["POST"])
@require("sales.manage")
def allocate_customer_advance_route(advance_id):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        selected_ids = request.form.getlist("invoice_ids")
        if selected_ids:
            allocations = [
                (int(invoice_id), request.form.get("allocation_amount_" + invoice_id, ""))
                for invoice_id in selected_ids
            ]
        else:
            # Backward compatibility for an older single-invoice form.
            allocations = [(
                request.form.get("invoice_id", type=int),
                request.form.get("amount", ""),
            )]
        result = allocate_customer_advance(
            c, advance_id, allocations,
            request.form.get("allocation_date", ""), session.get("user_id"),
        )
        c.commit()
        flash(
            f"تم تخصيص {result['total_amount']:.2f} على {result['invoice_count']} فاتورة بنجاح.",
            "success",
        )
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("sales.customer_advances"))


@sales_bp.route("/advances/reversals/<string:source_type>/<int:source_id>", methods=["POST"])
@require("sales.manage")
def reverse_customer_advance_route(source_type, source_id):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        result = reverse_customer_advance_transaction(
            c, source_type, source_id,
            request.form.get("reversal_date", ""),
            request.form.get("reason", ""), session.get("user_id"),
        )
        c.commit()
        g.audit_created_record = (
            "customer_advance_reversals", result["reversal_id"]
        )
        flash("تم عكس العملية بقيد عكسي مرتبط دون حذف المستند أو القيد الأصلي.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("sales.customer_advances"))


@sales_bp.route("/advances/<int:advance_id>/refund", methods=["POST"])
@require("sales.manage")
def refund_customer_advance_route(advance_id):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        result = post_customer_advance_refund(
            c, advance_id, request.form.get("refund_date", ""),
            request.form.get("amount", ""),
            request.form.get("cash_account_id", type=int), session.get("user_id"),
            request.form.get("reference", ""),
        )
        c.commit()
        flash("تم رد مقدم العميل وترحيل القيد: " + result["refund_no"], "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("sales.customer_advances"))


@sales_bp.route("/")
@require("sales.view")
def index():
    c = db()
    customers = c.execute("SELECT * FROM customers ORDER BY name").fetchall()
    invoice_page = max(1, request.args.get("invoice_page", 1, type=int) or 1)
    invoice_per_page = request.args.get("invoice_per_page", 50, type=int) or 50
    if invoice_per_page not in (50, 100):
        invoice_per_page = 50
    invoice_total = c.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0]
    invoice_pages = max(1, (invoice_total + invoice_per_page - 1) // invoice_per_page)
    invoice_page = min(invoice_page, invoice_pages)
    invoices = c.execute("""SELECT i.*,c.name customer_name FROM sales_invoices i
        JOIN customers c ON c.id=i.customer_id ORDER BY i.id DESC LIMIT ? OFFSET ?""",
        (invoice_per_page, (invoice_page - 1) * invoice_per_page)).fetchall()
    enriched_invoices = []
    for invoice in invoices:
        item = dict(invoice)
        refunded = _refund_total(c, invoice["id"])
        item["remaining_amount"] = (Decimal(invoice["total_amount"]) - Decimal(invoice["paid_amount"] or "0")
                                    - Decimal(invoice["credited_amount"] or "0") + Decimal(refunded or "0"))
        item["paid_positive"] = Decimal(invoice["paid_amount"] or "0") > 0
        item["creditable"] = Decimal(invoice["total_amount"]) > Decimal(invoice["credited_amount"] or "0")
        item["closed_by_credit"] = item["remaining_amount"] <= 0 and Decimal(invoice["credited_amount"] or "0") > 0 and Decimal(invoice["paid_amount"] or "0") < Decimal(invoice["total_amount"])
        enriched_invoices.append(item)
    receipts_page = fetch_page(c,
        """SELECT r.*,i.invoice_no,c.name customer_name FROM customer_receipts r
           JOIN sales_invoices i ON i.id=r.invoice_id JOIN customers c ON c.id=i.customer_id
           ORDER BY r.id DESC""",
        "SELECT COUNT(*) FROM customer_receipts",
        page=request.args.get("receipts_page",1,type=int),
        per_page=request.args.get("receipts_per_page",50,type=int))
    batch_receipts_page = fetch_page(c,
        """SELECT b.*,cu.name customer_name,COUNT(a.id) invoice_count,
              GROUP_CONCAT(i.invoice_no || ' (' || a.amount || ')', ', ') invoice_numbers
           FROM customer_receipt_batches b JOIN customers cu ON cu.id=b.customer_id
           LEFT JOIN customer_receipt_allocations a ON a.batch_id=b.id
           LEFT JOIN sales_invoices i ON i.id=a.invoice_id
           GROUP BY b.id ORDER BY b.id DESC""",
        "SELECT COUNT(*) FROM customer_receipt_batches",
        page=request.args.get("batch_receipts_page",1,type=int),
        per_page=request.args.get("batch_receipts_per_page",50,type=int))
    credit_notes_page = fetch_page(c,
        """SELECT n.*,i.invoice_no,c.name customer_name FROM sales_credit_notes n
           JOIN sales_invoices i ON i.id=n.invoice_id JOIN customers c ON c.id=i.customer_id
           ORDER BY n.id DESC""",
        "SELECT COUNT(*) FROM sales_credit_notes",
        page=request.args.get("credit_notes_page",1,type=int),
        per_page=request.args.get("credit_notes_per_page",50,type=int))
    refunds_page = fetch_page(c,
        """SELECT rf.*,cn.credit_note_no,i.invoice_no,cu.name customer_name,a.code cash_account_code
           FROM customer_refunds rf JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id
           JOIN sales_invoices i ON i.id=cn.invoice_id JOIN customers cu ON cu.id=i.customer_id
           JOIN accounts a ON a.id=rf.cash_account_id ORDER BY rf.id DESC""",
        "SELECT COUNT(*) FROM customer_refunds",
        page=request.args.get("refunds_page",1,type=int),
        per_page=request.args.get("refunds_per_page",50,type=int))
    receipts = receipts_page["rows"]
    batch_receipts = batch_receipts_page["rows"]
    credit_notes = credit_notes_page["rows"]
    refunds = refunds_page["rows"]
    enriched_notes = []
    for note in credit_notes:
        note_refunds = c.execute("SELECT amount FROM customer_refunds WHERE credit_note_id=?", (note["id"],)).fetchall()
        refunded = sum((Decimal(row["amount"]) for row in note_refunds), Decimal("0"))
        item = dict(note)
        item["refunded_amount"] = refunded
        item["refundable_remaining"] = max(Decimal("0"), Decimal(note["refundable_amount"] or "0") - refunded)
        enriched_notes.append(item)
    c.close()
    return render_template("sales.html", customers=customers, invoices=enriched_invoices,
                           receipts=receipts, batch_receipts=batch_receipts, credit_notes=enriched_notes, refunds=refunds,
                           invoice_page=invoice_page, invoice_pages=invoice_pages,
                           invoice_per_page=invoice_per_page, invoice_total=invoice_total,
                           receipts_page=receipts_page,batch_receipts_page=batch_receipts_page,
                           credit_notes_page=credit_notes_page,refunds_page=refunds_page,
                           today=date.today().isoformat())


def _customer_discount_input(form):
    discount_type = str(form.get("discount_type", "none") or "none").strip().lower()
    if discount_type not in {"none", "percent", "fixed"}:
        raise ValueError("اختر نوع خصم صحيحًا.")
    try:
        value = Decimal(str(form.get("discount_value", "0") or "0")).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("قيمة الخصم غير صحيحة.")
    if not value.is_finite() or value < 0:
        raise ValueError("قيمة الخصم يجب أن تكون صفرًا أو أكبر.")
    if discount_type == "percent" and value > 100:
        raise ValueError("نسبة الخصم يجب ألا تتجاوز 100%.")
    if discount_type == "none":
        value = Decimal("0.00")
    return discount_type, value


@sales_bp.route("/customers", methods=["GET", "POST"])
@require("sales.manage")
def customer_list():
    c = db()
    if request.method == "POST":
        try:
            credit_limit = Decimal(request.form.get("credit_limit", "0") or "0")
            discount_type, discount_value = _customer_discount_input(request.form)
            if not credit_limit.is_finite() or credit_limit < 0: raise ValueError("الحد الائتماني يجب أن يكون صفرًا أو أكبر.")
            c.execute("""INSERT INTO customers(code,name,tax_number,phone,email,address,credit_limit,discount_type,discount_value)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (request.form["code"].strip(), request.form["name"].strip(),
                       request.form.get("tax_number", "").strip(), request.form.get("phone", "").strip(),
                       request.form.get("email", "").strip(), request.form.get("address", "").strip(),
                       str(credit_limit), discount_type, str(discount_value)))
            c.commit()
            flash("تم حفظ العميل.", "success")
        except sqlite3.IntegrityError:
            c.rollback()
            flash("كود العميل مستخدم بالفعل.", "error")
        except (ValueError, InvalidOperation) as exc:
            c.rollback()
            flash(str(exc), "error")
    customers_page = fetch_page(c,"SELECT * FROM customers ORDER BY name",
        "SELECT COUNT(*) FROM customers",
        page=request.args.get("customers_page",1,type=int),
        per_page=request.args.get("customers_per_page",50,type=int))
    rows = customers_page["rows"]
    invoices_by_customer = {}
    for balance_row in c.execute("""WITH refunds_by_invoice AS (
            SELECT cn.invoice_id, COALESCE(DECIMAL_SUM(r.amount),'0') refunded
            FROM sales_credit_notes cn JOIN customer_refunds r ON r.credit_note_id=cn.id
            GROUP BY cn.invoice_id
        )
        SELECT i.customer_id,
            COALESCE(DECIMAL_SUM(i.total_amount),'0') total_amount,
            COALESCE(DECIMAL_SUM(i.paid_amount),'0') paid_amount,
            COALESCE(DECIMAL_SUM(i.credited_amount),'0') credited_amount,
            COALESCE(DECIMAL_SUM(COALESCE(r.refunded,'0')),'0') refunded_amount
        FROM sales_invoices i LEFT JOIN refunds_by_invoice r ON r.invoice_id=i.id
        WHERE i.status='posted' GROUP BY i.customer_id"""):
        invoices_by_customer[balance_row["customer_id"]] = (
            Decimal(balance_row["total_amount"]) - Decimal(balance_row["paid_amount"])
            - Decimal(balance_row["credited_amount"]) + Decimal(balance_row["refunded_amount"])
        )
    rows = [dict(row, outstanding=invoices_by_customer.get(row["id"], Decimal("0"))) for row in rows]
    c.close()
    return render_template("sales_customers.html", rows=rows, customers_page=customers_page)


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
            discount_type, discount_value = _customer_discount_input(request.form)
            if not limit.is_finite() or limit<0: raise ValueError("الحد الائتماني يجب أن يكون صفرًا أو أكبر.")
            c.execute("""UPDATE customers SET code=?,name=?,tax_number=?,phone=?,email=?,address=?,credit_limit=?,
                         discount_type=?,discount_value=? WHERE id=?""",
              (request.form["code"].strip(),request.form["name"].strip(),request.form.get("tax_number","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("address","").strip(),str(limit),discount_type,str(discount_value),customer_id))
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


@sales_bp.route("/salespersons", methods=["GET", "POST"])
@require("sales.manage")
def salesperson_list():
    c = db()
    if request.method == "POST":
        try:
            code = request.form.get("code", "").strip().upper()
            name = request.form.get("name", "").strip()
            if not code or len(code) > 30 or not name or len(name) > 120:
                raise ValueError("أدخل كودًا واسمًا صحيحين لمندوب المبيعات.")
            c.execute("INSERT INTO salespersons(code,name) VALUES(?,?)", (code, name))
            c.commit()
            flash("تم إضافة مندوب المبيعات.", "success")
        except (ValueError, sqlite3.IntegrityError) as exc:
            c.rollback()
            flash(str(exc) or "كود مندوب المبيعات مستخدم بالفعل.", "error")
    rows = c.execute("SELECT * FROM salespersons ORDER BY code").fetchall()
    c.close()
    return render_template("sales_salespersons.html", rows=rows)


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
                        price_rules = c.execute("""SELECT id,unit_price,min_qty,valid_from FROM customer_price_rules
                          WHERE customer_id=? AND product_id=? AND active=1 AND valid_from<=?
                            AND (valid_to IS NULL OR valid_to>=?)""",
                          (int(request.form["customer_id"]), int(pid), request.form["invoice_date"],
                           request.form["invoice_date"])).fetchall()
                        matched_price = _select_customer_price_rule(price_rules, qty)
                        if matched_price is not None:
                            selected_price = matched_price
                    items.append({"product_id": int(pid) if pid else None, "quantity": qty,
                                  "unit_price": selected_price, "description": desc, "tax_rate": line_rate, "tax_treatment": treatment})
            c.execute("BEGIN IMMEDIATE")
            result = post_invoice(c, int(request.form["customer_id"]), request.form["invoice_date"],
                                  request.form.get("warehouse_id", type=int), items,
                                  request.form.get("tax_rate", "0"), session["user_id"],
                                  allow_credit_override=(session.get("role") == "admin" and request.form.get("credit_override") == "1"),
                                  dimensions=request.form,
                                  sales_channel=request.form.get("sales_channel", "default"))
            due_date = request.form.get("due_date") or request.form["invoice_date"]
            if due_date < request.form["invoice_date"]:
                raise ValueError("تاريخ الاستحقاق لا يمكن أن يسبق تاريخ الفاتورة.")
            c.execute("UPDATE sales_invoices SET due_date=? WHERE id=?", (due_date, result["invoice_id"]))
            c.commit()
            g.audit_created_record = ("sales_invoices", result["invoice_id"], "sales_invoice_lines", "invoice_id")
            c.close()
            flash("تم حفظ وترحيل فاتورة البيع " + result["invoice_no"], "success")
            if result.get("credit_warning"):
                flash("تنبيه ائتماني: تجاوز العميل الحد المسموح. الحد: " + str(result["credit_limit"]) +
                      " | المديونية السابقة: " + str(result["current_exposure"]) +
                      " | المديونية بعد الفاتورة: " + str(result["new_exposure"]), "warning")
            return redirect(url_for("sales.invoice_print", invoice_id=result["invoice_id"]))
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
    dimension_options = active_dimension_options(c)
    c.close()
    return render_template("sales_invoice_new.html", customers=customers, products=products,
                           warehouses=warehouses, settings=settings, dimension_options=dimension_options,
                           today=date.today().isoformat())



@sales_bp.route("/invoices/<int:invoice_id>/print")
@require("sales.view")
def invoice_print(invoice_id):
    c = db()
    invoice = c.execute("""SELECT i.*,cu.code customer_code,cu.name customer_name,
        cu.tax_number customer_tax_number FROM sales_invoices i
        JOIN customers cu ON cu.id=i.customer_id WHERE i.id=?""", (invoice_id,)).fetchone()
    if not invoice:
        c.close()
        abort(404)
    lines = c.execute("""SELECT l.*,p.sku,p.name_ar product_name
        FROM sales_invoice_lines l LEFT JOIN stock_products p ON p.id=l.product_id
        WHERE l.invoice_id=? ORDER BY l.id""", (invoice_id,)).fetchall()
    settings = {r["setting_key"]:r["setting_value"] for r in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','base_currency','tax_number','company_address','company_phone','company_email','company_website','company_logo_data')")}
    branch = None
    invoice_columns = {row["name"] for row in c.execute("PRAGMA table_info(sales_invoices)")}
    if "branch_id" in invoice_columns and c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='branches'"
    ).fetchone() and invoice["branch_id"]:
        branch_columns = {row["name"] for row in c.execute("PRAGMA table_info(branches)")}
        branch_fields = [
            column if column in branch_columns else "'' AS " + column
            for column in ("address", "phone", "email", "tax_number")
        ]
        branch = c.execute(
            "SELECT code,name_ar," + ",".join(branch_fields) + " FROM branches WHERE id=?",
            (invoice["branch_id"],),
        ).fetchone()
    c.close()
    return render_template("sales_invoice_print.html", invoice=invoice, lines=lines,
                           settings=settings, branch=branch)

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
            selected_ids = request.form.getlist("invoice_ids")
            if selected_ids:
                customer_id = int(request.form["customer_id"])
                allocations = []
                for raw_id in selected_ids:
                    invoice_id = int(raw_id)
                    amount = request.form.get("allocation_amount_" + raw_id, "").strip()
                    allocations.append((invoice_id, amount))
                result = post_customer_receipt_batch(
                    c, customer_id, request.form["receipt_date"], allocations,
                    int(request.form["cash_account_id"]), session["user_id"],
                    request.form.get("reference", "").strip()
                )
                c.commit()
                flash("تم تسجيل إيصال التحصيل المجمع " + result["receipt_no"] +
                      " وتوزيع " + str(result["total_amount"]) + " على " +
                      str(result["invoice_count"]) + " فاتورة.", "success")
            else:
                raise ValueError("حدد الفواتير التي تريد تحصيلها.")
            return redirect(url_for("sales.index"))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
    customers = c.execute("SELECT id,code,name FROM customers WHERE active=1 ORDER BY name").fetchall()
    raw_invoices = c.execute("""SELECT i.id,i.invoice_no,i.customer_id,i.invoice_date,i.due_date,
        i.total_amount,i.paid_amount,COALESCE(i.credited_amount,'0') credited_amount,c.name customer_name
        FROM sales_invoices i JOIN customers c ON c.id=i.customer_id
        WHERE i.status='posted' ORDER BY c.name,i.invoice_date,i.id""").fetchall()
    invoices = []
    for row in raw_invoices:
        outstanding = (Decimal(row["total_amount"]) - Decimal(row["paid_amount"] or "0")
                       - Decimal(row["credited_amount"] or "0") + _refund_total(c, row["id"]))
        if outstanding > 0:
            item = dict(row)
            item["remaining_amount"] = outstanding
            invoices.append(item)
    from activity_profiles import active_account_role_ids
    cash_account_ids = sorted(active_account_role_ids(c).get("cash", set()))
    cash_accounts = []
    if cash_account_ids:
        placeholders = ",".join("?" for _ in cash_account_ids)
        cash_accounts = c.execute(
            f"""SELECT id,code,name_ar FROM accounts
                WHERE id IN ({placeholders}) AND account_type='asset'
                  AND is_group=0 AND active=1 ORDER BY code""",
            tuple(cash_account_ids),
        ).fetchall()
    c.close()
    return render_template("sales_receipt_new.html", invoices=invoices, customers=customers,
                           cash_accounts=cash_accounts, today=date.today().isoformat())


@sales_bp.route("/receipts/single/<int:receipt_id>/print")
@require("sales.view")
def single_receipt_print(receipt_id):
    c = db()
    receipt = c.execute("""SELECT r.*,i.invoice_no,cu.code customer_code,cu.name customer_name,
        a.code cash_code,a.name_ar cash_name FROM customer_receipts r
        JOIN sales_invoices i ON i.id=r.invoice_id
        JOIN customers cu ON cu.id=i.customer_id
        JOIN accounts a ON a.id=r.cash_account_id WHERE r.id=?""", (receipt_id,)).fetchone()
    if not receipt:
        c.close()
        abort(404)
    settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','base_currency','tax_number','company_address','company_phone','company_email','company_website','company_logo_data')")}
    c.close()
    return render_template("sales_single_receipt_print.html", receipt=receipt,
                           settings=settings, branch=None)


@sales_bp.route("/receipts/<int:receipt_id>/print")
@require("sales.view")
def receipt_print(receipt_id):
    c = db()
    receipt = c.execute("""SELECT r.*,cu.code customer_code,cu.name customer_name,
        a.code cash_code,a.name_ar cash_name FROM customer_receipt_batches r
        JOIN customers cu ON cu.id=r.customer_id
        JOIN accounts a ON a.id=r.cash_account_id WHERE r.id=?""", (receipt_id,)).fetchone()
    if not receipt:
        c.close()
        abort(404)
    allocations = c.execute("""SELECT i.invoice_no,i.invoice_date,i.total_amount,
        a.amount FROM customer_receipt_allocations a
        JOIN sales_invoices i ON i.id=a.invoice_id WHERE a.batch_id=? ORDER BY a.id""",
        (receipt_id,)).fetchall()
    settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','base_currency','tax_number','company_address','company_phone','company_email','company_website','company_logo_data')")}
    c.close()
    return render_template("sales_receipt_print.html", receipt=receipt, allocations=allocations,
                           settings=settings, branch=None)






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
    from activity_profiles import active_account_role_ids
    cash_account_ids = sorted(active_account_role_ids(c).get("cash", set()))
    cash_accounts = []
    if cash_account_ids:
        placeholders = ",".join("?" for _ in cash_account_ids)
        cash_accounts = c.execute(
            f"""SELECT id,code,name_ar FROM accounts
                WHERE id IN ({placeholders}) AND account_type='asset'
                  AND is_group=0 AND active=1 ORDER BY code""",
            tuple(cash_account_ids),
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
