from app_paths import DB_PATH
from database import connect_database, fetch_page
from db_compat import (table_columns, table_exists, database_dialect, begin_write_transaction, insert_and_get_id, execute_script, DB_ERROR, DB_INTEGRITY_ERROR)
from db_sql import execute_compat
"""NEXUS ERP inventory foundation (v0.2).

Implements product master, warehouses, stock receipts/issues/transfers/scrap,
lot/expiry tracking, non-negative stock validation and valuation layers.
Manufacturing orders and production conversion are intentionally excluded.
"""
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from datetime import date, datetime
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal, money, safe_error_message, decimal_sum_expression, decimal_compare_expression
from analytic_dimensions import initialize_analytic_dimensions_schema
from code_sequences import next_code

stock_bp = Blueprint("stock", __name__, url_prefix="/stock")

D = Decimal
QTY = D("0.0001")
MONEY = D("0.01")


def get_db():
    return connect_database(DB_PATH, isolation_level=None)


def _execute(connection, sql, params=()):
    """Run stock SQL through the staged SQLite/PostgreSQL placeholder adapter."""
    return execute_compat(connection, sql, params)


def _table_exists(conn, name):
    """Database-neutral metadata check; stock operations remain SQLite-only."""
    return table_exists(conn, name)


def _columns(conn, name):
    """Return table columns without SQLite-specific PRAGMA at call sites."""
    return table_columns(conn, name)


def lock_stock_warehouses(conn, *warehouse_ids, allow_inactive=False):
    """Validate warehouse existence and serialize mutations; historical reversals may opt into inactive warehouses."""
    ids = sorted({int(value) for value in warehouse_ids if value not in (None, 0, "")})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = f"SELECT id,active FROM stock_warehouses WHERE id IN ({placeholders}) ORDER BY id"
    if database_dialect(conn) == "postgresql":
        sql += " FOR UPDATE"
    rows = _execute(conn, sql, ids).fetchall()
    locked = {int(row["id"]) for row in rows}
    missing = [warehouse_id for warehouse_id in ids if warehouse_id not in locked]
    if missing:
        raise ValueError("المخزن المحدد غير موجود أو لم يعد متاحًا.")
    inactive = [int(row["id"]) for row in rows if not bool(row["active"])]
    if inactive and not allow_inactive:
        raise ValueError("لا يمكن تنفيذ حركة جديدة على مخزن غير نشط. أعد تفعيل المخزن أولًا.")
    return rows


def _stocktake_for_update(conn, stocktake_id):
    sql = "SELECT * FROM stocktakes WHERE id=?"
    if database_dialect(conn) == "postgresql":
        sql += " FOR UPDATE"
    return _execute(conn, sql, (stocktake_id,)).fetchone()


def _stock_movement_for_update(conn, movement_id):
    sql = """SELECT m.*,p.id product_id,p.name_ar,p.sku
        FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
        WHERE m.id=?"""
    if database_dialect(conn) == "postgresql":
        sql += " FOR UPDATE OF m"
    return _execute(conn, sql, (movement_id,)).fetchone()


def _install_stock_location_triggers(conn):
    """Install SQLite guards; PostgreSQL equivalents live in its schema migrations."""
    if database_dialect(conn) != "sqlite":
        return
    execute_script(conn, """
    CREATE TRIGGER IF NOT EXISTS trg_stock_product_locations_same_wh_insert
    BEFORE INSERT ON stock_product_locations
    WHEN NOT EXISTS(SELECT 1 FROM stock_locations l WHERE l.id=NEW.location_id
      AND l.warehouse_id=NEW.warehouse_id AND l.active=1)
    BEGIN SELECT RAISE(ABORT,'موقع الصنف الافتراضي يجب أن يكون نشطًا ويتبع نفس المخزن.'); END;
    CREATE TRIGGER IF NOT EXISTS trg_stock_product_locations_same_wh_update
    BEFORE UPDATE OF warehouse_id,location_id ON stock_product_locations
    WHEN NOT EXISTS(SELECT 1 FROM stock_locations l WHERE l.id=NEW.location_id
      AND l.warehouse_id=NEW.warehouse_id AND l.active=1)
    BEGIN SELECT RAISE(ABORT,'موقع الصنف الافتراضي يجب أن يكون نشطًا ويتبع نفس المخزن.'); END;
    CREATE TRIGGER IF NOT EXISTS trg_stock_locations_prevent_default_deactivation
    BEFORE UPDATE OF active ON stock_locations
    WHEN OLD.active=1 AND NEW.active=0
      AND EXISTS(SELECT 1 FROM stock_product_locations p WHERE p.location_id=OLD.id)
    BEGIN SELECT RAISE(ABORT,'لا يمكن تعطيل موقع مستخدم كموقع افتراضي لصنف. غيّر الموقع الافتراضي أولًا.'); END;
    """)


