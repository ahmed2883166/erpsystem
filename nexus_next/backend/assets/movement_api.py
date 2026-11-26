"""واجهات مستندات نقل الأصول وتغيير العهدة والموقع مع اعتماد مستقل."""

from django.db import transaction
from django.utils import timezone
from rest_framework import decorators, mixins, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework import serializers

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from companies.models import Branch
from hr.models import Employee
from .models import AssetMovement, FixedAsset


class AssetMovementSerializer(serializers.ModelSerializer):
    asset_code = serializers.CharField(source="asset.code", read_only=True)
    asset_name = serializers.CharField(source="asset.name_ar", read_only=True)
    from_branch_name = serializers.CharField(source="from_branch.name_ar", read_only=True)
    to_branch_name = serializers.CharField(source="to_branch.name_ar", read_only=True)
    from_employee_name = serializers.CharField(source="from_employee.full_name_ar", read_only=True, allow_null=True)
    to_employee_name = serializers.CharField(source="to_employee.full_name_ar", read_only=True, allow_null=True)
    requested_by_name = serializers.CharField(source="requested_by.username", read_only=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.username", read_only=True, allow_null=True)

    class Meta:
        model = AssetMovement
        fields = (
            "id", "company", "asset", "asset_code", "asset_name", "movement_type",
            "movement_date", "from_branch", "from_branch_name", "to_branch",
            "to_branch_name", "from_employee", "from_employee_name", "to_employee",
            "to_employee_name", "from_custodian", "to_custodian", "from_location",
            "to_location", "document_number", "notes", "status", "requested_by",
            "requested_by_name", "reviewed_by", "reviewed_by_name", "reviewed_at",
            "review_note", "created_at",
        )
        read_only_fields = (
            "company", "from_branch", "from_employee", "from_custodian",
            "from_location", "status", "requested_by", "reviewed_by",
            "reviewed_at", "review_note", "created_at",
        )
        extra_kwargs = {
            "asset": {"queryset": FixedAsset.objects.all()},
            "to_branch": {"queryset": Branch.objects.all(), "required": False, "allow_null": True},
            "to_employee": {"queryset": Employee.objects.all(), "required": False, "allow_null": True},
            "to_custodian": {"required": False, "allow_blank": True},
            "to_location": {"required": False, "allow_blank": True},
            "notes": {"required": False, "allow_blank": True},
        }

    def validate(self, attrs):
        asset = attrs["asset"]
        movement_type = attrs["movement_type"]
        target_branch = attrs.get("to_branch") or asset.branch
        employee = attrs.get("to_employee")
        if asset.status != FixedAsset.Status.ACTIVE:
            raise serializers.ValidationError({"asset": "لا يمكن نقل أو تغيير عهدة أصل مستبعد."})
        if target_branch.company_id != asset.company_id:
            raise serializers.ValidationError({"to_branch": "الفرع المستلم يجب أن يتبع نفس الشركة."})
        if movement_type == AssetMovement.MovementType.BRANCH_TRANSFER:
            if target_branch.pk == asset.branch_id:
                raise serializers.ValidationError({"to_branch": "اختر فرعًا مختلفًا عن الفرع الحالي."})
            if (asset.custodian_employee_id or asset.custodian.strip()) and not (
                employee or str(attrs.get("to_custodian", "")).strip()
            ):
                raise serializers.ValidationError({"to_employee": "حدد الموظف أو المسؤول المستلم عند نقل أصل له عهدة حالية."})
        elif target_branch.pk != asset.branch_id:
            raise serializers.ValidationError({"to_branch": "تغيير العهدة أو الموقع لا يغيّر فرع الأصل."})
        if employee:
            if employee.company_id != asset.company_id or employee.branch_id != target_branch.pk:
                raise serializers.ValidationError({"to_employee": "الموظف المستلم يجب أن يتبع نفس الشركة والفرع المستلم."})
            if employee.status != Employee.Status.ACTIVE:
                raise serializers.ValidationError({"to_employee": "لا يمكن تسليم عهدة لموظف غير نشط."})
        if attrs["movement_date"] < asset.acquisition_date:
            raise serializers.ValidationError({"movement_date": "تاريخ الحركة لا يسبق تاريخ اقتناء الأصل."})
        if not str(attrs.get("document_number", "")).strip():
            raise serializers.ValidationError({"document_number": "رقم مستند الحركة مطلوب."})
        attrs["to_branch"] = target_branch
        return attrs


class AssetMovementViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = AssetMovementSerializer
    http_method_names = ("get", "post", "head", "options")

    def get_queryset(self):
        branches = accessible_branches(self.request.user)
        qs = AssetMovement.objects.filter(
            company__in=accessible_companies(self.request.user),
            from_branch__in=branches, to_branch__in=branches,
        ).select_related(
            "company", "asset", "from_branch", "to_branch", "from_employee",
            "to_employee", "requested_by", "reviewed_by",
        )
        company_id = self.request.query_params.get("company")
        if company_id:
            if not company_id.isdigit() or not can_access_company(self.request.user, int(company_id)):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
            qs = qs.filter(company_id=int(company_id))
        asset_id = self.request.query_params.get("asset")
        if asset_id:
            if not asset_id.isdigit():
                raise ValidationError({"asset": "معرّف الأصل غير صالح."})
            qs = qs.filter(asset_id=int(asset_id))
        return qs

    def _write(self, company_id):
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")

    def _review(self, company_id):
        if not has_company_role(self.request.user, company_id, ("admin",)):
            raise PermissionDenied("اعتماد أو رفض حركة الأصل متاح لمدير الشركة فقط.")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        asset = data["asset"]
        target_branch = data["to_branch"]
        self._write(asset.company_id)
        if not can_access_branch(request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل الحالي.")
        if not can_access_branch(request.user, target_branch.pk, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المستلم.")
        movement = AssetMovement(
            company=asset.company, asset=asset, movement_type=data["movement_type"],
            movement_date=data["movement_date"], from_branch=asset.branch,
            to_branch=target_branch, from_employee=asset.custodian_employee,
            to_employee=data.get("to_employee"), from_custodian=asset.custodian,
            to_custodian=data.get("to_custodian", ""),
            from_location=asset.location, to_location=data.get("to_location", ""),
            document_number=data["document_number"].strip(), notes=data.get("notes", ""),
            requested_by=request.user,
        )
        movement.full_clean()
        movement.save()
        return response.Response(self.get_serializer(movement).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("post",))
    def approve(self, request, pk=None):
        movement = self.get_object()
        self._review(movement.company_id)
        with transaction.atomic():
            movement = AssetMovement.objects.select_for_update().select_related(
                "asset", "to_branch", "to_employee",
            ).get(pk=movement.pk)
            if movement.status != AssetMovement.Status.PENDING:
                raise ValidationError("مستند الحركة تمت مراجعته بالفعل.")
            asset = FixedAsset.objects.select_for_update().get(pk=movement.asset_id)
            if (
                asset.branch_id != movement.from_branch_id
                or asset.custodian_employee_id != movement.from_employee_id
                or asset.custodian != movement.from_custodian
                or asset.location != movement.from_location
            ):
                raise ValidationError("بيانات الأصل تغيرت بعد إنشاء المستند؛ ألغِ الطلب وأنشئ مستندًا جديدًا.")
            if not can_access_branch(request.user, movement.from_branch_id, movement.company_id) or not can_access_branch(request.user, movement.to_branch_id, movement.company_id):
                raise PermissionDenied("لا تملك صلاحية اعتماد الحركة على الفرعين.")
            if movement.movement_type == AssetMovement.MovementType.BRANCH_TRANSFER:
                asset.branch = movement.to_branch
                asset.custodian_employee = movement.to_employee
                asset.custodian = movement.to_employee.full_name_ar if movement.to_employee_id else movement.to_custodian
            elif movement.movement_type == AssetMovement.MovementType.CUSTODY_CHANGE:
                asset.custodian_employee = movement.to_employee
                asset.custodian = movement.to_employee.full_name_ar if movement.to_employee_id else movement.to_custodian
            elif movement.movement_type == AssetMovement.MovementType.LOCATION_CHANGE:
                asset.location = movement.to_location
            asset.save(update_fields=("branch", "custodian_employee", "custodian", "location"))
            movement.status = AssetMovement.Status.APPROVED
            movement.reviewed_by = request.user
            movement.reviewed_at = timezone.now()
            movement.review_note = str(request.data.get("review_note", "")).strip()[:500]
            movement.save(update_fields=("status", "reviewed_by", "reviewed_at", "review_note"))
        return response.Response(self.get_serializer(movement).data)

    @decorators.action(detail=True, methods=("post",))
    def reject(self, request, pk=None):
        movement = self.get_object()
        self._review(movement.company_id)
        if movement.status != AssetMovement.Status.PENDING:
            raise ValidationError("مستند الحركة تمت مراجعته بالفعل.")
        note = str(request.data.get("review_note", "")).strip()
        if not note:
            raise ValidationError({"review_note": "سبب رفض الحركة مطلوب."})
        movement.status = AssetMovement.Status.REJECTED
        movement.reviewed_by = request.user
        movement.reviewed_at = timezone.now()
        movement.review_note = note[:500]
        movement.save(update_fields=("status", "reviewed_by", "reviewed_at", "review_note"))
        return response.Response(self.get_serializer(movement).data)
