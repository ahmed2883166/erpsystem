from app_paths import DB_PATH
from database import connect_database
"""NEXUS ERP fixed assets register and IAS 16 depreciation posting."""
import sqlite3
import calendar
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, timedelta
from functools import wraps
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal

assets_bp = Blueprint("assets", __name__, url_prefix="/assets")

M = Decimal("0.01")

def db():
    return connect_database(DB_PATH, isolation_level=None)

def require(write=False):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a,**kw):
            if not session.get("user_id"): return redirect(url_for("login"))
            required = "assets.manage" if write or request.method == "POST" else "assets.view"
            if (required not in set(session["permissions"])) if "permissions" in session else (write and session.get("role") not in {"admin","accountant"}):
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
    CREATE TABLE IF NOT EXISTS asset_disposals(
      id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL UNIQUE REFERENCES fixed_assets(id),
      disposal_date TEXT NOT NULL, proceeds_account_id INTEGER NOT NULL REFERENCES accounts(id),
      proceeds_amount TEXT NOT NULL, output_vat_account_id INTEGER REFERENCES accounts(id),
      output_vat_amount TEXT NOT NULL DEFAULT '0',
      gain_loss_account_id INTEGER NOT NULL REFERENCES accounts(id),
      gain_loss_amount TEXT NOT NULL, net_book_value TEXT NOT NULL,
      journal_entry_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),
      reason TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_asset_disposals_date ON asset_disposals(disposal_date);
    """)
    c.close()


def _asset_period_amount(asset, accumulated, period, cutoff_date=None):
    """Calculate one period's depreciation, optionally prorated to a disposal date."""
    try:
        first_day = date.fromisoformat(str(period) + "-01")
        ready = date.fromisoformat(str(asset["ready_date"]))
        cutoff = date.fromisoformat(str(cutoff_date)) if cutoff_date else None
    except (TypeError, ValueError):
        raise ValueError("فترة الإهلاك أو تاريخ الجاهزية غير صحيح.")
    if cutoff and cutoff.strftime("%Y-%m") != period:
        raise ValueError("تاريخ الاستبعاد يجب أن يقع داخل فترة الإهلاك المحددة.")
    offset = (first_day.year - ready.year) * 12 + first_day.month - ready.month
    life = int(asset["useful_life_months"])
    if offset < 0:
        raise ValueError("لا يجوز بدء الإهلاك قبل جاهزية الأصل للاستخدام.")
    final_offset = life if ready.day > 1 else life - 1
    if offset > final_offset:
        raise ValueError("انتهى العمر الإنتاجي المحدد للأصل.")
    cost = Decimal(str(asset["cost"]))
    residual = Decimal(str(asset["residual_value"]))
    remaining = max(cost - residual - accumulated, Decimal("0"))
    if remaining <= 0:
        raise ValueError("تم إهلاك كامل القيمة القابلة للإهلاك.")
    method = asset["depreciation_method"]
    days_in_month = calendar.monthrange(first_day.year, first_day.month)[1]
    if method == "straight_line":
        monthly = (cost - residual) / Decimal(life)
        if offset == 0:
            first_start = ready.day
        else:
            first_start = 1
        if cutoff:
            last_day = cutoff.day
        elif offset == life and ready.day > 1:
            last_day = ready.day - 1
        else:
            last_day = days_in_month
        if offset == life and ready.day > 1:
            first_start = 1
        days = max(last_day - first_start + 1, 0)
        fraction = Decimal(days) / Decimal(days_in_month)
        amount = monthly * fraction
    elif method == "declining_balance":
        rate = Decimal(str(asset["annual_rate"])) / Decimal("100")
        amount = (cost - accumulated) * rate / Decimal("12")
        if offset == 0:
            first_start = ready.day
        else:
            first_start = 1
        last_day = cutoff.day if cutoff else days_in_month
        if offset == life and ready.day > 1 and not cutoff:
            amount = remaining
        else:
            amount *= Decimal(max(last_day - first_start + 1, 0)) / Decimal(days_in_month)
    else:
        raise ValueError("طريقة الإهلاك غير مدعومة.")
    # At the end of the useful life, clear only the remaining depreciable base.
    if offset == final_offset and not cutoff:
        amount = remaining
    amount = min(amount.quantize(M, rounding=ROUND_HALF_UP), remaining)
    if amount <= 0:
        raise ValueError("لا توجد قيمة قابلة للإهلاك في هذه الفترة.")
    return amount


