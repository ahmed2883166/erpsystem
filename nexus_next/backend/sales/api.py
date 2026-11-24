from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from accounting.models import FxSubledgerRevaluation, JournalEntry, JournalLine
from accounting.services import reverse_operational_journal
from accounting.fx_subledger_services import revalue_fx_invoice
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from companies.models import Branch
from inventory.models import StockMovement
from .models import CustomerReceipt, SalesDelivery, SalesInvoice, SalesReturn, SalesQuotation, SalesDocumentTemplate
from .serializers import CustomerReceiptSerializer, SalesDeliverySerializer, SalesInvoiceSerializer, SalesQuotationSerializer, SalesDocumentTemplateSerializer
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
            raise ValidationError({"company": "حدد الشركة."})
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
        if hasattr(instance, "invoice"):
            raise ValidationError("لا يمكن حذف إذن تسليم مرتبط بفاتورة.")
        movement = instance.stock_movement
        instance.delete()
        movement.delete()

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        delivery = self.get_object()
        self._check_write(delivery.company_id)
        if not _sales_warehouse_accessible(request.user, delivery.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")
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
            raise ValidationError("انتهت صلاحية عرض السعر.")
        try:
            warehouse_id = int(request.data.get("warehouse"))
        except (TypeError, ValueError):
            raise ValidationError({"warehouse": "حدد المخزن."})
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
    queryset = SalesInvoice.objects.select_related("company", "customer", "delivery", "delivery__warehouse", "journal_entry").prefetch_related("lines__delivery_line__product", "lines__tax_code", "receipts", "approval_requests")
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
            | Q(delivery__warehouse__branch__isnull=True, company_id__in=default_company_ids)
        )

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        warehouse = data["delivery"].warehouse
        if not _sales_warehouse_accessible(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن التسليم.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        invoice = serializer.instance
        self._check_write(invoice.company_id)
        if invoice.status != SalesInvoice.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل فاتورة مبيعات مرحلة.")
        warehouse = invoice.delivery.warehouse
        if not _sales_warehouse_accessible(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        """حذف فاتورة مسودة بلا تحصيل أو اعتماد أو مردود أو أثر إعادة تقييم."""
        invoice = SalesInvoice.objects.select_for_update().select_related(
            "company", "delivery__warehouse"
        ).get(pk=instance.pk)
        self._check_write(invoice.company_id)
        if not _sales_warehouse_accessible(self.request.user, invoice.delivery.warehouse):
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
        self._check_write(invoice.company_id)
        warehouse = invoice.delivery.warehouse
        if not _sales_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن التسليم.")
        invoice.print_template_snapshot = _document_template_snapshot(invoice.company, SalesDocumentTemplate.DocumentType.INVOICE)
        invoice.save(update_fields=("print_template_snapshot",))
        invoice = post_sales_invoice(invoice.pk, request.user)
        return Response(self.get_serializer(invoice).data, status=status.HTTP_200_OK)


    @action(detail=True, methods=("post",), url_path="revalue")
    def revalue(self, request, pk=None):
        invoice = self.get_object()
        self._check_write(invoice.company_id)
        if not _sales_warehouse_accessible(request.user, invoice.delivery.warehouse):
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
            raise ValidationError({"detail": "حدد الشركة والعميل لعرض كشف الحساب."})
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
                raise ValidationError({"branch": "معرّف الفرع غير صالح."})
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

        invoices = self.get_queryset().filter(
            company_id=company_id, customer_id=customer_id,
            status=SalesInvoice.Status.POSTED,
        ).select_related("customer", "delivery__warehouse")
        if branch_id:
            invoices = invoices.filter(delivery__warehouse__branch_id=branch_id)

        events = []
        for invoice in invoices:
            events.append({
                "date": invoice.invoice_date, "kind": "invoice",
                "number": invoice.invoice_number,
                "description": f"فاتورة مبيعات {invoice.invoice_number}",
                "debit": invoice.grand_total, "credit": Decimal("0"), "sort": 0,
            })
        receipts = CustomerReceipt.objects.filter(
            invoice__in=invoices, status=CustomerReceipt.Status.POSTED,
        ).select_related("invoice")
        for receipt in receipts:
            events.append({
                "date": receipt.receipt_date, "kind": "receipt",
                "number": receipt.receipt_number,
                "description": f"سند قبض عن فاتورة {receipt.invoice.invoice_number}",
                "debit": Decimal("0"), "credit": receipt.carrying_base_amount if receipt.carrying_base_amount is not None else receipt.amount, "sort": 1,
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
            raise ValidationError({"company": "حدد الشركة لعرض أعمار مديونية العملاء."})
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
                raise ValidationError({"customer": "معرّف العميل غير صالح."})
            from customers.models import Customer
            if not Customer.objects.filter(pk=customer_id, company_id=company_id).exists():
                raise ValidationError({"customer": "العميل غير موجود في الشركة المحددة."})

        branch_id_raw = request.query_params.get("branch")
        branch_id = None
        if branch_id_raw:
            try:
                branch_id = int(branch_id_raw)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."})
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

        invoices = self.get_queryset().filter(
            company_id=company_id,
            status=SalesInvoice.Status.POSTED,
            invoice_date__lte=as_of,
        ).select_related("customer", "delivery__warehouse").prefetch_related("receipts")
        if customer_id:
            invoices = invoices.filter(customer_id=customer_id)
        if branch_id:
            invoices = invoices.filter(delivery__warehouse__branch_id=branch_id)

        returned_by_invoice = {
            row["invoice_id"]: row["total"]
            for row in SalesReturn.objects.filter(
                invoice__in=invoices, return_date__lte=as_of,
            ).filter(
                Q(status=SalesReturn.Status.POSTED)
                | Q(status=SalesReturn.Status.REVERSED, reversal_date__gt=as_of)
            ).values("invoice_id").annotate(total=Sum("grand_total"))
        }
        customers = {}
        totals = {
            "not_due": Decimal("0"), "1_30": Decimal("0"),
            "31_60": Decimal("0"), "61_90": Decimal("0"),
            "over_90": Decimal("0"), "total_due": Decimal("0"),
        }
        for invoice in invoices:
            paid_as_of = sum(
                ((receipt.carrying_base_amount if receipt.carrying_base_amount is not None else receipt.amount) for receipt in invoice.receipts.all()
                 if receipt.status == CustomerReceipt.Status.POSTED
                 and receipt.receipt_date <= as_of),
                Decimal("0"),
            )
            returned_as_of = returned_by_invoice.get(invoice.pk, Decimal("0"))
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
                "invoices": [],
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
                "balance_due": str(balance_due),
            })
            totals[bucket] += balance_due
            totals["total_due"] += balance_due

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


class CustomerReceiptViewSet(SalesBaseViewSet):
    serializer_class = CustomerReceiptSerializer
    queryset = CustomerReceipt.objects.select_related("company", "customer", "invoice", "journal_entry", "journal_entry__reversal_entry")
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
            | Q(invoice__delivery__warehouse__branch__isnull=True, company_id__in=default_company_ids)
        )

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        warehouse = data["invoice"].delivery.warehouse
        if not _sales_warehouse_accessible(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة أو الفرع الرئيسي للمخزن العام.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل سند القبض غير متاح؛ أنشئ سندًا جديدًا إذا كان مسودة.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        receipt = self.get_object()
        self._check_write(receipt.company_id)
        warehouse = receipt.invoice.delivery.warehouse
        if not _sales_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة أو الفرع الرئيسي للمخزن العام.")
        receipt = post_customer_receipt(receipt.pk, request.user)
        return Response(self.get_serializer(receipt).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        """عكس آخر سند قبض مرحّل بقيد عكسي كامل مع حماية تسلسل التسويات."""
        receipt = CustomerReceipt.objects.select_for_update(of=("self",)).select_related(
            "invoice", "invoice__delivery__warehouse", "journal_entry"
        ).get(pk=self.get_object().pk)
        self._check_write(receipt.company_id)
        invoice = SalesInvoice.objects.select_for_update().select_related("delivery__warehouse").get(pk=receipt.invoice_id)
        warehouse = invoice.delivery.warehouse
        if not _sales_warehouse_accessible(request.user, warehouse):
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
