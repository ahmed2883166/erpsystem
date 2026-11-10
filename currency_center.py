"""Foreign currency transaction subledger, settlement and period-end revaluation."""
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal

currency_bp = Blueprint("currency_center", __name__)
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
CENT = Decimal("0.01")


def D(v):
    try:
        x = Decimal(str(v or "0"))
        if not x.is_finite():
            raise InvalidOperation()
        return x
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("قيمة العملة أو سعر الصرف غير صحيحة.")


def M(v):
    return D(v).quantize(CENT, rounding=ROUND_HALF_UP)


def _db():
    c = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=20000")
    return c


def initialize_currency_schema():
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS fx_exchange_rates(
      id INTEGER PRIMARY KEY, currency_code TEXT NOT NULL, rate_date TEXT NOT NULL,
      rate_to_base TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(currency_code,rate_date)
    );
    CREATE TABLE IF NOT EXISTS fx_transactions(
      id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE, party_type TEXT NOT NULL
        CHECK(party_type IN ('receivable','payable')),
      party_name TEXT NOT NULL, currency_code TEXT NOT NULL, transaction_date TEXT NOT NULL,
      original_foreign TEXT NOT NULL, balance_foreign TEXT NOT NULL,
      original_rate TEXT NOT NULL, original_base TEXT NOT NULL, balance_base TEXT NOT NULL,
      party_account_id INTEGER NOT NULL REFERENCES accounts(id),
      counterpart_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER UNIQUE, status TEXT NOT NULL DEFAULT 'open',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS fx_settlements(
      id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES fx_transactions(id),
      settlement_date TEXT NOT NULL, foreign_amount TEXT NOT NULL, rate TEXT NOT NULL,
      base_amount TEXT NOT NULL, carrying_amount TEXT NOT NULL, fx_difference TEXT NOT NULL,
      journal_entry_id INTEGER UNIQUE, created_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS fx_revaluations(
      id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES fx_transactions(id),
      revaluation_date TEXT NOT NULL, rate TEXT NOT NULL, old_base TEXT NOT NULL,
      new_base TEXT NOT NULL, difference TEXT NOT NULL, journal_entry_id INTEGER UNIQUE,
      created_by INTEGER, UNIQUE(transaction_id,revaluation_date)
    );
    """)
    c.close()


def _role(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in {"admin", "accountant"}:
            flash("إدارة العملات وفروق الصرف متاحة للمحاسب ومدير النظام فقط.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapped


def _audit(c, action, details):
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone():
        c.execute("INSERT INTO audit_log(user_id,action,details,created_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                  (session.get("user_id"), action, details))


def _account(c, account_id):
    row = c.execute("SELECT id,is_group,active FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row or row["is_group"] or not row["active"]:
        raise ValueError("اختر حسابًا تفصيليًا ونشطًا من دليل الحسابات.")
    return row["id"]


def _rate(c, currency, rate_date, supplied=None):
    if supplied is not None and str(supplied).strip():
        rate = D(supplied)
        if rate <= 0:
            raise ValueError("سعر الصرف يجب أن يكون أكبر من صفر.")
        c.execute("""INSERT INTO fx_exchange_rates(currency_code,rate_date,rate_to_base,created_by)
                     VALUES(?,?,?,?) ON CONFLICT(currency_code,rate_date)
                     DO UPDATE SET rate_to_base=excluded.rate_to_base,source='manual',created_by=excluded.created_by""",
                  (currency,rate_date,str(rate),session.get("user_id")))
        return rate
    row = c.execute("SELECT rate_to_base FROM fx_exchange_rates WHERE currency_code=? AND rate_date<=? ORDER BY rate_date DESC LIMIT 1",
                    (currency,rate_date)).fetchone()
    if not row:
        raise ValueError("لا يوجد سعر صرف مسجل لهذه العملة في التاريخ المحدد.")
    return D(row["rate_to_base"])


@currency_bp.route("/currencies", methods=["GET", "POST"])
@_role
def index():
    c = _db()
    if request.method == "POST":
        try:
            currency = request.form["currency_code"].upper().strip()
            rate_date = date.fromisoformat(request.form["rate_date"]).isoformat()
            rate = D(request.form["rate_to_base"])
            if rate <= 0:
                raise ValueError("سعر الصرف يجب أن يكون أكبر من صفر.")
            if not c.execute("SELECT 1 FROM currencies WHERE code=? AND active=1", (currency,)).fetchone():
                raise ValueError("العملة غير موجودة أو غير مفعلة في إعدادات الشركة.")
            c.execute("""INSERT INTO fx_exchange_rates(currency_code,rate_date,rate_to_base,created_by)
                         VALUES(?,?,?,?) ON CONFLICT(currency_code,rate_date)
                         DO UPDATE SET rate_to_base=excluded.rate_to_base,source='manual',created_by=excluded.created_by""",
                      (currency,rate_date,str(rate),session["user_id"]))
            _audit(c, "fx_rate_updated", currency + " " + rate_date + " " + str(rate))
            c.commit()
            flash("تم حفظ سعر الصرف بتاريخ السريان المحدد.", "success")
        except (ValueError, KeyError, sqlite3.Error) as e:
            c.rollback()
            flash(str(e), "error")
    rates = c.execute("SELECT * FROM fx_exchange_rates ORDER BY rate_date DESC,currency_code LIMIT 100").fetchall()
    currencies = c.execute("SELECT code,name_ar FROM currencies WHERE active=1 AND is_base=0 ORDER BY code").fetchall()
    rows = c.execute("""SELECT t.*,a.code party_code,a.name_ar party_account FROM fx_transactions t
                        JOIN accounts a ON a.id=t.party_account_id ORDER BY t.id DESC LIMIT 100""").fetchall()
    accounts = c.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE is_group=0 AND active=1 ORDER BY code").fetchall()
    c.close()
    return render_template("currencies.html", rates=rates, currencies=currencies, transactions=rows,
                           accounts=accounts, today=date.today().isoformat())


@currency_bp.route("/currencies/transactions", methods=["POST"])
@_role
def create_transaction():
    c = _db()
    try:
        kind = request.form["party_type"]
        if kind not in {"receivable", "payable"}:
            raise ValueError("نوع العملية غير صحيح.")
        currency = request.form["currency_code"].upper().strip()
        trans_date = date.fromisoformat(request.form["transaction_date"]).isoformat()
        foreign = D(request.form["foreign_amount"])
        rate = _rate(c,currency,trans_date,request.form.get("rate"))
        if foreign <= 0:
            raise ValueError("المبلغ بالعملة الأجنبية يجب أن يكون أكبر من صفر.")
        party_account = _account(c,int(request.form["party_account_id"]))
        counterpart = _account(c,int(request.form["counterpart_account_id"]))
        base_amount = M(foreign*rate)
        reference = request.form["reference"].strip()
        if not reference:
            raise ValueError("رقم المستند أو المرجع مطلوب.")
        if kind == "receivable":
            lines = [(party_account,base_amount,0,"إثبات ذمة مدينة بالعملة الأجنبية"),
                     (counterpart,0,base_amount,"إيراد / حساب مقابل")]
        else:
            lines = [(counterpart,base_amount,0,"مصروف / حساب مقابل"),
                     (party_account,0,base_amount,"إثبات ذمة دائنة بالعملة الأجنبية")]
        c.execute("BEGIN IMMEDIATE")
        journal_id = post_journal(c,trans_date,"FX-OPEN-"+reference,"إثبات معاملة بعملة أجنبية",lines,session["user_id"])
        c.execute("""INSERT INTO fx_transactions(reference,party_type,party_name,currency_code,transaction_date,
            original_foreign,balance_foreign,original_rate,original_base,balance_base,party_account_id,
            counterpart_account_id,journal_entry_id,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (reference,kind,request.form["party_name"].strip(),currency,trans_date,str(foreign),str(foreign),
             str(rate),str(base_amount),str(base_amount),party_account,counterpart,journal_id,session["user_id"]))
        _audit(c, "fx_transaction_created", reference + " " + currency + " " + str(foreign))
        c.commit()
        flash("تم تسجيل المعاملة الأجنبية وترحيل قيدها بسعر تاريخ العملية.", "success")
    except (ValueError, KeyError, sqlite3.Error) as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("currency_center.index"))


@currency_bp.route("/currencies/<int:transaction_id>/settle", methods=["POST"])
@_role
def settle(transaction_id):
    c = _db()
    try:
        t = c.execute("SELECT * FROM fx_transactions WHERE id=? AND status='open'", (transaction_id,)).fetchone()
        if not t:
            raise ValueError("المعاملة غير موجودة أو تمت تسويتها.")
        amount = D(request.form["foreign_amount"])
        if amount <= 0 or amount > D(t["balance_foreign"]):
            raise ValueError("مبلغ التسوية أكبر من الرصيد الأجنبي المتبقي أو غير صحيح.")
        settle_date = date.fromisoformat(request.form["settlement_date"]).isoformat()
        rate = _rate(c,t["currency_code"],settle_date,request.form.get("rate"))
        cash = M(amount*rate)
        carrying = M(D(t["balance_base"])*amount/D(t["balance_foreign"]))
        diff = M(cash-carrying) if t["party_type"]=="receivable" else M(carrying-cash)
        cash_account = _account(c,int(request.form["cash_account_id"]))
        gain_account = _account(c,int(request.form["fx_gain_account_id"]))
        loss_account = _account(c,int(request.form["fx_loss_account_id"]))
        party = int(t["party_account_id"])
        if t["party_type"] == "receivable":
            lines = [(cash_account,cash,0,"تحصيل بالعملة الأجنبية"),(party,0,carrying,"إقفال الجزء المحصل من العميل")]
        else:
            lines = [(party,carrying,0,"تخفيض التزام المورد"),(cash_account,0,cash,"سداد بالعملة الأجنبية")]
        if diff > 0:
            lines.append((gain_account,0,diff,"أرباح فروق عملة محققة"))
        elif diff < 0:
            lines.append((loss_account,-diff,0,"خسائر فروق عملة محققة"))
        c.execute("BEGIN IMMEDIATE")
        ref = "FX-SETTLE-" + str(transaction_id) + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        jid = post_journal(c,settle_date,ref,"تسوية معاملة عملة أجنبية "+t["reference"],lines,session["user_id"])
        remaining_foreign = D(t["balance_foreign"])-amount
        remaining_base = M(D(t["balance_base"])-carrying)
        c.execute("""INSERT INTO fx_settlements(transaction_id,settlement_date,foreign_amount,rate,base_amount,
            carrying_amount,fx_difference,journal_entry_id,created_by) VALUES(?,?,?,?,?,?,?,?,?)""",
            (transaction_id,settle_date,str(amount),str(rate),str(cash),str(carrying),str(diff),jid,session["user_id"]))
        c.execute("UPDATE fx_transactions SET balance_foreign=?,balance_base=?,status=? WHERE id=?",
                  (str(remaining_foreign),str(remaining_base),"settled" if remaining_foreign==0 else "open",transaction_id))
        _audit(c, "fx_transaction_settled", t["reference"] + " foreign=" + str(amount) + " diff=" + str(diff))
        c.commit()
        flash("تم تسجيل التحصيل/السداد وإثبات فرق العملة المحقق.", "success")
    except (ValueError, KeyError, sqlite3.Error, ZeroDivisionError) as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("currency_center.index"))


