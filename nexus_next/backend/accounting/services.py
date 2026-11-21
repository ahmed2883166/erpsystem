from collections import defaultdict
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from companies.models import FiscalYear
from .models import JournalEntry, JournalLine


@transaction.atomic
def post_journal(entry_id: int) -> JournalEntry:
    entry = JournalEntry.objects.select_for_update().select_related(
        "company", "branch", "fiscal_year", "reversal_of"
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

    entry.status = JournalEntry.Status.POSTED
    entry.posted_at = timezone.now()
    entry.save(update_fields=["status", "posted_at"])
    return entry
