"""واجهات API للمحاسبة: دليل الحسابات والقيود والتقارير والتصدير.
تُطبق صلاحيات الشركة والفرع وتُمرر عمليات الترحيل إلى خدمات المحاسبة.
"""

from io import BytesIO
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, transaction
from django.http import HttpResponse
from openpyxl import Workbook
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.views import APIView

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from companies.models import Company, FiscalYear
from purchasing.models import PurchaseInvoiceLine
from sales.models import SalesInvoiceLine
from .models import AccountRole, ChartOfAccount, CostCenter, FxSubledgerRevaluation, JournalEntry, JournalLine, TaxCode
from .permissions import CompanyRolePermission
from .serializers import AccountRoleSerializer, ChartOfAccountSerializer, CostCenterSerializer, JournalEntrySerializer, TaxCodeSerializer
from .services import post_journal
from .account_templates import ACTIVITY_LABELS, apply_activity_template, build_activity_template
from .account_imports import inspect_account_workbook, import_account_rows


def _branch_aware_opening_filter(branch_ids, snapshot_branch_ids, cutoff, fiscal_year_start, *, prefix="entry__"):
    """Build opening-balance scope independently for branches with/without a fiscal-year snapshot."""
    snapshot_ids = set(snapshot_branch_ids or ())
    carry_forward_ids = set(branch_ids or ()) - snapshot_ids
    result = Q(pk__in=[])
    if snapshot_ids:
        result |= Q(**{
            f"{prefix}branch_id__in": snapshot_ids,
            f"{prefix}entry_date__gte": fiscal_year_start,
            f"{prefix}entry_date__lt": cutoff,
        })
        result |= Q(**{
            f"{prefix}branch_id__in": snapshot_ids,
            f"{prefix}entry_date": cutoff,
            f"{prefix}source_type": JournalEntry.Source.OPENING,
        })
    if carry_forward_ids:
        result |= Q(**{
            f"{prefix}branch_id__in": carry_forward_ids,
            f"{prefix}entry_date__lt": cutoff,
        })
        result |= Q(**{
            f"{prefix}branch_id__in": carry_forward_ids,
            f"{prefix}entry_date": cutoff,
            f"{prefix}source_type": JournalEntry.Source.OPENING,
        })
    return result


def _branch_aware_balance_filter(branch_ids, snapshot_branch_ids, cutoff, fiscal_year_start, *, prefix="entry__"):
    """Use the current fiscal-year opening snapshot only for branches that actually have one."""
    snapshot_ids = set(snapshot_branch_ids or ())
    carry_forward_ids = set(branch_ids or ()) - snapshot_ids
    result = Q(pk__in=[])
    if snapshot_ids:
        result |= Q(**{
            f"{prefix}branch_id__in": snapshot_ids,
            f"{prefix}entry_date__gte": fiscal_year_start,
            f"{prefix}entry_date__lte": cutoff,
        })
    if carry_forward_ids:
        result |= Q(**{
            f"{prefix}branch_id__in": carry_forward_ids,
            f"{prefix}entry_date__lte": cutoff,
        })
    return result


class AccountingViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return self.queryset
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def _validate_scope(self, data):
        """التحقق من ارتباط البيانات المرجعية بالشركة والفرع المسموحين."""
        company = data.get("company")
        if company and not can_access_company(self.request.user, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        branch = data.get("branch")
        if branch and not can_access_branch(
            self.request.user, branch.pk, company.pk if company else None
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

    def _require_write_role(self, company_id, *, delete=False):
        """التحقق من امتلاك المستخدم دورًا يسمح بتعديل البيانات المالية."""
        roles = ("admin",) if delete else ("admin", "accountant")
        if not has_company_role(self.request.user, company_id, roles):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب لتنفيذ هذا الإجراء.")

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        self._validate_scope(serializer.validated_data)
        self._require_write_role(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        self._validate_scope(serializer.validated_data)
        company = serializer.validated_data.get("company", serializer.instance.company)
        if company.pk != serializer.instance.company_id:
            raise ValidationError("لا يمكن نقل سجل محاسبي إلى شركة أخرى بعد إنشائه.")
        self._require_write_role(company.pk)
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        self._require_write_role(instance.company_id, delete=True)
        instance.delete()


class AccountTemplateView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def _company(self, company_id):
        """تنفيذ وظيفة _company ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
        company = Company.objects.filter(pk=company_id).first()
        if not company:
            raise ValidationError({"company": "الشركة المحددة غير موجودة."})
        if not can_access_company(self.request.user, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        return company

    def get(self, request):
        """إرجاع البيانات المطلوبة للعرض مع احترام صلاحيات الوصول."""
        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة المطلوبة."})
        company = self._company(int(company_id))
        rows = build_activity_template(company.activity)
        return response.Response({
            "company": company.pk,
            "activity": company.activity,
            "activity_label": ACTIVITY_LABELS[company.activity],
            "template_account_count": len(rows),
            "existing_account_count": ChartOfAccount.objects.filter(company=company).count(),
            "has_accounts": ChartOfAccount.objects.filter(company=company).exists(),
        })

    def post(self, request):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        company_id = request.data.get("company")
        if not str(company_id or "").isdigit():
            raise ValidationError({"company": "حدد الشركة المطلوبة."})
        company = self._company(int(company_id))
        if not has_company_role(request.user, company.pk, ("admin",)):
            raise PermissionDenied("تطبيق قالب دليل الحسابات متاح لمدير الشركة فقط.")
        activity = request.data.get("activity", company.activity)
        if activity != company.activity:
            raise ValidationError({"activity": "قالب النشاط يجب أن يطابق نشاط الشركة المسجل في الإعدادات."})
        with transaction.atomic():
            result = apply_activity_template(company, activity)
        if result["code_conflicts"]:
            return response.Response({
                "detail": "توجد أكواد حسابات مستخدمة بمعانٍ مختلفة. لم يتم إنشاء أو تعديل أي حساب.",
                **result,
            }, status=status.HTTP_409_CONFLICT)
        return response.Response({
            "detail": "تم تجهيز دليل الحسابات وربط الأدوار المتاحة دون تغيير الحسابات الموجودة.",
            "company": company.pk,
            "activity": activity,
            **result,
        }, status=status.HTTP_200_OK)


class AccountImportView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        """إرجاع البيانات المطلوبة للعرض مع احترام صلاحيات الوصول."""
        if not request.user.is_authenticated:
            raise PermissionDenied("سجل الدخول أولًا لتحميل النموذج.")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "دليل الحسابات"
        sheet.append(["كود الحساب", "اسم الحساب", "كود الأب", "طبيعة الحساب", "التصنيف"])
        sheet.append(["1000", "الأصول", None, "أصول", "مجموعة"])
        sheet.append(["1100", "الأصول المتداولة", "1000", "أصول", "مجموعة"])
        sheet.append(["1110", "الصندوق", "1100", "أصول", "تفصيلي"])
        sheet.append(["2000", "الالتزامات", None, "التزامات", "مجموعة"])
        sheet.append(["2100", "الالتزامات المتداولة", "2000", "التزامات", "مجموعة"])
        sheet.append(["2110", "الموردون", "2100", "التزامات", "تفصيلي"])
        for column, width in {"A": 18, "B": 34, "C": 18, "D": 22, "E": 18}.items():
            sheet.column_dimensions[column].width = width
        instructions = workbook.create_sheet("تعليمات")
        instructions.append(["تعليمات استيراد دليل الحسابات"])
        instructions.append(["استبدل الصفوف التجريبية بحسابات شركتك أو احذفها."])
        instructions.append(["الأعمدة الخمسة في ورقة دليل الحسابات مطلوبة، وكود الأب يترك فارغًا للحساب الرئيسي."])
        instructions.append(["طبيعة الحساب: أصول، التزامات، حقوق الملكية، إيرادات، مصروفات."])
        instructions.append(["التصنيف: مجموعة أو تفصيلي. الحساب التفصيلي فقط يقبل الترحيل."])
        instructions.append(["يجب أن يكون الحساب الأب موجودًا في الملف أو في دليل الشركة وأن يكون مجموعة."])
        instructions.append(["لن يتم تعديل أو حذف الحسابات الموجودة؛ الأكواد المتعارضة تمنع الاعتماد."])
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        response_file = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response_file["Content-Disposition"] = 'attachment; filename="nexus-chart-template.xlsx"'
        return response_file

    def post(self, request):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        company_id = request.data.get("company")
        if not str(company_id or "").isdigit():
            raise ValidationError({"company": "حدد الشركة المطلوبة."})
        company = Company.objects.filter(pk=int(company_id)).first()
        if not company:
            raise ValidationError({"company": "الشركة المحددة غير موجودة."})
        if not can_access_company(request.user, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        action = request.data.get("action", "preview")
        if action not in ("preview", "import"):
            raise ValidationError({"action": "الإجراء يجب أن يكون preview أو import."})
        if action == "import" and not has_company_role(request.user, company.pk, ("admin",)):
            raise PermissionDenied("اعتماد استيراد دليل الحسابات متاح لمدير الشركة فقط.")
        result = inspect_account_workbook(request.FILES.get("file"), company)
        if result["errors"]:
            return response.Response(result, status=status.HTTP_409_CONFLICT if action == "import" else status.HTTP_200_OK)
        if action == "preview":
            return response.Response({"detail": "اكتملت المعاينة. لم يتم حفظ أي حساب.", **result})
        try:
            with transaction.atomic():
                created = import_account_rows(company, result["rows"])
        except (DatabaseError, DjangoValidationError, ValueError) as exc:
            return response.Response({
                "detail": "لم يتم استيراد أي حساب بسبب تعارض أثناء الاعتماد.",
                "errors": [str(exc)],
                "rows": result["rows"],
                "summary": result["summary"],
            }, status=status.HTTP_409_CONFLICT)
        return response.Response({
            "detail": "تم استيراد الحسابات الجديدة بأمان دون تعديل الحسابات الموجودة.",
            "created_accounts": created,
            "skipped_existing": result["summary"]["already_exists"],
            "summary": result["summary"],
        }, status=status.HTTP_200_OK)


class ChartOfAccountViewSet(AccountingViewSet):
    serializer_class = ChartOfAccountSerializer
    queryset = ChartOfAccount.objects.select_related("company", "parent").all()
    filterset_fields = ("company", "nature", "is_group", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    ordering_fields = ("code", "name_ar")

    def _validate_parent_chain(self, company, parent, current_id=None):
        """منع إنشاء حلقة في شجرة الحسابات حتى عند تعديل حساب أب قائم."""
        seen = set()
        node = parent
        while node is not None:
            if node.company_id != company.pk:
                raise ValidationError({"parent": "الحساب الأب يجب أن يتبع نفس الشركة."})
            if current_id and node.pk == current_id:
                raise ValidationError({"parent": "لا يمكن ربط الحساب بأحد أبنائه؛ سيؤدي ذلك إلى دورة في شجرة الحسابات."})
            if node.pk in seen:
                raise ValidationError({"parent": "سلسلة الحسابات الحالية تحتوي على دورة؛ صحح الشجرة قبل تعديلها."})
            seen.add(node.pk)
            if not node.is_group:
                raise ValidationError({"parent": "يجب أن يكون كل حساب أب حسابًا تجميعيًا."})
            node = node.parent

    def perform_create(self, serializer):
        self._validate_scope(serializer.validated_data)
        company = serializer.validated_data["company"]
        self._require_write_role(company.pk)
        self._validate_parent_chain(company, serializer.validated_data.get("parent"))
        serializer.save()

    def perform_update(self, serializer):
        self._validate_scope(serializer.validated_data)
        company = serializer.validated_data.get("company", serializer.instance.company)
        if company.pk != serializer.instance.company_id:
            raise ValidationError("لا يمكن نقل حساب إلى شركة أخرى بعد إنشائه.")
        self._require_write_role(company.pk)
        self._validate_parent_chain(
            company, serializer.validated_data.get("parent", serializer.instance.parent),
            current_id=serializer.instance.pk,
        )
        serializer.save()


class AccountRoleViewSet(AccountingViewSet):
    serializer_class = AccountRoleSerializer
    queryset = AccountRole.objects.select_related("company", "account").all()
    filterset_fields = ("company", "role")


class CostCenterViewSet(AccountingViewSet):
    serializer_class = CostCenterSerializer
    queryset = CostCenter.objects.select_related("company", "parent").all()
    filterset_fields = ("company", "is_active")
    search_fields = ("code", "name_ar")


class TaxCodeViewSet(AccountingViewSet):
    serializer_class = TaxCodeSerializer
    queryset = TaxCode.objects.select_related("company").all()
    filterset_fields = ("company", "country", "category", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "rate", "effective_from")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        qs = super().get_queryset()
        as_of = self.request.query_params.get("as_of")
        if as_of:
            tax_date = parse_date(as_of)
            if not tax_date:
                raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
            qs = qs.filter(effective_from__lte=tax_date).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=tax_date)
            )
        return qs.filter(is_active=True) if self.request.query_params.get("active_only") == "true" else qs

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        self._validate_scope(serializer.validated_data)
        company = serializer.validated_data["company"]
        self._require_write_role(company.pk)
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        instance = serializer.instance
        is_used = instance.sales_invoice_lines.exists() or instance.purchase_invoice_lines.exists()
        if is_used and set(serializer.validated_data) - {"is_active"}:
            raise ValidationError("لا يمكن تغيير نسبة أو تصنيف أو فترة كود ضريبي مستخدم في فواتير؛ أنشئ إصدارًا جديدًا بتاريخ سريان جديد.")
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        self._require_write_role(instance.company_id, delete=True)
        if instance.is_active:
            raise ValidationError("عطّل كود الضريبة بدل حذفه للحفاظ على أثر الفواتير.")
        if instance.sales_invoice_lines.exists() or instance.purchase_invoice_lines.exists():
            raise ValidationError("لا يمكن حذف كود ضريبي مرتبط بفواتير؛ اتركه معطلًا للحفاظ على السجل.")
        instance.delete()


class JournalEntryViewSet(AccountingViewSet):
    serializer_class = JournalEntrySerializer
    queryset = JournalEntry.objects.select_related(
        "company", "branch", "fiscal_year", "created_by"
    ).prefetch_related("lines__account", "lines__cost_center").all()
    filterset_fields = ("company", "branch", "fiscal_year", "status", "source_type")
    search_fields = ("entry_number", "description")
    ordering_fields = ("entry_date", "entry_number", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        qs = super().get_queryset()
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        return qs.filter(branch__in=accessible_branches(self.request.user))

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        if serializer.instance.status != JournalEntry.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل قيد مرحّل أو معكوس.")
        super().perform_update(serializer)

    @decorators.action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        entry = self.get_object()
        self._require_write_role(entry.company_id)
        try:
            entry = post_journal(entry.pk)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        return response.Response(self.get_serializer(entry).data, status=status.HTTP_200_OK)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        """إنشاء قيد عكسي مرتبط بالقيد الأصلي مع منع العكس غير المسموح."""
        original = self.get_object()
        self._require_write_role(original.company_id)
        if original.status != JournalEntry.Status.POSTED or original.reversal_of_id:
            raise ValidationError("يمكن عكس قيد مرحّل أصلي فقط؛ لا يمكن عكس مسودة أو قيد عكسي.")
        fx_revaluation = None
        if original.source_type == JournalEntry.Source.FX_REVALUATION:
            fx_revaluation = FxSubledgerRevaluation.objects.select_for_update().filter(journal_entry=original).first()
            if not fx_revaluation:
                raise ValidationError("قيد إعادة تقييم العملة غير مرتبط بسجل إعادة تقييم؛ لا يمكن عكسه من المسار العام.")
            if fx_revaluation.sales_invoice_id:
                from sales.models import CustomerReceipt
                later_settlements = CustomerReceipt.objects.filter(
                    invoice_id=fx_revaluation.sales_invoice_id,
                    status=CustomerReceipt.Status.POSTED,
                    receipt_date__gte=fx_revaluation.revaluation_date,
                ).exists()
                later_revaluations = FxSubledgerRevaluation.objects.filter(
                    sales_invoice_id=fx_revaluation.sales_invoice_id,
                    revaluation_date__gt=fx_revaluation.revaluation_date,
                ).filter(Q(journal_entry__isnull=True) | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)).exists()
            else:
                from purchasing.models import SupplierPayment
                later_settlements = SupplierPayment.objects.filter(
                    invoice_id=fx_revaluation.purchase_invoice_id,
                    status=SupplierPayment.Status.POSTED,
                    payment_date__gte=fx_revaluation.revaluation_date,
                ).exists()
                later_revaluations = FxSubledgerRevaluation.objects.filter(
                    purchase_invoice_id=fx_revaluation.purchase_invoice_id,
                    revaluation_date__gt=fx_revaluation.revaluation_date,
                ).filter(Q(journal_entry__isnull=True) | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)).exists()
            if later_settlements or later_revaluations:
                raise ValidationError("لا يمكن عكس إعادة تقييم لها تسوية لاحقة أو إعادة تقييم أحدث؛ صحح التسلسل من آخر حدث إلى أوله.")
        elif original.source_type != JournalEntry.Source.MANUAL:
            raise ValidationError("عكس القيود التشغيلية أو الافتتاحية أو الإقفالية غير متاح من زر العكس العام؛ استخدم إجراء الإلغاء أو التصحيح داخل الوحدة المصدر.")
        if JournalEntry.objects.filter(reversal_of=original).exists():
            raise ValidationError("تم إنشاء قيد عكسي لهذا القيد من قبل.")
        entry_date_raw = request.data.get("entry_date")
        entry_date = parse_date(entry_date_raw) if entry_date_raw else timezone.localdate()
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        if entry_date < original.entry_date:
            raise ValidationError({"entry_date": "تاريخ القيد العكسي لا يجوز أن يسبق تاريخ القيد الأصلي."})
        fiscal_year = FiscalYear.objects.filter(
            company=original.company, status=FiscalYear.Status.OPEN,
            start_date__lte=entry_date, end_date__gte=entry_date,
        ).order_by("-start_date").first()
        if not fiscal_year:
            raise ValidationError("لا توجد سنة مالية مفتوحة تغطي تاريخ العكس.")
        reversal = JournalEntry.objects.create(
            company=original.company, branch=original.branch, fiscal_year=fiscal_year,
            entry_number=f"REV-{original.pk}", entry_date=entry_date,
            description=f"عكس القيد {original.entry_number}: {original.description}"[:500],
            source_type=JournalEntry.Source.MANUAL, reversal_of=original,
            created_by=request.user,
        )
        original_lines = list(original.lines.all())
        JournalLine.objects.bulk_create([
            JournalLine(
                entry=reversal, account=line.account, cost_center=line.cost_center,
                debit=line.credit, credit=line.debit,
                description=f"عكس: {line.description}"[:300],
            )
            for line in original_lines
        ])
        try:
            reversal = post_journal(reversal.pk)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        return response.Response(self.get_serializer(reversal).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=False, methods=("get",), url_path="expense-analysis")
    def expense_analysis(self, request):
        """تحليل المصروفات المرحلة حسب الحساب والفرع ومركز التكلفة والشهر."""
        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض تحليل المصروفات."})
        company_id = int(company_id)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        date_from = parse_date(request.query_params.get("date_from", ""))
        date_to = parse_date(request.query_params.get("date_to", ""))
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError({"date_from": "حدد فترة صحيحة من تاريخ وإلى تاريخ."})

        lines = JournalLine.objects.filter(
            entry__company_id=company_id,
            entry__status=JournalEntry.Status.POSTED,
            entry__entry_date__gte=date_from,
            entry__entry_date__lte=date_to,
            account__nature=ChartOfAccount.Nature.EXPENSE,
            account__is_group=False,
            account__is_active=True,
        ).filter(entry__branch__in=accessible_branches(request.user))
        branch_id = request.query_params.get("branch")
        if branch_id:
            if not branch_id.isdigit() or not can_access_branch(request.user, int(branch_id), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            lines = lines.filter(entry__branch_id=int(branch_id))
        cost_center_id = request.query_params.get("cost_center")
        if cost_center_id:
            if not cost_center_id.isdigit():
                raise ValidationError({"cost_center": "مركز التكلفة غير صحيح."})
            if not CostCenter.objects.filter(pk=int(cost_center_id), company_id=company_id, is_active=True).exists():
                raise ValidationError({"cost_center": "مركز التكلفة غير موجود أو غير نشط في الشركة."})
            lines = lines.filter(cost_center_id=int(cost_center_id))

        closing_ids = JournalEntry.objects.filter(
            company_id=company_id, source_type=JournalEntry.Source.CLOSING,
        ).values("pk")
        if request.query_params.get("exclude_closing", "true").lower() not in ("0", "false", "no"):
            lines = lines.exclude(entry_id__in=closing_ids)

        cogs_ids = set(AccountRole.objects.filter(
            company_id=company_id, role=AccountRole.Role.COGS,
        ).values_list("account_id", flat=True))
        detail = list(lines.values(
            "account_id", "account__code", "account__name_ar",
            "entry__branch_id", "entry__branch__code", "entry__branch__name_ar",
            "cost_center_id", "cost_center__code", "cost_center__name_ar",
        ).annotate(
            debit=Sum("debit"), credit=Sum("credit"),
        ).order_by("account__code", "entry__branch__code", "cost_center__code"))
        monthly = list(lines.values("entry__entry_date__year", "entry__entry_date__month").annotate(
            debit=Sum("debit"), credit=Sum("credit"),
        ).order_by("entry__entry_date__year", "entry__entry_date__month"))

        rows = []
        total = Decimal("0")
        direct_total = Decimal("0")
        operating_total = Decimal("0")
        for row in detail:
            debit = row["debit"] or Decimal("0")
            credit = row["credit"] or Decimal("0")
            amount = debit - credit
            direct = row["account_id"] in cogs_ids
            total += amount
            if direct:
                direct_total += amount
            else:
                operating_total += amount
            rows.append({
                "account_id": row["account_id"], "code": row["account__code"],
                "name_ar": row["account__name_ar"], "branch_id": row["entry__branch_id"],
                "branch_code": row["entry__branch__code"], "branch_name": row["entry__branch__name_ar"],
                "cost_center_id": row["cost_center_id"], "cost_center_code": row["cost_center__code"] or "",
                "cost_center_name": row["cost_center__name_ar"] or "غير محدد",
                "debit": debit, "credit": credit, "amount": amount,
                "category": "direct_cost" if direct else "operating_expense",
            })
        monthly_rows = [{
            "month": f"{row['entry__entry_date__year']:04d}-{row['entry__entry_date__month']:02d}",
            "amount": (row["debit"] or Decimal("0")) - (row["credit"] or Decimal("0")),
        } for row in monthly]
        return response.Response({
            "company": company_id, "date_from": date_from, "date_to": date_to,
            "currency": Company.objects.get(pk=company_id).base_currency.code,
            "total": total, "direct_total": direct_total, "operating_total": operating_total,
            "rows": rows, "monthly_rows": monthly_rows,
            "note": "يعرض التقرير المصروفات من القيود المرحلة فقط، مع استبعاد قيود الإقفال افتراضيًا. التصنيف المباشر يعتمد على ربط دور تكلفة المبيعات.",
        })

    @decorators.action(detail=False, methods=("get",), url_path="trial-balance")
    def trial_balance(self, request):
        """إعداد ميزان المراجعة للفترة والفروع المحددة وفق حالة القيود."""
        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض ميزان المراجعة."})
        if not can_access_company(request.user, int(company_id)):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from and not parse_date(date_from):
            raise ValidationError({"date_from": "صيغة تاريخ البداية يجب أن تكون YYYY-MM-DD."})
        if date_to and not parse_date(date_to):
            raise ValidationError({"date_to": "صيغة تاريخ النهاية يجب أن تكون YYYY-MM-DD."})
        if date_from and date_to and date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        date_filter = Q(journal_lines__entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED))
        exclude_closing = request.query_params.get("exclude_closing", "").lower() in ("1", "true", "yes")
        if exclude_closing:
            date_filter &= ~Q(journal_lines__entry__source_type=JournalEntry.Source.CLOSING)
        branch_raw = request.query_params.get("branch")
        if branch_raw:
            if not branch_raw.isdigit() or not can_access_branch(request.user, int(branch_raw), int(company_id)):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            branch_ids = [int(branch_raw)]
            date_filter &= Q(journal_lines__entry__branch_id=int(branch_raw))
        elif not request.user.is_superuser or is_platform_owner(request.user):
            branch_ids = list(accessible_branches(request.user, int(company_id)).values_list("pk", flat=True))
            date_filter &= Q(journal_lines__entry__branch_id__in=branch_ids)
        else:
            branch_ids = list(JournalEntry.objects.filter(company_id=company_id).values_list("branch_id", flat=True).distinct())
        opening_filter = date_filter
        if date_from:
            opening_year = FiscalYear.objects.filter(
                company_id=company_id, start_date__lte=date_from, end_date__gte=date_from
            ).order_by("-start_date").first()
            snapshot_branch_ids = set()
            if opening_year:
                snapshot_branch_ids = set(JournalEntry.objects.filter(
                    company_id=company_id, fiscal_year=opening_year,
                    status=JournalEntry.Status.POSTED, source_type=JournalEntry.Source.OPENING,
                    entry_date__lte=date_from, branch_id__in=branch_ids,
                ).values_list("branch_id", flat=True).distinct())
            opening_filter &= _branch_aware_opening_filter(
                branch_ids, snapshot_branch_ids, date_from,
                opening_year.start_date if opening_year else date_from,
                prefix="journal_lines__entry__",
            )
            date_filter &= Q(journal_lines__entry__entry_date__gte=date_from)
            date_filter &= ~Q(
                journal_lines__entry__entry_date=date_from,
                journal_lines__entry__source_type=JournalEntry.Source.OPENING,
            )
        if date_to:
            date_filter &= Q(journal_lines__entry__entry_date__lte=date_to)
        accounts = ChartOfAccount.objects.filter(company_id=company_id).annotate(
            debit_total=Sum("journal_lines__debit", filter=date_filter),
            credit_total=Sum("journal_lines__credit", filter=date_filter),
            opening_debit=Sum("journal_lines__debit", filter=opening_filter) if date_from else Sum("journal_lines__debit", filter=Q(pk__isnull=True)),
            opening_credit=Sum("journal_lines__credit", filter=opening_filter) if date_from else Sum("journal_lines__credit", filter=Q(pk__isnull=True)),
        ).order_by("code")
        rows = []
        debit_sum = 0
        credit_sum = 0
        opening_debit_sum = 0
        opening_credit_sum = 0
        for account in accounts:
            debit = account.debit_total or 0
            credit = account.credit_total or 0
            opening_debit = account.opening_debit or 0
            opening_credit = account.opening_credit or 0
            debit_sum += debit
            credit_sum += credit
            opening_debit_sum += opening_debit
            opening_credit_sum += opening_credit
            rows.append({
                "account_id": account.pk,
                "code": account.code,
                "name_ar": account.name_ar,
                "nature": account.nature,
                "is_group": account.is_group,
                "opening_debit": opening_debit,
                "opening_credit": opening_credit,
                "opening_balance": opening_debit - opening_credit,
                "debit_total": debit,
                "credit_total": credit,
                "net_balance": debit - credit,
                "closing_balance": opening_debit - opening_credit + debit - credit,
            })
        return response.Response({
            "company_id": int(company_id),
            "date_from": date_from,
            "date_to": date_to,
            "accounts": rows,
            "opening_debit_total": opening_debit_sum,
            "opening_credit_total": opening_credit_sum,
            "debit_total": debit_sum,
            "credit_total": credit_sum,
            "closing_debit_credit_difference": (opening_debit_sum + debit_sum) - (opening_credit_sum + credit_sum),
            "difference": debit_sum - credit_sum,
        })



    @decorators.action(detail=False, methods=("get",), url_path="balance-sheet")
    def balance_sheet(self, request):
        """إعداد قائمة المركز المالي حتى التاريخ المحدد."""
        from decimal import Decimal
        company_raw = request.query_params.get("company")
        as_of = parse_date(request.query_params.get("as_of") or "")
        if not company_raw or not company_raw.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض الميزانية."})
        company_id = int(company_raw)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not as_of:
            raise ValidationError({"as_of": "حدد تاريخ الميزانية بصيغة YYYY-MM-DD."})
        branch_raw = request.query_params.get("branch")
        visible_branches = accessible_branches(request.user, company_id)
        if branch_raw:
            if not branch_raw.isdigit() or not can_access_branch(request.user, int(branch_raw), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            visible_branches = visible_branches.filter(pk=int(branch_raw))
        current_year = FiscalYear.objects.filter(
            company_id=company_id, start_date__lte=as_of, end_date__gte=as_of
        ).order_by("-start_date").first()
        has_current_year_opening = bool(current_year and JournalEntry.objects.filter(
            company_id=company_id, fiscal_year=current_year,
            status=JournalEntry.Status.POSTED,
            source_type=JournalEntry.Source.OPENING,
            entry_date__lte=as_of, branch__in=visible_branches,
        ).exists())
        posted_filter = Q(
            journal_lines__entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
            journal_lines__entry__entry_date__lte=as_of,
            journal_lines__entry__branch__in=visible_branches,
        )
        # When a fiscal year has a posted opening journal, it is the brought-forward
        # balance. Do not add the prior year's closing ledger movements a second time.
        if has_current_year_opening:
            posted_filter &= Q(journal_lines__entry__entry_date__gte=current_year.start_date)
        accounts = ChartOfAccount.objects.filter(company_id=company_id).annotate(
            debit_total=Sum("journal_lines__debit", filter=posted_filter),
            credit_total=Sum("journal_lines__credit", filter=posted_filter),
        ).order_by("nature", "code")
        sections = {key: [] for key in ("asset", "liability", "equity")}
        totals = {key: Decimal("0") for key in sections}
        for account in accounts:
            debit = account.debit_total or Decimal("0")
            credit = account.credit_total or Decimal("0")
            balance = debit - credit if account.nature == ChartOfAccount.Nature.ASSET else credit - debit
            if account.nature not in sections or balance == 0:
                continue
            sections[account.nature].append({
                "account_id": account.pk, "code": account.code, "name_ar": account.name_ar,
                "debit_total": debit, "credit_total": credit, "balance": balance,
            })
            totals[account.nature] += balance
        current_profit = Decimal("0")
        if current_year and current_year.status == FiscalYear.Status.OPEN:
            current_year_entries = JournalEntry.objects.filter(
                company_id=company_id, status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
                entry_date__gte=current_year.start_date, entry_date__lte=as_of,
                branch__in=visible_branches,
            ).exclude(source_type__in=(JournalEntry.Source.OPENING, JournalEntry.Source.CLOSING))
            current_year_lines = JournalLine.objects.filter(entry__in=current_year_entries)
            rev = current_year_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(
                credit=Sum("credit"), debit=Sum("debit")
            )
            exp = current_year_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(
                debit=Sum("debit"), credit=Sum("credit")
            )
            current_profit = (
                (rev["credit"] or Decimal("0")) - (rev["debit"] or Decimal("0"))
                - (exp["debit"] or Decimal("0")) + (exp["credit"] or Decimal("0"))
            )
            if current_profit:
                sections["equity"].append({
                    "account_id": None, "code": "CURRENT-PROFIT",
                    "name_ar": "صافي ربح السنة حتى تاريخ الميزانية",
                    "debit_total": Decimal("0"), "credit_total": Decimal("0"),
                    "balance": current_profit,
                })
                totals["equity"] += current_profit
        return response.Response({
            "company": company_id, "as_of": as_of,
            "assets": sections["asset"], "liabilities": sections["liability"], "equity": sections["equity"],
            "total_assets": totals["asset"], "total_liabilities": totals["liability"],
            "total_equity": totals["equity"], "current_year_profit": current_profit,
            "liabilities_and_equity": totals["liability"] + totals["equity"],
            "difference": totals["asset"] - totals["liability"] - totals["equity"],
        })

    @decorators.action(detail=False, methods=("get",), url_path="income-statement")
    def income_statement(self, request):
        """إعداد قائمة الدخل للفترة المحددة مع فصل تكلفة المبيعات عن المصروفات."""
        from decimal import Decimal
        company_raw = request.query_params.get("company")
        date_from = parse_date(request.query_params.get("date_from") or "")
        date_to = parse_date(request.query_params.get("date_to") or "")
        if not company_raw or not company_raw.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض قائمة الدخل."})
        company_id = int(company_raw)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not date_from or not date_to:
            raise ValidationError({"date_from": "حدد تاريخ البداية والنهاية بصيغة YYYY-MM-DD."})
        if date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        visible_branches = accessible_branches(request.user, company_id)
        branch_raw = request.query_params.get("branch")
        if branch_raw:
            if not branch_raw.isdigit() or not can_access_branch(request.user, int(branch_raw), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            visible_branches = visible_branches.filter(pk=int(branch_raw))
        entries = JournalEntry.objects.filter(
            company_id=company_id, status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
            entry_date__gte=date_from, entry_date__lte=date_to,
            branch__in=visible_branches,
        ).exclude(source_type__in=(JournalEntry.Source.OPENING, JournalEntry.Source.CLOSING))
        grouped = JournalLine.objects.filter(
            entry__in=entries, account__nature__in=(ChartOfAccount.Nature.REVENUE, ChartOfAccount.Nature.EXPENSE)
        ).values("account_id", "account__code", "account__name_ar", "account__nature").annotate(
            debit=Sum("debit"), credit=Sum("credit")
        ).order_by("account__code")
        cogs_role = AccountRole.objects.filter(
            company_id=company_id, role=AccountRole.Role.COGS
        ).values_list("account_id", flat=True).first()
        revenue_rows, cogs_rows, expense_rows = [], [], []
        revenue_total = Decimal("0")
        cogs_total = Decimal("0")
        expense_total = Decimal("0")
        for row in grouped:
            debit = row["debit"] or Decimal("0")
            credit = row["credit"] or Decimal("0")
            if row["account__nature"] == ChartOfAccount.Nature.REVENUE:
                amount = credit - debit
                if amount:
                    revenue_rows.append({"account_id": row["account_id"], "code": row["account__code"], "name_ar": row["account__name_ar"], "amount": amount})
                    revenue_total += amount
            else:
                amount = debit - credit
                if amount:
                    item = {"account_id": row["account_id"], "code": row["account__code"], "name_ar": row["account__name_ar"], "amount": amount}
                    if row["account_id"] == cogs_role:
                        cogs_rows.append(item); cogs_total += amount
                    else:
                        expense_rows.append(item); expense_total += amount
        gross_profit = revenue_total - cogs_total
        net_profit = gross_profit - expense_total
        return response.Response({
            "company": company_id, "date_from": date_from, "date_to": date_to,
            "revenues": revenue_rows, "cost_of_sales": cogs_rows, "operating_and_other_expenses": expense_rows,
            "revenue_total": revenue_total, "cost_of_sales_total": cogs_total,
            "gross_profit": gross_profit, "expenses_total": expense_total, "net_profit": net_profit,
            "note": "يُفصل حساب تكلفة المبيعات عند ربطه بدور تكلفة المبيعات في إعدادات أدوار الحسابات؛ بقية حسابات المصروفات تظهر ضمن المصروفات التشغيلية والأخرى.",
        })


    @decorators.action(detail=False, methods=("get",), url_path="general-ledger")
    def general_ledger(self, request):
        """استخراج حركة الأستاذ العام مع أرصدة أول وآخر الفترة."""
        from decimal import Decimal
        company_raw = request.query_params.get("company")
        account_raw = request.query_params.get("account")
        date_from = parse_date(request.query_params.get("date_from") or "")
        date_to = parse_date(request.query_params.get("date_to") or "")
        if not company_raw or not company_raw.isdigit() or not account_raw or not account_raw.isdigit():
            raise ValidationError({"account": "حدد الشركة والحساب المطلوب لعرض دفتر الأستاذ."})
        company_id, account_id = int(company_raw), int(account_raw)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError({"date_from": "حدد فترة صحيحة بصيغة YYYY-MM-DD."})
        account = ChartOfAccount.objects.filter(company_id=company_id, pk=account_id).first()
        if not account:
            raise ValidationError({"account": "الحساب غير موجود ضمن الشركة المحددة."})
        visible_branches = accessible_branches(request.user, company_id)
        branch_raw = request.query_params.get("branch")
        if branch_raw:
            if not branch_raw.isdigit() or not can_access_branch(request.user, int(branch_raw), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            visible_branches = visible_branches.filter(pk=int(branch_raw))
        base = account.journal_lines.filter(
            entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
            entry__company_id=company_id,
            entry__branch__in=visible_branches,
        )
        branch_ids = list(visible_branches.values_list("pk", flat=True))
        fiscal_year = FiscalYear.objects.filter(
            company_id=company_id, start_date__lte=date_from, end_date__gte=date_from
        ).order_by("-start_date").first()
        snapshot_branch_ids = set()
        if fiscal_year:
            snapshot_branch_ids = set(JournalEntry.objects.filter(
                company_id=company_id, fiscal_year=fiscal_year,
                status=JournalEntry.Status.POSTED,
                source_type=JournalEntry.Source.OPENING,
                entry_date__lte=date_from, branch_id__in=branch_ids,
            ).values_list("branch_id", flat=True).distinct())
        opening_scope = _branch_aware_opening_filter(
            branch_ids, snapshot_branch_ids, date_from,
            fiscal_year.start_date if fiscal_year else date_from,
        )
        opening_lines = base.filter(opening_scope)
        opening_totals = opening_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        debit_nature = account.nature in (ChartOfAccount.Nature.ASSET, ChartOfAccount.Nature.EXPENSE)
        balance_sign = Decimal("1") if debit_nature else Decimal("-1")
        opening_balance = ((opening_totals["debit"] or Decimal("0")) - (opening_totals["credit"] or Decimal("0"))) * balance_sign
        period_lines = base.filter(entry__entry_date__gte=date_from, entry__entry_date__lte=date_to).exclude(
            entry__source_type=JournalEntry.Source.OPENING, entry__entry_date=date_from
        ).select_related("entry", "entry__branch").order_by("entry__entry_date", "entry_id", "id")
        running = opening_balance
        rows = []
        for line in period_lines:
            running += (line.debit - line.credit) * balance_sign
            rows.append({
                "line_id": line.pk, "date": line.entry.entry_date, "entry_id": line.entry_id,
                "entry_number": line.entry.entry_number, "description": line.description or line.entry.description,
                "source_type": line.entry.source_type, "branch": line.entry.branch.name_ar,
                "debit": line.debit, "credit": line.credit, "running_balance": running,
            })
        return response.Response({
            "company": company_id, "account": account_id, "account_code": account.code,
            "account_name": account.name_ar, "date_from": date_from, "date_to": date_to,
            "opening_balance": opening_balance, "rows": rows, "closing_balance": running,
        })

    @decorators.action(detail=False, methods=("get",), url_path="changes-in-equity")
    def changes_in_equity(self, request):
        """إعداد حركة حقوق الملكية وربطها بالأرصدة والنتيجة المالية."""
        from decimal import Decimal
        company_raw = request.query_params.get("company")
        date_from = parse_date(request.query_params.get("date_from") or "")
        date_to = parse_date(request.query_params.get("date_to") or "")
        if not company_raw or not company_raw.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض التغيرات في حقوق الملكية."})
        company_id = int(company_raw)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError({"date_from": "حدد فترة صحيحة بصيغة YYYY-MM-DD."})
        visible_branches = accessible_branches(request.user, company_id)
        branch_raw = request.query_params.get("branch")
        if branch_raw:
            if not branch_raw.isdigit() or not can_access_branch(request.user, int(branch_raw), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            visible_branches = visible_branches.filter(pk=int(branch_raw))
        equity_fiscal_year = FiscalYear.objects.filter(
            company_id=company_id, start_date__lte=date_from, end_date__gte=date_from
        ).order_by("-start_date").first()
        equity_branch_ids = list(visible_branches.values_list("pk", flat=True))
        equity_snapshot_branch_ids = set()
        if equity_fiscal_year:
            equity_snapshot_branch_ids = set(JournalEntry.objects.filter(
                company_id=company_id, fiscal_year=equity_fiscal_year,
                status=JournalEntry.Status.POSTED,
                source_type=JournalEntry.Source.OPENING,
                entry_date__lte=date_from, branch_id__in=equity_branch_ids,
            ).values_list("branch_id", flat=True).distinct())
        opening_filter = _branch_aware_opening_filter(
            equity_branch_ids, equity_snapshot_branch_ids, date_from,
            equity_fiscal_year.start_date if equity_fiscal_year else date_from,
        )
        movement_filter = (
            Q(entry__entry_date__gte=date_from, entry__entry_date__lte=date_to)
            & ~Q(entry__source_type__in=(JournalEntry.Source.OPENING, JournalEntry.Source.CLOSING))
        )
        grouped_equity = JournalLine.objects.filter(
            account__company_id=company_id, account__nature=ChartOfAccount.Nature.EQUITY,
            entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED), entry__company_id=company_id,
            entry__branch__in=visible_branches,
        ).values("account_id", "account__code", "account__name_ar").annotate(
            opening_debit=Sum("debit", filter=opening_filter),
            opening_credit=Sum("credit", filter=opening_filter),
            movement_debit=Sum("debit", filter=movement_filter),
            movement_credit=Sum("credit", filter=movement_filter),
        ).order_by("account__code")
        account_rows = []
        opening_total = Decimal("0")
        movement_total = Decimal("0")
        for item in grouped_equity:
            opening_balance = (item["opening_credit"] or Decimal("0")) - (item["opening_debit"] or Decimal("0"))
            period_movement = (item["movement_credit"] or Decimal("0")) - (item["movement_debit"] or Decimal("0"))
            if opening_balance or period_movement:
                account_rows.append({
                    "account_id": item["account_id"], "code": item["account__code"],
                    "name_ar": item["account__name_ar"], "opening_balance": opening_balance,
                    "period_movement": period_movement,
                    "closing_balance_before_profit": opening_balance + period_movement,
                })
            opening_total += opening_balance
            movement_total += period_movement
        period_entries = JournalEntry.objects.filter(
            company_id=company_id, status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
            entry_date__gte=date_from, entry_date__lte=date_to, branch__in=visible_branches,
        ).exclude(source_type__in=(JournalEntry.Source.OPENING, JournalEntry.Source.CLOSING))
        period_lines = JournalLine.objects.filter(entry__in=period_entries)
        revenue = period_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(
            credit=Sum("credit"), debit=Sum("debit")
        )
        expenses = period_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(
            debit=Sum("debit"), credit=Sum("credit")
        )
        net_profit = (
            (revenue["credit"] or Decimal("0")) - (revenue["debit"] or Decimal("0"))
            - (expenses["debit"] or Decimal("0")) + (expenses["credit"] or Decimal("0"))
        )
        return response.Response({
            "company": company_id, "date_from": date_from, "date_to": date_to,
            "accounts": account_rows, "opening_equity": opening_total,
            "equity_movements": movement_total, "period_net_profit": net_profit,
            "closing_equity": opening_total + movement_total + net_profit,
            "note": "يشمل صافي ربح الفترة بصورة مستقلة، ويستبعد قيود الإقفال من حركة حقوق الملكية لتجنب احتساب الربح مرتين.",
        })

    @decorators.action(detail=False, methods=("get",), url_path="indirect-cash-flow")
    def indirect_cash_flow(self, request):
        """إعداد التدفقات النقدية بالطريقة غير المباشرة مع إظهار فروق المطابقة."""
        from decimal import Decimal
        from django.db.models import Sum
        from assets.models import AssetDepreciation, FixedAsset
        from treasury.models import TreasuryAccount, TreasuryTransaction

        company_raw = request.query_params.get("company")
        date_from_raw = request.query_params.get("date_from")
        date_to_raw = request.query_params.get("date_to")
        if not company_raw or not company_raw.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض قائمة التدفقات النقدية."})
        company_id = int(company_raw)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        date_from = parse_date(date_from_raw or "")
        date_to = parse_date(date_to_raw or "")
        if not date_from or not date_to:
            raise ValidationError({"date_from": "حدد تاريخ البداية والنهاية بصيغة YYYY-MM-DD."})
        if date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        branch_raw = request.query_params.get("branch")
        branch_id = None
        if branch_raw:
            if not branch_raw.isdigit():
                raise ValidationError({"branch": "رقم الفرع غير صحيح."})
            branch_id = int(branch_raw)
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

        visible_branches = accessible_branches(request.user, company_id)
        if branch_id:
            visible_branches = visible_branches.filter(pk=branch_id)
        entries = JournalEntry.objects.filter(
            company_id=company_id, status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
            entry_date__gte=date_from, entry_date__lte=date_to,
            branch__in=visible_branches,
        )
        period_lines = JournalLine.objects.filter(entry__in=entries)
        profit_lines = period_lines.exclude(entry__source_type__in=(JournalEntry.Source.CLOSING, JournalEntry.Source.OPENING))

        revenue_totals = profit_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(
            credit=Sum("credit"), debit=Sum("debit")
        )
        expense_totals = profit_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(
            debit=Sum("debit"), credit=Sum("credit")
        )
        net_profit = (
            (revenue_totals["credit"] or Decimal("0")) - (revenue_totals["debit"] or Decimal("0"))
            - (expense_totals["debit"] or Decimal("0")) + (expense_totals["credit"] or Decimal("0"))
        )

        role_codes = (
            AccountRole.Role.ACCOUNTS_RECEIVABLE, AccountRole.Role.INVENTORY,
            AccountRole.Role.VAT_INPUT, AccountRole.Role.ACCOUNTS_PAYABLE,
            AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED, AccountRole.Role.VAT_OUTPUT,
            AccountRole.Role.PAYROLL_PAYABLE, AccountRole.Role.EMPLOYEE_INSURANCE_PAYABLE,
            AccountRole.Role.EMPLOYER_INSURANCE_PAYABLE,
        )
        role_accounts = {
            item.role: item.account
            for item in AccountRole.objects.filter(company_id=company_id, role__in=role_codes).select_related("account")
        }

        role_account_ids = list({account.pk for account in role_accounts.values()})
        opening_fiscal_year = FiscalYear.objects.filter(
            company_id=company_id, start_date__lte=date_from, end_date__gte=date_from
        ).order_by("-start_date").first()
        cash_flow_branch_ids = list(visible_branches.values_list("pk", flat=True))
        opening_snapshot_branch_ids = set()
        if opening_fiscal_year:
            opening_snapshot_branch_ids = set(JournalEntry.objects.filter(
                company_id=company_id, fiscal_year=opening_fiscal_year,
                status=JournalEntry.Status.POSTED,
                source_type=JournalEntry.Source.OPENING,
                entry_date__lte=date_from, branch_id__in=cash_flow_branch_ids,
            ).values_list("branch_id", flat=True).distinct())
        opening_balance_filter = _branch_aware_opening_filter(
            cash_flow_branch_ids, opening_snapshot_branch_ids, date_from,
            opening_fiscal_year.start_date if opening_fiscal_year else date_from,
        )

        closing_fiscal_year = FiscalYear.objects.filter(
            company_id=company_id, start_date__lte=date_to, end_date__gte=date_to
        ).order_by("-start_date").first()
        closing_snapshot_branch_ids = set()
        if closing_fiscal_year:
            closing_snapshot_branch_ids = set(JournalEntry.objects.filter(
                company_id=company_id, fiscal_year=closing_fiscal_year,
                status=JournalEntry.Status.POSTED,
                source_type=JournalEntry.Source.OPENING,
                entry_date__lte=date_to, branch_id__in=cash_flow_branch_ids,
            ).values_list("branch_id", flat=True).distinct())
        closing_balance_filter = _branch_aware_balance_filter(
            cash_flow_branch_ids, closing_snapshot_branch_ids, date_to,
            closing_fiscal_year.start_date if closing_fiscal_year else date_to,
        )
        role_balance_rows = {
            row["account_id"]: row
            for row in JournalLine.objects.filter(
                account_id__in=role_account_ids,
                entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
                entry__company_id=company_id,
                entry__branch__in=visible_branches,
            ).values("account_id").annotate(
                opening_debit=Sum("debit", filter=opening_balance_filter),
                opening_credit=Sum("credit", filter=opening_balance_filter),
                closing_debit=Sum("debit", filter=closing_balance_filter),
                closing_credit=Sum("credit", filter=closing_balance_filter),
            )
        }

        working_capital = []
        operating_adjustments = Decimal("0")
        missing_roles = []
        for role in role_codes:
            account = role_accounts.get(role)
            if not account:
                missing_roles.append(role)
                continue
            totals = role_balance_rows.get(account.pk, {})
            opening_debit = totals.get("opening_debit") or Decimal("0")
            opening_credit = totals.get("opening_credit") or Decimal("0")
            closing_debit = totals.get("closing_debit") or Decimal("0")
            closing_credit = totals.get("closing_credit") or Decimal("0")
            is_current_asset = account.nature == ChartOfAccount.Nature.ASSET
            opening = opening_debit - opening_credit if is_current_asset else opening_credit - opening_debit
            closing = closing_debit - closing_credit if is_current_asset else closing_credit - closing_debit
            movement = closing - opening
            adjustment = -movement if is_current_asset else movement
            operating_adjustments += adjustment
            working_capital.append({
                "role": role, "account_code": account.code, "account_name": account.name_ar,
                "opening_balance": opening, "closing_balance": closing,
                "change": movement, "cash_flow_adjustment": adjustment,
            })

        depreciation_qs = AssetDepreciation.objects.filter(
            asset__company_id=company_id, period_date__gte=date_from, period_date__lte=date_to,
            journal_entry__status=JournalEntry.Status.POSTED,
        )
        depreciation_qs = depreciation_qs.filter(asset__branch__in=visible_branches)
        depreciation_addback = depreciation_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0")

        disposals = FixedAsset.objects.filter(
            company_id=company_id, status=FixedAsset.Status.DISPOSED,
            disposed_at__gte=date_from, disposed_at__lte=date_to,
            disposal_journal__status=JournalEntry.Status.POSTED,
        ).select_related("disposal_journal")
        disposals = disposals.filter(branch__in=visible_branches)
        disposal_ids = list(disposals.values_list("disposal_journal_id", flat=True))
        disposal_lines = JournalLine.objects.filter(
            entry_id__in=disposal_ids
        )
        disposal_gain = (
            (disposal_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(v=Sum("credit"))["v"] or Decimal("0"))
            - (disposal_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(v=Sum("debit"))["v"] or Decimal("0"))
        )
        disposal_loss = (
            (disposal_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(v=Sum("debit"))["v"] or Decimal("0"))
            - (disposal_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(v=Sum("credit"))["v"] or Decimal("0"))
        )
        non_cash_adjustments = depreciation_addback + disposal_loss - disposal_gain
        operating_cash_flow = net_profit + operating_adjustments + non_cash_adjustments

        treasury_rows = TreasuryTransaction.objects.filter(
            company_id=company_id, status=TreasuryTransaction.Status.POSTED,
            transaction_date__gte=date_from, transaction_date__lte=date_to,
        ).exclude(kind=TreasuryTransaction.Kind.TRANSFER)
        treasury_rows = treasury_rows.filter(branch__in=visible_branches)
        investing_rows = treasury_rows.filter(cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING)
        financing_rows = treasury_rows.filter(cash_flow_category=TreasuryTransaction.CashFlowCategory.FINANCING)

        def cash_category_total(queryset):
            """تنفيذ وظيفة cash_category_total ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
            receipts = queryset.filter(kind=TreasuryTransaction.Kind.RECEIPT).aggregate(v=Sum("amount"))["v"] or Decimal("0")
            payments = queryset.filter(kind=TreasuryTransaction.Kind.PAYMENT).aggregate(v=Sum("amount"))["v"] or Decimal("0")
            return receipts - payments

        investing_cash_flow = cash_category_total(investing_rows)
        financing_cash_flow = cash_category_total(financing_rows)
        net_change = operating_cash_flow + investing_cash_flow + financing_cash_flow

        treasury_accounts = TreasuryAccount.objects.filter(company_id=company_id)
        treasury_accounts = treasury_accounts.filter(Q(branch__isnull=True) | Q(branch__in=visible_branches))
        cash_account_ids = list(treasury_accounts.values_list("account_id", flat=True))
        cash_lines = JournalLine.objects.filter(
            account_id__in=cash_account_ids, entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
            entry__company_id=company_id, entry__entry_date__gte=date_from, entry__entry_date__lte=date_to,
        ).exclude(entry__source_type__in=(JournalEntry.Source.OPENING, JournalEntry.Source.CLOSING))
        cash_lines = cash_lines.filter(entry__branch__in=visible_branches)
        cash_totals = cash_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        ledger_cash_change = (cash_totals["debit"] or Decimal("0")) - (cash_totals["credit"] or Decimal("0"))

        return response.Response({
            "company": company_id, "branch": branch_id, "date_from": date_from, "date_to": date_to,
            "net_profit": net_profit,
            "non_cash_adjustments": {
                "depreciation": depreciation_addback, "disposal_loss": disposal_loss,
                "disposal_gain": disposal_gain, "net": non_cash_adjustments,
            },
            "working_capital": working_capital,
            "missing_account_roles": missing_roles,
            "operating_adjustments": operating_adjustments,
            "operating_cash_flow": operating_cash_flow,
            "investing_cash_flow": investing_cash_flow,
            "financing_cash_flow": financing_cash_flow,
            "net_change_in_cash": net_change,
            "ledger_cash_change": ledger_cash_change,
            "reconciliation_difference": ledger_cash_change - net_change,
            "investing_transactions": list(investing_rows.values(
                "id", "transaction_date", "document_number", "description", "kind", "amount"
            ).order_by("transaction_date", "id")),
            "financing_transactions": list(financing_rows.values(
                "id", "transaction_date", "document_number", "description", "kind", "amount"
            ).order_by("transaction_date", "id")),
            "note": "القائمة غير المباشرة تعتمد على أدوار الحسابات المرتبطة. اربط حسابات العملاء والمخزون والضريبة والموردين والبضاعة المستلمة غير المفوترة والمرتبات المستحقة وحصتي التأمينات للحصول على تسوية مكتملة. فرق المطابقة يُعرض للمراجعة ولا يُرحّل تلقائيًا.",
        })

    @decorators.action(detail=False, methods=("get",), url_path="vat-return")
    def vat_return(self, request):
        """تجميع بيانات الإقرار الضريبي حسب الأكواد والفترات الضريبية."""
        from decimal import Decimal

        company_id = request.query_params.get("company")
        date_from = parse_date(request.query_params.get("date_from", ""))
        date_to = parse_date(request.query_params.get("date_to", ""))
        branch_id = request.query_params.get("branch")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة."})
        if not date_from or not date_to or date_to < date_from:
            raise ValidationError({"date_from": "حدد تاريخ بداية ونهاية صحيحين."})
        if not can_access_company(request.user, int(company_id)):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        company = Company.objects.select_related("base_currency").get(pk=company_id)
        if company.country not in (Company.Country.EGYPT, Company.Country.SAUDI_ARABIA):
            raise ValidationError({"company": "تقرير ضريبة القيمة المضافة متاح لمصر والسعودية فقط."})

        visible_branches = accessible_branches(request.user, company.pk)
        if request.user.is_superuser and not is_platform_owner(request.user):
            visible_branches = company.branches.all()
        if branch_id:
            if not branch_id.isdigit() or not can_access_branch(request.user, int(branch_id), company.pk):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            visible_branches = visible_branches.filter(pk=branch_id)
        branch_ids = list(visible_branches.values_list("pk", flat=True))

        sales_rows = list(SalesInvoiceLine.objects.filter(
            invoice__company_id=company.pk,
            invoice__status="posted",
            invoice__invoice_date__gte=date_from,
            invoice__invoice_date__lte=date_to,
            invoice__journal_entry__status=JournalEntry.Status.POSTED,
            invoice__journal_entry__branch_id__in=branch_ids,
        ).values(
            "tax_rate", "tax_code_id", "tax_code__code", "tax_code__name_ar",
            "tax_code__category",
        ).annotate(
            taxable_base=Sum("net_amount"), tax_amount=Sum("tax_amount"),
            gross_amount=Sum("total_amount"),
        ).order_by("tax_code__code", "tax_rate"))

        purchase_rows = list(PurchaseInvoiceLine.objects.filter(
            invoice__company_id=company.pk,
            invoice__status="posted",
            invoice__invoice_date__gte=date_from,
            invoice__invoice_date__lte=date_to,
            invoice__journal_entry__status=JournalEntry.Status.POSTED,
            invoice__journal_entry__branch_id__in=branch_ids,
        ).values(
            "tax_rate", "tax_code_id", "tax_code__code", "tax_code__name_ar",
            "tax_code__category", "tax_code__input_tax_recoverable",
        ).annotate(
            taxable_base=Sum("net_amount"), tax_amount=Sum("tax_amount"),
            gross_amount=Sum("total_amount"),
        ).order_by("tax_code__code", "tax_rate"))

        def summarize(rows, purchases=False):
            """تنفيذ وظيفة summarize ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
            normalized = []
            by_category = {}
            by_rate = {}
            taxable_base = Decimal("0")
            tax_amount = Decimal("0")
            gross_amount = Decimal("0")
            recoverable_tax = Decimal("0")
            for row in rows:
                base = row["taxable_base"] or Decimal("0")
                tax = row["tax_amount"] or Decimal("0")
                gross = row["gross_amount"] or Decimal("0")
                category = row.get("tax_code__category") or "legacy_unclassified"
                code = row.get("tax_code__code")
                recoverable = (not purchases or row.get("tax_code_id") is None
                               or row.get("tax_code__input_tax_recoverable", True))
                normalized.append({
                    "tax_code_id": row.get("tax_code_id"),
                    "tax_code": code,
                    "tax_name": row.get("tax_code__name_ar"),
                    "category": category,
                    "tax_rate": str(row["tax_rate"]),
                    "taxable_base": str(base),
                    "tax_amount": str(tax),
                    "recoverable_tax_amount": str(tax if recoverable else Decimal("0")),
                    "gross_amount": str(gross),
                })
                taxable_base += base
                tax_amount += tax
                gross_amount += gross
                if recoverable:
                    recoverable_tax += tax
                category_row = by_category.setdefault(category, {
                    "taxable_base": Decimal("0"), "tax_amount": Decimal("0"),
                    "gross_amount": Decimal("0"), "recoverable_tax_amount": Decimal("0"),
                })
                category_row["taxable_base"] += base
                category_row["tax_amount"] += tax
                category_row["gross_amount"] += gross
                if recoverable:
                    category_row["recoverable_tax_amount"] += tax
                rate_key = str(row["tax_rate"])
                rate_row = by_rate.setdefault(rate_key, {
                    "taxable_base": Decimal("0"), "tax_amount": Decimal("0"),
                    "gross_amount": Decimal("0"),
                })
                rate_row["taxable_base"] += base
                rate_row["tax_amount"] += tax
                rate_row["gross_amount"] += gross

            def strings(values):
                """تنفيذ وظيفة strings ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
                return {key: str(value) for key, value in values.items()}

            return {
                "taxable_base": str(taxable_base),
                "tax_amount": str(tax_amount),
                "recoverable_tax_amount": str(recoverable_tax),
                "gross_amount": str(gross_amount),
                "by_rate": [{"tax_rate": rate, **strings(values)} for rate, values in by_rate.items()],
                "by_category": [{"category": category, **strings(values)}
                                for category, values in by_category.items()],
                "by_code": normalized,
            }

        sales = summarize(sales_rows)
        purchases = summarize(purchase_rows, purchases=True)
        output_tax = Decimal(sales["tax_amount"])
        input_tax = Decimal(purchases["recoverable_tax_amount"])
        return response.Response({
            "company": company.pk,
            "country": company.country,
            "currency": company.base_currency.code,
            "date_from": date_from,
            "date_to": date_to,
            "branch": int(branch_id) if branch_id else None,
            "scope_branch_ids": branch_ids,
            "sales": sales,
            "purchases": purchases,
            "output_tax": str(output_tax),
            "input_tax": str(input_tax),
            "net_tax_payable": str(output_tax - input_tax),
            "report_kind": "internal_vat_reconciliation",
            "note": "تقرير داخلي للمراجعة والتجهيز؛ لا يمثل إقرارًا رسميًا ولا يرسل بيانات إلى مصلحة الضرائب المصرية أو هيئة الزكاة والضريبة والجمارك.",
        })

    @decorators.action(detail=True, methods=("get",))
    def totals(self, request, pk=None):
        """إرجاع إجماليات السجل وتفاصيله المالية المرتبطة."""
        entry = self.get_object()
        lines = list(entry.lines.all())
        debit = sum((line.debit for line in lines), start=0)
        credit = sum((line.credit for line in lines), start=0)
        return response.Response({"debit_total": debit, "credit_total": credit, "difference": debit - credit})
