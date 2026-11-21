from collections import defaultdict
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from companies.models import FiscalYear
from audit.models import AuditEvent
from .models import JournalEntry, JournalLine

ACCOUNT_ROLE_NATURES = {
    "cash": {"asset"}, "bank": {"asset"}, "accounts_receivable": {"asset"},
    "inventory": {"asset"}, "vat_input": {"asset"},
    "accounts_payable": {"liability"}, "vat_output": {"liability"}, "vat_payable": {"liability"},
    "employee_insurance_payable": {"liability"}, "payroll_tax_payable": {"liability"},
    "employer_insurance_payable": {"liability"},
    "payroll_payable": {"liability"}, "goods_received_not_invoiced": {"liability"},
    "inter_branch_clearing": {"asset", "liability"},
    "sales": {"revenue"}, "sales_returns": {"revenue"}, "inventory_adjustment_gain": {"revenue"},
    "cogs": {"expense"}, "operating_expense": {"expense"}, "payroll_expense": {"expense"},
    "employer_insurance_expense": {"expense"}, "inventory_adjustment_loss": {"expense"},
    "retained_earnings": {"equity"},
}


def get_account_for_role(company, role):
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
def post_journal(entry_id: int) -> JournalEntry:
    entry = JournalEntry.objects.select_for_update().select_related(
        "company", "branch", "fiscal_year"
    ).get(pk=entry_id)

    if entry.status != JournalEntry.Status.DRAFT:
        raise ValidationError("لا يمكن ترحيل إلا القيود المسجلة كمسودة.")
    if entry.fiscal_year.status != FiscalYear.Status.OPEN:
        raise ValidationError("لا يمكن الترحيل داخل سنة مالية مغلقة.")
    if not (entry.fiscal_year.start_date <= entry.entry_date <= entry.fiscal_year.end_date):
        raise ValidationError("تاريخ القيد خارج حدود السنة المالية المحددة.")
    if entry.branch.company_id != entry.company_id:
        raise ValidationError("الفرع لا يتبع شركة القيد.")

    lines = list(entry.lines.select_related("account", "cost_center").all())
    if len(lines) < 2:
        raise ValidationError("القيد يجب أن يحتوي على سطرين على الأقل.")

    debit_total = Decimal("0")
    credit_total = Decimal("0")
    for line in lines:
        if line.account.company_id != entry.company_id:
            raise ValidationError("يوجد حساب لا يتبع شركة القيد.")
        if not line.account.is_active or line.account.is_group or not line.account.allow_posting:
            raise ValidationError(f"الحساب {line.account.code} غير متاح للترحيل.")
        if line.debit < 0 or line.credit < 0 or ((line.debit > 0) == (line.credit > 0)):
            raise ValidationError("كل سطر يجب أن يحتوي على مدين أو دائن فقط وبقيمة غير سالبة.")
        if line.cost_center_id and line.cost_center.company_id != entry.company_id:
            raise ValidationError("مركز تكلفة لا يتبع شركة القيد.")
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
            actor=entry.created_by,
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
        actor=entry.created_by,
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
