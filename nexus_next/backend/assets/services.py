"""خدمات اقتناء الأصول الثابتة واحتساب الإهلاك والتصرف والبيع مع القيود المحاسبية."""

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError, ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone, dateparse
from django.db.models import Sum

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal
from companies.models import FiscalYear
from .models import FixedAsset, AssetDepreciation, AssetPurchaseInvoice, AssetSupplierPayment


MONEY = Decimal("0.0001")


def _post_entry(asset, fiscal_year, entry_number, entry_date, description, lines, user=None):
    """إنشاء وترحيل قيد محاسبي مرتبط بحركة الأصل الثابت."""
    entry = JournalEntry.objects.create(
        company=asset.company, branch=asset.branch, fiscal_year=fiscal_year,
        entry_number=entry_number, entry_date=entry_date, description=description,
        source_type=JournalEntry.Source.ASSETS, created_by=user,
    )
    JournalLine.objects.bulk_create([JournalLine(entry=entry, **line) for line in lines])
    return post_journal(entry.pk, actor=user)


@transaction.atomic
def acquire_asset(asset_id, fiscal_year_id, user=None):
    """إثبات اقتناء الأصل وتكلفته في الحسابات والسنة المالية المحددة."""
    asset = FixedAsset.objects.select_for_update().select_related(
        "company", "branch", "asset_account", "acquisition_counter_account"
    ).get(pk=asset_id)
    if asset.acquisition_journal_id:
        raise ValidationError("تم تسجيل قيد اقتناء الأصل بالفعل.")
    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.") from None
    if not (year.start_date <= asset.acquisition_date <= year.end_date):
        raise ValidationError("تاريخ الاقتناء خارج السنة المالية المفتوحة.")

    from treasury.models import TreasuryAccount, TreasuryTransaction
    treasury_account = TreasuryAccount.objects.filter(
        company=asset.company, account=asset.acquisition_counter_account, is_active=True
    ).first()
    if treasury_account:
        if treasury_account.branch_id and treasury_account.branch_id != asset.branch_id:
            raise ValidationError("حساب الخزينة المرتبط بمقابل الاقتناء لا يتبع فرع الأصل.")
        if asset.acquisition_treasury_transaction_id:
            treasury_doc = TreasuryTransaction.objects.select_for_update().get(
                pk=asset.acquisition_treasury_transaction_id
            )
        else:
            treasury_doc = TreasuryTransaction.objects.create(
                company=asset.company, branch=asset.branch, fiscal_year=year,
                source_account=treasury_account, counter_account=asset.asset_account,
                kind=TreasuryTransaction.Kind.PAYMENT,
                document_number=f"FA-ACQ-{asset.pk}", transaction_date=asset.acquisition_date,
                amount=asset.original_cost, description=f"سداد تكلفة اقتناء الأصل: {asset.name_ar}",
                cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING,
                created_by=user,
            )
            asset.acquisition_treasury_transaction = treasury_doc
            asset.save(update_fields=("acquisition_treasury_transaction",))
            from approvals.models import FinancialApprovalRequest
            from approvals.services import refresh_financial_approval
            approval = refresh_financial_approval(
                FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT, treasury_doc, user
            )
            if approval:
                return asset

        from treasury.services import post_treasury_transaction
        treasury_doc = post_treasury_transaction(treasury_doc.pk, user)
        asset.acquisition_journal = treasury_doc.journal_entry
        asset.save(update_fields=("acquisition_journal",))
        return asset

    entry = _post_entry(
        asset, year, f"FA-ACQ-{asset.pk}", asset.acquisition_date,
        f"إثبات اقتناء الأصل: {asset.name_ar}", [
            {"account": asset.asset_account, "debit": asset.original_cost, "credit": 0, "description": asset.name_ar},
            {"account": asset.acquisition_counter_account, "debit": 0, "credit": asset.original_cost, "description": asset.name_ar},
        ], user,
    )
    asset.acquisition_journal = entry
    asset.save(update_fields=("acquisition_journal",))
    return asset


