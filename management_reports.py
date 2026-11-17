"""Read-only management reports: period comparison, inventory ageing and cash forecast."""
from datetime import date, timedelta
from decimal import Decimal

from income_statement import build_income_statement, comparison_period
from cash_commitments import list_cash_commitments_forecast
from db_compat import table_columns, table_exists
from db_sql import execute_compat
from receivables import (
    _customer_aging_documents, _customer_aging_movements,
    _supplier_aging_documents, _supplier_aging_movements,
)


def _exists(conn, table):
    """Use shared metadata inspection for both supported database drivers."""
    return table_exists(conn, table)


def _cols(conn, table):
    """Return validated column metadata for the selected database schema."""
    return table_columns(conn, table)


def _d(value):
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")

def _first_value(row):
    """Read a one-column aggregate from SQLite rows or PostgreSQL mappings."""
    if row is None:
        return "0"
    if hasattr(row, "values"):
        return next(iter(row.values()), "0")
    return row[0]


def build_period_comparison(conn, date_from, date_to, mode="previous_period"):
    if mode not in {"previous_period", "previous_year"}:
        mode = "previous_period"
    previous_dates = comparison_period(date_from, date_to, mode)
    current = build_income_statement(conn, date_from, date_to)
    previous = build_income_statement(conn, *previous_dates)
    keys = (
        ("revenue_total", "الإيرادات"),
        ("cogs_total", "تكلفة المبيعات"),
        ("gross_profit", "مجمل الربح"),
        ("operating_expense_total", "المصروفات التشغيلية"),
        ("net_profit", "صافي الربح"),
    )
    rows = []
    for key, label in keys:
        current_value = _d(current.get(key))
        previous_value = _d(previous.get(key))
        variance = current_value - previous_value
        rows.append({
            "key": key, "label": label, "current": current_value,
            "previous": previous_value, "variance": variance,
            "variance_percent": (variance / abs(previous_value) * Decimal("100"))
                if previous_value else None,
        })
    return {
        "rows": rows, "current": current, "previous": previous,
        "previous_date_from": previous_dates[0], "previous_date_to": previous_dates[1],
        "date_from": date_from, "date_to": date_to, "mode": mode,
    }


