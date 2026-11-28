"""Safe configurable Excel exports for operational master data."""
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse
from openpyxl import Workbook
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, ValidationError

from companies.access import (
    accessible_branches, can_access_company, has_company_role,
    has_company_wide_branch_access,
)
from customers.models import Customer
from hr.models import Employee
from inventory.models import Product, StockBalance, StockMovement
from suppliers.models import Supplier
from treasury.models import TreasuryAccount, TreasuryTransaction


EXPORTS = {
    "inventory": {
        "model": Product,
        "label": "المخزون - الأصناف",
        "fields": ("code", "barcode", "name_ar", "name_en", "product_type", "stock_class",
                   "category", "unit", "purchase_price", "sale_price", "costing_method",
                   "min_stock", "track_expiry", "is_active", "created_at"),
    },
    "inventory_balances": {
        "model": StockBalance,
        "label": "المخزون - الأرصدة",
        "fields": ("product", "warehouse", "location", "expiry_date", "quantity", "inventory_value", "average_cost", "updated_at"),
    },
    "inventory_movements": {
        "model": StockMovement,
        "label": "المخزون - الحركات",
        "fields": ("movement_number", "movement_type", "adjustment_reason", "movement_date", "source_warehouse", "destination_warehouse", "status", "notes", "posted_at", "created_at"),
    },
    "customers": {
        "model": Customer,
        "label": "العملاء",
        "fields": ("code", "customer_type", "name_ar", "name_en", "tax_number", "phone",
                   "mobile", "email", "address", "credit_limit", "payment_terms_days",
                   "default_discount_percent", "is_active", "created_at"),
    },
    "suppliers": {
        "model": Supplier,
        "label": "الموردون",
        "fields": ("code", "supplier_type", "name_ar", "name_en", "tax_number", "phone",
                   "mobile", "email", "address", "credit_limit", "payment_terms_days",
                   "is_active", "created_at"),
    },
    "bank": {
        "model": TreasuryAccount,
        "label": "الحسابات البنكية والخزائن",
        "fields": ("code", "kind", "name_ar", "bank_name", "account_number", "iban",
                   "currency_code", "opening_balance", "branch", "is_active", "created_at"),
    },
    "bank_transactions": {
        "model": TreasuryTransaction,
        "label": "البنك - الحركات المالية",
        "fields": ("document_number", "transaction_date", "kind", "status", "amount",
                   "transaction_currency", "transaction_amount", "exchange_rate", "carrying_amount",
                   "source_account", "destination_account", "counter_account", "description",
                   "reference", "cash_flow_category", "branch", "posted_at", "created_at"),
    },
    "employees": {
        "model": Employee,
        "label": "الموظفون",
        "fields": ("code", "full_name_ar", "full_name_en", "national_id", "insurance_number",
                   "email", "phone", "branch", "department", "position", "contract_type",
                   "hire_date", "contract_end_date", "termination_date", "status",
                   "basic_salary", "currency", "payment_method", "bank_name", "bank_account",
                   "cost_center", "created_at"),
    },
}


