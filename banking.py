from app_paths import DB_PATH
from database import connect_database, fetch_page
"""Bank register and reconciliation support for NEXUS ERP."""
from datetime import date, datetime
import csv
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal, decimal_net_expression
from chart_templates import ensure_account_metadata
from db_compat import table_columns, table_exists, begin_write_transaction, execute_script, DB_ERROR, transactional_connection_initializer
from db_sql import execute_compat
from db_insert import insert_one_returning_id

bank_bp = Blueprint("bank", __name__, url_prefix="/bank")
BANK_ERRORS = (ValueError, InvalidOperation, KeyError) + DB_ERROR

def db():
    return connect_database(DB_PATH, isolation_level=None)


def _table_exists(conn, name):
    """Database-neutral table existence check; SQL execution remains opt-in."""
    return table_exists(conn, name)


def _columns(conn, name):
    """Database-neutral column metadata for a known table."""
    return table_columns(conn, name)


def signed_ledger_amount(debit, credit):
    """Convert a posted bank-ledger line to statement direction: debit positive, credit negative."""
    return (Decimal(str(debit or "0"))-Decimal(str(credit or "0"))).quantize(Decimal("0.01"))


def parse_statement_csv(payload, max_rows=5000):
    """Parse a bank CSV export without writing anything to the database."""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("ملف كشف البنك يجب أن يكون CSV بترميز UTF-8.") from exc
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("ملف كشف البنك فارغ.")
    try:
        sample = payload[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(payload, newline=""), dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("ملف CSV لا يحتوي على صف عناوين.")
        normalized = {str(name or "").strip().casefold(): name for name in reader.fieldnames}
        def column(*aliases):
            for alias in aliases:
                key = alias.casefold()
                if key in normalized:
                    return normalized[key]
            return None
        date_col = column("date", "transaction date", "posting date", "التاريخ", "تاريخ الحركة", "تاريخ العملية")
        amount_col = column("amount", "transaction amount", "signed amount", "المبلغ", "قيمة الحركة", "صافي المبلغ")
        ref_col = column("reference", "ref", "transaction reference", "رقم المرجع", "المرجع", "رقم العملية")
        desc_col = column("description", "details", "narration", "البيان", "الوصف", "تفاصيل")
        if not date_col or not amount_col:
            raise ValueError("يلزم وجود عمود للتاريخ وعمود للمبلغ. الأعمدة الاختيارية: المرجع والبيان.")
        rows = []
        for row_number, row in enumerate(reader, start=2):
            if not any(str(value or "").strip() for value in row.values()):
                continue
            if len(rows) >= max_rows:
                raise ValueError(f"الحد الأقصى لاستيراد كشف البنك هو {max_rows} حركة في الملف الواحد.")
            raw_date = str(row.get(date_col) or "").strip()
            parsed_date = None
            for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                try:
                    parsed_date = datetime.strptime(raw_date, date_format).date()
                    break
                except ValueError:
                    continue
            if parsed_date is None:
                raise ValueError(f"تاريخ غير صحيح في السطر {row_number}: {raw_date}")
            raw_amount = str(row.get(amount_col) or "").strip().replace(" ", "").replace("\u00a0", "")
            if "," in raw_amount and "." not in raw_amount:
                if raw_amount.count(",") == 1 and len(raw_amount.rsplit(",", 1)[1]) in (1, 2):
                    raw_amount = raw_amount.replace(",", ".")
                elif not all(len(group) == 3 for group in raw_amount.lstrip("+-").split(",")[1:]):
                    raise ValueError(f"تنسيق مبلغ ملتبس في السطر {row_number}; استخدم فاصلة آلاف واضحة أو نقطة عشرية.")
                else:
                    raw_amount = raw_amount.replace(",", "")
            else:
                raw_amount = raw_amount.replace(",", "")
            try:
                amount = Decimal(raw_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except InvalidOperation as exc:
                raise ValueError(f"مبلغ غير صحيح في السطر {row_number}.") from exc
            if amount == 0:
                raise ValueError(f"المبلغ في السطر {row_number} لا يمكن أن يكون صفرًا.")
            rows.append({
                "statement_date": parsed_date.isoformat(),
                "signed_amount": amount,
                "bank_reference": str(row.get(ref_col) or "").strip() if ref_col else "",
                "description": str(row.get(desc_col) or "").strip() if desc_col else "",
            })
        if not rows:
            raise ValueError("لا توجد حركات صالحة للاستيراد في الملف.")
        return rows
    except csv.Error as exc:
        raise ValueError("تعذر قراءة ملف CSV؛ راجع الفواصل وصف العناوين.") from exc

def opening_reconciliation_state(balances, ledger_opening, book_total):
    """Return an explicit, safe bank opening/closing reconciliation state."""
    ledger_opening = Decimal(str(ledger_opening or "0")).quantize(Decimal("0.01"))
    book_total = Decimal(str(book_total or "0")).quantize(Decimal("0.01"))
    if balances is None:
        return {
            "balances_entered": False, "opening_balance": None, "closing_balance": None,
            "opening_ledger_balance": ledger_opening, "opening_difference": None,
            "opening_reviewed": False, "opening_review_note": "", "opening_ready": False,
            "expected_closing_balance": None, "closing_difference": None,
        }
    opening = Decimal(str(balances["opening_balance"])).quantize(Decimal("0.01"))
    closing = Decimal(str(balances["closing_balance"])).quantize(Decimal("0.01"))
    opening_difference = opening - ledger_opening
    reviewed = bool(balances["opening_reviewed"])
    ready = opening_difference == 0 or reviewed
    expected = opening + book_total
    return {
        "balances_entered": True, "opening_balance": opening, "closing_balance": closing,
        "opening_ledger_balance": ledger_opening, "opening_difference": opening_difference,
        "opening_reviewed": reviewed, "opening_review_note": balances["opening_review_note"] or "",
        "opening_ready": ready, "expected_closing_balance": expected,
        "closing_difference": closing - expected if ready else None,
    }


def count_same_amount_candidates(rows, amount):
    """Count posted bank-ledger rows matching the statement signed amount."""
    target = Decimal(str(amount)).quantize(Decimal("0.01"))
    return sum(1 for row in rows if signed_ledger_amount(row["debit"], row["credit"]) == target)


def refresh_bank_transaction_reconciled(conn, entry_id):
    """Mark a bank transaction reconciled only after every bank-side journal line is matched."""
    tx=execute_compat(conn, """SELECT id,bank_account_id,destination_bank_account_id
        FROM bank_transactions WHERE journal_entry_id=?""",(entry_id,)).fetchone()
    if not tx:
        return False
    bank_ids=sorted({int(x) for x in (tx["bank_account_id"],tx["destination_bank_account_id"]) if x is not None})
    marks=",".join("?" for _ in bank_ids)
    unmatched=execute_compat(conn, f"""SELECT COUNT(*) FROM journal_lines jl
        LEFT JOIN bank_statement_lines sl ON sl.matched_journal_line_id=jl.id
        WHERE jl.entry_id=? AND jl.account_id IN ({marks}) AND sl.id IS NULL""",
        (entry_id,*bank_ids)).fetchone()[0]
    cur=execute_compat(conn, "UPDATE bank_transactions SET reconciled=? WHERE id=?",
        (1 if int(unmatched or 0)==0 else 0,tx["id"]))
    return cur.rowcount==1


def bank_opening_ledger_balance(conn, account_id, period_start):
    """Return bank opening balance without later-year opening copies already represented in prior history."""
    from datetime import timedelta
    from management_dashboard import _copied_opening_entry_ids
    cutoff=(date.fromisoformat(period_start)-timedelta(days=1)).isoformat()
    excluded=sorted(_copied_opening_entry_ids(conn,cutoff))
    exclusion=(" AND je.id NOT IN ("+",".join("?" for _ in excluded)+")") if excluded else ""
    rows=execute_compat(conn, f"""SELECT jl.debit,jl.credit FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        WHERE jl.account_id=? AND je.status='posted' AND je.entry_date<?{exclusion}""",
        (account_id,period_start,*excluded)).fetchall()
    return sum((signed_ledger_amount(row["debit"],row["credit"]) for row in rows),Decimal("0.00"))


def save_statement_line_match(c, line_id, journal_line_id, matched_at, matched_by):
    """Claim an unmatched statement line exactly once, even if callers race."""
    updated = execute_compat(c, """UPDATE bank_statement_lines
        SET matched_journal_line_id=?,matched_at=?,matched_by=?
        WHERE id=? AND matched_transaction_id IS NULL
          AND matched_journal_line_id IS NULL""",
        (journal_line_id, matched_at, matched_by, line_id)).rowcount
    if updated != 1:
        raise ValueError("سطر كشف البنك تمت مطابقته بالفعل أو لم يعد متاحًا للمطابقة.")


@transactional_connection_initializer(lambda: db())
def init_bank_schema(c):
    execute_script(c, """
    CREATE TABLE IF NOT EXISTS bank_transactions(
      id INTEGER PRIMARY KEY, transaction_no TEXT NOT NULL UNIQUE,
      transaction_date TEXT NOT NULL, bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
      transaction_type TEXT NOT NULL CHECK(transaction_type IN ('deposit','withdrawal','transfer','fee')),
      amount TEXT NOT NULL, counter_account_id INTEGER REFERENCES accounts(id),
      destination_bank_account_id INTEGER REFERENCES accounts(id),
      description TEXT NOT NULL, bank_reference TEXT NOT NULL DEFAULT '',
      journal_entry_id INTEGER UNIQUE, reconciled INTEGER NOT NULL DEFAULT 0,
      statement_date TEXT, statement_reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_bank_tx_date ON bank_transactions(transaction_date,bank_account_id);
    CREATE TABLE IF NOT EXISTS cash_accounts(
      id INTEGER PRIMARY KEY,
      kind TEXT NOT NULL CHECK(kind IN ('cash','bank')),
      name_ar TEXT NOT NULL,
      account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id),
      branch_id INTEGER REFERENCES branches(id),
      currency_code TEXT NOT NULL DEFAULT 'EGP' REFERENCES currencies(code),
      bank_name TEXT NOT NULL DEFAULT '',
      iban TEXT NOT NULL DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1,
      opening_date TEXT NOT NULL,
      created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_cash_accounts_kind_active ON cash_accounts(kind,active);
    CREATE TABLE IF NOT EXISTS bank_statement_lines(
      id INTEGER PRIMARY KEY,
      bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
      statement_date TEXT NOT NULL,
      signed_amount TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      bank_reference TEXT NOT NULL DEFAULT '',
      matched_transaction_id INTEGER REFERENCES bank_transactions(id),
      matched_journal_line_id INTEGER REFERENCES journal_lines(id),
      matched_side TEXT CHECK(matched_side IN ('source','destination')),
      matched_at TEXT,
      matched_by INTEGER,
      created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_bank_statement_account_date
      ON bank_statement_lines(bank_account_id,statement_date,id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_statement_tx_side
      ON bank_statement_lines(matched_transaction_id,matched_side)
      WHERE matched_transaction_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS bank_statement_balances(
      id INTEGER PRIMARY KEY,
      bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
      period_start TEXT NOT NULL,
      period_end TEXT NOT NULL,
      opening_balance TEXT NOT NULL,
      closing_balance TEXT NOT NULL,
      created_by INTEGER,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(bank_account_id,period_start,period_end)
    );
    """)
    statement_columns=_columns(c, "bank_statement_lines")
    if "matched_journal_line_id" not in statement_columns:
        execute_compat(c, "ALTER TABLE bank_statement_lines ADD COLUMN matched_journal_line_id INTEGER REFERENCES journal_lines(id)")
    balance_columns=_columns(c, "bank_statement_balances")
    if "opening_reviewed" not in balance_columns:
        execute_compat(c, "ALTER TABLE bank_statement_balances ADD COLUMN opening_reviewed INTEGER NOT NULL DEFAULT 0")
    if "opening_review_note" not in balance_columns:
        execute_compat(c, "ALTER TABLE bank_statement_balances ADD COLUMN opening_review_note TEXT NOT NULL DEFAULT ''")
    execute_compat(c, """CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_statement_journal_line
        ON bank_statement_lines(matched_journal_line_id)
        WHERE matched_journal_line_id IS NOT NULL""")

def require(permission="manage"):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if "user_id" not in session: return redirect(url_for("login"))
            required = "bank.manage" if request.method == "POST" else "bank.view"
            if (required not in set(session["permissions"])) if "permissions" in session else (session.get("role") not in {"admin","accountant"}):
                flash("لا تملك الصلاحية المطلوبة.","error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco

@bank_bp.route("/", methods=["GET","POST"])
@require()
def index():
    c=db()
    if request.method=="POST":
        try:
            base_currency_row=execute_compat(c, "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'").fetchone()
            base_currency=base_currency_row["setting_value"] if base_currency_row else "EGP"
            kind=request.form["transaction_type"]
            bank_id=int(request.form["bank_account_id"])
            amount=Decimal(request.form["amount"]).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            if amount<=0: raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")
            def valid_account(aid):
                r=execute_compat(c, "SELECT id,code,name_ar,account_type,is_group,active FROM accounts WHERE id=?",(aid,)).fetchone()
                if not r or r["account_type"]!="asset" or r["is_group"] or not r["active"]:
                    raise ValueError("اختر حساب بنك/نقد تفصيليًا ونشطًا.")
                registry=execute_compat(c, "SELECT active,currency_code,branch_id FROM cash_accounts WHERE account_id=?",(aid,)).fetchone()
                if registry and not registry["active"]:
                    raise ValueError("الخزنة أو البنك معطل؛ فعّله أولًا قبل تسجيل حركة جديدة.")
                if registry and registry["currency_code"]!=base_currency:
                    raise ValueError("حسابات الخزائن والبنوك في هذه النسخة تُرحّل بالعملة الأساسية فقط؛ لا تسجل حركة بعملة أجنبية قبل تفعيل معالجة فروق وأسعار الصرف.")
                if registry and registry["branch_id"] and not execute_compat(c, 
                    "SELECT 1 FROM branches WHERE id=? AND active=1",(registry["branch_id"],)
                ).fetchone():
                    raise ValueError("الفرع المرتبط بالخزنة/البنك غير نشط؛ راجع ربط الحساب بالفرع قبل تسجيل الحركة.")
                if not registry and not any(term in (r["name_ar"] or "") for term in ("صندوق","بنك","بنوك","مصرف")):
                    raise ValueError("الحساب غير معرف كخزنة أو بنك. أضفه أولًا من شاشة إدارة الخزائن والبنوك.")
                return aid
            valid_account(bank_id)
            counter_id=int(request.form["counter_account_id"]) if request.form.get("counter_account_id") else None
            destination_id=int(request.form["destination_bank_account_id"]) if request.form.get("destination_bank_account_id") else None
            if kind=="transfer":
                valid_account(destination_id)
                if destination_id==bank_id: raise ValueError("حساب التحويل المستفيد يجب أن يختلف عن حساب المصدر.")
                source_currency=execute_compat(c, "SELECT currency_code FROM cash_accounts WHERE account_id=? AND active=1",(bank_id,)).fetchone()
                target_currency=execute_compat(c, "SELECT currency_code FROM cash_accounts WHERE account_id=? AND active=1",(destination_id,)).fetchone()
                if source_currency and target_currency and source_currency["currency_code"]!=target_currency["currency_code"]:
                    raise ValueError("لا يمكن تحويل نفس المبلغ بين خزنة/بنك بعملتين مختلفتين؛ استخدم قيد صرف عملة مستقلًا.")
                source_registry=execute_compat(c, "SELECT branch_id FROM cash_accounts WHERE account_id=? AND active=1",(bank_id,)).fetchone()
                target_registry=execute_compat(c, "SELECT branch_id FROM cash_accounts WHERE account_id=? AND active=1",(destination_id,)).fetchone()
                source_dimensions={"branch_id":source_registry["branch_id"]} if source_registry and source_registry["branch_id"] else {}
                target_dimensions={"branch_id":target_registry["branch_id"]} if target_registry and target_registry["branch_id"] else {}
                lines=[(destination_id,amount,0,"تحويل وارد إلى حساب النقدية/البنك",target_dimensions),
                       (bank_id,0,amount,"تحويل صادر من حساب النقدية/البنك",source_dimensions)]
            else:
                if not counter_id: raise ValueError("حدد الحساب المقابل للحركة.")
                counter=execute_compat(c, "SELECT id,code,name_ar,is_group,active FROM accounts WHERE id=?",(counter_id,)).fetchone()
                if not counter or counter["is_group"] or not counter["active"]: raise ValueError("الحساب المقابل يجب أن يكون تفصيليًا ونشطًا.")
                if execute_compat(c, "SELECT 1 FROM cash_accounts WHERE account_id=?",(counter_id,)).fetchone() or any(
                    term in (counter["name_ar"] or "") for term in ("صندوق","بنك","بنوك","مصرف")
                ):
                    raise ValueError("الحساب المقابل خزنة أو بنك؛ استخدم نوع الحركة تحويل بين الخزائن والبنوك حتى يظهر الطرفان في دفترهما دون تكرار.")
                registry=execute_compat(c, "SELECT branch_id FROM cash_accounts WHERE account_id=? AND active=1",(bank_id,)).fetchone()
                branch_dimensions={"branch_id":registry["branch_id"]} if registry and registry["branch_id"] else {}
                if kind in ("deposit",): lines=[(bank_id,amount,0,"إيداع بنكي",branch_dimensions),(counter_id,0,amount,"الحساب المقابل",branch_dimensions)]
                elif kind in ("withdrawal","fee"): lines=[(counter_id,amount,0,"مصروف/سحب بنكي",branch_dimensions),(bank_id,0,amount,"الحساب البنكي",branch_dimensions)]
                else: raise ValueError("نوع الحركة غير صحيح.")
            begin_write_transaction(c)
            no="BANK-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            jid=post_journal(c,request.form["transaction_date"],no,request.form["description"].strip() or "حركة بنكية",lines,session["user_id"])
            bank_transaction_id = insert_one_returning_id(c, """INSERT INTO bank_transactions(transaction_no,transaction_date,bank_account_id,transaction_type,amount,counter_account_id,destination_bank_account_id,description,bank_reference,journal_entry_id,created_by)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(no,request.form["transaction_date"],bank_id,kind,str(amount),counter_id,destination_id,request.form["description"].strip() or "حركة بنكية",request.form.get("bank_reference","").strip(),jid,session["user_id"]))
            c.commit()
            g.audit_created_record = ("bank_transactions", bank_transaction_id, None, None)
            flash("تم تسجيل الحركة البنكية وترحيل قيدها المحاسبي.","success")
            return redirect(url_for("bank.index"))
        except BANK_ERRORS as e:
            c.rollback(); flash(str(e),"error")
    accounts=execute_compat(c, """SELECT a.id,a.code,a.name_ar,ca.kind,ca.name_ar display_name,ca.currency_code
        FROM cash_accounts ca JOIN accounts a ON a.id=ca.account_id
        WHERE ca.active=1 AND a.active=1
          AND ca.currency_code=(SELECT setting_value FROM company_settings WHERE setting_key='base_currency')
        UNION ALL
        SELECT a.id,a.code,a.name_ar,'bank',a.name_ar,NULL
        FROM accounts a WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
          AND (lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%' OR lower(a.name_ar) LIKE '%صندوق%')
          AND NOT EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id)
        ORDER BY code""").fetchall()
    all_accounts=execute_compat(c, """SELECT a.id,a.code,a.name_ar FROM accounts a
        WHERE a.is_group=0 AND a.active=1
          AND NOT EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id)
        ORDER BY a.code""").fetchall()
    bank_page = fetch_page(c,
        """SELECT t.*,a.code bank_code,a.name_ar bank_name FROM bank_transactions t
           JOIN accounts a ON a.id=t.bank_account_id ORDER BY t.transaction_date DESC,t.id DESC""",
        "SELECT COUNT(*) FROM bank_transactions",
        page=request.args.get("bank_page",1,type=int),
        per_page=request.args.get("bank_per_page",50,type=int))
    rows=bank_page["rows"]
    bank_net = decimal_net_expression(
        c,
        "CASE WHEN je.status='posted' THEN jl.debit ELSE '0' END",
        "CASE WHEN je.status='posted' THEN jl.credit ELSE '0' END",
    )
    bank_balances=execute_compat(c, f"""SELECT a.id,a.code,a.name_ar,
        {bank_net} balance
        FROM accounts a LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON je.id=jl.entry_id
        WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
          AND (EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id AND ca.active=1
              AND ca.currency_code=(SELECT setting_value FROM company_settings WHERE setting_key='base_currency'))
            OR lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%' OR lower(a.name_ar) LIKE '%صندوق%')
        GROUP BY a.id ORDER BY a.code""").fetchall()
    bank_balances=[dict(row,balance=Decimal(str(row["balance"] or "0"))) for row in bank_balances]
    c.close()
    return render_template("bank.html",bank_accounts=accounts,accounts=all_accounts,rows=rows,bank_balances=bank_balances,bank_page=bank_page,today=date.today().isoformat())

@bank_bp.route("/reconcile/<int:transaction_id>",methods=["POST"])
@require()
def reconcile(transaction_id):
    c=db()
    row=execute_compat(c, "SELECT bank_account_id FROM bank_transactions WHERE id=?",(transaction_id,)).fetchone()
    c.close()
    if not row:
        flash("الحركة البنكية غير موجودة.","error")
        return redirect(url_for("bank.index"))
    flash("تم إيقاف المطابقة اليدوية حتى لا تُعلّم الحركة كمطابقة دون كشف. استخدم شاشة التسوية البنكية لمطابقة سطر الكشف مع سطر الأستاذ.","warning")
    return redirect(url_for("bank.statement_reconciliation",account_id=row["bank_account_id"]))


@bank_bp.route("/reconciliation", methods=["GET", "POST"])
@require()
def statement_reconciliation():
    c=db()
    try:
        if request.method=="POST":
            action=request.form.get("action","add_line")

            if action=="import_csv":
                account_id=request.form.get("bank_account_id",type=int)
                period_start=request.form.get("start_date","").strip()
                period_end=request.form.get("end_date","").strip()
                date.fromisoformat(period_start); date.fromisoformat(period_end)
                if period_end < period_start:
                    raise ValueError("نهاية الفترة لا يجوز أن تسبق بدايتها.")
                upload=request.files.get("statement_file")
                if not upload or not upload.filename:
                    raise ValueError("اختر ملف كشف بنك بصيغة CSV.")
                payload=upload.stream.read(5*1024*1024+1)
                if len(payload)>5*1024*1024:
                    raise ValueError("حجم ملف كشف البنك يتجاوز 5 ميجابايت.")
                imported=parse_statement_csv(payload)
                if any(not (period_start <= row["statement_date"] <= period_end) for row in imported):
                    raise ValueError("يوجد تاريخ حركة خارج فترة التسوية المحددة.")
                account=execute_compat(c, """SELECT a.id FROM accounts a
                    WHERE a.id=? AND a.account_type='asset' AND a.is_group=0 AND a.active=1
                    AND (EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id AND ca.kind='bank' AND ca.active=1
                        AND ca.currency_code=(SELECT setting_value FROM company_settings WHERE setting_key='base_currency'))
                      OR lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%')""",
                    (account_id,)).fetchone()
                if not account:
                    raise ValueError("اختر حساب بنك نشطًا من سجل الخزائن والبنوك.")
                begin_write_transaction(c)
                inserted=0
                for row in imported:
                    duplicate=execute_compat(c, """SELECT id FROM bank_statement_lines
                        WHERE bank_account_id=? AND statement_date=? AND signed_amount=?
                          AND bank_reference=? AND description=? LIMIT 1""",
                        (account_id,row["statement_date"],str(row["signed_amount"]),
                         row["bank_reference"],row["description"])).fetchone()
                    if duplicate:
                        continue
                    insert_one_returning_id(c, """INSERT INTO bank_statement_lines(
                        bank_account_id,statement_date,signed_amount,description,bank_reference,created_by)
                        VALUES(?,?,?,?,?,?)""",
                        (account_id,row["statement_date"],str(row["signed_amount"]),
                         row["description"],row["bank_reference"],session.get("user_id")))
                    inserted+=1
                if inserted == 0:
                    raise ValueError("كل حركات الملف موجودة مسبقًا؛ لم تتم إضافة أي حركة مكررة.")
                execute_compat(c, """INSERT INTO audit_log(user_id,action,details,created_at)
                    VALUES(?,?,?,?)""",
                    (session.get("user_id"),"bank_statement_csv_import",
                     f"استيراد كشف بنك للحساب {account_id}: {inserted} حركة جديدة من أصل {len(imported)}؛ الفترة {period_start} إلى {period_end}",
                     datetime.now().isoformat(timespec="seconds")))
                c.commit()
                flash(f"تم استيراد {inserted} حركة جديدة، وتم تجاوز {len(imported)-inserted} حركة مكررة.", "success")
                return redirect(url_for("bank.statement_reconciliation",account_id=account_id,
                    start_date=period_start,end_date=period_end))
            if action=="add_line":
                account_id=request.form.get("bank_account_id",type=int)
                statement_date=request.form.get("statement_date","").strip()
                amount=Decimal(request.form.get("signed_amount","")).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
                reference=request.form.get("bank_reference","").strip()
                description=request.form.get("description","").strip()
                if amount==0: raise ValueError("قيمة حركة كشف البنك لا يمكن أن تكون صفرًا.")
                date.fromisoformat(statement_date)
                account=execute_compat(c, """SELECT a.id FROM accounts a
                    WHERE a.id=? AND a.account_type='asset' AND a.is_group=0 AND a.active=1
                    AND (EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id AND ca.kind='bank' AND ca.active=1
                        AND ca.currency_code=(SELECT setting_value FROM company_settings WHERE setting_key='base_currency'))
                      OR lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%')""",
                    (account_id,)).fetchone()
                if not account: raise ValueError("اختر حساب بنك نشطًا من سجل الخزائن والبنوك.")
                duplicate=execute_compat(c, """SELECT id FROM bank_statement_lines
                    WHERE bank_account_id=? AND statement_date=? AND signed_amount=? AND bank_reference=?""",
                    (account_id,statement_date,str(amount),reference)).fetchone() if reference else None
                if duplicate: raise ValueError("سطر كشف البنك موجود بالفعل بنفس الحساب والتاريخ والمبلغ والمرجع.")
                statement_line_id=insert_one_returning_id(c, """INSERT INTO bank_statement_lines(
                    bank_account_id,statement_date,signed_amount,description,bank_reference,created_by)
                    VALUES(?,?,?,?,?,?)""",
                    (account_id,statement_date,str(amount),description,reference,session.get("user_id")))
                c.commit()
                g.audit_created_record=("bank_statement_lines",statement_line_id,None,None)
                flash("تمت إضافة سطر كشف البنك للمطابقة.","success")
                return redirect(url_for("bank.statement_reconciliation",account_id=account_id,
                    start_date=request.form.get("start_date") or None,end_date=request.form.get("end_date") or None))
            if action=="save_balances":
                account_id=request.form.get("bank_account_id",type=int)
                period_start=request.form.get("start_date","").strip()
                period_end=request.form.get("end_date","").strip()
                opening=Decimal(request.form.get("opening_balance","0")).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
                closing=Decimal(request.form.get("closing_balance","0")).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
                date.fromisoformat(period_start); date.fromisoformat(period_end)
                if period_end<period_start: raise ValueError("تاريخ نهاية كشف البنك يجب ألا يسبق تاريخ بدايته.")
                if not execute_compat(c, """SELECT 1 FROM accounts a WHERE a.id=? AND a.account_type='asset'
                    AND a.is_group=0 AND a.active=1
                    AND (EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id AND ca.kind='bank' AND ca.active=1
                        AND ca.currency_code=(SELECT setting_value FROM company_settings WHERE setting_key='base_currency'))
                      OR lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%')""",
                    (account_id,)).fetchone():
                    raise ValueError("اختر حساب بنك تفصيليًا ونشطًا من سجل الخزائن والبنوك.")
                begin_write_transaction(c)
                execute_compat(c, """INSERT INTO bank_statement_balances(bank_account_id,period_start,period_end,opening_balance,closing_balance,created_by)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(bank_account_id,period_start,period_end) DO UPDATE SET
                    opening_balance=excluded.opening_balance,closing_balance=excluded.closing_balance,
                    opening_reviewed=0,opening_review_note='',
                    created_by=excluded.created_by,updated_at=CURRENT_TIMESTAMP""",
                    (account_id,period_start,period_end,str(opening),str(closing),session.get("user_id")))
                execute_compat(c, """INSERT INTO audit_log(user_id,action,details,created_at)
                    VALUES(?,?,?,?)""",(session.get("user_id"),"bank_statement_balance",
                    f"حفظ رصيد افتتاحي وختامي لكشف البنك للحساب {account_id} للفترة {period_start} إلى {period_end}",
                    datetime.now().isoformat(timespec="seconds")))
                c.commit()
                flash("تم حفظ رصيد بداية ونهاية كشف البنك للفترة.","success")
                return redirect(url_for("bank.statement_reconciliation",account_id=account_id,start_date=period_start,end_date=period_end))
            if action=="confirm_opening":
                account_id=request.form.get("bank_account_id",type=int)
                period_start=request.form.get("start_date","").strip()
                period_end=request.form.get("end_date","").strip()
                note=request.form.get("opening_review_note","").strip()
                date.fromisoformat(period_start); date.fromisoformat(period_end)
                if period_end<period_start: raise ValueError("تاريخ نهاية الفترة لا يجوز أن يسبق بدايتها.")
                if request.form.get("confirm_opening_review")!="1":
                    raise ValueError("أكد صراحة أنك راجعت فرق الرصيد الافتتاحي قبل تسجيل المراجعة.")
                if len(note)<10: raise ValueError("اكتب سببًا واضحًا لمراجعة فرق الرصيد الافتتاحي (10 أحرف على الأقل).")
                begin_write_transaction(c)
                updated=execute_compat(c, """UPDATE bank_statement_balances SET opening_reviewed=1,opening_review_note=?,updated_at=CURRENT_TIMESTAMP
                    WHERE bank_account_id=? AND period_start=? AND period_end=?""",
                    (note,account_id,period_start,period_end)).rowcount
                if not updated: raise ValueError("احفظ أرصدة كشف البنك أولًا قبل اعتماد مراجعة الرصيد الافتتاحي.")
                execute_compat(c, """INSERT INTO audit_log(user_id,action,details,created_at) VALUES(?,?,?,?)""",
                    (session.get("user_id"),"bank_opening_balance_review",
                     f"مراجعة فرق رصيد افتتاحي للبنك {account_id} للفترة {period_start} إلى {period_end}: {note}",
                     datetime.now().isoformat(timespec="seconds")))
                c.commit()
                flash("تم تسجيل مراجعة فرق الرصيد الافتتاحي. راجع العناصر القائمة قبل اعتماد التسوية النهائية.","warning")
                return redirect(url_for("bank.statement_reconciliation",account_id=account_id,start_date=period_start,end_date=period_end))
            if action=="match":
                # Lock before reading either row; competing requests re-check after the lock.
                begin_write_transaction(c)
                line_id=request.form.get("line_id",type=int)
                journal_line_id=request.form.get("journal_line_id",type=int)
                line=execute_compat(c, "SELECT * FROM bank_statement_lines WHERE id=?",(line_id,)).fetchone()
                journal_line=execute_compat(c, """SELECT jl.id,jl.account_id,jl.debit,jl.credit,je.id entry_id,
                    je.reference,je.description,je.entry_date
                    FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE jl.id=? AND je.status='posted'""",(journal_line_id,)).fetchone()
                if not line or not journal_line: raise ValueError("سطر الكشف أو سطر الأستاذ غير موجود أو غير مرحّل.")
                if line["matched_transaction_id"] or line["matched_journal_line_id"]:
                    raise ValueError("سطر كشف البنك تمت مطابقته بالفعل.")
                if journal_line["account_id"]!=line["bank_account_id"]:
                    raise ValueError("سطر الأستاذ لا يخص حساب البنك المحدد.")
                expected=signed_ledger_amount(journal_line["debit"],journal_line["credit"])
                statement_amount=Decimal(str(line["signed_amount"])).quantize(Decimal("0.01"))
                if expected!=statement_amount:
                    raise ValueError("المبلغ أو اتجاه الحركة غير متطابق؛ المدين موجب والدائن سالب في كشف البنك.")
                period_start=request.form.get("start_date","").strip()
                period_end=request.form.get("end_date","").strip()
                date.fromisoformat(period_start); date.fromisoformat(period_end)
                if not (period_start<=line["statement_date"]<=period_end):
                    raise ValueError("سطر كشف البنك خارج فترة التسوية الحالية.")
                if not (period_start<=journal_line["entry_date"]<=period_end):
                    raise ValueError("القيد المختار خارج فترة التسوية الحالية.")
                possible_rows=execute_compat(c, """SELECT jl.debit,jl.credit FROM journal_lines jl
                    JOIN journal_entries je ON je.id=jl.entry_id
                    LEFT JOIN bank_statement_lines matched ON matched.matched_journal_line_id=jl.id
                    WHERE jl.account_id=? AND je.status='posted' AND je.entry_date BETWEEN ? AND ?
                      AND matched.id IS NULL""",
                    (line["bank_account_id"],period_start,period_end)).fetchall()
                possible=count_same_amount_candidates(possible_rows,expected)
                if possible>1 and request.form.get("confirm_multiple")!="1":
                    raise ValueError(f"يوجد {possible} قيود بنفس المبلغ والاتجاه. راجع رقم القيد والتاريخ والبيان، ثم أكد أنك اخترت القيد الصحيح.")
                already=execute_compat(c, "SELECT 1 FROM bank_statement_lines WHERE matched_journal_line_id=? LIMIT 1",
                                  (journal_line_id,)).fetchone()
                if already: raise ValueError("سطر الأستاذ تمت مطابقته مع سطر كشف آخر بالفعل.")
                save_statement_line_match(
                    c, line_id, journal_line_id,
                    datetime.now().isoformat(timespec="seconds"), session.get("user_id")
                )
                refresh_bank_transaction_reconciled(c,journal_line["entry_id"])
                execute_compat(c, """INSERT INTO audit_log(user_id,action,details,created_at)
                    VALUES(?,?,?,?)""",(session.get("user_id"),"bank_statement_match",
                    f"مطابقة سطر كشف #{line_id} مع سطر الأستاذ #{journal_line_id} بالقيد #{journal_line['entry_id']}؛ عدد القيود المحتملة بنفس المبلغ والاتجاه: {possible}",
                    datetime.now().isoformat(timespec="seconds")))
                c.commit()
                flash("تمت مطابقة سطر كشف البنك مع سطر الأستاذ بعد التحقق من الحساب والمبلغ والاتجاه.","success")
                return redirect(url_for("bank.statement_reconciliation",account_id=line["bank_account_id"],
                    start_date=period_start,end_date=period_end))
            raise ValueError("الإجراء المطلوب غير معروف.")
        account_id=request.args.get("account_id",type=int)
        accounts=execute_compat(c, """SELECT a.id,a.code,a.name_ar FROM accounts a
            WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
            AND (EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id AND ca.kind='bank' AND ca.active=1
                AND ca.currency_code=(SELECT setting_value FROM company_settings WHERE setting_key='base_currency'))
              OR lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%')
            ORDER BY a.code""").fetchall()
        if account_id is None and accounts:
            account_id=accounts[0]["id"]
        today=date.today()
        period_start=request.args.get("start_date",today.replace(day=1).isoformat())
        period_end=request.args.get("end_date",today.isoformat())
        if account_id:
            lines=execute_compat(c, """SELECT l.*,COALESCE(t.transaction_no,'قيد #'||mje.id) transaction_no
                FROM bank_statement_lines l
                LEFT JOIN bank_transactions t ON t.id=l.matched_transaction_id
                LEFT JOIN journal_lines mjl ON mjl.id=l.matched_journal_line_id
                LEFT JOIN journal_entries mje ON mje.id=mjl.entry_id
                WHERE l.bank_account_id=? AND l.statement_date BETWEEN ? AND ?
                ORDER BY l.statement_date,l.id""",(account_id,period_start,period_end)).fetchall()
            gl_lines=execute_compat(c, """SELECT jl.id journal_line_id,je.id entry_id,je.entry_date transaction_date,
                je.reference transaction_no,je.description,jl.memo,jl.debit,jl.credit,
                sl.id matched_statement_line_id
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                LEFT JOIN bank_statement_lines sl ON sl.matched_journal_line_id=jl.id
                WHERE jl.account_id=? AND je.status='posted' AND je.entry_date BETWEEN ? AND ?
                ORDER BY je.entry_date,je.id,jl.id""",(account_id,period_start,period_end)).fetchall()
            book_rows=[]
            book_total=Decimal("0")
            for gl_line in gl_lines:
                signed=signed_ledger_amount(gl_line["debit"],gl_line["credit"])
                book_total+=signed
                book_rows.append(dict(gl_line,signed_amount=signed))
            candidate_lines=[]
            for statement_line in lines:
                statement_row=dict(statement_line)
                statement_amount=Decimal(str(statement_row["signed_amount"])).quantize(Decimal("0.01"))
                statement_row["candidates"]=[book_row for book_row in book_rows
                    if not book_row["matched_statement_line_id"] and book_row["signed_amount"]==statement_amount]
                candidate_lines.append(statement_row)
            lines=candidate_lines
            statement_total=sum((Decimal(str(line["signed_amount"])) for line in lines),Decimal("0"))
            matched_count=sum(1 for line in lines if line["matched_transaction_id"] or line["matched_journal_line_id"])
            balances=execute_compat(c, """SELECT opening_balance,closing_balance,opening_reviewed,opening_review_note
                FROM bank_statement_balances WHERE bank_account_id=? AND period_start=? AND period_end=?""",
                (account_id,period_start,period_end)).fetchone()
            opening_ledger_balance=bank_opening_ledger_balance(c,account_id,period_start)
            reconciliation_state=opening_reconciliation_state(balances,opening_ledger_balance,book_total)
            balances_entered=reconciliation_state["balances_entered"]
            opening_balance=reconciliation_state["opening_balance"]
            closing_balance=reconciliation_state["closing_balance"]
            opening_ledger_balance=reconciliation_state["opening_ledger_balance"]
            opening_difference=reconciliation_state["opening_difference"]
            opening_reviewed=reconciliation_state["opening_reviewed"]
            opening_review_note=reconciliation_state["opening_review_note"]
            expected_closing_balance=reconciliation_state["expected_closing_balance"]
            opening_ready=reconciliation_state["opening_ready"]
            closing_difference=reconciliation_state["closing_difference"]
        else:
            lines=[]; book_rows=[]; statement_total=Decimal("0"); book_total=Decimal("0"); matched_count=0
            balances_entered=False; opening_balance=None; closing_balance=None; opening_ledger_balance=Decimal("0")
            opening_difference=None; opening_reviewed=False; opening_review_note=""; opening_ready=False
            expected_closing_balance=None; closing_difference=None
        c.close()
        return render_template("bank_reconciliation.html",accounts=accounts,account_id=account_id,
            lines=lines,book_rows=book_rows,statement_total=statement_total,book_total=book_total,
            difference=(statement_total-book_total) if lines else None,matched_count=matched_count,
            opening_balance=opening_balance,closing_balance=closing_balance,
            expected_closing_balance=expected_closing_balance,closing_difference=closing_difference,
            balances_entered=balances_entered,opening_ledger_balance=opening_ledger_balance,
            opening_difference=opening_difference,opening_reviewed=opening_reviewed,
            opening_review_note=opening_review_note,opening_ready=opening_ready,
            start_date=period_start,end_date=period_end,today=today.isoformat())
    except BANK_ERRORS as exc:
        c.rollback()
        flash(str(exc),"error")
        c.close()
        return redirect(url_for("bank.statement_reconciliation"))

@bank_bp.route("/accounts", methods=["GET", "POST"])
@require()
def cash_accounts():
    c=db()
    if request.method=="POST":
        try:
            begin_write_transaction(c)
            kind=request.form.get("kind","").strip()
            name=request.form.get("name_ar","").strip()
            mode=request.form.get("account_mode","existing")
            if kind not in {"cash","bank"} or not name:
                raise ValueError("حدد نوع الحساب واكتب اسم الخزنة أو البنك.")
            branch_id=request.form.get("branch_id",type=int)
            if branch_id and not execute_compat(c, "SELECT 1 FROM branches WHERE id=? AND active=1",(branch_id,)).fetchone():
                raise ValueError("الفرع المحدد غير موجود أو غير نشط.")
            currency=(request.form.get("currency_code") or "EGP").strip().upper()
            base_currency_row=execute_compat(c, "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'").fetchone()
            base_currency=base_currency_row["setting_value"] if base_currency_row else "EGP"
            if currency!=base_currency:
                raise ValueError("حاليًا تُرحّل حركات الخزائن والبنوك بالعملة الأساسية فقط؛ لا تختَر عملة أجنبية حتى تُضاف معالجة فروق وأسعار الصرف.")
            if not execute_compat(c, "SELECT 1 FROM currencies WHERE code=? AND active=1",(currency,)).fetchone():
                raise ValueError("العملة الأساسية غير مفعلة في إعدادات العملات.")
            if mode=="new":
                parent_id=request.form.get("parent_id",type=int)
                parent=execute_compat(c, "SELECT id,code,account_type,is_group,active FROM accounts WHERE id=?",(parent_id,)).fetchone()
                if not parent or parent["account_type"]!="asset" or not parent["is_group"] or not parent["active"] or str(parent["code"])!="1100":
                    raise ValueError("لإنشاء خزنة أو بنك اختر مجموعة الأصول المتداولة النقدية (مثل 1100) حتى يظهر الحساب في التصنيف الصحيح بالقوائم المالية.")
                code=None
                for sequence in range(1,1000):
                    candidate=str(parent["code"])+f"{sequence:03d}"
                    if not execute_compat(c, "SELECT 1 FROM accounts WHERE code=?",(candidate,)).fetchone():
                        code=candidate
                        break
                if not code:
                    raise ValueError("تعذر توليد كود حساب فرعي جديد تحت المجموعة المختارة.")
                account_name=("خزنة - " if kind=="cash" else "بنك - ")+name
                account_id=insert_one_returning_id(c, "INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group,active) VALUES(?,?,'asset',?,0,1)",
                              (code,account_name,parent_id))
                ensure_account_metadata(c)
            elif mode=="existing":
                account_id=request.form.get("account_id",type=int)
                account=execute_compat(c, "SELECT id,code,account_type,is_group,active FROM accounts WHERE id=?",(account_id,)).fetchone()
                if not account or account["account_type"]!="asset" or account["is_group"] or not account["active"]:
                    raise ValueError("اربط الحساب بحساب أصول تفصيلي ونشط فقط.")
                from activity_profiles import active_account_role_ids
                cash_role_ids=active_account_role_ids(c).get("cash", set())
                already_registered=execute_compat(c, "SELECT 1 FROM cash_accounts WHERE account_id=?",(account_id,)).fetchone()
                if account_id not in cash_role_ids and not already_registered:
                    raise ValueError("الحساب المحدد ليس ضمن حسابات النقدية/البنوك المعتمدة. اختر حسابًا من دور النقدية في ملف النشاط أو أنشئ حسابًا تحت مجموعة النقدية.")
            else:
                raise ValueError("طريقة ربط حساب الأستاذ غير صحيحة.")
            bank_name=request.form.get("bank_name","").strip() if kind=="bank" else ""
            iban=request.form.get("iban","").strip() if kind=="bank" else ""
            row_id=insert_one_returning_id(c, """INSERT INTO cash_accounts(kind,name_ar,account_id,branch_id,currency_code,bank_name,iban,opening_date,created_by)
                VALUES(?,?,?,?,?,?,?,?,?)""",(kind,name,account_id,branch_id,currency,bank_name,iban,
                request.form.get("opening_date") or date.today().isoformat(),session.get("user_id")))
            c.commit()
            g.audit_created_record=("cash_accounts",row_id,None,None)
            flash("تم ربط الخزنة/البنك بحساب أستاذ مستقل. ستظهر حركاته في دفتر الحساب المرتبط.","success")
            return redirect(url_for("bank.cash_accounts"))
        except BANK_ERRORS as exc:
            c.rollback()
            flash(str(exc),"error")
    rows=execute_compat(c, """SELECT ca.*,a.code account_code,a.name_ar account_name,
        b.code branch_code,b.name_ar branch_name
        FROM cash_accounts ca JOIN accounts a ON a.id=ca.account_id
        LEFT JOIN branches b ON b.id=ca.branch_id ORDER BY ca.kind,ca.name_ar""").fetchall()
    groups=execute_compat(c, "SELECT id,code,name_ar FROM accounts WHERE account_type='asset' AND is_group=1 AND active=1 ORDER BY code").fetchall()
    available=execute_compat(c, """SELECT a.id,a.code,a.name_ar FROM accounts a
        WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
        AND NOT EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id)
        ORDER BY a.code""").fetchall()
    branches=execute_compat(c, "SELECT id,code,name_ar FROM branches WHERE active=1 ORDER BY code").fetchall()
    base_currency_row=execute_compat(c, "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'").fetchone()
    base_currency=base_currency_row["setting_value"] if base_currency_row else "EGP"
    currencies=execute_compat(c, "SELECT code,name_ar FROM currencies WHERE code=? AND active=1",(base_currency,)).fetchall()
    c.close()
    return render_template("cash_accounts.html",rows=rows,groups=groups,available=available,
        branches=branches,currencies=currencies,today=date.today().isoformat())


@bank_bp.route("/accounts/<int:cash_account_id>/toggle", methods=["POST"])
@require()
def toggle_cash_account(cash_account_id):
    c=db()
    try:
        begin_write_transaction(c)
        row=execute_compat(c, "SELECT id,name_ar,active,account_id FROM cash_accounts WHERE id=?",(cash_account_id,)).fetchone()
        if not row:
            raise ValueError("الخزنة أو البنك غير موجود.")
        active=0 if row["active"] else 1
        if not active:
            balance_rows=execute_compat(c, """SELECT jl.debit,jl.credit FROM journal_lines jl
                JOIN journal_entries je ON je.id=jl.entry_id
                WHERE jl.account_id=? AND je.status='posted'""",(row["account_id"],)).fetchall()
            balance=sum((Decimal(str(item["debit"] or "0"))-Decimal(str(item["credit"] or "0"))
                         for item in balance_rows),Decimal("0"))
            if balance != 0:
                raise ValueError(f"لا يمكن تعطيل {row['name_ar']} لأن رصيد دفتر الأستاذ الحالي {balance:.2f}. صفّر الرصيد أو حوّله إلى حساب نقدية آخر أولًا.")
        execute_compat(c, "UPDATE cash_accounts SET active=? WHERE id=?",(active,cash_account_id))
        execute_compat(c, """INSERT INTO audit_log(user_id,action,details,created_at)
            VALUES(?,?,?,?)""",(session.get("user_id"),"cash_account_activated" if active else "cash_account_deactivated",
            ("تفعيل " if active else "تعطيل ")+row["name_ar"]+"؛ السجل والحركات التاريخية محفوظة.",
            datetime.now().isoformat(timespec="seconds")))
        c.commit()
        flash(("تم تفعيل " if active else "تم تعطيل ")+row["name_ar"]+" مع الاحتفاظ بالحركات والرصيد التاريخي.","success")
    except BANK_ERRORS as exc:
        c.rollback(); flash(str(exc),"error")
    finally:
        c.close()
    return redirect(url_for("bank.cash_accounts"))

if __name__=="__main__": init_bank_schema()
