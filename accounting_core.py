"""Shared NEXUS ERP posting, integrity checks, and operational reconciliation."""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, date

CENT = Decimal("0.01")


def amt(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def money(value):
    """Shared two-decimal money quantizer used by inventory and purchasing."""
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


class _DecimalSum:
    def __init__(self):
        self.total = Decimal("0")
    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value))
    def finalize(self):
        return format(self.total, "f")


class _DecimalNet:
    def __init__(self):
        self.total = Decimal("0")
    def step(self, debit, credit):
        self.total += Decimal(str(debit or "0")) - Decimal(str(credit or "0"))
    def finalize(self):
        return format(self.total, "f")


class _DecimalLayerValueSum:
    def __init__(self):
        self.total = Decimal("0")
    def step(self, value_remaining, quantity, unit_cost):
        if value_remaining not in (None, ""):
            self.total += Decimal(str(value_remaining))
        else:
            self.total += Decimal(str(quantity or "0")) * Decimal(str(unit_cost or "0"))
    def finalize(self):
        return format(self.total, "f")


class _DecimalRunningNet:
    """Exact Decimal window function for chronological ledger balances."""
    def __init__(self):
        self.total = Decimal("0")
    def step(self, debit, credit):
        self.total += Decimal(str(debit or "0")) - Decimal(str(credit or "0"))
    def inverse(self, debit, credit):
        self.total -= Decimal(str(debit or "0")) - Decimal(str(credit or "0"))
    def value(self):
        return format(self.total, "f")
    def finalize(self):
        return format(self.total, "f")


def register_decimal_aggregates(conn):
    """Register exact SQLite aggregate functions for TEXT monetary columns."""
    conn.create_aggregate("DECIMAL_SUM", 1, _DecimalSum)
    conn.create_aggregate("DECIMAL_NET", 2, _DecimalNet)
    conn.create_aggregate("DECIMAL_LAYER_VALUE_SUM", 3, _DecimalLayerValueSum)
    if hasattr(conn, "create_window_function"):
        conn.create_window_function("DECIMAL_RUNNING_NET", 2, _DecimalRunningNet)