def _post_asset_depreciation(conn, asset, period, user_id, cutoff_date=None):
    """Post one asset's depreciation exactly once, within the caller transaction."""
    if conn.execute(
        "SELECT 1 FROM asset_depreciations WHERE asset_id=? AND period=?",
        (asset["id"], period),
    ).fetchone():
        raise ValueError(f"تم تسجيل إهلاك الأصل {asset['asset_code']} عن الفترة {period} بالفعل.")
    first_day = date.fromisoformat(period + "-01")
    ready = date.fromisoformat(asset["ready_date"])
    offset = (first_day.year - ready.year) * 12 + first_day.month - ready.month
    if offset > 0:
        previous_month = (first_day.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        if not conn.execute(
            "SELECT 1 FROM asset_depreciations WHERE asset_id=? AND period=?",
            (asset["id"], previous_month),
        ).fetchone():
            raise ValueError(
                f"لا يمكن ترحيل {period} للأصل {asset['asset_code']} قبل ترحيل الفترة السابقة {previous_month}."
            )
    accumulated = sum(
        (Decimal(str(row["amount"])) for row in conn.execute(
            "SELECT amount FROM asset_depreciations WHERE asset_id=?", (asset["id"],)
        ).fetchall()),
        Decimal("0"),
    )
    amount = _asset_period_amount(asset, accumulated, period, cutoff_date=cutoff_date)
    entry_date = cutoff_date or (period + "-01")
    reference = "DEP-" + str(asset["id"]) + "-" + period
    entry = post_journal(
        conn, entry_date, reference, "إهلاك الأصل " + asset["asset_code"],
        [
            (asset["expense_account_id"], amount, 0, "مصروف إهلاك " + asset["asset_name"]),
            (asset["accum_account_id"], 0, amount, "مجمع إهلاك " + asset["asset_name"]),
        ], user_id,
    )
    conn.execute(
        "INSERT INTO asset_depreciations(asset_id,period,amount,journal_entry_id) VALUES(?,?,?,?)",
        (asset["id"], period, str(amount), entry),
    )
    return amount, entry

@assets_bp.route("/", methods=["GET","POST"])
@require()
def index():
    c=db()
    if request.method=="POST":
        if ("assets.manage" not in set(session.get("permissions", []))) if "permissions" in session else (session.get("role") not in {"admin","accountant"}):
            c.close()
            flash("لا تملك صلاحية إدارة الأصول الثابتة.","error")
            return redirect(url_for("assets.index"))
        try:
            cost=Decimal(request.form["cost"]); residual=Decimal(request.form.get("residual_value","0"))
            life=int(request.form["useful_life_months"])
            if cost<=0 or residual<0 or residual>=cost or life<=0: raise ValueError("تحقق من التكلفة والقيمة التخريدية والعمر الإنتاجي.")
            if request.form["ready_date"] < request.form["purchase_date"]: raise ValueError("تاريخ الجاهزية لا يمكن أن يسبق تاريخ الشراء.")
            method=request.form["depreciation_method"]
            if method not in {"straight_line","declining_balance"}: raise ValueError("طريقة الإهلاك غير صحيحة.")
            ids=[int(request.form[k]) for k in ("asset_account_id","accum_account_id","expense_account_id")]
            for aid in ids:
                row=c.execute("SELECT is_group,active,account_type FROM accounts WHERE id=?",(aid,)).fetchone()
                if not row or row["is_group"] or not row["active"]: raise ValueError("اختر حسابات تفصيلية نشطة.")
            account_types=[c.execute("SELECT account_type FROM accounts WHERE id=?",(aid,)).fetchone()["account_type"] for aid in ids]
            if account_types[0]!="asset" or account_types[1]!="asset" or account_types[2]!="expense":
                raise ValueError("حساب الأصل ومجمع الإهلاك يجب أن يكونا من الأصول، ومصروف الإهلاك من المصروفات.")
            if method=="declining_balance" and not (Decimal("0") < Decimal(request.form.get("annual_rate","0")) <= Decimal("100")): raise ValueError("معدل الإهلاك السنوي يجب أن يكون أكبر من صفر ولا يتجاوز 100%.")
            asset_cursor = c.execute("""INSERT INTO fixed_assets(asset_code,asset_name,category,purchase_date,ready_date,cost,residual_value,useful_life_months,depreciation_method,annual_rate,total_units,asset_account_id,accum_account_id,expense_account_id,notes,created_by)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (request.form["asset_code"].strip(),request.form["asset_name"].strip(),request.form.get("category","").strip(),
               request.form["purchase_date"],request.form["ready_date"],str(cost),str(residual),life,method,
               request.form.get("annual_rate","0"),request.form.get("total_units","0"),*ids,request.form.get("notes",""),session["user_id"]))
            asset_id = asset_cursor.lastrowid
            c.commit(); g.audit_created_record = ("fixed_assets", asset_id, None, None); flash("تم تسجيل الأصل. تأكد من أن قيد الشراء الأصلي مسجل قبل بدء الإهلاك.","success")
            return redirect(url_for("assets.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود الأصل مستخدم أو توجد بيانات غير صحيحة.","error")
    assets=c.execute("""SELECT f.*,a.code asset_code_gl,a.name_ar asset_account_name,
      COALESCE((SELECT DECIMAL_SUM(d.amount) FROM asset_depreciations d WHERE d.asset_id=f.id),0) accumulated
      FROM fixed_assets f JOIN accounts a ON a.id=f.asset_account_id ORDER BY f.asset_code""").fetchall()
    assets=[dict(row,accumulated=Decimal(str(row["accumulated"] or "0"))) for row in assets]
    accounts=c.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 ORDER BY code").fetchall()
    c.close()
    return render_template("fixed_assets.html",assets=assets,accounts=accounts,today=date.today().isoformat())

@assets_bp.route("/<int:asset_id>/schedule")
@require()
def schedule(asset_id):
    c=db(); asset=c.execute("SELECT * FROM fixed_assets WHERE id=?",(asset_id,)).fetchone()
    if not asset: c.close(); flash("الأصل غير موجود.","error"); return redirect(url_for("assets.index"))
    rows=c.execute("SELECT * FROM asset_depreciations WHERE asset_id=? ORDER BY period",(asset_id,)).fetchall()
    accounts=c.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 ORDER BY code").fetchall()
    posted={x["period"]:Decimal(x["amount"]) for x in rows}
    accumulated=sum(posted.values(),Decimal("0"))
    c.close()
    cost,residual=Decimal(asset["cost"]),Decimal(asset["residual_value"])
    depreciable=cost-residual
    ready=date.fromisoformat(asset["ready_date"])
    schedule_rows=[]
    life_months=int(asset["useful_life_months"])
    period_count=life_months+(1 if ready.day>1 else 0)
    cumulative=Decimal("0")
    for offset in range(period_count):
        month_index=ready.month-1+offset
        year=ready.year+month_index//12
        month=month_index%12+1
        period=f"{year:04d}-{month:02d}"
        if cumulative >= depreciable:
            break
        try:
            planned=_asset_period_amount(asset,cumulative,period)
        except ValueError:
            break
        actual=posted.get(period)
        schedule_rows.append({"period":period,"planned":planned,"actual":actual,"posted":period in posted})
        cumulative += actual if actual is not None else planned
    return render_template("asset_schedule.html",asset=asset,rows=rows,schedule_rows=schedule_rows,accumulated=accumulated,
      net=max(cost-accumulated,residual),depreciable=depreciable,accounts=accounts)

@assets_bp.route("/<int:asset_id>/depreciate",methods=["POST"])
@require(write=True)
def depreciate(asset_id):
    c=db()
    try:
        period=request.form["period"]
        date.fromisoformat(period+"-01")
        c.execute("BEGIN IMMEDIATE")
        asset=c.execute("SELECT * FROM fixed_assets WHERE id=? AND status='active'",(asset_id,)).fetchone()
        if not asset: raise ValueError("الأصل غير موجود أو غير نشط.")
        amount,entry=_post_asset_depreciation(c,asset,period,session["user_id"])
        c.commit()
        flash("تم ترحيل قيد الإهلاك بقيمة "+str(amount),"success")
    except (ValueError,KeyError,sqlite3.Error) as e:
        c.rollback(); flash(str(e),"error")
    finally:
        c.close()
    return redirect(url_for("assets.schedule",asset_id=asset_id))


@assets_bp.route("/depreciation-run",methods=["GET","POST"])
@require(write=True)
def depreciation_run():
    c=db()
    results=[]
    period=request.values.get("period","").strip()
    if request.method=="POST":
        try:
            date.fromisoformat(period+"-01")
            c.execute("BEGIN IMMEDIATE")
            assets=c.execute("SELECT * FROM fixed_assets WHERE status='active' AND ready_date<=? ORDER BY asset_code",
                             (period+"-31",)).fetchall()
            posted=0
            skipped=[]
            for asset in assets:
                if c.execute("SELECT 1 FROM asset_depreciations WHERE asset_id=? AND period=?",
                             (asset["id"],period)).fetchone():
                    skipped.append(asset["asset_code"]+" (مرحل بالفعل)")
                    continue
                ready=date.fromisoformat(asset["ready_date"])
                current=date.fromisoformat(period+"-01")
                offset=(current.year-ready.year)*12+current.month-ready.month
                final_offset=int(asset["useful_life_months"])-(1 if ready.day==1 else 0)
                accumulated=sum((Decimal(str(row["amount"])) for row in c.execute(
                    "SELECT amount FROM asset_depreciations WHERE asset_id=?",(asset["id"],)).fetchall()),Decimal("0"))
                remaining=Decimal(asset["cost"])-Decimal(asset["residual_value"])-accumulated
                if offset>final_offset:
                    skipped.append(asset["asset_code"]+" (انتهى العمر الإنتاجي)")
                    continue
                if remaining<=0:
                    skipped.append(asset["asset_code"]+" (اكتمل الإهلاك)")
                    continue
                try:
                    amount,_entry=_post_asset_depreciation(c,asset,period,session["user_id"])
                    posted+=1
                    results.append({"asset":asset["asset_code"],"amount":amount})
                except ValueError as exc:
                    # A batch is atomic: do not silently omit assets with a gap or bad setup.
                    raise ValueError(f"{asset['asset_code']}: {exc}")
            c.commit()
            flash(f"تم ترحيل إهلاك {posted} أصلًا عن {period}. تم تجاوز {len(skipped)} أصلًا سبق ترحيلها.","success")
        except (ValueError,KeyError,sqlite3.Error) as e:
            c.rollback(); results=[]; flash(str(e),"error")
    c.close()
    return render_template("asset_depreciation_run.html",period=period,results=results)


@assets_bp.route("/disposal/<int:asset_id>",methods=["POST"])
@require(write=True)
def dispose(asset_id):
    c=db()
    try:
        disposal_date=date.fromisoformat(request.form["disposal_date"]).isoformat()
        proceeds_account_id=int(request.form["proceeds_account_id"])
        gain_loss_account_id=int(request.form["gain_loss_account_id"])
        output_vat_account_raw=request.form.get("output_vat_account_id","").strip()
        output_vat_account_id=int(output_vat_account_raw) if output_vat_account_raw else None
        proceeds=Decimal(request.form.get("proceeds_amount","0"))
        vat=Decimal(request.form.get("output_vat_amount","0"))
        reason=request.form.get("reason","").strip()
        if not proceeds.is_finite() or not vat.is_finite() or proceeds<0 or vat<0 or vat>proceeds:
            raise ValueError("قيمة المتحصلات والضريبة غير صحيحة؛ يجب ألا تتجاوز الضريبة إجمالي المتحصلات.")
        if vat and not output_vat_account_id:
            raise ValueError("اختر حساب ضريبة المخرجات عند إدخال ضريبة على بيع الأصل.")
        c.execute("BEGIN IMMEDIATE")
        asset=c.execute("SELECT * FROM fixed_assets WHERE id=? AND status='active'",(asset_id,)).fetchone()
        if not asset: raise ValueError("الأصل غير موجود أو سبق استبعاده.")
        if disposal_date < asset["ready_date"]:
            raise ValueError("تاريخ الاستبعاد لا يمكن أن يسبق جاهزية الأصل.")
        def checked_account(account_id, allowed_types, label):
            row=c.execute("SELECT account_type,is_group,active FROM accounts WHERE id=?",(account_id,)).fetchone()
            if not row or row["is_group"] or not row["active"] or row["account_type"] not in allowed_types:
                raise ValueError("اختر "+label+" تفصيليًا ونشطًا ومن النوع المحاسبي الصحيح.")
        checked_account(proceeds_account_id,{"asset"},"حساب النقدية أو العملاء")
        checked_account(gain_loss_account_id,{"revenue","expense"},"حساب أرباح/خسائر الاستبعاد")
        if output_vat_account_id:
            checked_account(output_vat_account_id,{"liability"},"حساب ضريبة المخرجات")
        period=disposal_date[:7]
        already_depreciated=c.execute("SELECT 1 FROM asset_depreciations WHERE asset_id=? AND period=?",
                                       (asset_id,period)).fetchone()
        if already_depreciated:
            last_day=calendar.monthrange(date.fromisoformat(disposal_date).year,
                                         date.fromisoformat(disposal_date).month)[1]
            if date.fromisoformat(disposal_date).day != last_day:
                raise ValueError("تم ترحيل إهلاك شهر الاستبعاد بالكامل؛ لا يمكن استبعاد الأصل في منتصف الشهر دون عكس القيد. استخدم تاريخ آخر يوم بالشهر أو عالج القيد بعكس معتمد.")
        else:
            # Recognize depreciation only through the actual disposal date.
            _amount,_entry=_post_asset_depreciation(c,asset,period,session["user_id"],cutoff_date=disposal_date)
        accumulated=sum((Decimal(str(x["amount"])) for x in c.execute(
            "SELECT amount FROM asset_depreciations WHERE asset_id=?",(asset_id,)).fetchall()),Decimal("0"))
        cost=Decimal(asset["cost"])
        nbv=max(cost-accumulated,Decimal(asset["residual_value"]))
        net_proceeds=proceeds-vat
        gain_loss=net_proceeds-nbv
        lines=[
            (proceeds_account_id,proceeds,0,"متحصلات استبعاد الأصل "+asset["asset_name"]),
            (asset["accum_account_id"],accumulated,0,"إقفال مجمع إهلاك "+asset["asset_name"]),
            (asset["asset_account_id"],0,cost,"استبعاد تكلفة الأصل "+asset["asset_name"]),
        ]
        if vat:
            lines.append((output_vat_account_id,0,vat,"ضريبة مخرجات بيع الأصل"))
        if gain_loss>0:
            lines.append((gain_loss_account_id,0,gain_loss,"ربح استبعاد الأصل "+asset["asset_name"]))
        elif gain_loss<0:
            lines.append((gain_loss_account_id,-gain_loss,0,"خسارة استبعاد الأصل "+asset["asset_name"]))
        entry=post_journal(c,disposal_date,"ASSET-DISPOSAL-"+str(asset_id),
            "استبعاد الأصل "+asset["asset_code"],lines,session["user_id"])
        cur=c.execute("""INSERT INTO asset_disposals(asset_id,disposal_date,proceeds_account_id,
            proceeds_amount,output_vat_account_id,output_vat_amount,gain_loss_account_id,
            gain_loss_amount,net_book_value,journal_entry_id,reason,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (asset_id,disposal_date,proceeds_account_id,str(proceeds),output_vat_account_id,
             str(vat),gain_loss_account_id,str(gain_loss),str(nbv),entry,reason,session["user_id"]))
        c.execute("UPDATE fixed_assets SET status='disposed' WHERE id=?",(asset_id,))
        g.audit_created_record=("asset_disposals",cur.lastrowid,None,None)
        c.commit()
        flash("تم استبعاد الأصل وترحيل قيد البيع/الاستبعاد. ربح أو خسارة الاستبعاد: "+str(gain_loss),"success")
    except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e:
        c.rollback(); flash(str(e),"error")
    finally:
        c.close()
    return redirect(url_for("assets.index"))


@assets_bp.route("/reconciliation")
@require()
def reconciliation():
    as_of=request.args.get("as_of",date.today().isoformat())
    try:
        as_of=date.fromisoformat(as_of).isoformat()
    except ValueError:
        as_of=date.today().isoformat()
    c=db()
    rows=[]
    try:
        assets=c.execute("""SELECT f.id,f.asset_account_id,f.accum_account_id,f.cost,
            x.disposal_date
            FROM fixed_assets f LEFT JOIN asset_disposals x ON x.asset_id=f.id
            WHERE f.purchase_date<=? AND (x.id IS NULL OR x.disposal_date>?)
            ORDER BY f.asset_account_id,f.accum_account_id,f.id""",(as_of,as_of)).fetchall()
        grouped={}
        for asset_row in assets:
            key=(asset_row["asset_account_id"],asset_row["accum_account_id"])
            item=grouped.setdefault(key,{"gross_cost":Decimal("0"),"accumulated":Decimal("0")})
            item["gross_cost"]+=Decimal(str(asset_row["cost"]))
            depreciation_rows=c.execute("""SELECT d.amount FROM asset_depreciations d
                JOIN journal_entries je ON je.id=d.journal_entry_id
                WHERE d.asset_id=? AND je.status='posted' AND je.entry_date<=?""",
                (asset_row["id"],as_of)).fetchall()
            item["accumulated"]+=sum((Decimal(str(x["amount"])) for x in depreciation_rows),Decimal("0"))
        for (asset_account_id,accum_account_id),item in grouped.items():
            for account_id,kind,expected in (
                (asset_account_id,"تكلفة الأصول",item["gross_cost"]),
                (accum_account_id,"مجمع الإهلاك",item["accumulated"]),
            ):
                gl=c.execute("""SELECT COALESCE(DECIMAL_NET(jl.debit,jl.credit),0) balance
                    FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                    WHERE jl.account_id=? AND je.status='posted' AND je.entry_date<=?""",
                    (account_id,as_of)).fetchone()
                raw=Decimal(str(gl["balance"] or "0"))
                gl_balance=raw if kind=="تكلفة الأصول" else -raw
                account=c.execute("SELECT code,name_ar FROM accounts WHERE id=?",(account_id,)).fetchone()
                rows.append({"account_id":account_id,"code":account["code"],"name":account["name_ar"],
                    "kind":kind,"register":expected,"gl":gl_balance,"difference":gl_balance-expected})
    finally:
        c.close()
    return render_template("asset_reconciliation.html",rows=rows,as_of=as_of,
        difference_total=sum((abs(x["difference"]) for x in rows),Decimal("0")))
