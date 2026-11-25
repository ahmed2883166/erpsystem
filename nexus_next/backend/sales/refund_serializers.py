from rest_framework import serializers

from companies.access import resolve_warehouse_branch
from sales.models import CustomerRefund, CustomerReceiptBatch, SalesReturn
from treasury.models import TreasuryAccount


class CustomerRefundSerializer(serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    treasury_account_name = serializers.CharField(source="treasury_account.name_ar", read_only=True)
    source_return_number = serializers.CharField(source="source_return.return_number", read_only=True, allow_null=True)
    source_receipt_number = serializers.CharField(
        source="source_receipt_batch.receipt_number", read_only=True, allow_null=True,
    )
    treasury_document_number = serializers.CharField(
        source="treasury_transaction.document_number", read_only=True, allow_null=True,
    )
    journal_number = serializers.CharField(
        source="treasury_transaction.journal_entry.entry_number", read_only=True, allow_null=True,
    )
    reversal_journal_number = serializers.CharField(
        source="reversal_journal_entry.entry_number", read_only=True, allow_null=True,
    )

    class Meta:
        model = CustomerRefund
        fields = (
            "id", "company", "customer", "customer_name", "branch", "branch_name",
            "refund_number", "refund_date", "source_return", "source_return_number",
            "source_receipt_batch", "source_receipt_number", "treasury_account",
            "treasury_account_name", "amount", "reference", "status",
            "treasury_transaction", "treasury_document_number", "journal_number",
            "reversal_treasury_transaction", "reversal_journal_entry",
            "reversal_journal_number", "reversal_date", "created_at", "posted_at",
        )
        read_only_fields = (
            "status", "treasury_transaction", "treasury_document_number", "journal_number",
            "reversal_treasury_transaction", "reversal_journal_entry",
            "reversal_journal_number", "reversal_date", "created_at", "posted_at",
        )

    def validate(self, attrs):
        instance = self.instance
        if instance:
            if instance.status != CustomerRefund.Status.DRAFT:
                raise serializers.ValidationError("لا يمكن تعديل رد مبلغ مرحّل.")
            raise serializers.ValidationError("تعديل مسودة رد المبلغ غير متاح؛ احذفها وأنشئها من جديد.")

        company = attrs["company"]
        customer = attrs["customer"]
        source_return = attrs.get("source_return")
        source_batch = attrs.get("source_receipt_batch")
        if bool(source_return) == bool(source_batch):
            raise serializers.ValidationError("حدد مصدرًا واحدًا فقط: مردود مبيعات أو دفعة مقدمة.")
        if customer.company_id != company.pk:
            raise serializers.ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})

        if source_return:
            if source_return.company_id != company.pk or source_return.invoice.customer_id != customer.pk:
                raise serializers.ValidationError({"source_return": "مردود المبيعات لا يتبع العميل والشركة المحددين."})
            if source_return.status != SalesReturn.Status.POSTED:
                raise serializers.ValidationError({"source_return": "اختر مردود مبيعات مرحّلًا."})
            branch = source_return.branch or source_return.invoice.branch
            if branch is None and source_return.invoice.delivery_id:
                branch = resolve_warehouse_branch(company, source_return.invoice.delivery.warehouse)
            source_date = source_return.return_date
        else:
            if source_batch.company_id != company.pk or source_batch.customer_id != customer.pk:
                raise serializers.ValidationError({"source_receipt_batch": "سند القبض لا يتبع العميل والشركة المحددين."})
            if source_batch.status != CustomerReceiptBatch.Status.POSTED:
                raise serializers.ValidationError({"source_receipt_batch": "اختر سند قبض مجمعًا مرحّلًا."})
            branch = source_batch.branch
            source_date = source_batch.receipt_date

        if branch is None or branch.company_id != company.pk:
            raise serializers.ValidationError({"source_return": "لا يمكن تحديد فرع المستند الأصلي."})
        if attrs["refund_date"] < source_date:
            raise serializers.ValidationError({"refund_date": "تاريخ الرد لا يجوز أن يسبق تاريخ المستند الأصلي."})

        account = attrs["treasury_account"]
        if account.company_id != company.pk or not account.is_active:
            raise serializers.ValidationError({"treasury_account": "اختر حساب خزينة/بنك نشطًا تابعًا للشركة."})
        if account.currency_code != company.base_currency.code:
            raise serializers.ValidationError({"treasury_account": "رد المبلغ متاح من حساب بالعملة الأساسية للشركة."})
        if account.branch_id and account.branch_id != branch.pk:
            raise serializers.ValidationError({"treasury_account": "حساب الخزينة لا يتبع فرع المستند الأصلي."})
        attrs["_refund_branch"] = branch
        return attrs

    def create(self, validated_data):
        branch = validated_data.pop("_refund_branch")
        return CustomerRefund.objects.create(
            **validated_data, branch=branch, created_by=self.context["request"].user,
        )
