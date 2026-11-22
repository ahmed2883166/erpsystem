"""Atomic foreign-currency revaluation for open customer and supplier invoices."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from accounting.fx_arap import to_base_amount
from accounting.models import (
    AccountRole, FxSubledgerRevaluation, JournalEntry, JournalLine,
)
from accounting.services import get_account_for_role, post_journal
from companies.models import ExchangeRate, FiscalYear
from purchasing.models import PurchaseInvoice, SupplierPayment
from sales.models import CustomerReceipt, SalesInvoice


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
        ).order_by("-revaluation_date", "-pk").first()
    else:
        settlements = SupplierPayment.objects.filter(
            invoice=invoice, status=SupplierPayment.Status.POSTED,
            payment_date__lte=as_of,
        )
        latest = FxSubledgerRevaluation.objects.filter(
            purchase_invoice=invoice, revaluation_date__lte=as_of,
        ).order_by("-revaluation_date", "-pk").first()

    foreign_used = _sum(settlements, "transaction_amount")
    historical_used = _sum(settlements, "historical_carrying_base_amount")
    if historical_used == 0:
        historical_used = _sum(settlements, "carrying_base_amount")
    remaining_foreign = (invoice.transaction_grand_total or Decimal("0")) - foreign_used
    historical_remaining = invoice.grand_total - historical_used

    if latest:
        date_field = "receipt_date" if direction == "receivable" else "payment_date"
        after_latest = settlements.filter(**{f"{date_field}__gt": latest.revaluation_date})
        current_used_after = _sum(after_latest, "carrying_base_amount")
        current_remaining = latest.new_carrying_amount - current_used_after
    else:
        current_remaining = invoice.grand_total - _sum(settlements, "carrying_base_amount")

    return (
        remaining_foreign.quantize(Decimal("0.0001")),
        historical_remaining.quantize(Decimal("0.0001")),
        current_remaining.quantize(Decimal("0.0001")),
    )


def _branch_and_year(invoice, direction, entry_date):
    if direction == "receivable":
        from sales.services import _branch_and_year as resolver
        return resolver(invoice.delivery, entry_date)
    from purchasing.services import _branch_and_year as resolver
    return resolver(invoice.receipt, entry_date)


@transaction.atomic
def revalue_fx_invoice(*, direction, invoice_id, revaluation_date, rate_date, rate, user):
    """Revalue the outstanding AR/AP amount and post unrealized FX gain/loss."""
    if direction == "receivable":
        invoice = SalesInvoice.objects.select_for_update().select_related(
            "company", "transaction_currency", "delivery__warehouse",
        ).get(pk=invoice_id)
        invoice_model = SalesInvoice
    elif direction == "payable":
        invoice = PurchaseInvoice.objects.select_for_update().select_related(
            "company", "transaction_currency", "receipt__warehouse",
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
        journal = post_journal(journal.pk)

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
