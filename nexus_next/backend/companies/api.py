from calendar import monthrange
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from django.db.models import Count, F, Q, Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .access import accessible_branches, accessible_companies, has_company_role, is_platform_owner
from .models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalPeriod, FiscalYear
from .serializers import BranchSerializer, CompanyMembershipSerializer, CompanySerializer, CurrencySerializer, ExchangeRateSerializer, FiscalYearSerializer


class ReadOnlyOrAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        """تنفيذ وظيفة has_permission ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
        if request.method in permissions.SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)


class CompanyAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        """تنفيذ وظيفة has_permission ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
        return bool(request.user and request.user.is_authenticated)


class CurrencyViewSet(viewsets.ModelViewSet):
    queryset = Currency.objects.all()
    serializer_class = CurrencySerializer
    permission_classes = (ReadOnlyOrAdmin,)


class ExchangeRateViewSet(viewsets.ModelViewSet):
    """إدارة أسعار الصرف التاريخية ضمن نطاق الشركة."""
    serializer_class = ExchangeRateSerializer
    permission_classes = (CompanyAdminOrReadOnly,)
    http_method_names = ("get", "post", "head", "options")

    def get_queryset(self):
        queryset = ExchangeRate.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related("company", "currency", "company__base_currency")
        company_id = self.request.query_params.get("company")
        currency_id = self.request.query_params.get("currency")
        if company_id:
            queryset = queryset.filter(company_id=company_id)
        if currency_id:
            queryset = queryset.filter(currency_id=currency_id)
        return queryset

    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        company = serializer.validated_data["company"]
        if not has_company_role(self.request.user, company.pk, ("admin",)):
            raise PermissionDenied("إضافة أسعار الصرف متاحة لمدير الشركة فقط.")
        serializer.save()


class CompanyViewSet(viewsets.ModelViewSet):
    serializer_class = CompanySerializer
    permission_classes = (CompanyAdminOrReadOnly,)

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        if is_platform_owner(self.request.user):
            # Owner sees company registry metadata only; tenant ledgers remain membership-scoped.
            return Company.objects.all().select_related("base_currency")
        return accessible_companies(self.request.user).select_related("base_currency")

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        from rest_framework.exceptions import PermissionDenied
        if not is_platform_owner(self.request.user):
            raise PermissionDenied("إنشاء الشركات متاح لمالك المنصة المسجل فقط.")
        # لا ننشئ عضوية تشغيلية للمالك؛ تعيين مدير الشركة خطوة مستقلة ومقصودة.
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        from rest_framework.exceptions import PermissionDenied
        owner = is_platform_owner(self.request.user)
        if not (owner or has_company_role(self.request.user, self.get_object().pk, ("admin",))):
            raise PermissionDenied("تعديل بيانات الشركة متاح لمالك المنصة أو مدير الشركة فقط.")
        requested_active = serializer.validated_data.get("is_active", self.get_object().is_active)
        if not owner and requested_active != self.get_object().is_active:
            raise PermissionDenied("إيقاف الشركة أو إعادة تفعيلها من اختصاص مالك المنصة فقط.")
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        if not (
            is_platform_owner(self.request.user)
            or has_company_role(self.request.user, instance.pk, ("admin",))
        ):
            raise PermissionDenied("إدارة حالة الشركة متاحة لمالك المنصة أو مدير الشركة فقط.")
        raise ValidationError("الحذف النهائي للشركات غير متاح؛ أوقف الشركة للاحتفاظ بالسجلات والمراجع المالية.")



class CompanyMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyMembershipSerializer
    permission_classes = (CompanyAdminOrReadOnly,)
    http_method_names = ("get", "post", "patch", "head", "options")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        qs = CompanyMembership.objects.all() if is_platform_owner(self.request.user) else CompanyMembership.objects.filter(
            company__in=accessible_companies(self.request.user)
        )
        qs = qs.select_related("company", "user").prefetch_related("branches")
        if not self.request.user.is_superuser and not is_platform_owner(self.request.user):
            qs = qs.filter(
                Q(user=self.request.user)
                | Q(
                    company__memberships__user=self.request.user,
                    company__memberships__is_active=True,
                    company__memberships__role=CompanyMembership.Role.ADMIN,
                )
            ).distinct()
        company_id = self.request.query_params.get("company")
        return qs.filter(company_id=company_id) if company_id else qs

    def _require_admin(self, company_id):
        """تنفيذ وظيفة _require_admin ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
        from rest_framework.exceptions import PermissionDenied
        if not (is_platform_owner(self.request.user) or has_company_role(self.request.user, company_id, ("admin",))):
            raise PermissionDenied("إدارة المستخدمين والصلاحيات متاحة لمالك المنصة أو مدير الشركة فقط.")

    def get_object(self):
        """تنفيذ وظيفة get_object ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
        instance = super().get_object()
        self._require_admin(instance.company_id)
        return instance

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        from rest_framework.exceptions import ValidationError
        try:
            company_id = int(request.data.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة أولًا."})
        self._require_admin(company_id)
        return super().create(request, *args, **kwargs)

    def _protect_last_admin(self, membership, new_role, new_active):
        """تنفيذ وظيفة _protect_last_admin ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
        from rest_framework.exceptions import ValidationError
        remains_admin = (
            membership.is_active and membership.role == CompanyMembership.Role.ADMIN
            and (not new_active or new_role != CompanyMembership.Role.ADMIN)
        )
        if remains_admin:
            active_admins = CompanyMembership.objects.filter(
                company_id=membership.company_id, is_active=True,
                role=CompanyMembership.Role.ADMIN,
            ).count()
            if active_admins <= 1:
                raise ValidationError("لا يمكن تعطيل أو تخفيض صلاحية آخر مدير نشط للشركة.")

    @action(detail=False, methods=("get",), url_path="current")
    def current(self, request):
        """إرجاع سياق الشركة والفرع الحاليين المسموح بهما للمستخدم."""
        from rest_framework.exceptions import NotFound, ValidationError
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة أولًا."})
        if is_platform_owner(request.user):
            raise NotFound("مالك المنصة لا يمتلك عضوية تشغيلية داخل الشركات.")
        membership = CompanyMembership.objects.filter(
            user=request.user, company_id=company_id, is_active=True,
        ).first()
        if not membership and (not request.user.is_superuser or is_platform_owner(request.user)):
            raise NotFound("لا توجد عضوية نشطة للمستخدم في الشركة.")
        if request.user.is_superuser and not is_platform_owner(request.user):
            return Response({"role": "admin", "all_branches": True, "branches": []})
        return Response(self.get_serializer(membership).data)

    @action(detail=False, methods=("get",), url_path="users")
    def users(self, request):
        """عرض المستخدمين المرتبطين بالشركة ضمن حدود صلاحيات الإدارة."""
        from rest_framework.exceptions import ValidationError
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة أولًا."})
        self._require_admin(company_id)
        term = request.query_params.get("search", "").strip()
        if len(term) < 2:
            return Response([])
        User = get_user_model()
        users = User.objects.filter(is_active=True).filter(
            Q(username__icontains=term) | Q(display_name__icontains=term)
        ).exclude(company_memberships__company_id=company_id).exclude(
            platform_owner_profile__isnull=False
        ).order_by("username")[:25]
        return Response([
            {"id": user.pk, "username": user.username, "display_name": user.display_name}
            for user in users
        ])

    @action(detail=False, methods=("post",), url_path="create-company-admin")
    @transaction.atomic
    def create_company_admin(self, request):
        """إنشاء حساب مستخدم عادي وتعيينه مديرًا لشركة في معاملة واحدة."""
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.contrib.auth.password_validation import validate_password
        from rest_framework.exceptions import PermissionDenied, ValidationError

        if not is_platform_owner(request.user):
            raise PermissionDenied("إنشاء حساب مدير شركة متاح لمالك المنصة فقط.")
        try:
            company_id = int(request.data.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة أولًا."})
        company = Company.objects.select_for_update().filter(pk=company_id).first()
        if not company:
            raise ValidationError({"company": "الشركة المحددة غير موجودة."})
        if not company.is_active:
            raise ValidationError({"company": "أعد تفعيل الشركة قبل تعيين مدير جديد."})

        username = str(request.data.get("username", "")).strip()
        display_name = str(request.data.get("display_name", "")).strip()
        email = str(request.data.get("email", "")).strip()
        password = request.data.get("password", "")
        if not username or len(username) > 150:
            raise ValidationError({"username": "أدخل اسم مستخدم صالحًا لا يتجاوز 150 حرفًا."})
        if not isinstance(password, str) or not password:
            raise ValidationError({"password": "أدخل كلمة مرور للحساب الجديد."})
        User = get_user_model()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError({"username": "اسم المستخدم مستخدم بالفعل."})
        try:
            validate_password(password)
        except DjangoValidationError as exc:
            raise ValidationError({"password": list(exc.messages)})
        user = User.objects.create_user(
            username=username, password=password, display_name=display_name, email=email,
        )
        membership = CompanyMembership.objects.create(
            company=company, user=user, role=CompanyMembership.Role.ADMIN,
            all_branches=True, is_active=True,
        )
        return Response(CompanyMembershipSerializer(membership).data, status=201)

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        from rest_framework.exceptions import PermissionDenied
        company = serializer.validated_data["company"]
        target_user = serializer.validated_data["user"]
        self._require_admin(company.pk)
        if is_platform_owner(target_user):
            raise PermissionDenied("حساب مالك المنصة منفصل ولا يمكن إضافته كعضو تشغيلي في شركة.")
        serializer.save()

    @transaction.atomic
    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        instance = serializer.instance
        Company.objects.select_for_update().get(pk=instance.company_id)
        locked = CompanyMembership.objects.select_for_update().get(pk=instance.pk)
        self._require_admin(locked.company_id)
        new_role = serializer.validated_data.get("role", locked.role)
        new_active = serializer.validated_data.get("is_active", locked.is_active)
        self._protect_last_admin(locked, new_role, new_active)
        serializer.instance = locked
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("لا تحذف عضويات المستخدمين؛ عطّل العضوية للاحتفاظ بسجل الصلاحيات.")


class BranchViewSet(viewsets.ModelViewSet):
    serializer_class = BranchSerializer
    permission_classes = (CompanyAdminOrReadOnly,)

    def get_queryset(self):
        """تجهيز الفروع النشطة للتشغيل، والسماح لمدير الشركة بعرض الموقوفة في شاشة الإعداد فقط."""
        if self.request.query_params.get("include_inactive") != "true":
            return accessible_branches(self.request.user).select_related("company")
        company_id = self.request.query_params.get("company")
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            queryset = Branch.objects.filter(company__in=accessible_companies(self.request.user))
        else:
            admin_company_ids = CompanyMembership.objects.filter(
                user=self.request.user,
                is_active=True,
                role=CompanyMembership.Role.ADMIN,
                company__is_active=True,
            ).values_list("company_id", flat=True)
            queryset = Branch.objects.filter(company_id__in=admin_company_ids, company__is_active=True)
        if company_id:
            queryset = queryset.filter(company_id=company_id)
        return queryset.select_related("company")

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        company = serializer.validated_data["company"]
        if not has_company_role(self.request.user, company.pk, ("admin",)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("إنشاء الفروع متاح لمدير الشركة فقط.")
        serializer.save()

    @transaction.atomic
    def perform_update(self, serializer):
        """حفظ تعديلات الفرع بقفل الشركة لمنع إيقاف آخر فرع عبر طلبات متزامنة."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        branch_id = serializer.instance.pk
        current_company_id = serializer.instance.company_id
        # قفل صف الشركة يجعل فحص عدد الفروع النشطة وتحديث الحالة عملية متسلسلة.
        Company.objects.select_for_update().get(pk=current_company_id)
        branch = Branch.objects.select_for_update().get(pk=branch_id)
        if not has_company_role(self.request.user, current_company_id, ("admin",)):
            raise PermissionDenied("تعديل الفروع متاح لمدير الشركة فقط.")
        target_company = serializer.validated_data.get("company", branch.company)
        if target_company.pk != current_company_id:
            raise ValidationError({"company": "لا يمكن نقل فرع قائم إلى شركة أخرى."})
        if branch.is_active and serializer.validated_data.get("is_active") is False:
            active_count = Branch.objects.filter(company_id=current_company_id, is_active=True).count()
            if active_count <= 1:
                raise ValidationError({"is_active": "لا يمكن إيقاف آخر فرع نشط بالشركة؛ أضف فرعًا بديلًا أولًا."})
        serializer.instance = branch
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف الفروع متاح لمدير الشركة فقط.")
        if instance.audit_events.exists():
            raise ValidationError("لا يمكن حذف فرع له سجل تدقيق؛ عطّل الفرع بدل حذفه.")
        instance.delete()


class FiscalYearViewSet(viewsets.ModelViewSet):
    serializer_class = FiscalYearSerializer
    permission_classes = (CompanyAdminOrReadOnly,)

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        return FiscalYear.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related("company")

    @transaction.atomic
    def perform_create(self, serializer):
        """حفظ السنة مع قفل الشركة لمنع إنشاء فترتين ماليتين متداخلتين بالتزامن."""
        company = serializer.validated_data["company"]
        if not has_company_role(self.request.user, company.pk, ("admin",)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("إنشاء السنوات المالية متاح لمدير الشركة فقط.")
        Company.objects.select_for_update().get(pk=company.pk)
        serializer.save()

    @transaction.atomic
    def perform_update(self, serializer):
        """تحديث بيانات السنة قبل تسجيل أي قيود مالية مع منع تعديل حدود فترة مستخدمة."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        current_company_id = serializer.instance.company_id
        if not has_company_role(self.request.user, current_company_id, ("admin",)):
            raise PermissionDenied("تعديل السنوات المالية متاح لمدير الشركة فقط.")
        Company.objects.select_for_update().get(pk=current_company_id)
        FiscalYear.objects.select_for_update().get(pk=serializer.instance.pk)
        if serializer.instance.journal_entries.exists():
            raise ValidationError("لا يمكن تعديل اسم أو تواريخ سنة مالية لها قيود؛ حافظًا على ارتباط القيود بالفترة الأصلية.")
        serializer.save()

    def perform_destroy(self, instance):
        """منع حذف سنة مالية لها قيود أو مستندات مرتبطة."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف السنوات المالية متاح لمدير الشركة فقط.")
        if instance.journal_entries.exists():
            raise ValidationError("لا يمكن حذف سنة مالية لها قيود؛ استخدم إجراءات الإقفال المعتمدة.")
        instance.delete()

    def _closing_review(self, fiscal_year):
        """فحص توازن القيود وإقفال نتائج الأعمال ومطابقة افتتاح السنة التالية."""
        from accounting.models import JournalEntry, JournalLine, ChartOfAccount

        entries = JournalEntry.objects.filter(fiscal_year=fiscal_year)
        drafts = entries.filter(status=JournalEntry.Status.DRAFT).count()
        financial_entries = entries.filter(
            status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)
        ).annotate(
            line_count=Count("lines"),
            debit_total=Coalesce(
                Sum("lines__debit"), Value(Decimal("0")),
                output_field=DecimalField(max_digits=24, decimal_places=4),
            ),
            credit_total=Coalesce(
                Sum("lines__credit"), Value(Decimal("0")),
                output_field=DecimalField(max_digits=24, decimal_places=4),
            ),
        )
        invalid_entries = financial_entries.filter(
            Q(line_count__lt=2) | ~Q(debit_total=F("credit_total"))
        ).count()

        pnl_lines = JournalLine.objects.filter(
            entry__fiscal_year=fiscal_year,
            entry__status=JournalEntry.Status.POSTED,
            account__nature__in=(ChartOfAccount.Nature.REVENUE, ChartOfAccount.Nature.EXPENSE),
        )
        pnl_before_closing = pnl_lines.exclude(entry__source_type=JournalEntry.Source.CLOSING).values(
            "entry__branch_id", "account_id"
        ).annotate(net=Sum(F("debit") - F("credit"))).exclude(net=Decimal("0"))
        pnl_activity_accounts = pnl_before_closing.count()
        pnl_after_closing = pnl_lines.values(
            "entry__branch_id", "account_id"
        ).annotate(net=Sum(F("debit") - F("credit"))).exclude(net=Decimal("0"))
        unclosed_pnl_accounts = pnl_after_closing.count()

        closing_entry_exists = entries.filter(
            source_type=JournalEntry.Source.CLOSING,
            status=JournalEntry.Status.POSTED,
            entry_date=fiscal_year.end_date,
        ).exists()
        closing_entry_ok = (
            pnl_activity_accounts == 0
            or (closing_entry_exists and unclosed_pnl_accounts == 0)
        )

        next_year = FiscalYear.objects.filter(
            company_id=fiscal_year.company_id,
            start_date__gt=fiscal_year.end_date,
        ).order_by("start_date").first()
        opening_entry_exists = False
        opening_entry_required = False
        opening_balance_mismatches = 0
        if next_year:
            balance_sheet_natures = (
                ChartOfAccount.Nature.ASSET,
                ChartOfAccount.Nature.LIABILITY,
                ChartOfAccount.Nature.EQUITY,
            )
            expected_rows = JournalLine.objects.filter(
                entry__company_id=fiscal_year.company_id,
                entry__entry_date__lte=fiscal_year.end_date,
                entry__status=JournalEntry.Status.POSTED,
                account__nature__in=balance_sheet_natures,
            ).values("entry__branch_id", "account_id").annotate(
                net=Sum(F("debit") - F("credit"))
            )
            expected = {
                (row["entry__branch_id"], row["account_id"]): row["net"]
                for row in expected_rows if row["net"] != 0
            }
            opening_entries = JournalEntry.objects.filter(
                fiscal_year=next_year,
                source_type=JournalEntry.Source.OPENING,
                status=JournalEntry.Status.POSTED,
                entry_date=next_year.start_date,
            )
            opening_entry_exists = opening_entries.exists()
            opening_entry_required = bool(expected)
            actual_rows = JournalLine.objects.filter(
                entry__in=opening_entries,
                account__nature__in=balance_sheet_natures,
            ).values("entry__branch_id", "account_id").annotate(
                net=Sum(F("debit") - F("credit"))
            )
            actual = {
                (row["entry__branch_id"], row["account_id"]): row["net"]
                for row in actual_rows if row["net"] != 0
            }
            opening_balance_mismatches = sum(
                1 for key in (set(expected) | set(actual))
                if expected.get(key, Decimal("0")) != actual.get(key, Decimal("0"))
            )
            opening_non_balance_sheet_lines = JournalLine.objects.filter(
                entry__in=opening_entries,
            ).exclude(account__nature__in=balance_sheet_natures).count()
            opening_balance_mismatches += opening_non_balance_sheet_lines

        opening_entry_ok = not next_year or opening_balance_mismatches == 0
        checks = {
            "no_drafts": drafts == 0,
            "balanced_entries": invalid_entries == 0,
            "closing_entry": closing_entry_ok,
            "next_year_opening": opening_entry_ok,
        }
        return {
            "fiscal_year": fiscal_year.pk,
            "status": fiscal_year.status,
            "draft_entries": drafts,
            "unbalanced_entries": invalid_entries,
            "closing_entry_required": pnl_activity_accounts > 0,
            "closing_entry_exists": closing_entry_exists,
            "closing_entry_posted": closing_entry_ok,
            "unclosed_income_statement_accounts": unclosed_pnl_accounts,
            "next_fiscal_year": next_year.pk if next_year else None,
            "opening_entry_required": opening_entry_required,
            "opening_entry_exists": opening_entry_exists,
            "opening_entry_posted": opening_entry_ok,
            "opening_balance_mismatches": opening_balance_mismatches,
            "can_close": fiscal_year.status == FiscalYear.Status.OPEN and all(checks.values()),
            "checks": checks,
        }

    @action(detail=True, methods=("get",))
    def closing_review(self, request, pk=None):
        """إرجاع قائمة مراجعة الإقفال دون تنفيذ أي تغييرات."""
        fiscal_year = self.get_object()
        return Response(self._closing_review(fiscal_year))

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def close(self, request, pk=None):
        """إقفال السنة بعد اجتياز المراجعة ووجود قيد الإقفال والافتتاح المطلوبين."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        fiscal_year = self.get_object()
        if not has_company_role(request.user, fiscal_year.company_id, ("admin",)):
            raise PermissionDenied("إقفال السنة المالية متاح لمدير الشركة فقط.")
        Company.objects.select_for_update().get(pk=fiscal_year.company_id)
        fiscal_year = FiscalYear.objects.select_for_update().get(pk=fiscal_year.pk)
        review = self._closing_review(fiscal_year)
        if not review["can_close"]:
            raise ValidationError({
                "detail": "لا يمكن إقفال السنة قبل استكمال جميع متطلبات المراجعة.",
                "review": review,
            })
        fiscal_year.status = FiscalYear.Status.CLOSED
        fiscal_year.save(update_fields=("status",))
        from audit.models import AuditEvent
        AuditEvent.objects.create(
            actor=request.user,
            company_id=fiscal_year.company_id,
            action=AuditEvent.Action.ACTION,
            resource="companies.fiscal_year.closed",
            object_id=str(fiscal_year.pk),
            http_method="POST",
            status_code=200,
            succeeded=True,
            metadata={
                "name": fiscal_year.name,
                "start_date": fiscal_year.start_date.isoformat(),
                "end_date": fiscal_year.end_date.isoformat(),
                "review": review,
            },
        )
        return Response({"detail": "تم إقفال السنة المالية بعد اجتياز المراجعة.", "review": self._closing_review(fiscal_year)})


    @action(detail=True, methods=("get",), url_path="periods")
    def periods(self, request, pk=None):
        """عرض الفترات الشهرية للسنة المالية وحالات إقفالها."""
        fiscal_year = self.get_object()
        rows = FiscalPeriod.objects.filter(fiscal_year=fiscal_year).select_related(
            "closed_by", "reopened_by",
        ).order_by("start_date", "id")
        return Response([{
            "id": row.pk, "name": row.name, "start_date": row.start_date,
            "end_date": row.end_date, "status": row.status,
            "closed_at": row.closed_at, "closed_by": row.closed_by_id,
            "reopened_at": row.reopened_at, "reopened_by": row.reopened_by_id,
            "reopen_reason": row.reopen_reason,
        } for row in rows])

    @action(detail=True, methods=("post",), url_path="generate-periods")
    @transaction.atomic
    def generate_periods(self, request, pk=None):
        """إنشاء الفترات الشهرية الناقصة بحدود السنة المالية دون تكرار الفترات القائمة."""
        fiscal_year = self.get_object()
        if not has_company_role(request.user, fiscal_year.company_id, ("admin",)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("إنشاء الفترات الشهرية متاح لمدير الشركة فقط.")
        Company.objects.select_for_update().get(pk=fiscal_year.company_id)
        fiscal_year = FiscalYear.objects.select_for_update().get(pk=fiscal_year.pk)
        if fiscal_year.status != FiscalYear.Status.OPEN:
            raise ValidationError("لا يمكن إنشاء فترات شهرية داخل سنة مالية مغلقة.")
        cursor = fiscal_year.start_date
        created = 0
        while cursor <= fiscal_year.end_date:
            month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
            period_end = min(month_end, fiscal_year.end_date)
            period_name = f"{cursor.year:04d}-{cursor.month:02d}"
            _, was_created = FiscalPeriod.objects.get_or_create(
                fiscal_year=fiscal_year, name=period_name,
                defaults={"start_date": cursor, "end_date": period_end},
            )
            created += int(was_created)
            cursor = period_end + timedelta(days=1)
        return Response({
            "detail": "تم تجهيز الفترات الشهرية دون تكرار الفترات الموجودة.",
            "created": created,
            "periods": FiscalPeriod.objects.filter(fiscal_year=fiscal_year).count(),
        }, status=201 if created else 200)

    @action(detail=True, methods=("post",), url_path="close-period")
    @transaction.atomic
    def close_period(self, request, pk=None):
        """إقفال فترة شهرية بعد التأكد من عدم وجود قيود يومية مسودة داخلها."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        from accounting.models import JournalEntry
        fiscal_year = self.get_object()
        if not has_company_role(request.user, fiscal_year.company_id, ("admin",)):
            raise PermissionDenied("إقفال الفترة الشهرية متاح لمدير الشركة فقط.")
        Company.objects.select_for_update().get(pk=fiscal_year.company_id)
        fiscal_year = FiscalYear.objects.select_for_update().get(pk=fiscal_year.pk)
        if fiscal_year.status != FiscalYear.Status.OPEN:
            raise ValidationError("السنة المالية مغلقة؛ لا يمكن تعديل حالة فتراتها.")
        try:
            period = FiscalPeriod.objects.select_for_update().get(
                pk=request.data.get("period_id"), fiscal_year=fiscal_year,
            )
        except (FiscalPeriod.DoesNotExist, TypeError, ValueError):
            raise ValidationError({"period_id": "الفترة الشهرية غير موجودة ضمن السنة المالية."})
        if period.status == FiscalPeriod.Status.CLOSED:
            raise ValidationError("الفترة الشهرية مغلقة بالفعل.")
        drafts = JournalEntry.objects.filter(
            fiscal_year=fiscal_year, status=JournalEntry.Status.DRAFT,
            entry_date__range=(period.start_date, period.end_date),
        ).count()
        if drafts:
            raise ValidationError({
                "detail": "لا يمكن إقفال الفترة مع وجود قيود يومية مسودة.",
                "draft_entries": drafts,
            })
        period.status = FiscalPeriod.Status.CLOSED
        period.closed_at = timezone.now()
        period.closed_by = request.user
        period.save(update_fields=("status", "closed_at", "closed_by"))
        from audit.models import AuditEvent
        AuditEvent.objects.create(
            actor=request.user, company_id=fiscal_year.company_id,
            action=AuditEvent.Action.ACTION, resource="companies.fiscal_period.closed",
            object_id=str(period.pk), http_method="POST", status_code=200, succeeded=True,
            metadata={"fiscal_year_id": fiscal_year.pk, "name": period.name,
                      "start_date": period.start_date.isoformat(), "end_date": period.end_date.isoformat()},
        )
        return Response({"detail": f"تم إقفال الفترة {period.name}.", "period_id": period.pk})

    @action(detail=True, methods=("post",), url_path="reopen-period")
    @transaction.atomic
    def reopen_period(self, request, pk=None):
        """إعادة فتح فترة مغلقة بموافقة مدير الشركة وسبب موثق."""
        from rest_framework.exceptions import PermissionDenied, ValidationError
        fiscal_year = self.get_object()
        if not has_company_role(request.user, fiscal_year.company_id, ("admin",)):
            raise PermissionDenied("إعادة فتح الفترة الشهرية متاحة لمدير الشركة فقط.")
        reason = str(request.data.get("reason", "")).strip()
        if len(reason) < 10:
            raise ValidationError({"reason": "اكتب سببًا واضحًا لإعادة الفتح لا يقل عن 10 أحرف."})
        Company.objects.select_for_update().get(pk=fiscal_year.company_id)
        fiscal_year = FiscalYear.objects.select_for_update().get(pk=fiscal_year.pk)
        if fiscal_year.status != FiscalYear.Status.OPEN:
            raise ValidationError("يجب إعادة فتح السنة المالية أولًا قبل إعادة فتح إحدى فتراتها.")
        try:
            period = FiscalPeriod.objects.select_for_update().get(
                pk=request.data.get("period_id"), fiscal_year=fiscal_year,
            )
        except (FiscalPeriod.DoesNotExist, TypeError, ValueError):
            raise ValidationError({"period_id": "الفترة الشهرية غير موجودة ضمن السنة المالية."})
        if period.status != FiscalPeriod.Status.CLOSED:
            raise ValidationError("الفترة الشهرية مفتوحة بالفعل.")
        period.status = FiscalPeriod.Status.OPEN
        period.reopened_at = timezone.now()
        period.reopened_by = request.user
        period.reopen_reason = reason
        period.save(update_fields=("status", "reopened_at", "reopened_by", "reopen_reason"))
        from audit.models import AuditEvent
        AuditEvent.objects.create(
            actor=request.user, company_id=fiscal_year.company_id,
            action=AuditEvent.Action.ACTION, resource="companies.fiscal_period.reopened",
            object_id=str(period.pk), http_method="POST", status_code=200, succeeded=True,
            metadata={"fiscal_year_id": fiscal_year.pk, "name": period.name, "reason": reason},
        )
        return Response({"detail": f"تمت إعادة فتح الفترة {period.name} مع تسجيل السبب.", "period_id": period.pk})
