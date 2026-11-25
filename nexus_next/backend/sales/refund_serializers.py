from rest_framework import serializers

from companies.access import resolve_warehouse_branch
from companies.models import Currency, ExchangeRate
from core.money import round_currency_amount
from sales.models import CustomerRefund, CustomerReceiptBatch, SalesReturn


class CustomerRefundSerializer(serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    treasury_account_name = serializers.CharField(source="treasury_account.name_ar", read_only=True)
    transaction_currency_code = serializers.CharField(source="transaction_currency.code", read_only=True, allow_null=True)
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
    transaction_currency = serializers.PrimaryKeyRelatedField(
        queryset=Currency.objects.all(), required=False, allow_null=True,
    )
    amount = serializers.DecimalField(max_digits=20, decimal_places=4, required=False)

    class Meta:
        model = CustomerRefund
        fields = (
            "id", "company", "customer", "customer_name", "branch", "branch_name",
            "refund_number", "refund_date", "source_return", "source_return_number",
            "source_receipt_batch", "source_receipt_number", "treasury_account",
            "treasury_account_name", "amount", "transaction_currency", "transaction_currency_code",
            "transaction_amount", "exchange_rate", "exchange_rate_date",
            "reference", "status", "treasury_transaction", "treasury_document_number",
            "journal_number", "reversal_treasury_transaction", "reversal_journal_entry",
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
        refund_date = attrs["refund_date"]
        if refund_date < source_date:
            raise serializers.ValidationError({"refund_date": "تاريخ الرد لا يجوز أن يسبق تاريخ المستند الأصلي."})

        account = attrs["treasury_account"]
        if account.company_id != company.pk or not account.is_active:
            raise serializers.ValidationError({"treasury_account": "اختر حساب خزينة/بنك نشطًا تابعًا للشركة."})
        if account.branch_id and account.branch_id != branch.pk:
            raise serializers.ValidationError({"treasury_account": "حساب الخزينة لا يتبع فرع المستند الأصلي."})

        fx_fields = ("transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date")
        fx_values = [attrs.get(field) for field in fx_fields]
        is_fx = any(value is not None for value in fx_values)
        if is_fx and any(value is None for value in fx_values):
            raise serializers.ValidationError("بيانات العملة الأجنبية وسعر الصرف يجب إدخالها كاملة.")
        if source_return and source_return.invoice.transaction_currency_id:
            invoice_currency = source_return.invoice.transaction_currency
            if not is_fx:
                raise serializers.ValidationError({"transaction_amount": "أدخل مبلغ الرد بعملة الفاتورة الأجنبية."})
            if attrs["transaction_currency"].pk != invoice_currency.pk:
                raise serializers.ValidationError({"transaction_currency": "عملة الرد يجب أن تطابق عملة الفاتورة."})
            if account.currency_code != invoice_currency.code:
                raise serializers.ValidationError({"treasury_account": "اختر خزينة أو بنكًا بنفس عملة الفاتورة الأجنبية."})
            if attrs["transaction_amount"] <= 0 or attrs["exchange_rate"] <= 0:
                raise serializers.ValidationError("المبلغ الأجنبي وسعر الصرف يجب أن يكونا أكبر من صفر.")
            rate_date = attrs["exchange_rate_date"]
            if rate_date > refund_date:
                raise serializers.ValidationError({"exchange_rate_date": "تاريخ سعر الصرف لا يجوز أن يتجاوز تاريخ الرد."})
            if not ExchangeRate.objects.filter(
                company=company, currency=invoice_currency,
                rate_date=rate_date, rate=attrs["exchange_rate"],
            ).exists():
                raise serializers.ValidationError({"exchange_rate": "سعر الصرف لا يطابق سعرًا معتمدًا مسجلًا للشركة والتاريخ."})
            attrs["amount"] = round_currency_amount(
                attrs["transaction_amount"] * attrs["exchange_rate"], company.base_currency,
            )
        else:
            if is_fx:
                raise serializers.ValidationError("بيانات العملة الأجنبية مسموحة فقط لمردود فاتورة أجنبية.")
            if account.currency_code != company.base_currency.code:
                raise serializers.ValidationError({"treasury_account": "رد الدفعات المقدمة والمردودات الأساسية يتطلب حسابًا بالعملة الأساسية."})
            if "amount" not in attrs:
                raise serializers.ValidationError({"amount": "أدخل مبلغ الرد بالعملة الأساسية."})

        attrs["_refund_branch"] = branch
        return attrs

    def create(self, validated_data):
        branch = validated_data.pop("_refund_branch")
        return CustomerRefund.objects.create(
            **validated_data, branch=branch, created_by=self.context["request"].user,
        )
