from database import connect_database
"""Budget planning and cash-flow reporting for NEXUS ERP."""
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

planning_bp = Blueprint("planning", __name__)
CENT = Decimal("0.01")


def _money(value):
    amount = Decimal(str(value or "0"))
    if not amount.is_finite() or amount < 0:
        raise ValueError("أدخل مبلغًا رقميًا موجبًا أو صفرًا.")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def initialize_planning_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS budget_plans(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, date_from TEXT NOT NULL,
      date_to TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS budget_lines(
      id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budget_plans(id) ON DELETE CASCADE,
      account_id INTEGER NOT NULL REFERENCES accounts(id), amount TEXT NOT NULL,
      UNIQUE(budget_id,account_id)
    );
    CREATE INDEX IF NOT EXISTS idx_budget_period ON budget_plans(date_from,date_to,status);
    """)


def _require(permission):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            allowed = set(session.get("permissions", ()))
            # Existing installations may have saved permission JSON before
            # cash-flow reporting was introduced. Treat report-view access as
            # sufficient to discover and open this financial report.
            if permission not in allowed and not (
                permission == "cashflow.view" and "reports.view" in allowed
            ):
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorate


def _connect():
    from pathlib import Path
    return connect_database(Path(__file__).resolve().parent / "data" / "nexus.db")


@planning_bp.route("/budget", methods=["GET", "POST"])
@_require("budget.view")
def budget_index():
    conn = _connect()
    if request.method == "POST":
        if "budget.manage" not in set(session.get("permissions", ())):
            conn.close()
            abort(403)
        try:
            name = request.form.get("name", "").strip()
            start = request.form.get("date_from", "")
            end = request.form.get("date_to", "")
            from datetime import date
            date.fromisoformat(start); date.fromisoformat(end)
            if not name or len(name) > 120 or end < start:
                raise ValueError("أدخل اسمًا وفترة موازنة صحيحة.")
            ids = request.form.getlist("account_id")
            if not ids:
                raise ValueError("أضف حسابًا واحدًا على الأقل وحدد موازنته.")
            lines = []
            for account_id in ids:
                raw_amount = request.form.get("amount_" + str(account_id), "0")
                account = conn.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?", (int(account_id),)).fetchone()
                if not account or account["is_group"] or not account["active"] or account["account_type"] not in {"revenue","expense"}:
                    raise ValueError("الموازنة تقبل حسابات الإيرادات والمصروفات التفصيلية النشطة فقط.")
                lines.append((int(account_id), str(_money(raw_amount))))
            if len({x[0] for x in lines}) != len(lines):
                raise ValueError("لا تكرر الحساب داخل الموازنة نفسها.")
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("INSERT INTO budget_plans(name,date_from,date_to,created_by) VALUES(?,?,?,?)",
                               (name,start,end,session["user_id"]))
            conn.executemany("INSERT INTO budget_lines(budget_id,account_id,amount) VALUES(?,?,?)",
                             [(cur.lastrowid, aid, amount) for aid,amount in lines])
            conn.commit()
            flash("تم حفظ الموازنة بنجاح.", "success")
            return redirect(url_for("planning.budget_detail", budget_id=cur.lastrowid))
        except (ValueError, TypeError, InvalidOperation, sqlite3.IntegrityError) as exc:
            conn.rollback()
            flash(str(exc) or "تعذر حفظ الموازنة.", "error")
    plans = conn.execute("SELECT * FROM budget_plans ORDER BY date_from DESC,id DESC").fetchall()
    accounts = conn.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 AND account_type IN ('revenue','expense') ORDER BY account_type,code").fetchall()
    conn.close()
    return render_template("budget.html", plans=plans, accounts=accounts)


@planning_bp.route("/budget/<int:budget_id>")
@_require("budget.view")
def budget_detail(budget_id):
    conn = _connect()
    plan = conn.execute("SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
    if not plan:
        conn.close(); abort(404)
    rows = conn.execute("""
      SELECT a.id,a.code,a.name_ar,a.account_type,b.amount,
        COALESCE((SELECT DECIMAL_NET(jl.credit,jl.debit) FROM journal_lines jl
          JOIN journal_entries je ON je.id=jl.entry_id
          WHERE jl.account_id=a.id AND je.status='posted'
            AND je.entry_date>=? AND je.entry_date<=?), '0') actual
      FROM budget_lines b JOIN accounts a ON a.id=b.account_id
      WHERE b.budget_id=? ORDER BY a.account_type,a.code
    """, (plan["date_from"],plan["date_to"],budget_id)).fetchall()
    results=[]
    for row in rows:
        budget=_money(row["amount"])
        raw=Decimal(str(row["actual"] or "0"))
        actual=raw if row["account_type"]=="revenue" else -raw
        variance=actual-budget
        results.append({**dict(row),"budget":budget,"actual":actual,"variance":variance,
                        "variance_pct":(variance*100/budget) if budget else None})
    conn.close()
    return render_template("budget_detail.html",plan=plan,rows=results,
                           revenue_budget=sum((x["budget"] for x in results if x["account_type"]=="revenue"),Decimal("0")),
                           revenue_actual=sum((x["actual"] for x in results if x["account_type"]=="revenue"),Decimal("0")),
                           expense_budget=sum((x["budget"] for x in results if x["account_type"]=="expense"),Decimal("0")),
                           expense_actual=sum((x["actual"] for x in results if x["account_type"]=="expense"),Decimal("0")))



def build_balance_sheet_snapshot(conn, as_of):
    rows = conn.execute("""SELECT a.id,a.code,a.name_ar,a.account_type,a.parent_id,a.is_group,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),0) debit,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),0) credit
      FROM accounts a
      LEFT JOIN journal_lines jl ON jl.account_id=a.id
      LEFT JOIN journal_entries je ON je.id=jl.entry_id AND je.status='posted' AND je.entry_date<=?
      GROUP BY a.id ORDER BY a.code""", (as_of,)).fetchall()
    accounts = {}
    for row in rows:
        debit=Decimal(str(row["debit"] or "0"))
        credit=Decimal(str(row["credit"] or "0"))
        kind=row["account_type"]
        balance=debit-credit if kind=="asset" or kind=="expense" else credit-debit
        accounts[row["id"]]={**dict(row),"balance":balance}
    totals={"asset":Decimal("0"),"liability":Decimal("0"),"equity":Decimal("0"),
            "revenue":Decimal("0"),"expense":Decimal("0")}
    for item in accounts.values():
        if item["is_group"]:
            continue
        totals[item["account_type"]]+=item["balance"]
    net_profit=totals["revenue"]-totals["expense"]
    group_totals={account_id:Decimal("0") for account_id,item in accounts.items() if item["is_group"]}
    for account_id,item in accounts.items():
        if item["is_group"]:
            continue
        parent=item["parent_id"]
        seen=set()
        while parent and parent in accounts and parent not in seen:
            seen.add(parent)
            if accounts[parent]["is_group"]:
                group_totals[parent]+=item["balance"]
            parent=accounts[parent]["parent_id"]
    children={}
    for item in accounts.values():
        children.setdefault(item["parent_id"],[]).append(item)
    sections={}
    for kind in ("asset","liability","equity"):
        flat=[]
        def walk(parent_id,depth=0,seen=None):
            seen=seen or set()
            for item in sorted(children.get(parent_id,[]),key=lambda x:x["code"]):
                if item["account_type"]!=kind or item["id"] in seen:
                    continue
                value=group_totals.get(item["id"],Decimal("0")) if item["is_group"] else item["balance"]
                flat.append({**item,"display_balance":value,"depth":depth})
                walk(item["id"],depth+1,seen|{item["id"]})
        walk(None)
        sections[kind]=flat
    assets=totals["asset"]
    liabilities=totals["liability"]
    equity=totals["equity"]
    total_equity=equity+net_profit
    return {"sections":sections,"total_assets":assets,"total_liabilities":liabilities,
            "equity_before_profit":equity,"net_profit":net_profit,"total_equity":total_equity,
            "balance_difference":assets-liabilities-total_equity,
            "balanced":assets==liabilities+total_equity,"as_of":as_of}


@planning_bp.route("/reports/balance-sheet")
@_require("reports.view")
def balance_sheet():
    from datetime import date
    raw=request.args.get("as_of","").strip()
    try:
        as_of=date.fromisoformat(raw) if raw else date.today()
    except ValueError:
        flash("تاريخ قائمة المركز المالي غير صحيح.","error")
        as_of=date.today()
    conn=_connect()
    try:
        snapshot=build_balance_sheet_snapshot(conn,as_of.isoformat())
    finally:
        conn.close()
    return render_template("balance_sheet.html",s=snapshot)


@planning_bp.route("/reports/cash-flow")
@_require("cashflow.view")
def cash_flow():
    from datetime import date
    raw_from=request.args.get("date_from","").strip()
    raw_to=request.args.get("date_to","").strip()
    today=date.today()
    try:
        start=date.fromisoformat(raw_from) if raw_from else today.replace(day=1)
        end=date.fromisoformat(raw_to) if raw_to else today
        if end<start: raise ValueError()
    except ValueError:
        flash("فترة التقرير غير صحيحة.","error")
        start,end=today.replace(day=1),today
    conn=_connect()
    try:
        role_rows=conn.execute("SELECT account_id FROM management_account_roles WHERE role='cash'").fetchall()
        cash_ids={r["account_id"] for r in role_rows}
        custom_mapping=conn.execute("SELECT meta_value FROM management_dashboard_meta WHERE meta_key='roles_customized'").fetchone()
        if not cash_ids and not (custom_mapping and custom_mapping["meta_value"]=="1"):
            cash_ids={r["id"] for r in conn.execute(
              "SELECT id FROM accounts WHERE is_group=0 AND (code IN ('1110','1120') OR name_ar LIKE '%صندوق%' OR name_ar LIKE '%بنك%')")}
        if not cash_ids:
            flash("حدد حسابات الصندوق والبنوك من شاشة ربط حسابات لوحة الإدارة أولًا.","warning")
            opening=closing=Decimal("0"); sections={k:{"in":Decimal("0"),"out":Decimal("0")} for k in ("operating","investing","financing")}
        else:
            marks=",".join("?" for _ in cash_ids)
            ids=tuple(sorted(cash_ids))
            opening_row=conn.execute(f"""SELECT COALESCE(DECIMAL_NET(jl.debit,jl.credit),0) value
              FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
              WHERE jl.account_id IN ({marks}) AND je.status='posted' AND je.entry_date<?""",(*ids,start.isoformat())).fetchone()
            opening=Decimal(str(opening_row["value"] or "0"))
            sections={k:{"in":Decimal("0"),"out":Decimal("0")} for k in ("operating","investing","financing")}
            entries=conn.execute(f"""SELECT je.id,
                COALESCE(DECIMAL_NET(CASE WHEN jl.account_id IN ({marks}) THEN jl.debit ELSE '0' END,
                                     CASE WHEN jl.account_id IN ({marks}) THEN jl.credit ELSE '0' END),0) cash_net
              FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
              WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
              GROUP BY je.id ORDER BY je.entry_date,je.id""",(*ids,*ids,start.isoformat(),end.isoformat())).fetchall()
            for entry in entries:
                net=Decimal(str(entry["cash_net"] or "0"))
                if not net: continue
                counterpart=conn.execute(f"""SELECT a.account_type,a.code,a.name_ar,jl.debit,jl.credit
                    FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                    WHERE jl.entry_id=? AND jl.account_id NOT IN ({marks})""",(entry["id"],*ids)).fetchall()
                weights={"operating":Decimal("0"),"investing":Decimal("0"),"financing":Decimal("0")}
                for item in counterpart:
                    code=item["code"]; name=item["name_ar"]
                    if item["account_type"]=="asset" and not (code.startswith(("11","21")) or "عميل" in name or "مخزون" in name or "مقدم" in name):
                        category="investing"
                    elif item["account_type"] == "equity" or (item["account_type"] == "liability" and (code.startswith("22") or "قرض" in name)):
                        category="financing"
                    else:
                        category="operating"
                    weights[category]+=abs(Decimal(str(item["debit"] or "0")))+abs(Decimal(str(item["credit"] or "0")))
                total_weight=sum(weights.values(),Decimal("0"))
                if not total_weight: weights["operating"]=Decimal("1"); total_weight=Decimal("1")
                remaining=abs(net)
                active=[k for k,v in weights.items() if v]
                for category in active[:-1]:
                    part=(abs(net)*weights[category]/total_weight).quantize(CENT,rounding=ROUND_HALF_UP)
                    remaining-=part
                    if net>0: sections[category]["in"]+=part
                    else: sections[category]["out"]+=part
                category=active[-1]
                if net>0: sections[category]["in"]+=remaining
                else: sections[category]["out"]+=remaining
            movement=sum((x["in"]-x["out"] for x in sections.values()),Decimal("0"))
            closing=opening+movement
    finally:
        conn.close()
    total_in=sum((x["in"] for x in sections.values()),Decimal("0"))
    total_out=sum((x["out"] for x in sections.values()),Decimal("0"))
    net_change=closing-opening
    reconciliation_difference=net_change-(total_in-total_out)
    return render_template("cash_flow.html",date_from=start.isoformat(),date_to=end.isoformat(),
        opening=opening,closing=closing,sections=sections,total_in=total_in,total_out=total_out,
        net_change=net_change,reconciliation_difference=reconciliation_difference,
        cash_mapping_available=bool(cash_ids),
        cash_reconciles=(bool(cash_ids) and reconciliation_difference==0))


__all__=["planning_bp","initialize_planning_schema"]
