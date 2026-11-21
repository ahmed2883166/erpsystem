from rest_framework import serializers
from .models import TreasuryAccount, TreasuryTransaction, BankReconciliation, BankStatementLine


class TreasuryAccountSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name_ar", read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True, default="")

    class Meta:
        model = TreasuryAccount
        fields = ("id", "company", "branch", "branch_name", "account", "account_code", "account_name",
                  "kind", "code", "name_ar", "bank_name", "account_number", "iban", "currency_code",
                  "opening_balance", "opening_journal_entry", "is_active", "created_at")
        read_only_fields = ("created_at", "opening_journal_entry")

    def validate(self, attrs):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        account = attrs.get("account", getattr(self.instance, "account", None))
        if branch and company and branch.company_id != company.id:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if account and company and account.company_id != company.id:
            raise serializers.ValidationError({"account": "الحساب لا يتبع الشركة."})
        if account and (account.is_group or not account.allow_posting or not account.is_active):
            raise serializers.ValidationError({"account": "اختر حسابًا تفصيليًا نشطًا."})
        if account and account.nature != account.Nature.ASSET:
            raise serializers.ValidationError({"account": "يجب أن يكون حساب الخزينة من طبيعة الأصول."})
        opening_balance = attrs.get("opening_balance", getattr(self.instance, "opening_balance", 0))
        if opening_balance < 0:
            raise serializers.ValidationError({"opening_balance": "الرصيد الافتتاحي لا يمكن أن يكون سالبًا."})
        if self.instance and "opening_balance" in attrs:
            raise serializers.ValidationError({"opening_balance": "الرصيد الافتتاحي لا يتغير بعد إنشاء الحساب؛ صحّح الرصيد بقيد افتتاحي أو تسوية معتمدة."})
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
                  "reference", "cash_flow_category", "journal_entry", "journal_number", "created_at", "posted_at")
        read_only_fields = ("status", "journal_entry", "created_at", "posted_at")

    def validate(self, attrs):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
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


class BankStatementLineSerializer(serializers.ModelSerializer):
    matched_document = serializers.CharField(source="matched_transaction.document_number", read_only=True, default="")
    matched_amount = serializers.DecimalField(source="matched_transaction.amount", max_digits=20, decimal_places=4, read_only=True, default=None)

    class Meta:
        model = BankStatementLine
        fields = ("id", "reconciliation", "line_number", "transaction_date", "description", "reference",
                  "amount", "matched_transaction", "matched_document", "matched_amount", "matched_at")
        read_only_fields = ("matched_transaction", "matched_at")

    def validate(self, attrs):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        recon = attrs.get("reconciliation", getattr(self.instance, "reconciliation", None))
        line_date = attrs.get("transaction_date", getattr(self.instance, "transaction_date", None))
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        if amount == 0:
            raise serializers.ValidationError({"amount": "قيمة حركة كشف البنك لا يمكن أن تكون صفرًا."})
        if recon and line_date and line_date > recon.statement_date:
            raise serializers.ValidationError({"transaction_date": "تاريخ الحركة بعد تاريخ كشف البنك."})
        return attrs


class BankReconciliationSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source="bank_account.name_ar", read_only=True)
    bank_code = serializers.CharField(source="bank_account.code", read_only=True)
    line_count = serializers.IntegerField(source="lines.count", read_only=True)

    class Meta:
        model = BankReconciliation
        fields = ("id", "company", "bank_account", "bank_name", "bank_code", "statement_number",
                  "statement_date", "opening_balance", "closing_balance", "status", "line_count",
                  "created_at", "reconciled_at")
        read_only_fields = ("status", "created_at", "reconciled_at", "line_count")

    def validate(self, attrs):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        bank = attrs.get("bank_account", getattr(self.instance, "bank_account", None))
        if bank and company and bank.company_id != company.id:
            raise serializers.ValidationError({"bank_account": "الحساب البنكي لا يتبع الشركة."})
        if bank and (bank.kind != TreasuryAccount.Kind.BANK or not bank.is_active):
            raise serializers.ValidationError({"bank_account": "اختر حسابًا بنكيًا نشطًا."})
        return attrs
