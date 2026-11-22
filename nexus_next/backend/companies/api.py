from django.contrib.auth import get_user_model
from django.db import transaction
from decimal import Decimal

from django.db.models import Count, F, Q, Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .access import accessible_branches, accessible_companies, has_company_role, is_platform_owner
from .models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalYear
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
        """فحص محاسبي قبل الإقفال دون تعديل أي قيد أو رصيد."""
        from accounting.models import JournalEntry
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
        closing_entry = entries.filter(
            source_type=JournalEntry.Source.CLOSING,
            status=JournalEntry.Status.POSTED,
            entry_date=fiscal_year.end_date,
        ).exists()
        next_year = FiscalYear.objects.filter(
            company_id=fiscal_year.company_id,
            start_date__gt=fiscal_year.end_date,
        ).order_by("start_date").first()
        opening_entry_required = bool(next_year)
        opening_entry = False
        if next_year:
            opening_entry = JournalEntry.objects.filter(
                fiscal_year=next_year,
                source_type=JournalEntry.Source.OPENING,
                status=JournalEntry.Status.POSTED,
                entry_date=next_year.start_date,
            ).exists()
        checks = {
            "no_drafts": drafts == 0,
            "balanced_entries": invalid_entries == 0,
            "closing_entry": closing_entry,
            "next_year_opening": (not opening_entry_required) or opening_entry,
        }
        return {
            "fiscal_year": fiscal_year.pk,
            "status": fiscal_year.status,
            "draft_entries": drafts,
            "unbalanced_entries": invalid_entries,
            "closing_entry_posted": closing_entry,
            "next_fiscal_year": next_year.pk if next_year else None,
            "opening_entry_required": opening_entry_required,
            "opening_entry_posted": opening_entry,
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
