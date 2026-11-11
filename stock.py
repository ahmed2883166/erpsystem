"""NEXUS ERP inventory foundation (v0.2).

Implements product master, warehouses, stock receipts/issues/transfers/scrap,
lot/expiry tracking, non-negative stock validation and valuation layers.
Manufacturing orders and production conversion are intentionally excluded.
"""
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from datetime import date
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal, money

stock_bp = Blueprint("stock", __name__, url_prefix="/stock")
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
D = Decimal
QTY = D("0.0001")
MONEY = D("0.01")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def initialize_stock_schema():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = get_db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS stock_products(
      id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
      category TEXT NOT NULL DEFAULT '', product_type TEXT NOT NULL DEFAULT 'goods',
      uom TEXT NOT NULL DEFAULT 'قطعة', barcode TEXT UNIQUE, track_lot INTEGER NOT NULL DEFAULT 0,
      track_expiry INTEGER NOT NULL DEFAULT 0, min_qty TEXT NOT NULL DEFAULT '0',
      purchase_price TEXT NOT NULL DEFAULT '0', sale_price TEXT NOT NULL DEFAULT '0',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stock_categories(
      id INTEGER PRIMARY KEY, name_ar TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE,
      description TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stock_warehouses(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
      branch TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS stock_locations(
      id INTEGER PRIMARY KEY, warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      parent_id INTEGER REFERENCES stock_locations(id), code TEXT NOT NULL, name_ar TEXT NOT NULL,
      location_type TEXT NOT NULL CHECK(location_type IN ('floor','zone','rack','bin')),
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(warehouse_id,parent_id,code)
    );
    CREATE TABLE IF NOT EXISTS stock_product_locations(
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES stock_products(id),
      warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      location_id INTEGER NOT NULL REFERENCES stock_locations(id),
      is_default INTEGER NOT NULL DEFAULT 1,
      UNIQUE(product_id,warehouse_id)
    );
    CREATE TABLE IF NOT EXISTS stock_lots(
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES stock_products(id),
      lot_number TEXT NOT NULL, production_date TEXT, expiry_date TEXT,
      UNIQUE(product_id,lot_number)
    );
    CREATE TABLE IF NOT EXISTS stock_movements(
      id INTEGER PRIMARY KEY, movement_no TEXT NOT NULL UNIQUE, movement_type TEXT NOT NULL,
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      source_warehouse_id INTEGER REFERENCES stock_warehouses(id),
      target_warehouse_id INTEGER REFERENCES stock_warehouses(id),
      source_location_id INTEGER REFERENCES stock_locations(id),
      target_location_id INTEGER REFERENCES stock_locations(id),
      lot_id INTEGER REFERENCES stock_lots(id), quantity TEXT NOT NULL,
      unit_cost TEXT NOT NULL DEFAULT '0', total_cost TEXT NOT NULL DEFAULT '0',
      movement_date TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stock_valuation_layers(
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES stock_products(id),
      warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      location_id INTEGER REFERENCES stock_locations(id),
      lot_id INTEGER REFERENCES stock_lots(id), source_movement_id INTEGER NOT NULL REFERENCES stock_movements(id),
      qty_in TEXT NOT NULL, qty_remaining TEXT NOT NULL, unit_cost TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_stock_moves_product_date ON stock_movements(product_id,movement_date,id);
    CREATE INDEX IF NOT EXISTS idx_stock_layers_product_wh ON stock_valuation_layers(product_id,warehouse_id,id);
    """)
    # Additive migrations preserve all existing stock and product data.
    product_columns = {row["name"] for row in c.execute("PRAGMA table_info(stock_products)")}
    if "category_id" not in product_columns:
        c.execute("ALTER TABLE stock_products ADD COLUMN category_id INTEGER REFERENCES stock_categories(id)")
    for name in ("purchase_price", "sale_price"):
        if name not in product_columns:
            c.execute(f"ALTER TABLE stock_products ADD COLUMN {name} TEXT NOT NULL DEFAULT '0'")
    # Convert legacy free-text categories into selectable product groups without losing labels.
    legacy_categories = c.execute("SELECT DISTINCT TRIM(category) value FROM stock_products WHERE TRIM(COALESCE(category,''))<>'' AND category_id IS NULL ORDER BY 1").fetchall()
    for index, legacy in enumerate(legacy_categories, 1):
        label = legacy["value"]
        code = f"LEGACY-{index:04d}"
        c.execute("INSERT OR IGNORE INTO stock_categories(code,name_ar) VALUES(?,?)", (code, label))
        c.execute("UPDATE stock_products SET category_id=(SELECT id FROM stock_categories WHERE name_ar=?) WHERE TRIM(category)=? AND category_id IS NULL", (label, label))
    movement_columns = {row["name"] for row in c.execute("PRAGMA table_info(stock_movements)")}
    if "journal_entry_id" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN journal_entry_id INTEGER")
    if "accounting_status" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN accounting_status TEXT NOT NULL DEFAULT 'pending'")
    if "issue_purpose" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN issue_purpose TEXT")
    if "source_location_id" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN source_location_id INTEGER REFERENCES stock_locations(id)")
    if "target_location_id" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN target_location_id INTEGER REFERENCES stock_locations(id)")
    layer_columns = {row["name"] for row in c.execute("PRAGMA table_info(stock_valuation_layers)")}
    if "location_id" not in layer_columns:
        c.execute("ALTER TABLE stock_valuation_layers ADD COLUMN location_id INTEGER REFERENCES stock_locations(id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_locations_wh_parent ON stock_locations(warehouse_id,parent_id,active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_layers_location ON stock_valuation_layers(location_id,product_id)")
    # One summarized COGS journal may cover multiple stock issue movements.
    # The journal link is therefore intentionally non-unique.
    layer_columns = {row["name"] for row in c.execute("PRAGMA table_info(stock_valuation_layers)")}
    if "value_remaining" not in layer_columns:
        c.execute("ALTER TABLE stock_valuation_layers ADD COLUMN value_remaining TEXT NOT NULL DEFAULT ''")
        # Backfill legacy layers using Decimal in Python, avoiding SQLite REAL arithmetic.
        for layer in c.execute("SELECT id,qty_remaining,unit_cost FROM stock_valuation_layers").fetchall():
            value = D(str(layer["qty_remaining"])) * D(str(layer["unit_cost"]))
            c.execute("UPDATE stock_valuation_layers SET value_remaining=? WHERE id=?",
                      (format(value, "f"), layer["id"]))
    # For untouched layers, the originating movement's exact total is authoritative.
    # This also repairs legacy return layers whose unit_cost had been rounded to four places.
    for layer in c.execute("""SELECT l.id,l.qty_in,l.qty_remaining,m.total_cost
        FROM stock_valuation_layers l JOIN stock_movements m ON m.id=l.source_movement_id""").fetchall():
        if (D(str(layer["qty_in"])) == D(str(layer["qty_remaining"]))
            and layer["total_cost"] not in (None, "")):
            c.execute("UPDATE stock_valuation_layers SET value_remaining=? WHERE id=?",
                      (str(D(str(layer["total_cost"]))), layer["id"]))
    c.execute("DROP INDEX IF EXISTS idx_stock_movement_journal")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_movement_journal ON stock_movements(journal_entry_id) WHERE journal_entry_id IS NOT NULL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS stocktakes(
      id INTEGER PRIMARY KEY, count_no TEXT NOT NULL UNIQUE,
      warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      count_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      approved_by INTEGER, approved_at TEXT, journal_entry_id INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stocktake_lines(
      id INTEGER PRIMARY KEY, stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      snapshot_qty TEXT NOT NULL, counted_qty TEXT, variance_qty TEXT,
      counted_unit_cost TEXT NOT NULL DEFAULT '0', variance_value TEXT NOT NULL DEFAULT '0',
      notes TEXT NOT NULL DEFAULT '', UNIQUE(stocktake_id,product_id)
    );
    CREATE INDEX IF NOT EXISTS idx_stocktake_lines_header ON stocktake_lines(stocktake_id,product_id);
    """)
    c.close()


def post_scrap_journal(c, movement_id, movement_no, product, movement_date, total_cost, created_by):
    """Post an approved scrap movement only when explicit chart accounts exist."""
    existing = c.execute("SELECT journal_entry_id FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    if existing and existing["journal_entry_id"]:
        return existing["journal_entry_id"]
    settings = {row["setting_key"]: row["setting_value"] for row in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key='activity'") }
    activity = settings.get("activity", "commercial")
    inventory_codes = {
        "commercial": "1150", "industrial": "1130", "medical": "1150",
        "restaurant": "1140", "service": None,
    }
    inventory_code = inventory_codes.get(activity)
    if not inventory_code:
        c.execute("UPDATE stock_movements SET accounting_status='pending_setup' WHERE id=?", (movement_id,))
        return None
    inventory = c.execute("SELECT id,is_group,active FROM accounts WHERE code=?", (inventory_code,)).fetchone()
    # Require explicit chart mappings; never create accounts during posting.
    writeoff = c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code='6990'").fetchone()
    writeoff_id = writeoff["id"] if writeoff and not writeoff["is_group"] and writeoff["active"] and writeoff["account_type"]=="expense" else None
    if not inventory or inventory["is_group"] or not inventory["active"] or not writeoff_id:
        raise ValueError("لا يمكن ترحيل الإعدام قبل ضبط حساب المخزون وحساب خسائر التالف 6990 كحساب مصروف تفصيلي نشط.")
    amount = money(D(str(total_cost)))
    if amount <= 0:
        c.execute("UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        return None
    entry_id = post_journal(c,movement_date,movement_no,
        f"قيد آلي لإعدام مخزون - {product['name_ar']}",
        [(writeoff_id,amount,0,"خسائر تالف مخزون"),
         (inventory["id"],0,amount,"تخفيض قيمة المخزون")],created_by)
    c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?", (entry_id,movement_id))
    return entry_id


def post_issue_journal(c, movement_id, movement_no, product, movement_date, total_cost, created_by, purpose):
    """Create the GL entry for an issue using the valuation cost consumed in this transaction."""
    existing = c.execute("SELECT journal_entry_id FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    if existing and existing["journal_entry_id"]:
        return existing["journal_entry_id"]
    settings = {row["setting_key"]: row["setting_value"] for row in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key='activity'")}
    activity = settings.get("activity", "commercial")
    cogs_codes = {"commercial":"5100", "medical":"5300", "restaurant":"5100", "industrial":"5400"}
    if purpose == "sale":
        expense_code = cogs_codes.get(activity)
        expense_name = "تكلفة المبيعات / تكلفة الخدمات المصروفة"
    elif purpose == "internal":
        expense_code = "6991"
        expense_name = "مصروف استخدام داخلي من المخزون"
        existing_expense = c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (expense_code,)).fetchone()
        expense_id = existing_expense["id"] if existing_expense and not existing_expense["is_group"] and existing_expense["active"] and existing_expense["account_type"]=="expense" else None
    else:
        c.execute("UPDATE stock_movements SET accounting_status='pending_purpose' WHERE id=?", (movement_id,))
        return None
    if purpose == "sale":
        expense = c.execute("SELECT id,is_group,active FROM accounts WHERE code=?", (expense_code,)).fetchone() if expense_code else None
        expense_id = expense["id"] if expense and not expense["is_group"] and expense["active"] else None
    inventory_codes = {"commercial":"1150", "industrial":"1130", "medical":"1150", "restaurant":"1140"}
    inventory_code = inventory_codes.get(activity)
    inventory = c.execute("SELECT id,is_group,active FROM accounts WHERE code=?", (inventory_code,)).fetchone() if inventory_code else None
    if not expense_id or not inventory or inventory["is_group"] or not inventory["active"]:
        raise ValueError("لا يمكن ترحيل حركة الصرف قبل ضبط حساب المصروف وحساب المخزون كحسابين تفصيليين نشطين بالنوع الصحيح.")
    amount = money(D(str(total_cost)))
    if amount <= 0:
        c.execute("UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        return None
    entry_id = post_journal(c,movement_date,movement_no,
        f"قيد آلي لصرف مخزون - {product['name_ar']}",
        [(expense_id,amount,0,expense_name),
         (inventory["id"],0,amount,"تخفيض المخزون بتكلفة الصرف")],created_by)
    c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?", (entry_id, movement_id))
    return entry_id


def stock_permission(view_name):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            role = session.get("role")
            allowed = (view_name in set(session["permissions"])) if "permissions" in session else (role == "admin" or (role == "accountant" and view_name in {"stock.manage","stock.view"}) or (role == "viewer" and view_name == "stock.view"))
            if not allowed:
                flash("ليس لديك صلاحية لهذه العملية.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco


def dec(value, label):
    try:
        n = D(str(value))
        if not n.is_finite():
            raise InvalidOperation()
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"قيمة {label} غير صحيحة.")


def q4(n):
    return n.quantize(QTY, rounding=ROUND_HALF_UP)



def current_qty(c, product_id, warehouse_id, lot_id=None):
    sql = """SELECT COALESCE(SUM(CAST(quantity AS REAL) *
      CASE WHEN movement_type='receipt' THEN 1 WHEN movement_type IN ('issue','scrap') THEN -1
      WHEN movement_type='transfer' AND target_warehouse_id=? THEN 1
      WHEN movement_type='transfer' AND source_warehouse_id=? THEN -1 ELSE 0 END),0) q
      FROM stock_movements WHERE product_id=?"""
    row = c.execute(sql, (warehouse_id, warehouse_id, product_id)).fetchone()
    return D(str(row["q"] or 0))


def available_layers(c, product_id, warehouse_id, lot_id=None, location_id=None):
    sql = """SELECT * FROM stock_valuation_layers WHERE product_id=? AND warehouse_id=?
             AND CAST(qty_remaining AS REAL)>0"""
    args = [product_id, warehouse_id]
    if lot_id:
        sql += " AND lot_id=?"; args.append(lot_id)
    if location_id:
        sql += " AND location_id=?"; args.append(location_id)
    return c.execute(sql + " ORDER BY id", args).fetchall()


def consume_layers(c, product_id, warehouse_id, qty, valuation_method, lot_id=None, location_id=None):
    layers = available_layers(c, product_id, warehouse_id, lot_id, location_id)
    available = sum((D(x["qty_remaining"]) for x in layers), D("0"))
    if available < qty:
        raise ValueError(f"الرصيد غير كافٍ. المتاح {q4(available)} والمطلوب {q4(qty)}.")
    total_value = sum((
        D(x["value_remaining"]) if x["value_remaining"] not in (None, "") else
        D(x["qty_remaining"]) * D(x["unit_cost"]) for x in layers
    ), D("0"))
    avg_cost = total_value / available if available else D("0")
    remaining, cost_total, allocations = qty, D("0"), []
    for layer in layers:
        if remaining <= 0: break
        layer_qty = D(layer["qty_remaining"])
        take = min(layer_qty, remaining)
        layer_value = (D(layer["value_remaining"]) if layer["value_remaining"] not in (None, "")
                       else layer_qty * D(layer["unit_cost"]))
        if take == layer_qty:
            cost = layer_value
        elif valuation_method == "average":
            cost = money(take * avg_cost)
        else:
            cost = money(layer_value * take / layer_qty)
        unit_cost = cost / take if take else D("0")
        new_remaining = layer_qty - take
        new_value = layer_value - cost
        c.execute("UPDATE stock_valuation_layers SET qty_remaining=?,value_remaining=? WHERE id=?",
                  (str(q4(new_remaining)), format(new_value, "f"), layer["id"]))
        allocations.append((layer["lot_id"], take, unit_cost, cost))
        cost_total += cost
        remaining -= take
    return money(cost_total), allocations


def make_lot(c, product, lot_number, production_date, expiry_date, as_of=None):
    if not product["track_lot"] and not product["track_expiry"]:
        return None
    if not lot_number:
        raise ValueError("هذا الصنف يتطلب رقم تشغيلة.")
    if product["track_expiry"] and not expiry_date:
        raise ValueError("تاريخ الصلاحية مطلوب لهذا الصنف.")
    reference_date = date.fromisoformat(as_of) if as_of else date.today()
    production = None
    if production_date:
        try:
            production = date.fromisoformat(production_date)
        except ValueError:
            raise ValueError("تاريخ الإنتاج غير صحيح.")
        if production > reference_date:
            raise ValueError("تاريخ الإنتاج لا يمكن أن يكون بعد تاريخ الاستلام.")
    if expiry_date:
        try:
            exp = date.fromisoformat(expiry_date)
        except ValueError:
            raise ValueError("تاريخ الصلاحية غير صحيح.")
        if exp < reference_date:
            raise ValueError("لا يمكن استلام تشغيلة منتهية الصلاحية.")
        if production and exp < production:
            raise ValueError("تاريخ الصلاحية لا يمكن أن يسبق تاريخ الإنتاج.")
    c.execute("""INSERT OR IGNORE INTO stock_lots(product_id,lot_number,production_date,expiry_date)
                 VALUES(?,?,?,?)""", (product["id"],lot_number,production_date or None,expiry_date or None))
    lot = c.execute("SELECT * FROM stock_lots WHERE product_id=? AND lot_number=?", (product["id"],lot_number)).fetchone()
    if lot["expiry_date"] != (expiry_date or None) or lot["production_date"] != (production_date or None):
        raise ValueError("رقم التشغيلة موجود بتواريخ إنتاج أو صلاحية مختلفة.")
    return lot["id"]


@stock_bp.route("/")
@stock_permission("stock.view")
def index():
    c=get_db()
    products=c.execute("SELECT COUNT(*) n FROM stock_products WHERE active=1").fetchone()["n"]
    warehouses=c.execute("SELECT COUNT(*) n FROM stock_warehouses WHERE active=1").fetchone()["n"]
    rows=c.execute("""SELECT p.id,p.sku,p.name_ar,p.uom,
      COALESCE(SUM(CAST(l.qty_remaining AS REAL)),0) qty
      FROM stock_products p LEFT JOIN stock_valuation_layers l ON l.product_id=p.id
      WHERE p.active=1 GROUP BY p.id ORDER BY p.name_ar LIMIT 10""").fetchall()
    c.close()
    return render_template("stock_dashboard.html",products=products,warehouses=warehouses,rows=rows)


@stock_bp.route("/products", methods=["GET","POST"])
@stock_permission("stock.manage")
def products():
    c=get_db()
    categories=c.execute("SELECT id,code,name_ar FROM stock_categories WHERE active=1 ORDER BY name_ar").fetchall()
    if request.method=="POST":
        try:
            sku=request.form.get("sku","").strip()
            name=request.form.get("name_ar","").strip()
            uom=request.form.get("uom","قطعة").strip()
            category_id=request.form.get("category_id",type=int)
            category=c.execute("SELECT name_ar FROM stock_categories WHERE id=? AND active=1",(category_id,)).fetchone() if category_id else None
            if not sku or not name or not uom: raise ValueError("الكود والاسم ووحدة القياس حقول مطلوبة.")
            if category_id and not category: raise ValueError("مجموعة الصنف غير صحيحة.")
            purchase_price=money(dec(request.form.get("purchase_price","0"),"سعر الشراء"))
            sale_price=money(dec(request.form.get("sale_price","0"),"سعر البيع"))
            if purchase_price<0 or sale_price<0: raise ValueError("أسعار الشراء والبيع لا يمكن أن تكون سالبة.")
            c.execute("""INSERT INTO stock_products(sku,name_ar,category,category_id,product_type,uom,barcode,track_lot,track_expiry,min_qty,purchase_price,sale_price)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(sku,name,category["name_ar"] if category else "",category_id,
              request.form.get("product_type","goods"),uom,request.form.get("barcode","").strip() or None,
              int(request.form.get("track_lot")=="on"),int(request.form.get("track_expiry")=="on"),
              str(q4(dec(request.form.get("min_qty","0"),"الحد الأدنى"))),str(purchase_price),str(sale_price)))
            c.commit(); flash("تم حفظ الصنف.", "success")
        except (ValueError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود الصنف أو الباركود مستخدم بالفعل.", "error")
        finally: c.close()
        return redirect(url_for("stock.products"))
    q=request.args.get("q","").strip()
    category_filter=request.args.get("category_id","").strip()
    sql="SELECT p.*,sc.name_ar category_name FROM stock_products p LEFT JOIN stock_categories sc ON sc.id=p.category_id WHERE 1=1"
    args=[]
    if q:
        sql+=" AND (p.sku LIKE ? OR p.name_ar LIKE ? OR p.barcode LIKE ?)"
        args.extend([f"%{q}%"]*3)
    if category_filter.isdigit():
        sql+=" AND p.category_id=?";args.append(int(category_filter))
    rows=c.execute(sql+" ORDER BY p.sku",args).fetchall(); c.close()
    return render_template("stock_products.html",rows=rows,categories=categories,search=q,category_filter=category_filter)


@stock_bp.route("/categories", methods=["GET","POST"])
@stock_permission("stock.manage")
def categories():
    c=get_db()
    if request.method=="POST":
        try:
            category_id=request.form.get("category_id",type=int)
            name=request.form.get("name_ar","").strip()
            code=request.form.get("code","").strip().upper()
            description=request.form.get("description","").strip()
            if not name or not code: raise ValueError("كود واسم المجموعة مطلوبان.")
            if category_id:
                c.execute("UPDATE stock_categories SET code=?,name_ar=?,description=? WHERE id=?",(code,name,description,category_id))
            else:
                c.execute("INSERT INTO stock_categories(code,name_ar,description) VALUES(?,?,?)",(code,name,description))
            c.commit();flash("تم حفظ مجموعة المنتجات.","success")
        except (ValueError,sqlite3.IntegrityError) as e:
            c.rollback();flash(str(e) if isinstance(e,ValueError) else "كود المجموعة أو اسمها مستخدم بالفعل.","error")
        finally:c.close()
        return redirect(url_for("stock.categories"))
    rows=c.execute("""SELECT sc.*,COUNT(p.id) product_count FROM stock_categories sc
        LEFT JOIN stock_products p ON p.category_id=sc.id GROUP BY sc.id ORDER BY sc.name_ar""").fetchall()
    c.close();return render_template("stock_categories.html",rows=rows)


@stock_bp.route("/categories/<int:category_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_category(category_id):
    c=get_db()
    try:
        used=c.execute("SELECT COUNT(*) n FROM stock_products WHERE category_id=?",(category_id,)).fetchone()["n"]
        if used: raise ValueError("لا يمكن حذف المجموعة لوجود أصناف مرتبطة بها؛ انقل الأصناف أولًا.")
        c.execute("DELETE FROM stock_categories WHERE id=?",(category_id,));c.commit();flash("تم حذف المجموعة.","success")
    except (ValueError,sqlite3.IntegrityError) as e:
        c.rollback();flash(str(e),"error")
    finally:c.close()
    return redirect(url_for("stock.categories"))

@stock_bp.route("/warehouses", methods=["GET","POST"])
@stock_permission("stock.manage")
def warehouses():
    c=get_db()
    if request.method=="POST":
        try:
            code=request.form.get("code","").strip(); name=request.form.get("name_ar","").strip()
            if not code or not name: raise ValueError("كود واسم المخزن مطلوبان.")
            c.execute("INSERT INTO stock_warehouses(code,name_ar,branch) VALUES(?,?,?)",(code,name,request.form.get("branch","").strip()))
            c.commit(); flash("تم حفظ المخزن.", "success")
        except (ValueError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود المخزن مستخدم بالفعل.", "error")
        finally: c.close()
        return redirect(url_for("stock.warehouses"))
    rows=c.execute("SELECT * FROM stock_warehouses ORDER BY code").fetchall(); c.close()
    return render_template("stock_warehouses.html",rows=rows)


@stock_bp.route("/locations", methods=["GET", "POST"])
@stock_permission("stock.manage")
def locations():
    c = get_db()
    if request.method == "POST":
        try:
            warehouse_id = request.form.get("warehouse_id", type=int)
            parent_id = request.form.get("parent_id", type=int)
            code = request.form.get("code", "").strip().upper()
            name = request.form.get("name_ar", "").strip()
            location_type = request.form.get("location_type", "")
            expected_parent = {"floor": None, "zone": "floor", "rack": "zone", "bin": "rack"}
            if location_type not in expected_parent or not code or not name:
                raise ValueError("أدخل كود واسم الموقع وحدد نوعه.")
            if not c.execute("SELECT 1 FROM stock_warehouses WHERE id=? AND active=1", (warehouse_id,)).fetchone():
                raise ValueError("اختر مخزنًا نشطًا.")
            if c.execute("""SELECT 1 FROM stock_locations WHERE warehouse_id=? AND parent_id IS ? AND lower(code)=lower(?)""",
                          (warehouse_id, parent_id, code)).fetchone():
                raise ValueError("كود الموقع مستخدم بالفعل في نفس المستوى.")
            if expected_parent[location_type] is None:
                if parent_id:
                    raise ValueError("الدور يجب أن يكون مستوى رئيسيًا داخل المخزن.")
            else:
                parent = c.execute("SELECT location_type,warehouse_id,active FROM stock_locations WHERE id=?", (parent_id,)).fetchone() if parent_id else None
                if not parent or not parent["active"] or parent["warehouse_id"] != warehouse_id or parent["location_type"] != expected_parent[location_type]:
                    raise ValueError("الموقع الأب غير صحيح أو لا يتبع نفس المخزن.")
            c.execute("INSERT INTO stock_locations(warehouse_id,parent_id,code,name_ar,location_type) VALUES(?,?,?,?,?)",
                      (warehouse_id, parent_id, code, name, location_type))
            c.commit()
            flash("تم إنشاء موقع التخزين.", "success")
        except (ValueError, sqlite3.IntegrityError) as exc:
            c.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "كود الموقع مستخدم في نفس المستوى.", "error")
        finally:
            c.close()
        return redirect(url_for("stock.locations"))
    warehouses = c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    parents = c.execute("SELECT id,warehouse_id,code,name_ar,location_type FROM stock_locations WHERE active=1 ORDER BY warehouse_id,location_type,code").fetchall()
    rows = c.execute("""SELECT l.*,w.code warehouse_code,w.name_ar warehouse_name,p.code parent_code,p.name_ar parent_name
        FROM stock_locations l JOIN stock_warehouses w ON w.id=l.warehouse_id
        LEFT JOIN stock_locations p ON p.id=l.parent_id ORDER BY w.code,l.location_type,l.code""").fetchall()
    c.close()
    return render_template("stock_locations.html", warehouses=warehouses, parents=parents, rows=rows)


@stock_bp.route("/product-locations", methods=["GET", "POST"])
@stock_permission("stock.manage")
def product_locations():
    c = get_db()
    if request.method == "POST":
        try:
            product_id = request.form.get("product_id", type=int)
            warehouse_id = request.form.get("warehouse_id", type=int)
            location_id = request.form.get("location_id", type=int)
            valid = c.execute("""SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1""",
                              (location_id, warehouse_id)).fetchone()
            if not c.execute("SELECT 1 FROM stock_products WHERE id=? AND active=1", (product_id,)).fetchone() or not valid:
                raise ValueError("الصنف أو موقع التخزين غير صحيح.")
            c.execute("""INSERT INTO stock_product_locations(product_id,warehouse_id,location_id,is_default)
                VALUES(?,?,?,1) ON CONFLICT(product_id,warehouse_id)
                DO UPDATE SET location_id=excluded.location_id,is_default=1""",
                (product_id, warehouse_id, location_id))
            # Existing stock without a recorded bin is assigned to the location
            # explicitly chosen by the user as this product's default location.
            c.execute("""UPDATE stock_valuation_layers SET location_id=?
                WHERE product_id=? AND warehouse_id=? AND location_id IS NULL
                  AND CAST(qty_remaining AS REAL)>0""", (location_id, product_id, warehouse_id))
            c.commit()
            flash("تم تحديد الموقع الافتراضي وإسناد الأرصدة القديمة غير المحددة إليه.", "success")
        except (ValueError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
        finally:
            c.close()
        return redirect(url_for("stock.product_locations"))
    products = c.execute("SELECT p.id,p.sku,p.name_ar,COALESCE(sc.name_ar,p.category,'') category_name FROM stock_products p LEFT JOIN stock_categories sc ON sc.id=p.category_id WHERE p.active=1 ORDER BY sc.name_ar,p.sku").fetchall()
    warehouses = c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    locations = c.execute("""SELECT l.id,l.warehouse_id,l.code,l.name_ar,l.location_type,w.code warehouse_code
        FROM stock_locations l JOIN stock_warehouses w ON w.id=l.warehouse_id
        WHERE l.active=1 ORDER BY w.code,l.code""").fetchall()
    rows = c.execute("""SELECT m.*,p.sku,p.name_ar product_name,COALESCE(sc.name_ar,p.category,'') category_name,w.code warehouse_code,l.code location_code,l.name_ar location_name
        FROM stock_product_locations m JOIN stock_products p ON p.id=m.product_id LEFT JOIN stock_categories sc ON sc.id=p.category_id
        JOIN stock_warehouses w ON w.id=m.warehouse_id JOIN stock_locations l ON l.id=m.location_id
        ORDER BY w.code,p.sku""").fetchall()
    c.close()
    return render_template("stock_product_locations.html", products=products, warehouses=warehouses,
                           locations=locations, rows=rows)


@stock_bp.route("/products/<int:product_id>/edit", methods=["GET","POST"])
@stock_permission("stock.manage")
def edit_product(product_id):
    c=get_db(); categories=c.execute("SELECT id,code,name_ar FROM stock_categories WHERE active=1 ORDER BY name_ar").fetchall(); product=c.execute("SELECT * FROM stock_products WHERE id=?",(product_id,)).fetchone()
    if not product:
        c.close(); flash("الصنف غير موجود.","error"); return redirect(url_for("stock.products"))
    used=c.execute("SELECT 1 FROM stock_movements WHERE product_id=? LIMIT 1",(product_id,)).fetchone() is not None
    if request.method=="POST":
        try:
            sku=request.form.get("sku","").strip(); name=request.form.get("name_ar","").strip(); uom=request.form.get("uom","").strip()
            if not sku or not name or not uom: raise ValueError("الكود والاسم والوحدة مطلوبة.")
            if used and (request.form.get("product_type") != product["product_type"] or uom != product["uom"] or int(request.form.get("track_lot")=="on") != product["track_lot"] or int(request.form.get("track_expiry")=="on") != product["track_expiry"]):
                raise ValueError("الصنف له حركات سابقة؛ لا يمكن تغيير نوعه أو وحدة قياسه أو إعدادات تتبع التشغيلة/الصلاحية.")
            category_id=request.form.get("category_id",type=int)
            category=c.execute("SELECT name_ar FROM stock_categories WHERE id=? AND active=1",(category_id,)).fetchone() if category_id else None
            if category_id and not category: raise ValueError("مجموعة الصنف غير صحيحة.")
            purchase_price = money(dec(request.form.get("purchase_price", product["purchase_price"] or "0"), "سعر الشراء"))
            sale_price = money(dec(request.form.get("sale_price", product["sale_price"] or "0"), "سعر البيع"))
            if purchase_price < 0 or sale_price < 0:
                raise ValueError("أسعار الشراء والبيع لا يمكن أن تكون سالبة.")
            c.execute("""UPDATE stock_products SET sku=?,name_ar=?,category=?,category_id=?,product_type=?,uom=?,barcode=?,track_lot=?,track_expiry=?,min_qty=?,purchase_price=?,sale_price=? WHERE id=?""",
              (sku,name,category["name_ar"] if category else "",category_id,request.form.get("product_type","goods"),uom,request.form.get("barcode","").strip() or None,int(request.form.get("track_lot")=="on"),int(request.form.get("track_expiry")=="on"),str(q4(dec(request.form.get("min_qty","0"),"الحد الأدنى"))),str(purchase_price),str(sale_price),product_id))
            c.commit(); c.close(); flash("تم تعديل الصنف.","success"); return redirect(url_for("stock.products"))
        except (ValueError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "الكود أو الباركود مستخدم بالفعل.","error")
    c.close(); return render_template("stock_product_edit.html",product=product,used=used,categories=categories)


@stock_bp.route("/products/<int:product_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_product(product_id):
    c=get_db(); product=c.execute("SELECT * FROM stock_products WHERE id=?",(product_id,)).fetchone()
    if not product: flash("الصنف غير موجود.","error")
    else:
        refs=[("stock_movements","حركات المخزون"),("stock_lots","التشغيلات"),("stock_valuation_layers","طبقات التقييم"),("purchase_order_lines","أوامر الشراء"),("goods_receipt_lines","استلامات المشتريات"),("sales_invoice_lines","فواتير البيع")]
        used=next((label for table,label in refs if c.execute(f"SELECT 1 FROM {table} WHERE product_id=? LIMIT 1",(product_id,)).fetchone()),None)
        if used: flash("لا يمكن حذف الصنف لوجود ارتباط في "+used+".","error")
        else:
            try: c.execute("DELETE FROM stock_products WHERE id=?",(product_id,)); c.commit(); flash("تم حذف الصنف لعدم وجود حركات مرتبطة.","success")
            except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف الصنف لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("stock.products"))


@stock_bp.route("/warehouses/<int:warehouse_id>/edit",methods=["GET","POST"])
@stock_permission("stock.manage")
def edit_warehouse(warehouse_id):
    c=get_db(); warehouse=c.execute("SELECT * FROM stock_warehouses WHERE id=?",(warehouse_id,)).fetchone()
    if not warehouse:
        c.close(); flash("المخزن غير موجود.","error"); return redirect(url_for("stock.warehouses"))
    if request.method=="POST":
        try:
            code=request.form.get("code","").strip(); name=request.form.get("name_ar","").strip()
            if not code or not name: raise ValueError("كود واسم المخزن مطلوبان.")
            c.execute("UPDATE stock_warehouses SET code=?,name_ar=?,branch=? WHERE id=?",(code,name,request.form.get("branch","").strip(),warehouse_id))
            c.commit(); c.close(); flash("تم تعديل المخزن.","success"); return redirect(url_for("stock.warehouses"))
        except (ValueError,sqlite3.IntegrityError) as e: c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود المخزن مستخدم بالفعل.","error")
    c.close(); return render_template("stock_warehouse_edit.html",warehouse=warehouse)


@stock_bp.route("/warehouses/<int:warehouse_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_warehouse(warehouse_id):
    c=get_db(); warehouse=c.execute("SELECT * FROM stock_warehouses WHERE id=?",(warehouse_id,)).fetchone()
    if not warehouse: flash("المخزن غير موجود.","error")
    else:
        used = None
        checks = [
            ("SELECT 1 FROM stock_movements WHERE source_warehouse_id=? OR target_warehouse_id=? LIMIT 1",(warehouse_id,warehouse_id),"حركات المخزون"),
            ("SELECT 1 FROM stock_valuation_layers WHERE warehouse_id=? LIMIT 1",(warehouse_id,),"طبقات تقييم المخزون"),
            ("SELECT 1 FROM goods_receipts WHERE warehouse_id=? LIMIT 1",(warehouse_id,),"استلامات المشتريات"),
            ("SELECT 1 FROM sales_invoices WHERE warehouse_id=? LIMIT 1",(warehouse_id,),"فواتير البيع")
        ]
        for query, params, label in checks:
            if c.execute(query,params).fetchone():
                used=label
                break
        if used: flash("لا يمكن حذف المخزن لوجود "+used+" مرتبطة به.","error")
        else:
            try: c.execute("DELETE FROM stock_warehouses WHERE id=?",(warehouse_id,)); c.commit(); flash("تم حذف المخزن.","success")
            except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف المخزن لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("stock.warehouses"))


@stock_bp.route("/movements", methods=["GET","POST"])
@stock_permission("stock.manage")
def movements():
    c=get_db()
    if request.method=="POST":
        try:
            kind=request.form.get("movement_type")
            if kind not in {"receipt","issue","transfer","relocate","scrap"}: raise ValueError("نوع الحركة غير صحيح.")
            product=c.execute("SELECT * FROM stock_products WHERE id=? AND active=1",(request.form.get("product_id"),)).fetchone()
            if not product: raise ValueError("الصنف غير موجود أو غير نشط.")
            if product["product_type"] == "service": raise ValueError("الخدمات لا تُدار كأصناف مخزنية.")
            qty=q4(dec(request.form.get("quantity"),"الكمية"))
            if qty<=0: raise ValueError("الكمية يجب أن تكون أكبر من صفر.")
            src=request.form.get("source_warehouse_id",type=int)
            dst=request.form.get("target_warehouse_id",type=int)
            if kind=="receipt" and not dst: raise ValueError("حدد مخزن الاستلام.")
            if kind in {"issue","scrap","transfer","relocate"} and not src: raise ValueError("حدد مخزن الصرف.")
            if kind=="transfer" and (not dst or dst==src): raise ValueError("حدد مخزن وجهة مختلفًا عن المصدر.")
            source_location_id=request.form.get("source_location_id",type=int)
            target_location_id=request.form.get("target_location_id",type=int)
            if kind=="relocate":
                if not source_location_id or not target_location_id or source_location_id==target_location_id:
                    raise ValueError("حدد موقع المصدر وموقع الوجهة المختلفين.")
                if not c.execute("SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(source_location_id,src)).fetchone():
                    raise ValueError("موقع المصدر لا يتبع المخزن المحدد.")
                if not c.execute("SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(target_location_id,src)).fetchone():
                    raise ValueError("موقع الوجهة لا يتبع نفس المخزن.")
                dst=src
            if kind=="transfer" and dst:
                if not target_location_id:
                    default_location=c.execute("SELECT location_id FROM stock_product_locations WHERE product_id=? AND warehouse_id=?",(product["id"],dst)).fetchone()
                    target_location_id=default_location["location_id"] if default_location else None
                if target_location_id and not c.execute("SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(target_location_id,dst)).fetchone():
                    raise ValueError("موقع الوجهة لا يتبع مخزن الوجهة.")
            c.execute("BEGIN IMMEDIATE")
            lot_id=None
            lot_no=request.form.get("lot_number","").strip()
            if kind=="receipt":
                raise ValueError("استلام المخزون يجب أن يتم من شاشة استلام المشتريات حتى يُسجل قيد GRNI ويرتبط بالمورد. لا تستخدم حركة استلام عامة.")
            else:
                if product["track_lot"] or product["track_expiry"]:
                    if not lot_no: raise ValueError("أدخل رقم التشغيلة للصرف من صنف يتطلب تتبعًا.")
                    lot=c.execute("SELECT * FROM stock_lots WHERE product_id=? AND lot_number=?",(product["id"],lot_no)).fetchone()
                    if not lot: raise ValueError("التشغيلة غير مسجلة.")
                    if lot["expiry_date"] and date.fromisoformat(lot["expiry_date"])<date.today(): raise ValueError("لا يمكن صرف تشغيلة منتهية الصلاحية.")
                    lot_id=lot["id"]
                method_row=c.execute("SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
                method=method_row["setting_value"] if method_row else "average"
                if kind in {"issue","scrap","relocate","transfer"} and source_location_id and not c.execute("SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(source_location_id,src)).fetchone():
                    raise ValueError("موقع المصدر لا يتبع المخزن المحدد.")
                total,allocations=consume_layers(c,product["id"],src,qty,method,lot_id,source_location_id)
                unit_cost=(total/qty).quantize(MONEY,rounding=ROUND_HALF_UP)
            movement_no=f"STK-{date.today().strftime('%Y%m%d')}-{uuid.uuid4().hex[:12].upper()}"
            movement_date=request.form.get("movement_date") or date.today().isoformat()
            issue_purpose=request.form.get("issue_purpose") if kind=="issue" else None
            if kind=="issue" and issue_purpose not in {"sale","internal"}:
                raise ValueError("حدد غرض الصرف: تكلفة مبيعات أو استخدام داخلي.")
            cur=c.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,source_warehouse_id,target_warehouse_id,
              source_location_id,target_location_id,lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,issue_purpose)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(movement_no,kind,product["id"],src,dst,source_location_id,target_location_id,lot_id,str(qty),str(unit_cost),str(total),movement_date,request.form.get("notes","").strip(),session.get("user_id"),issue_purpose))
            mid=cur.lastrowid
            if kind=="receipt":
                c.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost)
                  VALUES(?,?,?,?,?,?,?)""",(product["id"],dst,lot_id,mid,str(qty),str(qty),str(unit_cost)))
            elif kind in {"transfer","relocate"}:
                for allocated_lot,allocated_qty,allocated_cost,allocated_total in allocations:
                    c.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,location_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost,value_remaining)
                      VALUES(?,?,?,?,?,?,?,?,?)""",(product["id"],dst,target_location_id,allocated_lot,mid,str(allocated_qty),str(allocated_qty),str(allocated_cost),str(allocated_total)))
            if kind=="scrap":
                post_scrap_journal(c,mid,movement_no,product,movement_date,total,session.get("user_id"))
            elif kind in {"transfer","relocate"}:
                # Internal warehouse/bin transfers do not affect the consolidated inventory GL.
                c.execute("UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (mid,))
            elif kind=="receipt":
                # A receipt needs a supplier invoice or an explicit GRNI document before posting.
                c.execute("UPDATE stock_movements SET accounting_status='pending_document' WHERE id=?", (mid,))
            elif kind=="issue":
                post_issue_journal(c,mid,movement_no,product,movement_date,total,session.get("user_id"),issue_purpose)
            c.commit(); flash(f"تم ترحيل الحركة {movement_no}.", "success")
        except (ValueError,InvalidOperation,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e), "error")
        finally: c.close()
        return redirect(url_for("stock.movements"))
    products=c.execute("SELECT id,sku,name_ar,uom,track_lot,track_expiry FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    warehouses=c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    locations=c.execute("SELECT id,warehouse_id,code,name_ar,location_type FROM stock_locations WHERE active=1 ORDER BY warehouse_id,code").fetchall()
    rows=c.execute("""SELECT m.*,p.sku,p.name_ar,s.name_ar source_name,t.name_ar target_name
      FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
      LEFT JOIN stock_warehouses s ON s.id=m.source_warehouse_id LEFT JOIN stock_warehouses t ON t.id=m.target_warehouse_id
      ORDER BY m.id DESC LIMIT 30""").fetchall()
    c.close()
    return render_template("stock_movements.html",products=products,warehouses=warehouses,locations=locations,rows=rows,today=date.today().isoformat())


@stock_bp.route("/reports")
@stock_permission("stock.view")
def reports():
    c=get_db()
    products=c.execute("SELECT id,sku,name_ar,uom FROM stock_products WHERE active=1 ORDER BY sku").fetchall()
    layers=c.execute("SELECT * FROM stock_valuation_layers WHERE CAST(qty_remaining AS REAL)>0 ORDER BY id").fetchall()
    product_totals={}
    warehouse_totals={}
    for layer in layers:
        qty=Decimal(str(layer["qty_remaining"] or "0"))
        if qty<=0: continue
        value=(Decimal(str(layer["value_remaining"])) if "value_remaining" in layer.keys()
               and layer["value_remaining"] not in (None,"") else qty*Decimal(str(layer["unit_cost"] or "0")))
        product_id=layer["product_id"]
        total=product_totals.setdefault(product_id,{"qty":Decimal("0"),"value":Decimal("0")})
        total["qty"]+=qty; total["value"]+=value
        key=(product_id,layer["warehouse_id"])
        warehouse=warehouse_totals.setdefault(key,{"qty":Decimal("0"),"value":Decimal("0")})
        warehouse["qty"]+=qty; warehouse["value"]+=value
    rows=[]
    for product in products:
        total=product_totals.get(product["id"],{"qty":Decimal("0"),"value":Decimal("0")})
        rows.append({"id":product["id"],"sku":product["sku"],"name_ar":product["name_ar"],
                     "uom":product["uom"],"qty":total["qty"],"value":total["value"],
                     "unit_cost":total["value"]/total["qty"] if total["qty"] else Decimal("0")})
    lots=[]
    for lot in c.execute("""SELECT l.id,l.lot_number,l.expiry_date,p.sku,p.name_ar
        FROM stock_lots l JOIN stock_products p ON p.id=l.product_id
        WHERE l.expiry_date IS NOT NULL ORDER BY l.expiry_date""").fetchall():
        lot_qty=sum((Decimal(str(x["qty_remaining"] or "0")) for x in layers if x["lot_id"]==lot["id"]),Decimal("0"))
        lots.append({"sku":lot["sku"],"name_ar":lot["name_ar"],"lot_number":lot["lot_number"],
                     "expiry_date":lot["expiry_date"],"qty":lot_qty})
    warehouse_lookup={row["id"]:row for row in c.execute("SELECT id,code,name_ar FROM stock_warehouses").fetchall()}
    location_lookup={row["id"]:row for row in c.execute("SELECT id,code,name_ar,location_type FROM stock_locations").fetchall()}
    product_lookup={row["id"]:row for row in products}
    location_totals={}
    for layer in layers:
        qty=Decimal(str(layer["qty_remaining"] or "0"))
        if qty<=0: continue
        key=(layer["product_id"],layer["warehouse_id"],layer["location_id"])
        total=location_totals.setdefault(key,{"qty":Decimal("0"),"value":Decimal("0")})
        total["qty"]+=qty
        total["value"]+=(Decimal(str(layer["value_remaining"])) if "value_remaining" in layer.keys() and layer["value_remaining"] not in (None,"") else qty*Decimal(str(layer["unit_cost"] or "0")))
    location_rows=[]
    for (product_id,warehouse_id,location_id),total in location_totals.items():
        product=product_lookup[product_id]; warehouse=warehouse_lookup.get(warehouse_id); location=location_lookup.get(location_id)
        location_rows.append({"sku":product["sku"],"name_ar":product["name_ar"],"uom":product["uom"],
            "warehouse_code":warehouse["code"] if warehouse else "", "warehouse_name":warehouse["name_ar"] if warehouse else "",
            "location_code":location["code"] if location else "غير محدد", "location_name":location["name_ar"] if location else "غير محدد",
            "qty":total["qty"],"value":total["value"]})
    warehouse_rows=[]
    for (product_id,warehouse_id),total in warehouse_totals.items():
        product=product_lookup[product_id]; warehouse=warehouse_lookup.get(warehouse_id)
        warehouse_rows.append({"sku":product["sku"],"name_ar":product["name_ar"],"uom":product["uom"],
            "warehouse_code":warehouse["code"] if warehouse else "",
            "warehouse_name":warehouse["name_ar"] if warehouse else "",
            "qty":total["qty"],"value":total["value"],
            "unit_cost":total["value"]/total["qty"] if total["qty"] else Decimal("0")})
    movements=c.execute("""SELECT m.*,p.sku,p.name_ar,w.name_ar warehouse
      FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
      LEFT JOIN stock_warehouses w ON w.id=COALESCE(m.target_warehouse_id,m.source_warehouse_id)
      ORDER BY m.id DESC LIMIT 100""").fetchall()
    c.close()
    return render_template("stock_reports.html",rows=rows,lots=lots,movements=movements,warehouse_rows=warehouse_rows,location_rows=location_rows)


@stock_bp.route("/barcode")
@stock_permission("stock.view")
def barcode_lookup():
    code=request.args.get("q","").strip()
    c=get_db()
    product=c.execute("SELECT * FROM stock_products WHERE barcode=? OR sku=?",(code,code)).fetchone() if code else None
    c.close()
    return render_template("stock_barcode.html",product=product,code=code)


def _stocktake_layer_balance(c, product_id, warehouse_id):
    layers = c.execute("""SELECT qty_remaining,unit_cost,value_remaining
        FROM stock_valuation_layers WHERE product_id=? AND warehouse_id=?
        AND CAST(qty_remaining AS REAL)>0 ORDER BY id""", (product_id, warehouse_id)).fetchall()
    qty = sum((D(str(row["qty_remaining"] or "0")) for row in layers), D("0"))
    value = sum((D(str(row["value_remaining"])) if row["value_remaining"] not in (None, "")
                 else D(str(row["qty_remaining"] or "0")) * D(str(row["unit_cost"] or "0"))
                 for row in layers), D("0"))
    return q4(qty), value


@stock_bp.route("/counts", methods=["GET", "POST"])
@stock_permission("stock.manage")
def counts():
    c = get_db()
    if request.method == "POST":
        try:
            warehouse_id = request.form.get("warehouse_id", type=int)
            count_date = request.form.get("count_date", "").strip()
            date.fromisoformat(count_date)
            if not c.execute("SELECT 1 FROM stock_warehouses WHERE id=? AND active=1", (warehouse_id,)).fetchone():
                raise ValueError("اختر مخزنًا نشطًا.")
            c.execute("BEGIN IMMEDIATE")
            count_no = "STK-" + date.fromisoformat(count_date).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6].upper()
            c.execute("INSERT INTO stocktakes(count_no,warehouse_id,count_date,notes,created_by) VALUES(?,?,?,?,?)",
                (count_no, warehouse_id, count_date, request.form.get("notes", "").strip(), session.get("user_id")))
            stocktake_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            for product in c.execute("SELECT id FROM stock_products WHERE active=1 ORDER BY sku").fetchall():
                qty, _ = _stocktake_layer_balance(c, product["id"], warehouse_id)
                c.execute("INSERT INTO stocktake_lines(stocktake_id,product_id,snapshot_qty) VALUES(?,?,?)",
                          (stocktake_id, product["id"], str(qty)))
            c.commit()
            flash("تم إنشاء أمر الجرد وحفظ الأرصدة الدفترية وقت الإنشاء. أدخل الكميات الفعلية ثم احفظ العد.", "success")
            return redirect(url_for("stock.count_detail", stocktake_id=stocktake_id))
        except (ValueError, sqlite3.IntegrityError) as exc:
            c.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "تعذر إنشاء أمر الجرد.", "error")
        finally:
            c.close()
        return redirect(url_for("stock.counts"))
    warehouses = c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    rows = c.execute("""SELECT s.*,w.code warehouse_code,w.name_ar warehouse_name,
        (SELECT COUNT(*) FROM stocktake_lines l WHERE l.stocktake_id=s.id) line_count
        FROM stocktakes s JOIN stock_warehouses w ON w.id=s.warehouse_id ORDER BY s.id DESC LIMIT 100""").fetchall()
    c.close()
    return render_template("stock_counts.html", warehouses=warehouses, rows=rows, today=date.today().isoformat())


@stock_bp.route("/counts/<int:stocktake_id>", methods=["GET", "POST"])
@stock_permission("stock.manage")
def count_detail(stocktake_id):
    c = get_db()
    stocktake = c.execute("""SELECT s.*,w.code warehouse_code,w.name_ar warehouse_name
        FROM stocktakes s JOIN stock_warehouses w ON w.id=s.warehouse_id WHERE s.id=?""", (stocktake_id,)).fetchone()
    if not stocktake:
        c.close()
        flash("أمر الجرد غير موجود.", "error")
        return redirect(url_for("stock.counts"))
    if request.method == "POST":
        try:
            if stocktake["status"] not in ("draft", "counted"):
                raise ValueError("لا يمكن تعديل جرد تم اعتماده.")
            lines = c.execute("SELECT id,snapshot_qty FROM stocktake_lines WHERE stocktake_id=?", (stocktake_id,)).fetchall()
            c.execute("BEGIN IMMEDIATE")
            for line in lines:
                raw = request.form.get("counted_" + str(line["id"]), "").strip()
                if raw == "":
                    raise ValueError("يجب إدخال الكمية الفعلية لكل صنف، حتى لو كانت صفرًا.")
                counted = dec(raw, "الكمية الفعلية")
                unit_cost = dec(request.form.get("cost_" + str(line["id"]), "0"), "تكلفة الوحدة")
                if counted < 0 or unit_cost < 0:
                    raise ValueError("الكميات والتكاليف لا يمكن أن تكون سالبة.")
                variance = q4(counted - D(str(line["snapshot_qty"])))
                estimated_value = money(abs(variance) * unit_cost)
                c.execute("""UPDATE stocktake_lines SET counted_qty=?,variance_qty=?,counted_unit_cost=?,
                    variance_value=?,notes=? WHERE id=?""",
                    (str(q4(counted)), str(variance), str(money(unit_cost)), str(estimated_value),
                     request.form.get("notes_" + str(line["id"]), "").strip(), line["id"]))
            c.execute("UPDATE stocktakes SET status='counted' WHERE id=?", (stocktake_id,))
            c.commit()
            flash("تم حفظ العد الفعلي وحساب فروق الجرد. راجع التقرير قبل الاعتماد والترحيل.", "success")
            return redirect(url_for("stock.count_detail", stocktake_id=stocktake_id))
        except (ValueError, sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc), "error")
    lines = c.execute("""SELECT l.*,p.sku,p.name_ar,p.uom,p.purchase_price
        FROM stocktake_lines l JOIN stock_products p ON p.id=l.product_id
        WHERE l.stocktake_id=? ORDER BY p.sku""", (stocktake_id,)).fetchall()
    accounts = c.execute("""SELECT id,code,name_ar,account_type FROM accounts
        WHERE active=1 AND is_group=0 AND account_type IN ('expense','revenue') ORDER BY account_type,code""").fetchall()
    c.close()
    return render_template("stock_count_detail.html", stocktake=stocktake, lines=lines, accounts=accounts)


@stock_bp.route("/counts/<int:stocktake_id>/approve", methods=["POST"])
@stock_permission("stock.manage")
def approve_count(stocktake_id):
    c = get_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        stocktake = c.execute("SELECT * FROM stocktakes WHERE id=?", (stocktake_id,)).fetchone()
        if not stocktake or stocktake["status"] != "counted":
            raise ValueError("يجب حفظ العد أولًا، ولا يمكن اعتماد أمر غير جاهز للمراجعة.")
        lines = c.execute("""SELECT l.*,p.sku,p.name_ar FROM stocktake_lines l
            JOIN stock_products p ON p.id=l.product_id WHERE l.stocktake_id=? ORDER BY p.sku""",
            (stocktake_id,)).fetchall()
        if not lines or any(line["counted_qty"] is None for line in lines):
            raise ValueError("بيانات العد غير مكتملة.")
        activity_row = c.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
        activity = activity_row["setting_value"] if activity_row else "commercial"
        inventory_code = {"commercial":"1150", "industrial":"1130", "medical":"1150",
                          "restaurant":"1140", "service":None}.get(activity)
        inventory = c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code=?",
                              (inventory_code,)).fetchone() if inventory_code else None
        if not inventory or inventory["is_group"] or not inventory["active"] or inventory["account_type"] != "asset":
            raise ValueError("اضبط حساب المخزون كحساب أصول تفصيلي نشط قبل اعتماد الجرد.")
        shortage_id = request.form.get("shortage_account_id", type=int)
        surplus_id = request.form.get("surplus_account_id", type=int)
        shortage_account = c.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?", (shortage_id,)).fetchone() if shortage_id else None
        surplus_account = c.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?", (surplus_id,)).fetchone() if surplus_id else None
        journal_lines, movement_ids = [], []
        for line in lines:
            snapshot = D(str(line["snapshot_qty"]))
            current, _ = _stocktake_layer_balance(c, line["product_id"], stocktake["warehouse_id"])
            if current != snapshot:
                raise ValueError("تغير الرصيد الدفتري للصنف " + line["sku"] + " بعد بدء الجرد. أوقف الحركة وأنشئ أمر جرد جديد.")
            variance = D(str(line["variance_qty"] or "0"))
            if variance == 0:
                c.execute("UPDATE stocktake_lines SET variance_value='0' WHERE id=?", (line["id"],))
                continue
            if variance < 0:
                if not shortage_account or shortage_account["account_type"] != "expense" or shortage_account["is_group"] or not shortage_account["active"]:
                    raise ValueError("اختر حساب مصروف تفصيلي نشط لعجز المخزون.")
                qty = -variance
                method_row = c.execute("SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
                method = method_row["setting_value"] if method_row else "fifo"
                cost, _ = consume_layers(c, line["product_id"], stocktake["warehouse_id"], qty, method)
                movement_no = stocktake["count_no"] + "-" + str(line["id"]) + "-S"
                cur = c.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,
                    source_warehouse_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status)
                    VALUES(?,'issue',?,?,?,?,?,?,?,?, 'pending')""",
                    (movement_no,line["product_id"],stocktake["warehouse_id"],str(qty),
                     str(cost / qty),str(cost),stocktake["count_date"],"عجز جرد فعلي",session.get("user_id")))
                movement_ids.append(cur.lastrowid)
                journal_lines.extend([(shortage_account["id"],cost,0,"عجز جرد - " + line["name_ar"]),
                                      (inventory["id"],0,cost,"تخفيض مخزون - " + line["name_ar"])])
                c.execute("UPDATE stocktake_lines SET variance_value=? WHERE id=?", (str(-cost),line["id"]))
            else:
                if not surplus_account or surplus_account["account_type"] != "revenue" or surplus_account["is_group"] or not surplus_account["active"]:
                    raise ValueError("اختر حساب إيراد/أرباح أخرى تفصيلي نشط لزيادة المخزون.")
                unit_cost = D(str(line["counted_unit_cost"] or "0"))
                if unit_cost <= 0:
                    raise ValueError("أدخل تكلفة الوحدة للصنف " + line["sku"] + " ذي الزيادة قبل الاعتماد.")
                cost = money(variance * unit_cost)
                default_location = c.execute("SELECT location_id FROM stock_product_locations WHERE product_id=? AND warehouse_id=?",
                                             (line["product_id"],stocktake["warehouse_id"])).fetchone()
                movement_no = stocktake["count_no"] + "-" + str(line["id"]) + "-P"
                cur = c.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,
                    target_warehouse_id,target_location_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status)
                    VALUES(?,'receipt',?,?,?,?,?,?,?,?,?, 'pending')""",
                    (movement_no,line["product_id"],stocktake["warehouse_id"],
                     default_location["location_id"] if default_location else None,str(variance),
                     str(unit_cost),str(cost),stocktake["count_date"],"زيادة جرد فعلي",session.get("user_id")))
                movement_id = cur.lastrowid
                c.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,location_id,
                    source_movement_id,qty_in,qty_remaining,unit_cost,value_remaining)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (line["product_id"],stocktake["warehouse_id"],default_location["location_id"] if default_location else None,
                     movement_id,str(variance),str(variance),str(unit_cost),str(cost)))
                movement_ids.append(movement_id)
                journal_lines.extend([(inventory["id"],cost,0,"زيادة مخزون - " + line["name_ar"]),
                                      (surplus_account["id"],0,cost,"أرباح/فروق جرد - " + line["name_ar"])])
                c.execute("UPDATE stocktake_lines SET variance_value=? WHERE id=?", (str(cost),line["id"]))
        journal_id = None
        if journal_lines:
            journal_id = post_journal(c,stocktake["count_date"],stocktake["count_no"],
                "تسوية فروق الجرد - " + stocktake["count_no"],journal_lines,session.get("user_id"))
        for movement_id in movement_ids:
            c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?",
                      (journal_id,movement_id))
        c.execute("""UPDATE stocktakes SET status='approved',approved_by=?,approved_at=CURRENT_TIMESTAMP,
            journal_entry_id=? WHERE id=?""", (session.get("user_id"),journal_id,stocktake_id))
        c.commit()
        flash("تم اعتماد الجرد وتحديث طبقات المخزون وترحيل قيد التسوية." if journal_id
              else "تم اعتماد الجرد؛ لا توجد فروق تستلزم قيدًا محاسبيًا.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    finally:
        c.close()
    return redirect(url_for("stock.count_detail", stocktake_id=stocktake_id))
