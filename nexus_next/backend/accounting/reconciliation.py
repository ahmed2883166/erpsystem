"""Read-only subledger/control-account reconciliation with pending-document warnings."""
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import accessible_branches, can_access_branch, can_access_company, is_platform_owner
from companies.models import Branch, Company, CompanyMembership
from .models import AccountRole, JournalEntry, JournalLine


POSTED_STATUSES = (JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)


def _journal_balance(account_ids, company_id, branch_ids, as_of, journal_ids=None):
    lines = JournalLine.objects.filter(
        account_id__in=account_ids,
        entry__company_id=company_id,
        entry__branch_id__in=branch_ids,
        entry__entry_date__lte=as_of,
        entry__status__in=POSTED_STATUSES,
    )
    if journal_ids is not None:
        lines = lines.filter(entry_id__in=journal_ids)
    totals = lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    debit = totals["debit"] or Decimal("0")
    credit = totals["credit"] or Decimal("0")
    return debit, credit, lines.values("entry_id").distinct().count()


def _collect_ids(querysets, fields):
    ids = set()
    for queryset, field_names in zip(querysets, fields):
        for row in queryset.values_list(*field_names):
            ids.update(value for value in row if value)
    return ids


def _with_reversals(journal_ids):
    if not journal_ids:
        return set()
    reversal_ids = JournalEntry.objects.filter(
        reversal_of_id__in=journal_ids, status__in=POSTED_STATUSES
    ).values_list("pk", flat=True)
    return set(journal_ids).union(reversal_ids)


