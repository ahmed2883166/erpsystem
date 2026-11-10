"""Shared NEXUS ERP posting, integrity checks, and operational reconciliation."""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, date

CENT = Decimal("0.01")


def amt(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def post_journal(conn, entry_date, reference, description, lines, created_by=None):
    """Idempotently post a balanced journal inside the caller's DB transaction."""
    try:
        normalized_date = date.fromisoformat(str(entry_date)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("تاريخ القيد غير صحيح؛ استخدم YYYY-MM-DD.")
    if normalized_date != str(entry_date):
        raise ValueError("تاريخ القيد غير صحيح؛ استخدم YYYY-MM-DD.")
    if _table_exists(conn, "accounting_periods"):
        locked = conn.execute("""SELECT period_name FROM accounting_periods
            WHERE locked=1 AND start_date<=? AND end_date>=? LIMIT 1""",
            (normalized_date, normalized_date)).fetchone()
        if locked:
            raise ValueError(f"الفترة المحاسبية مغلقة: {locked['period_name']}.")
    normalized = []
    for account_id, debit, credit, memo in lines:
        try:
            dr, cr = amt(debit), amt(credit)
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError("قيمة المدين أو الدائن غير رقمية.")
        if not dr.is_finite() or not cr.is_finite():
            raise ValueError("يجب أن تكون المبالغ أرقامًا محدودة وصحيحة.")
        if dr < 0 or cr < 0 or (dr and cr):
            raise ValueError("كل سطر يجب أن يكون مدينًا أو دائنًا فقط وبقيمة غير سالبة.")
        if dr or cr:
            account = conn.execute(
                "SELECT id,is_group,active FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not account or account["is_group"] or not account["active"]:
                raise ValueError("لا يمكن الترحيل إلى حساب غير موجود أو غير نشط أو حساب تجميعي.")
            normalized.append((int(account_id), dr, cr, str(memo or "")))
    if len(normalized) < 2:
        raise ValueError("القيد يحتاج إلى سطرين على الأقل.")
    debit = sum((x[1] for x in normalized), Decimal("0"))
    credit = sum((x[2] for x in normalized), Decimal("0"))
    if debit <= 0 or debit != credit:
        raise ValueError(f"القيد غير متوازن: مدين {debit} مقابل دائن {credit}.")
    existing = conn.execute(
        "SELECT id FROM journal_entries WHERE reference=? AND status='posted' ORDER BY id LIMIT 1",
        (reference,),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO journal_entries(entry_date,reference,description,status,created_by,created_at) "
        "VALUES(?,?,?,'posted',?,?)",
        (entry_date, reference, description, created_by, datetime.now().isoformat(timespec="seconds")),
    )
    entry_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?)",
        [(entry_id, aid, float(dr), float(cr), memo) for aid, dr, cr, memo in normalized],
    )
    return entry_id


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn, table):
    if not _table_exists(conn, table):
        return set()
    return {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def _inventory_codes(activity):
    # Include every inventory control account used by each activity's chart.
    return {
        "commercial": ("1150",),
        "industrial": ("1130", "1140", "1150", "1160"),
        "medical": ("1150", "1160"),
        "restaurant": ("1140", "1150", "1160"),
        "service": (),
    }.get(activity, ("1150",))


def reconciliation_snapshot(conn):
    """Return a schema-tolerant snapshot for the reconciliation screen."""
    unbalanced = conn.execute("""SELECT je.id, je.reference,
        ROUND(COALESCE(SUM(jl.debit),0),2) AS debit,
        ROUND(COALESCE(SUM(jl.credit),0),2) AS credit
      FROM journal_entries je
      LEFT JOIN journal_lines jl ON jl.entry_id=je.id
      WHERE je.status='posted'
      GROUP BY je.id
      HAVING ABS(COALESCE(SUM(jl.debit),0)-COALESCE(SUM(jl.credit),0)) > 0.005
      ORDER BY je.id DESC""").fetchall()

    journal_totals = conn.execute("""SELECT
        ROUND(COALESCE(SUM(jl.debit),0),2) AS debit,
        ROUND(COALESCE(SUM(jl.credit),0),2) AS credit
      FROM journal_lines jl
      JOIN journal_entries je ON je.id=jl.entry_id
      WHERE je.status='posted'""").fetchone()

    settings = {r["setting_key"]: r["setting_value"] for r in conn.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity','company_name','base_currency')"
    )} if _table_exists(conn, "company_settings") else {}
    activity = settings.get("activity", "commercial")
    codes = _inventory_codes(activity)

    stock_value = 0.0
    if _table_exists(conn, "stock_valuation_layers"):
        stock_value = float(_scalar(conn, """SELECT COALESCE(SUM(
            CAST(qty_remaining AS REAL) * CAST(unit_cost AS REAL)),0)
            FROM stock_valuation_layers
            WHERE CAST(qty_remaining AS REAL) > 0""") or 0)

    found_codes = []
    missing_codes = []
    gl_stock = None
    if codes:
        placeholders = ",".join("?" for _ in codes)
        found_codes = [r["code"] for r in conn.execute(
            f"SELECT code FROM accounts WHERE code IN ({placeholders}) AND is_group=0 AND active=1 ORDER BY code",
            codes,
        )]
        missing_codes = [code for code in codes if code not in found_codes]
        if found_codes:
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM accounts WHERE code IN ({','.join('?' for _ in found_codes)}) AND is_group=0 AND active=1",
                found_codes,
            )]
            id_marks = ",".join("?" for _ in ids)
            gl_stock = float(_scalar(conn, f"""SELECT COALESCE(SUM(jl.debit-jl.credit),0)
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE je.status='posted' AND jl.account_id IN ({id_marks})""", ids) or 0)

    # Calculate unmatched GRNI by receipt, not by whether any bill is linked.
    # This preserves partially invoiced receipts and avoids excluding the full receipt.
    grni = 0.0
    if _table_exists(conn, "goods_receipts") and _table_exists(conn, "supplier_bill_receipts"):
        grni = float(_scalar(conn, """SELECT COALESCE(SUM(
            CASE WHEN CAST(r.total_value AS REAL)-COALESCE(m.matched,0) > 0
                 THEN CAST(r.total_value AS REAL)-COALESCE(m.matched,0) ELSE 0 END),0)
          FROM goods_receipts r
          LEFT JOIN (
            SELECT receipt_id, SUM(CAST(matched_amount AS REAL)) AS matched
            FROM supplier_bill_receipts GROUP BY receipt_id
          ) m ON m.receipt_id=r.id
          WHERE r.status='posted'""") or 0)

    open_ap = 0.0
    if _table_exists(conn, "supplier_bills"):
        bill_cols = _columns(conn, "supplier_bills")
        if {"total_amount", "paid_amount", "status"}.issubset(bill_cols):
            open_ap = float(_scalar(conn, """SELECT COALESCE(SUM(
                CAST(total_amount AS REAL)-CAST(paid_amount AS REAL)),0)
                FROM supplier_bills WHERE status='posted'""") or 0)

    # Documents that should have a journal link. Missing tables/older schemas are skipped safely.
    linkage_checks = [
        ("goods_receipts", "إيصالات الاستلام", "journal_entry_id"),
        ("supplier_bills", "فواتير الموردين", "journal_entry_id"),
        ("supplier_payments", "مدفوعات الموردين", "journal_entry_id"),
        ("sales_invoices", "فواتير البيع", "journal_entry_id"),
        ("customer_receipts", "تحصيلات العملاء", "journal_entry_id"),
        ("bank_transactions", "الحركات البنكية", "journal_entry_id"),
    ]
    unlinked_documents = []
    for table, label, link_col in linkage_checks:
        cols = _columns(conn, table)
        if "id" not in cols or link_col not in cols:
            continue
        count = int(_scalar(conn, f'''SELECT COUNT(*) FROM "{table}" d
            LEFT JOIN journal_entries je ON je.id=d."{link_col}"
            WHERE d."{link_col}" IS NULL OR je.id IS NULL''') or 0)
        if count:
            unlinked_documents.append({"label": label, "count": count})

    pending_stock = 0
    if {"accounting_status"}.issubset(_columns(conn, "stock_movements")):
        pending_stock = int(_scalar(conn, """SELECT COUNT(*) FROM stock_movements
            WHERE accounting_status LIKE 'pending%'""") or 0)

    stock_difference = round(gl_stock - stock_value, 2) if gl_stock is not None else None
    return {
        "unbalanced": unbalanced,
        "journal_totals": journal_totals,
        "stock_value": round(stock_value, 2),
        "gl_stock": round(gl_stock, 2) if gl_stock is not None else None,
        "stock_difference": stock_difference,
        "inventory_account_codes": found_codes,
        "missing_inventory_codes": missing_codes,
        "grni": round(grni, 2),
        "open_ap": round(open_ap, 2),
        "unlinked_documents": unlinked_documents,
        "pending_stock": pending_stock,
        "settings": settings,
    }
