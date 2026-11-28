from decimal import Decimal

from rest_framework import serializers

from accounting.models import ChartOfAccount
from companies.access import resolve_warehouse_branch
from sales.models import (
    CustomerReceipt, CustomerReceiptBatch, CustomerReceiptBatchAllocation,
    SalesInvoice,
)
from treasury.models import TreasuryAccount


class CustomerReceiptBatchAllocationInputSerializer(serializers.Serializer):
    invoice = serializers.PrimaryKeyRelatedField(queryset=SalesInvoice.objects.all())
    amount = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))


class CustomerReceiptBatchAllocationSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    invoice_date = serializers.DateField(source="invoice.invoice_date", read_only=True)

    class Meta:
        model = CustomerReceiptBatchAllocation
        fields = ("id", "invoice", "invoice_number", "invoice_date", "amount", "allocation_date", "created_at")
        read_only_fields = fields


class CustomerReceiptBatchSerializer(serializers.ModelSerializer):
    payment_account = serializers.PrimaryKeyRelatedField(
        queryset=ChartOfAccount.objects.all(), required=False, allow_null=True,
    )
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True)
    payment_account_name = serializers.CharField(source="payment_account.name_ar", read_only=True, allow_null=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    reversal_journal_number = serializers.CharField(
        source="reversal_journal_entry.entry_number", read_only=True, allow_null=True,
    )
    allocation_lines = CustomerReceiptBatchAllocationSerializer(source="allocations", many=True, read_only=True)
    allocations = CustomerReceiptBatchAllocationInputSerializer(many=True, required=False, write_only=True)
    allocated_amount = serializers.SerializerMethodField()
    unallocated_amount = serializers.SerializerMethodField()

    class Meta:
        model = CustomerReceiptBatch
        fields = (
            "id", "company", "customer", "customer_name", "branch", "branch_name",
            "receipt_number", "receipt_date", "method", "payment_account",
            "payment_account_name", "amount", "allocated_amount", "unallocated_amount",
            "allocations", "allocation_lines", "reference", "status", "print_template_snapshot", "journal_entry",
            "journal_number", "reversal_journal_entry", "reversal_journal_number",
            "reversal_date", "created_at", "posted_at",
        )
        read_only_fields = (
            "status", "print_template_snapshot", "journal_entry", "journal_number", "reversal_journal_entry",
            "reversal_journal_number", "reversal_date", "created_at", "posted_at",
        )

    def get_allocated_amount(self, obj):
        return str(sum((row.amount for row in obj.allocations.all()), Decimal("0")))

    def get_unallocated_amount(self, obj):
        from sales.customer_refund_services import _available_advance
        return str(_available_advance(obj, obj.receipt_date if obj.status == CustomerReceiptBatch.Status.DRAFT else __import__("django.utils.timezone", fromlist=["localdate"]).localdate()))

    def validate(self, attrs):
        company = attrs.get("company")
        customer = attrs.get("customer")
        branch = attrs.get("branch")
        receipt_date = attrs.get("receipt_date")
        amount = attrs.get("amount")
        allocations = attrs.get("allocations", [])

        if customer.company_id != company.pk:
            raise serializers.ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if not customer.is_active:
            raise serializers.ValidationError({"customer": "العميل موقوف."})
        if branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        if not branch.is_active:
            raise serializers.ValidationError({"branch": "لا يمكن إنشاء سند قبض على فرع موقوف."})
        if (
            CustomerReceipt.objects.filter(
                company=company, receipt_number=attrs.get("receipt_number"),
            ).exists()
            or CustomerReceiptBatch.objects.filter(
                company=company, receipt_number=attrs.get("receipt_number"),
            ).exists()
        ):
            raise serializers.ValidationError({"receipt_number": "رقم سند القبض مستخدم في سند آخر."})

        allocation_total = sum((row["amount"] for row in allocations), Decimal("0"))
        if allocation_total > amount:
            raise serializers.ValidationError({
                "allocations": "إجمالي التخصيصات لا يجوز أن يتجاوز مبلغ سند القبض."
            })

        seen = set()
        for row in allocations:
            invoice = row["invoice"]
            if invoice.pk in seen:
                raise serializers.ValidationError({"allocations": "لا تكرر الفاتورة في سند القبض؛ اجمع تخصيصها في سطر واحد."})
            seen.add(invoice.pk)
            if invoice.status != SalesInvoice.Status.POSTED:
                raise serializers.ValidationError({"allocations": "كل الفواتير المختارة يجب أن تكون مرحلة."})
            if invoice.company_id != company.pk or invoice.customer_id != customer.pk:
                raise serializers.ValidationError({"allocations": "كل الفواتير يجب أن تتبع العميل والشركة المحددين."})
            if invoice.transaction_currency_id:
                raise serializers.ValidationError({
                    "allocations": "التخصيص المجمع متاح لفواتير العملة الأساسية فقط؛ استخدم سند العملة الأجنبية المنفرد."
                })
            if receipt_date < invoice.invoice_date:
                raise serializers.ValidationError({"receipt_date": "تاريخ السند لا يجوز أن يسبق تاريخ أي فاتورة مخصصة."})
            invoice_branch = (
                resolve_warehouse_branch(company, invoice.delivery.warehouse)
                if invoice.delivery_id else invoice.branch
            )
            if not invoice_branch or invoice_branch.pk != branch.pk:
                raise serializers.ValidationError({
                    "allocations": "يجب أن تتبع كل الفواتير المخصصة نفس فرع سند القبض."
                })

        account = attrs.get("payment_account")
        if account:
            method = attrs.get("method", CustomerReceipt.Method.BANK)
            expected_kind = TreasuryAccount.Kind.CASH if method == CustomerReceipt.Method.CASH else TreasuryAccount.Kind.BANK
            treasury_account = TreasuryAccount.objects.filter(
                company=company, account=account, kind=expected_kind, is_active=True,
            ).first()
            if (
                account.company_id != company.pk
                or account.nature != ChartOfAccount.Nature.ASSET
                or not account.is_active or account.is_group or not account.allow_posting
                or not treasury_account
                or (treasury_account.branch_id and treasury_account.branch_id != branch.pk)
                or treasury_account.currency_code != company.base_currency.code
            ):
                raise serializers.ValidationError({
                    "payment_account": "اختر حساب خزينة/بنك نشطًا تابعًا للفرع وبعملة الشركة وبنفس طريقة التحصيل."
                })
        return attrs

    def create(self, validated_data):
        allocations = validated_data.pop("allocations", [])
        batch = CustomerReceiptBatch.objects.create(
            **validated_data, created_by=self.context["request"].user,
        )
        for row in allocations:
            CustomerReceiptBatchAllocation.objects.create(
                batch=batch, invoice=row["invoice"], amount=row["amount"],
                allocation_date=batch.receipt_date, created_by=self.context["request"].user,
            )
        return batch
