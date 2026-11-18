"""Read-only operational and general-ledger summaries grouped by company activity.

Legacy documents without an activity_code are attributed to the company's existing
primary activity setting. This module never changes accounts or journal entries.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from db_compat import table_columns, table_exists
from db_sql import execute_compat


ZERO = Decimal("0.00")


def _decimal(value):
    try:
        result = Decimal(str(value or "0"))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("توجد قيمة مالية غير صالحة في مستندات النشاط؛ راجع البيانات قبل اعتماد التقرير.")


def _legacy_activity(conn):
    if not table_exists(conn, "company_settings"):
        return "commercial"
    row = execute_compat(
        conn,
        "SELECT setting_value FROM company_settings WHERE setting_key='activity'",
    ).fetchone()
    return str(row["setting_value"] or "commercial") if row else "commercial"


def _activity_expression(conn, table, alias=""):
    prefix = alias + "." if alias else ""
    if "activity_code" in table_columns(conn, table):
        return f"COALESCE(NULLIF({prefix}activity_code,''),?)"
    return "?"


def _bucket(report, code):
    if code not in report:
        report[code] = {
            "activity_code": code,
            "sales_invoice_count": 0,
            "sales_subtotal": ZERO,
            "sales_tax": ZERO,
            "sales_total": ZERO,
            "cogs_total": ZERO,
            "supplier_bill_count": 0,
            "purchase_subtotal": ZERO,
            "purchase_tax": ZERO,
            "purchase_total": ZERO,
            "receipt_count": 0,
            "stock_receipt_value": ZERO,
            "stock_issue_value": ZERO,
            "journal_entry_count": 0,
            "gl_debit": ZERO,
            "gl_credit": ZERO,
            "gl_difference": ZERO,
            "unbalanced_journal_count": 0,
        }
    return report[code]


def build_activity_performance_report(conn, activity_code=None):
    """Summarize posted sales, purchases, stock and linked GL entries per activity.

    The report is deliberately read-only. Journal entries are de-duplicated when
    a document and its stock movements point to the same entry.
    """
    legacy = _legacy_activity(conn)
    report = {}
    journal_activity = {}
    journal_kinds = defaultdict(set)

    def register_journal(raw_id, code, kind):
        if raw_id in (None, ""):
            return
        entry_id = int(raw_id)
        previous = journal_activity.get(entry_id)
        if previous is not None and previous != code:
            raise ValueError(
                f"القيد رقم {entry_id} مرتبط بأكثر من نشاط؛ أوقف التقرير وراجع ربط المستندات."
            )
        journal_activity[entry_id] = code
        journal_kinds[(entry_id, code)].add(kind)

    if table_exists(conn, "sales_invoices"):
        rows = execute_compat(conn, """
            SELECT activity_code,subtotal,tax_amount,total_amount,
                   journal_entry_id,cogs_journal_entry_id
            FROM sales_invoices WHERE status='posted'
        """).fetchall() if "activity_code" in table_columns(conn, "sales_invoices") else execute_compat(conn, """
            SELECT NULL AS activity_code,subtotal,tax_amount,total_amount,
                   journal_entry_id,cogs_journal_entry_id
            FROM sales_invoices WHERE status='posted'
        """).fetchall()
        for row in rows:
            code = str(row["activity_code"] or legacy)
            if activity_code and code != activity_code:
                continue
            item = _bucket(report, code)
            item["sales_invoice_count"] += 1
            item["sales_subtotal"] += _decimal(row["subtotal"])
            item["sales_tax"] += _decimal(row["tax_amount"])
            item["sales_total"] += _decimal(row["total_amount"])
            register_journal(row["journal_entry_id"], code, "sales")
            register_journal(row["cogs_journal_entry_id"], code, "cogs")

    if table_exists(conn, "supplier_bills"):
        rows = execute_compat(conn, """
            SELECT activity_code,subtotal,tax_amount,total_amount,journal_entry_id
            FROM supplier_bills WHERE status='posted'
        """).fetchall() if "activity_code" in table_columns(conn, "supplier_bills") else execute_compat(conn, """
            SELECT NULL AS activity_code,subtotal,tax_amount,total_amount,journal_entry_id
            FROM supplier_bills WHERE status='posted'
        """).fetchall()
        for row in rows:
            code = str(row["activity_code"] or legacy)
            if activity_code and code != activity_code:
                continue
            item = _bucket(report, code)
            item["supplier_bill_count"] += 1
            item["purchase_subtotal"] += _decimal(row["subtotal"])
            item["purchase_tax"] += _decimal(row["tax_amount"])
            item["purchase_total"] += _decimal(row["total_amount"])
            register_journal(row["journal_entry_id"], code, "purchase")

    if table_exists(conn, "goods_receipts"):
        has_activity = "activity_code" in table_columns(conn, "goods_receipts")
        expression = "activity_code" if has_activity else "NULL AS activity_code"
        rows = execute_compat(conn, f"""
            SELECT {expression},total_value,journal_entry_id
            FROM goods_receipts WHERE status='posted'
        """).fetchall()
        for row in rows:
            code = str(row["activity_code"] or legacy)
            if activity_code and code != activity_code:
                continue
            item = _bucket(report, code)
            item["receipt_count"] += 1
            # Value is counted from the stock subledger below, not twice from
            # both the GRN header and its linked stock movement.
            register_journal(row["journal_entry_id"], code, "receipt")

    if table_exists(conn, "stock_movements"):
        has_activity = "activity_code" in table_columns(conn, "stock_movements")
        expression = "activity_code" if has_activity else "NULL AS activity_code"
        rows = execute_compat(conn, f"""
            SELECT {expression},movement_type,total_cost,journal_entry_id,accounting_status
            FROM stock_movements
            WHERE accounting_status='posted'
        """).fetchall()
        for row in rows:
            code = str(row["activity_code"] or legacy)
            if activity_code and code != activity_code:
                continue
            item = _bucket(report, code)
            movement_type = str(row["movement_type"] or "")
            amount = _decimal(row["total_cost"])
            if movement_type == "receipt":
                item["stock_receipt_value"] += amount
            elif movement_type in {"issue", "scrap"}:
                item["stock_issue_value"] += amount
            register_journal(row["journal_entry_id"], code, "stock")

    entry_ids = sorted(journal_activity)
    entry_totals = defaultdict(lambda: [ZERO, ZERO])
    if entry_ids and table_exists(conn, "journal_lines"):
        # Stay below PostgreSQL/SQLite parameter limits and aggregate with Decimal.
        for offset in range(0, len(entry_ids), 400):
            batch = entry_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = execute_compat(
                conn,
                f"SELECT entry_id,debit,credit FROM journal_lines WHERE entry_id IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                entry_id = int(row["entry_id"])
                entry_totals[entry_id][0] += _decimal(row["debit"])
                entry_totals[entry_id][1] += _decimal(row["credit"])

    for entry_id, code in journal_activity.items():
        item = _bucket(report, code)
        item["journal_entry_count"] += 1
        debit, credit = entry_totals[entry_id]
        item["gl_debit"] += debit
        item["gl_credit"] += credit
        item["gl_difference"] += debit - credit
        if debit != credit:
            item["unbalanced_journal_count"] += 1
        if "cogs" in journal_kinds[(entry_id, code)]:
            item["cogs_total"] += debit

    for item in report.values():
        for key, value in list(item.items()):
            if isinstance(value, Decimal):
                item[key] = value.quantize(Decimal("0.01"))
    return {
        "legacy_activity": legacy,
        "activities": [report[key] for key in sorted(report)],
        "activity_count": len(report),
        "journal_entry_count": len(journal_activity),
        "unbalanced_journal_count": sum(
            item["unbalanced_journal_count"] for item in report.values()
        ),
    }
