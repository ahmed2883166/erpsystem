"""Optional analytical dimensions for NEXUS ERP.

Legacy journal lines remain valid: all dimension references are nullable and
are introduced through additive SQLite/PostgreSQL migrations.
"""
from db_compat import database_dialect, execute_script, table_columns, table_exists, transactional_migration
from db_sql import execute_compat


DIMENSIONS = {
    "branch": ("branches", "الفرع"),
    "cost_center": ("cost_centers", "مركز التكلفة"),
    "project": ("projects", "المشروع"),
}


def _add_dimension_column(conn, columns, name, table):
    if name not in columns:
        execute_compat(
            conn,
            f"ALTER TABLE journal_lines ADD COLUMN {name} INTEGER REFERENCES {table}(id)",
        )
        columns.add(name)


@transactional_migration
def initialize_analytic_dimensions_schema(conn):
    """Create dimension tables and add nullable journal references portably."""
    execute_script(conn, """
    CREATE TABLE IF NOT EXISTS branches(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS cost_centers(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS projects(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active', active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    branch_columns = table_columns(conn, "branches")
    for column in ("address", "phone", "email", "tax_number"):
        if column not in branch_columns:
            execute_compat(
                conn,
                f"ALTER TABLE branches ADD COLUMN {column} TEXT NOT NULL DEFAULT ''",
            )
            branch_columns.add(column)

    if not table_exists(conn, "journal_lines"):
        if database_dialect(conn) == "sqlite":
            conn.commit()
        return

    columns = table_columns(conn, "journal_lines")
    for name, table in (
        ("branch_id", "branches"),
        ("cost_center_id", "cost_centers"),
        ("project_id", "projects"),
    ):
        _add_dimension_column(conn, columns, name, table)

    for name, columns_sql in (
        ("idx_journal_lines_branch", "branch_id,entry_id"),
        ("idx_journal_lines_cost_center", "cost_center_id,entry_id"),
        ("idx_journal_lines_project", "project_id,entry_id"),
    ):
        execute_compat(
            conn,
            f"CREATE INDEX IF NOT EXISTS {name} ON journal_lines({columns_sql})",
        )

    # Operational stock dimensions are separate from analytical dimensions.
    if table_exists(conn, "stock_products"):
        _add_dimension_column(conn, columns, "stock_product_id", "stock_products")
        execute_compat(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_journal_lines_stock_product "
            "ON journal_lines(stock_product_id,account_id,entry_id)",
        )
    if table_exists(conn, "stock_warehouses"):
        _add_dimension_column(conn, columns, "stock_warehouse_id", "stock_warehouses")
        execute_compat(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_journal_lines_stock_warehouse "
            "ON journal_lines(stock_warehouse_id,account_id,entry_id)",
        )

    for table, column, index in (
        ("customers", "customer_id", "idx_journal_lines_customer"),
        ("salespersons", "salesperson_id", "idx_journal_lines_salesperson"),
        ("suppliers", "supplier_id", "idx_journal_lines_supplier"),
    ):
        if table_exists(conn, table):
            _add_dimension_column(conn, columns, column, table)
            execute_compat(
                conn,
                f"CREATE INDEX IF NOT EXISTS {index} ON journal_lines({column},entry_id)",
            )
    if database_dialect(conn) == "sqlite":
        conn.commit()


def active_dimension_options(conn):
    """Load active dimension choices using portable metadata and SQL helpers."""
    branch_columns = table_columns(conn, "branches")
    branch_fields = [
        column if column in branch_columns else f"'' AS {column}"
        for column in ("address", "phone", "email", "tax_number")
    ]
    branches_sql = (
        "SELECT id,code,name_ar," + ",".join(branch_fields) +
        " FROM branches WHERE active=1 ORDER BY code"
    )
    return {
        "branches": execute_compat(conn, branches_sql).fetchall(),
        "cost_centers": execute_compat(
            conn,
            "SELECT id,code,name_ar FROM cost_centers WHERE active=1 ORDER BY code",
        ).fetchall(),
        "projects": execute_compat(
            conn,
            "SELECT id,code,name_ar FROM projects WHERE active=1 ORDER BY code",
        ).fetchall(),
        "customers": execute_compat(
            conn,
            "SELECT id,code,name AS name_ar FROM customers WHERE active=1 ORDER BY code",
        ).fetchall() if table_exists(conn, "customers") else [],
        "salespersons": execute_compat(
            conn,
            "SELECT id,code,name AS name_ar FROM salespersons WHERE active=1 ORDER BY code",
        ).fetchall() if table_exists(conn, "salespersons") else [],
        "suppliers": execute_compat(
            conn,
            "SELECT id,code,name AS name_ar FROM suppliers WHERE active=1 ORDER BY code",
        ).fetchall() if table_exists(conn, "suppliers") else [],
    }


def validate_journal_dimensions(conn, dimensions):
    """Validate optional dimension IDs; return normalized nullable IDs."""
    dimensions = dimensions or {}
    result = {}
    for key, table in (
        ("branch_id", "branches"),
        ("cost_center_id", "cost_centers"),
        ("project_id", "projects"),
        ("customer_id", "customers"),
        ("salesperson_id", "salespersons"),
        ("supplier_id", "suppliers"),
    ):
        raw = dimensions.get(key)
        if raw in (None, ""):
            result[key] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError("قيمة أحد الأبعاد التحليلية غير صحيحة.")
        if not table_exists(conn, table):
            raise ValueError("جدول أحد الأبعاد التحليلية غير مهيأ. أعد تشغيل البرنامج لإكمال التحديث.")
        if not execute_compat(
            conn,
            f"SELECT 1 FROM {table} WHERE id=? AND active=1",
            (value,),
        ).fetchone():
            raise ValueError("اختر بُعدًا تحليليًا موجودًا ونشطًا.")
        result[key] = value
    return result
