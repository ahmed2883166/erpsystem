from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from accounting.models import FxSubledgerRevaluation, JournalEntry
from accounting.services import reverse_operational_journal
from accounting.fx_subledger_services import revalue_fx_invoice
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from companies.models import Branch
from inventory.models import StockMovement
from .models import CustomerReceipt, SalesDelivery, SalesInvoice
from .serializers import CustomerReceiptSerializer, SalesDeliverySerializer, SalesInvoiceSerializer
from .services import post_customer_receipt, post_sales_delivery, post_sales_invoice

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
        raise PermissionDenied("لا يمكن حذف مستندات المبيعات؛ استخدم الإلغاء المعتمد بعد إضافته.")


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

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        invoice = self.get_object()
        self._check_write(invoice.company_id)
        warehouse = invoice.delivery.warehouse
        if not _sales_warehouse_accessible(request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن التسليم.")
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
            balance_due = max(invoice.grand_total - paid_as_of, Decimal("0"))
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
    queryset = CustomerReceipt.objects.select_related("company", "customer", "invoice", "journal_entry")
    filterset_fields = ("company", "customer", "invoice", "status", "receipt_date", "method")
    search_fields = ("receipt_number", "reference", "customer__name_ar", "invoice__invoice_number")
    ordering_fields = ("receipt_date", "receipt_number", "amount", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        return qs.filter(Q(invoice__delivery__warehouse__branch__isnull=True) | Q(invoice__delivery__warehouse__branch__in=accessible_branches(self.request.user)))

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
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
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
        if warehouse.branch_id and not can_access_branch(request.user, warehouse.branch_id, receipt.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        receipt = post_customer_receipt(receipt.pk, request.user)
        return Response(self.get_serializer(receipt).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        """عكس آخر سند قبض مرحّل بقيد عكسي كامل مع حماية تسلسل التسويات."""
        receipt = CustomerReceipt.objects.select_for_update().select_related(
            "invoice", "invoice__delivery__warehouse", "journal_entry"
        ).get(pk=self.get_object().pk)
        self._check_write(receipt.company_id)
        warehouse = receipt.invoice.delivery.warehouse
        if warehouse.branch_id and not can_access_branch(request.user, warehouse.branch_id, receipt.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
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
        if FxSubledgerRevaluation.objects.filter(sales_invoice_id=receipt.invoice_id).exists():
            raise ValidationError("لا يمكن عكس سند قبض لفاتورة لها سجل إعادة تقييم عملة؛ راجع تسلسل التسويات وإعادة التقييم أولًا.")
        entry_date = parse_date(request.data.get("entry_date", "")) if request.data.get("entry_date") else timezone.localdate()
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
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
