from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.utils import timezone, dateparse

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from .models import FixedAsset, AssetDepreciation
from .serializers import FixedAssetSerializer, AssetDepreciationSerializer
from .services import acquire_asset, depreciate_asset, dispose_asset


class FixedAssetViewSet(viewsets.ModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = FixedAssetSerializer
    queryset = FixedAsset.objects.select_related("company", "branch", "asset_account",
        "accumulated_depreciation_account", "depreciation_expense_account",
        "acquisition_counter_account", "disposal_gain_loss_account", "disposal_loss_account", "acquisition_journal, "acquisition_treasury_transaction", "disposal_treasury_transaction").prefetch_related("depreciations").all()

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        company_id = self.request.query_params.get("company")
        if company_id:
            if not company_id.isdigit() or not can_access_company(self.request.user, int(company_id)):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
            qs = qs.filter(company_id=int(company_id))
        if not self.request.user.is_superuser:
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    def _write(self, company_id):
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")

    def perform_create(self, serializer):
        data = serializer.validated_data
        if not can_access_company(self.request.user, data["company"].pk) or not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول للشركة أو الفرع.")
        self._write(data["company"].pk)
        try:
            with transaction.atomic():
                asset = serializer.save()
                acquire_asset(asset.pk, self.request.data.get("fiscal_year"), self.request.user)
                serializer.instance.refresh_from_db()
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc

    def perform_update(self, serializer):
        if serializer.instance.acquisition_journal_id:
            raise ValidationError("الأصل المثبت محاسبيًا لا يمكن تعديل بيانات تكلفته أو حساباته.")
        self._write(serializer.instance.company_id)
        serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id)
        if instance.acquisition_journal_id or instance.depreciations.exists():
            raise ValidationError("لا يمكن حذف أصل له أثر محاسبي؛ استخدم إجراء الاستبعاد.")
        instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def depreciate(self, request, pk=None):
        asset = self.get_object()
        self._write(asset.company_id)
        try:
            amount = depreciate_asset(asset.pk, request.data.get("period_date"), request.data.get("fiscal_year"), request.user)
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response({"asset_id": asset.pk, "depreciation_amount": amount}, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("get",))
    def schedule(self, request, pk=None):
        asset = self.get_object()
        rows = asset.depreciations.select_related("journal_entry").order_by("period_date")
        accumulated = sum((row.amount for row in rows), start=0)
        return response.Response({
            "asset_id": asset.pk, "original_cost": asset.original_cost,
            "accumulated_depreciation": accumulated, "net_book_value": asset.original_cost - accumulated,
            "rows": AssetDepreciationSerializer(rows, many=True).data,
        })


    def _report_assets(self, request):
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
        assets = self._report_assets(request)
        as_of_raw = request.query_params.get("as_of")
        as_of = dateparse.parse_date(as_of_raw) if as_of_raw else timezone.localdate()
        if not as_of:
            raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        assets = assets.filter(acquisition_date__lte=as_of).annotate(
            reported_accumulated=Sum("depreciations__amount", filter=Q(depreciations__period_date__lte=as_of))
        ).select_related("branch")
        rows = []
        total_cost = total_accumulated = total_book = 0
        for asset in assets:
            accumulated = min(asset.reported_accumulated or 0, asset.depreciable_amount)
            book_value = asset.original_cost - accumulated
            disposed = bool(asset.disposed_at and asset.disposed_at <= as_of)
            rows.append({
                "asset_id": asset.pk, "code": asset.code, "name_ar": asset.name_ar,
                "branch_id": asset.branch_id, "branch_name": asset.branch.name_ar,
                "acquisition_date": asset.acquisition_date, "original_cost": asset.original_cost,
                "salvage_value": asset.salvage_value, "useful_life_months": asset.useful_life_months,
                "accumulated_depreciation": accumulated, "net_book_value": book_value,
                "status": "disposed" if disposed else "active", "disposed_at": asset.disposed_at,
                "acquisition_journal": asset.acquisition_journal_id, "disposal_journal": asset.disposal_journal_id,
            })
            total_cost += asset.original_cost
            total_accumulated += accumulated
            total_book += book_value if not disposed else 0
        return response.Response({
            "company": int(request.query_params["company"]), "as_of": as_of,
            "totals": {"original_cost": total_cost, "accumulated_depreciation": total_accumulated,
                       "net_book_value_active": total_book, "asset_count": len(rows)},
            "rows": rows,
        })

    @decorators.action(detail=False, methods=("get",), url_path="depreciation-report")
    def depreciation_report(self, request):
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
        assets = self._report_assets(request).select_related("branch", "acquisition_journal", "disposal_journal").prefetch_related("depreciations")
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
                candidates.append({
                    "date": dep.period_date, "event_type": "depreciation", "event_label": "إهلاك",
                    "amount": dep.amount, "journal_entry": dep.journal_entry_id, "treasury_transaction": None,
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


    @decorators.action(detail=True, methods=("post",))
    def dispose(self, request, pk=None):
        asset = self.get_object()
        self._write(asset.company_id)
        if not can_access_branch(request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل.")
        try:
            result = dispose_asset(
                asset.pk, request.data.get("disposal_date"), request.data.get("proceeds", "0"),
                request.data.get("treasury_account"), request.data.get("fiscal_year"), request.user,
            )
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response(result, status=status.HTTP_201_CREATED)
