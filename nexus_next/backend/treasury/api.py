from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import decorators, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from .models import TreasuryAccount, TreasuryTransaction
from .serializers import TreasuryAccountSerializer, TreasuryTransactionSerializer
from .services import post_treasury_transaction


class TreasuryBaseViewSet(viewsets.ModelViewSet):
    permission_classes = ()
    def _write(self, company_id):
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")
    def _scope(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")


class TreasuryAccountViewSet(TreasuryBaseViewSet):
    serializer_class = TreasuryAccountSerializer
    queryset = TreasuryAccount.objects.select_related("company", "branch", "account").all()
    filterset_fields = ("company", "branch", "kind", "is_active")
    search_fields = ("code", "name_ar", "bank_name", "account_number")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser:
            qs = qs.filter(branch__isnull=True) | qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    def perform_create(self, serializer):
        company_id = serializer.validated_data["company"].pk
        self._scope(company_id); self._write(company_id); serializer.save()

    def perform_update(self, serializer):
        company_id = serializer.validated_data.get("company", serializer.instance.company).pk
        self._scope(company_id); self._write(company_id); serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id)
        if TreasuryTransaction.objects.filter(source_account=instance).exists() or TreasuryTransaction.objects.filter(destination_account=instance).exists():
            raise ValidationError("لا يمكن حذف خزينة لها حركات؛ أوقفها بدلًا من حذفها.")
        instance.delete()


class TreasuryTransactionViewSet(TreasuryBaseViewSet):
    serializer_class = TreasuryTransactionSerializer
    queryset = TreasuryTransaction.objects.select_related(
        "company", "branch", "fiscal_year", "source_account", "destination_account",
        "counter_account", "journal_entry"
    ).all()
    filterset_fields = ("company", "branch", "kind", "status", "transaction_date")
    search_fields = ("document_number", "description", "reference")
    ordering_fields = ("transaction_date", "document_number", "amount", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser:
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._scope(data["company"].pk); self._write(data["company"].pk)
        if not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.instance
        if instance.status != TreasuryTransaction.Status.DRAFT:
            raise ValidationError("المستند المرحّل غير قابل للتعديل.")
        company = serializer.validated_data.get("company", instance.company)
        self._scope(company.pk); self._write(company.pk); serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id); instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        doc = self.get_object()
        self._write(doc.company_id)
        if not can_access_branch(request.user, doc.branch_id, doc.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع.")
        try:
            doc = post_treasury_transaction(doc.pk, request.user)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return response.Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)
