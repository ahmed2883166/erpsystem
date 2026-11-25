"""Atomic foreign-currency revaluation for open customer and supplier invoices."""
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from rest_framework.exceptions import ValidationError

from accounting.fx_arap import to_base_amount
from accounting.models import (
    AccountRole, FxSubledgerRevaluation, JournalEntry, JournalLine,
)
from accounting.services import get_account_for_role, post_journal
from companies.models import ExchangeRate
from purchasing.models import PurchaseInvoice, SupplierPayment, SupplierReturn
from sales.models import CustomerReceipt, SalesInvoice, SalesReturn


def _sum(queryset, field):
    return queryset.aggregate(value=Sum(field))["value"] or Decimal("0")


def open_fx_invoice_balances(invoice, *, direction, as_of):
    """Return open foreign balance, historical carrying and current book carrying."""
    if direction == "receivable":
        settlements = CustomerReceipt.objects.filter(
            invoice=invoice, status=CustomerReceipt.Status.POSTED,
            receipt_date__lte=as_of,
        )
        latest = FxSubledgerRevaluation.objects.filter(
            sales_invoice=invoice, revaluation_date__lte=as_of,
        ).filter(Q(journal_entry__isnull=True) | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)).order_by("-revaluation_date", "-pk").first()
    else:
        settlements = SupplierPayment.objects.filter(
            invoice=invoice, status=SupplierPayment.Status.POSTED,
            payment_date__lte=as_of,
        )
        latest = FxSubledgerRevaluation.objects.filter(
            purchase_invoice=invoice, revaluation_date__lte=as_of,
        ).filter(Q(journal_entry__isnull=True) | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)).order_by("-revaluation_date", "-pk").first()

    foreign_used = _sum(settlements, "transaction_amount")
    historical_used = _sum(settlements, "historical_carrying_base_amount")
    if historical_used == 0:
        historical_used = _sum(settlements, "carrying_base_amount")
    returned_foreign = Decimal("0")
    returned_base = Decimal("0")
    if direction == "payable":
        active_returns = SupplierReturn.objects.filter(
            invoice=invoice,
            return_date__lte=as_of,
        ).filter(
            Q(status=SupplierReturn.Status.POSTED)
            | Q(status=SupplierReturn.Status.REVERSED, reversal_date__gt=as_of)
        )
    else:
        active_returns = SalesReturn.objects.filter(
            invoice=invoice,
            return_date__lte=as_of,
        ).filter(
            Q(status=SalesReturn.Status.POSTED)
            | Q(status=SalesReturn.Status.REVERSED, reversal_date__gt=as_of)
        )
    if active_returns.filter(transaction_grand_total__isnull=True).exists():
        message = (
            "يوجد مردود مبيعات قديم بلا لقطة عملة أجنبية؛ راجع بيانات المردود قبل تحصيل الفاتورة."
            if direction == "receivable"
            else "يوجد مرتجع مورد بلا لقطة عملة أجنبية؛ راجع بيانات المرتجع قبل سداد الفاتورة."
        )
        raise ValidationError(message)
    returned_foreign = _sum(active_returns, "transaction_grand_total")
    returned_base = _sum(active_returns, "grand_total")
    remaining_foreign = (invoice.transaction_grand_total or Decimal("0")) - foreign_used - returned_foreign
    historical_remaining = invoice.grand_total - historical_used - returned_base

    if latest:
        date_field = "receipt_date" if direction == "receivable" else "payment_date"
        after_latest = settlements.filter(**{f"{date_field}__gt": latest.revaluation_date})
        current_used_after = _sum(after_latest, "carrying_base_amount")
        returned_after_latest = active_returns.filter(
            return_date__gt=latest.revaluation_date,
        )
        # Returns are already posted in company base currency at their own
        # document-date rate. Repricing their foreign amount at the prior
        # invoice revaluation rate overstates the remaining carrying balance.
        returned_current = _sum(returned_after_latest, "grand_total")
        reversed_before_latest = Decimal("0")
        if direction == "receivable":
            reversed_before_latest = _sum(
                SalesReturn.objects.filter(
                    invoice=invoice,
                    status=SalesReturn.Status.REVERSED,
                    return_date__lte=latest.revaluation_date,
                    reversal_date__gt=latest.revaluation_date,
                    reversal_date__lte=as_of,
                ),
                "transaction_grand_total",
            ) * latest.rate
        else:
            reversed_before_latest = _sum(
                SupplierReturn.objects.filter(
                    invoice=invoice,
                    status=SupplierReturn.Status.REVERSED,
                    return_date__lte=latest.revaluation_date,
                    reversal_date__gt=latest.revaluation_date,
                    reversal_date__lte=as_of,
                ),
                "transaction_grand_total",
            ) * latest.rate
        current_remaining = (
            latest.new_carrying_amount - current_used_after
            - returned_current + reversed_before_latest
        )
    else:
        current_remaining = invoice.grand_total - _sum(settlements, "carrying_base_amount") - returned_base

    return (
        remaining_foreign.quantize(Decimal("0.0001")),
        historical_remaining.quantize(Decimal("0.0001")),
        current_remaining.quantize(Decimal("0.0001")),
    )


