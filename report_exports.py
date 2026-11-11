"""Excel exports for NEXUS ERP reports. PDF output uses the browser's print-to-PDF engine for Arabic support."""
import io
import sqlite3
from decimal import Decimal
from datetime import datetime
from accounting_core import register_decimal_aggregates
from functools import wraps

from flask import Blueprint, Response, abort, request, send_file, session


def register_report_exports(app, db_path, permissions):
    bp = Blueprint("report_exports", __name__)

    def require(permission):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                role = session.get("role")
                if not session.get("user_id"):
                    abort(401)
                if permission not in set(session.get("permissions", permissions.get(role, set()))):
                    abort(403)
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        register_decimal_aggregates(conn)
        return conn

    @bp.get("/reports/export/<report>/excel")
    @require("reports.view")
    def export_report_excel(report):
        conn = connect()
        try:
            if report == "accounts":
                rows = conn.execute("""
                    SELECT a.code, a.name_ar, a.account_type, a.parent_id, p.code parent_code,
                           p.name_ar parent_name, a.is_group, a.active
                    FROM accounts a LEFT JOIN accounts p ON p.id=a.parent_id
                    ORDER BY a.code
                """).fetchall()
                headers = ["كود الحساب", "اسم الحساب", "نوع الحساب", "كود الحساب الأب",
                           "اسم الحساب الأب", "التصنيف", "الحالة"]
                data = [[r["code"], r["name_ar"], r["account_type"], r["parent_code"] or "",
                         r["parent_name"] or "", "رئيسي / مجموعة" if r["is_group"] else "فرعي / تفصيلي",
                         "نشط" if r["active"] else "موقوف"] for r in rows]
                filename = "nexus-chart-of-accounts.xlsx"
                title = "دليل الحسابات"
            elif report == "trial-balance":
                date_from = request.args.get("date_from", "").strip()
                date_to = request.args.get("date_to", "").strip()
                account_type = request.args.get("account_type", "").strip()
                q = request.args.get("q", "").strip()
                conditions, params = ["1=1"], []
                if date_from:
                    conditions.append("je.entry_date >= ?"); params.append(date_from)
                if date_to:
                    conditions.append("je.entry_date <= ?"); params.append(date_to)
                if account_type in {"asset", "liability", "equity", "revenue", "expense"}:
                    conditions.append("a.account_type = ?"); params.append(account_type)
                if q:
                    conditions.append("(a.code LIKE ? OR a.name_ar LIKE ?)")
                    params.extend([f"%{q}%", f"%{q}%"])
                rows = conn.execute(f"""
                    SELECT a.code,a.name_ar,a.account_type,
                           COALESCE(DECIMAL_SUM(CASE WHEN je.status='posted' THEN jl.debit ELSE '0' END),0) debit,
                           COALESCE(DECIMAL_SUM(CASE WHEN je.status='posted' THEN jl.credit ELSE '0' END),0) credit
                    FROM accounts a
                    LEFT JOIN journal_lines jl ON jl.account_id=a.id
                    LEFT JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE {' AND '.join(conditions)}
                    GROUP BY a.id ORDER BY a.code
                """, params).fetchall()
                headers = ["كود الحساب", "اسم الحساب", "نوع الحساب", "مدين", "دائن", "صافي الرصيد"]
                data = [[r["code"], r["name_ar"], r["account_type"],
                         Decimal(str(r["debit"] or "0")), Decimal(str(r["credit"] or "0")),
                         Decimal(str(r["debit"] or "0")) - Decimal(str(r["credit"] or "0"))] for r in rows]
                filename = "nexus-trial-balance.xlsx"
                title = "ميزان المراجعة"
            elif report == "ledger":
                account_id = request.args.get("account_id", type=int)
                date_from = request.args.get("date_from", "").strip()
                date_to = request.args.get("date_to", "").strip()
                if not account_id:
                    abort(400)
                account = conn.execute("SELECT code,name_ar FROM accounts WHERE id=? AND is_group=0",
                                       (account_id,)).fetchone()
                if not account:
                    abort(404)
                conditions = ["jl.account_id=?", "je.status='posted'"]
                params = [account_id]
                if date_from:
                    conditions.append("je.entry_date >= ?"); params.append(date_from)
                if date_to:
                    conditions.append("je.entry_date <= ?"); params.append(date_to)
                rows = conn.execute(f"""
                    SELECT je.entry_date,je.reference,je.description,jl.debit,jl.credit,jl.memo
                    FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE {' AND '.join(conditions)} ORDER BY je.entry_date,jl.id
                """, params).fetchall()
                headers = ["التاريخ", "المرجع", "البيان", "مدين", "دائن", "ملاحظة"]
                data = [[r["entry_date"], r["reference"], r["description"],
                         Decimal(str(r["debit"] or "0")), Decimal(str(r["credit"] or "0")), r["memo"] or ""] for r in rows]
                filename = f"nexus-ledger-{account['code']}.xlsx"
                title = f"دفتر الأستاذ - {account['code']} {account['name_ar']}"
            elif report == "stock":
                rows = conn.execute("""
                    SELECT p.sku,p.name_ar,p.uom,w.code warehouse_code,w.name_ar warehouse_name,
                           COALESCE(SUM(CAST(l.qty_remaining AS REAL)),0) qty,
                           COALESCE(DECIMAL_LAYER_VALUE_SUM(l.value_remaining,l.qty_remaining,l.unit_cost),0) value
                    FROM stock_products p LEFT JOIN stock_valuation_layers l ON l.product_id=p.id
                    LEFT JOIN stock_warehouses w ON w.id=l.warehouse_id
                    WHERE p.active=1 GROUP BY p.id,w.id ORDER BY p.sku,w.code
                """).fetchall()
                headers = ["كود الصنف", "اسم الصنف", "وحدة القياس", "كود المخزن", "اسم المخزن", "الرصيد", "قيمة المخزون"]
                data = [[r["sku"], r["name_ar"], r["uom"], r["warehouse_code"] or "", r["warehouse_name"] or "",
                         r["qty"], Decimal(str(r["value"] or "0"))] for r in rows]
                filename = "nexus-stock-valuation.xlsx"
                title = "تقييم وأرصدة المخزون"
            elif report == "fixed-assets":
                rows = conn.execute("""SELECT f.asset_code,f.asset_name,f.category,f.purchase_date,f.ready_date,
                  f.cost,f.residual_value,f.useful_life_months,f.depreciation_method,
                  COALESCE(DECIMAL_SUM(d.amount),0) accumulated
                  FROM fixed_assets f LEFT JOIN asset_depreciations d ON d.asset_id=f.id
                  GROUP BY f.id ORDER BY f.asset_code""").fetchall()
                headers = ["كود الأصل","اسم الأصل","الفئة","تاريخ الشراء","تاريخ الجاهزية",
                           "التكلفة","القيمة التخريدية","العمر بالشهور","طريقة الإهلاك","مجمع الإهلاك","صافي القيمة الدفترية"]
                data = [[r["asset_code"],r["asset_name"],r["category"],r["purchase_date"],r["ready_date"],
                         Decimal(str(r["cost"])),Decimal(str(r["residual_value"])),r["useful_life_months"],r["depreciation_method"],
                         Decimal(str(r["accumulated"] or "0")),max(Decimal(str(r["cost"]))-Decimal(str(r["accumulated"] or "0")),Decimal(str(r["residual_value"])))] for r in rows]
                filename = "nexus-fixed-assets.xlsx"
                title = "سجل الأصول الثابتة"
            elif report == "aging":
                kind = request.args.get("kind", "customers")
                as_of = request.args.get("as_of", datetime.now().date().isoformat())
                if kind == "customers":
                    invoice_rows = conn.execute("""SELECT i.id invoice_id,i.invoice_no doc_no,cu.code party_code,cu.name party_name,
                      i.invoice_date doc_date,COALESCE(i.due_date,i.invoice_date) due_date,
                      i.total_amount,i.paid_amount,COALESCE(i.credited_amount,'0') credited_amount
                      FROM sales_invoices i JOIN customers cu ON cu.id=i.customer_id
                      WHERE i.status='posted' AND i.invoice_date<=? ORDER BY due_date,cu.name""",(as_of,)).fetchall()
                    refund_rows = conn.execute("""SELECT cn.invoice_id,rf.amount FROM customer_refunds rf
                      JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id
                      WHERE cn.status='posted' AND rf.refund_date<=?""",(as_of,)).fetchall()
                    refunds_by_invoice = {}
                    for refund in refund_rows:
                        refunds_by_invoice[refund["invoice_id"]] = refunds_by_invoice.get(refund["invoice_id"],Decimal("0")) + Decimal(refund["amount"])
                    rows = []
                    for invoice in invoice_rows:
                        balance = (Decimal(invoice["total_amount"])-Decimal(invoice["paid_amount"] or "0")
                                   -Decimal(invoice["credited_amount"] or "0")
                                   +refunds_by_invoice.get(invoice["invoice_id"],Decimal("0")))
                        if balance > 0:
                            rows.append({**dict(invoice),"balance":balance})
                elif kind == "suppliers":
                    bill_rows = conn.execute("""SELECT b.bill_no doc_no,s.code party_code,s.name party_name,
                      b.bill_date doc_date,COALESCE(b.due_date,b.bill_date) due_date,
                      b.total_amount,b.paid_amount
                      FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
                      WHERE b.status='posted' AND b.bill_date<=? ORDER BY due_date,s.name""",(as_of,)).fetchall()
                    rows = []
                    for bill in bill_rows:
                        balance = Decimal(bill["total_amount"])-Decimal(bill["paid_amount"] or "0")
                        if balance > 0:
                            rows.append({**dict(bill),"balance":balance})
                else:
                    abort(400)
                from datetime import date as date_type
                report_date = date_type.fromisoformat(as_of)
                headers = ["كود الطرف","اسم الطرف","رقم المستند","تاريخ المستند","تاريخ الاستحقاق",
                           "أيام التأخير","غير مستحق","0-30","31-60","61-90","أكثر من 90"]
                data = []
                for r in rows:
                    days=(report_date-date_type.fromisoformat(r["due_date"])).days
                    buckets=[Decimal("0") for _ in range(5)]
                    buckets[0 if days<=0 else 1 if days<=30 else 2 if days<=60 else 3 if days<=90 else 4]=Decimal(r["balance"])
                    data.append([r["party_code"],r["party_name"],r["doc_no"],r["doc_date"],r["due_date"],
                                 max(days,0),*buckets])
                filename = "nexus-aging-"+kind+".xlsx"
            elif report == "reconciliation":
                from accounting_core import reconciliation_snapshot
                snapshot = reconciliation_snapshot(conn)
                headers = ["القسم", "البند", "القيمة"]
                data = [
                    ["القيود", "إجمالي المدين", snapshot["journal_totals"]["debit"]],
                    ["القيود", "إجمالي الدائن", snapshot["journal_totals"]["credit"]],
                    ["القيود", "عدد القيود غير المتوازنة", len(snapshot["unbalanced"])],
                    ["المخزون", "قيمة طبقات المخزون", snapshot["stock_value"]],
                    ["المخزون", "رصيد حسابات المخزون بالأستاذ", snapshot["gl_stock"] if snapshot["gl_stock"] is not None else "غير محدد"],
                    ["المخزون", "فرق المطابقة", snapshot["stock_difference"] if snapshot["stock_difference"] is not None else "غير محدد"],
                    ["المشتريات", "بضاعة مستلمة غير مفوترة GRNI", snapshot["grni"]],
                    ["الموردون", "صافي الذمم المفتوحة", snapshot["open_ap"]],
                    ["الترابط", "عدد مجموعات المستندات غير المرتبطة", len(snapshot["unlinked_documents"])],
                    ["المخزون", "عدد الحركات المحاسبية المعلقة", snapshot["pending_stock"]],
                ]
                filename = "nexus-reconciliation.xlsx"
                title = "تقرير المطابقة والترابط"
            else:
                abort(404)
        finally:
            conn.close()

        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            return Response("مكتبة openpyxl غير مثبتة. نفّذ: python -m pip install -r requirements.txt",
                            status=500, mimetype="text/plain; charset=utf-8")

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "NEXUS ERP"
        sheet.sheet_view.rightToLeft = True
        sheet.append([title])
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        sheet["A1"].font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
        sheet["A1"].fill = PatternFill("solid", fgColor="17365D")
        sheet["A1"].alignment = Alignment(horizontal="center")
        sheet.append(headers)
        for cell in sheet[2]:
            cell.font = Font(name="Arial", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="167D8D")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in data:
            sheet.append(row)
        for row in sheet.iter_rows(min_row=3):
            for cell in row:
                cell.font = Font(name="Arial", size=11)
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if cell.column >= 4 and isinstance(cell.value, (int, float)):
                    cell.number_format = '#,##0.00;[Red]-#,##0.00'
        for column in sheet.columns:
            letter = get_column_letter(column[0].column)
            width = min(max(max(len(str(c.value or "")) for c in column) + 3, 14), 42)
            sheet.column_dimensions[letter].width = width
        sheet.freeze_panes = "A3"
        sheet.auto_filter.ref = sheet.dimensions
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=filename,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    app.register_blueprint(bp)
