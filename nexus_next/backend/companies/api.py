from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .access import accessible_branches, accessible_companies, can_access_company, has_company_role, is_platform_owner
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
        """تحديث السنة مع تسلسل عمليات الشركة ومنع تداخل الفترات بالتزامن."""
        from rest_framework.exceptions import PermissionDenied
        current_company_id = serializer.instance.company_id
        if not has_company_role(self.request.user, current_company_id, ("admin",)):
            raise PermissionDenied("تعديل السنوات المالية متاح لمدير الشركة فقط.")
        Company.objects.select_for_update().get(pk=current_company_id)
        # يمنع FiscalYearSerializer تغيير الشركة ويعيد خطأ تحقق واضحًا قبل الوصول للحفظ.
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        from rest_framework.exceptions import PermissionDenied
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف السنوات المالية متاح لمدير الشركة فقط.")
        instance.delete()