def build_inventory_ageing(conn, as_of):
    """Age remaining valuation layers by their receipt-layer creation date."""
    required = {"stock_valuation_layers", "stock_products", "stock_warehouses"}
    if not all(_exists(conn, table) for table in required):
        return {"available": False, "rows": [], "totals": {}, "as_of": as_of}
    layer_cols = _cols(conn, "stock_valuation_layers")
    product_cols = _cols(conn, "stock_products")
    if not {"product_id", "warehouse_id", "qty_remaining", "unit_cost"}.issubset(layer_cols):
        return {"available": False, "rows": [], "totals": {}, "as_of": as_of}
    created_expr = "substr(l.created_at,1,10)" if "created_at" in layer_cols else (
        "substr(m.movement_date,1,10)" if _exists(conn, "stock_movements") else "?"
    )
    join_movement = "LEFT JOIN stock_movements m ON m.id=l.source_movement_id" if _exists(conn, "stock_movements") else ""
    lot_join = "LEFT JOIN stock_lots lot ON lot.id=l.lot_id" if _exists(conn, "stock_lots") else ""
    expiry_expr = "lot.expiry_date" if _exists(conn, "stock_lots") else "NULL"
    sku_expr = "p.sku" if "sku" in product_cols else "CAST(p.id AS TEXT)"
    name_expr = "p.name_ar" if "name_ar" in product_cols else "CAST(p.id AS TEXT)"
    has_movements = _exists(conn, "stock_movements")
    last_movement_expr = (
        """(SELECT MAX(mv.movement_date) FROM stock_movements mv
             WHERE mv.product_id=l.product_id
               AND (mv.source_warehouse_id=l.warehouse_id
                    OR mv.target_warehouse_id=l.warehouse_id)
               AND substr(mv.movement_date,1,10)<=?)"""
        if has_movements else "NULL"
    )
    layer_date_filter = f"{created_expr}<=?"
    params = []
    if created_expr == "?":
        params.append(as_of)
    params.append(as_of)
    if has_movements:
        params.append(as_of)
    rows = execute_compat(conn, 
        f"""SELECT l.product_id,l.warehouse_id,{sku_expr} sku,{name_expr} product_name,
                   w.code warehouse_code,w.name_ar warehouse_name,
                   {created_expr} layer_date,{expiry_expr} expiry_date,
                   {last_movement_expr} last_movement_date,
                   l.qty_remaining,l.unit_cost,l.lot_id
            FROM stock_valuation_layers l
            JOIN stock_products p ON p.id=l.product_id
            JOIN stock_warehouses w ON w.id=l.warehouse_id
            {join_movement} {lot_join}
            WHERE CAST(l.qty_remaining AS NUMERIC)>0
              AND {layer_date_filter}
            ORDER BY layer_date,product_name""",
        tuple(params),
    ).fetchall()
    cutoff = date.fromisoformat(as_of)
    buckets = {"0_30": Decimal("0"), "31_60": Decimal("0"),
               "61_90": Decimal("0"), "over_90": Decimal("0")}
    expiry_totals = {"expired": Decimal("0"), "within_30": Decimal("0"),
                     "within_90": Decimal("0"), "later": Decimal("0"),
                     "no_expiry": Decimal("0")}
    result = []
    for row in rows:
        try:
            layer_date = date.fromisoformat(str(row["layer_date"])[:10])
        except (TypeError, ValueError):
            continue
        age = max((cutoff - layer_date).days, 0)
        qty = _d(row["qty_remaining"])
        value = qty * _d(row["unit_cost"])
        bucket = "0_30" if age <= 30 else "31_60" if age <= 60 else "61_90" if age <= 90 else "over_90"
        buckets[bucket] += value
        expiry_status = "no_expiry"
        expiry_days = None
        if row["expiry_date"]:
            try:
                expiry_day = date.fromisoformat(str(row["expiry_date"])[:10])
                expiry_days = (expiry_day - cutoff).days
                if expiry_days < 0:
                    expiry_status = "expired"
                elif expiry_days <= 30:
                    expiry_status = "within_30"
                elif expiry_days <= 90:
                    expiry_status = "within_90"
                else:
                    expiry_status = "later"
            except (TypeError, ValueError):
                expiry_status = "unknown"
        if expiry_status in expiry_totals:
            expiry_totals[expiry_status] += value
        last_movement_age = None
        if row["last_movement_date"]:
            try:
                last_movement_age = max(
                    (cutoff - date.fromisoformat(str(row["last_movement_date"])[:10])).days, 0
                )
            except (TypeError, ValueError):
                last_movement_age = None
        result.append({
            "product_id": row["product_id"], "warehouse_id": row["warehouse_id"],
            "sku": row["sku"], "product_name": row["product_name"],
            "warehouse": f'{row["warehouse_code"]} - {row["warehouse_name"]}',
            "layer_date": row["layer_date"], "age_days": age, "quantity": qty,
            "unit_cost": _d(row["unit_cost"]), "value": value,
            "expiry_date": row["expiry_date"],
            "expiry_status": expiry_status,
            "expiry_days": expiry_days,
            "last_movement_date": row["last_movement_date"],
            "last_movement_age_days": last_movement_age,
            "slow_moving": (
                (last_movement_age > 90) if last_movement_age is not None
                else (age > 90 if has_movements else None)
            ),
            "slow_moving_status": (
                "راكد: لا توجد حركة لأكثر من 90 يومًا" if last_movement_age is not None and last_movement_age > 90
                else "له حركة خلال آخر 90 يومًا" if last_movement_age is not None
                else "لا توجد حركة مسجلة" if has_movements and age > 90
                else "لا توجد بيانات حركات كافية للتصنيف" if not has_movements
                else "لا توجد حركة مسجلة حديثًا"
            ),
            "bucket": bucket,
        })
    return {"available": True, "rows": result, "totals": buckets,
            "expiry_totals": expiry_totals,
            "total_value": sum(buckets.values(), Decimal("0")), "as_of": as_of}


