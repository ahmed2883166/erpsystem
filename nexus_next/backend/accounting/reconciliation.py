"""Read-only subledger/control-account reconciliation with pending-document warnings."""
from decimal import Decimal

from django.db.models import Exists, F, OuterRef, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import accessible_branches, can_access_branch, can_access_company, is_platform_owner
from companies.models import Branch, Company, CompanyMembership
from inventory.models import StockMovement, StockMovementLine
from .models import AccountRole, ChartOfAccount, JournalEntry, JournalLine


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
    """Return linked journal IDs as a database subquery, never a Python ID set."""
    predicate = Q()
    has_fields = False
    for queryset, field_names in zip(querysets, fields):
        for field_name in field_names:
            predicate |= Q(
                pk__in=queryset.exclude(**{f"{field_name}__isnull": True}).values(field_name)
            )
            has_fields = True
    if not has_fields:
        return JournalEntry.objects.none().values("pk")
    return JournalEntry.objects.filter(predicate).values("pk")


def _with_reversals(journal_ids):
    """Keep original and posted reversal IDs in SQL instead of materializing them."""
    return JournalEntry.objects.filter(
        Q(pk__in=journal_ids) | Q(reversal_of_id__in=journal_ids),
        status__in=POSTED_STATUSES,
    ).values("pk")