FIELD_LABELS = {
    "code": "الكود", "barcode": "الباركود", "name_ar": "الاسم بالعربية",
    "name_en": "الاسم بالإنجليزية", "product_type": "نوع الصنف",
    "stock_class": "تصنيف المخزون", "category": "المجموعة الصنفية",
    "unit": "وحدة القياس", "purchase_price": "سعر الشراء", "sale_price": "سعر البيع",
    "costing_method": "طريقة تقييم التكلفة", "min_stock": "حد إعادة الطلب",
    "track_expiry": "تتبع الصلاحية", "is_active": "نشط", "created_at": "تاريخ الإنشاء",
    "product": "الصنف", "warehouse": "المخزن", "location": "موقع التخزين",
    "expiry_date": "تاريخ الصلاحية", "quantity": "الكمية", "inventory_value": "قيمة المخزون",
    "average_cost": "متوسط التكلفة", "updated_at": "آخر تحديث",
    "movement_number": "رقم الحركة", "movement_type": "نوع الحركة",
    "adjustment_reason": "سبب التسوية", "movement_date": "تاريخ الحركة",
    "source_warehouse": "مخزن المصدر", "destination_warehouse": "مخزن الوجهة",
    "status": "الحالة", "notes": "ملاحظات", "posted_at": "تاريخ الترحيل",
    "customer_type": "نوع العميل", "supplier_type": "نوع المورد",
    "tax_number": "الرقم الضريبي", "phone": "الهاتف", "mobile": "المحمول",
    "email": "البريد الإلكتروني", "address": "العنوان", "credit_limit": "حد الائتمان",
    "payment_terms_days": "مدة السداد بالأيام", "default_discount_percent": "نسبة الخصم الافتراضية",
    "bank_name": "اسم البنك", "account_number": "رقم الحساب", "iban": "رقم IBAN",
    "kind": "نوع الحساب", "currency_code": "رمز العملة", "opening_balance": "الرصيد الافتتاحي",
    "branch": "الفرع", "full_name_ar": "اسم الموظف بالعربية",
    "full_name_en": "اسم الموظف بالإنجليزية", "national_id": "الرقم القومي",
    "insurance_number": "الرقم التأميني", "department": "الإدارة", "position": "الوظيفة",
    "contract_type": "نوع العقد", "hire_date": "تاريخ التعيين",
    "contract_end_date": "تاريخ نهاية العقد", "termination_date": "تاريخ انتهاء الخدمة",
    "basic_salary": "الراتب الأساسي", "currency": "العملة",
    "payment_method": "طريقة صرف الراتب", "bank_account": "الحساب البنكي",
    "cost_center": "مركز التكلفة", "document_number": "رقم المستند",
    "transaction_date": "تاريخ الحركة", "amount": "المبلغ بالعملة الأساسية",
    "transaction_currency": "عملة الحركة", "transaction_amount": "المبلغ بعملة الحركة",
    "exchange_rate": "سعر الصرف", "carrying_amount": "القيمة الدفترية",
    "source_account": "حساب المصدر", "destination_account": "حساب الوجهة",
    "counter_account": "الحساب المقابل", "description": "البيان",
    "reference": "المرجع", "cash_flow_category": "تصنيف التدفق النقدي",
}


def _field_label(model, name):
    field = model._meta.get_field(name)
    if model is TreasuryTransaction and name == "kind":
        return "نوع الحركة"
    return FIELD_LABELS.get(name, str(field.verbose_name))


def _queryset_for(resource, company_id, user):
    spec = EXPORTS[resource]
    if resource == "inventory_balances":
        qs = spec["model"].objects.filter(warehouse__company_id=company_id)
    else:
        qs = spec["model"].objects.filter(company_id=company_id)
    if resource in ("inventory_movements", "inventory_balances"):
        from django.db.models import Q
        branches = accessible_branches(user, company_id)
        company_wide = has_company_wide_branch_access(user, company_id)
        if resource == "inventory_balances":
            scope = Q(warehouse__branch__in=branches)
            if company_wide:
                scope |= Q(warehouse__branch__isnull=True)
            qs = qs.filter(scope)
        else:
            source_scope = Q(source_warehouse__branch__in=branches) | Q(source_warehouse__isnull=True)
            destination_scope = Q(destination_warehouse__branch__in=branches) | Q(destination_warehouse__isnull=True)
            if company_wide:
                source_scope |= Q(source_warehouse__branch__isnull=True)
                destination_scope |= Q(destination_warehouse__branch__isnull=True)
            qs = qs.filter(source_scope & destination_scope)
    if resource == "employees":
        qs = qs.filter(branch__in=accessible_branches(user, company_id))
    elif resource == "bank_transactions":
        qs = qs.filter(branch__in=accessible_branches(user, company_id))
    elif resource == "bank":
        branches = accessible_branches(user, company_id)
        qs = qs.filter(branch__in=branches)
        if has_company_wide_branch_access(user, company_id):
            from django.db.models import Q
            qs = spec["model"].objects.filter(company_id=company_id).filter(Q(branch__in=branches) | Q(branch__isnull=True))
    return qs.order_by("pk")


