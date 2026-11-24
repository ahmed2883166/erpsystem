"""خدمات التحقق من أدوار الحسابات وترحيل قيود اليومية مع حماية الفترة المالية والتوازن."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from companies.models import FiscalYear
from companies.access import acquire_fiscal_year_lock, ensure_fiscal_period_open
from audit.models import AuditEvent
from .models import JournalEntry

ACCOUNT_ROLE_NATURES = {
    "cash": {"asset"}, "bank": {"asset"}, "accounts_receivable": {"asset"},
    "inventory": {"asset"}, "vat_input": {"asset"},
    "accounts_payable": {"liability"}, "vat_output": {"liability"}, "vat_payable": {"liability"},
    "employee_insurance_payable": {"liability"}, "payroll_tax_payable": {"liability"},
    "employer_insurance_payable": {"liability"},
    "payroll_payable": {"liability"}, "goods_received_not_invoiced": {"liability"},
    "inter_branch_clearing": {"asset", "liability"},
    "realized_fx_gain": {"revenue"}, "unrealized_fx_gain": {"revenue"},
    "realized_fx_loss": {"expense"}, "unrealized_fx_loss": {"expense"},
    "sales": {"revenue"}, "sales_returns": {"revenue"}, "inventory_adjustment_gain": {"revenue"},
    "cogs": {"expense"}, "operating_expense": {"expense"}, "payroll_expense": {"expense"},
    "employer_insurance_expense": {"expense"}, "inventory_adjustment_loss": {"expense"},
    "retained_earnings": {"equity"},
}


def get_account_for_role(company, role):
    """إرجاع الحساب التفصيلي النشط المرتبط بدور محاسبي داخل الشركة."""
    from .models import AccountRole

    assignment = AccountRole.objects.select_related("account").filter(company=company, role=role).first()
    if not assignment:
        raise ValidationError(f"يجب ربط دور الحساب ({role}) في إعدادات الشركة.")
    account = assignment.account
    if account.company_id != company.pk or not account.is_active or account.is_group or not account.allow_posting:
        raise ValidationError(f"الحساب المرتبط بالدور ({role}) غير صالح للترحيل.")
    expected = ACCOUNT_ROLE_NATURES.get(role)
    if expected and account.nature not in expected:
        raise ValidationError(f"طبيعة الحساب المرتبط بالدور ({role}) غير متوافقة؛ راجع ربط الأدوار المحاسبية.")
    return account



@transaction.atomic
def post_journal(entry_id: int, actor=None) -> JournalEntry:
    """ترحيل القيد اليومي بعد التحقق من توازنه وصلاحية حساباته."""
    # Lock only the journal row. Locking select_related rows (company/branch/
    # fiscal year) creates a lock cycle with FK KEY SHARE checks when the audit
    # trail inserts its company/branch references during concurrent postings.
    entry = JournalEntry.objects.select_for_update(of=("self",)).select_related(
        "company", "company__base_currency", "branch", "fiscal_year"
    ).get(pk=entry_id)
    # Shared transaction lock allows independent postings to proceed together,
    # while fiscal-year/period closure takes the matching exclusive lock.
    fiscal_year = FiscalYear.objects.get(pk=entry.fiscal_year_id)
    has_advisory_lock = acquire_fiscal_year_lock(fiscal_year, shared=True)
    if has_advisory_lock:
        # The lock may have waited for a concurrent close; refresh only after
        # acquiring it so a stale pre-lock OPEN value cannot authorize posting.
        fiscal_year.refresh_from_db(fields=("status", "start_date", "end_date"))
    else:
        # Preserve the previous row-lock behavior on non-PostgreSQL backends.
        fiscal_year = FiscalYear.objects.select_for_update().get(pk=entry.fiscal_year_id)

    if entry.status != JournalEntry.Status.DRAFT:
        raise ValidationError("لا يمكن ترحيل إلا القيود المسجلة كمسودة.")
    if fiscal_year.status != FiscalYear.Status.OPEN:
        raise ValidationError("لا يمكن الترحيل داخل سنة مالية مغلقة.")
    if not (fiscal_year.start_date <= entry.entry_date <= fiscal_year.end_date):
        raise ValidationError("تاريخ القيد خارج حدود السنة المالية المحددة.")
    ensure_fiscal_period_open(fiscal_year, entry.entry_date)
    if entry.branch.company_id != entry.company_id:
        raise ValidationError("الفرع لا يتبع شركة القيد.")

    lines = list(entry.lines.select_related("account", "cost_center").all())
    if len(lines) < 2:
        raise ValidationError("القيد يجب أن يحتوي على سطرين على الأقل.")

    debit_total = Decimal("0")
    credit_total = Decimal("0")
    currency_places = entry.company.base_currency.decimal_places
    base_quantum = Decimal("1").scaleb(-currency_places)
    for line in lines:
        rounded_debit = line.debit.quantize(base_quantum, rounding=ROUND_HALF_UP)
        rounded_credit = line.credit.quantize(base_quantum, rounding=ROUND_HALF_UP)
        if rounded_debit == 0 and rounded_credit == 0:
            raise ValidationError("قيمة أحد أسطر القيد أقل من أصغر وحدة نقدية للعملة الأساسية بعد التقريب.")
        if rounded_debit != line.debit or rounded_credit != line.credit:
            line.debit = rounded_debit
            line.credit = rounded_credit
            line.save(update_fields=("debit", "credit"))
        if line.account.company_id != entry.company_id:
            raise ValidationError("يوجد حساب لا يتبع شركة القيد.")
        if not line.account.is_active or line.account.is_group or not line.account.allow_posting:
            raise ValidationError(f"الحساب {line.account.code} غير متاح للترحيل.")
        if line.debit < 0 or line.credit < 0 or ((line.debit > 0) == (line.credit > 0)):
            raise ValidationError("كل سطر يجب أن يحتوي على مدين أو دائن فقط وبقيمة غير سالبة.")
        if line.cost_center_id and line.cost_center.company_id != entry.company_id:
            raise ValidationError("مركز تكلفة لا يتبع شركة القيد.")
        line.validate_currency_snapshot(entry_date=entry.entry_date, company=entry.company)
        debit_total += line.debit
        credit_total += line.credit

    if debit_total <= 0 or debit_total != credit_total:
        raise ValidationError("القيد غير متوازن؛ إجمالي المدين يجب أن يساوي إجمالي الدائن.")

    if entry.reversal_of_id:
        original = JournalEntry.objects.select_for_update().get(pk=entry.reversal_of_id)
        if original.status != JournalEntry.Status.POSTED or original.reversal_of_id:
            raise ValidationError("القيد الأصلي غير صالح للعكس.")
        if JournalEntry.objects.filter(reversal_of=original).exclude(pk=entry.pk).exists():
            raise ValidationError("تم إنشاء قيد عكسي لهذا القيد من قبل.")
        expected = defaultdict(lambda: [Decimal("0"), Decimal("0")])
        actual = defaultdict(lambda: [Decimal("0"), Decimal("0")])
        for line in original.lines.all():
            key = (line.account_id, line.cost_center_id)
            expected[key][0] += line.credit
            expected[key][1] += line.debit
        for line in lines:
            key = (line.account_id, line.cost_center_id)
            actual[key][0] += line.debit
            actual[key][1] += line.credit
        if dict(expected) != dict(actual):
            raise ValidationError("القيد العكسي يجب أن يعكس حسابات ومبالغ القيد الأصلي بالكامل.")
        JournalEntry.objects.filter(pk=original.pk).update(status=JournalEntry.Status.REVERSED)
        AuditEvent.objects.create(
            actor=actor or entry.created_by,
            company=original.company,
            branch=original.branch,
            action=AuditEvent.Action.ACTION,
            resource="accounting.journal_entry.reversed",
            object_id=str(original.pk),
            http_method="JOURNAL",
            status_code=200,
            succeeded=True,
            metadata={
                "entry_number": original.entry_number,
                "reversal_entry_id": entry.pk,
                "reversal_entry_number": entry.entry_number,
            },
        )

    entry.status = JournalEntry.Status.POSTED
    entry.posted_at = timezone.now()
    entry.save(update_fields=["status", "posted_at"])
    AuditEvent.objects.create(
        actor=actor or entry.created_by,
        company=entry.company,
        branch=entry.branch,
        action=AuditEvent.Action.ACTION,
        resource="accounting.journal_entry.posted",
        object_id=str(entry.pk),
        http_method="JOURNAL",
        status_code=200,
        succeeded=True,
        metadata={
            "entry_number": entry.entry_number,
            "source_type": entry.source_type,
            "reversal_of_id": entry.reversal_of_id,
        },
    )
    return entry


@transaction.atomic
def reverse_operational_journal(entry_id: int, user, entry_date, expected_source: str) -> JournalEntry:
    """Create and post a full, linked reversal for an approved operational source document."""
    # Keep related rows readable without taking write locks on company/branch;
    # the journal row alone protects this reversal workflow from duplicate work.
    original = JournalEntry.objects.select_for_update(of=("self",)).select_related(
        "company", "branch", "fiscal_year"
    ).get(pk=entry_id)
    if original.status != JournalEntry.Status.POSTED or original.reversal_of_id:
        raise ValidationError("لا يمكن عكس إلا قيد تشغيلي أصلي مرحّل.")
    if original.source_type != expected_source:
        raise ValidationError("مصدر القيد لا يطابق نوع المستند المطلوب عكسه.")
    if JournalEntry.objects.filter(reversal_of=original).exists():
        raise ValidationError("تم إنشاء قيد عكسي لهذا القيد من قبل.")
    if entry_date < original.entry_date:
        raise ValidationError("تاريخ القيد العكسي لا يجوز أن يسبق تاريخ القيد الأصلي.")

    # IAS 36 reversals are capped by the carrying amount that would have
    # existed without impairment. A generic full journal reversal cannot
    # calculate that cap or revise the remaining depreciation basis safely.
    from assets.models import AssetImpairment
    if AssetImpairment.objects.filter(journal_entry=original).exists():
        raise ValidationError("لا يمكن عكس خسارة انخفاض القيمة بعكس قيد عام؛ يلزم إجراء عكس مخصص وفق حدود IAS 36.")

    fiscal_year = FiscalYear.objects.filter(
        company=original.company,
        status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date,
        end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not fiscal_year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تغطي تاريخ العكس.")

    reversal = JournalEntry.objects.create(
        company=original.company,
        branch=original.branch,
        fiscal_year=fiscal_year,
        entry_number=f"REV-{original.pk}",
        entry_date=entry_date,
        description=f"عكس المستند المرتبط بالقيد {original.entry_number}: {original.description}"[:500],
        source_type=original.source_type,
        reversal_of=original,
        created_by=user,
    )
    original_lines = list(original.lines.all())
    if len(original_lines) < 2:
        raise ValidationError("القيد الأصلي لا يحتوي على سطور كافية للعكس.")
    from .models import JournalLine
    JournalLine.objects.bulk_create([
        JournalLine(
            entry=reversal,
            account=line.account,
            cost_center=line.cost_center,
            debit=line.credit,
            credit=line.debit,
            description=f"عكس: {line.description}"[:300],
            transaction_currency=line.transaction_currency,
            transaction_amount=line.transaction_amount,
            exchange_rate=line.exchange_rate,
            exchange_rate_date=line.exchange_rate_date,
        )
        for line in original_lines
    ])
    return post_journal(reversal.pk, actor=user)
