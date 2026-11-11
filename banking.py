"""Bank register and reconciliation support for NEXUS ERP."""
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal, register_decimal_aggregates

bank_bp = Blueprint("bank", __name__, url_prefix="/bank")
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"

def db():
    c = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=20000")
    register_decimal_aggregates(c)
    return c

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
                lines=[(destination_id,amount,0,"تحويل وارد إلى البنك"),(bank_id,0,amount,"تحويل صادر من البنك")]
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
            c.execute("""INSERT INTO bank_transactions(transaction_no,transaction_date,bank_account_id,transaction_type,amount,counter_account_id,destination_bank_account_id,description,bank_reference,journal_entry_id,created_by)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(no,request.form["transaction_date"],bank_id,kind,str(amount),counter_id,destination_id,request.form["description"].strip() or "حركة بنكية",request.form.get("bank_reference","").strip(),jid,session["user_id"]))
            c.commit()
            flash("تم تسجيل الحركة البنكية وترحيل قيدها المحاسبي.","success")
            return redirect(url_for("bank.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e:
            c.rollback(); flash(str(e),"error")
    accounts=c.execute("""SELECT id,code,name_ar FROM accounts WHERE account_type='asset' AND is_group=0 AND active=1 AND (lower(name_ar) LIKE '%بنك%' OR lower(name_ar) LIKE '%بنوك%' OR lower(name_ar) LIKE '%مصرف%' OR code IN ('1110','1120','1121','1122')) ORDER BY code""").fetchall()
    all_accounts=c.execute("""SELECT id,code,name_ar FROM accounts WHERE is_group=0 AND active=1 ORDER BY code""").fetchall()
    rows=c.execute("""SELECT t.*,a.code bank_code,a.name_ar bank_name FROM bank_transactions t JOIN accounts a ON a.id=t.bank_account_id ORDER BY t.transaction_date DESC,t.id DESC LIMIT 250""").fetchall()
    bank_balances=c.execute("""SELECT a.id,a.code,a.name_ar,
        COALESCE(DECIMAL_NET(CASE WHEN je.status='posted' THEN jl.debit ELSE '0' END,
                             CASE WHEN je.status='posted' THEN jl.credit ELSE '0' END),0) balance
        FROM accounts a LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON je.id=jl.entry_id
        WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
          AND (lower(a.name_ar) LIKE '%بنك%' OR lower(a.name_ar) LIKE '%بنوك%' OR lower(a.name_ar) LIKE '%مصرف%' OR a.code IN ('1110','1120','1121','1122'))
        GROUP BY a.id ORDER BY a.code""").fetchall()
    bank_balances=[dict(row,balance=Decimal(str(row["balance"] or "0"))) for row in bank_balances]
    c.close()
    return render_template("bank.html",bank_accounts=accounts,accounts=all_accounts,rows=rows,bank_balances=bank_balances,today=date.today().isoformat())

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

if __name__=="__main__": init_bank_schema()
