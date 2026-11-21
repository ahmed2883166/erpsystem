from __future__ import annotations

from io import BytesIO

from django.db import transaction
from openpyxl import load_workbook

from .models import ChartOfAccount

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000

HEADER_ALIASES = {
    "code": {"code", "accountcode", "كود", "كودالحساب", "رقمالحساب"},
    "name_ar": {"name", "namear", "accountname", "اسم", "اسمالحساب", "اسم الحساب"},
    "parent_code": {"parent", "parentcode", "parentaccount", "كودالاب", "كودالأب", "الحسابالاب", "الحسابالأب"},
    "nature": {"nature", "accountnature", "طبيعة", "طبيعةالحساب"},
    "kind": {"type", "kind", "accounttype", "التصنيف", "نوعالحساب"},
}

NATURES = {
    "asset": "asset", "assets": "asset", "أصول": "asset", "الاصول": "asset", "الأصول": "asset",
    "liability": "liability", "liabilities": "liability", "التزامات": "liability", "الالتزامات": "liability",
    "equity": "equity", "حقوقملكية": "equity", "حقوقالملكية": "equity", "حقوق الملكية": "equity",
    "revenue": "revenue", "income": "revenue", "إيرادات": "revenue", "الايرادات": "revenue", "الإيرادات": "revenue",
    "expense": "expense", "expenses": "expense", "مصروفات": "expense", "التكاليفوالمصروفات": "expense",
}

def _norm(value):
    return "".join(str(value or "").strip().lower().replace("_", " ").split())

def _code_value(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)) and float(value).is_integer():
        return str(int(value))
    return str(value).strip()

def _truthy_group(value):
    value = _norm(value)
    return value in {"group", "header", "مجموعه", "مجموعة", "تجميعي", "تجميعى", "1", "true"}

