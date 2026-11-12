from database import connect_database
"""Excel exports for NEXUS ERP reports. PDF output uses the browser's print-to-PDF engine for Arabic support."""
import io
import re
from decimal import Decimal
from datetime import datetime
from functools import wraps

from flask import Blueprint, Response, abort, request, send_file, session
from income_statement import build_income_statement, normalize_income_statement_period


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
        return connect_database(db_path)

    @bp.get("/reports/export/<report>/excel")
    @bp.get("/reports/export/<report>/pdf")
    @require("reports.view")
    def export_report_excel(report):
        conn = connect()
        company_row = conn.execute(
            "SELECT setting_value FROM company_settings WHERE setting_key='company_name'"
        ).fetchone()
        company_name = company_row["setting_value"] if company_row else "NEXUS ERP"
        currency_row = conn.execute(
            "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'"
        ).fetchone()
        base_currency = currency_row["setting_value"] if currency_row else ""
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
                if date_from and date_to and date_from > date_to:
                    abort(400)
                account_conditions, account_params = ["1=1"], []
                if account_type in {"asset", "liability", "equity", "revenue", "expense"}:
                    account_conditions.append("a.account_type = ?"); account_params.append(account_type)
                if q:
                    account_conditions.append("(a.code LIKE ? OR a.name_ar LIKE ?)")
                    account_params.extend([f"%{q}%", f"%{q}%"])
                years = conn.execute(
                    "SELECT id,year_name,start_date,end_date,status FROM financial_years ORDER BY start_date DESC,id DESC"
                ).fetchall()
                periods = []
                today = datetime.now().date().isoformat()
                for year in years:
                    period_from = max(year["start_date"], date_from) if date_from else year["start_date"]
                    natural_end = min(year["end_date"], today) if year["status"] == "open" else year["end_date"]
                    period_to = min(natural_end, date_to) if date_to else natural_end
                    if period_from <= period_to:
                        periods.append((year["year_name"], "الفترة الحالية / مفتوحة" if year["status"] == "open" else "فترة مالية مقفولة", period_from, period_to))
                if not years:
                    periods.append(("الفترة المحددة", "الفترة الحالية", date_from, date_to))
                headers = ["الفترة", "التصنيف", "من تاريخ", "إلى تاريخ", "كود الحساب", "اسم الحساب", "نوع الحساب", "مدين", "دائن", "صافي الرصيد"]
                data = []
                type_labels = {"asset": "أصول", "liability": "التزامات", "equity": "حقوق ملكية", "revenue": "إيرادات", "expense": "مصروفات"}
                for period_name, period_label, period_from, period_to in periods:
                    join_conditions = ["je.id=jl.entry_id", "je.status='posted'"]
                    join_params = []
                    if period_from:
                        join_conditions.append("je.entry_date >= ?"); join_params.append(period_from)
                    if period_to:
                        join_conditions.append("je.entry_date <= ?"); join_params.append(period_to)
                    rows = conn.execute(f"""
                        SELECT a.code,a.name_ar,a.account_type,
                               COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),0) debit,
                               COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),0) credit
                        FROM accounts a
                        LEFT JOIN journal_lines jl ON jl.account_id=a.id
                        LEFT JOIN journal_entries je ON {' AND '.join(join_conditions)}
                        WHERE {' AND '.join(account_conditions)}
                        GROUP BY a.id ORDER BY a.code
                    """, [*join_params, *account_params]).fetchall()
                    total_debit = Decimal("0")
                    total_credit = Decimal("0")
                    data.append([period_name, period_label, period_from or "", period_to or "", "", "", "", "", "", ""])
                    for row in rows:
                        debit = Decimal(str(row["debit"] or "0"))
                        credit = Decimal(str(row["credit"] or "0"))
                        total_debit += debit
                        total_credit += credit
                        data.append([period_name, period_label, period_from or "", period_to or "",
                                     row["code"], row["name_ar"], type_labels.get(row["account_type"], row["account_type"]),
                                     debit, credit, debit - credit])
                    data.append([period_name, "إجمالي الفترة", "", "", "", "", "", total_debit, total_credit, total_debit-total_credit])
                    data.append([""] * len(headers))
                filename = "nexus-trial-balance.xlsx"
                title = "ميزان المراجعة حسب الفترات المالية"
            elif report == "income-statement":
                date_from, date_to = normalize_income_statement_period(
                    request.args.get("date_from"), request.args.get("date_to")
                )
                statement = build_income_statement(conn, date_from, date_to)
                headers = ["القسم", "كود الحساب", "اسم الحساب", "مدين", "دائن", "صافي الرصيد"]
                data = []
                for label, rows in (
                    ("الإيرادات", statement["revenue_rows"]),
                    ("تكلفة البضاعة المباعة / تكلفة النشاط", statement["cogs_rows"]),
                    ("المصروفات التشغيلية والإدارية", statement["operating_expense_rows"]),
                ):
                    for row in rows:
                        data.append([label, row["code"], row["name_ar"],
                                     row["debit"], row["credit"], row["balance"]])
                data.extend([
                    ["الإجمالي", "", "إجمالي الإيرادات", "", "", statement["revenue_total"]],
                    ["الإجمالي", "", "إجمالي تكلفة النشاط", "", "", statement["cogs_total"]],
                    ["النتيجة", "", "مجمل الربح / (الخسارة)", "", "", statement["gross_profit"]],
                    ["الإجمالي", "", "المصروفات التشغيلية والإدارية", "", "", statement["operating_expense_total"]],
                    ["النتيجة", "", "صافي الربح / (الخسارة)", "", "", statement["net_profit"]],
                ])
                filename = "nexus-income-statement.xlsx"
                title = f"قائمة الدخل من {date_from} إلى {date_to}"
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
            elif report == "payroll":
                run_id = request.args.get("run_id", type=int)
                if not run_id:
                    abort(400)
                run = conn.execute("SELECT period,status FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
                if not run:
                    abort(404)
                rows = conn.execute("""SELECT e.employee_code,e.full_name,l.basic_salary,l.salary_raise,l.allowances,
                    l.incentives,l.incentive_name,l.incentive_method,l.incentive_days,
                    l.overtime_hours,l.overtime_rate_percent,l.overtime_amount,
                    l.employer_insurance,l.employee_insurance,l.taxable_income_tax,
                    l.advance_deduction,l.other_deductions,l.absence_deduction,
                    l.total_entitlements,l.total_deductions,l.net_pay
                    FROM payroll_lines l JOIN employees e ON e.id=l.employee_id
                    WHERE l.run_id=? ORDER BY e.employee_code""", (run_id,)).fetchall()
                headers = ["كود الموظف","اسم الموظف","الأساسي بعد العلاوة","قيمة العلاوة","البدلات",
                    "قيمة الحافز","اسم الحافز","طريقة حساب الحافز","أيام الحافز",
                    "ساعات الإضافي","نسبة الإضافي %","قيمة الإضافي","حصة صاحب العمل",
                    "حصة العامل","ضريبة المرتبات","السلف","خصومات أخرى","خصم الغياب",
                    "إجمالي الاستحقاق","إجمالي الاستقطاع","صافي المستحق"]
                data = [[
                    r["employee_code"], r["full_name"],
                    *[Decimal(str(r[k] or "0")) for k in ("basic_salary","salary_raise","allowances","incentives")],
                    r["incentive_name"], r["incentive_method"],
                    *[Decimal(str(r[k] or "0")) for k in
                      ("incentive_days","overtime_hours","overtime_rate_percent","overtime_amount",
                       "employer_insurance","employee_insurance","taxable_income_tax","advance_deduction",
                       "other_deductions","absence_deduction","total_entitlements","total_deductions","net_pay")]
                ] for r in rows]
                filename = "nexus-payroll-" + run["period"] + ".xlsx"
                title = "مسير الرواتب - " + run["period"]
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

        if request.path.endswith("/pdf"):
            try:
                import os
                import arabic_reshaper
                from bidi.algorithm import get_display
                from reportlab.lib import colors
                from reportlab.lib.enums import TA_RIGHT, TA_CENTER
                from reportlab.lib.pagesizes import A4, landscape
                from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
                from reportlab.lib.units import mm
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
                from xml.sax.saxutils import escape
            except ImportError:
                return Response(
                    "مكتبات PDF غير مثبتة. نفّذ: python -m pip install -r requirements.txt",
                    status=500, mimetype="text/plain; charset=utf-8"
                )
            font_candidates = [
                os.path.join(os.path.dirname(__file__), "static", "fonts", "DejaVuSans.ttf"),
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/tahoma.ttf",
            ]
            font_path = next((path for path in font_candidates if os.path.isfile(path)), None)
            if not font_path:
                return Response(
                    "تعذر إنشاء PDF: لم يتم العثور على خط يدعم العربية. أضف DejaVuSans.ttf إلى static/fonts.",
                    status=503, mimetype="text/plain; charset=utf-8"
                )
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            if "NexusArabic" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("NexusArabic", font_path))

            def pdf_text(value):
                text_value = "" if value is None else str(value)
                shaped = get_display(arabic_reshaper.reshape(text_value))
                return escape(shaped).replace("\n", "<br/>")

            page_size = landscape(A4) if len(headers) > 6 else A4
            output = io.BytesIO()
            document = SimpleDocTemplate(
                output, pagesize=page_size, rightMargin=12*mm, leftMargin=12*mm,
                topMargin=14*mm, bottomMargin=14*mm, title=str(locals().get("title", report)),
                author=company_name,
            )
            styles = getSampleStyleSheet()
            heading_style = ParagraphStyle(
                "NexusHeading", parent=styles["Title"], fontName="NexusArabic",
                fontSize=15, leading=21, alignment=TA_CENTER, textColor=colors.HexColor("#17365D"),
            )
            cell_style = ParagraphStyle(
                "NexusCell", parent=styles["BodyText"], fontName="NexusArabic",
                fontSize=7.5, leading=10, alignment=TA_RIGHT,
            )
            header_style = ParagraphStyle(
                "NexusHeader", parent=cell_style, fontSize=8, leading=11,
                textColor=colors.white, alignment=TA_CENTER,
            )
            story = [
                Paragraph(pdf_text(company_name), heading_style),
                Paragraph(pdf_text(locals().get("title", report)), heading_style),
                Paragraph(pdf_text("العملة الأساسية: " + base_currency), cell_style),
                Paragraph(pdf_text("تاريخ إعداد التقرير: " + datetime.now().strftime("%Y-%m-%d %H:%M")), cell_style),
                Spacer(1, 5*mm),
            ]
            table_data = [[Paragraph(pdf_text(value), header_style) for value in reversed(headers)]]
            for row in data:
                table_data.append([
                    Paragraph(pdf_text(value), cell_style) if isinstance(value, str) else
                    Paragraph(pdf_text(f"{value:,.2f}" if isinstance(value, (int, float, Decimal)) else value), cell_style)
                    for value in reversed(row)
                ])
            widths = [(page_size[0] - 24*mm) / max(1, len(headers))] * len(headers)
            table = Table(table_data, colWidths=widths, repeatRows=1, hAlign="RIGHT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#167D8D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B7C3CC")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F8")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(table)
            document.build(story)
            output.seek(0)
            return send_file(output, as_attachment=True,
                download_name=re.sub(r"\\.xlsx$", ".pdf", filename),
                mimetype="application/pdf")

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
        sheet.append([company_name])
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        sheet["A1"].font = Font(name="Arial", size=12, bold=True, color="FFFFFF")
        sheet["A1"].fill = PatternFill("solid", fgColor="17365D")
        sheet["A1"].alignment = Alignment(horizontal="center")
        sheet.append([title])
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
        sheet["A2"].font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
        sheet["A2"].fill = PatternFill("solid", fgColor="17365D")
        sheet["A2"].alignment = Alignment(horizontal="center")
        sheet.append(["تاريخ إعداد التقرير: " + datetime.now().strftime("%Y-%m-%d %H:%M")])
        sheet.merge_cells(start_row=3, start_column=1, end_row=3, end_column=len(headers))
        sheet["A3"].font = Font(name="Arial", italic=True, color="666666")
        sheet["A3"].alignment = Alignment(horizontal="right")
        sheet.append(headers)
        for cell in sheet[4]:
            cell.font = Font(name="Arial", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="167D8D")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in data:
            sheet.append(row)
        for row in sheet.iter_rows(min_row=5):
            for cell in row:
                cell.font = Font(name="Arial", size=11)
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if isinstance(cell.value, (int, float, Decimal)):
                    cell.number_format = '#,##0.00;[Red]-#,##0.00'
        for column in sheet.columns:
            letter = get_column_letter(column[0].column)
            width = min(max(max(len(str(c.value or "")) for c in column) + 3, 14), 42)
            sheet.column_dimensions[letter].width = width
        sheet.freeze_panes = "A5"
        sheet.auto_filter.ref = f"A4:{get_column_letter(len(headers))}{sheet.max_row}"
        sheet.print_title_rows = "1:4"
        sheet.oddFooter.center.text = "صفحة &[Page] من &[Pages]"
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.page_setup.orientation = "landscape" if len(headers) > 6 else "portrait"
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=filename,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @bp.post("/reports/export/current-screen/excel")
    def export_current_screen_excel():
        if not session.get("user_id"):
            abort(401)
        allowed = {
            "reports.view", "accounts.view", "accounts.manage", "stock.view", "stock.manage",
            "purchases.view", "purchases.manage", "sales.view", "sales.manage",
            "assets.view", "assets.manage", "bank.view", "bank.manage", "tax.view",
            "workforce.view", "workforce.manage", "party.view", "budget.view",
            "budget.manage", "cashflow.view", "quotes.view", "quotes.manage",
        }
        active_permissions = set(session.get("permissions", permissions.get(session.get("role"), set())))
        if not active_permissions.intersection(allowed):
            abort(403)
        payload = request.get_json(silent=True) or {}
        tables = payload.get("tables")
        if not isinstance(tables, list) or not tables or len(tables) > 20:
            abort(400)
        cell_count = sum(
            len(row) for table in tables if isinstance(table, dict)
            for row in table.get("rows", []) if isinstance(row, list)
        )
        if cell_count > 20000:
            abort(413)
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            return Response("مكتبة openpyxl غير مثبتة.", status=500, mimetype="text/plain; charset=utf-8")

        conn = connect()
        company_row = conn.execute(
            "SELECT setting_value FROM company_settings WHERE setting_key='company_name'"
        ).fetchone()
        company_name = company_row["setting_value"] if company_row else "NEXUS ERP"
        conn.close()

        workbook = Workbook()
        workbook.remove(workbook.active)
        used_names = set()
        for index, table in enumerate(tables, start=1):
            if not isinstance(table, dict):
                continue
            rows = table.get("rows", [])
            if not isinstance(rows, list) or not rows:
                continue
            raw_title = str(table.get("title") or ("تقرير " + str(index))).strip()
            safe_title = "".join(ch for ch in raw_title if ch not in '[]:*?/\\')
            safe_title = (safe_title or ("Report " + str(index)))[:31]
            base_name, suffix = safe_title, 1
            while safe_title in used_names:
                suffix += 1
                safe_title = (base_name[:27] + "_" + str(suffix))[:31]
            used_names.add(safe_title)
            sheet = workbook.create_sheet(safe_title)
            sheet.sheet_view.rightToLeft = True
            width = max((len(row) for row in rows if isinstance(row, list)), default=1)
            width = max(1, min(width, 80))
            sheet.append([company_name])
            sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
            sheet["A1"].font = Font(name="Arial", bold=True, color="FFFFFF", size=12)
            sheet["A1"].fill = PatternFill("solid", fgColor="17365D")
            sheet["A1"].alignment = Alignment(horizontal="center")
            sheet.append([raw_title])
            sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
            sheet["A2"].font = Font(name="Arial", bold=True, color="FFFFFF", size=15)
            sheet["A2"].fill = PatternFill("solid", fgColor="17365D")
            sheet["A2"].alignment = Alignment(horizontal="center")
            sheet.append(["تاريخ إعداد التقرير: " + datetime.now().strftime("%Y-%m-%d %H:%M")])
            sheet.merge_cells(start_row=3, start_column=1, end_row=3, end_column=width)
            sheet["A3"].font = Font(name="Arial", italic=True, color="666666")
            sheet.append(rows[0][:80] if isinstance(rows[0], list) else [str(rows[0])])
            for cell in sheet[4]:
                cell.font = Font(name="Arial", bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="167D8D")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            for row in rows[1:]:
                if not isinstance(row, list):
                    continue
                # Append a complete row after converting values.
                converted = []
                for value in row[:80]:
                    text_value = "" if value is None else str(value).strip()
                    numeric = text_value.replace(",", "")
                    if re.fullmatch(r"-?\d+(?:\.\d+)?", numeric):
                        converted.append(Decimal(numeric))
                    elif text_value.startswith(("=", "+", "@")):
                        converted.append("'" + text_value)
                    else:
                        converted.append(text_value)
                sheet.append(converted)
            for row in sheet.iter_rows(min_row=5):
                for cell in row:
                    cell.font = Font(name="Arial", size=10)
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                    if isinstance(cell.value, (int, float, Decimal)):
                        cell.number_format = '#,##0.00;[Red]-#,##0.00'
            for column in sheet.columns:
                letter = get_column_letter(column[0].column)
                max_width = max((len(str(cell.value or "")) for cell in column), default=10)
                sheet.column_dimensions[letter].width = min(max(max_width + 3, 12), 42)
            sheet.freeze_panes = "A5"
            sheet.auto_filter.ref = f"A4:{get_column_letter(width)}{sheet.max_row}"
            sheet.print_title_rows = "1:4"
            sheet.oddFooter.center.text = "صفحة &[Page] من &[Pages]"
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            sheet.page_setup.orientation = "landscape" if width > 6 else "portrait"
        if not workbook.sheetnames:
            abort(400)
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="NEXUS-ERP-Report.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    app.register_blueprint(bp)