def initialize_stock_schema():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = get_db()
    initialize_analytic_dimensions_schema(c)
    execute_script(c,"""
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
      branch TEXT NOT NULL DEFAULT '', branch_id INTEGER REFERENCES branches(id), active INTEGER NOT NULL DEFAULT 1
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
    warehouse_columns = _columns(c, "stock_warehouses")
    if "branch_id" not in warehouse_columns:
        _execute(c, "ALTER TABLE stock_warehouses ADD COLUMN branch_id INTEGER REFERENCES branches(id)")
    # Link legacy free-text branch labels only when an exact branch name/code match exists.
    _execute(c, """UPDATE stock_warehouses SET branch_id=(
        SELECT id FROM branches WHERE lower(name_ar)=lower(stock_warehouses.branch)
           OR lower(code)=lower(stock_warehouses.branch) ORDER BY id LIMIT 1
        ) WHERE branch_id IS NULL AND TRIM(COALESCE(branch,''))<>''""")
    product_columns = _columns(c, "stock_products")
    if "category_id" not in product_columns:
        _execute(c, "ALTER TABLE stock_products ADD COLUMN category_id INTEGER REFERENCES stock_categories(id)")
    for name in ("purchase_price", "sale_price", "reorder_point", "reorder_qty"):
        if name not in product_columns:
            _execute(c, f"ALTER TABLE stock_products ADD COLUMN {name} TEXT NOT NULL DEFAULT '0'")
    # Convert legacy free-text categories into selectable product groups without losing labels.
    legacy_categories = _execute(c, "SELECT DISTINCT TRIM(category) value FROM stock_products WHERE TRIM(COALESCE(category,''))<>'' AND category_id IS NULL ORDER BY 1").fetchall()
    for index, legacy in enumerate(legacy_categories, 1):
        label = legacy["value"]
        code = f"LEGACY-{index:04d}"
        _execute(c, "INSERT INTO stock_categories(code,name_ar) VALUES(?,?) ON CONFLICT DO NOTHING", (code, label))
        _execute(c, "UPDATE stock_products SET category_id=(SELECT id FROM stock_categories WHERE name_ar=?) WHERE TRIM(category)=? AND category_id IS NULL", (label, label))
    movement_columns = _columns(c, "stock_movements")
    if "journal_entry_id" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN journal_entry_id INTEGER")
    if "accounting_status" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN accounting_status TEXT NOT NULL DEFAULT 'pending'")
    if "accounting_note" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN accounting_note TEXT")
    if "issue_purpose" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN issue_purpose TEXT")
    if "activity_code" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN activity_code TEXT")
    if "source_location_id" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN source_location_id INTEGER REFERENCES stock_locations(id)")
    if "target_location_id" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN target_location_id INTEGER REFERENCES stock_locations(id)")
    layer_columns = _columns(c, "stock_valuation_layers")
    if "location_id" not in layer_columns:
        _execute(c, "ALTER TABLE stock_valuation_layers ADD COLUMN location_id INTEGER REFERENCES stock_locations(id)")
    if "receipt_line_id" not in layer_columns:
        # Purchases schema initializes immediately after stock schema in app startup.
        _execute(c, "ALTER TABLE stock_valuation_layers ADD COLUMN receipt_line_id INTEGER")
    execute_script(c,"""
    CREATE TABLE IF NOT EXISTS stock_warehouse_product_settings(
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      min_qty TEXT NOT NULL DEFAULT '0', reorder_point TEXT NOT NULL DEFAULT '0',
      reorder_qty TEXT NOT NULL DEFAULT '0', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(product_id,warehouse_id)
    );
    CREATE INDEX IF NOT EXISTS idx_stock_wh_product_reorder ON stock_warehouse_product_settings(warehouse_id,reorder_point,product_id);
    """)
    _install_stock_location_triggers(c)
    # Carry legacy product-level reorder thresholds forward as initial per-warehouse settings.
    _execute(c, f"""INSERT INTO stock_warehouse_product_settings
        (product_id,warehouse_id,min_qty,reorder_point,reorder_qty)
        SELECT p.id,w.id,p.min_qty,COALESCE(p.reorder_point,'0'),COALESCE(p.reorder_qty,'0')
        FROM stock_products p CROSS JOIN stock_warehouses w
        WHERE p.active=1 AND (
          {decimal_compare_expression(c, "COALESCE(p.min_qty, '0')")} > 0
          OR {decimal_compare_expression(c, "COALESCE(p.reorder_point, '0')")} > 0
          OR {decimal_compare_expression(c, "COALESCE(p.reorder_qty, '0')")} > 0
        )
        ON CONFLICT DO NOTHING""")
    _execute(c, "CREATE INDEX IF NOT EXISTS idx_stock_locations_wh_parent ON stock_locations(warehouse_id,parent_id,active)")
    _execute(c, "CREATE INDEX IF NOT EXISTS idx_stock_layers_location ON stock_valuation_layers(location_id,product_id)")
    # One summarized COGS journal may cover multiple stock issue movements.
    # The journal link is therefore intentionally non-unique.
    layer_columns = _columns(c, "stock_valuation_layers")
    if "value_remaining" not in layer_columns:
        _execute(c, "ALTER TABLE stock_valuation_layers ADD COLUMN value_remaining TEXT NOT NULL DEFAULT ''")
        # Backfill legacy layers using Decimal in Python, avoiding SQLite REAL arithmetic.
        for layer in _execute(c, "SELECT id,qty_remaining,unit_cost FROM stock_valuation_layers").fetchall():
            value = D(str(layer["qty_remaining"])) * D(str(layer["unit_cost"]))
            _execute(c, "UPDATE stock_valuation_layers SET value_remaining=? WHERE id=?",
                      (format(value, "f"), layer["id"]))
        # Use the source movement's exact value only during this one-time migration.
        # Reapplying it on every startup would undo moving-average revaluation of
        # untouched layers and make stock valuation disagree with the general ledger.
        for layer in _execute(c, """SELECT l.id,l.qty_in,l.qty_remaining,m.total_cost
            FROM stock_valuation_layers l JOIN stock_movements m ON m.id=l.source_movement_id""").fetchall():
            if (D(str(layer["qty_in"])) == D(str(layer["qty_remaining"]))
                and layer["total_cost"] not in (None, "")):
                _execute(c, "UPDATE stock_valuation_layers SET value_remaining=? WHERE id=?",
                          (str(D(str(layer["total_cost"]))), layer["id"]))
    _execute(c, "DROP INDEX IF EXISTS idx_stock_movement_journal")
    _execute(c, "CREATE INDEX IF NOT EXISTS idx_stock_movement_journal ON stock_movements(journal_entry_id) WHERE journal_entry_id IS NOT NULL")
    execute_script(c,"""
    CREATE TABLE IF NOT EXISTS stocktakes(
      id INTEGER PRIMARY KEY, count_no TEXT NOT NULL UNIQUE,
      warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      count_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      approved_by INTEGER, approved_at TEXT, journal_entry_id INTEGER,
      movement_cutoff_id INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stocktake_lines(
      id INTEGER PRIMARY KEY, stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      location_id INTEGER REFERENCES stock_locations(id),
      lot_id INTEGER REFERENCES stock_lots(id),
      snapshot_qty TEXT NOT NULL, counted_qty TEXT, variance_qty TEXT,
      counted_unit_cost TEXT NOT NULL DEFAULT '0', variance_value TEXT NOT NULL DEFAULT '0',
      notes TEXT NOT NULL DEFAULT ''
    );
    """)
    # Upgrade legacy one-line-per-product counts to location/lot-aware lines.
    line_columns = _columns(c, "stocktake_lines")
    if "location_id" not in line_columns or "lot_id" not in line_columns:
        _execute(c, "DROP INDEX IF EXISTS idx_stocktake_lines_header")
        _execute(c, "ALTER TABLE stocktake_lines RENAME TO stocktake_lines_legacy")
        execute_script(c,"""
        CREATE TABLE stocktake_lines(
          id INTEGER PRIMARY KEY, stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id) ON DELETE CASCADE,
          product_id INTEGER NOT NULL REFERENCES stock_products(id),
          location_id INTEGER REFERENCES stock_locations(id),
          lot_id INTEGER REFERENCES stock_lots(id),
          snapshot_qty TEXT NOT NULL, counted_qty TEXT, variance_qty TEXT,
          counted_unit_cost TEXT NOT NULL DEFAULT '0', variance_value TEXT NOT NULL DEFAULT '0',
          notes TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO stocktake_lines(id,stocktake_id,product_id,snapshot_qty,counted_qty,variance_qty,
          counted_unit_cost,variance_value,notes)
        SELECT id,stocktake_id,product_id,snapshot_qty,counted_qty,variance_qty,
          counted_unit_cost,variance_value,notes FROM stocktake_lines_legacy;
        DROP TABLE stocktake_lines_legacy;
        """)
    _execute(c, "CREATE INDEX IF NOT EXISTS idx_stocktake_lines_header ON stocktake_lines(stocktake_id,product_id,location_id,lot_id)")
    _execute(c, """CREATE UNIQUE INDEX IF NOT EXISTS idx_stocktake_line_scope
        ON stocktake_lines(stocktake_id,product_id,COALESCE(location_id,0),COALESCE(lot_id,0))""")
    stocktake_columns = _columns(c, "stocktakes")
    if "movement_cutoff_id" not in stocktake_columns:
        _execute(c, "ALTER TABLE stocktakes ADD COLUMN movement_cutoff_id INTEGER NOT NULL DEFAULT 0")

    # Unit catalog and additive links; legacy quantities remain untouched.
    execute_script(c,"""
    CREATE TABLE IF NOT EXISTS stock_uom_categories(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL UNIQUE,
      active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS stock_uoms(
      id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES stock_uom_categories(id),
      code TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL, factor_to_base TEXT NOT NULL,
      is_base INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
      UNIQUE(category_id,name_ar)
    );
    CREATE INDEX IF NOT EXISTS idx_stock_uoms_category ON stock_uoms(category_id,active);
    """)
    for code,label in [("count","العدد"),("mass","الوزن"),("volume","الحجم"),("length","الطول"),("area","المساحة"),("time","الزمن")]:
        _execute(c, "INSERT INTO stock_uom_categories(code,name_ar) VALUES(?,?) ON CONFLICT DO NOTHING",(code,label))
    seeded_units = [
      ("count","PCS","قطعة","1",1),("count","DOZ","دستة","12",0),
      ("mass","MG","ملليجرام","0.001",0),("mass","G","جرام","1",1),("mass","KG","كيلوجرام","1000",0),("mass","TON","طن متري","1000000",0),
      ("volume","ML","ملليلتر","1",1),("volume","L","لتر","1000",0),("volume","M3","متر مكعب","1000000",0),
      ("length","MM","ملليمتر","1",1),("length","CM","سنتيمتر","10",0),("length","M","متر","1000",0),("length","KM","كيلومتر","1000000",0),
      ("area","CM2","سنتيمتر مربع","1",1),("area","M2","متر مربع","10000",0),
      ("time","MIN","دقيقة","1",1),("time","HOUR","ساعة","60",0),("time","DAY","يوم","1440",0)
    ]
    for category_code,code,label,factor,is_base in seeded_units:
        category=_execute(c, "SELECT id FROM stock_uom_categories WHERE code=?",(category_code,)).fetchone()
        _execute(c, "INSERT INTO stock_uoms(category_id,code,name_ar,factor_to_base,is_base) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
            (category["id"],code,label,factor,is_base))
    product_columns=_columns(c, "stock_products")
    if "base_uom_id" not in product_columns:
        _execute(c, "ALTER TABLE stock_products ADD COLUMN base_uom_id INTEGER REFERENCES stock_uoms(id)")
    # Link existing products to a matching seeded unit by label; unmatched legacy labels remain intact.
    _execute(c, """UPDATE stock_products SET base_uom_id=(
        SELECT id FROM stock_uoms WHERE name_ar=stock_products.uom ORDER BY is_base DESC LIMIT 1
        ) WHERE base_uom_id IS NULL AND EXISTS(SELECT 1 FROM stock_uoms WHERE name_ar=stock_products.uom)""")
    execute_script(c,"""
    CREATE TABLE IF NOT EXISTS stock_product_uoms(
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES stock_products(id),
      uom_id INTEGER REFERENCES stock_uoms(id), custom_code TEXT NOT NULL,
      custom_name TEXT NOT NULL, factor_to_base TEXT NOT NULL,
      is_base INTEGER NOT NULL DEFAULT 0, purchase_enabled INTEGER NOT NULL DEFAULT 1,
      sales_enabled INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
      UNIQUE(product_id,custom_code), UNIQUE(product_id,uom_id)
    );
    CREATE INDEX IF NOT EXISTS idx_stock_product_uoms_product ON stock_product_uoms(product_id,active);
    """)
    # Ensure every linked product has a product-specific base unit mapping.
    for product in _execute(c, "SELECT id,base_uom_id FROM stock_products WHERE base_uom_id IS NOT NULL").fetchall():
        unit=_execute(c, "SELECT code,name_ar FROM stock_uoms WHERE id=?",(product["base_uom_id"],)).fetchone()
        if unit:
            _execute(c, """INSERT INTO stock_product_uoms
                (product_id,uom_id,custom_code,custom_name,factor_to_base,is_base,purchase_enabled,sales_enabled)
                VALUES(?,?,?,?, '1',1,1,1) ON CONFLICT DO NOTHING""",
                (product["id"],product["base_uom_id"],unit["code"],unit["name_ar"]))
    for table, fields in (
      ("purchase_order_lines",(("entered_quantity","TEXT"),("uom_id","INTEGER REFERENCES stock_uoms(id)"),("conversion_factor","TEXT NOT NULL DEFAULT '1'"))),
      ("goods_receipt_lines",(("entered_quantity","TEXT"),("uom_id","INTEGER REFERENCES stock_uoms(id)"),("conversion_factor","TEXT NOT NULL DEFAULT '1'"))),
    ):
        if not _table_exists(c, table):
            continue
        existing=_columns(c, table)
        for name,declaration in fields:
            if name not in existing: _execute(c, "ALTER TABLE "+table+" ADD COLUMN "+name+" "+declaration)

    movement_columns=_columns(c, "stock_movements")
    if "entered_quantity" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN entered_quantity TEXT")
    if "entered_uom_id" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN entered_uom_id INTEGER REFERENCES stock_uoms(id)")
    if "entered_uom_key" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN entered_uom_key TEXT")
    if "entered_uom_name" not in movement_columns:
        _execute(c, "ALTER TABLE stock_movements ADD COLUMN entered_uom_name TEXT")

    # Re-run after stock master tables exist so journal_lines receives the
    # nullable operational stock references on both fresh and upgraded DBs.
    initialize_analytic_dimensions_schema(c)
    # Conservative historical backfill: only journals linked to exactly one
    # stock movement, with one matching inventory-control line, are attributable.
    journal_columns = _columns(c, "journal_lines")
    if {"stock_product_id", "stock_warehouse_id"}.issubset(journal_columns):
        from activity_profiles import active_posting_accounts, resolve_product_account_code
        try:
            posting = active_posting_accounts(c)
            default_inventory = posting.get("inventory")
            if default_inventory:
                candidates = _execute(c, """SELECT m.id,m.journal_entry_id,m.movement_type,
                        m.product_id,m.source_warehouse_id,m.target_warehouse_id
                    FROM stock_movements m
                    WHERE m.journal_entry_id IS NOT NULL
                      AND m.movement_type IN ('receipt','issue','scrap')
                      AND (m.movement_type='receipt' AND m.target_warehouse_id IS NOT NULL
                           OR m.movement_type IN ('issue','scrap') AND m.source_warehouse_id IS NOT NULL)
                      AND NOT EXISTS (SELECT 1 FROM journal_lines jl
                        WHERE jl.entry_id=m.journal_entry_id
                          AND (jl.stock_product_id IS NOT NULL OR jl.stock_warehouse_id IS NOT NULL))
                    ORDER BY m.id""").fetchall()
                for movement in candidates:
                    movement_count = _execute(c, 
                        "SELECT COUNT(*) AS movement_count FROM stock_movements WHERE journal_entry_id=?",
                        (movement["journal_entry_id"],)
                    ).fetchone()["movement_count"]
                    if movement_count != 1:
                        continue
                    product = _execute(c, "SELECT id FROM stock_products WHERE id=?", (movement["product_id"],)).fetchone()
                    if not product:
                        continue
                    code = resolve_product_account_code(c, movement["product_id"], "inventory", default_inventory)
                    account = _execute(c, 
                        "SELECT id FROM accounts WHERE code=? AND active=1 AND is_group=0", (code,)
                    ).fetchone() if code else None
                    warehouse_id = movement["target_warehouse_id"] if movement["movement_type"] == "receipt" else movement["source_warehouse_id"]
                    if not account or not _execute(c, 
                        "SELECT 1 FROM stock_warehouses WHERE id=?", (warehouse_id,)
                    ).fetchone():
                        continue
                    matching_lines = _execute(c, """SELECT id FROM journal_lines
                        WHERE entry_id=? AND account_id=? AND stock_product_id IS NULL
                          AND stock_warehouse_id IS NULL""",
                        (movement["journal_entry_id"], account["id"])
                    ).fetchall()
                    if len(matching_lines) != 1:
                        continue
                    _execute(c, """UPDATE journal_lines SET stock_product_id=?,stock_warehouse_id=?
                        WHERE id=? AND stock_product_id IS NULL AND stock_warehouse_id IS NULL""",
                        (movement["product_id"], warehouse_id, matching_lines[0]["id"]))
        except (ValueError, *DB_ERROR):
            # A profile/account mapping that cannot be resolved is not grounds
            # to guess; the balance remains explicitly unallocated in the report.
            pass
    c.commit()
    c.close()

def product_uom_factor(c, product_id, uom_id, purpose=None):
    """Return the exact multiplier converting one selected product unit to base stock units."""
    product=_execute(c, "SELECT id,base_uom_id,uom FROM stock_products WHERE id=? AND active=1",(product_id,)).fetchone()
    if not product:
        raise ValueError("الصنف غير موجود أو غير نشط.")
    if not product["base_uom_id"]:
        if uom_id in (None, ""):
            return D("1"), product["uom"]
        raise ValueError("الصنف غير مربوط بوحدة قياس أساسية من الدليل؛ حدّث بياناته أولًا.")
    base=_execute(c, "SELECT category_id,factor_to_base,name_ar FROM stock_uoms WHERE id=? AND active=1",(product["base_uom_id"],)).fetchone()
    if not base:
        raise ValueError("وحدة الصنف الأساسية غير نشطة.")
    if uom_id in (None, ""):
        uom_id=product["base_uom_id"]
    raw_id=str(uom_id)
    if raw_id.startswith("P:"):
        package_id=int(raw_id[2:])
        package=_execute(c, """SELECT custom_name,factor_to_base,purchase_enabled,sales_enabled
            FROM stock_product_uoms WHERE product_id=? AND id=? AND active=1""",(product_id,package_id)).fetchone()
    else:
        package=None
        if raw_id.startswith("U:"): uom_id=int(raw_id[2:])
        else: uom_id=int(uom_id)
    if package:
        if purpose=="purchase" and not package["purchase_enabled"]:
            raise ValueError("وحدة التعبئة غير مفعلة للمشتريات لهذا الصنف.")
        if purpose=="sales" and not package["sales_enabled"]:
            raise ValueError("وحدة التعبئة غير مفعلة للمبيعات لهذا الصنف.")
        return D(str(package["factor_to_base"])), package["custom_name"]
    unit=_execute(c, "SELECT category_id,factor_to_base,name_ar FROM stock_uoms WHERE id=? AND active=1",(uom_id,)).fetchone()
    if not unit or unit["category_id"]!=base["category_id"]:
        raise ValueError("وحدة الحركة يجب أن تكون من نفس مجموعة وحدة الصنف.")
    factor=D(str(unit["factor_to_base"]))/D(str(base["factor_to_base"]))
    return factor,unit["name_ar"]


def product_uom_storage(c, product_id, uom_key):
    """Resolve a UI unit token to its FK-safe global unit id and stable display name."""
    if not uom_key:
        product=_execute(c, "SELECT base_uom_id,uom FROM stock_products WHERE id=?",(product_id,)).fetchone()
        return (product["base_uom_id"], product["uom"]) if product else (None,"")
    raw=str(uom_key)
    if raw.startswith("P:"):
        row=_execute(c, "SELECT uom_id,custom_name FROM stock_product_uoms WHERE product_id=? AND id=? AND active=1",
                      (product_id,int(raw[2:]))).fetchone()
        if not row: raise ValueError("وحدة التعبئة المحددة غير موجودة أو موقوفة.")
        return row["uom_id"],row["custom_name"]
    if raw.startswith("U:"):
        unit_id=int(raw[2:])
    else:
        unit_id=int(raw)
    row=_execute(c, "SELECT id,name_ar FROM stock_uoms WHERE id=? AND active=1",(unit_id,)).fetchone()
    if not row: raise ValueError("وحدة القياس المحددة غير موجودة أو غير نشطة.")
    return row["id"],row["name_ar"]


def product_uom_options(c, product_id):
    product=_execute(c, "SELECT base_uom_id FROM stock_products WHERE id=?",(product_id,)).fetchone()
    if not product or not product["base_uom_id"]:
        return []
    base=_execute(c, "SELECT category_id,factor_to_base FROM stock_uoms WHERE id=?",(product["base_uom_id"],)).fetchone()
    if not base:
        return []
    rows=_execute(c, """SELECT id,custom_name name_ar,custom_code code,uom_id,factor_to_base,is_base,purchase_enabled,sales_enabled
        FROM stock_product_uoms WHERE product_id=? AND active=1 ORDER BY is_base DESC,custom_name""",(product_id,)).fetchall()
    result=[dict(row,id="P:"+str(row["id"])) for row in rows]
    # Standard units from the same dimension are available automatically as conversions.
    standard=_execute(c, "SELECT id,name_ar,code,factor_to_base FROM stock_uoms WHERE category_id=? AND active=1",(base["category_id"],)).fetchall()
    known={row["uom_id"] for row in _execute(c, "SELECT uom_id FROM stock_product_uoms WHERE product_id=? AND uom_id IS NOT NULL",(product_id,)).fetchall()}
    for unit in standard:
        if unit["id"] in known:
            continue
        factor=D(str(unit["factor_to_base"]))/D(str(base["factor_to_base"]))
        result.append({"id":"U:"+str(unit["id"]),"name_ar":unit["name_ar"],"code":unit["code"],
            "factor_to_base":str(factor),"purchase_enabled":1,"sales_enabled":1,"is_base":int(unit["id"]==product["base_uom_id"])})
    result.sort(key=lambda item:(not bool(item.get("is_base")),item["name_ar"]))
    return result


def _movement_activity_code(c, movement_id):
    columns = _columns(c, "stock_movements")
    if "activity_code" not in columns:
        return None
    row = _execute(c, "SELECT activity_code FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    return str(row["activity_code"]).strip().lower() if row and row["activity_code"] else None


def post_interbranch_transfer_journal(c, movement_id, movement_no, product, movement_date,
                                      total_cost, created_by, source_branch_id, target_branch_id):
    """Post branch-tagged inventory transfer clearing lines without changing consolidated stock value."""
    if source_branch_id is None and target_branch_id is None:
        _execute(c, "UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        set_stock_accounting_note(c,movement_id,None)
        return None
    if source_branch_id is None or target_branch_id is None:
        raise ValueError(
            "لا يمكن اعتماد التحويل قبل ربط مخزني المصدر والوجهة بفرعيهما؛ "
            "تحديد فرع واحد فقط لا يكفي لتسجيل قيد التحويل البيني."
        )
    if source_branch_id == target_branch_id:
        _execute(c, "UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        set_stock_accounting_note(c,movement_id,None)
        return None
    existing = _execute(c, "SELECT journal_entry_id FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    if existing and existing["journal_entry_id"]:
        return existing["journal_entry_id"]
    activity_code = _movement_activity_code(c, movement_id)
    clearing_code = "1185"
    if activity_code:
        from company_activities import resolve_activity_profile
        clearing_code = resolve_activity_profile(c, activity_code).get("posting_accounts", {}).get("interbranch_transfer") or clearing_code
    clearing = _execute(c,
        "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (clearing_code,)
    ).fetchone()
    if not clearing or clearing["is_group"] or not clearing["active"] or clearing["account_type"] != "asset":
        raise ValueError(f"لا يمكن ترحيل التحويل بين الفروع قبل تفعيل الحساب التفصيلي {clearing_code} (تحويلات مخزون بين الفروع) من نوع أصل.")
    amount = money(D(str(total_cost)))
    if amount <= 0:
        _execute(c, "UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        return None
    entry_id = post_journal(
        c, movement_date, movement_no,
        f"قيد تحويل مخزون بين الفروع - {product['name_ar']}",
        [
            (clearing["id"], amount, 0, "مدين - مخزون مستلم بالفرع الوجهة",
             {"branch_id": target_branch_id}),
            (clearing["id"], 0, amount, "دائن - مخزون محول من الفرع المصدر",
             {"branch_id": source_branch_id}),
        ], created_by,
    )
    _execute(c, "UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?",
              (entry_id, movement_id))
    return entry_id


def set_stock_accounting_note(c, movement_id, note):
    """Write a pending reason when the additive note column exists (legacy unit schemas may omit it)."""
    columns=_columns(c, "stock_movements")
    if "accounting_note" in columns:
        _execute(c, "UPDATE stock_movements SET accounting_note=? WHERE id=?",(note,movement_id))


def post_scrap_journal(c, movement_id, movement_no, product, movement_date, total_cost, created_by):
    """Post an approved scrap movement only when explicit chart accounts exist."""
    existing = _execute(c, "SELECT journal_entry_id FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    if existing and existing["journal_entry_id"]:
        return existing["journal_entry_id"]
    activity_code = _movement_activity_code(c, movement_id)
    if activity_code:
        from company_activities import resolve_activity_profile
        profile = resolve_activity_profile(c, activity_code)
        posting_accounts = profile.get("posting_accounts", {})
        activity = activity_code
    else:
        settings = {row["setting_key"]: row["setting_value"] for row in _execute(c,
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key='activity'")}
        activity = settings.get("activity", "commercial")
        from chart_templates import ACTIVITY_POSTING_ACCOUNTS
        posting_accounts = ACTIVITY_POSTING_ACCOUNTS.get(activity, {})
    default_inventory_code = posting_accounts.get("inventory")
    if not default_inventory_code:
        _execute(c, "UPDATE stock_movements SET accounting_status='pending_setup' WHERE id=?", (movement_id,))
        set_stock_accounting_note(c,movement_id,"النشاط الحالي لا يحتوي حساب مخزون؛ لا يمكن ترحيل الإعدام قبل ضبط ملف النشاط.")
        return None
    from activity_profiles import resolve_product_account_code
    inventory_code = resolve_product_account_code(
        c, product["id"], "inventory", default_inventory_code
    )
    inventory = _execute(c, "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (inventory_code,)).fetchone()
    # Restaurant waste is classified in the dedicated spoilage expense account.
    writeoff_code = posting_accounts.get("stock_scrap_expense") or ("5300" if activity == "restaurant" else "6990")
    writeoff = _execute(c, "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (writeoff_code,)).fetchone()
    writeoff_id = writeoff["id"] if writeoff and not writeoff["is_group"] and writeoff["active"] and writeoff["account_type"]=="expense" else None
    if (not inventory or inventory["is_group"] or not inventory["active"]
            or inventory["account_type"] != "asset" or not writeoff_id):
        reason=(f"تعذر ترحيل الإعدام: راجع حساب المخزون {inventory_code} "
                f"وحساب الهالك {writeoff_code}؛ يجب أن يكونا حسابين تفصيليين نشطين بالنوع الصحيح.")
        _execute(c, "UPDATE stock_movements SET accounting_status='pending_setup' WHERE id=?", (movement_id,))
        set_stock_accounting_note(c,movement_id,reason)
        return None
    amount = money(D(str(total_cost)))
    if amount <= 0:
        _execute(c, "UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        return None
    movement_columns = _columns(c, "stock_movements")
    journal_columns = _columns(c, "journal_lines")
    stock_dims = None
    if {"product_id", "source_warehouse_id"}.issubset(movement_columns) and {
        "stock_product_id", "stock_warehouse_id"
    }.issubset(journal_columns):
        movement = _execute(c, 
            "SELECT product_id,source_warehouse_id FROM stock_movements WHERE id=?", (movement_id,)
        ).fetchone()
        if not movement or int(movement["product_id"]) != int(product["id"]) or not movement["source_warehouse_id"]:
            raise ValueError("تعذر تحديد الصنف والمخزن المصدر لحركة الإعدام؛ لم يتم ترحيل القيد.")
        stock_dims = {
            "stock_product_id": int(movement["product_id"]),
            "stock_warehouse_id": int(movement["source_warehouse_id"]),
        }
    inventory_line = (inventory["id"],0,amount,"تخفيض قيمة المخزون", stock_dims) if stock_dims else (
        inventory["id"],0,amount,"تخفيض قيمة المخزون"
    )
    entry_id = post_journal(c,movement_date,movement_no,
        f"قيد آلي لإعدام مخزون - {product['name_ar']}",
        [(writeoff_id,amount,0,"خسائر تالف مخزون"), inventory_line],created_by)
    _execute(c, "UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?", (entry_id,movement_id))
    set_stock_accounting_note(c,movement_id,None)
    return entry_id


def post_issue_journal(c, movement_id, movement_no, product, movement_date, total_cost, created_by, purpose, dimensions=None):
    """Create the GL entry for an issue using the valuation cost consumed in this transaction."""
    existing = _execute(c, "SELECT journal_entry_id FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    if existing and existing["journal_entry_id"]:
        return existing["journal_entry_id"]
    activity_code = _movement_activity_code(c, movement_id)
    if activity_code:
        from company_activities import resolve_activity_profile
        posting_accounts = resolve_activity_profile(c, activity_code).get("posting_accounts", {})
        activity = activity_code
    else:
        settings = {row["setting_key"]: row["setting_value"] for row in _execute(c,
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key='activity'")}
        activity = settings.get("activity", "commercial")
        from chart_templates import ACTIVITY_POSTING_ACCOUNTS
        if purpose == "sale":
            from activity_profiles import active_posting_accounts
            posting_accounts = active_posting_accounts(c)
        else:
            posting_accounts = ACTIVITY_POSTING_ACCOUNTS.get(activity, {})
    if purpose == "sale":
        from activity_profiles import resolve_product_account_code
        expense_code = resolve_product_account_code(
            c, product["id"], "cogs", posting_accounts.get("cogs")
        )
        expense_name = "تكلفة المبيعات / تكلفة الخدمات المصروفة"
    elif purpose == "internal":
        expense_code = posting_accounts.get("internal_stock_use", "6991")
        expense_name = "مصروف استخدام داخلي من المخزون"
        existing_expense = _execute(c, "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (expense_code,)).fetchone()
        expense_id = existing_expense["id"] if existing_expense and not existing_expense["is_group"] and existing_expense["active"] and existing_expense["account_type"]=="expense" else None
    else:
        _execute(c, "UPDATE stock_movements SET accounting_status='pending_purpose' WHERE id=?", (movement_id,))
        set_stock_accounting_note(c,movement_id,"حدد غرض الصرف: تكلفة مبيعات أو استخدام داخلي قبل ترحيل القيد.")
        return None
    if purpose == "sale":
        expense = _execute(c, "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (expense_code,)).fetchone() if expense_code else None
        expense_id = expense["id"] if expense and not expense["is_group"] and expense["active"] and expense["account_type"] == "expense" else None
    from activity_profiles import resolve_product_account_code
    inventory_code = resolve_product_account_code(
        c, product["id"], "inventory", posting_accounts.get("inventory")
    )
    inventory = _execute(c, "SELECT id,is_group,active,account_type FROM accounts WHERE code=?", (inventory_code,)).fetchone() if inventory_code else None
    if not expense_id or not inventory or inventory["is_group"] or not inventory["active"] or inventory["account_type"] != "asset":
        reason=(f"تعذر ترحيل الصرف: راجع حساب المصروف {expense_code or 'غير محدد'} "
                f"وحساب المخزون {inventory_code or 'غير محدد'}؛ يجب أن يكونا حسابين تفصيليين نشطين بالنوع الصحيح.")
        _execute(c, "UPDATE stock_movements SET accounting_status='pending_setup' WHERE id=?", (movement_id,))
        set_stock_accounting_note(c,movement_id,reason)
        return None
    amount = money(D(str(total_cost)))
    if amount <= 0:
        _execute(c, "UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        return None
    movement_columns = _columns(c, "stock_movements")
    journal_columns = _columns(c, "journal_lines")
    stock_dims = None
    if {"product_id", "source_warehouse_id"}.issubset(movement_columns) and {
        "stock_product_id", "stock_warehouse_id"
    }.issubset(journal_columns):
        movement = _execute(c, 
            "SELECT product_id,source_warehouse_id FROM stock_movements WHERE id=?", (movement_id,)
        ).fetchone()
        if not movement or int(movement["product_id"]) != int(product["id"]) or not movement["source_warehouse_id"]:
            raise ValueError("تعذر تحديد الصنف والمخزن المصدر لحركة الصرف؛ لم يتم ترحيل القيد.")
        stock_dims = {
            "stock_product_id": int(movement["product_id"]),
            "stock_warehouse_id": int(movement["source_warehouse_id"]),
        }
    inventory_line = (inventory["id"],0,amount,"تخفيض المخزون بتكلفة الصرف", stock_dims) if stock_dims else (
        inventory["id"],0,amount,"تخفيض المخزون بتكلفة الصرف"
    )
    entry_id = post_journal(c,movement_date,movement_no,
        f"قيد آلي لصرف مخزون - {product['name_ar']}",
        [(expense_id,amount,0,expense_name), inventory_line],created_by,
        dimensions=dimensions)
    _execute(c, "UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?", (entry_id, movement_id))
    set_stock_accounting_note(c,movement_id,None)
    return entry_id



def ensure_stock_date_open(conn, value, label="تاريخ حركة المخزون"):
    """Prevent stock subledger changes inside a locked accounting period or closed year."""
    try:
        normalized_date = date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError(label + " غير صحيح؛ استخدم YYYY-MM-DD.")
    if normalized_date != str(value):
        raise ValueError(label + " غير صحيح؛ استخدم YYYY-MM-DD.")

    tables = {name for name in ("accounting_periods", "financial_years") if _table_exists(conn, name)}
    if "accounting_periods" in tables:
        locked = _execute(conn, 
            """SELECT period_name FROM accounting_periods
               WHERE locked=1 AND start_date<=? AND end_date>=? LIMIT 1""",
            (normalized_date, normalized_date),
        ).fetchone()
        if locked:
            raise ValueError("الفترة المحاسبية مغلقة: " + locked["period_name"] + ".")
    if "financial_years" in tables:
        year = _execute(conn, 
            """SELECT year_name,status FROM financial_years
               WHERE start_date<=? AND end_date>=? LIMIT 1""",
            (normalized_date, normalized_date),
        ).fetchone()
        if year and year["status"] != "open":
            raise ValueError("لا يمكن تعديل المخزون داخل السنة المالية " + year["year_name"] + " لأنها مغلقة.")


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
    """Return the exact on-hand quantity for one product/warehouse/lot.

    Movement quantities are stored as decimal strings, so aggregate them with
    Decimal in Python rather than SQLite REAL arithmetic.
    """
    rows = _execute(c, 
        """SELECT movement_type,source_warehouse_id,target_warehouse_id,lot_id,quantity
           FROM stock_movements
           WHERE product_id=?
             AND ((movement_type='receipt' AND target_warehouse_id=?)
               OR (movement_type IN ('issue','scrap') AND source_warehouse_id=?)
               OR (movement_type='transfer' AND
                   (source_warehouse_id=? OR target_warehouse_id=?)))
             AND (lot_id = ? OR (lot_id IS NULL AND CAST(? AS BIGINT) IS NULL))""",
        (product_id, warehouse_id, warehouse_id, warehouse_id, warehouse_id, lot_id, lot_id)
    ).fetchall()
    balance = D("0")
    for row in rows:
        qty = D(str(row["quantity"] or "0"))
        if row["movement_type"] == "receipt" and row["target_warehouse_id"] == warehouse_id:
            balance += qty
        elif row["movement_type"] in ("issue", "scrap") and row["source_warehouse_id"] == warehouse_id:
            balance -= qty
        elif row["movement_type"] == "transfer":
            if row["target_warehouse_id"] == warehouse_id:
                balance += qty
            if row["source_warehouse_id"] == warehouse_id:
                balance -= qty
    return balance


def available_layers(c, product_id, warehouse_id, lot_id=None, location_id=None):
    positive_qty_sql = decimal_compare_expression(c, "qty_remaining") + " > 0"
    sql = f"""SELECT * FROM stock_valuation_layers WHERE product_id=? AND warehouse_id=?
             AND {positive_qty_sql}"""
    args = [product_id, warehouse_id]
    if lot_id is not None:
        if lot_id == 0:
            sql += " AND lot_id IS NULL"
        else:
            sql += " AND lot_id=?"; args.append(lot_id)
    if location_id is not None:
        if location_id == 0:
            sql += " AND location_id IS NULL"
        else:
            sql += " AND location_id=?"; args.append(location_id)
    sql += " ORDER BY id"
    # Concurrent PostgreSQL issues must serialize on the exact valuation rows.
    # SQLite already serializes writers through BEGIN IMMEDIATE.
    if database_dialect(c) == "postgresql":
        sql += " FOR UPDATE"
    return _execute(c, sql, args).fetchall()


def consume_layers(c, product_id, warehouse_id, qty, valuation_method, lot_id=None, location_id=None, include_layer_details=False):
    # Validate the public costing boundary before acquiring locks or touching layers.
    # A malformed quantity or unknown method must never be interpreted as a zero-cost
    # issue or silently fall through to FIFO.
    try:
        qty = D(str(qty))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("كمية الصرف يجب أن تكون رقمًا موجبًا وصالحًا.")

    if not qty.is_finite() or qty <= 0:
        raise ValueError("كمية الصرف يجب أن تكون أكبر من صفر.")

    valuation_method = str(valuation_method or "").strip().lower()
    if valuation_method not in {"fifo", "average"}:
        raise ValueError("طريقة تقييم المخزون غير معتمدة. اختر FIFO أو المتوسط المرجح.")

    # Lock the warehouse before valuation rows so sales, returns, transfers and counts
    # share one deterministic serialization point on PostgreSQL.
    lock_stock_warehouses(c, warehouse_id)
    layers = available_layers(c, product_id, warehouse_id, lot_id, location_id)
    # Treat valuation-layer corruption as a hard stop. Reconciliation reports the
    # underlying issue, but an operational issue must never consume a negative,
    # non-finite, or non-numeric carrying value and create a misleading GL cost.
    normalized_layers = []
    for layer in layers:
        try:
            layer_qty = D(str(layer["qty_remaining"]))
            unit_cost = D(str(layer["unit_cost"]))
            layer_value = (
                D(str(layer["value_remaining"]))
                if layer["value_remaining"] not in (None, "")
                else layer_qty * unit_cost
            )
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("طبقة تكلفة المخزون تحتوي على كمية أو تكلفة غير رقمية؛ أوقف الصرف وراجع تقرير المطابقة.")
        if (not layer_qty.is_finite() or layer_qty <= 0
                or not unit_cost.is_finite() or unit_cost < 0
                or not layer_value.is_finite() or layer_value < 0):
            raise ValueError("طبقة تكلفة المخزون تحتوي على قيمة سالبة أو غير صالحة؛ أوقف الصرف وراجع تقرير المطابقة.")
        normalized_layers.append((layer, layer_qty, unit_cost, layer_value))
    available = sum((item[1] for item in normalized_layers), D("0"))
    if available < qty:
        raise ValueError(f"الرصيد غير كافٍ. المتاح {q4(available)} والمطلوب {q4(qty)}.")
    total_value = sum((item[3] for item in normalized_layers), D("0"))
    avg_cost = total_value / available if available else D("0")
    target_issue_cost = money(qty * avg_cost) if valuation_method == "average" else None
    remaining_value_after_issue = (total_value - target_issue_cost) if valuation_method == "average" else None
    remaining_value_allocated = D("0")
    # Identify the final layer that retains stock so any cent-rounding remainder
    # is assigned there and the sum of layer values stays equal to the GL value.
    quantity_to_issue = qty
    remaining_quantities = []
    for candidate, candidate_qty, _, _ in normalized_layers:
        candidate_take = min(candidate_qty, quantity_to_issue) if quantity_to_issue > 0 else D("0")
        remaining_quantities.append(candidate_qty - candidate_take)
        quantity_to_issue -= candidate_take
    last_remaining_index = next(
        (index for index in range(len(remaining_quantities) - 1, -1, -1)
         if remaining_quantities[index] > 0), None
    )
    remaining, cost_total, allocations, layer_details = qty, D("0"), [], []
    for layer_index, (layer, layer_qty, _, layer_value) in enumerate(normalized_layers):
        if remaining <= 0 and valuation_method != "average":
            break
        take = min(layer_qty, remaining) if remaining > 0 else D("0")
        if valuation_method == "average":
            # Moving average applies to every issued unit, including a whole layer.
            # Allocate issue cost and rebase the remaining layer values to the
            # same weighted average, preserving the total inventory carrying value.
            cost = (target_issue_cost - cost_total) if take == remaining else money(take * avg_cost)
        elif take == layer_qty:
            cost = layer_value
        else:
            cost = money(layer_value * take / layer_qty)
        unit_cost = cost / take if take else D("0")
        new_remaining = layer_qty - take
        if valuation_method == "average":
            if new_remaining <= 0:
                new_value = D("0")
            elif layer_index == last_remaining_index:
                new_value = remaining_value_after_issue - remaining_value_allocated
            else:
                new_value = money(new_remaining * avg_cost)
            remaining_value_allocated += new_value
        else:
            new_value = layer_value - cost
        _execute(c, "UPDATE stock_valuation_layers SET qty_remaining=?,value_remaining=? WHERE id=?",
                  (str(q4(new_remaining)), format(new_value, "f"), layer["id"]))
        if take > 0:
            allocations.append((layer["lot_id"], take, unit_cost, cost))
            if include_layer_details:
                layer_details.append({"layer_id": int(layer["id"]), "lot_id": layer["lot_id"],
                    "location_id": layer["location_id"], "receipt_line_id": layer["receipt_line_id"],
                    "quantity": take, "unit_cost": unit_cost, "total_cost": cost})
        cost_total += cost
        remaining -= take
    return (money(cost_total), allocations, layer_details) if include_layer_details else (money(cost_total), allocations)


def ensure_lot_not_expired(lot, movement_date):
    """Validate lot expiry against the stock movement date, not today's date."""
    expiry_value = lot["expiry_date"] if lot else None
    if not expiry_value:
        return
    try:
        expiry = date.fromisoformat(str(expiry_value))
        effective_date = date.fromisoformat(str(movement_date))
    except (TypeError, ValueError):
        raise ValueError("تاريخ الصلاحية أو تاريخ حركة المخزون غير صحيح.")
    if expiry < effective_date:
        raise ValueError("لا يمكن صرف تشغيلة منتهية الصلاحية في تاريخ الحركة المحدد.")


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
    _execute(c, """INSERT INTO stock_lots(product_id,lot_number,production_date,expiry_date)
                 VALUES(?,?,?,?) ON CONFLICT DO NOTHING""", (product["id"],lot_number,production_date or None,expiry_date or None))
    lot = _execute(c, "SELECT * FROM stock_lots WHERE product_id=? AND lot_number=?", (product["id"],lot_number)).fetchone()
    if lot["expiry_date"] != (expiry_date or None) or lot["production_date"] != (production_date or None):
        raise ValueError("رقم التشغيلة موجود بتواريخ إنتاج أو صلاحية مختلفة.")
    return lot["id"]


def calculate_warehouse_reorder_alerts(conn, warehouse_totals):
    """Return reorder alerts by product and warehouse, never by consolidated product stock."""
    alerts=[]
    settings=_execute(conn, """SELECT s.*,p.sku,p.name_ar,p.uom,w.code warehouse_code,w.name_ar warehouse_name
        FROM stock_warehouse_product_settings s
        JOIN stock_products p ON p.id=s.product_id AND p.active=1
        JOIN stock_warehouses w ON w.id=s.warehouse_id AND w.active=1
        ORDER BY w.code,p.sku""").fetchall()
    for setting in settings:
        current=warehouse_totals.get((setting["product_id"],setting["warehouse_id"]),{"qty":D("0")})["qty"]
        point=D(str(setting["reorder_point"] or "0"))
        minimum=D(str(setting["min_qty"] or "0"))
        threshold=point if point>0 else minimum
        suggested=D(str(setting["reorder_qty"] or "0"))
        if threshold>0 and current<=threshold:
            if suggested<=0:
                suggested=max(minimum,threshold-current)
            alerts.append({"sku":setting["sku"],"name_ar":setting["name_ar"],"uom":setting["uom"],
                "warehouse_code":setting["warehouse_code"],"warehouse_name":setting["warehouse_name"],
                "qty":current,"point":threshold,"minimum":minimum,"suggested":suggested})
    return alerts


@stock_bp.route("/")
@stock_permission("stock.view")
def index():
    c=get_db()
    products=_execute(c, "SELECT COUNT(*) n FROM stock_products WHERE active=1").fetchone()["n"]
    warehouses=_execute(c, "SELECT COUNT(*) n FROM stock_warehouses WHERE active=1").fetchone()["n"]
    qty_sum_sql = decimal_sum_expression(c, "l.qty_remaining")
    rows=_execute(c, f"""SELECT p.id,p.sku,p.name_ar,p.uom,
      {qty_sum_sql} qty
      FROM stock_products p LEFT JOIN stock_valuation_layers l ON l.product_id=p.id
      WHERE p.active=1 GROUP BY p.id ORDER BY p.name_ar LIMIT 10""").fetchall()
    today=date.today()
    product_qty={}; lot_qty={}
    for layer in _execute(c, "SELECT product_id,lot_id,qty_remaining FROM stock_valuation_layers").fetchall():
        quantity=Decimal(str(layer["qty_remaining"] or "0"))
        product_qty[layer["product_id"]]=product_qty.get(layer["product_id"],Decimal("0"))+quantity
        if layer["lot_id"] is not None:
            lot_qty[layer["lot_id"]]=lot_qty.get(layer["lot_id"],Decimal("0"))+quantity
    dashboard_warehouse_totals={}
    positive_qty_sql = decimal_compare_expression(c, "qty_remaining") + " > 0"
    for layer in _execute(c, f"""SELECT warehouse_id,product_id,qty_remaining
        FROM stock_valuation_layers WHERE {positive_qty_sql}""").fetchall():
        key=(layer["product_id"],layer["warehouse_id"])
        total=dashboard_warehouse_totals.setdefault(key,{"qty":Decimal("0")})
        total["qty"]+=Decimal(str(layer["qty_remaining"] or "0"))
    reorder_alerts=calculate_warehouse_reorder_alerts(c,dashboard_warehouse_totals)
    reorder_alert_count=len(reorder_alerts)
    low_stock_skus={str(item.get("sku") or "") for item in reorder_alerts}
    dashboard_rows=[]
    for row in rows:
        item=dict(row)
        quantity=Decimal(str(item.get("qty") or "0"))
        if quantity <= 0:
            item["availability_label"]="غير متوفر"
            item["availability_class"]="state-unpaid"
        elif item["sku"] in low_stock_skus:
            item["availability_label"]="رصيد منخفض"
            item["availability_class"]="state-partial"
        else:
            item["availability_label"]="متوفر"
            item["availability_class"]="state-paid"
        dashboard_rows.append(item)
    expiry_alert_count=0
    for lot in _execute(c, """SELECT id,expiry_date FROM stock_lots WHERE expiry_date IS NOT NULL""").fetchall():
        if (date.fromisoformat(lot["expiry_date"])-today).days<=30 and lot_qty.get(lot["id"],Decimal("0"))>0:
            expiry_alert_count+=1
    c.close()
    return render_template("stock_dashboard.html",products=products,warehouses=warehouses,rows=dashboard_rows,
        reorder_alert_count=reorder_alert_count,expiry_alert_count=expiry_alert_count)


@stock_bp.route("/products", methods=["GET","POST"])
@stock_permission("stock.manage")
def products():
    c=get_db()
    categories=_execute(c, "SELECT id,code,name_ar FROM stock_categories WHERE active=1 ORDER BY name_ar").fetchall()
    if request.method=="POST":
        try:
            name=request.form.get("name_ar","").strip()
            base_uom_id=request.form.get("base_uom_id",type=int)
            selected_uom=_execute(c, "SELECT id,name_ar FROM stock_uoms WHERE id=? AND active=1",(base_uom_id,)).fetchone() if base_uom_id else None
            uom=selected_uom["name_ar"] if selected_uom else ""
            category_id=request.form.get("category_id",type=int)
            category=_execute(c, "SELECT name_ar FROM stock_categories WHERE id=? AND active=1",(category_id,)).fetchone() if category_id else None
            if not name or not selected_uom: raise ValueError("اسم الصنف ووحدة القياس حقول مطلوبة.")
            sku = next_code(c, "product", "ITM", "stock_products", "sku")
            if category_id and not category: raise ValueError("مجموعة الصنف غير صحيحة.")
            purchase_price=money(dec(request.form.get("purchase_price","0"),"سعر الشراء"))
            sale_price=money(dec(request.form.get("sale_price","0"),"سعر البيع"))
            if purchase_price<0 or sale_price<0:
                raise ValueError("الأسعار لا يمكن أن تكون سالبة.")
            product_type = request.form.get("product_type","goods").strip()
            if product_type == "goods":
                from activity_profiles import active_posting_accounts, resolve_product_account_code
                default_inventory = active_posting_accounts(c).get("inventory")
                inventory_code = resolve_product_account_code(c, None, "inventory", default_inventory)
                inventory = _execute(c, 
                    "SELECT code,is_group,active,account_type FROM accounts WHERE code=?",
                    (inventory_code,)
                ).fetchone() if inventory_code else None
                if (not inventory or inventory["is_group"] or not inventory["active"]
                        or inventory["account_type"] != "asset"):
                    raise ValueError(
                        "لا يمكن إنشاء صنف من نوع بضاعة في النشاط الخدمي قبل ضبط حساب مخزون تفصيلي نشط من نوع أصل في ملف النشاط."
                    )
            _execute(c, """INSERT INTO stock_products(sku,name_ar,category,category_id,product_type,uom,base_uom_id,barcode,track_lot,track_expiry,purchase_price,sale_price)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(sku,name,category["name_ar"] if category else "",category_id,
              product_type,uom,base_uom_id,request.form.get("barcode","").strip() or None,
              int(request.form.get("track_lot")=="on"),int(request.form.get("track_expiry")=="on"),
              str(purchase_price),str(sale_price)))
            c.commit(); flash("تم حفظ الصنف.", "success")
        except (ValueError,*DB_INTEGRITY_ERROR) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود الصنف أو الباركود مستخدم بالفعل.", "error")
        finally: c.close()
        return redirect(url_for("stock.products"))
    uoms=_execute(c, "SELECT u.id,u.code,u.name_ar,u.category_id,c.name_ar category_name FROM stock_uoms u JOIN stock_uom_categories c ON c.id=u.category_id WHERE u.active=1 AND c.active=1 ORDER BY c.id,u.is_base DESC,u.name_ar").fetchall()
    q=request.args.get("q","").strip()
    category_filter=request.args.get("category_id","").strip()
    sql="SELECT p.*,sc.name_ar category_name FROM stock_products p LEFT JOIN stock_categories sc ON sc.id=p.category_id WHERE 1=1"
    args=[]
    if q:
        sql+=" AND (p.sku LIKE ? OR p.name_ar LIKE ? OR p.barcode LIKE ?)"
        args.extend([f"%{q}%"]*3)
    if category_filter.isdigit():
        sql+=" AND p.category_id=?";args.append(int(category_filter))
    count_sql="SELECT COUNT(*) FROM stock_products p LEFT JOIN stock_categories sc ON sc.id=p.category_id WHERE 1=1"
    for condition in (" AND (p.sku LIKE ? OR p.name_ar LIKE ? OR p.barcode LIKE ?)",
                      " AND p.category_id=?"):
        if condition in sql:
            count_sql += condition
    products_page = fetch_page(c,sql+" ORDER BY p.sku",count_sql,args,
        page=request.args.get("products_page",1,type=int),
        per_page=request.args.get("products_per_page",50,type=int))
    rows=products_page["rows"]; c.close()
    return render_template("stock_products.html",rows=rows,categories=categories,uoms=uoms,search=q,
        category_filter=category_filter,products_page=products_page)



@stock_bp.route("/product-uoms", methods=["GET","POST"])
@stock_permission("stock.manage")
def product_uoms():
    c=get_db()
    if request.method=="POST":
        try:
            product_id=request.form.get("product_id",type=int)
            name=request.form.get("unit_name","").strip()
            code=request.form.get("unit_code","").strip().upper()
            factor=dec(request.form.get("factor_to_base",""),"معامل التحويل")
            product=_execute(c, """SELECT p.id,p.base_uom_id,u.category_id FROM stock_products p
                JOIN stock_uoms u ON u.id=p.base_uom_id WHERE p.id=? AND p.active=1""",(product_id,)).fetchone()
            if not product: raise ValueError("اختر صنفًا نشطًا مرتبطًا بوحدة أساسية.")
            if not name or not code or factor<=0: raise ValueError("اسم وكود الوحدة ومعامل تحويل أكبر من صفر مطلوبة.")
            if factor==1: raise ValueError("معامل الوحدة الإضافية يجب أن يختلف عن 1؛ وحدة الأساس موجودة تلقائيًا.")
            _execute(c, """INSERT INTO stock_product_uoms(product_id,custom_code,custom_name,factor_to_base,
                purchase_enabled,sales_enabled) VALUES(?,?,?,?,?,?)""",
                (product_id,code,name,str(factor),int(request.form.get("purchase_enabled")=="on"),
                 int(request.form.get("sales_enabled")=="on")))
            c.commit(); flash("تمت إضافة وحدة التعبئة الخاصة بالصنف.","success")
        except (ValueError,*DB_INTEGRITY_ERROR) as exc:
            c.rollback(); flash(str(exc) if isinstance(exc,ValueError) else "كود الوحدة مستخدم لهذا الصنف بالفعل.","error")
        finally: c.close()
        return redirect(url_for("stock.product_uoms"))
    products=_execute(c, """SELECT p.id,p.sku,p.name_ar,p.uom FROM stock_products p
        WHERE p.active=1 AND p.base_uom_id IS NOT NULL ORDER BY p.name_ar""").fetchall()
    rows=_execute(c, """SELECT x.*,p.sku,p.name_ar product_name FROM stock_product_uoms x
        JOIN stock_products p ON p.id=x.product_id WHERE x.is_base=0 ORDER BY p.name_ar,x.custom_name""").fetchall()
    c.close()
    return render_template("stock_product_uoms.html",products=products,rows=rows)


@stock_bp.route("/uoms", methods=["GET","POST"])
@stock_permission("stock.manage")
def uoms():
    c=get_db()
    if request.method=="POST":
        try:
            action=request.form.get("action","unit")
            if action=="category":
                code=request.form.get("code","").strip().upper()
                name=request.form.get("name_ar","").strip()
                if not code or not name: raise ValueError("كود واسم مجموعة الوحدات مطلوبان.")
                _execute(c, "INSERT INTO stock_uom_categories(code,name_ar) VALUES(?,?)",(code,name))
                flash("تم إنشاء مجموعة وحدات القياس.","success")
            else:
                category_id=request.form.get("category_id",type=int)
                code=request.form.get("unit_code","").strip().upper()
                name=request.form.get("unit_name","").strip()
                factor=dec(request.form.get("factor_to_base",""),"معامل التحويل")
                is_base=int(request.form.get("is_base")=="on")
                category=_execute(c, "SELECT id FROM stock_uom_categories WHERE id=? AND active=1",(category_id,)).fetchone()
                if not category or not code or not name or factor<=0: raise ValueError("أدخل المجموعة والكود والاسم ومعامل تحويل أكبر من صفر.")
                existing_base=_execute(c, "SELECT 1 FROM stock_uoms WHERE category_id=? AND is_base=1 AND active=1",(category_id,)).fetchone()
                if is_base and factor!=1: raise ValueError("وحدة الأساس يجب أن يكون معاملها 1.")
                if is_base and existing_base:
                    raise ValueError("يوجد بالفعل وحدة أساس نشطة لهذه المجموعة.")
                if not is_base and not existing_base:
                    raise ValueError("أضف وحدة الأساس أولًا لهذه المجموعة بمعامل تحويل يساوي 1.")
                _execute(c, "INSERT INTO stock_uoms(category_id,code,name_ar,factor_to_base,is_base) VALUES(?,?,?,?,?)",
                    (category_id,code,name,str(factor),is_base))
                flash("تم حفظ وحدة القياس.","success")
            c.commit()
        except (ValueError,*DB_INTEGRITY_ERROR) as exc:
            c.rollback(); flash(str(exc) if isinstance(exc,ValueError) else "الكود أو الاسم مستخدم بالفعل.","error")
        finally: c.close()
        return redirect(url_for("stock.uoms"))
    categories=_execute(c, "SELECT * FROM stock_uom_categories WHERE active=1 ORDER BY name_ar").fetchall()
    rows=_execute(c, "SELECT u.*,c.name_ar category_name FROM stock_uoms u JOIN stock_uom_categories c ON c.id=u.category_id ORDER BY c.name_ar,u.is_base DESC,u.name_ar").fetchall()
    c.close()
    return render_template("stock_uoms.html",categories=categories,rows=rows)


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
            if not name: raise ValueError("اسم المجموعة مطلوب.")
            if category_id:
                if not code: raise ValueError("كود المجموعة مطلوب عند التعديل.")
                _execute(c, "UPDATE stock_categories SET code=?,name_ar=?,description=? WHERE id=?",(code,name,description,category_id))
            else:
                code=next_code(c, "stock_category", "CAT", "stock_categories", "code")
                _execute(c, "INSERT INTO stock_categories(code,name_ar,description) VALUES(?,?,?)",(code,name,description))
            c.commit();flash("تم حفظ مجموعة المنتجات.","success")
        except (ValueError,*DB_INTEGRITY_ERROR) as e:
            c.rollback();flash(str(e) if isinstance(e,ValueError) else "كود المجموعة أو اسمها مستخدم بالفعل.","error")
        finally:c.close()
        return redirect(url_for("stock.categories"))
    rows=_execute(c, """SELECT sc.*,COUNT(p.id) product_count FROM stock_categories sc
        LEFT JOIN stock_products p ON p.category_id=sc.id GROUP BY sc.id ORDER BY sc.name_ar""").fetchall()
    c.close();return render_template("stock_categories.html",rows=rows)


@stock_bp.route("/categories/<int:category_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_category(category_id):
    c=get_db()
    try:
        used=_execute(c, "SELECT COUNT(*) n FROM stock_products WHERE category_id=?",(category_id,)).fetchone()["n"]
        if used: raise ValueError("لا يمكن حذف المجموعة لوجود أصناف مرتبطة بها؛ انقل الأصناف أولًا.")
        _execute(c, "DELETE FROM stock_categories WHERE id=?",(category_id,));c.commit();flash("تم حذف المجموعة.","success")
    except (ValueError,*DB_INTEGRITY_ERROR) as e:
        c.rollback();flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    finally:c.close()
    return redirect(url_for("stock.categories"))

@stock_bp.route("/warehouses", methods=["GET","POST"])
@stock_permission("stock.manage")
def warehouses():
    c=get_db()
    if request.method=="POST":
        try:
            name=request.form.get("name_ar","").strip()
            if not name: raise ValueError("اسم المخزن مطلوب.")
            code=next_code(c, "warehouse", "WH", "stock_warehouses", "code")
            branch_id=request.form.get("branch_id",type=int)
            branch=_execute(c, "SELECT name_ar FROM branches WHERE id=? AND active=1",(branch_id,)).fetchone() if branch_id else None
            if branch_id and not branch: raise ValueError("اختر فرعًا موجودًا ونشطًا.")
            _execute(c, "INSERT INTO stock_warehouses(code,name_ar,branch,branch_id) VALUES(?,?,?,?)",
                      (code,name,branch["name_ar"] if branch else "",branch_id))
            c.commit(); flash("تم حفظ المخزن وربطه بالفرع المحدد.", "success")
        except (ValueError,*DB_INTEGRITY_ERROR) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود المخزن مستخدم بالفعل.", "error")
        finally: c.close()
        return redirect(url_for("stock.warehouses"))
    rows=_execute(c, """SELECT w.*,b.code branch_code,b.name_ar branch_name
        FROM stock_warehouses w LEFT JOIN branches b ON b.id=w.branch_id ORDER BY w.code""").fetchall()
    branches=_execute(c, "SELECT id,code,name_ar FROM branches WHERE active=1 ORDER BY code").fetchall()
    c.close()
    return render_template("stock_warehouses.html",rows=rows,branches=branches)


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
            if not _execute(c, "SELECT 1 FROM stock_warehouses WHERE id=? AND active=1", (warehouse_id,)).fetchone():
                raise ValueError("اختر مخزنًا نشطًا.")
            if _execute(c, """SELECT 1 FROM stock_locations WHERE warehouse_id=? AND (parent_id = ? OR (parent_id IS NULL AND ? IS NULL)) AND lower(code)=lower(?)""",
                          (warehouse_id, parent_id, parent_id, code)).fetchone():
                raise ValueError("كود الموقع مستخدم بالفعل في نفس المستوى.")
            if expected_parent[location_type] is None:
                if parent_id:
                    raise ValueError("الدور يجب أن يكون مستوى رئيسيًا داخل المخزن.")
            else:
                parent = _execute(c, "SELECT location_type,warehouse_id,active FROM stock_locations WHERE id=?", (parent_id,)).fetchone() if parent_id else None
                if not parent or not parent["active"] or parent["warehouse_id"] != warehouse_id or parent["location_type"] != expected_parent[location_type]:
                    raise ValueError("الموقع الأب غير صحيح أو لا يتبع نفس المخزن.")
            _execute(c, "INSERT INTO stock_locations(warehouse_id,parent_id,code,name_ar,location_type) VALUES(?,?,?,?,?)",
                      (warehouse_id, parent_id, code, name, location_type))
            c.commit()
            flash("تم إنشاء موقع التخزين.", "success")
        except (ValueError, *DB_INTEGRITY_ERROR) as exc:
            c.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "كود الموقع مستخدم في نفس المستوى.", "error")
        finally:
            c.close()
        return redirect(url_for("stock.locations"))
    warehouses = _execute(c, "SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    parents = _execute(c, "SELECT id,warehouse_id,code,name_ar,location_type FROM stock_locations WHERE active=1 ORDER BY warehouse_id,location_type,code").fetchall()
    rows = _execute(c, """SELECT l.*,w.code warehouse_code,w.name_ar warehouse_name,p.code parent_code,p.name_ar parent_name
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
            if request.form.get("action") == "save_reorder":
                product_ok=_execute(c, "SELECT 1 FROM stock_products WHERE id=? AND active=1",(product_id,)).fetchone()
                warehouse_ok=_execute(c, "SELECT 1 FROM stock_warehouses WHERE id=? AND active=1",(warehouse_id,)).fetchone()
                minimum=q4(dec(request.form.get("min_qty","0"),"الحد الأدنى"))
                point=q4(dec(request.form.get("reorder_point","0"),"نقطة إعادة الطلب"))
                reorder=q4(dec(request.form.get("reorder_qty","0"),"كمية إعادة الطلب"))
                if not product_ok or not warehouse_ok: raise ValueError("اختر صنفًا ومخزنًا نشطين.")
                if min(minimum,point,reorder)<0: raise ValueError("حدود المخزون لا يمكن أن تكون سالبة.")
                _execute(c, """INSERT INTO stock_warehouse_product_settings
                    (product_id,warehouse_id,min_qty,reorder_point,reorder_qty)
                    VALUES(?,?,?,?,?) ON CONFLICT(product_id,warehouse_id) DO UPDATE SET
                    min_qty=excluded.min_qty,reorder_point=excluded.reorder_point,
                    reorder_qty=excluded.reorder_qty,updated_at=CURRENT_TIMESTAMP""",
                    (product_id,warehouse_id,str(minimum),str(point),str(reorder)))
                c.commit()
                flash("تم حفظ حدود إعادة الطلب للصنف في هذا المخزن فقط.","success")
                return redirect(url_for("stock.product_locations"))
            location_id = request.form.get("location_id", type=int)
            valid = _execute(c, """SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1""",
                              (location_id, warehouse_id)).fetchone()
            if not _execute(c, "SELECT 1 FROM stock_products WHERE id=? AND active=1", (product_id,)).fetchone() or not valid:
                raise ValueError("الصنف أو موقع التخزين غير صحيح.")
            # Serialize location reassignment with all stock movements that
            # can consume or create valuation layers in this warehouse.
            begin_write_transaction(c)
            lock_stock_warehouses(c, warehouse_id)
            _execute(c, """INSERT INTO stock_product_locations(product_id,warehouse_id,location_id,is_default)
                VALUES(?,?,?,1) ON CONFLICT(product_id,warehouse_id)
                DO UPDATE SET location_id=excluded.location_id,is_default=1""",
                (product_id, warehouse_id, location_id))
            # Existing stock without a recorded bin is assigned to the location
            # explicitly chosen by the user as this product's default location.
            _execute(c, f"""UPDATE stock_valuation_layers SET location_id=?
                WHERE product_id=? AND warehouse_id=? AND location_id IS NULL
                  AND {decimal_compare_expression(c, "qty_remaining")} > 0""", (location_id, product_id, warehouse_id))
            c.commit()
            flash("تم تحديد الموقع الافتراضي وإسناد الأرصدة القديمة غير المحددة إليه.", "success")
        except (ValueError, *DB_ERROR) as exc:
            c.rollback()
            flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
        finally:
            c.close()
        return redirect(url_for("stock.product_locations"))
    products = _execute(c, "SELECT p.id,p.sku,p.name_ar,COALESCE(sc.name_ar,p.category,'') category_name FROM stock_products p LEFT JOIN stock_categories sc ON sc.id=p.category_id WHERE p.active=1 ORDER BY sc.name_ar,p.sku").fetchall()
    warehouses = _execute(c, "SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    locations = _execute(c, """SELECT l.id,l.warehouse_id,l.code,l.name_ar,l.location_type,w.code warehouse_code
        FROM stock_locations l JOIN stock_warehouses w ON w.id=l.warehouse_id
        WHERE l.active=1 ORDER BY w.code,l.code""").fetchall()
    rows = _execute(c, """SELECT m.*,p.sku,p.name_ar product_name,COALESCE(sc.name_ar,p.category,'') category_name,w.code warehouse_code,l.code location_code,l.name_ar location_name
        FROM stock_product_locations m JOIN stock_products p ON p.id=m.product_id LEFT JOIN stock_categories sc ON sc.id=p.category_id
        JOIN stock_warehouses w ON w.id=m.warehouse_id JOIN stock_locations l ON l.id=m.location_id
        ORDER BY w.code,p.sku""").fetchall()
    reorder_settings=_execute(c, """SELECT s.*,p.sku,p.name_ar product_name,w.code warehouse_code,w.name_ar warehouse_name
        FROM stock_warehouse_product_settings s JOIN stock_products p ON p.id=s.product_id
        JOIN stock_warehouses w ON w.id=s.warehouse_id ORDER BY w.code,p.sku""").fetchall()
    c.close()
    return render_template("stock_product_locations.html", products=products, warehouses=warehouses,
                           locations=locations, rows=rows,reorder_settings=reorder_settings)


@stock_bp.route("/products/<int:product_id>/edit", methods=["GET","POST"])
@stock_permission("stock.manage")
def edit_product(product_id):
    c=get_db(); categories=_execute(c, "SELECT id,code,name_ar FROM stock_categories WHERE active=1 ORDER BY name_ar").fetchall(); product=_execute(c, "SELECT * FROM stock_products WHERE id=?",(product_id,)).fetchone()
    if not product:
        c.close(); flash("الصنف غير موجود.","error"); return redirect(url_for("stock.products"))
    used=_execute(c, "SELECT 1 FROM stock_movements WHERE product_id=? LIMIT 1",(product_id,)).fetchone() is not None
    for table in ("purchase_order_lines","goods_receipt_lines","sales_invoice_lines"):
        if _table_exists(c, table):
            used=used or _execute(c, "SELECT 1 FROM "+table+" WHERE product_id=? LIMIT 1",(product_id,)).fetchone() is not None
    uoms=_execute(c, "SELECT u.id,u.code,u.name_ar,u.category_id,c.name_ar category_name FROM stock_uoms u JOIN stock_uom_categories c ON c.id=u.category_id WHERE u.active=1 AND c.active=1 ORDER BY c.id,u.is_base DESC,u.name_ar").fetchall()
    if request.method=="POST":
        try:
            sku=request.form.get("sku","").strip(); name=request.form.get("name_ar","").strip()
            base_uom_id=request.form.get("base_uom_id",type=int)
            selected_uom=_execute(c, "SELECT id,name_ar FROM stock_uoms WHERE id=? AND active=1",(base_uom_id,)).fetchone() if base_uom_id else None
            uom=selected_uom["name_ar"] if selected_uom else (product["uom"] if not product["base_uom_id"] else "")
            if not sku or not name or not uom: raise ValueError("الكود والاسم ووحدة القياس مطلوبة.")
            if product["base_uom_id"] and not selected_uom: raise ValueError("اختر وحدة قياس نشطة.")
            package_units_exist=_execute(c, "SELECT 1 FROM stock_product_uoms WHERE product_id=? AND is_base=0 AND active=1 LIMIT 1",(product_id,)).fetchone()
            if package_units_exist and (base_uom_id or None)!=(product["base_uom_id"] or None):
                raise ValueError("لا يمكن تغيير وحدة الأساس بعد تعريف وحدات تعبئة خاصة بالصنف؛ راجع وحدات التعبئة أولًا.")
            if used and (request.form.get("product_type") != product["product_type"] or (base_uom_id or None) != (product["base_uom_id"] or None) or int(request.form.get("track_lot")=="on") != product["track_lot"] or int(request.form.get("track_expiry")=="on") != product["track_expiry"]):
                raise ValueError("الصنف له حركات سابقة؛ لا يمكن تغيير نوعه أو وحدة قياسه أو إعدادات تتبع التشغيلة/الصلاحية.")
            category_id=request.form.get("category_id",type=int)
            category=_execute(c, "SELECT name_ar FROM stock_categories WHERE id=? AND active=1",(category_id,)).fetchone() if category_id else None
            if category_id and not category: raise ValueError("مجموعة الصنف غير صحيحة.")
            purchase_price = money(dec(request.form.get("purchase_price", product["purchase_price"] or "0"), "سعر الشراء"))
            sale_price = money(dec(request.form.get("sale_price", product["sale_price"] or "0"), "سعر البيع"))
            if purchase_price < 0 or sale_price < 0:
                raise ValueError("الأسعار لا يمكن أن تكون سالبة.")
            _execute(c, """UPDATE stock_products SET sku=?,name_ar=?,category=?,category_id=?,product_type=?,uom=?,base_uom_id=?,barcode=?,track_lot=?,track_expiry=?,purchase_price=?,sale_price=? WHERE id=?""",
              (sku,name,category["name_ar"] if category else "",category_id,request.form.get("product_type","goods"),uom,base_uom_id,request.form.get("barcode","").strip() or None,int(request.form.get("track_lot")=="on"),int(request.form.get("track_expiry")=="on"),str(purchase_price),str(sale_price),product_id))
            if (base_uom_id or None)!=(product["base_uom_id"] or None):
                base_unit=_execute(c, "SELECT code,name_ar FROM stock_uoms WHERE id=? AND active=1",(base_uom_id,)).fetchone()
                if not base_unit: raise ValueError("وحدة الأساس الجديدة غير نشطة.")
                base_mapping=_execute(c, "SELECT id FROM stock_product_uoms WHERE product_id=? AND is_base=1",(product_id,)).fetchone()
                if base_mapping:
                    _execute(c, """UPDATE stock_product_uoms SET uom_id=?,custom_code=?,custom_name=?,factor_to_base='1'
                        WHERE id=?""",(base_uom_id,base_unit["code"],base_unit["name_ar"],base_mapping["id"]))
                else:
                    _execute(c, """INSERT INTO stock_product_uoms(product_id,uom_id,custom_code,custom_name,factor_to_base,is_base)
                        VALUES(?,?,?,?, '1',1)""",(product_id,base_uom_id,base_unit["code"],base_unit["name_ar"]))
            c.commit(); c.close(); flash("تم تعديل الصنف.","success"); return redirect(url_for("stock.products"))
        except (ValueError,*DB_INTEGRITY_ERROR) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "الكود أو الباركود مستخدم بالفعل.","error")
    c.close(); return render_template("stock_product_edit.html",product=product,used=used,categories=categories,uoms=uoms)


@stock_bp.route("/products/<int:product_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_product(product_id):
    c=get_db(); product=_execute(c, "SELECT * FROM stock_products WHERE id=?",(product_id,)).fetchone()
    if not product: flash("الصنف غير موجود.","error")
    else:
        refs=[("stock_movements","حركات المخزون"),("stock_lots","التشغيلات"),("stock_valuation_layers","طبقات التقييم"),("purchase_order_lines","أوامر الشراء"),("goods_receipt_lines","استلامات المشتريات"),("sales_invoice_lines","فواتير البيع")]
        used=next((label for table,label in refs if _execute(c, f"SELECT 1 FROM {table} WHERE product_id=? LIMIT 1",(product_id,)).fetchone()),None)
        if used: flash("لا يمكن حذف الصنف لوجود ارتباط في "+used+".","error")
        else:
            try: _execute(c, "DELETE FROM stock_products WHERE id=?",(product_id,)); c.commit(); flash("تم حذف الصنف لعدم وجود حركات مرتبطة.","success")
            except DB_INTEGRITY_ERROR: c.rollback(); flash("لا يمكن حذف الصنف لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("stock.products"))


@stock_bp.route("/warehouses/<int:warehouse_id>/edit",methods=["GET","POST"])
@stock_permission("stock.manage")
def edit_warehouse(warehouse_id):
    c=get_db(); warehouse=_execute(c, "SELECT * FROM stock_warehouses WHERE id=?",(warehouse_id,)).fetchone()
    if not warehouse:
        c.close(); flash("المخزن غير موجود.","error"); return redirect(url_for("stock.warehouses"))
    if request.method=="POST":
        try:
            code=request.form.get("code","").strip(); name=request.form.get("name_ar","").strip()
            if not code or not name: raise ValueError("كود واسم المخزن مطلوبان.")
            branch_id=request.form.get("branch_id",type=int)
            branch=_execute(c, "SELECT name_ar FROM branches WHERE id=? AND active=1",(branch_id,)).fetchone() if branch_id else None
            if branch_id and not branch: raise ValueError("اختر فرعًا موجودًا ونشطًا.")
            if warehouse["branch_id"] and (branch_id or None)!=(warehouse["branch_id"] or None) and _execute(c, 
                "SELECT 1 FROM stock_movements WHERE source_warehouse_id=? OR target_warehouse_id=? LIMIT 1",
                (warehouse_id,warehouse_id)
            ).fetchone():
                raise ValueError("لا يمكن تغيير فرع مخزن له حركات تاريخية؛ حتى لا تتغير دلالة تقارير الفروع. أنشئ مخزنًا جديدًا أو نفّذ إعادة تصنيف محاسبية موثقة.")
            _execute(c, "UPDATE stock_warehouses SET code=?,name_ar=?,branch=?,branch_id=? WHERE id=?",
                      (code,name,branch["name_ar"] if branch else "",branch_id,warehouse_id))
            c.commit(); c.close(); flash("تم تعديل المخزن وربطه بالفرع المحدد.","success"); return redirect(url_for("stock.warehouses"))
        except (ValueError,*DB_INTEGRITY_ERROR) as e: c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود المخزن مستخدم بالفعل.","error")
    branches=_execute(c, "SELECT id,code,name_ar FROM branches WHERE active=1 ORDER BY code").fetchall()
    c.close(); return render_template("stock_warehouse_edit.html",warehouse=warehouse,branches=branches)


@stock_bp.route("/warehouses/<int:warehouse_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_warehouse(warehouse_id):
    # Permanent deletion is intentionally disabled: warehouse history, balances,
    # counts and reorder settings must remain auditable. Use the active toggle.
    c=get_db()
    warehouse=_execute(c, "SELECT id,name_ar FROM stock_warehouses WHERE id=?",(warehouse_id,)).fetchone()
    c.close()
    if not warehouse:
        flash("المخزن غير موجود.","error")
    else:
        flash("الحذف النهائي للمخازن غير متاح حفاظًا على الأرصدة والحركات والتقارير التاريخية. استخدم التعطيل بدل الحذف.","error")
    return redirect(url_for("stock.warehouses"))


@stock_bp.route("/warehouses/<int:warehouse_id>/toggle-active",methods=["POST"])
@stock_permission("stock.manage")
def toggle_warehouse_active(warehouse_id):
    c=get_db()
    warehouse=_execute(c, "SELECT id,name_ar,active FROM stock_warehouses WHERE id=?",(warehouse_id,)).fetchone()
    if not warehouse:
        flash("المخزن غير موجود.","error")
    else:
        new_active=0 if warehouse["active"] else 1
        _execute(c, "UPDATE stock_warehouses SET active=? WHERE id=?",(new_active,warehouse_id))
        c.commit()
        flash(("تم تفعيل" if new_active else "تم تعطيل")+" المخزن "+warehouse["name_ar"]+" دون حذف حركاته أو أرصدته.","success")
    c.close()
    return redirect(url_for("stock.warehouses"))


@stock_bp.route("/movements", methods=["GET","POST"])
@stock_permission("stock.manage")
def movements():
    c=get_db()
    if request.method=="POST":
        try:
            kind=request.form.get("movement_type")
            if kind not in {"receipt","issue","transfer","relocate","scrap"}: raise ValueError("نوع الحركة غير صحيح.")
            product=_execute(c, "SELECT * FROM stock_products WHERE id=? AND active=1",(request.form.get("product_id"),)).fetchone()
            if not product: raise ValueError("الصنف غير موجود أو غير نشط.")
            if product["product_type"] == "service": raise ValueError("الخدمات لا تُدار كأصناف مخزنية.")
            entered_qty=q4(dec(request.form.get("quantity"),"الكمية"))
            if entered_qty<=0: raise ValueError("الكمية يجب أن تكون أكبر من صفر.")
            entered_uom_key=request.form.get("uom_id") or None
            if product["base_uom_id"]:
                factor,_=product_uom_factor(c,product["id"],entered_uom_key)
                qty=q4(entered_qty*factor)
                entered_uom_id,entered_uom_name=product_uom_storage(c,product["id"],entered_uom_key)
            else:
                if entered_uom_key: raise ValueError("الصنف القديم غير مربوط بدليل وحدات القياس؛ استخدم وحدته الحالية.")
                qty=entered_qty
                entered_uom_id,entered_uom_name=None,product["uom"]
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
                if not _execute(c, "SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(source_location_id,src)).fetchone():
                    raise ValueError("موقع المصدر لا يتبع المخزن المحدد.")
                if not _execute(c, "SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(target_location_id,src)).fetchone():
                    raise ValueError("موقع الوجهة لا يتبع نفس المخزن.")
                dst=src
            if kind=="transfer" and dst:
                if not target_location_id:
                    default_location=_execute(c, "SELECT location_id FROM stock_product_locations WHERE product_id=? AND warehouse_id=?",(product["id"],dst)).fetchone()
                    target_location_id=default_location["location_id"] if default_location else None
                if target_location_id and not _execute(c, "SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(target_location_id,dst)).fetchone():
                    raise ValueError("موقع الوجهة لا يتبع مخزن الوجهة.")
            begin_write_transaction(c)
            lock_stock_warehouses(c, src, dst)
            source_branch_id = target_branch_id = None
            if kind == "transfer":
                source_branch = _execute(c, "SELECT branch_id FROM stock_warehouses WHERE id=?", (src,)).fetchone()
                target_branch = _execute(c, "SELECT branch_id FROM stock_warehouses WHERE id=?", (dst,)).fetchone()
                if not source_branch or not target_branch:
                    raise ValueError("مخزن المصدر أو الوجهة غير موجود.")
                source_branch_id = source_branch["branch_id"]
                target_branch_id = target_branch["branch_id"]
                if bool(source_branch_id) != bool(target_branch_id):
                    raise ValueError("اربط المخزنين بفرعين محددين قبل التحويل؛ لا يمكن اعتبار تحويل فرع إلى مخزن بلا فرع تحويلًا داخليًا.")
            movement_date=request.form.get("movement_date") or date.today().isoformat()
            ensure_stock_date_open(c, movement_date)
            lot_id=None
            lot_no=request.form.get("lot_number","").strip()
            if kind=="receipt":
                raise ValueError("استلام المخزون يجب أن يتم من شاشة استلام المشتريات حتى يُسجل قيد GRNI ويرتبط بالمورد. لا تستخدم حركة استلام عامة.")
            else:
                if product["track_lot"] or product["track_expiry"]:
                    if not lot_no: raise ValueError("أدخل رقم التشغيلة للصرف من صنف يتطلب تتبعًا.")
                    lot=_execute(c, "SELECT * FROM stock_lots WHERE product_id=? AND lot_number=?",(product["id"],lot_no)).fetchone()
                    if not lot: raise ValueError("التشغيلة غير مسجلة.")
                    ensure_lot_not_expired(lot, movement_date)
                    lot_id=lot["id"]
                settings_table = _table_exists(c, "company_settings")
                method_row = _execute(c, "SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone() if settings_table else None
                method = method_row["setting_value"] if method_row else "average"
                if kind in {"issue","scrap","relocate","transfer"} and source_location_id and not _execute(c, "SELECT 1 FROM stock_locations WHERE id=? AND warehouse_id=? AND active=1",(source_location_id,src)).fetchone():
                    raise ValueError("موقع المصدر لا يتبع المخزن المحدد.")
                total,allocations=consume_layers(c,product["id"],src,qty,method,lot_id,source_location_id)
                unit_cost=(total/qty).quantize(MONEY,rounding=ROUND_HALF_UP)
            movement_no=f"STK-{date.today().strftime('%Y%m%d')}-{uuid.uuid4().hex[:12].upper()}"
            issue_purpose=request.form.get("issue_purpose") if kind=="issue" else None
            if kind=="issue" and issue_purpose not in {"sale","internal"}:
                raise ValueError("حدد غرض الصرف: تكلفة مبيعات أو استخدام داخلي.")
            mid=insert_and_get_id(c, """INSERT INTO stock_movements(movement_no,movement_type,product_id,source_warehouse_id,target_warehouse_id,
              source_location_id,target_location_id,lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,issue_purpose,entered_quantity,entered_uom_id,entered_uom_key,entered_uom_name)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(movement_no,kind,product["id"],src,dst,source_location_id,target_location_id,lot_id,str(qty),str(unit_cost),str(total),movement_date,request.form.get("notes","").strip(),session.get("user_id"),issue_purpose,str(entered_qty),entered_uom_id,entered_uom_key,entered_uom_name))
            if kind=="receipt":
                _execute(c, """INSERT INTO stock_valuation_layers(product_id,warehouse_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost)
                  VALUES(?,?,?,?,?,?,?)""",(product["id"],dst,lot_id,mid,str(qty),str(qty),str(unit_cost)))
            elif kind in {"transfer","relocate"}:
                for allocated_lot,allocated_qty,allocated_cost,allocated_total in allocations:
                    _execute(c, """INSERT INTO stock_valuation_layers(product_id,warehouse_id,location_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost,value_remaining)
                      VALUES(?,?,?,?,?,?,?,?,?)""",(product["id"],dst,target_location_id,allocated_lot,mid,str(allocated_qty),str(allocated_qty),str(allocated_cost),str(allocated_total)))
            if kind=="scrap":
                post_scrap_journal(c,mid,movement_no,product,movement_date,total,session.get("user_id"))
            elif kind in {"transfer","relocate"}:
                if kind == "transfer":
                    if source_branch_id and target_branch_id and source_branch_id != target_branch_id:
                        post_interbranch_transfer_journal(
                            c, mid, movement_no, product, movement_date, total,
                            session.get("user_id"), source_branch_id, target_branch_id
                        )
                    else:
                        _execute(c, "UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (mid,))
                        set_stock_accounting_note(c,mid,None)
                else:
                    # Bin relocation inside the same warehouse never changes GL.
                    _execute(c, "UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (mid,))
            elif kind=="receipt":
                # A receipt needs a supplier invoice or an explicit GRNI document before posting.
                _execute(c, "UPDATE stock_movements SET accounting_status='pending_document' WHERE id=?", (mid,))
                set_stock_accounting_note(c,mid,"استلام المخزون محفوظ؛ يجب استكمال فاتورة المورد أو مستند GRNI من شاشة المشتريات.")
            elif kind=="issue":
                post_issue_journal(c,mid,movement_no,product,movement_date,total,session.get("user_id"),issue_purpose)
            c.commit()
            g.audit_created_record = ("stock_movements", mid, None, None)
            saved_status=_execute(c, "SELECT accounting_status FROM stock_movements WHERE id=?",(mid,)).fetchone()["accounting_status"]
            if saved_status=="posted":
                flash(f"تم حفظ الحركة {movement_no} وترحيل قيدها المحاسبي.","success")
            elif saved_status=="pending_purpose":
                flash(f"تم حفظ الحركة {movement_no}، لكنها معلقة حتى تحدد غرض الصرف من شاشة الحركات المعلقة.","warning")
            elif saved_status=="pending_setup":
                flash(f"تم حفظ الحركة {movement_no} دون ترحيل؛ راجع حسابات المخزون والمصروف في ملف النشاط.","warning")
            elif saved_status=="pending_document":
                flash(f"تم حفظ الحركة {movement_no} وربطها بالمخزون؛ استكمل مستند الشراء/المرتجع الأصلي لإتمام القيد.","warning")
            else:
                flash(f"تم حفظ الحركة {movement_no}.","success")
        except (ValueError,InvalidOperation,*DB_INTEGRITY_ERROR) as e:
            c.rollback(); flash(safe_error_message(e, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
        finally: c.close()
        return redirect(url_for("stock.movements"))
    products=_execute(c, "SELECT p.id,p.sku,p.name_ar,p.uom,p.base_uom_id,p.track_lot,p.track_expiry,u.category_id base_uom_category FROM stock_products p LEFT JOIN stock_uoms u ON u.id=p.base_uom_id WHERE p.active=1 ORDER BY p.name_ar").fetchall()
    product_uoms={p["id"]:product_uom_options(c,p["id"]) for p in products}
    warehouses=_execute(c, "SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    locations=_execute(c, "SELECT id,warehouse_id,code,name_ar,location_type FROM stock_locations WHERE active=1 ORDER BY warehouse_id,code").fetchall()
    movement_page = max(1, request.args.get("movement_page", 1, type=int) or 1)
    movement_per_page = request.args.get("movement_per_page", 50, type=int) or 50
    if movement_per_page not in (50, 100):
        movement_per_page = 50
    movement_total = _execute(c, "SELECT COUNT(*) AS movement_total FROM stock_movements").fetchone()["movement_total"]
    movement_pages = max(1, (movement_total + movement_per_page - 1) // movement_per_page)
    movement_page = min(movement_page, movement_pages)
    rows=_execute(c, """SELECT m.*,p.sku,p.name_ar,s.name_ar source_name,t.name_ar target_name
      FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
      LEFT JOIN stock_warehouses s ON s.id=m.source_warehouse_id LEFT JOIN stock_warehouses t ON t.id=m.target_warehouse_id
      ORDER BY m.id DESC LIMIT ? OFFSET ?""",
      (movement_per_page, (movement_page - 1) * movement_per_page)).fetchall()
    c.close()
    return render_template("stock_movements.html",products=products,product_uoms=product_uoms,warehouses=warehouses,locations=locations,rows=rows,
        movement_page=movement_page,movement_pages=movement_pages,movement_per_page=movement_per_page,
        movement_total=movement_total,today=date.today().isoformat())


def build_demand_replenishment_forecast(conn, warehouse_totals, product_lookup, today,
                                          forecast_days=90, target_cover_days=21):
    """Estimate net daily demand per product/warehouse from posted sales less returns."""
    required_tables = (
        "sales_invoices", "sales_invoice_lines",
        "sales_credit_notes", "sales_credit_note_lines",
    )
    existing_tables = {name for name in required_tables if _table_exists(conn, name)}
    if not set(required_tables).issubset(existing_tables):
        return [], False
    invoice_columns = _columns(conn, "sales_invoice_lines")
    credit_columns = _columns(conn, "sales_credit_note_lines")
    if not {"product_id", "quantity", "invoice_id"}.issubset(invoice_columns):
        return [], False
    if not {"product_id", "quantity", "credit_note_id"}.issubset(credit_columns):
        return [], False
    if forecast_days <= 0 or target_cover_days <= 0:
        raise ValueError("فترة توقع الطلب وهدف التغطية يجب أن يكونا أكبر من صفر.")

    start_date = (today - date.resolution * (forecast_days - 1)).isoformat()
    end_date = today.isoformat()
    net_demand = {}
    sales = _execute(conn, 
        """SELECT i.warehouse_id,l.product_id,l.quantity
           FROM sales_invoices i
           JOIN sales_invoice_lines l ON l.invoice_id=i.id
           WHERE i.status='posted' AND i.invoice_date>=? AND i.invoice_date<=?
             AND l.product_id IS NOT NULL""",
        (start_date, end_date),
    ).fetchall()
    for sale in sales:
        key = (sale["product_id"], sale["warehouse_id"])
        net_demand[key] = net_demand.get(key, Decimal("0")) + Decimal(str(sale["quantity"] or "0"))

    returns = _execute(conn, 
        """SELECT i.warehouse_id,l.product_id,l.quantity
           FROM sales_credit_notes cn
           JOIN sales_invoices i ON i.id=cn.invoice_id
           JOIN sales_credit_note_lines l ON l.credit_note_id=cn.id
           WHERE cn.status='posted' AND cn.credit_date>=? AND cn.credit_date<=?
             AND l.product_id IS NOT NULL""",
        (start_date, end_date),
    ).fetchall()
    for returned in returns:
        key = (returned["product_id"], returned["warehouse_id"])
        net_demand[key] = net_demand.get(key, Decimal("0")) - Decimal(str(returned["quantity"] or "0"))

    warehouse_names = {
        row["id"]: row for row in _execute(conn, 
            "SELECT id,code,name_ar FROM stock_warehouses WHERE active=1"
        ).fetchall()
    }
    result = []
    for (product_id, warehouse_id), net_qty in net_demand.items():
        daily_demand = max(Decimal("0"), net_qty) / Decimal(forecast_days)
        if daily_demand <= 0:
            continue
        current_qty = warehouse_totals.get(
            (product_id, warehouse_id), {"qty": Decimal("0")}
        )["qty"]
        target_qty = daily_demand * Decimal(target_cover_days)
        suggested_qty = max(Decimal("0"), target_qty - current_qty)
        product = product_lookup.get(product_id)
        warehouse = warehouse_names.get(warehouse_id)
        if not product or not warehouse:
            continue
        coverage_days = current_qty / daily_demand
        result.append({
            "sku": product["sku"], "name_ar": product["name_ar"], "uom": product["uom"],
            "warehouse_code": warehouse["code"], "warehouse_name": warehouse["name_ar"],
            "current_qty": current_qty, "net_sold_qty": net_qty,
            "daily_demand": daily_demand, "coverage_days": coverage_days,
            "target_qty": target_qty, "suggested_qty": suggested_qty,
            "priority": "عاجل" if coverage_days < Decimal("7") else (
                "قريب" if coverage_days < Decimal("14") else "مراقبة"
            ),
        })
    result.sort(key=lambda row: (
        row["coverage_days"], row["sku"], row["warehouse_code"]
    ))
    return result, True


@stock_bp.route("/reports")
@stock_permission("stock.view")
def reports():
    c=get_db()
    products=_execute(c, "SELECT id,sku,name_ar,uom,min_qty,reorder_point,reorder_qty FROM stock_products WHERE active=1 ORDER BY sku").fetchall()
    positive_qty_sql = decimal_compare_expression(c, "qty_remaining") + " > 0"
    layers=_execute(c, f"SELECT * FROM stock_valuation_layers WHERE {positive_qty_sql} ORDER BY id").fetchall()
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
    for lot in _execute(c, """SELECT l.id,l.lot_number,l.expiry_date,p.sku,p.name_ar
        FROM stock_lots l JOIN stock_products p ON p.id=l.product_id
        WHERE l.expiry_date IS NOT NULL ORDER BY l.expiry_date""").fetchall():
        lot_qty=sum((Decimal(str(x["qty_remaining"] or "0")) for x in layers if x["lot_id"]==lot["id"]),Decimal("0"))
        lots.append({"sku":lot["sku"],"name_ar":lot["name_ar"],"lot_number":lot["lot_number"],
                     "expiry_date":lot["expiry_date"],"qty":lot_qty})
    warehouse_lookup={row["id"]:row for row in _execute(c, "SELECT id,code,name_ar FROM stock_warehouses").fetchall()}
    location_lookup={row["id"]:row for row in _execute(c, "SELECT id,code,name_ar,location_type FROM stock_locations").fetchall()}
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
    movements=_execute(c, """SELECT m.*,p.sku,p.name_ar,w.name_ar warehouse
      FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
      LEFT JOIN stock_warehouses w ON w.id=COALESCE(m.target_warehouse_id,m.source_warehouse_id)
      ORDER BY m.id DESC LIMIT 100""").fetchall()
    # Reorder alerts are evaluated independently for each active warehouse.
    reorder_alerts=calculate_warehouse_reorder_alerts(c,warehouse_totals)
    # 90-day turnover is cost of goods issued for sale divided by average inventory
    # value. Average stock is reconstructed from today's valuation and net stock movements.
    today=date.today()
    period_start=today-date.resolution*89
    movement_rows=_execute(c, """SELECT product_id,movement_type,issue_purpose,quantity,total_cost,movement_date
        FROM stock_movements WHERE movement_date>=? AND movement_date<=?""",
        (period_start.isoformat(),today.isoformat())).fetchall()
    turnover={}
    for product in products:
        pid=product["id"]
        current_value=product_totals.get(pid,{"value":Decimal("0")})["value"]
        inbound=Decimal("0"); outbound=Decimal("0"); sold_cost=Decimal("0"); sold_qty=Decimal("0")
        last_sale=None
        for movement in movement_rows:
            if movement["product_id"]!=pid: continue
            value=Decimal(str(movement["total_cost"] or "0"))
            qty=Decimal(str(movement["quantity"] or "0"))
            if movement["movement_type"]=="receipt": inbound+=value
            elif movement["movement_type"] in {"issue","scrap"}:
                outbound+=value
                if movement["movement_type"]=="issue" and movement["issue_purpose"]=="sale":
                    sold_cost+=value; sold_qty+=qty
                    if last_sale is None or movement["movement_date"]>last_sale: last_sale=movement["movement_date"]
        opening_value=max(Decimal("0"),current_value-inbound+outbound)
        average_value=(opening_value+current_value)/Decimal("2")
        rate=(sold_cost/average_value) if average_value>0 else Decimal("0")
        days=(Decimal("90")/rate) if rate>0 else None
        turnover[pid]={"sold_qty":sold_qty,"sold_cost":sold_cost,"average_value":average_value,
            "rate":rate,"days":days,"last_sale":last_sale}
    slow_rows=[]
    for product in products:
        stats=turnover[product["id"]]
        if stats["sold_qty"]==0 or (stats["days"] is not None and stats["days"]>90):
            slow_rows.append({"sku":product["sku"],"name_ar":product["name_ar"],"uom":product["uom"],
                "qty":product_totals.get(product["id"],{"qty":Decimal("0")})["qty"],
                "value":product_totals.get(product["id"],{"value":Decimal("0")})["value"],**stats})
    # Demand-based replenishment forecast: net posted sales after returns.
    forecast_rows, forecast_available = build_demand_replenishment_forecast(
        c, warehouse_totals, product_lookup, today,
        forecast_days=90, target_cover_days=21,
    )
    forecast_days=90
    target_cover_days=21
    # Expiry alerts are lot- and valuation-layer-specific so warehouse, bin and quantity
    # remain visible. The default alert window is 30 days, including already expired lots.
    expiry_alerts=[]
    expiry_window=30
    for lot in _execute(c, """SELECT l.id,l.lot_number,l.expiry_date,p.sku,p.name_ar,p.uom
        FROM stock_lots l JOIN stock_products p ON p.id=l.product_id
        WHERE l.expiry_date IS NOT NULL AND p.track_expiry=1 ORDER BY l.expiry_date""").fetchall():
        days_left=(date.fromisoformat(lot["expiry_date"])-today).days
        if days_left>expiry_window: continue
        for layer in _execute(c, f"""SELECT vl.qty_remaining,vl.warehouse_id,vl.location_id,w.code warehouse_code,w.name_ar warehouse_name,
            sl.code location_code,sl.name_ar location_name
            FROM stock_valuation_layers vl LEFT JOIN stock_warehouses w ON w.id=vl.warehouse_id
            LEFT JOIN stock_locations sl ON sl.id=vl.location_id
            WHERE vl.lot_id=? AND {decimal_compare_expression(c, "vl.qty_remaining")} > 0""",(lot["id"],)).fetchall():
            qty=Decimal(str(layer["qty_remaining"] or "0"))
            if qty<=0: continue
            expiry_alerts.append({"sku":lot["sku"],"name_ar":lot["name_ar"],"uom":lot["uom"],
                "lot_number":lot["lot_number"],"expiry_date":lot["expiry_date"],"days_left":days_left,"qty":qty,
                "warehouse":layer["warehouse_code"]+" - "+layer["warehouse_name"],
                "location":(layer["location_code"]+" - "+layer["location_name"]) if layer["location_code"] else "غير محدد"})
    c.close()
    return render_template("stock_reports.html",rows=rows,lots=lots,movements=movements,
        warehouse_rows=warehouse_rows,location_rows=location_rows,reorder_alerts=reorder_alerts,
        slow_rows=slow_rows,expiry_alerts=expiry_alerts,turnover_days=90,
        forecast_rows=forecast_rows,forecast_available=forecast_available,
        forecast_days=forecast_days,target_cover_days=target_cover_days)



def _stock_valuation_layer_integrity(conn, limit=200):
    """Find valuation-layer defects that a value-to-GL total alone cannot expose."""
    if not _table_exists(conn, "stock_valuation_layers"):
        return 0, []
    columns = _columns(conn, "stock_valuation_layers")
    if not {"id", "qty_remaining", "unit_cost"}.issubset(columns):
        return 0, []
    joins = ""
    if (_table_exists(conn, "stock_movements") and
            {"id", "product_id", "target_warehouse_id"}.issubset(_columns(conn, "stock_movements")) and
            "source_movement_id" in columns):
        joins = " LEFT JOIN stock_movements sm ON sm.id=l.source_movement_id "
    select = ["l.*"]
    if joins:
        select.extend(["sm.id AS integrity_source_found",
                       "sm.product_id AS integrity_source_product_id",
                       "sm.target_warehouse_id AS integrity_source_warehouse_id"])
    rows = _execute(conn, "SELECT " + ",".join(select) +
                    " FROM stock_valuation_layers l" + joins + " ORDER BY l.id").fetchall()
    issues, issue_count = [], 0

    def add(row, code, message):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < limit:
            issues.append({
                "layer_id": int(row["id"]) if row["id"] is not None else None,
                "product_id": row["product_id"] if "product_id" in columns else None,
                "warehouse_id": row["warehouse_id"] if "warehouse_id" in columns else None,
                "source_movement_id": row["source_movement_id"] if "source_movement_id" in columns else None,
                "code": code, "message": message,
            })

    for row in rows:
        try:
            qty = D(str(row["qty_remaining"] or "0"))
            unit_cost = D(str(row["unit_cost"] or "0"))
            value = (D(str(row["value_remaining"])) if "value_remaining" in columns
                     and row["value_remaining"] not in (None, "") else qty * unit_cost)
            qty_in = (D(str(row["qty_in"])) if "qty_in" in columns
                      and row["qty_in"] not in (None, "") else None)
            if not all(number.is_finite() for number in (qty, unit_cost, value)):
                raise InvalidOperation
            if qty_in is not None and not qty_in.is_finite():
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            add(row, "invalid_numeric_value", "تحتوي الطبقة على كمية أو تكلفة أو قيمة غير رقمية أو غير منتهية.")
            continue
        if qty < 0:
            add(row, "negative_quantity", "كمية متبقية سالبة في طبقة التقييم.")
        if value < 0:
            add(row, "negative_value", "قيمة متبقية سالبة في طبقة التقييم.")
        if qty_in is not None:
            if qty_in <= 0 and (qty != 0 or value != 0):
                add(row, "invalid_original_quantity", "الطبقة تحمل رصيدًا رغم أن كمية الإدخال الأصلية غير موجبة.")
            elif qty > qty_in:
                add(row, "remaining_exceeds_received", "الكمية المتبقية أكبر من كمية الإدخال الأصلية.")
        if qty == 0 and abs(value) >= D("0.01"):
            add(row, "value_without_quantity", "توجد قيمة دفترية متبقية دون كمية مقابلة.")
        if joins and "source_movement_id" in columns:
            if row["source_movement_id"] is None:
                add(row, "missing_source_reference", "طبقة التقييم غير مرتبطة بحركة مصدر.")
            elif row["integrity_source_found"] is None:
                add(row, "missing_source_movement", "طبقة التقييم مرتبطة بحركة مخزون غير موجودة.")
            else:
                if ("product_id" in columns and
                        row["product_id"] != row["integrity_source_product_id"]):
                    add(row, "source_product_mismatch", "الصنف في طبقة التقييم لا يطابق صنف الحركة المصدر.")
                if ("warehouse_id" in columns and
                        row["warehouse_id"] != row["integrity_source_warehouse_id"]):
                    add(row, "source_warehouse_mismatch", "مخزن طبقة التقييم لا يطابق مخزن وجهة الحركة المصدر.")

    # Also audit the opposite direction: every inbound movement must have a
    # matching valuation layer, and the total original layer quantity must equal
    # the movement quantity. This catches orphan receipts/transfers that a
    # layer-to-movement join cannot discover.
    if (_table_exists(conn, "stock_movements") and
            {"id", "product_id", "target_warehouse_id", "movement_type", "quantity"}.issubset(_columns(conn, "stock_movements")) and
            {"source_movement_id", "qty_in"}.issubset(columns)):
        movement_rows = _execute(conn, """
            SELECT m.id,m.product_id,m.target_warehouse_id,m.quantity,m.movement_type,
                   l.id AS layer_id,l.qty_in
            FROM stock_movements m
            LEFT JOIN stock_valuation_layers l ON l.source_movement_id=m.id
            WHERE m.movement_type IN ('receipt','transfer','relocate')
            ORDER BY m.id,l.id
        """).fetchall()
        coverage = {}
        for movement in movement_rows:
            bucket = coverage.setdefault(int(movement["id"]), {
                "movement": movement, "layer_quantities": []
            })
            if movement["layer_id"] is not None:
                bucket["layer_quantities"].append(movement["qty_in"])
        for movement_id, bucket in coverage.items():
            movement = bucket["movement"]
            linked = bucket["layer_quantities"]
            issue_row = {"id": None, "product_id": movement["product_id"],
                         "warehouse_id": movement["target_warehouse_id"],
                         "source_movement_id": movement_id}
            if not linked:
                add(issue_row, "inbound_movement_without_layer",
                    "حركة استلام أو تحويل واردة لا تحتوي على أي طبقة تقييم مرتبطة.")
                continue
            try:
                movement_qty = D(str(movement["quantity"] or "0"))
                layer_qty = sum((D(str(quantity or "0")) for quantity in linked), D("0"))
                if not movement_qty.is_finite() or not layer_qty.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, TypeError, ValueError):
                add(issue_row, "invalid_inbound_quantity",
                    "كمية الحركة الواردة أو كميات طبقاتها غير رقمية.")
                continue
            if abs(layer_qty - movement_qty) > QTY:
                add(issue_row, "inbound_layer_quantity_mismatch",
                    "إجمالي كمية طبقات التقييم لا يساوي كمية حركة الاستلام أو التحويل.")
    return issue_count, issues


@stock_bp.route("/reconciliation")
@stock_permission("stock.view")
def valuation_reconciliation():
    """Compare current remaining valuation layers with posted inventory GL balances."""
    conn = get_db()
    try:
        from activity_profiles import active_posting_accounts, resolve_product_account_code
        posting = active_posting_accounts(conn)
        default_inventory = posting.get("inventory")
        if not default_inventory:
            raise ValueError("لا يوجد حساب مخزون مضبوط في ملف النشاط الحالي.")
        layer_integrity_issue_count, layer_integrity_issues = _stock_valuation_layer_integrity(conn)
        products = _execute(conn, "SELECT id,sku,name_ar FROM stock_products ORDER BY sku").fetchall()
        valuation_by_code = {}
        valuation_by_warehouse = {}
        warehouse_product_totals = {}
        for product in products:
            code = resolve_product_account_code(conn, product["id"], "inventory", default_inventory)
            if not code:
                raise ValueError("تعذر تحديد حساب المخزون للصنف " + product["sku"] + ".")
            # Read all product layers and parse with Decimal in Python. The
            # integrity audit may have found malformed legacy text values; a
            # database-side numeric comparison must not turn that finding into
            # a report crash.
            layers = _execute(conn, """SELECT warehouse_id,qty_remaining,unit_cost,value_remaining
                FROM stock_valuation_layers WHERE product_id=?""",
                (product["id"],)).fetchall()
            value = D("0")
            for layer in layers:
                try:
                    layer_qty = D(str(layer["qty_remaining"] or "0"))
                    layer_unit_cost = D(str(layer["unit_cost"] or "0"))
                    layer_value = (D(str(layer["value_remaining"])) if layer["value_remaining"] not in (None, "")
                                   else layer_qty * layer_unit_cost)
                    if not all(number.is_finite() for number in (layer_qty, layer_unit_cost, layer_value)):
                        raise InvalidOperation
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if layer_qty == 0 and layer_value == 0:
                    continue
                value += layer_value
                warehouse_id = layer["warehouse_id"]
                valuation_by_warehouse[warehouse_id] = valuation_by_warehouse.get(warehouse_id, D("0")) + layer_value
                key=(warehouse_id,product["id"],code)
                product_bucket=warehouse_product_totals.setdefault(key,{
                    "sku":product["sku"],"name":product["name_ar"],"account_code":code,
                    "qty":D("0"),"value":D("0")
                })
                product_bucket["qty"]+=D(str(layer["qty_remaining"] or "0"))
                product_bucket["value"]+=layer_value
            valuation_by_code[code] = valuation_by_code.get(code, D("0")) + value
        warehouse_rows = []
        warehouse_records = _execute(conn, "SELECT id,code,name_ar,active FROM stock_warehouses ORDER BY code").fetchall()
        for warehouse in warehouse_records:
            warehouse_rows.append({
                "code": warehouse["code"], "name": warehouse["name_ar"],
                "active": warehouse["active"],
                "valuation": valuation_by_warehouse.get(warehouse["id"], D("0"))
            })
        unassigned_value = valuation_by_warehouse.get(None, D("0"))
        if unassigned_value:
            warehouse_rows.append({"code":"—","name":"طبقات غير مرتبطة بمخزن","active":0,"valuation":unassigned_value})
        warehouse_product_rows=[]
        warehouse_names={row["id"]:row for row in warehouse_records}
        for (warehouse_id,_product_id,account_code),bucket in sorted(
            warehouse_product_totals.items(),key=lambda item:(
                warehouse_names[item[0][0]]["code"] if item[0][0] in warehouse_names else "ZZZ",
                item[1]["sku"]
            )
        ):
            warehouse=warehouse_names.get(warehouse_id)
            warehouse_product_rows.append({
                "warehouse_code":warehouse["code"] if warehouse else "—",
                "warehouse_name":warehouse["name_ar"] if warehouse else "غير مرتبط بمخزن",
                "sku":bucket["sku"],"name":bucket["name"],"account_code":account_code,
                "qty":bucket["qty"],"value":bucket["value"]
            })
        account_rows = _execute(conn, """SELECT id,code,name_ar FROM accounts
            WHERE active=1 AND is_group=0 AND code IN (
                SELECT DISTINCT code FROM accounts WHERE code IS NOT NULL
            ) ORDER BY code""").fetchall()
        profile = __import__("activity_profiles").get_active_activity_profile(conn)
        configured_inventory = profile.get("account_roles", {}).get("inventory", [])
        if not isinstance(configured_inventory, (list, tuple)):
            configured_inventory = [configured_inventory]
        inventory_codes = set(valuation_by_code)
        inventory_codes.update(str(code) for code in configured_inventory if code not in (None, ""))
        inventory_codes.add(default_inventory)
        accounts = {row["code"]: row for row in account_rows if row["code"] in inventory_codes}
        missing = sorted(inventory_codes - set(accounts))
        if missing:
            raise ValueError("حسابات المخزون التالية غير موجودة أو غير نشطة: " + "، ".join(missing))
        gl_by_account = {code: D("0") for code in inventory_codes}
        journal_tables = {name for name in ("journal_entries", "journal_lines") if _table_exists(conn, name)}
        if journal_tables != {"journal_entries", "journal_lines"}:
            raise ValueError("دفتر الأستاذ غير مهيأ؛ لا يمكن إجراء المطابقة.")
        journal_columns = _columns(conn, "journal_lines")
        has_stock_dimensions = {"stock_product_id", "stock_warehouse_id"}.issubset(journal_columns)
        from management_dashboard import _copied_opening_entry_ids
        copied_openings=sorted(_copied_opening_entry_ids(conn,date.today().isoformat()))
        opening_exclusion=(" AND je.id NOT IN ("+",".join("?" for _ in copied_openings)+")") if copied_openings else ""
        gl_rows = _execute(conn, f"""SELECT a.code,jl.debit,jl.credit
            FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
            JOIN accounts a ON a.id=jl.account_id
            WHERE je.status='posted' AND a.code IN (%s){opening_exclusion}""" %
            ",".join("?" for _ in inventory_codes),
            (*tuple(sorted(inventory_codes)),*copied_openings)).fetchall()
        for row in gl_rows:
            gl_by_account[row["code"]] += D(str(row["debit"] or "0")) - D(str(row["credit"] or "0"))

        tagged_gl = {}
        tagged_by_code = {code: D("0") for code in inventory_codes}
        if has_stock_dimensions:
            tagged_rows = _execute(conn, f"""SELECT a.code,jl.stock_product_id,jl.stock_warehouse_id,
                    jl.debit,jl.credit
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                JOIN accounts a ON a.id=jl.account_id
                WHERE je.status='posted' AND a.code IN (%s){opening_exclusion}
                  AND jl.stock_product_id IS NOT NULL AND jl.stock_warehouse_id IS NOT NULL""" %
                ",".join("?" for _ in inventory_codes),
                (*tuple(sorted(inventory_codes)),*copied_openings)).fetchall()
            for line in tagged_rows:
                balance = D(str(line["debit"] or "0")) - D(str(line["credit"] or "0"))
                key = (line["code"], int(line["stock_product_id"]), int(line["stock_warehouse_id"]))
                tagged_gl[key] = tagged_gl.get(key, D("0")) + balance
                tagged_by_code[line["code"]] = tagged_by_code.get(line["code"], D("0")) + balance
        product_by_id = {int(row["id"]): row for row in products}
        warehouse_by_id = {int(row["id"]): row for row in warehouse_records}
        detailed_scopes = set(warehouse_product_totals)
        detailed_scopes.update((wh_id, product_id, code) for code, product_id, wh_id in tagged_gl)
        detail_rows = []
        detail_tagged_total = D("0")
        for warehouse_id, product_id, code in sorted(detailed_scopes, key=lambda x: (str(x[2]), x[0] or 0, x[1] or 0)):
            bucket = warehouse_product_totals.get((warehouse_id, product_id, code), {})
            product = product_by_id.get(product_id)
            warehouse = warehouse_by_id.get(warehouse_id)
            valuation = bucket.get("value", D("0"))
            ledger = tagged_gl.get((code, product_id, warehouse_id), D("0"))
            detail_tagged_total += ledger
            detail_rows.append({
                "warehouse_code": warehouse["code"] if warehouse else "—",
                "warehouse_name": warehouse["name_ar"] if warehouse else "غير مرتبط بمخزن",
                "sku": bucket.get("sku", product["sku"] if product else "صنف غير معروف"),
                "name": bucket.get("name", product["name_ar"] if product else "صنف غير معروف"),
                "account_code": code, "qty": bucket.get("qty", D("0")),
                "value": valuation, "ledger": ledger, "difference": valuation-ledger,
                "status": "matched" if (valuation-ledger).quantize(D("0.01")) == D("0.00") else "review",
            })
        unallocated_by_account = {
            code: gl_by_account.get(code, D("0")) - tagged_by_code.get(code, D("0"))
            for code in inventory_codes
        }
        unallocated_total = sum(unallocated_by_account.values(), D("0"))
        rows = []
        for code in sorted(inventory_codes):
            account = accounts[code]
            valuation = valuation_by_code.get(code, D("0"))
            ledger = gl_by_account.get(code, D("0"))
            rows.append({"code":code,"name":account["name_ar"],"valuation":valuation,
                         "ledger":ledger,"difference":valuation-ledger})
        total_valuation = sum((row["valuation"] for row in rows), D("0"))
        total_ledger = sum((row["ledger"] for row in rows), D("0"))
        total_difference = total_valuation-total_ledger
        for row in rows:
            row["status"] = "matched" if row["difference"].quantize(D("0.01")) == D("0.00") else "review"
        matched_account_count = sum(1 for row in rows if row["status"] == "matched")
        difference_account_count = len(rows)-matched_account_count
        reconciliation_status = "matched" if (total_difference.quantize(D("0.01")) == D("0.00") and layer_integrity_issue_count == 0) else "review"
        detail_matched_count = sum(1 for row in detail_rows if row["status"] == "matched")
        detail_difference_count = len(detail_rows) - detail_matched_count
        unallocated_rows = [{"code": code, "name": accounts[code]["name_ar"],
            "ledger": value} for code, value in sorted(unallocated_by_account.items()) if value != 0]
        return render_template("stock_reconciliation.html", rows=rows, warehouse_rows=warehouse_rows,
            warehouse_product_rows=warehouse_product_rows, detail_rows=detail_rows,
            unallocated_rows=unallocated_rows, unallocated_total=unallocated_total,
            detail_tagged_total=detail_tagged_total, detail_matched_count=detail_matched_count,
            detail_difference_count=detail_difference_count,
            total_valuation=total_valuation,total_ledger=total_ledger,
            total_difference=total_difference,matched_account_count=matched_account_count,
            difference_account_count=difference_account_count,reconciliation_status=reconciliation_status,
            layer_integrity_issue_count=layer_integrity_issue_count,
            layer_integrity_issues=layer_integrity_issues,
            report_generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except (ValueError, *DB_ERROR) as exc:
        flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
        return render_template("stock_reconciliation.html", rows=[], warehouse_rows=[], warehouse_product_rows=[],
            detail_rows=[],unallocated_rows=[],unallocated_total=D("0"),detail_tagged_total=D("0"),
            detail_matched_count=0,detail_difference_count=0,
            total_valuation=D("0"),total_ledger=D("0"),total_difference=D("0"),
            matched_account_count=0,difference_account_count=0,reconciliation_status="error",
            layer_integrity_issue_count=0,layer_integrity_issues=[],
            report_generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),error=True)
    finally:
        conn.close()


@stock_bp.route("/pending")
@stock_permission("stock.manage")
def pending_movements():
    conn = get_db()
    rows = _execute(conn, """SELECT m.*,p.sku,p.name_ar,
        s.name_ar source_name,t.name_ar target_name
        FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
        LEFT JOIN stock_warehouses s ON s.id=m.source_warehouse_id
        LEFT JOIN stock_warehouses t ON t.id=m.target_warehouse_id
        WHERE m.accounting_status IN ('pending_purpose','pending_setup','pending_document')
        ORDER BY m.id""").fetchall()
    conn.close()
    return render_template("stock_pending_movements.html", rows=rows)


@stock_bp.route("/pending/<int:movement_id>/resolve", methods=["POST"])
@stock_permission("stock.manage")
def resolve_pending_movement(movement_id):
    conn = get_db()
    try:
        begin_write_transaction(conn)
        movement_row = _stock_movement_for_update(conn, movement_id)
        movement = dict(movement_row) if movement_row else None
        if movement:
            # The journal helpers expect product["id"]; keep movement_id separate.
            movement["id"] = movement["product_id"]
        if not movement or movement["accounting_status"] not in ("pending_purpose","pending_setup","pending_document"):
            raise ValueError("الحركة غير موجودة أو لم تعد معلقة.")
        if movement["accounting_status"] == "pending_document":
            raise ValueError("هذه الحركة مرتبطة بمستند شراء/مرتجع؛ يجب إكمالها من شاشة المستند الأصلي حتى لا ينشأ قيد مكرر.")
        purpose = movement["issue_purpose"]
        if movement["accounting_status"] == "pending_purpose":
            purpose = request.form.get("issue_purpose", "").strip()
            if purpose not in {"sale","internal"}:
                raise ValueError("حدد غرض الصرف: تكلفة مبيعات أو استخدام داخلي.")
            _execute(conn, "UPDATE stock_movements SET issue_purpose=? WHERE id=?",(purpose,movement_id))
        if movement["movement_type"] == "issue":
            if purpose not in {"sale","internal"}:
                raise ValueError("لا يمكن استكمال الصرف قبل تحديد غرضه.")
            post_issue_journal(conn,movement_id,movement["movement_no"],movement,
                movement["movement_date"],movement["total_cost"],session.get("user_id"),purpose)
        elif movement["movement_type"] == "scrap":
            post_scrap_journal(conn,movement_id,movement["movement_no"],movement,
                movement["movement_date"],movement["total_cost"],session.get("user_id"))
        else:
            raise ValueError("نوع الحركة لا يدعم المعالجة اليدوية من شاشة الحركات المعلقة.")
        final_status = _execute(conn, 
            "SELECT accounting_status FROM stock_movements WHERE id=?", (movement_id,)
        ).fetchone()["accounting_status"]
        conn.commit()
        try:
            audit_columns=_columns(conn, "audit_log")
            if {"user_id","action","details","created_at"}.issubset(audit_columns):
                _execute(conn, """INSERT INTO audit_log(user_id,action,details,created_at)
                    VALUES(?,?,?,?)""",(session.get("user_id"),"stock_pending_movement_processed",
                    f"معالجة الحركة المعلقة {movement['movement_no']}؛ الحالة النهائية: {final_status}؛ غرض الصرف: {purpose or 'غير محدد'}",
                    datetime.now().isoformat(timespec="seconds")))
                conn.commit()
        except DB_ERROR:
            conn.rollback()  # Audit failure must never roll back the accounting operation.
        if final_status == "posted":
            flash("تمت معالجة الحركة وترحيل قيدها المحاسبي.", "success")
        else:
            flash("لم تُرحّل الحركة؛ ما زالت معلقة بسبب نقص إعدادات الحسابات. راجع ملف النشاط وربط الحسابات ثم أعد المحاولة.", "warning")
    except (ValueError, *DB_ERROR) as exc:
        conn.rollback()
        flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."),"error")
    finally:
        conn.close()
    return redirect(url_for("stock.pending_movements"))


@stock_bp.route("/barcode")
@stock_permission("stock.view")
def barcode_lookup():
    code=request.args.get("q","").strip()
    c=get_db()
    product=_execute(c, "SELECT * FROM stock_products WHERE barcode=? OR sku=?",(code,code)).fetchone() if code else None
    c.close()
    return render_template("stock_barcode.html",product=product,code=code)


def _stocktake_layer_balance(c, product_id, warehouse_id, location_id=None, lot_id=None):
    sql = f"""SELECT qty_remaining,unit_cost,value_remaining
        FROM stock_valuation_layers WHERE product_id=? AND warehouse_id=?
        AND {decimal_compare_expression(c, "qty_remaining")} > 0"""
    args = [product_id, warehouse_id]
    if location_id is not None:
        sql += " AND location_id IS NULL" if location_id == 0 else " AND location_id=?"
        if location_id != 0: args.append(location_id)
    if lot_id is not None:
        sql += " AND lot_id IS NULL" if lot_id == 0 else " AND lot_id=?"
        if lot_id != 0: args.append(lot_id)
    layers = _execute(c, sql + " ORDER BY id", args).fetchall()
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
            if not _execute(c, "SELECT 1 FROM stock_warehouses WHERE id=? AND active=1", (warehouse_id,)).fetchone():
                raise ValueError("اختر مخزنًا نشطًا.")
            begin_write_transaction(c)
            lock_stock_warehouses(c, warehouse_id)
            ensure_stock_date_open(c, count_date, "تاريخ الجرد")
            count_no = "STK-" + date.fromisoformat(count_date).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6].upper()
            movement_cutoff = _execute(c, """SELECT COALESCE(MAX(id),0) AS movement_cutoff FROM stock_movements
                WHERE source_warehouse_id=? OR target_warehouse_id=?""",
                (warehouse_id,warehouse_id)).fetchone()["movement_cutoff"]
            stocktake_id = insert_and_get_id(c, """INSERT INTO stocktakes(count_no,warehouse_id,count_date,notes,created_by,movement_cutoff_id)
                VALUES(?,?,?,?,?,?)""",
                (count_no, warehouse_id, count_date, request.form.get("notes", "").strip(),
                 session.get("user_id"), movement_cutoff))
            locations = [row["id"] for row in _execute(c, """SELECT id FROM stock_locations
                WHERE warehouse_id=? AND active=1 AND location_type='bin' ORDER BY id""", (warehouse_id,)).fetchall()]
            for product in _execute(c, "SELECT id,track_lot FROM stock_products WHERE active=1 ORDER BY sku").fetchall():
                existing_scopes = _execute(c, f"""SELECT DISTINCT location_id,lot_id FROM stock_valuation_layers
                    WHERE product_id=? AND warehouse_id=? AND {decimal_compare_expression(c, "qty_remaining")} > 0""",
                    (product["id"], warehouse_id)).fetchall()
                existing_locations = {row["location_id"] for row in existing_scopes}
                default_row = _execute(c, """SELECT location_id FROM stock_product_locations
                    WHERE product_id=? AND warehouse_id=?""", (product["id"], warehouse_id)).fetchone()
                if locations:
                    product_locations = set(locations) | existing_locations
                    if default_row:
                        product_locations.add(default_row["location_id"])
                else:
                    product_locations = existing_locations
                    if default_row:
                        product_locations.add(default_row["location_id"])
                if not product_locations:
                    product_locations = {None}
                product_lots = {row["lot_id"] for row in existing_scopes}
                if product["track_lot"]:
                    product_lots.update(row["id"] for row in _execute(c, 
                        "SELECT id FROM stock_lots WHERE product_id=? ORDER BY id", (product["id"],)).fetchall())
                if not product_lots:
                    product_lots = {None}
                for location_id in sorted(product_locations, key=lambda x: (x is None, x or 0)):
                    for lot_id in sorted(product_lots, key=lambda x: (x is None, x or 0)):
                        qty, _ = _stocktake_layer_balance(c, product["id"], warehouse_id,
                            location_id if location_id is not None else 0, lot_id if lot_id is not None else 0)
                        _execute(c, """INSERT INTO stocktake_lines(stocktake_id,product_id,location_id,lot_id,snapshot_qty)
                            VALUES(?,?,?,?,?)""", (stocktake_id, product["id"], location_id, lot_id, str(qty)))
            c.commit()
            flash("تم إنشاء أمر الجرد وحفظ الأرصدة الدفترية وقت الإنشاء. أدخل الكميات الفعلية ثم احفظ العد.", "success")
            return redirect(url_for("stock.count_detail", stocktake_id=stocktake_id))
        except (ValueError, *DB_INTEGRITY_ERROR) as exc:
            c.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "تعذر إنشاء أمر الجرد.", "error")
        finally:
            c.close()
        return redirect(url_for("stock.counts"))
    warehouses = _execute(c, "SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    counts_page = fetch_page(c,
        """SELECT s.*,w.code warehouse_code,w.name_ar warehouse_name,
           (SELECT COUNT(*) FROM stocktake_lines l WHERE l.stocktake_id=s.id) line_count
           FROM stocktakes s JOIN stock_warehouses w ON w.id=s.warehouse_id ORDER BY s.id DESC""",
        "SELECT COUNT(*) FROM stocktakes",
        page=request.args.get("counts_page",1,type=int),
        per_page=request.args.get("counts_per_page",50,type=int))
    rows = counts_page["rows"]
    c.close()
    return render_template("stock_counts.html", warehouses=warehouses, rows=rows, counts_page=counts_page, today=date.today().isoformat())


@stock_bp.route("/counts/<int:stocktake_id>", methods=["GET", "POST"])
@stock_permission("stock.manage")
def count_detail(stocktake_id):
    c = get_db()
    stocktake = _execute(c, """SELECT s.*,w.code warehouse_code,w.name_ar warehouse_name
        FROM stocktakes s JOIN stock_warehouses w ON w.id=s.warehouse_id WHERE s.id=?""", (stocktake_id,)).fetchone()
    if not stocktake:
        c.close()
        flash("أمر الجرد غير موجود.", "error")
        return redirect(url_for("stock.counts"))
    if request.method == "POST":
        try:
            begin_write_transaction(c)
            # Lock the document and warehouse so approvals and stock movements serialize.
            stocktake = _stocktake_for_update(c, stocktake_id)
            if stocktake:
                lock_stock_warehouses(c, stocktake["warehouse_id"])
            if not stocktake or stocktake["status"] not in ("draft", "counted"):
                raise ValueError("لا يمكن تعديل جرد تم اعتماده.")
            lines = _execute(c, "SELECT id,snapshot_qty FROM stocktake_lines WHERE stocktake_id=?", (stocktake_id,)).fetchall()
            ensure_stock_date_open(c, stocktake["count_date"], "تاريخ الجرد")
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
                _execute(c, """UPDATE stocktake_lines SET counted_qty=?,variance_qty=?,counted_unit_cost=?,
                    variance_value=?,notes=? WHERE id=?""",
                    (str(q4(counted)), str(variance), str(money(unit_cost)), str(estimated_value),
                     request.form.get("notes_" + str(line["id"]), "").strip(), line["id"]))
            _execute(c, "UPDATE stocktakes SET status='counted' WHERE id=?", (stocktake_id,))
            c.commit()
            flash("تم حفظ العد الفعلي وحساب فروق الجرد. راجع التقرير قبل الاعتماد والترحيل.", "success")
            return redirect(url_for("stock.count_detail", stocktake_id=stocktake_id))
        except (ValueError, *DB_ERROR) as exc:
            c.rollback()
            flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
    lines = _execute(c, """WITH RECURSIVE location_paths(id,warehouse_id,path) AS (
        SELECT id,warehouse_id,code || ' - ' || name_ar FROM stock_locations WHERE parent_id IS NULL
        UNION ALL
        SELECT child.id,child.warehouse_id,parent.path || ' / ' || child.code || ' - ' || child.name_ar
        FROM stock_locations child JOIN location_paths parent ON child.parent_id=parent.id
    )
    SELECT l.*,p.sku,p.name_ar,p.uom,p.purchase_price,
        loc.code location_code,loc.name_ar location_name,lp.path location_path,lot.lot_number
        FROM stocktake_lines l JOIN stock_products p ON p.id=l.product_id
        LEFT JOIN stock_locations loc ON loc.id=l.location_id
        LEFT JOIN location_paths lp ON lp.id=l.location_id
        LEFT JOIN stock_lots lot ON lot.id=l.lot_id
        WHERE l.stocktake_id=? ORDER BY p.sku,lp.path,lot.lot_number""", (stocktake_id,)).fetchall()
    accounts = _execute(c, """SELECT id,code,name_ar,account_type FROM accounts
        WHERE active=1 AND is_group=0 AND account_type IN ('expense','revenue') ORDER BY account_type,code""").fetchall()
    c.close()
    return render_template("stock_count_detail.html", stocktake=stocktake, lines=lines, accounts=accounts)


@stock_bp.route("/counts/<int:stocktake_id>/approve", methods=["POST"])
@stock_permission("stock.manage")
def approve_count(stocktake_id):
    c = get_db()
    try:
        begin_write_transaction(c)
        stocktake = _stocktake_for_update(c, stocktake_id)
        if stocktake:
            lock_stock_warehouses(c, stocktake["warehouse_id"])
        if not stocktake or stocktake["status"] != "counted":
            raise ValueError("يجب حفظ العد أولًا، ولا يمكن اعتماد أمر غير جاهز للمراجعة.")
        if int(stocktake["created_by"] or 0) == int(session.get("user_id") or 0):
            raise ValueError("لا يجوز اعتماد الجرد بواسطة نفس المستخدم الذي أنشأه. اطلب مراجعة واعتماد مستخدم آخر.")
        ensure_stock_date_open(c, stocktake["count_date"], "تاريخ الجرد")
        lines = _execute(c, """SELECT l.*,p.sku,p.name_ar FROM stocktake_lines l
            JOIN stock_products p ON p.id=l.product_id WHERE l.stocktake_id=? ORDER BY p.sku,l.location_id,l.lot_id""",
            (stocktake_id,)).fetchall()
        if not lines or any(line["counted_qty"] is None for line in lines):
            raise ValueError("بيانات العد غير مكتملة.")
        later_movement = _execute(c, """SELECT 1 FROM stock_movements
            WHERE id>? AND (source_warehouse_id=? OR target_warehouse_id=?) LIMIT 1""",
            (stocktake["movement_cutoff_id"],stocktake["warehouse_id"],stocktake["warehouse_id"])).fetchone()
        if later_movement:
            raise ValueError("تم تسجيل حركة مخزنية بعد بدء الجرد. لا يمكن اعتماد المطابقة؛ أوقف الحركة وأنشئ أمر جرد جديد.")
        from activity_profiles import active_posting_accounts, get_active_activity_profile, resolve_product_account_code
        profile = get_active_activity_profile(c)
        posting = active_posting_accounts(c)
        base_defaults = __import__("chart_templates").ACTIVITY_POSTING_ACCOUNTS.get(
            profile["base_activity"], {}
        )
        inventory_code = posting.get("inventory")
        inventory = _execute(c, "SELECT id,is_group,active,account_type FROM accounts WHERE code=?",
                              (inventory_code,)).fetchone() if inventory_code else None
        if not inventory or inventory["is_group"] or not inventory["active"] or inventory["account_type"] != "asset":
            raise ValueError("اضبط حساب المخزون كحساب أصول تفصيلي نشط قبل اعتماد الجرد.")
        # Variance accounts are resolved from the selected activity profile.
        # Older custom profiles inherit the safe base-activity defaults.
        shortage_code = posting.get("stocktake_shortage") or base_defaults.get("stocktake_shortage")
        surplus_code = posting.get("stocktake_surplus") or base_defaults.get("stocktake_surplus")
        shortage_account = _execute(c, 
            "SELECT id,code,account_type,is_group,active FROM accounts WHERE code=?",
            (shortage_code,)
        ).fetchone() if shortage_code else None
        surplus_account = _execute(c, 
            "SELECT id,code,account_type,is_group,active FROM accounts WHERE code=?",
            (surplus_code,)
        ).fetchone() if surplus_code else None
        if (not shortage_account or shortage_account["is_group"] or not shortage_account["active"]
                or shortage_account["account_type"] != "expense"):
            raise ValueError("يجب ضبط حساب عجز المخزون في ملف النشاط كحساب مصروف تفصيلي نشط قبل اعتماد الجرد.")
        if (not surplus_account or surplus_account["is_group"] or not surplus_account["active"]
                or surplus_account["account_type"] != "expense"):
            raise ValueError("يجب ضبط حساب زيادة المخزون في ملف النشاط كحساب مصروف تفصيلي نشط قبل اعتماد الجرد.")
        journal_lines, movement_ids, non_journal_movement_ids = [], [], []
        for line in lines:
            product_inventory_code = resolve_product_account_code(
                c, line["product_id"], "inventory", inventory_code
            )
            product_inventory = _execute(c, 
                "SELECT id,is_group,active,account_type FROM accounts WHERE code=?",
                (product_inventory_code,)
            ).fetchone() if product_inventory_code else None
            if (not product_inventory or product_inventory["is_group"] or
                    not product_inventory["active"] or product_inventory["account_type"] != "asset"):
                raise ValueError("حساب مخزون الصنف " + line["sku"] + " غير مضبوط كحساب أصول تفصيلي نشط.")
            snapshot = D(str(line["snapshot_qty"]))
            scope_location = line["location_id"] if line["location_id"] is not None else 0
            scope_lot = line["lot_id"] if line["lot_id"] is not None else 0
            current, _ = _stocktake_layer_balance(c, line["product_id"], stocktake["warehouse_id"],
                                                  scope_location, scope_lot)
            if current != snapshot:
                raise ValueError("تغير الرصيد الدفتري للصنف " + line["sku"] + " في الموقع/التشغيلة المحددة بعد بدء الجرد. أنشئ أمر جرد جديد.")
            variance = D(str(line["variance_qty"] or "0"))
            if variance == 0:
                _execute(c, "UPDATE stocktake_lines SET variance_value='0' WHERE id=?", (line["id"],))
                continue
            if variance < 0:
                qty = -variance
                method_row = _execute(c, "SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
                method = method_row["setting_value"] if method_row else "fifo"
                cost, _ = consume_layers(c, line["product_id"], stocktake["warehouse_id"], qty, method,
                                         lot_id=scope_lot, location_id=scope_location)
                movement_no = stocktake["count_no"] + "-" + str(line["id"]) + "-S"
                movement_id = insert_and_get_id(c, """INSERT INTO stock_movements(movement_no,movement_type,product_id,
                    source_warehouse_id,source_location_id,lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status)
                    VALUES(?,'issue',?,?,?,?,?,?,?,?,?,?, 'pending')""",
                    (movement_no,line["product_id"],stocktake["warehouse_id"],line["location_id"],line["lot_id"],str(qty),
                     str(cost / qty),str(cost),stocktake["count_date"],"عجز جرد فعلي",session.get("user_id")))
                if cost > 0:
                    movement_ids.append(movement_id)
                    journal_lines.append((shortage_account["id"],cost,0,"عجز جرد - " + line["name_ar"]))
                    journal_lines.append((
                        product_inventory["id"],0,cost,"تخفيض مخزون - " + line["name_ar"],
                        {"stock_product_id": int(line["product_id"]),
                         "stock_warehouse_id": int(stocktake["warehouse_id"])}
                    ))
                else:
                    # Zero-value layers can be consumed without a journal entry.
                    non_journal_movement_ids.append(movement_id)
                _execute(c, "UPDATE stocktake_lines SET variance_value=? WHERE id=?", (str(-cost),line["id"]))
            else:
                unit_cost = D(str(line["counted_unit_cost"] or "0"))
                if unit_cost <= 0:
                    raise ValueError("أدخل تكلفة الوحدة للصنف " + line["sku"] + " ذي الزيادة قبل الاعتماد.")
                cost = money(variance * unit_cost)
                movement_no = stocktake["count_no"] + "-" + str(line["id"]) + "-P"
                movement_id = insert_and_get_id(c, """INSERT INTO stock_movements(movement_no,movement_type,product_id,
                    target_warehouse_id,target_location_id,lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status)
                    VALUES(?,'receipt',?,?,?,?,?,?,?,?,?, ?, 'pending')""",
                    (movement_no,line["product_id"],stocktake["warehouse_id"],line["location_id"],line["lot_id"],
                     str(variance),str(unit_cost),str(cost),stocktake["count_date"],"زيادة جرد فعلي",session.get("user_id")))
                _execute(c, """INSERT INTO stock_valuation_layers(product_id,warehouse_id,location_id,lot_id,
                    source_movement_id,qty_in,qty_remaining,unit_cost,value_remaining)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (line["product_id"],stocktake["warehouse_id"],line["location_id"],line["lot_id"],
                     movement_id,str(variance),str(variance),str(unit_cost),str(cost)))
                if cost > 0:
                    movement_ids.append(movement_id)
                    journal_lines.extend([(product_inventory["id"],cost,0,"زيادة مخزون - " + line["name_ar"],
                                          {"stock_product_id": int(line["product_id"]),
                                           "stock_warehouse_id": int(stocktake["warehouse_id"])}),
                                          (surplus_account["id"],0,cost,"زيادة مخزون - عكس مصروف فروق الجرد - " + line["name_ar"])])
                else:
                    non_journal_movement_ids.append(movement_id)
                _execute(c, "UPDATE stocktake_lines SET variance_value=? WHERE id=?", (str(cost),line["id"]))
        journal_id = None
        if journal_lines:
            journal_id = post_journal(c,stocktake["count_date"],stocktake["count_no"],
                "تسوية فروق الجرد - " + stocktake["count_no"],journal_lines,session.get("user_id"))
        for movement_id in movement_ids:
            _execute(c, "UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?",
                      (journal_id,movement_id))
        for movement_id in non_journal_movement_ids:
            _execute(c, "UPDATE stock_movements SET journal_entry_id=NULL,accounting_status='not_required' WHERE id=?",
                      (movement_id,))
        _execute(c, """UPDATE stocktakes SET status='approved',approved_by=?,approved_at=CURRENT_TIMESTAMP,
            journal_entry_id=? WHERE id=?""", (session.get("user_id"),journal_id,stocktake_id))
        c.commit()
        flash("تم اعتماد الجرد وتحديث طبقات المخزون وترحيل قيد التسوية." if journal_id
              else "تم اعتماد الجرد؛ لا توجد فروق تستلزم قيدًا محاسبيًا.", "success")
    except (ValueError, *DB_ERROR) as exc:
        c.rollback()
        flash(safe_error_message(exc, "تعذر إتمام العملية بسبب تعارض في البيانات أو قاعدة البيانات. لم يتم حفظ أي تغييرات."), "error")
    finally:
        c.close()
    return redirect(url_for("stock.count_detail", stocktake_id=stocktake_id))
