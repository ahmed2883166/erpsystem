from rest_framework import serializers
from .models import TreasuryAccount, TreasuryTransaction


class TreasuryAccountSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name_ar", read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True, default="")

    class Meta:
        model = TreasuryAccount
        fields = ("id", "company", "branch", "branch_name", "account", "account_code", "account_name",
                  "kind", "code", "name_ar", "bank_name", "account_number", "iban", "currency_code",
                  "opening_balance", "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        account = attrs.get("account", getattr(self.instance, "account", None))
        if branch and company and branch.company_id != company.id:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if account and company and account.company_id != company.id:
            raise serializers.ValidationError({"account": "الحساب لا يتبع الشركة."})
        if account and (account.is_group or not account.allow_posting or not account.is_active):
            raise serializers.ValidationError({"account": "اختر حسابًا تفصيليًا نشطًا."})
        return attrs


class TreasuryTransactionSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source_account.name_ar", read_only=True)
    destination_name = serializers.CharField(source="destination_account.name_ar", read_only=True, default="")
    counter_account_code = serializers.CharField(source="counter_account.code", read_only=True, default="")
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, default="")

    class Meta:
        model = TreasuryTransaction
        fields = ("id", "company", "branch", "fiscal_year", "source_account", "source_name",
                  "destination_account", "destination_name", "counter_account", "counter_account_code",
                  "kind", "status", "document_number", "transaction_date", "amount", "description",
                  "reference", "journal_entry", "journal_number", "created_at", "posted_at")
        read_only_fields = ("status", "journal_entry", "created_at", "posted_at")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        for key in ("source_account", "destination_account", "counter_account"):
            obj = attrs.get(key, getattr(self.instance, key, None))
            if obj and company and obj.company_id != company.id:
                raise serializers.ValidationError({key: "العنصر المحدد لا يتبع الشركة."})
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        fiscal_year = attrs.get("fiscal_year", getattr(self.instance, "fiscal_year", None))
        if branch and company and branch.company_id != company.id:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if fiscal_year and company and fiscal_year.company_id != company.id:
            raise serializers.ValidationError({"fiscal_year": "السنة المالية لا تتبع الشركة."})
        if attrs.get("amount", getattr(self.instance, "amount", 0)) <= 0:
            raise serializers.ValidationError({"amount": "القيمة يجب أن تكون أكبر من صفر."})
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        dest = attrs.get("destination_account", getattr(self.instance, "destination_account", None))
        counter = attrs.get("counter_account", getattr(self.instance, "counter_account", None))
        if kind == TreasuryTransaction.Kind.TRANSFER and (not dest or counter):
            raise serializers.ValidationError("التحويل يتطلب خزينة مستقبلة فقط.")
        if kind != TreasuryTransaction.Kind.TRANSFER and (not counter or dest):
            raise serializers.ValidationError("القبض والصرف يتطلبان حسابًا مقابلًا فقط.")
        return attrs