def post_journal(conn, entry_date, reference, description, lines, created_by=None, dimensions=None):
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
    if _table_exists(conn, "financial_years"):
        financial_year = conn.execute("""SELECT year_name,status FROM financial_years
            WHERE start_date<=? AND end_date>=? LIMIT 1""",
            (normalized_date, normalized_date)).fetchone()
        if financial_year and financial_year["status"] != "open":
            raise ValueError(f"لا يمكن الترحيل داخل السنة المالية {financial_year['year_name']} لأنها ليست مفتوحة.")
    dimensions = dimensions or {}
    dimension_keys = ("branch_id", "cost_center_id", "project_id", "customer_id", "salesperson_id", "supplier_id")
    unknown_dimensions = sorted(set(dimensions) - set(dimension_keys))
    if unknown_dimensions:
        raise ValueError(
            "أبعاد تحليلية غير معروفة: " + ", ".join(unknown_dimensions)
        )
    available_dimension_keys = tuple(key for key in dimension_keys if key in _columns(conn, "journal_lines"))
    missing_dimensions = [key for key, value in dimensions.items()
                          if key in dimension_keys and value not in (None, "") and key not in available_dimension_keys]
    if missing_dimensions:
        raise ValueError("يجب تحديث مخطط الأبعاد التحليلية قبل ترحيل القيد.")
    normalized = []
    for line in lines:
        if len(line) not in (4, 5):
            raise ValueError("صيغة أحد أسطر القيد غير صحيحة.")
        account_id, debit, credit, memo = line[:4]
        line_dimensions = dict(dimensions)
        if len(line) == 5:
            if line[4] is not None and not isinstance(line[4], dict):
                raise ValueError("الأبعاد التحليلية لسطر القيد يجب أن تكون في صورة قاموس.")
            line_dimensions.update(line[4] or {})
        unknown_line_dimensions = sorted(set(line_dimensions) - set(dimension_keys))
        if unknown_line_dimensions:
            raise ValueError("أبعاد تحليلية غير معروفة: " + ", ".join(unknown_line_dimensions))
        missing_line_dimensions = [
            key for key, value in line_dimensions.items()
            if value not in (None, "") and key not in available_dimension_keys
        ]
        if missing_line_dimensions:
            raise ValueError("يجب تحديث مخطط الأبعاد التحليلية قبل ترحيل القيد.")
        dimension_values = tuple(line_dimensions.get(key) for key in available_dimension_keys)
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
                "SELECT id,is_group,active,account_type FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not account or account["is_group"] or not account["active"]:
                raise ValueError("لا يمكن الترحيل إلى حساب غير موجود أو غير نشط أو حساب تجميعي.")
            normalized.append((int(account_id), dr, cr, str(memo or ""), *dimension_values))
    if len(normalized) < 2:
        raise ValueError("القيد يحتاج إلى سطرين على الأقل.")
    debit = sum((x[1] for x in normalized), Decimal("0"))
    credit = sum((x[2] for x in normalized), Decimal("0"))
    if debit <= 0 or debit != credit:
        raise ValueError(f"القيد غير متوازن: مدين {debit} مقابل دائن {credit}.")
    existing = conn.execute(
        "SELECT id,entry_date,description FROM journal_entries WHERE reference=? AND status='posted' ORDER BY id LIMIT 1",
        (reference,),
    ).fetchone()
    if existing:
        has_dimensions = bool(available_dimension_keys)
        selected = "account_id,debit,credit,memo" + ("," + ",".join(available_dimension_keys) if has_dimensions else "")
        existing_lines = conn.execute(
            f"SELECT {selected} FROM journal_lines WHERE entry_id=? ORDER BY id",
            (existing["id"],),
        ).fetchall()
        if has_dimensions:
            expected_lines = sorted(
                [(aid, format(dr, "f"), format(cr, "f"), memo, *dims)
                 for aid, dr, cr, memo, *dims in normalized],
                key=lambda item: tuple(str(x) for x in item),
            )
            actual_lines = sorted(
                [(int(row["account_id"]), format(amt(row["debit"]), "f"),
                  format(amt(row["credit"]), "f"), str(row["memo"] or ""),
                  *(row[key] for key in available_dimension_keys)) for row in existing_lines],
                key=lambda item: tuple(str(x) for x in item),
            )
        else:
            expected_lines = sorted(
                [(aid, format(dr, "f"), format(cr, "f"), memo)
                 for aid, dr, cr, memo, *_dims in normalized],
                key=lambda item: tuple(str(x) for x in item),
            )
            actual_lines = sorted(
                [(int(row["account_id"]), format(amt(row["debit"]), "f"),
                  format(amt(row["credit"]), "f"), str(row["memo"] or "")) for row in existing_lines],
                key=lambda item: tuple(str(x) for x in item),
            )
        if (existing["entry_date"] != normalized_date or
                str(existing["description"] or "") != str(description or "") or
                actual_lines != expected_lines):
            raise ValueError("رقم مرجع القيد مستخدم بالفعل في قيد مختلف. راجع المستند ورقم المرجع قبل الترحيل.")
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO journal_entries(entry_date,reference,description,status,created_by,created_at) "
        "VALUES(?,?,?,'posted',?,?)",
        (entry_date, reference, description, created_by, datetime.now().isoformat(timespec="seconds")),
    )
    entry_id = cur.lastrowid
    if available_dimension_keys:
        columns = ("entry_id", "account_id", "debit", "credit", "memo", *available_dimension_keys)
        placeholders = ",".join("?" for _ in columns)
        conn.executemany(
            "INSERT INTO journal_lines(" + ",".join(columns) + ") VALUES(" + placeholders + ")",
            [(entry_id, aid, format(dr, 'f'), format(cr, 'f'), memo, *dims)
             for aid, dr, cr, memo, *dims in normalized],
        )
    else:
        conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?)",
            [(entry_id, aid, format(dr, 'f'), format(cr, 'f'), memo) for aid, dr, cr, memo, *_dims in normalized],
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
    """Return inventory control accounts from the selected activity chart."""
    from chart_templates import ACTIVITY_ACCOUNT_ROLES
    return ACTIVITY_ACCOUNT_ROLES.get(activity, ACTIVITY_ACCOUNT_ROLES["commercial"])["inventory"]


def _decimal_sum(values):
    return sum((Decimal(str(value or "0")) for value in values), Decimal("0"))