@transaction.atomic
def depreciate_asset(asset_id, period_date, fiscal_year_id, user=None):
    """احتساب وترحيل إهلاك الأصل عن الفترة حتى تاريخها المحدد."""
    if isinstance(period_date, str):
        period_date = dateparse.parse_date(period_date)
    if not period_date:
        raise ValidationError("أدخل تاريخ فترة إهلاك صحيحًا.")
    asset = FixedAsset.objects.select_for_update().get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("الأصل غير نشط أو لم يتم تسجيل اقتنائه.")
    if period_date < asset.depreciation_start_date:
        raise ValidationError("تاريخ الإهلاك يسبق بداية الإهلاك المحددة.")
    if AssetDepreciation.objects.filter(asset=asset, period_date__year=period_date.year, period_date__month=period_date.month).exists():
        raise ValidationError("تم تسجيل إهلاك هذا الأصل لهذا الشهر من قبل.")
    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.") from None
    if not (year.start_date <= period_date <= year.end_date):
        raise ValidationError("تاريخ الإهلاك خارج السنة المالية المفتوحة.")
    last_depreciation = asset.depreciations.order_by("-period_date", "-pk").first()
    previous_date = last_depreciation.period_date if last_depreciation else asset.depreciation_start_date - timedelta(days=1)
    elapsed_days = (period_date - previous_date).days
    if elapsed_days <= 0:
        raise ValidationError("تاريخ الإهلاك يجب أن يكون بعد آخر فترة إهلاك مسجلة.")
    posted_total = asset.depreciations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = asset.depreciable_amount - posted_total
    if remaining <= 0:
        raise ValidationError("تم استهلاك كامل القيمة القابلة للإهلاك.")
    daily_rate = asset.depreciable_amount / Decimal(asset.useful_life_months * 30)
    amount = min((daily_rate * Decimal(elapsed_days)).quantize(MONEY, rounding=ROUND_HALF_UP), remaining)
    entry = _post_entry(asset, year, f"FA-DEP-{asset.pk}-{period_date:%Y%m}", period_date,
        f"إهلاك الأصل: {asset.name_ar} - {period_date:%Y-%m}", [
            {"account": asset.depreciation_expense_account, "debit": amount, "credit": 0, "description": asset.name_ar},
            {"account": asset.accumulated_depreciation_account, "debit": 0, "credit": amount, "description": asset.name_ar},
        ], user)
    AssetDepreciation.objects.create(asset=asset, period_date=period_date, amount=amount, journal_entry=entry)
    return amount