def _open_document_balances(conn, kind, as_of):
    if kind == "customers":
        docs = _customer_aging_documents(conn, as_of)
        has_detail, paid, paid_all, credited, credited_all, refunds = _customer_aging_movements(conn, as_of)
        has_credits = _exists(conn, "sales_credit_notes")
    else:
        docs = _supplier_aging_documents(conn, as_of)
        has_detail, paid, paid_all, credited, credited_all = _supplier_aging_movements(conn, as_of)
        refunds = {}
        has_credits = _exists(conn, "supplier_credit_notes")
    result = []
    for row in docs:
        doc_id = row["invoice_id"]
        stored_paid = _d(row["paid_amount"])
        paid_amount = (
            paid.get(doc_id, Decimal("0")) +
            max(Decimal("0"), stored_paid - paid_all.get(doc_id, Decimal("0")))
        ) if has_detail else stored_paid
        stored_credit = _d(row["credited_amount"] if "credited_amount" in row.keys() else "0")
        credit_amount = (
            credited.get(doc_id, Decimal("0")) +
            max(Decimal("0"), stored_credit - credited_all.get(doc_id, Decimal("0")))
        ) if has_credits else stored_credit
        balance = _d(row["total_amount"]) - paid_amount - credit_amount + refunds.get(doc_id, Decimal("0"))
        if balance <= 0:
            continue
        try:
            due = date.fromisoformat(row["due_date"])
        except (TypeError, ValueError):
            due = date.fromisoformat(row["doc_date"])
        days = (due - date.fromisoformat(as_of)).days
        result.append({
            "kind": kind, "party": row["party_name"], "document": row["doc_no"],
            "due_date": due.isoformat(), "balance": balance,
            "days_until_due": days, "status": "متأخر" if days < 0 else "مستحق" if days == 0 else "متوقع",
        })
    return result


