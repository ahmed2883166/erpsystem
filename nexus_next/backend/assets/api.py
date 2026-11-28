"""واجهات الأصول الثابتة للاقتناء والإهلاك والاستبعاد وربط الخزينة."""

import logging

from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.utils import timezone, dateparse
from decimal import Decimal, InvalidOperation

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from .models import FixedAsset, AssetDepreciation, AssetPurchaseInvoice, AssetSupplierPayment, AssetImpairmentReversal
from .serializers import FixedAssetSerializer, AssetDepreciationSerializer
from .services import acquire_asset, depreciate_asset, dispose_asset, record_asset_impairment, reverse_asset_impairment, post_asset_purchase_invoice, post_asset_supplier_payment
from accounting.models import ChartOfAccount, JournalEntry, JournalLine

logger = logging.getLogger(__name__)


class FixedAssetViewSet(viewsets.ModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = FixedAssetSerializer
    queryset = FixedAsset.objects.select_related("company", "branch", "custodian_employee", "asset_account",
        "accumulated_depreciation_account", "depreciation_expense_account",
        "acquisition_counter_account", "disposal_gain_loss_account", "disposal_loss_account", "acquisition_journal", "acquisition_treasury_transaction", "disposal_treasury_transaction", "vendor_invoice").prefetch_related("depreciations", "impairments", "impairment_reversals", "vendor_invoice__payments").all()

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        company_id = self.request.query_params.get("company")
        if company_id:
            if not company_id.isdigit() or not can_access_company(self.request.user, int(company_id)):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
            qs = qs.filter(company_id=int(company_id))
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    def _write(self, company_id):
        """التحقق من صلاحية الكتابة على حسابات وحركات الخزينة."""
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        if not can_access_company(self.request.user, data["company"].pk) or not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول للشركة أو الفرع.")
        self._write(data["company"].pk)
        try:
            with transaction.atomic():
                asset = serializer.save()
                if self.request.data.get("acquisition_mode") != "supplier_invoice":
                    acquire_asset(asset.pk, self.request.data.get("fiscal_year"), self.request.user)
                serializer.instance.refresh_from_db()
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق الشركة والفرع."""
        instance = serializer.instance
        company = serializer.validated_data.get("company", instance.company)
        branch = serializer.validated_data.get("branch", instance.branch)
        if company.pk != instance.company_id:
            raise ValidationError({"company": "لا يمكن نقل سجل أصل ثابت بين الشركات."})
        if not can_access_company(self.request.user, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى شركة الأصل.")
        if not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل المحدد.")
        if instance.acquisition_journal_id or instance.acquisition_treasury_transaction_id or getattr(instance, "vendor_invoice", None):
            raise ValidationError("الأصل المرتبط بحركة اقتناء أو فاتورة مورد أو المثبت محاسبيًا لا يمكن تعديل بياناته أو حساباته.")
        self._write(company.pk)
        serializer.save()

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        self._write(instance.company_id)
        if instance.acquisition_journal_id or instance.acquisition_treasury_transaction_id or instance.depreciations.exists() or instance.impairments.exists() or getattr(instance, "vendor_invoice", None):
            raise ValidationError("لا يمكن حذف أصل مرتبط بحركة اقتناء أو فاتورة مورد أو له أثر محاسبي؛ استخدم إجراء الاستبعاد.")
        instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def depreciate(self, request, pk=None):
        """تنفيذ إهلاك الأصل عن الفترة المحددة وترحيل أثره."""
        asset = self.get_object()
        self._write(asset.company_id)
        try:
            amount = depreciate_asset(
                asset.pk, request.data.get("period_date"), request.data.get("fiscal_year"),
                request.user, request.data.get("units_produced"),
                request.data.get("units_produced_by_component"),
            )
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response({"asset_id": asset.pk, "depreciation_amount": amount}, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("post",), url_path="impair")
    def impair(self, request, pk=None):
        """اختبار انخفاض القيمة وترحيل الخسارة وفق IAS 36."""
        asset = self.get_object()
        self._write(asset.company_id)
        if not can_access_branch(request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل.")
        try:
            impairment = record_asset_impairment(
                asset.pk, request.data.get("impairment_date"),
                request.data.get("recoverable_amount"),
                request.data.get("impairment_expense_account"),
                request.data.get("accumulated_impairment_account"),
                request.data.get("fiscal_year"), request.user,
            )
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response({
            "id": impairment.pk, "asset_id": asset.pk,
            "impairment_date": impairment.impairment_date,
            "carrying_amount_before": impairment.carrying_amount_before,
            "recoverable_amount": impairment.recoverable_amount,
            "impairment_loss": impairment.impairment_loss,
            "journal_entry": impairment.journal_entry_id,
            "accounting_model": "cost",
            "oci_impact": "none_under_cost_model",
            "oci_note": "خسارة الانخفاض تُثبت في قائمة الدخل؛ لا يوجد نموذج إعادة تقييم للأصول مفعّل لمعالجة فائض إعادة التقييم ضمن الدخل الشامل الآخر.",
        }, status=status.HTTP_201_CREATED)


    @decorators.action(detail=True, methods=("post",), url_path="reverse-impairment")
    def reverse_impairment(self, request, pk=None):
        """عكس انخفاض القيمة مع الالتزام بسقف القيمة الدفترية الافتراضية وفق IAS 36."""
        asset = self.get_object()
        self._write(asset.company_id)
        if not can_access_branch(request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل.")
        try:
            reversal = reverse_asset_impairment(
                asset.pk, request.data.get("reversal_date"),
                request.data.get("recoverable_amount"),
                request.data.get("reversal_income_account"),
                request.data.get("fiscal_year"), request.user,
            )
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response({
            "id": reversal.pk, "asset_id": asset.pk,
            "reversal_date": reversal.reversal_date,
            "carrying_amount_before": reversal.carrying_amount_before,
            "recoverable_amount": reversal.recoverable_amount,
            "hypothetical_carrying_amount": reversal.hypothetical_carrying_amount,
            "reversal_amount": reversal.reversal_amount,
            "journal_entry": reversal.journal_entry_id,
            "accounting_model": "cost",
            "oci_impact": "none_under_cost_model",
        }, status=status.HTTP_201_CREATED)


    @decorators.action(detail=True, methods=("get",))
    def schedule(self, request, pk=None):
        """عرض سجل الإهلاك والانخفاض والقيمة الدفترية الحالية للأصل."""
        asset = self.get_object()
        rows = asset.depreciations.select_related("journal_entry").order_by("period_date")
        impairments = asset.impairments.select_related(
            "journal_entry", "impairment_expense_account", "accumulated_impairment_account"
        ).order_by("impairment_date")
        reversals = asset.impairment_reversals.select_related(
            "journal_entry", "reversal_income_account"
        ).prefetch_related("allocations").order_by("reversal_date")
        accumulated = sum((row.amount for row in rows if row.journal_entry.status == JournalEntry.Status.POSTED), start=Decimal("0"))
        impairment_total = sum((row.impairment_loss for row in impairments if row.journal_entry.status == JournalEntry.Status.POSTED), start=Decimal("0"))
        reversal_total = sum((row.reversal_amount for row in reversals if row.journal_entry.status == JournalEntry.Status.POSTED), start=Decimal("0"))
        return response.Response({
            "asset_id": asset.pk, "original_cost": asset.original_cost,
            "accumulated_depreciation": accumulated,
            "accumulated_impairment": impairment_total - reversal_total,
            "net_book_value": asset.original_cost - accumulated - impairment_total + reversal_total,
            "rows": AssetDepreciationSerializer(rows, many=True).data,
            "impairments": [{
                "id": row.pk, "impairment_date": row.impairment_date,
                "carrying_amount_before": row.carrying_amount_before,
                "recoverable_amount": row.recoverable_amount,
                "impairment_loss": row.impairment_loss, "journal_entry": row.journal_entry_id, "status": row.journal_entry.status,
            } for row in impairments],
            "impairment_reversals": [{
                "id": row.pk, "reversal_date": row.reversal_date,
                "carrying_amount_before": row.carrying_amount_before,
                "recoverable_amount": row.recoverable_amount,
                "hypothetical_carrying_amount": row.hypothetical_carrying_amount,
                "reversal_amount": row.reversal_amount,
                "journal_entry": row.journal_entry_id, "status": row.journal_entry.status,
            } for row in reversals],
        })


    def _report_assets(self, request):
        """تجهيز نطاق الأصول المستخدمة في التقرير."""
        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض تقارير الأصول."})
        company_id = int(company_id)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        assets = self.get_queryset().filter(company_id=company_id)
        branch_id = request.query_params.get("branch")
        if branch_id:
            if not branch_id.isdigit() or not can_access_branch(request.user, int(branch_id), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            assets = assets.filter(branch_id=int(branch_id))
        return assets

    @decorators.action(detail=False, methods=("get",), url_path="register")
    def register(self, request):
        """تسجيل أصل ثابت جديد وربطه بحساباته وبيانات اقتنائه."""
        assets = self._report_assets(request)
        as_of_raw = request.query_params.get("as_of")
        as_of = dateparse.parse_date(as_of_raw) if as_of_raw else timezone.localdate()
        if not as_of:
            raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        assets = assets.filter(acquisition_date__lte=as_of, acquisition_journal__status=JournalEntry.Status.POSTED).select_related("branch").prefetch_related(
            "depreciations__journal_entry", "impairments__journal_entry", "impairment_reversals__journal_entry"
        )
        rows = []
        total_cost = total_accumulated = total_impairment = total_book = 0
        for asset in assets:
            accumulated = min(sum((
                dep.amount for dep in asset.depreciations.all()
                if dep.period_date <= as_of and dep.journal_entry.status == JournalEntry.Status.POSTED
            ), Decimal("0")), asset.depreciable_amount)
            impairment_total = sum((
                item.impairment_loss for item in asset.impairments.all()
                if item.impairment_date <= as_of and item.journal_entry.status == JournalEntry.Status.POSTED
            ), Decimal("0"))
            reversal_total = sum((
                item.reversal_amount for item in asset.impairment_reversals.all()
                if item.reversal_date <= as_of and item.journal_entry.status == JournalEntry.Status.POSTED
            ), Decimal("0"))
            net_impairment = impairment_total - reversal_total
            book_value = asset.original_cost - accumulated - net_impairment
            disposed = bool(asset.disposed_at and asset.disposed_at <= as_of)
            rows.append({
                "asset_id": asset.pk, "code": asset.code, "name_ar": asset.name_ar,
                "branch_id": asset.branch_id, "branch_name": asset.branch.name_ar,
                "acquisition_date": asset.acquisition_date, "original_cost": asset.original_cost,
                "salvage_value": asset.salvage_value, "useful_life_months": asset.useful_life_months,
                "accumulated_depreciation": accumulated, "accumulated_impairment": net_impairment,
                "net_book_value": book_value,
                "status": "disposed" if disposed else "active", "disposed_at": asset.disposed_at,
                "acquisition_journal": asset.acquisition_journal_id, "disposal_journal": asset.disposal_journal_id,
            })
            total_cost += asset.original_cost
            total_accumulated += accumulated
            total_impairment += net_impairment
            total_book += book_value if not disposed else 0
        return response.Response({
            "company": int(request.query_params["company"]), "as_of": as_of,
            "totals": {"original_cost": total_cost, "accumulated_depreciation": total_accumulated,
                       "accumulated_impairment": total_impairment,
                       "net_book_value_active": total_book, "asset_count": len(rows)},
            "rows": rows,
        })

    @decorators.action(detail=False, methods=("get",), url_path="depreciation-report")
    def depreciation_report(self, request):
        """إعداد تقرير الإهلاك للفترة والأصول المحددة."""
        assets = self._report_assets(request)
        date_from_raw, date_to_raw = request.query_params.get("date_from"), request.query_params.get("date_to")
        date_from = dateparse.parse_date(date_from_raw) if date_from_raw else None
        date_to = dateparse.parse_date(date_to_raw) if date_to_raw else None
        if date_from_raw and not date_from:
            raise ValidationError({"date_from": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_to_raw and not date_to:
            raise ValidationError({"date_to": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_from and date_to and date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        rows = AssetDepreciation.objects.filter(asset__in=assets).select_related(
            "asset", "asset__branch", "journal_entry"
        ).order_by("period_date", "asset__code")
        if date_from:
            rows = rows.filter(period_date__gte=date_from)
        if date_to:
            rows = rows.filter(period_date__lte=date_to)
        details = [{
            "asset_id": row.asset_id, "asset_code": row.asset.code, "asset_name": row.asset.name_ar,
            "branch_name": row.asset.branch.name_ar, "period_date": row.period_date,
            "amount": row.amount, "is_partial_period": row.is_partial_period,
            "journal_entry": row.journal_entry_id,
        } for row in rows]
        total = sum((row["amount"] for row in details), start=0)
        branch_totals = {}
        for row in details:
            key = row["branch_name"]
            branch_totals[key] = branch_totals.get(key, 0) + row["amount"]
        return response.Response({"company": int(request.query_params["company"]),
            "date_from": date_from, "date_to": date_to, "total_depreciation": total,
            "branch_totals": [{"branch_name": key, "amount": value} for key, value in sorted(branch_totals.items())],
            "rows": details})

    @decorators.action(detail=False, methods=("get",), url_path="movement-report")
    def movement_report(self, request):
        """إعداد تقرير حركة الأصول والإضافات والاستبعادات."""
        assets = self._report_assets(request).filter(acquisition_journal__status=JournalEntry.Status.POSTED).select_related("branch", "acquisition_journal", "disposal_journal").prefetch_related("depreciations__journal_entry", "impairments__journal_entry", "impairment_reversals__journal_entry")
        asset_id = request.query_params.get("asset")
        if asset_id:
            if not asset_id.isdigit():
                raise ValidationError({"asset": "معرف الأصل غير صحيح."})
            assets = assets.filter(pk=int(asset_id))
        date_from_raw, date_to_raw = request.query_params.get("date_from"), request.query_params.get("date_to")
        date_from = dateparse.parse_date(date_from_raw) if date_from_raw else None
        date_to = dateparse.parse_date(date_to_raw) if date_to_raw else None
        if date_from_raw and not date_from:
            raise ValidationError({"date_from": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_to_raw and not date_to:
            raise ValidationError({"date_to": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_from and date_to and date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        events = []
        for asset in assets:
            candidates = [{
                "date": asset.acquisition_date, "event_type": "acquisition", "event_label": "اقتناء الأصل",
                "amount": asset.original_cost, "journal_entry": asset.acquisition_journal_id,
                "treasury_transaction": asset.acquisition_treasury_transaction_id,
            }]
            for dep in asset.depreciations.all():
                if dep.journal_entry.status == JournalEntry.Status.POSTED:
                    candidates.append({
                        "date": dep.period_date, "event_type": "depreciation", "event_label": "إهلاك",
                        "amount": dep.amount, "journal_entry": dep.journal_entry_id, "treasury_transaction": None,
                    })
            for impairment in asset.impairments.all():
                if impairment.journal_entry.status == JournalEntry.Status.POSTED:
                    candidates.append({
                        "date": impairment.impairment_date, "event_type": "impairment",
                        "event_label": "انخفاض قيمة", "amount": impairment.impairment_loss,
                        "journal_entry": impairment.journal_entry_id, "treasury_transaction": None,
                    })
            for reversal in asset.impairment_reversals.all():
                if reversal.journal_entry.status == JournalEntry.Status.POSTED:
                    candidates.append({
                        "date": reversal.reversal_date, "event_type": "impairment_reversal",
                        "event_label": "عكس انخفاض قيمة", "amount": reversal.reversal_amount,
                        "journal_entry": reversal.journal_entry_id, "treasury_transaction": None,
                    })
            if asset.disposed_at:
                candidates.append({
                    "date": asset.disposed_at, "event_type": "disposal", "event_label": "بيع / استبعاد",
                    "amount": asset.disposal_proceeds or 0, "journal_entry": asset.disposal_journal_id,
                    "treasury_transaction": asset.disposal_treasury_transaction_id,
                })
            for event in candidates:
                if date_from and event["date"] < date_from:
                    continue
                if date_to and event["date"] > date_to:
                    continue
                events.append({"asset_id": asset.pk, "asset_code": asset.code,
                    "asset_name": asset.name_ar, "branch_name": asset.branch.name_ar, **event})
        events.sort(key=lambda item: (item["date"], item["asset_code"], item["event_type"]))
        return response.Response({"company": int(request.query_params["company"]),
            "date_from": date_from, "date_to": date_to, "rows": events})


    @decorators.action(detail=False, methods=("post",), url_path="depreciation-run")
    def depreciation_run(self, request):
        """تشغيل الإهلاك الدوري للأصول النشطة دفعة واحدة مع بيان ما تم وما تعذر."""
        assets = self._report_assets(request).filter(
            status=FixedAsset.Status.ACTIVE,
            acquisition_journal__status=JournalEntry.Status.POSTED,
        ).select_related("company")
        self._write(int(request.query_params["company"]))
        period_date = dateparse.parse_date(request.data.get("period_date", ""))
        fiscal_year_id = request.data.get("fiscal_year")
        if not period_date:
            raise ValidationError({"period_date": "أدخل تاريخ فترة صحيحًا."})
        try:
            from companies.models import FiscalYear
            year = FiscalYear.objects.get(
                pk=fiscal_year_id, company_id=request.query_params["company"],
                status=FiscalYear.Status.OPEN,
            )
        except (FiscalYear.DoesNotExist, TypeError, ValueError):
            raise ValidationError({"fiscal_year": "حدد سنة مالية مفتوحة وصحيحة."}) from None
        if not (year.start_date <= period_date <= year.end_date):
            raise ValidationError({"period_date": "تاريخ الإهلاك خارج السنة المالية المحددة."})
        processed, skipped = [], []
        for asset in assets.filter(depreciation_start_date__lte=period_date).order_by("code"):
            if asset.depreciations.filter(
                period_date__year=period_date.year, period_date__month=period_date.month
            ).exists():
                skipped.append({"asset_id": asset.pk, "code": asset.code, "reason": "الفترة مسجلة مسبقًا"})
                continue
            try:
                amount = depreciate_asset(asset.pk, period_date, year.pk, request.user)
                processed.append({"asset_id": asset.pk, "code": asset.code, "amount": str(amount)})
            except DjangoValidationError as exc:
                detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
                skipped.append({"asset_id": asset.pk, "code": asset.code, "reason": detail})
        return response.Response({
            "period_date": period_date, "fiscal_year": year.pk,
            "processed_count": len(processed), "processed_total": str(sum((Decimal(row["amount"]) for row in processed), Decimal("0"))),
            "skipped_count": len(skipped), "processed": processed, "skipped": skipped,
            "note": "تم ترحيل القيود لكل أصل على حدة. راجع قائمة المتعذر قبل اعتماد إقفال الفترة.",
        }, status=status.HTTP_200_OK)

    @decorators.action(detail=False, methods=("get",), url_path="account-reconciliation")
    def account_reconciliation(self, request):
        """مطابقة تكلفة الأصل ومجمع الإهلاك والانخفاض ومصروفاتهما مع الأستاذ العام."""
        from collections import defaultdict
        from decimal import Decimal

        as_of_raw = request.query_params.get("as_of")
        as_of = dateparse.parse_date(as_of_raw) if as_of_raw else timezone.localdate()
        assets = self._report_assets(request).select_related(
            "asset_account", "accumulated_depreciation_account", "depreciation_expense_account"
        ).prefetch_related(
            "depreciations__journal_entry",
            "impairments__journal_entry",
            "impairments__impairment_expense_account",
            "impairments__accumulated_impairment_account",
            "impairments__reversal_allocations__reversal__journal_entry",
            "impairment_reversals__journal_entry",
            "impairment_reversals__reversal_income_account",
        )
        date_from_raw, date_to_raw = request.query_params.get("date_from"), request.query_params.get("date_to")
        date_from = dateparse.parse_date(date_from_raw) if date_from_raw else (as_of.replace(month=1, day=1) if as_of else None)
        date_to = dateparse.parse_date(date_to_raw) if date_to_raw else as_of
        if not as_of or not date_from or not date_to or date_from > date_to:
            raise ValidationError("حدد تاريخ مطابقة وفترة إهلاك صحيحة.")
        assets = assets.filter(acquisition_date__lte=as_of, acquisition_journal__status=JournalEntry.Status.POSTED)
        company_id = int(request.query_params["company"])
        asset_journal_ids = set()
        expected_cost = defaultdict(Decimal)
        expected_accum = defaultdict(Decimal)
        expected_impairment = defaultdict(Decimal)
        expected_depreciation_expense = defaultdict(Decimal)
        expected_impairment_expense = defaultdict(Decimal)
        expected_impairment_reversal_income = defaultdict(Decimal)
        account_objects = {}
        for asset in assets:
            account_objects[asset.asset_account_id] = asset.asset_account
            account_objects[asset.accumulated_depreciation_account_id] = asset.accumulated_depreciation_account
            account_objects[asset.depreciation_expense_account_id] = asset.depreciation_expense_account
            if asset.acquisition_journal_id:
                asset_journal_ids.add(asset.acquisition_journal_id)
            if asset.disposal_journal_id:
                asset_journal_ids.add(asset.disposal_journal_id)
            disposed_by_date = bool(asset.disposed_at and asset.disposed_at <= as_of)
            if not disposed_by_date:
                expected_cost[asset.asset_account_id] += asset.original_cost
                expected_accum[asset.accumulated_depreciation_account_id] += sum(
                    (dep.amount for dep in asset.depreciations.all()
                     if dep.period_date <= as_of and dep.journal_entry.status == JournalEntry.Status.POSTED),
                    Decimal("0"),
                )
            for dep in asset.depreciations.all():
                if dep.journal_entry.status == JournalEntry.Status.POSTED:
                    asset_journal_ids.add(dep.journal_entry_id)
                    if date_from <= dep.period_date <= date_to:
                        expected_depreciation_expense[asset.depreciation_expense_account_id] += dep.amount
            for impairment in asset.impairments.all():
                if impairment.journal_entry.status == JournalEntry.Status.POSTED:
                    asset_journal_ids.add(impairment.journal_entry_id)
                account_objects[impairment.accumulated_impairment_account_id] = impairment.accumulated_impairment_account
                account_objects[impairment.impairment_expense_account_id] = impairment.impairment_expense_account
                if not disposed_by_date and impairment.impairment_date <= as_of and impairment.journal_entry.status == JournalEntry.Status.POSTED:
                    reversed_amount = impairment.reversal_allocations.filter(
                        reversal__reversal_date__lte=as_of,
                        reversal__journal_entry__status=JournalEntry.Status.POSTED,
                    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
                    expected_impairment[impairment.accumulated_impairment_account_id] += impairment.impairment_loss - reversed_amount
                if date_from <= impairment.impairment_date <= date_to and impairment.journal_entry.status == JournalEntry.Status.POSTED:
                    expected_impairment_expense[impairment.impairment_expense_account_id] += impairment.impairment_loss
            for reversal in asset.impairment_reversals.all():
                if reversal.journal_entry.status == JournalEntry.Status.POSTED:
                    asset_journal_ids.add(reversal.journal_entry_id)
                account_objects[reversal.reversal_income_account_id] = reversal.reversal_income_account
                if date_from <= reversal.reversal_date <= date_to and reversal.journal_entry.status == JournalEntry.Status.POSTED:
                    expected_impairment_reversal_income[reversal.reversal_income_account_id] += reversal.reversal_amount

        reversal_journal_ids = list(AssetImpairmentReversal.objects.filter(
            asset__in=assets, reversal_date__gte=date_from, reversal_date__lte=date_to,
            journal_entry__status=JournalEntry.Status.POSTED,
        ).values_list("journal_entry_id", flat=True))
        account_ids = list(account_objects)
        ledger = JournalLine.objects.filter(
            entry_id__in=asset_journal_ids, entry__company_id=company_id,
            entry__status=JournalEntry.Status.POSTED, entry__entry_date__lte=as_of,
            account_id__in=account_ids,
        ).values("account_id").annotate(debit=Sum("debit"), credit=Sum("credit"))
        actual = {row["account_id"]: row for row in ledger}
        cost_rows, accum_rows, impairment_rows, expense_rows, reversal_income_rows = [], [], [], [], []
        for account_id, account in account_objects.items():
            row = actual.get(account_id, {})
            debit = row.get("debit") or Decimal("0")
            credit = row.get("credit") or Decimal("0")
            if any(asset.asset_account_id == account_id for asset in assets):
                expected = expected_cost[account_id]
                actual_value = debit - credit
                cost_rows.append({"account_id": account_id, "code": account.code, "name": account.name_ar,
                                  "expected": str(expected), "actual": str(actual_value), "difference": str(expected - actual_value)})
            if any(asset.accumulated_depreciation_account_id == account_id for asset in assets):
                expected = expected_accum[account_id]
                actual_value = credit - debit
                accum_rows.append({"account_id": account_id, "code": account.code, "name": account.name_ar,
                                   "expected": str(expected), "actual": str(actual_value), "difference": str(expected - actual_value)})
            if expected_impairment[account_id] or any(
                item.accumulated_impairment_account_id == account_id
                for asset in assets for item in asset.impairments.all()
            ):
                expected = expected_impairment[account_id]
                actual_value = credit - debit
                impairment_rows.append({"account_id": account_id, "code": account.code, "name": account.name_ar,
                                        "expected": str(expected), "actual": str(actual_value), "difference": str(expected - actual_value)})
            if expected_depreciation_expense[account_id] or expected_impairment_expense[account_id] or any(
                asset.depreciation_expense_account_id == account_id for asset in assets
            ) or any(
                item.impairment_expense_account_id == account_id
                for asset in assets for item in asset.impairments.all()
            ):
                expense_lines = JournalLine.objects.filter(
                    entry_id__in=asset_journal_ids, entry__company_id=company_id,
                    entry__status=JournalEntry.Status.POSTED,
                    entry__entry_date__gte=date_from, entry__entry_date__lte=date_to,
                    account_id=account_id,
                ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
                expected_dep = expected_depreciation_expense[account_id]
                expected_imp = expected_impairment_expense[account_id]
                expected = expected_dep + expected_imp
                actual_value = (expense_lines["debit"] or Decimal("0")) - (expense_lines["credit"] or Decimal("0"))
                expense_rows.append({
                    "account_id": account_id, "code": account.code, "name": account.name_ar,
                    "expected_depreciation": str(expected_dep), "expected_impairment": str(expected_imp),
                    "expected": str(expected), "actual": str(actual_value), "difference": str(expected - actual_value),
                })
            if expected_impairment_reversal_income[account_id]:
                income_lines = JournalLine.objects.filter(
                    entry_id__in=reversal_journal_ids, account_id=account_id,
                ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
                expected = expected_impairment_reversal_income[account_id]
                actual_value = (income_lines["credit"] or Decimal("0")) - (income_lines["debit"] or Decimal("0"))
                reversal_income_rows.append({
                    "account_id": account_id, "code": account.code, "name": account.name_ar,
                    "expected_reversal_income": str(expected), "actual": str(actual_value),
                    "difference": str(expected - actual_value),
                })
        all_rows = cost_rows + accum_rows + impairment_rows + expense_rows + reversal_income_rows
        return response.Response({
            "company": company_id, "as_of": as_of, "date_from": date_from, "date_to": date_to,
            "cost_accounts": cost_rows, "accumulated_depreciation_accounts": accum_rows,
            "accumulated_impairment_accounts": impairment_rows,
            "asset_expense_accounts": expense_rows,
            "depreciation_expense_accounts": expense_rows,
            "impairment_reversal_income_accounts": reversal_income_rows,
            "is_reconciled": all(Decimal(row["difference"]) == 0 for row in all_rows),
            "difference_count": sum(1 for row in all_rows if Decimal(row["difference"]) != 0),
            "note": "المطابقة تربط قيود كل أصل بسجله عبر جميع الفروع التي تحرك بينها؛ لذلك لا تستبعد قيد الاقتناء أو الإهلاك التاريخي لمجرد انتقال الأصل إلى فرع آخر. هذه مطابقة سجل الأصول مع الأستاذ وليست ميزان مراجعة مستقلًا لكل فرع.",
        })

    @decorators.action(detail=True, methods=("post",))
    def dispose(self, request, pk=None):
        """تسجيل بيع الأصل أو استبعاده ومعالجة أثره المحاسبي."""
        asset = self.get_object()
        self._write(asset.company_id)
        if not can_access_branch(request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل.")
        try:
            result = dispose_asset(
                asset.pk, request.data.get("disposal_date"), request.data.get("proceeds", "0"),
                request.data.get("treasury_account"), request.data.get("fiscal_year"), request.user,
                request.data.get("units_produced"),
                request.data.get("units_produced_by_component"),
            )
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response(result, status=status.HTTP_201_CREATED)


    @decorators.action(detail=True, methods=("post",), url_path="purchase-invoice")
    @transaction.atomic
    def purchase_invoice(self, request, pk=None):
        """إثبات فاتورة مورد مباشرة للأصل دون المرور بدورة استلام المخزون."""
        from suppliers.models import Supplier
        from core.money import round_currency_amount

        asset = self.get_object()
        self._write(asset.company_id)
        if not can_access_branch(request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل.")
        if asset.acquisition_journal_id or asset.status != FixedAsset.Status.ACTIVE:
            raise ValidationError("يجب أن يكون الأصل غير مثبت محاسبيًا قبل ربط فاتورة المورد.")
        if hasattr(asset, "vendor_invoice"):
            raise ValidationError("تم ربط فاتورة مورد بهذا الأصل بالفعل.")
        try:
            supplier = Supplier.objects.get(
                pk=request.data.get("supplier"), company_id=asset.company_id, is_active=True,
            )
            from companies.models import FiscalYear
            year = FiscalYear.objects.get(
                pk=request.data.get("fiscal_year"), company_id=asset.company_id,
                status=FiscalYear.Status.OPEN,
            )
            invoice_date = dateparse.parse_date(str(request.data.get("invoice_date", "")))
            net_amount = Decimal(str(request.data.get("net_amount", "")))
            if not invoice_date or net_amount <= 0:
                raise ValueError("تاريخ الفاتورة أو صافي قيمتها غير صحيح.")
            if not (year.start_date <= invoice_date <= year.end_date):
                raise ValueError("تاريخ الفاتورة خارج السنة المالية المفتوحة.")
            tax_code = None
            tax_amount = recoverable = nonrecoverable = Decimal("0")
            tax_code_id = request.data.get("tax_code")
            if tax_code_id not in (None, ""):
                from accounting.models import TaxCode
                tax_code = TaxCode.objects.get(pk=tax_code_id, company_id=asset.company_id)
                if (
                    not tax_code.is_active or tax_code.country != asset.company.country
                    or tax_code.category == TaxCode.Category.REVERSE_CHARGE
                    or tax_code.effective_from > invoice_date
                    or (tax_code.effective_to and tax_code.effective_to < invoice_date)
                ):
                    raise ValueError("كود الضريبة غير صالح لتاريخ الفاتورة.")
                tax_amount = round_currency_amount(
                    net_amount * tax_code.rate / Decimal("100"), asset.company.base_currency,
                )
                if tax_code.input_tax_recoverable:
                    recoverable = tax_amount
                else:
                    nonrecoverable = tax_amount
            grand_total = net_amount + tax_amount
            capitalized_cost = net_amount + nonrecoverable
            if asset.salvage_value > capitalized_cost:
                raise ValueError("القيمة التخريدية تتجاوز تكلفة الأصل بعد الضريبة غير القابلة للاسترداد.")
            invoice = AssetPurchaseInvoice(
                company=asset.company, branch=asset.branch, asset=asset, supplier=supplier,
                fiscal_year=year, invoice_number=str(request.data.get("invoice_number", "")).strip(),
                invoice_date=invoice_date, net_amount=net_amount, tax_code=tax_code,
                tax_amount=tax_amount, recoverable_tax_amount=recoverable,
                nonrecoverable_tax_amount=nonrecoverable, grand_total=grand_total,
                created_by=request.user,
            )
            if not invoice.invoice_number:
                raise ValueError("رقم فاتورة المورد مطلوب.")
            invoice.full_clean()
            invoice.save()
            # Normalize DecimalField values from the database before taking the
            # immutable approval snapshot (e.g. 0 becomes 0.0000).
            invoice.refresh_from_db()
            from approvals.models import FinancialApprovalRequest
            from approvals.services import refresh_financial_approval
            approval = refresh_financial_approval(
                FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE, invoice, request.user,
            )
            if approval:
                return response.Response({
                    "id": invoice.pk, "asset": asset.pk, "supplier": supplier.pk,
                    "invoice_number": invoice.invoice_number, "status": invoice.status,
                    "approval_required": True, "approval_request": approval.pk,
                    "grand_total": invoice.grand_total,
                }, status=status.HTTP_201_CREATED)
            invoice = post_asset_purchase_invoice(invoice.pk, request.user)
            asset.refresh_from_db()
        except (ObjectDoesNotExist, ValueError, TypeError, InvalidOperation):
            logger.exception("Asset purchase invoice request failed validation or posting.")
            raise ValidationError(
                "تعذر معالجة فاتورة شراء الأصل. راجع البيانات والمراجع المرتبطة ثم أعد المحاولة."
            ) from None
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from None
        return response.Response({
            "id": invoice.pk, "asset": asset.pk, "supplier": supplier.pk,
            "supplier_name": supplier.name_ar, "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date, "net_amount": invoice.net_amount,
            "tax_amount": invoice.tax_amount, "recoverable_tax_amount": invoice.recoverable_tax_amount,
            "nonrecoverable_tax_amount": invoice.nonrecoverable_tax_amount,
            "grand_total": invoice.grand_total, "status": invoice.status,
            "journal_entry": invoice.journal_entry_id,
            "asset_cost": asset.original_cost,
        }, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("post",), url_path="supplier-payment")
    @transaction.atomic
    def supplier_payment(self, request, pk=None):
        """إنشاء وترحيل سند سداد مخصص لفاتورة شراء الأصل."""
        from django.utils.dateparse import parse_date

        asset = self.get_object()
        self._write(asset.company_id)
        if not can_access_branch(request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل.")
        try:
            invoice = AssetPurchaseInvoice.objects.select_related("supplier").get(
                asset=asset, company=asset.company, status=AssetPurchaseInvoice.Status.POSTED,
            )
            payment_date = parse_date(str(request.data.get("payment_date", "")))
            amount = Decimal(str(request.data.get("amount", "")))
            account = ChartOfAccount.objects.get(
                pk=request.data.get("payment_account"), company=asset.company,
            )
            payment = AssetSupplierPayment(
                company=asset.company, branch=asset.branch, invoice=invoice,
                supplier=invoice.supplier, payment_number=str(request.data.get("payment_number", "")).strip(),
                payment_date=payment_date, payment_account=account, amount=amount,
                reference=str(request.data.get("reference", ""))[:120], created_by=request.user,
            )
            if not payment.payment_number or not payment.payment_date:
                raise ValueError("رقم السند وتاريخ السداد مطلوبان.")
            payment.full_clean()
            payment.save()
            from approvals.models import FinancialApprovalRequest
            from approvals.services import refresh_financial_approval
            approval = refresh_financial_approval(
                FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT, payment, request.user,
            )
            if approval:
                return response.Response({
                    "id": payment.pk, "invoice": invoice.pk, "payment_number": payment.payment_number,
                    "payment_date": payment.payment_date, "amount": payment.amount,
                    "status": payment.status, "approval_required": True,
                    "approval_request": approval.pk,
                }, status=status.HTTP_201_CREATED)
            payment = post_asset_supplier_payment(payment.pk, request.user)
        except (ObjectDoesNotExist, ValueError, TypeError, InvalidOperation):
            logger.exception("Asset supplier payment request failed validation or posting.")
            raise ValidationError(
                "تعذر معالجة سند سداد الأصل. راجع البيانات والمراجع المرتبطة ثم أعد المحاولة."
            ) from None
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from None
        return response.Response({
            "id": payment.pk, "invoice": invoice.pk, "payment_number": payment.payment_number,
            "payment_date": payment.payment_date, "amount": payment.amount,
            "status": payment.status, "journal_entry": payment.journal_entry_id,
        }, status=status.HTTP_201_CREATED)


    @decorators.action(detail=True, methods=("post",), url_path="post-purchase-invoice")
    def post_purchase_invoice(self, request, pk=None):
        """ترحيل فاتورة الأصل بعد استكمال اعتمادها المالي عند اللزوم."""
        asset = self.get_object()
        self._write(asset.company_id)
        try:
            invoice = AssetPurchaseInvoice.objects.get(asset=asset, status=AssetPurchaseInvoice.Status.DRAFT)
            invoice = post_asset_purchase_invoice(invoice.pk, request.user)
        except AssetPurchaseInvoice.DoesNotExist as exc:
            raise ValidationError("لا توجد فاتورة أصل مسودة لهذا الأصل.") from exc
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response({
            "id": invoice.pk, "status": invoice.status,
            "journal_entry": invoice.journal_entry_id, "grand_total": invoice.grand_total,
        }, status=status.HTTP_200_OK)

    @decorators.action(detail=True, methods=("post",), url_path="post-supplier-payment")
    def post_supplier_payment(self, request, pk=None):
        """ترحيل سند سداد الأصل بعد استكمال اعتماده المالي عند اللزوم."""
        asset = self.get_object()
        self._write(asset.company_id)
        try:
            payment = AssetSupplierPayment.objects.get(
                invoice__asset=asset, status=AssetSupplierPayment.Status.DRAFT,
                pk=request.data.get("payment"),
            )
            payment = post_asset_supplier_payment(payment.pk, request.user)
        except AssetSupplierPayment.DoesNotExist as exc:
            raise ValidationError("سند السداد المسودة غير موجود لهذا الأصل.") from exc
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response({
            "id": payment.pk, "status": payment.status,
            "journal_entry": payment.journal_entry_id, "amount": payment.amount,
        }, status=status.HTTP_200_OK)
