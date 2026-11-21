from rest_framework import serializers
from .models import Branch, Company, Currency, FiscalYear

class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ("id", "code", "name_ar", "name_en", "symbol", "decimal_places", "is_active")

class CompanySerializer(serializers.ModelSerializer):
    base_currency_code = serializers.CharField(source="base_currency.code", read_only=True)

    class Meta:
        model = Company
        fields = (
            "id", "code", "name_ar", "name_en", "country", "activity",
            "base_currency", "base_currency_code", "tax_registration_number",
            "address", "phone", "email", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

class BranchSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = Branch
        fields = ("id", "company", "company_code", "code", "name_ar", "address", "phone", "is_head_office", "is_active", "created_at")
        read_only_fields = ("created_at",)

class FiscalYearSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = FiscalYear
        fields = ("id", "company", "company_code", "name", "start_date", "end_date", "status", "created_at")
        read_only_fields = ("created_at",)
