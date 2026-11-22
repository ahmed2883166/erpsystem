from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from inventory.models import Warehouse
from .models import PurchaseInvoice, PurchaseOrder, PurchaseReceipt, SupplierPayment
from .serializers import PurchaseInvoiceSerializer, PurchaseOrderSerializer, PurchaseReceiptSerializer, SupplierPaymentSerializer
from .services import post_purchase_invoice, post_purchase_receipt, post_supplier_payment


class PurchaseBaseViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def _check_write(self, company_id):
        """التحقق من صلاحية المستخدم لتعديل بيانات الوحدة داخل الشركة."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("إجراءات المشتريات متاحة لمدير الشركة والمحاسب فقط.")

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        raise PermissionDenied("لا يمكن حذف مستندات المشتريات؛ استخدم الإلغاء المعتمد بعد إضافته.")


class PurchaseOrderViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseOrderSerializer
    queryset = PurchaseOrder.objects.select_related("company", "supplier", "warehouse", "warehouse__branch").prefetch_related("lines__product")
    filterset_fields = ("company", "supplier", "warehouse", "status", "order_date")
    search_fields = ("order_number", "supplier__name_ar")
    ordering_fields = ("order_date", "order_number", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        return qs.filter(Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=accessible_branches(self.request.user)))

    def create(self, request, *args, **kwargs):
        """إنشاء مستند جديد والتحقق من مدخلاته وصلاحيات الشركة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        if data["warehouse"].branch_id and not can_access_branch(self.request.user, data["warehouse"].branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        order = serializer.instance
        self._check_write(order.company_id)
        warehouse = serializer.validated_data.get("warehouse", order.warehouse)
        if warehouse.branch_id and not can_access_branch(
            self.request.user, warehouse.branch_id, order.company_id
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    @action(detail=True, methods=("post",))
    def confirm(self, request, pk=None):
        """اعتماد المستند ونقله إلى الحالة التالية وفق دورة العمل."""
        order = self.get_object()
        self._check_write(order.company_id)
        if order.status != PurchaseOrder.Status.DRAFT:
            raise ValidationError("أمر الشراء ليس مسودة.")
        if not order.lines.exists():
            raise ValidationError("أمر الشراء لا يحتوي على أصناف.")
        from django.utils import timezone
        PurchaseOrder.objects.filter(pk=order.pk, status=PurchaseOrder.Status.DRAFT).update(
            status=PurchaseOrder.Status.CONFIRMED, confirmed_at=timezone.now()
        )
        order.refresh_from_db()
        return Response(self.get_serializer(order).data, status=status.HTTP_200_OK)


class PurchaseReceiptViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseReceiptSerializer
    queryset = PurchaseReceipt.objects.select_related("company", "supplier", "warehouse", "stock_movement").prefetch_related("stock_movement__lines__product")
    filterset_fields = ("company", "supplier", "warehouse", "receipt_date")
    search_fields = ("receipt_number", "supplier_delivery_ref", "supplier__name_ar")
    ordering_fields = ("receipt_date", "receipt_number", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        branches = accessible_branches(self.request.user)
        return qs.filter(Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=branches))

    def create(self, request, *args, **kwargs):
        """إنشاء مستند جديد والتحقق من مدخلاته وصلاحيات الشركة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        warehouse = data["warehouse"]
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        receipt = serializer.instance
        self._check_write(receipt.company_id)
        if receipt.stock_movement.status != StockMovement.Status.DRAFT:
            raise ValidationError("إذن الاستلام المرحّل غير قابل للتعديل؛ أنشئ مستند تسوية مستقلًا بدل تغيير حركة المخزون.")
        raise ValidationError("تعديل إذن الاستلام غير متاح حاليًا؛ أنشئ إذن استلام مسودة جديدًا.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """ترحيل المستند أو تنفيذ إجراء الترحيل المرتبط به."""
        receipt = self.get_object()
        self._check_write(receipt.company_id)
        if receipt.warehouse.branch_id and not can_access_branch(request.user, receipt.warehouse.branch_id, receipt.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        receipt = post_purchase_receipt(receipt.pk, request.user)
        return Response(self.get_serializer(receipt).data, status=status.HTTP_200_OK)


class PurchaseInvoiceViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseInvoiceSerializer
    queryset = PurchaseInvoice.objects.select_related("company", "supplier", "receipt", "receipt__warehouse", "journal_entry").prefetch_related("lines__receipt_line__product", "payments")
    filterset_fields = ("company", "supplier", "status", "invoice_date")
    search_fields = ("invoice_number", "supplier__name_ar", "receipt__receipt_number")
    ordering_fields = ("invoice_date", "invoice_number", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        branches = accessible_branches(self.request.user)
        return qs.filter(Q(receipt__warehouse__branch__isnull=True) | Q(receipt__warehouse__branch__in=branches))

    def create(self, request, *args, **kwargs):
        """إنشاء مستند جديد والتحقق من مدخلاته وصلاحيات الشركة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        receipt = data["receipt"]
        if receipt.warehouse.branch_id and not can_access_branch(self.request.user, receipt.warehouse.branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        invoice = serializer.instance
        self._check_write(invoice.company_id)
        if invoice.status != PurchaseInvoice.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل فاتورة مورد مرحلة.")
        receipt = invoice.receipt
        if receipt.warehouse.branch_id and not can_access_branch(
            self.request.user, receipt.warehouse.branch_id, invoice.company_id
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        serializer.save()

    @action(detail=False, methods=("get",), url_path="supplier-statement")
    def supplier_statement(self, request):
        """إعداد كشف حركة المورد ورصيده للفترة المحددة."""
        try:
            company_id = int(request.query_params.get("company", ""))
            supplier_id = int(request.query_params.get("supplier", ""))
        except (TypeError, ValueError):
            raise ValidationError({"detail": "حدد الشركة والمورد لعرض كشف الحساب."})

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

        invoices = self.get_queryset().filter(
            company_id=company_id, supplier_id=supplier_id,
            status=PurchaseInvoice.Status.POSTED,
        ).select_related("supplier")
        supplier = invoices.first()
        if supplier is None:
            # Distinguish an accessible supplier with no posted invoices from an inaccessible one.
            from suppliers.models import Supplier
            supplier_obj = Supplier.objects.filter(pk=supplier_id, company_id=company_id).first()
            if not supplier_obj or company_id not in accessible_companies(request.user).values_list("id", flat=True):
                raise NotFound("المورد غير موجود أو لا تملك صلاحية الوصول إليه.")
            supplier_name = supplier_obj.name_ar
        else:
            supplier_name = supplier.supplier.name_ar

        events = []
        for invoice in invoices:
            events.append({
                "date": invoice.invoice_date, "kind": "invoice",
                "number": invoice.invoice_number, "description": f"فاتورة مورد {invoice.invoice_number}",
                "debit": Decimal("0"), "credit": invoice.grand_total,
                "sort": 1,
            })
        payments = SupplierPayment.objects.filter(
            invoice__in=invoices, status=SupplierPayment.Status.POSTED
        ).select_related("invoice")
        for payment in payments:
            events.append({
                "date": payment.payment_date, "kind": "payment",
                "number": payment.payment_number, "description": f"سداد فاتورة {payment.invoice.invoice_number}",
                "debit": payment.amount, "credit": Decimal("0"), "sort": 0,
            })
        events.sort(key=lambda row: (row["date"], row["sort"], row["number"]))

        opening = sum(
            (row["credit"] - row["debit"] for row in events if date_from and row["date"] < date_from),
            Decimal("0"),
        )
        balance = opening
        rows = []
        for row in events:
            if date_from and row["date"] < date_from:
                continue
            if date_to and row["date"] > date_to:
                continue
            balance += row["credit"] - row["debit"]
            rows.append({
                "date": row["date"].isoformat(), "document_type": row["kind"],
                "document_number": row["number"], "description": row["description"],
                "debit": str(row["debit"]), "credit": str(row["credit"]),
                "balance": str(balance),
            })
        return Response({
            "company": company_id, "supplier": supplier_id, "supplier_name": supplier_name,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "opening_balance": str(opening), "closing_balance": str(balance), "rows": rows,
        })

    @action(detail=False, methods=("get",), url_path="supplier-aging")
    def supplier_aging(self, request):
        """تصنيف مديونيات الموردين حسب تاريخ الاستحقاق."""
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة لعرض أعمار المديونية."})
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        branch_raw = request.query_params.get("branch")
        branch_id = None
        if branch_raw:
            try:
                branch_id = int(branch_raw)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."})
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
        as_of_raw = request.query_params.get("as_of")
        as_of = parse_date(as_of_raw) if as_of_raw else None
        if as_of_raw and not as_of:
            raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if not as_of:
            from django.utils import timezone
            as_of = timezone.localdate()

        invoices = self.get_queryset().filter(
            company_id=company_id, status=PurchaseInvoice.Status.POSTED, invoice_date__lte=as_of
        ).select_related("supplier", "receipt__warehouse").prefetch_related("payments")
        if branch_id:
            invoices = invoices.filter(receipt__warehouse__branch_id=branch_id)
        suppliers = {}
        for invoice in invoices:
            paid = sum(
                (payment.amount for payment in invoice.payments.all()
                 if payment.status == SupplierPayment.Status.POSTED and payment.payment_date <= as_of),
                Decimal("0"),
            )
            balance_due = invoice.grand_total - paid
            if balance_due <= 0:
                continue
            due_date = invoice.invoice_date + timedelta(days=invoice.supplier.payment_terms_days)
            overdue_days = max((as_of - due_date).days, 0)
            bucket = (
                "not_due" if overdue_days == 0 else
                "1_30" if overdue_days <= 30 else
                "31_60" if overdue_days <= 60 else
                "61_90" if overdue_days <= 90 else "over_90"
            )
            supplier = suppliers.setdefault(invoice.supplier_id, {
                "supplier": invoice.supplier_id, "supplier_code": invoice.supplier.code,
                "supplier_name": invoice.supplier.name_ar, "not_due": Decimal("0"),
                "1_30": Decimal("0"), "31_60": Decimal("0"), "61_90": Decimal("0"),
                "over_90": Decimal("0"), "total_due": Decimal("0"), "invoices": [],
            })
            supplier[bucket] += balance_due
            supplier["total_due"] += balance_due
            supplier["invoices"].append({
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date.isoformat(),
                "due_date": due_date.isoformat(),
                "overdue_days": overdue_days,
                "bucket": bucket, "balance_due": str(balance_due),
            })
        rows = []
        totals = {key: Decimal("0") for key in ("not_due", "1_30", "31_60", "61_90", "over_90", "total_due")}
        for supplier in sorted(suppliers.values(), key=lambda item: item["supplier_name"]):
            for key in totals:
                totals[key] += supplier[key]
                supplier[key] = str(supplier[key])
            rows.append(supplier)
        return Response({
            "company": company_id, "as_of": as_of.isoformat(),
            "totals": {key: str(value) for key, value in totals.items()}, "suppliers": rows,
        })

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """ترحيل المستند أو تنفيذ إجراء الترحيل المرتبط به."""
        invoice = self.get_object()
        self._check_write(invoice.company_id)
        if invoice.receipt.warehouse.branch_id and not can_access_branch(request.user, invoice.receipt.warehouse.branch_id, invoice.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        invoice = post_purchase_invoice(invoice.pk, request.user)
        return Response(self.get_serializer(invoice).data, status=status.HTTP_200_OK)



class SupplierPaymentViewSet(PurchaseBaseViewSet):
    serializer_class = SupplierPaymentSerializer
    queryset = SupplierPayment.objects.select_related(
        "company", "supplier", "invoice", "journal_entry"
    ).all()
    filterset_fields = ("company", "supplier", "invoice", "status", "payment_date", "method")
    search_fields = ("payment_number", "reference", "supplier__name_ar", "invoice__invoice_number")
    ordering_fields = ("payment_date", "payment_number", "amount", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        branches = accessible_branches(self.request.user)
        return qs.filter(
            Q(invoice__receipt__warehouse__branch__isnull=True)
            | Q(invoice__receipt__warehouse__branch__in=branches)
        )

    def create(self, request, *args, **kwargs):
        """إنشاء مستند جديد والتحقق من مدخلاته وصلاحيات الشركة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        invoice = data["invoice"]
        if invoice.receipt.warehouse.branch_id and not can_access_branch(
            self.request.user, invoice.receipt.warehouse.branch_id, data["company"].pk
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل سند الصرف غير متاح؛ أنشئ سندًا جديدًا إذا كان مسودة.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """ترحيل المستند أو تنفيذ إجراء الترحيل المرتبط به."""
        payment = self.get_object()
        self._check_write(payment.company_id)
        if payment.invoice.receipt.warehouse.branch_id and not can_access_branch(
            request.user, payment.invoice.receipt.warehouse.branch_id, payment.company_id
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        payment = post_supplier_payment(payment.pk, request.user)
        return Response(self.get_serializer(payment).data, status=status.HTTP_200_OK)
