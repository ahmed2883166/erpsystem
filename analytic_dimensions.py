"""Optional analytical dimensions for NEXUS ERP.

Legacy journal lines remain valid: all dimension references are nullable and
are introduced through additive SQLite migrations.
"""

DIMENSIONS = {
    "branch": ("branches", "الفرع"),
    "cost_center": ("cost_centers", "مركز التكلفة"),
    "project": ("projects", "المشروع"),
}


def initialize_analytic_dimensions_schema(conn):
    conn.executescript("""
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
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_lines'").fetchone():
        # Dimension tables exist; journal columns are added once the journal table is created.
        conn.commit()
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(journal_lines)")}
    for name, table in (("branch_id", "branches"), ("cost_center_id", "cost_centers"),
                        ("project_id", "projects")):
        if name not in columns:
            conn.execute(f"ALTER TABLE journal_lines ADD COLUMN {name} INTEGER REFERENCES {table}(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_branch ON journal_lines(branch_id,entry_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_cost_center ON journal_lines(cost_center_id,entry_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_project ON journal_lines(project_id,entry_id)")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customers'").fetchone():
        if "customer_id" not in columns:
            conn.execute("ALTER TABLE journal_lines ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_customer ON journal_lines(customer_id,entry_id)")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='salespersons'").fetchone():
        if "salesperson_id" not in columns:
            conn.execute("ALTER TABLE journal_lines ADD COLUMN salesperson_id INTEGER REFERENCES salespersons(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_salesperson ON journal_lines(salesperson_id,entry_id)")
    conn.commit()


def active_dimension_options(conn):
    return {
        "branches": conn.execute("SELECT id,code,name_ar FROM branches WHERE active=1 ORDER BY code").fetchall(),
        "cost_centers": conn.execute("SELECT id,code,name_ar FROM cost_centers WHERE active=1 ORDER BY code").fetchall(),
        "projects": conn.execute("SELECT id,code,name_ar FROM projects WHERE active=1 ORDER BY code").fetchall(),
        "customers": conn.execute("SELECT id,code,name AS name_ar FROM customers WHERE active=1 ORDER BY code").fetchall()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customers'").fetchone() else [],
        "salespersons": conn.execute("SELECT id,code,name AS name_ar FROM salespersons WHERE active=1 ORDER BY code").fetchall()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='salespersons'").fetchone() else [],
    }


def validate_journal_dimensions(conn, dimensions):
    """Validate optional dimension IDs; return normalized nullable IDs."""
    dimensions = dimensions or {}
    result = {}
    for key, table in (("branch_id", "branches"), ("cost_center_id", "cost_centers"),
                       ("project_id", "projects"), ("customer_id", "customers"), ("salesperson_id", "salespersons")):
        raw = dimensions.get(key)
        if raw in (None, ""):
            result[key] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError("قيمة أحد الأبعاد التحليلية غير صحيحة.")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            raise ValueError("جدول أحد الأبعاد التحليلية غير مهيأ. أعد تشغيل البرنامج لإكمال التحديث.")
        if not conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND active=1", (value,)).fetchone():
            raise ValueError("اختر بُعدًا تحليليًا موجودًا ونشطًا.")
        result[key] = value
    return result