def _money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def reconciliation_snapshot(conn):
    """Build the integrity snapshot using Decimal only for every monetary total."""
    # Aggregate every posted journal in one grouped query. The previous
    # implementation issued one additional SELECT per entry (N+1), which
    # became expensive on large ledgers. DECIMAL_SUM keeps TEXT money exact.
    register_decimal_aggregates(conn)
    entries = conn.execute(
        """SELECT je.id,je.reference,
                  COALESCE(DECIMAL_SUM(jl.debit),'0') debit,
                  COALESCE(DECIMAL_SUM(jl.credit),'0') credit
           FROM journal_entries je
           LEFT JOIN journal_lines jl ON jl.entry_id=je.id
           WHERE je.status='posted'
           GROUP BY je.id,je.reference
           ORDER BY je.id DESC"""
    ).fetchall()
    unbalanced = []
    all_debit, all_credit = Decimal("0"), Decimal("0")
    for entry in entries:
        debit = Decimal(str(entry["debit"] or "0"))
        credit = Decimal(str(entry["credit"] or "0"))
        all_debit += debit
        all_credit += credit
        if debit != credit:
            unbalanced.append({"id": entry["id"], "reference": entry["reference"],
                               "debit": _money(debit), "credit": _money(credit)})

    journal_totals = {"debit": _money(all_debit), "credit": _money(all_credit)}
    settings = {r["setting_key"]: r["setting_value"] for r in conn.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity','company_name','base_currency')"
    )} if _table_exists(conn, "company_settings") else {}
    codes = _inventory_codes(settings.get("activity", "commercial"))

    stock_value = Decimal("0")
    if _table_exists(conn, "stock_valuation_layers"):
        layer_cols = _columns(conn, "stock_valuation_layers")
        value_col = "value_remaining" if "value_remaining" in layer_cols else None
        for row in conn.execute("SELECT * FROM stock_valuation_layers").fetchall():
            qty = Decimal(str(row["qty_remaining"] or "0"))
            if qty <= 0:
                continue
            if value_col and row["value_remaining"] not in (None, ""):
                stock_value += Decimal(str(row["value_remaining"]))
            else:
                stock_value += qty * Decimal(str(row["unit_cost"] or "0"))

    found_codes, missing_codes, gl_stock = [], [], None
    if codes:
        placeholders = ",".join("?" for _ in codes)
        found_codes = [r["code"] for r in conn.execute(
            f"SELECT code FROM accounts WHERE code IN ({placeholders}) AND is_group=0 AND active=1 ORDER BY code", codes
        )]
        missing_codes = [code for code in codes if code not in found_codes]
        if found_codes:
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM accounts WHERE code IN ({','.join('?' for _ in found_codes)}) AND is_group=0 AND active=1",
                found_codes
            )]
            gl_stock = Decimal("0")
            if ids:
                marks = ",".join("?" for _ in ids)
                for row in conn.execute(
                    f"""SELECT jl.debit,jl.credit FROM journal_lines jl
                        JOIN journal_entries je ON je.id=jl.entry_id
                        WHERE je.status='posted' AND jl.account_id IN ({marks})""", ids
                ):
                    gl_stock += Decimal(str(row["debit"] or "0")) - Decimal(str(row["credit"] or "0"))

    grni = Decimal("0")
    if (_table_exists(conn, "goods_receipts") and
            _table_exists(conn, "supplier_bill_receipts") and
            _table_exists(conn, "supplier_bills")):
        receipt_link_columns = _columns(conn, "supplier_bill_receipts")
        bill_columns = _columns(conn, "supplier_bills")
        # A draft, pending, or rejected invoice does not settle GRNI. The
        # liability moves to AP only after the supplier bill is posted.
        if {"bill_id", "receipt_id", "matched_amount"}.issubset(receipt_link_columns) and "status" in bill_columns:
            matched_by_receipt = {}
            for row in conn.execute("""SELECT x.receipt_id,x.matched_amount
                FROM supplier_bill_receipts x
                JOIN supplier_bills b ON b.id=x.bill_id
                WHERE b.status='posted'"""):
                matched_by_receipt[row["receipt_id"]] = matched_by_receipt.get(row["receipt_id"], Decimal("0")) + Decimal(str(row["matched_amount"] or "0"))
            for row in conn.execute("SELECT id,total_value FROM goods_receipts WHERE status='posted'"):
                unmatched = Decimal(str(row["total_value"] or "0")) - matched_by_receipt.get(row["id"], Decimal("0"))
                if unmatched > 0:
                    grni += unmatched

    open_ap = Decimal("0")
    if _table_exists(conn, "supplier_bills"):
        cols = _columns(conn, "supplier_bills")
        if {"total_amount", "paid_amount", "status"}.issubset(cols):
            credited_expr = "credited_amount" if "credited_amount" in cols else "'0'"
            for row in conn.execute(
                f"SELECT total_amount,paid_amount,{credited_expr} credited_amount "
                "FROM supplier_bills WHERE status='posted'"
            ):
                open_ap += (Decimal(str(row["total_amount"] or "0")) -
                            Decimal(str(row["paid_amount"] or "0")) -
                            Decimal(str(row["credited_amount"] or "0")))

    linkage_checks = [
        ("goods_receipts", "إيصالات الاستلام", "journal_entry_id"),
        ("supplier_bills", "فواتير الموردين", "journal_entry_id"),
        ("supplier_payments", "مدفوعات الموردين", "journal_entry_id"),
        ("sales_invoices", "فواتير البيع", "journal_entry_id"),
        ("customer_receipts", "تحصيلات العملاء", "journal_entry_id"),
        ("customer_receipt_batches", "إيصالات التحصيل المجمعة", "journal_entry_id"),
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

    pending_stock = int(_scalar(conn, """SELECT COUNT(*) FROM stock_movements
        WHERE accounting_status LIKE 'pending%'""") or 0) if "accounting_status" in _columns(conn, "stock_movements") else 0
    difference = _money(gl_stock - stock_value) if gl_stock is not None else None
    return {
        "unbalanced": unbalanced, "journal_totals": journal_totals,
        "stock_value": _money(stock_value), "gl_stock": _money(gl_stock) if gl_stock is not None else None,
        "stock_difference": difference, "inventory_account_codes": found_codes,
        "missing_inventory_codes": missing_codes, "grni": _money(grni), "open_ap": _money(open_ap),
        "unlinked_documents": unlinked_documents, "pending_stock": pending_stock, "settings": settings,
    }
