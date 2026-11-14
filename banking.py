from app_paths import DB_PATH
from database import connect_database, fetch_page
"""Bank register and reconciliation support for NEXUS ERP."""
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal
from code_sequences import next_code
from chart_templates import ensure_account_metadata

bank_bp = Blueprint("bank", __name__, url_prefix="/bank")

def db():
    return connect_database(DB_PATH, isolation_level=None)

def init_bank_schema():
    c = db()
    c.executescript("""
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
    """)
    c.close()

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
            kind=request.form["transaction_type"]
            bank_id=int(request.form["bank_account_id"])
            amount=Decimal(request.form["amount"]).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            if amount<=0: raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")
            def valid_account(aid):
                r=c.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?",(aid,)).fetchone()
                if not r or r["account_type"]!="asset" or r["is_group"] or not r["active"]:
                    raise ValueError("اختر حساب بنك/نقد تفصيليًا ونشطًا.")
                return aid
            valid_account(bank_id)
            counter_id=int(request.form["counter_account_id"]) if request.form.get("counter_account_id") else None
            destination_id=int(request.form["destination_bank_account_id"]) if request.form.get("destination_bank_account_id") else None
            if kind=="transfer":
                valid_account(destination_id)
                if destination_id==bank_id: raise ValueError("حساب التحويل المستفيد يجب أن يختلف عن حساب المصدر.")
                source_currency=c.execute("SELECT currency_code FROM cash_accounts WHERE account_id=? AND active=1",(bank_id,)).fetchone()
                target_currency=c.execute("SELECT currency_code FROM cash_accounts WHERE account_id=? AND active=1",(destination_id,)).fetchone()
                if source_currency and target_currency and source_currency["currency_code"]!=target_currency["currency_code"]:
                    raise ValueError("لا يمكن تحويل نفس المبلغ بين خزنة/بنك بعملتين مختلفتين؛ استخدم قيد صرف عملة مستقلًا.")
                lines=[(destination_id,amount,0,"تحويل وارد إلى حساب النقدية/البنك"),(bank_id,0,amount,"تحويل صادر من حساب النقدية/البنك")]
            else:
                if not counter_id: raise ValueError("حدد الحساب المقابل للحركة.")
                counter=c.execute("SELECT id,is_group,active FROM accounts WHERE id=?",(counter_id,)).fetchone()
                if not counter or counter["is_group"] or not counter["active"]: raise ValueError("الحساب المقابل يجب أن يكون تفصيليًا ونشطًا.")
                if kind in ("deposit",): lines=[(bank_id,amount,0,"إيداع بنكي"),(counter_id,0,amount,"الحساب المقابل")]
                elif kind in ("withdrawal","fee"): lines=[(counter_id,amount,0,"مصروف/سحب بنكي"),(bank_id,0,amount,"الحساب البنكي")]
                else: raise ValueError("نوع الحركة غير صحيح.")
            c.execute("BEGIN IMMEDIATE")
            no="BANK-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            jid=post_journal(c,request.form["transaction_date"],no,request.form["description"].strip() or "حركة بنكية",lines,session["user_id"])
            bank_cursor = c.execute("""INSERT INTO bank_transactions(transaction_no,transaction_date,bank_account_id,transaction_type,amount,counter_account_id,destination_bank_account_id,description,bank_reference,journal_entry_id,created_by)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(no,request.form["transaction_date"],bank_id,kind,str(amount),counter_id,destination_id,request.form["description"].strip() or "حركة بنكية",request.form.get("bank_reference","").strip(),jid,session["user_id"]))
            c.commit()
            g.audit_created_record = ("bank_transactions", bank_cursor.lastrowid, None, None)
            flash("تم تسجيل الحركة البنكية وترحيل قيدها المحاسبي.","success")
            return redirect(url_for("bank.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e:
            c.rollback(); flash(str(e),"error")
    accounts=c.execute("""SELECT a.id,a.code,a.name_ar,ca.kind,ca.name_ar display_name,ca.currency_code
        FROM cash_accounts ca JOIN accounts a ON a.id=ca.account_id
        WHERE ca.active=1 AND a.active=1
        UNION ALL
        SELECT a.id,a.code,a.name_ar,'bank',a.name_ar,NULL
        FROM accounts a WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
          AND (lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%' OR a.code IN ('1110','1120','1121','1122'))
          AND NOT EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id)
        ORDER BY code""").fetchall()
    all_accounts=c.execute("""SELECT id,code,name_ar FROM accounts WHERE is_group=0 AND active=1 ORDER BY code""").fetchall()
    bank_page = fetch_page(c,
        """SELECT t.*,a.code bank_code,a.name_ar bank_name FROM bank_transactions t
           JOIN accounts a ON a.id=t.bank_account_id ORDER BY t.transaction_date DESC,t.id DESC""",
        "SELECT COUNT(*) FROM bank_transactions",
        page=request.args.get("bank_page",1,type=int),
        per_page=request.args.get("bank_per_page",50,type=int))
    rows=bank_page["rows"]
    bank_balances=c.execute("""SELECT a.id,a.code,a.name_ar,
        COALESCE(DECIMAL_NET(CASE WHEN je.status='posted' THEN jl.debit ELSE '0' END,
                             CASE WHEN je.status='posted' THEN jl.credit ELSE '0' END),0) balance
        FROM accounts a LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON je.id=jl.entry_id
        WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
          AND (EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id AND ca.active=1)
            OR lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%' OR a.code IN ('1110','1120','1121','1122'))
        GROUP BY a.id ORDER BY a.code""").fetchall()
    bank_balances=[dict(row,balance=Decimal(str(row["balance"] or "0"))) for row in bank_balances]
    c.close()
    return render_template("bank.html",bank_accounts=accounts,accounts=all_accounts,rows=rows,bank_balances=bank_balances,bank_page=bank_page,today=date.today().isoformat())

@bank_bp.route("/reconcile/<int:transaction_id>",methods=["POST"])
@require()
def reconcile(transaction_id):
    c=db()
    row=c.execute("SELECT * FROM bank_transactions WHERE id=?",(transaction_id,)).fetchone()
    if not row:
        c.close(); flash("الحركة البنكية غير موجودة.","error"); return redirect(url_for("bank.index"))
    c.execute("UPDATE bank_transactions SET reconciled=1,statement_date=?,statement_reference=? WHERE id=?",
              (request.form.get("statement_date") or row["transaction_date"],request.form.get("statement_reference","").strip(),transaction_id))
    c.commit(); c.close()
    flash("تم تعليم الحركة كمطابقة مع كشف البنك.","success")
    return redirect(url_for("bank.index"))



@bank_bp.route("/accounts", methods=["GET", "POST"])
@require()
def cash_accounts():
    c=db()
    if request.method=="POST":
        try:
            c.execute("BEGIN IMMEDIATE")
            kind=request.form.get("kind","").strip()
            name=request.form.get("name_ar","").strip()
            mode=request.form.get("account_mode","existing")
            if kind not in {"cash","bank"} or not name:
                raise ValueError("حدد نوع الحساب واكتب اسم الخزنة أو البنك.")
            branch_id=request.form.get("branch_id",type=int)
            if branch_id and not c.execute("SELECT 1 FROM branches WHERE id=? AND active=1",(branch_id,)).fetchone():
                raise ValueError("الفرع المحدد غير موجود أو غير نشط.")
            currency=(request.form.get("currency_code") or "EGP").strip().upper()
            if not c.execute("SELECT 1 FROM currencies WHERE code=? AND active=1",(currency,)).fetchone():
                raise ValueError("اختر عملة نشطة في إعدادات العملات.")
            if mode=="new":
                parent_id=request.form.get("parent_id",type=int)
                parent=c.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?",(parent_id,)).fetchone()
                if not parent or parent["account_type"]!="asset" or not parent["is_group"] or not parent["active"]:
                    raise ValueError("لإنشاء حساب أستاذ جديد اختر مجموعة أصول نشطة.")
                prefix="CA" if kind=="cash" else "BK"
                code=next_code(c,"cash_ledger_"+kind,prefix,"accounts","code")
                account_name=("خزنة - " if kind=="cash" else "بنك - ")+name
                cur=c.execute("INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group,active) VALUES(?,?,'asset',?,0,1)",
                              (code,account_name,parent_id))
                account_id=cur.lastrowid
                ensure_account_metadata(c)
            elif mode=="existing":
                account_id=request.form.get("account_id",type=int)
                account=c.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?",(account_id,)).fetchone()
                if not account or account["account_type"]!="asset" or account["is_group"] or not account["active"]:
                    raise ValueError("اربط الحساب بحساب أصول تفصيلي ونشط فقط.")
            else:
                raise ValueError("طريقة ربط حساب الأستاذ غير صحيحة.")
            bank_name=request.form.get("bank_name","").strip() if kind=="bank" else ""
            iban=request.form.get("iban","").strip() if kind=="bank" else ""
            c.execute("""INSERT INTO cash_accounts(kind,name_ar,account_id,branch_id,currency_code,bank_name,iban,opening_date,created_by)
                VALUES(?,?,?,?,?,?,?,?,?)""",(kind,name,account_id,branch_id,currency,bank_name,iban,
                request.form.get("opening_date") or date.today().isoformat(),session.get("user_id")))
            row_id=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.commit()
            g.audit_created_record=("cash_accounts",row_id,None,None)
            flash("تم ربط الخزنة/البنك بحساب أستاذ مستقل. ستظهر حركاته في دفتر الحساب المرتبط.","success")
            return redirect(url_for("bank.cash_accounts"))
        except (ValueError,sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc),"error")
    rows=c.execute("""SELECT ca.*,a.code account_code,a.name_ar account_name,
        b.code branch_code,b.name_ar branch_name
        FROM cash_accounts ca JOIN accounts a ON a.id=ca.account_id
        LEFT JOIN branches b ON b.id=ca.branch_id ORDER BY ca.kind,ca.name_ar""").fetchall()
    groups=c.execute("SELECT id,code,name_ar FROM accounts WHERE account_type='asset' AND is_group=1 AND active=1 ORDER BY code").fetchall()
    available=c.execute("""SELECT a.id,a.code,a.name_ar FROM accounts a
        WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
        AND NOT EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id)
        ORDER BY a.code""").fetchall()
    branches=c.execute("SELECT id,code,name_ar FROM branches WHERE active=1 ORDER BY code").fetchall()
    currencies=c.execute("SELECT code,name_ar FROM currencies WHERE active=1 ORDER BY code").fetchall()
    c.close()
    return render_template("cash_accounts.html",rows=rows,groups=groups,available=available,
        branches=branches,currencies=currencies,today=date.today().isoformat())


@bank_bp.route("/accounts/<int:cash_account_id>/toggle", methods=["POST"])
@require()
def toggle_cash_account(cash_account_id):
    c=db()
    try:
        row=c.execute("SELECT id,name_ar,active FROM cash_accounts WHERE id=?",(cash_account_id,)).fetchone()
        if not row:
            raise ValueError("الخزنة أو البنك غير موجود.")
        active=0 if row["active"] else 1
        c.execute("UPDATE cash_accounts SET active=? WHERE id=?",(active,cash_account_id))
        c.commit()
        flash(("تم تفعيل " if active else "تم تعطيل ")+row["name_ar"]+" مع الاحتفاظ بالحركات والرصيد التاريخي.","success")
    except (ValueError,sqlite3.Error) as exc:
        c.rollback(); flash(str(exc),"error")
    finally:
        c.close()
    return redirect(url_for("bank.cash_accounts"))

if __name__=="__main__": init_bank_schema()
