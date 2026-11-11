"""Customer and supplier receivables/payables aging reports."""
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from functools import wraps

aging_bp=Blueprint("aging",__name__,url_prefix="/reports/aging")
DB_PATH=Path(__file__).resolve().parent/"data"/"nexus.db"

def db():
 c=sqlite3.connect(DB_PATH,timeout=20); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c

def initialize_aging_schema():
 c=db()
 for table in ("sales_invoices","supplier_bills"):
  cols={r["name"] for r in c.execute("PRAGMA table_info("+table+")")}
  if "due_date" not in cols: c.execute("ALTER TABLE "+table+" ADD COLUMN due_date TEXT")
 c.execute("UPDATE sales_invoices SET due_date=invoice_date WHERE due_date IS NULL OR due_date=''")
 c.execute("UPDATE supplier_bills SET due_date=bill_date WHERE due_date IS NULL OR due_date=''")
 c.commit(); c.close()

def auth(fn):
 @wraps(fn)
 def wrapped(*a,**kw):
  if not session.get("user_id"): return redirect(url_for("login"))
  kind=request.args.get("kind","customers")
  permission="purchases.view" if kind=="suppliers" else "sales.view"
  allowed=(permission in set(session["permissions"])) if "permissions" in session else (session.get("role") in {"admin","accountant","viewer"})
  if not allowed:
   flash("لا تملك صلاحية عرض تقرير أعمار الديون.","error")
   return redirect(url_for("dashboard"))
  return fn(*a,**kw)
 return wrapped

def _amount(value):
 try:
  amount=Decimal(str(value or "0"))
 except (InvalidOperation,ValueError,TypeError):
  raise ValueError("يوجد مبلغ غير صالح في مستندات الأعمار.")
 if not amount.is_finite():
  raise ValueError("يوجد مبلغ غير محدود في مستندات الأعمار.")
 return amount

@aging_bp.route("/")
@auth
def index():
 kind=request.args.get("kind","customers")
 if kind not in {"customers","suppliers"}: kind="customers"
 try:
  as_of=request.args.get("as_of") or date.today().isoformat()
  today=date.fromisoformat(as_of)
  as_of=today.isoformat()
 except ValueError:
  flash("تاريخ التقرير غير صحيح.","error")
  today=date.today(); as_of=today.isoformat()
 c=db()
 if kind=="customers":
  rows=c.execute("""SELECT i.id invoice_id,i.invoice_no doc_no,cu.code party_code,cu.name party_name,i.invoice_date doc_date,
    COALESCE(i.due_date,i.invoice_date) due_date,i.total_amount total_amount,i.paid_amount paid_amount,
    COALESCE(i.credited_amount,'0') credited_amount
    FROM sales_invoices i JOIN customers cu ON cu.id=i.customer_id
    WHERE i.status='posted' AND i.invoice_date<=?
    ORDER BY due_date,cu.name""",(as_of,)).fetchall()
 else:
  rows=c.execute("""SELECT b.bill_no doc_no,s.code party_code,s.name party_name,b.bill_date doc_date,
    COALESCE(b.due_date,b.bill_date) due_date,b.total_amount total_amount,b.paid_amount paid_amount,
    COALESCE(b.credited_amount,'0') credited_amount
    FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
    WHERE b.status='posted' AND b.bill_date<=?
    ORDER BY due_date,s.name""",(as_of,)).fetchall()
 refunds_by_invoice={}
 if kind=="customers" and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_refunds'").fetchone():
  for refund in c.execute("""SELECT cn.invoice_id,rf.amount FROM customer_refunds rf
      JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id
      WHERE cn.status='posted' AND rf.refund_date<=?""",(as_of,)).fetchall():
   refunds_by_invoice[refund["invoice_id"]]=refunds_by_invoice.get(refund["invoice_id"],Decimal("0"))+_amount(refund["amount"])
 c.close()
 result=[]; totals=[Decimal("0.00") for _ in range(5)]
 for r in rows:
  refund_amount = refunds_by_invoice.get(r["invoice_id"], Decimal("0")) if kind=="customers" else Decimal("0")
  balance=_amount(r["total_amount"])-_amount(r["paid_amount"])-_amount(r["credited_amount"] if "credited_amount" in r.keys() else "0")+refund_amount
  if balance<=0:
   continue
  try:
   due_date=date.fromisoformat(r["due_date"])
  except (TypeError,ValueError):
   due_date=today
  days=(today-due_date).days
  bucket=0 if days<=0 else 1 if days<=30 else 2 if days<=60 else 3 if days<=90 else 4
  vals=[Decimal("0.00") for _ in range(5)]; vals[bucket]=balance
  for i,v in enumerate(vals): totals[i]+=v
  result.append({**dict(r),"balance":balance,"days":max(days,0),"buckets":vals})
 return render_template("aging_report.html",kind=kind,as_of=as_of,rows=result,totals=totals)
