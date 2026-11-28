from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from accounting.models import FxSubledgerRevaluation, JournalEntry, JournalLine
from accounting.services import reverse_operational_journal
from accounting.fx_subledger_services import open_fx_invoice_balances, revalue_fx_invoice
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from companies.models import Branch, Company, CompanyMembership
from inventory.models import Product, StockMovement
from customers.models import Customer
from .models import CustomerReceipt, CustomerReceiptBatch, CustomerReceiptBatchAllocation, CustomerRefund, SalesDelivery, SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine, SalesQuotation, SalesDocumentTemplate, SalesOrder, SalesDeliveryAllocation
from .serializers import CustomerReceiptSerializer, SalesDeliverySerializer, SalesInvoiceSerializer, SalesQuotationSerializer, SalesDocumentTemplateSerializer, SalesOrderSerializer
from .services import post_customer_receipt, post_sales_delivery, post_sales_invoice


def _document_template_snapshot(company, document_type):
    """التقاط إعدادات القالب الافتراضي لحظة إصدار المستند."""
    template = SalesDocumentTemplate.objects.filter(
        company=company, document_type=document_type, is_active=True, is_default=True,
    ).first()
    if template is None:
        return {"template_id": None, "name": "Classic", "style": "classic", "config": {}}
    return {
        "template_id": template.pk, "name": template.name,
        "style": template.style, "config": template.config,
    }


def _sales_warehouse_accessible(user, warehouse):
    """تنفيذ وظيفة _sales_warehouse_accessible ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
    if user.is_superuser and not is_platform_owner(user):
        return True
    branch = warehouse.branch
    if branch is None:
        branch = Branch.objects.filter(
            company_id=warehouse.company_id, is_active=True
        ).order_by("-is_head_office", "code").first()
    return bool(branch and can_access_branch(user, branch.pk, warehouse.company_id))


def _historical_branch_accessible(user, branch_id, company_id):
    """السماح بقراءة تاريخ الفرع الموقوف للمستخدم المصرح له به فقط."""
    if not Branch.objects.filter(pk=branch_id, company_id=company_id).exists():
        return False
    if user.is_superuser and not is_platform_owner(user):
        return True
    return CompanyMembership.objects.filter(
        user=user, company_id=company_id, is_active=True, company__is_active=True,
    ).filter(
        Q(all_branches=True) | Q(branches__id=branch_id, branches__company_id=company_id)
    ).exists()


def _historical_sales_invoice_queryset(user, company_id):
    """نطاق فواتير التقارير التاريخية مع الاحتفاظ بصلاحيات الفروع حتى عند إيقافها."""
    qs = SalesInvoice.objects.filter(company_id=company_id)
    if user.is_superuser and not is_platform_owner(user):
        return qs

    memberships = CompanyMembership.objects.filter(
        user=user, company_id=company_id, is_active=True, company__is_active=True,
    )
    if memberships.filter(all_branches=True).exists():
        branch_ids = Branch.objects.filter(company_id=company_id).values("id")
    else:
        branch_ids = memberships.filter(
            branches__company_id=company_id,
        ).values("branches__id")

    scope = Q(delivery__warehouse__branch_id__in=branch_ids) | Q(branch_id__in=branch_ids)
    default_branch = Branch.objects.filter(
        company_id=company_id, is_active=True,
    ).order_by("-is_head_office", "code").first()
    if default_branch and _historical_branch_accessible(user, default_branch.pk, company_id):
        scope |= Q(delivery__isnull=False, delivery__warehouse__branch__isnull=True)
    return qs.filter(scope).distinct()





class SalesBaseViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def _check_write(self, company_id):
        """التحقق من صلاحية تنفيذ عملية تعديل داخل نطاق الشركة."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("إجراءات المبيعات متاحة لمدير الشركة والمحاسب فقط.")

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        raise PermissionDenied("لا يمكن حذف مستندات المبيعات؛ استخدم إجراء مردود المبيعات أو عكس سند القبض المعتمد حسب نوع المستند.")


