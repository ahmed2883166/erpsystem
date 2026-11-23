from django.db import migrations


def ensure_head_offices(apps, schema_editor):
    Company = apps.get_model("companies", "Company")
    Branch = apps.get_model("companies", "Branch")
    db_alias = schema_editor.connection.alias

    for company in Company.objects.using(db_alias).all().iterator():
        existing_head_office = Branch.objects.using(db_alias).filter(
            company_id=company.pk, is_head_office=True
        ).first()
        if existing_head_office:
            # بيانات قديمة قد تحتوي على مركز رئيسي موقوف قبل تطبيق الحماية.
            if not existing_head_office.is_active:
                Branch.objects.using(db_alias).filter(pk=existing_head_office.pk).update(
                    is_active=True
                )
            continue

        base_code = "MAIN"
        code = base_code
        suffix = 1
        while Branch.objects.using(db_alias).filter(
            company_id=company.pk, code=code
        ).exists():
            suffix += 1
            code = f"{base_code}-{suffix}"

        Branch.objects.using(db_alias).create(
            company_id=company.pk,
            code=code,
            name_ar="الفرع الرئيسي",
            is_head_office=True,
            is_active=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0005_fiscal_periods"),
    ]

    operations = [
        migrations.RunPython(ensure_head_offices, migrations.RunPython.noop),
    ]