def _replayed_inventory_value(company_id, branch_ids, as_of, include_unassigned_warehouses):
    """Rebuild stock value from immutable posted movement-line costs for a historical date."""
    movements = StockMovementLine.objects.filter(
        movement__company_id=company_id,
        movement__status=StockMovement.Status.POSTED,
        movement__movement_date__lte=as_of,
    )
    destination_scope = Q(movement__destination_warehouse__branch_id__in=branch_ids)
    source_scope = Q(movement__source_warehouse__branch_id__in=branch_ids)
    if include_unassigned_warehouses:
        destination_scope |= Q(movement__destination_warehouse__branch__isnull=True)
        source_scope |= Q(movement__source_warehouse__branch__isnull=True)
    inbound = movements.filter(
        (Q(movement__movement_type__in=(StockMovement.MovementType.RECEIPT, StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL, StockMovement.MovementType.TRANSFER)) & destination_scope)
        | (Q(movement__movement_type=StockMovement.MovementType.ADJUSTMENT, adjustment_direction=StockMovementLine.AdjustmentDirection.IN) & destination_scope)
    ).aggregate(total=Sum('cost_amount'))['total'] or Decimal('0')
    outbound = movements.filter(
        (Q(movement__movement_type__in=(StockMovement.MovementType.ISSUE, StockMovement.MovementType.SUPPLIER_RETURN, StockMovement.MovementType.SALES_RETURN_REVERSAL, StockMovement.MovementType.TRANSFER)) & source_scope)
        | (Q(movement__movement_type=StockMovement.MovementType.ADJUSTMENT, adjustment_direction=StockMovementLine.AdjustmentDirection.OUT) & source_scope)
    ).aggregate(total=Sum('cost_amount'))['total'] or Decimal('0')
    return inbound - outbound

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

        from sales.models import SalesInvoice, CustomerReceipt, CustomerReceiptBatch, CustomerRefund, SalesReturn
        from purchasing.models import PurchaseInvoice, SupplierPayment, SupplierReturn, SupplierRefund
        from customers.models import Customer
        from suppliers.models import Supplier
        from accounting.models import FxSubledgerRevaluation
        from inventory.models import StockBalance, StockMovementJournalLink
        from hr.models import Employee, PayrollComponent, PayrollCycle, PayrollPosting, PayrollResult, PayrollResultLine

        # The warehouse branch is the source of truth for operational-document branch scope.
        sales_invoice_scope = Q(branch_id__in=branch_ids) | Q(delivery__warehouse__branch_id__in=branch_ids)
        receipt_scope = Q(invoice__branch_id__in=branch_ids) | Q(invoice__delivery__warehouse__branch_id__in=branch_ids)
        sales_return_scope = Q(branch_id__in=branch_ids) | Q(warehouse__branch_id__in=branch_ids)
        purchase_invoice_scope = Q(branch_id__in=branch_ids) | Q(receipt__warehouse__branch_id__in=branch_ids)
        supplier_payment_scope = Q(invoice__branch_id__in=branch_ids) | Q(invoice__receipt__warehouse__branch_id__in=branch_ids)
        supplier_return_scope = (
            Q(stock_movement__source_warehouse__branch_id__in=branch_ids)
            | Q(stock_movement__destination_warehouse__branch_id__in=branch_ids)
        )
        supplier_refund_scope = (
            Q(return_document__stock_movement__source_warehouse__branch_id__in=branch_ids)
            | Q(return_document__stock_movement__destination_warehouse__branch_id__in=branch_ids)
        )
        if include_unassigned_warehouses:
            sales_invoice_scope |= Q(delivery__isnull=False, delivery__warehouse__branch__isnull=True)
            receipt_scope |= Q(invoice__delivery__isnull=False, invoice__delivery__warehouse__branch__isnull=True)
            sales_return_scope |= Q(warehouse__isnull=False, warehouse__branch__isnull=True)
            purchase_invoice_scope |= Q(branch__isnull=True, receipt__warehouse__branch__isnull=True)
            supplier_payment_scope |= Q(invoice__branch__isnull=True, invoice__receipt__warehouse__branch__isnull=True)
            supplier_return_scope |= (Q(stock_movement__source_warehouse__branch__isnull=True, stock_movement__destination_warehouse__branch_id__in=branch_ids) | Q(stock_movement__destination_warehouse__branch__isnull=True, stock_movement__source_warehouse__branch_id__in=branch_ids) | Q(stock_movement__source_warehouse__branch__isnull=True, stock_movement__destination_warehouse__branch__isnull=True))
            supplier_refund_scope |= (Q(return_document__stock_movement__source_warehouse__branch__isnull=True, return_document__stock_movement__destination_warehouse__branch_id__in=branch_ids) | Q(return_document__stock_movement__destination_warehouse__branch__isnull=True, return_document__stock_movement__source_warehouse__branch_id__in=branch_ids) | Q(return_document__stock_movement__source_warehouse__branch__isnull=True, return_document__stock_movement__destination_warehouse__branch__isnull=True))

        sales_invoices = SalesInvoice.objects.filter(
            company_id=company_id, invoice_date__lte=as_of, status=SalesInvoice.Status.POSTED
        ).filter(sales_invoice_scope)
        customer_receipts = CustomerReceipt.objects.filter(
            company_id=company_id, receipt_date__lte=as_of, status__in=(CustomerReceipt.Status.POSTED, CustomerReceipt.Status.REVERSED)
        ).filter(receipt_scope)
        customer_receipt_batches = CustomerReceiptBatch.objects.filter(
            company_id=company_id, branch_id__in=branch_ids, receipt_date__lte=as_of,
            status__in=(CustomerReceiptBatch.Status.POSTED, CustomerReceiptBatch.Status.REVERSED),
        )
        sales_returns = SalesReturn.objects.filter(
            company_id=company_id, return_date__lte=as_of, status__in=(SalesReturn.Status.POSTED, SalesReturn.Status.REVERSED)
        ).filter(sales_return_scope)
        customer_refunds = CustomerRefund.objects.filter(
            company_id=company_id, branch_id__in=branch_ids,
            refund_date__lte=as_of,
            status__in=(CustomerRefund.Status.POSTED, CustomerRefund.Status.REVERSED),
        )
        sales_fx = FxSubledgerRevaluation.objects.filter(
            company_id=company_id, sales_invoice__isnull=False, revaluation_date__lte=as_of
        ).filter(
            Q(sales_invoice__branch_id__in=branch_ids)
            | Q(sales_invoice__delivery__warehouse__branch_id__in=branch_ids)
            | (Q(sales_invoice__delivery__isnull=False, sales_invoice__delivery__warehouse__branch__isnull=True) if include_unassigned_warehouses else Q(pk__in=[]))
        )

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
        ).filter(
            Q(purchase_invoice__branch_id__in=branch_ids)
            | Q(purchase_invoice__receipt__warehouse__branch_id__in=branch_ids)
            | (Q(purchase_invoice__branch__isnull=True, purchase_invoice__receipt__warehouse__branch__isnull=True) if include_unassigned_warehouses else Q(pk__in=[]))
        )

        # Direct fixed-asset vendor invoices and their payments also belong to AP.
        from assets.models import AssetPurchaseInvoice, AssetSupplierPayment
        asset_purchase_invoices = AssetPurchaseInvoice.objects.filter(
            company_id=company_id, invoice_date__lte=as_of,
            status=AssetPurchaseInvoice.Status.POSTED, branch_id__in=branch_ids,
        )
        asset_supplier_payments = AssetSupplierPayment.objects.filter(
            company_id=company_id, payment_date__lte=as_of,
            status=AssetSupplierPayment.Status.POSTED, branch_id__in=branch_ids,
        )

        # Rebuild AR/AP from posted source documents grouped by the actual party.
        # Receipt/payment carrying amounts are used for foreign-currency settlements.
        active_sales_returns = sales_returns.filter(
            Q(status=SalesReturn.Status.POSTED)
            | Q(status=SalesReturn.Status.REVERSED, reversal_date__gt=as_of)
        )
        active_customer_refunds = customer_refunds.filter(
            Q(status=CustomerRefund.Status.POSTED)
            | (
                Q(status=CustomerRefund.Status.REVERSED)
                & (
                    Q(reversal_journal_entry__entry_date__gt=as_of)
                    | Q(reversal_date__isnull=True)
                    | Q(reversal_journal_entry__isnull=True)
                    | ~Q(reversal_journal_entry__status=JournalEntry.Status.POSTED)
                )
            )
        )
        active_supplier_returns = supplier_returns.filter(
            Q(status=SupplierReturn.Status.POSTED)
            | Q(status=SupplierReturn.Status.REVERSED, reversal_date__gt=as_of)
        )
        reversal_journals_as_of = JournalEntry.objects.filter(
            reversal_of_id=OuterRef("journal_entry_id"),
            entry_date__lte=as_of, status__in=POSTED_STATUSES,
        )
        posted_customer_receipts = customer_receipts.annotate(
            _reversed_by_as_of=Exists(reversal_journals_as_of)
        ).filter(
            Q(status=CustomerReceipt.Status.POSTED)
            | Q(status=CustomerReceipt.Status.REVERSED, _reversed_by_as_of=False)
        )
        posted_supplier_payments = supplier_payments.annotate(
            _reversed_by_as_of=Exists(reversal_journals_as_of)
        ).filter(
            Q(status=SupplierPayment.Status.POSTED)
            | Q(status=SupplierPayment.Status.REVERSED, _reversed_by_as_of=False)
        )
        posted_supplier_refunds = supplier_refunds.annotate(
            _reversed_by_as_of=Exists(reversal_journals_as_of)
        ).filter(
            Q(status=SupplierRefund.Status.POSTED)
            | Q(status=SupplierRefund.Status.REVERSED, _reversed_by_as_of=False)
        )

        def grouped_amount(queryset, party_id_field, amount_field, carrying_field=None):
            if carrying_field:
                queryset = queryset.annotate(_base_amount=Coalesce(carrying_field, amount_field))
                amount_field = "_base_amount"
            return {
                item[party_id_field]: item["total"] or Decimal("0")
                for item in queryset.values(party_id_field).annotate(total=Sum(amount_field))
            }

        customer_invoice_totals = grouped_amount(sales_invoices, "customer_id", "grand_total")
        customer_receipt_totals = grouped_amount(
            posted_customer_receipts, "customer_id", "amount", "carrying_base_amount"
        )
        active_customer_receipt_batches = customer_receipt_batches.filter(
            Q(status=CustomerReceiptBatch.Status.POSTED)
            | Q(status=CustomerReceiptBatch.Status.REVERSED, reversal_date__gt=as_of)
        )
        for party_id, amount in grouped_amount(
            active_customer_receipt_batches, "customer_id", "amount"
        ).items():
            customer_receipt_totals[party_id] = customer_receipt_totals.get(party_id, Decimal("0")) + amount
        customer_return_totals = grouped_amount(active_sales_returns, "invoice__customer_id", "grand_total")
        customer_refund_totals = grouped_amount(active_customer_refunds, "customer_id", "amount")
        active_sales_fx = sales_fx.annotate(
            _reversed_by_as_of=Exists(reversal_journals_as_of)
        ).filter(
            Q(journal_entry__isnull=True)
            | Q(journal_entry__status=JournalEntry.Status.POSTED)
            | Q(journal_entry__status=JournalEntry.Status.REVERSED, _reversed_by_as_of=False)
        )
        customer_fx_totals = grouped_amount(
            active_sales_fx, "sales_invoice__customer_id", "difference",
        )
        customer_ids = set().union(
            customer_invoice_totals, customer_receipt_totals, customer_return_totals,
            customer_refund_totals, customer_fx_totals,
        )
        customer_rows = {
            item["id"]: item for item in Customer.objects.filter(
                company_id=company_id, pk__in=customer_ids
            ).values("id", "code", "name_ar")
        }
        customer_party_balances = []
        for party_id in customer_ids:
            balance = (
                customer_invoice_totals.get(party_id, Decimal("0"))
                - customer_receipt_totals.get(party_id, Decimal("0"))
                - customer_return_totals.get(party_id, Decimal("0"))
                + customer_refund_totals.get(party_id, Decimal("0"))
                + customer_fx_totals.get(party_id, Decimal("0"))
            )
            party = customer_rows.get(party_id)
            if party:
                customer_party_balances.append({
                    "party_id": party_id, "code": party["code"], "name": party["name_ar"],
                    "balance": str(balance),
                })
        customer_party_balances.sort(key=lambda item: abs(Decimal(item["balance"])), reverse=True)
        customer_subledger_total = sum(
            (Decimal(item["balance"]) for item in customer_party_balances), Decimal("0")
        )

        supplier_invoice_totals = grouped_amount(purchase_invoices, "supplier_id", "grand_total")
        asset_supplier_invoice_totals = grouped_amount(asset_purchase_invoices, "supplier_id", "grand_total")
        supplier_payment_totals = grouped_amount(
            posted_supplier_payments, "supplier_id", "amount", "carrying_base_amount"
        )
        asset_supplier_payment_totals = grouped_amount(asset_supplier_payments, "supplier_id", "amount")
        supplier_return_totals = grouped_amount(active_supplier_returns, "supplier_id", "grand_total")
        supplier_refund_totals = grouped_amount(posted_supplier_refunds, "supplier_id", "amount")
        active_purchase_fx = purchase_fx.annotate(
            _reversed_by_as_of=Exists(reversal_journals_as_of)
        ).filter(
            Q(journal_entry__isnull=True)
            | Q(journal_entry__status=JournalEntry.Status.POSTED)
            | Q(journal_entry__status=JournalEntry.Status.REVERSED, _reversed_by_as_of=False)
        )
        supplier_fx_totals = grouped_amount(
            active_purchase_fx, "purchase_invoice__supplier_id", "difference",
        )
        supplier_ids = set().union(
            supplier_invoice_totals, asset_supplier_invoice_totals,
            supplier_payment_totals, asset_supplier_payment_totals,
            supplier_return_totals, supplier_refund_totals, supplier_fx_totals,
        )
        supplier_rows = {
            item["id"]: item for item in Supplier.objects.filter(
                company_id=company_id, pk__in=supplier_ids
            ).values("id", "code", "name_ar")
        }
        supplier_party_balances = []
        for party_id in supplier_ids:
            balance = (
                supplier_invoice_totals.get(party_id, Decimal("0"))
                + asset_supplier_invoice_totals.get(party_id, Decimal("0"))
                - supplier_payment_totals.get(party_id, Decimal("0"))
                - asset_supplier_payment_totals.get(party_id, Decimal("0"))
                - supplier_return_totals.get(party_id, Decimal("0"))
                + supplier_refund_totals.get(party_id, Decimal("0"))
                + supplier_fx_totals.get(party_id, Decimal("0"))
            )
            party = supplier_rows.get(party_id)
            if party:
                supplier_party_balances.append({
                    "party_id": party_id, "code": party["code"], "name": party["name_ar"],
                    "balance": str(balance),
                })
        supplier_party_balances.sort(key=lambda item: abs(Decimal(item["balance"])), reverse=True)
        supplier_subledger_total = sum(
            (Decimal(item["balance"]) for item in supplier_party_balances), Decimal("0")
        )

        sales_journal_ids = _with_reversals(_collect_ids(
            [sales_invoices, customer_receipts, sales_returns, sales_fx, customer_receipt_batches, customer_refunds],
            [("journal_entry_id",), ("journal_entry_id",), ("journal_entry_id", "reversal_journal_entry_id"),
             ("journal_entry_id",), ("journal_entry_id", "reversal_journal_entry_id"),
             ("treasury_transaction__journal_entry_id", "reversal_treasury_transaction__journal_entry_id",
              "reversal_journal_entry_id")],
        ))
        purchase_journal_ids = _with_reversals(_collect_ids(
            [purchase_invoices, supplier_payments, supplier_returns, supplier_refunds, purchase_fx,
             asset_purchase_invoices, asset_supplier_payments],
            [("journal_entry_id",), ("journal_entry_id",), ("journal_entry_id", "reversal_journal_entry_id"),
             ("journal_entry_id", "reversal_journal_entry_id"), ("journal_entry_id",),
             ("journal_entry_id",), ("journal_entry_id",)],
        ))

        payroll_rows = PayrollPosting.objects.filter(
            cycle__company_id=company_id, cycle__period_start__lte=as_of, branch_id__in=branch_ids
        )
        payroll_journal_ids = _with_reversals(_collect_ids(
            [payroll_rows], [("journal_entry_id", "payment_journal_entry_id")]
        ))

        # Only payroll branches whose original posting remains posted at the
        # cutoff contribute a liability; a later reversal must not erase history.
        payroll_reversal_as_of = JournalEntry.objects.filter(
            reversal_of_id=OuterRef("journal_entry_id"),
            entry_date__lte=as_of, status__in=POSTED_STATUSES,
        )
        active_payroll_postings = PayrollPosting.objects.filter(
            cycle__company_id=company_id, cycle__period_end__lte=as_of,
            branch_id__in=branch_ids, journal_entry__entry_date__lte=as_of,
            journal_entry__status=JournalEntry.Status.POSTED,
        ).annotate(_reversed_as_of=Exists(payroll_reversal_as_of)).filter(
            _reversed_as_of=False
        )
        active_posting_for_employee = active_payroll_postings.filter(
            cycle_id=OuterRef("cycle_id"), branch_id=OuterRef("employee__branch_id")
        )
        reversed_payroll_payment = JournalEntry.objects.filter(
            reversal_of_id=OuterRef("payment_journal_entry_id"),
            entry_date__lte=as_of, status__in=POSTED_STATUSES,
        )
        paid_payroll_posting = PayrollPosting.objects.filter(
            cycle_id=OuterRef("cycle_id"),
            branch_id=OuterRef("employee__branch_id"),
            payment_journal_entry__isnull=False,
            payment_journal_entry__entry_date__lte=as_of,
            payment_journal_entry__status=JournalEntry.Status.POSTED,
        ).annotate(
            _payment_reversed=Exists(reversed_payroll_payment)
        ).filter(_payment_reversed=False)
        unpaid_payroll_results = PayrollResult.objects.filter(
            cycle__company_id=company_id,
            cycle__period_end__lte=as_of,
            cycle__status__in=(PayrollCycle.Status.POSTED, PayrollCycle.Status.PAYMENT_PENDING, PayrollCycle.Status.PAID),
            employee__branch_id__in=branch_ids,
        ).annotate(
            _has_active_posting=Exists(active_posting_for_employee),
            _has_posted_payment=Exists(paid_payroll_posting),
        ).filter(_has_active_posting=True, _has_posted_payment=False)
        payroll_employee_totals = grouped_amount(
            unpaid_payroll_results, "employee_id", "net_pay"
        )
        payroll_employee_ids = set(payroll_employee_totals)
        payroll_employee_rows = {
            item["id"]: item for item in Employee.objects.filter(
                company_id=company_id, pk__in=payroll_employee_ids
            ).values("id", "code", "full_name_ar")
        }
        payroll_employee_balances = [{
            "party_id": employee_id,
            "code": payroll_employee_rows[employee_id]["code"],
            "name": payroll_employee_rows[employee_id]["full_name_ar"],
            "balance": str(balance),
        } for employee_id, balance in payroll_employee_totals.items() if employee_id in payroll_employee_rows]
        payroll_employee_balances.sort(key=lambda item: abs(Decimal(item["balance"])), reverse=True)

        # Statutory payroll liabilities are rebuilt from immutable payroll result
        # lines belonging to payroll journals still posted at the requested date.
        # The active posting queryset above is reused for statutory detail.

        statutory_specs = (
            ("employee_insurance", PayrollComponent.Method.EMPLOYEE_INSURANCE,
             "deduction", False, AccountRole.Role.EMPLOYEE_INSURANCE_PAYABLE,
             "التأمينات المستقطعة من الموظفين"),
            ("payroll_tax", PayrollComponent.Method.PROGRESSIVE_TAX,
             "deduction", False, AccountRole.Role.PAYROLL_TAX_PAYABLE,
             "ضريبة المرتبات المستقطعة"),
            ("employer_insurance", PayrollComponent.Method.EMPLOYER_INSURANCE,
             "employer_share", True, AccountRole.Role.EMPLOYER_INSURANCE_PAYABLE,
             "حصة صاحب العمل في التأمينات"),
        )
        statutory_balances = {}
        statutory_extra_accounts = {}
        statutory_unassigned = {}
        for key, method, kind, display_only, role, label in statutory_specs:
            source_lines = PayrollResultLine.objects.filter(
                result__cycle__postings__in=active_payroll_postings,
                result__cycle__postings__branch_id=F("result__employee__branch_id"),
                result__employee__branch_id__in=branch_ids,
                result__cycle__period_end__lte=as_of,
                component__method=method, kind_snapshot=kind,
                display_only=display_only,
            )
            grouped = source_lines.values("component__payable_account_id").annotate(
                total=Sum("amount")
            )
            fallback_account = role_rows.get(role)
            totals_by_account = {}
            unassigned_total = Decimal("0")
            for item in grouped:
                account_id = item["component__payable_account_id"] or (
                    fallback_account.pk if fallback_account else None
                )
                amount = item["total"] or Decimal("0")
                if account_id is None:
                    unassigned_total += amount
                else:
                    totals_by_account[account_id] = totals_by_account.get(
                        account_id, Decimal("0")
                    ) + amount
            statutory_balances[key] = sum(totals_by_account.values(), Decimal("0")) + unassigned_total
            statutory_extra_accounts[key] = set(totals_by_account) - (
                {fallback_account.pk} if fallback_account else set()
            )
            statutory_unassigned[key] = unassigned_total

        stock_links = StockMovementJournalLink.objects.filter(
            movement__company_id=company_id, movement__movement_date__lte=as_of
        ).filter(
            Q(movement__source_warehouse__branch_id__in=branch_ids)
            | Q(movement__destination_warehouse__branch_id__in=branch_ids)
            | (Q(movement__source_warehouse__branch__isnull=True) | Q(movement__destination_warehouse__branch__isnull=True) if include_unassigned_warehouses else Q(pk__in=[]))
        )
        inventory_journal_ids = _with_reversals(_collect_ids(
            [stock_links], [("journal_entry_id",)]
        ))

        def scoped_ids(ids):
            return JournalEntry.objects.filter(
                pk__in=ids, company_id=company_id, branch_id__in=branch_ids,
                entry_date__lte=as_of, status__in=POSTED_STATUSES,
            ).values("pk")

        sales_journal_ids = scoped_ids(sales_journal_ids)
        purchase_journal_ids = scoped_ids(purchase_journal_ids)
        payroll_journal_ids = scoped_ids(payroll_journal_ids)
        inventory_journal_ids = scoped_ids(inventory_journal_ids)

        pending = {
            "sales": SalesInvoice.objects.filter(company_id=company_id, invoice_date__lte=as_of, status=SalesInvoice.Status.DRAFT).filter(sales_invoice_scope).count()
            + CustomerReceipt.objects.filter(company_id=company_id, receipt_date__lte=as_of, status=CustomerReceipt.Status.DRAFT).filter(receipt_scope).count()
            + CustomerRefund.objects.filter(company_id=company_id, refund_date__lte=as_of, status=CustomerRefund.Status.DRAFT, branch_id__in=branch_ids).count()
            + CustomerReceiptBatch.objects.filter(company_id=company_id, branch_id__in=branch_ids, receipt_date__lte=as_of, status=CustomerReceiptBatch.Status.DRAFT).count()
            + SalesReturn.objects.filter(company_id=company_id, return_date__lte=as_of, status=SalesReturn.Status.DRAFT).filter(sales_return_scope).count(),
            "purchasing": PurchaseInvoice.objects.filter(company_id=company_id, invoice_date__lte=as_of, status=PurchaseInvoice.Status.DRAFT).filter(purchase_invoice_scope).count()
            + SupplierPayment.objects.filter(company_id=company_id, payment_date__lte=as_of, status=SupplierPayment.Status.DRAFT).filter(supplier_payment_scope).count()
            + AssetPurchaseInvoice.objects.filter(company_id=company_id, invoice_date__lte=as_of, status=AssetPurchaseInvoice.Status.DRAFT, branch_id__in=branch_ids).count()
            + AssetSupplierPayment.objects.filter(company_id=company_id, payment_date__lte=as_of, status=AssetSupplierPayment.Status.DRAFT, branch_id__in=branch_ids).count()
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
        )
        missing_links = {
            "sales": (
                sales_invoices.filter(journal_entry__isnull=True).count()
                + customer_receipts.filter(journal_entry__isnull=True).count()
                + customer_receipt_batches.filter(journal_entry__isnull=True).count()
                + sales_returns.filter(journal_entry__isnull=True).count()
                + customer_refunds.filter(
                    Q(treasury_transaction__isnull=True)
                    | Q(treasury_transaction__journal_entry__isnull=True)
                ).count()
                + customer_refunds.filter(
                    status=CustomerRefund.Status.REVERSED,
                ).filter(
                    Q(reversal_date__isnull=True)
                    | Q(reversal_date__lte=as_of)
                ).filter(
                    Q(reversal_treasury_transaction__isnull=True)
                    | Q(reversal_treasury_transaction__journal_entry__isnull=True)
                    | Q(reversal_treasury_transaction__status__in=(
                        "draft", "void", "reversed",
                    ))
                    | ~Q(reversal_treasury_transaction__journal_entry__status=JournalEntry.Status.POSTED)
                    | Q(reversal_journal_entry__isnull=True)
                    | ~Q(reversal_journal_entry__status=JournalEntry.Status.POSTED)
                ).count()
            ),
            "purchasing": purchase_invoices.filter(journal_entry__isnull=True).count() + supplier_payments.filter(journal_entry__isnull=True).count() + supplier_returns.filter(journal_entry__isnull=True).count() + supplier_refunds.filter(journal_entry__isnull=True).count(),
            "inventory": posted_stock_movements.filter(journal_links__isnull=True).count(),
            "payroll": PayrollPosting.objects.filter(
                cycle__company_id=company_id, cycle__period_start__lte=as_of,
                branch_id__in=branch_ids, journal_entry__isnull=True,
            ).count(),
        }
        missing_links_total = sum(missing_links.values())

        def make_row(key, label, roles, nature, linked_ids, pending_count=0,
                     document_balance=None, party_balances=None, require_partial=False,
                     extra_account_ids=()):
            accounts = [role_rows[role] for role in roles if role in role_rows]
            if extra_account_ids:
                extra_accounts = ChartOfAccount.objects.filter(
                    company_id=company_id, pk__in=extra_account_ids
                )
                known_ids = {account.pk for account in accounts}
                accounts.extend(account for account in extra_accounts if account.pk not in known_ids)
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
                linked_journal_balance, gl_balance = sub_credit - sub_debit, gl_credit - gl_debit
            else:
                linked_journal_balance, gl_balance = sub_debit - sub_credit, gl_debit - gl_credit
            subledger_balance = document_balance if document_balance is not None else linked_journal_balance
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
            elif require_partial:
                # Keep the accounting limitation visible even when draft payroll
                # documents also exist; pending_count remains available separately.
                status = "partial_reconciliation"
            elif pending_count:
                status = "pending_documents"
            else:
                status = "matched"
            return {
                "key": key, "label": label, "status": status,
                "party_balances": (party_balances or [])[:100],
                "party_count": len(party_balances or []),
                "accounts": [{"id": a.pk, "code": a.code, "name": a.name_ar} for a in accounts],
                "subledger_balance": str(subledger_balance), "gl_balance": str(gl_balance),
                "difference": str(difference), "source_journal_count": source_count,
                "pending_documents_count": pending_count, "unmatched_entry_count": unmatched_count,
                "unmatched_entries": [{
                    "journal_id": line.entry_id, "entry_number": line.entry.entry_number,
                    "entry_date": line.entry.entry_date.isoformat(), "description": line.entry.description,
                    "account_code": line.account.code, "debit": str(line.debit), "credit": str(line.credit),
                } for line in unmatched],
                "detail": (
                    "الرصيد محسوب من الفواتير والتحصيلات/المدفوعات والمرتجعات وإعادة تقييم العملة، ومجمع حسب الطرف. تعرض الشاشة أعلى 100 طرف حسب قيمة الرصيد."
                    if document_balance is not None else
                    "يعرض صافي مستحقات الموظفين غير المصروفة حسب الموظف؛ أما التأمينات والضرائب وحصة صاحب العمل فتُقارن بقيود الترحيل لعدم وجود دفتر فرعي مستقل للجهات المستحقة."
                    if key == "payroll" else
                    "المقارنة مبنية على القيود المرحلة المرتبطة بمستندات الوحدة مقابل كامل حركة الحساب؛ هذه مقارنة جزئية وليست رصيدًا تفصيليًا على مستوى الطرف."
                ),
            }

        rows = [
            make_row("customers", "حسابات العملاء", (AccountRole.Role.ACCOUNTS_RECEIVABLE,), "asset", sales_journal_ids, pending["sales"], customer_subledger_total, customer_party_balances),
            make_row("suppliers", "حسابات الموردين", (AccountRole.Role.ACCOUNTS_PAYABLE,), "liability", purchase_journal_ids, pending["purchasing"], supplier_subledger_total, supplier_party_balances),
            make_row("payroll", "صافي مستحقات الموظفين", (
                AccountRole.Role.PAYROLL_PAYABLE,
            ), "liability", payroll_journal_ids, pending["payroll"],
            document_balance=sum((Decimal(item["balance"]) for item in payroll_employee_balances), Decimal("0")),
            party_balances=payroll_employee_balances),
        ]
        for key, method, kind, display_only, role, label in statutory_specs:
            if (
                role not in role_rows
                and not statutory_extra_accounts[key]
                and statutory_balances[key] == 0
                and statutory_unassigned[key] == 0
            ):
                continue
            row = make_row(
                f"payroll_{key}", label, (role,), "liability", payroll_journal_ids,
                document_balance=statutory_balances[key], require_partial=True,
                extra_account_ids=statutory_extra_accounts[key],
            )
            if statutory_unassigned[key]:
                row["status"] = "not_configured"
                row["detail"] = (
                    "توجد مبالغ مستحقة من تفاصيل المرتبات دون حساب التزام محدد؛ "
                    "لم يتم إنشاء أو تعديل أي قيد."
                )
                row["unassigned_amount"] = str(statutory_unassigned[key])
            else:
                row["detail"] = (
                    "الرصيد محسوب من تفاصيل بنود المرتبات المرحلة حسب الموظف والحساب الدائن "
                    "المستخدم فعليًا. لا تتم تسوية أو سداد هذه الالتزامات من هذا التقرير؛ "
                    "وتظل المطابقة جزئية لحين وجود مستندات سداد مستقلة."
                )
            rows.append(row)

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
            current_date = timezone.localdate()
            replayed_current = _replayed_inventory_value(company_id, branch_ids, current_date, include_unassigned_warehouses) if as_of < current_date else None
            if as_of == current_date:
                difference = gl_balance - stock_total
                inventory_status = "difference" if difference != 0 else ("pending_documents" if pending["inventory"] else "matched")
                inventory_detail = "مطابقة رصيد المخزون الحالي مع الأستاذ العام. كما يتم فحص اكتمال سجل تكلفة الحركات."
            elif as_of < current_date and replayed_current == stock_total:
                historical_stock_total = _replayed_inventory_value(company_id, branch_ids, as_of, include_unassigned_warehouses)
                stock_total = historical_stock_total
                difference = gl_balance - historical_stock_total
                inventory_status = "difference" if difference != 0 else ("pending_documents" if pending["inventory"] else "matched")
                inventory_detail = "تمت إعادة بناء قيمة المخزون التاريخية من تكاليف حركات المخزون المرحلة، بعد التحقق من تطابق إعادة البناء حتى تاريخ اليوم مع رصيد المخزون الحالي."
            else:
                difference = None
                inventory_status = "historical_snapshot_unavailable"
                inventory_detail = "تعذر اعتماد إعادة البناء التاريخية: سجل تكلفة الحركات لا يطابق رصيد المخزون الحالي أو التاريخ المطلوب مستقبلي."
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
            "is_reconciled": all(row["status"] == "matched" for row in rows) and pending_total == 0 and missing_links_total == 0,
            "note": "تقرير قراءة فقط ولا ينشئ أو يعدل قيودًا. أرصدة العملاء والموردين محسوبة من المستندات المرحلة ومجمعة حسب الطرف، مع استخدام القيمة الدفترية الفعلية للتحصيلات والمدفوعات الأجنبية. يفصل قسم المرتبات صافي مستحقات الموظفين عن تأمينات الموظف وضريبة المرتبات وحصة صاحب العمل، وتُحسب الالتزامات النظامية من تفاصيل بنود دورات المرتبات ذات القيود المرحلة غير المعكوسة حتى تاريخ التقرير. تظل مطابقة الالتزامات النظامية جزئية لعدم وجود مستندات سداد مستقلة لها داخل النظام. المستندات المرحلة بلا قيود تُعرض كتحذير. المخزون يُعاد بناؤه تاريخيًا من سجل تكاليف الحركات فقط إذا تطابقت إعادة البناء حتى اليوم مع الرصيد الحالي.",
        })
