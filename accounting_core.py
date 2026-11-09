"""Shared NEXUS ERP accounting posting and reconciliation primitives."""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

CENT = Decimal("0.01")

def amt(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)

def post_journal(conn, entry_date, reference, description, lines, created_by=None):
    """Idempotently post a balanced journal inside the caller's DB transaction."""
    normalized=[]
    for account_id,debit,credit,memo in lines:
        dr,cr=amt(debit),amt(credit)
        if dr<0 or cr<0 or (dr and cr):
            raise ValueError("كل سطر يجب أن يكون مدينًا أو دائنًا فقط وبقيمة غير سالبة.")
        if dr or cr:
            account=conn.execute("SELECT id,is_group,active FROM accounts WHERE id=?",(account_id,)).fetchone()
            if not account or account["is_group"] or not account["active"]:
                raise ValueError("لا يمكن الترحيل إلى حساب غير موجود أو غير نشط أو حساب تجميعي.")
            normalized.append((int(account_id),dr,cr,str(memo or "")))
    if len(normalized)<2:
        raise ValueError("القيد يحتاج إلى سطرين على الأقل.")
    debit=sum((x[1] for x in normalized),Decimal("0"))
    credit=sum((x[2] for x in normalized),Decimal("0"))
    if debit<=0 or debit!=credit:
        raise ValueError(f"القيد غير متوازن: مدين {debit} مقابل دائن {credit}.")
    existing=conn.execute("SELECT id FROM journal_entries WHERE reference=? AND status='posted' ORDER BY id LIMIT 1",(reference,)).fetchone()
    if existing:
        return existing["id"]
    cur=conn.execute(
      "INSERT INTO journal_entries(entry_date,reference,description,status,created_by,created_at) VALUES(?,?,?,'posted',?,?)",
      (entry_date,reference,description,created_by,datetime.now().isoformat(timespec="seconds")))
    entry_id=cur.lastrowid
    conn.executemany(
      "INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?)",
      [(entry_id,aid,float(dr),float(cr),memo) for aid,dr,cr,memo in normalized])
    return entry_id

def reconciliation_snapshot(conn):
    unbalanced=conn.execute("""SELECT je.id,je.reference,
      ROUND(COALESCE(SUM(jl.debit),0),2) debit,ROUND(COALESCE(SUM(jl.credit),0),2) credit
      FROM journal_entries je LEFT JOIN journal_lines jl ON jl.entry_id=je.id
      WHERE je.status='posted' GROUP BY je.id
      HAVING ABS(COALESCE(SUM(jl.debit),0)-COALESCE(SUM(jl.credit),0))>0.005""").fetchall()
    journal_totals=conn.execute("""SELECT ROUND(COALESCE(SUM(debit),0),2) debit,
      ROUND(COALESCE(SUM(credit),0),2) credit FROM journal_lines jl
      JOIN journal_entries je ON je.id=jl.entry_id WHERE je.status='posted'""").fetchone()
    stock=conn.execute("""SELECT COALESCE(SUM(CAST(qty_remaining AS REAL)*CAST(unit_cost AS REAL)),0) value
      FROM stock_valuation_layers WHERE CAST(qty_remaining AS REAL)>0""").fetchone()
    settings={r["setting_key"]:r["setting_value"] for r in conn.execute(
      "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity')")}
    inv_codes={"commercial":"1150","industrial":"1130","medical":"1150","restaurant":"1140"}
    inv_code=inv_codes.get(settings.get("activity","commercial"))
    inv_account=conn.execute("SELECT id FROM accounts WHERE code=?",(inv_code,)).fetchone() if inv_code else None
    gl_stock=conn.execute("SELECT COALESCE(SUM(debit-credit),0) v FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id WHERE je.status='posted' AND jl.account_id=?",(inv_account["id"],)).fetchone()["v"] if inv_account else None
    return {"unbalanced":unbalanced,"journal_totals":journal_totals,
      "stock_value":round(float(stock["value"] or 0),2),
      "gl_stock":round(float(gl_stock),2) if gl_stock is not None else None,
      "stock_difference":round(float(gl_stock)-float(stock["value"]),2) if gl_stock is not None else None}