@transaction.atomic
def dispose_asset(asset_id, disposal_date, proceeds, cash_account_id, fiscal_year_id, user=None):
    """إثبات بيع الأصل أو استبعاده ومعالجة النقدية ومجمع الإهلاك والربح أو الخسارة."""
    from treasury.models import TreasuryAccount, TreasuryTransaction

    if isinstance(disposal_date, str):
        disposal_date = dateparse.parse_date(disposal_date)
    if not disposal_date:
        raise ValidationError("أدخل تاريخ استبعاد صحيحًا.")
    try:
        proceeds = Decimal(str(proceeds))
    except Exception as exc:
        raise ValidationError("قيمة البيع غير صحيحة.") from exc
    if proceeds < 0:
        raise ValidationError("قيمة البيع لا يمكن أن تكون سالبة.")

    asset = FixedAsset.objects.select_for_update(of=("self",)).select_related(
        "company", "branch", "asset_account", "accumulated_depreciation_account",
        "depreciation_expense_account", "disposal_gain_loss_account", "disposal_loss_account"
    ).get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("الأصل غير نشط أو لم يتم إثبات اقتنائه.")
    if disposal_date < asset.depreciation_start_date:
        raise ValidationError("تاريخ الاستبعاد يسبق بداية الإهلاك.")
    if AssetDepreciation.objects.filter(asset=asset, period_date__gt=disposal_date).exists():
        raise ValidationError("توجد قيود إهلاك بتاريخ لاحق لتاريخ الاستبعاد؛ راجعها أولًا.")

    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.") from None
    if not (year.start_date <= disposal_date <= year.end_date):
        raise ValidationError("تاريخ الاستبعاد خارج السنة المالية المفتوحة.")

    treasury_account = None
    if proceeds > 0:
        try:
            treasury_account = TreasuryAccount.objects.select_related("account").get(pk=cash_account_id, company=asset.company, is_active=True)
        except (TreasuryAccount.DoesNotExist, TypeError, ValueError):
            raise ValidationError("اختر حساب بنك أو خزينة نشطًا لمتحصلات البيع.") from None
        cash_account = treasury_account.account
        if not cash_account.is_active or cash_account.is_group or not cash_account.allow_posting or cash_account.nature != "asset":
            raise ValidationError("الحساب المحاسبي المرتبط بالخزينة غير صالح.")
        if treasury_account.branch_id and treasury_account.branch_id != asset.branch_id:
            raise ValidationError("الخزينة المختارة لا تتبع فرع الأصل.")

    last_depreciation = asset.depreciations.order_by("-period_date", "-pk").first()
    previous_date = last_depreciation.period_date if last_depreciation else asset.depreciation_start_date - timedelta(days=1)
    elapsed_days = max(0, (disposal_date - previous_date).days)
    posted_total = asset.depreciations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = max(Decimal("0"), asset.depreciable_amount - posted_total)

    if elapsed_days and remaining:
        daily_rate = asset.depreciable_amount / Decimal(asset.useful_life_months * 30)
        partial_amount = min(remaining, (daily_rate * Decimal(elapsed_days)).quantize(MONEY, rounding=ROUND_HALF_UP))
        if partial_amount > 0:
            partial_entry = _post_entry(
                asset, year, f"FA-DEP-PART-{asset.pk}-{disposal_date:%Y%m%d}", disposal_date,
                f"إهلاك حتى تاريخ استبعاد الأصل: {asset.name_ar}",
                [
                    {"account": asset.depreciation_expense_account, "debit": partial_amount, "credit": 0, "description": asset.name_ar},
                    {"account": asset.accumulated_depreciation_account, "debit": 0, "credit": partial_amount, "description": asset.name_ar},
                ], user,
            )
            AssetDepreciation.objects.create(
                asset=asset, period_date=disposal_date, amount=partial_amount,
                is_partial_period=True, journal_entry=partial_entry,
            )

    accumulated = asset.depreciations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    accumulated = min(accumulated, asset.depreciable_amount)
    net_book_value = asset.original_cost - accumulated
    gain = max(Decimal("0"), proceeds - net_book_value)
    loss = max(Decimal("0"), net_book_value - proceeds)
    gain_account = asset.disposal_gain_loss_account
    loss_account = asset.disposal_loss_account
    if gain > 0 and gain_account.nature != "revenue":
        raise ValidationError("حساب أرباح الاستبعاد يجب أن يكون من طبيعة الإيرادات.")
    if loss > 0:
        if loss_account is None and gain_account.nature == "expense":
            loss_account = gain_account
        if loss_account is None or loss_account.nature != "expense":
            raise ValidationError("حدد حساب خسائر الاستبعاد من طبيعة المصروفات.")
    settlement_counter = gain_account if gain > 0 else loss_account if loss > 0 else asset.asset_account

    lines = [
        {"account": asset.accumulated_depreciation_account, "debit": accumulated, "credit": 0, "description": f"إقفال مجمع إهلاك {asset.name_ar}"},
        {"account": asset.asset_account, "debit": 0, "credit": asset.original_cost, "description": f"استبعاد تكلفة {asset.name_ar}"},
    ]
    if proceeds > 0:
        lines.insert(0, {"account": cash_account, "debit": proceeds, "credit": 0, "description": f"متحصلات استبعاد {asset.name_ar}"})
    if gain > 0:
        lines.append({"account": gain_account, "debit": 0, "credit": gain, "description": f"ربح استبعاد {asset.name_ar}"})
    elif loss > 0:
        lines.append({"account": loss_account, "debit": loss, "credit": 0, "description": f"خسارة استبعاد {asset.name_ar}"})

    entry = _post_entry(
        asset, year, f"FA-DISP-{asset.pk}", disposal_date,
        f"بيع / استبعاد الأصل: {asset.name_ar}", lines, user,
    )
    treasury_doc = None
    if proceeds > 0:
        treasury_doc = TreasuryTransaction.objects.create(
            company=asset.company, branch=asset.branch, fiscal_year=year, source_account=treasury_account,
            counter_account=settlement_counter, kind=TreasuryTransaction.Kind.RECEIPT,
            status=TreasuryTransaction.Status.POSTED, document_number=f"FA-DISP-{asset.pk}",
            transaction_date=disposal_date, amount=proceeds,
            description=f"متحصلات بيع / استبعاد الأصل: {asset.name_ar}",
            cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING,
            journal_entry=entry, created_by=user, posted_at=timezone.now(),
        )
    asset.status = FixedAsset.Status.DISPOSED
    asset.disposed_at = disposal_date
    asset.disposal_proceeds = proceeds
    asset.disposal_journal = entry
    asset.disposal_treasury_transaction = treasury_doc
    asset.save(update_fields=("status", "disposed_at", "disposal_proceeds", "disposal_journal", "disposal_treasury_transaction"))
    return {
        "asset_id": asset.pk, "disposal_journal": entry.pk, "treasury_transaction": treasury_doc.pk if treasury_doc else None, "accumulated_depreciation": accumulated,
        "net_book_value": net_book_value, "proceeds": proceeds, "gain": gain, "loss": loss,
    }


