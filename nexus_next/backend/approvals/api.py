"""واجهات إدارة طلبات الاعتماد المالي واعتماد الفواتير وسياسات الخصم."""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_company, has_company_role, is_platform_owner, parse_company_param
from companies.models import Branch
from customers.models import Customer
from sales.models import SalesInvoice
from .models import DiscountApprovalPolicy, SalesInvoiceApproval, FinancialApprovalRequest
from .serializers import DiscountApprovalPolicySerializer, SalesInvoiceApprovalSerializer, FinancialApprovalRequestSerializer
from .snapshots import invoice_discount_snapshot, financial_document_snapshot


class DiscountApprovalPolicyViewSet(viewsets.ModelViewSet):
    serializer_class = DiscountApprovalPolicySerializer
    permission_classes = (CompanyRolePermission,)
    http_method_names = ("get", "post", "patch", "head", "options")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = DiscountApprovalPolicy.objects.filter(company__in=accessible_companies(self.request.user)).select_related("company")
        company_id = parse_company_param(self.request)
        return qs.filter(company_id=company_id) if company_id else qs

    def _admin(self, company_id):
        """تنفيذ وظيفة _admin ضمن واجهات api.py مع احترام دورة المستند والصلاحيات المحاسبية."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, company_id, ("admin",)):
            raise PermissionDenied("إعداد حد الخصم متاح لمدير الشركة فقط.")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        company = serializer.validated_data["company"]
        self._admin(company.pk)
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        self._admin(serializer.instance.company_id)
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        raise PermissionDenied("لا يمكن حذف سياسة الخصم؛ عدّل الحد أو أوقف استخدامه.")


class SalesInvoiceApprovalViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SalesInvoiceApprovalSerializer
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = SalesInvoiceApproval.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related("company", "invoice", "invoice__customer", "requested_by", "decided_by", "invoice__delivery__warehouse", "invoice__branch")
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            scoped = qs
        else:
            companies = accessible_companies(self.request.user)
            branches = accessible_branches(self.request.user)
            default_company_ids = []
            for company_id in companies.values_list("id", flat=True):
                default_branch = Branch.objects.filter(
                    company_id=company_id, is_active=True,
                ).order_by("-is_head_office", "code").first()
                if default_branch and default_branch.pk in set(branches.values_list("id", flat=True)):
                    default_company_ids.append(company_id)
            scoped = qs.filter(
                Q(invoice__delivery__warehouse__branch__in=branches)
                | Q(invoice__delivery__isnull=False, invoice__delivery__warehouse__branch__isnull=True, company_id__in=default_company_ids)
                | Q(invoice__branch__in=branches)
            )
        company_id = parse_company_param(self.request)
        return scoped.filter(company_id=company_id) if company_id else scoped

    def _manager(self, approval):
        """تنفيذ وظيفة _manager ضمن واجهات api.py مع احترام دورة المستند والصلاحيات المحاسبية."""
        if not has_company_role(self.request.user, approval.company_id, ("admin",)):
            raise PermissionDenied("اعتماد استثناءات فواتير المبيعات متاح لمدير الشركة فقط.")
        if approval.requested_by_id == self.request.user.pk:
            raise PermissionDenied("لا يجوز اعتماد طلب الفاتورة بواسطة منشئه.")
        if approval.status != SalesInvoiceApproval.Status.PENDING:
            raise ValidationError("تم اتخاذ قرار على هذا الطلب بالفعل.")
        if approval.invoice.status != "draft":
            raise ValidationError("لا يمكن اعتماد طلب فاتورة ليست مسودة.")
        latest = approval.invoice.approval_requests.order_by("-requested_at", "-pk").first()
        if not latest or latest.pk != approval.pk:
            raise ValidationError("هذا الطلب لم يعد الأحدث؛ راجع طلب الموافقة الحالي.")
        current_max = max(
            approval.invoice.lines.values_list("discount_percent", flat=True),
            default=0,
        )
        if (
            approval.requested_discount_total != approval.invoice.discount_total
            or approval.requested_max_discount_percent != current_max
            or approval.requested_invoice_limit != approval.invoice.invoice_approval_limit
            or approval.requested_lines_snapshot != invoice_discount_snapshot(approval.invoice)
        ):
            raise ValidationError("تغيرت تفاصيل الفاتورة بعد إنشاء الطلب؛ يجب مراجعة الطلب الأحدث.")
        if approval.requested_credit_limit is not None:
            from sales.services import customer_credit_exposure
            customer = Customer.objects.select_for_update().get(pk=approval.invoice.customer_id)
            current_exposure = customer_credit_exposure(customer)
            if (
                customer.credit_limit != approval.requested_credit_limit
                or current_exposure != approval.requested_credit_exposure
                or approval.invoice.grand_total != approval.requested_credit_amount
                or current_exposure + approval.invoice.grand_total <= customer.credit_limit
            ):
                raise ValidationError("تغير موقف العميل الائتماني منذ إنشاء الطلب؛ أعد مراجعة حد الائتمان قبل الاعتماد.")

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def approve(self, request, pk=None):
        """اعتماد الطلب بعد التحقق من الدور وعدم تعارض المصالح."""
        candidate = self.get_object()
        SalesInvoice.objects.select_for_update().get(pk=candidate.invoice_id)
        approval = SalesInvoiceApproval.objects.select_for_update().select_related("invoice").get(pk=candidate.pk)
        self._manager(approval)
        approval.status = SalesInvoiceApproval.Status.APPROVED
        approval.decided_by = request.user
        approval.decision_note = str(request.data.get("note", "")).strip()[:500]
        approval.save()
        return Response(self.get_serializer(approval).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reject(self, request, pk=None):
        """رفض الطلب وتسجيل قرار الرفض وسببه."""
        candidate = self.get_object()
        SalesInvoice.objects.select_for_update().get(pk=candidate.invoice_id)
        approval = SalesInvoiceApproval.objects.select_for_update().select_related("invoice").get(pk=candidate.pk)
        self._manager(approval)
        note = str(request.data.get("note", "")).strip()
        if not note:
            raise ValidationError({"note": "سبب رفض الخصم مطلوب."})
        approval.status = SalesInvoiceApproval.Status.REJECTED
        approval.decided_by = request.user
        approval.decision_note = note[:500]
        approval.save()
        return Response(self.get_serializer(approval).data, status=status.HTTP_200_OK)


class FinancialApprovalRequestViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = FinancialApprovalRequestSerializer
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = FinancialApprovalRequest.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related(
            "company", "requested_by", "decided_by", "purchase_invoice",
            "purchase_invoice__supplier", "purchase_invoice__branch", "purchase_invoice__receipt__warehouse", "purchase_order", "purchase_order__supplier",
            "purchase_order__warehouse", "purchase_order__warehouse__branch", "supplier_payment", "supplier_payment__supplier",
            "treasury_transaction", "treasury_transaction__source_account",
            "treasury_transaction__counter_account", "asset_purchase_invoice",
            "asset_purchase_invoice__supplier", "asset_purchase_invoice__asset",
            "asset_purchase_invoice__tax_code", "asset_supplier_payment",
            "asset_supplier_payment__supplier", "asset_supplier_payment__invoice",
            "asset_supplier_payment__payment_account",
        )
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            companies = accessible_companies(self.request.user)
            branches = accessible_branches(self.request.user)
            default_company_ids = []
            accessible_branch_ids = set(branches.values_list("id", flat=True))
            for company_id in companies.values_list("id", flat=True):
                default_branch = Branch.objects.filter(
                    company_id=company_id, is_active=True,
                ).order_by("-is_head_office", "code").first()
                if default_branch and default_branch.pk in accessible_branch_ids:
                    default_company_ids.append(company_id)
            qs = qs.filter(
                Q(document_type=FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE,
                  purchase_invoice__branch__in=branches)
                | Q(document_type=FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE,
                    purchase_invoice__receipt__warehouse__branch__in=branches)
                | Q(document_type=FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE,
                    purchase_invoice__branch__isnull=True, purchase_invoice__receipt__warehouse__branch__isnull=True, company_id__in=default_company_ids)
                | Q(document_type=FinancialApprovalRequest.DocumentType.PURCHASE_ORDER,
                    purchase_order__warehouse__branch__in=branches)
                | Q(document_type=FinancialApprovalRequest.DocumentType.PURCHASE_ORDER,
                    purchase_order__warehouse__branch__isnull=True, company_id__in=default_company_ids)
                | Q(document_type=FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT,
                    supplier_payment__invoice__branch__in=branches)
                | Q(document_type=FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT,
                    supplier_payment__invoice__receipt__warehouse__branch__in=branches)
                | Q(document_type=FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT,
                    supplier_payment__invoice__branch__isnull=True, supplier_payment__invoice__receipt__warehouse__branch__isnull=True, company_id__in=default_company_ids)
                | Q(document_type=FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT,
                    treasury_transaction__branch__in=branches)
                | Q(document_type=FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE,
                    asset_purchase_invoice__branch__in=branches)
                | Q(document_type=FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT,
                    asset_supplier_payment__branch__in=branches)
            )
        company_id = parse_company_param(self.request)
        if company_id:
            qs = qs.filter(company_id=company_id)
        document_type = self.request.query_params.get("document_type")
        if document_type:
            qs = qs.filter(document_type=document_type)
        return qs

    def _locked_document(self, approval):
        """تنفيذ وظيفة _locked_document ضمن واجهات api.py مع احترام دورة المستند والصلاحيات المحاسبية."""
        if approval.document_type == FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE:
            from purchasing.models import PurchaseInvoice
            return PurchaseInvoice.objects.select_for_update(of=("self",)).select_related(
                "supplier", "branch", "receipt"
            ).prefetch_related("lines__receipt_line__product", "lines__expense_account", "lines__cost_center", "lines__tax_code").get(pk=approval.purchase_invoice_id)
        if approval.document_type == FinancialApprovalRequest.DocumentType.PURCHASE_ORDER:
            from purchasing.models import PurchaseOrder
            return PurchaseOrder.objects.select_for_update(of=("self",)).select_related(
                "supplier", "warehouse", "warehouse__branch",
            ).prefetch_related("lines__product").get(pk=approval.purchase_order_id)
        if approval.document_type == FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT:
            from purchasing.models import SupplierPayment
            return SupplierPayment.objects.select_for_update(of=("self",)).select_related(
                "supplier", "invoice"
            ).get(pk=approval.supplier_payment_id)
        if approval.document_type == FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE:
            from assets.models import AssetPurchaseInvoice
            return AssetPurchaseInvoice.objects.select_for_update(of=("self",)).select_related(
                "supplier", "asset", "tax_code", "branch", "fiscal_year",
            ).get(pk=approval.asset_purchase_invoice_id)
        if approval.document_type == FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT:
            from assets.models import AssetSupplierPayment
            return AssetSupplierPayment.objects.select_for_update(of=("self",)).select_related(
                "supplier", "invoice", "payment_account", "branch",
            ).get(pk=approval.asset_supplier_payment_id)
        from treasury.models import TreasuryTransaction
        return TreasuryTransaction.objects.select_for_update(of=("self",)).select_related(
            "source_account", "counter_account"
        ).get(pk=approval.treasury_transaction_id)

    def _validate_manager_decision(self, approval, document):
        """تنفيذ وظيفة _validate_manager_decision ضمن واجهات api.py مع احترام دورة المستند والصلاحيات المحاسبية."""
        if not has_company_role(self.request.user, approval.company_id, ("admin",)):
            raise PermissionDenied("اعتماد الطلبات المالية متاح لمدير الشركة فقط.")
        if approval.requested_by_id == self.request.user.pk:
            raise PermissionDenied("لا يجوز اعتماد الطلب بواسطة منشئه.")
        if approval.status != FinancialApprovalRequest.Status.PENDING:
            raise ValidationError("تم اتخاذ قرار على هذا الطلب بالفعل.")
        relation = {
            FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE: "purchase_invoice",
            FinancialApprovalRequest.DocumentType.PURCHASE_ORDER: "purchase_order",
            FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT: "supplier_payment",
            FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT: "treasury_transaction",
            FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE: "asset_purchase_invoice",
            FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT: "asset_supplier_payment",
        }[approval.document_type]
        latest = FinancialApprovalRequest.objects.filter(
            document_type=approval.document_type, **{f"{relation}_id": document.pk}
        ).order_by("-requested_at", "-pk").first()
        if not latest or latest.pk != approval.pk:
            raise ValidationError("هذا الطلب لم يعد الأحدث؛ راجع طلب الموافقة الحالي.")
        if document.status != "draft":
            raise ValidationError("لا يمكن اعتماد مستند ليس مسودة.")
        snapshot = financial_document_snapshot(approval.document_type, document)
        amount = document.grand_total if approval.document_type in (FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE, FinancialApprovalRequest.DocumentType.PURCHASE_ORDER, FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE) else document.amount
        policy = DiscountApprovalPolicy.objects.filter(company_id=approval.company_id).first()
        field = {
            FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE: "max_purchase_invoice_amount",
            FinancialApprovalRequest.DocumentType.PURCHASE_ORDER: "max_purchase_order_amount",
            FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT: "max_payment_amount",
            FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT: "max_payment_amount",
            FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE: "max_purchase_invoice_amount",
            FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT: "max_payment_amount",
        }[approval.document_type]
        limit = getattr(policy, field, None) if policy else None
        if approval.document_type == FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT:
            from treasury.models import TreasuryTransaction
            if document.kind != TreasuryTransaction.Kind.PAYMENT:
                limit = None
        if (
            snapshot != approval.requested_snapshot
            or amount != approval.requested_amount
            or limit != approval.requested_limit
            or limit is None
            or amount <= limit
        ):
            raise ValidationError("تغير المستند أو حد الاعتماد بعد الطلب؛ يجب إنشاء طلب جديد ومراجعة النسخة الحالية.")

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def approve(self, request, pk=None):
        """اعتماد الطلب بعد التحقق من الدور وعدم تعارض المصالح."""
        candidate = self.get_object()
        approval = FinancialApprovalRequest.objects.select_for_update().get(pk=candidate.pk)
        document = self._locked_document(approval)
        self._validate_manager_decision(approval, document)
        approval.status = FinancialApprovalRequest.Status.APPROVED
        approval.decided_by = request.user
        approval.decision_note = str(request.data.get("note", "")).strip()[:500]
        approval.save()
        if approval.document_type == FinancialApprovalRequest.DocumentType.PURCHASE_ORDER:
            document.status = "confirmed"
            document.confirmed_at = timezone.now()
            document.save(update_fields=("status", "confirmed_at"))
        return Response(self.get_serializer(approval).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reject(self, request, pk=None):
        """رفض الطلب وتسجيل قرار الرفض وسببه."""
        candidate = self.get_object()
        approval = FinancialApprovalRequest.objects.select_for_update().get(pk=candidate.pk)
        document = self._locked_document(approval)
        self._validate_manager_decision(approval, document)
        note = str(request.data.get("note", "")).strip()
        if not note:
            raise ValidationError({"note": "سبب رفض الطلب المالي مطلوب."})
        approval.status = FinancialApprovalRequest.Status.REJECTED
        approval.decided_by = request.user
        approval.decision_note = note[:500]
        approval.save()
        return Response(self.get_serializer(approval).data, status=status.HTTP_200_OK)
