"""Analytical expense reporting by account and optional business dimensions."""
from datetime import date
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from income_statement import closing_entries_exclusion_sql


def build_expense_analysis(conn, date_from, date_to, dimensions=None):
    register_decimal_aggregates(conn)
    dimensions = dimensions or {}
    conditions = [
        "je.status='posted'", "je.entry_date>=?", "je.entry_date<=?",
        closing_entries_exclusion_sql(conn), "a.account_type='expense'",
        "a.is_group=0", "a.active=1",
    ]
    params = [date_from, date_to]
    for key in ("branch_id", "cost_center_id", "project_id"):
        value = dimensions.get(key)
        if value is not None:
            conditions.append(f"jl.{key}=?")
            params.append(value)

    role_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='management_account_roles'"
    ).fetchone()
    direct_ids = {
        row["account_id"] for row in conn.execute(
            "SELECT account_id FROM management_account_roles WHERE role='direct_cost'"
        )
    } if role_table else set()

    rows = conn.execute(f"""SELECT a.id account_id,a.code,a.name_ar,
        jl.branch_id,jl.cost_center_id,jl.project_id,
        COALESCE(b.code,'') branch_code,COALESCE(b.name_ar,'غير محدد') branch_name,
        COALESCE(cc.code,'') cost_center_code,COALESCE(cc.name_ar,'غير محدد') cost_center_name,
        COALESCE(p.code,'') project_code,COALESCE(p.name_ar,'غير محدد') project_name,
        COALESCE(DECIMAL_SUM(jl.debit),0) debit,
        COALESCE(DECIMAL_SUM(jl.credit),0) credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        LEFT JOIN branches b ON b.id=jl.branch_id
        LEFT JOIN cost_centers cc ON cc.id=jl.cost_center_id
        LEFT JOIN projects p ON p.id=jl.project_id
        WHERE {' AND '.join(conditions)}
        GROUP BY a.id,jl.branch_id,jl.cost_center_id,jl.project_id
        ORDER BY a.code,branch_code,cost_center_code,project_code""", params).fetchall()

    detail_rows=[]
    total=Decimal("0")
    direct_total=Decimal("0")
    operating_total=Decimal("0")
    dimension_totals={}
    for row in rows:
        debit=Decimal(str(row["debit"] or "0"))
        credit=Decimal(str(row["credit"] or "0"))
        amount=debit-credit
        category="تكلفة مباشرة" if row["account_id"] in direct_ids else "مصروف تشغيلي / إداري"
        item={**dict(row),"debit":debit,"credit":credit,"amount":amount,"category":category}
        detail_rows.append(item)
        total+=amount
        if category=="تكلفة مباشرة":
            direct_total+=amount
        else:
            operating_total+=amount
        for key,label,code,name in (
            ("branch","الفرع",row["branch_code"],row["branch_name"]),
            ("cost_center","مركز التكلفة",row["cost_center_code"],row["cost_center_name"]),
            ("project","المشروع",row["project_code"],row["project_name"]),
        ):
            bucket=dimension_totals.setdefault((key,row[key+"_id"],code,name),{"dimension":label,"code":code,"name":name,"amount":Decimal("0")})
            bucket["amount"]+=amount

    month_rows=conn.execute(f"""SELECT substr(je.entry_date,1,7) month,
        COALESCE(DECIMAL_SUM(jl.debit-jl.credit),0) amount
        FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE {' AND '.join(conditions)}
        GROUP BY substr(je.entry_date,1,7) ORDER BY month""",params).fetchall()
    monthly=[{"month":r["month"],"amount":Decimal(str(r["amount"] or "0"))} for r in month_rows]
    return {
        "detail_rows":detail_rows,
        "dimension_rows":sorted(dimension_totals.values(),key=lambda x:(x["dimension"],-x["amount"])),
        "monthly_rows":monthly,
        "total":total,"direct_total":direct_total,"operating_total":operating_total,
        "transaction_count":len(detail_rows),
    }
