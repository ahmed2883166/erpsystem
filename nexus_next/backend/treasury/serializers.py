from decimal import Decimal
from rest_framework import serializers
from django.db import models
from companies.models import Currency, ExchangeRate
from .models import TreasuryAccount, TreasuryTransaction, TreasuryFxPosition, TreasuryFxRevaluation, BankReconciliation, BankStatementLine


class TreasuryAccountSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name_ar", read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True, default="")
    fx_position_foreign_amount = serializers.SerializerMethodField()
    fx_position_carrying_base_amount = serializers.SerializerMethodField()

    def get_fx_position_foreign_amount(self, obj):
        try:
            return obj.fx_position.foreign_amount
        except TreasuryFxPosition.DoesNotExist:
            return Decimal("0.0000")

    def get_fx_position_carrying_base_amount(self, obj):
        try:
            return obj.fx_position.carrying_base_amount
        except TreasuryFxPosition.DoesNotExist:
            return Decimal("0.0000")

    class Meta:
        model = TreasuryAccount
        fields = ("id", "company", "branch", "branch_name", "account", "account_code", "account_name",
                  "kind", "code", "name_ar", "bank_name", "account_number", "iban", "currency_code",
                  "opening_balance", "opening_journal_entry", "fx_position_foreign_amount", "fx_position_carrying_base_amount", "is_active", "created_at")
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
        currency_code = attrs.get(
            "currency_code",
            getattr(self.instance, "currency_code", None) or (company.base_currency.code if company else ""),
        )
        if not self.instance and company and "currency_code" not in attrs:
            attrs["currency_code"] = company.base_currency.code
        if not Currency.objects.filter(code=currency_code, is_active=True).exists():
            raise serializers.ValidationError({"currency_code": "اختر عملة موجودة ونشطة من قائمة العملات المعتمدة."})
        opening_balance = attrs.get("opening_balance", getattr(self.instance, "opening_balance", 0))
        if company and currency_code != company.base_currency.code and opening_balance > 0:
            raise serializers.ValidationError({"opening_balance": "الرصيد الافتتاحي للحساب الأجنبي يحتاج إدخال المبلغ الأجنبي وسعره المعتمد؛ ابدأ بصفر حتى تُسجل حركة افتتاحية موثقة."})
        if opening_balance < 0:
            raise serializers.ValidationError({"opening_balance": "الرصيد الافتتاحي لا يمكن أن يكون سالبًا."})
        if self.instance and "opening_balance" in attrs:
            raise serializers.ValidationError({"opening_balance": "الرصيد الافتتاحي لا يتغير بعد إنشاء الحساب؛ صحّح الرصيد بقيد افتتاحي أو تسوية معتمدة."})
        if self.instance:
            has_history = (
                self.instance.opening_journal_entry_id
                or TreasuryTransaction.objects.filter(
                    models.Q(source_account=self.instance) | models.Q(destination_account=self.instance)
                ).exists()
                or TreasuryFxPosition.objects.filter(account=self.instance).exists()
                or TreasuryFxRevaluation.objects.filter(account=self.instance).exists()
            )
            if has_history:
                protected_fields = ("account", "kind", "code", "currency_code", "branch")
                changed = [
                    field for field in protected_fields
                    if field in attrs and getattr(self.instance, field + "_id" if field in ("account", "branch") else field)
                    != (attrs[field].pk if field in ("account", "branch") and attrs[field] else attrs[field])
                ]
                if changed:
                    raise serializers.ValidationError({
                        field: "لا يمكن تغيير بيانات الربط أو العملة أو الفرع لحساب خزينة له حركات؛ أنشئ حسابًا جديدًا للتغيير."
                        for field in changed
                    })
        return attrs


class TreasuryTransactionSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source_account.name_ar", read_only=True)
    destination_name = serializers.CharField(source="destination_account.name_ar", read_only=True, default="")
    counter_account_code = serializers.CharField(source="counter_account.code", read_only=True, default="")
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, default="")
    reversal_journal_number = serializers.CharField(source="reversal_journal_entry.entry_number", read_only=True, default="")
    reversal_journal_number = serializers.CharField(source="reversal_journal_entry.entry_number", read_only=True, default="")

    class Meta:
        model = TreasuryTransaction
        fields = ("id", "company", "branch", "fiscal_year", "source_account", "source_name",
                  "destination_account", "destination_name", "counter_account", "counter_account_code",
                  "kind", "status", "document_number", "transaction_date", "amount",
                  "transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date", "carrying_amount", "description",
                  "reference", "cash_flow_category", "journal_entry", "journal_number", "reversal_journal_entry", "reversal_journal_number", "reversed_at", "reversed_by", "created_at", "posted_at")
        read_only_fields = ("status", "journal_entry", "reversal_journal_entry", "reversed_at", "reversed_by", "created_at", "posted_at", "carrying_amount")

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
        amount = attrs.get("amount", getattr(self.instance, "amount", 0))
        if amount <= 0:
            raise serializers.ValidationError({"amount": "القيمة يجب أن تكون أكبر من صفر."})
        fx_names = ("transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date")
        fx = {
            name: attrs.get(name, getattr(self.instance, name, None))
            for name in fx_names
        }
        source_account_for_currency = attrs.get("source_account", getattr(self.instance, "source_account", None))
        if (
            source_account_for_currency
            and source_account_for_currency.currency_code != company.base_currency.code
            and any(value is None for value in fx.values())
        ):
            raise serializers.ValidationError("حساب الخزينة بعملة أجنبية؛ يجب إدخال المبلغ الأجنبي وسعر الصرف المعتمد كاملًا.")
        if any(value is not None for value in fx.values()):
            if any(value is None for value in fx.values()):
                raise serializers.ValidationError("بيانات العملة الأجنبية وسعر الصرف يجب إدخالها كاملة.")
            currency = fx["transaction_currency"]
            if not currency.is_active:
                raise serializers.ValidationError({"transaction_currency": "العملة المختارة موقوفة."})
            if currency.code == company.base_currency.code:
                raise serializers.ValidationError({"transaction_currency": "استخدم العملة الأساسية دون بيانات تحويل أجنبي."})
            source_account = attrs.get("source_account", getattr(self.instance, "source_account", None))
            if source_account and source_account.currency_code != currency.code:
                raise serializers.ValidationError({"transaction_currency": "عملة المعاملة لا تطابق عملة حساب الخزينة."})
            if fx["transaction_amount"] <= 0 or fx["exchange_rate"] <= 0:
                raise serializers.ValidationError("المبلغ الأجنبي وسعر الصرف يجب أن يكونا أكبر من صفر.")
            transaction_date = attrs.get("transaction_date", getattr(self.instance, "transaction_date", None))
            if fx["exchange_rate_date"] > transaction_date:
                raise serializers.ValidationError({"exchange_rate_date": "تاريخ سعر الصرف لا يجوز أن يتجاوز تاريخ الحركة."})
            if not ExchangeRate.objects.filter(
                company=company, currency=currency,
                rate_date=fx["exchange_rate_date"], rate=fx["exchange_rate"],
            ).exists():
                raise serializers.ValidationError("سعر الصرف لا يطابق سعرًا معتمدًا مسجلًا للشركة والتاريخ.")
            from decimal import Decimal
            expected_base = (fx["transaction_amount"] * fx["exchange_rate"]).quantize(Decimal("0.0001"))
            if amount != expected_base:
                raise serializers.ValidationError({"amount": "القيمة بالعملة الأساسية لا تطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
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
