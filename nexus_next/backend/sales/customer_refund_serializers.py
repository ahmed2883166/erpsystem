from decimal import Decimal

from rest_framework import serializers

from sales.models import CustomerRefund, CustomerReceiptBatch
from treasury.models import TreasuryAccount


class CustomerRefundSerializer(serializers.ModelSerializer):
    treasury_account = serializers.PrimaryKeyRelatedField(queryset=TreasuryAccount.objects.all())
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True)
    source_receipt_number = serializers.CharField(source="source_receipt_batch.receipt_number", read_only=True)
    treasury_account_name = serializers.CharField(source="treasury_account.account.name_ar", read_only=True)
    journal_number = serializers.SerializerMethodField()
    reversal_journal_number = serializers.CharField(
        source="reversal_journal_entry.entry_number", read_only=True, allow_null=True,
    )
    available_advance = serializers.SerializerMethodField()

    class Meta:
        model = CustomerRefund
        fields = (
            "id", "company", "customer", "customer_name", "branch", "branch_name",
            "source_receipt_batch", "source_receipt_number", "treasury_account",
            "treasury_account_name", "refund_number", "refund_date", "amount",
            "available_advance", "status", "treasury_transaction", "journal_number",
            "reversal_treasury_transaction", "reversal_journal_entry",
            "reversal_journal_number", "reversal_date", "created_by", "created_at",
        )
        read_only_fields = (
            "status", "treasury_transaction", "journal_number",
            "reversal_treasury_transaction", "reversal_journal_entry",
            "reversal_journal_number", "reversal_date", "created_by", "created_at",
            "available_advance",
        )

    def get_journal_number(self, obj):
        movement = obj.treasury_transaction
        return movement.journal_entry.entry_number if movement and movement.journal_entry_id else None

    def get_available_advance(self, obj):
        from sales.customer_refund_services import _available_advance
        return str(_available_advance(obj.source_receipt_batch, obj.refund_date))

    def validate(self, attrs):
        company = attrs["company"]
        customer = attrs["customer"]
        branch = attrs["branch"]
        batch = attrs["source_receipt_batch"]
        treasury = attrs["treasury_account"]
        refund_date = attrs["refund_date"]
        amount = attrs["amount"]
        if customer.company_id != company.pk or batch.company_id != company.pk:
            raise serializers.ValidationError({"company": "الشركة لا تطابق العميل وسند القبض."})
        if batch.customer_id != customer.pk or batch.branch_id != branch.pk:
            raise serializers.ValidationError({"source_receipt_batch": "سند القبض لا يطابق العميل والفرع."})
        if batch.status != CustomerReceiptBatch.Status.POSTED:
            raise serializers.ValidationError({"source_receipt_batch": "اختر سند قبض مجمعًا مرحّلًا وغير معكوس."})
        if not customer.is_active or not branch.is_active:
            raise serializers.ValidationError("العميل والفرع يجب أن يكونا نشطين.")
        if refund_date < batch.receipt_date:
            raise serializers.ValidationError({"refund_date": "تاريخ المردود لا يجوز أن يسبق سند القبض."})
        if amount <= 0 or amount != amount.quantize(Decimal("0.0001")):
            raise serializers.ValidationError({"amount": "أدخل مبلغًا موجبًا حتى 4 منازل عشرية."})
        if treasury.company_id != company.pk or not treasury.is_active:
            raise serializers.ValidationError({"treasury_account": "حساب الخزينة غير نشط أو لا يتبع الشركة."})
        if treasury.branch_id and treasury.branch_id != branch.pk:
            raise serializers.ValidationError({"treasury_account": "حساب الخزينة لا يتبع فرع المردود."})
        if treasury.currency_code != company.base_currency.code:
            raise serializers.ValidationError({"treasury_account": "مردود الدفعة المجمعة يجب أن يكون بعملة الشركة الأساسية."})
        if treasury.kind not in (TreasuryAccount.Kind.CASH, TreasuryAccount.Kind.BANK):
            raise serializers.ValidationError({"treasury_account": "اختر حساب خزينة أو بنك."})
        if treasury.account.nature != "asset" or not treasury.account.is_active or treasury.account.is_group or not treasury.account.allow_posting:
            raise serializers.ValidationError({"treasury_account": "حساب الخزينة يجب أن يكون تفصيليًا ونشطًا وقابلًا للترحيل."})
        if CustomerRefund.objects.filter(company=company, refund_number=attrs["refund_number"]).exists():
            raise serializers.ValidationError({"refund_number": "رقم المردود مستخدم داخل الشركة."})
        return attrs

    def create(self, validated_data):
        return CustomerRefund.objects.create(**validated_data, created_by=self.context["request"].user)
