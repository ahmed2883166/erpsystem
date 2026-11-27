from decimal import Decimal
from rest_framework import serializers
from django.db import models
from companies.models import Currency, ExchangeRate
from .models import TreasuryAccount, TreasuryTransaction, TreasuryCheque, TreasuryFxPosition, TreasuryFxRevaluation, BankReconciliation, BankStatementLine, CashCommitment, CashCommitmentPayment, CashCommitmentSchedule


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
                  "opening_balance", "opening_foreign_amount", "opening_exchange_rate", "opening_exchange_rate_date", "opening_journal_entry", "fx_position_foreign_amount", "fx_position_carrying_base_amount", "is_active", "created_at")
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
        opening_foreign_amount = attrs.get("opening_foreign_amount", getattr(self.instance, "opening_foreign_amount", Decimal("0")))
        opening_exchange_rate = attrs.get("opening_exchange_rate", getattr(self.instance, "opening_exchange_rate", None))
        opening_exchange_rate_date = attrs.get("opening_exchange_rate_date", getattr(self.instance, "opening_exchange_rate_date", None))
        if opening_balance < 0 or opening_foreign_amount < 0:
            raise serializers.ValidationError({"opening_balance": "الرصيد الافتتاحي لا يمكن أن يكون سالبًا."})
        is_foreign = bool(company and currency_code != company.base_currency.code)
        if is_foreign:
            supplied = (opening_foreign_amount > 0 or opening_exchange_rate is not None or opening_exchange_rate_date is not None)
            if opening_balance > 0 and not supplied:
                raise serializers.ValidationError({"opening_foreign_amount": "أدخل الرصيد الاسمي الأجنبي وسعر الصرف وتاريخه."})
            if supplied:
                if opening_foreign_amount <= 0 or opening_balance <= 0 or opening_exchange_rate is None or opening_exchange_rate <= 0 or not opening_exchange_rate_date:
                    raise serializers.ValidationError("الرصيد الافتتاحي الأجنبي يتطلب المبلغ الأجنبي والقيمة الأساسية وسعر الصرف وتاريخه.")
                rate_row = ExchangeRate.objects.filter(
                    company=company, currency__code=currency_code,
                    rate_date=opening_exchange_rate_date, rate=opening_exchange_rate,
                ).first()
                if not rate_row:
                    raise serializers.ValidationError({"opening_exchange_rate": "سعر الصرف لا يطابق سعرًا معتمدًا للشركة في التاريخ المحدد."})
                expected = (opening_foreign_amount * opening_exchange_rate).quantize(Decimal("0.0001"))
                if expected != opening_balance:
                    raise serializers.ValidationError({"opening_balance": "القيمة الأساسية لا تطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
        elif opening_foreign_amount > 0 or opening_exchange_rate is not None or opening_exchange_rate_date is not None:
            raise serializers.ValidationError("بيانات الرصيد الأجنبي لا تصلح لحساب بالعملة الأساسية.")
        if self.instance and any(key in attrs for key in ("opening_balance", "opening_foreign_amount", "opening_exchange_rate", "opening_exchange_rate_date")):
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
    matched_document = serializers.SerializerMethodField()
    matched_amount = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    allocations = serializers.SerializerMethodField()
    adjustment_document = serializers.CharField(source="adjustment_transaction.document_number", read_only=True, default="")
    adjustment_status = serializers.CharField(source="adjustment_transaction.status", read_only=True, default="")

    class Meta:
        model = BankStatementLine
        fields = (
            "id", "reconciliation", "line_number", "transaction_date", "description",
            "reference", "amount", "matched_transaction", "matched_document",
            "matched_amount", "remaining_amount", "allocations", "adjustment_transaction",
            "adjustment_document", "adjustment_status", "matched_at",
        )
        read_only_fields = (
            "matched_transaction", "matched_at", "matched_document", "matched_amount",
            "remaining_amount", "allocations", "adjustment_transaction",
        )

    def get_allocations(self, obj):
        return [{
            "id": allocation.pk,
            "transaction_id": allocation.transaction_id,
            "document_number": allocation.transaction.document_number,
            "transaction_date": allocation.transaction.transaction_date,
            "allocated_amount": str(allocation.amount),
        } for allocation in obj.allocations.select_related("transaction").all()]

    def get_matched_amount(self, obj):
        return str(sum((item.amount for item in obj.allocations.all()), Decimal("0.0000")))

    def get_remaining_amount(self, obj):
        return str(max(Decimal("0.0000"), abs(obj.amount) - sum(
            (item.amount for item in obj.allocations.all()), Decimal("0.0000")
        )))

    def get_matched_document(self, obj):
        documents = list(obj.allocations.select_related("transaction").values_list(
            "transaction__document_number", flat=True
        )[:3])
        if documents:
            return "، ".join(documents) + ("…" if obj.allocations.count() > 3 else "")
        return obj.matched_transaction.document_number if obj.matched_transaction_id else ""

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


class TreasuryChequeSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source="bank_account.name_ar", read_only=True)
    transaction_number = serializers.CharField(source="treasury_transaction.document_number", read_only=True, default="")
    class Meta:
        model = TreasuryCheque
        fields = (
            "id", "company", "branch", "fiscal_year", "bank_account", "bank_name",
            "direction", "cheque_number", "counterparty", "amount", "issue_date",
            "due_date", "status", "treasury_transaction", "transaction_number",
            "deposit_date", "clearing_date", "bounce_date", "notes", "created_at", "updated_at",
        )
        read_only_fields = ("status", "deposit_date", "clearing_date", "bounce_date", "created_at", "updated_at")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        year = attrs.get("fiscal_year", getattr(self.instance, "fiscal_year", None))
        bank = attrs.get("bank_account", getattr(self.instance, "bank_account", None))
        direction = attrs.get("direction", getattr(self.instance, "direction", None))
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        issue_date = attrs.get("issue_date", getattr(self.instance, "issue_date", None))
        due_date = attrs.get("due_date", getattr(self.instance, "due_date", None))
        doc = attrs.get("treasury_transaction", getattr(self.instance, "treasury_transaction", None))
        if company and branch and branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if company and year and year.company_id != company.pk:
            raise serializers.ValidationError({"fiscal_year": "السنة المالية لا تتبع الشركة."})
        if bank and company and bank.company_id != company.pk:
            raise serializers.ValidationError({"bank_account": "الحساب البنكي لا يتبع الشركة."})
        if bank and (bank.kind != TreasuryAccount.Kind.BANK or not bank.is_active):
            raise serializers.ValidationError({"bank_account": "اختر حسابًا بنكيًا نشطًا."})
        if bank and company and bank.currency_code != company.base_currency.code:
            raise serializers.ValidationError({"bank_account": "دورة الشيكات متاحة بالعملة الأساسية فقط حاليًا."})
        if bank and branch and bank.branch_id and bank.branch_id != branch.pk:
            raise serializers.ValidationError({"branch": "فرع الشيك لا يطابق فرع الحساب البنكي."})
        if amount is not None and amount <= 0:
            raise serializers.ValidationError({"amount": "قيمة الشيك يجب أن تكون أكبر من صفر."})
        if issue_date and due_date and due_date < issue_date:
            raise serializers.ValidationError({"due_date": "تاريخ الاستحقاق لا يجوز أن يسبق تاريخ إصدار/استلام الشيك."})
        if doc and company and bank and amount is not None:
            expected = TreasuryTransaction.Kind.RECEIPT if direction == TreasuryCheque.Direction.INCOMING else TreasuryTransaction.Kind.PAYMENT
            if doc.company_id != company.pk or doc.source_account_id != bank.pk or doc.kind != expected or doc.amount != amount:
                raise serializers.ValidationError({"treasury_transaction": "حركة الخزينة يجب أن تطابق الشركة والبنك ونوع ومبلغ الشيك."})
            if doc.status != TreasuryTransaction.Status.POSTED:
                raise serializers.ValidationError({"treasury_transaction": "اربط حركة خزينة مرحلة فقط."})
        cheque_number = attrs.get("cheque_number", getattr(self.instance, "cheque_number", None))
        if company and bank and direction and cheque_number and TreasuryCheque.objects.filter(
            company=company, bank_account=bank, direction=direction, cheque_number=cheque_number
        ).exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError({"cheque_number": "رقم الشيك مسجل بالفعل على الحساب البنكي نفسه."})
        if direction == TreasuryCheque.Direction.OUTGOING and not doc:
            raise serializers.ValidationError({"treasury_transaction": "الشيك الصادر يجب ربطه بسند صرف بنكي مرحّل حتى لا يظهر كالتزام غير مسجل."})
        return attrs


class CashCommitmentPaymentSerializer(serializers.ModelSerializer):
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True)
    reversal_journal_number = serializers.CharField(source="reversal_journal_entry.entry_number", read_only=True, default="")

    class Meta:
        model = CashCommitmentPayment
        fields = ("id", "payment_date", "amount", "base_amount", "journal_entry", "journal_number",
                  "reversal_journal_entry", "reversal_journal_number", "reference", "notes", "created_at")
        read_only_fields = ("base_amount", "reversal_journal_entry", "created_at")


class CashCommitmentSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name_ar", read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True, default="")
    payable_account_code = serializers.CharField(source="payable_account.code", read_only=True)
    payable_account_name = serializers.CharField(source="payable_account.name_ar", read_only=True)
    paid_base_amount = serializers.SerializerMethodField()
    remaining_base_amount = serializers.SerializerMethodField()
    payments = CashCommitmentPaymentSerializer(many=True, read_only=True)

    class Meta:
        model = CashCommitment
        fields = ("id", "company", "company_name", "branch", "branch_name", "commitment_type",
                  "title", "period_label", "amount", "currency_code", "exchange_rate", "base_amount",
                  "due_date", "payable_account", "payable_account_code", "payable_account_name",
                  "origin_type", "origin_id", "status", "notes", "paid_base_amount",
                  "remaining_base_amount", "payments", "created_at", "updated_at")
        read_only_fields = ("status", "created_at", "updated_at", "paid_base_amount",
                            "remaining_base_amount", "payments")

    def _paid(self, obj):
        from accounting.models import JournalEntry
        total = Decimal("0")
        for payment in obj.payments.select_related("reversal_journal_entry").all():
            if payment.reversal_journal_entry_id and payment.reversal_journal_entry.status == JournalEntry.Status.POSTED:
                continue
            total += payment.base_amount
        return total

    def get_paid_base_amount(self, obj):
        return self._paid(obj)

    def get_remaining_base_amount(self, obj):
        return max(Decimal("0"), obj.base_amount - self._paid(obj))

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        account = attrs.get("payable_account", getattr(self.instance, "payable_account", None))
        currency_code = attrs.get("currency_code", getattr(self.instance, "currency_code", ""))
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        rate = attrs.get("exchange_rate", getattr(self.instance, "exchange_rate", Decimal("1")))
        base_amount = attrs.get("base_amount", getattr(self.instance, "base_amount", None))
        if company and branch and branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if company and account:
            if account.company_id != company.pk or account.is_group or not account.is_active or account.nature != account.Nature.LIABILITY:
                raise serializers.ValidationError({"payable_account": "اختر حساب خصوم تفصيليًا ونشطًا تابعًا للشركة."})
        if company and currency_code and amount is not None:
            if currency_code == company.base_currency.code:
                rate = Decimal("1")
                base_amount = amount
                attrs["exchange_rate"] = rate
                attrs["base_amount"] = base_amount
            else:
                from companies.models import ExchangeRate
                rate_date = attrs.get("due_date", getattr(self.instance, "due_date", None))
                approved = ExchangeRate.objects.filter(
                    company=company, currency__code=currency_code, rate_date=rate_date, rate=rate,
                ).exists()
                if not approved:
                    raise serializers.ValidationError({"exchange_rate": "سعر الصرف يجب أن يطابق سعرًا معتمدًا بتاريخ الاستحقاق."})
                expected = (amount * rate).quantize(Decimal("0.0001"))
                if base_amount is None:
                    attrs["base_amount"] = expected
                elif base_amount != expected:
                    raise serializers.ValidationError({"base_amount": "القيمة الأساسية لا تطابق المبلغ وسعر الصرف."})
        if self.instance and self.instance.payments.exists():
            raise serializers.ValidationError("لا يمكن تعديل التزام له دفعات مسجلة؛ استخدم العكس أو أنشئ التزامًا جديدًا.")
        origin_type = attrs.get("origin_type", getattr(self.instance, "origin_type", ""))
        origin_id = attrs.get("origin_id", getattr(self.instance, "origin_id", None))
        if bool(origin_type) != (origin_id is not None):
            raise serializers.ValidationError({"origin_id": "مرجع المصدر يجب أن يحتوي النوع والمعرف معًا."})
        if company and origin_type and origin_id is not None:
            duplicate = CashCommitment.objects.filter(company=company, origin_type=origin_type, origin_id=origin_id).exclude(status=CashCommitment.Status.VOID)
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError({"origin_id": "يوجد التزام مرتبط بالمصدر نفسه بالفعل."})
        return attrs


class CashCommitmentScheduleSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name_ar", read_only=True)
    payable_account_code = serializers.CharField(source="payable_account.code", read_only=True)

    class Meta:
        model = CashCommitmentSchedule
        fields = ("id", "company", "company_name", "branch", "title", "amount", "currency_code",
                  "exchange_rate", "base_amount", "frequency", "interval_months", "anchor_day",
                  "next_due_date", "end_date", "payable_account", "payable_account_code",
                  "active", "notes", "created_at", "updated_at")
        read_only_fields = ("interval_months", "anchor_day", "base_amount", "created_at", "updated_at")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        account = attrs.get("payable_account", getattr(self.instance, "payable_account", None))
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        currency = attrs.get("currency_code", getattr(self.instance, "currency_code", ""))
        rate = attrs.get("exchange_rate", getattr(self.instance, "exchange_rate", Decimal("1")))
        if company and branch and branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if company and account and (account.company_id != company.pk or account.is_group or not account.is_active or account.nature != account.Nature.LIABILITY):
            raise serializers.ValidationError({"payable_account": "اختر حساب خصوم تفصيليًا ونشطًا تابعًا للشركة."})
        if company and amount is not None:
            if currency == company.base_currency.code:
                rate = Decimal("1")
                attrs["exchange_rate"] = rate
            else:
                from companies.models import ExchangeRate
                rate_date = attrs.get("next_due_date", getattr(self.instance, "next_due_date", None))
                if not ExchangeRate.objects.filter(company=company, currency__code=currency, rate_date=rate_date, rate=rate).exists():
                    raise serializers.ValidationError({"exchange_rate": "سعر الصرف يجب أن يطابق سعرًا معتمدًا بتاريخ أول استحقاق."})
            attrs["base_amount"] = (amount * rate).quantize(Decimal("0.0001"))
        frequency = attrs.get("frequency", getattr(self.instance, "frequency", None))
        intervals = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}
        if frequency not in intervals:
            raise serializers.ValidationError({"frequency": "دورية غير صحيحة."})
        attrs["interval_months"] = intervals[frequency]
        next_due = attrs.get("next_due_date", getattr(self.instance, "next_due_date", None))
        if next_due:
            attrs["anchor_day"] = next_due.day
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if end_date and next_due and end_date < next_due:
            raise serializers.ValidationError({"end_date": "تاريخ النهاية يسبق أول استحقاق."})
        return attrs
