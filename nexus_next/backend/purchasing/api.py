from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from accounting.models import AccountRole, FxSubledgerRevaluation, JournalEntry, JournalLine
from accounting.services import reverse_operational_journal
from accounting.fx_subledger_services import revalue_fx_invoice
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from inventory.models import StockMovement

from companies.models import Branch


def _purchase_warehouse_accessible(user, warehouse):
    """Apply the default-branch rule to shared warehouses."""
    if user.is_superuser and not is_platform_owner(user):
        return True
    branch = warehouse.branch
    if branch is None:
        branch = Branch.objects.filter(
            company_id=warehouse.company_id, is_active=True
        ).order_by("-is_head_office", "code").first()
    return bool(branch and can_access_branch(user, branch.pk, warehouse.company_id))


def _default_branch_company_ids(user, companies):
    """Return company IDs where the user's default branch is accessible."""
    if user.is_superuser and not is_platform_owner(user):
        return list(companies.values_list("id", flat=True))
    company_ids = []
    for company_id in companies.values_list("id", flat=True):
        branch = Branch.objects.filter(
            company_id=company_id, is_active=True
        ).order_by("-is_head_office", "code").first()
        if branch and can_access_branch(user, branch.pk, company_id):
            company_ids.append(company_id)
    return company_ids
from .models import PurchaseInvoice, PurchaseOrder, PurchaseOrderLine, PurchaseOrderReceiptLine, PurchaseReceipt, PurchaseRequisition, PurchaseRFQ, SupplierQuotation, SupplierPayment, SupplierRefund, SupplierReturn
from .serializers import PurchaseInvoiceSerializer, PurchaseOrderSerializer, PurchaseReceiptSerializer, PurchaseRequisitionSerializer, PurchaseRFQSerializer, SupplierQuotationSerializer, SupplierPaymentSerializer, SupplierRefundSerializer, SupplierReturnSerializer
from .services import post_purchase_invoice, post_purchase_receipt, post_supplier_payment, post_supplier_refund, post_supplier_return, reverse_supplier_refund, reverse_supplier_return


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
        raise PermissionDenied("لا يمكن حذف مستندات المشتريات؛ استخدم إجراء مرتجع المورد أو عكس سند الصرف/الاسترداد المعتمد حسب نوع المستند.")


class PurchaseOrderViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseOrderSerializer
    queryset = PurchaseOrder.objects.select_related("company", "supplier", "warehouse", "warehouse__branch").prefetch_related("lines__product", "approval_requests")
    filterset_fields = ("company", "supplier", "warehouse", "status", "order_date")
    search_fields = ("order_number", "supplier__name_ar")
    ordering_fields = ("order_date", "order_number", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        companies = accessible_companies(self.request.user)
        qs = self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        defaults = _default_branch_company_ids(self.request.user, companies)
        return qs.filter(Q(warehouse__branch__in=branches) | Q(warehouse__branch__isnull=True, company_id__in=defaults))

    def create(self, request, *args, **kwargs):
        """إنشاء مستند جديد والتحقق من مدخلاته وصلاحيات الشركة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        if not _purchase_warehouse_accessible(self.request.user, data["warehouse"]):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        order = serializer.instance
        self._check_write(order.company_id)
        warehouse = serializer.validated_data.get("warehouse", order.warehouse)
        if not _purchase_warehouse_accessible(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        """حذف أمر شراء مسودة فقط قبل أي استلام أو أثر مخزني."""
        order = PurchaseOrder.objects.select_for_update().get(pk=instance.pk)
        self._check_write(order.company_id)
        if not _purchase_warehouse_accessible(self.request.user, order.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        if order.status != PurchaseOrder.Status.DRAFT:
            raise ValidationError("لا يمكن حذف أمر شراء معتمد أو ملغي؛ استخدم إجراء الإلغاء أو التسوية المناسب.")
        if (
            order.receipts.exists()
            or order.lines.filter(received_quantity__gt=0).exists()
            or order.lines.filter(receipt_links__isnull=False).exists()
        ):
            raise ValidationError("لا يمكن حذف أمر شراء له إذن استلام أو كمية مستلمة أو روابط استلام.")
        # PurchaseOrderLine uses PROTECT to preserve receipt history. Once the
        # guards above prove that no receipt references exist, remove only the
        # draft's own editable lines before deleting its header.
        order.lines.all().delete()
        order.delete()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def confirm(self, request, pk=None):
        """اعتماد المستند ونقله إلى الحالة التالية وفق دورة العمل."""
        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(order.company_id)
        if order.status != PurchaseOrder.Status.DRAFT:
            raise ValidationError("أمر الشراء ليس مسودة.")
        if not order.lines.exists():
            raise ValidationError("أمر الشراء لا يحتوي على أصناف.")
        from approvals.models import FinancialApprovalRequest
        from approvals.services import refresh_financial_approval
        approval_request = refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.PURCHASE_ORDER, order, request.user
        )
        if approval_request:
            return Response({
                "status": "pending_approval",
                "detail": "قيمة أمر الشراء تجاوزت الحد المعتمد؛ أُرسل إلى مدير الشركة للموافقة قبل التأكيد.",
                "approval_request_id": approval_request.pk,
                "order": self.get_serializer(order).data,
            }, status=status.HTTP_202_ACCEPTED)
        from django.utils import timezone
        PurchaseOrder.objects.filter(pk=order.pk, status=PurchaseOrder.Status.DRAFT).update(
            status=PurchaseOrder.Status.CONFIRMED, confirmed_at=timezone.now()
        )
        order.refresh_from_db()
        return Response(self.get_serializer(order).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def cancel(self, request, pk=None):
        """إلغاء أمر شراء قبل أي استلام فعلي مع الحفاظ على المستند وسجلّه."""
        order = PurchaseOrder.objects.select_for_update().select_related("company").get(pk=self.get_object().pk)
        self._check_write(order.company_id)
        if order.status not in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.CONFIRMED):
            raise ValidationError("لا يمكن إلغاء أمر شراء بدأ استلامه أو اكتمل استلامه.")
        if order.receipts.exists() or order.lines.filter(received_quantity__gt=0).exists():
            raise ValidationError("لا يمكن إلغاء أمر شراء له إذن استلام أو كمية مستلمة؛ استخدم مستند تسوية مستقلًا.")
        updated = PurchaseOrder.objects.filter(
            pk=order.pk, status__in=(PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.CONFIRMED)
        ).update(status=PurchaseOrder.Status.CANCELLED)
        if updated != 1:
            raise ValidationError("تغيرت حالة أمر الشراء؛ حدّث الشاشة ثم أعد المحاولة.")
        from approvals.models import FinancialApprovalRequest
        pending_approvals = FinancialApprovalRequest.objects.select_for_update().filter(
            document_type=FinancialApprovalRequest.DocumentType.PURCHASE_ORDER,
            purchase_order=order, status=FinancialApprovalRequest.Status.PENDING,
        )
        for approval in pending_approvals:
            approval.status = FinancialApprovalRequest.Status.SUPERSEDED
            approval.save(update_fields=("status", "decided_at"))
        order.refresh_from_db()
        return Response(self.get_serializer(order).data, status=status.HTTP_200_OK)


class PurchaseReceiptViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseReceiptSerializer
    queryset = PurchaseReceipt.objects.select_related("company", "supplier", "warehouse", "stock_movement", "journal_entry").prefetch_related("stock_movement__lines__product")
    filterset_fields = ("company", "supplier", "warehouse", "receipt_date")
    search_fields = ("receipt_number", "supplier_delivery_ref", "supplier__name_ar")
    ordering_fields = ("receipt_date", "receipt_number", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        companies = accessible_companies(self.request.user)
        qs = self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        defaults = _default_branch_company_ids(self.request.user, companies)
        return qs.filter(Q(warehouse__branch__in=branches) | Q(warehouse__branch__isnull=True, company_id__in=defaults))

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """قفل أمر الشراء وبنوده قبل فحص حجوزات أذون الاستلام المسودة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        purchase_order_id = request.data.get("purchase_order")
        if purchase_order_id not in (None, ""):
            try:
                purchase_order_id = int(purchase_order_id)
            except (TypeError, ValueError):
                raise ValidationError({"purchase_order": "أمر الشراء المحدد غير صالح."}) from None
            order = PurchaseOrder.objects.select_for_update().select_related(
                "warehouse", "warehouse__branch",
            ).filter(pk=purchase_order_id, company_id=company_id).first()
            if not order:
                raise ValidationError({"purchase_order": "أمر الشراء غير موجود داخل الشركة المحددة."})
            if not _purchase_warehouse_accessible(request.user, order.warehouse):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع أمر الشراء.")
            # Lock all order lines in a stable order so concurrent draft receipts
            # cannot reserve the same remaining quantity.
            list(PurchaseOrderLine.objects.select_for_update().filter(
                order=order,
            ).order_by("pk"))
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        warehouse = data["warehouse"]
        if not _purchase_warehouse_accessible(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        receipt = serializer.instance
        self._check_write(receipt.company_id)
        if receipt.stock_movement.status != StockMovement.Status.DRAFT:
            raise ValidationError("إذن الاستلام المرحّل غير قابل للتعديل؛ أنشئ مستند تسوية مستقلًا بدل تغيير حركة المخزون.")
        raise ValidationError("تعديل إذن الاستلام غير متاح حاليًا؛ أنشئ إذن استلام مسودة جديدًا.")

    @transaction.atomic
    def perform_destroy(self, instance):
        """حذف إذن استلام مسودة غير مفوتر فقط مع تحرير ربط أمر الشراء بأمان."""
        receipt = PurchaseReceipt.objects.select_for_update().select_related(
            "company", "warehouse", "stock_movement"
        ).get(pk=instance.pk)
        self._check_write(receipt.company_id)
        if not _purchase_warehouse_accessible(self.request.user, receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        movement = StockMovement.objects.select_for_update().get(pk=receipt.stock_movement_id)
        if movement.status != StockMovement.Status.DRAFT:
            raise ValidationError("لا يمكن حذف إذن استلام مرحّل؛ استخدم إجراء عكس أو تسوية مخزون معتمد.")
        if PurchaseInvoice.objects.filter(receipt=receipt).exists():
            raise ValidationError("لا يمكن حذف إذن استلام مرتبط بفاتورة مورد؛ احذف مسودة الفاتورة أولًا أو استخدم الإجراء المحاسبي المعتمد.")
        # Draft PO links reserve received quantities; remove them before deleting
        # movement lines so the purchase order's remaining quantity is restored.
        PurchaseOrderReceiptLine.objects.filter(receipt_line__movement=movement).delete()
        receipt.delete()
        movement.delete()


    @action(detail=False, methods=("get",), url_path="grni-aging")
    def grni_aging(self, request):
        """تقرير أعمار البضاعة المستلمة غير المفوترة مع ملخص شهري للمطابقة."""
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة لعرض تقرير GRNI."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        as_of = parse_date(request.query_params.get("as_of", "")) if request.query_params.get("as_of") else timezone.localdate()
        if not as_of:
            raise ValidationError({"as_of": "تاريخ التقرير غير صالح."})
        branch_id = request.query_params.get("branch")
        if branch_id:
            try:
                branch_id = int(branch_id)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."}) from None
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
        companies = accessible_companies(request.user).filter(pk=company_id)
        qs = self.queryset.filter(
            company__in=companies, receipt_date__lte=as_of,
            stock_movement__status=StockMovement.Status.POSTED,
        ).select_related("invoice")
        if branch_id:
            qs = qs.filter(warehouse__branch_id=branch_id)
        else:
            branches = accessible_branches(request.user)
            defaults = _default_branch_company_ids(request.user, companies)
            qs = qs.filter(Q(warehouse__branch__in=branches) | Q(warehouse__branch__isnull=True, company_id__in=defaults))
        # Keep receipts with no invoice or a draft invoice in GRNI; only a posted
        # supplier invoice clears the receipt from this operational aging view.
        qs = qs.filter(
            Q(invoice__isnull=True) | ~Q(invoice__status=PurchaseInvoice.Status.POSTED)
        ).order_by("receipt_date", "receipt_number")
        rows = []
        aging = {"0_30": Decimal("0"), "31_60": Decimal("0"), "61_90": Decimal("0"), "over_90": Decimal("0")}
        monthly = {}
        grand_total = Decimal("0")
        for receipt in qs:
            amount = sum((line.cost_amount for line in receipt.stock_movement.lines.all()), Decimal("0")).quantize(Decimal("0.0001"))
            if amount <= 0:
                continue
            age_days = max((as_of - receipt.receipt_date).days, 0)
            bucket = "0_30" if age_days <= 30 else "31_60" if age_days <= 60 else "61_90" if age_days <= 90 else "over_90"
            aging[bucket] += amount
            grand_total += amount
            month = receipt.receipt_date.strftime("%Y-%m")
            monthly[month] = monthly.get(month, Decimal("0")) + amount
            invoice = getattr(receipt, "invoice", None)
            rows.append({
                "receipt_id": receipt.pk, "receipt_number": receipt.receipt_number,
                "receipt_date": receipt.receipt_date.isoformat(), "age_days": age_days,
                "aging_bucket": bucket, "supplier_id": receipt.supplier_id,
                "supplier_name": receipt.supplier.name_ar, "warehouse_id": receipt.warehouse_id,
                "warehouse_name": receipt.warehouse.name_ar,
                "branch_id": receipt.warehouse.branch_id,
                "amount": str(amount), "invoice_status": invoice.status if invoice else "not_invoiced",
                "invoice_number": invoice.invoice_number if invoice else None,
            })
        # Reconcile the operational receipt subledger with the posted GRNI
        # control-account balance as of the same date and branch scope.
        role = AccountRole.objects.filter(
            company_id=company_id,
            role=AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED,
        ).select_related("account").first()
        gl_balance = None
        reconciliation_difference = None
        reconciled = None
        if role:
            ledger_lines = JournalLine.objects.filter(
                account_id=role.account_id,
                entry__company_id=company_id,
                entry__status=JournalEntry.Status.POSTED,
                entry__entry_date__lte=as_of,
            )
            if branch_id:
                ledger_lines = ledger_lines.filter(entry__branch_id=branch_id)
            else:
                ledger_lines = ledger_lines.filter(
                    Q(entry__branch__in=accessible_branches(request.user))
                    | Q(entry__company_id__in=defaults, entry__branch__isnull=True)
                )
            ledger_totals = ledger_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            gl_balance = (
                (ledger_totals["credit"] or Decimal("0"))
                - (ledger_totals["debit"] or Decimal("0"))
            ).quantize(Decimal("0.0001"))
            reconciliation_difference = (
                grand_total.quantize(Decimal("0.0001")) - gl_balance
            ).quantize(Decimal("0.0001"))
            reconciled = abs(reconciliation_difference) <= Decimal("0.0001")

        return Response({
            "company": company_id, "as_of": as_of.isoformat(),
            "grand_total": str(grand_total.quantize(Decimal("0.0001"))),
            "aging": {key: str(value.quantize(Decimal("0.0001"))) for key, value in aging.items()},
            "monthly_summary": [{"month": key, "amount": str(value.quantize(Decimal("0.0001")))} for key, value in sorted(monthly.items())],
            "gl_reconciliation": {
                "account_id": role.account_id if role else None,
                "account_code": role.account.code if role else None,
                "account_name": role.account.name_ar if role else None,
                "operational_open_receipts": str(grand_total.quantize(Decimal("0.0001"))),
                "posted_gl_balance": str(gl_balance) if gl_balance is not None else None,
                "difference": str(reconciliation_difference) if reconciliation_difference is not None else None,
                "reconciled": reconciled,
            },
            "rows": rows,
        })

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """ترحيل المستند أو تنفيذ إجراء الترحيل المرتبط به."""
        receipt = self.get_object()
        self._check_write(receipt.company_id)
        if not _purchase_warehouse_accessible(request.user, receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        receipt = post_purchase_receipt(receipt.pk, request.user)
        return Response(self.get_serializer(receipt).data, status=status.HTTP_200_OK)


class PurchaseInvoiceViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseInvoiceSerializer
    queryset = PurchaseInvoice.objects.select_related("company", "supplier", "receipt", "receipt__warehouse", "journal_entry").prefetch_related("lines__receipt_line__product", "payments", "tax_allocations__branch", "tax_allocations__journal_entry")
    filterset_fields = ("company", "supplier", "status", "invoice_date")
    search_fields = ("invoice_number", "supplier__name_ar", "receipt__receipt_number")
    ordering_fields = ("invoice_date", "invoice_number", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        companies = accessible_companies(self.request.user)
        qs = self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        defaults = _default_branch_company_ids(self.request.user, companies)
        return qs.filter(Q(receipt__warehouse__branch__in=branches) | Q(receipt__warehouse__branch__isnull=True, company_id__in=defaults))

    def create(self, request, *args, **kwargs):
        """إنشاء مستند جديد والتحقق من مدخلاته وصلاحيات الشركة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        receipt = data["receipt"]
        if not _purchase_warehouse_accessible(self.request.user, receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        invoice = serializer.instance
        self._check_write(invoice.company_id)
        if invoice.status != PurchaseInvoice.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل فاتورة مورد مرحلة.")
        receipt = invoice.receipt
        if not _purchase_warehouse_accessible(self.request.user, receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        """حذف فاتورة مسودة فقط إذا لم تنشئ اعتمادًا أو دفعة أو مردودًا أو قيدًا."""
        invoice = PurchaseInvoice.objects.select_for_update().select_related(
            "company", "receipt__warehouse"
        ).get(pk=instance.pk)
        self._check_write(invoice.company_id)
        if not _purchase_warehouse_accessible(self.request.user, invoice.receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        if invoice.status != PurchaseInvoice.Status.DRAFT or invoice.journal_entry_id:
            raise ValidationError("لا يمكن حذف فاتورة مورد مرحلة أو مرتبطة بقيد.")
        if (
            invoice.payments.exists()
            or invoice.returns.exists()
            or invoice.approval_requests.exists()
            or invoice.fx_revaluations.exists()
            or invoice.tax_allocations.exists()
        ):
            raise ValidationError("لا يمكن حذف فاتورة لها دفعة أو مردود أو طلب اعتماد أو إعادة تقييم أو توزيع ضريبي.")
        for line in invoice.lines.select_for_update():
            line.delete()
        invoice.delete()

    @action(detail=True, methods=("post",), url_path="revalue")
    def revalue(self, request, pk=None):
        invoice = self.get_object()
        self._check_write(invoice.company_id)
        if not _purchase_warehouse_accessible(request.user, invoice.receipt.warehouse):
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
            direction="payable", invoice_id=invoice.pk,
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
        rows = FxSubledgerRevaluation.objects.filter(purchase_invoice=invoice).select_related("journal_entry")
        return Response([{
            "id": row.pk, "revaluation_date": row.revaluation_date, "rate_date": row.rate_date,
            "rate": str(row.rate), "foreign_balance": str(row.foreign_balance),
            "previous_carrying_amount": str(row.previous_carrying_amount),
            "new_carrying_amount": str(row.new_carrying_amount), "difference": str(row.difference),
            "journal_entry": row.journal_entry_id,
            "journal_number": row.journal_entry.entry_number if row.journal_entry_id else None,
            "status": "reversed" if row.journal_entry_id and row.journal_entry.status == "reversed" else ("posted" if row.journal_entry_id else "no_difference"),
        } for row in rows])

    @action(detail=False, methods=("get",), url_path="supplier-statement")
    def supplier_statement(self, request):
        """إعداد كشف حركة المورد ورصيده للفترة المحددة."""
        try:
            company_id = int(request.query_params.get("company", ""))
            supplier_id = int(request.query_params.get("supplier", ""))
        except (TypeError, ValueError):
            raise ValidationError({"detail": "حدد الشركة والمورد لعرض كشف الحساب."}) from None

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

        # Include direct fixed-asset purchases in the same supplier subledger.
        from assets.models import AssetPurchaseInvoice, AssetSupplierPayment
        asset_invoices = AssetPurchaseInvoice.objects.filter(
            company_id=company_id, supplier_id=supplier_id,
            status=AssetPurchaseInvoice.Status.POSTED,
        ).select_related("asset")
        if not request.user.is_superuser or is_platform_owner(request.user):
            asset_invoices = asset_invoices.filter(branch__in=accessible_branches(request.user))
        for invoice in asset_invoices:
            events.append({
                "date": invoice.invoice_date, "kind": "asset_invoice",
                "number": invoice.invoice_number,
                "description": f"فاتورة شراء أصل {invoice.invoice_number} - {invoice.asset.name_ar}",
                "debit": Decimal("0"), "credit": invoice.grand_total, "sort": 1,
            })
        asset_payments = AssetSupplierPayment.objects.filter(
            company_id=company_id, supplier_id=supplier_id,
            status=AssetSupplierPayment.Status.POSTED,
        ).select_related("invoice")
        if not request.user.is_superuser or is_platform_owner(request.user):
            asset_payments = asset_payments.filter(branch__in=accessible_branches(request.user))
        for payment in asset_payments:
            events.append({
                "date": payment.payment_date, "kind": "asset_payment",
                "number": payment.payment_number,
                "description": f"سداد فاتورة أصل {payment.invoice.invoice_number}",
                "debit": payment.amount, "credit": Decimal("0"), "sort": 0,
            })
        payments = SupplierPayment.objects.filter(
            invoice__in=invoices, status=SupplierPayment.Status.POSTED
        ).select_related("invoice")
        for payment in payments:
            events.append({
                "date": payment.payment_date, "kind": "payment",
                "number": payment.payment_number, "description": f"سداد فاتورة {payment.invoice.invoice_number}",
                "debit": payment.carrying_base_amount if payment.carrying_base_amount is not None else payment.amount, "credit": Decimal("0"), "sort": 0,
            })
        returns = SupplierReturn.objects.filter(
            invoice__in=invoices, status__in=(SupplierReturn.Status.POSTED, SupplierReturn.Status.REVERSED),
        ).select_related("invoice")
        for returned in returns:
            events.append({
                "date": returned.return_date, "kind": "supplier_return",
                "number": returned.return_number,
                "description": f"مرتجع مورد عن الفاتورة {returned.invoice.invoice_number}",
                "debit": returned.grand_total, "credit": Decimal("0"), "sort": 0,
            })
            if returned.status == SupplierReturn.Status.REVERSED and returned.reversal_date:
                events.append({
                    "date": returned.reversal_date, "kind": "supplier_return_reversal",
                    "number": f"REV-{returned.return_number}",
                    "description": f"عكس مرتجع المورد {returned.return_number}",
                    "debit": Decimal("0"), "credit": returned.grand_total, "sort": 1,
                })
        refunds = SupplierRefund.objects.filter(
            company_id=company_id, supplier_id=supplier_id, status=SupplierRefund.Status.POSTED,
        ).select_related("return_document")
        for refund in refunds:
            events.append({
                "date": refund.refund_date, "kind": "supplier_refund",
                "number": refund.refund_number,
                "description": f"استرداد نقدي من المورد عن المرتجع {refund.return_document.return_number}",
                "debit": Decimal("0"), "credit": refund.amount, "sort": 1,
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
            "opening_balance": str(opening), "closing_balance": str(balance),
            "supplier_credit_balance": str(max(Decimal("0"), -balance)), "rows": rows,
        })

    @action(detail=False, methods=("get",), url_path="supplier-aging")
    def supplier_aging(self, request):
        """تصنيف مديونيات الموردين حسب تاريخ الاستحقاق."""
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة لعرض أعمار المديونية."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        branch_raw = request.query_params.get("branch")
        branch_id = None
        if branch_raw:
            try:
                branch_id = int(branch_raw)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."}) from None
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
        ).select_related("supplier", "receipt__warehouse").prefetch_related("payments", "returns")
        if branch_id:
            invoices = invoices.filter(receipt__warehouse__branch_id=branch_id)
        suppliers = {}
        for invoice in invoices:
            paid = sum(
                ((payment.carrying_base_amount if payment.carrying_base_amount is not None else payment.amount) for payment in invoice.payments.all()
                 if payment.status == SupplierPayment.Status.POSTED and payment.payment_date <= as_of),
                Decimal("0"),
            )
            returned = sum((
                item.grand_total for item in invoice.returns.all()
                if item.return_date <= as_of and (
                    item.status == SupplierReturn.Status.POSTED
                    or (
                        item.status == SupplierReturn.Status.REVERSED
                        and (item.reversal_date is None or item.reversal_date > as_of)
                    )
                )
            ), Decimal("0"))
            balance_due = invoice.grand_total - paid - returned
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
        # Direct fixed-asset vendor invoices participate in the same aging analysis.
        from assets.models import AssetPurchaseInvoice, AssetSupplierPayment
        asset_invoices = AssetPurchaseInvoice.objects.filter(
            company_id=company_id, status=AssetPurchaseInvoice.Status.POSTED,
            invoice_date__lte=as_of,
        ).select_related("supplier", "asset").prefetch_related("payments")
        if branch_id:
            asset_invoices = asset_invoices.filter(branch_id=branch_id)
        elif not request.user.is_superuser or is_platform_owner(request.user):
            asset_invoices = asset_invoices.filter(branch__in=accessible_branches(request.user))
        for invoice in asset_invoices:
            paid = sum((
                payment.amount for payment in invoice.payments.all()
                if payment.status == AssetSupplierPayment.Status.POSTED and payment.payment_date <= as_of
            ), Decimal("0"))
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
                "due_date": due_date.isoformat(), "overdue_days": overdue_days,
                "bucket": bucket, "balance_due": str(balance_due),
                "document_type": "asset_purchase_invoice", "asset_name": invoice.asset.name_ar,
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
        if not _purchase_warehouse_accessible(request.user, invoice.receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        invoice = post_purchase_invoice(invoice.pk, request.user)
        return Response(self.get_serializer(invoice).data, status=status.HTTP_200_OK)





class SupplierReturnViewSet(PurchaseBaseViewSet):
    serializer_class = SupplierReturnSerializer
    queryset = SupplierReturn.objects.select_related(
        "company", "supplier", "invoice", "invoice__receipt__warehouse",
        "stock_movement", "journal_entry", "reversal_stock_movement", "reversal_journal_entry",
    ).prefetch_related("lines__invoice_line__receipt_line__product", "lines__movement_line")
    filterset_fields = ("company", "supplier", "invoice", "status", "return_date")
    search_fields = ("return_number", "supplier__name_ar", "invoice__invoice_number")
    ordering_fields = ("return_date", "return_number", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        qs = self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        defaults = _default_branch_company_ids(self.request.user, companies)
        return qs.filter(
            Q(invoice__receipt__warehouse__branch__in=branches)
            | Q(invoice__receipt__warehouse__branch__isnull=True, company_id__in=defaults)
        )

    def create(self, request, *args, **kwargs):
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        invoice = data["invoice"]
        if not _purchase_warehouse_accessible(self.request.user, invoice.receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع فاتورة المورد.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل مرتجع المورد غير متاح؛ احذف المسودة وأعد إنشاءها.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        document = self.get_object()
        self._check_write(document.company_id)
        if not _purchase_warehouse_accessible(request.user, document.invoice.receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع فاتورة المورد.")
        document = post_supplier_return(document.pk, request.user)
        return Response(self.get_serializer(document).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        document = self.get_object()
        self._check_write(document.company_id)
        if not _purchase_warehouse_accessible(request.user, document.invoice.receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع فاتورة المورد.")
        entry_date_raw = request.data.get("entry_date")
        entry_date = parse_date(str(entry_date_raw)) if entry_date_raw else timezone.localdate()
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        document = reverse_supplier_return(document.pk, request.user, entry_date)
        return Response(self.get_serializer(document).data, status=status.HTTP_200_OK)




class SupplierRefundViewSet(PurchaseBaseViewSet):
    serializer_class = SupplierRefundSerializer
    queryset = SupplierRefund.objects.select_related(
        "company", "supplier", "return_document", "return_document__invoice",
        "return_document__invoice__receipt__warehouse", "payment_account", "journal_entry",
    )
    filterset_fields = ("company", "supplier", "return_document", "status", "refund_date")
    search_fields = ("refund_number", "supplier__name_ar", "return_document__return_number")
    ordering_fields = ("refund_date", "refund_number", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        qs = self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        defaults = _default_branch_company_ids(self.request.user, companies)
        return qs.filter(
            Q(return_document__invoice__receipt__warehouse__branch__in=branches)
            | Q(return_document__invoice__receipt__warehouse__branch__isnull=True, company_id__in=defaults)
        )

    def create(self, request, *args, **kwargs):
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        if not _purchase_warehouse_accessible(
            self.request.user, data["return_document"].invoice.receipt.warehouse,
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مرتجع المورد.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل استرداد المورد غير متاح؛ احذف المسودة وأعد إنشاءها.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        document = self.get_object()
        self._check_write(document.company_id)
        if not _purchase_warehouse_accessible(
            request.user, document.return_document.invoice.receipt.warehouse,
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مرتجع المورد.")
        document = post_supplier_refund(document.pk, request.user)
        return Response(self.get_serializer(document).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    def reverse(self, request, pk=None):
        """عكس استرداد المورد وإعادة الرصيد الدائن إلى حساب المورد."""
        document = self.get_object()
        self._check_write(document.company_id)
        warehouse = document.return_document.invoice.receipt.warehouse
        if not _purchase_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مرتجع المورد.")
        entry_date = parse_date(str(request.data.get("entry_date") or timezone.localdate()))
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        document = reverse_supplier_refund(document.pk, request.user, entry_date)
        return Response(self.get_serializer(document).data, status=status.HTTP_200_OK)


class SupplierPaymentViewSet(PurchaseBaseViewSet):
    serializer_class = SupplierPaymentSerializer
    queryset = SupplierPayment.objects.select_related(
        "company", "supplier", "invoice", "journal_entry", "journal_entry__reversal_entry"
    ).all()
    filterset_fields = ("company", "supplier", "invoice", "status", "payment_date", "method")
    search_fields = ("payment_number", "reference", "supplier__name_ar", "invoice__invoice_number")
    ordering_fields = ("payment_date", "payment_number", "amount", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        companies = accessible_companies(self.request.user)
        qs = self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        defaults = _default_branch_company_ids(self.request.user, companies)
        return qs.filter(
            Q(invoice__receipt__warehouse__branch__in=branches)
            | Q(invoice__receipt__warehouse__branch__isnull=True, company_id__in=defaults)
        )

    def create(self, request, *args, **kwargs):
        """إنشاء مستند جديد والتحقق من مدخلاته وصلاحيات الشركة."""
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        invoice = data["invoice"]
        if not _purchase_warehouse_accessible(self.request.user, invoice.receipt.warehouse):
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
        if not _purchase_warehouse_accessible(request.user, payment.invoice.receipt.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        payment = post_supplier_payment(payment.pk, request.user)
        return Response(self.get_serializer(payment).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        """عكس آخر سند صرف مرحّل بقيد عكسي كامل مع حماية تسلسل التسويات."""
        payment = SupplierPayment.objects.select_for_update(of=("self",)).select_related(
            "invoice", "invoice__receipt__warehouse", "journal_entry"
        ).get(pk=self.get_object().pk)
        self._check_write(payment.company_id)
        invoice = PurchaseInvoice.objects.select_for_update().select_related("receipt__warehouse").get(pk=payment.invoice_id)
        warehouse = invoice.receipt.warehouse
        if not _purchase_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        if payment.status != SupplierPayment.Status.POSTED or not payment.journal_entry_id:
            raise ValidationError("لا يمكن عكس سند صرف غير مرحّل.")
        if payment.journal_entry.status != JournalEntry.Status.POSTED:
            raise ValidationError("القيد المرتبط بسند الصرف ليس مرحّلًا.")
        later_payments = SupplierPayment.objects.filter(
            invoice_id=payment.invoice_id, status=SupplierPayment.Status.POSTED
        ).filter(
            Q(payment_date__gt=payment.payment_date)
            | Q(payment_date=payment.payment_date, pk__gt=payment.pk)
        ).exists()
        if later_payments:
            raise ValidationError("يجب عكس أحدث سند صرف على الفاتورة أولًا.")
        if FxSubledgerRevaluation.objects.filter(purchase_invoice_id=payment.invoice_id).exists():
            raise ValidationError("لا يمكن عكس سند صرف لفاتورة لها سجل إعادة تقييم عملة؛ راجع تسلسل التسويات وإعادة التقييم أولًا.")
        entry_date_raw = request.data.get("entry_date")
        try:
            entry_date = parse_date(str(entry_date_raw)) if entry_date_raw else timezone.localdate()
        except (TypeError, ValueError):
            entry_date = None
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        try:
            reversal = reverse_operational_journal(
                payment.journal_entry_id, request.user, entry_date, JournalEntry.Source.PURCHASES
            )
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        SupplierPayment.objects.filter(pk=payment.pk, status=SupplierPayment.Status.POSTED).update(
            status=SupplierPayment.Status.REVERSED
        )
        payment.refresh_from_db()
        data = self.get_serializer(payment).data
        data["reversal_journal_entry"] = reversal.pk
        data["reversal_journal_number"] = reversal.entry_number
        return Response(data, status=status.HTTP_200_OK)



class PurchaseRFQViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseRFQSerializer
    queryset = PurchaseRFQ.objects.select_related("company", "source_requisition", "awarded_quotation__supplier", "awarded_by").prefetch_related("lines__product", "invitations__supplier", "quotations__lines")
    filterset_fields = ("company", "status", "issue_date", "closing_date")
    search_fields = ("rfq_number", "terms", "invitations__supplier__name_ar")
    ordering_fields = ("issue_date", "closing_date", "rfq_number", "created_at")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user)).distinct()

    def create(self, request, *args, **kwargs):
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        self._check_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        raise ValidationError("طلب عروض الأسعار بعد إنشائه لا يعدل؛ أنشئ طلبًا جديدًا إذا تغيرت المواصفات أو الكميات.")

    @transaction.atomic
    def perform_destroy(self, instance):
        rfq = PurchaseRFQ.objects.select_for_update().get(pk=instance.pk)
        self._check_write(rfq.company_id)
        if rfq.status != PurchaseRFQ.Status.DRAFT or rfq.quotations.exists():
            raise ValidationError("لا يمكن حذف طلب عروض مفتوح أو مرتبط بعروض موردين.")
        rfq.invitations.all().delete()
        rfq.lines.all().delete()
        rfq.delete()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def open(self, request, pk=None):
        rfq = PurchaseRFQ.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(rfq.company_id)
        if rfq.status != PurchaseRFQ.Status.DRAFT or not rfq.lines.exists() or not rfq.invitations.exists():
            raise ValidationError("لا يمكن فتح الطلب قبل إضافة الأصناف والموردين المدعوين.")
        if rfq.closing_date < timezone.localdate():
            raise ValidationError("موعد إغلاق طلب العروض انتهى.")
        PurchaseRFQ.objects.filter(pk=rfq.pk, status=PurchaseRFQ.Status.DRAFT).update(status=PurchaseRFQ.Status.OPEN)
        rfq.refresh_from_db()
        return Response(self.get_serializer(rfq).data)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def close(self, request, pk=None):
        rfq = PurchaseRFQ.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(rfq.company_id)
        if rfq.status != PurchaseRFQ.Status.OPEN:
            raise ValidationError("طلب العروض ليس مفتوحًا.")
        PurchaseRFQ.objects.filter(pk=rfq.pk, status=PurchaseRFQ.Status.OPEN).update(status=PurchaseRFQ.Status.CLOSED)
        rfq.refresh_from_db()
        return Response(self.get_serializer(rfq).data)


    @action(detail=True, methods=("post",), url_path="convert-award")
    @transaction.atomic
    def convert_award(self, request, pk=None):
        rfq = PurchaseRFQ.objects.select_for_update().select_related("awarded_quotation__supplier").get(pk=self.get_object().pk)
        self._check_write(rfq.company_id)
        if rfq.status != PurchaseRFQ.Status.AWARDED or not rfq.awarded_quotation_id:
            raise ValidationError("لا يمكن إصدار أمر شراء قبل ترسية عرض مورد.")
        if rfq.converted_order_id:
            raise ValidationError("تم تحويل العرض الفائز إلى أمر شراء بالفعل.")
        order_number = str(request.data.get("order_number", "")).strip()
        order_date = parse_date(str(request.data.get("order_date", "")))
        expected_date = parse_date(str(request.data.get("expected_date", ""))) if request.data.get("expected_date") else None
        try:
            warehouse_id = int(request.data.get("warehouse"))
        except (TypeError, ValueError):
            warehouse_id = None
        if not order_number or not order_date or not warehouse_id:
            raise ValidationError("رقم الأمر وتاريخه ومخزن الاستلام حقول مطلوبة.")
        if expected_date and expected_date < order_date:
            raise ValidationError({"expected_date": "تاريخ التوريد المتوقع لا يسبق تاريخ أمر الشراء."})
        from inventory.models import Warehouse
        warehouse = Warehouse.objects.filter(pk=warehouse_id, company_id=rfq.company_id, is_active=True).first()
        if not warehouse or not _purchase_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("المخزن غير متاح للمستخدم أو لا يتبع الشركة.")
        quotation = rfq.awarded_quotation
        if quotation.status != SupplierQuotation.Status.SUBMITTED or quotation.valid_until < timezone.localdate():
            raise ValidationError("العرض الفائز لم يعد صالحًا للتحويل إلى أمر شراء.")
        if PurchaseOrder.objects.filter(company_id=rfq.company_id, order_number=order_number).exists():
            raise ValidationError({"order_number": "رقم أمر الشراء مستخدم بالفعل داخل الشركة."})
        source_requisition = None
        if rfq.source_requisition_id:
            source_requisition = PurchaseRequisition.objects.select_for_update().get(
                pk=rfq.source_requisition_id, company_id=rfq.company_id,
            )
            if (
                source_requisition.status != PurchaseRequisition.Status.APPROVED
                or source_requisition.converted_order_id
            ):
                raise ValidationError(
                    "طلب الاحتياج المرتبط لم يعد معتمدًا أو تم تحويله إلى أمر شراء بالفعل."
                )

        order = PurchaseOrder.objects.create(
            company_id=rfq.company_id, supplier=quotation.supplier, warehouse=warehouse,
            order_number=order_number, order_date=order_date, expected_date=expected_date,
            notes=f"مصدر أمر الشراء: طلب عروض الأسعار {rfq.rfq_number}، العرض الفائز {quotation.quotation_number}. الأسعار تشمل الخصم الممنوح في العرض.",
            created_by=request.user,
        )
        quote_lines = quotation.lines.select_related("rfq_line__product").all()
        PurchaseOrderLine.objects.bulk_create([
            PurchaseOrderLine(
                order=order, product=line.rfq_line.product, quantity=line.rfq_line.quantity,
                unit_price=(line.unit_price * (Decimal("100") - line.discount_percent) / Decimal("100")).quantize(Decimal("0.0001")),
                tax_rate=line.tax_rate,
            ) for line in quote_lines
        ])
        if source_requisition:
            requisition_updated = PurchaseRequisition.objects.filter(
                pk=source_requisition.pk,
                status=PurchaseRequisition.Status.APPROVED,
                converted_order__isnull=True,
            ).update(status=PurchaseRequisition.Status.CONVERTED, converted_order=order)
            if requisition_updated != 1:
                raise ValidationError(
                    "تم تحويل طلب الاحتياج بالفعل؛ لم يتم إنشاء أمر شراء إضافي."
                )
        updated = PurchaseRFQ.objects.filter(pk=rfq.pk, status=PurchaseRFQ.Status.AWARDED, converted_order__isnull=True).update(converted_order=order)
        if updated != 1:
            raise ValidationError("تم تحويل العرض الفائز بالفعل؛ لم يتم إنشاء أمر شراء إضافي.")
        rfq.refresh_from_db()
        return Response({"rfq": self.get_serializer(rfq).data, "purchase_order_id": order.pk}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def award(self, request, pk=None):
        rfq = PurchaseRFQ.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(rfq.company_id)
        if not has_company_role(request.user, rfq.company_id, ("admin",)):
            raise PermissionDenied("ترسية عروض الموردين متاحة لمدير الشركة فقط.")
        if rfq.status not in (PurchaseRFQ.Status.OPEN, PurchaseRFQ.Status.CLOSED):
            raise ValidationError("لا يمكن ترسية طلب العروض في حالته الحالية.")
        quotation_id = request.data.get("quotation")
        reason = str(request.data.get("reason", "")).strip()
        if not reason:
            raise ValidationError({"reason": "سبب الترسية مطلوب لحفظ مبرر الاختيار."})
        quotation = SupplierQuotation.objects.filter(pk=quotation_id, rfq=rfq, status=SupplierQuotation.Status.SUBMITTED).first()
        if not quotation:
            raise ValidationError({"quotation": "اختر عرضًا صالحًا تابعًا لطلب العروض."})
        if quotation.submitted_by_id == request.user.pk:
            raise PermissionDenied("لا يجوز ترسية عرض قمت بتسجيله بنفسك.")
        if quotation.valid_until < timezone.localdate():
            raise ValidationError({"quotation": "صلاحية العرض المختار منتهية."})
        PurchaseRFQ.objects.filter(pk=rfq.pk, awarded_quotation__isnull=True).update(
            awarded_quotation=quotation, award_reason=reason, awarded_by=request.user,
            awarded_at=timezone.now(), status=PurchaseRFQ.Status.AWARDED,
        )
        rfq.refresh_from_db()
        if not rfq.awarded_quotation_id:
            raise ValidationError("تمت ترسية طلب العروض بالفعل.")
        return Response(self.get_serializer(rfq).data)


class SupplierQuotationViewSet(PurchaseBaseViewSet):
    serializer_class = SupplierQuotationSerializer
    queryset = SupplierQuotation.objects.select_related("rfq", "supplier").prefetch_related("lines__rfq_line__product")
    filterset_fields = ("rfq", "rfq__company", "supplier", "status")
    search_fields = ("quotation_number", "supplier__name_ar", "rfq__rfq_number")
    ordering_fields = ("quotation_date", "valid_until", "submitted_at")

    def get_queryset(self):
        return self.queryset.filter(rfq__company__in=accessible_companies(self.request.user))

    def create(self, request, *args, **kwargs):
        try:
            rfq = PurchaseRFQ.objects.get(pk=request.data.get("rfq"), company__in=accessible_companies(request.user))
        except (PurchaseRFQ.DoesNotExist, TypeError, ValueError):
            raise NotFound("طلب عروض الأسعار غير موجود.") from None
        self._check_write(rfq.company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        self._check_write(serializer.validated_data["rfq"].company_id)
        serializer.save()

    def perform_update(self, serializer):
        raise ValidationError("عرض المورد المقدم لا يعدل؛ اطلب سحبه وإصدار عرض جديد.")

    def perform_destroy(self, instance):
        raise ValidationError("لا يمكن حذف عرض مورد مقدم حفاظًا على سجل المنافسة.")


class PurchaseRequisitionViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseRequisitionSerializer
    queryset = PurchaseRequisition.objects.select_related("company", "requested_by", "reviewed_by", "converted_order").prefetch_related("lines__product")
    filterset_fields = ("company", "status", "request_date", "department")
    search_fields = ("request_number", "department", "purpose")
    ordering_fields = ("request_date", "request_number", "created_at")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def create(self, request, *args, **kwargs):
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        self._check_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        requisition = PurchaseRequisition.objects.select_for_update().get(pk=instance.pk)
        self._check_write(requisition.company_id)
        if requisition.status != PurchaseRequisition.Status.DRAFT:
            raise ValidationError("لا يمكن حذف طلب شراء تم إرساله أو اعتماده.")
        requisition.lines.all().delete()
        requisition.delete()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def submit(self, request, pk=None):
        requisition = PurchaseRequisition.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(requisition.company_id)
        if requisition.status != PurchaseRequisition.Status.DRAFT:
            raise ValidationError("لا يمكن إرسال طلب إلا إذا كان مسودة.")
        if not requisition.lines.exists():
            raise ValidationError("أضف بندًا واحدًا على الأقل قبل الإرسال.")
        updated = PurchaseRequisition.objects.filter(pk=requisition.pk, status=PurchaseRequisition.Status.DRAFT).update(status=PurchaseRequisition.Status.SUBMITTED)
        if updated != 1:
            raise ValidationError("تغيرت حالة الطلب؛ حدّث الشاشة ثم أعد المحاولة.")
        requisition.refresh_from_db()
        return Response(self.get_serializer(requisition).data)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def review(self, request, pk=None):
        requisition = PurchaseRequisition.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(requisition.company_id)
        if requisition.status != PurchaseRequisition.Status.SUBMITTED:
            raise ValidationError("لا يمكن مراجعة طلب غير مرسل للاعتماد.")
        if requisition.requested_by_id == request.user.pk:
            raise PermissionDenied("لا يجوز اعتماد أو رفض طلب الشراء بواسطة مقدمه نفسه.")
        decision = request.data.get("decision")
        if decision not in ("approve", "reject"):
            raise ValidationError({"decision": "حدد approve أو reject."})
        note = str(request.data.get("note", "")).strip()
        if decision == "reject" and not note:
            raise ValidationError({"note": "سبب الرفض مطلوب."})
        requisition.status = PurchaseRequisition.Status.APPROVED if decision == "approve" else PurchaseRequisition.Status.REJECTED
        requisition.reviewed_by = request.user
        requisition.reviewed_at = timezone.now()
        requisition.review_note = note
        PurchaseRequisition.objects.filter(pk=requisition.pk, status=PurchaseRequisition.Status.SUBMITTED).update(
            status=requisition.status, reviewed_by=request.user, reviewed_at=requisition.reviewed_at, review_note=note
        )
        requisition.refresh_from_db()
        return Response(self.get_serializer(requisition).data)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def convert(self, request, pk=None):
        requisition = PurchaseRequisition.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(requisition.company_id)
        if requisition.status != PurchaseRequisition.Status.APPROVED or requisition.converted_order_id:
            raise ValidationError("يجب أن يكون طلب الشراء معتمدًا وغير محول من قبل.")
        supplier_id = request.data.get("supplier")
        warehouse_id = request.data.get("warehouse")
        order_number = str(request.data.get("order_number", "")).strip()
        order_date = parse_date(str(request.data.get("order_date", "")))
        expected_date = parse_date(str(request.data.get("expected_date", ""))) if request.data.get("expected_date") else None
        if not supplier_id or not warehouse_id or not order_number or not order_date:
            raise ValidationError("المورد والمخزن ورقم الأمر وتاريخه حقول مطلوبة للتحويل.")
        if expected_date and expected_date < order_date:
            raise ValidationError({"expected_date": "تاريخ التوريد المتوقع لا يمكن أن يسبق تاريخ أمر الشراء."})
        from suppliers.models import Supplier
        from inventory.models import Warehouse
        supplier = Supplier.objects.filter(pk=supplier_id, company_id=requisition.company_id, is_active=True).first()
        warehouse = Warehouse.objects.filter(pk=warehouse_id, company_id=requisition.company_id, is_active=True).first()
        if not supplier:
            raise ValidationError({"supplier": "اختر موردًا نشطًا تابعًا للشركة."})
        if not warehouse or not _purchase_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("المخزن غير متاح للمستخدم أو لا يتبع الشركة.")
        order = PurchaseOrder.objects.create(
            company_id=requisition.company_id, supplier=supplier, warehouse=warehouse,
            order_number=order_number, order_date=order_date, expected_date=expected_date,
            notes=f"مصدر أمر الشراء: طلب الاحتياج {requisition.request_number}",
            created_by=request.user,
        )
        PurchaseOrderLine.objects.bulk_create([
            PurchaseOrderLine(order=order, product=line.product, quantity=line.requested_quantity,
                              unit_price=line.estimated_unit_price or Decimal("0"))
            for line in requisition.lines.select_related("product").all()
        ])
        converted = PurchaseRequisition.objects.filter(pk=requisition.pk, status=PurchaseRequisition.Status.APPROVED, converted_order__isnull=True).update(
            status=PurchaseRequisition.Status.CONVERTED, converted_order=order
        )
        if converted != 1:
            raise ValidationError("تم تحويل طلب الاحتياج بالفعل؛ لم يتم إنشاء أمر شراء إضافي.")
        requisition.refresh_from_db()
        return Response({"requisition": self.get_serializer(requisition).data, "purchase_order_id": order.pk}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def cancel(self, request, pk=None):
        requisition = PurchaseRequisition.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_write(requisition.company_id)
        if requisition.status not in (PurchaseRequisition.Status.DRAFT, PurchaseRequisition.Status.SUBMITTED, PurchaseRequisition.Status.REJECTED):
            raise ValidationError("لا يمكن إلغاء طلب معتمد أو محول إلى أمر شراء.")
        PurchaseRequisition.objects.filter(pk=requisition.pk, status=requisition.status).update(status=PurchaseRequisition.Status.CANCELLED)
        requisition.refresh_from_db()
        return Response(self.get_serializer(requisition).data)
