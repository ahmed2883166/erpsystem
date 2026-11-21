"""Country-aware VAT operation categories.

These are internal bookkeeping classifications, not a claim that a tax return is
ready for filing. A category always maps to the existing posting treatment so
legacy VAT arithmetic and journal posting remain unchanged.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaxCategory:
    code: str
    label_ar: str
    treatment: str
    note_ar: str = ""


_CATEGORIES = {
    ("EG", "sale"): (
        TaxCategory("eg_sales_standard", "مبيعات محلية خاضعة للسعر العام", "standard"),
        TaxCategory("eg_sales_zero_export", "صادرات بسعر صفر", "zero_rated",
                    "يلزم الاحتفاظ بمستندات التصدير المؤيدة."),
        TaxCategory("eg_sales_exempt", "مبيعات معفاة", "exempt"),
        TaxCategory("eg_sales_out_of_scope", "عمليات خارج نطاق الضريبة", "out_of_scope"),
    ),
    ("EG", "purchase"): (
        TaxCategory("eg_purchase_standard", "مشتريات محلية خاضعة", "standard"),
        TaxCategory("eg_purchase_customs_vat", "واردات سُددت ضريبتها بالجمارك", "standard",
                    "يلزم مستند الإفراج/السداد الجمركي قبل المطالبة بالخصم."),
        TaxCategory("eg_purchase_zero", "مشتريات بسعر صفر", "zero_rated"),
        TaxCategory("eg_purchase_exempt", "مشتريات معفاة", "exempt"),
        TaxCategory("eg_purchase_out_of_scope", "مشتريات خارج نطاق الضريبة", "out_of_scope"),
    ),
    ("SA", "sale"): (
        TaxCategory("sa_sales_standard", "توريدات محلية خاضعة للنسبة الأساسية", "standard"),
        TaxCategory("sa_sales_zero_export", "صادرات/توريدات صفرية مؤهلة", "zero_rated",
                    "يجب التحقق من شروط التصفير والمستندات المؤيدة."),
        TaxCategory("sa_sales_exempt", "توريدات معفاة", "exempt"),
        TaxCategory("sa_sales_out_of_scope", "عمليات خارج نطاق الضريبة", "out_of_scope"),
    ),
    ("SA", "purchase"): (
        TaxCategory("sa_purchase_standard", "مشتريات محلية خاضعة", "standard"),
        TaxCategory("sa_purchase_customs_vat", "واردات سُددت ضريبتها للجمارك", "standard",
                    "يلزم مستند الاستيراد والسداد قبل المطالبة بالخصم."),
        TaxCategory("sa_purchase_zero", "مشتريات صفرية", "zero_rated"),
        TaxCategory("sa_purchase_exempt", "مشتريات معفاة", "exempt"),
        TaxCategory("sa_purchase_out_of_scope", "مشتريات خارج نطاق الضريبة", "out_of_scope"),
    ),
}

_INDEX = {
    (country, kind, item.code): item
    for (country, kind), items in _CATEGORIES.items()
    for item in items
}


def tax_category_options(country, document_kind):
    """Return selectable internal categories for EG/SA sales or purchases."""
    country = str(country or "").strip().upper()
    document_kind = str(document_kind or "").strip().lower()
    if country not in {"EG", "SA"} or document_kind not in {"sale", "purchase"}:
        return ()
    return _CATEGORIES[(country, document_kind)]


def validate_tax_category(country, document_kind, category_code, treatment):
    """Validate category/treatment agreement; blank remains valid for legacy rows."""
    category_code = str(category_code or "").strip()
    treatment = str(treatment or "standard").strip().lower()
    if not category_code:
        return ""
    country = str(country or "").strip().upper()
    document_kind = str(document_kind or "").strip().lower()
    item = _INDEX.get((country, document_kind, category_code))
    if item is None:
        raise ValueError("التصنيف الضريبي التفصيلي غير متاح لدولة الشركة أو نوع المستند.")
    if item.treatment != treatment:
        raise ValueError("التصنيف الضريبي التفصيلي لا يتفق مع المعاملة الضريبية المحددة.")
    return item.code


def tax_category_label(country, document_kind, category_code):
    """Return an Arabic label for a stored category, or an empty string for legacy data."""
    country = str(country or "").strip().upper()
    document_kind = str(document_kind or "").strip().lower()
    item = _INDEX.get((country, document_kind, str(category_code or "").strip()))
    return item.label_ar if item else ""