def _month_end(period):
    """Return the last calendar day for a YYYY-MM payroll period."""
    try:
        first = date.fromisoformat(str(period)[:7] + "-01")
        next_month = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
        return (next_month - timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return None


def _cash_forecast_payroll(conn, as_of):
    items = []
    if not all(_exists(conn, table) for table in ("payroll_runs", "payroll_lines", "payroll_payments")):
        return items
    for run in execute_compat(conn, 
        """SELECT id,period FROM payroll_runs
           WHERE status='posted' AND journal_entry_id IS NOT NULL ORDER BY period,id"""
    ).fetchall():
        due_date = _month_end(run["period"])
        if not due_date:
            continue
        net_payable = _d(_first_value(execute_compat(conn,
            "SELECT COALESCE(SUM(CAST(net_pay AS NUMERIC)),0) AS total FROM payroll_lines WHERE run_id=?",
            (run["id"],),
        ).fetchone()))
        paid = _d(_first_value(execute_compat(conn,
            """SELECT COALESCE(SUM(CAST(amount AS NUMERIC)),0) AS total FROM payroll_payments
               WHERE run_id=? AND payment_date<=?""", (run["id"], as_of)
        ).fetchone()))
        balance = max(net_payable - paid, Decimal("0"))
        if balance:
            items.append({
                "kind": "payroll", "party": "الموظفون", "document": "مرتبات " + str(run["period"]),
                "due_date": due_date, "balance": balance,
                "status": "متأخر" if due_date < as_of else "مستحق/متوقع",
                "date_basis": "نهاية شهر المرتبات (تاريخ تقديري)",
            })
    return items


def _cash_forecast_grants(conn, as_of):
    items = []
    required = {"employee_grants", "employee_grant_lines", "employee_grant_payments"}
    if not all(_exists(conn, table) for table in required):
        return items
    grants = execute_compat(conn, 
        """SELECT id,grant_name_snapshot,grant_date FROM employee_grants
           WHERE status IN ('approved','partially_paid') ORDER BY grant_date,id"""
    ).fetchall()
    for grant in grants:
        total = _d(_first_value(execute_compat(conn,
            "SELECT COALESCE(SUM(CAST(amount AS NUMERIC)),0) AS total FROM employee_grant_lines WHERE grant_id=?",
            (grant["id"],),
        ).fetchone()))
        paid = _d(_first_value(execute_compat(conn,
            """SELECT COALESCE(SUM(CAST(amount AS NUMERIC)),0) AS total FROM employee_grant_payments
               WHERE grant_id=? AND payment_date<=?""", (grant["id"], as_of)
        ).fetchone()))
        balance = max(total - paid, Decimal("0"))
        if balance:
            due_date = str(grant["grant_date"] or as_of)[:10]
            items.append({
                "kind": "employee_grants", "party": "الموظفون",
                "document": str(grant["grant_name_snapshot"] or "منحة موظفين"),
                "due_date": due_date, "balance": balance,
                "status": "متأخر" if due_date < as_of else "مستحق/متوقع",
                "date_basis": "تاريخ المنحة المسجل",
            })
    return items


def build_cash_forecast(conn, as_of, horizon_days=90):
    """Forecast documented receivables/payables and recorded payroll/grants."""
    horizon_days = max(1, min(int(horizon_days), 365))
    end = (date.fromisoformat(as_of) + timedelta(days=horizon_days)).isoformat()
    items, errors = [], []
    for kind in ("customers", "suppliers"):
        try:
            items.extend(_open_document_balances(conn, kind, as_of))
        except Exception:
            errors.append("تعذر احتساب " + ("أرصدة العملاء" if kind == "customers" else "أرصدة الموردين") + " بسبب نقص/اختلاف بيانات الاستحقاق.")
    try:
        items.extend(_cash_forecast_payroll(conn, as_of))
        items.extend(_cash_forecast_grants(conn, as_of))
        items.extend(list_cash_commitments_forecast(conn, as_of, end))
    except Exception:
        errors.append("تعذر احتساب التزامات المرتبات أو المنح بسبب نقص/اختلاف بيانات الموارد البشرية.")
    visible = [item for item in items if "error" not in item and item["due_date"] <= end]
    daily = {}
    for item in visible:
        # Overdue balances are still outstanding today. Keep the document's
        # original due_date in the detail list, but place its forecast impact
        # on the as-of date instead of a historical day in the daily timeline.
        forecast_date = max(item["due_date"], as_of)
        day = daily.setdefault(forecast_date, {
            "collections": Decimal("0"), "supplier_payments": Decimal("0"),
            "payroll": Decimal("0"), "employee_grants": Decimal("0"),
            "other_commitments": Decimal("0"),
            "payments": Decimal("0"),
        })
        if item["kind"] == "customers":
            day["collections"] += item["balance"]
        else:
            category = {
                "suppliers": "supplier_payments",
                "payroll": "payroll",
                "employee_grants": "employee_grants",
                "cash_commitment": "other_commitments",
            }.get(item["kind"], "supplier_payments")
            day[category] += item["balance"]
            day["payments"] += item["balance"]
    days = [{"date": day, **values} for day, values in sorted(daily.items())]
    collections = sum((item["balance"] for item in visible if item["kind"] == "customers"), Decimal("0"))
    payments = sum((item["balance"] for item in visible if item["kind"] != "customers"), Decimal("0"))
    return {
        "as_of": as_of, "horizon_days": horizon_days, "end_date": end,
        "items": visible, "errors": errors,
        "daily": days, "collections": collections, "payments": payments,
        "net_expected": collections - payments,
        "coverage_notes": [
            "المرتبات: صافي المرتبات المرحلة بعد خصم دفعات الصرف المسجلة حتى تاريخ التقرير؛ تاريخ الاستحقاق نهاية شهر المرتبات كتقدير.",
            "المنح: المنح المعتمدة فقط، بعد خصم دفعاتها المسجلة حتى تاريخ التقرير.",
            "الضرائب: مركز الضرائب يعرض ملخص ضريبة القيمة المضافة ومطابقتها مع الأستاذ العام، لكنه لا يحتفظ حاليًا بسجل مستقل لاستحقاق الضريبة أو سدادها؛ سجّل الالتزام يدويًا بعد مراجعة التقرير.",
            "المصروفات الدورية والالتزامات اليدوية تُعرض بعد خصم الدفعات المرتبطة بقيود مرحلة حتى تاريخ التقرير. بدل الجمعة يُنشأ التزامه عند ترحيل الكشف، ويمكن استيراد الكشوف القديمة غير المرتبطة.",
        ],
    }