class SubledgerReconciliationView(APIView):
    """Compare operationally linked posted journals with their GL control accounts."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        raw_company = request.query_params.get("company")
        if not raw_company or not raw_company.isdigit():
            raise ValidationError({"company": "حدد الشركة المطلوبة."})
        company_id = int(raw_company)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        as_of = parse_date(request.query_params.get("as_of") or timezone.localdate().isoformat())
        if not as_of:
            raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})

        has_all_branches = (
            request.user.is_superuser and not is_platform_owner(request.user)
        ) or CompanyMembership.objects.filter(
            user=request.user, company_id=company_id, is_active=True, all_branches=True
        ).exists()
        if has_all_branches:
            branch_ids = list(Branch.objects.filter(company_id=company_id).values_list("id", flat=True))
        else:
            branch_ids = list(
                accessible_branches(request.user, company_id).filter(company_id=company_id).values_list("id", flat=True)
            )

        selected_branch = None
        raw_branch = request.query_params.get("branch")
        if raw_branch:
            if not raw_branch.isdigit():
                raise ValidationError({"branch": "معرّف الفرع غير صحيح."})
            selected_branch = Branch.objects.filter(
                pk=int(raw_branch), company_id=company_id, is_active=True
            ).first()
            if selected_branch is None:
                raise ValidationError({"branch": "الفرع المحدد غير موجود أو موقوف."})
            if selected_branch.pk not in branch_ids and not can_access_branch(request.user, selected_branch.pk, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            branch_ids = [selected_branch.pk]

        company = Company.objects.select_related("base_currency").get(pk=company_id)
        role_rows = {
            item.role: item.account
            for item in AccountRole.objects.filter(company_id=company_id).select_related("account")
        }
        default_branch = Branch.objects.filter(
            company_id=company_id, is_active=True, is_head_office=True
        ).first()
        if default_branch is None:
            default_branch = Branch.objects.filter(company_id=company_id, is_active=True).order_by("code").first()
        include_unassigned_warehouses = bool(default_branch and default_branch.pk in branch_ids)

        from sales.models import SalesInvoice, CustomerReceipt, SalesReturn
        from purchasing.models import PurchaseInvoice, SupplierPayment, SupplierReturn, SupplierRefund
        from accounting.models import FxSubledgerRevaluation
        from inventory.models import StockBalance, StockMovement, StockMovementJournalLink
        from hr.models import PayrollCycle, PayrollPosting

        # The warehouse branch is the source of truth for operational-document branch scope.
        sales_invoice_scope = Q(delivery__warehouse__branch_id__in=branch_ids)
        receipt_scope = Q(invoice__delivery__warehouse__branch_id__in=branch_ids)
        sales_return_scope = Q(warehouse__branch_id__in=branch_ids)
        purchase_invoice_scope = Q(receipt__warehouse__branch_id__in=branch_ids)
        supplier_payment_scope = Q(invoice__receipt__warehouse__branch_id__in=branch_ids)
        supplier_return_scope = (
            Q(stock_movement__source_warehouse__branch_id__in=branch_ids)
            | Q(stock_movement__destination_warehouse__branch_id__in=branch_ids)
        )
        supplier_refund_scope = (
            Q(return_document__stock_movement__source_warehouse__branch_id__in=branch_ids)
            | Q(return_document__stock_movement__destination_warehouse__branch_id__in=branch_ids)
        )
        if include_unassigned_warehouses:
            sales_invoice_scope |= Q(delivery__warehouse__branch__isnull=True)
            receipt_scope |= Q(invoice__delivery__warehouse__branch__isnull=True)
            sales_return_scope |= Q(warehouse__branch__isnull=True)
            purchase_invoice_scope |= Q(receipt__warehouse__branch__isnull=True)
            supplier_payment_scope |= Q(invoice__receipt__warehouse__branch__isnull=True)
            supplier_return_scope |= (Q(stock_movement__source_warehouse__branch__isnull=True, stock_movement__destination_warehouse__branch_id__in=branch_ids) | Q(stock_movement__destination_warehouse__branch__isnull=True, stock_movement__source_warehouse__branch_id__in=branch_ids) | Q(stock_movement__source_warehouse__branch__isnull=True, stock_movement__destination_warehouse__branch__isnull=True))
            supplier_refund_scope |= (Q(return_document__stock_movement__source_warehouse__branch__isnull=True, return_document__stock_movement__destination_warehouse__branch_id__in=branch_ids) | Q(return_document__stock_movement__destination_warehouse__branch__isnull=True, return_document__stock_movement__source_warehouse__branch_id__in=branch_ids) | Q(return_document__stock_movement__source_warehouse__branch__isnull=True, return_document__stock_movement__destination_warehouse__branch__isnull=True))

        sales_invoices = SalesInvoice.objects.filter(
            company_id=company_id, invoice_date__lte=as_of, status=SalesInvoice.Status.POSTED
        ).filter(sales_invoice_scope)
        customer_receipts = CustomerReceipt.objects.filter(
            company_id=company_id, receipt_date__lte=as_of, status__in=(CustomerReceipt.Status.POSTED, CustomerReceipt.Status.REVERSED)
        ).filter(receipt_scope)
        sales_returns = SalesReturn.objects.filter(
            company_id=company_id, return_date__lte=as_of, status__in=(SalesReturn.Status.POSTED, SalesReturn.Status.REVERSED)
        ).filter(sales_return_scope)
        sales_fx = FxSubledgerRevaluation.objects.filter(
            company_id=company_id, sales_invoice__isnull=False, revaluation_date__lte=as_of
        ).filter(Q(sales_invoice__delivery__warehouse__branch_id__in=branch_ids) | (Q(sales_invoice__delivery__warehouse__branch__isnull=True) if include_unassigned_warehouses else Q(pk__in=[])))

        purchase_invoices = PurchaseInvoice.objects.filter(
            company_id=company_id, invoice_date__lte=as_of, status=PurchaseInvoice.Status.POSTED
        ).filter(purchase_invoice_scope)
        supplier_payments = SupplierPayment.objects.filter(
            company_id=company_id, payment_date__lte=as_of, status__in=(SupplierPayment.Status.POSTED, SupplierPayment.Status.REVERSED)
        ).filter(supplier_payment_scope)
        supplier_returns = SupplierReturn.objects.filter(
            company_id=company_id, return_date__lte=as_of, status__in=(SupplierReturn.Status.POSTED, SupplierReturn.Status.REVERSED)
        ).filter(supplier_return_scope)
        supplier_refunds = SupplierRefund.objects.filter(
            company_id=company_id, refund_date__lte=as_of, status__in=(SupplierRefund.Status.POSTED, SupplierRefund.Status.REVERSED)
        ).filter(supplier_refund_scope)
        purchase_fx = FxSubledgerRevaluation.objects.filter(
            company_id=company_id, purchase_invoice__isnull=False, revaluation_date__lte=as_of
        ).filter(Q(purchase_invoice__receipt__warehouse__branch_id__in=branch_ids) | (Q(purchase_invoice__receipt__warehouse__branch__isnull=True) if include_unassigned_warehouses else Q(pk__in=[])))

        sales_journal_ids = _with_reversals(_collect_ids(
            [sales_invoices, customer_receipts, sales_returns, sales_fx],
            [("journal_entry_id",), ("journal_entry_id",), ("journal_entry_id", "reversal_journal_entry_id"), ("journal_entry_id",)],
        ))
        purchase_journal_ids = _with_reversals(_collect_ids(
            [purchase_invoices, supplier_payments, supplier_returns, supplier_refunds, purchase_fx],
            [("journal_entry_id",), ("journal_entry_id",), ("journal_entry_id", "reversal_journal_entry_id"), ("journal_entry_id", "reversal_journal_entry_id"), ("journal_entry_id",)],
        ))

        payroll_rows = PayrollPosting.objects.filter(
            cycle__company_id=company_id, cycle__period_start__lte=as_of, branch_id__in=branch_ids
        ).values_list("journal_entry_id", "payment_journal_entry_id")
        payroll_journal_ids = {value for row in payroll_rows for value in row if value}
        payroll_journal_ids = _with_reversals(payroll_journal_ids)

        stock_links = StockMovementJournalLink.objects.filter(
            movement__company_id=company_id, movement__movement_date__lte=as_of
        ).filter(
            Q(movement__source_warehouse__branch_id__in=branch_ids)
            | Q(movement__destination_warehouse__branch_id__in=branch_ids)
            | (Q(movement__source_warehouse__branch__isnull=True) | Q(movement__destination_warehouse__branch__isnull=True) if include_unassigned_warehouses else Q(pk__in=[]))
        ).values_list("journal_entry_id")
        inventory_journal_ids = _with_reversals({row[0] for row in stock_links if row[0]})

        def scoped_ids(ids):
            if not ids:
                return set()
            return set(JournalEntry.objects.filter(
                pk__in=ids, company_id=company_id, branch_id__in=branch_ids,
                entry_date__lte=as_of, status__in=POSTED_STATUSES,
            ).values_list("pk", flat=True))

        sales_journal_ids = scoped_ids(sales_journal_ids)
        purchase_journal_ids = scoped_ids(purchase_journal_ids)
        payroll_journal_ids = scoped_ids(payroll_journal_ids)
        inventory_journal_ids = scoped_ids(inventory_journal_ids)

        pending = {
            "sales": SalesInvoice.objects.filter(company_id=company_id, invoice_date__lte=as_of, status=SalesInvoice.Status.DRAFT).filter(sales_invoice_scope).count()
            + CustomerReceipt.objects.filter(company_id=company_id, receipt_date__lte=as_of, status=CustomerReceipt.Status.DRAFT).filter(receipt_scope).count()
            + SalesReturn.objects.filter(company_id=company_id, return_date__lte=as_of, status=SalesReturn.Status.DRAFT).filter(sales_return_scope).count(),
            "purchasing": PurchaseInvoice.objects.filter(company_id=company_id, invoice_date__lte=as_of, status=PurchaseInvoice.Status.DRAFT).filter(purchase_invoice_scope).count()
            + SupplierPayment.objects.filter(company_id=company_id, payment_date__lte=as_of, status=SupplierPayment.Status.DRAFT).filter(supplier_payment_scope).count()
            + SupplierReturn.objects.filter(company_id=company_id, return_date__lte=as_of, status=SupplierReturn.Status.DRAFT).filter(supplier_return_scope).count()
            + SupplierRefund.objects.filter(company_id=company_id, refund_date__lte=as_of, status=SupplierRefund.Status.DRAFT).filter(supplier_refund_scope).count(),
            "payroll": PayrollCycle.objects.filter(company_id=company_id, period_end__lte=as_of, status__in=(PayrollCycle.Status.DRAFT, PayrollCycle.Status.CALCULATED, PayrollCycle.Status.REVIEW, PayrollCycle.Status.APPROVED)).count(),
            "inventory": StockMovement.objects.filter(company_id=company_id, movement_date__lte=as_of, status=StockMovement.Status.DRAFT).filter(
                Q(source_warehouse__branch_id__in=branch_ids)
                | Q(destination_warehouse__branch_id__in=branch_ids)
                | (Q(source_warehouse__branch__isnull=True, destination_warehouse__branch_id__in=branch_ids)
                   | Q(destination_warehouse__branch__isnull=True, source_warehouse__branch_id__in=branch_ids)
                   | Q(source_warehouse__branch__isnull=True, destination_warehouse__branch__isnull=True)
                   if include_unassigned_warehouses else Q(pk__in=[]))
            ).count(),
        }
        pending_total = sum(pending.values())
        posted_stock_movements = StockMovement.objects.filter(
            company_id=company_id, movement_date__lte=as_of, status=StockMovement.Status.POSTED
        ).filter(
            Q(source_warehouse__branch_id__in=branch_ids) | Q(destination_warehouse__branch_id__in=branch_ids)
            | (Q(source_warehouse__branch__isnull=True, destination_warehouse__branch_id__in=branch_ids)
               | Q(destination_warehouse__branch__isnull=True, source_warehouse__branch_id__in=branch_ids)
               | Q(source_warehouse__branch__isnull=True, destination_warehouse__branch__isnull=True)
               if include_unassigned_warehouses else Q(pk__in=[]))
        )
        missing_links = {
            "sales": sales_invoices.filter(journal_entry__isnull=True).count() + customer_receipts.filter(journal_entry__isnull=True).count() + sales_returns.filter(journal_entry__isnull=True).count(),
            "purchasing": purchase_invoices.filter(journal_entry__isnull=True).count() + supplier_payments.filter(journal_entry__isnull=True).count() + supplier_returns.filter(journal_entry__isnull=True).count() + supplier_refunds.filter(journal_entry__isnull=True).count(),
            "inventory": posted_stock_movements.filter(journal_links__isnull=True).count(),
        }
        missing_links_total = sum(missing_links.values())

        def make_row(key, label, roles, nature, linked_ids, pending_count=0):
            accounts = [role_rows[role] for role in roles if role in role_rows]
            if not accounts:
                return {
                    "key": key, "label": label, "status": "not_configured",
                    "accounts": [], "subledger_balance": None, "gl_balance": None,
                    "difference": None, "source_journal_count": 0, "pending_documents_count": pending_count,
                    "unmatched_entries": [], "unmatched_entry_count": 0,
                    "detail": "لم يتم ربط الحساب الرقابي بهذا الدور في إعدادات الشركة.",
                }
            invalid = [
                account for account in accounts
                if not account.is_active or account.is_group or not account.allow_posting or account.nature != nature
            ]
            if invalid:
                return {
                    "key": key, "label": label, "status": "invalid_account",
                    "accounts": [{"id": a.pk, "code": a.code, "name": a.name_ar} for a in accounts],
                    "subledger_balance": None, "gl_balance": None, "difference": None,
                    "source_journal_count": 0, "pending_documents_count": pending_count,
                    "unmatched_entries": [], "unmatched_entry_count": 0,
                    "detail": "يجب أن تكون الحسابات الرقابية نشطة وتفصيلية وقابلة للترحيل وبالطبيعة المحاسبية الصحيحة.",
                }
            account_ids = [account.pk for account in accounts]
            sub_debit, sub_credit, source_count = _journal_balance(account_ids, company_id, branch_ids, as_of, linked_ids)
            gl_debit, gl_credit, _ = _journal_balance(account_ids, company_id, branch_ids, as_of)
            if nature == "liability":
                subledger_balance, gl_balance = sub_credit - sub_debit, gl_credit - gl_debit
            else:
                subledger_balance, gl_balance = sub_debit - sub_credit, gl_debit - gl_credit
            difference = gl_balance - subledger_balance
            unmatched_qs = JournalLine.objects.filter(
                account_id__in=account_ids, entry__company_id=company_id,
                entry__branch_id__in=branch_ids, entry__entry_date__lte=as_of,
                entry__status__in=POSTED_STATUSES,
            ).exclude(entry_id__in=linked_ids).select_related("entry", "account").order_by("-entry__entry_date", "-id")
            unmatched_count = unmatched_qs.count()
            unmatched = unmatched_qs[:50]
            if difference != 0:
                status = "difference"
            elif pending_count:
                status = "pending_documents"
            else:
                # Linked-journal comparison is not a complete party-level subledger.
                # Never label it fully reconciled until invoice allocations/open items
                # are included in the calculation.
                status = "partial_reconciliation"
            return {
                "key": key, "label": label, "status": status,
                "accounts": [{"id": a.pk, "code": a.code, "name": a.name_ar} for a in accounts],
                "subledger_balance": str(subledger_balance), "gl_balance": str(gl_balance),
                "difference": str(difference), "source_journal_count": source_count,
                "pending_documents_count": pending_count, "unmatched_entry_count": unmatched_count,
                "unmatched_entries": [{
                    "journal_id": line.entry_id, "entry_number": line.entry.entry_number,
                    "entry_date": line.entry.entry_date.isoformat(), "description": line.entry.description,
                    "account_code": line.account.code, "debit": str(line.debit), "credit": str(line.credit),
                } for line in unmatched],
                "detail": "مطابقة جزئية: تقارن القيود المرحلة المرتبطة بالمستندات بحركة الحساب الرقابي. لا تمثل هذه القيمة رصيدًا مفتوحًا تفصيليًا لكل عميل أو مورد، لذلك لا تُعد اعتمادًا نهائيًا للأستاذ المساعد. المستندات المسودة تظهر كتنبيه."
            }

        rows = [
            make_row("customers", "حسابات العملاء", (AccountRole.Role.ACCOUNTS_RECEIVABLE,), "asset", sales_journal_ids, pending["sales"]),
            make_row("suppliers", "حسابات الموردين", (AccountRole.Role.ACCOUNTS_PAYABLE,), "liability", purchase_journal_ids, pending["purchasing"]),
            make_row("payroll", "التزامات المرتبات والتأمينات والضرائب", (
                AccountRole.Role.PAYROLL_PAYABLE, AccountRole.Role.EMPLOYEE_INSURANCE_PAYABLE,
                AccountRole.Role.EMPLOYER_INSURANCE_PAYABLE, AccountRole.Role.PAYROLL_TAX_PAYABLE,
            ), "liability", payroll_journal_ids, pending["payroll"]),
        ]

        inventory_account = role_rows.get(AccountRole.Role.INVENTORY)
        if inventory_account and inventory_account.is_active and not inventory_account.is_group and inventory_account.allow_posting and inventory_account.nature == "asset":
            stock_scope = Q(warehouse__branch_id__in=branch_ids)
            if include_unassigned_warehouses:
                stock_scope |= Q(warehouse__branch__isnull=True)
            stock_total = StockBalance.objects.filter(warehouse__company_id=company_id).filter(stock_scope).aggregate(
                total=Sum("inventory_value")
            )["total"] or Decimal("0")
            gl_debit, gl_credit, _ = _journal_balance([inventory_account.pk], company_id, branch_ids, as_of)
            gl_balance = gl_debit - gl_credit
            if as_of == timezone.localdate():
                difference = gl_balance - stock_total
                inventory_status = "difference" if difference != 0 else ("pending_documents" if pending["inventory"] else "matched")
                inventory_detail = "مطابقة لقطة قيمة المخزون الحالية مع حساب المخزون في الأستاذ العام."
            else:
                difference = None
                inventory_status = "historical_snapshot_unavailable"
                inventory_detail = "لا توجد لقطات مخزون تاريخية محفوظة لهذا التاريخ؛ التقرير يمنع عرض فرق تاريخي غير موثوق."
            rows.append({
                "key": "inventory", "label": "قيمة المخزون", "status": inventory_status,
                "accounts": [{"id": inventory_account.pk, "code": inventory_account.code, "name": inventory_account.name_ar}],
                "subledger_balance": str(stock_total), "gl_balance": str(gl_balance),
                "difference": str(difference) if difference is not None else None,
                "source_journal_count": len(inventory_journal_ids), "pending_documents_count": pending["inventory"],
                "unmatched_entries": [], "unmatched_entry_count": 0, "detail": inventory_detail,
            })
        else:
            rows.append({
                "key": "inventory", "label": "قيمة المخزون", "status": "not_configured",
                "accounts": [], "subledger_balance": None, "gl_balance": None, "difference": None,
                "source_journal_count": 0, "pending_documents_count": pending["inventory"],
                "unmatched_entries": [], "unmatched_entry_count": 0, "detail": "حساب المخزون غير مربوط أو غير صالح.",
            })

        return Response({
            "company": company_id, "as_of": as_of.isoformat(),
            "branch": selected_branch.pk if selected_branch else None,
            "branch_name": selected_branch.name_ar if selected_branch else "",
            "scope_branch_ids": branch_ids, "currency": company.base_currency.code,
            "currency_decimal_places": company.base_currency.decimal_places,
            "rows": rows, "pending_documents": pending, "pending_documents_count": pending_total,
            "posted_documents_missing_journal": missing_links, "posted_documents_missing_journal_count": missing_links_total,
            "matched_count": sum(row["status"] == "matched" for row in rows),
            "partial_reconciliation_count": sum(row["status"] == "partial_reconciliation" for row in rows),
            "difference_count": sum(row["status"] == "difference" for row in rows),
            "pending_count": sum(row["status"] == "pending_documents" for row in rows),
            "configuration_issue_count": sum(row["status"] in ("not_configured", "invalid_account") for row in rows),
            "unavailable_count": sum(row["status"] == "historical_snapshot_unavailable" for row in rows),
            "is_reconciled": False,
            "note": "تقرير قراءة فقط وفي مرحلته الحالية لا يمنح اعتماد مطابقة نهائيًا للعملاء والموردين والرواتب؛ إذ يلزم احتساب الأرصدة المفتوحة على مستوى كل طرف وتخصيصات السداد. يعرض التقرير مقارنة القيود المرتبطة، والفروق، والمسودات، والمستندات المرحلة بلا قيود. المخزون يُطابق بتاريخ اليوم فقط لعدم وجود سجل لقطات تاريخية؛ أي تاريخ سابق يُعلّم كبيانات غير متاحة.",
        })
