"""NEXUS ERP fixed assets register and IAS 16 depreciation posting."""
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from pathlib import Path
from functools import wraps
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal

assets_bp = Blueprint("assets", __name__, url_prefix="/assets")
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
M = Decimal("0.01")

def db():
    c=sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=20000")
    return c

def require(write=False):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a,**kw):
            if not session.get("user_id"): return redirect(url_for("login"))
            if write and session.get("role") not in {"admin","accountant"}:
                flash("لا تملك صلاحية إدارة الأصول الثابتة.","error"); return redirect(url_for("assets.index"))
            return fn(*a,**kw)
        return wrapped
    return deco

def initialize_assets_schema():
    c=db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS fixed_assets(
      id INTEGER PRIMARY KEY, asset_code TEXT NOT NULL UNIQUE, asset_name TEXT NOT NULL,
      category TEXT NOT NULL DEFAULT '', purchase_date TEXT NOT NULL, ready_date TEXT NOT NULL,
      cost TEXT NOT NULL, residual_value TEXT NOT NULL DEFAULT '0', useful_life_months INTEGER NOT NULL,
      depreciation_method TEXT NOT NULL DEFAULT 'straight_line', annual_rate TEXT NOT NULL DEFAULT '0',
      total_units TEXT NOT NULL DEFAULT '0', asset_account_id INTEGER NOT NULL REFERENCES accounts(id),
      accum_account_id INTEGER NOT NULL REFERENCES accounts(id), expense_account_id INTEGER NOT NULL REFERENCES accounts(id),
      status TEXT NOT NULL DEFAULT 'active', notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS asset_depreciations(
      id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES fixed_assets(id),
      period TEXT NOT NULL, amount TEXT NOT NULL, journal_entry_id INTEGER UNIQUE,
      posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(asset_id,period)
    );
    CREATE INDEX IF NOT EXISTS idx_asset_depreciation_period ON asset_depreciations(period,asset_id);
    """)
    c.close()

@assets_bp.route("/", methods=["GET","POST"])
@require(write=True)
def index():
    c=db()
    if request.method=="POST":
        try:
            cost=Decimal(request.form["cost"]); residual=Decimal(request.form.get("residual_value","0"))
            life=int(request.form["useful_life_months"])
            if cost<=0 or residual<0 or residual>=cost or life<=0: raise ValueError("تحقق من التكلفة والقيمة التخريدية والعمر الإنتاجي.")
            method=request.form["depreciation_method"]
            if method not in {"straight_line","declining_balance","units"}: raise ValueError("طريقة الإهلاك غير صحيحة.")
            ids=[int(request.form[k]) for k in ("asset_account_id","accum_account_id","expense_account_id")]
            for aid in ids:
                row=c.execute("SELECT is_group,active,account_type FROM accounts WHERE id=?",(aid,)).fetchone()
                if not row or row["is_group"] or not row["active"]: raise ValueError("اختر حسابات تفصيلية نشطة.")
            if method=="declining_balance" and Decimal(request.form.get("annual_rate","0"))<=0: raise ValueError("أدخل معدل الإهلاك السنوي للطريقة المتناقصة.")
            if method=="units" and Decimal(request.form.get("total_units","0"))<=0: raise ValueError("أدخل إجمالي وحدات الإنتاج المتوقعة.")
            c.execute("""INSERT INTO fixed_assets(asset_code,asset_name,category,purchase_date,ready_date,cost,residual_value,useful_life_months,depreciation_method,annual_rate,total_units,asset_account_id,accum_account_id,expense_account_id,notes,created_by)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (request.form["asset_code"].strip(),request.form["asset_name"].strip(),request.form.get("category","").strip(),
               request.form["purchase_date"],request.form["ready_date"],str(cost),str(residual),life,method,
               request.form.get("annual_rate","0"),request.form.get("total_units","0"),*ids,request.form.get("notes",""),session["user_id"]))
            c.commit(); flash("تم تسجيل الأصل. تأكد من أن قيد الشراء الأصلي مسجل قبل بدء الإهلاك.","success")
            return redirect(url_for("assets.index"))
        except (ValueError,KeyError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود الأصل مستخدم أو توجد بيانات غير صحيحة.","error")
    assets=c.execute("""SELECT f.*,a.code asset_code_gl,a.name_ar asset_account_name,
      COALESCE((SELECT SUM(CAST(d.amount AS REAL)) FROM asset_depreciations d WHERE d.asset_id=f.id),0) accumulated
      FROM fixed_assets f JOIN accounts a ON a.id=f.asset_account_id ORDER BY f.asset_code""").fetchall()
    accounts=c.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 ORDER BY code").fetchall()
    c.close()
    return render_template("fixed_assets.html",assets=assets,accounts=accounts,today=date.today().isoformat())

@assets_bp.route("/<int:asset_id>/schedule")
@require()
def schedule(asset_id):
    c=db(); asset=c.execute("SELECT * FROM fixed_assets WHERE id=?",(asset_id,)).fetchone()
    if not asset: c.close(); flash("الأصل غير موجود.","error"); return redirect(url_for("assets.index"))
    rows=c.execute("SELECT * FROM asset_depreciations WHERE asset_id=? ORDER BY period",(asset_id,)).fetchall()
    accumulated=sum((Decimal(x["amount"]) for x in rows),Decimal("0"))
    c.close()
    depreciable=Decimal(asset["cost"])-Decimal(asset["residual_value"])
    return render_template("asset_schedule.html",asset=asset,rows=rows,accumulated=accumulated,
      net=max(Decimal(asset["cost"])-accumulated,Decimal(asset["residual_value"])),depreciable=depreciable)

@assets_bp.route("/<int:asset_id>/depreciate",methods=["POST"])
@require(write=True)
def depreciate(asset_id):
    c=db()
    try:
        period=request.form["period"]
        date.fromisoformat(period+"-01")
        asset=c.execute("SELECT * FROM fixed_assets WHERE id=? AND status='active'",(asset_id,)).fetchone()
        if not asset: raise ValueError("الأصل غير موجود أو غير نشط.")
        if period < asset["ready_date"][:7]: raise ValueError("لا يجوز بدء الإهلاك قبل جاهزية الأصل للاستخدام.")
        if c.execute("SELECT 1 FROM asset_depreciations WHERE asset_id=? AND period=?",(asset_id,period)).fetchone(): raise ValueError("تم تسجيل إهلاك هذا الأصل عن هذه الفترة بالفعل.")
        cost,residual=Decimal(asset["cost"]),Decimal(asset["residual_value"])
        accumulated= sum((Decimal(x["amount"]) for x in c.execute("SELECT amount FROM asset_depreciations WHERE asset_id=?",(asset_id,)).fetchall()),Decimal("0"))
        remaining=max(cost-residual-accumulated,Decimal("0"))
        method=asset["depreciation_method"]
        if method=="straight_line": amount=(cost-residual)/Decimal(asset["useful_life_months"])
        elif method=="declining_balance":
            rate=Decimal(asset["annual_rate"])/Decimal("100")
            amount=(cost-accumulated)*rate/Decimal("12")
        else:
            raise ValueError("طريقة وحدات الإنتاج تتطلب إدخال إنتاج الفترة؛ سيتم تفعيلها في شاشة مستقلة قبل استخدامها.")
        amount=min(amount.quantize(M,rounding=ROUND_HALF_UP),remaining)
        if amount<=0: raise ValueError("لا توجد قيمة قابلة للإهلاك في هذه الفترة.")
        c.execute("BEGIN IMMEDIATE")
        entry=post_journal(c,period+"-01","DEP-"+str(asset_id)+"-"+period,"إهلاك الأصل "+asset["asset_code"],
          [(asset["expense_account_id"],amount,0,"مصروف إهلاك "+asset["asset_name"]),
           (asset["accum_account_id"],0,amount,"مجمع إهلاك "+asset["asset_name"])],session["user_id"])
        c.execute("INSERT INTO asset_depreciations(asset_id,period,amount,journal_entry_id) VALUES(?,?,?,?)",(asset_id,period,str(amount),entry))
        c.commit(); flash("تم ترحيل قيد الإهلاك بقيمة "+str(amount),"success")
    except (ValueError,KeyError,sqlite3.Error) as e:
        c.rollback(); flash(str(e),"error")
    finally: c.close()
    return redirect(url_for("assets.schedule",asset_id=asset_id))
