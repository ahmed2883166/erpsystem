"""قواعد إنشاء الاعتمادات والتحقق منها وتنفيذها مع حفظ سلامة الأثر المالي."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import DiscountApprovalPolicy, FinancialApprovalRequest
from .snapshots import financial_document_snapshot


LIMIT_FIELD = {
    FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE: "max_purchase_invoice_amount",
    FinancialApprovalRequest.DocumentType.PURCHASE_ORDER: "max_purchase_order_amount",
    FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT: "max_payment_amount",
    FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT: "max_payment_amount",
    FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE: "max_purchase_invoice_amount",
    FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT: "max_payment_amount",
}


def _document_relation(document_type):
    return {
        FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE: "purchase_invoice",
        FinancialApprovalRequest.DocumentType.PURCHASE_ORDER: "purchase_order",
        FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT: "supplier_payment",
        FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT: "treasury_transaction",
        FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE: "asset_purchase_invoice",
        FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT: "asset_supplier_payment",
    }[document_type]


@transaction.atomic
def refresh_financial_approval(document_type, document, user):
    """Create a fresh immutable request when a draft exceeds its company's configured limit."""
    relation = _document_relation(document_type)
    amount = Decimal(document.grand_total if document_type in (FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE, FinancialApprovalRequest.DocumentType.PURCHASE_ORDER, FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE) else document.amount)
    policy = DiscountApprovalPolicy.objects.filter(company_id=document.company_id).first()
    limit = getattr(policy, LIMIT_FIELD[document_type], None) if policy else None
    if document_type == FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT:
        from treasury.models import TreasuryTransaction
        if document.kind != TreasuryTransaction.Kind.PAYMENT:
            limit = None
    pending = FinancialApprovalRequest.objects.select_for_update().filter(
        document_type=document_type, **{f"{relation}_id": document.pk},
        status=FinancialApprovalRequest.Status.PENDING,
    )
    if limit is None or amount <= limit:
        for request in pending:
            request.status = FinancialApprovalRequest.Status.SUPERSEDED
            request.save(update_fields=("status", "decided_at"))
        return None

    snapshot = financial_document_snapshot(document_type, document)
    latest = FinancialApprovalRequest.objects.filter(
        document_type=document_type, **{f"{relation}_id": document.pk}
    ).order_by("-requested_at", "-pk").first()
    if latest and latest.status == FinancialApprovalRequest.Status.PENDING:
        if latest.requested_snapshot == snapshot and latest.requested_limit == limit:
            return latest
        latest.status = FinancialApprovalRequest.Status.SUPERSEDED
        latest.save(update_fields=("status", "decided_at"))

    return FinancialApprovalRequest.objects.create(
        company_id=document.company_id,
        document_type=document_type,
        **{relation: document},
        requested_by=user,
        requested_limit=limit,
        requested_amount=amount,
        requested_snapshot=snapshot,
    )


def require_financial_approval(document_type, document):
    """Block posting unless the latest approval matches the exact current draft snapshot."""
    relation = _document_relation(document_type)
    amount = Decimal(document.grand_total if document_type in (FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE, FinancialApprovalRequest.DocumentType.PURCHASE_ORDER, FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE) else document.amount)
    policy = DiscountApprovalPolicy.objects.filter(company_id=document.company_id).first()
    limit = getattr(policy, LIMIT_FIELD[document_type], None) if policy else None
    if document_type == FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT:
        from treasury.models import TreasuryTransaction
        if document.kind != TreasuryTransaction.Kind.PAYMENT:
            limit = None
    if limit is None or amount <= limit:
        return
    latest = FinancialApprovalRequest.objects.filter(
        document_type=document_type, **{f"{relation}_id": document.pk}
    ).order_by("-requested_at", "-pk").first()
    if (
        not latest
        or latest.status != FinancialApprovalRequest.Status.APPROVED
        or latest.requested_limit != limit
        or latest.requested_amount != amount
        or latest.requested_snapshot != financial_document_snapshot(document_type, document)
    ):
        raise ValidationError(
            "تجاوز المستند حد الاعتماد المالي؛ يلزم اعتماد مدير الشركة لآخر نسخة قبل الترحيل.",
            code="approval_required",
        )