def _excel_value(value):
    if isinstance(value, str) and value.lstrip(" \\t\\r\\n")[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _plain(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


class CustomExcelExportView(APIView):
    """Metadata, preview and XLSX export with the same validated field selection."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        resource = request.query_params.get("resource", "")
        if resource not in EXPORTS:
            raise ValidationError({"resource": "قسم التصدير غير مدعوم."})
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "يجب تحديد شركة صحيحة."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تصدير البيانات متاح لمدير الشركة والمحاسب فقط.")

        spec = EXPORTS[resource]
        model = spec["model"]
        if request.query_params.get("mode") == "fields":
            response = Response({
                "resource": resource,
                "label": spec["label"],
                "fields": [{"key": name, "label": _field_label(model, name)} for name in spec["fields"]],
            })
            response["Cache-Control"] = "no-store"
            return response

        selected = [item for item in request.query_params.get("fields", "").split(",") if item]
        if not selected or len(selected) > len(spec["fields"]) or len(set(selected)) != len(selected):
            raise ValidationError({"fields": "اختر عمودًا واحدًا على الأقل دون تكرار."})
        invalid = set(selected) - set(spec["fields"])
        if invalid:
            raise ValidationError({"fields": "توجد أعمدة غير مسموح بتصديرها."})

        qs = _queryset_for(resource, company_id, request.user)
        branch_id = request.query_params.get("branch")
        if branch_id:
            if resource not in ("bank", "bank_transactions", "employees", "inventory_balances", "inventory_movements"):
                raise ValidationError({"branch": "تصفية الفرع غير متاحة لهذا القسم."})
            try:
                branch_id = int(branch_id)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."}) from None
            if branch_id <= 0:
                raise ValidationError({"branch": "معرّف الفرع غير صالح."})
            if not accessible_branches(request.user, company_id).filter(pk=branch_id).exists():
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            if resource == "inventory_balances":
                qs = qs.filter(warehouse__branch_id=branch_id)
            elif resource == "inventory_movements":
                from django.db.models import Q
                source_scope = Q(source_warehouse__branch_id=branch_id) | Q(source_warehouse__isnull=True)
                destination_scope = Q(destination_warehouse__branch_id=branch_id) | Q(destination_warehouse__isnull=True)
                if has_company_wide_branch_access(request.user, company_id):
                    source_scope |= Q(source_warehouse__branch__isnull=True)
                    destination_scope |= Q(destination_warehouse__branch__isnull=True)
                qs = qs.filter(source_scope & destination_scope)
            else:
                qs = qs.filter(branch_id=branch_id)

        total = qs.count()
        headers = [_field_label(model, field) for field in selected]
        sample = list(qs.values_list(*selected)[:10])
        if request.query_params.get("preview") == "1":
            response = Response({
                "resource": resource, "total_rows": total, "preview_rows": [
                    [_plain(value) for value in row] for row in sample
                ], "headers": headers,
            })
            response["Cache-Control"] = "no-store"
            return response

        if total > 100000:
            raise ValidationError({"detail": "عدد السجلات يتجاوز الحد الآمن للتصدير (100,000). استخدم الفلاتر لتقسيم الملف."})

        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet(title=spec["label"][:31])
        sheet.append(headers)
        for row in qs.values_list(*selected).iterator(chunk_size=2000):
            sheet.append([_excel_value(value) for value in row])
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{resource}-export.xlsx"'
        response["Cache-Control"] = "no-store"
        return response