def inspect_account_workbook(upload, company):
    if not upload:
        return {"errors": ["اختر ملف Excel بصيغة .xlsx."], "rows": [], "summary": {}}
    if upload.size > MAX_FILE_BYTES:
        return {"errors": ["حجم الملف يتجاوز 5 ميجابايت."], "rows": [], "summary": {}}
    if not upload.name.lower().endswith(".xlsx"):
        return {"errors": ["الصيغة المدعومة حاليًا هي Excel .xlsx فقط."], "rows": [], "summary": {}}
    try:
        workbook = load_workbook(BytesIO(upload.read()), read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        headers = next(iterator, None)
        if not headers:
            return {"errors": ["ملف Excel فارغ."], "rows": [], "summary": {}}
        columns = {}
        for index, raw in enumerate(headers):
            key = _norm(raw)
            for field, aliases in HEADER_ALIASES.items():
                if key in {_norm(alias) for alias in aliases}:
                    columns[field] = index
        required = {"code", "name_ar", "nature", "kind"}
        missing = required - set(columns)
        if missing:
            labels = {"code": "كود الحساب", "name_ar": "اسم الحساب", "nature": "طبيعة الحساب", "kind": "التصنيف"}
            return {"errors": ["أعمدة مطلوبة غير موجودة: " + "، ".join(labels[x] for x in sorted(missing))], "rows": [], "summary": {}}
        raw_rows = []
        for excel_row, values in enumerate(iterator, start=2):
            if not any(value is not None and str(value).strip() for value in values):
                continue
            if len(raw_rows) >= MAX_ROWS:
                return {"errors": [f"الملف يتجاوز الحد الأقصى وهو {MAX_ROWS} حساب."], "rows": [], "summary": {}}
            def cell(field):
                idx = columns.get(field)
                return values[idx] if idx is not None and idx < len(values) else None
            code = _code_value(cell("code"))
            name = str(cell("name_ar") or "").strip()
            parent_code = _code_value(cell("parent_code")) or None
            nature_raw = str(cell("nature") or "").strip()
            nature = NATURES.get(_norm(nature_raw)) or NATURES.get(nature_raw)
            kind_raw = cell("kind")
            is_group = _truthy_group(kind_raw)
            kind_norm = _norm(kind_raw)
            is_detail = kind_norm in {"detail", "posting", "تفصيلي", "تفصيلى", "حسابتفصيلي", "حساب تفصيلي", "0", "false"}
            errors = []
            if not code: errors.append("كود الحساب مطلوب.")
            if len(code) > 40: errors.append("كود الحساب يتجاوز 40 حرفًا.")
            if not name: errors.append("اسم الحساب مطلوب.")
            if len(name) > 180: errors.append("اسم الحساب يتجاوز 180 حرفًا.")
            if not nature: errors.append(f"طبيعة الحساب غير معروفة: {nature_raw or 'فارغة'}.")
            if not is_group and not is_detail: errors.append("التصنيف يجب أن يكون مجموعة أو تفصيلي.")
            raw_rows.append({"excel_row": excel_row, "code": code, "name_ar": name, "parent_code": parent_code,
                             "nature": nature, "is_group": is_group, "errors": errors, "warnings": [], "status": "جاهز"})
        workbook.close()
    except Exception:
        return {"errors": ["تعذر قراءة الملف. تأكد أنه ملف Excel سليم وغير محمي بكلمة مرور."], "rows": [], "summary": {}}

    by_code = {}
    for row in raw_rows:
        if row["code"]:
            by_code.setdefault(row["code"], []).append(row)
    for code, matches in by_code.items():
        if len(matches) > 1:
            for row in matches:
                row["errors"].append(f"الكود {code} مكرر داخل الملف (الصفوف: {', '.join(str(x['excel_row']) for x in matches)}).")

    existing = {a.code: a for a in ChartOfAccount.objects.filter(company=company).select_related("parent")}
    for row in raw_rows:
        if row["parent_code"] == row["code"]:
            row["errors"].append("لا يمكن أن يكون الحساب أبًا لنفسه.")
        parent_row = by_code.get(row["parent_code"], [None])[0] if row["parent_code"] else None
        parent_account = existing.get(row["parent_code"]) if row["parent_code"] else None
        if row["parent_code"] and not parent_row and not parent_account:
            row["errors"].append(f"الحساب الأب {row['parent_code']} غير موجود.")
        if parent_row and not parent_row["is_group"]:
            row["errors"].append(f"الحساب الأب {row['parent_code']} ليس مجموعة.")
        if parent_account and not parent_account.is_group:
            row["errors"].append(f"الحساب الأب {row['parent_code']} الموجود بالشركة ليس مجموعة.")
        current = existing.get(row["code"])
        if current:
            current_parent = current.parent.code if current.parent_id else None
            same = (current.name_ar == row["name_ar"] and current.nature == row["nature"]
                    and current_parent == row["parent_code"] and current.is_group == row["is_group"])
            if same:
                row["status"] = "موجود مطابق - سيتم تخطيه"
            else:
                row["errors"].append(
                    f"تعارض في الكود {row['code']}: الموجود «{current.name_ar}» "
                    f"والملف «{row['name_ar']}» أو اختلاف في الأب/الطبيعة/التصنيف."
                )
        if row["errors"]:
            row["status"] = "يحتاج مراجعة"

    # Detect cycles in parent links among rows.
    for row in raw_rows:
        seen = {row["code"]}
        parent = row["parent_code"]
        while parent in by_code and by_code[parent]:
            if parent in seen:
                row["errors"].append("يوجد تسلسل دائري في الحسابات الأب.")
                row["status"] = "يحتاج مراجعة"
                break
            seen.add(parent)
            parent = by_code[parent][0]["parent_code"]

    # Similar names are warnings only; never merge or rename accounts automatically.
    existing_names = {}
    for account in existing.values():
        existing_names.setdefault(_norm(account.name_ar), []).append(account.code)
    uploaded_names = {}
    for row in raw_rows:
        if row["name_ar"]:
            uploaded_names.setdefault(_norm(row["name_ar"]), []).append(row["code"])
    for row in raw_rows:
        name_key = _norm(row["name_ar"])
        other_codes = [code for code in existing_names.get(name_key, []) if code != row["code"]]
        other_codes += [code for code in uploaded_names.get(name_key, []) if code != row["code"]]
        if other_codes:
            row["warnings"].append("اسم مشابه/مكرر مع كود آخر (" + "، ".join(sorted(set(other_codes))) + ")؛ لن يتم الدمج تلقائيًا.")
    errors = [f"الصف {row['excel_row']} - {message}" for row in raw_rows for message in row["errors"]]
    ready = sum(1 for row in raw_rows if not row["errors"] and row["status"] == "جاهز")
    reused = sum(1 for row in raw_rows if not row["errors"] and row["status"].startswith("موجود"))
    return {"errors": errors, "rows": raw_rows, "summary": {
        "total": len(raw_rows), "ready_to_create": ready, "already_exists": reused,
        "blocked": sum(1 for row in raw_rows if row["errors"]),
        "warnings": sum(1 for row in raw_rows if row["warnings"]),
    }}


def import_account_rows(company, rows):
    with transaction.atomic():
        pending = {row["code"]: row for row in rows if not row["errors"] and row["status"] == "جاهز"}
        created = []
        while pending:
            progressed = False
            for code, row in list(pending.items()):
                parent = None
                if row["parent_code"]:
                    parent = ChartOfAccount.objects.filter(company=company, code=row["parent_code"]).first()
                    if parent is None:
                        continue
                    if not parent.is_group:
                        raise ValueError(f"الحساب الأب {row['parent_code']} ليس مجموعة.")
                account = ChartOfAccount(
                    company=company, code=row["code"], name_ar=row["name_ar"], parent=parent,
                    nature=row["nature"], is_group=row["is_group"], allow_posting=not row["is_group"],
                )
                account.full_clean()
                account.save()
                created.append(account.code)
                del pending[code]
                progressed = True
            if not progressed:
                raise ValueError("تعذر ترتيب الحسابات حسب الحساب الأب؛ راجع العلاقات الدائرية أو المفقودة.")
        return created