def _branch_and_year(invoice, direction, entry_date):
    if direction == "receivable":
        from sales.services import _branch_and_year_for_invoice as resolver
        return resolver(invoice, entry_date)
    from purchasing.services import _branch_and_year_for_invoice
    return _branch_and_year_for_invoice(invoice, entry_date)


@transaction.atomic
def revalue_fx_invoice(*, direction, invoice_id, revaluation_date, rate_date, rate, user):
    """Revalue the outstanding AR/AP amount and post unrealized FX gain/loss."""
    if direction == "receivable":
        invoice = SalesInvoice.objects.select_for_update(of=("self",)).select_related(
            "company", "transaction_currency", "delivery__warehouse", "branch",
        ).get(pk=invoice_id)
        invoice_model = SalesInvoice
    elif direction == "payable":
        invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).select_related(
            "company", "transaction_currency", "branch", "receipt__warehouse",
        ).get(pk=invoice_id)
        invoice_model = PurchaseInvoice
    else:
        raise ValidationError("نوع الذمة يجب أن يكون receivable أو payable.")

    if invoice.status != invoice_model.Status.POSTED:
        raise ValidationError("لا يمكن إعادة تقييم فاتورة غير مرحلة.")
    if not invoice.transaction_currency_id or not invoice.transaction_grand_total:
        raise ValidationError("إعادة التقييم متاحة للفواتير ذات العملة الأجنبية فقط.")
    if revaluation_date < invoice.invoice_date or rate_date > revaluation_date:
        raise ValidationError("تواريخ إعادة التقييم وسعر الصرف غير صحيحة.")
    if FxSubledgerRevaluation.objects.filter(
        **({"sales_invoice": invoice} if direction == "receivable" else {"purchase_invoice": invoice}),
        revaluation_date=revaluation_date,
    ).exists():
        raise ValidationError("تمت إعادة تقييم هذه الفاتورة في التاريخ المحدد بالفعل.")

    if direction == "receivable":
        later_settlements = CustomerReceipt.objects.filter(
            invoice=invoice, status=CustomerReceipt.Status.POSTED,
            receipt_date__gte=revaluation_date,
        ).exists()
        later_revaluations = FxSubledgerRevaluation.objects.filter(
            sales_invoice=invoice, revaluation_date__gt=revaluation_date,
        ).filter(Q(journal_entry__isnull=True) | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)).exists()
    else:
        later_settlements = SupplierPayment.objects.filter(
            invoice=invoice, status=SupplierPayment.Status.POSTED,
            payment_date__gte=revaluation_date,
        ).exists()
        later_revaluations = FxSubledgerRevaluation.objects.filter(
            purchase_invoice=invoice, revaluation_date__gt=revaluation_date,
        ).filter(Q(journal_entry__isnull=True) | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)).exists()
        later_returns = SupplierReturn.objects.filter(
            invoice=invoice,
            status__in=(SupplierReturn.Status.POSTED, SupplierReturn.Status.REVERSED),
            return_date__gte=revaluation_date,
        ).exists()
    if later_settlements or later_revaluations or (direction == "payable" and later_returns):
        raise ValidationError("لا يمكن إعادة تقييم تاريخ يسبق تسوية أو مرتجعًا أو إعادة تقييم مرحّلة.")

    approved = ExchangeRate.objects.filter(
        company=invoice.company, currency=invoice.transaction_currency,
        rate_date=rate_date, rate=rate,
    ).first()
    if not approved:
        raise ValidationError("سعر الصرف لا يطابق سعرًا معتمدًا للشركة والعملة والتاريخ.")

    remaining_foreign, historical_remaining, previous_carrying = open_fx_invoice_balances(
        invoice, direction=direction, as_of=revaluation_date,
    )
    if remaining_foreign <= 0 or historical_remaining <= 0 or previous_carrying <= 0:
        raise ValidationError("لا يوجد رصيد أجنبي مفتوح صالح لإعادة التقييم.")
    new_carrying = to_base_amount(remaining_foreign, approved.rate)
    difference = (new_carrying - previous_carrying).quantize(Decimal("0.0001"))

    journal = None
    if difference:
        branch, year = _branch_and_year(invoice, direction, revaluation_date)
        if direction == "receivable":
            balance_account = get_account_for_role(invoice.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
            if difference > 0:
                offset_account = get_account_for_role(invoice.company, AccountRole.Role.UNREALIZED_FX_GAIN)
                debit_account, credit_account, amount = balance_account, offset_account, difference
            else:
                offset_account = get_account_for_role(invoice.company, AccountRole.Role.UNREALIZED_FX_LOSS)
                debit_account, credit_account, amount = offset_account, balance_account, -difference
        else:
            balance_account = get_account_for_role(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE)
            if difference > 0:
                offset_account = get_account_for_role(invoice.company, AccountRole.Role.UNREALIZED_FX_LOSS)
                debit_account, credit_account, amount = offset_account, balance_account, difference
            else:
                offset_account = get_account_for_role(invoice.company, AccountRole.Role.UNREALIZED_FX_GAIN)
                debit_account, credit_account, amount = balance_account, offset_account, -difference
        if debit_account.pk == credit_account.pk:
            raise ValidationError("حساب الذمم وحساب فروق العملة يجب أن يكونا مختلفين.")
        prefix = "FX-AR" if direction == "receivable" else "FX-AP"
        journal = JournalEntry.objects.create(
            company=invoice.company, branch=branch, fiscal_year=year,
            entry_number=f"{prefix}-{invoice.pk}-{revaluation_date:%Y%m%d}",
            entry_date=revaluation_date,
            description=f"إعادة تقييم عملة فاتورة {invoice.invoice_number} حتى {revaluation_date}",
            source_type=JournalEntry.Source.FX_REVALUATION, created_by=user,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=journal, account=debit_account,
                        description="إعادة تقييم رصيد الذمم بالعملة الأجنبية",
                        debit=amount, credit=0),
            JournalLine(entry=journal, account=credit_account,
                        description="فرق إعادة تقييم العملة غير المحقق",
                        debit=0, credit=amount),
        ])
        try:
            journal = post_journal(journal.pk, actor=user)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc

    record = FxSubledgerRevaluation.objects.create(
        company=invoice.company,
        sales_invoice=invoice if direction == "receivable" else None,
        purchase_invoice=invoice if direction == "payable" else None,
        revaluation_date=revaluation_date, rate_date=rate_date, rate=approved.rate,
        foreign_balance=remaining_foreign,
        previous_carrying_amount=previous_carrying,
        new_carrying_amount=new_carrying,
        difference=difference, journal_entry=journal, created_by=user,
    )
    return record
