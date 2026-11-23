"""Read-only control-account reconciliation against operationally linked journals."""
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, ValidationError

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


class SubledgerReconciliationView(APIView):
    """Compare control-account GL balances with journals linked to their source modules."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        raw_company = request.query_params.get("company")
        if not raw_company or not raw_company.isdigit():
            raise ValidationError({"company": "حدد الشركة المطلوبة."})
        company_id = int(raw_company)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        raw_date = request.query_params.get("as_of") or timezone.localdate().isoformat()
        as_of = parse_date(raw_date)
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

        # Collect only source journals that are actually linked to posted operational documents.
        from sales.models import SalesInvoice, CustomerReceipt, SalesReturn
        from purchasing.models import PurchaseInvoice, SupplierPayment, SupplierReturn, SupplierRefund
        from accounting.models import FxSubledgerRevaluation
        from inventory.models import StockBalance, StockMovementJournalLink
        from hr.models import PayrollPosting

        sales_docs = [
            SalesInvoice.objects.filter(company_id=company_id, invoice_date__lte=as_of, status=SalesInvoice.Status.POSTED),
            CustomerReceipt.objects.filter(company_id=company_id, receipt_date__lte=as_of, status=CustomerReceipt.Status.POSTED),
            SalesReturn.objects.filter(company_id=company_id, return_date__lte=as_of, status__in=(SalesReturn.Status.POSTED, SalesReturn.Status.REVERSED)),
            FxSubledgerRevaluation.objects.filter(company_id=company_id, sales_invoice__isnull=False, revaluation_date__lte=as_of),
        ]
        sales_fields = [("journal_entry_id",), ("journal_entry_id",), ("journal_entry_id", "reversal_journal_entry_id"), ("journal_entry_id",)]
        sales_journal_ids = _collect_ids(sales_docs, sales_fields)

        purchase_docs = [
            PurchaseInvoice.objects.filter(company_id=company_id, invoice_date__lte=as_of, status=PurchaseInvoice.Status.POSTED),
            SupplierPayment.objects.filter(company_id=company_id, payment_date__lte=as_of, status=SupplierPayment.Status.POSTED),
            SupplierReturn.objects.filter(company_id=company_id, return_date__lte=as_of, status__in=(SupplierReturn.Status.POSTED, SupplierReturn.Status.REVERSED)),
            SupplierRefund.objects.filter(company_id=company_id, refund_date__lte=as_of, status__in=(SupplierRefund.Status.POSTED, SupplierRefund.Status.REVERSED)),
            FxSubledgerRevaluation.objects.filter(company_id=company_id, purchase_invoice__isnull=False, revaluation_date__lte=as_of),
        ]
        purchase_fields = [("journal_entry_id",), ("journal_entry_id",), ("journal_entry_id", "reversal_journal_entry_id"), ("journal_entry_id", "reversal_journal_entry_id"), ("journal_entry_id",)]
        purchase_journal_ids = _collect_ids(purchase_docs, purchase_fields)

        payroll_rows = PayrollPosting.objects.filter(
            cycle__company_id=company_id, cycle__period_start__lte=as_of, branch_id__in=branch_ids
        ).values_list("journal_entry_id", "payment_journal_entry_id")
        payroll_journal_ids = {value for row in payroll_rows for value in row if value}

        stock_links = StockMovementJournalLink.objects.filter(
            movement__company_id=company_id, movement__movement_date__lte=as_of
        ).values_list("journal_entry_id")
        inventory_journal_ids = {row[0] for row in stock_links if row[0]}

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

        def make_row(key, label, roles, nature, linked_ids):
            accounts = [role_rows[role] for role in roles if role in role_rows]
            if not accounts:
                return {
                    "key": key, "label": label, "status": "not_configured",
                    "accounts": [], "subledger_balance": None, "gl_balance": None,
                    "difference": None, "source_journal_count": 0, "unmatched_entries": [],
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
                    "source_journal_count": 0, "unmatched_entries": [],
                    "detail": "يجب أن تكون الحسابات الرقابية نشطة وتفصيلية وقابلة للترحيل وبالطبيعة المحاسبية الصحيحة.",
                }
            account_ids = [account.pk for account in accounts]
            sub_debit, sub_credit, source_count = _journal_balance(
                account_ids, company_id, branch_ids, as_of, linked_ids
            )
            gl_debit, gl_credit, _ = _journal_balance(account_ids, company_id, branch_ids, as_of)
            if nature == "liability":
                subledger_balance = sub_credit - sub_debit
                gl_balance = gl_credit - gl_debit
            else:
                subledger_balance = sub_debit - sub_credit
                gl_balance = gl_debit - gl_credit
            difference = gl_balance - subledger_balance
            unmatched = JournalLine.objects.filter(
                account_id__in=account_ids, entry__company_id=company_id,
                entry__branch_id__in=branch_ids, entry__entry_date__lte=as_of,
                entry__status__in=POSTED_STATUSES,
            ).exclude(entry_id__in=linked_ids).select_related("entry", "account").order_by("-entry__entry_date", "-id")[:50]
            return {
                "key": key, "label": label,
                "status": "matched" if difference == 0 else "difference",
                "accounts": [{"id": a.pk, "code": a.code, "name": a.name_ar} for a in accounts],
                "subledger_balance": str(subledger_balance), "gl_balance": str(gl_balance),
                "difference": str(difference), "source_journal_count": source_count,
                "unmatched_entries": [{
                    "journal_id": line.entry_id, "entry_number": line.entry.entry_number,
                    "entry_date": line.entry.entry_date.isoformat(), "description": line.entry.description,
                    "account_code": line.account.code, "debit": str(line.debit), "credit": str(line.credit),
                } for line in unmatched],
                "detail": "المقارنة مبنية على القيود المرحلة المرتبطة بمستندات الوحدة مقابل كامل حركة الحساب في الأستاذ العام.",
            }

        rows = [
            make_row("customers", "حسابات العملاء", (AccountRole.Role.ACCOUNTS_RECEIVABLE,), "asset", sales_journal_ids),
            make_row("suppliers", "حسابات الموردين", (AccountRole.Role.ACCOUNTS_PAYABLE,), "liability", purchase_journal_ids),
            make_row("payroll", "التزامات المرتبات والتأمينات والضرائب", (
                AccountRole.Role.PAYROLL_PAYABLE,
                AccountRole.Role.EMPLOYEE_INSURANCE_PAYABLE,
                AccountRole.Role.EMPLOYER_INSURANCE_PAYABLE,
                AccountRole.Role.PAYROLL_TAX_PAYABLE,
            ), "liability", payroll_journal_ids),
        ]

        inventory_account = role_rows.get(AccountRole.Role.INVENTORY)
        if inventory_account and inventory_account.is_active and not inventory_account.is_group and inventory_account.allow_posting and inventory_account.nature == "asset":
            default_branch = Branch.objects.filter(company_id=company_id, is_active=True, is_head_office=True).first()
            if default_branch is None:
                default_branch = Branch.objects.filter(company_id=company_id, is_active=True).order_by("code").first()
            stock_scope = Q(warehouse__branch_id__in=branch_ids)
            if default_branch and default_branch.pk in branch_ids:
                stock_scope |= Q(warehouse__branch__isnull=True)
            stock_total = StockBalance.objects.filter(warehouse__company_id=company_id).filter(stock_scope).aggregate(
                total=Sum("inventory_value")
            )["total"] or Decimal("0")
            gl_debit, gl_credit, _ = _journal_balance([inventory_account.pk], company_id, branch_ids, as_of)
            gl_balance = gl_debit - gl_credit
            current_date = timezone.localdate()
            if as_of == current_date:
                difference = gl_balance - stock_total
                inventory_status = "matched" if difference == 0 else "difference"
                inventory_detail = "مطابقة لقطة قيمة المخزون الحالية مع حساب المخزون في الأستاذ العام."
            else:
                difference = None
                inventory_status = "historical_snapshot_unavailable"
                inventory_detail = "لا يحتفظ النظام حاليًا بلقطات تاريخية لقيمة المخزون؛ لذلك لا يصح مقارنة رصيد اليوم بتاريخ سابق."
            rows.append({
                "key": "inventory", "label": "قيمة المخزون",
                "status": inventory_status,
                "accounts": [{"id": inventory_account.pk, "code": inventory_account.code, "name": inventory_account.name_ar}],
                "subledger_balance": str(stock_total), "gl_balance": str(gl_balance),
                "difference": str(difference) if difference is not None else None,
                "source_journal_count": len(inventory_journal_ids), "unmatched_entries": [],
                "detail": inventory_detail,
            })
        else:
            rows.append({
                "key": "inventory", "label": "قيمة المخزون", "status": "not_configured",
                "accounts": [], "subledger_balance": None, "gl_balance": None, "difference": None,
                "source_journal_count": 0, "unmatched_entries": [],
                "detail": "حساب المخزون غير مربوط أو غير صالح.",
            })

        return Response({
            "company": company_id, "as_of": as_of.isoformat(),
            "branch": selected_branch.pk if selected_branch else None,
            "branch_name": selected_branch.name_ar if selected_branch else "",
            "scope_branch_ids": branch_ids, "currency": company.base_currency.code,
            "currency_decimal_places": company.base_currency.decimal_places,
            "rows": rows,
            "matched_count": sum(row["status"] == "matched" for row in rows),
            "difference_count": sum(row["status"] == "difference" for row in rows),
            "configuration_issue_count": sum(row["status"] in ("not_configured", "invalid_account") for row in rows),
            "unavailable_count": sum(row["status"] == "historical_snapshot_unavailable" for row in rows),
            "note": "تقرير قراءة فقط. مطابقة العملاء والموردين والمرتبات تقارن قيود المستندات المرتبطة بالوحدة مع كامل حساب الرقابة؛ أما المخزون فيُقارن بلقطة الرصيد الحالية فقط، ولا تتوفر لقطة تاريخية للمخزون بعد.",
        })