class SalesOrderViewSet(SalesBaseViewSet):
    """إدارة أوامر البيع واعتمادها قبل بدء التسليم."""
    serializer_class = SalesOrderSerializer
    queryset = SalesOrder.objects.select_related("company", "customer", "warehouse").prefetch_related("lines__product", "lines__tax_code", "lines__delivery_allocations")
    filterset_fields = ("company", "customer", "warehouse", "status", "order_date")
    search_fields = ("order_number", "customer__name_ar")
    ordering_fields = ("order_date", "order_number", "created_at", "grand_total")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        branches = accessible_branches(self.request.user)
        default_company_ids = []
        for company_id in accessible_companies(self.request.user).values_list("id", flat=True):
            default_branch = Branch.objects.filter(
                company_id=company_id, is_active=True
            ).order_by("-is_head_office", "code").first()
            if default_branch and can_access_branch(self.request.user, default_branch.pk, company_id):
                default_company_ids.append(company_id)
        return qs.filter(
            Q(warehouse__branch__in=branches)
            | Q(warehouse__branch__isnull=True, company_id__in=default_company_ids)
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        if not _sales_warehouse_accessible(self.request.user, data["warehouse"]):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مخزن أمر البيع.")
        serializer.save()

    def perform_update(self, serializer):
        order = serializer.instance
        self._check_write(order.company_id)
        if order.status != SalesOrder.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل أمر بيع معتمد.")
        warehouse = serializer.validated_data.get("warehouse", order.warehouse)
        if not _sales_warehouse_accessible(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مخزن أمر البيع.")
        serializer.save()

    def perform_destroy(self, instance):
        self._check_write(instance.company_id)
        if instance.status != SalesOrder.Status.DRAFT:
            raise ValidationError("لا يمكن حذف أمر بيع معتمد.")
        for line in instance.lines.all():
            line.delete()
        instance.delete()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def cancel(self, request, pk=None):
        order = get_object_or_404(
            self.get_queryset().select_for_update(of=("self",)), pk=self.kwargs["pk"],
        )
        self.check_object_permissions(request, order)
        self._check_write(order.company_id)
        if order.status not in (SalesOrder.Status.CONFIRMED, SalesOrder.Status.PARTIALLY_DELIVERED):
            raise ValidationError("لا يمكن إلغاء أمر البيع في حالته الحالية.")
        if order.deliveries.filter(stock_movement__status=StockMovement.Status.DRAFT).exists():
            raise ValidationError("احذف أذون التسليم المسودة أولًا قبل إلغاء أمر البيع.")
        order.status = SalesOrder.Status.CANCELLED
        order.save(update_fields=("status", "updated_at"))
        return Response(self.get_serializer(order).data, status=status.HTTP_200_OK)


    @action(detail=True, methods=("post",))
    @transaction.atomic
    def confirm(self, request, pk=None):
        order = get_object_or_404(
            self.get_queryset().select_for_update(of=("self",)), pk=self.kwargs["pk"],
        )
        self.check_object_permissions(request, order)
        self._check_write(order.company_id)
        if order.status != SalesOrder.Status.DRAFT:
            raise ValidationError("أمر البيع ليس في حالة مسودة.")
        if not order.lines.exists():
            raise ValidationError("لا يمكن اعتماد أمر بيع بدون بنود.")
        if not order.customer.is_active or not order.warehouse.is_active:
            raise ValidationError("العميل أو المخزن موقوف.")
        order.status = SalesOrder.Status.CONFIRMED
        order.save(update_fields=("status", "updated_at"))
        return Response(self.get_serializer(order).data, status=status.HTTP_200_OK)


class SalesDeliveryViewSet(SalesBaseViewSet):
    serializer_class = SalesDeliverySerializer
    queryset = SalesDelivery.objects.select_related("company", "customer", "warehouse", "stock_movement").prefetch_related("stock_movement__lines__product")
    filterset_fields = ("company", "customer", "warehouse", "delivery_date")
    search_fields = ("delivery_number", "customer_reference", "customer__name_ar")
    ordering_fields = ("delivery_date", "delivery_number", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        branches = accessible_branches(self.request.user)
        default_company_ids = []
        for company_id in accessible_companies(self.request.user).values_list("id", flat=True):
            default_branch = Branch.objects.filter(company_id=company_id, is_active=True).order_by("-is_head_office", "code").first()
            if default_branch and can_access_branch(self.request.user, default_branch.pk, company_id):
                default_company_ids.append(company_id)
        return qs.filter(
            Q(warehouse__branch__in=branches)
            | Q(warehouse__branch__isnull=True, company_id__in=default_company_ids)
        )

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        if not _sales_warehouse_accessible(self.request.user, data["warehouse"]):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        instance = serializer.instance
        self._check_write(instance.company_id)
        warehouse = serializer.validated_data.get("warehouse", instance.warehouse)
        if not _sales_warehouse_accessible(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        self._check_write(instance.company_id)
        if instance.stock_movement.status != StockMovement.Status.DRAFT:
            raise ValidationError("لا يمكن حذف إذن تسليم مرحّل.")
        if instance.invoices.exists():
            raise ValidationError("لا يمكن حذف إذن تسليم مرتبط بفاتورة.")
        movement = instance.stock_movement
        SalesDeliveryAllocation.objects.filter(delivery=instance).delete()
        instance.delete()
        movement.delete()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def post(self, request, pk=None):
        """ترحيل إذن التسليم مع تثبيت قالب الطباعة وقت الإصدار."""
        delivery = self.get_object()
        self._check_write(delivery.company_id)
        if not _sales_warehouse_accessible(request.user, delivery.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")
        delivery.print_template_snapshot = _document_template_snapshot(
            delivery.company, SalesDocumentTemplate.DocumentType.DELIVERY,
        )
        delivery.save(update_fields=("print_template_snapshot",))
        delivery = post_sales_delivery(delivery.pk, request.user)
        return Response(self.get_serializer(delivery).data, status=status.HTTP_200_OK)





class SalesDocumentTemplateViewSet(SalesBaseViewSet):
    """قوالب طباعة الشركة؛ تعديلها لمدير الشركة فقط."""
    serializer_class = SalesDocumentTemplateSerializer
    queryset = SalesDocumentTemplate.objects.select_related("company").all()
    filterset_fields = ("company", "document_type", "style", "is_default", "is_active")
    ordering_fields = ("document_type", "name", "created_at")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def _check_template_admin(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin",)):
            raise PermissionDenied("إدارة قوالب الطباعة متاحة لمدير الشركة فقط.")

    @transaction.atomic
    def perform_create(self, serializer):
        company_id = serializer.validated_data["company"].pk
        self._check_template_admin(company_id)
        if serializer.validated_data.get("is_default", False):
            SalesDocumentTemplate.objects.filter(
                company_id=company_id,
                document_type=serializer.validated_data["document_type"],
                is_default=True,
            ).update(is_default=False)
        serializer.save()

    @transaction.atomic
    def perform_update(self, serializer):
        instance = serializer.instance
        self._check_template_admin(instance.company_id)
        document_type = serializer.validated_data.get("document_type", instance.document_type)
        if serializer.validated_data.get("is_default", instance.is_default):
            SalesDocumentTemplate.objects.filter(
                company_id=instance.company_id, document_type=document_type,
                is_default=True,
            ).exclude(pk=instance.pk).update(is_default=False)
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        self._check_template_admin(instance.company_id)
        instance.is_active = False
        instance.is_default = False
        instance.save(update_fields=("is_active", "is_default", "updated_at"))


class SalesQuotationViewSet(SalesBaseViewSet):
    """إدارة عروض الأسعار وتحويل المقبول منها إلى إذن تسليم مسودة."""
    serializer_class = SalesQuotationSerializer
    queryset = SalesQuotation.objects.select_related(
        "company", "customer", "converted_delivery"
    ).prefetch_related("lines__product", "lines__tax_code", "lines__discount_rule")
    filterset_fields = ("company", "customer", "status", "quotation_date")
    search_fields = ("quotation_number", "customer__name_ar")
    ordering_fields = ("quotation_date", "quotation_number", "created_at", "grand_total")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def perform_create(self, serializer):
        self._check_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        if serializer.instance.status != SalesQuotation.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل عرض سعر غير مسودة.")
        serializer.save()

    def perform_destroy(self, instance):
        self._check_write(instance.company_id)
        if instance.status != SalesQuotation.Status.DRAFT or instance.converted_delivery_id:
            raise ValidationError("لا يمكن حذف عرض سعر مرسل أو مقبول أو محول.")
        for line in instance.lines.all():
            line.delete()
        instance.delete()

    @action(detail=True, methods=("post",))
    def send(self, request, pk=None):
        quotation = self.get_object()
        self._check_write(quotation.company_id)
        if quotation.status != SalesQuotation.Status.DRAFT:
            raise ValidationError("يمكن إرسال عرض السعر من حالة المسودة فقط.")
        if quotation.valid_until < timezone.localdate():
            quotation.status = SalesQuotation.Status.EXPIRED
            quotation.save(update_fields=("status", "updated_at"))
            raise ValidationError("انتهت صلاحية عرض السعر.")
        quotation.status = SalesQuotation.Status.SENT
        quotation.sent_at = timezone.now()
        quotation.print_template_snapshot = _document_template_snapshot(quotation.company, SalesDocumentTemplate.DocumentType.QUOTATION)
        quotation.save(update_fields=("status", "sent_at", "print_template_snapshot", "updated_at"))
        return Response(self.get_serializer(quotation).data)

    @action(detail=True, methods=("post",))
    def accept(self, request, pk=None):
        quotation = self.get_object()
        self._check_write(quotation.company_id)
        if quotation.status != SalesQuotation.Status.SENT:
            raise ValidationError("لا يمكن قبول عرض سعر غير مرسل.")
        if quotation.valid_until < timezone.localdate():
            quotation.status = SalesQuotation.Status.EXPIRED
            quotation.save(update_fields=("status", "updated_at"))
            raise ValidationError("انتهت صلاحية عرض السعر.")
        quotation.status = SalesQuotation.Status.ACCEPTED
        quotation.accepted_at = timezone.now()
        quotation.save(update_fields=("status", "accepted_at", "updated_at"))
        return Response(self.get_serializer(quotation).data)

    @action(detail=True, methods=("post",))
    def reject(self, request, pk=None):
        quotation = self.get_object()
        self._check_write(quotation.company_id)
        if quotation.status != SalesQuotation.Status.SENT:
            raise ValidationError("لا يمكن رفض عرض سعر غير مرسل.")
        quotation.status = SalesQuotation.Status.REJECTED
        quotation.save(update_fields=("status", "updated_at"))
        return Response(self.get_serializer(quotation).data)

    @action(detail=True, methods=("post",), url_path="convert-to-delivery")
    @transaction.atomic
    def convert_to_delivery(self, request, pk=None):
        # Lock only the quotation row. Concurrent conversion requests wait here,
        # then observe converted_delivery_id written by the first transaction.
        quotation = get_object_or_404(
            self.get_queryset().select_for_update(of=("self",)),
            pk=self.kwargs["pk"],
        )
        self.check_object_permissions(request, quotation)
        self._check_write(quotation.company_id)
        if quotation.converted_delivery_id:
            return Response({
                "detail": "تم تحويل عرض السعر من قبل؛ لا يُنشأ إذن تسليم مكرر.",
                "delivery": SalesDeliverySerializer(quotation.converted_delivery, context=self.get_serializer_context()).data,
            }, status=status.HTTP_200_OK)
        if quotation.status != SalesQuotation.Status.ACCEPTED:
            raise ValidationError("يجب قبول عرض السعر قبل تحويله.")
        if quotation.valid_until < timezone.localdate():
            quotation.status = SalesQuotation.Status.EXPIRED
            quotation.save(update_fields=("status", "updated_at"))
            # Return normally so the surrounding atomic block commits the EXPIRED status.
            return Response(
                {"detail": "انتهت صلاحية عرض السعر."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            warehouse_id = int(request.data.get("warehouse"))
        except (TypeError, ValueError):
            raise ValidationError({"warehouse": "حدد المخزن."}) from None
        from inventory.models import Warehouse
        warehouse = Warehouse.objects.filter(pk=warehouse_id, company_id=quotation.company_id, is_active=True).first()
        if not warehouse:
            raise ValidationError({"warehouse": "المخزن غير نشط أو لا يتبع الشركة."})
        if not _sales_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        delivery_number = str(request.data.get("delivery_number", "")).strip()
        if not delivery_number:
            raise ValidationError({"delivery_number": "رقم إذن التسليم مطلوب."})
        delivery_date = parse_date(request.data.get("delivery_date", "")) or timezone.localdate()
        expiry_dates = request.data.get("expiry_dates") or {}
        lines = []
        for line in quotation.lines.select_related("product").all():
            lines.append({
                "product": line.product_id,
                "quantity": str(line.quantity),
                "expiry_date": expiry_dates.get(str(line.pk)),
            })
        delivery_serializer = SalesDeliverySerializer(
            data={
                "company": quotation.company_id, "customer": quotation.customer_id,
                "warehouse": warehouse.pk, "delivery_number": delivery_number,
                "delivery_date": delivery_date.isoformat(),
                "customer_reference": quotation.quotation_number,
                "notes": quotation.notes, "lines": lines,
            },
            context=self.get_serializer_context(),
        )
        delivery_serializer.is_valid(raise_exception=True)
        delivery = delivery_serializer.save()
        delivery.quotation_snapshot = {
            "quotation_id": quotation.pk,
            "quotation_number": quotation.quotation_number,
            "quotation_date": quotation.quotation_date.isoformat(),
            "valid_until": quotation.valid_until.isoformat(),
            "lines": [{
                "quotation_line_id": line.pk, "product": line.product_id,
                "quantity": str(line.quantity), "unit_price": str(line.unit_price),
                "tax_rate": str(line.tax_rate), "tax_code": line.tax_code_id,
                "discount_percent": str(line.discount_percent),
                "discount_rule": line.discount_rule_id,
            } for line in quotation.lines.all()],
        }
        delivery.save(update_fields=("quotation_snapshot",))
        quotation.converted_delivery = delivery
        quotation.status = SalesQuotation.Status.CONVERTED
        quotation.converted_at = timezone.now()
        quotation.save(update_fields=("converted_delivery", "status", "converted_at", "updated_at"))
        return Response(SalesDeliverySerializer(delivery, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)


class SalesInvoiceViewSet(SalesBaseViewSet):
    serializer_class = SalesInvoiceSerializer
    queryset = SalesInvoice.objects.select_related("company", "customer", "delivery", "delivery__warehouse", "branch", "journal_entry", "transaction_currency").prefetch_related("lines__delivery_line__product", "lines__product", "lines__tax_code", "receipts", "receipt_batch_allocations__batch", "approval_requests")
    filterset_fields = ("company", "customer", "status", "invoice_date")
    search_fields = ("invoice_number", "customer__name_ar", "delivery__delivery_number")
    ordering_fields = ("invoice_date", "invoice_number", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        branches = accessible_branches(self.request.user)
        default_company_ids = []
        for company_id in accessible_companies(self.request.user).values_list("id", flat=True):
            default_branch = Branch.objects.filter(company_id=company_id, is_active=True).order_by("-is_head_office", "code").first()
            if default_branch and can_access_branch(self.request.user, default_branch.pk, company_id):
                default_company_ids.append(company_id)
        return qs.filter(
            Q(delivery__warehouse__branch__in=branches)
            | Q(delivery__isnull=False, delivery__warehouse__branch__isnull=True, company_id__in=default_company_ids)
            | Q(branch__in=branches)
        )

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        delivery = data.get("delivery")
        if delivery:
            if not _sales_warehouse_accessible(self.request.user, delivery.warehouse):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن التسليم.")
        elif not data.get("branch") or not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع فاتورة الخدمة.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        invoice = serializer.instance
        self._check_write(invoice.company_id)
        if invoice.status != SalesInvoice.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل فاتورة مبيعات مرحلة.")
        if invoice.delivery_id:
            if not _sales_warehouse_accessible(self.request.user, invoice.delivery.warehouse):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        elif not invoice.branch_id or not can_access_branch(self.request.user, invoice.branch_id, invoice.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        """حذف فاتورة مسودة بلا تحصيل أو اعتماد أو مردود أو أثر إعادة تقييم."""
        invoice = SalesInvoice.objects.select_for_update(of=("self",)).select_related(
            "company", "delivery__warehouse", "branch"
        ).get(pk=instance.pk)
        self._check_write(invoice.company_id)
        if invoice.delivery_id:
            if not _sales_warehouse_accessible(self.request.user, invoice.delivery.warehouse):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        elif not invoice.branch_id or not can_access_branch(self.request.user, invoice.branch_id, invoice.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        if invoice.status != SalesInvoice.Status.DRAFT or invoice.journal_entry_id:
            raise ValidationError("لا يمكن حذف فاتورة مبيعات مرحلة أو مرتبطة بقيد.")
        if (
            invoice.receipts.exists()
            or invoice.returns.exists()
            or invoice.approval_requests.exists()
            or invoice.fx_revaluations.exists()
        ):
            raise ValidationError("لا يمكن حذف فاتورة لها تحصيل أو مردود أو طلب اعتماد أو إعادة تقييم.")
        for line in invoice.lines.select_for_update():
            line.delete()
        invoice.delete()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def post(self, request, pk=None):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        invoice = self.get_object()
        invoice = SalesInvoice.objects.select_for_update(of=("self",)).get(pk=invoice.pk)
        self._check_write(invoice.company_id)
        if invoice.delivery_id:
            if not _sales_warehouse_accessible(request.user, invoice.delivery.warehouse):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن التسليم.")
        elif not invoice.branch_id or not can_access_branch(request.user, invoice.branch_id, invoice.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع فاتورة الخدمة.")
        from approvals.models import SalesInvoiceApproval
        from approvals.snapshots import invoice_discount_snapshot
        from sales.services import customer_credit_exposure
        customer = Customer.objects.select_for_update().get(pk=invoice.customer_id)
        credit_exposure = customer_credit_exposure(customer)
        if customer.credit_limit > 0 and credit_exposure + invoice.grand_total > customer.credit_limit:
            latest = invoice.approval_requests.order_by("-requested_at", "-pk").first()
            current_max_discount = max(invoice.lines.values_list("discount_percent", flat=True), default=Decimal("0"))
            credit_snapshot_matches = bool(
                latest
                and latest.status in (SalesInvoiceApproval.Status.PENDING, SalesInvoiceApproval.Status.APPROVED)
                and latest.requested_credit_limit == customer.credit_limit
                and latest.requested_credit_exposure == credit_exposure
                and latest.requested_credit_amount == invoice.grand_total
                and latest.requested_discount_total == invoice.discount_total
                and latest.requested_max_discount_percent == current_max_discount
                and latest.requested_invoice_limit == invoice.invoice_approval_limit
                and latest.requested_lines_snapshot == invoice_discount_snapshot(invoice)
            )
            if not credit_snapshot_matches:
                for old_request in invoice.approval_requests.filter(status=SalesInvoiceApproval.Status.PENDING):
                    old_request.status = SalesInvoiceApproval.Status.SUPERSEDED
                    old_request.decision_note = "تغير موقف العميل الائتماني؛ أُنشئ طلب اعتماد أحدث."
                    old_request.save()
                latest = SalesInvoiceApproval.objects.create(
                    company=invoice.company, invoice=invoice, requested_by=request.user,
                    requested_limit=invoice.discount_approval_limit,
                    requested_invoice_limit=invoice.invoice_approval_limit,
                    requested_discount_total=invoice.discount_total,
                    requested_max_discount_percent=current_max_discount,
                    requested_credit_limit=customer.credit_limit,
                    requested_credit_exposure=credit_exposure,
                    requested_credit_amount=invoice.grand_total,
                    requested_lines_snapshot=invoice_discount_snapshot(invoice),
                )
            if latest.status != SalesInvoiceApproval.Status.APPROVED:
                return Response({
                    "detail": "تجاوزت الفاتورة حد ائتمان العميل. تم تسجيل طلب اعتماد مدير الشركة، ولا يمكن الترحيل قبل الموافقة.",
                    "approval_request": latest.pk,
                    "credit_limit": str(customer.credit_limit),
                    "current_exposure": str(credit_exposure),
                    "invoice_amount": str(invoice.grand_total),
                }, status=status.HTTP_400_BAD_REQUEST)
        invoice.print_template_snapshot = _document_template_snapshot(invoice.company, SalesDocumentTemplate.DocumentType.INVOICE)
        invoice.save(update_fields=("print_template_snapshot",))
        invoice = post_sales_invoice(invoice.pk, request.user)
        return Response(self.get_serializer(invoice).data, status=status.HTTP_200_OK)


    @action(detail=True, methods=("post",), url_path="revalue")
    def revalue(self, request, pk=None):
        invoice = self.get_object()
        self._check_write(invoice.company_id)
        if invoice.delivery_id:
            if not _sales_warehouse_accessible(request.user, invoice.delivery.warehouse):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        elif not invoice.branch_id or not can_access_branch(request.user, invoice.branch_id, invoice.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        revaluation_date = parse_date(request.data.get("revaluation_date", ""))
        rate_date = parse_date(request.data.get("rate_date", ""))
        try:
            rate = Decimal(str(request.data.get("rate")))
        except Exception as exc:
            raise ValidationError({"rate": "أدخل سعر صرف صحيحًا."}) from exc
        if not revaluation_date or not rate_date:
            raise ValidationError({"revaluation_date": "حدد تاريخ إعادة التقييم وتاريخ سعر الصرف."})
        record = revalue_fx_invoice(
            direction="receivable", invoice_id=invoice.pk,
            revaluation_date=revaluation_date, rate_date=rate_date, rate=rate,
            user=request.user,
        )
        return Response({
            "id": record.pk, "invoice": invoice.pk, "revaluation_date": record.revaluation_date,
            "rate_date": record.rate_date, "rate": str(record.rate),
            "foreign_balance": str(record.foreign_balance),
            "previous_carrying_amount": str(record.previous_carrying_amount),
            "new_carrying_amount": str(record.new_carrying_amount),
            "difference": str(record.difference),
            "journal_entry": record.journal_entry_id,
            "journal_number": record.journal_entry.entry_number if record.journal_entry_id else None,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("get",), url_path="revaluations")
    def revaluations(self, request, pk=None):
        invoice = self.get_object()
        rows = FxSubledgerRevaluation.objects.filter(sales_invoice=invoice).select_related("journal_entry")
        return Response([{
            "id": row.pk, "revaluation_date": row.revaluation_date, "rate_date": row.rate_date,
            "rate": str(row.rate), "foreign_balance": str(row.foreign_balance),
            "previous_carrying_amount": str(row.previous_carrying_amount),
            "new_carrying_amount": str(row.new_carrying_amount), "difference": str(row.difference),
            "journal_entry": row.journal_entry_id,
            "journal_number": row.journal_entry.entry_number if row.journal_entry_id else None,
            "status": "reversed" if row.journal_entry_id and row.journal_entry.status == "reversed" else ("posted" if row.journal_entry_id else "no_difference"),
        } for row in rows])

    @action(detail=False, methods=("get",), url_path="customer-statement")
    def customer_statement(self, request):
        """إعداد كشف حركة العميل ورصيده للفترة المحددة من الفواتير والتحصيلات المرحلة."""
        try:
            company_id = int(request.query_params.get("company", ""))
            customer_id = int(request.query_params.get("customer", ""))
        except (TypeError, ValueError):
            raise ValidationError({"detail": "حدد الشركة والعميل لعرض كشف الحساب."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        date_from_raw = request.query_params.get("date_from")
        date_to_raw = request.query_params.get("date_to")
        date_from = parse_date(date_from_raw) if date_from_raw else None
        date_to = parse_date(date_to_raw) if date_to_raw else None
        if date_from_raw and not date_from:
            raise ValidationError({"date_from": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_to_raw and not date_to:
            raise ValidationError({"date_to": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_from and date_to and date_from > date_to:
            raise ValidationError({"date_to": "تاريخ النهاية يجب ألا يسبق تاريخ البداية."})

        from customers.models import Customer
        customer = Customer.objects.filter(pk=customer_id, company_id=company_id).first()
        if customer is None:
            raise ValidationError({"customer": "العميل غير موجود في الشركة المحددة."})

        branch_id_raw = request.query_params.get("branch")
        branch_id = None
        if branch_id_raw:
            try:
                branch_id = int(branch_id_raw)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."}) from None
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

        invoices = self.get_queryset().filter(
            company_id=company_id, customer_id=customer_id,
            status=SalesInvoice.Status.POSTED,
        ).select_related("customer", "delivery__warehouse", "branch")
        if branch_id:
            invoices = invoices.filter(Q(delivery__warehouse__branch_id=branch_id) | Q(branch_id=branch_id))

        events = []
        for invoice in invoices:
            events.append({
                "date": invoice.invoice_date, "kind": "invoice",
                "number": invoice.invoice_number,
                "description": f"فاتورة مبيعات {invoice.invoice_number}",
                "debit": invoice.grand_total, "credit": Decimal("0"), "sort": 0,
            })
        receipts = CustomerReceipt.objects.filter(
            invoice__in=invoices,
            status__in=(CustomerReceipt.Status.POSTED, CustomerReceipt.Status.REVERSED),
        ).select_related("invoice", "journal_entry__reversal_entry")
        for receipt in receipts:
            carrying_amount = (
                receipt.carrying_base_amount
                if receipt.carrying_base_amount is not None else receipt.amount
            )
            events.append({
                "date": receipt.receipt_date, "kind": "receipt",
                "number": receipt.receipt_number,
                "description": f"سند قبض عن فاتورة {receipt.invoice.invoice_number}",
                "debit": Decimal("0"), "credit": carrying_amount, "sort": 1,
            })
            reversal = (
                receipt.journal_entry.reversal_entry
                if receipt.status == CustomerReceipt.Status.REVERSED and receipt.journal_entry_id
                else None
            )
            if reversal:
                events.append({
                    "date": reversal.entry_date, "kind": "receipt_reversal",
                    "number": f"REV-{receipt.receipt_number}",
                    "description": f"عكس سند قبض العميل {receipt.receipt_number}",
                    "debit": carrying_amount, "credit": Decimal("0"), "sort": 3,
                })
        receipt_batches = CustomerReceiptBatch.objects.filter(
            company_id=company_id, customer_id=customer_id,
            branch__in=accessible_branches(request.user),
            status__in=(CustomerReceiptBatch.Status.POSTED, CustomerReceiptBatch.Status.REVERSED),
        ).select_related("journal_entry", "reversal_journal_entry").prefetch_related("allocations__invoice")
        if branch_id:
            receipt_batches = receipt_batches.filter(branch_id=branch_id)
        statement_as_of = date_to or timezone.localdate()
        for batch in receipt_batches:
            invoice_numbers = ", ".join(
                allocation.invoice.invoice_number
                for allocation in batch.allocations.filter(
                    allocation_date__lte=statement_as_of,
                ).select_related("invoice")
            )
            description = (
                f"سند قبض مجمع عن الفواتير {invoice_numbers}"
                if invoice_numbers else "دفعة مقدمة غير مخصصة من العميل"
            )
            events.append({
                "date": batch.receipt_date, "kind": "receipt_batch",
                "number": batch.receipt_number, "description": description,
                "debit": Decimal("0"), "credit": batch.amount, "sort": 1,
            })
            if batch.status == CustomerReceiptBatch.Status.REVERSED and batch.reversal_journal_entry_id:
                events.append({
                    "date": batch.reversal_date or batch.reversal_journal_entry.entry_date,
                    "kind": "receipt_batch_reversal",
                    "number": f"REV-{batch.receipt_number}",
                    "description": f"عكس سند القبض المجمع {batch.receipt_number}",
                    "debit": batch.amount, "credit": Decimal("0"), "sort": 3,
                })
        customer_refunds = CustomerRefund.objects.filter(
            company_id=company_id, customer_id=customer_id,
            branch__in=accessible_branches(request.user),
            status__in=(CustomerRefund.Status.POSTED, CustomerRefund.Status.REVERSED),
        ).select_related("source_receipt_batch", "reversal_journal_entry")
        if branch_id:
            customer_refunds = customer_refunds.filter(branch_id=branch_id)
        for refund in customer_refunds:
            events.append({
                "date": refund.refund_date, "kind": "customer_refund",
                "number": refund.refund_number,
                "description": f"رد مبلغ للعميل من الدفعة المقدمة {refund.source_receipt_batch.receipt_number}",
                "debit": refund.amount, "credit": Decimal("0"), "sort": 2,
            })
            if (
                refund.status == CustomerRefund.Status.REVERSED
                and refund.reversal_journal_entry_id
                and refund.reversal_journal_entry.status == JournalEntry.Status.POSTED
            ):
                events.append({
                    "date": refund.reversal_journal_entry.entry_date,
                    "kind": "customer_refund_reversal",
                    "number": f"REV-{refund.refund_number}",
                    "description": f"عكس رد المبلغ للعميل {refund.refund_number}",
                    "debit": Decimal("0"), "credit": refund.amount, "sort": 3,
                })
        returns = SalesReturn.objects.filter(
            invoice__in=invoices,
            status__in=(SalesReturn.Status.POSTED, SalesReturn.Status.REVERSED),
        ).select_related("invoice")
        for sales_return in returns:
            events.append({
                "date": sales_return.return_date, "kind": "sales_return",
                "number": sales_return.return_number,
                "description": f"مردود مبيعات عن الفاتورة {sales_return.invoice.invoice_number}",
                "debit": Decimal("0"), "credit": sales_return.grand_total, "sort": 2,
            })
            if sales_return.status == SalesReturn.Status.REVERSED and sales_return.reversal_date:
                events.append({
                    "date": sales_return.reversal_date, "kind": "sales_return_reversal",
                    "number": f"REV-{sales_return.return_number}",
                    "description": f"عكس مردود المبيعات {sales_return.return_number}",
                    "debit": sales_return.grand_total, "credit": Decimal("0"), "sort": 3,
                })
        # FX revaluations affect the receivable control balance and must appear
        # in the customer statement on their posting date, with reversals dated
        # from the linked reversal journal rather than silently disappearing.
        fx_revaluations = FxSubledgerRevaluation.objects.filter(
            sales_invoice__in=invoices,
        ).select_related("sales_invoice", "journal_entry__reversal_entry")
        for revaluation in fx_revaluations:
            journal = revaluation.journal_entry
            if journal is None or not revaluation.difference:
                continue
            amount = abs(revaluation.difference)
            increases_receivable = revaluation.difference > 0
            events.append({
                "date": journal.entry_date, "kind": "fx_revaluation",
                "number": journal.entry_number,
                "description": f"إعادة تقييم عملة الفاتورة {revaluation.sales_invoice.invoice_number}",
                "debit": amount if increases_receivable else Decimal("0"),
                "credit": Decimal("0") if increases_receivable else amount,
                "sort": 2,
            })
            reversal = journal.reversal_entry if journal.status == JournalEntry.Status.REVERSED else None
            if reversal:
                events.append({
                    "date": reversal.entry_date, "kind": "fx_revaluation_reversal",
                    "number": reversal.entry_number,
                    "description": f"عكس إعادة تقييم عملة الفاتورة {revaluation.sales_invoice.invoice_number}",
                    "debit": Decimal("0") if increases_receivable else amount,
                    "credit": amount if increases_receivable else Decimal("0"),
                    "sort": 3,
                })

        events.sort(key=lambda row: (row["date"], row["sort"], row["number"]))

        opening = sum(
            (row["debit"] - row["credit"] for row in events if date_from and row["date"] < date_from),
            Decimal("0"),
        )
        balance = opening
        rows = []
        for row in events:
            if date_from and row["date"] < date_from:
                continue
            if date_to and row["date"] > date_to:
                continue
            balance += row["debit"] - row["credit"]
            rows.append({
                "date": row["date"].isoformat(), "document_type": row["kind"],
                "document_number": row["number"], "description": row["description"],
                "debit": str(row["debit"]), "credit": str(row["credit"]),
                "balance": str(balance),
            })
        return Response({
            "company": company_id, "customer": customer_id, "customer_name": customer.name_ar,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "branch_filter": branch_id,
            "opening_balance": str(opening), "closing_balance": str(balance), "rows": rows,
        })

    @action(detail=False, methods=("get",), url_path="customer-aging")
    def customer_aging(self, request):
        """تجميع أرصدة العملاء حسب شرائح أعمار الديون."""
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة لعرض أعمار مديونية العملاء."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        as_of_raw = request.query_params.get("as_of")
        as_of = parse_date(as_of_raw) if as_of_raw else timezone.localdate()
        if as_of_raw and not as_of:
            raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})

        customer_id_raw = request.query_params.get("customer")
        customer_id = None
        if customer_id_raw:
            try:
                customer_id = int(customer_id_raw)
            except (TypeError, ValueError):
                raise ValidationError({"customer": "معرّف العميل غير صالح."}) from None
            from customers.models import Customer
            if not Customer.objects.filter(pk=customer_id, company_id=company_id).exists():
                raise ValidationError({"customer": "العميل غير موجود في الشركة المحددة."})

        branch_id_raw = request.query_params.get("branch")
        branch_id = None
        if branch_id_raw:
            try:
                branch_id = int(branch_id_raw)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."}) from None
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

        invoices = self.get_queryset().filter(
            company_id=company_id,
            status=SalesInvoice.Status.POSTED,
            invoice_date__lte=as_of,
        ).select_related("customer", "delivery__warehouse", "branch").prefetch_related(
            "receipts__journal_entry__reversal_entry"
        )
        if customer_id:
            invoices = invoices.filter(customer_id=customer_id)
        if branch_id:
            invoices = invoices.filter(Q(delivery__warehouse__branch_id=branch_id) | Q(branch_id=branch_id))

        returned_by_invoice = {
            row["invoice_id"]: row["total"]
            for row in SalesReturn.objects.filter(
                invoice__in=invoices, return_date__lte=as_of,
            ).filter(
                Q(status=SalesReturn.Status.POSTED)
                | Q(status=SalesReturn.Status.REVERSED, reversal_date__gt=as_of)
            ).values("invoice_id").annotate(total=Sum("grand_total"))
        }
        batch_paid_by_invoice = {
            row["invoice_id"]: row["total"]
            for row in CustomerReceiptBatchAllocation.objects.filter(
                invoice__in=invoices, batch__receipt_date__lte=as_of,
                allocation_date__lte=as_of,
            ).filter(
                Q(batch__status=CustomerReceiptBatch.Status.POSTED)
                | Q(batch__status=CustomerReceiptBatch.Status.REVERSED, batch__reversal_date__gt=as_of)
            ).values("invoice_id").annotate(total=Sum("amount"))
        }
        customers = {}
        totals = {
            "not_due": Decimal("0"), "1_30": Decimal("0"),
            "31_60": Decimal("0"), "61_90": Decimal("0"),
            "over_90": Decimal("0"), "total_due": Decimal("0"),
            "unallocated_advances": Decimal("0"),
        }
        for invoice in invoices:
            paid_as_of = Decimal("0")
            for receipt in invoice.receipts.all():
                if (
                    receipt.status not in (CustomerReceipt.Status.POSTED, CustomerReceipt.Status.REVERSED)
                    or receipt.receipt_date > as_of
                ):
                    continue
                reversal = (
                    receipt.journal_entry.reversal_entry
                    if receipt.status == CustomerReceipt.Status.REVERSED and receipt.journal_entry_id
                    else None
                )
                if reversal and reversal.entry_date <= as_of:
                    continue
                paid_as_of += (
                    receipt.carrying_base_amount
                    if receipt.carrying_base_amount is not None else receipt.amount
                )
            batch_paid_as_of = batch_paid_by_invoice.get(invoice.pk, Decimal("0"))
            paid_as_of += batch_paid_as_of
            returned_as_of = returned_by_invoice.get(invoice.pk, Decimal("0"))
            fx_revaluation_adjustment = Decimal("0")
            if invoice.transaction_currency_id:
                _, _, carrying_balance = open_fx_invoice_balances(
                    invoice, direction="receivable", as_of=as_of,
                )
                balance_due = max(carrying_balance, Decimal("0"))
                fx_revaluation_adjustment = (
                    balance_due - max(invoice.grand_total - paid_as_of - returned_as_of, Decimal("0"))
                )
            else:
                balance_due = max(invoice.grand_total - paid_as_of - returned_as_of, Decimal("0"))
            if balance_due <= 0:
                continue
            due_date = invoice.invoice_date + timedelta(days=invoice.customer.payment_terms_days)
            overdue_days = max((as_of - due_date).days, 0)
            bucket = (
                "not_due" if due_date >= as_of else
                "1_30" if overdue_days <= 30 else
                "31_60" if overdue_days <= 60 else
                "61_90" if overdue_days <= 90 else "over_90"
            )
            customer = customers.setdefault(invoice.customer_id, {
                "customer": invoice.customer_id,
                "customer_code": invoice.customer.code,
                "customer_name": invoice.customer.name_ar,
                "not_due": Decimal("0"), "1_30": Decimal("0"),
                "31_60": Decimal("0"), "61_90": Decimal("0"),
                "over_90": Decimal("0"), "total_due": Decimal("0"),
                "unallocated_advances": Decimal("0"), "invoices": [],
            })
            customer[bucket] += balance_due
            customer["total_due"] += balance_due
            customer["invoices"].append({
                "invoice": invoice.pk,
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date.isoformat(),
                "due_date": due_date.isoformat(),
                "overdue_days": overdue_days,
                "bucket": bucket,
                "grand_total": str(invoice.grand_total),
                "paid_as_of": str(paid_as_of),
                "returned_as_of": str(returned_as_of),
                "fx_revaluation_adjustment": str(fx_revaluation_adjustment),
                "balance_due": str(balance_due),
            })
            totals[bucket] += balance_due
            totals["total_due"] += balance_due

        advance_batches = CustomerReceiptBatch.objects.filter(
            company_id=company_id, branch__in=accessible_branches(request.user),
            receipt_date__lte=as_of,
        ).filter(
            Q(status=CustomerReceiptBatch.Status.POSTED)
            | Q(status=CustomerReceiptBatch.Status.REVERSED, reversal_date__gt=as_of)
        ).select_related("customer")
        if customer_id:
            advance_batches = advance_batches.filter(customer_id=customer_id)
        if branch_id:
            advance_batches = advance_batches.filter(branch_id=branch_id)
        allocated_by_batch = {
            row["batch_id"]: row["total"]
            for row in CustomerReceiptBatchAllocation.objects.filter(
                batch__in=advance_batches, allocation_date__lte=as_of,
            ).values("batch_id").annotate(total=Sum("amount"))
        }
        refunded_by_batch = {
            row["source_receipt_batch_id"]: row["total"]
            for row in CustomerRefund.objects.filter(
                source_receipt_batch__in=advance_batches, refund_date__lte=as_of,
            ).filter(
                Q(status=CustomerRefund.Status.POSTED)
                | (
                    Q(status=CustomerRefund.Status.REVERSED)
                    & (
                        Q(reversal_journal_entry__entry_date__gt=as_of)
                        | Q(reversal_journal_entry__isnull=True)
                        | ~Q(reversal_journal_entry__status=JournalEntry.Status.POSTED)
                    )
                )
            ).values("source_receipt_batch_id").annotate(total=Sum("amount"))
        }
        for batch in advance_batches:
            allocated_as_of = allocated_by_batch.get(batch.pk, Decimal("0"))
            refunded_as_of = refunded_by_batch.get(batch.pk, Decimal("0"))
            unallocated = max(batch.amount - allocated_as_of - refunded_as_of, Decimal("0"))
            if unallocated <= 0:
                continue
            customer = customers.setdefault(batch.customer_id, {
                "customer": batch.customer_id,
                "customer_code": batch.customer.code,
                "customer_name": batch.customer.name_ar,
                "not_due": Decimal("0"), "1_30": Decimal("0"),
                "31_60": Decimal("0"), "61_90": Decimal("0"),
                "over_90": Decimal("0"), "total_due": Decimal("0"),
                "unallocated_advances": Decimal("0"), "invoices": [],
            })
            customer["unallocated_advances"] += unallocated
            totals["unallocated_advances"] += unallocated

        for customer in customers.values():
            for key in (*totals.keys(),):
                customer[key] = str(customer[key])
            customer["invoices"].sort(key=lambda row: (row["due_date"], row["invoice_number"]))
        return Response({
            "company": company_id,
            "as_of": as_of.isoformat(),
            "customer_filter": customer_id,
            "branch_filter": branch_id,
            "totals": {key: str(value) for key, value in totals.items()},
            "customers": sorted(customers.values(), key=lambda row: row["customer_name"]),
        })


    @action(detail=False, methods=("get",), url_path="sales-performance")
    def sales_performance(self, request):
        """تقرير المبيعات والمرتجعات والربحية حسب العميل والصنف لفترة محددة."""
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة لعرض تقرير المبيعات."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        date_from_raw = request.query_params.get("date_from", "")
        date_to_raw = request.query_params.get("date_to", "")
        date_from = parse_date(date_from_raw)
        date_to = parse_date(date_to_raw)
        if not date_from:
            raise ValidationError({"date_from": "حدد تاريخ بداية صحيحًا بصيغة YYYY-MM-DD."})
        if not date_to:
            raise ValidationError({"date_to": "حدد تاريخ نهاية صحيحًا بصيغة YYYY-MM-DD."})
        if date_from > date_to:
            raise ValidationError({"date_to": "تاريخ النهاية يجب ألا يسبق تاريخ البداية."})

        accessible_invoices = _historical_sales_invoice_queryset(
            request.user, company_id,
        ).filter(status=SalesInvoice.Status.POSTED)
        branch_raw = request.query_params.get("branch")
        branch_id = None
        if branch_raw:
            try:
                branch_id = int(branch_raw)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."}) from None
            if not _historical_branch_accessible(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            accessible_invoices = accessible_invoices.filter(
                Q(delivery__warehouse__branch_id=branch_id) | Q(branch_id=branch_id)
            )

        customer_raw = request.query_params.get("customer")
        customer_id = None
        if customer_raw:
            try:
                customer_id = int(customer_raw)
            except (TypeError, ValueError):
                raise ValidationError({"customer": "معرّف العميل غير صالح."}) from None
            accessible_invoices = accessible_invoices.filter(customer_id=customer_id)

        product_raw = request.query_params.get("product")
        product_id = None
        if product_raw:
            try:
                product_id = int(product_raw)
            except (TypeError, ValueError):
                raise ValidationError({"product": "معرّف الصنف غير صالح."}) from None
            accessible_invoices = accessible_invoices.filter(lines__product_id=product_id).distinct()

        invoices = accessible_invoices.filter(invoice_date__range=(date_from, date_to))
        # Keep the original return in its posting period even if it was later
        # reversed; report the reversal as a positive adjustment in its own period.
        returns = SalesReturn.objects.filter(
            invoice__in=accessible_invoices,
            status__in=(SalesReturn.Status.POSTED, SalesReturn.Status.REVERSED),
            return_date__range=(date_from, date_to),
            journal_entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
        )
        return_reversals = SalesReturn.objects.filter(
            invoice__in=accessible_invoices,
            status=SalesReturn.Status.REVERSED,
            reversal_date__range=(date_from, date_to),
            reversal_journal_entry__status=JournalEntry.Status.POSTED,
        )
        if product_id is not None:
            returns = returns.filter(lines__invoice_line__product_id=product_id).distinct()
            return_reversals = return_reversals.filter(
                lines__invoice_line__product_id=product_id,
            ).distinct()
        invoice_lines = SalesInvoiceLine.objects.filter(invoice__in=invoices)
        return_lines = SalesReturnLine.objects.filter(sales_return__in=returns)
        return_reversal_lines = SalesReturnLine.objects.filter(
            sales_return__in=return_reversals,
        )
        if product_id is not None:
            invoice_lines = invoice_lines.filter(product_id=product_id)
            return_lines = return_lines.filter(invoice_line__product_id=product_id)
            return_reversal_lines = return_reversal_lines.filter(
                invoice_line__product_id=product_id,
            )

        def amount(value):
            return value if value is not None else Decimal("0")

        def cost_amount(value):
            # Monetary cost is stored and reported to four decimal places.
            return amount(value).quantize(Decimal("0.0001"))

        if product_id is not None:
            # عند اختيار صنف، تُحسب الإجماليات من بنوده فقط لا من إجمالي الفاتورة التي قد تضم أصنافًا أخرى.
            sales_totals = invoice_lines.aggregate(
                net=Sum("net_amount"), discount=Sum("discount_amount"),
                tax=Sum("tax_amount"), total=Sum("total_amount"),
            )
            invoice_count = invoice_lines.values("invoice_id").distinct().count()
            return_count = return_lines.values("sales_return_id").distinct().count()
        else:
            sales_totals = invoices.aggregate(
                net=Sum("net_total"), discount=Sum("discount_total"),
                tax=Sum("tax_total"), total=Sum("grand_total"),
            )
            invoice_count = invoices.count()
            return_count = returns.count()
        return_totals_raw = return_lines.aggregate(
            net=Sum("net_amount"), tax=Sum("tax_amount"), total=Sum("total_amount"),
        )
        return_reversal_totals = return_reversal_lines.aggregate(
            net=Sum("net_amount"), tax=Sum("tax_amount"), total=Sum("total_amount"),
        )
        return_totals = {
            key: amount(return_totals_raw[key]) - amount(return_reversal_totals[key])
            for key in ("net", "tax", "total")
        }
        def invoice_lines_with_allocated_cost():
            # Annotate the per-invoice share first, then aggregate the annotation.
            # This avoids passing a mutable expression directly into multiple aggregates.
            return invoice_lines.annotate(
                _allocated_sales_cost=ExpressionWrapper(
                    F("delivery_line__cost_amount") * F("quantity") / F("delivery_line__quantity"),
                    output_field=DecimalField(max_digits=20, decimal_places=4),
                )
            )

        sales_cost = cost_amount(
            invoice_lines_with_allocated_cost().aggregate(total=Sum("_allocated_sales_cost"))["total"]
        )
        return_cost = (
            amount(return_lines.aggregate(total=Sum("cost_amount"))["total"])
            - amount(return_reversal_lines.aggregate(total=Sum("cost_amount"))["total"])
        )
        sales_net = amount(sales_totals["net"])
        returned_net = amount(return_totals["net"])
        net_sales = sales_net - returned_net
        net_cost = sales_cost - return_cost

        customer_sales = invoice_lines_with_allocated_cost().values(
            "invoice__customer_id", "invoice__customer__name_ar",
        ).annotate(
            invoice_count=Count("invoice_id", distinct=True),
            quantity=Sum("quantity"), net=Sum("net_amount"), tax=Sum("tax_amount"),
            discount=Sum("discount_amount"), cost=Sum("_allocated_sales_cost"),
        )
        customer_returns = return_lines.values(
            "sales_return__invoice__customer_id",
            "sales_return__invoice__customer__name_ar",
        ).annotate(
            quantity=Sum("quantity"), net=Sum("net_amount"),
            tax=Sum("tax_amount"), cost=Sum("cost_amount"),
        )
        customer_return_reversals = return_reversal_lines.values(
            "sales_return__invoice__customer_id",
            "sales_return__invoice__customer__name_ar",
        ).annotate(
            quantity=Sum("quantity"), net=Sum("net_amount"),
            tax=Sum("tax_amount"), cost=Sum("cost_amount"),
        )
        customers = {}
        for row in customer_sales:
            key = row["invoice__customer_id"]
            customers[key] = {
                "customer": key,
                "customer_name": row["invoice__customer__name_ar"],
                "invoice_count": row["invoice_count"],
                "quantity_sold": amount(row["quantity"]),
                "quantity_returned": Decimal("0"),
                "sales_net": amount(row["net"]),
                "return_net": Decimal("0"),
                "sales_tax": amount(row["tax"]),
                "return_tax": Decimal("0"),
                "sales_cost": cost_amount(row["cost"]),
                "return_cost": Decimal("0"),
            }
        for grouped_rows, sign in (
            (customer_returns, Decimal("1")),
            (customer_return_reversals, Decimal("-1")),
        ):
            for row in grouped_rows:
                key = row["sales_return__invoice__customer_id"]
                customer = customers.setdefault(key, {
                    "customer": key,
                    "customer_name": row["sales_return__invoice__customer__name_ar"],
                    "invoice_count": 0,
                    "quantity_sold": Decimal("0"),
                    "quantity_returned": Decimal("0"),
                    "sales_net": Decimal("0"),
                    "return_net": Decimal("0"),
                    "sales_tax": Decimal("0"),
                    "return_tax": Decimal("0"),
                    "sales_cost": Decimal("0"),
                    "return_cost": Decimal("0"),
                })
                customer["quantity_returned"] += sign * amount(row["quantity"])
                customer["return_net"] += sign * amount(row["net"])
                customer["return_tax"] += sign * amount(row["tax"])
                customer["return_cost"] += sign * amount(row["cost"])

        product_sales = invoice_lines_with_allocated_cost().values(
            "product_id", "product__code", "product__name_ar",
        ).annotate(
            quantity=Sum("quantity"), net=Sum("net_amount"), tax=Sum("tax_amount"),
            discount=Sum("discount_amount"), cost=Sum("_allocated_sales_cost"),
            invoice_count=Count("invoice_id", distinct=True),
        )
        product_returns = return_lines.values(
            "invoice_line__product_id", "invoice_line__product__code",
            "invoice_line__product__name_ar",
        ).annotate(
            quantity=Sum("quantity"), net=Sum("net_amount"),
            tax=Sum("tax_amount"), cost=Sum("cost_amount"),
        )
        product_return_reversals = return_reversal_lines.values(
            "invoice_line__product_id", "invoice_line__product__code",
            "invoice_line__product__name_ar",
        ).annotate(
            quantity=Sum("quantity"), net=Sum("net_amount"),
            tax=Sum("tax_amount"), cost=Sum("cost_amount"),
        )
        products = {}
        for row in product_sales:
            key = row["product_id"]
            products[key] = {
                "product": key,
                "product_code": row["product__code"],
                "product_name": row["product__name_ar"],
                "invoice_count": row["invoice_count"],
                "quantity_sold": amount(row["quantity"]),
                "quantity_returned": Decimal("0"),
                "sales_net": amount(row["net"]),
                "return_net": Decimal("0"),
                "sales_tax": amount(row["tax"]),
                "return_tax": Decimal("0"),
                "sales_cost": cost_amount(row["cost"]),
                "return_cost": Decimal("0"),
            }
        for grouped_rows, sign in (
            (product_returns, Decimal("1")),
            (product_return_reversals, Decimal("-1")),
        ):
            for row in grouped_rows:
                key = row["invoice_line__product_id"]
                product = products.setdefault(key, {
                    "product": key,
                    "product_code": row["invoice_line__product__code"],
                    "product_name": row["invoice_line__product__name_ar"],
                    "invoice_count": 0,
                    "quantity_sold": Decimal("0"),
                    "quantity_returned": Decimal("0"),
                    "sales_net": Decimal("0"),
                    "return_net": Decimal("0"),
                    "sales_tax": Decimal("0"),
                    "return_tax": Decimal("0"),
                    "sales_cost": Decimal("0"),
                    "return_cost": Decimal("0"),
                })
                product["quantity_returned"] += sign * amount(row["quantity"])
                product["return_net"] += sign * amount(row["net"])
                product["return_tax"] += sign * amount(row["tax"])
                product["return_cost"] += sign * amount(row["cost"])

        def serialize_group(row):
            sales_net_value = row["sales_net"] - row["return_net"]
            cost_value = row["sales_cost"] - row["return_cost"]
            return {
                **{key: value for key, value in row.items() if not key.endswith("_cost")},
                "net_sales": str(sales_net_value),
                "net_cost": str(cost_value),
                "gross_profit": str(sales_net_value - cost_value),
                "quantity_sold": str(row["quantity_sold"]),
                "quantity_returned": str(row["quantity_returned"]),
                "sales_net": str(row["sales_net"]),
                "return_net": str(row["return_net"]),
                "sales_tax": str(row["sales_tax"]),
                "return_tax": str(row["return_tax"]),
            }

        return Response({
            "company": company_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "branch": branch_id,
            "customer_filter": customer_id,
            "product_filter": product_id,
            "totals": {
                "invoice_count": invoice_count,
                "return_count": return_count,
                "sales_net": str(sales_net),
                "discount_total": str(amount(sales_totals["discount"])),
                "sales_tax": str(amount(sales_totals["tax"])),
                "sales_total": str(amount(sales_totals["total"])),
                "return_net": str(returned_net),
                "return_tax": str(amount(return_totals["tax"])),
                "return_total": str(amount(return_totals["total"])),
                "net_sales": str(net_sales),
                "net_cost": str(net_cost),
                "gross_profit": str(net_sales - net_cost),
            },
            "by_customer": sorted(
                (serialize_group(row) for row in customers.values()),
                key=lambda row: row["customer_name"],
            ),
            "by_product": sorted(
                (serialize_group(row) for row in products.values()),
                key=lambda row: row["product_name"],
            ),
        })


    @action(detail=False, methods=("get",), url_path="sales-performance-export")
    def sales_performance_export(self, request):
        """تنزيل تقرير أداء المبيعات كملف Excel متعدد الأوراق بنفس فلاتر التقرير وصلاحياته."""
        report = self.sales_performance(request).data
        company = Company.objects.select_related("base_currency").get(pk=report["company"])
        currency_code = company.base_currency.code
        branch_label = (
            Branch.objects.filter(pk=report["branch"], company_id=company.pk)
            .values_list("name_ar", flat=True).first() if report["branch"] else "كل الفروع"
        )
        customer_label = (
            Customer.objects.filter(pk=report["customer_filter"], company_id=company.pk)
            .values_list("name_ar", flat=True).first() if report["customer_filter"] else "كل العملاء"
        )
        product_label = (
            Product.objects.filter(pk=report["product_filter"], company_id=company.pk)
            .values_list("name_ar", flat=True).first() if report["product_filter"] else "كل الأصناف"
        )

        def safe_text(value):
            value = str(value or "")
            return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value

        workbook = Workbook()
        summary = workbook.active
        summary.title = "ملخص"
        summary.sheet_view.rightToLeft = True
        summary.append(["تقرير أداء المبيعات"])
        summary.append(["الشركة", safe_text(company.name_ar)])
        summary.append(["العملة الأساسية", currency_code])
        summary.append(["من تاريخ", report["date_from"], "إلى تاريخ", report["date_to"]])
        summary.append(["الفرع", safe_text(branch_label)])
        summary.append(["العميل", safe_text(customer_label)])
        summary.append(["الصنف", safe_text(product_label)])
        summary.append([])
        summary.append(["المؤشر", "القيمة"])
        total_labels = (
            ("عدد الفواتير", "invoice_count"), ("عدد المرتجعات", "return_count"),
            ("صافي المبيعات قبل المرتجعات", "sales_net"), ("إجمالي الخصومات", "discount_total"),
            ("ضريبة المبيعات", "sales_tax"), ("إجمالي المبيعات", "sales_total"),
            ("صافي المرتجعات", "return_net"), ("ضريبة المرتجعات", "return_tax"),
            ("إجمالي المرتجعات", "return_total"), ("صافي المبيعات بعد المرتجعات", "net_sales"),
            ("تكلفة المبيعات الصافية", "net_cost"), ("مجمل الربح", "gross_profit"),
        )
        for label, key in total_labels:
            value = report["totals"][key]
            summary.append([label, int(value) if key.endswith("_count") else Decimal(str(value))])
        customers = workbook.create_sheet("حسب العميل")
        customers.sheet_view.rightToLeft = True
        customer_headers = ["العميل", "عدد الفواتير", "الكمية المباعة", "الكمية المرتجعة",
                            "المبيعات قبل المرتجعات", "المرتجعات", "ضريبة المبيعات",
                            "ضريبة المرتجعات", "صافي المبيعات", "تكلفة المبيعات", "مجمل الربح"]
        customers.append(customer_headers)
        for row in report["by_customer"]:
            customers.append([
                safe_text(row["customer_name"]), row["invoice_count"], Decimal(row["quantity_sold"]),
                Decimal(row["quantity_returned"]), Decimal(row["sales_net"]), Decimal(row["return_net"]),
                Decimal(row["sales_tax"]), Decimal(row["return_tax"]), Decimal(row["net_sales"]),
                Decimal(row["net_cost"]), Decimal(row["gross_profit"]),
            ])
        products = workbook.create_sheet("حسب الصنف")
        products.sheet_view.rightToLeft = True
        product_headers = ["كود الصنف", "الصنف", "عدد الفواتير", "الكمية المباعة", "الكمية المرتجعة",
                           "المبيعات قبل المرتجعات", "المرتجعات", "ضريبة المبيعات",
                           "ضريبة المرتجعات", "صافي المبيعات", "تكلفة المبيعات", "مجمل الربح"]
        products.append(product_headers)
        for row in report["by_product"]:
            products.append([
                safe_text(row["product_code"]), safe_text(row["product_name"]), row["invoice_count"],
                Decimal(row["quantity_sold"]), Decimal(row["quantity_returned"]),
                Decimal(row["sales_net"]), Decimal(row["return_net"]), Decimal(row["sales_tax"]),
                Decimal(row["return_tax"]), Decimal(row["net_sales"]), Decimal(row["net_cost"]),
                Decimal(row["gross_profit"]),
            ])

        for sheet in workbook.worksheets:
            sheet.freeze_panes = "A2" if sheet is not summary else "A9"
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF", size=14)
                cell.fill = PatternFill("solid", fgColor="1F4E79")
            for cell in sheet[1 if sheet is not summary else 9]:
                cell.alignment = Alignment(horizontal="right", vertical="center")
            if sheet is not summary:
                sheet.auto_filter.ref = sheet.dimensions
                for cell in sheet[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="1F4E79")
            for column in sheet.columns:
                letter = column[0].column_letter
                width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 34)
                sheet.column_dimensions[letter].width = width
            for row in sheet.iter_rows():
                for cell in row:
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                    if isinstance(cell.value, Decimal):
                        cell.number_format = '#,##0.00;[Red]-#,##0.00'
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="sales-performance-{report["date_from"]}-{report["date_to"]}.xlsx"'
        )
        response["Cache-Control"] = "private, no-store"
        return response


class CustomerReceiptViewSet(SalesBaseViewSet):
    serializer_class = CustomerReceiptSerializer
    queryset = CustomerReceipt.objects.select_related("company", "customer", "invoice", "transaction_currency", "journal_entry", "journal_entry__reversal_entry")
    filterset_fields = ("company", "customer", "invoice", "status", "receipt_date", "method")
    search_fields = ("receipt_number", "reference", "customer__name_ar", "invoice__invoice_number")
    ordering_fields = ("receipt_date", "receipt_number", "amount", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        companies = accessible_companies(self.request.user)
        qs = self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        default_company_ids = []
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            default_company_ids = list(companies.values_list("id", flat=True))
        else:
            for company_id in companies.values_list("id", flat=True):
                default_branch = Branch.objects.filter(
                    company_id=company_id, is_active=True
                ).order_by("-is_head_office", "code").first()
                if default_branch and can_access_branch(self.request.user, default_branch.pk, company_id):
                    default_company_ids.append(company_id)
        return qs.filter(
            Q(invoice__delivery__warehouse__branch__in=branches)
            | Q(invoice__delivery__isnull=False, invoice__delivery__warehouse__branch__isnull=True, company_id__in=default_company_ids)
            | Q(invoice__branch__in=branches)
        )

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        invoice = data["invoice"]
        if invoice.delivery_id:
            accessible = _sales_warehouse_accessible(self.request.user, invoice.delivery.warehouse)
        else:
            accessible = bool(invoice.branch_id and can_access_branch(self.request.user, invoice.branch_id, invoice.company_id))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة أو الفرع الرئيسي للمخزن العام.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل سند القبض غير متاح؛ أنشئ سندًا جديدًا إذا كان مسودة.")

    @transaction.atomic
    def perform_destroy(self, instance):
        """حذف سند قبض مسودة فقط مع التحقق من صلاحية فرع الفاتورة."""
        receipt = CustomerReceipt.objects.select_for_update().select_related(
            "company", "invoice", "invoice__delivery__warehouse", "invoice__branch",
        ).get(pk=instance.pk)
        self._check_write(receipt.company_id)
        invoice = receipt.invoice
        if invoice.delivery_id:
            accessible = _sales_warehouse_accessible(self.request.user, invoice.delivery.warehouse)
        else:
            accessible = bool(invoice.branch_id and can_access_branch(
                self.request.user, invoice.branch_id, invoice.company_id,
            ))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة أو الفرع الرئيسي للمخزن العام.")
        if receipt.status != CustomerReceipt.Status.DRAFT or receipt.journal_entry_id:
            raise ValidationError("لا يمكن حذف سند قبض مرحّل أو مرتبط بقيد؛ استخدم إجراء العكس المعتمد.")
        receipt.delete()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def post(self, request, pk=None):
        """ترحيل سند القبض مع تثبيت قالب الطباعة وقت الإصدار."""
        receipt = self.get_object()
        self._check_write(receipt.company_id)
        invoice = receipt.invoice
        if invoice.delivery_id:
            accessible = _sales_warehouse_accessible(request.user, invoice.delivery.warehouse)
        else:
            accessible = bool(invoice.branch_id and can_access_branch(request.user, invoice.branch_id, invoice.company_id))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة أو الفرع الرئيسي للمخزن العام.")
        receipt.print_template_snapshot = _document_template_snapshot(
            receipt.company, SalesDocumentTemplate.DocumentType.RECEIPT,
        )
        receipt.save(update_fields=("print_template_snapshot",))
        receipt = post_customer_receipt(receipt.pk, request.user)
        return Response(self.get_serializer(receipt).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        """عكس آخر سند قبض مرحّل بقيد عكسي كامل مع حماية تسلسل التسويات."""
        receipt = CustomerReceipt.objects.select_for_update(of=("self",)).select_related(
            "invoice", "invoice__delivery__warehouse", "invoice__branch", "journal_entry"
        ).get(pk=self.get_object().pk)
        self._check_write(receipt.company_id)
        invoice = SalesInvoice.objects.select_for_update(of=("self",)).select_related("delivery__warehouse", "branch").get(pk=receipt.invoice_id)
        if invoice.delivery_id:
            accessible = _sales_warehouse_accessible(request.user, invoice.delivery.warehouse)
        else:
            accessible = bool(invoice.branch_id and can_access_branch(request.user, invoice.branch_id, invoice.company_id))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة أو الفرع الرئيسي للمخزن العام.")
        if receipt.status != CustomerReceipt.Status.POSTED or not receipt.journal_entry_id:
            raise ValidationError("لا يمكن عكس سند قبض غير مرحّل.")
        if receipt.journal_entry.status != JournalEntry.Status.POSTED:
            raise ValidationError("القيد المرتبط بسند القبض ليس مرحّلًا.")
        later_receipts = CustomerReceipt.objects.filter(
            invoice_id=receipt.invoice_id, status=CustomerReceipt.Status.POSTED
        ).filter(
            Q(receipt_date__gt=receipt.receipt_date)
            | Q(receipt_date=receipt.receipt_date, pk__gt=receipt.pk)
        ).exists()
        if later_receipts:
            raise ValidationError("يجب عكس أحدث سند قبض على الفاتورة أولًا.")
        if FxSubledgerRevaluation.objects.filter(
            sales_invoice_id=receipt.invoice_id,
        ).filter(
            Q(journal_entry__isnull=True)
            | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)
        ).exists():
            raise ValidationError("لا يمكن عكس سند قبض لفاتورة لها إعادة تقييم عملة سارية؛ راجع تسلسل التسويات وإعادة التقييم أولًا.")
        entry_date_raw = request.data.get("entry_date")
        try:
            entry_date = parse_date(str(entry_date_raw)) if entry_date_raw else timezone.localdate()
        except (TypeError, ValueError):
            entry_date = None
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        cash_account_id = receipt.payment_account_id or receipt.journal_entry.lines.filter(
            description="استلام نقدية/تحويل من العميل",
        ).values_list("account_id", flat=True).first()
        cash_totals = JournalLine.objects.filter(
            account_id=cash_account_id,
            entry__entry_date__lte=entry_date,
            entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        from treasury.models import TreasuryAccount
        opening_balance = TreasuryAccount.objects.filter(
            company_id=receipt.company_id, account_id=cash_account_id,
            opening_journal_entry__isnull=True,
        ).aggregate(total=Sum("opening_balance"))["total"] or Decimal("0")
        cash_balance = opening_balance + (cash_totals["debit"] or Decimal("0")) - (cash_totals["credit"] or Decimal("0"))
        if cash_balance < receipt.amount:
            raise ValidationError(
                f"لا يمكن عكس سند القبض: رصيد حساب الخزينة/البنك في تاريخ العكس ({cash_balance}) أقل من مبلغ السند ({receipt.amount})."
            )
        try:
            reversal = reverse_operational_journal(
                receipt.journal_entry_id, request.user, entry_date, JournalEntry.Source.SALES
            )
            from treasury.services import reverse_linked_document_treasury_movement
            reverse_linked_document_treasury_movement(
                journal_entry_id=receipt.journal_entry_id,
                reversal_journal_entry=reversal,
                user=request.user,
            )
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        CustomerReceipt.objects.filter(pk=receipt.pk, status=CustomerReceipt.Status.POSTED).update(
            status=CustomerReceipt.Status.REVERSED
        )
        receipt.refresh_from_db()
        data = self.get_serializer(receipt).data
        data["reversal_journal_entry"] = reversal.pk
        data["reversal_journal_number"] = reversal.entry_number
        return Response(data, status=status.HTTP_200_OK)