@transaction.atomic
def post_asset_purchase_invoice(invoice_id, user=None):
    """ترحيل فاتورة شراء أصل مباشر: تكلفة الأصل والضريبة القابلة للاسترداد مقابل حساب الموردين."""
    invoice = AssetPurchaseInvoice.objects.select_for_update().select_related(
        "company", "branch", "asset", "supplier", "fiscal_year", "tax_code",
    ).get(pk=invoice_id)
    if invoice.status != AssetPurchaseInvoice.Status.DRAFT:
        raise ValidationError("فاتورة شراء الأصل مرحلة بالفعل.")
    if invoice.fiscal_year.status != FiscalYear.Status.OPEN or not (
        invoice.fiscal_year.start_date <= invoice.invoice_date <= invoice.fiscal_year.end_date
    ):
        raise ValidationError("تاريخ الفاتورة يجب أن يقع داخل سنة مالية مفتوحة.")
    if not invoice.supplier.is_active:
        raise ValidationError("لا يمكن ترحيل فاتورة مورد موقوف.")
    asset = FixedAsset.objects.select_for_update(of=("self",)).get(pk=invoice.asset_id)
    if asset.acquisition_journal_id or asset.status != FixedAsset.Status.ACTIVE:
        raise ValidationError("الأصل مثبت محاسبيًا أو مستبعد بالفعل.")
    if asset.company_id != invoice.company_id or asset.branch_id != invoice.branch_id:
        raise ValidationError("بيانات الأصل لا تطابق فاتورة المورد.")
    expected_cost = invoice.net_amount + invoice.nonrecoverable_tax_amount
    if asset.salvage_value > expected_cost:
        raise ValidationError("القيمة التخريدية تتجاوز تكلفة الأصل بعد معالجة الضريبة.")
    if invoice.tax_code_id:
        code = invoice.tax_code
        if (
            not code.is_active or code.company_id != invoice.company_id
            or code.country != invoice.company.country
            or code.category == "reverse_charge"
            or code.effective_from > invoice.invoice_date
            or (code.effective_to and code.effective_to < invoice.invoice_date)
        ):
            raise ValidationError("كود الضريبة غير صالح لهذه الشركة أو تاريخ الفاتورة.")
        expected_recoverable = invoice.tax_amount if code.input_tax_recoverable else Decimal("0")
        expected_nonrecoverable = invoice.tax_amount - expected_recoverable
        if (
            invoice.recoverable_tax_amount != expected_recoverable
            or invoice.nonrecoverable_tax_amount != expected_nonrecoverable
        ):
            raise ValidationError("توزيع الضريبة لا يطابق قابلية الاسترداد في كود الضريبة.")
    elif invoice.tax_amount or invoice.recoverable_tax_amount or invoice.nonrecoverable_tax_amount:
        raise ValidationError("لا يمكن تسجيل ضريبة دون كود ضريبي معتمد.")

    payable = get_account_for_role(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    vat_input = (
        get_account_for_role(invoice.company, AccountRole.Role.VAT_INPUT)
        if invoice.recoverable_tax_amount > 0 else None
    )
    accounts = [asset.asset_account, payable] + ([vat_input] if vat_input else [])
    if len({account.pk for account in accounts}) != len(accounts):
        raise ValidationError("يجب فصل حساب الأصل والموردين وضريبة المدخلات.")
    if payable.nature != "liability" or not payable.is_active or payable.is_group or not payable.allow_posting:
        raise ValidationError("حساب الموردين المرتبط غير صالح.")
    if vat_input and (vat_input.nature != "asset" or not vat_input.is_active or vat_input.is_group or not vat_input.allow_posting):
        raise ValidationError("حساب ضريبة المدخلات المرتبط غير صالح.")

    entry = JournalEntry.objects.create(
        company=invoice.company, branch=invoice.branch, fiscal_year=invoice.fiscal_year,
        entry_number=f"FA-INV-{invoice.pk}", entry_date=invoice.invoice_date,
        description=f"فاتورة شراء أصل من {invoice.supplier.name_ar} رقم {invoice.invoice_number}"[:500],
        source_type=JournalEntry.Source.ASSETS, created_by=user,
    )
    lines = [
        JournalLine(
            entry=entry, account=asset.asset_account,
            description=f"رسملة تكلفة الأصل {asset.name_ar}",
            debit=expected_cost, credit=0,
        ),
        JournalLine(
            entry=entry, account=payable,
            description=f"إثبات مستحق المورد {invoice.supplier.name_ar}",
            debit=0, credit=invoice.grand_total,
        ),
    ]
    if vat_input and invoice.recoverable_tax_amount:
        lines.append(JournalLine(
            entry=entry, account=vat_input,
            description="ضريبة مدخلات قابلة للاسترداد",
            debit=invoice.recoverable_tax_amount, credit=0,
        ))
    JournalLine.objects.bulk_create(lines)
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc

    asset.original_cost = expected_cost
    asset.acquisition_net_amount = invoice.net_amount
    asset.nonrecoverable_tax_amount = invoice.nonrecoverable_tax_amount
    asset.acquisition_tax_code = invoice.tax_code
    asset.supplier_name = invoice.supplier.name_ar
    asset.supplier_invoice = invoice.invoice_number
    asset.acquisition_journal = posted
    asset.save(update_fields=(
        "original_cost", "acquisition_net_amount", "nonrecoverable_tax_amount",
        "acquisition_tax_code", "supplier_name", "supplier_invoice", "acquisition_journal",
    ))
    invoice.status = AssetPurchaseInvoice.Status.POSTED
    invoice.journal_entry = posted
    invoice.posted_at = timezone.now()
    invoice.save(update_fields=("status", "journal_entry", "posted_at"))
    return invoice


@transaction.atomic
def post_asset_supplier_payment(payment_id, user=None):
    """ترحيل سداد فاتورة الأصل على حساب الموردين مع منع تجاوز الرصيد المستحق."""
    payment = AssetSupplierPayment.objects.select_for_update().select_related(
        "company", "branch", "invoice", "supplier", "payment_account",
    ).get(pk=payment_id)
    if payment.status != AssetSupplierPayment.Status.DRAFT:
        raise ValidationError("سند سداد فاتورة الأصل ليس مسودة.")
    invoice = AssetPurchaseInvoice.objects.select_for_update(of=("self",)).get(pk=payment.invoice_id)
    if invoice.status != AssetPurchaseInvoice.Status.POSTED:
        raise ValidationError("لا يمكن سداد فاتورة أصل غير مرحلة.")
    if payment.company_id != invoice.company_id or payment.branch_id != invoice.branch_id or payment.supplier_id != invoice.supplier_id:
        raise ValidationError("بيانات سند السداد لا تطابق فاتورة الأصل.")
    if payment.payment_date < invoice.invoice_date:
        raise ValidationError("تاريخ السداد لا يمكن أن يسبق تاريخ الفاتورة.")
    paid = AssetSupplierPayment.objects.filter(
        invoice=invoice, status=AssetSupplierPayment.Status.POSTED,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = invoice.grand_total - paid
    if payment.amount > remaining:
        raise ValidationError(f"قيمة السداد تتجاوز المتبقي على الفاتورة ({remaining}).")
    year = FiscalYear.objects.filter(
        company=payment.company, status=FiscalYear.Status.OPEN,
        start_date__lte=payment.payment_date, end_date__gte=payment.payment_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ السداد.")
    payable = get_account_for_role(payment.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    cash = payment.payment_account
    if cash.company_id != payment.company_id or cash.nature != "asset" or not cash.is_active or cash.is_group or not cash.allow_posting:
        raise ValidationError("حساب البنك أو الخزينة غير صالح.")
    if cash.pk == payable.pk:
        raise ValidationError("يجب فصل حساب الموردين عن حساب البنك أو الخزينة.")

    entry = JournalEntry.objects.create(
        company=payment.company, branch=payment.branch, fiscal_year=year,
        entry_number=f"FA-PAY-{payment.pk}", entry_date=payment.payment_date,
        description=f"سداد فاتورة أصل {invoice.invoice_number} للمورد {payment.supplier.name_ar}"[:500],
        source_type=JournalEntry.Source.ASSETS, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=payable, description="تخفيض مستحق المورد", debit=payment.amount, credit=0),
        JournalLine(entry=entry, account=cash, description="سداد من البنك أو الخزينة", debit=0, credit=payment.amount),
    ])
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc

    # Keep bank/cash movements visible in the treasury cash-flow subsystem as well as the GL.
    from treasury.models import TreasuryAccount, TreasuryTransaction
    from treasury.services import record_posted_document_bank_movement
    treasury_account = TreasuryAccount.objects.filter(
        company=payment.company, account=cash, is_active=True,
    ).first()
    if (
        not treasury_account
        or (treasury_account.branch_id and treasury_account.branch_id != payment.branch_id)
        or treasury_account.currency_code != payment.company.base_currency.code
    ):
        raise ValidationError("حساب السداد يجب أن يكون مرتبطًا بحساب خزينة/بنك نشط بعملة الشركة ومطابقًا للفرع.")
    record_posted_document_bank_movement(
        company=payment.company, branch=payment.branch, fiscal_year=year, account=cash,
        kind=TreasuryTransaction.Kind.PAYMENT, amount=payment.amount,
        transaction_date=payment.payment_date, document_number=f"AUTO-FA-PAY-{payment.pk}",
        description=f"سداد فاتورة أصل {invoice.invoice_number} - سند {payment.payment_number}",
        counter_account=payable, journal_entry=posted, user=user, reference=payment.reference,
    )
    payment.journal_entry = posted
    payment.status = AssetSupplierPayment.Status.POSTED
    payment.posted_at = timezone.now()
    payment.save(update_fields=("journal_entry", "status", "posted_at"))
    return payment