@currency_bp.route("/currencies/<int:transaction_id>/revalue", methods=["POST"])
@_role
def revalue(transaction_id):
    c = _db()
    try:
        t = c.execute("SELECT * FROM fx_transactions WHERE id=? AND status='open'", (transaction_id,)).fetchone()
        if not t:
            raise ValueError("لا يوجد رصيد مفتوح لإعادة تقييمه.")
        revalue_date = date.fromisoformat(request.form["revaluation_date"]).isoformat()
        if c.execute("SELECT 1 FROM fx_revaluations WHERE transaction_id=? AND revaluation_date=?",
                     (transaction_id,revalue_date)).fetchone():
            raise ValueError("تمت إعادة تقييم هذه المعاملة في هذا التاريخ بالفعل.")
        rate = _rate(c,t["currency_code"],revalue_date,request.form.get("rate"))
        new_base = M(D(t["balance_foreign"])*rate)
        old_base = M(t["balance_base"])
        delta = M(new_base-old_base)
        gain = _account(c,int(request.form["fx_gain_account_id"]))
        loss = _account(c,int(request.form["fx_loss_account_id"]))
        party = int(t["party_account_id"])
        if delta == 0:
            flash("لا توجد فروق إعادة تقييم في هذا التاريخ.", "info")
            c.close()
            return redirect(url_for("currency_center.index"))
        if t["party_type"] == "receivable":
            lines = [(party,delta,0,"زيادة قيمة الذمم المدينة")] if delta>0 else [(loss,-delta,0,"خسائر إعادة تقييم")] 
            lines.append((gain,0,delta,"أرباح إعادة تقييم") if delta>0 else (party,0,-delta,"تخفيض الذمم المدينة"))
        else:
            lines = [(loss,delta,0,"خسائر إعادة تقييم الالتزام"),(party,0,delta,"زيادة الالتزام")] if delta>0 else [(party,-delta,0,"تخفيض الالتزام"),(gain,0,-delta,"أرباح إعادة تقييم")]
        c.execute("BEGIN IMMEDIATE")
        ref = "FX-REVALUE-" + str(transaction_id) + "-" + revalue_date
        jid = post_journal(c,revalue_date,ref,"إعادة تقييم رصيد عملة أجنبية "+t["reference"],lines,session["user_id"])
        c.execute("""INSERT INTO fx_revaluations(transaction_id,revaluation_date,rate,old_base,new_base,difference,
            journal_entry_id,created_by) VALUES(?,?,?,?,?,?,?,?)""",
            (transaction_id,revalue_date,str(rate),str(old_base),str(new_base),str(delta),jid,session["user_id"]))
        c.execute("UPDATE fx_transactions SET balance_base=? WHERE id=?", (str(new_base),transaction_id))
        _audit(c, "fx_revalued", t["reference"] + " date=" + revalue_date + " diff=" + str(delta))
        c.commit()
        flash("تمت إعادة تقييم الرصيد وإثبات الفرق غير المحقق.", "success")
    except (ValueError, KeyError, sqlite3.Error) as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("currency_center.index"))
